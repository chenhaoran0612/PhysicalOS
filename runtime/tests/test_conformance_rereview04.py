"""Regression tests for the round-04 re-review (docs/evolution/reviews/2026-11-09-iter04-rereview.md).

Each scenario re-creates one of the re-review's reproduction scripts (scratchpad/rereview04/):

* R4-01 ``ur_pose.py``: an honest arm whose ``joint.state`` is in alphabetical order. The suite
  never infers the command order from ``joint.state``: only the manifest's
  ``arm.joint_position.params.joint_names`` (spec v1.1) maps joints; without it (or when the
  declared names are not in ``joint.state``) every arm / joint-chunk case is blocked and nothing
  reaches the adapter. The dry guard maps by the declared names, independently of the suite.
* R4-02 ``restop.py``: motion seen after the suite stopped (latched or not) is answered with
  repeated, bounded stops; the report says truthfully whether the body was still at the end.
* R4-03 ``race.py in_adapter_reset_replay``: a stop arriving during ``adapter.reset()`` makes the
  supervisor stop the adapter again after the reset returns, and keep the latch.
* R4-04 wz / vy are exercised and checked like vx.
* R4-05 a hung adapter call fails the case, aborts, keeps stop available and releases the lease.
"""
import copy
import tempfile
import threading
import time
import unittest

import _support  # noqa: F401

from neuxmind_runtime import conformance, scaffold
from neuxmind_runtime.adapters._sim import SimGoal, SimHandle
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import RuntimeApiError

UR_CMD = ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint", "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"]
UR_HOME = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
ALPHA = sorted(range(6), key=lambda i: UR_CMD[i])       # joint.state index -> command index
FAST = conformance.Thresholds(adapter_call_timeout_s=1.0, reset_call_timeout_s=2.0)


def case(report, cid):
    return next(c for c in report["cases"] if c["id"] == cid)


def blocked(report, prefix):
    return [c for c in report["cases"] if c["id"].startswith(prefix)]


class Rec:
    """Mixin recording what reaches the adapter (after the dry guard) and physical joint travel."""

    def _rec(self, *item):
        self.__dict__.setdefault("calls", []).append(item)

    def invoke(self, cap_id, args):
        self._rec("invoke", cap_id, dict(args))
        return super().invoke(cap_id, args)

    def start_action(self, cap_id, args):
        self._rec("start_action", cap_id, copy.deepcopy(args))
        return super().start_action(cap_id, args)

    def stop(self):
        self._rec("stop", None, None)
        return super().stop()

    def reset(self):
        self._rec("reset", None, None)
        return super().reset()

    def arm_calls(self):
        return [c for c in getattr(self, "calls", []) if c[1] in ("arm.joint_position", "policy.action_chunk")]


class URAlphabetical(Rec, SimArm):
    """ur_pose.py: honest UR-like arm, /joint_states in alphabetical order, UR home pose. ``declare``
    selects what the manifest says about joint names."""

    def __init__(self, declare="ur", chunk=False):
        super().__init__()
        self.q = list(UR_HOME)
        self.declare, self.chunk = declare, chunk
        self.q0 = list(UR_HOME)
        self.max_dev = 0.0

    def manifest(self):
        m = super().manifest()
        for c in m["capabilities"]:
            if c["id"] == "arm.joint_position":
                if self.declare == "ur":
                    c["params"]["joint_names"] = list(UR_CMD)
                elif self.declare is None:
                    c["params"].pop("joint_names", None)
                # "sim": SimArm's own joint1..6, which this body does not publish
        if self.chunk:
            m["capabilities"].append({"id": "policy.action_chunk",
                                      "params": {"action_space": "joint_position", "dim": 6, "rate_hz": 10.0}})
        return m

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "joint.state":
            return {"names": [UR_CMD[i] for i in ALPHA], "position": [out["position"][i] for i in ALPHA],
                    "velocity": [out["velocity"][i] for i in ALPHA]}
        return out

    def start_action(self, cap_id, args):
        if cap_id == "policy.action_chunk":
            self._rec("start_action", cap_id, copy.deepcopy(args))
            with self._lock:
                self.q = list(args["actions"][-1])
                goal = SimGoal(cap_id, None)
                goal.finish({"executed": len(args["actions"])})
            self._track()
            return SimHandle(self, goal)
        return super().start_action(cap_id, args)

    def _track(self):
        self.max_dev = max(self.max_dev, max(abs(a - b) for a, b in zip(self.q, self.q0)))

    def _step(self, dt):
        super()._step(dt)
        self._track()


class JointNamesMapping(unittest.TestCase):
    """R4-01."""

    def test_ur_pose_repro_nothing_sent_nothing_moves(self):
        """ur_pose.py exactly: SimArm's manifest (joint1..6) on a body publishing UR names in
        alphabetical order -> no declared name can be found -> blocked, 0 arm commands."""
        adapter = URAlphabetical(declare="sim")
        report = conformance.run_adapter(adapter, "dry")
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(adapter.q, UR_HOME)
        self.assertEqual(adapter.max_dev, 0.0)
        for cid in ("dry.arm.joint_position.zero_displacement", "dry.arm.joint_position.rejects_out_of_limits"):
            c = case(report, cid)
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"), c)
            self.assertIn("joint_names", c["reason"])
        self.assertIs(report["dry_ok"], False)
        self.assertEqual(report["dry_guard"]["violations"], [])

    def test_without_joint_names_every_arm_and_chunk_case_is_blocked(self):
        adapter = URAlphabetical(declare=None, chunk=True)
        report = conformance.run_adapter(adapter, "dry")
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(adapter.max_dev, 0.0)
        for cid in ("dry.arm.joint_position.zero_displacement", "dry.arm.joint_position.rejects_out_of_limits",
                    "dry.policy.action_chunk.zero_chunk", "dry.policy.action_chunk.rejects_bad_dt",
                    "dry.safety.stop.stop_epoch"):
            c = case(report, cid)
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"), c)
            self.assertIn("joint_names", c["reason"])
        static = case(report, "static.arm.joint_position.declaration")
        self.assertEqual(static["status"], "passed")              # optional param: static stays valid
        self.assertIn("joint_names", static["evidence"]["note"])
        # motion is never attempted on a blocked dry level
        adapter = URAlphabetical(declare=None, chunk=True)
        report = conformance.run_adapter(adapter, "motion", True)
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(adapter.max_dev, 0.0)
        self.assertIs(report["motion_ok"], False)

    def test_declared_names_map_alphabetical_joint_state(self):
        """With joint_names declared, dry passes -- and still sends no arm target or joint chunk
        (R4b-02: dry only proves request validation / refusal)."""
        adapter = URAlphabetical(declare="ur", chunk=True)
        report = conformance.run_adapter(adapter, "dry")
        self.assertIs(report["dry_ok"], True, [c for c in report["cases"] if c["status"] not in ("passed", "skipped")])
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(case(report, "dry.arm.joint_position.zero_displacement")["skip_kind"], "not_applicable")
        self.assertEqual(case(report, "dry.policy.action_chunk.zero_chunk")["skip_kind"], "not_applicable")
        self.assertEqual(adapter.max_dev, 0.0)
        self.assertEqual(report["dry_guard"]["violations"], [])

    def test_motion_moves_only_the_wrist(self):
        adapter = URAlphabetical(declare="ur")
        report = conformance.run_adapter(adapter, "motion", True)
        self.assertIs(report["motion_ok"], True, [c["id"] for c in report["cases"] if c["status"] != "passed"])
        for target in [c[2]["position"] for c in adapter.arm_calls()]:
            self.assertEqual(target[:5], UR_HOME[:5])              # only wrist_3 (command index 5) moves
            self.assertLessEqual(abs(target[5] - UR_HOME[5]), 0.1 + 1e-9)
        self.assertLessEqual(adapter.max_dev, 0.1 + 1e-6)

    def test_stable_reorder_mid_run_is_mapped_by_name(self):
        """arm_names_flip: after 3 reads the driver swaps two joints in joint.state, consistently."""
        class Flip(URAlphabetical):
            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "joint.state":
                    self.n = getattr(self, "n", 0) + 1
                    if self.n > 3:
                        idx = [1, 0, 2, 3, 4, 5]
                        return {k: [out[k][i] for i in idx] for k in ("names", "position", "velocity")}
                return out
        adapter = Flip(declare="ur")
        report = conformance.run_adapter(adapter, "dry")
        for target in [c[2]["position"] for c in adapter.arm_calls()]:
            self.assertEqual(target, UR_HOME)
        self.assertEqual(adapter.max_dev, 0.0)
        self.assertEqual(report["dry_guard"]["violations"], [])

    def test_guard_refuses_arm_targets_whatever_the_suite_infers(self):
        """A suite that (wrongly) infers the order from joint.state and sends a dry arm target is
        refused by the guard (R4b-02: the guard refuses every dry arm target)."""
        class InferringSuite(conformance.Suite):
            def _dry_arm_zero(self, ev):
                self._arm_joint_map()
                q = [float(v) for v in self._joints()["position"]]          # the old R4-01 inference
                self.target.start_action("arm.joint_position", {"position": q}, True)
        adapter = URAlphabetical(declare="ur")
        target = conformance.AdapterTarget(adapter)
        try:
            report = InferringSuite(target, "dry").run()
        finally:
            target.close()
        c = case(report, "dry.arm.joint_position.zero_displacement")
        self.assertEqual(c["status"], "failed", c)
        self.assertIn("套件缺陷", c["reason"])
        self.assertTrue(report["dry_guard"]["violations"])
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(adapter.max_dev, 0.0)

    def test_declared_names_missing_mid_run_aborts(self):
        class Vanish(URAlphabetical):
            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "joint.state":
                    self.n = getattr(self, "n", 0) + 1
                    if self.n > 4:
                        out = {k: v[1:] for k, v in out.items()}
                return out
        adapter = Vanish(declare="ur")
        report = conformance.run_adapter(adapter, "dry")
        self.assertIs(report["dry_ok"], False)
        self.assertEqual(adapter.max_dev, 0.0)
        for target in [c[2]["position"] for c in adapter.arm_calls()]:
            self.assertEqual(target, UR_HOME)

    def test_runtime_chunk_limiter_maps_by_name(self):
        """The supervisor's per-step chunk limit starts from joint.state mapped by joint_names."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = URAlphabetical(declare="ur", chunk=True)
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        row = list(UR_HOME)
        row[5] += 0.05                         # 0.05 rad on wrist_3 only; vmax*dt = 0.1 rad
        out = runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [row], "dt": 0.1},
                                                      "hardware_authorized": True})
        rec = runtime.runner.wait(out["run_id"], 5.0) if hasattr(runtime.runner, "wait") else None
        sent = [c[2]["actions"] for c in adapter.arm_calls() if c[1] == "policy.action_chunk"]
        self.assertEqual(sent, [[row]], rec)   # by-order mapping would clamp shoulder/elbow by 1.57 rad


# ------------------------------------------------------------------ R4-02
class StopResumeVelocity(SimQuadruped):
    """restop.py stop_resume_velocity_1.3: every stop() is followed, 1.3 s later, by the driver
    re-applying the last non-zero setpoint (ignores the runtime's latch)."""
    DELAY = 1.3

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._lastcmd = (0.0, 0.0, 0.0)

    def invoke(self, cap_id, args):
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz")):
            self._lastcmd = (args["vx"], args.get("vy", 0.0), args["wz"])
        return out

    def stop(self):
        super().stop()
        last = self._lastcmd
        if any(last):
            def later():
                time.sleep(self.DELAY)
                with self._lock:
                    self.cmd = last
            threading.Thread(target=later, daemon=True).start()


class StopResumeBounded(StopResumeVelocity):
    """Resumes after each of its first ``LIMIT`` stops only: one or two extra stops are not enough,
    only the repeated stops of the post-abort check bring it to rest."""
    LIMIT = 4

    def stop(self):
        if any(self._lastcmd):
            self.n_stops = getattr(self, "n_stops", 0) + 1
        if getattr(self, "n_stops", 0) > self.LIMIT:
            self._lastcmd = (0.0, 0.0, 0.0)
        super().stop()


class NavCancelResume25(SimQuadruped):
    """restop.py nav_cancel_resume_2.5: a cancelled goal silently resumes 2.5 s later."""

    def start_action(self, cap_id, args):
        handle = super().start_action(cap_id, args)
        adapter, goal = self, handle._goal

        class H(SimHandle):
            def cancel(inner):
                super().cancel()
                saved = SimGoal(goal.kind, goal.target, **goal.extra)

                def resume():
                    time.sleep(2.5)
                    with adapter._lock:
                        if adapter.goal is None:
                            adapter.goal = saved
                threading.Thread(target=resume, daemon=True).start()
        return H(self, goal)


def x_after(adapter, seconds):
    t0 = time.monotonic()
    adapter.advance()
    x0 = adapter.x
    while time.monotonic() - t0 < seconds:
        adapter.advance()
        time.sleep(0.05)
    return abs(adapter.x - x0)


class MotionAfterStopIsStoppedAgain(unittest.TestCase):
    def test_body_that_resumes_after_every_stop_is_reported_moving(self):
        adapter = StopResumeVelocity()
        target = conformance.AdapterTarget(adapter)
        try:
            report = conformance.run(target, "motion", True)
            fm = report["final_motion"]
            self.assertTrue(report["aborted"])
            self.assertIs(fm["still"], False, fm)
            self.assertGreaterEqual(fm["restops"], 2)
            self.assertIn("警告", report["end_state"])
            self.assertIn("不能确认本体已停下", report["aborted"])
            self.assertNotIn("已停车", report["aborted"])
            self.assertIn("硬件急停", conformance.summarize(report))
            self.assertTrue(report["final_latched"])
        finally:
            target.close()

    def test_repeated_stops_bring_a_resuming_driver_to_rest(self):
        adapter = StopResumeBounded()
        target = conformance.AdapterTarget(adapter)
        try:
            report = conformance.run(target, "motion", True)
            fm = report["final_motion"]
            self.assertTrue(report["aborted"])
            self.assertGreaterEqual(fm["motion_seen"], 1, fm)
            self.assertGreaterEqual(fm["restops"], 1, fm)
            self.assertIs(fm["still"], True, fm)
            self.assertEqual(x_after(adapter, 3.0), 0.0)
        finally:
            target.close()

    def test_resume_found_while_latched_is_stopped_and_stays_stopped(self):
        adapter = NavCancelResume25()
        target = conformance.AdapterTarget(adapter)
        try:
            report = conformance.run(target, "motion", True)
            self.assertTrue(report["aborted"])
            fm = report["final_motion"]
            self.assertIs(fm["still"], True, fm)
            self.assertIn("观察窗内持续静止", report["end_state"])
            self.assertTrue(target.runtime.supervisor.latched)
            self.assertEqual(x_after(adapter, 3.0), 0.0)          # before the fix: 0.171 m
        finally:
            target.close()

    def test_restop_keeps_an_operators_latch_reason_and_count(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = EdgeRuntime(SimQuadruped(), tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        runtime.emergency_stop("operator: person in path")
        count = runtime.supervisor.stop_count
        out = conformance.RuntimeTarget(runtime).restop("conformance:x:restop")
        self.assertTrue(out["stopped"])
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in path")
        self.assertEqual(runtime.supervisor.stop_count, count)
        self.assertTrue(runtime.supervisor.latched)


# ------------------------------------------------------------------ R4-03
class ReplayOnReset(Rec, SimQuadruped):
    """race.py in_adapter_reset_replay: reset() takes 0.3 s; the operator presses stop 0.1 s into
    it; when the reset completes the driver re-applies the last setpoint."""

    def __init__(self, runtime_ref):
        super().__init__()
        self.rt = runtime_ref
        self.fire_on = None
        self._lastcmd = (0.0, 0.0, 0.0)

    def invoke(self, cap_id, args):
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz")):
            self._lastcmd = (args["vx"], args.get("vy", 0.0), args["wz"])
        return out

    def reset(self):
        self._rec("reset", None, None)
        self.n_reset = getattr(self, "n_reset", 0) + 1
        if self.fire_on is not None and self.n_reset == self.fire_on:
            threading.Thread(target=lambda: (time.sleep(0.1),
                                             self.rt[0].emergency_stop("operator: person in path", source="api")),
                             daemon=True).start()
            time.sleep(0.3)
        if any(self._lastcmd):
            with self._lock:
                self.cmd = self._lastcmd
        self._rec("reset_done", None, None)


class ResetStopRace(unittest.TestCase):
    def test_supervisor_stops_again_after_a_raced_reset(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ref = [None]
        adapter = ReplayOnReset(ref)
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        ref[0] = runtime
        self.addCleanup(runtime.close)
        runtime.invoke("base.velocity", {"request_id": "v1", "args": {"vx": 0.1, "wz": 0.0, "timeout_s": 0.2},
                                         "hardware_authorized": True})
        runtime.emergency_stop("first")
        adapter.fire_on = adapter.n_reset + 1 if hasattr(adapter, "n_reset") else 1
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True})
        self.assertEqual(ctx.exception.code, "estop_latched")
        self.assertTrue(runtime.supervisor.latched)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in path")
        kinds = [c[0] for c in adapter.calls]
        last_done = len(kinds) - 1 - kinds[::-1].index("reset_done")
        self.assertIn("stop", kinds[last_done + 1:], kinds[-6:])       # stop after the reset returned
        self.assertEqual(adapter.cmd, (0.0, 0.0, 0.0))
        self.assertEqual(x_after(adapter, 1.0), 0.0)
        raced = [e for e in runtime.events.since(0, 10 ** 6) if e["type"] == "safety.reset_raced"]
        self.assertTrue(raced)

    def test_suite_level_repro(self):
        """race.py in_adapter_reset_replay 4: before the fix the body drove 0.316 m after the suite."""
        ref = [None]
        adapter = ReplayOnReset(ref)
        adapter.fire_on = 4
        target = conformance.AdapterTarget(adapter)
        target.start_runtime()
        ref[0] = target.runtime
        try:
            report = conformance.run(target, "motion", True)
            self.assertTrue(report["aborted"])
            self.assertTrue(report["final_latched"])
            self.assertEqual(target.runtime.supervisor.latch_reason, "operator: person in path")
            self.assertEqual(adapter.cmd, (0.0, 0.0, 0.0))
            self.assertEqual(x_after(adapter, 3.0), 0.0)
        finally:
            target.close()


# ------------------------------------------------------------------ R4-04
class WzReversedX5(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and "wz" in args:
            args = dict(args, wz=-5.0 * args["wz"])
        return super().invoke(cap_id, args)


class VyLeak(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity":
            args = dict(args, vy=args.get("vy", 0.0) + 0.4 * args.get("vx", 0.0))
        return super().invoke(cap_id, args)


class VyReversed(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and "vy" in args:
            args = dict(args, vy=-args["vy"])
        return super().invoke(cap_id, args)


class NonHolonomic(SimQuadruped):
    def manifest(self):
        m = super().manifest()
        for c in m["capabilities"]:
            if c["id"] == "base.velocity":
                c["params"].update(holonomic=False, max_vy=0.0)
        return m


class VelocityAxes(unittest.TestCase):
    def test_sim_passes_turn_and_lateral(self):
        report = conformance.run_adapter(SimQuadruped(), "motion", True)
        for name in ("turn", "lateral", "watchdog"):
            c = case(report, f"motion.base.velocity.{name}")
            self.assertEqual(c["status"], "passed", c)
        self.assertGreater(case(report, "motion.base.velocity.turn")["evidence"]["turned_rad"], 0.0)

    def test_liars_fail(self):
        for cls, cid, needle in ((WzReversedX5, "motion.base.velocity.turn", "wz"),
                                 (VyLeak, "motion.base.velocity.watchdog", "侧向"),
                                 (VyReversed, "motion.base.velocity.lateral", "vy")):
            with self.subTest(body=cls.__name__):
                report = conformance.run_adapter(cls(), "motion", True)
                c = case(report, cid)
                self.assertEqual(c["status"], "failed", c)
                self.assertIn(needle, c["reason"])
                self.assertIs(report["motion_ok"], False)
                self.assertTrue(report["final_latched"])

    def test_lateral_not_applicable_on_a_non_holonomic_base(self):
        report = conformance.run_adapter(NonHolonomic(), "motion", True)
        c = case(report, "motion.base.velocity.lateral")
        self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "not_applicable"), c)
        self.assertEqual(case(report, "motion.base.velocity.turn")["status"], "passed")


# ------------------------------------------------------------------ R4-05
class HangingOdometry(SimQuadruped):
    HANG_S = 20.0

    def __init__(self, *a, after=15, **kw):
        super().__init__(*a, **kw)
        self.after, self.n_odom, self.hung = after, 0, threading.Event()

    def read(self, cap_id):
        if cap_id == "base.odometry":
            self.n_odom += 1
            if self.n_odom == self.after and not self.hung.is_set():
                self.hung.set()
                time.sleep(self.HANG_S)
        return super().read(cap_id)


class HungAdapterCalls(unittest.TestCase):
    def test_hung_read_fails_aborts_and_keeps_stop_available(self):
        adapter = HangingOdometry()
        target = conformance.AdapterTarget(adapter)
        try:
            t0 = time.monotonic()
            box = {}
            th = threading.Thread(target=lambda: box.update(r=conformance.run(target, "dry", thresholds=FAST)))
            th.start()
            self.assertTrue(adapter.hung.wait(15))
            s0 = time.monotonic()
            target.runtime.emergency_stop("operator during hang", source="api")     # stop never blocked
            self.assertLess(time.monotonic() - s0, 0.5)
            th.join(15)
            self.assertFalse(th.is_alive())
            report = box["r"]
            self.assertLess(time.monotonic() - t0, 15.0)           # before the fix: hang + 5 s
            self.assertTrue(report["aborted"])
            self.assertTrue(report["call_timeout"])
            self.assertTrue(report["final_latched"])
            self.assertIn("超时", "".join(c["reason"] for c in report["cases"]) + report["aborted"])
            self.assertIs(report["final_motion"]["still"], None)
        finally:
            target.close()

    def test_server_mode_releases_the_lease_promptly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = HangingOdometry()
        runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
        self.addCleanup(runtime.close)
        t0 = time.monotonic()
        cid = runtime.start_conformance({"request_id": "cf-hang", "level": "dry"})["conformance_id"]
        self.assertTrue(adapter.hung.wait(15))
        record = runtime.conformance.wait(cid, 20.0)
        took = time.monotonic() - t0
        self.assertEqual(record["status"], "completed")
        self.assertLess(took, 15.0)                                 # before the fix: 65 s for a 60 s hang
        self.assertIsNone(runtime.conformance_lease)
        self.assertTrue(record["report"]["aborted"])
        runtime.reset({"hardware_authorized": True})               # the operator can act again
        self.assertFalse(runtime.supervisor.latched)


# ------------------------------------------------------------------ R4-06 / R4-07
class ScaffoldNames(unittest.TestCase):
    BASE = {"name": "tiny", "vendor": "V", "model": "M", "family": "custom",
            "capabilities": [{"id": "safety.stop"}, {"id": "system.health"}]}

    def test_stdlib_named_package_is_refused(self):
        for name in ("json", "typing", "asyncio", "socket"):
            with self.subTest(name=name), self.assertRaises(scaffold.ScaffoldError):
                scaffold.normalize_spec(dict(self.BASE, name=name))

    def test_markdown_link_in_text_is_refused(self):
        with self.assertRaises(scaffold.ScaffoldError):
            scaffold.normalize_spec(dict(self.BASE, vendor="Acme [官方驱动下载](http://evil.example/x)"))


if __name__ == "__main__":
    unittest.main()
