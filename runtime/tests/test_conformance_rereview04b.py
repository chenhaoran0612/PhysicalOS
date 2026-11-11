"""Regression tests for the round-04 second re-review (docs/evolution/reviews/2026-11-09-iter04-rereview2.md).

* R4b-01 ``reset_hang.py``: the suite's reset call times out, the suite aborts; the abandoned
  ``adapter.reset()`` returns later (and the driver replays its last setpoint). The abort stop is
  counted (keeps the latch reason) and the supervisor treats any stop/halt during ``adapter.reset()``
  as a race: the runtime stays latched, stops the adapter again, the body does not move.
* R4b-02 ``arm_names.py``: a wrong ``joint_names`` declaration (alphabetical / reversed) -- dry
  never sends arm targets or joint_position chunks; any displacement seen in dry aborts with stop.
* R4b-03 ``chunk_limiter.py``: the runtime refuses joint_position chunks without ``joint_names``.
* R4b-04 a driver resuming 2.5 s after the abort stop is caught by the >= 5 s post-abort window.
* R4b-05 a vx command that also turns the base fails.
* R4b-08 a re-stop keeps the operator's latch reason (in-process and over ``/v2/stop``).
"""
import tempfile
import threading
import time
import unittest

import _support  # noqa: F401

from neuxmind_runtime import conformance
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.core import EdgeRuntime
from neuxmind_runtime.errors import RuntimeApiError

from test_conformance_rereview04 import UR_CMD, UR_HOME, URAlphabetical, case, x_after

FAST_RESET = conformance.Thresholds(reset_call_timeout_s=2.0)


def runtime_for(test, adapter):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    runtime = EdgeRuntime(adapter, tmp.name, sandbox="inprocess", telemetry_capacity=None)
    test.addCleanup(runtime.close)
    return runtime


# ------------------------------------------------------------------ R4b-01
class HangingReset(SimQuadruped):
    """reset_hang.py: the ``nth`` reset takes ``hang`` s; when it returns the driver replays the
    last non-zero setpoint (motors re-enabled, last command resumed)."""

    def __init__(self, nth, hang, replay=True):
        super().__init__()
        self.nth, self.hang, self.replay = nth, hang, replay
        self.n_reset = 0
        self.calls = []
        self.returned = threading.Event()
        self._lastsp = (0.0, 0.0, 0.0)

    def invoke(self, cap_id, args):
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz")):
            self._lastsp = (args.get("vx", 0.0), args.get("vy", 0.0), args.get("wz", 0.0))
        return out

    def stop(self):
        self.calls.append("stop")
        return super().stop()

    def reset(self):
        self.n_reset += 1
        self.calls.append("reset")
        if self.n_reset == self.nth:
            time.sleep(self.hang)
            if self.replay and any(self._lastsp):
                with self._lock:
                    self.cmd = self._lastsp
            self.calls.append("reset_done")
            self.returned.set()


class TimedOutResetNeverUnlatches(unittest.TestCase):
    def _run(self, level, nth, hang=5.0):
        adapter = HangingReset(nth, hang)
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        report = conformance.run(target, level, authorized=(level == "motion"), thresholds=FAST_RESET)
        sup = target.runtime.supervisor
        self.assertTrue(adapter.returned.wait(hang + 5))
        time.sleep(0.2)
        return adapter, target, report, sup

    def _assert_safe(self, adapter, report, sup):
        self.assertTrue(report["aborted"])
        self.assertTrue(report["call_timeout"])
        self.assertIs(report["final_latched"], True)
        self.assertIn("硬件急停", report["end_state"])
        self.assertTrue(sup.latched)                                  # before the fix: silently unlatched
        self.assertTrue(sup.latch_reason.startswith("conformance:"))  # the suite's own latch, unchanged
        done = len(adapter.calls) - 1 - adapter.calls[::-1].index("reset_done")
        self.assertIn("stop", adapter.calls[done + 1:], adapter.calls[-6:])   # stopped after reset returned
        self.assertEqual(x_after(adapter, 1.5), 0.0)                  # before the fix: 0.1 m/s, not stopping

    def test_motion_level_reset_timeout_with_replay(self):
        adapter, _target, report, sup = self._run("motion", 4)
        self._assert_safe(adapter, report, sup)

    def test_dry_level_reset_timeout(self):
        adapter, _target, report, sup = self._run("dry", 2)
        self._assert_safe(adapter, report, sup)

    def test_reset_returning_after_the_suite_keeps_the_latch(self):
        """Default-threshold shape: the reset returns only after the suite has returned."""
        adapter = HangingReset(4, 11.0)
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        th = conformance.Thresholds(reset_call_timeout_s=2.0, restop_deadline_s=6.0)
        report = conformance.run(target, "motion", True, thresholds=th)
        self.assertFalse(adapter.returned.is_set())                   # still inside adapter.reset()
        self.assertIs(report["final_latched"], True)
        self.assertIn("reset()", report["end_state"])                 # reported as still in flight
        self.assertTrue(adapter.returned.wait(15))
        time.sleep(0.2)
        sup = target.runtime.supervisor
        self.assertTrue(sup.latched)
        self.assertEqual(x_after(adapter, 1.5), 0.0)
        raced = [e for e in target.runtime.events.since(0, 10 ** 6) if e["type"] == "safety.reset_raced"]
        self.assertTrue(raced)

    def test_supervisor_halt_during_adapter_reset_is_a_race(self):
        adapter = HangingReset(1, 0.5)
        runtime = runtime_for(self, adapter)
        runtime.emergency_stop("operator: person in path")
        count = runtime.supervisor.stop_count
        threading.Thread(target=lambda: (time.sleep(0.1), runtime.supervisor.halt_motion("x")), daemon=True).start()
        with self.assertRaises(RuntimeApiError) as ctx:
            runtime.reset({"hardware_authorized": True})
        self.assertEqual(ctx.exception.code, "estop_latched")
        self.assertTrue(runtime.supervisor.latched)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in path")
        self.assertEqual(runtime.supervisor.stop_count, count)        # a halt is not a counted stop
        self.assertEqual(adapter.calls[-1], "stop")
        runtime.reset({"hardware_authorized": True})                  # a later, clean reset works
        self.assertFalse(runtime.supervisor.latched)

    def test_counted_restop_keeps_the_reason(self):
        runtime = runtime_for(self, SimQuadruped())
        runtime.emergency_stop("operator: person in path")
        count = runtime.supervisor.stop_count
        out = conformance.RuntimeTarget(runtime).restop("conformance:x:abort", counted=True)
        self.assertTrue(out["stopped"])
        self.assertEqual(runtime.supervisor.stop_count, count + 1)
        self.assertEqual(runtime.supervisor.latch_reason, "operator: person in path")


# ------------------------------------------------------------------ R4b-02
class WrongJointNames(URAlphabetical):
    """arm_names.py: the manifest's joint_names are wrong (alphabetical or reversed)."""

    def __init__(self, names, chunk=True):
        super().__init__(declare="custom", chunk=chunk)
        self.names = names

    def manifest(self):
        m = super().manifest()
        for c in m["capabilities"]:
            if c["id"] == "arm.joint_position":
                c["params"]["joint_names"] = list(self.names)
        return m


class DrySendsNoArmTargets(unittest.TestCase):
    def test_wrong_declarations_send_nothing_and_move_nothing(self):
        for label, names in (("alpha", sorted(UR_CMD)), ("reversed", list(reversed(UR_CMD))), ("right", list(UR_CMD))):
            with self.subTest(label):
                adapter = WrongJointNames(names)
                report = conformance.run_adapter(adapter, "dry")
                self.assertEqual(adapter.arm_calls(), [])
                self.assertEqual(adapter.max_dev, 0.0)
                self.assertEqual(adapter.q, UR_HOME)
                for cid in ("dry.arm.joint_position.zero_displacement", "dry.policy.action_chunk.zero_chunk"):
                    c = case(report, cid)
                    self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "not_applicable"), c)
                    self.assertTrue(c["evidence"]["dry_skipped"])
                    self.assertIn("dry 级不向适配器下发机械臂目标", c["reason"])
                for cid in ("dry.arm.joint_position.rejects_out_of_limits", "dry.policy.action_chunk.rejects_bad_dt"):
                    self.assertEqual(case(report, cid)["status"], "passed")
                self.assertIs(report["dry_ok"], True)
                self.assertEqual(report["dry_guard"]["violations"], [])

    def test_guard_refuses_any_arm_target_in_dry(self):
        class Leaky(conformance.Suite):
            def _dry_arm_zero(self, ev):
                self.target.start_action("arm.joint_position", {"position": self._arm_current()}, True)
        adapter = WrongJointNames(list(UR_CMD), chunk=False)
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        report = Leaky(target, "dry").run()
        self.assertEqual(case(report, "dry.arm.joint_position.zero_displacement")["status"], "failed")
        self.assertTrue(report["dry_guard"]["violations"])
        self.assertEqual(adapter.arm_calls(), [])

    def test_motion_still_runs_with_a_correct_declaration(self):
        adapter = WrongJointNames(list(UR_CMD))
        report = conformance.run_adapter(adapter, "motion", True)
        self.assertIs(report["motion_ok"], True, [c["id"] for c in report["cases"] if c["status"] == "failed"])
        self.assertLessEqual(adapter.max_dev, 0.1 + 1e-6)


class CreepsOnZeroVelocity(SimQuadruped):
    """A driver that drives off a little on a zero-velocity setpoint (a dry case that moves)."""

    def invoke(self, cap_id, args):
        if cap_id == "base.velocity" and not any(args.get(k, 0.0) for k in ("vx", "vy", "wz")):
            with self._lock:
                self.x += 0.05
        return super().invoke(cap_id, args)


class DryDisplacementAborts(unittest.TestCase):
    def test_displacement_in_dry_stops_and_aborts(self):
        adapter = CreepsOnZeroVelocity()
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        report = conformance.run(target, "dry")
        self.assertTrue(report["aborted"])
        self.assertIn("dry 级观察到本体位移", report["aborted"])
        self.assertIs(report["final_latched"], True)
        self.assertTrue(target.runtime.supervisor.latched)
        c = case(report, "dry.base.velocity.zero_setpoint")
        self.assertEqual(c["status"], "failed")
        self.assertTrue(c["evidence"]["dry_displacement"])
        later = [x for x in report["cases"] if x["level"] == "dry" and x["status"] == "skipped"
                 and x.get("skip_kind") == "blocked"]
        self.assertTrue(later)                                        # nothing else was sent


# ------------------------------------------------------------------ R4b-03
class RuntimeChunkFailsClosed(unittest.TestCase):
    def test_joint_chunk_without_joint_names_is_refused(self):
        adapter = URAlphabetical(declare=None, chunk=True)
        runtime = runtime_for(self, adapter)
        row = list(UR_HOME)
        row[0] = 1.5
        try:
            out = runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [row], "dt": 0.1},
                                                          "hardware_authorized": True})
            record = runtime.runner.wait(out["run_id"], 5.0)
            error = record.get("error") if isinstance(record, dict) else getattr(record, "error", None)
        except RuntimeApiError as exc:
            error = str(exc)
        self.assertIn("joint_names", str(error))
        self.assertEqual(adapter.arm_calls(), [])
        self.assertEqual(adapter.max_dev, 0.0)

    def test_declared_names_still_limit_per_step(self):
        adapter = URAlphabetical(declare="ur", chunk=True)
        runtime = runtime_for(self, adapter)
        row = list(UR_HOME)
        row[0] = 1.5
        out = runtime.invoke("policy.action_chunk", {"request_id": "c1", "args": {"actions": [row], "dt": 0.1},
                                                      "hardware_authorized": True})
        runtime.runner.wait(out["run_id"], 5.0)
        sent = [c[2]["actions"][0] for c in adapter.arm_calls() if c[1] == "policy.action_chunk"]
        self.assertEqual(len(sent), 1)
        self.assertAlmostEqual(sent[0][0], 0.1, places=6)             # 1 rad/s * 0.1 s
        self.assertEqual(sent[0][1:], UR_HOME[1:])


# ------------------------------------------------------------------ R4b-04
class ResumesOnceAfterAbort(SimQuadruped):
    """restop2.py twice 2.5: after the stop that follows a failed case, the driver resumes the last
    setpoint 2.5 s later (outside the old 2 s window), once."""

    def __init__(self):
        super().__init__()
        self._lastsp = (0.0, 0.0, 0.0)
        self.armed = False

    def invoke(self, cap_id, args):
        out = super().invoke(cap_id, args)
        if cap_id == "base.velocity" and any(abs(args.get(k, 0.0)) > 0 for k in ("vx", "vy", "wz")):
            self._lastsp = (args["vx"], args.get("vy", 0.0), args["wz"])
            self.armed = True
        return out

    def stop(self):
        super().stop()
        if self.armed and any(self._lastsp):
            self.armed = False
            last = self._lastsp

            def later():
                time.sleep(2.5)
                with self._lock:
                    self.cmd = last
            threading.Thread(target=later, daemon=True).start()


class LateResumeIsCaught(unittest.TestCase):
    def test_resume_after_2_5_s_is_stopped_and_reported(self):
        class Suite(conformance.Suite):
            def _motion_velocity_stop(self, ev):                      # fail right after a moving case
                super()._motion_velocity_stop(ev)
                raise conformance._Fail("forced failure")
        adapter = ResumesOnceAfterAbort()
        target = conformance.AdapterTarget(adapter)
        self.addCleanup(target.close)
        report = Suite(target, "motion", True).run()
        fm = report["final_motion"]
        self.assertTrue(report["aborted"])
        self.assertGreaterEqual(fm["held_still_s"], 5.0 - 1e-3, fm)
        self.assertEqual(fm.get("final_recheck"), "still")
        self.assertIn("观察窗", report["end_state"])
        self.assertEqual(x_after(adapter, 3.0), 0.0)                  # before the fix: 0.45 m in 5 s


# ------------------------------------------------------------------ R4b-05
class TurnsOnVx(SimQuadruped):
    def invoke(self, cap_id, args):
        if cap_id == "base.velocity":
            args = dict(args)
            if args.get("vx"):
                args["wz"] = 5.0 * args["vx"]
        return super().invoke(cap_id, args)


class VxMustNotTurn(unittest.TestCase):
    def test_vx_with_yaw_fails(self):
        report = conformance.run_adapter(TurnsOnVx(), "motion", True)
        self.assertIs(report["motion_ok"], False)
        failed = [c for c in report["cases"] if c["status"] == "failed"]
        self.assertTrue(any("wz" in c["reason"] or "转向" in c["reason"] for c in failed), failed)


# ------------------------------------------------------------------ R4b-08
class HttpRestopKeepsReason(unittest.TestCase):
    def test_v2_stop_keep_reason(self):
        from _support import RuntimeFixture, http
        fx = RuntimeFixture(http=True)
        self.addCleanup(fx.close)
        self.assertEqual(http(fx.url, "/v2/stop", {"reason": "operator: person in path"})[0], 200)
        count = fx.runtime.supervisor.stop_count
        target = conformance.HttpTarget(fx.url, _token())
        target.restop("conformance:x:abort", counted=True)
        self.assertTrue(fx.runtime.supervisor.latched)
        self.assertEqual(fx.runtime.supervisor.latch_reason, "operator: person in path")
        self.assertEqual(fx.runtime.supervisor.stop_count, count + 1)
        # without keep_reason the reason is annotated as before
        http(fx.url, "/v2/stop", {"reason": "second"})
        self.assertEqual(fx.runtime.supervisor.latch_reason, "second")


def _token():
    from _support import TOKEN
    return TOKEN


if __name__ == "__main__":
    unittest.main()
