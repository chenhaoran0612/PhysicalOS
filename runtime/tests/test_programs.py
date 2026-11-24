"""Issue #38: on-body programs run as actions (``neuxmind_runtime.adapters.programs``).

The "programs" here are small stand-in scripts written into a temporary directory: they print, wait,
exit with a code, ignore signals. Nothing here is a robot's navigation; what is tested is the process
handling around one (success rules, order, retry, time-out, cancel, stop, the speed bound, regions).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _support  # noqa: F401

from neuxmind_runtime.adapters import programs
from neuxmind_runtime.adapters.programs import ProgramRefused, ProgramRunner, normalize_config

PY = sys.executable

SCRIPT = r'''
import json, os, signal, sys, time
args = sys.argv[1:]
name = args[0]
with open("calls.jsonl", "a") as fh:
    fh.write(json.dumps({"name": name, "args": args[1:], "cwd": os.getcwd(), "pid": os.getpid(),
                         "probe": os.environ.get("NX_PROBE")}) + "\n")
if "--ignore-term" in args:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if "--term-clean" in args:
    def bye(*_):
        print("stopped cleanly", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
if "--child" in args:
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open("child.pid", "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    while not os.path.exists("child.pid"):
        time.sleep(0.01)
if "--count-term" in args:                # a controlled stop that takes a while; counts every SIGTERM
    seen = []
    def slow_stop(*_):
        seen.append(1)
        with open("term-count", "w") as fh:
            fh.write(str(len(seen)))
    signal.signal(signal.SIGTERM, slow_stop)
    print("walking", flush=True)
    while not seen:
        time.sleep(0.02)
    time.sleep(0.8)
    with open("term-count", "w") as fh:
        fh.write(str(len(seen)))
    sys.exit(0)
if "--helper" in args:                    # leaves a helper in another session that keeps our output open
    if os.fork() == 0:
        os.setsid()
        with open("helper.pid", "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(float(args[args.index("--helper") + 1]))
        os._exit(0)
    while not os.path.exists("helper.pid"):
        time.sleep(0.01)
if "--on-term-touch" in args:
    def touched(*_):
        open(args[args.index("--on-term-touch") + 1], "w").close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, touched)
if "--noise" in args:
    for _ in range(4000):
        print("x" * 100)
if "--sleep" in args:
    print("walking", flush=True)
    time.sleep(float(args[args.index("--sleep") + 1]))
if "--once" in args:                      # fail the first time with a retryable message
    marker = "once-" + name
    if not os.path.exists(marker):
        open(marker, "w").close()
        print("sensors not ready", flush=True)
        sys.exit(0)
if "--say" in args:
    print(args[args.index("--say") + 1], flush=True)
sys.exit(int(args[args.index("--exit") + 1]) if "--exit" in args else 0)
'''


def step(name, *args, **fields):
    """A step that runs the stand-in script; succeeds on the text "route done" unless told otherwise."""
    out = {"title": name, "argv": [PY, "prog.py", name, *args], "timeout_s": 20}
    if "exit_code_only" not in fields and "ok" not in fields:
        out["ok"] = "route done"
    out.update(fields)
    return out


def done(name, *args, **fields):
    return step(name, "--say", "exit route done", *args, **fields)


class _Dir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(os.path.realpath(self._tmp.name))
        (self.dir / "prog.py").write_text(SCRIPT)
        self.halts = []
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close(0.1)
        for name in ("child.pid", "helper.pid"):
            left = self.dir / name
            if left.exists():
                try:
                    os.kill(int(left.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

    def raw(self, **sections) -> dict:
        config = {"workdir": str(self.dir), "stop_grace_s": 0.3}
        config.update(sections)
        return config

    def runner(self, **sections) -> ProgramRunner:
        runner = ProgramRunner(normalize_config(self.raw(**sections)), on_halt=lambda: self.halts.append(time.monotonic()))
        self.runners.append(runner)
        return runner

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls()]

    def finish(self, handle, timeout=15.0):
        self.assertTrue(handle.done.wait(timeout), "the action did not end in time")
        return handle.poll()

    def wait_started(self, name, timeout=5.0):
        _support.wait_until(lambda: name in self.names(), timeout)


class Config(_Dir):
    def ok(self, **sections):
        return normalize_config(self.raw(**sections))

    def bad(self, needle, **sections):
        with self.assertRaises(ValueError) as caught:
            normalize_config(self.raw(**sections))
        self.assertIn(needle, str(caught.exception))

    def test_minimal(self):
        config = self.ok(places={"会议室": {"steps": [done("a")]}})
        self.assertEqual(list(config["places"]), ["会议室"])
        self.assertFalse(config["regional"])
        self.assertEqual(programs.manifest_capabilities(config),
                         [{"id": "base.navigate_to_place", "params": {"places": ["会议室"]}}])

    def test_explore_only(self):
        config = self.ok(explore={
            "max_duration_s": 600, "max_radius": 10, "default_radius": 5,
            "steps": [{"argv": [PY, "prog.py", "w", "--seconds", "{duration_s}", "--leash", "{radius}"],
                       "ok": "done", "timeout_margin_s": 30}]})
        self.assertEqual(programs.manifest_capabilities(config),
                         [{"id": "base.explore", "params": {"max_duration_s": 600.0, "max_radius": 10.0}}])

    def test_the_example_in_the_repository_is_valid(self):
        from neuxmind_spec import manifest as spec_manifest
        from neuxmind_runtime.adapters import ros2
        path = Path(_support.RUNTIME_DIR) / "examples" / "ros2" / "onbody-programs.example.json"
        config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
        for key, value in ros2.parse_config_text(path.read_text(encoding="utf-8")).items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
        manifest = spec_manifest.validate_manifest_v2(ros2.build_manifest(config))
        declared = {c["id"]: c["params"] for c in manifest["capabilities"]}
        self.assertEqual(declared["base.navigate_to_place"]["places"], ["出发点", "会议室", "二楼"])
        self.assertEqual(declared["base.explore"], {"max_duration_s": 600.0, "max_radius": 10.0})
        normalized = normalize_config(config["programs"])
        self.assertEqual(normalized["regions"], ["一楼", "二楼"])
        # every step with a fixed speed fits under the limits the example itself declares
        fixed = [s["speed"] for place in normalized["places"].values() for steps in place["routes"].values()
                 for s in steps if s["speed"] is not None]
        self.assertLessEqual(max(fixed), config["limits"]["max_vx"])

    def test_nothing_configured(self):
        self.bad("既没有 places 也没有 explore")

    def test_unknown_keys(self):
        self.bad("不认识的项", places={"a": {"steps": [done("a")]}}, shell=True)
        self.bad("不认识的项", places={"a": {"steps": [done("a", shell=True)]}})
        self.bad("不认识的项", places={"a": {"steps": [done("a")], "colour": "red"}})

    def test_stop_grace_stays_below_what_the_device_gives_the_runtime_to_stop(self):
        self.ok(places={"a": {"steps": [done("a")]}}, stop_grace_s=8)
        self.bad("stop_grace_s", places={"a": {"steps": [done("a")]}}, stop_grace_s=8.5)

    def test_workdir_must_be_absolute(self):
        with self.assertRaises(ValueError):
            normalize_config({"workdir": "walk", "places": {"a": {"steps": [done("a")]}}})

    def test_success_rule_is_required(self):
        s = done("a")
        del s["ok"]
        self.bad("怎样算成功", places={"a": {"steps": [s]}})
        self.bad("怎样算成功", places={"a": {"steps": [done("a", ok="route done", exit_code_only=True)]}})
        self.ok(places={"a": {"steps": [step("a", exit_code_only=True)]}})

    def test_bad_pattern(self):
        self.bad("正则", places={"a": {"steps": [step("a", ok="(")]}})

    def test_argv(self):
        for argv in ([], "python3 x.py", ["python3", ""], ["python3", 3], ["a"] * 65, ["a\x00b"]):
            with self.subTest(argv=argv):
                self.bad("argv", places={"a": {"steps": [{"argv": argv, "ok": "x", "timeout_s": 5}]}})

    def test_unknown_token(self):
        self.bad("占位符", places={"a": {"steps": [done("a", "{place}")]}})
        self.bad("占位符", places={"a": {"steps": [done("a", "{duration_s}")]}})     # not a place token
        self.bad("占位符", places={"a": {"steps": [done("a", "{}")]}})

    def test_timeout_is_required_and_bounded(self):
        for value in (None, 0, -1, 3601, "30", True, float("inf")):
            with self.subTest(value=value):
                s = done("a")
                if value is None:
                    del s["timeout_s"]
                else:
                    s["timeout_s"] = value
                with self.assertRaises(ValueError):
                    normalize_config(self.raw(places={"a": {"steps": [s]}}))

    def test_max_speed_needs_a_range(self):
        self.bad("speed_range", places={"a": {"steps": [done("a", "--cruise", "{max_speed}")]}})
        self.ok(places={"a": {"steps": [done("a", "--cruise", "{max_speed}", speed_range=[0.25, 0.6])]}})
        self.bad("只能写一个", places={"a": {"steps": [done("a", speed=0.3, speed_range=[0.2, 0.4])]}})
        self.bad("最低不能高于最高", places={"a": {"steps": [done("a", speed_range=[0.5, 0.4])]}})

    def test_retry(self):
        self.ok(places={"a": {"steps": [done("a", retry={"when": "not ready", "times": 2})]}})
        self.ok(places={"a": {"steps": [done("a", retry={"exit_code": 3})]}})
        self.ok(places={"a": {"steps": [done("a", retry={"when": "not ready", "exit_code": 3, "times": 2})]}})
        for retry in ({"times": 2}, {"when": "x", "times": 0}, {"when": "x", "times": 6}, {"when": "x", "extra": 1}, "x",
                      {"exit_code": 0}, {"exit_code": 256}, {"exit_code": "3"}, {"exit_code": True}, {"exit_code": 97}):
            with self.subTest(retry=retry):
                with self.assertRaises(ValueError):
                    normalize_config(self.raw(places={"a": {"steps": [done("a", retry=retry)]}}))

    def regional(self, **changes):
        places = {
            "会议室": {"region": "一楼", "routes": {"一楼": [done("goto-meeting")],
                                                 "二楼": [done("down", enters="一楼"), done("goto-meeting")]}},
            "二楼": {"region": "二楼", "routes": {"一楼": [done("goto-stairs"), done("up", enters="二楼")], "二楼": []}},
        }
        sections = {"places": places, "start_region": "一楼", "state_file": str(self.dir / "region.json")}
        sections.update(changes)
        return sections

    def test_regions(self):
        config = self.ok(**self.regional())
        self.assertTrue(config["regional"])
        self.assertEqual(config["regions"], ["一楼", "二楼"])

    def test_regions_need_a_start_and_a_state_file(self):
        self.bad("start_region", **self.regional(start_region=None))
        self.bad("start_region", **self.regional(start_region="地下室"))
        self.bad("state_file", **self.regional(state_file=None))
        self.bad("state_file", **self.regional(state_file="region.json"))

    def test_a_route_must_end_in_the_place_s_region(self):
        sections = self.regional()
        sections["places"]["二楼"]["routes"]["一楼"] = [done("goto-stairs"), done("up")]      # forgot "enters"
        self.bad("enters", **sections)

    def test_route_origins_and_entered_regions_must_exist(self):
        sections = self.regional()
        sections["places"]["会议室"]["routes"]["地下室"] = [done("x")]
        self.bad("出发区域", **sections)
        sections = self.regional()
        sections["places"]["二楼"]["routes"]["一楼"] = [done("up", enters="屋顶")]
        self.bad("enters", **sections)

    def test_every_place_names_its_region_or_none_does(self):
        sections = self.regional()
        sections["places"]["前台"] = {"steps": [done("x")]}
        self.bad("每个地点都要写", **sections)

    def test_only_the_place_s_own_region_may_have_an_empty_route(self):
        sections = self.regional()
        sections["places"]["会议室"]["routes"]["一楼"] = []
        self.ok(**sections)
        sections["places"]["二楼"]["routes"]["一楼"] = []
        with self.assertRaises(ValueError):
            normalize_config(self.raw(**sections))

    def test_explore_rules(self):
        base = {"max_duration_s": 600, "steps": [{"argv": [PY, "prog.py", "w", "{duration_s}"], "ok": "done",
                                                 "timeout_margin_s": 30}]}
        self.ok(explore=base)
        self.bad("max_duration_s", explore={**base, "max_duration_s": None})
        self.bad("default_radius", explore={**base, "steps": [{"argv": [PY, "w", "{radius}"], "ok": "d",
                                                               "timeout_margin_s": 5}]})
        self.bad("max_radius", explore={**base, "default_radius": 5,
                                        "steps": [{"argv": [PY, "w", "{radius}"], "ok": "d", "timeout_margin_s": 5}]})
        self.bad("args_if", explore={**base, "steps": [{"argv": [PY, "w", "{max_distance}"], "ok": "d",
                                                        "timeout_margin_s": 5}]})
        self.bad("default_radius 不能大于", explore={**base, "max_radius": 5, "default_radius": 6})
        self.bad("二者取一", explore={**base, "steps": [{"argv": [PY, "w"], "ok": "d"}]})
        self.bad("只在地点写了 region 时使用", explore={**base, "regions": ["一楼"]})


class Running(_Dir):
    REQUEST = {"max_speed": 0.5}

    def test_reaches_a_place(self):
        runner = self.runner(places={"会议室": {"steps": [done("goto")]}})
        handle = runner.start_place("会议室", self.REQUEST)
        self.assertEqual(handle.poll().state, "running")
        status = self.finish(handle)
        self.assertEqual((status.state, status.progress), ("succeeded", 1.0))
        self.assertEqual(status.result, {"reached": True, "place": "会议室"})
        self.assertEqual(self.halts, [])
        self.assertFalse(runner.busy)
        self.assertEqual(self.calls()[0]["cwd"], str(self.dir))

    def test_exit_zero_without_the_success_text_is_a_failure(self):
        runner = self.runner(places={"会议室": {"steps": [step("goto", "--say", "exit refused: not ready: battery")]}})
        status = self.finish(runner.start_place("会议室", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertIn("输出里没有表示成功的内容", status.error)
        self.assertIn("exit refused: not ready: battery", status.error)
        self.assertEqual(len(self.halts), 1)

    def test_success_text_with_a_failing_exit_code_is_a_failure(self):
        runner = self.runner(places={"会议室": {"steps": [done("goto", "--exit", "3")]}})
        status = self.finish(runner.start_place("会议室", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertIn("退出码 3", status.error)

    def test_exit_code_only(self):
        runner = self.runner(places={"a": {"steps": [step("up", exit_code_only=True)]},
                                     "b": {"steps": [step("up", "--exit", "1", exit_code_only=True)]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")
        self.assertEqual(self.finish(runner.start_place("b", self.REQUEST)).state, "failed")

    def test_steps_run_in_order_and_stop_at_the_first_failure(self):
        runner = self.runner(places={"二楼": {"steps": [done("one"), step("two", "--say", "nope"), done("three")]}})
        status = self.finish(runner.start_place("二楼", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertIn("步骤「two」", status.error)
        self.assertEqual(self.names(), ["one", "two"])
        self.assertAlmostEqual(status.progress, 1 / 3, places=3)
        self.assertEqual(status.feedback["step"], "two")
        self.assertEqual((status.feedback["index"], status.feedback["of"]), (2, 3))

    def test_missing_program_is_a_failure_not_a_hang(self):
        runner = self.runner(places={"a": {"steps": [{"argv": ["/no/such/program"], "ok": "x", "timeout_s": 5}]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "failed")

    def test_missing_workdir_is_refused(self):
        config = normalize_config({"workdir": str(self.dir / "gone"), "places": {"a": {"steps": [done("x")]}}})
        with self.assertRaises(ProgramRefused):
            ProgramRunner(config).start_place("a", self.REQUEST)

    def test_retry_only_on_the_named_refusal(self):
        retry = {"when": "sensors not ready", "times": 2, "delay_s": 0.05}
        runner = self.runner(places={"a": {"steps": [done("flaky", "--once", retry=retry)]},
                                     "b": {"steps": [step("other", "--say", "blocked", retry=retry)]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")
        self.assertEqual(self.names(), ["flaky", "flaky"])
        self.assertEqual(self.finish(runner.start_place("b", self.REQUEST)).state, "failed")
        self.assertEqual(self.names().count("other"), 1)

    def test_retry_on_the_reserved_exit_code(self):
        runner = self.runner(places={"a": {"steps": [step("never", "--exit", "3", exit_code_only=True,
                                                          retry={"exit_code": 3, "times": 2, "delay_s": 0.01})]},
                                     "b": {"steps": [step("other", "--exit", "1", exit_code_only=True,
                                                          retry={"exit_code": 3, "times": 2, "delay_s": 0.01})]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "failed")
        self.assertEqual(self.names().count("never"), 3)
        self.assertEqual(self.finish(runner.start_place("b", self.REQUEST)).state, "failed")
        self.assertEqual(self.names().count("other"), 1)

    def test_retry_gives_up(self):
        runner = self.runner(places={"a": {"steps": [step("never", "--say", "sensors not ready",
                                                          retry={"when": "sensors not ready", "times": 2, "delay_s": 0.01})]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "failed")
        self.assertEqual(self.names().count("never"), 3)

    def test_time_out_ends_the_program(self):
        runner = self.runner(places={"a": {"steps": [done("slow", "--sleep", "30", timeout_s=0.4)]}})
        started = time.monotonic()
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(status.state, "failed")
        self.assertIn("没有结束", status.error)
        self.assertFalse(_alive(self.calls()[0]["pid"]))
        self.assertEqual(len(self.halts), 1)

    def test_cancel_ends_it_and_halts(self):
        runner = self.runner(places={"a": {"steps": [done("walk", "--term-clean", "--sleep", "30"), done("next")]}})
        handle = runner.start_place("a", self.REQUEST)
        self.wait_started("walk")
        time.sleep(0.2)
        handle.cancel()
        status = self.finish(handle, 5.0)
        self.assertEqual(status.state, "cancelled")
        self.assertEqual(self.names(), ["walk"])                  # the next step never started
        self.assertEqual(len(self.halts), 1)
        self.assertFalse(_alive(self.calls()[0]["pid"]))

    def test_a_program_that_ignores_the_request_is_killed(self):
        runner = self.runner(places={"a": {"steps": [done("deaf", "--ignore-term", "--sleep", "30")]}})
        handle = runner.start_place("a", self.REQUEST)
        self.wait_started("deaf")
        time.sleep(0.2)
        started = time.monotonic()
        runner.stop_all()
        self.assertLess(time.monotonic() - started, 0.1)          # the stop path does not wait
        status = self.finish(handle, 5.0)
        self.assertEqual(status.state, "cancelled")
        self.assertFalse(_alive(self.calls()[0]["pid"]))

    def test_cancel_is_idempotent_and_safe_after_the_end(self):
        runner = self.runner(places={"a": {"steps": [done("goto")]}})
        handle = runner.start_place("a", self.REQUEST)
        self.finish(handle)
        handle.cancel()
        handle.cancel()
        runner.stop_all()
        self.assertEqual(handle.poll().state, "succeeded")

    @unittest.skipUnless(hasattr(os, "waitid"), "needs waitid to see an ended program before reaping it (Linux)")
    def test_what_a_program_left_running_is_ended_with_it(self):
        runner = self.runner(places={"a": {"steps": [done("parent", "--child")]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "succeeded")
        child = int((self.dir / "child.pid").read_text())
        _support.wait_until(lambda: not _alive(child), 3.0)

    def test_a_helper_that_keeps_the_output_open_does_not_hold_the_action(self):
        # Review finding: the action stayed "running" (and deaf to stop) for as long as such a helper lived.
        runner = self.runner(places={"a": {"steps": [done("with-helper", "--helper", "30")]},
                                     "b": {"steps": [done("next")]}})
        started = time.monotonic()
        status = self.finish(runner.start_place("a", self.REQUEST), 8.0)
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertEqual(status.state, "succeeded")
        self.assertFalse(runner.busy)
        self.assertEqual(self.finish(runner.start_place("b", self.REQUEST)).state, "succeeded")

    def test_output_is_read_even_when_many_files_are_open(self):
        # Second review: select() cannot watch a descriptor above 1023; the reader died silently.
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 1400
        if soft < want:
            if hard != resource.RLIM_INFINITY and hard < want:
                self.skipTest(f"cannot open {want} files here (hard limit {hard})")
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        try:
            runner = self.runner(places={"a": {"steps": [done("x", "--noise")]}})
            status = self.finish(runner.start_place("a", self.REQUEST))
        finally:
            for fd in held:
                os.close(fd)
        self.assertEqual(status.state, "succeeded", status.error)

    def test_a_helper_s_chatter_after_the_program_ended_does_not_hide_its_result(self):
        chatty = self.dir / "chatty.py"
        chatty.write_text(
            "import os, sys, time\n"
            "print('exit route done', flush=True)\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    open('helper.pid', 'w').write(str(os.getpid()))\n"
            "    time.sleep(0.2)\n"
            "    try:\n"
            "        for _ in range(3000):\n"
            "            os.write(1, b'y' * 100 + b'\\n')\n"
            "    except OSError:\n"
            "        pass\n"
            "    os._exit(0)\n"
            "while not os.path.exists('helper.pid'):\n"
            "    time.sleep(0.01)\n")
        runner = self.runner(places={"a": {"steps": [{"argv": [PY, "chatty.py"], "ok": "exit route done", "timeout_s": 20}]}})
        status = self.finish(runner.start_place("a", self.REQUEST), 8.0)
        self.assertEqual(status.state, "succeeded", status.error)

    def test_a_full_pipe_of_final_output_is_not_cut_short_when_the_runtime_is_busy(self):
        # Third review: with a busy thread in the runtime, more than 16 KB still unread at exit lost its end.
        big = self.dir / "big.txt"
        big.write_text("x" * 99 + "\n" + ("y" * 99 + "\n") * 599 + "exit route done\n")
        import threading
        busy = threading.Event()

        def spin():
            while not busy.is_set():
                sum(range(2000))
        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            runner = self.runner(places={"a": {"steps": [{"argv": ["/bin/cat", "big.txt"], "ok": "exit route done",
                                                          "timeout_s": 20}]}})
            states = [self.finish(runner.start_place("a", self.REQUEST)).state for _ in range(25)]
        finally:
            busy.set()
            thread.join(2)
        self.assertEqual(states, ["succeeded"] * 25)

    def test_takeover_is_announced_before_any_program_exists(self):
        order = []
        runner = ProgramRunner(normalize_config(self.raw(places={"a": {"steps": [done("x")]}})),
                               on_takeover=lambda: order.append(("takeover", len(self.calls()))))
        self.runners.append(runner)
        self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(order, [("takeover", 0)])
        with self.assertRaises(ProgramRefused):
            runner.start_place("nowhere", self.REQUEST)
        self.assertEqual(len(order), 1)                        # a refusal announces nothing

    def test_a_number_nothing_can_be_written_from_is_a_refusal_not_a_fault(self):
        runner = self.runner(places={"a": {"steps": [done("x", "--cruise", "{max_speed}", speed_range=[0.2, 0.6])]}})
        handle = runner.start("base.navigate_to_place", {"place": "a", "max_speed": 10 ** 400})
        self.assertEqual(handle.poll().state, "failed")
        self.assertEqual(self.calls(), [])

    def test_a_program_is_asked_to_stop_only_once(self):
        # The runtime stops an adapter again and again while its e-stop is latched; a program whose own
        # stop is a controlled motion must not have it interrupted by a second SIGTERM.
        runner = self.runner(places={"a": {"steps": [done("careful", "--count-term")]}})
        handle = runner.start_place("a", self.REQUEST)
        self.wait_started("careful")
        time.sleep(0.3)
        for _ in range(5):
            handle.cancel()
            runner.stop_all()
            time.sleep(0.05)
        self.assertEqual(self.finish(handle, 5.0).state, "cancelled")
        self.assertEqual((self.dir / "term-count").read_text(), "1")

    def test_no_thread_to_watch_with_means_no_program(self):
        runner = self.runner(places={"a": {"steps": [done("x")]}})
        original = programs.threading.Thread.start
        try:
            def refuse(_self):
                raise RuntimeError("can't start new thread")
            programs.threading.Thread.start = refuse
            with self.assertRaises(ProgramRefused):
                runner.start_place("a", self.REQUEST)
        finally:
            programs.threading.Thread.start = original
        self.assertFalse(runner.busy)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")

    def test_no_reader_thread_means_the_started_program_is_ended(self):
        original = programs.threading.Thread.start
        calls = []

        def second_fails(thread):
            calls.append(thread.name)
            if thread.name == "nx-program-output":
                raise RuntimeError("can't start new thread")
            return original(thread)
        runner = self.runner(places={"a": {"steps": [done("x", "--sleep", "30")]}})
        try:
            programs.threading.Thread.start = second_fails
            status = self.finish(runner.start_place("a", self.REQUEST), 8.0)
        finally:
            programs.threading.Thread.start = original
        self.assertEqual(status.state, "failed")
        self.assertFalse(runner.busy)
        time.sleep(0.3)
        for call in self.calls():
            self.assertFalse(_alive(call["pid"]))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the kernel tells a child of its parent's death on Linux only")
    def test_a_program_is_told_when_the_runtime_dies(self):
        # Review finding: a killed runtime left its program walking, and nothing knew about it.
        marker = self.dir / "told"
        script = (
            "import sys, time\n"
            f"sys.path[:0] = {[str(_support.RUNTIME_DIR), str(_support.REPO_ROOT / 'spec')]!r}\n"
            "from neuxmind_runtime.adapters.programs import ProgramRunner, normalize_config\n"
            f"config = normalize_config({self.raw(places={'a': {'steps': [done('walk', '--on-term-touch', str(marker), '--sleep', '60')]}})!r})\n"
            "ProgramRunner(config).start_place('a', {'max_speed': 0.5})\n"
            "print('started', flush=True)\n"
            "time.sleep(120)\n")
        import subprocess
        runtime = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(runtime.stdout.readline().strip(), "started")
            self.wait_started("walk")
            time.sleep(0.3)
            runtime.kill()                       # SIGKILL: no clean-up code of the runtime runs
            runtime.wait(5)
            _support.wait_until(marker.exists, 5.0)
            _support.wait_until(lambda: not _alive(self.calls()[0]["pid"]), 5.0)
        finally:
            if runtime.poll() is None:
                runtime.kill()
            runtime.stdout.close()

    def test_one_at_a_time(self):
        runner = self.runner(places={"a": {"steps": [done("walk", "--sleep", "30")]}, "b": {"steps": [done("other")]}})
        runner.start_place("a", self.REQUEST)
        with self.assertRaises(ProgramRefused) as caught:
            runner.start_place("b", self.REQUEST)
        self.assertIn("还在运行", str(caught.exception))
        self.assertTrue(runner.busy)
        self.assertEqual(runner.status()["running"], "place:a")

    def test_unknown_place(self):
        runner = self.runner(places={"a": {"steps": [done("x")]}})
        with self.assertRaises(ProgramRefused):
            runner.start_place("b", self.REQUEST)

    def test_large_output_is_bounded_and_the_end_still_counts(self):
        runner = self.runner(places={"a": {"steps": [done("loud", "--noise")]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")

    def test_env_file_is_sourced(self):
        env = self.dir / "env.bash"
        env.write_text("export NX_PROBE=from-env-file\n")
        runner = self.runner(env_file=str(env), places={"a": {"steps": [done("x")]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")
        self.assertEqual(self.calls()[0]["probe"], "from-env-file")

    def test_env_file_cannot_see_or_replace_the_command_or_speak_for_the_program(self):
        env = self.dir / "env.bash"
        env.write_text('echo "args:$#:$*" > env-saw\necho "exit route done"\nset -- /bin/true\nshift 2>/dev/null\n'
                       'export NX_PROBE=ok\n')
        runner = self.runner(env_file=str(env), places={"a": {"steps": [step("real", "--say", "blocked")]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual((self.dir / "env-saw").read_text().strip(), "args:0:")
        self.assertEqual(self.names(), ["real"])               # the real program ran, not /bin/true
        self.assertEqual(self.calls()[0]["probe"], "ok")
        self.assertEqual(status.state, "failed")               # the env file's "route done" did not count

    def test_env_file_that_ends_the_shell_does_not_pass_for_the_program(self):
        # Second review: "exit 0" in the file made a step that only looks at the exit code succeed
        # (and record a new region) although its program never ran.
        env = self.dir / "env.bash"
        env.write_text("export NX_PROBE=ok\nexit 0\n")
        runner = self.runner(env_file=str(env), places={"a": {"steps": [step("up", exit_code_only=True)]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertIn("环境文件", status.error)
        self.assertEqual(self.calls(), [])

    def test_env_file_ending_in_a_false_test_or_changing_directory_is_fine(self):
        env = self.dir / "env.bash"
        env.write_text("export NX_PROBE=ok\ncd /\n[ -f /no/such/file ] && . /no/such/file\n")
        runner = self.runner(env_file=str(env), places={"a": {"steps": [done("x")]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "succeeded", status.error)
        self.assertEqual((self.calls()[0]["probe"], self.calls()[0]["cwd"]), ("ok", str(self.dir)))

    def test_what_the_env_file_leaves_behind_does_not_reach_the_launcher(self):
        # Third review: functions named cd / exec, "set -e" with a false last command, "set -x" tracing
        # and a directory given as the file.
        import subprocess
        major = int(subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                                   text=True).stdout.strip() or 0)
        # bash 3.2 (macOS) leaves the shell on "set -e" + a false command even here; the step then fails
        # without starting anything, which is the safe side. Devices run bash 4 or later.
        errexit = "set -e\n" if major >= 4 else ""
        env = self.dir / "env.bash"
        env.write_text('cd() { echo "exit route done"; }\nexec() { echo "exit route done"; }\n'
                       + errexit + 'set -x\nexport NX_PROBE=ok\nfalse\n')
        runner = self.runner(env_file=str(env), places={"a": {"steps": [step("real", "--say", "blocked")]},
                                                        "b": {"steps": [done("fine")]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(self.names(), ["real"])
        self.assertEqual(status.state, "failed")
        self.assertNotIn("exec", status.error)                 # no shell trace in the output either
        self.assertEqual(self.finish(runner.start_place("b", self.REQUEST)).state, "succeeded")
        folder = self.runner(env_file=str(self.dir), places={"a": {"steps": [done("x")]}})
        self.assertEqual(self.finish(folder.start_place("a", self.REQUEST)).state, "failed")
        self.assertNotIn("x", self.names())

    def test_a_program_that_exits_97_by_itself_is_not_blamed_on_an_env_file(self):
        runner = self.runner(places={"a": {"steps": [step("x", "--exit", "97", exit_code_only=True)]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertIn("退出码 97", status.error)

    def test_a_program_name_that_looks_like_an_option_is_run_as_a_program(self):
        runner = self.runner(places={"a": {"steps": [{"argv": ["-c", "prog.py"], "ok": "x", "timeout_s": 5}]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertEqual(self.calls(), [])

    def test_unreadable_env_file_fails_the_step(self):
        runner = self.runner(env_file=str(self.dir / "missing.bash"), places={"a": {"steps": [done("x")]}})
        status = self.finish(runner.start_place("a", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertEqual(self.calls(), [])

    def test_arguments_are_not_interpreted_by_a_shell(self):
        runner = self.runner(places={"a": {"steps": [done("x", "$(touch pwned)", "; touch pwned2", "a b")]}})
        self.assertEqual(self.finish(runner.start_place("a", self.REQUEST)).state, "succeeded")
        self.assertEqual(self.calls()[0]["args"][2:], ["$(touch pwned)", "; touch pwned2", "a b"])
        self.assertFalse((self.dir / "pwned").exists() or (self.dir / "pwned2").exists())


class Speed(_Dir):
    def test_bound_is_passed_and_capped_at_the_program_s_own_maximum(self):
        runner = self.runner(places={"a": {"steps": [done("x", "--cruise", "{max_speed}", speed_range=[0.25, 0.6])]}})
        self.finish(runner.start_place("a", {"max_speed": 0.4}))
        self.finish(runner.start_place("a", {"max_speed": 2.0}))
        self.finish(runner.start_place("a", {"max_speed": 0.33333}))
        cruise = [c["args"][c["args"].index("--cruise") + 1] for c in self.calls()]
        self.assertEqual(cruise, ["0.4", "0.6", "0.333"])

    def test_bound_below_what_a_step_needs_refuses_the_whole_route_before_it_starts(self):
        route = [done("one", "--cruise", "{max_speed}", speed_range=[0.25, 0.6]), done("stairs", speed=0.3)]
        runner = self.runner(places={"a": {"steps": route}})
        for bound in (0.2, 0.28):
            with self.subTest(bound=bound):
                with self.assertRaises(ProgramRefused):
                    runner.start_place("a", {"max_speed": bound})
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.finish(runner.start_place("a", {"max_speed": 0.3})).state, "succeeded")

    def test_the_speed_given_to_a_program_is_never_rounded_above_the_bound(self):
        runner = self.runner(places={"a": {"steps": [done("x", "--cruise", "{max_speed}", speed_range=[0.25, 0.6])]}})
        for bound in (0.2996, 0.59999, 0.2999999999995, 0.3, 0.1 + 0.2, 0.58):
            self.finish(runner.start_place("a", {"max_speed": bound}))
        cruise = [c["args"][c["args"].index("--cruise") + 1] for c in self.calls()]
        self.assertEqual(cruise, ["0.299", "0.599", "0.299", "0.3", "0.3", "0.58"])

    def test_a_bound_that_falls_below_the_program_s_minimum_once_written_is_refused(self):
        runner = self.runner(places={"a": {"steps": [done("x", "--cruise", "{max_speed}", speed_range=[0.2505, 0.6])]}})
        with self.assertRaises(ProgramRefused):
            runner.start_place("a", {"max_speed": 0.2509})         # would be written as 0.25
        self.assertEqual(self.calls(), [])

    def test_no_bound_no_program(self):
        runner = self.runner(places={"a": {"steps": [done("x")]}})
        for request in ({}, {"max_speed": None}, {"max_speed": 0}, {"max_speed": float("nan")}, {"max_speed": True}):
            with self.subTest(request=request):
                with self.assertRaises(ProgramRefused):
                    runner.start_place("a", request)
        self.assertEqual(self.calls(), [])


class ExploreProgram(_Dir):
    def explore(self, **changes):
        section = {"max_duration_s": 600, "max_radius": 10, "default_radius": 5,
                   "steps": [{"title": "自由走", "argv": [PY, "prog.py", "wander", "--say", "exit time", "--seconds",
                                                       "{duration_s}", "--leash", "{radius}", "--cruise", "{max_speed}",
                                                       "--run", "{run}"],
                              "args_if": {"max_distance": ["--max-dist", "{max_distance}"]},
                              "speed_range": [0.25, 0.4], "ok": "exit (time|segment done)", "timeout_margin_s": 5}]}
        section.update(changes)
        return section

    def value(self, call, flag):
        return call["args"][call["args"].index(flag) + 1]

    def test_request_values_reach_the_program(self):
        runner = self.runner(explore=self.explore())
        status = self.finish(runner.start_explore({"duration_s": 120, "radius": 3.5, "max_distance": 40.0, "max_speed": 1.0}))
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result, {"completed": True})
        call = self.calls()[0]
        self.assertEqual([self.value(call, f) for f in ("--seconds", "--leash", "--cruise", "--max-dist")],
                         ["120", "3.5", "0.4", "40"])
        self.assertRegex(self.value(call, "--run"), r"^nx-[0-9a-f]{8}$")

    def test_defaults(self):
        runner = self.runner(explore=self.explore())
        self.finish(runner.start_explore({"duration_s": 30.0, "max_speed": 0.3}))
        call = self.calls()[0]
        self.assertEqual(self.value(call, "--leash"), "5")
        self.assertNotIn("--max-dist", call["args"])

    def test_time_out_follows_the_requested_duration(self):
        section = self.explore(steps=[{"argv": [PY, "prog.py", "wander", "--sleep", "30", "{duration_s}"], "ok": "x",
                                       "timeout_margin_s": 0.2}])
        runner = self.runner(explore=section)
        started = time.monotonic()
        status = self.finish(runner.start_explore({"duration_s": 0.3, "max_speed": 0.3}))
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("没有结束", status.error)

    def test_a_value_too_small_to_write_is_refused_not_passed_as_zero(self):
        # "--leash 0" or "--max-dist 0" may mean "no limit" to a program.
        runner = self.runner(explore=self.explore())
        for request in ({"duration_s": 0.0000004}, {"duration_s": 30, "radius": 0.0000004},
                        {"duration_s": 30, "max_distance": 0.0000004}):
            with self.subTest(request=request):
                with self.assertRaises(ProgramRefused):
                    runner.start_explore({"max_speed": 0.3, **request})
        self.assertEqual(self.calls(), [])
        for bad in (float("nan"), float("inf"), "30", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ProgramRefused):
                    runner.start_explore({"max_speed": 0.3, "duration_s": bad})
        self.assertEqual(self.calls(), [])
        self.finish(runner.start_explore({"max_speed": 0.3, "duration_s": 0.0004, "radius": 0.0004}))
        call = self.calls()[0]
        self.assertEqual((self.value(call, "--seconds"), self.value(call, "--leash")), ("0.0004", "0.0004"))

    def test_not_configured(self):
        runner = self.runner(places={"a": {"steps": [done("x")]}})
        with self.assertRaises(ProgramRefused):
            runner.start_explore({"duration_s": 10, "max_speed": 0.3})


class Regions(_Dir):
    REQUEST = {"max_speed": 0.5}

    def sections(self, up=("--say", "exit route done"), explore_regions=("一楼",)):
        places = {
            "出发点": {"region": "一楼", "routes": {"一楼": [done("goto-home")],
                                                 "二楼": [done("down", enters="一楼"), done("goto-home")]}},
            "会议室": {"region": "一楼", "routes": {"一楼": [done("goto-meeting")],
                                                 "二楼": [done("down", enters="一楼"), done("goto-meeting")]}},
            "二楼": {"region": "二楼", "routes": {"一楼": [done("goto-stairs"), step("up", *up, enters="二楼")], "二楼": []}},
        }
        explore = {"max_duration_s": 60, "regions": list(explore_regions),
                   "steps": [{"argv": [PY, "prog.py", "wander", "--say", "exit time", "{duration_s}"], "ok": "exit time",
                              "timeout_margin_s": 5}]}
        return {"places": places, "explore": explore, "start_region": "一楼", "state_file": str(self.dir / "region.json")}

    def saved(self):
        return json.loads((self.dir / "region.json").read_text())["region"]

    def test_route_depends_on_where_the_robot_is(self):
        runner = self.runner(**self.sections())
        self.assertEqual(runner.status()["region"], "一楼")
        self.assertEqual(self.finish(runner.start_place("二楼", self.REQUEST)).state, "succeeded")
        self.assertEqual((runner.region, self.saved()), ("二楼", "二楼"))
        self.assertEqual(self.finish(runner.start_place("会议室", self.REQUEST)).state, "succeeded")
        self.assertEqual(self.names(), ["goto-stairs", "up", "down", "goto-meeting"])
        self.assertEqual((runner.region, self.saved()), ("一楼", "一楼"))

    def test_already_there_is_a_success_without_a_program(self):
        runner = self.runner(**self.sections())
        self.finish(runner.start_place("二楼", self.REQUEST))
        before = len(self.calls())
        status = self.finish(runner.start_place("二楼", self.REQUEST))
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(len(self.calls()), before)

    def test_region_survives_a_restart(self):
        self.finish(self.runner(**self.sections()).start_place("二楼", self.REQUEST))
        again = self.runner(**self.sections())
        self.assertEqual(again.region, "二楼")
        self.finish(again.start_place("出发点", self.REQUEST))
        self.assertEqual(self.names()[-2:], ["down", "goto-home"])

    def test_a_failed_crossing_leaves_the_region_unknown_and_everything_refused(self):
        runner = self.runner(**self.sections(up=("--say", "blocked on the stairs", "--exit", "1")))
        status = self.finish(runner.start_place("二楼", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertIsNone(self.saved())
        self.assertEqual(runner.status(), {"running": None, "region": None, "region_known": False})
        before = len(self.calls())
        for start in (lambda: runner.start_place("会议室", self.REQUEST), lambda: runner.start_place("二楼", self.REQUEST),
                      lambda: runner.start_explore({"duration_s": 10, "max_speed": 0.3})):
            with self.assertRaises(ProgramRefused) as caught:
                start()
            self.assertIn("不知道机器人现在在哪个区域", str(caught.exception))
        self.assertEqual(len(self.calls()), before)

    def test_unknown_survives_a_restart_until_a_person_clears_it(self):
        runner = self.runner(**self.sections(up=("--exit", "1")))
        self.finish(runner.start_place("二楼", self.REQUEST))
        again = self.runner(**self.sections())
        with self.assertRaises(ProgramRefused):
            again.start_place("会议室", self.REQUEST)
        os.unlink(self.dir / "region.json")                     # what the instructions tell the person to do
        cleared = self.runner(**self.sections())
        self.assertEqual(cleared.region, "一楼")
        self.assertEqual(self.finish(cleared.start_place("会议室", self.REQUEST)).state, "succeeded")

    def test_a_cancelled_crossing_is_unknown_too(self):
        runner = self.runner(**self.sections(up=("--term-clean", "--sleep", "30")))
        handle = runner.start_place("二楼", self.REQUEST)
        self.wait_started("up")
        time.sleep(0.2)
        handle.cancel()
        self.assertEqual(self.finish(handle, 5.0).state, "cancelled")
        self.assertFalse(runner.status()["region_known"])

    def test_a_failure_before_the_crossing_keeps_the_region(self):
        sections = self.sections()
        sections["places"]["二楼"]["routes"]["一楼"][0] = step("goto-stairs", "--say", "blocked")
        runner = self.runner(**sections)
        self.assertEqual(self.finish(runner.start_place("二楼", self.REQUEST)).state, "failed")
        self.assertEqual(runner.region, "一楼")
        self.assertTrue(runner.status()["region_known"])
        self.assertEqual(self.names(), ["goto-stairs"])

    def test_a_crossing_refused_before_it_moved_keeps_the_region(self):
        # The program reserves exit code 3 for "not ready, nothing sent".
        sections = self.sections()
        sections["places"]["二楼"]["routes"]["一楼"][1] = step(
            "up", "--say", "!! not ready (no motion sent)", "--exit", "3", enters="二楼",
            retry={"exit_code": 3, "times": 2, "delay_s": 0.01})
        runner = self.runner(**sections)
        status = self.finish(runner.start_place("二楼", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertEqual(self.names().count("up"), 3)
        self.assertEqual((runner.region, self.saved()), ("一楼", "一楼"))
        self.assertTrue(runner.status()["region_known"])

    def test_refusal_text_alone_is_no_proof_that_nothing_moved(self):
        # Second review: a program that climbed three steps and then printed the same words as its
        # start-up refusal got the old floor written back.
        sections = self.sections()
        sections["places"]["二楼"]["routes"]["一楼"][1] = step(
            "up", "--say", "climbed 3 steps; sensors lost: not ready (no motion sent)", "--exit", "1", enters="二楼",
            retry={"when": "no motion sent", "times": 2, "delay_s": 0.01})
        runner = self.runner(**sections)
        self.assertEqual(self.finish(runner.start_place("二楼", self.REQUEST)).state, "failed")
        self.assertEqual(self.names().count("up"), 3)                   # the text still makes it try again
        self.assertFalse(runner.status()["region_known"])
        self.assertIsNone(self.saved())

    def test_a_crossing_that_was_refused_once_and_then_failed_another_way_is_unknown(self):
        sections = self.sections()
        (self.dir / "attempt").write_text("0")
        flaky = self.dir / "flaky.py"
        flaky.write_text("import sys\nn = int(open('attempt').read()) + 1\nopen('attempt', 'w').write(str(n))\n"
                         "print('blocked halfway' if n == 2 else 'not ready')\nsys.exit(1 if n == 2 else 3)\n")
        sections["places"]["二楼"]["routes"]["一楼"][1] = {
            "title": "up", "argv": [PY, "flaky.py"], "ok": "route done", "timeout_s": 20, "enters": "二楼",
            "retry": {"exit_code": 3, "times": 3, "delay_s": 0.01}}
        runner = self.runner(**sections)
        self.assertEqual(self.finish(runner.start_place("二楼", self.REQUEST)).state, "failed")
        self.assertEqual((self.dir / "attempt").read_text(), "2")       # the second failure was not retried
        self.assertFalse(runner.status()["region_known"])
        self.assertIsNone(self.saved())

    def test_a_crossing_that_timed_out_is_unknown_even_if_its_output_looks_like_a_refusal(self):
        sections = self.sections()
        sections["places"]["二楼"]["routes"]["一楼"][1] = step(
            "up", "--say", "no motion sent", "--ignore-term", "--sleep", "30", enters="二楼", timeout_s=0.4,
            retry={"when": "no motion sent|walking", "exit_code": 3, "times": 1, "delay_s": 0.01})
        runner = self.runner(**sections)
        self.assertEqual(self.finish(runner.start_place("二楼", self.REQUEST)).state, "failed")
        self.assertFalse(runner.status()["region_known"])

    def test_a_damaged_state_file_means_unknown(self):
        (self.dir / "region.json").write_text("{not json")
        with self.assertRaises(ProgramRefused):
            self.runner(**self.sections()).start_place("会议室", self.REQUEST)
        (self.dir / "region.json").write_text(json.dumps({"region": "地下室"}))
        with self.assertRaises(ProgramRefused):
            self.runner(**self.sections()).start_place("会议室", self.REQUEST)

    def test_state_file_that_cannot_be_written_stops_before_the_crossing(self):
        sections = self.sections()
        sections["state_file"] = str(self.dir / "no-such-dir" / "region.json")
        runner = self.runner(**sections)
        status = self.finish(runner.start_place("二楼", self.REQUEST))
        self.assertEqual(status.state, "failed")
        self.assertEqual(self.names(), ["goto-stairs"])            # the stairs program was never started
        self.assertEqual(runner.region, "一楼")

    def test_free_walk_only_where_allowed(self):
        runner = self.runner(**self.sections())
        self.assertEqual(self.finish(runner.start_explore({"duration_s": 5, "max_speed": 0.3})).state, "succeeded")
        self.finish(runner.start_place("二楼", self.REQUEST))
        with self.assertRaises(ProgramRefused) as caught:
            runner.start_explore({"duration_s": 5, "max_speed": 0.3})
        self.assertIn("只允许在", str(caught.exception))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # a zombie still answers signal 0
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


if __name__ == "__main__":
    unittest.main()
