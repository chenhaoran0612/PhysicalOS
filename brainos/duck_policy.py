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


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


class Policy:
    """The deterministic policy of a checkpoint: observation in, 14 joint corrections in [-1, 1] out."""

    def __init__(self, state):
        policy, shape = state['policy'], state.get('architecture') or {}
        try:
            self.layers = [(np.asarray(policy[f'actor.{i}.weight'], dtype=np.float32),
                            np.asarray(policy[f'actor.{i}.bias'], dtype=np.float32)) for i in (0, 2, 4)]
            self.mean = np.asarray(policy['obs_mean'], dtype=np.float32)
            self.var = np.asarray(policy['obs_var'], dtype=np.float32)
        except KeyError as exc:
            raise CheckpointError(f'权重文件缺少 {exc.args[0]}') from None
        self.obs_size, self.action_size = int(self.layers[0][0].shape[1]), int(self.layers[-1][0].shape[0])
        if (shape.get('obs_size', self.obs_size), shape.get('action_size', self.action_size)) != (self.obs_size, self.action_size):
            raise CheckpointError('权重文件的网络大小和它自己写的不一致')
        if self.mean.shape != (self.obs_size,) or self.var.shape != (self.obs_size,):
            raise CheckpointError('权重文件的观测归一化参数大小不对')
        for (weight, bias), (after, _b) in zip(self.layers, self.layers[1:]):
            if weight.shape[0] != bias.shape[0] or after.shape[1] != weight.shape[0]:
                raise CheckpointError('权重文件的网络各层对不上')
        self.task = str(state.get('task') or '')
        self.iteration, self.total_steps = state.get('iteration'), state.get('total_steps')
        self.recorded_digest = state.get('policy_sha256')
        self.mujoco_version = str(state.get('mujoco_version') or '')
        self.digest = policy_digest(policy)
        self._scale = np.sqrt(self.var + np.float32(1e-6))

    def __call__(self, observation):
        x = np.asarray(observation, dtype=np.float32)
        if x.shape != (self.obs_size,) or not np.isfinite(x).all():
            raise ValueError('策略收到的观测大小不对或含有非有限值')
        x = np.clip((x - self.mean) / self._scale, -OBS_CLIP, OBS_CLIP)
        for index, (weight, bias) in enumerate(self.layers):
            x = weight @ x + bias
            if index < len(self.layers) - 1:
                x = np.tanh(x)
        return np.tanh(x)


def load_policy(path):
    """The policy of a checkpoint file; refuses one whose numbers do not reproduce its own recorded digest."""
    policy = Policy(read_checkpoint(path))
    if policy.recorded_digest and policy.recorded_digest != policy.digest:
        raise CheckpointError('权重文件读出的参数和它自己记录的指纹不一致')
    return policy
