"""In-process local-file routing engine; no credential reads or legacy adapter.

The caller must supply host-authorized paths, host-owned model catalogs and the
actual core mutation lock. This engine never discovers a profile or credentials.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from hermes_cli import config_backend as cb
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_constants import VALID_REASONING_EFFORTS

ROUTE_ID = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_./:@+\[\]-]{0,199}$')
SECRET_PREFIX = re.compile(r'^(?:sk[-_]|xai-|hf_|gh[pousr]_)', re.I)
EFFORTS = tuple(dict.fromkeys(('none', *VALID_REASONING_EFFORTS)))
# auxiliary also holds global policy scalars/lists, not task route blocks.
AUX_DEFAULTS = {k:v for k,v in DEFAULT_CONFIG.get('auxiliary', {}).items() if isinstance(v,dict)}

def safe_value(key, value):
    if key.endswith('service_tier'):
        from agent.fast_mode import NORMAL_TIER_WORDS, SERVICE_TIER_WORDS
        return isinstance(value,str) and value in NORMAL_TIER_WORDS | SERVICE_TIER_WORDS.keys()
    if key.endswith('max_concurrent_children'):
        return type(value) is int and 1<=value<=16
    if key.endswith('context_length'):
        return isinstance(value,int) and not isinstance(value,bool) and value > 0
    if key.endswith('reasoning_effort'):
        return value is False or (isinstance(value,str) and value in ('', *EFFORTS))
    return isinstance(value,str) and (value == '' or bool(ROUTE_ID.fullmatch(value)) and '://' not in value and not SECRET_PREFIX.match(value) and not (':' in value and '@' in value))

TASK = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,79}$')
OVERRIDES = {'base_url','api_url','api_key','api','key_env','api_key_env','api_mode','extra_headers'}

class BoardError(Exception):
    def __init__(self,status,detail):
        super().__init__(detail)
        self.status,self.detail=status,detail

def field(doc,key):
    node=doc
    for part in key.split('.'):
        if not isinstance(node,dict) or part not in node:return {'present':False}
        node=node[part]
    return {'present':True,'value':node}

def transition_clear_fields(block, draft):
    """Retire public, host-derived routing pins without resolving any credentials.

    Same-provider model changes leave connection/auth fields untouched. A provider
    transition can clear only endpoint/mode values exactly vouched for by offline
    host metadata. Custom/private overrides remain host-managed and are never
    returned, transferred to another provider, or silently discarded here.
    """
    if draft['route'] == 'keep':
        return ()
    if draft['route'] == 'explicit' and block.get('provider') == draft['provider']:
        return ()
    active = {k for k in OVERRIDES.intersection(block) if block[k] not in ('', None)}
    if not active:
        return ()
    reason = 'Changing provider with custom endpoint/auth overrides requires host-managed setup; existing credentials were not read or changed'
    if active - {'base_url', 'api_mode'}:
        raise BoardError(422, reason)
    try:
        # Public provider definitions only: never switch_model, auth resolution,
        # environment hydration, catalog refresh, HTTP calls or model probes.
        from hermes_cli.providers import get_provider, TRANSPORT_TO_API_MODE
        provider = block.get('provider')
        definition = get_provider(provider, allow_network=False)
        if definition is None or definition.id != provider:
            raise ValueError('Ambiguous or unavailable host provider definition')
        if 'base_url' in active:
            from urllib.parse import urlsplit
            endpoint = block['base_url']
            if not isinstance(endpoint, str) or endpoint != definition.base_url:
                raise ValueError('Custom endpoint')
            url = urlsplit(endpoint)
            if (url.scheme not in ('https', 'http', 'process') or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError('Credential-bearing or unsupported endpoint')
        if 'api_mode' in active:
            mode = TRANSPORT_TO_API_MODE.get(definition.transport, definition.transport)
            if not mode or not safe_value('api_mode', mode) or block['api_mode'] != mode:
                raise ValueError('Custom API mode')
    except Exception:
        raise BoardError(422, reason) from None
    return tuple(sorted(active))


def put(doc,key,value):
    parts=key.split('.');node=doc
    for part in parts[:-1]:
        node=node.setdefault(part,{})
        if not isinstance(node,dict):raise BoardError(422,'Routing block is not a mapping')
    if value is None:node.pop(parts[-1],None)
    else:node[parts[-1]]=value

def safe_routes(doc):
    out={'model':{},'agent':{},'delegation':{},'auxiliary':{}}
    specs=[('model',('provider','default')),('agent',('reasoning_effort','service_tier')),('delegation',('provider','model','reasoning_effort','max_concurrent_children'))]
    aux=doc.get('auxiliary',{})
    if isinstance(aux,dict):specs += [('auxiliary.'+k,('provider','model','reasoning_effort')) for k in aux if isinstance(k,str) and TASK.fullmatch(k)]
    for base,keys in specs:
        for key in keys:
            val=field(doc,base+'.'+key)
            if val['present']:
                put(out,base+'.'+key,val['value'] if safe_value(key,val['value']) else '[withheld: unsupported route value]')
    if 'fallback_providers' in doc:out['fallback_providers']=safe_fallback(doc)
    return out

def uses_external_secrets(doc):
    block=doc.get('secrets')
    if not block:return False
    if not isinstance(block,dict):return True
    sources=block.get('sources') if isinstance(block.get('sources'),dict) else block
    return any(not isinstance(v,dict) or v.get('enabled') is not False for v in sources.values())


def valid_chain(chain):
    return isinstance(chain,list) and len(chain)<=10 and all(isinstance(r,dict) and set(r)=={'provider','model'} and all(isinstance(r[k],str) and r[k] and safe_value(k,r[k]) for k in ('provider','model')) for r in chain) and len({(r['provider'],r['model']) for r in chain})==len(chain)


def fallback_lock(doc):
    if doc.get('fallback_model'):return 'Legacy fallback_model present; host-managed editing required'
    raw=doc.get('fallback_providers')
    if raw is not None and not valid_chain(raw):return 'Fallback has unsupported endpoint/auth/options or malformed entries; not changed'
    return ''


def safe_fallback(doc):
    raw=doc.get('fallback_providers')
    if valid_chain(raw):return copy.deepcopy(raw)
    return []  # lock/reason distinguishes withheld malformed data from empty


def fast_capability(doc):
    """Pure configured-route host gate: no account discovery, environment or probe.

    Deliberately refuse custom definitions/ambiguous aliases. No provider secrets
    are resolved; endpoint strings are used internally only, never returned.
    """
    from urllib.parse import urlsplit
    raw=field(doc,'agent.service_tier')
    current='normal' if not raw['present'] or raw['value']=='' else raw['value'] if safe_value('service_tier',raw['value']) else 'unknown'
    out={'current':current,'supported':False,'reason':'Fast requires a supported first-party configured route; availability not checked'}
    model=doc.get('model');providers=doc.get('providers',{})
    if not isinstance(model,dict) or not isinstance(providers,dict) or doc.get('custom_providers'):return out
    provider=model.get('provider');mid=model.get('default')
    if provider not in ('openai','openai-api','openai-codex','anthropic','xai') or not safe_value('model',mid):return out
    entry=providers.get(provider,{})
    if not isinstance(entry,dict) or entry.get('enabled') is False:return out
    try:
        from hermes_cli.models import resolve_fast_mode_overrides
        endpoints=[b[k] for b in (model,entry) for k in ('base_url','api_url','api','url') if b.get(k)]
        for endpoint in endpoints:
            u=urlsplit(endpoint)
            if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.port not in (None,443):return out
        if all(resolve_fast_mode_overrides(mid,provider=provider,base_url=e) for e in endpoints or [None]):
            out.update(supported=True,reason='Host offline configured-route capability; not login or entitlement')
    except Exception:
        out['reason']='Host Fast capability unavailable; Normal clear remains possible'
    return out


def task_metadata(doc):
    aux=doc.get('auxiliary',{})
    names=set(AUX_DEFAULTS)
    if isinstance(aux,dict):names.update(k for k,v in aux.items() if isinstance(k,str) and TASK.fullmatch(k) and isinstance(v,dict))
    return [{'task':k,'label':k.replace('_',' ').capitalize(),'editable':k in AUX_DEFAULTS,
             'effort':isinstance(AUX_DEFAULTS.get(k),dict) and 'reasoning_effort' in AUX_DEFAULTS[k],
             'configured':isinstance(aux,dict) and k in aux,
             'note':'' if k in AUX_DEFAULTS else 'Unknown legacy/provider task: saved state only; host metadata unavailable'} for k in sorted(names)]

class HostBoard:
    def __init__(self,paths,catalog,lock,*,principal):
        self.paths={p:Path(v) for p,v in paths.items()}
        self.catalog,self.lock,self.principal=copy.deepcopy(catalog),lock,principal
        self.instance=secrets.token_hex(32);self.previews={}
        self.catalog_status={};self.active=0;self.last_used=time.monotonic()

    def expire_previews(self):
        now=time.monotonic()
        self.previews={t:v for t,v in self.previews.items() if now-v['at']<600}

    def editable(self,doc):
        # Fail closed for any configured external secret source, without hydrating it.
        return not uses_external_secrets(doc)

    def read(self,p):
        path=self.paths[p]
        if not path.is_absolute() or path.name!='config.yaml' or path.resolve()!=path:
            raise BoardError(503,'Profile location changed or unsupported; reload')
        try:
            doc=cb.read_config_doc(path)
        except Exception:
            raise BoardError(503,'Profile configuration unreadable; no repair attempted') from None
        if not isinstance(doc,dict):raise BoardError(503,'Profile configuration is not a mapping')
        return doc

    def revision(self,doc):
        # YAML has dates, binary values, sets and mixed-type mapping keys.
        # Tag scalars and sort encoded keys, never Python's incomparable keys.
        import datetime
        def encode(value):
            if isinstance(value,dict):
                return ['map',sorted([[encode(k),encode(v)] for k,v in value.items()],key=lambda pair:json.dumps(pair[0]))]
            if isinstance(value,(list,tuple)):return ['list',[encode(v) for v in value]]
            if isinstance(value,set):return ['set',sorted([encode(v) for v in value],key=json.dumps)]
            if isinstance(value,(datetime.date,datetime.datetime)):return [type(value).__name__,value.isoformat()]
            if isinstance(value,bytes):return ['bytes',value.hex()]
            if value is None or isinstance(value,(str,bool,int,float)):return [type(value).__name__,repr(value)]
            raise ValueError('Unsupported configuration value')
        try:payload=json.dumps(encode(doc),ensure_ascii=True)
        except (ValueError,TypeError,RecursionError):raise BoardError(503,'Profile configuration cannot be safely revisioned') from None
        return hashlib.sha256((self.instance+payload).encode()).hexdigest()

    def row(self,p):
        try:
            doc=self.read(p)
            revision=self.revision(doc)
        except BoardError:
            return {'profile':p,'editable':False,'config_status':'unreadable','reason':'Profile configuration unreadable or cannot be safely revisioned; no repair attempted','models':[],'catalog':{'status':'unavailable'}}
        models=[{'provider':m['provider'],'model':m['model']} for m in self.catalog.get(p,[]) if isinstance(m,dict) and m.get('provider') and m.get('model') and safe_value('provider',m.get('provider')) and safe_value('model',m.get('model'))]
        return {'profile':p,'models':models,'routes':safe_routes(doc),'revision':revision,
                'fallback':safe_fallback(doc),'fallback_lock':fallback_lock(doc),'fast_mode':fast_capability(doc),'tasks':task_metadata(doc),'editable':self.editable(doc),'config_status':'readable','reason':'' if self.editable(doc) else 'Read-only: external secret sources',
                'catalog':{'status':self.catalog_status.get(p,'ready' if models else 'empty'),'provenance':'Host metadata; availability/authentication unverified'}}

    def state(self):
        with self.lock:
            return {'available':True,'fixture':False,'authority':'Authenticated local host; same-process mutation lock, not a fleet transaction.',
                    'inventory_provenance':'Hermes /api/model/options; credentials remain with host',
                    'unsupported':['federation','jobs','remote config','isolated plugin workers','explicit endpoint/auth overrides'],
                    'profiles':[self.row(p) for p in self.paths],'efforts':list(EFFORTS)}

    def validate(self,d):
        required={'profiles','targets','route','provider','model','effort','expected_revisions'}
        optional={'max_concurrent_children','service_tier','fallback'}
        if not isinstance(d,dict) or not required<=set(d) or set(d)-required-optional:raise BoardError(422,'Invalid draft fields')
        limit=d.get('max_concurrent_children')
        if limit is not None and (type(limit) is not int or not 1<=limit<=16):raise BoardError(422,'Max subagents must be an integer from 1 to 16')
        tier=d.get('service_tier')
        if tier is not None and (not isinstance(tier,str) or tier not in ('normal','priority')):raise BoardError(422,'Choose Normal or Fast explicitly')
        fallback=d.get('fallback')
        if fallback is not None and not valid_chain(fallback):raise BoardError(422,'Fallback must contain at most 10 unique exact provider/model pairs, no extra options')
        wide=limit is not None or tier is not None or fallback is not None
        ps,ts=d['profiles'],d['targets']
        if not isinstance(ps,list) or not ps or len(ps)>64 or any(not isinstance(p,str) or p not in self.paths for p in ps) or len(set(ps))!=len(ps):raise BoardError(422,'Select exact unique authorized profiles')
        if not isinstance(ts,list) or (not ts and not wide) or len(ts)>64 or any(not isinstance(t,str) for t in ts) or len(set(ts))!=len(ts):raise BoardError(422,'Select unique supported targets')
        if not ts and (d['route']!='keep' or d['effort'] is not None):raise BoardError(422,'Route and effort require real targets')
        for p in ps:
            doc=self.read(p)
            if not self.editable(doc):raise BoardError(422,p+': read-only external secret sources')
            if fallback is not None:
                if fallback_lock(doc):raise BoardError(422,p+': '+fallback_lock(doc))
                persisted=doc.get('fallback_providers') or []
                if any(pair not in persisted and (self.catalog_status.get(p)=='unavailable' or pair not in self.catalog.get(p,[])) for pair in fallback):raise BoardError(422,p+': new fallback entry unavailable in host catalog')
            allowed={'main','delegation','auxiliary.*'}|{'auxiliary.'+k for k in AUX_DEFAULTS if TASK.fullmatch(k)}
            if any(t not in allowed for t in ts):raise BoardError(422,'Auxiliary target not declared in every selected profile')
        route,effort=d['route'],d['effort']
        if route not in ('keep','explicit','inherit','auto') or effort not in (None,'',*EFFORTS):raise BoardError(422,'Invalid route or effort')
        if any(not isinstance(d[k],str) for k in ('provider','model')):raise BoardError(422,'Exact provider/model strings required')
        if route=='explicit':
            if not safe_value('provider',d['provider']) or not safe_value('model',d['model']):raise BoardError(422,'Unsupported provider/model identifiers')
            pair={'provider':d['provider'],'model':d['model']}
            reasons=[p+': '+('host catalog unavailable' if self.catalog_status.get(p)=='unavailable' else 'selection not in host catalog') for p in ps if pair not in self.catalog.get(p,[]) or self.catalog_status.get(p)=='unavailable']
            if reasons:raise BoardError(422,'; '.join(reasons))
        elif d['provider'] or d['model']:raise BoardError(422,'Nonexplicit routes require empty provider/model')
        if ('main' in ts and route in ('inherit','auto')) or ('delegation' in ts and route=='auto'):raise BoardError(422,'Unsupported route for selected target')
        if route=='keep' and effort is None and not wide:raise BoardError(422,'No requested change')
        revs=d['expected_revisions']
        if not isinstance(revs,dict) or set(revs)!=set(ps) or any(not isinstance(v,str) for v in revs.values()):raise BoardError(422,'Exact profile revisions required')

    def plan(self,doc,d):
        after,changes=copy.deepcopy(doc),[]
        targets=[]
        for target in d['targets']:
            targets.extend(['auxiliary.'+k for k in AUX_DEFAULTS if d['route']=='explicit' or (isinstance(doc.get('auxiliary'),dict) and k in doc['auxiliary'])] if target=='auxiliary.*' else [target])
        for target in dict.fromkeys(targets):
            # Missing auxiliary blocks already use host defaults. Reset presets
            # must not manufacture empty blocks or change bytes for a no-op.
            if target.startswith('auxiliary.') and not field(doc,target)['present'] and d['route'] in ('inherit','auto') and d['effort'] is None:continue
            base='model' if target=='main' else target;mk='default' if target=='main' else 'model';eb='agent' if target=='main' else target
            block=field(doc,base)
            if block['present'] and not isinstance(block['value'],dict):raise BoardError(422,'Routing block is not a mapping')
            clear_fields = transition_clear_fields(block['value'], d) if block['present'] else ()
            for name in clear_fields:
                key = base + '.' + name
                old = field(doc, key)
                # The browser sees the effect, never the routing endpoint value.
                before = {'present': True, 'value': '[host-managed endpoint]'} if name == 'base_url' else old
                changes.append({'field': key, 'before': before, 'after': {'present': False}})
                put(after, key, None)
            if target.startswith('auxiliary.') and d['effort'] is not None:
                task=target.split('.',1)[1]
                definition=AUX_DEFAULTS.get(task)
                if not isinstance(definition,dict) or 'reasoning_effort' not in definition:
                    raise BoardError(422,'Selected auxiliary task does not honor its own reasoning effort; no fields changed')
                if task=='background_review':
                    main=doc.get('model',{})
                    candidate={'provider':d['provider'],'model':d['model']} if d['route']=='explicit' else (block['value'] if block['present'] else {})
                    if d['route'] in ('inherit','auto') or candidate.get('provider') in (None,'','main','auto') or (candidate.get('provider'),candidate.get('model'))==(main.get('provider'),main.get('default')):
                        raise BoardError(422,'Background review inherits main reasoning on this route; effort-only change refused')
            updates={}
            if d['route']=='explicit':updates.update({base+'.provider':d['provider'],base+'.'+mk:d['model']})
            elif d['route'] in ('inherit','auto'):
                updates[base+'.provider']=None if target=='delegation' else ('main' if d['route']=='inherit' else 'auto')
                updates[base+'.'+mk]=None if target=='delegation' else ''
            if target=='main' and d['route']=='explicit' and block['present'] and 'context_length' in block['value'] and (block['value'].get('provider'),block['value'].get('default'))!=(d['provider'],d['model']):
                updates['model.context_length']=None
            if d['effort'] is not None:
                # Removing a nonempty shipped aux default reactivates it; the
                # host stores an explicit empty string to request model default.
                default=AUX_DEFAULTS.get(target.split('.',1)[1],{}).get('reasoning_effort') if target.startswith('auxiliary.') else None
                updates[eb+'.reasoning_effort']=d['effort'] if d['effort'] or default else None
            for key,value in updates.items():
                old=field(doc,key);new={'present':False} if value is None else {'present':True,'value':value}
                if old['present'] and not safe_value(key,old['value']):raise BoardError(422,'Stored route has unsupported value; withheld, not changed')
                if old!=new:changes.append({'field':key,'before':old,'after':new});put(after,key,value)
        if d.get('max_concurrent_children') is not None:
            key='delegation.max_concurrent_children';old=field(doc,key);value=d['max_concurrent_children'];new={'present':True,'value':value}
            if old['present'] and not safe_value(key,old['value']):raise BoardError(422,'Stored concurrency unsupported; not changed')
            if old!=new:changes.append({'field':key,'before':old,'after':new});put(after,key,value)
        if d.get('service_tier') is not None:
            if d['service_tier']=='priority' and not fast_capability(after)['supported']:raise BoardError(422,'Fast unsupported for selected profile configured route')
            key='agent.service_tier';old=field(doc,key);value='priority' if d['service_tier']=='priority' else '';new={'present':True,'value':value}
            if old['present'] and not safe_value(key,old['value']):raise BoardError(422,'Stored service tier unsupported; withheld, not changed')
            if old!=new and not (value=='' and (not old['present'] or old['value'] in ('','normal'))):changes.append({'field':key,'before':old,'after':new});put(after,key,value)
        if d.get('fallback') is not None:
            key='fallback_providers';old=field(doc,key);value=d['fallback'];new={'present':True,'value':value}
            if old!=new and not (not value and (not old['present'] or old['value'] is None or old['value']==[])):
                changes.append({'field':key,'before':old,'after':new});put(after,key,value)
        return after,changes

    def preview(self,d):
        with self.lock:
            self.validate(d);plans={}
            for p in d['profiles']:
                doc=self.read(p)
                if self.revision(doc)!=d['expected_revisions'][p]:raise BoardError(409,'Profile changed; reload and preview again')
                after,changes=self.plan(doc,d);plans[p]={'before':self.revision(doc),'changes':changes}
            now=time.monotonic();self.previews={t:v for t,v in self.previews.items() if now-v['at']<600}
            if len(self.previews)>=128:raise BoardError(429,'Too many pending previews')
            token=secrets.token_urlsafe(32);self.previews[token]={'at':now,'draft':copy.deepcopy(d),'plans':plans}
            return {'fixture':False,'token':token,'profiles':[{'profile':p,'changes':v['changes']} for p,v in plans.items()],
                    'warning':('Fast may consume more usage or cost more. ' if d.get('service_tier')=='priority' else '')+'Sequential local-profile saves. Same-process conflict protection, not cross-process locking or a fleet transaction.'}

    def apply(self,d,token,*,confirmed):
        with self.lock:
            if confirmed is not True or not isinstance(token,str) or token not in self.previews:raise BoardError(409,'Explicit confirmation and current preview required')
            pending=self.previews.pop(token)  # one confirmed attempt, even when validation fails
            if time.monotonic()-pending['at']>600 or d!=pending['draft']:raise BoardError(409,'Preview changed or expired')
            self.validate(d)
            for p,plan in pending['plans'].items():
                if self.revision(self.read(p))!=plan['before']:
                    raise BoardError(409,'Stale preview: selected profile changed; nothing written')
            results=[]
            for p,plan in pending['plans'].items():
                try:
                    doc=self.read(p)
                    if self.revision(doc)!=plan['before']:
                        results.append({'profile':p,'state':'conflict','detail':'Profile changed; not written'});continue
                    after,changes=self.plan(doc,d)
                    if changes!=plan['changes']:
                        results.append({'profile':p,'state':'conflict','detail':'Plan changed; not written'});continue
                    if changes:cb.write_config_document(self.paths[p],after)
                    saved=self.read(p)
                    if any(field(saved,c['field'])!=c['after'] for c in changes):results.append({'profile':p,'state':'uncertain','detail':'Readback mismatch; reload, never replay'})
                    else:results.append({'profile':p,'state':'applied' if changes else 'unchanged','readback':safe_routes(saved)})
                except Exception:results.append({'profile':p,'state':'uncertain','detail':'Save/readback failed; reload, never replay'})
            ok=all(r['state'] in ('applied','unchanged') for r in results)
            return {'fixture':False,'ok':ok,'partial':not ok and any(r['state']=='applied' for r in results),'profiles':results}
