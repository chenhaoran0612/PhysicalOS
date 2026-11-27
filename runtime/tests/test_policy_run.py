"""Issue #67 (spec 1.3): ``policy.run`` through the capability bus.

A body that has trained skill models declares their names; the runtime starts one of them by name. What
this shows is the supervisor's part: authorization, only a declared model, the stop and its latch, the
filled limits on a real device. The motion inside a model is the body's own; nothing here bounds it.
"""
import unittest

import _support  # noqa: F401

from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest, EStopLatched, NotAuthorized, SafetyRejected
from neuxmind_runtime.hal import ActionHandle, ActionStatus, RobotAdapter
from neuxmind_runtime.runner import ACTION_DEFAULT_TIMEOUT_S

POLICIES = ["bow", "kick"]
STEPS = 5


class _Run(ActionHandle):
    def __init__(self, body, args):
        self.body, self.args, self.left, self.cancelled = body, args, STEPS, False

    def poll(self):
        if self.cancelled:
            return ActionStatus(state="cancelled", error="stopped")
        if self.left > 0:
            self.left -= 1
            return ActionStatus(progress=1 - self.left / STEPS)
        return ActionStatus(state="succeeded", progress=1.0,
                            result={"completed": True, "name": self.args["name"], "steps": STEPS, "duration_s": 0.1 * STEPS})

    def cancel(self):
        self.cancelled = True


class Body(RobotAdapter):
    """A body with two trained skill models and nothing else that moves."""

    def __init__(self, simulated=True, policies=POLICIES):
        self.clock = ManualClock()
        self.simulated, self.policies = simulated, policies
        self.started, self.stops = [], 0

    def manifest(self):
        return {"schema": "robot.adapter.v2", "spec_version": "1.3", "instance_id": "body-1", "name": "Body",
                "vendor": "Acme", "model": "B1", "family": "humanoid", "adapter_version": "0.1.0", "simulated": self.simulated,
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}}, {"id": "system.health"},
                                 {"id": "policy.run", "params": {"policies": list(self.policies), "max_duration_s": 30.0}}],
                "model_runtimes": [], "safety": {"hardware_estop": False}}

    def read(self, cap_id):
        return {"ok": True, "mode": "stand", "errors": {}}

    def invoke(self, cap_id, args):
        self.stop()
        return {"stopped": True}

    def start_action(self, cap_id, args):
        run = _Run(self, dict(args))
        self.started.append(run)
        return run

    def stop(self):
        self.stops += 1
        for run in self.started:
            run.cancel()


def events_of(bus, type_):
    return [e for e in bus.events.since(0) if e["type"] == type_]


class RunATrainedModel(unittest.TestCase):
    def setUp(self):
        self.body = Body()
        self.bus = CapabilityBus(self.body)

    def finish(self, handle):
        status = handle.poll()
        for _ in range(STEPS + 2):
            if status.done:
                break
            status = handle.poll()
        return status

    def test_runs_a_declared_model_to_its_end(self):
        status = self.finish(self.bus.start_action("policy.run", {"name": "bow"}, authorized=True))
        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.result, {"completed": True, "name": "bow", "steps": STEPS, "duration_s": 0.5})

    def test_the_seed_reaches_the_body(self):
        self.bus.start_action("policy.run", {"name": "kick", "seed": 1700003000}, authorized=True)
        self.assertEqual(self.body.started[-1].args, {"name": "kick", "seed": 1700003000})

    def test_needs_authorization(self):
        with self.assertRaises(NotAuthorized):
            self.bus.start_action("policy.run", {"name": "bow"}, authorized=False)
        self.assertEqual(self.body.started, [])

    def test_a_model_the_body_did_not_declare_is_refused_before_the_body(self):
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("policy.run", {"name": "backflip"}, authorized=True)
        self.assertEqual(self.body.started, [])
        self.assertTrue(events_of(self.bus, "safety.rejected"))

    def test_anything_but_a_name_and_a_seed_is_refused(self):
        for bad in ({}, {"name": "bow", "speed": 3.0}, {"name": "bow", "seed": -1}, {"name": ["bow"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(BadRequest):
                    self.bus.start_action("policy.run", bad, authorized=True)
        self.assertEqual(self.body.started, [])

    def test_stop_cancels_it_and_the_latch_refuses_the_next(self):
        handle = self.bus.start_action("policy.run", {"name": "bow"}, authorized=True)
        self.bus.supervisor.emergency_stop("test")
        self.assertEqual(handle.poll().state, "cancelled")
        self.assertGreaterEqual(self.body.stops, 1)
        with self.assertRaises(EStopLatched):
            self.bus.start_action("policy.run", {"name": "bow"}, authorized=True)

    def test_a_list_of_models_that_is_not_a_list_is_never_matched_by_a_part_of_it(self):
        self.bus.supervisor.params = {**self.bus.supervisor.params, "policy.run": {"policies": "bowkick"}}
        with self.assertRaises(SafetyRejected):
            self.bus.start_action("policy.run", {"name": "bow"}, authorized=True)
        self.assertEqual(self.body.started, [])

    def test_direct_invocation_has_a_time_limit(self):
        self.assertEqual(ACTION_DEFAULT_TIMEOUT_S["policy.run"], 600.0)


class OnARealDevice(unittest.TestCase):
    def test_without_filled_limits_nothing_is_started(self):
        body = Body(simulated=False)
        bus = CapabilityBus(body)                 # not simulated: it may not move before a person filled in its limits
        self.assertTrue(bus.supervisor.device_limits_required)
        with self.assertRaises(SafetyRejected) as caught:
            bus.start_action("policy.run", {"name": "bow"}, authorized=True)
        self.assertEqual(caught.exception.code, "safety_limits_missing")
        self.assertEqual(body.started, [])


if __name__ == "__main__":
    unittest.main()
