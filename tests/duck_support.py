"""Made-up skill model packages for the Microduck tests (issue #63). Nothing here is a real robot or a
trained policy: a box on a floor, a "show" that counts steps, and weights that are written by hand in
the file format PyTorch uses (so the reader is tested without PyTorch)."""
import hashlib
import io
import json
import pickle
import sys
import types
import zipfile
from pathlib import Path

import numpy as np

OBS, ACT, HIDDEN = 4, 2, 3

ENV = '''"""A made-up show environment with the attributes the physics child uses."""
import types

import mujoco
import numpy as np

SCENE = """<mujoco><option timestep="0.005"/><worldbody><light pos="0 0 2"/>
<geom name="floor" type="plane" size="2 2 0.1"/>
<body name="trunk" pos="0 0 0.06"><freejoint name="trunk_base_freejoint"/><geom type="box" size="0.05 0.04 0.05" mass="0.8"/></body>
</worldbody></mujoco>"""
TASKS = {"nod": (25, ("低头", "抬头")), "long": (1500, ("走",)), "fall": (10, ("站",)), "crash": (10, ("站",)), "stuck": (10, ("站",))}


class _Drive:
    def __init__(self, env):
        self.env = env

    def step(self, target):
        mujoco.mj_step(self.env.model, self.env.data)


class ShowEnv:
    obs_size, action_size = %(obs)d, %(act)d

    def __init__(self, task, scene):
        self.task = task
        self.max_steps, phases = TASKS[task]
        self.spec = types.SimpleNamespace(phases=phases)
        self.model = mujoco.MjModel.from_xml_string(SCENE)
        self.data = mujoco.MjData(self.model)
        self.home = np.zeros(self.action_size)
        self.drive = _Drive(self)
        self.steps = 0
        self.seed = None

    def reset(self, seed=None):
        if self.task == "stuck" and seed:
            raise RuntimeError("made-up: this show cannot start")
        mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)
        self.steps, self.seed = 0, seed
        return np.zeros(self.obs_size, dtype=np.float32)

    def step(self, action):
        assert np.asarray(action).shape == (self.action_size,) and np.all(np.abs(action) <= 1.0)
        if self.task == "crash":
            raise FloatingPointError("made-up divergence")
        for _ in range(4):
            mujoco.mj_step(self.model, self.data)
        self.steps += 1
        phases = self.spec.phases
        phase = phases[min(len(phases) - 1, self.steps * len(phases) // self.max_steps)]
        done = self.steps >= self.max_steps
        success = done and self.task != "fall"
        info = {"phase": phase, "phase_index": 0, "success": success, "reason": "complete" if success else ("nonfoot_ground_contact" if done else "")}
        return np.full(self.obs_size, self.steps / 100, dtype=np.float32), 0.0, done, False, info
''' % {'obs': OBS, 'act': ACT}


class _FloatStorage:
    pass


class _DoubleStorage:
    pass


def _rebuild_tensor_v2(*args):
    raise AssertionError('only named, never called by the writer')


class _Tensor:
    def __init__(self, array, key):
        self.array, self.key = np.ascontiguousarray(array) if array.ndim else array, key

    def __reduce__(self):
        a = self.array
        stride = tuple(int(np.prod(a.shape[i + 1:], dtype=int)) for i in range(a.ndim))
        return _rebuild_tensor_v2, (_Storage(a, self.key), 0, tuple(a.shape), stride, False, {})


class _Storage:
    def __init__(self, array, key):
        self.array, self.key = array, key


def weights(seed=0):
    """A made-up policy: three layers, the normalisation, and what else a checkpoint's ``policy`` holds."""
    rng = np.random.default_rng(seed)
    sizes = [(HIDDEN, OBS), (HIDDEN, HIDDEN), (ACT, HIDDEN)]
    policy = {}
    for index, shape in zip((0, 2, 4), sizes):
        policy[f'actor.{index}.weight'] = rng.normal(size=shape).astype(np.float32)
        policy[f'actor.{index}.bias'] = rng.normal(size=shape[0]).astype(np.float32)
    policy['log_std'] = np.full(ACT, -0.65, dtype=np.float32)
    policy['obs_mean'] = rng.normal(size=OBS).astype(np.float32)
    policy['obs_var'] = rng.uniform(0.5, 2.0, size=OBS).astype(np.float32)
    policy['obs_count'] = np.array(1e-4, dtype=np.float64)
    return policy


def digest(policy):
    """The training code's ``policy_digest``, written here a second time on purpose."""
    out = hashlib.sha256()
    for key, array in sorted(policy.items()):
        out.update(f'{key}\0{array.dtype}\0{tuple(array.shape)}\0'.encode())
        out.update(array.tobytes())
    return out.hexdigest()


def checkpoint_bytes(task, policy, mujoco_version, extra=None, recorded=None, payload=None):
    """The bytes of a checkpoint file as ``torch.save`` writes one (a zip: ``<name>/data.pkl`` and raw blocks)."""
    torch, utils = types.ModuleType('torch'), types.ModuleType('torch._utils')
    _FloatStorage.__module__ = _DoubleStorage.__module__ = 'torch'
    _FloatStorage.__qualname__, _FloatStorage.__name__ = 'FloatStorage', 'FloatStorage'
    _DoubleStorage.__qualname__, _DoubleStorage.__name__ = 'DoubleStorage', 'DoubleStorage'
    _rebuild_tensor_v2.__module__ = 'torch._utils'
    torch.FloatStorage, torch.DoubleStorage, torch._utils, utils._rebuild_tensor_v2 = _FloatStorage, _DoubleStorage, utils, _rebuild_tensor_v2
    blocks = {}

    class Writer(pickle.Pickler):
        def persistent_id(self, obj):
            if isinstance(obj, _Storage):
                blocks[obj.key] = obj.array.tobytes()
                kind = _DoubleStorage if obj.array.dtype == np.float64 else _FloatStorage
                return ('storage', kind, obj.key, 'cpu', obj.array.size)
            return None

    state = payload if payload is not None else {
        'format_version': 1, 'task': task, 'architecture': {'obs_size': OBS, 'action_size': ACT, 'hidden': HIDDEN},
        'policy': {name: _Tensor(array, str(index)) for index, (name, array) in enumerate(policy.items())},
        'policy_sha256': recorded or digest(policy), 'iteration': 4, 'total_steps': 2048, 'mujoco_version': mujoco_version,
        **(extra or {})}
    saved = {name: sys.modules.get(name) for name in ('torch', 'torch._utils')}
    sys.modules.update({'torch': torch, 'torch._utils': utils})
    try:
        stream = io.BytesIO()
        Writer(stream, protocol=2).dump(state)
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('checkpoint/data.pkl', stream.getvalue())
        for key, raw in blocks.items():
            archive.writestr(f'checkpoint/data/{key}', raw)
        archive.writestr('checkpoint/version', '3\n')
    return out.getvalue()


def package_bytes(task, mujoco_version, more=None, sums=True, replay=b'\x00\x00\x00\x18ftypmp42made-up-video'):
    """A made-up show package as a zip: weights, scene, environment code, a replay, and the file list."""
    files = {f'{task}.pt': checkpoint_bytes(task, weights(), mujoco_version),
             'reports/adp102/scene.xml': b'<mujoco/>', 'duck_show/__init__.py': b'', 'duck_show/env.py': ENV.encode(),
             'replay.mp4': replay, **(more or {})}
    listing = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(files.items()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if sums:
            archive.writestr('SHA256SUMS', listing)
    return out.getvalue()


def kit(root, tasks, mujoco_version, versions=None):
    """A made-up kit under ``root`` for the given shows; returns ``(kit folder, the SHOWS list for it)``."""
    root = Path(root)
    (root / 'shows').mkdir(parents=True)
    shows, listed = [], {}
    for task in tasks:
        data = package_bytes(task, (versions or {}).get(task, mujoco_version))
        (root / 'shows' / f'{task}.zip').write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        shows.append({'id': task, 'title': f'节目 {task}', 'status': 'delivered', 'summary': '测试用的节目。', 'package': f'{task}.zip',
                      'sha256': sha, 'origin': {'round': 1, 'update': 4, 'steps': 2048, 'device': 'CPU'},
                      'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}})
        listed[task] = {'package': f'{task}.zip', 'sha256': sha, 'size': len(data)}
    (root / 'kit.json').write_text(json.dumps({'schema': 'duck.kit.v1', 'built_at': '2026-11-27T00:00:00Z', 'shows': listed,
                                               'engine': {'installed': True}}), encoding='utf-8')
    return root, tuple(shows)
