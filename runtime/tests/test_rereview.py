"""Re-review fixes (docs/evolution/reviews/2026-11-05-iter01-rereview.md): N-02, N-03, N-04, N-06
and tests for the surviving mutants M2, M4, M6."""
import socket
import tempfile
import threading
import time
import unittest

import _support  # noqa: F401
from _support import TOKEN, RuntimeFixture, example, wait_until

from neuxmind_runtime.adapters import sim_arm as sim_arm_module
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import EStopLatched, RuntimeApiError, SafetyRejected
from neuxmind_runtime.gateway import GatewayServer
from neuxmind_runtime.skills import SkillContext
from neuxmind_runtime import runner as runner_module


class _Recording(SimQuadruped):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.dispatched = []
        self.stops = 0
        self.block_moving_velocity = None        # Event: a moving velocity invoke waits on it

    def invoke(self, cap_id, args):
        moving = any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz"))
        self.dispatched.append(("invoke", cap_id, moving, time.monotonic()))
        if cap_id == "base.velocity" and moving and self.block_moving_velocity is not None:
            self.block_moving_velocity.wait(10)
        return super().invoke(cap_id, args)

    def start_action(self, cap_id, args):
        self.dispatched.append(("start_action", cap_id, True, time.monotonic()))
        return super().start_action(cap_id, args)

    def stop(self):
        self.stops += 1
        super().stop()

    def motion(self):
        return [d for d in self.dispatched if d[2]]


class _SlowLedgerStore:
    """Blocks writing the RUNNING record of one request_id until released (SD-card fsync stall)."""

    def __init__(self, inner, request_id):
        self._inner, self._rid = inner, request_id
        self.release = threading.Event()
        self.blocked = threading.Event()

    def put(self, kind, obj):
        if kind == "requests" and obj.get("id") == self._rid and obj.get("status") == "running":
            self.blocked.set()
            self.release.wait(10)
        return self._inner.put(kind, obj)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class LatchedArrivalTests(unittest.TestCase):
    """N-02: a motion request that arrived while latched never runs after a reset."""

    def _scenario(self, send):
        adapter = _Recording(time_scale=20.0)
        with tempfile.TemporaryDirectory() as state:
            rt = EdgeRuntime(adapter, state)
            try:
                rt.install_skill({"request_id": "inst", "package": example("patrol")})
                slow = _SlowLedgerStore(rt.ledger._store, "late-1")
                rt.ledger._store = slow
                rt.emergency_stop("operator")
                adapter.dispatched.clear()
                result = {}

                def go():
                    try:
                        result["ok"] = send(rt)
                    except RuntimeApiError as exc:
                        result["err"] = exc.code
                thread = threading.Thread(target=go)
                thread.start()
                self.assertTrue(slow.blocked.wait(5))      # arrived while latched, stuck on disk
                self.assertFalse(rt.reset({"hardware_authorized": True})["latched"])
                slow.release.set()
                thread.join(5)
                time.sleep(0.3)
                self.assertEqual(adapter.motion(), [], f"锁存期间到达的请求在 reset 后下发了：{result}")
                self.assertEqual(result.get("err"), "stale_after_stop", result)
                self.assertEqual(rt.runner.active_runs(), [])
            finally:
                slow.release.set()
                rt.close()

    def test_command(self):
        self._scenario(lambda rt: rt.invoke("base.velocity", {
            "request_id": "late-1", "hardware_authorized": True, "args": {"vx": 0.3, "wz": 0.0}}))

    def test_action(self):
        self._scenario(lambda rt: rt.invoke("base.navigate_to", {
            "request_id": "late-1", "hardware_authorized": True, "args": {"goal": [5.0, 0, 0, 0, 0, 0, 1]}}))

    def test_skill_run(self):
        self._scenario(lambda rt: rt.run_skill("patrol", {
            "request_id": "late-1", "hardware_authorized": True, "parameters": {"waypoints": [[5, 0]]}}))

    def test_reset_advances_epoch(self):
        bus = CapabilityBus(SimQuadruped(clock=ManualClock()))
        sup = bus.supervisor
        sup.emergency_stop("x")
        during_latch = sup.epoch
        sup.reset(authorized=True)
        self.assertNotEqual(sup.epoch, during_latch)
        with self.assertRaises(EStopLatched):
            bus.invoke("base.velocity", {"vx": 0.3, "wz": 0.0}, authorized=True, epoch=during_latch)


class StuckVelocityCommandTests(unittest.TestCase):
    """N-03: a velocity command stuck in the adapter cannot disable the watchdog."""

    def test_watchdog_zeroes_while_invoke_is_stuck(self):
        adapter = _Recording(time_scale=1.0)
        with tempfile.TemporaryDirectory() as state:
            rt = EdgeRuntime(adapter, state)
            release = threading.Event()
            try:
                rt.bus.invoke("base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 0.2}, authorized=True)
                started = time.monotonic()
                adapter.block_moving_velocity = release
                stuck = threading.Thread(target=lambda: rt.bus.invoke(
                    "base.velocity", {"vx": 0.5, "wz": 0.0, "timeout_s": 0.2}, authorized=True))
                stuck.start()
                wait_until(lambda: rt.supervisor.velocity_lock.locked(), 2)
                zero = wait_until(lambda: [d for d in adapter.dispatched
                                           if d[1] == "base.velocity" and not d[2]], 1.5)
                self.assertLess(zero[0][3] - started, 0.6)   # 0.2 s timeout + watchdog period
                self.assertGreaterEqual(adapter.stops, 1)
                self.assertTrue(stuck.is_alive())            # the stuck command is still stuck
                fired = [e for e in rt.events.since(0) if e["type"] == "safety.watchdog"]
                self.assertTrue(fired and fired[0].get("unlocked"))
            finally:
                release.set()
                rt.close()


class FrozenLimitsTests(unittest.TestCase):
    """N-04: limits are deep-copied and frozen; runtime manifest changes refuse motion."""

    def setUp(self):
        self.saved = list(sim_arm_module.LOWER), list(sim_arm_module.UPPER)

    def tearDown(self):
        sim_arm_module.LOWER[:], sim_arm_module.UPPER[:] = self.saved

    def test_adapter_mutating_its_lists_in_place_has_no_effect(self):
        arm = SimArm(clock=ManualClock())
        bus = CapabilityBus(arm)
        lower = bus.supervisor.params["arm.joint_position"]["lower"]
        self.assertIsInstance(lower, tuple)
        self.assertIsNot(bus.manifest["capabilities"], arm.manifest()["capabilities"])
        sim_arm_module.LOWER[5] = float("nan")
        sim_arm_module.UPPER[5] = float("nan")
        with self.assertRaises(SafetyRejected):
            bus.start_action("arm.joint_position", {"position": [0, 0, 0, 0, 0, 50.0]}, authorized=True)
        sim_arm_module.LOWER[5], sim_arm_module.UPPER[5] = -1e6, 1e6
        with self.assertRaises(SafetyRejected):
            bus.start_action("arm.joint_position", {"position": [0, 0, 0, 0, 0, 1000.0]}, authorized=True)
        self.assertIsNone(arm.arm_goal)
        with self.assertRaises(TypeError):
            bus.supervisor.params["arm.joint_position"]["lower"] = [-1e6] * 6
        # The runtime's own manifest copy (served on /v2/manifest, used for
        # compatibility checks) does not follow the adapter's objects either.
        arm_cap = next(c for c in bus.manifest["capabilities"] if c["id"] == "arm.joint_position")
        self.assertEqual(arm_cap["params"]["lower"][5], self.saved[0][5])
        self.assertEqual(arm_cap["params"]["upper"][5], self.saved[1][5])

    def test_nan_limit_comparison_rejects(self):
        bus = CapabilityBus(SimArm(clock=ManualClock()))
        bus.supervisor.params["arm.joint_position"] = {"dof": 6, "lower": [float("nan")] * 6,
                                                        "upper": [float("nan")] * 6, "max_velocity": 1.0}
        with self.assertRaises(SafetyRejected):
            bus.supervisor.check("arm.joint_position", {"position": [0.0] * 6}, True)

    def test_manifest_drift_refuses_motion_until_restart(self):
        fx = RuntimeFixture("arm", time_scale=20.0)
        try:
            self.assertIsNone(fx.runtime.bus.check_manifest_drift())        # unchanged: fine
            sim_arm_module.UPPER[5] = 100.0                                 # adapter "refreshes" its URDF
            self.assertIsNotNone(fx.runtime.bus.check_manifest_drift())
            with self.assertRaises(SafetyRejected) as ctx:
                fx.runtime.bus.start_action("arm.joint_position", {"position": [0.1, 0, 0, 0, 0, 0]},
                                            authorized=True)
            self.assertEqual(ctx.exception.code, "manifest_changed")
            self.assertTrue(fx.runtime.status()["manifest_changed"])
            self.assertTrue(any(e["type"] == "safety.manifest_changed" for e in fx.runtime.events.since(0)))
        finally:
            fx.close()

    def test_monitor_detects_drift(self):
        fx = RuntimeFixture("arm", time_scale=20.0)
        try:
            sim_arm_module.LOWER[0] = -10.0
            wait_until(lambda: fx.runtime.supervisor.manifest_drift, 3)
        finally:
            fx.close()

    def test_skill_param_is_a_plain_copy(self):
        fx = RuntimeFixture("arm")
        try:
            run = runner_module.RunState("skill", "x", True, True, {})
            ctx = SkillContext(run, fx.runtime.bus, {"arm.joint_position"}, {}, fx.runtime.emergency_stop)
            lower = ctx.param("arm.joint_position", "lower")
            self.assertIsInstance(lower, list)
            lower[0] = -100.0
            self.assertEqual(fx.runtime.supervisor.params["arm.joint_position"]["lower"][0], self.saved[0][0])
        finally:
            fx.close()


class ChunkTimingTests(unittest.TestCase):
    """N-06: dt and total chunk duration are bounded."""

    def _sup(self, rate=10.0):
        arm = SimArm(clock=ManualClock())
        manifest = arm.manifest()
        params = {"action_space": "joint_position", "dim": 6}
        if rate is not None:
            params["rate_hz"] = rate
        manifest["capabilities"].append({"id": "policy.action_chunk", "params": params})
        arm.manifest = lambda: manifest
        return CapabilityBus(arm).supervisor

    def _check(self, sup, steps, dt):
        return sup.check("policy.action_chunk", {"actions": [[0.0] * 6] * steps, "dt": dt}, True,
                         {"joint_position": [0.0] * 6})

    def test_dt_upper_bound_from_rate(self):
        sup = self._sup(10.0)
        self._check(sup, 1, 0.2)                     # 2 control periods: allowed
        for dt in (0.21, 1.0, 1e6):
            with self.subTest(dt=dt), self.assertRaises(SafetyRejected):
                self._check(sup, 1, dt)

    def test_dt_upper_bound_without_rate(self):
        sup = self._sup(None)
        self._check(sup, 1, 0.5)
        with self.assertRaises(SafetyRejected):
            self._check(sup, 1, 0.51)

    def test_total_duration_and_steps(self):
        sup = self._sup(10.0)
        self._check(sup, 100, 0.1)                   # 10 s: allowed
        with self.assertRaises(SafetyRejected):
            self._check(sup, 101, 0.1)
        sup = self._sup(1000.0)
        with self.assertRaises(SafetyRejected):
            self._check(sup, 1001, 0.001)


class SurvivingMutantTests(unittest.TestCase):
    def test_pre_dispatch_rechecks_epoch(self):
        """M2: a stop+reset landing between check() and dispatch must still block the command."""
        robot = _Recording(clock=ManualClock())
        bus = CapabilityBus(robot)
        sup = bus.supervisor

        def stop_and_reset_in_the_window():
            sup.emergency_stop("window")
            sup.reset(authorized=True)
        robot.dispatched.clear()
        with self.assertRaises(EStopLatched):
            bus.invoke("base.velocity", {"vx": 0.4, "wz": 0.0}, authorized=True, guard=stop_and_reset_in_the_window)
        self.assertEqual(robot.motion(), [])

    def test_stop_between_reset_check_and_clear_stays_latched(self):
        """M4: a stop landing exactly between reset's check and the clear re-latches."""
        bus = CapabilityBus(SimQuadruped(clock=ManualClock()))
        sup = bus.supervisor
        sup.emergency_stop("first")

        class StopOnClear(threading.Event):
            fired = False

            def clear(self_inner):
                if not StopOnClear.fired:
                    StopOnClear.fired = True
                    sup.emergency_stop("lands between check and clear")
                super().clear()
        latch = StopOnClear()
        latch.set()
        sup._latched = latch
        with self.assertRaises(EStopLatched):
            sup.reset(authorized=True)
        self.assertTrue(StopOnClear.fired)
        self.assertTrue(sup.latched)
        with self.assertRaises(EStopLatched):
            bus.invoke("base.velocity", {"vx": 0.2, "wz": 0.0}, authorized=True)

    def test_stop_during_adapter_reset_leaves_disk_latched(self):
        """A stop landing inside adapter.reset() aborts the reset; disk must end up latched too."""
        adapter = SimQuadruped(time_scale=1.0)
        with tempfile.TemporaryDirectory() as state:
            rt = EdgeRuntime(adapter, state)
            try:
                rt.emergency_stop("first")
                wait_until(lambda: (rt.store.get("safety", "latch") or {}).get("latched"))
                adapter.reset = lambda: rt.emergency_stop("operator pressed stop during reset")
                with self.assertRaises(EStopLatched):
                    rt.reset({"hardware_authorized": True})
                self.assertTrue(rt.supervisor.latched)
                time.sleep(0.5)                              # let the persister run
                self.assertTrue(rt.store.get("safety", "latch")["latched"])
            finally:
                rt.close()
            restarted = EdgeRuntime(SimQuadruped(), state, start_monitor=False)
            try:
                self.assertTrue(restarted.supervisor.latched)   # clean close, still latched from disk
                self.assertNotIn("未正常关闭", restarted.supervisor.latch_reason)
            finally:
                restarted.close()

    def test_reserved_slot_drip_feeder_released_quickly(self):
        """M6: a slow-dripping non-stop connection in a reserved slot is dropped after the short sniff deadline."""
        fx = RuntimeFixture()
        server = GatewayServer(fx.runtime, TOKEN, port=0, max_connections=2, stop_reserved=1,
                               request_timeout=10.0, stop_sniff_timeout=0.5).start()
        socks = []
        try:
            for _ in range(2):                                       # fill ordinary slots
                s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
                s.sendall(b"GET /v1/status HTTP/1.1\r\n")
                socks.append(s)
            wait_until(lambda: server.httpd.connection_counts()["general"] == 2, 2)
            drip = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            socks.append(drip)
            wait_until(lambda: server.httpd.connection_counts()["reserved"] == 1, 2)
            started = time.monotonic()
            for ch in b"GET /v1/st":
                try:
                    drip.sendall(bytes([ch]))
                except OSError:
                    break
                if server.httpd.connection_counts()["reserved"] == 0:
                    break
                time.sleep(0.1)
            wait_until(lambda: server.httpd.connection_counts()["reserved"] == 0, 3)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            for s in socks:
                s.close()
            server.close()
            fx.close()


if __name__ == "__main__":
    unittest.main()
