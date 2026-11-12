"""Skill sandbox: the child process side (one process per skill run).

This file is copied into the run's sandbox directory and executed as a
script with ``python3 -I -B child.py <fd> <parent_pid> <limits-json>``:

* ``-I`` (isolated mode): ``PYTHON*`` environment variables and the user's
  site directory are ignored and the script's directory is *not* put on
  ``sys.path``, so the runtime package next to the original file is not
  importable through it.
* the environment is built by the runtime from scratch (no token, no
  ``BRAIN_EDGE_TOKEN``); the only inherited descriptor besides /dev/null on
  0/1/2 is the protocol socket ``<fd>``.

It must only use the standard library and must not import the runtime.

Start-up order (before any skill code runs):
1. resource limits (``RLIMIT_AS``/``CPU``/``NOFILE``/``NPROC``/``FSIZE``/``CORE``,
   soft = hard, so skill code cannot raise them again; ``NPROC`` is first re-counted
   against the kernel, see :func:`_kernel_task_count`), ``PR_SET_NO_NEW_PRIVS``,
   ``PR_SET_PDEATHSIG=SIGKILL`` (the skill dies with the runtime);
2. an import guard that refuses ``neuxmind_runtime.*`` -- except two small
   stand-in modules (``neuxmind_runtime`` and ``neuxmind_runtime.skills``)
   that provide ``Skill`` and the exception classes skills catch, so
   existing skills and SDK templates run unchanged;
3. ``sys.stdout``/``sys.stderr`` replaced by a bounded tail buffer;
4. SIGTERM handler: the runtime's stop notification only sets a flag; from
   then on every ``ctx`` method raises ``SkillStopped`` locally (no round trip).
   Nothing is raised at arbitrary points of skill code. A skill that never
   touches ``ctx`` again is SIGKILLed by the runtime after the grace period.

Protocol ("JSON lines" over a Unix socketpair, one JSON object per line,
at most ``MAX_MESSAGE_BYTES`` per line):

    parent → child   {"type": "init", ...}                               (once)
    child  → parent  {"type": "hello", "protocol": ..., "pid": ..., "limits": {...}}
    child  → parent  {"type": "request", "id": n, "op": "...", ...}     n = 1, 2, 3 ... strictly
    parent → child   {"type": "reply", "id": n, "ok": true, "value": ...}
                     {"type": "reply", "id": n, "ok": false, "error": {"type", "message", "code"?}}
    child  → parent  {"type": "done", "ok": true, "result": ..., "output_tail": "..."}
                     {"type": "done", "ok": false, "error": {...}, "output_tail": "..."}

The parent treats anything else as a protocol violation and kills the
process group. Nothing the child says can authorise motion: authorisation,
stop epoch, declared capabilities and limits live only in the parent.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import signal
import socket
import sys
import threading
import traceback
import types

PROTOCOL = "nx-sandbox/1"
MAX_MESSAGE_BYTES = 1 << 20          # 1 MiB per JSON line from the child (enforced by the parent)
MAX_PARENT_MESSAGE_BYTES = 16 << 20  # init (skill parameters up to the 2 MB HTTP body) and replies
OUTPUT_TAIL_CHARS = 4000             # last characters of print()/stderr kept for diagnostics
REQUEST_OPS = ("call", "read", "check_stop", "stopped", "time_left", "sleep", "log", "progress")
_STOPPABLE_OPS = ("call", "read", "check_stop", "sleep")    # log/progress keep working in cleanup code

# Names of reply errors that map onto the stand-in classes / builtins below.
_BUILTIN_ERRORS = {"TypeError": TypeError, "ValueError": ValueError, "KeyError": KeyError}

PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38

# Limits entry (not an rlimit): tasks the skill may add on top of its uid's other tasks.
NPROC_HEADROOM = "nproc_headroom"
NPROC_PROBE_CEILING = 1 << 22        # PID_MAX_LIMIT on 64-bit Linux: no uid can have more tasks


# ---------------------------------------------------------------- stand-ins
class SkillInterrupted(BaseException):
    """Raised inside a skill when it must stop (BaseException on purpose)."""


class SkillStopped(SkillInterrupted):
    """Global stop / e-stop."""


class SkillCancelled(SkillInterrupted):
    """This run was cancelled."""


class SkillTimeout(SkillInterrupted):
    """The run exceeded its timeout."""


class ActionFailed(Exception):
    """An action capability finished unsuccessfully. Skills may catch this."""


class SkillError(Exception):
    """Misuse of the SkillContext (e.g. calling an undeclared capability)."""


class CapabilityError(Exception):
    """A runtime API error raised by the parent (safety rejection, bad args ...).

    Concrete subclasses carry the parent's class name (``SafetyRejected``,
    ``EStopLatched``, ``BadRequest`` ...) and ``code``.
    """

    code = "error"


class Skill:
    """Optional base class for class-style skills."""

    def run(self, ctx, **parameters):
        raise NotImplementedError


_INTERRUPTS = {"SkillStopped": SkillStopped, "SkillCancelled": SkillCancelled, "SkillTimeout": SkillTimeout,
               "SkillInterrupted": SkillInterrupted}
_KNOWN = {**_INTERRUPTS, "ActionFailed": ActionFailed, "SkillError": SkillError}
_dynamic_errors: dict[str, type] = {}


def _error_from_reply(error: dict) -> BaseException:
    name = str(error.get("type") or "CapabilityError")
    message = str(error.get("message") or "")
    if name in _KNOWN:
        return _KNOWN[name](message)
    if name in _BUILTIN_ERRORS:
        return _BUILTIN_ERRORS[name](message)
    if not name.isidentifier():
        name = "CapabilityError"
    cls = _dynamic_errors.get(name)
    if cls is None:
        cls = type(name, (CapabilityError,), {})
        _dynamic_errors[name] = cls
    exc = cls(message)
    exc.code = str(error.get("code") or "error")
    return exc


# ---------------------------------------------------------------- hardening
def _prctl(option: int, value: int) -> bool:
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(option, value, 0, 0, 0) == 0
    except Exception:
        return False


def _fork_fits(resource, limit: int, hard: int) -> bool | None:
    """Whether one more task of our uid can start under a soft ``RLIMIT_NPROC`` of ``limit``
    (the kernel refuses fork/clone with EAGAIN once the uid's task count reaches it).
    ``None``: the probe failed for another reason."""
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (limit, hard))
    except (ValueError, OSError):
        return None
    try:
        pid = os.fork()                      # still single-threaded: no skill code has run
    except BlockingIOError:
        return False
    except OSError:
        return None
    if pid == 0:
        os._exit(0)
    try:
        os.waitpid(pid, 0)                   # reaped: the uid's count is back to what it was
    except ChildProcessError:
        pass
    return True


def _kernel_task_count(resource, hint: int) -> int | None:
    """Tasks of our real uid as the kernel counts them for ``RLIMIT_NPROC``, this process included.

    The parent counts the uid's tasks in ``/proc``. In a PID namespace (a runtime in a
    container) that shows only the namespace's tasks, while the kernel counts every task
    of the uid -- other containers', the host's -- so "visible + headroom" could be below
    the real count and the skill could not start a single thread. With soft limit ``L`` a
    fork succeeds exactly when count < ``L``: one fork confirms count <= ``hint + 1`` (the
    usual case, ``hint`` being the parent's count without us); otherwise an exponential and
    then a binary search, about 2*log2(hidden tasks) forks. ``None`` when undetermined."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    ceiling = NPROC_PROBE_CEILING if hard == resource.RLIM_INFINITY else min(hard, NPROC_PROBE_CEILING)
    try:
        lo = min(hint + 2, ceiling)
        fits = _fork_fits(resource, lo, hard)
        if fits is None:
            return None
        if fits:
            return lo - 1                    # an upper bound; the caller never lowers the limit
        step = 1
        while True:                          # invariant: fork refused at lo
            hi = min(lo + step, ceiling)
            fits = _fork_fits(resource, hi, hard)
            if fits is None or (not fits and hi == ceiling):
                return None
            if fits:
                break
            lo, step = hi, step * 2
        while hi - lo > 1:                   # refused at lo, fits at hi
            mid = (lo + hi) // 2
            fits = _fork_fits(resource, mid, hard)
            if fits is None:
                return None
            lo, hi = (lo, mid) if fits else (mid, hi)
        return hi - 1
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))
        except (ValueError, OSError):
            pass


def _calibrated(resource, limits: dict) -> dict:
    """``limits`` without the headroom entry; ``RLIMIT_NPROC`` raised to the kernel's count
    of the uid's other tasks + headroom when ``/proc`` did not show them all. Never lowered."""
    out = {name: value for name, value in limits.items() if name != NPROC_HEADROOM}
    headroom, value = limits.get(NPROC_HEADROOM), out.get("RLIMIT_NPROC")
    if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (headroom, value)):
        return out
    counted = _kernel_task_count(resource, max(0, value - headroom))
    if counted is not None:
        out["RLIMIT_NPROC"] = max(value, counted - 1 + headroom)     # -1: this process itself
    return out


def _apply_limits(limits: dict) -> dict:
    """Lower our own resource limits (soft = hard). Returns what was applied."""
    applied = {}
    try:
        import resource
    except ImportError:          # not Linux/Unix
        return applied
    for name, value in _calibrated(resource, limits).items():
        res = getattr(resource, name, None)
        if res is None or not isinstance(value, int) or value < 0:
            continue
        hard = value + 1 if name == "RLIMIT_CPU" else value   # SIGXCPU at soft, SIGKILL at hard
        try:
            resource.setrlimit(res, (value, hard))
            applied[name] = value
        except (ValueError, OSError):
            pass
    return applied


class _ImportGuard:
    """Meta-path finder refusing runtime internals inside the sandbox.

    This is a guard rail, not the security boundary: the boundary is the
    process. Even code that bypasses it only gets a fresh, unconnected copy
    of runtime source -- no adapter object, no runtime state, no token.
    """

    BLOCKED = ("neuxmind_runtime",)

    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in self.BLOCKED:
            raise ImportError(f"技能沙箱中不能导入运行时内部模块 {name}；请只使用 SkillContext（ctx）")
        return None


def _install_stand_in_modules() -> None:
    package = types.ModuleType("neuxmind_runtime")
    package.__path__ = []                 # a package with no importable submodules
    package.__doc__ = "Sandbox stand-in: only neuxmind_runtime.skills (Skill, exceptions) is available."
    skills = types.ModuleType("neuxmind_runtime.skills")
    for cls in (Skill, SkillInterrupted, SkillStopped, SkillCancelled, SkillTimeout, ActionFailed, SkillError,
                CapabilityError):
        setattr(skills, cls.__name__, cls)
    package.skills = skills
    sys.modules["neuxmind_runtime"] = package
    sys.modules["neuxmind_runtime.skills"] = skills
    sys.meta_path.insert(0, _ImportGuard())


class _TailBuffer:
    """Replacement for sys.stdout/stderr: keeps only the last characters."""

    encoding = "utf-8"

    def __init__(self):
        self._text = ""

    def write(self, text) -> int:
        text = str(text)
        self._text = (self._text + text)[-OUTPUT_TAIL_CHARS:]
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    @property
    def tail(self) -> str:
        return self._text


_stop_notified = threading.Event()


def _on_sigterm(_signum, _frame):
    _stop_notified.set()


def _check_notified() -> None:
    if _stop_notified.is_set():
        raise SkillStopped("已收到停止指令（运行时要求技能进程退出）")


# ---------------------------------------------------------------- channel
class ProtocolError(Exception):
    pass


class Channel:
    """Blocking JSON-lines channel to the runtime (thread-safe for requests)."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()
        self._next_id = 1
        self._lock = threading.Lock()

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(line) > MAX_MESSAGE_BYTES:
            raise ValueError(f"消息超过 {MAX_MESSAGE_BYTES} 字节")
        self._sock.sendall(line)

    def recv(self) -> dict:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[:idx + 1]
                return json.loads(line)
            if len(self._buf) > MAX_PARENT_MESSAGE_BYTES:
                raise ProtocolError("运行时消息过大")
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ProtocolError("运行时关闭了连接")
            self._buf += chunk

    def request(self, op: str, **fields):
        if op in _STOPPABLE_OPS:
            _check_notified()
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self.send({"type": "request", "id": request_id, "op": op, **fields})
            while True:
                msg = self.recv()
                if msg.get("type") != "reply":
                    raise ProtocolError(f"意外的运行时消息 {msg.get('type')!r}")
                if msg.get("id") == request_id:
                    break
                raise ProtocolError("运行时回复编号错误")
        if msg.get("ok"):
            return msg.get("value")
        raise _error_from_reply(msg.get("error") or {})


# ---------------------------------------------------------------- context
class SkillContext:
    """Same API as the runtime's SkillContext; every call goes to the runtime."""

    def __init__(self, channel: Channel, init: dict):
        self._ch = channel
        self._manifest = init["manifest"]
        self._allowed = set(init["allowed"])
        self._capability_ids = set(init["capability_ids"])
        self._cap_params = init["cap_params"]
        self._models = init.get("models") or {}
        self.params = dict(init["params"])
        self.run_id = init["run_id"]

    # ---- introspection (local copies of public data) ------------------------
    @property
    def manifest(self) -> dict:
        return copy.deepcopy(self._manifest)

    @property
    def simulated(self) -> bool:
        return self._manifest["simulated"]

    @property
    def models(self) -> dict:
        """Installed on-device models (read-only files; see the runtime's ctx.models)."""
        return copy.deepcopy(self._models)

    def has(self, cap_id: str) -> bool:
        return cap_id in self._allowed and cap_id in self._capability_ids

    def param(self, cap_id: str, name: str, default=None):
        return copy.deepcopy(self._cap_params.get(cap_id, {}).get(name, default))

    @property
    def stopped(self) -> bool:
        if _stop_notified.is_set():
            return True
        return bool(self._ch.request("stopped"))

    def time_left(self):
        return self._ch.request("time_left")

    # ---- control --------------------------------------------------------------
    def check_stop(self) -> None:
        self._ch.request("check_stop")

    def sleep(self, seconds: float) -> None:
        self._ch.request("sleep", seconds=float(seconds))

    def log(self, message: str, **data) -> None:
        data = json.loads(json.dumps(data, ensure_ascii=False, default=repr, allow_nan=False))
        self._ch.request("log", message=str(message)[:2000], data=data)

    def progress(self, fraction: float, message: str | None = None) -> None:
        self._ch.request("progress", fraction=float(fraction), message=None if message is None else str(message))

    # ---- capabilities -----------------------------------------------------------
    def read(self, cap_id: str) -> dict:
        return self._ch.request("read", cap=str(cap_id))

    def call(self, cap_id: str, **args) -> dict:
        return self._ch.request("call", cap=str(cap_id), args=args)


# ---------------------------------------------------------------- entry
def _load_entry(rel: str, attr: str, base: str):
    """Import the skill entry from the read-only package copy (cwd)."""
    pkg_name = "_nx_skill"
    root = types.ModuleType(pkg_name)
    root.__path__ = [base]
    sys.modules[pkg_name] = root
    parts = rel[:-3].split("/")
    parent, parent_dir = pkg_name, base
    for part in parts[:-1]:
        parent_dir = os.path.join(parent_dir, part)
        sub = types.ModuleType(f"{parent}.{part}")
        sub.__path__ = [parent_dir]
        sys.modules[sub.__name__] = sub
        parent = sub.__name__
    mod_name = f"{parent}.{parts[-1]}"
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(base, rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    target = getattr(module, attr)
    if isinstance(target, type):
        return target().run
    if not callable(target):
        raise SkillError(f"入口 {attr} 不可调用")
    return target


def _describe(exc: BaseException) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)[:2000],
            "interrupted": isinstance(exc, SkillInterrupted),
            "traceback": traceback.format_exc(limit=8)[-4000:]}


def main(argv: list[str]) -> None:
    fd, parent_pid, limits = int(argv[1]), int(argv[2]), json.loads(argv[3])
    _prctl(PR_SET_NO_NEW_PRIVS, 1)
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent_pid:          # the runtime died before PDEATHSIG was armed
        os._exit(3)
    applied = _apply_limits(limits)
    sock = socket.socket(fileno=fd)
    _install_stand_in_modules()
    output = _TailBuffer()
    sys.stdout = sys.stderr = output
    signal.signal(signal.SIGTERM, _on_sigterm)
    channel = Channel(sock)

    init = channel.recv()
    if init.get("type") != "init":
        os._exit(4)
    channel.send({"type": "hello", "protocol": PROTOCOL, "pid": os.getpid(), "limits": applied})
    try:
        entry = _load_entry(init["entry_file"], init["entry_attr"], os.getcwd())
        ctx = SkillContext(channel, init)
        result = entry(ctx, **init["params"])
        try:
            json.dumps(result, allow_nan=False)
            outcome = {"ok": True, "result": result}
        except (TypeError, ValueError):
            outcome = {"ok": False, "error": {"type": "_unserializable", "message": "技能返回值不能序列化为 JSON"}}
    except BaseException as exc:            # noqa: B902 -- report everything, including stops
        outcome = {"ok": False, "error": _describe(exc)}
    done = {"type": "done", **outcome, "output_tail": output.tail}
    try:
        channel.send(done)
    except ValueError:                       # result larger than a message
        channel.send({"type": "done", "ok": False, "output_tail": output.tail,
                      "error": {"type": "_too_large", "message": "技能返回值过大"}})
    except BaseException:
        pass
    os._exit(0)                              # skip atexit hooks and lingering skill threads


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BaseException:
        os._exit(5)
