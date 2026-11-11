"""Shared test helpers. Every test uses simulated bodies only."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNTIME_DIR.parent
for p in (str(RUNTIME_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import neuxmind_runtime  # noqa: E402,F401  (bootstraps neuxmind_spec)
from neuxmind_runtime.adapters.sim_arm import SimArm  # noqa: E402
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped  # noqa: E402
from neuxmind_runtime.core import EdgeRuntime  # noqa: E402
from neuxmind_runtime.gateway import GatewayServer  # noqa: E402
from neuxmind_runtime.sandbox import SandboxConfig, choose_python, python_usable, resolve_user  # noqa: E402
from neuxmind_runtime.skills import load_package_dir  # noqa: E402

EXAMPLES = RUNTIME_DIR / "examples" / "skills"
TOKEN = "test-runtime-token-0123456789abcdef"


# Interpreter for skill processes in tests (default: the runtime's own, see sandbox.choose_python).
SANDBOX_PYTHON = os.environ.get("NEUXMIND_TEST_SANDBOX_PYTHON") or None


def _sandbox_skip_reason() -> str | None:
    """Why process-sandbox tests cannot run here, or ``None``.

    As root the skill runs as an unprivileged user, which must be able to execute
    the interpreter. A uv/pyenv Python under ``/root`` (0700) is not executable by
    ``nobody``; the runtime then reports ``sandbox.python_usable=false`` and every
    skill run fails -- an environment problem, not a code path to test 80 times.
    """
    user = resolve_user("auto")
    python, _note = choose_python(SANDBOX_PYTHON, user)
    if python_usable(python, user):
        return None
    return (f"技能用户 {user[2]} 无法执行解释器 {python}（例如 uv/pyenv 装在 /root 下）；"
            "请用系统解释器运行测试，或设置 NEUXMIND_TEST_SANDBOX_PYTHON 为技能用户可执行的同版本解释器")


SANDBOX_SKIP_REASON = _sandbox_skip_reason()

NOBODY_IDS = (65534, 65534)


def _traversable_tmp_root() -> str | None:
    """A temp root whose whole path ``nobody`` can traverse, or ``None``.

    The default temp dir is not always one: on the user's machine ``TMPDIR`` was
    under ``/sessions/...`` with a 0700 parent, so files created there could never
    be executed by ``nobody`` however their own bits were set (iter03 leftover)."""
    from neuxmind_runtime.sandbox import user_can_execute
    seen = []
    for candidate in (tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"):
        real = os.path.realpath(candidate)
        if real in seen or not os.path.isdir(real) or not os.access(real, os.W_OK):
            continue
        seen.append(real)
        if user_can_execute(real, *NOBODY_IDS):
            return real
    return None


TRAVERSABLE_TMP = _traversable_tmp_root()
TRAVERSABLE_SKIP_REASON = None if TRAVERSABLE_TMP else (
    f"没有 nobody 可穿越的临时目录（默认临时目录 {tempfile.gettempdir()} 的上级目录对 nobody 不可执行，"
    "/tmp、/var/tmp、/dev/shm 也不可用）；请把 TMPDIR 设为 nobody 可穿越的目录")


def public_tempdir(prefix: str = "nx-public-") -> str:
    """``mkdtemp`` (mode 0755) in a directory ``nobody`` can traverse; SkipTest when there is none."""
    if TRAVERSABLE_TMP is None:
        raise unittest.SkipTest(TRAVERSABLE_SKIP_REASON)
    path = tempfile.mkdtemp(prefix=prefix, dir=TRAVERSABLE_TMP)
    os.chmod(path, 0o755)
    return path


def example(name: str) -> dict:
    return load_package_dir(EXAMPLES / name)


def custom_package(skill_id: str, code: str, requires=("system.health",), parameters=(), timeout_s=30,
                   optional=()) -> dict:
    """Build an inline skill package for tests."""
    return {"schema": "skill.package.v1", "id": skill_id, "version": "0.1.0", "entry": "skill.py:run",
            "requires": [{"id": r} for r in requires], "optional": [{"id": o} for o in optional],
            "parameters": list(parameters), "timeout_s": timeout_s, "files": {"skill.py": code}}


def load_toml(text: str) -> dict:
    """``tomllib.loads`` on Python 3.11+; a small fallback for 3.10 (§5.0).

    The fallback understands what ``pyproject.toml`` uses here: ``[a.b]`` tables,
    ``key = "string"``, numbers, booleans and one-line lists of strings.
    """
    try:
        import tomllib                     # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli                       # optional backport
        return tomli.loads(text)
    except ImportError:
        pass
    import ast
    import re
    root: dict = {}
    table = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[([A-Za-z0-9_.\-]+)\]", line)
        if m:
            table = root
            for part in m.group(1).split("."):
                table = table.setdefault(part, {})
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"无法解析的 TOML 行：{raw!r}")
        key = key.strip().strip('"')
        value = value.split(" #")[0].strip()
        if value in ("true", "false"):
            table[key] = value == "true"
        else:
            table[key] = ast.literal_eval(value)
    return root


def readline_within(stream, timeout: float) -> str:
    """``stream.readline()`` that gives up after ``timeout`` s (a test must never hang)."""
    import threading
    out: list = []
    reader = threading.Thread(target=lambda: out.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not out:
        raise AssertionError(f"{timeout} s 内没有读到输出行")
    return out[0]


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("条件未在时限内满足")


class RuntimeFixture:
    """An EdgeRuntime on a temp state dir, optionally served over HTTP."""

    def __init__(self, body: str = "quadruped", time_scale: float = 20.0, http: bool = False,
                 state_dir: str | None = None, sandbox="process"):
        self._tmp = None
        if state_dir is None:
            self._tmp = tempfile.TemporaryDirectory()
            state_dir = self._tmp.name
        self.state_dir = state_dir
        if sandbox == "process":
            if SANDBOX_SKIP_REASON:
                raise unittest.SkipTest(SANDBOX_SKIP_REASON)
            if SANDBOX_PYTHON:
                sandbox = SandboxConfig(python=SANDBOX_PYTHON)
        adapter = SimQuadruped(time_scale=time_scale) if body == "quadruped" else SimArm(time_scale=time_scale)
        self.adapter = adapter
        self.runtime = EdgeRuntime(adapter, state_dir, sandbox=sandbox)
        self.server = GatewayServer(self.runtime, TOKEN, port=0).start() if http else None

    @property
    def url(self) -> str:
        return self.server.url

    def install(self, package: dict, request_id: str | None = None) -> dict:
        rid = request_id or f"install-{package['id']}-{time.monotonic_ns()}"
        return self.runtime.install_skill({"request_id": rid, "package": package})

    def run(self, skill: str, parameters=None, **extra) -> str:
        payload = {"request_id": f"run-{skill}-{time.monotonic_ns()}", "parameters": parameters or {},
                   "hardware_authorized": True, **extra}
        return self.runtime.run_skill(skill, payload)["run_id"]

    def wait(self, run_id: str, timeout: float = 10.0) -> dict:
        return self.runtime.runner.wait(run_id, timeout)

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
        self.runtime.runner.stop_all("test teardown")
        self.runtime.supervisor.halt_motion("test teardown")
        self.runtime.close()
        if self._tmp is not None:
            self._tmp.cleanup()


def http(url: str, path: str, payload=None, token: str | None = TOKEN, method: str | None = None,
         content_type: str = "application/json"):
    """Return ``(status, json_body)``; never raises on HTTP errors."""
    data = None
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# ----------------------------------------------------------------- ROS 2 test double
ROS2_DOUBLE = RUNTIME_DIR / "tests" / "ros2_double"
_DOUBLE_MODULES = ("rclpy", "geometry_msgs", "nav_msgs", "sensor_msgs", "nav2_msgs", "action_msgs", "fake_robot")


class ros2_double:
    """``with ros2_double() as graph:`` -- the rclpy test double on ``sys.path`` (and its fake
    robot running) for the block only; afterwards the modules are purged, so ``import rclpy``
    fails again for tests that check the "no ROS 2" error. NOT ROS 2 (see ros2_double/README.md)."""

    def __enter__(self):
        sys.path.insert(0, str(ROS2_DOUBLE))
        import rclpy._graph as graph
        graph.ensure_robot()
        self.graph = graph
        return graph

    def __exit__(self, *exc):
        robot = self.graph.robot()
        if robot is not None:
            robot._stop.set()
            robot.thread.join(1.0)
        for name in list(sys.modules):
            if name.split(".")[0] in _DOUBLE_MODULES:
                del sys.modules[name]
        while str(ROS2_DOUBLE) in sys.path:
            sys.path.remove(str(ROS2_DOUBLE))
        return False


def ros2_config(**limits) -> dict:
    from neuxmind_runtime.adapters import ros2
    config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
    config["limits"].update(max_vx=0.8, max_wz=1.0, **limits)
    return config
