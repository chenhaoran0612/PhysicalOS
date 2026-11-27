"""The physics of a simulated Microduck: one child process per show package (issue #63, docs/DUCK_SKILLS.md).

A show (a trained skill model) is delivered as a package: the scene and its meshes, the frozen code of the
choreography, the judge and the servo model, and the policy weights. It was accepted on one version of the
physics engine, and on another version the same weights fall over (measured: 2 of 10 instead of 10 of 10).
So a show runs here, in its own process, on the engine version its package names; that engine is a folder
next to the packages (``--engine``), not the MuJoCo the rest of the app uses.

The process stands the duck at HOME, performs the show when told to, and stands again. It talks in lines of
JSON: commands on stdin (``{"cmd": "show", "id": ..., "seed": ...}``, ``stop``, ``home``), events on stdout
(``ready``, ``started``, ``state``, ``done``, ``error``). When stdin closes it ends. While somebody is
watching (the console touches ``<view>/watch``) it draws the scene into ``<view>/frame.png``, the same
files a MuJoCo simulated robot of the app writes.

    python -m brainos.duck_engine --package DIR --view DIR [--engine DIR]
"""
import argparse
import json
import os
import queue
import struct
import sys
import threading
import time
import types
import zlib
from pathlib import Path

CONTROL_DT = 0.02
HOLD_SIM_STEPS = 4            # physics steps per control step while standing (the packages' own SIM_STEPS)
REST_AFTER_S = 3.0            # standing still this long: the physics is not stepped until something is asked
STATE_EVERY = 5               # a `state` event every this many control steps of a show
FRAME_PERIOD_S = 0.1
REST_FRAME_PERIOD_S = 1.0
WATCH_FRESH_S = 6.0
FRAME_SIZE = (360, 480)       # height, width
DEFAULT_SEED = 1_700_003_000  # the first seed of the acceptance set: the run of the delivered replay
MAX_SEED = 2 ** 32 - 1
BEHIND_S = 0.5
SCENE = 'reports/adp102/scene.xml'
BASE_JOINT = 'trunk_base_freejoint'


class EngineError(Exception):
    """Why this package cannot be performed here (the text is shown to the person)."""


def use_engine(engine):
    """Make ``import mujoco`` (and onnxruntime) come from ``engine`` only. Call before the first import."""
    import pkgutil
    sys.path.insert(0, str(engine))
    extend = pkgutil.extend_path
    # MuJoCo's package adds every other `mujoco` folder on the path to itself; the app's own (newer) one
    # must not be mixed into this one.
    pkgutil.extend_path = lambda path, name: path if name == 'mujoco' else extend(path, name)


def png(rgb):
    """An H×W×3 uint8 array as PNG bytes (as brainos.mujoco_body.png; this process does not load the runtime)."""
    import numpy as np
    height, width = rgb.shape[0], rgb.shape[1]
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgb.reshape(height, width * 3)], axis=1).tobytes()

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 1)) + chunk(b'IEND', b''))


def load(package):
    """``(env, policy, facts)`` of a show package. The weights file says which show it is and which engine
    version it was trained and accepted on; the package's own files were checked when it was unpacked."""
    from .duck_policy import CheckpointError, file_sha256, load_policy
    package = Path(package)
    weights = sorted(package.glob('*.pt'))
    if len(weights) != 1 or not (package / SCENE).is_file():
        raise EngineError('技能模型包里缺少（唯一的）权重文件或场景文件')
    checkpoint = weights[0]
    try:
        policy = load_policy(checkpoint)
    except (CheckpointError, OSError) as exc:
        raise EngineError(str(exc)) from None
    task = policy.task
    if not task or checkpoint.stem != task:
        raise EngineError('权重文件写的节目和它的文件名不一致')
    sys.path[:0] = [str(package), str(package / 'vendor' / 'bam')]
    if 'colorama' not in sys.modules:           # the servo model prints coloured warnings; nothing here reads them
        blank = type('Blank', (), {'__getattr__': lambda self, name: ''})()
        sys.modules['colorama'] = types.SimpleNamespace(Fore=blank, Back=blank, Style=blank, init=lambda *a, **k: None)
    import mujoco
    wanted = policy.mujoco_version
    if not wanted:
        raise EngineError('权重文件没有写明它是在哪个物理引擎版本上训练的，所以不运行')
    if str(mujoco.__version__) != wanted:
        raise EngineError(f'这个技能模型是在 MuJoCo {wanted} 上验收的，这里的物理引擎是 {mujoco.__version__}，结果不会一样，所以不运行')
    try:
        from duck_show.env import ShowEnv
        env = ShowEnv(task, package / SCENE)
    except Exception as exc:
        raise EngineError(f'技能模型包的仿真环境没有建起来：{type(exc).__name__}: {str(exc)[:200]}') from None
    if getattr(env, 'obs_size', policy.obs_size) != policy.obs_size or getattr(env, 'action_size', policy.action_size) != policy.action_size:
        raise EngineError('权重的输入输出大小和这个节目的仿真环境对不上')
    return env, policy, {'task': task, 'mujoco': str(mujoco.__version__), 'checkpoint_sha256': file_sha256(checkpoint),
                         'policy_sha256': policy.digest, 'iteration': policy.iteration, 'max_steps': int(env.max_steps),
                         'phases': [str(p) for p in getattr(getattr(env, 'spec', None), 'phases', ())]}


class Frames:
    """Draws the scene while somebody is watching. Used from the stepping thread only (one OpenGL context)."""

    def __init__(self, env, view):
        self.env, self.dir = env, Path(view) if view else None
        self.renderer = self.camera = None
        self.error = ''
        self.next = 0.0
        self.first = True
        self._watched = (0.0, False)
        try:
            self.base = int(env.model.joint(BASE_JOINT).qposadr[0])
        except Exception:
            self.base = None

    def watched(self, now):
        at, value = self._watched
        if now - at > 0.5:
            try:
                value = time.time() - (self.dir / 'watch').stat().st_mtime < WATCH_FRESH_S
            except OSError:
                value = False
            self._watched = (now, value)
        return value

    def _write(self, name, data):
        part = self.dir / (name + '.part')
        part.write_bytes(data)
        os.replace(part, self.dir / name)

    def maybe(self, now, moving, sim_time):
        if self.dir is None or now < self.next or not (self.first or self.watched(now)):
            return
        self.next = now + (FRAME_PERIOD_S if moving else REST_FRAME_PERIOD_S)
        env = self.env
        pose = [float(v) for v in env.data.qpos[self.base:self.base + 3]] if self.base is not None else [0.0, 0.0, 0.1]
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.renderer is None and not self.error:
                import mujoco
                self.renderer = mujoco.Renderer(env.model, FRAME_SIZE[0], FRAME_SIZE[1])
                self.camera = mujoco.MjvCamera()
                self.camera.type = mujoco.mjtCamera.mjCAMERA_FREE
                self.camera.azimuth, self.camera.elevation, self.camera.distance = 150.0, -12.0, 0.62
            if self.renderer is not None:
                self.camera.lookat[:] = [pose[0], pose[1], 0.11]
                self.renderer.update_scene(env.data, self.camera)
                self._write('frame.png', png(self.renderer.render()))
        except Exception as exc:                    # no OpenGL here: the show still runs and reports
            self.error = f'画面渲染不可用：{str(exc)[:200]}'
            self.renderer = None
        try:
            self._write('state.json', json.dumps({'t': round(sim_time, 2), 'touching': [], 'objects': {}, 'fault': '',
                                                  'pose': [round(v, 4) for v in pose], 'render_error': self.error},
                                                 ensure_ascii=False).encode())
        except OSError:
            pass
        self.first = False


def serve(env, policy, facts, commands, emit, view=None, clock=time.monotonic, sleep=time.sleep):
    """Stand, perform when asked, stand again; returns when the command stream ends."""
    frames = Frames(env, view)
    env.reset(seed=0)
    mode, obs, run_id, steps, phases, held, sim_time = 'hold', None, None, 0, [], 0.0, 0.0

    def home(failed_run=None):
        """Back to HOME. The package's own code does it; when that fails for a run, the run says so."""
        try:
            env.reset(seed=0)
            return True
        except Exception as exc:
            if failed_run is not None:
                emit('done', id=failed_run, success=False, stopped=False, reason='simulation_error', steps=0, duration_s=0.0,
                     phases=[], error=f'{type(exc).__name__}: {str(exc)[:200]}')
            return False
    emit('ready', **facts)
    tick = clock()
    while True:
        try:
            command = commands.get_nowait()
        except queue.Empty:
            command = False
        if command is None:
            return
        if command:
            what = command.get('cmd')
            if what == 'show':
                if mode == 'show':
                    emit('done', id=run_id, success=False, stopped=True, reason='replaced', steps=steps,
                         duration_s=round(steps * CONTROL_DT, 2), phases=phases)
                seed = command.get('seed', DEFAULT_SEED)
                seed = seed if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= MAX_SEED else DEFAULT_SEED
                run_id, steps, phases = command.get('id'), 0, []
                try:
                    obs = env.reset(seed=seed)
                except Exception as exc:            # this show cannot start from here; the duck stays where it is
                    emit('done', id=run_id, success=False, stopped=False, reason='simulation_error', steps=0, duration_s=0.0,
                         phases=[], error=f'{type(exc).__name__}: {str(exc)[:200]}')
                    mode, held = 'hold', 0.0
                    home()
                    continue
                mode = 'show'
                emit('started', id=run_id, seed=seed, max_steps=int(env.max_steps))
                tick = clock()
            elif what == 'stop' and mode == 'show':
                emit('done', id=run_id, success=False, stopped=True, reason='stopped', steps=steps,
                     duration_s=round(steps * CONTROL_DT, 2), phases=phases)
                mode, held = 'hold', 0.0
            elif what == 'home':
                if mode == 'show':
                    emit('done', id=run_id, success=False, stopped=True, reason='stopped', steps=steps,
                         duration_s=round(steps * CONTROL_DT, 2), phases=phases)
                home()
                mode, held = 'hold', 0.0
                frames.next = 0.0
        moving = mode == 'show' or held < REST_AFTER_S
        if mode == 'show':
            try:
                obs, _reward, terminated, truncated, info = env.step(policy(obs))
            except Exception as exc:                # a diverged simulation or a judge that cannot go on
                emit('done', id=run_id, success=False, stopped=False, reason='simulation_error', steps=steps,
                     duration_s=round(steps * CONTROL_DT, 2), phases=phases, error=f'{type(exc).__name__}: {str(exc)[:200]}')
                home()
                mode, held = 'hold', 0.0
                continue
            steps += 1
            phase = str(info.get('phase') or '')
            if phase and (not phases or phases[-1] != phase):
                phases.append(phase)
            if terminated or truncated:
                success = bool(info.get('success'))
                emit('done', id=run_id, success=success, stopped=False,
                     reason=str(info.get('reason') or ('complete' if success else 'ended')), steps=steps,
                     duration_s=round(steps * CONTROL_DT, 2), phases=phases)
                mode, held = 'hold', 0.0
            elif steps % STATE_EVERY == 0:
                emit('state', id=run_id, step=steps, phase=phase, progress=round(min(1.0, steps / max(1, env.max_steps)), 3))
        elif moving:
            try:
                for _ in range(HOLD_SIM_STEPS):
                    env.drive.step(env.home)
            except Exception:                       # standing diverged: stop stepping, the next command starts from HOME
                held = REST_AFTER_S
            held += CONTROL_DT
        if moving:
            sim_time += CONTROL_DT
        frames.maybe(clock(), moving, sim_time)
        tick += CONTROL_DT
        ahead = tick - clock()
        if ahead > 0:
            sleep(ahead)
        elif ahead < -BEHIND_S:                     # after a pause the show does not rush to catch up
            tick = clock()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Physics child of a simulated Microduck (one show package).')
    parser.add_argument('--package', required=True)
    parser.add_argument('--view', default='')
    parser.add_argument('--engine', default='')
    args = parser.parse_args(argv)
    # Events go to the real stdout; anything the package's code prints goes to the log.
    events = os.fdopen(os.dup(1), 'w', encoding='utf-8')
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def emit(event, **fields):
        with lock:
            events.write(json.dumps({'event': event, **fields}, ensure_ascii=False) + '\n')
            events.flush()

    commands = queue.Queue()

    def reader():
        fd = sys.stdin.fileno()
        pending = b''
        try:
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                pending += block
                *lines, pending = pending.split(b'\n')
                for line in lines:
                    try:
                        command = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(command, dict):
                        commands.put(command)
        except OSError:
            pass
        commands.put(None)

    threading.Thread(target=reader, name='nx-duck-commands', daemon=True).start()
    try:
        if args.engine:
            use_engine(args.engine)
        env, policy, facts = load(args.package)
    except EngineError as exc:
        emit('error', message=str(exc))
        return 1
    except Exception as exc:
        emit('error', message=f'物理引擎没有启动：{type(exc).__name__}: {str(exc)[:200]}')
        return 1
    try:
        serve(env, policy, facts, commands, emit, args.view or None)
    except BrokenPipeError:
        return 0
    except Exception as exc:
        emit('error', message=f'物理引擎停止了：{type(exc).__name__}: {str(exc)[:200]}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
