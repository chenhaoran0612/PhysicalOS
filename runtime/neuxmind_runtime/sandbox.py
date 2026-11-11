"""Skill process sandbox: the runtime (parent) side.

Every skill run (each step of a v1 application, too) executes in its own
child process (``sandbox_child.py``). The child gets a proxy ``ctx``; each
``ctx.call/read/sleep/log/progress/check_stop`` is a request over a
socketpair that the parent serves with the run's real :class:`SkillContext`
-- so every capability call still goes through the parent's capability bus,
safety supervisor, stop epoch and revocation checks. The child never holds
an adapter object, runtime state or the gateway token.

Process set-up (no ``preexec_fn``: the runtime is multi-threaded)
* explicit minimal environment (``PATH``, ``LANG``, ``HOME``/``TMPDIR`` =
  the run's temp dir) -- nothing is inherited, in particular no
  ``BRAIN_EDGE_TOKEN``;
* ``python3 -I -B``; own session/process group; ``close_fds`` except the
  protocol socket; stdin/stdout/stderr = /dev/null; umask 077;
* when the runtime runs as root, the child runs as an unprivileged user
  (``neuxmind-skill`` if it exists, else ``nobody``) with no supplementary
  groups; otherwise it runs as the runtime's user (see README: trust
  boundary) and the runtime marks itself non-dumpable so the child cannot
  read ``/proc/<runtime>/environ`` or ptrace it;
* working directory: a fresh read-only copy of the skill package; a private
  temp dir; the child applies ``RLIMIT_AS/CPU/NOFILE/NPROC/FSIZE/CORE`` to
  itself before any skill code runs.

Stopping
* the stop path (already after the robot was halted) calls
  :meth:`SandboxHost.notify_stop` -- one non-blocking ``kill(SIGTERM)``;
  it never waits for the child;
* the run manager's monitor calls :meth:`SandboxHost.kill` (``SIGKILL`` to
  the whole process group) once ``kill_grace_s`` has passed, and again when
  the run is finalised;
* when the child has exited, the parent-side context refuses everything
  (``alive()`` is part of its guard, re-checked right before each adapter
  dispatch), so a request that was being served is void.

Fail closed: a malformed, oversized or out-of-order message, an unknown
operation, a handshake timeout or an unexpected exit kills the process
group and fails the run; the parent stays healthy and the stop path is
untouched.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import sandbox_child as child_proto
from .errors import RuntimeApiError
from .skills import ActionFailed, SkillCancelled, SkillContext, SkillInterrupted, SkillStopped, SkillTimeout

PROCESS, INPROCESS = "process", "inprocess"
MODES = (PROCESS, INPROCESS)
CHILD_SCRIPT = Path(child_proto.__file__).resolve()
POLL_S = 0.05                         # host loop wake-up to notice child death without messages
REPLY_SEND_TIMEOUT_S = 5.0            # a child that never reads its replies is killed
SKILL_USER_CANDIDATES = ("neuxmind-skill", "nobody")
KILLED_MESSAGE = "技能进程未在宽限期内退出，已被强制终止（SIGKILL）"
REAP_PERIOD_S = 0.5                   # periodic escapee sweep (no cgroup) ...
REAP_WINDOW_S = 30.0                  # ... for this long after the last skill process exited


class ProtocolViolation(Exception):
    """The child broke the protocol; it is killed and the run fails."""


class SkillFailure(Exception):
    """The skill (in the child) raised, or the child failed. ``str()`` is the run error."""

    def __init__(self, message: str, traceback: str | None = None):
        super().__init__(message)
        self.traceback = traceback


@dataclass(frozen=True)
class SandboxConfig:
    """Runtime-wide sandbox settings (CLI ``--sandbox`` etc.)."""

    mode: str = PROCESS
    user: Any = "auto"                    # "auto" | None (same user) | name | uid
    memory_bytes: int = 1 << 30           # RLIMIT_AS (virtual address space)
    max_cpu_seconds: int = 3600           # RLIMIT_CPU upper bound; per run: wall timeout + margin
    min_cpu_seconds: int = 30
    nofile: int = 64                      # RLIMIT_NOFILE
    extra_tasks: int = 32                 # RLIMIT_NPROC = current tasks of the skill user + this
    fsize_bytes: int = 64 << 20           # RLIMIT_FSIZE (largest file the skill may write)
    kill_grace_s: float = 1.0             # SIGTERM → SIGKILL
    handshake_timeout_s: float = 15.0
    max_message_bytes: int = child_proto.MAX_MESSAGE_BYTES
    max_requests_per_s: float = 2000.0    # parent-side throttle (CPU the child can make us spend)
    request_burst: int = 200
    max_log_events: int = 500             # log/progress events kept per *run* (all its steps); rest counted
    max_log_data_bytes: int = 4096
    workspace_root: str | None = None     # override parent for per-run sandboxes (default: <state_dir>/sandbox)
    python: str | None = None             # interpreter for skill processes (default: the runtime's)
    allow_no_cgroup: bool = True          # start even without a writable pids cgroup (best-effort reaper then)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"sandbox 须为 {MODES} 之一")


def resolve_user(spec: Any) -> tuple[int, int, str] | None:
    """``(uid, gid, name)`` for the child, or ``None`` for "same user as the runtime"."""
    import pwd
    if spec in (None, "", "same", "none"):
        return None
    if spec == "auto":
        if os.geteuid() != 0:
            return None
        for name in SKILL_USER_CANDIDATES:
            try:
                entry = pwd.getpwnam(name)
                return entry.pw_uid, entry.pw_gid, entry.pw_name
            except KeyError:
                continue
        return None
    entry = pwd.getpwuid(int(spec)) if str(spec).isdigit() else pwd.getpwnam(str(spec))
    if entry.pw_uid == 0:
        raise ValueError("技能沙箱用户不能是 root")
    return entry.pw_uid, entry.pw_gid, entry.pw_name


def _user_name(uid: int) -> str:
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def user_can_execute(path: str, uid: int, gid: int) -> bool:
    """Classic permission-bit check (no ACLs): can ``uid`` traverse to and execute ``path``?"""
    def allowed(st, bit_user, bit_group, bit_other) -> bool:
        if st.st_uid == uid:
            return bool(st.st_mode & bit_user)
        if st.st_gid == gid:
            return bool(st.st_mode & bit_group)
        return bool(st.st_mode & bit_other)
    for candidate in {os.path.abspath(path), os.path.realpath(path)}:
        try:
            parts = Path(candidate).parents
            for directory in reversed(parts):
                if not allowed(os.stat(directory), stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH):
                    return False
            if not allowed(os.stat(candidate), stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH):
                return False
        except OSError:
            return False
    return True


def _same_version_candidates() -> list[str]:
    """System interpreters of the runtime's own ``major.minor`` (e.g. ``/usr/bin/python3.10``)."""
    name = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return [os.path.join(d, name) for d in ("/usr/bin", "/usr/local/bin", "/bin")]


def choose_python(configured: str | None, user: tuple[int, int, str] | None) -> tuple[str, str | None]:
    """Interpreter for skill processes and a note when it differs from the runtime's own.

    Order: ``--sandbox-python``; the runtime's interpreter (venv path, then its real
    path); a system interpreter of the same ``major.minor``. When the skill user can
    execute none of them (e.g. a uv/pyenv Python under ``/root``), the runtime's
    interpreter is returned with a note -- :func:`python_usable` is then False and
    ``/v1/status`` says so; skill runs fail with a clear error instead of hanging.
    """
    if configured:
        return configured, None
    if user is None or user_can_execute(sys.executable, user[0], user[1]):
        return sys.executable, None
    real = os.path.realpath(sys.executable)
    if user_can_execute(real, user[0], user[1]):
        return real, (f"运行时解释器 {sys.executable} 对技能用户 {user[2]} 不可访问，改用 {real}"
                      "（虚拟环境中的第三方包在技能中不可用；可用 --sandbox-python 指定）")
    for candidate in _same_version_candidates():
        if os.path.isfile(candidate) and user_can_execute(candidate, user[0], user[1]):
            return candidate, (f"运行时解释器 {real} 对技能用户 {user[2]} 不可访问，改用同版本系统解释器 "
                               f"{candidate}（可用 --sandbox-python 指定）")
    return sys.executable, (f"技能用户 {user[2]} 无法执行 {sys.executable}（及其真实路径 {real}）：技能将无法启动。"
                            "请改用系统解释器（如 /usr/bin/python3.10）运行运行时，或用 --sandbox-python 指定"
                            "一个技能用户可执行的解释器")


def python_usable(python: str, user: tuple[int, int, str] | None) -> bool:
    """Can the skill user start ``python`` at all (permission bits; ACLs not considered)?"""
    return user is None or user_can_execute(python, user[0], user[1])


PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36


def make_parent_non_dumpable() -> bool:
    """``PR_SET_DUMPABLE=0``: same-uid processes cannot read our /proc environ/mem or ptrace us."""
    return child_proto._prctl(PR_SET_DUMPABLE, 0)


_SUBREAPER = False          # this process is a child subreaper: orphans become *our* zombies
_LIVE_CHILDREN: set = set()          # pids of tracked skill processes, process-wide
_SKILL_SESSIONS: set = set()         # session ids of every skill process started by this process
# Marker in every skill process's environment (inherited by its descendants), so that
# a same-user reaper never mistakes a helper the runtime or an adapter started in a
# new session for an escaped skill process (review Q3-05). Not a secret.
SKILL_TAG_VAR = "NEUXMIND_SKILL_SANDBOX"
SKILL_TAG = os.urandom(8).hex()
_LIVE_CHILDREN_LOCK = threading.Lock()


def become_child_subreaper() -> bool:
    """``PR_SET_CHILD_SUBREAPER``: orphaned descendants reparent to us, not init, so a
    skill grandchild that escaped the process group with ``setsid()`` can still be found
    (``ppid == our pid``) and reaped. Used only when no writable pids cgroup is available."""
    global _SUBREAPER
    ok = child_proto._prctl(PR_SET_CHILD_SUBREAPER, 1)
    _SUBREAPER = _SUBREAPER or ok
    return ok


def _process_real_uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _process_stat(pid: int) -> list[bytes] | None:
    """Fields of ``/proc/<pid>/stat`` after ``comm`` (state, ppid, pgrp, session, ...)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # The comm field is parenthesised and may contain spaces/')' -> split after the last ')'.
    try:
        return data[data.rindex(b")") + 1:].split()
    except ValueError:
        return None


def _process_ppid(pid: int) -> int | None:
    fields = _process_stat(pid)
    try:
        return int(fields[1])          # ppid is the 2nd field after comm (state, ppid, ...)
    except (TypeError, ValueError, IndexError):
        return None


def _process_has_skill_tag(pid: int) -> bool:
    """True when ``/proc/<pid>/environ`` carries this process's skill marker."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            data = fh.read(65536)
    except OSError:
        return False
    return f"{SKILL_TAG_VAR}={SKILL_TAG}".encode() in data.split(b"\0")


def _process_group_and_session(pid: int) -> tuple[int | None, int | None]:
    fields = _process_stat(pid)
    try:
        return int(fields[2]), int(fields[3])
    except (TypeError, ValueError, IndexError):
        return None, None


def count_tasks(uid: int) -> int | None:
    """Threads currently owned by ``uid`` (RLIMIT_NPROC counts per real uid). Best effort."""
    total = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
                    real_uid, threads = None, 1
                    for line in fh:
                        if line.startswith("Uid:"):
                            real_uid = int(line.split()[1])
                        elif line.startswith("Threads:"):
                            threads = int(line.split()[1])
                if real_uid == uid:
                    total += threads
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return None
    return total


def _pid_alive(text: str) -> bool:
    try:
        os.kill(int(text), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


class PidsCgroup:
    """Optional pids cgroup for one skill process tree (best effort, needs write access).

    Rlimits and the process group cannot catch a grandchild that calls
    ``setsid()``; a cgroup can: every descendant stays in it (the skill user
    cannot write any other cgroup's ``cgroup.procs``), ``pids.max`` caps
    the tree, and :meth:`kill_all` reaches every member.
    """

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def base() -> Path | None:
        """A writable pids hierarchy: cgroup v1 ``pids`` or a delegated v2 subtree."""
        v1 = Path("/sys/fs/cgroup/pids")
        if (v1 / "cgroup.procs").is_file() and os.access(v1, os.W_OK):
            return v1 / "neuxmind-sandbox"
        try:
            line = next(l for l in Path("/proc/self/cgroup").read_text().splitlines() if l.startswith("0::"))
            own = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            if "pids" in (own / "cgroup.subtree_control").read_text().split() and os.access(own, os.W_OK):
                return own / "neuxmind-sandbox"
        except (OSError, StopIteration):
            pass
        return None

    @staticmethod
    def runtime_dir(base: Path | None) -> Path | None:
        """This runtime's own directory under ``base`` (other runtimes are never touched).

        Leftovers of runtimes that died without cleaning up (their PID, part of
        the directory name, no longer exists) are removed first; ``rmdir`` only
        succeeds on empty cgroups. Directories of live runtimes are never touched
        -- an empty one is still in use.
        """
        if base is None:
            return None
        try:
            base.mkdir(exist_ok=True)
            for runtime in [d for d in base.iterdir() if d.is_dir() and d.name.startswith("rt-")]:
                if _pid_alive(runtime.name.split("-")[1]):
                    continue
                for run in [d for d in runtime.iterdir() if d.is_dir()]:
                    try:
                        run.rmdir()
                    except OSError:
                        pass
                try:
                    runtime.rmdir()
                except OSError:
                    pass
            own = base / f"rt-{os.getpid()}-{os.urandom(4).hex()}"
            own.mkdir()
            return own
        except OSError:
            return None

    @classmethod
    def create(cls, base: Path | None, name: str, max_pids: int) -> "PidsCgroup | None":
        if base is None:
            return None
        try:
            base.mkdir(parents=True, exist_ok=True)     # recreated if someone removed it
            path = base / name
            path.mkdir()
            (path / "pids.max").write_text(str(max_pids))
            return cls(path)
        except OSError:
            return None

    def add(self, pid: int) -> bool:
        try:
            (self.path / "cgroup.procs").write_text(str(pid))
            return True
        except OSError:
            return False

    def members(self) -> list[int]:
        try:
            return [int(x) for x in (self.path / "cgroup.procs").read_text().split()]
        except (OSError, ValueError):
            return []

    def kill_all(self) -> None:
        for pid in self.members():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def remove(self) -> None:
        deadline = time.monotonic() + 2.0
        while self.members() and time.monotonic() < deadline:   # forks racing with the kill
            self.kill_all()
            time.sleep(0.01)
        try:
            self.path.rmdir()
        except OSError:
            pass


class Sandboxes:
    """Per-runtime sandbox settings, workspace root and isolation report.

    The per-run workspaces live under ``<state_dir>/sandbox`` (Q2-01): a
    runtime-owned directory, mode 0711, so the skill user can traverse into
    its own workspace but cannot *create* entries there -- it therefore cannot
    plant a symlink for the start-up sweep to follow. The old location was the
    world-writable system ``/tmp``.
    """

    def __init__(self, config: SandboxConfig, state_dir: str | Path | None = None):
        self.config = config
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.user = resolve_user(config.user) if config.mode == PROCESS else None
        self.non_dumpable = make_parent_non_dumpable() if config.mode == PROCESS else False
        self.root: Path | None = None
        self.cgroup_base = PidsCgroup.runtime_dir(PidsCgroup.base()) if config.mode == PROCESS else None
        # No cgroup: become the reaper for escaped (setsid) descendants.
        self.child_subreaper = (config.mode == PROCESS and self.cgroup_base is None
                                and become_child_subreaper())
        self.python, self.python_note = choose_python(config.python, self.user) if config.mode == PROCESS \
            else (sys.executable, None)
        self.python_usable = python_usable(self.python, self.user) if config.mode == PROCESS else True
        # Live skill children of *every* runtime in this process (tests embed several):
        # the reaper of one must never kill another's tracked child.
        self._live_children = _LIVE_CHILDREN
        self._children_lock = _LIVE_CHILDREN_LOCK
        self._last_exit: float | None = None
        self._next_reap = 0.0
        if config.mode == PROCESS:
            self.root = self._make_root()
            os.chmod(self.root, 0o711)      # traversable by the skill user, not listable

    def _make_root(self) -> Path:
        """Create this runtime's own per-run workspace directory and sweep dead siblings.

        Location (Q2-01 -- the old code used world-writable system ``/tmp`` and
        followed symlinks):

        * explicit ``workspace_root`` -> there (tests);
        * **same user** as the runtime (no privilege drop) -> ``<state_dir>/sandbox``,
          inside the runtime's own 0700 tree, unreachable by anyone else;
        * **privilege-dropped** child -> a runtime-owned, 0711, non-world-writable
          directory under the system temp dir. A private state dir is not an option
          there: the unprivileged child must be able to *traverse* to its workspace,
          and a 0700 ancestor would block it. 0711 + runtime ownership means the skill
          user can traverse but cannot create entries, so it cannot plant a symlink for
          the sweep; the system temp dir's sticky bit stops it from swapping the base.
        """
        base = self._base_dir()
        try:
            os.chmod(base, 0o711)
        except OSError:
            pass
        _sweep_dead_workspaces(base, os.geteuid())
        root = base / f"rt-{os.getpid()}-{os.urandom(4).hex()}"
        root.mkdir()
        return root

    def _base_dir(self) -> Path:
        if self.config.workspace_root is not None:
            base = Path(self.config.workspace_root)
            base.mkdir(parents=True, exist_ok=True)
            return base
        if self.user is None and self.state_dir is not None:
            base = self.state_dir / "sandbox"
            base.mkdir(parents=True, exist_ok=True)
            return base
        # Privilege-dropped (or no state dir): a runtime-owned shared base in the temp dir.
        base = Path(tempfile.gettempdir()) / f"neuxmind-sandbox-{os.geteuid()}"
        try:
            st = os.lstat(base)
        except FileNotFoundError:
            st = None
        except OSError:
            st = False
        if st is False or (st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
                                               or st.st_uid != os.geteuid())):
            # Pre-created by someone else (or a planted symlink): never use it.
            return Path(tempfile.mkdtemp(prefix="nx-sandbox-"))
        base.mkdir(mode=0o711, exist_ok=True)
        return base

    @property
    def mode(self) -> str:
        return self.config.mode

    # --- live child registry (so the orphan reaper never kills a tracked child) ---
    def register_child(self, pid: int) -> None:
        with self._children_lock:
            self._live_children.add(pid)

    def unregister_child(self, pid: int) -> None:
        with self._children_lock:
            self._live_children.discard(pid)

    def _reap_zombies(self, pids: list[int]) -> None:
        """``waitpid`` reparented orphans by explicit pid (never ``-1``: that would steal
        the exit status of a tracked child from ``subprocess``)."""
        deadline = time.monotonic() + 1.0
        pending = set(pids)
        while pending and time.monotonic() < deadline:
            for pid in list(pending):
                try:
                    done, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    done = pid                 # not our child (any more)
                if done:
                    pending.discard(pid)
            if pending:
                time.sleep(0.01)

    def reap_orphans(self, settle: bool = False) -> list[int]:
        """Kill skill processes reparented to us (escaped via ``setsid()``).

        Used only when there is no pids cgroup. Limitation: it finds escapees
        only once their direct parent has exited and the kernel has reparented
        them to this process (``PR_SET_CHILD_SUBREAPER``); a brief window exists
        between a skill process's death and that reparenting. Such a process can
        never drive the robot (its in-flight requests are void) -- see README.

        Which reparented processes are skill processes:

        * privilege-dropped: real uid == the skill user;
        * same user as the runtime (non-root deployment, §5.0): real uid == ours,
          a session other than the runtime's, **and** positive evidence that it
          descends from a skill process: its session is one a skill process was
          started in, or its environment carries the skill marker
          (``NEUXMIND_SKILL_SANDBOX``, inherited by every descendant). A helper the
          runtime or an adapter started -- even in a new session -- has neither and
          is never signalled or ``waitpid``-ed (review Q3-05: it used to be killed and
          its exit status taken, so its owner saw "exited 0"). A same-user skill that
          scrubs its environment *and* calls ``setsid`` escapes this best-effort
          sweep; with a cgroup or a privilege-dropped skill user it cannot.

        ``settle=True`` repeats the sweep for up to 2 s until two rounds find
        nothing (a fork chain reparents one generation at a time).
        """
        if not _SUBREAPER:
            return []
        uid = self.user[0] if self.user is not None else os.getuid()
        killed: list[int] = []
        deadline = time.monotonic() + 2.0
        quiet = 0
        while True:
            round_killed = self._reap_round(uid)
            killed += round_killed
            if not settle:
                break
            quiet = 0 if round_killed else quiet + 1
            if quiet >= 2 or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        if killed:
            self._last_exit = time.monotonic()        # keep the periodic sweep going a little longer
        return killed

    def _untracked_descendants(self) -> tuple[list[int], set[int]]:
        """All our descendants outside the subtrees of tracked (live) skill children,
        and the subset that are our direct children (reparented orphans)."""
        me = os.getpid()
        with self._children_lock:
            live = set(self._live_children)
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return [], set()
        children: dict[int, list[int]] = {}
        for pid in pids:
            ppid = _process_ppid(pid)
            if ppid is not None:
                children.setdefault(ppid, []).append(pid)
        direct = {pid for pid in children.get(me, []) if pid not in live and pid != me}
        found, stack = [], list(direct)
        while stack:
            pid = stack.pop()
            found.append(pid)
            stack.extend(c for c in children.get(pid, []) if c not in live)
        return found, direct

    def _reap_round(self, uid: int) -> list[int]:
        """One sweep: SIGSTOP every escaped skill process first (a stopped process
        cannot fork, so a fork bomb cannot outrun the sweep), then SIGKILL them, then
        collect the zombies that are our children."""
        try:
            my_session = os.getsid(0)
        except OSError:
            my_session = None
        candidates, direct = self._untracked_descendants()
        targets = []
        for pid in candidates:
            if _process_real_uid(pid) != uid:
                continue
            _group, session = _process_group_and_session(pid)
            if session is not None and session == my_session:
                continue                                # not a skill process (see reap_orphans)
            if self.user is None and session not in _SKILL_SESSIONS and not _process_has_skill_tag(pid):
                continue                                # same user: no evidence it is a skill process
            targets.append(pid)
        killed = []
        if self.cgroup_base is None and self.child_subreaper:      # with a cgroup, it already killed them
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    pass
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except OSError:
                    pass
        self._reap_zombies([pid for pid in targets if pid in direct])   # our children: collect exit status
        return killed

    def note_child_exit(self) -> None:
        """A skill process ended: keep sweeping for escapees for a while (no cgroup)."""
        self._last_exit = time.monotonic()

    def periodic_reap(self) -> None:
        """Called from the monitor thread. Without a cgroup, escapees of a run that has
        ended may reparent to us later (a fork chain, one generation at a time); sweep
        every ``REAP_PERIOD_S`` for ``REAP_WINDOW_S`` after the last skill exit. Never
        on the stop path."""
        if not (_SUBREAPER and self.child_subreaper and self.cgroup_base is None):
            return
        now = time.monotonic()
        if self._last_exit is None or now - self._last_exit > REAP_WINDOW_S or now < self._next_reap:
            return
        self._next_reap = now + REAP_PERIOD_S
        self.reap_orphans()

    def describe(self) -> dict:
        c = self.config
        if c.mode == INPROCESS:
            return {"mode": INPROCESS, "isolated": False,
                    "warning": "技能在运行时进程内执行（仅供调试）：技能代码可直接访问适配器与运行时内存"}
        out = {"mode": PROCESS, "isolated": True,
               "user": self.user[2] if self.user else "same-as-runtime",
               "runtime_non_dumpable": self.non_dumpable,
               "pids_cgroup": self.cgroup_base is not None,
               "child_subreaper": self.child_subreaper,
               "python": self.python, "python_usable": self.python_usable,
               **({"python_note": self.python_note} if self.python_note else {}),
               "limits": {"memory_bytes": c.memory_bytes, "max_cpu_seconds": c.max_cpu_seconds,
                          "nofile": c.nofile, "extra_tasks": c.extra_tasks, "fsize_bytes": c.fsize_bytes},
               "kill_grace_s": c.kill_grace_s, "network_isolated": False}
        if self.cgroup_base is None:
            out["process_containment"] = "best-effort"      # no cgroup: setsid escapees reaped after the fact
        # §5.0: a non-root runtime cannot drop privileges and usually has no cgroup;
        # the sandbox then works in "best-effort" mode and says why.
        reasons = []
        if self.user is None:
            reasons.append("运行时不是 root（无法降权到独立用户）：技能与运行时同一用户，"
                           "可读写运行时状态目录、可向运行时进程发信号")
        if self.cgroup_base is None:
            reasons.append("无可写 pids cgroup：调用 setsid() 脱离进程组的后代由孤儿收割者事后清理")
        if not self.python_usable:
            reasons.append(self.python_note or "技能用户无法执行解释器")
        out["runtime_user"] = _user_name(os.geteuid())
        out["privilege_drop"] = self.user is not None
        out["level"] = "best-effort" if reasons else "full"
        out["best_effort_reasons"] = reasons
        return out

    def close(self) -> None:
        self.reap_orphans(settle=True)
        if self.root is not None:
            _remove_tree(self.root)
        if self.cgroup_base is not None:
            try:
                self.cgroup_base.rmdir()
            except OSError:
                pass


def _remove_tree(path: Path) -> None:
    """Delete a sandbox directory, symlink-safe (Q2-01).

    A symlink is never followed: ``os.lstat`` decides what ``path`` is, the
    read-only package copy is made writable with ``fchmod`` over
    ``os.fwalk(follow_symlinks=False)`` (no path re-resolution), and
    ``shutil.rmtree`` on this platform removes entries with ``dir_fd``, so it
    cannot be redirected through a directory symlink swapped in mid-walk.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        try:
            os.unlink(path)                 # a symlink or stray file: remove the entry itself
        except OSError:
            pass
        return
    try:
        for _root, _dirs, _files, dirfd in os.fwalk(path, follow_symlinks=False):
            try:
                os.fchmod(dirfd, 0o700)
            except OSError:
                pass
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _sweep_dead_workspaces(base: Path, owner_uid: int) -> None:
    """Remove ``rt-<pid>-*`` workspaces of runtimes that no longer run (crash, kill -9).

    Only entries that are real directories (``lstat``, symlinks skipped) owned
    by this runtime's own uid are touched, and only when their PID is dead. The
    base itself is runtime-owned and not world-writable, so nothing hostile can
    appear here; the checks are defence in depth.
    """
    try:
        entries = list(os.scandir(base))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith("rt-"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != owner_uid:
            continue                        # never follow a planted symlink, never touch others' dirs
        parts = entry.name.split("-")
        if len(parts) >= 3 and parts[1].isdigit() and not _pid_alive(parts[1]):
            _remove_tree(Path(entry.path))


class _LineReader:
    """Bounded JSON-lines reader over the protocol socket."""

    EOF = object()

    def __init__(self, sock: socket.socket, max_bytes: int):
        self._sock = sock
        self._max = max_bytes
        self._buf = bytearray()

    def read(self, timeout: float):
        """Next message (dict), ``None`` on timeout, ``EOF``; raises ProtocolViolation."""
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                if idx > self._max:
                    raise ProtocolViolation(f"消息超过 {self._max} 字节")
                line = bytes(self._buf[:idx])
                del self._buf[:idx + 1]
                return self._parse(line)
            if len(self._buf) > self._max:
                raise ProtocolViolation(f"消息超过 {self._max} 字节")
            self._sock.settimeout(timeout)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                return self.EOF
            if not chunk:
                return self.EOF
            self._buf += chunk

    @staticmethod
    def _parse(line: bytes) -> dict:
        def reject(name):
            raise ValueError(name)
        try:
            msg = json.loads(line.decode("utf-8"), parse_constant=reject)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ProtocolViolation(f"畸形消息：{type(exc).__name__}") from None
        if not isinstance(msg, dict):
            raise ProtocolViolation("消息须为 JSON 对象")
        return msg


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _short_str(value: Any, limit: int = 200) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


# Allowed fields per request op, and their validators.
_REQUEST_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "call": {"cap": _short_str, "args": lambda v: isinstance(v, dict)},
    "read": {"cap": _short_str},
    "check_stop": {},
    "stopped": {},
    "time_left": {},
    "sleep": {"seconds": _number},
    "log": {"message": lambda v: isinstance(v, str) and len(v) <= 4000, "data": lambda v: isinstance(v, dict)},
    "progress": {"fraction": _number, "message": lambda v: v is None or (isinstance(v, str) and len(v) <= 4000)},
}
assert set(_REQUEST_FIELDS) == set(child_proto.REQUEST_OPS)
_RESERVED_LOG_KEYS = {"type", "type_", "ts", "seq", "run_id", "message"}


class SandboxHost:
    """Runs one skill in a child process and serves its context requests.

    ``run()`` is called on the run's own thread and returns the skill result
    (or raises like an in-process skill would). ``notify_stop()`` and
    ``kill()`` may be called from any thread and never block.
    """

    def __init__(self, sandboxes: Sandboxes, ctx: SkillContext, *, files_dir: Path, entry_file: str,
                 entry_attr: str, params: dict, timeout_wall_s: float, events):
        self._sb = sandboxes
        self.config = sandboxes.config
        self._ctx = ctx
        self._files_dir = Path(files_dir)
        self._entry_file = entry_file
        self._entry_attr = entry_attr
        self._params = params
        self._timeout_wall_s = timeout_wall_s
        self._events = events
        self._proc: subprocess.Popen | None = None
        self._cgroup: PidsCgroup | None = None
        self._sock: socket.socket | None = None
        self._signal_lock = threading.Lock()      # serialises signalling vs reaping (never blocks callers)
        self._reaped = False
        self._ready = False                       # handshake done: the child's SIGTERM handler is installed
        self._term_pending = False
        self.killed = False
        self.violation: str | None = None
        self.workspace: Path | None = None
        self.pid: int | None = None
        self._expected_id = 1
        self._tokens = float(self.config.request_burst)
        self._last_refill = time.monotonic()
        self._log_dropped = 0        # dropped by this host; the budget itself lives on the RunState
        self.requests_served = 0
        ctx.add_guard(self._require_alive)

    # ------------------------------------------------------------ control (any thread)
    def alive(self) -> bool:
        """True while the child has not exited (does not reap it)."""
        proc = self._proc
        if proc is None or self._reaped:
            return False
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return False
        return info is None

    def _require_alive(self) -> None:
        if self._proc is not None and not self.alive():
            raise SkillStopped("技能进程已退出；其在途请求作废")

    def notify_stop(self) -> None:
        """Stop path: ask the child to exit (SIGTERM). Non-blocking, never raises.

        Before the handshake the child's handler is not installed yet, so the
        signal is deferred until then (the monitor's SIGKILL grace still runs).
        """
        self._term_pending = True
        if not self._ready:
            return
        self._send_term()

    def _send_term(self) -> None:
        if not self._signal_lock.acquire(blocking=False):
            return                       # being reaped right now
        try:
            if self._proc is not None and not self._reaped:
                os.kill(self._proc.pid, signal.SIGTERM)
        except OSError:
            pass
        finally:
            self._signal_lock.release()

    def kill(self) -> None:
        """SIGKILL the child's whole process group. Non-blocking, idempotent."""
        self.killed = True
        if not self._signal_lock.acquire(blocking=False):
            return
        try:
            self._killpg()
        finally:
            self._signal_lock.release()

    def _killpg(self) -> None:
        if self._proc is not None and not self._reaped:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except OSError:
                pass
        if self._cgroup is not None:
            self._cgroup.kill_all()           # descendants that left the process group

    # ------------------------------------------------------------ main (run thread)
    def run(self) -> Any:
        try:
            self._prepare_workspace()
            parent_sock = self._spawn()
            if self.killed:                   # SIGKILL was requested before the process existed
                self.kill()
            reader = _LineReader(parent_sock, self.config.max_message_bytes)
            self._send(self._init_message())
            self._handshake(reader)
            self._ready = True
            if self._term_pending:            # a stop arrived during start-up
                self._send_term()
            return self._serve(reader)
        except ProtocolViolation as exc:
            self.violation = str(exc)
            self.kill()
            self._event("sandbox.violation", reason=str(exc))
            raise SkillFailure(f"技能进程违反沙箱协议，已终止：{exc}") from None
        finally:
            self._shutdown()

    def _event(self, type_: str, **data) -> None:
        self._events.emit(type_, run_id=self._ctx.run_id, pid=self.pid, **data)

    def _prepare_workspace(self) -> None:
        user = self._sb.user
        self.workspace = Path(tempfile.mkdtemp(prefix=f"{self._ctx.run_id}-", dir=self._sb.root))
        os.chmod(self.workspace, 0o711)
        pkg = self.workspace / "pkg"
        shutil.copytree(self._files_dir, pkg, ignore=shutil.ignore_patterns("__pycache__"))
        for dirpath, dirnames, filenames in os.walk(pkg, topdown=False):
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), 0o444)
            os.chmod(dirpath, 0o555)
        script = self.workspace / "child.py"
        shutil.copyfile(CHILD_SCRIPT, script)
        os.chmod(script, 0o444)
        tmp = self.workspace / "tmp"
        tmp.mkdir(mode=0o700)
        if user is not None:
            os.chown(tmp, user[0], user[1])

    def _limits(self) -> dict:
        c = self.config
        cpu = min(c.max_cpu_seconds, max(c.min_cpu_seconds, int(math.ceil(self._timeout_wall_s)) + 10))
        limits = {"RLIMIT_AS": c.memory_bytes, "RLIMIT_CPU": cpu, "RLIMIT_NOFILE": c.nofile,
                  "RLIMIT_FSIZE": c.fsize_bytes, "RLIMIT_CORE": 0}
        uid = self._sb.user[0] if self._sb.user else os.getuid()
        if uid != 0:                     # root ignores RLIMIT_NPROC
            current = count_tasks(uid)
            if current is not None:
                limits["RLIMIT_NPROC"] = current + c.extra_tasks
        return limits

    def _spawn(self) -> socket.socket:
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock = parent_sock
        tmp = str(self.workspace / "tmp")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               SKILL_TAG_VAR: SKILL_TAG,
               "HOME": tmp, "TMPDIR": tmp, "PYTHONDONTWRITEBYTECODE": "1"}
        argv = [self._sb.python, "-I", "-B", str(self.workspace / "child.py"), str(child_sock.fileno()),
                str(os.getpid()), json.dumps(self._limits())]
        kwargs: dict = {}
        user = self._sb.user
        if user is not None:
            kwargs.update(user=user[0], group=user[1], extra_groups=[])
        try:
            self._proc = subprocess.Popen(
                argv, cwd=str(self.workspace / "pkg"), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(child_sock.fileno(),),
                close_fds=True, start_new_session=True, umask=0o077, **kwargs)
        except OSError as exc:
            raise SkillFailure(f"无法启动技能进程（解释器 {self._sb.python}）：{exc}") from None
        finally:
            child_sock.close()
        self.pid = self._proc.pid
        _SKILL_SESSIONS.add(self.pid)          # start_new_session: its session id is its pid
        self._sb.register_child(self.pid)
        # The child waits for "init" before running any skill code, so it is in
        # the cgroup before it can fork.
        cgroup = PidsCgroup.create(self._sb.cgroup_base, self.workspace.name, self.config.extra_tasks)
        if cgroup is not None and cgroup.add(self.pid):
            self._cgroup = cgroup
        elif cgroup is not None:
            cgroup.remove()
        return parent_sock

    def _init_message(self) -> dict:
        ctx = self._ctx
        return {"type": "init", "protocol": child_proto.PROTOCOL, "run_id": ctx.run_id,
                "entry_file": self._entry_file, "entry_attr": self._entry_attr, "params": self._params,
                "allowed": sorted(ctx.declared), "capability_ids": sorted(ctx.body_capabilities),
                "manifest": ctx.manifest, "cap_params": ctx.all_params(), "models": ctx.model_view()}

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        sock = self._sock
        sock.settimeout(REPLY_SEND_TIMEOUT_S)
        try:
            sock.sendall(data)
        except socket.timeout:
            raise ProtocolViolation("技能进程不读取运行时回复") from None
        except OSError:
            pass                          # child gone: noticed by the read loop

    def _handshake(self, reader: _LineReader) -> None:
        deadline = time.monotonic() + self.config.handshake_timeout_s
        while True:
            msg = reader.read(POLL_S)
            if msg is _LineReader.EOF or (msg is None and not self.alive()):
                if self.killed:
                    raise SkillStopped(KILLED_MESSAGE)
                raise SkillFailure(f"技能进程启动失败（退出码 {self._exit_code()}）")
            if msg is not None:
                break
            if time.monotonic() > deadline:
                raise ProtocolViolation("技能进程启动握手超时")
        if msg.get("type") != "hello" or msg.get("protocol") != child_proto.PROTOCOL:
            raise ProtocolViolation("握手消息无效")
        self._event("sandbox.started", limits=msg.get("limits"), pids_cgroup=self._cgroup is not None,
                    user=self._sb.user[2] if self._sb.user else "same-as-runtime")

    def _exit_code(self, wait_s: float = 0.5) -> int | None:
        """Exit status without reaping (negative = signal); waits briefly after an EOF."""
        deadline = time.monotonic() + wait_s
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        try:
            info = os.waitid(os.P_PID, self._proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, AttributeError):
            return self._proc.returncode if self._proc else None
        if info is None:
            return None
        return -info.si_status if info.si_code in (os.CLD_KILLED, os.CLD_DUMPED) else info.si_status

    def _serve(self, reader: _LineReader) -> Any:
        while True:
            msg = reader.read(POLL_S)
            if msg is None:
                if self.alive():
                    continue
                msg = reader.read(0.0) or _LineReader.EOF   # drain a final message written just before exit
            if msg is _LineReader.EOF:
                code = self._exit_code()
                if self.killed:
                    raise SkillStopped(KILLED_MESSAGE)
                if self._ctx.stop_requested:
                    raise SkillStopped("技能进程已按停止指令退出")
                raise SkillFailure(f"技能进程意外退出（退出码 {code}）")
            kind = msg.get("type")
            if kind == "request":
                self._handle_request(msg)
            elif kind == "done":
                return self._finish(msg)
            else:
                raise ProtocolViolation(f"未知消息类型 {kind!r}")

    def _throttle(self) -> None:
        rate = self.config.max_requests_per_s
        now = time.monotonic()
        self._tokens = min(float(self.config.request_burst), self._tokens + (now - self._last_refill) * rate)
        self._last_refill = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) / rate)
            self._tokens = 1.0
            self._last_refill = time.monotonic()
        self._tokens -= 1.0

    def _validate_request(self, msg: dict) -> tuple[int, str]:
        request_id, op = msg.get("id"), msg.get("op")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id != self._expected_id:
            raise ProtocolViolation(f"请求编号无效（期望 {self._expected_id}，收到 {request_id!r}）")
        fields = _REQUEST_FIELDS.get(op) if isinstance(op, str) else None
        if fields is None:
            raise ProtocolViolation(f"未知请求 {op!r}")
        extra = set(msg) - {"type", "id", "op"} - set(fields)
        if extra:
            raise ProtocolViolation(f"请求 {op} 含未知字段 {sorted(extra)}")
        for name, ok in fields.items():
            if name not in msg or not ok(msg[name]):
                raise ProtocolViolation(f"请求 {op} 的字段 {name} 无效")
        self._expected_id += 1
        return request_id, op

    def _handle_request(self, msg: dict) -> None:
        request_id, op = self._validate_request(msg)
        self.requests_served += 1
        self._throttle()
        try:
            value = self._dispatch(op, msg)
            reply = {"type": "reply", "id": request_id, "ok": True, "value": value}
            json.dumps(reply, allow_nan=False)
        except BaseException as exc:          # noqa: B902 -- every outcome goes back to the skill
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            reply = {"type": "reply", "id": request_id, "ok": False, "error": self._error(exc)}
        self._send(reply)

    @staticmethod
    def _error(exc: BaseException) -> dict:
        error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        if isinstance(exc, RuntimeApiError):
            error["code"] = exc.code
        return error

    def _dispatch(self, op: str, msg: dict) -> Any:
        ctx = self._ctx
        if op == "call":
            return ctx.call(msg["cap"], **msg["args"])
        if op == "read":
            return ctx.read(msg["cap"])
        if op == "check_stop":
            ctx.check_stop()
            return None
        if op == "stopped":
            return ctx.stopped
        if op == "time_left":
            return ctx.time_left()
        if op == "sleep":
            ctx.sleep(msg["seconds"])
            return None
        # log / progress: bounded *per run* (all steps of a v1 application share the
        # same RunState) so a multi-step application cannot push safety events out of
        # telemetry one new process at a time (Q2-08).
        run = self._ctx._run
        if run.log_events >= self.config.max_log_events:
            run.log_dropped += 1
            self._log_dropped += 1
            if self._log_dropped == 1:
                self._event("sandbox.log_suppressed", limit=self.config.max_log_events)
            return None
        run.log_events += 1
        if op == "log":
            data = msg["data"]
            if len(json.dumps(data, ensure_ascii=False)) > self.config.max_log_data_bytes:
                data = {"truncated": True}
            # Keys that would overwrite event fields (a forged "type" etc.) are renamed.
            ctx.log(msg["message"], **{(f"skill_{k}" if k in _RESERVED_LOG_KEYS else k): v
                                       for k, v in data.items()})
        else:
            ctx.progress(msg["fraction"], msg["message"])
        return None

    def _finish(self, msg: dict) -> Any:
        extra = set(msg) - {"type", "ok", "result", "error", "output_tail"}
        if extra or not isinstance(msg.get("ok"), bool):
            raise ProtocolViolation("done 消息无效")
        tail = msg.get("output_tail")
        if isinstance(tail, str) and tail:
            self._event("sandbox.output", tail=tail[-child_proto.OUTPUT_TAIL_CHARS:])
        if self._log_dropped:
            self._event("sandbox.log_dropped", dropped=self._log_dropped)
        if msg["ok"]:
            return msg.get("result")
        error = msg.get("error")
        if not isinstance(error, dict):
            raise ProtocolViolation("done 消息缺少 error")
        name, message = str(error.get("type", "Error"))[:100], str(error.get("message", ""))[:2000]
        if error.get("interrupted") is True:
            cls = {"SkillCancelled": SkillCancelled, "SkillTimeout": SkillTimeout}.get(name, SkillStopped)
            raise cls(message)
        if name == "ActionFailed":
            raise ActionFailed(message)
        if name in ("_unserializable", "_too_large"):
            raise SkillFailure(message)
        traceback = error.get("traceback") if isinstance(error.get("traceback"), str) else None
        raise SkillFailure(f"{name}: {message}", traceback=traceback)

    def _shutdown(self) -> None:
        """Wait briefly for a voluntary exit, then SIGKILL the group, reap, clean up."""
        proc = self._proc
        if proc is not None:
            deadline = time.monotonic() + (0 if self.killed else self.config.kill_grace_s)
            while self.alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            with self._signal_lock:
                self._killpg()            # also catches grandchildren left in the group
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                self._reaped = proc.returncode is not None
            if self._cgroup is not None:
                self._cgroup.remove()
            self._sb.unregister_child(self.pid)
            self._sb.note_child_exit()
            orphans = self._sb.reap_orphans(settle=True)   # setsid escapees, when there is no cgroup
            self._event("sandbox.exited", returncode=proc.returncode, killed=self.killed,
                        violation=self.violation, reaped_orphans=len(orphans) or None)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.workspace is not None:
            _remove_tree(self.workspace)

