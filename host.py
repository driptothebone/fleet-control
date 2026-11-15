"""Fleet Control routing engine: pure planning over saved profile documents.

No Hermes internals. The dashboard adapter (dashboard/plugin_api.py) reads and
writes every profile through Hermes's public dashboard routes and hands this
engine plain data: saved YAML documents, shipped defaults, host model catalogs
and Fast capability flags. This module never opens files, credentials or sockets.
"""
import copy
import hashlib
import re
import secrets
import threading
import time

from hermes_constants import VALID_REASONING_EFFORTS

ROUTE_ID = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_./:@+\[\]-]{0,199}$')
SECRET_PREFIX = re.compile(r'^(?:sk[-_]|xai-|hf_|gh[pousr]_)', re.I)
EFFORTS = tuple(dict.fromkeys(('none', *VALID_REASONING_EFFORTS)))
# Stored agent.service_tier words Hermes understands (display validation only).
TIER_WORDS = frozenset({'', 'normal', 'default', 'standard', 'off', 'none',
                        'fast', 'priority', 'on', 'ultrafast', 'ultra', 'auto', 'cold'})
TASK = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
OVERRIDES = {'base_url', 'api_url', 'api_key', 'api', 'key_env', 'api_key_env', 'api_mode', 'extra_headers'}
PREVIEW_SECONDS = 600


def safe_value(key, value):
    if key.endswith('service_tier'):
        return isinstance(value, str) and value in TIER_WORDS
    if key.endswith('max_concurrent_children'):
        return type(value) is int and 1 <= value <= 16
    if key.endswith('context_length'):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if key.endswith('reasoning_effort'):
        return value is False or (isinstance(value, str) and value in ('', *EFFORTS))
    return isinstance(value, str) and (value == '' or bool(ROUTE_ID.fullmatch(value)) and '://' not in value
                                       and not SECRET_PREFIX.match(value) and not (':' in value and '@' in value))


class BoardError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def field(doc, key):
    node = doc
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return {'present': False}
        node = node[part]
    return {'present': True, 'value': node}


def put(doc, key, value):
    parts = key.split('.'); node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise BoardError(422, 'Routing block is not a mapping')
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


def aux_defaults_from(defaults):
    aux = defaults.get('auxiliary', {}) if isinstance(defaults, dict) else {}
    return {k: v for k, v in aux.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(aux, dict) else {}


def empty_means_inherit(key, aux_defaults):
    """Keys where Hermes reads an empty string exactly like an absent key."""
    if key in ('agent.reasoning_effort', 'delegation.provider', 'delegation.model', 'delegation.reasoning_effort',
               'delegation.base_url'):
        return True
    parts = key.split('.')
    if len(parts) == 3 and parts[0] == 'auxiliary' and parts[2] == 'base_url':
        return True
    if len(parts) == 3 and parts[0] == 'auxiliary' and parts[2] == 'reasoning_effort':
        return not (aux_defaults.get(parts[1]) or {}).get('reasoning_effort')
    return False


def same(key, a, b, aux_defaults):
    if a == b:
        return True
    if empty_means_inherit(key, aux_defaults):
        blank = lambda f: not f['present'] or f['value'] == ''
        return blank(a) and blank(b)
    return False


def transition_fields(block, draft):
    """Endpoint fields a provider change must retire on a subagent/auxiliary route.

    Only the old provider's own standard endpoint may be cleared. Custom
    endpoints and any auth override are never moved, transferred or dropped.
    Main-model switches go through Hermes's own model switch instead.
    """
    if draft['route'] == 'keep':
        return ()
    if draft['route'] == 'explicit' and block.get('provider') == draft['provider']:
        return ()
    active = {k for k in OVERRIDES if block.get(k) not in ('', None)}
    if not active:
        return ()
    if active == {'base_url'} and standard_endpoint(block.get('provider'), block['base_url']):
        return ('base_url',)
    raise BoardError(422, 'Changing provider on a route with a custom endpoint/auth override needs Hermes\'s own '
                          'setup; existing credentials were not read or changed')


def safe_routes(doc):
    out = {'model': {}, 'agent': {}, 'delegation': {}, 'auxiliary': {}}
    specs = [('model', ('provider', 'default')), ('agent', ('reasoning_effort', 'service_tier')),
             ('delegation', ('provider', 'model', 'reasoning_effort', 'max_concurrent_children'))]
    aux = doc.get('auxiliary', {})
    if isinstance(aux, dict):
        specs += [('auxiliary.' + k, ('provider', 'model', 'reasoning_effort')) for k in aux if isinstance(k, str) and TASK.fullmatch(k)]
    for base, keys in specs:
        for key in keys:
            val = field(doc, base + '.' + key)
            if val['present']:
                put(out, base + '.' + key, val['value'] if safe_value(key, val['value']) else '[withheld: unsupported route value]')
    if 'fallback_providers' in doc:
        out['fallback_providers'] = safe_fallback(doc)
    return out


def uses_external_secrets(doc):
    block = doc.get('secrets')
    if not block:
        return False
    if not isinstance(block, dict):
        return True
    sources = block.get('sources') if isinstance(block.get('sources'), dict) else block
    return any(not isinstance(v, dict) or v.get('enabled') is not False for v in sources.values())


def valid_chain(chain):
    return (isinstance(chain, list) and len(chain) <= 10
            and all(isinstance(r, dict) and set(r) == {'provider', 'model'}
                    and all(isinstance(r[k], str) and r[k] and safe_value(k, r[k]) for k in ('provider', 'model')) for r in chain)
            and len({(r['provider'], r['model']) for r in chain}) == len(chain))


def fallback_lock(doc):
    if doc.get('fallback_model'):
        return 'Legacy fallback_model present; host-managed editing required'
    raw = doc.get('fallback_providers')
    if raw is not None and not valid_chain(raw):
        return 'Fallback has unsupported endpoint/auth/options or malformed entries; not changed'
    return ''


def safe_fallback(doc):
    raw = doc.get('fallback_providers')
    return copy.deepcopy(raw) if valid_chain(raw) else []


# Providers' own published API hosts. Hermes writes these into model.base_url on
# a normal model switch, so they are not custom endpoints. Anything else fails
# closed to "can't verify"; Hermes's catalog flag stays the actual Fast gate.
STANDARD_HOSTS = {'anthropic': 'api.anthropic.com', 'openai': 'api.openai.com', 'openai-api': 'api.openai.com',
                  'openai-codex': 'chatgpt.com', 'xai': 'api.x.ai'}


def standard_endpoint(provider, endpoint):
    from urllib.parse import urlsplit
    if not isinstance(endpoint, str):
        return False
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError:
        return False
    return (url.scheme == 'https' and url.hostname == STANDARD_HOSTS.get(provider) and not url.username
            and not url.password and not url.query and not url.fragment and port in (None, 443))


def fast_capability(doc, caps, catalog_status='ready'):
    """Fast availability from Hermes's own per-model catalog flag (GET /api/model/options).

    The catalog flag describes the provider's standard route, so a profile with a
    custom endpoint or custom provider definition is reported as unverifiable.
    """
    raw = field(doc, 'agent.service_tier')
    current = 'normal' if not raw['present'] or raw['value'] == '' else raw['value'] if safe_value('service_tier', raw['value']) else 'unknown'
    out = {'current': current, 'supported': False, 'reason': 'Hermes does not offer Fast for this model and provider'}
    model = doc.get('model'); providers = doc.get('providers', {})
    if not isinstance(model, dict) or not isinstance(providers, dict):
        return out
    provider, mid = model.get('provider'), model.get('default')
    if not safe_value('provider', provider) or not safe_value('model', mid) or not provider or not mid:
        return out
    entry = providers.get(provider, {})
    if not isinstance(entry, dict) or entry.get('enabled') is False:
        return out
    endpoints = [b[k] for b in (model, entry) for k in ('base_url', 'api_url', 'api', 'url') if b.get(k)]
    if doc.get('custom_providers') or not all(standard_endpoint(provider, e) for e in endpoints):
        out['reason'] = 'Fast can\'t be verified on a custom endpoint'
        return out
    if catalog_status == 'unavailable':
        out['reason'] = 'Host model catalog unavailable; Fast not verified'
        return out
    if (caps or {}).get((provider, mid), {}).get('fast') is True:
        out.update(supported=True, reason='Hermes offers Fast for this model and provider; account entitlement not checked')
    return out


def task_metadata(doc, aux_defaults):
    aux = doc.get('auxiliary', {})
    names = set(aux_defaults)
    if isinstance(aux, dict):
        names.update(k for k, v in aux.items() if isinstance(k, str) and TASK.fullmatch(k) and isinstance(v, dict))
    return [{'task': k, 'label': k.replace('_', ' ').capitalize(), 'editable': k in aux_defaults,
             'effort': isinstance(aux_defaults.get(k), dict) and 'reasoning_effort' in aux_defaults[k],
             'configured': isinstance(aux, dict) and k in aux,
             'note': '' if k in aux_defaults else 'Unknown legacy/provider task: saved state only; host metadata unavailable'}
            for k in sorted(names)]


def revision(instance, raw_text):
    """Opaque revision of the exact saved file text, salted per board instance."""
    return hashlib.sha256((instance + '\0' + raw_text).encode('utf-8')).hexdigest()


class HostBoard:
    """Preview/confirm state for one authenticated dashboard principal and roster.

    The lock only guards this board's own preview bookkeeping. It is not a
    Hermes configuration lock and does not serialize other writers.
    """

    def __init__(self, profiles, *, principal):
        self.profiles = list(profiles)
        self.principal = principal
        self.instance = secrets.token_hex(32)
        self.previews = {}
        self.catalog, self.catalog_status, self.caps = {}, {}, {}
        self.aux_defaults = {}
        self.lock = threading.Lock()
        self.active = 0
        self.last_used = time.monotonic()

    def expire_previews(self):
        now = time.monotonic()
        self.previews = {t: v for t, v in self.previews.items() if now - v['at'] < PREVIEW_SECONDS}

    def row(self, p, snap):
        if snap is None:
            return {'profile': p, 'editable': False, 'config_status': 'unreadable',
                    'reason': 'Profile configuration unreadable through the host; no repair attempted',
                    'models': [], 'catalog': {'status': 'unavailable'}}
        raw, doc = snap
        models = [{'provider': m['provider'], 'model': m['model']} for m in self.catalog.get(p, [])
                  if isinstance(m, dict) and m.get('provider') and m.get('model')
                  and safe_value('provider', m['provider']) and safe_value('model', m['model'])]
        editable = not uses_external_secrets(doc)
        return {'profile': p, 'models': models, 'routes': safe_routes(doc), 'revision': revision(self.instance, raw),
                'fallback': safe_fallback(doc), 'fallback_lock': fallback_lock(doc),
                'fast_mode': fast_capability(doc, self.caps.get(p), self.catalog_status.get(p, 'ready')),
                'tasks': task_metadata(doc, self.aux_defaults), 'editable': editable, 'config_status': 'readable',
                'reason': '' if editable else 'Read-only: external secret sources',
                'catalog': {'status': self.catalog_status.get(p, 'ready' if models else 'empty'),
                            'provenance': 'Host metadata; availability/authentication unverified'}}

    def state(self, snaps):
        return {'available': True, 'fixture': False,
                'authority': 'Authenticated local host; every save goes through Hermes\'s public dashboard routes. '
                             'Not a fleet transaction.',
                'inventory_provenance': 'Hermes /api/model/options; credentials remain with host',
                'unsupported': ['federation', 'jobs', 'remote config', 'isolated plugin workers', 'explicit endpoint/auth overrides'],
                'profiles': [self.row(p, snaps.get(p)) for p in self.profiles], 'efforts': list(EFFORTS)}

    def validate(self, d, docs):
        required = {'profiles', 'targets', 'route', 'provider', 'model', 'effort', 'expected_revisions'}
        optional = {'max_concurrent_children', 'service_tier', 'fallback'}
        if not isinstance(d, dict) or not required <= set(d) or set(d) - required - optional:
            raise BoardError(422, 'Invalid draft fields')
        limit = d.get('max_concurrent_children')
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 16):
            raise BoardError(422, 'Max subagents must be an integer from 1 to 16')
        tier = d.get('service_tier')
        if tier is not None and (not isinstance(tier, str) or tier not in ('normal', 'priority')):
            raise BoardError(422, 'Choose Normal or Fast explicitly')
        fallback = d.get('fallback')
        if fallback is not None and not valid_chain(fallback):
            raise BoardError(422, 'Fallback must contain at most 10 unique exact provider/model pairs, no extra options')
        wide = limit is not None or tier is not None or fallback is not None
        ps, ts = d['profiles'], d['targets']
        if (not isinstance(ps, list) or not ps or len(ps) > 64 or any(not isinstance(p, str) or p not in self.profiles for p in ps)
                or len(set(ps)) != len(ps)):
            raise BoardError(422, 'Select exact unique authorized profiles')
        if (not isinstance(ts, list) or (not ts and not wide) or len(ts) > 64 or any(not isinstance(t, str) for t in ts)
                or len(set(ts)) != len(ts)):
            raise BoardError(422, 'Select unique supported targets')
        if not ts and (d['route'] != 'keep' or d['effort'] is not None):
            raise BoardError(422, 'Route and effort require real targets')
        allowed = {'main', 'delegation', 'auxiliary.*'} | {'auxiliary.' + k for k in self.aux_defaults if TASK.fullmatch(k)}
        for p in ps:
            doc = docs.get(p)
            if doc is None:
                raise BoardError(503, p + ': profile configuration unreadable through the host')
            if uses_external_secrets(doc):
                raise BoardError(422, p + ': read-only external secret sources')
            if fallback is not None:
                if fallback_lock(doc):
                    raise BoardError(422, p + ': ' + fallback_lock(doc))
                persisted = doc.get('fallback_providers') or []
                if any(pair not in persisted and (self.catalog_status.get(p) == 'unavailable' or pair not in self.catalog.get(p, []))
                       for pair in fallback):
                    raise BoardError(422, p + ': new fallback entry unavailable in host catalog')
            if any(t not in allowed for t in ts):
                raise BoardError(422, 'Auxiliary target not declared in every selected profile')
        route, effort = d['route'], d['effort']
        if route not in ('keep', 'explicit', 'inherit', 'auto') or effort not in (None, '', *EFFORTS):
            raise BoardError(422, 'Invalid route or effort')
        if any(not isinstance(d[k], str) for k in ('provider', 'model')):
            raise BoardError(422, 'Exact provider/model strings required')
        if route == 'explicit':
            if not safe_value('provider', d['provider']) or not safe_value('model', d['model']):
                raise BoardError(422, 'Unsupported provider/model identifiers')
            pair = {'provider': d['provider'], 'model': d['model']}
            reasons = [p + ': ' + ('host catalog unavailable' if self.catalog_status.get(p) == 'unavailable' else 'selection not in host catalog')
                       for p in ps if pair not in self.catalog.get(p, []) or self.catalog_status.get(p) == 'unavailable']
            if reasons:
                raise BoardError(422, '; '.join(reasons))
        elif d['provider'] or d['model']:
            raise BoardError(422, 'Nonexplicit routes require empty provider/model')
        if ('main' in ts and route in ('inherit', 'auto')) or ('delegation' in ts and route == 'auto'):
            raise BoardError(422, 'Unsupported route for selected target')
        if route == 'keep' and effort is None and not wide:
            raise BoardError(422, 'No requested change')
        revs = d['expected_revisions']
        if not isinstance(revs, dict) or set(revs) != set(ps) or any(not isinstance(v, str) for v in revs.values()):
            raise BoardError(422, 'Exact profile revisions required')

    def plan(self, p, doc, d):
        aux_defaults = self.aux_defaults
        after, changes = copy.deepcopy(doc), []

        def change(key, value):
            old = field(doc, key); new = {'present': False} if value is None else {'present': True, 'value': value}
            if old['present'] and not safe_value(key, old['value']):
                raise BoardError(422, 'Stored route has unsupported value; withheld, not changed')
            if not same(key, old, new, aux_defaults):
                changes.append({'field': key, 'before': old, 'after': new}); put(after, key, value)

        targets = []
        for target in d['targets']:
            if target == 'auxiliary.*':
                targets.extend('auxiliary.' + k for k in aux_defaults
                               if d['route'] == 'explicit' or (isinstance(doc.get('auxiliary'), dict) and k in doc['auxiliary']))
            else:
                targets.append(target)
        for target in dict.fromkeys(targets):
            # Missing auxiliary blocks already use host defaults; reset presets
            # must not manufacture blocks for a no-op.
            if target.startswith('auxiliary.') and not field(doc, target)['present'] and d['route'] in ('inherit', 'auto') and d['effort'] is None:
                continue
            base = 'model' if target == 'main' else target
            mk = 'default' if target == 'main' else 'model'
            eb = 'agent' if target == 'main' else target
            block = field(doc, base)
            if block['present'] and not isinstance(block['value'], dict):
                raise BoardError(422, 'Routing block is not a mapping')
            if block['present'] and target != 'main':
                for name in transition_fields(block['value'], d):
                    # The browser sees the effect, never the endpoint value.
                    changes.append({'field': base + '.' + name, 'before': {'present': True, 'value': '[host-managed endpoint]'},
                                    'after': {'present': False}})
                    put(after, base + '.' + name, None)
            if target.startswith('auxiliary.') and d['effort'] is not None:
                task = target.split('.', 1)[1]
                definition = aux_defaults.get(task)
                if not isinstance(definition, dict) or 'reasoning_effort' not in definition:
                    raise BoardError(422, 'Selected auxiliary task does not honor its own reasoning effort; no fields changed')
                if task == 'background_review':
                    main = doc.get('model', {}) if isinstance(doc.get('model'), dict) else {}
                    candidate = {'provider': d['provider'], 'model': d['model']} if d['route'] == 'explicit' else (block['value'] if block['present'] else {})
                    if (d['route'] in ('inherit', 'auto') or candidate.get('provider') in (None, '', 'main', 'auto')
                            or (candidate.get('provider'), candidate.get('model')) == (main.get('provider'), main.get('default'))):
                        raise BoardError(422, 'Background review inherits main reasoning on this route; effort-only change refused')
            if d['route'] == 'explicit':
                change(base + '.provider', d['provider']); change(base + '.' + mk, d['model'])
            elif d['route'] in ('inherit', 'auto'):
                change(base + '.provider', None if target == 'delegation' else ('main' if d['route'] == 'inherit' else 'auto'))
                change(base + '.' + mk, None if target == 'delegation' else '')
            if (target == 'main' and d['route'] == 'explicit' and block['present'] and 'context_length' in block['value']
                    and (block['value'].get('provider'), block['value'].get('default')) != (d['provider'], d['model'])):
                change('model.context_length', None)
            if d['effort'] is not None:
                default = aux_defaults.get(target.split('.', 1)[1], {}).get('reasoning_effort') if target.startswith('auxiliary.') else None
                change(eb + '.reasoning_effort', d['effort'] if d['effort'] or default else None)
        if d.get('max_concurrent_children') is not None:
            change('delegation.max_concurrent_children', d['max_concurrent_children'])
        if d.get('service_tier') is not None:
            if d['service_tier'] == 'priority' and not fast_capability(after, self.caps.get(p), self.catalog_status.get(p, 'ready'))['supported']:
                raise BoardError(422, p + ': Fast unsupported for this profile\'s model route')
            key = 'agent.service_tier'; old = field(doc, key); value = 'priority' if d['service_tier'] == 'priority' else ''
            if old['present'] and not safe_value(key, old['value']):
                raise BoardError(422, 'Stored service tier unsupported; withheld, not changed')
            new = {'present': True, 'value': value}
            if old != new and not (value == '' and (not old['present'] or old['value'] in ('', 'normal'))):
                changes.append({'field': key, 'before': old, 'after': new}); put(after, key, value)
        if d.get('fallback') is not None:
            key = 'fallback_providers'; old = field(doc, key); value = d['fallback']; new = {'present': True, 'value': value}
            if old != new and not (not value and (not old['present'] or old['value'] is None or old['value'] == [])):
                changes.append({'field': key, 'before': old, 'after': new}); put(after, key, value)
        return after, changes

    def preview(self, d, snaps):
        with self.lock:
            docs = {p: s[1] for p, s in snaps.items() if s is not None}
            self.validate(d, docs)
            plans = {}
            for p in d['profiles']:
                raw, doc = snaps[p]
                before = revision(self.instance, raw)
                if before != d['expected_revisions'][p]:
                    raise BoardError(409, 'Profile changed; reload and preview again')
                plans[p] = {'before': before, 'changes': self.plan(p, doc, d)[1]}
            self.expire_previews()
            if len(self.previews) >= 128:
                raise BoardError(429, 'Too many pending previews')
            token = secrets.token_urlsafe(32)
            self.previews[token] = {'at': time.monotonic(), 'draft': copy.deepcopy(d), 'plans': plans}
            main = any(c['field'] in ('model.provider', 'model.default') for v in plans.values() for c in v['changes'])
            warning = ('Fast may consume more usage or cost more. ' if d.get('service_tier') == 'priority' else '')
            warning += ('Main model changes use Hermes\'s own model switch. It validates the choice, sets that route\'s endpoint '
                        'and may ask for a cost confirmation. ' if main else '')
            warning += ('Sequential saves through Hermes\'s public routes. Each profile is rechecked right before saving, '
                        'but Hermes has no conditional save yet, so a change in that instant is not caught. Not a fleet transaction.')
            return {'fixture': False, 'token': token, 'profiles': [{'profile': p, 'changes': v['changes']} for p, v in plans.items()],
                    'warning': warning}

    def consume(self, d, token, confirmed):
        with self.lock:
            if confirmed is not True or not isinstance(token, str) or token not in self.previews:
                raise BoardError(409, 'Explicit confirmation and current preview required')
            pending = self.previews.pop(token)  # one confirmed attempt, even when validation fails
            if time.monotonic() - pending['at'] > PREVIEW_SECONDS or d != pending['draft']:
                raise BoardError(409, 'Preview changed or expired')
            return pending

    def verify(self, saved, changes):
        return all(same(c['field'], field(saved, c['field']), c['after'], self.aux_defaults) for c in changes)


def write_steps(changes, after, aux_slots):
    """Translate planned field changes into public route calls, in order.

    Main provider/model goes through POST /api/model/set (Hermes's own switch).
    Auxiliary provider/model on a host-known slot goes through model/set too.
    Everything else is one partial PUT /api/config. Removals become the value
    Hermes reads as inherit: empty strings, or model_context_length 0.
    """
    keys = {c['field'] for c in changes}
    steps, handled = [], set()
    if keys & {'model.provider', 'model.default'}:
        model = after.get('model', {})
        steps.append(('model_set', {'scope': 'main', 'provider': model.get('provider', ''), 'model': model.get('default', '')}))
        handled |= {'model.provider', 'model.default'}
    for task in sorted({k.split('.')[1] for k in keys if k.startswith('auxiliary.') and k.split('.')[2] in ('provider', 'model')}):
        if task not in aux_slots:
            continue
        block = field(after, 'auxiliary.' + task)['value']
        body = {'scope': 'auxiliary', 'task': task, 'provider': block.get('provider', ''), 'model': block.get('model', '')}
        ek = 'auxiliary.' + task + '.reasoning_effort'
        if ek in keys:
            body['reasoning_effort'] = block.get('reasoning_effort') or None
            handled.add(ek)
        steps.append(('model_set', body))
        # Hermes's auxiliary assignment retires the old provider's endpoint itself.
        handled |= {'auxiliary.' + task + '.provider', 'auxiliary.' + task + '.model', 'auxiliary.' + task + '.base_url'}
    partial = {}
    for c in changes:
        key = c['field']
        if key in handled:
            continue
        if key == 'model.context_length':
            partial['model_context_length'] = 0
            continue
        value = c['after']['value'] if c['after']['present'] else ''
        put(partial, key, value)
    if partial:
        steps.append(('put_config', partial))
    return steps
