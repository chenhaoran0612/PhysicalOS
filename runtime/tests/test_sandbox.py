"""Skill process sandbox (INTERFACES_V1 §4.2): normal path, SDK templates, attacks.

Every attack test also checks that the runtime stays healthy and that a stop
still takes effect within 1 s while the hostile skill is running.
"""
import json
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _support
from _support import TOKEN, RuntimeFixture, custom_package, example, http, wait_until

from neuxmind_runtime import cli
from neuxmind_runtime import sandbox as sandbox_module
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.sandbox import SandboxConfig, count_tasks

STOP_BUDGET_S = 1.0
MINIMAL_ENV = {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
               "NEUXMIND_SKILL_SANDBOX"}   # Q3-05: non-secret marker for the same-user reaper


def sandbox_events(fx, run_id, type_):
    return [e for e in fx.runtime.events.since(0) if e["type"] == type_ and e.get("run_id") == run_id]


def child_pid(fx, run_id):
    started = wait_until(lambda: sandbox_events(fx, run_id, "sandbox.started"), 10)
    return started[0]["pid"]


def process_gone(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    status = Path(f"/proc/{pid}/status")
    try:
        return "State:\tZ" in status.read_text()      # zombie of someone else's child: not running
    except OSError:
        return True


def escaped_skill_processes(uid: int) -> list[int]:
    """Processes of ``uid`` outside this (runtime) process's session: skill processes
    always start their own session, so after a run nothing of ``uid`` may remain there."""
    me, mine = os.getpid(), os.getsid(0)
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()
            cmdline = Path(f"/proc/{name}/cmdline").read_bytes().decode(errors="replace")
        except (OSError, IndexError):
            continue
        if fields[0] != b"Z" and int(fields[3]) != mine and "child.py" in cmdline:
            found.append(int(name))
    return found


def timed_http_stop(fx) -> float:
    started = time.monotonic()
    status, body = http(fx.url, "/v2/stop", {"reason": "sandbox test"})
    elapsed = time.monotonic() - started
    assert status == 200 and body["latched"], (status, body)
    return elapsed


class NormalPathTests(unittest.TestCase):
    def test_examples_run_in_child_process(self):
        quad, arm = RuntimeFixture("quadruped"), RuntimeFixture("arm", time_scale=50.0)
        try:
            for fx, name, params in ((quad, "status_report", {}), (quad, "patrol", {"waypoints": [[1, 0]]}),
                                     (arm, "status_report", {}), (arm, "wave", {"cycles": 1})):
                with self.subTest(skill=name, body=fx.runtime.manifest["family"]):
                    fx.install(example(name))
                    run_id = fx.run(name, params)
                    record = fx.wait(run_id, 20)
                    self.assertEqual(record["status"], "succeeded", record["error"])
                    self.assertEqual(record["sandbox"], "process")
                    pid = child_pid(fx, run_id)
                    self.assertNotEqual(pid, os.getpid())
                    self.assertTrue(wait_until(lambda: process_gone(pid), 5))
        finally:
            quad.close()
            arm.close()

    def test_context_api_over_the_proxy(self):
        code = '''
from neuxmind_runtime.skills import ActionFailed, SkillError, Skill
class Probe(Skill):
    def run(self, ctx, n=2):
        out = {"params": ctx.params, "run_id": ctx.run_id, "simulated": ctx.simulated,
               "family": ctx.manifest["family"], "has_battery": ctx.has("sensor.battery"),
               "has_velocity": ctx.has("base.velocity"), "max_vx": ctx.param("base.velocity", "max_vx"),
               "stopped": ctx.stopped, "time_left_positive": ctx.time_left() > 0}
        ctx.check_stop()
        ctx.sleep(0.5)
        ctx.log("hello", n=n, type="safety.estop", run_id="forged")
        ctx.progress(0.5, "half")
        out["battery"] = ctx.read("sensor.battery")["percent"] > 0
        out["posture"] = ctx.call("legged.posture", posture="sit")
        try:
            ctx.call("base.velocity", vx=1, wz=0)
        except SkillError as exc:
            out["undeclared"] = type(exc).__name__
        try:
            ctx.call("legged.posture", posture="fly")
        except Exception as exc:
            out["safety"] = [type(exc).__name__, getattr(exc, "code", None)]
        return out
'''
        fx = RuntimeFixture("quadruped")
        try:
            package = custom_package("probe", code, requires=("system.health", "legged.posture"),
                                     optional=("sensor.battery",),
                                     parameters=[{"name": "n", "type": "int", "default": 2}])
            package["entry"] = "skill.py:Probe"
            fx.install(package)
            run_id = fx.run("probe", {"n": 3})
            record = fx.wait(run_id, 10)
            self.assertEqual(record["status"], "succeeded", record)
            out = record["result"]
            self.assertEqual(out["params"], {"n": 3})
            self.assertEqual(out["run_id"], run_id)
            self.assertTrue(out["simulated"] and out["has_battery"] and out["battery"])
            self.assertFalse(out["has_velocity"])          # not declared by the skill
            self.assertEqual(out["max_vx"], fx.runtime.bus.supervisor.params["base.velocity"]["max_vx"])
            self.assertEqual((out["stopped"], out["time_left_positive"]), (False, True))
            self.assertEqual(out["undeclared"], "SkillError")
            self.assertEqual(out["safety"][0], "BadRequest")
            self.assertEqual(fx.adapter.posture, "sit")
            logs = [e for e in record["events"] if e["type"] == "log"]
            self.assertEqual(logs[0]["message"], "hello")
            # Forged event fields are renamed, never overwrite the event type / run id.
            self.assertEqual((logs[0]["skill_type"], logs[0]["skill_run_id"]), ("safety.estop", "forged"))
            self.assertFalse([e for e in fx.runtime.events.since(0) if e["type"] == "safety.estop"])
            self.assertTrue(any(e["type"] == "progress" for e in record["events"]))
        finally:
            fx.close()

    def test_failure_and_unserializable_result(self):
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(custom_package("boom", "def run(ctx):\n    raise ValueError('boom')\n"))
            fx.install(custom_package("weird", "def run(ctx):\n    return {'x': object()}\n"))
            fx.install(custom_package("big", "def run(ctx):\n    return {'x': 'a' * (3 << 20)}\n"))
            record = fx.wait(fx.run("boom"))
            self.assertEqual((record["status"], record["error"]), ("failed", "ValueError: boom"))
            self.assertIn("ValueError", [e for e in record["events"] if e["type"] == "exception"][0]["traceback"])
            record = fx.wait(fx.run("weird"))
            self.assertEqual((record["status"], record["error"]), ("failed", "技能返回值不能序列化为 JSON"))
            record = fx.wait(fx.run("big"))
            self.assertEqual((record["status"], record["error"]), ("failed", "技能返回值过大"))
        finally:
            fx.close()

    def test_inprocess_mode_is_explicit_and_visible(self):
        default = RuntimeFixture("quadruped")
        debug = RuntimeFixture("quadruped", sandbox="inprocess")
        try:
            self.assertEqual(default.runtime.status()["sandbox"]["mode"], "process")
            box = debug.runtime.status()["sandbox"]
            self.assertEqual((box["mode"], box["isolated"]), ("inprocess", False))
            self.assertIn("warning", box)
            started = [e for e in debug.runtime.events.since(0) if e["type"] == "runtime.started"][0]
            self.assertEqual(started["sandbox"]["mode"], "inprocess")
            debug.install(example("status_report"))
            record = debug.wait(debug.run("status_report"))
            self.assertEqual((record["status"], record["sandbox"]), ("succeeded", "inprocess"))
            self.assertFalse([e for e in debug.runtime.events.since(0) if e["type"] == "sandbox.started"])
            with self.assertRaises(ValueError):
                SandboxConfig(mode="none")
        finally:
            default.close()
            debug.close()

    def test_cli_sandbox_flag(self):
        seen = []
        with mock.patch.object(cli, "_serve", lambda args: seen.append(args.sandbox) or 0):
            self.assertEqual(cli.main(["serve", "--adapter", "sim_quadruped"]), 0)
            self.assertEqual(cli.main(["serve", "--adapter", "sim_quadruped", "--sandbox", "inprocess"]), 0)
        self.assertEqual(seen, ["process", "inprocess"])
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            cli.main(["serve", "--adapter", "sim_quadruped", "--sandbox", "off"])


class TemplateTests(unittest.TestCase):
    """SDK templates (sdk/neuxmind/templates.py) run unchanged in the sandbox."""

    @classmethod
    def setUpClass(cls):
        sdk_dir = _support.REPO_ROOT / "sdk"
        if str(sdk_dir) not in sys.path:
            sys.path.insert(0, str(sdk_dir))
        try:
            from neuxmind import templates
        except ImportError as exc:      # pragma: no cover - SDK absent
            raise unittest.SkipTest(f"SDK 不可用：{exc}")
        cls.templates = templates

    def _package(self, template: str, skill_id: str) -> dict:
        files = self.templates.render(template, skill_id)
        package = json.loads(files.pop("skill.json"))
        package["files"] = {k: v for k, v in files.items() if k.endswith(".py") and not k.startswith("tests/")}
        return package

    def test_every_template_runs_unchanged(self):
        bodies = {"blank": "quadruped", "status": "arm", "patrol": "quadruped", "pick_place": "arm"}
        for template in self.templates.TEMPLATES:
            with self.subTest(template=template):
                fx = RuntimeFixture(bodies[template], time_scale=50.0)
                try:
                    skill_id = f"tpl-{template.replace('_', '-')}"
                    fx.install(self._package(template, skill_id))
                    record = fx.wait(fx.run(skill_id), 30)
                    self.assertEqual(record["status"], "succeeded", record["error"])
                    self.assertEqual(record["sandbox"], "process")
                finally:
                    fx.close()

    def test_template_fallback_without_sdk(self):
        """On a robot without the SDK the template import falls back to the sandbox stand-in."""
        code = ("import sys\nsys.modules['neuxmind'] = None\nsys.modules['neuxmind.skill'] = None\n"
                + self.templates._IMPORT
                + "\ndef run(ctx):\n    return {'module': Skill.__module__, 'name': Skill.__name__}\n")
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(custom_package("fallback", code))
            record = fx.wait(fx.run("fallback"))
            self.assertEqual(record["status"], "succeeded", record["error"])
            self.assertEqual(record["result"]["name"], "Skill")
        finally:
            fx.close()


IMPORT_ATTACK = '''
import gc, sys
def run(ctx, runtime_dir):
    out = {}
    for name in ("neuxmind_runtime.core", "neuxmind_runtime.adapters.sim_quadruped", "neuxmind_runtime.bus",
                 "neuxmind_runtime.safety", "neuxmind_runtime.gateway"):
        try:
            __import__(name)
            out[name] = "imported"
        except ImportError:
            out[name] = "blocked"
    # Forget the stand-ins only: the import guard still refuses the real package.
    saved = {k: sys.modules.pop(k) for k in [k for k in sys.modules if k.startswith("neuxmind_runtime")]}
    sys.path.insert(0, runtime_dir)
    try:
        import neuxmind_runtime.core
        out["without_stand_ins"] = "imported"
    except ImportError:
        out["without_stand_ins"] = "blocked"
    sys.path.remove(runtime_dir)
    sys.modules.update(saved)
    # Bypass the guard rail: drop it, forget the stand-ins, point sys.path at the source.
    sys.meta_path[:] = [f for f in sys.meta_path if type(f).__name__ != "_ImportGuard"]
    for key in [k for k in sys.modules if k.startswith("neuxmind_runtime")]:
        del sys.modules[key]
    sys.path.insert(0, runtime_dir)
    try:
        import neuxmind_runtime.core
        out["bypass"] = "imported"
    except Exception as exc:
        out["bypass"] = type(exc).__name__
    names = ("EdgeRuntime", "CapabilityBus", "SafetySupervisor", "SimQuadruped", "Runner", "SkillStore",
             "GatewayServer", "Ledger")
    out["live"] = sorted({type(o).__name__ for o in gc.get_objects() if type(o).__name__ in names})
    return out
'''

SECRETS_ATTACK = '''
import os, resource
def run(ctx, state_dir):
    out = {"env": sorted(os.environ), "token": os.environ.get("BRAIN_EDGE_TOKEN"),
           "uid": os.getuid(), "groups": os.getgroups()}
    ppid = os.getppid()
    for name in ("environ", "mem"):
        try:
            with open(f"/proc/{ppid}/{name}", "rb") as fh:
                fh.read(64)
            out[name] = "readable"
        except OSError as exc:
            out[name] = type(exc).__name__
    for key, path in (("state_dir", state_dir), ("db", os.path.join(state_dir, "runtime.sqlite3"))):
        try:
            os.listdir(path) if key == "state_dir" else open(path, "rb").read(16)
            out[key] = "accessible"
        except OSError as exc:
            out[key] = type(exc).__name__
    try:
        open("skill.py", "a").write("# tampered")
        out["pkg_write"] = "writable"
    except OSError as exc:
        out["pkg_write"] = type(exc).__name__
    with open(os.path.join(os.environ["TMPDIR"], "scratch.txt"), "w") as fh:
        out["tmp_write"] = fh.write("ok")
    out["fds"] = sorted(int(fd) for fd in os.listdir("/proc/self/fd"))
    out["session_leader"] = os.getsid(0) == os.getpid()
    with open("/proc/self/status") as fh:
        out["no_new_privs"] = [l.split()[1] for l in fh if l.startswith("NoNewPrivs:")]
    out["limits"] = {name: resource.getrlimit(getattr(resource, name))[1]
                     for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_FSIZE")}
    try:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
        out["raise_limit"] = "raised"
    except (ValueError, OSError):
        out["raise_limit"] = "refused"
    try:
        block = bytearray(3 * 1024 ** 3)
        out["alloc_3g"] = len(block)
    except MemoryError:
        out["alloc_3g"] = "MemoryError"
    return out
'''


class IsolationAttackTests(unittest.TestCase):
    def setUp(self):
        self.fx = RuntimeFixture("quadruped")

    def tearDown(self):
        self.fx.close()

    def test_runtime_internals_cannot_be_imported_or_reached(self):
        self.fx.install(custom_package("imports", IMPORT_ATTACK, parameters=[
            {"name": "runtime_dir", "type": "str", "required": True}]))
        # A copy of the runtime source the skill user can read (the checkout may sit in a private dir),
        # so a refused import proves the guard, not a permission problem.
        public = _support.public_tempdir("nx-public-src-")
        self.addCleanup(__import__("shutil").rmtree, public, True)
        os.chmod(public, 0o755)
        __import__("shutil").copytree(_support.RUNTIME_DIR / "neuxmind_runtime", Path(public) / "neuxmind_runtime",
                                      ignore=__import__("shutil").ignore_patterns("__pycache__"))
        # ... and of the spec it imports: the skill interpreter may not see the runtime's
        # site-packages (a venv, Python 3.10 with spec installed only there).
        __import__("shutil").copytree(_support.REPO_ROOT / "spec" / "neuxmind_spec", Path(public) / "neuxmind_spec",
                                      ignore=__import__("shutil").ignore_patterns("__pycache__"))
        for dirpath, _dirs, files in os.walk(public):
            os.chmod(dirpath, 0o755)
            for name in files:
                os.chmod(os.path.join(dirpath, name), 0o644)
        record = self.fx.wait(self.fx.run("imports", {"runtime_dir": public}), 15)
        self.assertEqual(record["status"], "succeeded", record["error"])
        out = record["result"]
        for name in ("neuxmind_runtime.core", "neuxmind_runtime.adapters.sim_quadruped", "neuxmind_runtime.bus",
                     "neuxmind_runtime.safety", "neuxmind_runtime.gateway"):
            self.assertEqual(out[name], "blocked", name)
        self.assertEqual(out["without_stand_ins"], "blocked")
        self.assertEqual(out["bypass"], "imported")          # the copy is readable: the guard was the reason
        # Even after bypassing the guard rail the child only has source code: no live object.
        self.assertEqual(out["live"], [], out)

    def test_no_token_no_parent_memory_no_state(self):
        with mock.patch.dict(os.environ, {"BRAIN_EDGE_TOKEN": TOKEN, "NEUXMIND_SECRET": "x" * 30}):
            self.fx.install(custom_package("secrets", SECRETS_ATTACK, parameters=[
                {"name": "state_dir", "type": "str", "required": True}]))
            record = self.fx.wait(self.fx.run("secrets", {"state_dir": str(self.fx.state_dir)}), 20)
        self.assertEqual(record["status"], "succeeded", record["error"])
        out = record["result"]
        self.assertIsNone(out["token"])
        self.assertLessEqual(set(out["env"]), MINIMAL_ENV)
        self.assertEqual((out["environ"], out["mem"]), ("PermissionError", "PermissionError"))
        self.assertEqual(out["pkg_write"], "PermissionError")
        self.assertEqual(out["tmp_write"], 2)
        self.assertLessEqual(len(out["fds"]), 5, out["fds"])        # 0,1,2, protocol socket, listdir
        self.assertTrue(out["session_leader"])                      # own session → own process group
        self.assertEqual(out["no_new_privs"], ["1"])
        self.assertEqual(out["raise_limit"], "refused")
        self.assertEqual(out["alloc_3g"], "MemoryError")
        config = self.fx.runtime.sandboxes.config
        self.assertEqual(out["limits"]["RLIMIT_AS"], config.memory_bytes)
        self.assertEqual(out["limits"]["RLIMIT_NOFILE"], config.nofile)
        self.assertEqual(out["limits"]["RLIMIT_FSIZE"], config.fsize_bytes)
        if self.fx.runtime.sandboxes.user is None:
            self.skipTest("运行时不是 root：技能与运行时同用户，状态目录访问不受限（见 README 信任边界）")
        self.assertNotEqual(out["uid"], os.getuid())
        self.assertEqual(out["groups"], [] if out["uid"] != 0 else out["groups"])
        self.assertEqual((out["state_dir"], out["db"]), ("PermissionError", "PermissionError"))
        # The installed package itself is untouched (the child had a read-only copy).
        installed = self.fx.runtime.skills.files_dir(self.fx.runtime.skills.resolve("secrets"))
        self.assertFalse((installed / "skill.py").read_text().endswith("# tampered"))


FORGERIES = {
    "reply_from_child": b'{"type":"reply","id":1,"ok":true,"value":{}}\n',
    "wrong_id": b'{"type":"request","id":42,"op":"check_stop"}\n',
    "replayed_id": b'{"type":"request","id":1,"op":"check_stop"}\n',      # id 1 was used by ctx.check_stop()
    "unknown_op": b'{"type":"request","id":2,"op":"adapter_invoke","cap":"legged.posture"}\n',
    "extra_fields": (b'{"type":"request","id":2,"op":"call","cap":"legged.posture","args":{"posture":"sit"},'
                     b'"hardware_authorized":true,"epoch":0}\n'),
    "args_not_object": b'{"type":"request","id":2,"op":"call","cap":"legged.posture","args":["sit"]}\n',
    "bool_id": b'{"type":"request","id":true,"op":"check_stop"}\n',
    "malformed_json": b'{"type": "request", \n',
    "nan": b'{"type":"request","id":2,"op":"sleep","seconds":NaN}\n',
    "nan_in_log_data": b'{"type":"request","id":2,"op":"log","message":"m","data":{"v":Infinity}}\n',
    "not_object": b'[1, 2, 3]\n',
    "deep_nesting": b"[" * 200000 + b"\n",
    "invalid_utf8": b'{"type":"\xff\xfe"}\n',
    "huge_int": b'{"type":"request","id":' + b"9" * 5000 + b',"op":"check_stop"}\n',
    "oversized_no_newline": b"__BIG__",
    "oversized_line": b'{"type":"request","id":2,"op":"log","message":"__BIG__","data":{}}\n',
    "fake_done": b'{"type":"done","ok":"yes"}\n',
    "unknown_type": b'{"type":"grant","what":"motion"}\n',
}

FORGER = '''
import os, socket, sys, time
def run(ctx, attack, payload):
    ctx.check_stop()                         # request id 1 goes through the real channel
    raw = socket.socket(fileno=os.dup(int(sys.argv[1])))
    raw.sendall(payload.replace("__BIG__", "x" * (2 << 20)).encode("latin-1"))
    time.sleep(30)                           # wait to be killed
'''


class ForgedMessageTests(unittest.TestCase):
    def test_forged_and_malformed_messages_fail_closed(self):
        fx = RuntimeFixture("quadruped", http=True)
        try:
            fx.install(custom_package("forger", FORGER, requires=("legged.posture",), parameters=[
                {"name": "attack", "type": "str", "required": True},
                {"name": "payload", "type": "str", "required": True}]))
            fx.install(example("status_report"))
            invoked = []
            original = fx.adapter.invoke
            fx.adapter.invoke = lambda cap, args: (invoked.append(cap), original(cap, args))[1]
            for attack, payload in FORGERIES.items():
                with self.subTest(attack=attack):
                    started = time.monotonic()
                    run_id = fx.run("forger", {"attack": attack, "payload": payload.decode("latin-1")})
                    record = fx.wait(run_id, 15)
                    self.assertEqual(record["status"], "failed", record)
                    self.assertIn("违反沙箱协议", record["error"])
                    self.assertLess(time.monotonic() - started, 8.0)
                    self.assertTrue(sandbox_events(fx, run_id, "sandbox.violation"))
                    pid = child_pid(fx, run_id)
                    self.assertTrue(wait_until(lambda: process_gone(pid), 5))
            self.assertEqual(invoked, [], "伪造消息到达了适配器")
            self.assertEqual(fx.adapter.posture, "stand")
            # The runtime is unaffected: normal skills still run, stop still works.
            self.assertEqual(fx.wait(fx.run("status_report"))["status"], "succeeded")
            self.assertLess(timed_http_stop(fx), STOP_BUDGET_S)
        finally:
            fx.close()


IGNORES_EVERYTHING = '''
import signal
def run(ctx):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ctx.call("base.velocity", vx=0.5, wz=0.0, timeout_s=2.0)
    while True:
        try:
            while True:
                pass
        except BaseException:
            pass
'''

FORK_BOMB = '''
import os
def run(ctx):
    ctx.log("bomb")
    while True:
        try:
            if os.fork() == 0:
                try:
                    os.setsid()              # try to leave the process group
                except OSError:
                    pass
        except OSError:
            pass
'''

FLOOD = '''
def run(ctx, n=3000):
    for i in range(n):
        ctx.log("spam", i=i)
    for i in range(200000):
        print("x" * 200)
    return {"sent": n}
'''

CRASH_DURING_ACTION = '''
import os, threading, time
def run(ctx):
    worker = threading.Thread(target=lambda: ctx.call("base.navigate_to", goal=[150.0, 0, 0, 0, 0, 0, 1]),
                              daemon=True)
    worker.start()
    time.sleep(0.5)
    os._exit(9)
'''


class HostileRuntimeTests(unittest.TestCase):
    def test_infinite_loop_ignoring_stop_is_killed_and_stop_is_fast(self):
        fx = RuntimeFixture("quadruped", time_scale=1.0, http=True)
        try:
            fx.install(custom_package("spinner", IGNORES_EVERYTHING, requires=("base.velocity",)))
            run_id = fx.run("spinner")
            wait_until(lambda: fx.adapter.cmd[0] > 0, 10)
            pid = child_pid(fx, run_id)
            self.assertLess(timed_http_stop(fx), STOP_BUDGET_S)
            self.assertEqual(fx.adapter.cmd, (0.0, 0.0, 0.0))          # robot halted immediately
            grace = fx.runtime.sandboxes.config.kill_grace_s
            started = time.monotonic()
            self.assertTrue(wait_until(lambda: process_gone(pid), 3))
            # SIGKILL comes from the monitor after kill_grace_s (not only when the run is finalised at 2 s).
            self.assertLess(time.monotonic() - started, grace + 0.6)
            record = wait_until(lambda: (r := fx.runtime.runner.get(run_id))["status"] != "running" and r, 5)
            self.assertEqual(record["status"], "stopped")
            self.assertTrue(any(e.get("killed") for e in sandbox_events(fx, run_id, "sandbox.exited")))
            self.assertEqual(http(fx.url, "/v1/status")[0], 200)
        finally:
            fx.close()

    def test_fork_bomb_is_contained(self):
        fx = RuntimeFixture("quadruped", http=True)
        try:
            user = fx.runtime.sandboxes.user
            uid = user[0] if user else os.getuid()
            before = count_tasks(uid) if user else None
            fx.install(custom_package("bomb", FORK_BOMB))
            run_id = fx.run("bomb")
            pid = child_pid(fx, run_id)
            started = sandbox_events(fx, run_id, "sandbox.started")[0]
            limits = started["limits"]
            self.assertEqual(started["pids_cgroup"], fx.runtime.sandboxes.cgroup_base is not None)
            if uid != 0:
                self.assertIn("RLIMIT_NPROC", limits)
            time.sleep(1.0)                                        # let it fork as much as it can
            if user is not None:
                bound = before + fx.runtime.sandboxes.config.extra_tasks + 1
                self.assertLessEqual(count_tasks(uid), bound)
            self.assertLess(timed_http_stop(fx), STOP_BUDGET_S)
            record = wait_until(lambda: (r := fx.runtime.runner.get(run_id))["status"] != "running" and r, 6)
            self.assertEqual(record["status"], "stopped")
            self.assertTrue(wait_until(lambda: process_gone(pid), 3))
            if user is not None and fx.runtime.sandboxes.cgroup_base is not None:
                # Including the children that called setsid(): the cgroup reached them all.
                self.assertTrue(wait_until(lambda: count_tasks(uid) <= before, 5), count_tasks(uid))
            if fx.runtime.sandboxes.cgroup_base is None:
                # No cgroup (e.g. a non-root runtime, §5.0): the reaper must still leave
                # nothing behind -- surviving fork-bomb escapees used to keep forking
                # after the test and starve the rest of the suite.
                self.assertTrue(wait_until(lambda: not escaped_skill_processes(uid), 10),
                                escaped_skill_processes(uid))
            self.assertEqual(http(fx.url, "/v1/status")[0], 200)
        finally:
            fx.close()

    def test_log_and_output_flood(self):
        config = SandboxConfig(max_requests_per_s=5000, request_burst=500, max_log_events=100)
        fx = RuntimeFixture("quadruped", http=True, sandbox=config)
        try:
            fx.install(custom_package("flood", FLOOD, parameters=[{"name": "n", "type": "int", "default": 3000}]))
            before = fx.runtime.events.last_seq
            run_id = fx.run("flood")
            wait_until(lambda: len(fx.runtime.runner.get(run_id)["events"]) > 20, 10)
            started = time.monotonic()
            status, _ = http(fx.url, "/v1/status")
            self.assertEqual(status, 200)
            self.assertLess(time.monotonic() - started, STOP_BUDGET_S)
            record = fx.wait(run_id, 30)
            self.assertEqual(record["status"], "succeeded", record["error"])
            logs = [e for e in fx.runtime.events.since(before, limit=10 ** 6) if e["type"] == "run.log"]
            self.assertEqual(len(logs), 100)                   # bounded: telemetry is not flushed out
            self.assertTrue(sandbox_events(fx, run_id, "sandbox.log_suppressed"))
            dropped = sandbox_events(fx, run_id, "sandbox.log_dropped")[0]["dropped"]
            self.assertEqual(dropped, 3000 - 100)
            output = sandbox_events(fx, run_id, "sandbox.output")[0]["tail"]
            self.assertLessEqual(len(output), 4000)
            self.assertLess(timed_http_stop(fx), STOP_BUDGET_S)
        finally:
            fx.close()

    def test_setsid_escapee_reaped_without_cgroup(self):
        """Q2-04: with no writable pids cgroup, a setsid() escapee is reaped after its run."""
        escape = ("import os, time\n"
                  "def run(ctx):\n"
                  "    pid = os.fork()\n"
                  "    if pid == 0:\n"
                  "        try:\n"
                  "            os.setsid()\n"            # leave the process group
                  "        except OSError:\n"
                  "            pass\n"
                  "        time.sleep(120)\n"
                  "        os._exit(0)\n"
                  "    ctx.log('escaped', child=pid)\n"
                  "    return {'child': pid}\n")
        with mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)):
            fx = RuntimeFixture("quadruped")
            try:
                # Privilege-dropped or (§5.0) same user as a non-root runtime: both are reaped.
                self.assertTrue(fx.runtime.sandboxes.child_subreaper)       # set when there is no cgroup
                self.assertTrue(fx.runtime.status()["sandbox"]["child_subreaper"])
                self.assertFalse(fx.runtime.status()["sandbox"]["pids_cgroup"])
                self.assertEqual(fx.runtime.status()["sandbox"]["process_containment"], "best-effort")
                fx.install(custom_package("escape", escape))
                record = fx.wait(fx.run("escape"), 10)
                self.assertEqual(record["status"], "succeeded", record["error"])
                child = record["result"]["child"]
                # The escapee reparents to the runtime when its parent exits, then is reaped.
                self.assertTrue(wait_until(lambda: process_gone(child), 6),
                                "脱离进程组的后代在运行结束后仍存活")
            finally:
                fx.close()

    def test_crash_voids_in_flight_request(self):
        fx = RuntimeFixture("quadruped", time_scale=1.0)
        try:
            fx.install(custom_package("crasher", CRASH_DURING_ACTION, requires=("base.navigate_to",)))
            run_id = fx.run("crasher")
            wait_until(lambda: fx.adapter.goal is not None, 10)     # the navigation is being served
            record = fx.wait(run_id, 5)
            self.assertEqual(record["status"], "failed")
            self.assertIn("技能进程", record["error"])
            self.assertIsNone(fx.adapter.goal)                       # cancelled when the child died
            self.assertEqual(fx.adapter.cmd, (0.0, 0.0, 0.0))
            self.assertFalse(fx.runtime.supervisor.latched)
        finally:
            fx.close()

    def test_child_that_dies_at_start(self):
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(custom_package("suicide", "import os\nos._exit(7)\ndef run(ctx):\n    pass\n"))
            record = fx.wait(fx.run("suicide"), 10)
            self.assertEqual(record["status"], "failed")
            self.assertIn("退出码 7", record["error"])
        finally:
            fx.close()

    def test_robot_is_halted_before_the_child_is_notified(self):
        fx = RuntimeFixture("quadruped", time_scale=1.0)
        try:
            fx.install(custom_package("spinner", IGNORES_EVERYTHING, requires=("base.velocity",)))
            run_id = fx.run("spinner")
            wait_until(lambda: fx.adapter.cmd[0] > 0, 10)
            order = []
            original_stop = fx.adapter.stop
            fx.adapter.stop = lambda: (order.append("adapter.stop"), original_stop())[1]
            host = fx.runtime.runner.live_run(run_id).sandbox
            original_notify = host.notify_stop
            host.notify_stop = lambda: (order.append("notify"), original_notify())[1]
            fx.runtime.emergency_stop("ordering")
            self.assertEqual(order[:2], ["adapter.stop", "notify"])
            fx.runtime.runner.wait(run_id, 5)
            order.clear()
            fx.runtime.reset({"hardware_authorized": True})
            run_id = fx.run("spinner")
            wait_until(lambda: fx.adapter.cmd[0] > 0, 10)
            host = fx.runtime.runner.live_run(run_id).sandbox
            original_notify = host.notify_stop
            cmd_at_notify = []
            host.notify_stop = lambda: (cmd_at_notify.append(fx.adapter.cmd), original_notify())[1]
            fx.runtime.runner.cancel(run_id)                        # cancel: halt first, then notify
            self.assertEqual(cmd_at_notify, [(0.0, 0.0, 0.0)])
            fx.runtime.runner.wait(run_id, 5)
        finally:
            fx.close()

    def test_stop_during_child_start_up(self):
        """A stop while the interpreter boots: SIGTERM is deferred, the child is still reaped."""
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(example("status_report"))
            with mock.patch.object(sandbox_module, "POLL_S", 0.05):
                run_id = fx.run("status_report")
                fx.runtime.emergency_stop("immediately")
                record = wait_until(lambda: (r := fx.runtime.runner.get(run_id))["status"] != "running" and r, 6)
            self.assertIn(record["status"], ("stopped", "succeeded"))
            pid = child_pid(fx, run_id)
            self.assertTrue(wait_until(lambda: process_gone(pid), 3))
            exited = wait_until(lambda: sandbox_events(fx, run_id, "sandbox.exited"), 3)[0]
            # Not killed by SIGTERM's default action while the interpreter was booting.
            self.assertNotEqual(exited["returncode"], -signal.SIGTERM)
        finally:
            fx.close()


STOP_CHECKER = '''
def run(ctx):
    while True:
        try:
            ctx.check_stop()
        except BaseException:
            pass
'''

PARENT_DEATH = '''
import sys, time
sys.path[:0] = [{runtime!r}, {tests!r}]
import _support
from _support import RuntimeFixture, custom_package
fx = RuntimeFixture("quadruped")
fx.install(custom_package("sleepy", "import time\\ndef run(ctx):\\n    time.sleep(600)\\n"))
run_id = fx.run("sleepy")
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    started = [e for e in fx.runtime.events.since(0) if e["type"] == "sandbox.started"]
    if started:
        print(started[0]["pid"], flush=True)
        break
    time.sleep(0.01)
time.sleep(600)
'''


class NotificationTests(unittest.TestCase):
    def test_sigterm_makes_ctx_raise_locally(self):
        """After the stop notification a looping skill stops sending requests (local SkillStopped)."""
        fx = RuntimeFixture("quadruped")
        try:
            fx.install(custom_package("checker", STOP_CHECKER))
            run_id = fx.run("checker")
            run = fx.runtime.runner.live_run(run_id)
            wait_until(lambda: run.sandbox is not None and run.sandbox.requests_served > 50, 10)
            host = run.sandbox
            fx.runtime.emergency_stop("notify")
            time.sleep(0.2)
            served = host.requests_served
            time.sleep(0.3)                                    # still before the 1 s SIGKILL
            self.assertEqual(host.requests_served, served, "通知后技能仍在向运行时发请求")
            self.assertFalse(process_gone(host.pid))
            fx.wait(run_id, 5)
        finally:
            fx.close()

    def test_requests_are_throttled(self):
        config = SandboxConfig(max_requests_per_s=200, request_burst=10)
        fx = RuntimeFixture("quadruped", sandbox=config)
        try:
            fx.install(custom_package("chatty", "def run(ctx):\n    for _ in range(110):\n        ctx.check_stop()\n"))
            run_id = fx.run("chatty")
            started = wait_until(lambda: sandbox_events(fx, run_id, "sandbox.started"), 10)[0]["ts"]
            record = fx.wait(run_id, 10)
            self.assertEqual(record["status"], "succeeded", record["error"])
            ended = sandbox_events(fx, run_id, "sandbox.exited")[0]["ts"]
            self.assertGreaterEqual(ended - started, 0.45)    # (110 - 10 burst) / 200 per second
        finally:
            fx.close()

    def test_skill_process_dies_with_the_runtime(self):
        """PR_SET_PDEATHSIG: a runtime killed with SIGKILL leaves no skill process behind."""
        script = PARENT_DEATH.format(runtime=str(_support.RUNTIME_DIR), tests=str(Path(__file__).parent))
        proc = __import__("subprocess").Popen([sys.executable, "-c", script], stdout=-1, text=True)
        try:
            line = _support.readline_within(proc.stdout, 30)
            self.assertTrue(line.strip().isdigit(), line)
            pid = int(line)
            self.assertFalse(process_gone(pid))
        finally:
            proc.kill()
            proc.wait()
        self.assertTrue(wait_until(lambda: process_gone(pid), 5))


class LayeredDefenceTests(unittest.TestCase):
    """Defences that other layers normally mask; each is checked on its own."""

    def test_runtime_marks_itself_non_dumpable(self):
        fx = RuntimeFixture("quadruped")
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            self.assertEqual(libc.prctl(3, 0, 0, 0, 0), 0)          # PR_GET_DUMPABLE
            self.assertTrue(fx.runtime.status()["sandbox"]["runtime_non_dumpable"])
        finally:
            fx.close()

    def test_guard_is_checked_before_dispatch(self):
        """A request of an exited child is void: the context guard runs before the adapter is reached."""
        from neuxmind_runtime import runner as runner_module
        from neuxmind_runtime.skills import SkillContext, SkillStopped
        fx = RuntimeFixture("quadruped")
        try:
            run = runner_module.RunState("skill", "x", True, True, {})
            run.epoch = fx.runtime.supervisor.epoch
            ctx = SkillContext(run, fx.runtime.bus, {"legged.posture"}, {}, fx.runtime.emergency_stop)

            def dead():
                raise SkillStopped("技能进程已退出；其在途请求作废")
            ctx.add_guard(dead)
            for call in (lambda: ctx.call("legged.posture", posture="sit"), ctx.check_stop,
                         lambda: ctx.read("system.health")):
                with self.assertRaises(SkillStopped):
                    call()
            self.assertTrue(ctx.stopped)
            self.assertEqual(fx.adapter.posture, "stand")
        finally:
            fx.close()

    def test_sigterm_is_deferred_until_handshake(self):
        host = sandbox_module.SandboxHost.__new__(sandbox_module.SandboxHost)
        host._ready, host._term_pending, host._reaped = False, False, False
        host._signal_lock = sandbox_module.threading.Lock()
        host._proc = mock.Mock(pid=424242)
        with mock.patch.object(sandbox_module.os, "kill") as kill:
            host.notify_stop()
            kill.assert_not_called()
            self.assertTrue(host._term_pending)
            host._ready = True
            host.notify_stop()
            kill.assert_called_once_with(424242, signal.SIGTERM)

    def test_finalised_run_kills_its_process_without_waiting_for_grace(self):
        from neuxmind_runtime import runner as runner_module
        fx = RuntimeFixture("quadruped", sandbox=SandboxConfig(kill_grace_s=30.0))
        try:
            fx.install(custom_package("stubborn", "import time\ndef run(ctx):\n    while True:\n        try:\n"
                                      "            time.sleep(0.01)\n        except BaseException:\n            pass\n"))
            with mock.patch.object(runner_module, "STOP_GRACE_S", 0.2):
                run_id = fx.run("stubborn")
                pid = child_pid(fx, run_id)
                fx.runtime.runner.cancel(run_id)
                self.assertTrue(wait_until(lambda: process_gone(pid), 3))   # long before the 30 s grace
        finally:
            fx.close()


MULTI_STEP_FLOOD = "def run(ctx, n=400):\n    for i in range(n):\n        ctx.log('spam', i=i)\n    return {'sent': n}\n"


class PerRunLogBudgetTests(unittest.TestCase):
    """Q2-08: the log budget is per run, so a multi-step application cannot flush telemetry."""

    def test_budget_is_shared_across_application_steps(self):
        from test_v1_compat import application, step
        fx = RuntimeFixture("quadruped", sandbox=SandboxConfig(max_log_events=100))
        try:
            fx.install(custom_package("mflood", MULTI_STEP_FLOOD,
                                      parameters=[{"name": "n", "type": "int", "default": 400}]))
            app = application([step("mflood"), step("mflood"), step("mflood")], name="multi")
            before = fx.runtime.events.last_seq
            deployed = fx.runtime.v1_deploy({"request_id": "d", "application": app, "report_id": "r"})
            started = fx.runtime.v1_start({"request_id": "s", "release_id": deployed["release_id"],
                                           "hardware_authorized": True})
            record = fx.wait(started["run_id"], 30)
            self.assertEqual(record["status"], "succeeded", record["error"])
            run = fx.runtime.runner.get(started["run_id"])
            self.assertEqual([s["status"] for s in record["result"]["steps"]],
                             ["succeeded", "succeeded", "succeeded"])
            logs = [e for e in fx.runtime.events.since(before, limit=10 ** 6) if e["type"] == "run.log"]
            # 3 steps x 400 logs each, but the whole run keeps at most max_log_events.
            self.assertEqual(len(logs), 100, "日志未按运行累计封顶，技能可用多步应用挤掉安全事件")
        finally:
            fx.close()


class CgroupSweepTests(unittest.TestCase):
    def test_second_runtime_does_not_remove_first_runtimes_cgroup(self):
        first = RuntimeFixture("quadruped")
        try:
            own = first.runtime.sandboxes.cgroup_base
            if own is None:
                self.skipTest("无可写的 pids cgroup")
            dead = own.parent / "rt-999999999-deadbeef"
            dead.mkdir()
            second = RuntimeFixture("quadruped")
            second.close()
            self.assertTrue(own.is_dir(), "另一个运行时启动时删掉了仍在使用的 cgroup 目录")
            self.assertFalse(dead.exists())                 # a dead runtime's leftover is swept
        finally:
            first.close()


class BestEffortStatusTests(unittest.TestCase):
    """§5.0: /v1/status says whether the sandbox works fully or best-effort, and why."""

    def test_status_reports_the_isolation_level(self):
        fx = RuntimeFixture("quadruped")
        try:
            box = fx.runtime.status()["sandbox"]
            self.assertEqual(box["privilege_drop"], fx.runtime.sandboxes.user is not None)
            self.assertEqual(box["level"], "best-effort" if box["best_effort_reasons"] else "full")
            self.assertTrue(box["python_usable"])
            if os.geteuid() != 0:
                self.assertEqual(box["level"], "best-effort")
                self.assertFalse(box["privilege_drop"])
                self.assertTrue(any("不是 root" in r for r in box["best_effort_reasons"]))
            if not box["pids_cgroup"]:
                self.assertTrue(any("cgroup" in r for r in box["best_effort_reasons"]))
        finally:
            fx.close()

    def test_unusable_interpreter_is_best_effort_and_named(self):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(sandbox_module, "python_usable", lambda python, user: False):
            boxes = sandbox_module.Sandboxes(SandboxConfig(), state)
            try:
                box = boxes.describe()
                self.assertFalse(box["python_usable"])
                self.assertEqual(box["level"], "best-effort")
            finally:
                boxes.close()


class CleanupLayerTests(unittest.TestCase):
    """Each Q2-01 / Q2-04 defence on its own (the layers otherwise mask each other)."""

    def test_remove_tree_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as victim:
            (Path(victim) / "d").mkdir()
            os.chmod(Path(victim) / "d", 0o755)
            (Path(victim) / "d" / "f").write_text("x")
            link = Path(tmp) / "top-link"
            link.symlink_to(victim)
            sandbox_module._remove_tree(link)                         # top level is a symlink
            self.assertFalse(os.path.lexists(link))
            real = Path(tmp) / "real"
            (real / "pkg").mkdir(parents=True)
            (real / "pkg" / "inner-link").symlink_to(victim)           # nested symlink inside a real tree
            os.chmod(real / "pkg", 0o555)
            sandbox_module._remove_tree(real)
            self.assertFalse(real.exists())
            self.assertEqual((Path(victim) / "d").stat().st_mode & 0o777, 0o755)
            self.assertEqual((Path(victim) / "d" / "f").read_text(), "x")
            self.assertEqual(Path(victim).stat().st_mode & 0o777, 0o700)

    def test_sweep_skips_symlinks_and_foreign_directories(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as victim:
            link = Path(base) / "rt-999999999-link"
            link.symlink_to(victim)
            foreign = Path(base) / "rt-999999998-foreign"
            foreign.mkdir()
            mine = Path(base) / "rt-999999997-mine"
            mine.mkdir()
            # Owner check on its own: as another uid, every entry here is foreign.
            sandbox_module._sweep_dead_workspaces(Path(base), os.geteuid() + 4242)
            self.assertTrue(foreign.is_dir() and mine.is_dir(), "清理删了不属于该用户的目录")
            if os.geteuid() == 0:
                os.chown(foreign, 65534, 65534)                        # not ours (only root can chown)
            else:
                foreign.rmdir()                                        # non-root: covered by the call above
            sandbox_module._sweep_dead_workspaces(Path(base), os.geteuid())
            self.assertTrue(os.path.islink(link), "清理动了符号链接条目")
            if os.geteuid() == 0:
                self.assertTrue(foreign.is_dir(), "清理删了不属于本用户的目录")
            self.assertFalse(mine.exists())                            # our dead workspace is removed

    def test_reaper_only_kills_untracked_orphans_reparented_to_us(self):
        config = SandboxConfig()
        with mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)), \
                tempfile.TemporaryDirectory() as state:
            boxes = sandbox_module.Sandboxes(config, state)
            try:
                boxes.user = (65534, 65534, "nobody")
                boxes.child_subreaper = True
                me = os.getpid()
                table = {101: (65534, me), 102: (65534, 4242), 103: (65534, me), 104: (0, me)}
                boxes.register_child(103)                              # a tracked, live child
                with mock.patch.object(sandbox_module.os, "listdir", return_value=[str(p) for p in table]), \
                        mock.patch.object(sandbox_module, "_SUBREAPER", True), \
                        mock.patch.object(sandbox_module, "_process_real_uid", lambda pid: table[pid][0]), \
                        mock.patch.object(sandbox_module, "_process_ppid", lambda pid: table[pid][1]), \
                        mock.patch.object(sandbox_module.os, "waitpid", return_value=(1, 0)) as waitpid, \
                        mock.patch.object(sandbox_module.os, "kill") as kill:
                    killed = boxes.reap_orphans()
                self.assertEqual(killed, [101])          # not 102 (other parent), 103 (tracked), 104 (root)
                # Stopped first (cannot fork any more), then killed; nobody else is signalled.
                self.assertEqual([c.args for c in kill.call_args_list], [(101, signal.SIGSTOP), (101, signal.SIGKILL)])
                # The killed orphan is reaped by explicit pid; the tracked child (103) never is.
                reaped = {c.args[0] for c in waitpid.call_args_list}
                self.assertIn(101, reaped)
                self.assertNotIn(103, reaped)
                self.assertNotIn(-1, reaped)
            finally:
                boxes.unregister_child(103)
                boxes.user = None
                boxes.child_subreaper = False
                boxes.close()


class SameUserReaperTests(unittest.TestCase):
    """§5.0 non-root: the reaper also works when skills run as the runtime's own user."""

    def _boxes(self, state):
        boxes = sandbox_module.Sandboxes(SandboxConfig(), state)
        boxes.user = None                          # same user as the runtime
        boxes.child_subreaper = True
        boxes.cgroup_base = None
        return boxes

    def test_only_processes_outside_the_runtime_session_are_skill_processes(self):
        me, uid, mine = os.getpid(), os.getuid(), os.getsid(0)
        # 301: escaped skill process (own session); 302: a helper in the runtime's session;
        # 303: grandchild of 301 (not reparented yet) -- stopped and killed in the same sweep.
        table = {301: (uid, me, (301, 301)), 302: (uid, me, (mine, mine)), 303: (uid, 301, (301, 301)),
                 304: (uid, me, (304, 304))}       # 304: a helper started in a new session, untagged
        tagged = {301, 303}
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)):
            boxes = self._boxes(state)
            try:
                with mock.patch.object(sandbox_module.os, "listdir", return_value=[str(p) for p in table]), \
                        mock.patch.object(sandbox_module, "_SUBREAPER", True), \
                        mock.patch.object(sandbox_module, "_process_real_uid", lambda pid: table[pid][0]), \
                        mock.patch.object(sandbox_module, "_process_ppid", lambda pid: table[pid][1]), \
                        mock.patch.object(sandbox_module, "_process_group_and_session",
                                          lambda pid: table[pid][2]), \
                        mock.patch.object(sandbox_module, "_process_has_skill_tag", lambda pid: pid in tagged), \
                        mock.patch.object(sandbox_module.os, "waitpid", return_value=(1, 0)) as waitpid, \
                        mock.patch.object(sandbox_module.os, "kill") as kill:
                    killed = boxes.reap_orphans()
                self.assertEqual(sorted(killed), [301, 303])
                signalled = {c.args[0] for c in kill.call_args_list}
                self.assertNotIn(302, signalled, "收割者杀了运行时会话中的进程")
                self.assertNotIn(304, signalled, "收割者杀了运行时以新会话启动的辅助进程（Q3-05）")
                stops = [c.args[0] for c in kill.call_args_list if c.args[1] == signal.SIGSTOP]
                kills = [c.args[0] for c in kill.call_args_list if c.args[1] == signal.SIGKILL]
                self.assertEqual(sorted(stops), [301, 303])
                # Every target is stopped before any is killed (a fork bomb cannot outrun the sweep).
                order = [c.args[1] for c in kill.call_args_list]
                self.assertEqual(order, [signal.SIGSTOP] * 2 + [signal.SIGKILL] * 2)
                self.assertEqual(sorted(kills), [301, 303])
                self.assertEqual({c.args[0] for c in waitpid.call_args_list}, {301})    # only our child
            finally:
                boxes.child_subreaper = False
                boxes.close()

    def test_tracked_children_of_another_runtime_in_this_process_are_safe(self):
        me, uid = os.getpid(), os.getuid()
        table = {401: (uid, me, (401, 401))}
        with tempfile.TemporaryDirectory() as a_state, tempfile.TemporaryDirectory() as b_state, \
                mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)):
            a, b = self._boxes(a_state), self._boxes(b_state)
            a.register_child(401)                  # runtime A's live skill process
            try:
                with mock.patch.object(sandbox_module.os, "listdir", return_value=["401"]), \
                        mock.patch.object(sandbox_module, "_SUBREAPER", True), \
                        mock.patch.object(sandbox_module, "_process_real_uid", lambda pid: table[pid][0]), \
                        mock.patch.object(sandbox_module, "_process_ppid", lambda pid: table[pid][1]), \
                        mock.patch.object(sandbox_module, "_process_group_and_session",
                                          lambda pid: table[pid][2]), \
                        mock.patch.object(sandbox_module.os, "kill") as kill:
                    self.assertEqual(b.reap_orphans(), [])
                kill.assert_not_called()
            finally:
                a.unregister_child(401)
                for boxes in (a, b):
                    boxes.child_subreaper = False
                    boxes.close()

    def test_periodic_sweep_runs_only_for_a_while_after_a_skill_exit(self):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)):
            boxes = self._boxes(state)
            try:
                with mock.patch.object(sandbox_module, "_SUBREAPER", True), \
                        mock.patch.object(boxes, "reap_orphans", return_value=[]) as reap:
                    boxes.periodic_reap()
                    reap.assert_not_called()                     # no skill has run yet
                    boxes.note_child_exit()
                    boxes.periodic_reap()
                    boxes.periodic_reap()                        # rate limited
                    self.assertEqual(reap.call_count, 1)
                    boxes._next_reap = 0.0
                    boxes._last_exit = time.monotonic() - sandbox_module.REAP_WINDOW_S - 1
                    boxes.periodic_reap()
                    self.assertEqual(reap.call_count, 1)         # window over
            finally:
                boxes.child_subreaper = False
                boxes.close()


class HelperProcessTests(unittest.TestCase):
    """Q3-05 with real processes: helpers of the runtime/adapter survive skill runs and keep
    their own exit status, in a new session or not; escaped skill processes are still reaped."""

    def test_runtime_helpers_are_never_reaped(self):
        import subprocess
        helper_new = subprocess.Popen(["sleep", "60"], start_new_session=True)
        helper_same = subprocess.Popen(["sleep", "60"])
        try:
            with mock.patch.object(sandbox_module.PidsCgroup, "base", staticmethod(lambda: None)):
                fx = RuntimeFixture("quadruped")
                try:
                    self.assertTrue(fx.runtime.sandboxes.child_subreaper)
                    escape = ("import os, time\n"
                              "def run(ctx):\n"
                              "    pid = os.fork()\n"
                              "    if pid == 0:\n"
                              "        os.setsid()\n"
                              "        time.sleep(120)\n"
                              "        os._exit(0)\n"
                              "    return {'child': pid}\n")
                    fx.install(custom_package("escape2", escape))
                    record = fx.wait(fx.run("escape2"), 15)
                    self.assertEqual(record["status"], "succeeded", record["error"])
                    child = record["result"]["child"]
                    self.assertTrue(wait_until(lambda: process_gone(child), 8), "逃逸的技能进程未被收割")
                    time.sleep(1.2)                                   # a few periodic sweeps
                    fx.runtime.sandboxes.reap_orphans(settle=True)
                finally:
                    fx.close()                                        # close() sweeps once more
            self.assertIsNone(helper_new.poll(), "运行时以新会话启动的辅助进程被收割者杀掉")
            self.assertIsNone(helper_same.poll())
        finally:
            for helper in (helper_new, helper_same):
                helper.terminate()
        # Their exit status was not taken by the reaper: the owner sees the real signal.
        self.assertEqual(helper_new.wait(5), -signal.SIGTERM)
        self.assertEqual(helper_same.wait(5), -signal.SIGTERM)


class WorkspaceTests(unittest.TestCase):
    def test_sandbox_base_is_runtime_owned_and_not_world_writable(self):
        """Q2-01: the base is in the runtime's own tree, never world-writable, never a symlink."""
        with tempfile.TemporaryDirectory() as state:
            fx = RuntimeFixture("quadruped", state_dir=state)
            try:
                base = fx.runtime.sandboxes.root.parent
                st = os.lstat(base)
                self.assertFalse(stat.S_ISLNK(st.st_mode))
                self.assertFalse(st.st_mode & 0o022, oct(st.st_mode & 0o777))   # not group/other writable
                self.assertEqual(st.st_uid, os.geteuid())                       # runtime-owned
                if fx.runtime.sandboxes.user is None:
                    # Same user as the runtime: inside its own private state dir.
                    self.assertEqual(base, Path(state) / "sandbox")
                else:
                    # Privilege-dropped: a runtime-owned shared base the skill user can
                    # only traverse (it cannot create entries, so it cannot plant symlinks).
                    self.assertEqual(base.name, f"neuxmind-sandbox-{os.geteuid()}")
            finally:
                fx.close()

    def test_dead_runtimes_workspaces_are_swept(self):
        with tempfile.TemporaryDirectory() as parent:
            dead = Path(parent) / "rt-999999999-abc"
            (dead / "run-x" / "pkg").mkdir(parents=True)
            os.chmod(dead / "run-x" / "pkg", 0o555)              # read-only package copy
            alive = Path(parent) / f"rt-{os.getpid()}-def"
            alive.mkdir()
            fx = RuntimeFixture("quadruped", sandbox=SandboxConfig(workspace_root=parent))
            try:
                self.assertFalse(dead.exists())                 # dead runtime's leftover removed
                self.assertTrue(alive.exists())                 # a live runtime's directory is kept
                self.assertEqual(fx.runtime.sandboxes.root.parent, Path(parent))
            finally:
                fx.close()

    def test_sweep_does_not_follow_a_planted_symlink(self):
        """Q2-01: a symlink named like a dead workspace must not redirect the sweep's chmod/rmtree."""
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as victim:
            inner = Path(victim) / "keep"
            inner.mkdir()
            (inner / "file").write_text("important")
            os.chmod(inner, 0o755)
            before = inner.stat().st_mode & 0o777
            # An attacker who could write the base plants a symlink whose PID is dead.
            link = Path(parent) / "rt-999999999-evil"
            link.symlink_to(victim)
            fx = RuntimeFixture("quadruped", sandbox=SandboxConfig(workspace_root=parent))
            try:
                self.assertFalse(link.is_symlink() and link.exists() and Path(link).resolve() == Path(victim)
                                 and not (inner / "file").exists())
                self.assertTrue((inner / "file").exists(), "victim content was deleted through the symlink")
                self.assertEqual(inner.stat().st_mode & 0o777, before, "victim perms changed through the symlink")
                self.assertEqual((inner / "file").read_text(), "important")
            finally:
                fx.close()

    def test_sweep_leaves_a_nested_symlink_target_untouched(self):
        """The review's attack: a planted ``rt-<deadpid>`` symlink must not get its target's tree chmod'd."""
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as victim:
            sub = Path(victim) / "a" / "b"
            sub.mkdir(parents=True)
            for d in (Path(victim) / "a", sub):
                os.chmod(d, 0o755)
            (sub / "file").write_text("important")
            (Path(parent) / "rt-999999999-evil").symlink_to(victim)
            modes_before = {p: p.stat().st_mode & 0o777 for p in (Path(victim) / "a", sub)}
            fx = RuntimeFixture("quadruped", sandbox=SandboxConfig(workspace_root=parent))   # sweep runs
            try:
                self.assertTrue((sub / "file").exists())
                self.assertEqual((sub / "file").read_text(), "important")
                for p, mode in modes_before.items():
                    self.assertEqual(p.stat().st_mode & 0o777, mode, f"{p} 的权限被符号链接清理改掉了")
            finally:
                fx.close()

    def test_workspace_and_cgroup_are_cleaned_up(self):
        fx = RuntimeFixture("quadruped")
        root = fx.runtime.sandboxes.root
        try:
            fx.install(example("status_report"))
            self.assertEqual(fx.wait(fx.run("status_report"))["status"], "succeeded")
            self.assertEqual(oct(root.stat().st_mode & 0o777), "0o711")
            wait_until(lambda: list(root.iterdir()) == [], 5)
            base = fx.runtime.sandboxes.cgroup_base
            if base is not None:
                wait_until(lambda: not [p for p in base.iterdir() if p.is_dir()], 5)
        finally:
            fx.close()
        self.assertFalse(root.exists())
        if base is not None:
            self.assertFalse(base.exists())


if __name__ == "__main__":
    unittest.main()
