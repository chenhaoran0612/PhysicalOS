"""``runtime/deploy/device_install.sh``: install, start, status, stop and remove the runtime in a user's home.

The script is what puts the runtime on a robot (README §0.4), so it runs here the way it runs there:
copied next to the two wheels, as the current user, without network. A robot runs other software under
the same user, so most of these tests are about what the script must not do: touch anything outside its
install directory, or signal a process that is not the runtime it started.

The wheels are assembled from the package sources in this file (no setuptools, no index); what
``pip wheel`` produces was checked by hand on a device. Linux only: the script uses ``/proc``, ``setsid``,
``flock`` and GNU ``stat``/``realpath``, like the robots it is for.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from _support import REPO_ROOT, RUNTIME_DIR, http, load_toml, wait_until

SCRIPT = RUNTIME_DIR / "deploy" / "device_install.sh"
MARKER = ".neuxmind-device-install"       # the script's own file: only a directory holding it is "ours"
INSTALLING, INSTALLED = "state=installing\n", "state=installed\n"
MIN_TOKEN_CHARS = 24                       # the gateway refuses shorter tokens
COMMAND_TIMEOUT_S = 240
HUNG_STATUS_LIMIT_S = 30                   # status of a frozen runtime must give up well within this


def _skip_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "安装脚本面向机器人上的 Linux（用到 /proc、setsid、flock 和 GNU stat）"
    missing = [tool for tool in ("bash", "setsid", "flock", "realpath") if shutil.which(tool) is None]
    if missing:
        return "缺少 " + "、".join(missing)
    try:
        import ensurepip  # noqa: F401
        import venv  # noqa: F401
    except ImportError:
        return "这个解释器不能创建带 pip 的虚拟环境"
    return None


SKIP_REASON = _skip_reason()


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def build_wheel(dest: Path, project_dir: Path, packages: tuple[str, ...], scripts: dict[str, str] | None = None) -> Path:
    """A pure-Python wheel of ``packages`` under ``project_dir``; name, version and dependencies from its pyproject."""
    project = load_toml((project_dir / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"].replace("-", "_"), project["version"]
    info = f"{name}-{version}.dist-info"
    files: dict[str, bytes] = {}
    for package in packages:
        for path in sorted((project_dir / package).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(project_dir).as_posix()] = path.read_bytes()
    requires = "".join(f"Requires-Dist: {dependency}\n" for dependency in project.get("dependencies", []))
    files[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {version}\n"
                                 f"Requires-Python: >=3.10\n{requires}").encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if scripts:
        lines = "".join(f"{command} = {target}\n" for command, target in scripts.items())
        files[f"{info}/entry_points.txt"] = ("[console_scripts]\n" + lines).encode()
    record = "\n".join([_record_line(n, d) for n, d in files.items()] + [f"{info}/RECORD,,"]) + "\n"
    wheel = dest / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
        archive.writestr(f"{info}/RECORD", record)
    return wheel


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listen_addresses(port: int) -> set[str]:
    """Local addresses (hex, as in /proc/net/tcp*) with a listening socket on ``port``."""
    found = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            address, _, hex_port = fields[1].rpartition(":")
            if fields[3] == "0A" and int(hex_port, 16) == port:
                found.add(address)
    return found


def arguments(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def alive(pid: int) -> bool:
    try:                                   # a zombie is not a running process
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def snapshot(*directories: Path, exclude: Path | None = None) -> dict[str, str]:
    """Every path under ``directories`` (outside ``exclude``) with its content hash or link target."""
    seen: dict[str, str] = {}
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if exclude is not None and (path == exclude or exclude in path.parents):
                continue
            if path.is_symlink():
                seen[str(path)] = "link:" + os.readlink(path)
            elif path.is_file():
                seen[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                seen[str(path)] = "dir"
    return seen


class Device:
    """A throwaway "robot": a home directory and a package directory with the script and wheels."""

    def __init__(self, wheels: bool = True):
        self._tmp = tempfile.TemporaryDirectory(prefix="nx-device-")
        base = Path(self._tmp.name).resolve()
        self.home, self.package = base / "home", base / "pkg"
        self.home.mkdir()
        self.package.mkdir()
        (self.home / "notes.txt").write_text("belongs to the robot's owner\n")
        shutil.copy(SCRIPT, self.package / SCRIPT.name)
        if wheels:
            build_wheel(self.package, REPO_ROOT / "spec", ("neuxmind_spec",))
            build_wheel(self.package, RUNTIME_DIR, ("neuxmind_runtime",), {"neuxmind-runtime": "neuxmind_runtime.cli:main"})
        self.root = self.home / "neuxmind"
        self.port = free_port()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        return (self.root / "edge-token").read_text().strip()

    def pid(self) -> int:
        return int((self.root / "runtime.pid").read_text())

    def runtime_pids(self) -> list[int]:
        """Live processes running this device's runtime, whatever the pid file says."""
        entry = str(self.root / "venv" / "bin" / "neuxmind-runtime")
        return [int(p.name) for p in Path("/proc").iterdir()
                if p.name.isdigit() and entry in arguments(int(p.name)) and alive(int(p.name))]

    def _command(self, command: str, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("NX_", "BRAIN_", "PYTHON", "VIRTUAL_ENV", "PIP_"))}
        clean.update({"HOME": str(self.home), "NX_PORT": str(self.port), "NX_PYTHON": sys.executable})
        clean.update(env)
        return ["bash", str(self.package / SCRIPT.name), command], clean

    # The script always runs in a session of its own: if it ever signalled its own process group
    # (a pid file holding 0 once did that), it must not take the test runner down with it.
    def popen(self, command: str, **env: str) -> subprocess.Popen:
        argv, clean = self._command(command, env)
        return subprocess.Popen(argv, env=clean, cwd=self.package, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def run(self, command: str, timeout: float = COMMAND_TIMEOUT_S, **env: str) -> subprocess.CompletedProcess:
        argv, clean = self._command(command, env)
        return subprocess.run(argv, env=clean, cwd=self.package, capture_output=True, text=True, timeout=timeout,
                              start_new_session=True)

    def start(self, **env: str) -> subprocess.CompletedProcess:
        started = self.run("start", **env)
        if started.returncode != 0:
            raise AssertionError(f"start failed ({started.returncode}): {started.stdout}{started.stderr}")
        return started

    def close(self) -> None:
        for pid in self.runtime_pids():
            for send in (os.killpg, os.kill):
                try:
                    send(pid, signal.SIGKILL)
                except OSError:
                    continue
        self._tmp.cleanup()


def installed_device() -> Device:
    device = Device()
    installed = device.run("install")
    if installed.returncode != 0:
        device.close()
        raise AssertionError("install failed: " + installed.stdout + installed.stderr)
    return device


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = Device()
        cls.before = snapshot(cls.device.home, cls.device.package)
        cls.installed = cls.device.run("install")

    @classmethod
    def tearDownClass(cls):
        cls.device.close()

    def test_install_puts_the_runtime_in_a_private_directory(self):
        root = self.device.root

        self.assertEqual(self.installed.returncode, 0, self.installed.stderr)
        self.assertTrue((root / "venv" / "bin" / "neuxmind-runtime").is_file())
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / "state").stat().st_mode), 0o700)
        self.assertEqual((root / MARKER).read_text(), INSTALLED)

    def test_install_writes_a_token_only_the_owner_can_read(self):
        token_file = self.device.root / "edge-token"

        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        self.assertGreaterEqual(len(self.device.token), MIN_TOKEN_CHARS)
        self.assertNotIn(self.device.token, self.installed.stdout + self.installed.stderr)

    def test_install_brings_only_the_two_packages(self):
        listed = subprocess.run([str(self.device.root / "venv" / "bin" / "pip"), "list", "--format", "json",
                                 "--disable-pip-version-check"], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)

        names = {entry["name"].lower().replace("_", "-") for entry in json.loads(listed.stdout)}
        self.assertLessEqual({"neuxmind-runtime", "neuxmind-spec"}, names)
        self.assertLessEqual(names, {"neuxmind-runtime", "neuxmind-spec", "pip", "setuptools", "wheel"})

    def test_install_changes_nothing_outside_its_directory(self):
        after = snapshot(self.device.home, self.device.package, exclude=self.device.root)

        self.assertEqual(after, self.before)

    def test_second_install_is_refused_and_keeps_the_first(self):
        token_before = self.device.token

        again = self.device.run("install")

        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)
        self.assertEqual(self.device.token, token_before)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class RefusedInstallTests(unittest.TestCase):
    def device(self, wheels: bool = True) -> Device:
        device = Device(wheels=wheels)
        self.addCleanup(device.close)
        return device

    def elsewhere(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="nx-elsewhere-")).resolve()
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def test_missing_wheels_leave_nothing_behind(self):
        device = self.device(wheels=False)

        result = device.run("install")

        self.assertEqual(result.returncode, 2)
        self.assertIn("wheel", result.stderr)
        self.assertFalse(device.root.exists())

    def test_failed_install_removes_what_it_created(self):
        device = self.device()
        next(device.package.glob("neuxmind_runtime-*.whl")).write_bytes(b"not a wheel")

        result = device.run("install")

        self.assertEqual(result.returncode, 1)
        self.assertFalse(device.root.exists())

    def test_install_outside_the_home_directory_is_refused(self):
        device, elsewhere = self.device(), self.elsewhere()

        result = device.run("install", NX_ROOT=str(elsewhere / "neuxmind"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_install_path_that_climbs_out_of_home_is_refused(self):
        device = self.device()

        result = device.run("install", NX_ROOT=str(device.home) + "/../escaped")

        self.assertEqual(result.returncode, 2)
        self.assertFalse((device.home.parent / "escaped").exists())

    def test_install_through_a_symlink_out_of_home_is_refused(self):
        device, elsewhere = self.device(), self.elsewhere()
        (device.home / "link").symlink_to(elsewhere)

        result = device.run("install", NX_ROOT=str(device.home / "link" / "neuxmind"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_install_under_a_missing_parent_creates_nothing(self):
        device = self.device()

        result = device.run("install", NX_ROOT=str(device.home / "a" / "b" / "neuxmind"))

        self.assertEqual(result.returncode, 2)
        self.assertFalse((device.home / "a").exists())

    def test_empty_home_is_refused(self):
        device, elsewhere = self.device(), self.elsewhere()

        result = device.run("install", HOME="", NX_ROOT=str(elsewhere / "neuxmind"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unfinished_install_is_replaced_by_the_next_install(self):
        device = self.device()
        device.root.mkdir()
        (device.root / MARKER).write_text(INSTALLING)
        (device.root / "half-written").write_text("left by an interrupted install")

        result = device.run("install")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((device.root / "half-written").exists())
        self.assertTrue((device.root / "venv" / "bin" / "neuxmind-runtime").is_file())

    def test_empty_directory_left_by_an_interrupted_install_is_reused(self):
        device = self.device()
        device.root.mkdir()                # interrupted after mkdir, before the marker was written

        result = device.run("install")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((device.root / MARKER).read_text(), INSTALLED)

    def test_relative_install_path_is_resolved_before_it_is_judged(self):
        device = self.device()             # the script runs with the package directory as its cwd

        result = device.run("install", NX_ROOT="neuxmind")

        self.assertEqual(result.returncode, 2)
        self.assertFalse((device.package / "neuxmind").exists())

    def test_install_through_a_symlinked_home_lands_in_the_real_home(self):
        device = self.device()
        link = device.home.parent / "home-link"
        link.symlink_to(device.home)

        result = device.run("install", HOME=str(link))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((device.root / "venv" / "bin" / "neuxmind-runtime").is_file())

    def test_start_before_install_is_refused_and_creates_nothing(self):
        device = self.device()
        before = snapshot(device.home, device.package)

        result = device.run("start")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not installed", result.stderr)
        self.assertEqual(snapshot(device.home, device.package), before)

    def test_unknown_command_prints_usage(self):
        device = self.device(wheels=False)

        result = device.run("upgrade")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = installed_device()

    @classmethod
    def tearDownClass(cls):
        cls.device.close()

    def tearDown(self):
        self.device.run("stop")

    def test_start_serves_on_loopback_only(self):
        self.device.start()

        self.assertEqual(listen_addresses(self.device.port), {"0100007F"})

    def test_started_runtime_needs_the_token_and_has_no_hardware(self):
        self.device.start()

        refused, _ = http(self.device.url, "/v1/status", token=None)
        status, body = http(self.device.url, "/v1/status", token=self.device.token)

        self.assertEqual(refused, 401)
        self.assertEqual(status, 200)
        self.assertIs(body["simulated"], True)

    def test_token_is_not_in_the_process_arguments(self):
        self.device.start()

        self.assertNotIn(self.device.token, " ".join(arguments(self.device.pid())))

    def test_the_runtime_is_given_its_log_to_rotate(self):
        self.device.start()

        log = self.device.root / "runtime.log"
        given = arguments(self.device.pid())

        self.assertEqual(given[given.index("--log-file") + 1], str(log))
        self.assertIn("NeuxMind runtime", log.read_text())           # the start-up lines arrive in that file
        self.assertEqual(log.stat().st_mode & 0o077, 0)

    def test_runtime_outlives_the_shell_that_started_it(self):
        self.device.start()

        pid = self.device.pid()

        self.assertEqual(self.device.runtime_pids(), [pid])
        self.assertEqual(os.getsid(pid), pid)

    def test_second_start_keeps_the_running_runtime(self):
        self.device.start()
        pid = self.device.pid()

        again = self.device.run("start")

        self.assertEqual(again.returncode, 0)
        self.assertIn("already running", again.stdout)
        self.assertEqual(self.device.runtime_pids(), [pid])

    def test_concurrent_starts_leave_one_tracked_runtime(self):
        first, second = self.device.popen("start"), self.device.popen("start")

        outputs = [process.communicate(timeout=COMMAND_TIMEOUT_S) for process in (first, second)]

        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        self.assertEqual(self.device.runtime_pids(), [self.device.pid()])

    def test_start_refuses_a_port_that_something_else_holds(self):
        with socket.socket() as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", self.device.port))
            holder.listen()

            result = self.device.run("start")

        self.assertEqual(result.returncode, 1)
        self.assertIn("in use", result.stderr)
        self.assertEqual(self.device.runtime_pids(), [])
        self.assertFalse((self.device.root / "runtime.pid").exists())

    def test_status_reports_the_running_runtime_without_the_token(self):
        self.device.start()

        result = self.device.run("status")

        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(report["simulated"], True)
        self.assertIn("level", report["sandbox"])
        self.assertNotIn(self.device.token, result.stdout + result.stderr)

    def test_status_of_a_stopped_runtime_exits_1(self):
        result = self.device.run("status")

        self.assertEqual(result.returncode, 1)
        self.assertIn("not running", result.stdout)

    def test_status_of_a_frozen_runtime_gives_up(self):
        self.device.start()
        pid = self.device.pid()
        os.kill(pid, signal.SIGSTOP)
        try:
            result = self.device.run("status", timeout=HUNG_STATUS_LIMIT_S)
        finally:
            if alive(pid):
                os.kill(pid, signal.SIGCONT)

        self.assertEqual(result.returncode, 1)
        self.assertIn("did not answer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_manifest_prints_what_the_device_declares(self):
        self.device.start()

        result = self.device.run("manifest")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema"], "robot.adapter.v1")

    def test_stop_ends_the_process_and_can_be_repeated(self):
        self.device.start()
        pid = self.device.pid()

        stopped = self.device.run("stop")
        again = self.device.run("stop")

        self.assertEqual(stopped.returncode, 0)
        self.assertFalse(alive(pid))
        self.assertEqual(listen_addresses(self.device.port), set())
        self.assertEqual(again.returncode, 0)
        self.assertIn("not running", again.stdout)

    def test_failed_start_says_why_and_leaves_no_stale_pid(self):
        result = self.device.run("start", NX_ADAPTER="no_such_adapter")

        self.assertEqual(result.returncode, 1)
        self.assertIn("did not come up", result.stderr)
        self.assertFalse((self.device.root / "runtime.pid").exists())
        self.assertEqual(self.device.runtime_pids(), [])

    def test_stale_pid_of_another_process_is_never_signalled(self):
        bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)   # its own group, like a service
        self.addCleanup(bystander.kill)
        (self.device.root / "runtime.pid").write_text(f"{bystander.pid}\n")

        status = self.device.run("status")
        stopped = self.device.run("stop")
        self.device.start()
        runtime = self.device.pid()
        self.device.run("stop")

        self.assertEqual(status.returncode, 1)
        self.assertIn("not running", stopped.stdout)
        self.assertNotEqual(runtime, bystander.pid)
        self.assertIsNone(bystander.poll())

    def test_garbage_in_the_pid_file_is_never_signalled(self):
        # 0 means "my own process group" and -1 "every process of this user": a script that passed either
        # to kill would take down far more than the runtime. The script therefore runs with a kill that
        # only records its arguments (an exported function shadows the builtin), so a regression fails
        # this test instead of terminating whatever else the user is running.
        attempts = self.device.home.parent / "kill-attempts"
        recorder = {"BASH_FUNC_kill%%": f'() {{ echo "$*" >> "{attempts}"; }}'}
        for content in ("0\n", "-1\n", "1\n", "12 34\n", "abc\n", ""):
            with self.subTest(content=content):
                (self.device.root / "runtime.pid").write_text(content)

                stopped = self.device.run("stop", **recorder)

                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertIn("not running", stopped.stdout)
                self.assertFalse(attempts.exists(), attempts.read_text() if attempts.exists() else "")

    def test_start_and_stop_change_nothing_outside_the_install(self):
        before = snapshot(self.device.home, self.device.package, exclude=self.device.root)

        self.device.start()
        self.device.run("status")
        self.device.run("stop")

        self.assertEqual(snapshot(self.device.home, self.device.package, exclude=self.device.root), before)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class RemoveTests(unittest.TestCase):
    def device(self, wheels: bool = False) -> Device:
        device = Device(wheels=wheels)
        self.addCleanup(device.close)
        return device

    def test_remove_stops_the_runtime_and_deletes_only_the_install(self):
        device = installed_device()
        self.addCleanup(device.close)
        before = snapshot(device.home, device.package, exclude=device.root)
        device.start()
        pid = device.pid()

        removed = device.run("remove")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(alive(pid))
        self.assertFalse(device.root.exists())
        self.assertEqual(snapshot(device.home, device.package), before)

    def test_remove_refuses_a_directory_outside_home(self):
        device = self.device()
        elsewhere = Path(tempfile.mkdtemp(prefix="nx-elsewhere-")).resolve()
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (elsewhere / "keep.txt").write_text("not ours")

        result = device.run("remove", NX_ROOT=str(elsewhere))

        self.assertEqual(result.returncode, 2)
        self.assertTrue((elsewhere / "keep.txt").is_file())

    def test_remove_refuses_home_itself_however_it_is_spelled(self):
        device = self.device()

        for spelling in (f"{device.home}//", f"{device.home}/", f"{device.home}/.", f"{device.home}/x/.."):
            with self.subTest(spelling=spelling):
                result = device.run("remove", NX_ROOT=spelling)

                self.assertEqual(result.returncode, 2)
                self.assertTrue((device.home / "notes.txt").is_file())

    def test_remove_refuses_a_directory_that_is_not_an_install(self):
        device = self.device()
        documents = device.home / "Documents"
        documents.mkdir()
        (documents / "thesis.txt").write_text("three years of work")

        result = device.run("remove", NX_ROOT=str(documents))

        self.assertEqual(result.returncode, 2)
        self.assertTrue((documents / "thesis.txt").is_file())

    def test_remove_refuses_a_lookalike_without_the_marker(self):
        device = self.device()             # e.g. a developer's own checkout that happens to sit at ~/neuxmind
        entry = device.root / "venv" / "bin" / "neuxmind-runtime"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/bin/sh\n")
        entry.chmod(0o755)
        (device.root / "edge-token").write_text("someone else's token\n")
        before = snapshot(device.home)

        result = device.run("remove")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(snapshot(device.home), before)

    def test_remove_leaves_an_empty_directory_alone(self):
        device = self.device()
        device.root.mkdir()

        result = device.run("remove")

        self.assertEqual(result.returncode, 0)
        self.assertIn("not installed", result.stdout)
        self.assertTrue(device.root.is_dir())

    def test_stop_leaves_a_directory_it_did_not_make_alone(self):
        device = self.device()
        project = device.home / "projects" / "other"
        project.mkdir(parents=True)
        (project / "runtime.pid").write_text("4242\n")
        before = snapshot(device.home)

        result = device.run("stop", NX_ROOT=str(project))

        self.assertEqual(result.returncode, 0)
        self.assertIn("not running", result.stdout)
        self.assertEqual(snapshot(device.home), before)

    def test_remove_clears_an_unfinished_install(self):
        device = self.device()
        device.root.mkdir()
        (device.root / MARKER).write_text(INSTALLING)

        result = device.run("remove")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(device.root.exists())

    def test_remove_with_nothing_installed_does_nothing(self):
        device = self.device()
        before = snapshot(device.home, device.package)

        result = device.run("remove")

        self.assertEqual(result.returncode, 0)
        self.assertIn("not installed", result.stdout)
        self.assertEqual(snapshot(device.home, device.package), before)


if __name__ == "__main__":
    unittest.main()
