"""Simulated robots the user adds with one click (no terminal, no token, no path).

Each simulated robot is the edge runtime's own ``neuxmind_runtime serve`` with
a built-in simulated body (``sim_quadruped`` / ``sim_arm``), started as a child
process of the desktop on a free localhost port with a generated token. The
desktop registers and connects it like any other robot, so everything after
that (capability table, skill install, e-stop latch and reset) is the normal,
unmodified path.

* The child process is started through :mod:`brainos.sim_child`, which exits
  cleanly when the desktop's end of its stdin closes: a simulated robot never
  outlives the app.
* The robot record keeps ``sim`` (kind, adapter); its state directory
  (installed skills, e-stop latch) lives under ``<workspace>/sims/<robot id>``,
  so the robot, its skills and a latched e-stop come back after a restart.
* Everything is labelled simulated: the runtime's manifest says
  ``simulated: true`` and the desktop shows the 仿真 badge.

Also here: the read-only live status used by the status panel, and running an
installed skill — on simulated robots only, after explicit authorization.
"""
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import connector, robot_caps, runtime_bridge
from .contracts import required_text
from .errors import CodedError, refusal_fields

KINDS = {
    'quadruped': {'adapter': 'sim_quadruped', 'family': 'quadruped', 'name': '仿真四足机器人',
                  'summary': '会走路的四足机器人：可以站立、趴下、按速度行走、导航到目标点。'},
    'arm': {'adapter': 'sim_arm', 'family': 'manipulator', 'name': '仿真机械臂',
            'summary': '六轴机械臂加夹爪：可以移动各关节、移动末端到目标位姿、开合夹爪。'},
}

START_TIMEOUT_S = 45.0
STOP_TIMEOUT_S = 10.0
DEFAULT_PROJECT = {'name': '试用仿真机器人', 'objective': '在仿真机器人上安装示例技能，观察它执行，熟悉平台。'}


def now():
    from .service import now as _now
    return _now()


def uid(prefix):
    from .service import uid as _uid
    return _uid(prefix)


class SimProcess:
    """One running ``neuxmind_runtime serve`` child."""

    def __init__(self, robot_id, adapter, state_dir, token):
        self.robot_id, self.adapter, self.state_dir = robot_id, adapter, Path(state_dir)
        self.proc = None
        self.url = None
        self._token = token
        self._lines = []

    def start(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        args = ['serve', '--adapter', self.adapter, '--host', '127.0.0.1', '--port', '0',
                '--state-dir', str(self.state_dir / 'runtime'), '--sandbox', runtime_bridge.sandbox_mode()]
        env = runtime_bridge.child_env()
        env['BRAIN_EDGE_TOKEN'] = self._token
        log = open(self.state_dir / 'runtime.log', 'ab')
        try:
            self.proc = subprocess.Popen(runtime_bridge.child_command(args), cwd=str(self.state_dir),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                         env=env, start_new_session=True)
        finally:
            log.close()
        found = threading.Event()

        def reader():
            with open(self.state_dir / 'runtime.log', 'ab') as out:
                for raw in iter(self.proc.stdout.readline, b''):
                    out.write(raw)
                    out.flush()
                    line = raw.decode('utf-8', 'replace').strip()
                    if len(self._lines) < 50:
                        self._lines.append(line)
                    if not found.is_set() and '监听 http://127.0.0.1:' in line:
                        self.url = line.rsplit(' ', 1)[-1]
                        found.set()
            found.set()

        threading.Thread(target=reader, name='nx-sim-log-' + self.robot_id, daemon=True).start()
        found.wait(START_TIMEOUT_S)
        if not self.url:
            self.stop()
            tail = '；'.join(x for x in self._lines[-3:] if x) or '没有输出'
            raise ValueError(f'仿真机器人没有启动成功（{tail}）；详情见 {self.state_dir / "runtime.log"}')
        return self.url

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()                     # SIGTERM = clean runtime shutdown
                self.proc.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
            except OSError:
                pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass


class SimRobots:
    def __init__(self, platform, root):
        self.platform = platform
        self.root = Path(root) / 'sims'
        self.procs = {}
        self.lock = threading.RLock()
        self.locks = {}
        self.closed = False

    # ------------------------------------------------------------ helpers
    def _robot_lock(self, robot_id):
        with self.lock:
            return self.locks.setdefault(robot_id, threading.Lock())

    def _sim_robot(self, robot_id):
        robot = self.platform.store.get('robots', robot_id)
        if not robot.get('sim'):
            raise ValueError('这不是仿真机器人')
        return robot

    def kinds(self):
        return [{'kind': k, **{x: v[x] for x in ('name', 'family', 'summary')}} for k, v in KINDS.items()]

    # ------------------------------------------------------------ lifecycle
    def add(self, p):
        kind = p.get('kind')
        if kind not in KINDS:
            raise ValueError('请选择仿真机器人类型：四足或机械臂')
        spec = KINDS[kind]
        if p.get('project_id'):
            project = self.platform.project(p['project_id'])
        else:
            existing = self.platform.store.list('projects')
            if existing:
                project = existing[0]
            else:
                # Q5-12: an automatically created trial project is not a task the user described.
                project = self.platform.add_project(dict(DEFAULT_PROJECT))
                project['auto_created'] = True
                self.platform.store.put('projects', project)
        taken = {r['name'] for r in self.platform.store.list('robots') if r.get('project_id') == project['id']}
        name = p.get('name') or spec['name']
        name = required_text(name, '机器人名称', 100)
        base, n = name, 2
        while name in taken:
            name, n = f'{base} {n}', n + 1
        robot = {'id': uid('robot'), 'project_id': project['id'], 'name': name, 'family': spec['family'],
                 'endpoint': '', 'token': secrets.token_urlsafe(32), 'status': 'unconnected', 'capabilities': [],
                 'sim': {'kind': kind, 'adapter': spec['adapter'], 'enabled': True, 'state': 'starting'},
                 'created_at': now()}
        self.platform.store.put('robots', robot)
        self.platform.audit('robot.sim_added', {'robot_id': robot['id'], 'kind': kind}, project['id'])
        robot = self.start(robot['id'])
        return {**robot, 'project_id': project['id']}

    def start(self, robot_id):
        """Start (or adopt the running) child process and connect the robot. Returns the redacted robot."""
        with self._robot_lock(robot_id):
            robot = self._sim_robot(robot_id)
            if self.closed:
                raise ValueError('桌面服务正在退出')
            current = self.procs.get(robot_id)
            if current and current.alive and robot.get('endpoint') == current.url:
                return self.platform.redact(robot)
            if current:
                current.stop()
            robot['sim'].update(state='starting', enabled=True)
            robot['sim'].pop('error', None)
            self.platform.store.put('robots', robot)
            proc = SimProcess(robot_id, robot['sim']['adapter'], self.root / robot_id, robot['token'])
            try:
                url = proc.start()
            except (ValueError, OSError) as exc:
                robot = self.platform.store.get('robots', robot_id)
                robot['sim'].update(state='failed', error=str(exc))
                robot.update(status='error', error=str(exc))
                self.platform.store.put('robots', robot)
                raise ValueError(str(exc)) from None
            with self.lock:
                self.procs[robot_id] = proc
            robot = self.platform.store.get('robots', robot_id)
            robot['endpoint'] = url
            robot['sim'].update(state='running', started_at=now())
            self.platform.store.put('robots', robot)
            try:
                self.platform.connect_robot({'robot_id': robot_id})
                robot_caps.refresh(self.platform, self.platform.store.get('robots', robot_id))
            except ValueError as exc:
                robot = self.platform.store.get('robots', robot_id)
                robot['sim'].update(state='failed', error='仿真机器人已启动，但连接检查没有通过：' + str(exc))
                self.platform.store.put('robots', robot)
                raise
            return self.platform.redact(self.platform.store.get('robots', robot_id))

    def stop_process(self, robot_id, disable=True):
        with self._robot_lock(robot_id):
            with self.lock:
                proc = self.procs.pop(robot_id, None)
            if proc:
                proc.stop()
            try:
                robot = self._sim_robot(robot_id)
            except ValueError:
                return None
            robot['sim']['state'] = 'stopped'
            if disable:
                robot['sim']['enabled'] = False
            robot['status'] = 'unconnected'
            self.platform.store.put('robots', robot)
            return self.platform.redact(robot)

    def resume_all(self):
        """Bring back the simulated robots that were running when the app last closed."""
        robots = [r for r in self.platform.store.list('robots') if r.get('sim') and r['sim'].get('enabled')]
        for robot in robots:
            robot['sim']['state'] = 'starting'
            self.platform.store.put('robots', robot)

        def work():
            for robot in robots:
                if self.closed:
                    return
                try:
                    self.start(robot['id'])
                except Exception:
                    pass                                   # recorded on the robot (sim.state = failed)
        if robots:
            threading.Thread(target=work, name='nx-sim-resume', daemon=True).start()

    def remove(self, p):
        robot = self._sim_robot(p.get('robot_id'))
        self.stop_process(robot['id'])
        self.platform.store.delete('robots', robot['id'])
        shutil.rmtree(self.root / robot['id'], ignore_errors=True)
        self.platform.audit('robot.sim_removed', {'robot_id': robot['id']}, robot['project_id'])
        return {'removed': robot['id']}

    def close(self):
        self.closed = True
        with self.lock:
            procs = list(self.procs.items())
            self.procs.clear()
        for robot_id, proc in procs:
            proc.stop()
            try:
                robot = self.platform.store.get('robots', robot_id)
                robot['sim']['state'] = 'stopped'           # `enabled` stays: it comes back next start
                robot['status'] = 'unconnected'
                self.platform.store.put('robots', robot)
            except Exception:
                pass

    def process_view(self, robot):
        proc = self.procs.get(robot['id'])
        sim = robot.get('sim') or {}
        state = sim.get('state', 'stopped')
        if state == 'running' and not (proc and proc.alive):
            state = 'exited'
        return {'state': state, 'pid': proc.pid if proc and proc.alive else None, 'error': sim.get('error'),
                'kind': sim.get('kind'), 'adapter': sim.get('adapter')}

    # ------------------------------------------------------------ API
    def api_start(self, p):
        return self.start(self._sim_robot(p.get('robot_id'))['id'])

    def api_stop(self, p):
        return self.stop_process(self._sim_robot(p.get('robot_id'))['id'])

    def api_restart(self, p):
        """Restart the simulated robot's runtime process (ux04, Q5-03).

        The operator's way out when the runtime is stuck, e.g. an adapter reset that
        never returns (409 ``reset_in_progress``). Restarting never clears the e-stop:
        the latch lives in the robot's state directory and is still set afterwards, so
        the user then confirms on-site safety and resets as usual.
        """
        robot = self._sim_robot(p.get('robot_id'))
        if p.get('project_id') and robot.get('project_id') != p['project_id']:
            raise ValueError('该机器人不属于当前项目')
        self.stop_process(robot['id'], disable=False)
        self.platform.audit('robot.sim_restarted', {'robot_id': robot['id']}, robot['project_id'])
        return self.start(robot['id'])


# ---------------------------------------------------------------- live status
def _get(robot, path, timeout=3):
    return connector.call(robot['endpoint'], path, robot.get('token', ''), timeout=timeout)


def _yaw(q):
    import math
    try:
        x, y, z, w = (float(v) for v in q)
    except (TypeError, ValueError):
        return None
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def _condense(state):
    out = {}
    odom = state.get('base.odometry') or state.get('localization.pose')
    if isinstance(odom, dict) and isinstance(odom.get('pose'), list) and len(odom['pose']) == 7:
        pose = odom['pose']
        out['pose'] = {'x': pose[0], 'y': pose[1], 'yaw': _yaw(pose[3:7])}
        if isinstance(odom.get('velocity'), list):
            out['velocity'] = odom['velocity'][:3]
    joints = state.get('joint.state')
    if isinstance(joints, dict) and isinstance(joints.get('position'), list):
        names = joints.get('names') if isinstance(joints.get('names'), list) else []
        out['joints'] = [{'name': str(names[i]) if i < len(names) else f'joint{i + 1}', 'position': v}
                         for i, v in enumerate(joints['position'][:32])]
    gripper = state.get('gripper.state')
    if isinstance(gripper, dict):
        out['gripper'] = {'opening': gripper.get('opening'), 'grasped': gripper.get('grasped')}
    battery = state.get('sensor.battery')
    if isinstance(battery, dict) and isinstance(battery.get('percent'), (int, float)):
        out['battery'] = battery['percent']
    health = state.get('system.health')
    if isinstance(health, dict):
        out['mode'] = health.get('mode')
        out['healthy'] = health.get('ok') is True
    return out


def _run_view(run):
    events = run.get('events') if isinstance(run.get('events'), list) else []
    messages = []
    for e in events:
        if not isinstance(e, dict) or not e.get('message'):
            continue
        messages.append({'text': str(e['message'])[:200], 'type': str(e.get('type', '')), 'ts': e.get('ts')})
    messages = messages[-8:]
    return {'id': run.get('id'), 'target': run.get('target'), 'kind': run.get('kind'), 'status': run.get('status'),
            'progress': run.get('progress'), 'error': run.get('error'), 'stop_reason': run.get('stop_reason'),
            'created_at': run.get('created_at'), 'ended_at': run.get('ended_at'),
            'result': run.get('result') if isinstance(run.get('result'), (dict, list, str, int, float)) else None,
            'messages': messages}


# ---------------------------------------------------------------- latch guard, in plain words (ux05)
# The runtime's latch guard counters (``/v1/status.latched_motion``, INTERFACES §7-§8) are cumulative since the
# runtime started. The live view talks about *this* e-stop, so the baseline is the counters the desktop last saw
# while the robot was NOT latched (they only grow while latched). Without such a sighting (the desktop opened
# during a latch) the view says the counts are since the runtime started.
STOP_NOT_OBEYED = 'safety.stop_not_obeyed'
_TELEMETRY_BACKLOG = 500          # events looked at when a latch is first seen without a cursor
_TELEMETRY_PAGES = 3


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _num_or_none(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float('inf') else None


def _scalar_fields(d, limit=15):
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in list(d.items())[:40]:
        if len(out) >= limit or not isinstance(k, str):
            continue
        if isinstance(v, str):
            out[k] = v[:300]
        elif isinstance(v, bool) or v is None or _num_or_none(v) is not None:
            out[k] = v
    return out


_WATCH_LOCK = threading.Lock()


def _watch(platform, robot_id):
    with _WATCH_LOCK:
        watches = vars(platform).setdefault('_latch_watch', {})
        return watches.setdefault(robot_id, {'base': None, 'epoch': None, 'cursor': None, 'event': None})


def latch_view(platform, robot, status, latched):
    """What the latch guard did during this e-stop, reduced to what the live view says (no raw evidence)."""
    lm = status.get('latched_motion')
    w = _watch(platform, robot['id'])
    counters = {k: _count((lm or {}).get(k)) for k in ('restops', 'invalid_restops', 'seen', 'feedback_invalid')}
    epoch = status.get('stop_epoch', status.get('stop_count'))
    if not latched:
        w.update(base=counters, epoch=None)
    elif w['epoch'] != epoch:
        # a new latch: the baseline is the last unlatched sighting, if any
        w['epoch'] = epoch
        w['since'] = 'latch' if w['base'] is not None else 'runtime_start'
        w['episode_base'] = dict(w['base'] or {k: 0 for k in counters})
    if not isinstance(lm, dict):
        return None
    if lm.get('enabled') is not True:
        return {'enabled': False}
    view = {'enabled': True, 'active': lm.get('active') is True}
    if latched:
        base = w.get('episode_base') or {}
        for k in counters:
            view[k] = max(0, counters[k] - base.get(k, 0))
        view['since'] = w.get('since', 'latch')
    view['feedback_ok'] = lm.get('feedback_ok') is not False
    problems = lm.get('feedback_problems')
    view['feedback_problems'] = sorted(str(k)[:60] for k in problems)[:5] if isinstance(problems, dict) else []
    for k in ('read_in_progress_s', 'max_drift_m', 'max_drift_rad', 'settle_s'):
        view[k] = _num_or_none(lm.get(k))
    view['displaced'] = lm.get('displaced') is True
    # round 07 fix2: one non-escalating notice per latch when the body left its stop position (walk or real motion)
    if lm.get('drift_notice') is True:
        d = lm.get('drift_notice_detail')
        msg = d.get('message') if isinstance(d, dict) else None
        view['drift_notice'] = {'message': str(msg)[:300] if msg else '急停期间机器人离停下位置已超过首次发现界，请到现场确认',
                                'channel': str(d.get('channel'))[:60] if isinstance(d, dict) and d.get('channel') else None,
                                'ts': _num_or_none(lm.get('drift_notice_at'))}
    # the runtime may also flag a stop that is not obeyed on the status itself (round 07)
    flag = lm.get('stop_not_obeyed')
    if flag:
        detail = flag if isinstance(flag, dict) else lm.get('stop_not_obeyed_detail')
        view['stop_not_obeyed'] = _scalar_fields(detail) if isinstance(detail, dict) and detail else {'flag': True}
        at = lm.get('stop_not_obeyed_at')
        if 'ts' not in view['stop_not_obeyed'] and _num_or_none(at) is not None:
            view['stop_not_obeyed']['ts'] = at
    return view


def stop_not_obeyed(platform, robot, status, latched):
    """The latest ``safety.stop_not_obeyed`` event of this e-stop (``/v2/telemetry``), or None.

    Read only while latched; cleared when the robot is seen unlatched again (someone confirmed on site and
    released the e-stop). Telemetry problems never break the live view: the last answer is kept.
    """
    w = _watch(platform, robot['id'])
    telemetry = status.get('telemetry') if isinstance(status.get('telemetry'), dict) else {}
    last = telemetry.get('last_seq')
    last = last if isinstance(last, int) and not isinstance(last, bool) else None
    if not latched:
        w.update(cursor=last, event=None)
        return None
    cursor = w['cursor']
    if cursor is None or (last is not None and last < cursor):      # first sight, or a fresh telemetry ring
        cursor = max(0, (last or 0) - _TELEMETRY_BACKLOG)
    try:
        for _ in range(_TELEMETRY_PAGES):
            page = _get(robot, '/v2/telemetry?since=' + str(int(cursor)))
            events = page.get('events') if isinstance(page.get('events'), list) else []
            for e in events:
                if isinstance(e, dict) and e.get('type') == STOP_NOT_OBEYED:
                    w['event'] = _scalar_fields(e)
                elif isinstance(e, dict) and e.get('type') == 'safety.latch_guard_relaxed':
                    w['relaxed_event'] = _scalar_fields(e)       # ux05-fix: calibration loosened (Q7-05)
                elif isinstance(e, dict) and e.get('type') == 'safety.reset':
                    w['event'] = None            # an earlier e-stop, released on site since
            nxt = page.get('last_seq')
            if not isinstance(nxt, int) or isinstance(nxt, bool) or nxt <= cursor or not events:
                break
            cursor = nxt
    except connector.DeviceError:
        pass
    w['cursor'] = cursor
    return w['event']


def _reset_facts(status, robot):
    """``{reset_expire, expire_s, in_progress_s}`` for wording a reset that has not returned (Q7b-05/08)."""
    from .service import reset_policy
    policy = reset_policy(status, robot.get('manifest'))
    s = status.get('reset_in_progress_s')
    return {'reset_expire': policy['reset_expire'], 'expire_s': policy['expire_s'],
            'in_progress_s': s if isinstance(s, (int, float)) and not isinstance(s, bool) and s == s and s >= 0 else None}


def live(platform, p):
    """Read-only live view: device status, condensed state and the latest run. Nothing moves."""
    robot = platform.store.get('robots', p.get('robot_id'))
    if p.get('project_id') and robot.get('project_id') != p['project_id']:
        raise ValueError('该机器人不属于当前项目')
    view = {'robot_id': robot['id'], 'name': robot['name'], 'family': robot['family'],
            'simulated': bool(robot.get('sim')) or (robot.get('manifest') or {}).get('simulated') is True,
            'process': platform.sims.process_view(robot) if robot.get('sim') else None,
            'checked_at': now(), 'reachable': False}
    if not robot.get('endpoint'):
        view['error'] = '这台机器人没有设备端地址'
        return view
    if robot.get('sim') and view['process']['state'] != 'running':
        view['error'] = {'starting': '仿真机器人正在启动…', 'stopped': '仿真机器人已关闭',
                         'failed': view['process'].get('error') or '仿真机器人启动失败',
                         'exited': '仿真机器人进程已退出'}.get(view['process']['state'], '仿真机器人未运行')
        return view
    try:
        status = _get(robot, '/v1/status')
        state = _get(robot, '/v2/state')
        runs = _get(robot, '/v2/runs').get('runs') or []
    except connector.DeviceError as exc:
        view['error'] = f'暂时读不到机器人状态：{exc}'
        return view
    view['reachable'] = True
    latched = status.get('estop_latched') is True
    if robot.get('estop_latched') is not latched:
        robot = platform.store.get('robots', robot['id'])
        platform.note_estop(robot, latched, status.get('latch_reason'))
        platform.store.put('robots', robot)
    view.update(estop={'latched': latched, 'reason': status.get('latch_reason')},
                reset_in_progress=status.get('reset_in_progress') is True,
                # ux05-fix2 (Q7b-05/08): whether this runtime voids a late reset, and the limit the desktop sends.
                reset=_reset_facts(status, robot),
                mode=status.get('mode'), active_runs=status.get('active_runs') or [],
                state=_condense(state if isinstance(state, dict) else {}))
    # ux05: the latch guard in plain words, and "stop not obeyed" (use the hardware e-stop)
    view['latched_motion'] = latch_view(platform, robot, status, latched)
    event = stop_not_obeyed(platform, robot, status, latched)
    flag = (view['latched_motion'] or {}).get('stop_not_obeyed') if latched else None
    if event or flag:
        view['stop_not_obeyed'] = event or flag
    skills = [r for r in runs if isinstance(r, dict) and r.get('kind') == 'skill']
    if skills:
        try:
            view['run'] = _run_view(_get(robot, '/v2/runs/' + quote(str(skills[0]['id']), safe='')))
        except connector.DeviceError:
            view['run'] = _run_view(skills[0])
    view['recent_runs'] = [{k: r.get(k) for k in ('id', 'target', 'status', 'created_at')} for r in skills[:5]]
    return view


# ---------------------------------------------------------------- running a skill
def run_skill(platform, p):
    """POST /v2/skills/<id>/run on a simulated robot, after explicit authorization.

    ``request_id`` is idempotent here (same id → recorded outcome, never sent
    twice) and on the device (ledger). Real robots run skills through
    applications and on-site tests, not from this shortcut.
    """
    if p.get('hardware_authorized') is not True:
        raise CodedError('运行技能会让机器人运动，需要先勾选现场安全确认并授权', 'authorization_required')
    request_id = required_text(p.get('request_id'), '请求标识', 100)
    op_id = 'skill-run-' + request_id
    existing = next((o for o in platform.store.list('operations') if o['id'] == op_id), None)
    if existing:
        if existing.get('robot_id') != p.get('robot_id'):
            raise ValueError('请求标识已用于其他操作')
        return existing
    robot = platform.store.get('robots', p.get('robot_id'))
    if p.get('project_id') and robot.get('project_id') != p['project_id']:
        raise ValueError('该机器人不属于当前项目')
    if not robot.get('sim'):
        raise CodedError('直接运行技能目前只对仿真机器人开放；真机请在“机器人应用”中完成现场测试后部署', 'simulated_only')
    skill_id = required_text(p.get('skill_id'), '技能', 100)
    parameters = p.get('parameters', {})
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters or '{}')
        except ValueError:
            raise ValueError('运行参数不是合法 JSON') from None
    if not isinstance(parameters, dict):
        raise ValueError('运行参数须为 JSON 对象')
    platform.connect_robot({'robot_id': robot['id']})          # fresh identity and e-stop state
    robot = platform.store.get('robots', robot['id'])
    if robot.get('estop_latched') is True:
        raise CodedError('急停已锁存：请先确认现场安全并“解除急停”，再运行技能', 'estop_latched')
    # ux05-fix (Q7-04): numbers go out with the type the installed skill declared (1.0 stays 1.0).
    installed = robot_caps.installed_skills(robot)
    decls = next((s.get('parameters') for s in installed.get('items', []) if s.get('id') == skill_id), None)
    if decls:
        parameters = robot_caps.typed_parameters(decls, parameters, device_decls=True)   # the device's own declaration
    op = {'id': op_id, 'project_id': robot['project_id'], 'robot_id': robot['id'], 'kind': 'skill_run',
          'skill_id': skill_id, 'parameters': parameters, 'status': 'dispatching', 'created_at': now()}
    platform.store.put('operations', op)
    try:
        response = connector.call(robot['endpoint'], '/v2/skills/' + quote(skill_id, safe='') + '/run',
                                  robot.get('token', ''),
                                  {'request_id': op_id, 'parameters': parameters, 'hardware_authorized': True},
                                  timeout=10)
        if not isinstance(response.get('run_id'), str):
            raise connector.DeviceError('设备没有返回运行编号', status=200, sent=True)
        op.update(status='accepted', run_id=response['run_id'], run_status=response.get('status'))
    except connector.DeviceError as exc:
        if exc.code:
            op['code'] = exc.code
        if exc.status is not None:
            op['device_status'] = exc.status
        if exc.status is not None and 400 <= exc.status < 500:
            # ux04 (Q5-05): keep the device's field-level reason (param, min, max, choices) for the form.
            op.update(status='rejected', error=f'设备拒绝运行：{exc}', **refusal_fields(exc.body))
        elif exc.sent is False:
            op.update(status='failed', error='请求未发出：' + str(exc))
        else:
            op.update(status='unknown', error=f'{exc}；运行结果未知，不会自动重试，请看实时状态')
    platform.store.put('operations', op)
    platform.audit('skill.run', {'operation_id': op_id, 'robot_id': robot['id'], 'skill': skill_id,
                                 'status': op['status']}, robot['project_id'])
    return op


# ---------------------------------------------------------------- example gallery
# Parameters a first-time user can run as-is (shown and editable before running).
EXAMPLE_PRESETS = {
    'patrol': {'waypoints': [[1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]], 'loops': 1},
    'wave': {'cycles': 2, 'amplitude': 0.6},
    'status_report': {},
}
EXAMPLE_NOTES = {
    'patrol': '让四足机器人依次走到 4 个航点，绕一个 1 米见方的正方形回到原点。',
    'wave': '让机械臂抬起、左右摆动并开合夹爪，最后回到零位。',
    'status_report': '只读取机器人状态（电量、位置、关节等）并生成报告，不会让机器人运动。',
}


def example_path(example_id):
    root = runtime_bridge.examples_dir()
    if root is None:
        raise ValueError('这个安装包里没有示例技能')
    if not isinstance(example_id, str) or example_id not in {d.name for d in root.iterdir() if d.is_dir()}:
        raise ValueError('找不到该示例技能')
    return str(root / example_id)


def gallery(platform, p):
    """Bundled example skills with compatibility per robot of the project (read-only)."""
    from . import skill_packages
    root = runtime_bridge.examples_dir()
    robots = [r for r in platform.store.list('robots')
              if r.get('project_id') == p.get('project_id') and r.get('status') == 'connected']
    focus = p.get('robot_id')
    manifests = {}
    for r in robots:
        if r['id'] == focus:
            try:
                manifests[r['id']] = robot_caps.fetch_manifest_v2(r)
                continue
            except ValueError:
                pass
        if r.get('manifest_v2'):
            manifests[r['id']] = r['manifest_v2']
    items = []
    for folder in sorted(root.iterdir()) if root else []:
        if not (folder / 'skill.json').is_file():
            continue
        try:
            package, info = skill_packages.read_folder(str(folder), bundled=True)
            skill = skill_packages.validate(package)
        except ValueError as exc:
            items.append({'example': folder.name, 'valid': False, 'error': str(exc)})
            continue
        view = skill_packages._skill_view(skill)
        compat = []
        for r in robots:
            if r['id'] not in manifests:
                continue
            c = skill_packages.explain(skill, manifests[r['id']])
            compat.append({'robot_id': r['id'], 'robot_name': r['name'], 'compatible': c['compatible'],
                           'reasons': [m['text'] for m in c['missing_detail']] + c['violation_texts']})
        moves = any(robot_caps.capability_row(x['id'])['safety'] == 'motion' for x in skill['requires'])
        items.append({'example': folder.name, 'valid': True, 'skill': view, 'moves': moves,
                      'note': EXAMPLE_NOTES.get(folder.name, ''), 'preset': EXAMPLE_PRESETS.get(folder.name, {}),
                      'package_digest': skill_packages.package_digest(package), 'compatibility': compat})
    return {'available': root is not None, 'items': items}
