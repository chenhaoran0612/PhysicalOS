"""Safety watch for every connected robot of a project (ux05-fix, Q7-03 / Q7-05 / Q7-11).

The desktop shows "请立即使用硬件急停" wherever the user is in the app -- not only in the simulated
robot's live dialog -- when a robot keeps moving after repeated stops (``safety.stop_not_obeyed``,
INTERFACES §9). The browser polls ``robots/safety`` every few seconds; this module keeps that cheap
and bounded:

* only read-only GETs reach the devices (``/v1/status``; ``/v2/telemetry`` only while latched);
* each robot is asked at most every :data:`LATCHED_EVERY_S` while it is (or was last seen) latched
  or alerting, and every :data:`QUIET_EVERY_S` otherwise (to notice a latch made elsewhere, e.g.
  from a teach pendant, and the calibration state); a robot the user is looking at
  (``focus``) at most every :data:`FOCUS_EVERY_S`;
* devices are asked in parallel with a short timeout, so one unreachable robot never delays the
  others or the page;
* an alert seen once stays until the robot is seen unlatched (safe side): a robot that stops
  answering keeps its banner, marked "状态读不到".

Also reported per robot: the latch guard in plain words (shared with the live view) and whether the
runtime says its latch-guard calibration was loosened (``latched_motion.relaxed`` /
``safety.latch_guard_relaxed``).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import connector
from .sim_robots import latch_view, stop_not_obeyed, _scalar_fields, _reset_facts

LATCHED_EVERY_S = 2.0
QUIET_EVERY_S = 10.0
FOCUS_EVERY_S = 1.0
STATUS_TIMEOUT_S = 2.0
MAX_ROBOTS = 64
RELAXED_EVENT = 'safety.latch_guard_relaxed'

_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='nx-safety')


def _cache(platform):
    with _LOCK:
        return vars(platform).setdefault('_safety_watch', {})


def _hardware_estop(robot):
    """What the robot itself declares (manifest ``safety.hardware_estop`` or the ``safety.stop`` param)."""
    manifest = robot.get('manifest_v2') or robot.get('manifest') or {}
    safety = manifest.get('safety') if isinstance(manifest.get('safety'), dict) else {}
    if isinstance(safety.get('hardware_estop'), bool):
        return safety['hardware_estop']
    for cap in manifest.get('capabilities') or []:
        if isinstance(cap, dict) and cap.get('id') == 'safety.stop' and isinstance(cap.get('params'), dict):
            if isinstance(cap['params'].get('hardware_estop'), bool):
                return cap['params']['hardware_estop']
    return None


def _latch_defaults():
    try:
        from .spec_bridge import manifest as spec_manifest
        return {k: v[0] for k, v in spec_manifest.LATCH_GUARD_FIELDS.items()}
    except (ImportError, AttributeError):
        return {}


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _relaxed_keys(lm):
    """``latched_motion.relaxed_keys`` = ``[{key, value, default, source}]`` (round 07 fix runtime), or None."""
    raw = lm.get('relaxed_keys')
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:20]:
        if isinstance(item, dict) and isinstance(item.get('key'), str):
            out.append({'key': item['key'][:40], 'value': _number(item.get('value')),
                        'default': _number(item.get('default')),
                        'source': str(item['source'])[:20] if isinstance(item.get('source'), str) else None})
        elif isinstance(item, str):
            out.append({'key': item[:40], 'value': None, 'default': None, 'source': None})
    return out


def relaxed_view(status, lm):
    """The runtime's "calibration loosened" flag (round 07 fix), whatever spelling it arrives in:
    ``latched_motion.relaxed`` (bool / dict / list of keys), ``latched_motion.latch_guard_relaxed``,
    ``status.safety.latch_guard_relaxed`` or ``status.latch_guard_relaxed``. None when not loosened.

    ux05-fix2 (Q7b-07): which keys are loosened comes from the runtime's own ``relaxed_keys`` when it sends
    them. Otherwise a key is listed only when it is known to be *looser* than the default (every latch-guard
    key loosens the guard when larger, INTERFACES §9.1) -- a stricter field value (``pos_m = 0.03``) is
    calibration, never "放宽"."""
    lm = lm if isinstance(lm, dict) else {}
    safety = status.get('safety') if isinstance(status.get('safety'), dict) else {}
    keys = _relaxed_keys(lm)
    flag = None
    for candidate in (lm.get('relaxed'), lm.get('latch_guard_relaxed'), safety.get('latch_guard_relaxed'),
                      status.get('latch_guard_relaxed')):
        if candidate not in (None, False, [], {}):
            flag = candidate
            break
    if keys is not None and flag is None and not keys:
        return None
    if flag is None and not keys:
        return None
    config = lm.get('config') if isinstance(lm.get('config'), dict) else {}
    sources = lm.get('config_source') if isinstance(lm.get('config_source'), dict) else {}
    view = {'relaxed': True, 'fields': [], 'notes': [], 'values': {}, 'defaults': {}}
    if keys:
        view['fields'] = [k['key'] for k in keys][:10]
        view['values'] = {k['key']: k['value'] for k in keys if k['value'] is not None}
        view['defaults'] = {k['key']: k['default'] for k in keys if k['default'] is not None}
        view['sources'] = sorted({k['source'] for k in keys if k['source'] and k['source'] != 'default'})
        view['from'] = 'relaxed_keys'
    else:
        candidates = []
        if isinstance(flag, dict):
            candidates = flag.get('fields') if isinstance(flag.get('fields'), list) else list(flag.keys())
            view.update({k: v for k, v in _scalar_fields(flag, 8).items() if k in ('message', 'note', 'source')})
        elif isinstance(flag, list):
            candidates = flag
        if not candidates and isinstance(lm.get('relaxed_fields'), list):
            candidates = lm['relaxed_fields']
        named = bool(candidates)
        if not candidates:
            candidates = [k for k, v in sources.items() if v not in ('default', None)]
        defaults = _latch_defaults()
        fields = []
        for key in candidates:
            if not isinstance(key, str):
                continue
            value, default = _number(config.get(key)), defaults.get(key)
            if value is not None and default is not None:
                if value <= default + 1e-12:
                    continue                  # equal or stricter: not loosened
            elif not named:
                continue                      # cannot tell the direction: do not call it loosened
            fields.append(key[:40])
            if value is not None:
                view['values'][key[:40]] = value
            if default is not None:
                view['defaults'][key[:40]] = default
        view['fields'] = sorted(fields)[:10]
        view['sources'] = sorted({str(v)[:20] for k, v in sources.items() if k in view['fields'] and v != 'default'})
        view['from'] = 'compare'
    notes = lm.get('config_notes') if isinstance(lm.get('config_notes'), list) else []
    view['notes'] = [str(n)[:200] for n in notes if isinstance(n, str)][:5]
    return view


def calibration_view(lm):
    """Latch-guard settings that do not come from the runtime's defaults (manifest / runtime / adapter),
    with their values -- shown on the robot's detail page even when the runtime does not call them loosened."""
    if not isinstance(lm, dict) or not isinstance(lm.get('config_source'), dict):
        return None
    config = lm.get('config') if isinstance(lm.get('config'), dict) else {}
    fields = {}
    for key, source in list(lm['config_source'].items())[:20]:
        if isinstance(key, str) and isinstance(source, str) and source != 'default':
            value = config.get(key)
            fields[key[:40]] = {'source': source[:20],
                                'value': value if isinstance(value, (int, float)) and not isinstance(value, bool) else None}
    if not fields:
        return None
    notes = lm.get('config_notes') if isinstance(lm.get('config_notes'), list) else []
    return {'fields': fields, 'notes': [str(n)[:200] for n in notes if isinstance(n, str)][:5]}


def _fetch(robot):
    try:
        return {'status': connector.call(robot['endpoint'], '/v1/status', robot.get('token', ''),
                                         timeout=STATUS_TIMEOUT_S)}
    except connector.DeviceError as exc:
        return {'error': str(exc)}
    except Exception as exc:          # a malformed answer never breaks the watch
        return {'error': f'状态读取失败：{exc}'}


def _robot_entry(robot):
    sim = robot.get('sim') or {}
    simulated = bool(sim) or (robot.get('manifest') or {}).get('simulated') is True
    return {'robot_id': robot['id'], 'name': robot.get('name') or robot['id'], 'family': robot.get('family'),
            'simulated': simulated, 'desktop_sim': bool(sim), 'hardware_estop': _hardware_estop(robot)}


def _due(entry, now_s, focus):
    if entry is None:
        return True
    age = now_s - entry['_at']
    if focus:
        return age >= FOCUS_EVERY_S
    alerting = entry.get('latched') or entry.get('stop_not_obeyed')
    return age >= (LATCHED_EVERY_S if alerting else QUIET_EVERY_S)


def _apply(platform, robot, entry, fetched):
    status = fetched.get('status')
    if status is None:
        # Keep what we knew (an alert never disappears because the robot stopped answering).
        entry['reachable'] = False
        entry['error'] = fetched.get('error') or '读不到状态'
        return entry
    if not isinstance(status, dict):
        status = {}
    latched = status.get('estop_latched') is True
    if robot.get('estop_latched') is not latched and isinstance(status.get('estop_latched'), bool):
        fresh = platform.store.get('robots', robot['id'])
        platform.note_estop(fresh, latched, status.get('latch_reason'))
        platform.store.put('robots', fresh)
    lm_raw = status.get('latched_motion') if isinstance(status.get('latched_motion'), dict) else None
    view = latch_view(platform, robot, status, latched)
    event = stop_not_obeyed(platform, robot, status, latched)
    flag = None
    if latched and lm_raw and lm_raw.get('stop_not_obeyed'):
        detail = lm_raw.get('stop_not_obeyed_detail')
        flag = _scalar_fields(detail) if isinstance(detail, dict) and detail else (view or {}).get('stop_not_obeyed')
        if isinstance(flag, dict) and 'ts' not in flag and isinstance(lm_raw.get('stop_not_obeyed_at'), (int, float)):
            flag = {**flag, 'ts': lm_raw['stop_not_obeyed_at']}
    entry.update(reachable=True, error=None, latched=latched, reason=status.get('latch_reason') if latched else None,
                 reset_in_progress=status.get('reset_in_progress') is True, reset=_reset_facts(status, robot),
                 latched_motion=view,
                 relaxed=relaxed_view(status, lm_raw), calibration=calibration_view(lm_raw))
    if view and view.get('stop_not_obeyed') and flag is None and latched:
        flag = view['stop_not_obeyed']
    entry['stop_not_obeyed'] = (event or flag) if latched else None
    if not entry.get('relaxed'):
        ev = vars(platform).get('_latch_watch', {}).get(robot['id'], {}).get('relaxed_event')
        if ev:
            entry['relaxed'] = {'relaxed': True, 'fields': [], 'notes': [], 'sources': [], 'values': {},
                                **{k: v for k, v in ev.items() if k in ('message', 'note')}}
    return entry


def watch(platform, p):
    """``robots/safety``: e-stop latch, latch guard, "stop not obeyed" and calibration state of every
    connected robot of the project -- or, with ``all_projects: true`` (ux05-fix2, Q7b-10), of every project,
    so the banner names a robot of project B while the user works in project A. Read-only; nothing moves.
    The per-robot cadence above bounds the device traffic however often the page asks."""
    project_id = p.get('project_id')
    every = p.get('all_projects') is True
    focus = p.get('robot_id') if isinstance(p.get('robot_id'), str) else None
    robots = [r for r in platform.store.list('robots')
              if (every or r.get('project_id') == project_id) and r.get('endpoint')
              and (r.get('status') == 'connected' or r.get('estop_latched') is True)]
    # The project in view first, then latched robots, so the cap never drops the robots that matter.
    robots.sort(key=lambda r: (r.get('project_id') != project_id, r.get('estop_latched') is not True))
    robots = robots[:MAX_ROBOTS]
    names = {}
    if every:
        for pr in platform.store.list('projects'):
            names[pr.get('id')] = pr.get('name') or pr.get('id')
    cache = _cache(platform)
    now_s = time.monotonic()
    due = []
    for robot in robots:
        if robot.get('sim') and platform.sims.process_view(robot)['state'] != 'running':
            cache.pop(robot['id'], None)           # a closed simulated robot cannot move
            continue
        prior = cache.get(robot['id'])
        if robot.get('estop_latched') is True and prior is not None and not prior.get('latched'):
            prior = None                           # latched elsewhere (stop button, reset): look now
        if _due(prior, now_s, robot['id'] == focus):
            due.append(robot)
    futures = {r['id']: _POOL.submit(_fetch, r) for r in due}
    for robot in due:
        try:
            fetched = futures[robot['id']].result(timeout=STATUS_TIMEOUT_S + 1)
        except Exception as exc:
            fetched = {'error': f'状态读取超时：{exc}'}
        entry = dict(cache.get(robot['id']) or {})
        entry.update(_robot_entry(robot))
        try:
            entry = _apply(platform, robot, entry, fetched)
        except connector.DeviceError as exc:
            entry.update(reachable=False, error=str(exc))
        entry['_at'] = time.monotonic()
        entry['_project'] = robot.get('project_id')
        entry['checked_at'] = time.time()
        cache[robot['id']] = entry
    keep = {r['id'] for r in robots}
    out = []
    for robot in robots:
        entry = cache.get(robot['id'])
        if not entry:
            continue
        entry.update(_robot_entry(robot))
        entry['project_id'] = robot.get('project_id')
        if every:
            entry['project_name'] = names.get(robot.get('project_id'))
        if robot.get('estop_latched') is False and entry.get('latched') and robot['id'] not in futures:
            # reset through the desktop since the last look: the alert goes with the latch (re-read next time)
            entry.update(latched=False, stop_not_obeyed=None)
            entry['_at'] = 0
        out.append({k: v for k, v in entry.items() if not k.startswith('_')})
    for rid in list(cache):
        if rid not in keep and (every or cache[rid].get('_project') == project_id):
            cache.pop(rid, None)
    alerts = [e['robot_id'] for e in out if e.get('stop_not_obeyed')]
    return {'robots': out, 'alerts': alerts, 'latched': [e['robot_id'] for e in out if e.get('latched')],
            'relaxed': [e['robot_id'] for e in out if e.get('relaxed')],
            'poll_s': LATCHED_EVERY_S if any(e.get('latched') for e in out) else 3.0,
            'all_projects': every}
