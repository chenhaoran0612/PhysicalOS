"""The simulated Microduck as a robot body (issue #63, docs/DUCK_SKILLS.md).

The edge runtime loads it like any other body (``serve --adapter brainos.duck_body:body``): the robot is
online, reports its health, and ``safety.stop`` stops it. What moves is one child process with the physics
(``brainos.duck_engine``): the duck stands at HOME, and performs a show when the console asks for one.

Shows are not a capability of the standard, and the runtime does not run motion it cannot bound (vendor
extensions ``x.*`` are refused by the safety supervisor), so a show is not started through the runtime. The
console writes ``<control>/command.json`` (``{"id", "cmd": "show" | "stop" | "home", "show", "seed"}``) and
this body answers in ``<control>/status.json``. Both are files in the robot's own state directory, like the
picture of a MuJoCo simulated robot. A stop through the runtime (the stop button, the e-stop) ends a show
the same way.

Settings come from ``duck.json`` in the working directory (written by ``brainos.duck_kit`` before the
process starts) or the file named by ``NEUXMIND_DUCK_CONFIG``.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from neuxmind_runtime.clock import RealClock
from neuxmind_runtime.hal import RobotAdapter

from . import duck_paths

CONFIG_NAME = 'duck.json'
CONFIG_ENV = 'NEUXMIND_DUCK_CONFIG'
ADAPTER = 'brainos.duck_body:body'
ADAPTER_VERSION = '0.1.0'
READY_TIMEOUT_S = 120.0       # compiling the scene (38 meshes) and loading the policies
POLL_S = 0.1
CLOSE_S = 3.0
ROOT = Path(__file__).resolve().parents[1]
ENDED = ('succeeded', 'failed', 'stopped', 'error')


def load_config():
    path = Path(os.environ.get(CONFIG_ENV) or CONFIG_NAME)
    config = json.loads(path.read_text(encoding='utf-8'))
    for key in ('shows', 'packages_root', 'vendor', 'model', 'instance_id', 'control_dir'):
        if key not in config:
            raise ValueError(f'小鸭仿真配置缺少 {key}')
    if not isinstance(config['shows'], dict) or not config['shows']:
        raise ValueError('小鸭仿真配置里没有技能模型包')
    return config


def engine_command(args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--nx-duck-engine', *args]
    return [sys.executable, '-u', '-m', 'brainos.duck_engine', *args]


def engine_env():
    env = dict(os.environ)
    if not getattr(sys, 'frozen', False):
        paths = [str(ROOT)] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])
        env['PYTHONPATH'] = os.pathsep.join(paths)
    env['PYTHONUNBUFFERED'] = '1'
    return env


class Engine:
    """One running physics child for one show package."""

    def __init__(self, show, package, config, on_event, log_path):
        self.show = show
        self.ready = threading.Event()
        self.facts = {}
        self.error = ''
        self._on_event = on_event
        self._write = threading.Lock()
        args = ['--package', str(package), '--view', str(config.get('view_dir') or '')]
        if config.get('engine_dir'):
            args += ['--engine', str(config['engine_dir'])]
        log = open(log_path, 'ab')
        try:
            self.proc = subprocess.Popen(engine_command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                         env=engine_env(), start_new_session=True)
        finally:
            log.close()
        threading.Thread(target=self._read, name='nx-duck-engine-' + show, daemon=True).start()

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b''):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('event') == 'ready':
                self.facts = event
                self.ready.set()
            elif event.get('event') == 'error':
                self.error = str(event.get('message') or '物理引擎报告了错误')[:500]
                self.ready.set()
            self._on_event(self, event)
        if not self.ready.is_set():
            self.error = self.error or '物理引擎进程没有启动成功'
            self.ready.set()
        self._on_event(self, {'event': 'exit'})

    @property
    def alive(self):
        return self.proc.poll() is None

    def send(self, command):
        """Never blocks for long: one short line into a pipe the child reads all the time."""
        with self._write:
            try:
                self.proc.stdin.write((json.dumps(command, ensure_ascii=False) + '\n').encode())
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(CLOSE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(CLOSE_S)
            except subprocess.TimeoutExpired:
                pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass


class DuckBody(RobotAdapter):
    def __init__(self, config):
        self.clock = RealClock()
        self.config = config
        self.control = Path(config['control_dir'])
        self.control.mkdir(parents=True, exist_ok=True)
        # The packages are unpacked in the workspace, one folder per content; the engine is wherever the kit is now.
        packages = Path(config['packages_root'])
        self.shows = {str(k): str(packages / str(v)) for k, v in config['shows'].items()}
        kit = Path(config['kit_dir']) if config.get('kit_dir') else duck_paths.kit_dir()
        self.config = {**config, 'engine_dir': '' if self._installed_engine(kit) else str(duck_paths.engine_dir(kit))}
        self.default_show = config.get('default_show') if config.get('default_show') in self.shows else next(iter(self.shows))
        self.engine = None
        self.fault = ''
        self._lock = threading.Lock()                 # the run record; never held while a child starts or stops
        self._file = threading.Lock()                 # one writer of status.json at a time, in the order of the records
        self._stops = 0                               # counts stop(): a show that was being prepared when one came is not started
        self._closed = threading.Event()
        self._seen = self._command_id()               # a command from before this start is not performed again
        self.run = {'id': '', 'show': '', 'state': 'idle', 'phase': '', 'progress': 0.0, 'steps': 0, 'result': None, 'error': ''}
        self._manifest = {
            'schema': 'robot.adapter.v2', 'spec_version': '1.2', 'instance_id': config['instance_id'],
            'name': config.get('name') or f'{config["vendor"]} {config["model"]}（仿真）',
            'vendor': config['vendor'], 'model': config['model'], 'family': 'humanoid',
            'adapter_version': ADAPTER_VERSION, 'simulated': True,
            'capabilities': [{'id': 'safety.stop', 'params': {'hardware_estop': False}}, {'id': 'system.health'}],
            'model_runtimes': [], 'safety': {'hardware_estop': False}}

    @staticmethod
    def _installed_engine(kit):
        """A kit made for tests says so: its packages run on the MuJoCo that is installed."""
        try:
            engine = json.loads((kit / 'kit.json').read_text(encoding='utf-8')).get('engine')
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(engine, dict) and engine.get('installed') is True

    # ------------------------------------------------------------ adapter API
    def manifest(self):
        return json.loads(json.dumps(self._manifest))

    def read(self, cap_id):
        if cap_id != 'system.health':
            raise ValueError(f'仿真小鸭不能读取 {cap_id}')
        with self._lock:
            run, fault = dict(self.run), self.fault
        if fault:
            return {'ok': False, 'mode': 'sim:fault', 'errors': {'simulation': fault}}
        performing = run['state'] in ('starting', 'running')
        return {'ok': True, 'mode': 'sim:show:' + run['show'] if performing else 'sim:stand', 'errors': {}}

    def invoke(self, cap_id, args):
        if cap_id == 'safety.stop':
            self.stop()
            return {'stopped': True}
        raise ValueError(f'仿真小鸭不支持命令 {cap_id}')

    def start_action(self, cap_id, args):
        raise ValueError(f'仿真小鸭不支持动作 {cap_id}')

    def stop(self):
        self._stops += 1
        engine = self.engine
        if engine is not None:
            engine.send({'cmd': 'stop'})

    def status(self):
        with self._lock:
            return {'simulated': True, 'engine': 'mujoco', 'show': self.run['show'], 'show_state': self.run['state'],
                    **({'fault': self.fault} if self.fault else {})}

    # ------------------------------------------------------------ the physics child
    def _update(self, **fields):
        engine = self.engine
        facts = engine.facts if engine is not None else {}
        with self._file:
            with self._lock:
                self.run.update(fields)
                record = dict(self.run)
            record.update(engine={'mujoco': facts.get('mujoco', ''), 'task': facts.get('task', ''),
                                  'checkpoint_sha256': facts.get('checkpoint_sha256', '')},
                          fault=self.fault, updated_at=time.time())
            part = self.control / 'status.json.part'
            try:
                part.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                os.replace(part, self.control / 'status.json')
            except OSError:
                pass

    def _event(self, engine, event):
        if engine is not self.engine:
            return                                    # a child that was replaced
        kind = event.get('event')
        with self._lock:
            current = self.run['id']
        if kind == 'exit':
            if not self._closed.is_set():
                self.fault = engine.error or '物理引擎进程退出了'
                with self._lock:
                    busy = self.run['state'] in ('starting', 'running')
                self._update(**({'state': 'error', 'error': self.fault} if busy else {}))
            return
        if event.get('id') != current or not current:
            return
        if kind == 'started':
            self._update(state='running', phase='', progress=0.0, steps=0)
        elif kind == 'state':
            self._update(phase=str(event.get('phase') or ''), progress=float(event.get('progress') or 0.0), steps=int(event.get('step') or 0))
        elif kind == 'done':
            state = 'succeeded' if event.get('success') else 'stopped' if event.get('stopped') else 'failed'
            result = {key: event.get(key) for key in ('success', 'reason', 'steps', 'duration_s', 'phases', 'error') if key in event}
            phases = event.get('phases') or []
            self._update(state=state, result=result, steps=int(event.get('steps') or 0),
                         phase=str(phases[-1]) if phases else '', progress=1.0 if event.get('success') else self.run['progress'])

    def _start_engine(self, show):
        """The child for ``show`` running and ready; returns '' or why not."""
        old = self.engine
        if old is not None and old.show == show and old.alive and not old.error:
            return ''
        self.engine = None
        if old is not None:
            old.close()
        engine = Engine(show, self.shows[show], self.config, self._event, self.control / 'engine.log')
        self.engine = engine
        if not engine.ready.wait(READY_TIMEOUT_S):
            engine.error = '物理引擎在规定时间内没有准备好'
        if engine.error:
            self.fault = engine.error
            return engine.error
        self.fault = ''
        return ''

    def _command_id(self):
        command = self._command()
        return command.get('id') if command else None

    def _command(self):
        try:
            raw = (self.control / 'command.json').read_bytes()
            command = json.loads(raw) if len(raw) <= 4096 else None
        except (OSError, ValueError):
            return None
        return command if isinstance(command, dict) and isinstance(command.get('id'), str) else None

    def _handle(self, command):
        what, show = command.get('cmd'), command.get('show')
        if what == 'stop':
            self.stop()
            return
        if what == 'home':
            engine = self.engine
            if engine is not None:
                engine.send({'cmd': 'home'})
            return
        if what != 'show':
            return
        if show not in self.shows:
            self._update(id=command['id'], show=str(show)[:40], state='error', result=None, error='这个仿真机器人没有这个技能模型')
            return
        stops = self._stops
        self._update(id=command['id'], show=show, state='starting', phase='', progress=0.0, steps=0, result=None, error='')
        problem = self._start_engine(show)
        if problem:
            self._update(state='error', error=problem)
            return
        stopped = {'state': 'stopped', 'result': {'success': False, 'reason': 'stopped', 'steps': 0, 'duration_s': 0.0, 'phases': []}}
        if self._stops != stops:                      # a stop came while the show was being prepared: it is not started
            self._update(**stopped)
            return
        seed = command.get('seed')
        engine = self.engine
        sent = engine is not None and engine.send({'cmd': 'show', 'id': command['id'], **({'seed': seed} if isinstance(seed, int) else {})})
        if not sent:
            self._update(state='error', error='物理引擎没有收到开始的命令')
        elif self._stops != stops:                    # and one that came at that very moment still ends it
            engine.send({'cmd': 'stop'})

    def _serve(self):
        try:
            problem = self._start_engine(self.default_show)
        except Exception as exc:                      # the child could not even be started: said in the health and the status
            problem = self.fault = f'物理引擎没有启动：{type(exc).__name__}: {str(exc)[:200]}'
        self._update(**({'error': problem} if problem else {}))
        while not self._closed.wait(POLL_S):
            command = self._command()
            if command is None or command['id'] == self._seen:
                continue
            self._seen = command['id']
            try:
                self._handle(command)
            except Exception as exc:                  # the body keeps answering; the run says what went wrong
                self._update(state='error', error=f'{type(exc).__name__}: {str(exc)[:200]}')

    def start_background(self):
        threading.Thread(target=self._serve, name='nx-duck-serve', daemon=True).start()

    def close(self):
        self._closed.set()
        engine, self.engine = self.engine, None
        if engine is not None:
            engine.close()


def body(time_scale=1.0):
    """What ``--adapter brainos.duck_body:body`` loads."""
    made = DuckBody(load_config())
    made.start_background()
    return made
