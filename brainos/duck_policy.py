"""Reading a trained show policy without PyTorch, and running it with numpy (issue #63, docs/DUCK_SKILLS.md).

The show policies of the Microduck are saved by PyTorch (``torch.save`` of a plain dictionary: the actor's
three linear layers, the observation normalisation and the training state). The app has no PyTorch, and
what a show needs at run time is small: ``tanh(W3 · tanh(W2 · tanh(W1 · x)))`` on a normalised observation.

``read_checkpoint`` opens the file as what it is (a zip with one pickle and raw number blocks) and unpickles
it with a table of the few constructors a weights file uses; anything else in the pickle is refused, so a
file cannot run code here. ``policy_digest`` is the same digest the training code records with every
checkpoint: a reader that got one number wrong does not reproduce it.

Only the standard library and numpy: the physics child (``brainos.duck_engine``) imports this module.
"""
import hashlib
import io
import pickle
import zipfile
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import duck_tanh

MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
MAX_PICKLE_BYTES = 4 * 1024 * 1024
FORMAT_VERSION = 1
OBS_CLIP = 10.0
STORAGES = {'FloatStorage': np.float32, 'DoubleStorage': np.float64, 'LongStorage': np.int64,
            'IntStorage': np.int32, 'ByteStorage': np.uint8, 'BoolStorage': np.bool_}


class CheckpointError(ValueError):
    """The file is not a show checkpoint this reader understands."""


def _rebuild_tensor(storage, offset, size, stride, *_unused):
    size = tuple(int(n) for n in size)
    count = int(np.prod(size, dtype=np.int64)) if size else 1
    expected = tuple(int(np.prod(size[i + 1:], dtype=np.int64)) for i in range(len(size)))
    if count and tuple(int(s) for s in stride) != expected:
        raise CheckpointError('权重文件里有不连续存放的张量')
    if offset < 0 or offset + count > len(storage):
        raise CheckpointError('权重文件里的张量超出了它的数据块')
    return np.array(storage[offset:offset + count]).reshape(size)


def read_checkpoint(path):
    """The dictionary a show checkpoint holds, with every tensor as a numpy array."""
    path = Path(path)
    if path.stat().st_size > MAX_CHECKPOINT_BYTES:
        raise CheckpointError('权重文件太大')
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise CheckpointError('权重文件不是 PyTorch 的 zip 格式') from None
    with archive:
        names = [n for n in archive.namelist() if n.endswith('/data.pkl') and n.count('/') == 1]
        if len(names) != 1:
            raise CheckpointError('权重文件里没有（唯一的）data.pkl')
        root = names[0][:-len('data.pkl')]
        if archive.getinfo(names[0]).file_size > MAX_PICKLE_BYTES:
            raise CheckpointError('权重文件的目录部分太大')

        class Reader(pickle.Unpickler):
            def find_class(self, module, name):
                if (module, name) == ('collections', 'OrderedDict'):
                    return OrderedDict
                if (module, name) == ('torch._utils', '_rebuild_tensor_v2'):
                    return _rebuild_tensor
                if module == 'torch' and name in STORAGES:
                    return name
                raise CheckpointError(f'权重文件里有不认识的内容（{module}.{name}），没有读取')

            def persistent_load(self, pid):
                if not isinstance(pid, tuple) or len(pid) < 5 or pid[0] != 'storage' or pid[1] not in STORAGES:
                    raise CheckpointError('权重文件的数据块引用不对')
                key = str(pid[2])
                if not key.isdigit():
                    raise CheckpointError('权重文件的数据块引用不对')
                try:
                    raw = archive.read(f'{root}data/{key}')
                except KeyError:
                    raise CheckpointError('权重文件缺少数据块') from None
                return np.frombuffer(raw, dtype=STORAGES[pid[1]])

        try:
            state = Reader(io.BytesIO(archive.read(names[0]))).load()
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointError(f'权重文件读不出来：{type(exc).__name__}') from None
    if not isinstance(state, dict) or state.get('format_version') != FORMAT_VERSION or not isinstance(state.get('policy'), dict):
        raise CheckpointError('这不是节目策略的权重文件')
    return state


def policy_digest(policy):
    """The digest the training code records as ``policy_sha256`` (parameters and normalisation, by name)."""
    digest = hashlib.sha256()
    for key, array in sorted(policy.items()):
        array = np.asarray(array)
        digest.update(f'{key}\0{array.dtype}\0{tuple(array.shape)}\0'.encode())
        digest.update(array.tobytes(order='C'))
    return digest.hexdigest()


def tanh32(x):
    """``tanh`` of a float32 vector as PyTorch computes it (issue #79, :mod:`brainos.duck_tanh`). The matrix
    products already agree bit for bit (numpy and PyTorch both use Accelerate here); numpy's own ``tanh``
    differs in the last place for some inputs, and the physics of a long show turns that into a different
    episode."""
    return duck_tanh.tanh(x)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()




def _layers(policy, prefix):
    """The three linear layers ``<prefix>{0,2,4}.{weight,bias}`` of a network (tanh between them)."""
    try:
        return [(np.asarray(policy[f'{prefix}{i}.weight'], dtype=np.float32), np.asarray(policy[f'{prefix}{i}.bias'], dtype=np.float32))
                for i in (0, 2, 4)]
    except KeyError as exc:
        raise CheckpointError(f'权重文件缺少 {exc.args[0]}') from None


def _forward(layers, x):
    """The raw output of a network: ``W3 · tanh(W2 · tanh(W1 · x))`` (no tanh after the last layer)."""
    for index, (weight, bias) in enumerate(layers):
        x = weight @ x + bias
        if index < len(layers) - 1:
            x = tanh32(x)
    return x


def _check_layers(layers, obs_size, action_size):
    if int(layers[0][0].shape[1]) != obs_size or int(layers[-1][0].shape[0]) != action_size:
        raise CheckpointError('权重文件的网络大小和它自己写的不一致')
    for (weight, bias), (after, _b) in zip(layers, layers[1:]):
        if weight.ndim != 2 or weight.shape[0] != bias.shape[0] or after.shape[1] != weight.shape[0]:
            raise CheckpointError('权重文件的网络各层对不上')
    if layers[-1][0].shape[0] != layers[-1][1].shape[0]:
        raise CheckpointError('权重文件的网络各层对不上')


def _vector(policy, key, size):
    try:
        value = np.asarray(policy[key], dtype=np.float32)
    except KeyError:
        raise CheckpointError(f'权重文件缺少 {key}') from None
    if value.shape != (size,):
        raise CheckpointError('权重文件的观测归一化参数大小不对')
    return value


def _standardize(x, mean, scale):
    return np.clip((x - mean) / scale, -OBS_CLIP, OBS_CLIP)


def _smoothstep(x):
    value = np.clip(x, np.float32(0), np.float32(1))
    return value * value * (np.float32(3) - np.float32(2) * value)


class Policy:
    """The deterministic policy of a checkpoint: observation in, 14 joint corrections in [-1, 1] out.

    This is the plain layout (one actor on the normalised observation). The two others below are the ones
    the training project's own playback code builds for particular shows (issue #79)."""
    kind = 'plain'

    def __init__(self, state):
        policy, shape = state['policy'], state.get('architecture') or {}
        self.obs_size, self.action_size = self._sizes(policy, shape)
        self._build(policy)
        self.task = str(state.get('task') or '')
        self.iteration, self.total_steps = state.get('iteration'), state.get('total_steps')
        self.recorded_digest = state.get('policy_sha256')
        self.mujoco_version = str(state.get('mujoco_version') or '')
        self.digest = policy_digest(policy)

    def _sizes(self, policy, shape):
        layers = _layers(policy, 'actor.')
        obs_size, action_size = int(layers[0][0].shape[1]), int(layers[-1][0].shape[0])
        if (shape.get('obs_size', obs_size), shape.get('action_size', action_size)) != (obs_size, action_size):
            raise CheckpointError('权重文件的网络大小和它自己写的不一致')
        return obs_size, action_size

    def _build(self, policy):
        self.layers = _layers(policy, 'actor.')
        _check_layers(self.layers, self.obs_size, self.action_size)
        self.mean = _vector(policy, 'obs_mean', self.obs_size)
        self.var = _vector(policy, 'obs_var', self.obs_size)
        self._scale = np.sqrt(self.var + np.float32(1e-6))

    def _observation(self, observation):
        x = np.asarray(observation, dtype=np.float32)
        if x.shape != (self.obs_size,) or not np.isfinite(x).all():
            raise ValueError('策略收到的观测大小不对或含有非有限值')
        return x

    def mean_action(self, x):
        """The action before its final ``tanh``, from a checked raw observation."""
        return _forward(self.layers, _standardize(x, self.mean, self._scale))

    def __call__(self, observation):
        return tanh32(self.mean_action(self._observation(observation)))


class FloorPhasePolicy(Policy):
    """Two actors, each with its own normalisation, share one movement (the floor dance, ``architecture``
    ``floor_phase_actor``). The last observation is the show's elapsed fraction; its tick over a 475-tick
    horizon hands the action from the early actor to the late one between ticks 180 and 189, blending the
    two bounded actions (``duck_show/floor_phase_actor.py`` of the package, written again here in numpy)."""
    kind = 'floor_phase'
    HORIZON, START, WIDTH, BOUND = np.float32(475.0), np.float32(180.0), np.float32(9.0), np.float32(0.9999999)

    def _sizes(self, policy, shape):
        layers = _layers(policy, 'actor.early.network.')
        obs_size, action_size = int(layers[0][0].shape[1]), int(layers[-1][0].shape[0])
        if (shape.get('obs_size'), shape.get('action_size')) != (obs_size, action_size):
            raise CheckpointError('权重文件的网络大小和它自己写的不一致')
        return obs_size, action_size

    def _build(self, policy):
        self.experts = []
        for part in ('early', 'late'):
            layers = _layers(policy, f'actor.{part}.network.')
            _check_layers(layers, self.obs_size, self.action_size)
            mean = _vector(policy, f'actor.{part}.mean', self.obs_size)
            variance = _vector(policy, f'actor.{part}.variance', self.obs_size)
            self.experts.append((layers, mean, np.sqrt(variance + np.float32(1e-6))))

    def mean_action(self, x):
        (early_layers, early_mean, early_scale), (late_layers, late_mean, late_scale) = self.experts
        early = _forward(early_layers, _standardize(x, early_mean, early_scale))
        late = _forward(late_layers, _standardize(x, late_mean, late_scale))
        tick = np.rint(x[-1:] * self.HORIZON)
        blend = np.clip((tick - self.START) / self.WIDTH, np.float32(0), np.float32(1))
        if blend[0] <= 0:
            return early
        if blend[0] >= 1:
            return late
        first = tanh32(early)
        bounded = np.clip(first + blend * (tanh32(late) - first), -self.BOUND, self.BOUND)
        return duck_tanh.atanh(bounded)


class ExitSpecialistPolicy(Policy):
    """The recharge show: two frozen parent actors (``entrance-actor.npz``, ``exit-actor.npz`` next to the
    package's code) walk the opening and the exit, cross-faded at 16.06 s of the choreography clock; the
    trained actor adds its correction only in the opening window from 0.22 s to 2.40 s
    (``duck_show/exit_specialist.py`` and ``brake_residual.py`` of the package, written again here)."""
    kind = 'exit_specialist'
    PARENTS = ('entrance-actor.npz', 'exit-actor.npz')
    MAX_PARENT_BYTES = 16 * 1024 * 1024
    CLOCK, FADE_AT, FADE_S = np.float32(19.28), np.float32(16.06), np.float32(0.60)

    def __init__(self, state, code):
        self.code = Path(code)
        super().__init__(state)

    def _parent(self, name):
        path = self.code / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > self.MAX_PARENT_BYTES:
            raise CheckpointError(f'技能模型包里缺少它的父策略 {name}')
        try:
            loaded = np.load(path, allow_pickle=False)              # numbers only: an object array is refused, never unpickled
            if not hasattr(loaded, 'files'):
                raise ValueError('not an archive of arrays')
            with loaded as weights:
                layers = [(np.asarray(weights[f'{i}.weight'], dtype=np.float32), np.asarray(weights[f'{i}.bias'], dtype=np.float32)) for i in (0, 2, 4)]
        except (KeyError, ValueError, OSError, EOFError):
            raise CheckpointError(f'父策略 {name} 读不出来') from None
        _check_layers(layers, self.obs_size, self.action_size)
        return layers

    def _build(self, policy):
        super()._build(policy)
        self.entrance, self.exit = (self._parent(name) for name in self.PARENTS)

    def _seconds(self, normalized):
        return (normalized[-1:] * self._scale[-1] + self.mean[-1]) * self.CLOCK

    def mean_action(self, x):
        normalized = _standardize(x, self.mean, self._scale)
        clock = self._seconds(normalized)
        entrance, exit_actor = _forward(self.entrance, normalized), _forward(self.exit, normalized)
        weight = _smoothstep((clock - self.FADE_AT) / self.FADE_S)
        # torch.lerp as its vectorised kernel computes it: one fused multiply-add from the nearer end
        small = bool(abs(weight[0]) < 0.5)
        parent = duck_tanh.fma(np.broadcast_to(weight if small else weight - np.float32(1), entrance.shape), exit_actor - entrance,
                               entrance if small else exit_actor)
        correction = _forward(self.layers, normalized) - exit_actor
        seconds = np.rint(clock * np.float32(1e5)) / np.float32(1e5)
        brake = _smoothstep((seconds - np.float32(0.22)) / np.float32(0.30)) * _smoothstep((np.float32(2.40) - seconds) / np.float32(0.60))
        return parent + brake * correction


def load_policy(path, package=None):
    """The policy of a checkpoint file; refuses one whose numbers do not reproduce its own recorded digest.

    ``package``: the unpacked show package the checkpoint belongs to. The layout is chosen the way the
    package's own playback code chooses it: ``architecture.floor_phase_actor`` for the two-actor floor dance,
    and for the recharge show the exit specialist when the package carries that code."""
    state = read_checkpoint(path)
    shape = state.get('architecture') if isinstance(state.get('architecture'), dict) else {}
    code = Path(package) / 'duck_show' if package is not None else None
    if shape.get('floor_phase_actor') is True:
        policy = FloorPhasePolicy(state)
    elif code is not None and state.get('task') == 'recharge' and (code / 'exit_specialist.py').is_file():
        policy = ExitSpecialistPolicy(state, code)
    else:
        policy = Policy(state)
    if policy.recorded_digest and policy.recorded_digest != policy.digest:
        raise CheckpointError('权重文件读出的参数和它自己记录的指纹不一致')
    return policy
