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
"""
import hashlib
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

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


class HostUnavailable(Exception):
    pass


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


def board_for(request, names):
    session = getattr(request.state, 'session', None)
    principal = ('session', getattr(session, 'org_id', ''), getattr(session, 'user_id', '')) if session is not None else ('local-owner',)
    # Bind previews to the current management session without storing its secret.
    credential = request.headers.get('x-hermes-session-token', '') + '\0' + request.headers.get('cookie', '')
    principal += (hashlib.sha256(credential.encode()).hexdigest(),)
    key = (id(request.app), principal, tuple(sorted(names)))
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


async def dispatch(request, action):
    authority(request)
    data = await read_json(request) if action != 'state' else {}
    if action == 'apply' and set(data) != {'draft', 'token', 'confirmed'}:
        raise HTTPException(422, 'Exact apply envelope required')
    draft = data.get('draft') if action == 'apply' else data
    async with Host(request) as host:
        names = await roster(host)
        chosen = names if action == 'state' else selected(draft, names)
        board = board_for(request, names)
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
