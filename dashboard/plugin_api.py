"""Fleet Control dashboard routes over Hermes's public dashboard API only.

Every read and write goes through the same authenticated dashboard routes the
Hermes Config and Models pages use, called in-process with the caller's own
session headers:

- GET  /api/profiles                      roster
- GET  /api/config/raw?profile=<p>        saved settings (parsed here, never forwarded)
- GET  /api/config/defaults               shipped auxiliary task defaults
- GET  /api/model/options?profile=<p>     model catalog and per-model Fast flags
- GET  /api/model/auxiliary?profile=<p>   host-known auxiliary slots
- POST /api/model/set?profile=<p>         main and auxiliary provider/model
- PUT  /api/config?profile=<p>            reasoning, subagents, Fast, fallback

No Hermes internals, private locks, config-backend access or credential reads.
Hermes has no conditional config save yet (NousResearch/hermes-agent#156100), so
each profile is rechecked immediately before its first write. That narrows but
does not close the window for a competing writer.

Other machines (opt-in, 2.2): when the owner fills a machine slot in this
plugin's settings, the same seven routes are called on that machine's own
Hermes dashboard over HTTPS (or plain HTTP on loopback/Tailscale only) with
that dashboard's session token as a bearer. Settings come from
hermes_cli.config.load_config_readonly() (plugins.entries.fleet-control.settings)
and each token from agent.secret_scope.get_secret(), as the plugin guide
documents. With no machine configured nothing leaves this machine.
"""
import asyncio
import hashlib
import ipaddress
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request

from agent.secret_scope import get_secret
from hermes_cli.config import load_config_readonly
from hermes_yaml import safe_load as yaml_safe_load

# Load our own engine the same way Hermes loads a dashboard API file; never alter
# sys.path or patch a core module.
ENGINE_PATH = Path(__file__).resolve().parent.parent / 'host.py'
ENGINE_NAME = 'fleet_control_own_engine'
if ENGINE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(ENGINE_NAME, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError('Fleet Control engine unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[ENGINE_NAME] = module
    spec.loader.exec_module(module)
engine = sys.modules[ENGINE_NAME]
router = APIRouter()
BOARDS = {}
BOARDS_LOCK = threading.Lock()
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
FORWARDED = ('host', 'cookie', 'x-hermes-session-token')
PLUGIN_ID = 'fleet-control'
SLOTS = 4
QUALIFIED = re.compile(r'^(local|m[1-%d]):([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$' % SLOTS)
# Writes are never retried (httpx does not retry); a lost write reads back as uncertain.
REMOTE_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
REMOTE_DEADLINE = 90
REMOTE_MAX_BYTES = 8 * 1024 * 1024
TAILNET_NETS = (ipaddress.ip_network('100.64.0.0/10'), ipaddress.ip_network('fd7a:115c:a1e0::/48'))


class HostUnavailable(Exception):
    pass


class MachineDown(HostUnavailable):
    """Another machine's dashboard is unreachable, refused our token or redirected."""

    def __init__(self, status, reason):
        super().__init__(status)
        self.status, self.reason = status, reason


class Host:
    """Thin in-process client for the dashboard's public routes."""

    def __init__(self, request):
        # Only the caller's management-session headers, never provider credentials.
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),
                                        base_url=str(request.base_url), headers=headers, timeout=120)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def get(self, path, **params):
        response = await self.client.get(path, params=params or None)
        if response.status_code != 200:
            raise HostUnavailable(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise HostUnavailable('json') from None

    async def snapshot(self, profile):
        """(exact saved text, parsed mapping) or None when the host cannot provide it."""
        try:
            payload = await self.get('/api/config/raw', profile=profile)
            text = payload.get('yaml') if isinstance(payload, dict) else None
            if not isinstance(text, str):
                return None
            doc = yaml_safe_load(text) if text.strip() else {}
            return (text, doc) if isinstance(doc, dict) else None
        except Exception:
            return None

    async def write(self, kind, profile, body):
        if kind == 'model_set':
            response = await self.client.post('/api/model/set', params={'profile': profile}, json=body)
        else:
            response = await self.client.put('/api/config', params={'profile': profile}, json={'config': body})
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload


class RemoteHost(Host):
    """The same public routes on another machine's own Hermes dashboard.

    Bearer token only to that exact origin: redirects are refused, proxy
    environment variables are ignored and nothing is retried.
    """

    def __init__(self, machine):
        token = machine.token()
        self.client = httpx.AsyncClient(base_url=machine.url, headers={'Authorization': 'Bearer ' + token,
                                                                       'Accept': 'application/json'},
                                        follow_redirects=False, trust_env=False, timeout=REMOTE_TIMEOUT)

    async def _send(self, method, path, **kw):
        try:
            response = await self.client.request(method, path, **kw)
        except httpx.TimeoutException:
            raise MachineDown('offline', 'Timed out') from None
        except httpx.TransportError:
            raise MachineDown('offline', 'Could not connect') from None
        if response.status_code in (401, 403):
            raise MachineDown('auth_failed', 'Its dashboard refused the saved token')
        if 300 <= response.status_code < 400:
            raise MachineDown('refused', 'Its dashboard answered with a redirect, which is not followed')
        if len(response.content) > REMOTE_MAX_BYTES:
            raise HostUnavailable('size')
        return response

    async def get(self, path, **params):
        response = await self._send('GET', path, params=params or None)
        if response.status_code != 200:
            raise HostUnavailable(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise HostUnavailable('json') from None

    async def write(self, kind, profile, body):
        # Any transport failure propagates; apply_profile reports it as uncertain, never replays.
        try:
            if kind == 'model_set':
                response = await self._send('POST', '/api/model/set', params={'profile': profile}, json=body)
            else:
                response = await self._send('PUT', '/api/config', params={'profile': profile}, json={'config': body})
        except MachineDown as down:
            if down.status == 'offline':
                raise
            return 401, None  # refused before Hermes accepted the request
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload


def url_problem(url):
    """Why a machine URL is refused, or '' when allowed.

    https anywhere; plain http only on loopback, Tailscale addresses or *.ts.net
    names, where the network itself is private and encrypted.
    """
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        return 'Not a valid URL'
    if parts.scheme not in ('https', 'http') or not host:
        return 'Use an https:// address'
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        return 'Use the dashboard\'s base address only, without a path, login or query'
    if parts.scheme == 'http':
        try:
            ip = ipaddress.ip_address(host)
            private = ip.is_loopback or any(ip in net for net in TAILNET_NETS)
        except ValueError:
            private = host == 'localhost' or host.endswith('.ts.net')
        if not private:
            return 'Plain http is allowed only for loopback and Tailscale addresses; use https'
    return ''


def clean_label(value, fallback):
    text = ' '.join(value.split()) if isinstance(value, str) else ''
    text = ''.join(ch for ch in text if ch.isprintable())[:40]
    return text or fallback


class Machine:
    """One fixed settings slot. The id comes from the slot, never from a profile name."""

    def __init__(self, mid, label, url='', env=''):
        self.id, self.label, self.url, self.env = mid, label, url.rstrip('/'), env
        self.local = mid == 'local'
        self.problem = '' if self.local else url_problem(url)
        self.scope = () if self.local else ('machine', mid, hashlib.sha256(self.url.encode()).hexdigest())

    def token(self):
        try:
            token = get_secret(self.env)
        except Exception:
            # e.g. no profile secret scope while multiplexing: fail closed, never read files.
            raise MachineDown('auth_failed', 'This dashboard cannot read the saved token here') from None
        token = token.strip() if isinstance(token, str) else ''
        if not token or any(ch.isspace() or not ch.isprintable() for ch in token):
            raise MachineDown('auth_failed', 'No access token saved for this machine')
        return token

    def public(self):
        return {'id': self.id, 'label': self.label, 'local': self.local}


def configured_machines():
    """Remote slots the owner filled in, or [] (2.1 behaviour, no outbound calls)."""
    try:
        config = load_config_readonly()
        settings = config['plugins']['entries'][PLUGIN_ID]['settings']
    except Exception:
        return []
    if not isinstance(settings, dict):
        return []
    remotes = []
    for n in range(1, SLOTS + 1):
        url = settings.get('machine_%d_url' % n)
        if isinstance(url, str) and url.strip():
            remotes.append(Machine('m%d' % n, clean_label(settings.get('machine_%d_label' % n), 'Machine %d' % n),
                                   url.strip(), 'FLEET_CONTROL_MACHINE_%d_TOKEN' % n))
    if not remotes:
        return []
    return [Machine('local', clean_label(settings.get('local_label'), 'This machine'))] + remotes


def host_for(request, machine):
    return Host(request) if machine.local else RemoteHost(machine)


def authority(request):
    # Plugin routes already sit behind the dashboard session check. Profile-scoped
    # or service tickets do not grant machine-wide authority over every profile.
    if (getattr(request.state, 'native_http_principal', None)
            or getattr(request.state, 'hosted_http_authenticated', False)
            or getattr(request.state, 'token_authenticated', False)):
        raise HTTPException(403, 'Fleet operations require machine-dashboard browser authority, not a profile/service ticket')


async def roster(host):
    try:
        payload = await host.get('/api/profiles')
    except MachineDown:
        raise
    except HostUnavailable:
        raise HTTPException(503, 'Host profile roster unavailable') from None
    rows = payload.get('profiles') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 64:
        raise HTTPException(503, 'Host profile roster unavailable or exceeds candidate bound')
    names = []
    for row in rows:
        name = row.get('name') if isinstance(row, dict) else None
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or name in names:
            raise HTTPException(503, 'Unsupported host profile binding')
        names.append(name)
    return names


def board_for(request, names, scope=()):
    session = getattr(request.state, 'session', None)
    principal = ('session', getattr(session, 'org_id', ''), getattr(session, 'user_id', '')) if session is not None else ('local-owner',)
    # Bind previews to the current management session without storing its secret.
    credential = request.headers.get('x-hermes-session-token', '') + '\0' + request.headers.get('cookie', '')
    principal += (hashlib.sha256(credential.encode()).hexdigest(),)
    # Each machine gets its own board, so a preview token binds to exactly one machine.
    key = (id(request.app), principal, scope, tuple(sorted(names)))
    with BOARDS_LOCK:
        if key not in BOARDS:
            if len(BOARDS) >= 16:
                for candidate in BOARDS.values():
                    candidate.expire_previews()
                idle = [k for k, b in BOARDS.items() if not b.previews and not b.active]
                if not idle:
                    raise HTTPException(503, 'Authority cache busy; active previews retained')
                del BOARDS[min(idle, key=lambda k: BOARDS[k].last_used)]
            BOARDS[key] = engine.HostBoard(names, principal=principal)
        board = BOARDS[key]
        board.active += 1
        board.last_used = engine.time.monotonic()
    return board


def release(board):
    with BOARDS_LOCK:
        board.active -= 1
        board.last_used = engine.time.monotonic()


async def refresh_defaults(host, board):
    try:
        board.aux_defaults = engine.aux_defaults_from(await host.get('/api/config/defaults'))
    except MachineDown:
        raise
    except HostUnavailable:
        raise HTTPException(503, 'Host configuration defaults unavailable') from None


async def snapshots(host, names):
    return {p: await host.snapshot(p) for p in names}


async def refresh_catalog(host, board, names, snaps):
    for p in names:
        snap = snaps.get(p)
        # External secret profiles stay read-only and never trigger catalog warmers.
        if snap is None or engine.uses_external_secrets(snap[1]):
            board.catalog[p], board.caps[p], board.catalog_status[p] = [], {}, 'unavailable'
            continue
        try:
            payload = await host.get('/api/model/options', profile=p, explicit_only='true')
            providers = payload.get('providers') if isinstance(payload, dict) else None
            if not isinstance(providers, list):
                raise HostUnavailable('catalog')
            entries, caps = [], {}
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                slug = provider.get('slug')
                flags = provider.get('capabilities') if isinstance(provider.get('capabilities'), dict) else {}
                for model in provider.get('models', []) if isinstance(provider.get('models', []), list) else []:
                    mid = model.get('id') if isinstance(model, dict) else model
                    if (isinstance(slug, str) and isinstance(mid, str) and slug and mid
                            and engine.safe_value('provider', slug) and engine.safe_value('model', mid)):
                        pair = {'provider': slug, 'model': mid}
                        if pair not in entries:
                            entries.append(pair)
                        flag = flags.get(mid)
                        if isinstance(flag, dict):
                            caps[(slug, mid)] = {'fast': flag.get('fast') is True}
            board.catalog[p], board.caps[p] = entries, caps
            board.catalog_status[p] = 'ready' if entries else 'empty'
        except Exception:
            # One profile's failing metadata is never a fleet failure.
            board.catalog[p], board.caps[p], board.catalog_status[p] = [], {}, 'unavailable'


def needs_catalog(draft):
    return isinstance(draft, dict) and (draft.get('route') == 'explicit' or bool(draft.get('fallback'))
                                        or draft.get('service_tier') == 'priority')


async def read_json(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(415, 'JSON required')
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, 'Request too large')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'Invalid JSON') from None
    if not isinstance(data, dict):
        raise HTTPException(422, 'Object required')
    return data


def selected(draft, names):
    chosen = draft.get('profiles', []) if isinstance(draft, dict) else []
    if not isinstance(chosen, list) or any(not isinstance(p, str) or p not in names for p in chosen):
        raise HTTPException(422, 'Select exact authorized profiles')
    return list(dict.fromkeys(chosen))


async def apply_profile(host, board, p, plan, d):
    snap = await host.snapshot(p)
    if snap is None:
        return {'profile': p, 'state': 'uncertain', 'detail': 'Profile unreadable before saving; nothing written'}
    raw, doc = snap
    if engine.revision(board.instance, raw) != plan['before']:
        return {'profile': p, 'state': 'conflict', 'detail': 'Profile changed; not written'}
    after, changes = board.plan(p, doc, d)
    if changes != plan['changes']:
        return {'profile': p, 'state': 'conflict', 'detail': 'Plan changed; not written'}
    if changes:
        aux_slots = set()
        if any(c['field'].startswith('auxiliary.') for c in changes):
            try:
                rows = (await host.get('/api/model/auxiliary', profile=p)).get('tasks', [])
                aux_slots = {r.get('task') for r in rows if isinstance(r, dict)}
            except Exception:
                aux_slots = set()
        for index, (kind, body) in enumerate(engine.write_steps(changes, after, aux_slots)):
            try:
                status, payload = await host.write(kind, p, body)
            except Exception:
                return {'profile': p, 'state': 'uncertain', 'detail': 'Save failed; reload, never replay'}
            first = index == 0
            if status == 200 and isinstance(payload, dict) and payload.get('confirm_required'):
                if first:
                    return {'profile': p, 'state': 'refused',
                            'detail': 'Hermes asks you to confirm this model\'s cost first. Nothing saved for this profile; '
                                      'confirm it on Hermes\'s Models page.'}
                return {'profile': p, 'state': 'uncertain', 'detail': 'Partly saved; Hermes asked for a cost confirmation. Reload, never replay'}
            if status != 200 or not isinstance(payload, dict) or payload.get('ok') is False:
                if first:
                    return {'profile': p, 'state': 'refused', 'detail': 'Hermes rejected the change (%s); nothing saved for this profile' % status}
                return {'profile': p, 'state': 'uncertain', 'detail': 'Partly saved; Hermes rejected a later step (%s). Reload, never replay' % status}
    saved = await host.snapshot(p)
    if saved is None or not board.verify(saved[1], changes):
        return {'profile': p, 'state': 'uncertain', 'detail': 'Readback mismatch; reload, never replay'}
    return {'profile': p, 'state': 'applied' if changes else 'unchanged', 'readback': engine.safe_routes(saved[1])}


async def run(host, request, action, draft, data, scope=()):
    """The 2.1 state/preview/apply flow against one machine's public routes."""
    names = await roster(host)
    chosen = names if action == 'state' else selected(draft, names)
    board = board_for(request, names, scope)
    try:
        await refresh_defaults(host, board)
        snaps = await snapshots(host, chosen)
        if action == 'state' or needs_catalog(draft):
            await refresh_catalog(host, board, chosen, snaps)
        if action == 'state':
            return board.state(snaps)
        if action == 'preview':
            return board.preview(draft, snaps)
        pending = board.consume(draft, data['token'], data['confirmed'])
        board.validate(draft, {p: s[1] for p, s in snaps.items() if s is not None})
        for p, plan in pending['plans'].items():
            snap = snaps.get(p)
            if snap is None or engine.revision(board.instance, snap[0]) != plan['before']:
                raise engine.BoardError(409, 'Stale preview: selected profile changed; nothing written')
        results = []
        for p, plan in pending['plans'].items():
            try:
                results.append(await apply_profile(host, board, p, plan, draft))
            except engine.BoardError as error:
                results.append({'profile': p, 'state': 'refused', 'detail': error.detail})
            except Exception:
                results.append({'profile': p, 'state': 'uncertain', 'detail': 'Save/readback failed; reload, never replay'})
        ok = all(r['state'] in ('applied', 'unchanged') for r in results)
        return {'fixture': False, 'ok': ok, 'partial': not ok and any(r['state'] == 'applied' for r in results), 'profiles': results}
    except engine.BoardError as error:
        raise HTTPException(error.status, error.detail) from None
    finally:
        release(board)


def split_draft(draft, fleet):
    """Machine and machine-local draft. Every profile must name the same configured machine."""
    by_id = {m.id: m for m in fleet}
    ps = draft.get('profiles') if isinstance(draft, dict) else None
    revs = draft.get('expected_revisions') if isinstance(draft, dict) else None
    if not isinstance(ps, list) or not ps or not isinstance(revs, dict):
        raise HTTPException(422, 'Select exact authorized profiles')
    parsed = [QUALIFIED.fullmatch(q) if isinstance(q, str) else None for q in ps]
    keyed = [QUALIFIED.fullmatch(k) if isinstance(k, str) else None for k in revs]
    if not all(parsed) or not all(keyed):
        raise HTTPException(422, 'Select exact authorized profiles')
    ids = {m[1] for m in parsed} | {m[1] for m in keyed}
    if len(ids) != 1:
        raise HTTPException(422, 'One machine per preview: choose profiles from a single machine. Nothing was written.')
    mid = ids.pop()
    if mid not in by_id:
        raise HTTPException(422, 'That machine is not configured')
    local = dict(draft)
    local['profiles'] = [m[2] for m in parsed]
    local['expected_revisions'] = {m[2]: revs[m.string] for m in keyed}
    return by_id[mid], local


def qualify(machine, row):
    row = dict(row)
    name = row.get('profile')
    row.update(profile=machine.id + ':' + name, profile_name=name, host=machine.id, host_label=machine.label)
    return row


async def machine_state(request, machine):
    status = {**machine.public(), 'status': 'ok', 'reason': '', 'profiles': 0}
    if machine.problem:
        status.update(status='refused', reason=machine.problem)
        return status, None
    try:
        async with host_for(request, machine) as host:
            st = await asyncio.wait_for(run(host, request, 'state', None, {}, machine.scope), REMOTE_DEADLINE)
    except MachineDown as down:
        status.update(status=down.status, reason=down.reason)
        return status, None
    except asyncio.TimeoutError:
        status.update(status='offline', reason='Timed out')
        return status, None
    except HTTPException as error:
        status.update(status='unavailable', reason=str(error.detail))
        return status, None
    except Exception:
        status.update(status='unavailable', reason='Profiles unavailable')
        return status, None
    status['profiles'] = len(st.get('profiles', []))
    return status, st


async def fleet_state(request, fleet):
    answers = await asyncio.gather(*(machine_state(request, m) for m in fleet))
    machines, rows, sample = [], [], None
    for machine, (status, st) in zip(fleet, answers):
        machines.append(status)
        if st is None:
            continue  # no stale or selectable rows from a machine that did not answer
        sample = sample or st
        rows.extend(qualify(machine, r) for r in st.get('profiles', []))
    out = {'available': sample is not None, 'fixture': False,
           'authority': 'Authenticated local host plus the other machines you configured. Every save goes through '
                        'Hermes\'s public dashboard routes on the machine that owns the profile. Not a fleet transaction.',
           'inventory_provenance': 'Hermes /api/model/options on each machine; credentials remain with each host',
           'unsupported': (sample or {}).get('unsupported', []), 'profiles': rows,
           'efforts': (sample or {}).get('efforts', list(engine.EFFORTS)), 'machines': machines}
    if sample is None:
        out['reason'] = 'No machine answered'
    return out


async def dispatch(request, action):
    authority(request)
    data = await read_json(request) if action != 'state' else {}
    if action == 'apply' and set(data) != {'draft', 'token', 'confirmed'}:
        raise HTTPException(422, 'Exact apply envelope required')
    draft = data.get('draft') if action == 'apply' else data
    fleet = configured_machines()
    if not fleet:
        async with Host(request) as host:
            return await run(host, request, action, draft, data)
    if action == 'state':
        return await fleet_state(request, fleet)
    machine, local = split_draft(draft, fleet)
    if machine.problem:
        raise HTTPException(422, machine.label + ': ' + machine.problem + '. Nothing was written.')
    local_data = {**data, 'draft': local} if action == 'apply' else local
    try:
        async with host_for(request, machine) as host:
            result = await run(host, request, action, local, local_data, machine.scope)
    except MachineDown as down:
        # Raised only before the first write; later failures are per-profile outcomes.
        raise HTTPException(503, machine.label + ': ' + down.reason + '. Nothing was written.') from None
    except HTTPException as error:
        raise HTTPException(error.status_code, machine.label + ': ' + str(error.detail)) from None
    result = dict(result)
    result['profiles'] = [dict(p, profile=machine.id + ':' + p['profile']) for p in result.get('profiles', [])]
    result['machine'] = machine.public()
    if action == 'preview' and not machine.local:
        result['warning'] = 'Saves go to ' + machine.label + '\'s own Hermes dashboard. ' + result.get('warning', '')
    return result


@router.get('/state')
async def state(request: Request):
    try:
        return await dispatch(request, 'state')
    except HTTPException as error:
        if error.status_code != 503:
            raise
        return {'available': False, 'fixture': False, 'profiles': [], 'reason': error.detail}


@router.post('/preview')
async def preview(request: Request):
    return await dispatch(request, 'preview')


@router.post('/apply')
async def apply_changes(request: Request):
    return await dispatch(request, 'apply')
