"""Round-05 runtime regressions (docs/evolution/reviews/2026-11-09-iter04-rereview3.md, 遗留).

* R4b-04 root fix: the latched-state guard. A driver that resumes its last setpoint 6 s / 9 s after
  *every* stop (outside any finite observation window) is re-stopped while latched; displacement
  stays bounded; ``/v1/status`` shows ``latched_motion``. The guard never unlatches, never sends a
  motion command, never counts as a stop, never blocks the stop path, and stops on close().
* R4c-01: resets are serialised -- a reset while an earlier ``adapter.reset()`` is still in flight
  (including one the conformance suite abandoned) gets 409 ``reset_in_progress``; memory and disk
  latch state always agree; ``safety.reset_raced`` says what really happened.
* R4c-02: ``/v2/stop keep_reason`` is compare-and-set: a slow body cannot revert a newer reason.
* R4c-05: a joint_position action chunk that can never be limited is refused synchronously (400).
* R4c-03: the scaffold emits an ``ARM_JOINT_NAMES`` placeholder telling the author to take the
  names from ros2_control ``joints:`` (not from /joint_states).
"""
import json
import socket
import tempfile
import threading
import time
import unittest

import _support  # noqa: F401
from _support import TOKEN, http

from neuxmind_runtime import conformance
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import RuntimeApiError
from neuxmind_runtime.gateway import GatewayServer

from test_conformance_rereview04 import UR_HOME, URAlphabetical
from test_conformance_rereview04b import FAST_RESET, HangingReset


def make_runtime(test, adapter, **kw):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None, **kw)
    test.addCleanup(runtime.close)
    return runtime


def events_of(runtime, kind):
    return [e for e in runtime.events.since(0, 10 ** 6) if e["type"] == kind]


def disk_latched(runtime):
    return (runtime.store.get("safety", "latch") or {}).get("latched")


# ====================================================================== R4b-04
class ResumesAfterEveryStop(SimQuadruped):
    """restop2.py "每次停后恢复": ``delay`` simulated seconds after every stop() the driver resumes
    its last non-zero setpoint (motors re-enabled on a timer, last command replayed)."""

    def __init__(self, delay, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.lastsp = (0.0, 0.0, 0.0)
        self.resume_at = None
        self.resumes = 0
        self.invokes_after_stop = []
        self.stopped_once = False

    def invoke(self, cap_id, args):
        if self.stopped_once:
            self.invokes_after_stop.append((cap_id, dict(args)))
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz")):
            self.lastsp = (args["vx"], args.get("vy", 0.0), args["wz"])
        return out

    def stop(self):
        super().stop()
        self.stopped_once = True
        if any(self.lastsp):
            self.resume_at = self._t + self.delay

    def _step(self, dt):
        if self.resume_at is not None and self._t >= self.resume_at:
            self.resume_at = None
            self.resumes += 1
            self.cmd = self.lastsp
        super()._step(dt)


def drive_then_stop(runtime, adapter, clock=None):
    runtime.invoke("base.velocity", {"request_id": "v1", "hardware_authorized": True,
                                     "args": {"vx": 0.3, "vy": 0.0, "wz": 0.0, "timeout_s": 2.0}})
    if clock is not None:
        clock.advance(0.5)
    adapter.advance()
    runtime.emergency_stop("operator: person in path")


class LatchGuardManualClock(unittest.TestCase):
    """Deterministic: the guard is ticked by hand every 0.1 s of simulated time."""

    def _run(self, delay, guard=True, seconds=20.0):
        clock = ManualClock()
        adapter = ResumesAfterEveryStop(delay, clock=clock)
        kw = {} if guard else {"latch_guard": None}
        runtime = make_runtime(self, adapter, start_monitor=False, **kw)
        drive_then_stop(runtime, adapter, clock)
        sup = runtime.supervisor
        count, reason = sup.stop_count, sup.latch_reason
        x0 = adapter.x
        path = []
        for _ in range(int(round(seconds / 0.1))):
            clock.advance(0.1)
            if guard:
                runtime.latch_guard.tick()
            adapter.advance()
            path.append((adapter._t, adapter.x - x0))
        self.assertTrue(sup.latched)                              # never unlatched
        self.assertEqual(sup.stop_count, count)                   # guard re-stops are not counted stops
        self.assertEqual(sup.latch_reason, reason)                # ... and never touch the reason
        self.assertEqual(adapter.invokes_after_stop, [])          # never a motion (or any) command
        return runtime, adapter, path

    @staticmethod
    def window(path, start, length=10.0):
        inside = [d for t, d in path if start <= t - path[0][0] + 0.1 <= start + length]
        return max(inside) - min(inside)

    def test_without_guard_the_driver_drives_away(self):
        _rt, _adapter, path = self._run(6.0, guard=False)
        self.assertGreater(path[-1][1], 3.0)                       # 0.3 m/s from t=6 s on

    def test_resume_6s_is_restopped(self):
        runtime, adapter, path = self._run(6.0)
        self.assertGreaterEqual(adapter.resumes, 3)                # resumed after every (re-)stop
        for start in (0.0, 5.0, 10.0):
            self.assertLess(self.window(path, start), 0.05, path)  # <= ~0.1 s at 0.3 m/s per resume
        self.assertLess(path[-1][1], 0.12)
        lm = runtime.status()["latched_motion"]
        self.assertTrue(lm["enabled"])
        self.assertGreaterEqual(lm["restops"], 3)
        self.assertGreaterEqual(lm["seen"], lm["restops"])
        self.assertIsNotNone(lm["last_at"])
        ev = events_of(runtime, "safety.motion_while_latched")
        # round 07 (Q6-01): events are aggregated, each says how many re-stops it stands for
        pending = runtime.latch_guard._ev["motion"]["pending"]
        self.assertEqual(sum(e["count"] for e in ev) + pending, lm["restops"])
        self.assertLessEqual(len(ev), lm["restops"])
        self.assertEqual(ev[-1]["restops"] + pending, lm["restops"])
        self.assertTrue(ev[0]["adapter_stopped"])

    def test_resume_9s_is_restopped(self):
        runtime, adapter, path = self._run(9.0)
        self.assertGreaterEqual(adapter.resumes, 2)
        self.assertLess(self.window(path, 0.0), 0.05)
        self.assertLess(self.window(path, 8.0), 0.05)
        self.assertLess(path[-1][1], 0.08)
        self.assertGreaterEqual(runtime.status()["latched_motion"]["restops"], 2)

    def test_restops_are_rate_limited(self):
        """A driver that ignores stop() completely: re-stops at most every 0.5 s."""
        clock = ManualClock()

        class IgnoresStop(SimQuadruped):
            def stop(self):
                self._try_advance()            # keeps self.cmd: never stops
        adapter = IgnoresStop(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        drive_then_stop(runtime, adapter, clock)
        for _ in range(50):                   # 5 s, sampled every 0.1 s
            clock.advance(0.1)
            runtime.latch_guard.tick()
        lm = runtime.status()["latched_motion"]
        # 1 s settle after the stop, then at most one re-stop per 0.5 s over the remaining 4 s.
        self.assertGreaterEqual(lm["restops"], 7)
        self.assertLessEqual(lm["restops"], 9)
        self.assertGreater(lm["seen"], lm["restops"])

    def test_braking_after_stop_is_not_motion(self):
        """Settle window: a body still braking right after a counted stop is not flagged."""
        clock = ManualClock()

        class Brakes(SimQuadruped):
            def stop(self):
                self._try_advance()
                self.cmd = (0.05, 0.0, 0.0)       # coasts for 0.5 s, then still
                self.brake_until = self._t + 0.5

            def _step(self, dt):
                if getattr(self, "brake_until", None) is not None and self._t >= self.brake_until:
                    self.cmd, self.brake_until = (0.0, 0.0, 0.0), None
                super()._step(dt)
        adapter = Brakes(clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        drive_then_stop(runtime, adapter, clock)
        for _ in range(30):
            clock.advance(0.1)
            runtime.latch_guard.tick()
        self.assertEqual(runtime.status()["latched_motion"]["seen"], 0)

    def test_idle_when_not_latched(self):
        clock = ManualClock()
        adapter = ResumesAfterEveryStop(6.0, clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.invoke("base.velocity", {"request_id": "v1", "hardware_authorized": True,
                                         "args": {"vx": 0.3, "vy": 0.0, "wz": 0.0, "timeout_s": 2.0}})
        for _ in range(10):
            clock.advance(0.1)
            self.assertFalse(runtime.latch_guard.tick())
        self.assertEqual(runtime.status()["latched_motion"]["seen"], 0)
        self.assertFalse(runtime.status()["latched_motion"]["active"])


class LatchGuardThreaded(unittest.TestCase):
    """The real guard thread, real clock (x2), via /v1/status over HTTP."""

    def test_resume_6s_and_9s_over_http(self):
        for delay in (6.0, 9.0):
            with self.subTest(delay=delay):
                adapter = ResumesAfterEveryStop(delay, time_scale=2.0)
                runtime = make_runtime(self, adapter)
                server = GatewayServer(runtime, TOKEN, port=0).start()
                self.addCleanup(server.close)
                drive_then_stop(runtime, adapter)
                time.sleep(0.25)
                adapter.advance()
                x0, t0 = adapter.x, adapter._t
                deadline = time.monotonic() + 30
                while adapter._t - t0 < delay + 10.0 and time.monotonic() < deadline:
                    time.sleep(0.1)
                    adapter.advance()
                moved = adapter.x - x0
                self.assertGreaterEqual(adapter.resumes, 1)
                self.assertLess(moved, 0.1, moved)                 # 0.3 m/s * 10 s = 3 m without the guard
                status, body = http(server.url, "/v1/status")
                self.assertEqual(status, 200)
                lm = body["latched_motion"]
                self.assertGreaterEqual(lm["restops"], 1, lm)
                self.assertTrue(body["estop_latched"])
                self.assertEqual(body["latch_reason"], "operator: person in path")

    def test_hung_read_never_blocks_stop_or_close(self):
        release = threading.Event()

        class HangsOdometry(SimQuadruped):
            hang = False

            def read(self, cap_id):
                if self.hang and cap_id == "base.odometry" and threading.current_thread().name == "nx-latch-guard":
                    release.wait(10)
                return super().read(cap_id)
        adapter = HangsOdometry(time_scale=1.0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        try:
            runtime.emergency_stop("x")
            adapter.hang = True
            time.sleep(0.4)                                          # guard thread now stuck in read()
            t = time.monotonic()
            for _ in range(5):
                runtime.emergency_stop("again")
            self.assertLess(time.monotonic() - t, 0.5)
        finally:
            t = time.monotonic()
            runtime.close()
            closed_in = time.monotonic() - t
            release.set()
        self.assertLess(closed_in, 3.0)

    def test_guard_thread_exits_on_close(self):
        adapter = SimQuadruped(time_scale=1.0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        runtime.emergency_stop("x")
        time.sleep(0.3)
        runtime.close()
        time.sleep(0.2)
        self.assertNotIn("nx-latch-guard", [t.name for t in threading.enumerate()])

    def test_reset_that_moves_the_body_is_not_aborted(self):
        """A quadruped standing up inside adapter.reset() is observed, not re-stopped."""
        class StandsUp(SimQuadruped):
            def reset(self):
                with self._lock:
                    self.cmd = (0.0, 0.0, 0.3)
                time.sleep(0.6)
                with self._lock:
                    self.cmd = (0.0, 0.0, 0.0)
        adapter = StandsUp(time_scale=1.0)
        runtime = make_runtime(self, adapter)
        runtime.emergency_stop("x")
        time.sleep(1.2)                                            # past the settle window
        out = runtime.reset({"hardware_authorized": True})
        self.assertFalse(out["latched"])
        lm = runtime.status()["latched_motion"]
        self.assertEqual(lm["restops"], 0)
        self.assertGreaterEqual(lm["seen_during_reset"], 1)


# ====================================================================== R4c-01
class SerialisedReset(unittest.TestCase):
    def test_second_reset_while_adapter_reset_in_flight_is_409(self):
        adapter = HangingReset(1, 1.5, replay=False)
        runtime = make_runtime(self, adapter)
        runtime.emergency_stop("operator")
        box = {}
        first = threading.Thread(target=lambda: box.setdefault("out", runtime.reset({"hardware_authorized": True})))
        first.start()
        time.sleep(0.3)
        self.assertTrue(runtime.status()["reset_in_progress"])
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True, "request_id": "op-1"})
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "reset_in_progress"))
        self.assertIn("尚未返回", str(ctx.exception))
        self.assertEqual(adapter.calls.count("reset"), 1)                 # never entered concurrently
        first.join(5)
        self.assertFalse(box["out"]["latched"])
        self.assertFalse(runtime.supervisor.latched)
        self.assertIs(disk_latched(runtime), False)
        self.assertFalse(runtime.status()["reset_in_progress"])
        runtime.emergency_stop("again")
        out = runtime.reset({"hardware_authorized": True, "request_id": "op-1"})   # same id works later
        self.assertFalse(out["latched"])

    def test_supervisor_level_atomic_refusal(self):
        """Even past the core pre-check (two resets racing), only one enters adapter.reset()."""
        adapter = HangingReset(1, 1.0, replay=False)
        runtime = make_runtime(self, adapter)
        runtime.emergency_stop("operator")
        results = []

        def go():
            try:
                results.append(runtime.supervisor.reset(True))
            except RuntimeApiError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(adapter.calls.count("reset"), 1)
        self.assertEqual(sorted(map(str, results)).count("reset_in_progress"), 3)

    def test_operator_reset_while_abandoned_suite_reset_in_flight(self):
        """op_reset_inflight.py 4 25 0.5 (shortened): the suite gives up on its 4th reset; the
        operator's reset 0.5 s after the suite returns is refused until adapter.reset() returns;
        afterwards memory, disk and the event agree (latched), and a later reset works."""
        adapter = HangingReset(4, 12.0)
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        th = conformance.Thresholds(reset_call_timeout_s=2.0, restop_deadline_s=6.0)
        report = conformance.run(target, "motion", True, thresholds=th)
        runtime = target.runtime
        self.assertTrue(report["aborted"])
        self.assertFalse(adapter.returned.is_set())                       # still inside adapter.reset()
        time.sleep(0.5)
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True})
        self.assertEqual(ctx.exception.code, "reset_in_progress")         # before: latched:false at once
        self.assertEqual(adapter.calls.count("reset"), 4)
        self.assertTrue(adapter.returned.wait(20))
        time.sleep(0.3)
        sup = runtime.supervisor
        self.assertTrue(sup.latched)
        self.assertIs(disk_latched(runtime), True)                         # before: disk true, memory false
        raced = events_of(runtime, "safety.reset_raced")
        self.assertTrue(raced)
        self.assertIs(raced[-1]["latched"], True)
        self.assertIn("保持锁存", raced[-1]["note"])
        out = runtime.reset({"hardware_authorized": True})
        self.assertFalse(out["latched"])
        self.assertIs(disk_latched(runtime), False)

    def test_raced_reset_disk_and_memory_agree(self):
        adapter = HangingReset(1, 0.5, replay=True)
        runtime = make_runtime(self, adapter)
        runtime.emergency_stop("operator")
        threading.Thread(target=lambda: (time.sleep(0.1), runtime.emergency_stop("second")), daemon=True).start()
        with self.assertRaises(RuntimeApiError):
            runtime.reset({"hardware_authorized": True})
        time.sleep(0.4)                                                    # persister thread
        self.assertTrue(runtime.supervisor.latched)
        self.assertIs(disk_latched(runtime), True)


# ====================================================================== R4c-02
class KeepReasonCompareAndSet(unittest.TestCase):
    def _slow_stop(self, url, body: dict):
        """Send the request line and headers of POST /v2/stop now; the body only when told."""
        host, port = url.replace("http://", "").split(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        raw = json.dumps(body).encode()
        sock.sendall((f"POST /v2/stop HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                      f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n").encode())

        def finish():
            sock.sendall(raw)
            data = b""
            while b"\r\n\r\n" not in data or not data.rstrip().endswith(b"}"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.close()
            return json.loads(data.split(b"\r\n\r\n", 1)[1])
        return finish

    def test_slow_keep_reason_cannot_revert_a_newer_reason(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        server = GatewayServer(runtime, TOKEN, port=0).start()
        self.addCleanup(server.close)
        self.assertEqual(http(server.url, "/v2/stop", {"reason": "operator: first"})[0], 200)
        finish = self._slow_stop(server.url, {"reason": "conformance:x:abort", "keep_reason": True})
        deadline = time.monotonic() + 5
        while runtime.supervisor.latch_reason != "stop" and time.monotonic() < deadline:
            time.sleep(0.01)                                   # the slow stop has stopped (placeholder)
        self.assertEqual(http(server.url, "/v2/stop", {"reason": "operator: person in path"})[0], 200)
        out = finish()
        self.assertIs(out["latch_reason_kept"], False)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in path")   # before: "operator: first"

    def test_keep_reason_still_restores_when_nothing_newer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        server = GatewayServer(runtime, TOKEN, port=0).start()
        self.addCleanup(server.close)
        http(server.url, "/v2/stop", {"reason": "operator: first"})
        status, out = http(server.url, "/v2/stop", {"reason": "conformance:x", "keep_reason": True})
        self.assertEqual(status, 200)
        self.assertIs(out["latch_reason_kept"], True)
        self.assertIsInstance(out["stop_count"], int)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: first")

    def test_set_reason_if_semantics(self):
        runtime = make_runtime(self, SimQuadruped(), start_monitor=False)
        sup = runtime.supervisor
        n = sup.emergency_stop("stop")["stop_count"]
        self.assertFalse(sup.set_reason_if("a", stop_count=n, expect_reason="other"))
        self.assertTrue(sup.set_reason_if("a", stop_count=n, expect_reason="stop"))
        sup.emergency_stop("b")
        self.assertFalse(sup.set_reason_if("c", stop_count=n, expect_reason="b"))
        self.assertEqual(sup.latch_reason, "b")


# ====================================================================== R4c-05
class ChunkRefusedSynchronously(unittest.TestCase):
    def test_joint_chunk_without_joint_names_is_400(self):
        adapter = URAlphabetical(declare=None, chunk=True)
        runtime = make_runtime(self, adapter)
        before = len(runtime.runner.list())
        row = list(UR_HOME)
        row[0] = 1.5
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [row], "dt": 0.1},
                                                    "hardware_authorized": True})
        self.assertEqual((ctx.exception.status, ctx.exception.code), (400, "joint_names_required"))
        self.assertIn("ros2_control", str(ctx.exception))
        self.assertEqual(len(runtime.runner.list()), before)                # no run id handed out
        self.assertEqual(adapter.arm_calls(), [])
        # replay of the same request_id gives the same answer
        with self.assertRaises(RuntimeApiError) as again:
            runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [row], "dt": 0.1},
                                                    "hardware_authorized": True})
        self.assertEqual(again.exception.code, "joint_names_required")

    def test_over_http(self):
        adapter = URAlphabetical(declare=None, chunk=True)
        runtime = make_runtime(self, adapter)
        server = GatewayServer(runtime, TOKEN, port=0).start()
        self.addCleanup(server.close)
        status, body = http(server.url, "/v2/capabilities/policy.action_chunk/invoke",
                            {"request_id": "c2", "args": {"actions": [list(UR_HOME)], "dt": 0.1},
                             "hardware_authorized": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "joint_names_required")
        self.assertNotIn("run_id", body)

    def test_declared_names_still_run(self):
        adapter = URAlphabetical(declare="ur", chunk=True)
        runtime = make_runtime(self, adapter)
        out = runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [list(UR_HOME)],
                                                                                   "dt": 0.1},
                                                      "hardware_authorized": True})
        self.assertIn("run_id", out)


# ====================================================================== R4c-03
class ScaffoldJointNames(unittest.TestCase):
    def _gen(self, spec):
        from test_scaffold import _Generated
        return _Generated(self, spec)

    def test_placeholder_and_comment(self):
        import copy
        from test_scaffold import SDK_ARM
        gen = self._gen(copy.deepcopy(SDK_ARM))
        source = (gen.root / "orbit_arm" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("ARM_JOINT_NAMES = None   # TODO(joint_names)", source)
        self.assertIn("ros2_control", source)
        self.assertIn("`joints:`", source)
        self.assertIn("/joint_states", source)
        readme = (gen.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("joint_names", readme)
        self.assertIn("joints:", readme)
        mod = gen.module()
        config = mod.load_config()
        names = [f"j{i}" for i in range(1, 7)]
        mod.IMPLEMENTED = ("safety.stop", "system.health", "joint.state", "arm.joint_position")
        manifest = mod.build_manifest(config)
        arm = next(c for c in manifest["capabilities"] if c["id"] == "arm.joint_position")
        self.assertNotIn("joint_names", arm["params"])                  # placeholder None: not declared
        mod.ARM_JOINT_NAMES = names
        manifest = mod.build_manifest(config)
        arm = next(c for c in manifest["capabilities"] if c["id"] == "arm.joint_position")
        self.assertEqual(arm["params"]["joint_names"], names)

    def test_declared_names_are_kept(self):
        import copy
        from test_scaffold import SDK_ARM
        spec = copy.deepcopy(SDK_ARM)
        spec["name"] = "orbit_arm2"
        names = [f"j{i}" for i in range(1, 7)]
        spec["capabilities"][1]["params"]["joint_names"] = names
        gen = self._gen(spec)
        source = (gen.root / "orbit_arm2" / "adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("TODO(joint_names)", source)
        self.assertIn("Already declared in adapter.json", source)
        config = json.loads((gen.root / "orbit_arm2" / "adapter.json").read_text(encoding="utf-8"))
        arm = next(c for c in config["capabilities"] if c["id"] == "arm.joint_position")
        self.assertEqual(arm["params"]["joint_names"], names)

    def test_no_arm_no_placeholder(self):
        from test_scaffold import ROVER
        import copy
        gen = self._gen(copy.deepcopy(ROVER))
        source = (gen.root / "acme_rover" / "adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("ARM_JOINT_NAMES", source)


if __name__ == "__main__":
    unittest.main()
