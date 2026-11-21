"""Issue #22 (R1-06): safety limits are filled in per device by a person; a device without them
does not move.

What is checked here:

* the rules (shape, platform maximum, what the body itself declares);
* the supervisor: every motion capability is refused while the limits are missing, the filled
  values limit the commands, the stop path and the watchdog do not depend on any of it;
* the runtime: it starts and reports its state without limits, the limits are read and written
  with who and when, they survive a restart, a damaged record counts as not filled;
* the HTTP interface;
* the ROS 2 bridge without limits in its configuration (on the rclpy test double, NOT ROS 2).

Every body here is the simulated quadruped underneath (see ``_limit_bodies``).
"""
import copy
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import _support
from _limit_bodies import ChunkQuadruped, RealQuadruped, UndeclaredQuadruped
from _support import RUNTIME_DIR, TOKEN, GatewayServer, EdgeRuntime, custom_package, http
from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from neuxmind_runtime import conformance
from neuxmind_runtime import device_limits as dl
from neuxmind_runtime import safety
from neuxmind_runtime.adapters import ros2
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import Conflict, RuntimeApiError, SafetyRejected
from neuxmind_runtime.store import Store

BASE = {"max_linear_speed": 0.4, "max_angular_speed": 0.6, "command_timeout_s": 0.3}
FILLED = {"base": dict(BASE)}
GOAL = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
MISSING = "safety_limits_missing"
WHO = "现场工程师甲"
BODY_ID = "sim-quadruped-01"        # instance_id of the stand-in bodies (the simulated quadruped's default)
BODY = dl.body_signature(spec_manifest.validate_manifest_v2(RealQuadruped().manifest()))    # what a record is bound to


def filled(**changes) -> dict:
    return {"base": {**BASE, **changes}}


def events_of(source, type_):
    return [e for e in source.events.since(0) if e["type"] == type_]


def refusal(test, call, code, error=RuntimeApiError):
    with test.assertRaises(error) as ctx:
        call()
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))
    return ctx.exception


# --------------------------------------------------------------------------------- rules
class RuleTests(unittest.TestCase):
    def test_valid_limits_come_back_as_plain_numbers(self):
        out = dl.normalize({"base": {"max_linear_speed": 1, "max_angular_speed": 0.6, "command_timeout_s": 0.3}})
        self.assertEqual(out, {"base": {"max_linear_speed": 1.0, "max_angular_speed": 0.6, "command_timeout_s": 0.3}})
        self.assertTrue(all(type(v) is float for v in out["base"].values()))

    def test_malformed_limits_are_refused(self):
        bad = [None, [], "fast", {}, {"arm": {"max_velocity": 1.0}}, {"base": None}, {"base": []},
               {"base": {**BASE, "max_jerk": 1.0}}, {**FILLED, "arm": {}}]
        bad += [{"base": {k: v for k, v in BASE.items() if k != missing}} for missing in BASE]
        for field in BASE:
            bad += [filled(**{field: value}) for value in (None, True, "0.3", [0.3], float("nan"), float("inf"))]
        for limits in bad:
            with self.subTest(limits=limits):
                refusal(self, lambda: dl.normalize(limits), "limits_invalid", dl.LimitsError)

    def test_a_refused_field_is_named(self):
        exc = refusal(self, lambda: dl.normalize(filled(max_angular_speed="1")), "limits_invalid")
        self.assertEqual(exc.extra["field"], "max_angular_speed")

    def test_values_outside_the_platform_range_are_refused(self):
        cases = (("max_linear_speed", dl.PLATFORM_MAX_LINEAR_SPEED + 0.01), ("max_linear_speed", 0.0),
                 ("max_linear_speed", -0.4), ("max_angular_speed", dl.PLATFORM_MAX_ANGULAR_SPEED + 0.01),
                 ("max_angular_speed", 0), ("command_timeout_s", dl.MAX_COMMAND_TIMEOUT_S + 0.01),
                 ("command_timeout_s", dl.MIN_COMMAND_TIMEOUT_S - 0.001), ("command_timeout_s", 1e308))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                exc = refusal(self, lambda: dl.normalize(filled(**{field: value})), "limits_out_of_range")
                self.assertEqual((exc.extra["field"], exc.extra["bound"]), (field, "platform"))
                self.assertEqual(exc.extra["value"], value)
                self.assertIn("max", exc.extra)
                self.assertIn("min", exc.extra)

    def test_the_platform_range_itself_is_accepted(self):
        top = {"max_linear_speed": dl.PLATFORM_MAX_LINEAR_SPEED, "max_angular_speed": dl.PLATFORM_MAX_ANGULAR_SPEED,
               "command_timeout_s": dl.MAX_COMMAND_TIMEOUT_S}
        self.assertEqual(dl.normalize({"base": top})["base"], top)
        self.assertEqual(dl.normalize(filled(command_timeout_s=dl.MIN_COMMAND_TIMEOUT_S))["base"]["command_timeout_s"],
                         dl.MIN_COMMAND_TIMEOUT_S)

    def test_the_command_timeout_range_is_the_supervisors(self):
        # Kept: a person chooses inside 0.05–2 s, nobody goes beyond it.
        self.assertEqual((dl.MIN_COMMAND_TIMEOUT_S, dl.MAX_COMMAND_TIMEOUT_S), (0.05, 2.0))
        self.assertEqual((safety.MIN_VELOCITY_TIMEOUT_S, safety.MAX_VELOCITY_TIMEOUT_S),
                         (dl.MIN_COMMAND_TIMEOUT_S, dl.MAX_COMMAND_TIMEOUT_S))

    def test_values_above_what_the_body_declares_are_refused(self):
        ceilings = dl.body_ceilings({"max_vx": 1.0, "max_vy": 0.5, "max_wz": 1.5}, {"max_linear_speed": 1.0})
        self.assertEqual(ceilings, {"max_linear_speed": 1.0, "max_angular_speed": 1.5, "command_timeout_s": None})
        exc = refusal(self, lambda: dl.normalize(filled(max_linear_speed=1.2), ceilings), "limits_out_of_range")
        self.assertEqual((exc.extra["field"], exc.extra["bound"], exc.extra["max"]), ("max_linear_speed", "body", 1.0))
        exc = refusal(self, lambda: dl.normalize(filled(max_angular_speed=1.6), ceilings), "limits_out_of_range")
        self.assertEqual((exc.extra["field"], exc.extra["bound"], exc.extra["max"]), ("max_angular_speed", "body", 1.5))
        self.assertEqual(dl.normalize(filled(max_linear_speed=1.0, max_angular_speed=1.5), ceilings)["base"]["max_linear_speed"], 1.0)

    def test_body_ceilings_when_the_body_declares_nothing_or_part(self):
        nothing = {"max_linear_speed": None, "max_angular_speed": None, "command_timeout_s": None}
        self.assertEqual(dl.body_ceilings({}, {}), nothing)
        self.assertEqual(dl.body_ceilings({"max_vy": 0.0, "holonomic": False}, {"hardware_estop": False}), nothing)
        # the linear maximum is what the chassis can reach on the manifest alone: max_vx when it cannot
        # move sideways, both axes combined when it can, and never above a declared overall cap
        sideways = {"max_vx": 0.5, "max_vy": 0.8, "max_wz": 1.0}
        self.assertEqual(dl.body_ceilings(sideways, {})["max_linear_speed"], 0.5)
        self.assertEqual(dl.body_ceilings({**sideways, "holonomic": True}, {})["max_linear_speed"], math.hypot(0.5, 0.8))
        self.assertEqual(dl.body_ceilings({**sideways, "holonomic": True}, {"max_linear_speed": 0.3})["max_linear_speed"], 0.3)
        # an overall cap above what the axes allow does not raise it (the filled value must be what is enforced)
        self.assertEqual(dl.body_ceilings({"max_vx": 1.0, "max_wz": 1.0}, {"max_linear_speed": 1.5})["max_linear_speed"], 1.0)
        self.assertEqual(dl.body_ceilings({}, {"max_linear_speed": 1.5})["max_linear_speed"], 1.5)

    def test_bounds_say_what_will_be_accepted_and_why(self):
        bounds = dl.bounds(dl.body_ceilings({"max_vx": 1.0, "max_wz": 1.5}, {}))["base"]
        self.assertEqual(bounds["max_linear_speed"], {"unit": "m/s", "min": 0.0, "min_exclusive": True, "max": 1.0,
                                                      "platform_max": dl.PLATFORM_MAX_LINEAR_SPEED, "body_max": 1.0})
        self.assertEqual(bounds["command_timeout_s"], {"unit": "s", "min": 0.05, "min_exclusive": False, "max": 2.0,
                                                       "platform_max": 2.0, "body_max": None})
        free = dl.bounds(dl.body_ceilings({}, {}))["base"]["max_angular_speed"]
        self.assertEqual((free["max"], free["body_max"], free["unit"]), (dl.PLATFORM_MAX_ANGULAR_SPEED, None, "rad/s"))

    def test_which_bodies_need_a_person_to_fill_the_limits(self):
        declared = {"max_vx": 1.0, "max_vy": 0.5, "max_wz": 1.5, "holonomic": True}
        self.assertTrue(dl.required_for(False, declared))                # a real body: always
        self.assertTrue(dl.required_for(False, None))                    # a real arm too
        self.assertFalse(dl.required_for(True, declared))                # a simulator with its own limits
        self.assertFalse(dl.required_for(True, None))                    # a simulated arm (no chassis)
        self.assertTrue(dl.required_for(True, {"max_vy": 0.0}))          # a simulated chassis without limits
        self.assertTrue(dl.required_for(True, {"max_vx": 1.0}))          # ... or with only half of them
        for not_true in (None, "true", 1):
            self.assertTrue(dl.required_for(not_true, declared))         # only a literal True counts as simulated

    def test_effective_limits_are_the_lower_of_filled_and_declared(self):
        ceilings = dl.body_ceilings({"max_vx": 1.0, "max_wz": 0.5}, {})
        self.assertEqual(dl.effective(dl.normalize(FILLED), ceilings),
                         {"base": {"max_linear_speed": 0.4, "max_angular_speed": 0.5, "command_timeout_s": 0.3}})


# ---------------------------------------------------------------------------- supervisor
class BusCase(unittest.TestCase):
    body = RealQuadruped

    def setUp(self):
        self.clock = ManualClock()
        self.robot = self.body(clock=self.clock)
        self.bus = CapabilityBus(self.robot)
        self.sup = self.bus.supervisor

    def velocity(self, **args):
        return self.bus.invoke("base.velocity", {"vx": 0.0, "wz": 0.0, **args}, authorized=True)

    def assert_blocked(self, call):
        exc = refusal(self, call, MISSING, SafetyRejected)
        self.assertIn("安全限值", str(exc))
        return exc


class UnfilledBodyTests(BusCase):
    """Acceptance: no limits filled -> every motion command is stopped before the adapter."""

    def test_every_motion_command_is_refused_with_the_reason(self):
        self.assertTrue(self.sup.device_limits_required)
        self.assertIsNone(self.sup.device_limits)
        self.assert_blocked(lambda: self.velocity(vx=0.1))
        self.assert_blocked(lambda: self.velocity())                                   # a zero setpoint too
        self.assert_blocked(lambda: self.bus.invoke("legged.posture", {"posture": "sit"}, authorized=True))
        self.assert_blocked(lambda: self.bus.start_action("base.navigate_to", {"goal": GOAL}, authorized=True))
        self.assertEqual((self.robot.cmd, self.robot.goal, self.robot.posture), ((0.0, 0.0, 0.0), None, "stand"))
        rejected = events_of(self.bus, "safety.rejected")
        self.assertEqual(len(rejected), 4)
        self.assertEqual({e["code"] for e in rejected}, {MISSING})

    def test_no_standard_or_vendor_motion_capability_gets_past_the_check(self):
        motion = [cap for cap in spec_caps.CAPABILITIES if safety.is_motion(cap)] + ["x.vendor.backflip"]
        self.assertGreaterEqual(len(motion), 8)
        for cap in motion:
            with self.subTest(cap=cap):
                self.assert_blocked(lambda: self.sup.check(cap, {}, True))
                self.assert_blocked(lambda: self.sup.ensure_device_limits(cap))

    def test_authorization_does_not_replace_the_limits(self):
        for authorized in (True, False):
            with self.assertRaises(RuntimeApiError):
                self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorized=authorized)
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_state_is_still_readable(self):
        self.assertTrue(self.bus.read("system.health")["ok"])
        self.assertEqual(len(self.bus.read("base.odometry")["pose"]), 7)
        self.sup.check("system.health", {}, False)          # not motion: no limits needed

    def test_stop_and_halt_reach_the_adapter_without_limits(self):
        self.assertTrue(self.sup.halt_motion("test"))
        self.assertEqual(self.robot.stop_calls, 1)
        result = self.sup.emergency_stop("test")
        self.assertEqual((result["stopped"], result["latched"], self.robot.stop_calls), (True, True, 2))

    def test_there_is_no_public_way_to_switch_the_requirement_off(self):
        with self.assertRaises(AttributeError):
            self.sup.device_limits_required = False
        with self.assertRaises(AttributeError):
            self.sup.device_limits = dl.normalize(FILLED)
        self.assert_blocked(lambda: self.velocity(vx=0.1))


class FilledBodyTests(BusCase):
    def setUp(self):
        super().setUp()
        self.sup.set_device_limits(FILLED)

    def test_velocity_is_limited_to_the_filled_values(self):
        self.assertEqual(self.velocity(vx=5.0, wz=-9.0), {"accepted": True})
        self.assertEqual((self.robot.cmd[0], self.robot.cmd[2]), (0.4, -0.6))       # below the body's 1.0 / 1.5
        limited = {e["field"]: e for e in events_of(self.bus, "safety.limited")}
        self.assertEqual((limited["vx"]["limit"], limited["wz"]["limit"]), (0.4, 0.6))

    def test_the_filled_linear_speed_caps_the_combined_speed(self):
        self.velocity(vx=5.0, vy=5.0)
        self.assertAlmostEqual(math.hypot(self.robot.cmd[0], self.robot.cmd[1]), 0.4, places=9)

    def test_the_combined_speed_never_exceeds_the_cap_by_a_rounding_step(self):
        rng = random.Random(22)
        for _ in range(3000):
            cap = rng.uniform(0.05, 1.0)
            self.sup.set_device_limits(filled(max_linear_speed=cap))
            limited, _notes = self.sup.check("base.velocity", {"vx": rng.uniform(-3, 3), "vy": rng.uniform(-3, 3),
                                                               "wz": 0.0}, True)
            self.assertLessEqual(math.hypot(limited["vx"], limited["vy"]), cap)
            self.assertLessEqual(max(abs(limited["vx"]), abs(limited["vy"])), cap)

    def test_what_the_body_declares_still_applies_when_it_is_lower(self):
        self.sup.set_device_limits(filled(max_linear_speed=2.0, max_angular_speed=2.5))
        self.velocity(vx=5.0, wz=9.0)
        self.assertEqual((self.robot.cmd[0], self.robot.cmd[2]), (1.0, 1.5))
        self.velocity(vy=5.0)
        self.assertEqual(self.robot.cmd[1], 0.5)                                        # the body's max_vy

    def test_the_filled_timeout_is_the_default(self):
        self.velocity(vx=0.2)
        self.clock.advance(0.29)
        self.assertFalse(self.sup.watchdog_tick())
        self.clock.advance(0.02)
        self.assertTrue(self.sup.watchdog_tick())
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_a_command_cannot_ask_for_a_longer_timeout_than_filled(self):
        self.velocity(vx=0.2, timeout_s=1.5)
        self.clock.advance(0.31)
        self.assertTrue(self.sup.watchdog_tick())
        note = [e for e in events_of(self.bus, "safety.limited") if e["field"] == "timeout_s"][0]
        self.assertEqual((note["requested"], note["applied"]), (1.5, 0.3))

    def test_a_command_may_ask_for_a_shorter_timeout(self):
        self.velocity(vx=0.2, timeout_s=0.1)
        self.clock.advance(0.11)
        self.assertTrue(self.sup.watchdog_tick())

    def test_other_motion_is_allowed_once_filled(self):
        self.bus.invoke("legged.posture", {"posture": "sit"}, authorized=True)
        self.assertEqual(self.robot.posture, "sit")
        self.bus.invoke("legged.posture", {"posture": "stand"}, authorized=True)
        self.bus.start_action("base.navigate_to", {"goal": GOAL}, authorized=True)
        self.assertIsNotNone(self.robot.goal)

    def test_taking_the_limits_away_blocks_again_and_the_watchdog_still_zeroes(self):
        self.velocity(vx=0.2)
        self.sup.set_device_limits(None)
        self.assert_blocked(lambda: self.velocity(vx=0.2))
        self.assertEqual(self.robot.cmd[0], 0.2)                # the refused command changed nothing
        self.clock.advance(0.31)
        self.assertTrue(self.sup.watchdog_tick())               # the earlier setpoint still expires
        self.assertEqual(self.robot.cmd, (0.0, 0.0, 0.0))

    def test_invalid_limits_are_never_applied(self):
        bad = [{}, {"base": {}}, filled(max_linear_speed=float("nan")), filled(max_angular_speed=-1.0),
               filled(command_timeout_s=5.0), filled(max_linear_speed=dl.PLATFORM_MAX_LINEAR_SPEED * 2),
               filled(max_linear_speed="0.4"), "fast", 3]
        for limits in bad:
            with self.subTest(limits=limits):
                with self.assertRaises(Exception):
                    self.sup.set_device_limits(limits)
                self.assertEqual(safety.thaw(self.sup.device_limits), FILLED)

    def test_the_applied_limits_cannot_be_changed_from_outside(self):
        mine = copy.deepcopy(FILLED)
        self.sup.set_device_limits(mine)
        mine["base"]["max_linear_speed"] = 99.0
        with self.assertRaises(TypeError):
            self.sup.device_limits["base"]["max_linear_speed"] = 99.0
        self.velocity(vx=5.0)
        self.assertEqual(self.robot.cmd[0], 0.4)

    def test_a_callers_context_cannot_bring_its_own_limits(self):
        loose = {"device_limits": {"base": {"max_linear_speed": 99.0, "max_angular_speed": 99.0, "command_timeout_s": 2.0}}}
        limited, _notes = self.sup.check("base.velocity", {"vx": 5.0, "wz": 9.0}, True, loose)
        self.assertEqual((limited["vx"], limited["wz"], limited["timeout_s"]), (0.4, 0.6, 0.3))

    def test_velocity_in_effect_is_visible(self):
        self.assertFalse(self.sup.velocity_active)
        self.velocity(vx=0.2)
        self.assertTrue(self.sup.velocity_active)
        self.velocity()
        self.assertFalse(self.sup.velocity_active)


class ActionChunkTests(BusCase):
    body = ChunkQuadruped

    def test_velocity_chunks_are_refused_without_limits_and_limited_with_them(self):
        chunk = {"actions": [[5.0, 0.0, 9.0], [0.1, 0.0, -0.1]], "dt": 0.1}
        self.assert_blocked(lambda: self.sup.check("policy.action_chunk", chunk, True))
        self.sup.set_device_limits(FILLED)
        limited, _notes = self.sup.check("policy.action_chunk", chunk, True)
        self.assertEqual(limited["actions"], [[0.4, 0.0, 0.6], [0.1, 0.0, -0.1]])


class BodyWithoutDeclaredLimitsTests(BusCase):
    """A chassis whose manifest carries no speed limits (the ROS 2 bridge without config limits)."""
    body = UndeclaredQuadruped

    def test_it_is_blocked_even_though_it_calls_itself_simulated(self):
        self.assertTrue(self.bus.manifest["simulated"])
        self.assertTrue(self.sup.device_limits_required)
        self.assert_blocked(lambda: self.velocity())
        self.assert_blocked(lambda: self.velocity(vx=0.1))
        self.assert_blocked(lambda: self.bus.start_action("base.navigate_to", {"goal": GOAL}, authorized=True))
        self.assertEqual((self.robot.cmd, self.robot.goal), ((0.0, 0.0, 0.0), None))

    def test_the_filled_limits_are_then_the_only_ones(self):
        self.sup.set_device_limits(FILLED)
        self.velocity(vx=5.0, vy=1.0, wz=9.0)
        self.assertEqual(self.robot.cmd, (0.4, 0.0, 0.6))          # vy: not holonomic


class SimulatedBodyTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.robot = SimQuadruped(clock=self.clock)
        self.bus = CapabilityBus(self.robot)
        self.sup = self.bus.supervisor

    def test_a_simulator_with_its_own_limits_moves_as_before(self):
        self.assertFalse(self.sup.device_limits_required)
        self.bus.invoke("base.velocity", {"vx": 5.0, "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd[0], 1.0)
        self.clock.advance(safety.DEFAULT_VELOCITY_TIMEOUT_S - 0.01)
        self.assertFalse(self.sup.watchdog_tick())
        self.clock.advance(0.02)
        self.assertTrue(self.sup.watchdog_tick())
        self.assertFalse(CapabilityBus(SimArm(clock=ManualClock())).supervisor.device_limits_required)

    def test_filling_limits_on_a_simulator_tightens_it(self):
        self.sup.set_device_limits(FILLED)
        self.bus.invoke("base.velocity", {"vx": 5.0, "wz": 0.0}, authorized=True)
        self.assertEqual(self.robot.cmd[0], 0.4)

    def test_the_requirement_can_be_switched_on_but_not_off(self):
        self.sup.require_device_limits()
        self.assertTrue(self.sup.device_limits_required)
        refusal(self, lambda: self.bus.invoke("base.velocity", {"vx": 0.1, "wz": 0.0}, authorized=True), MISSING)
        self.sup.set_device_limits(FILLED)
        self.sup.set_device_limits(None)
        self.assertTrue(self.sup.device_limits_required)        # taking the values away does not lift it


# ------------------------------------------------------------------------------- runtime
class Rig:
    """An EdgeRuntime on a temp state dir around one of the stand-in bodies."""

    def __init__(self, body=RealQuadruped, state_dir=None, serve=False, sandbox="inprocess", time_scale=20.0,
                 monitor=True):
        """``monitor=False``: no watchdog thread, so a velocity setpoint stays until the test ends it
        (tests that look at the commanded velocity must not race the watchdog)."""
        self._tmp = None
        if state_dir is None:
            self._tmp = tempfile.TemporaryDirectory()
            state_dir = self._tmp.name
        self.state_dir = state_dir
        self.adapter = body(time_scale=time_scale)
        self.runtime = EdgeRuntime(self.adapter, state_dir, sandbox=sandbox, start_monitor=monitor)
        self.limits = self.runtime.device_limits
        self.server = GatewayServer(self.runtime, TOKEN, port=0).start() if serve else None

    @property
    def url(self):
        return self.server.url

    def write(self, limits=None, **extra):
        return self.limits.write({"limits": FILLED if limits is None else limits, "set_by": WHO, **extra})

    def velocity(self, **args):
        payload = {"args": {"vx": 0.0, "wz": 0.0, **args}, "hardware_authorized": True,
                   "request_id": f"vel-{time.monotonic_ns()}"}
        return self.runtime.invoke("base.velocity", payload)

    def close(self):
        if self.server is not None:
            self.server.close()
        self.runtime.runner.stop_all("test teardown")
        self.runtime.supervisor.halt_motion("test teardown")
        self.runtime.close()
        if self._tmp is not None:
            self._tmp.cleanup()


class RigCase(unittest.TestCase):
    body = RealQuadruped
    serve = False

    def setUp(self):
        self.rig = Rig(self.body, serve=self.serve, monitor=False)
        self.addCleanup(self.rig.close)
        self.rt = self.rig.runtime


class RuntimeWithoutLimitsTests(RigCase):
    def test_it_starts_and_reports_its_state(self):
        status = self.rt.status()
        self.assertEqual(status["safety_limits"]["required"], True)
        self.assertEqual({k: status["safety_limits"][k] for k in ("filled", "motion_blocked", "limits", "set_by", "set_at")},
                         {"filled": False, "motion_blocked": True, "limits": None, "set_by": None, "set_at": None})
        self.assertIn("安全限值", status["safety_limits"]["reason"])
        self.assertTrue(status["health"]["ok"])
        self.assertIn("base.odometry", self.rt.read_state(["base.odometry"]))
        view = self.rig.limits.view()
        self.assertEqual((view["schema"], view["revision"], view["effective"], view["history"], view["groups"]),
                         ("neuxmind.device_limits.v1", 0, None, [], ["base"]))
        self.assertEqual(view["bounds"]["base"]["max_linear_speed"]["max"], 1.0)        # what the body declares
        self.assertEqual(view["bounds"]["base"]["max_angular_speed"]["body_max"], 1.5)
        json.dumps(view, allow_nan=False)

    def test_direct_commands_are_refused_before_anything_starts(self):
        exc = refusal(self, lambda: self.rig.velocity(vx=0.2), MISSING, SafetyRejected)
        self.assertEqual(exc.status, 409)
        nav = {"args": {"goal": GOAL}, "hardware_authorized": True, "request_id": "nav-1"}
        refusal(self, lambda: self.rt.invoke("base.navigate_to", nav), MISSING, SafetyRejected)
        refusal(self, lambda: self.rt.invoke("legged.posture", {"args": {"posture": "sit"}, "hardware_authorized": True,
                                                                "request_id": "sit-1"}), MISSING, SafetyRejected)
        self.assertEqual(self.rt.runner.list(), [])                 # no run was handed out
        # a malformed request is still answered as malformed
        bad = {"args": {"vx": "fast", "wz": 0.0}, "hardware_authorized": True, "request_id": "bad-1"}
        self.assertEqual(refusal(self, lambda: self.rt.invoke("base.velocity", bad), "bad_request").status, 400)
        self.assertEqual((self.rig.adapter.cmd, self.rig.adapter.goal), ((0.0, 0.0, 0.0), None))
        # ... and the request ids were not used up: the same request works once the limits are there
        self.rig.write()
        self.assertEqual(self.rt.invoke("base.navigate_to", nav)["status"] in ("queued", "running"), True)

    def test_stop_works_and_latches_without_limits(self):
        result = self.rt.emergency_stop("test")
        self.assertEqual((result["stopped"], result["latched"]), (True, True))
        self.assertEqual(self.rig.adapter.stop_calls, 1)
        # latched and without limits: the latch answers first, as it does inside the supervisor
        refusal(self, lambda: self.rig.velocity(vx=0.2), "estop_latched", SafetyRejected)
        nav = {"args": {"goal": GOAL}, "hardware_authorized": True, "request_id": "nav-latched"}
        refusal(self, lambda: self.rt.invoke("base.navigate_to", nav), "estop_latched", SafetyRejected)
        self.assertEqual(self.rt.runner.list(), [])
        self.rt.reset({"hardware_authorized": True})
        refusal(self, lambda: self.rig.velocity(vx=0.2), MISSING, SafetyRejected)      # unlatched: still no limits

    def test_an_application_that_needs_motion_fails_the_device_check_with_the_reason(self):
        self.rt.install_skill({"request_id": "i-1", "package": _support.example("patrol")})
        self.rt.install_skill({"request_id": "i-2", "package": _support.example("status_report")})
        moving = {"schema": "robot.application.v1", "steps": [
            {"entrypoint": "patrol", "requires": ["base.navigate_to"], "parameters": {"waypoints": [[1, 0]]}}]}
        check = self.rt.v1_validate(moving)
        case = [c for c in check["cases"] if "安全限值" in c["name"]]
        self.assertEqual((check["passed"], len(case), case[0]["passed"]), (False, 1, False))
        self.assertIn("没有填写", case[0]["detail"])
        reading = {"schema": "robot.application.v1", "steps": [
            {"entrypoint": "status_report", "requires": ["system.health"], "parameters": {}}]}
        check = self.rt.v1_validate(reading)
        self.assertEqual([c for c in check["cases"] if "安全限值" in c["name"]], [])
        self.assertTrue(check["passed"], check["cases"])
        self.rig.write()
        check = self.rt.v1_validate(moving)
        self.assertTrue([c for c in check["cases"] if "安全限值" in c["name"]][0]["passed"])


SKILL = '''
def run(ctx):
    calls = []
    for cap, args in (("base.velocity", {"vx": 0.2, "wz": 0.0}),
                      ("base.velocity", {"vx": 0.0, "wz": 0.0}),
                      ("legged.posture", {"posture": "sit"}),
                      ("base.navigate_to", {"goal": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]})):
        try:
            ctx.call(cap, **args)
            calls.append([cap, "accepted", ""])
        except Exception as exc:
            calls.append([cap, getattr(exc, "code", type(exc).__name__), str(exc)])
    return {"calls": calls, "healthy": ctx.read("system.health")["ok"]}
'''


class MotionSkillWithoutLimitsTests(unittest.TestCase):
    """Acceptance (R1): 安全限值没填时，发运动指令的能力启动后，它的指令全部被拦住。"""

    def run_skill(self, sandbox):
        rig = Rig(RealQuadruped, sandbox=sandbox)
        self.addCleanup(rig.close)
        package = custom_package("mover", SKILL, requires=("system.health", "base.velocity", "legged.posture",
                                                           "base.navigate_to"))
        rig.runtime.install_skill({"request_id": "install-mover", "package": package})
        started = rig.runtime.run_skill("mover", {"request_id": "run-mover", "parameters": {},
                                                  "hardware_authorized": True})
        record = rig.runtime.runner.wait(started["run_id"], 20.0)
        return rig, record

    def check(self, sandbox):
        rig, record = self.run_skill(sandbox)
        self.assertEqual(record["status"], "succeeded", record)         # the capability itself did start and run
        calls = record["result"]["calls"]
        self.assertEqual(len(calls), 4)
        for cap, code, message in calls:
            self.assertEqual(code, MISSING, (cap, message))
            self.assertIn("安全限值", message)
        self.assertTrue(record["result"]["healthy"])                      # reading still works
        self.assertEqual((rig.adapter.cmd, rig.adapter.goal, rig.adapter.posture), ((0.0, 0.0, 0.0), None, "stand"))
        self.assertEqual(len([e for e in events_of(rig.runtime, "safety.rejected") if e.get("code") == MISSING]), 4)

    def test_in_its_own_process(self):
        if _support.SANDBOX_SKIP_REASON:
            self.skipTest(_support.SANDBOX_SKIP_REASON)
        self.check(_support.SandboxConfig(python=_support.SANDBOX_PYTHON) if _support.SANDBOX_PYTHON else "process")

    def test_in_the_runtime_process(self):
        self.check("inprocess")


class WriteAndReadTests(RigCase):
    def test_filled_limits_take_effect_and_say_who_and_when(self):
        before = time.time()
        out = self.rig.write()
        self.assertTrue(out["accepted"])
        view = self.rig.limits.view()
        self.assertEqual({k: v for k, v in out.items() if k != "accepted"}, view)
        self.assertEqual((view["filled"], view["motion_blocked"], view["reason"], view["revision"]), (True, False, None, 1))
        self.assertEqual((view["limits"], view["effective"], view["set_by"]), (FILLED, FILLED, WHO))
        self.assertTrue(before <= view["set_at"] <= time.time())
        self.assertEqual(view["history"], [{"revision": 1, "limits": FILLED, "set_by": WHO, "set_at": view["set_at"]}])
        summary = self.rt.status()["safety_limits"]
        self.assertEqual((summary["filled"], summary["motion_blocked"], summary["limits"], summary["set_by"],
                          summary["set_at"]), (True, False, FILLED, WHO, view["set_at"]))
        event = events_of(self.rt, "safety.limits_set")[0]
        self.assertEqual((event["revision"], event["limits"], event["previous"], event["set_by"]), (1, FILLED, None, WHO))
        self.rig.velocity(vx=5.0, wz=-9.0)
        self.assertEqual((self.rig.adapter.cmd[0], self.rig.adapter.cmd[2]), (0.4, -0.6))

    def test_limits_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as state:
            first = Rig(state_dir=state, monitor=False)
            first.write()
            first.close()
            second = Rig(state_dir=state, monitor=False)
            self.addCleanup(second.close)
            view = second.limits.view()
            self.assertEqual((view["filled"], view["limits"], view["set_by"], view["revision"]), (True, FILLED, WHO, 1))
            second.velocity(vx=5.0)
            self.assertEqual(second.adapter.cmd[0], 0.4)

    def test_a_change_is_recorded_and_the_last_twenty_are_kept(self):
        for n in range(22):
            self.rig.write(filled(max_linear_speed=round(0.1 + n * 0.01, 2)), set_by=f"工程师{n}")
        view = self.rig.limits.view()
        self.assertEqual((view["revision"], len(view["history"])), (22, dl.HISTORY_KEPT))
        self.assertEqual([h["revision"] for h in view["history"]], list(range(22, 2, -1)))
        self.assertEqual((view["history"][0]["set_by"], view["set_by"], view["limits"]["base"]["max_linear_speed"]),
                         ("工程师21", "工程师21", 0.31))
        self.assertEqual(events_of(self.rt, "safety.limits_set")[-1]["previous"]["base"]["max_linear_speed"], 0.3)

    def refused(self, payload, code, error=RuntimeApiError):
        before = self.rig.limits.view()
        exc = refusal(self, lambda: self.rig.limits.write(payload), code, error)
        self.assertEqual(self.rig.limits.view(), before)                 # nothing changed
        return exc

    def test_values_beyond_the_platform_maximum_are_refused(self):
        exc = self.refused({"limits": filled(command_timeout_s=3.0), "set_by": WHO}, "limits_out_of_range")
        self.assertEqual((exc.status, exc.extra["field"], exc.extra["bound"], exc.extra["max"]),
                         (400, "command_timeout_s", "platform", 2.0))
        undeclared = Rig(UndeclaredQuadruped, monitor=False)             # nothing declared: the platform maximum decides
        self.addCleanup(undeclared.close)
        exc = refusal(self, lambda: undeclared.write(filled(max_linear_speed=dl.PLATFORM_MAX_LINEAR_SPEED + 0.5)),
                      "limits_out_of_range")
        self.assertEqual((exc.extra["bound"], exc.extra["max"]), ("platform", dl.PLATFORM_MAX_LINEAR_SPEED))
        self.assertTrue(undeclared.limits.view()["motion_blocked"])

    def test_values_beyond_what_the_body_declares_are_refused(self):
        exc = self.refused({"limits": filled(max_linear_speed=1.2), "set_by": WHO}, "limits_out_of_range")
        self.assertEqual((exc.extra["field"], exc.extra["bound"], exc.extra["max"]), ("max_linear_speed", "body", 1.0))
        self.assertTrue(self.rig.limits.view()["motion_blocked"])
        self.assertEqual(events_of(self.rt, "safety.limits_set"), [])

    def test_malformed_requests_are_refused(self):
        self.refused({"set_by": WHO}, "limits_invalid")
        self.refused({"limits": {"arm": {}}, "set_by": WHO}, "limits_invalid")
        self.refused({"limits": filled(max_linear_speed="0.4"), "set_by": WHO}, "limits_invalid")
        for nobody in (None, "", "   ", 7, ["a"]):
            self.refused({"limits": FILLED, "set_by": nobody}, "set_by_required")
        for odd in ("甲" * (dl.SET_BY_MAX_CHARS + 1), "a\nb", "a\x00b", "a\x85b", "a\u2028b", "a\u2029b", "a\u202eb",
                    "a\u2066b", "a\u200fb"):
            exc = self.refused({"limits": FILLED, "set_by": odd}, "limits_invalid")
            self.assertEqual(exc.extra["field"], "set_by")
        self.rig.limits.write({"limits": FILLED, "set_by": "علی‌رضا 👩‍🔧"})      # joiners in names and emoji are fine
        self.assertEqual(self.rig.limits.view()["set_by"], "علی‌رضا 👩‍🔧")

    def test_who_is_stored_trimmed(self):
        self.rig.limits.write({"limits": FILLED, "set_by": "  " + WHO + " "})
        self.assertEqual(self.rig.limits.view()["set_by"], WHO)

    def test_no_change_while_something_is_moving(self):
        self.rig.write()
        self.rig.velocity(vx=0.2, timeout_s=0.3)
        change = {"limits": filled(max_linear_speed=0.2), "set_by": WHO, "request_id": "tighten-1"}
        exc = self.refused(change, "runs_active", Conflict)
        self.assertEqual(exc.status, 409)
        self.rt.emergency_stop("test")
        self.rig.limits.write(change)              # stopped and latched: the very same request now goes through
        self.assertEqual(self.rig.limits.view()["revision"], 2)

    def test_a_run_that_cannot_move_does_not_hold_up_a_change(self):
        idle = custom_package("idler", "def run(ctx):\n    ctx.sleep(600)\n", requires=("system.health",))
        self.rt.install_skill({"request_id": "install-idler", "package": idle})
        self.rt.run_skill("idler", {"request_id": "run-idler", "parameters": {}, "hardware_authorized": True})
        _support.wait_until(lambda: self.rt.runner.active_runs(), 5.0)
        self.rig.write()
        self.assertTrue(self.rig.limits.view()["filled"])
        self.assertTrue(self.rt.runner.active_runs())

    def test_no_change_while_a_run_is_active(self):
        self.rig.write()
        far = {"args": {"goal": [150.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]}, "hardware_authorized": True, "request_id": "far"}
        self.rt.invoke("base.navigate_to", far)
        self.refused({"limits": filled(max_linear_speed=0.2), "set_by": WHO}, "runs_active", Conflict)

    def test_no_change_during_a_conformance_run(self):
        self.rt.conformance_lease = "lease-1"
        self.refused({"limits": FILLED, "set_by": WHO}, "conformance_running", Conflict)
        self.rt.conformance_lease = None

    def test_a_repeated_request_is_answered_once(self):
        first = self.rig.write(request_id="limits-1")
        again = self.rig.write(request_id="limits-1")
        self.assertEqual(first, again)
        self.assertEqual((self.rig.limits.view()["revision"], len(events_of(self.rt, "safety.limits_set"))), (1, 1))
        refusal(self, lambda: self.rig.write(filled(max_linear_speed=0.2), request_id="limits-1"),
                "request_id_reused", Conflict)

    def test_a_write_that_cannot_be_saved_changes_nothing(self):
        real_put = self.rt.store.put

        def failing_put(kind, obj):
            if obj.get("id") == dl.RECORD_ID:
                raise OSError("disk full")
            return real_put(kind, obj)
        self.rt.store.put = failing_put
        exc = self.refused({"limits": FILLED, "set_by": WHO}, "limits_not_saved")
        self.assertEqual(exc.status, 500)
        self.rt.store.put = real_put
        self.assertIsNone(self.rt.supervisor.device_limits)
        refusal(self, lambda: self.rig.velocity(vx=0.2), MISSING)

    def test_a_failed_commit_leaves_nothing_for_a_later_write_to_save(self):
        store = self.rt.store
        real_db = store._db

        class FailingCommit:
            """The sqlite connection with one commit that fails after its statement ran (disk full)."""

            def __init__(self):
                self.failed = False

            def commit(self):
                if not self.failed:
                    self.failed = True
                    raise OSError("database or disk is full")
                return real_db.commit()

            def __getattr__(self, name):
                return getattr(real_db, name)
        store._db = FailingCommit()
        try:
            refusal(self, lambda: self.rig.write(), "limits_not_saved")
            store.put("notes", {"id": "later", "text": "an unrelated write that succeeds"})
        finally:
            store._db = real_db
        self.assertIsNone(store.get(dl.RECORD_KIND, dl.RECORD_ID))     # not smuggled in by the later commit
        self.assertEqual(store.get("notes", "later")["text"], "an unrelated write that succeeds")
        self.assertTrue(self.rig.limits.view()["motion_blocked"])

    def test_a_second_write_does_not_queue_behind_a_stuck_one(self):
        real_put, entered, release = self.rt.store.put, threading.Event(), threading.Event()

        def stuck_put(kind, obj):
            if obj.get("id") == dl.RECORD_ID:
                entered.set()
                release.wait(10)
            return real_put(kind, obj)
        self.rt.store.put = stuck_put
        wait, dl.WRITE_WAIT_S = dl.WRITE_WAIT_S, 0.2
        first = threading.Thread(target=self.rig.write, daemon=True)
        try:
            first.start()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            exc = refusal(self, lambda: self.rig.write(filled(max_linear_speed=0.2)), "busy")
            self.assertEqual(exc.status, 503)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            dl.WRITE_WAIT_S = wait
            release.set()
            first.join(5)
            self.rt.store.put = real_put
        self.assertEqual(self.rig.limits.view()["limits"], FILLED)       # the first one went through, alone

    def test_stop_does_not_wait_for_a_write_in_progress(self):
        real_put, entered, release = self.rt.store.put, threading.Event(), threading.Event()

        def slow_put(kind, obj):
            if obj.get("id") == dl.RECORD_ID:
                entered.set()
                release.wait(10)
            return real_put(kind, obj)
        self.rt.store.put = slow_put
        writer = threading.Thread(target=self.rig.write, daemon=True)
        writer.start()
        self.assertTrue(entered.wait(5))
        started = time.monotonic()
        result = self.rt.emergency_stop("stop during a limits write")
        took = time.monotonic() - started
        release.set()
        writer.join(5)
        self.rt.store.put = real_put
        self.assertEqual((result["stopped"], self.rig.adapter.stop_calls), (True, 1))
        self.assertLess(took, 0.5)
        self.assertTrue(self.rt.supervisor.latched)


class StoredRecordTests(unittest.TestCase):
    def boot_with(self, record):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        store = Store(state.name)
        store.put("safety", {"id": dl.RECORD_ID, **record})
        store.close()
        rig = Rig(state_dir=state.name, monitor=False)
        self.addCleanup(rig.close)
        return rig

    def test_a_damaged_record_counts_as_not_filled(self):
        good = {"schema": dl.SCHEMA, "body": BODY, "limits": FILLED, "set_by": WHO, "set_at": 1.0, "revision": 1}
        damaged = [{**good, "limits": filled(max_linear_speed=99.0)},
                   {**good, "limits": {"base": {"max_linear_speed": 0.4}}},
                   {**good, "limits": "fast"},
                   {**good, "set_by": ""},
                   {**good, "set_at": "yesterday"},
                   {**good, "revision": 0},
                   {**good, "schema": "something.else"},
                   {k: v for k, v in good.items() if k != "schema"},
                   {**good, "body": {**BODY, "instance_id": "another-robot-07"}},     # copied from another robot
                   {**good, "body": {**BODY, "model": "another-model"}},              # another adapter
                   {**good, "body": {**BODY, "base_velocity": {**BODY["base_velocity"], "max_vx": 2.0}}},   # limits changed
                   {**good, "body": "sim-quadruped-01"},
                   {k: v for k, v in good.items() if k != "body"},
                   {}]
        for record in damaged:
            with self.subTest(record=record):
                rig = self.boot_with(record)
                view = rig.limits.view()
                self.assertEqual((view["filled"], view["motion_blocked"], view["limits"]), (False, True, None))
                self.assertIn("无效", view["reason"])
                self.assertIn("无效", rig.runtime.status()["safety_limits"]["reason"])
                self.assertEqual(len(events_of(rig.runtime, "safety.limits_invalid")), 1)
                refusal(self, lambda: rig.velocity(vx=0.2), MISSING)
                rig.write()                                              # filling again repairs it
                self.assertTrue(rig.limits.view()["filled"])

    def test_a_record_filled_for_another_robot_says_so(self):
        rig = self.boot_with({"schema": dl.SCHEMA, "body": {**BODY, "instance_id": "another-robot-07"},
                              "limits": FILLED, "set_by": WHO, "set_at": 1.0, "revision": 1})
        view = rig.limits.view()
        self.assertIn("另一台本体", view["reason"])
        self.assertIn("another-robot-07", view["invalid"])
        rig.write()
        self.assertEqual(rig.runtime.store.get(dl.RECORD_KIND, dl.RECORD_ID)["body"], BODY)
        self.assertEqual(BODY["instance_id"], BODY_ID)

    def test_what_a_record_is_bound_to(self):
        self.assertEqual(BODY, {"instance_id": BODY_ID, "vendor": "neuxmind-sim", "model": "sim-quadruped",
                                "family": "quadruped", "max_linear_speed": 1.0,
                                "base_velocity": {"max_vx": 1.0, "max_vy": 0.5, "max_wz": 1.5, "holonomic": True}})
        arm = dl.body_signature(spec_manifest.validate_manifest_v2(SimArm().manifest()))
        self.assertEqual((arm["base_velocity"], arm["max_linear_speed"]), (None, None))       # no chassis

    def test_a_good_record_written_by_hand_is_checked_and_used(self):
        rig = self.boot_with({"schema": dl.SCHEMA, "body": BODY, "limits": FILLED, "set_by": WHO,
                              "set_at": 12.5, "revision": 4, "history": []})
        view = rig.limits.view()
        self.assertEqual((view["filled"], view["set_at"], view["revision"]), (True, 12.5, 4))

    def test_a_stored_value_above_what_the_body_declares_is_limited_by_the_body(self):
        # cannot be written through the interface; a record edited by hand is still held to the body's own limit
        rig = self.boot_with({"schema": dl.SCHEMA, "body": BODY, "limits": filled(max_linear_speed=2.0),
                              "set_by": WHO, "set_at": 1.0, "revision": 1, "history": []})
        view = rig.limits.view()
        self.assertEqual((view["filled"], view["limits"]["base"]["max_linear_speed"],
                          view["effective"]["base"]["max_linear_speed"]), (True, 2.0, 1.0))
        rig.velocity(vx=5.0)
        self.assertEqual(rig.adapter.cmd[0], 1.0)


class SimulatedRuntimeTests(RigCase):
    body = SimQuadruped

    def test_a_simulator_is_not_blocked_and_says_so(self):
        view = self.rig.limits.view()
        self.assertEqual((view["required"], view["filled"], view["motion_blocked"], view["reason"]),
                         (False, False, False, None))
        self.rig.velocity(vx=5.0)
        self.assertEqual(self.rig.adapter.cmd[0], 1.0)

    def test_the_requirement_can_be_forced_for_a_simulator(self):
        self.rig.limits.require()
        self.assertTrue(self.rig.limits.view()["motion_blocked"])
        refusal(self, lambda: self.rig.velocity(vx=0.2), MISSING)
        self.rig.write()
        self.rig.velocity(vx=5.0)
        self.assertEqual(self.rig.adapter.cmd[0], 0.4)


# ---------------------------------------------------------------------------------- HTTP
class GatewayTests(RigCase):
    serve = True

    def test_read_write_and_refusals_over_http(self):
        url = self.rig.url
        self.assertEqual(http(url, "/v2/safety/limits", token=None)[0], 401)
        self.assertEqual(http(url, "/v2/safety/limits", {"limits": FILLED, "set_by": WHO}, token="x" * 30)[0], 401)
        status, view = http(url, "/v2/safety/limits")
        self.assertEqual((status, view["filled"], view["motion_blocked"]), (200, False, True))
        status, body = http(url, "/v1/status")
        self.assertEqual((status, body["safety_limits"]["motion_blocked"]), (200, True))

        move = {"args": {"vx": 0.2, "wz": 0.0}, "hardware_authorized": True, "request_id": "http-move-1"}
        status, body = http(url, "/v2/capabilities/base.velocity/invoke", move)
        self.assertEqual((status, body["code"]), (409, MISSING))
        self.assertIn("安全限值", body["error"])

        status, body = http(url, "/v2/safety/limits", {"limits": filled(max_linear_speed=9.0), "set_by": WHO})
        self.assertEqual((status, body["code"], body["field"], body["bound"], body["max"]),
                         (400, "limits_out_of_range", "max_linear_speed", "body", 1.0))
        status, body = http(url, "/v2/safety/limits", {"limits": FILLED})
        self.assertEqual((status, body["code"]), (400, "set_by_required"))

        status, body = http(url, "/v2/safety/limits", {"limits": FILLED, "set_by": WHO, "request_id": "http-limits-1"})
        self.assertEqual((status, body["accepted"], body["filled"], body["set_by"], body["limits"]),
                         (200, True, True, WHO, FILLED))
        status, body = http(url, "/v2/capabilities/base.velocity/invoke", move)      # the same request id, now fine
        self.assertEqual((status, body), (200, {"accepted": True}))
        self.assertEqual(self.rig.adapter.cmd[0], 0.2)

    def test_stop_over_http_is_answered_while_a_limits_write_waits_for_the_disk(self):
        store_lock = self.rt.store._lock                    # held: every store write now waits, as on a hung disk
        writer = threading.Thread(target=lambda: http(self.rig.url, "/v2/safety/limits",
                                                      {"limits": FILLED, "set_by": WHO}), daemon=True)
        store_lock.acquire()
        try:
            writer.start()
            _support.wait_until(lambda: self.rig.limits._lock.locked(), 5.0)     # the write is inside, stuck
            started = time.monotonic()
            status, body = http(self.rig.url, "/v1/stop", {"reason": "stop during a stuck limits write"})
            took = time.monotonic() - started
            self.assertEqual((status, body["stopped"], body["latched"]), (200, True, True))
            self.assertEqual(self.rig.adapter.stop_calls, 1)
            self.assertLess(took, 1.0)
            self.assertTrue(writer.is_alive())                                   # still waiting for the disk
        finally:
            store_lock.release()
            writer.join(5)

    def test_stop_is_served_without_limits(self):
        status, body = http(self.rig.url, "/v1/stop", {"reason": "test"})
        self.assertEqual((status, body["stopped"], body["latched"]), (200, True, True))
        self.assertEqual(self.rig.adapter.stop_calls, 1)


# --------------------------------------------------------------------------------- ROS 2
def bare_ros2_config() -> dict:
    return json.loads(json.dumps(ros2.DEFAULT_CONFIG))          # no limits filled in the configuration


class Ros2WithoutConfiguredLimitsTests(unittest.TestCase):
    """On the rclpy test double (NOT ROS 2; it reports use_sim_time, so the body is ``simulated``)."""

    def test_the_manifest_is_valid_and_declares_no_speed_limits(self):
        manifest = spec_manifest.validate_manifest_v2(ros2.build_manifest(bare_ros2_config()))
        velocity = next(c for c in manifest["capabilities"] if c["id"] == "base.velocity")
        self.assertNotIn("max_vx", velocity["params"])
        self.assertNotIn("max_wz", velocity["params"])
        self.assertNotIn("max_linear_speed", manifest["safety"])
        config = bare_ros2_config()
        config["limits"].update(max_vx=0.8)                      # only one of the two: declared as given
        velocity = next(c for c in ros2.build_manifest(config)["capabilities"] if c["id"] == "base.velocity")
        self.assertEqual((velocity["params"].get("max_vx"), "max_wz" in velocity["params"]), (0.8, False))

    def test_the_bridge_starts_shows_its_state_and_refuses_every_motion_command(self):
        with _support.ros2_double() as graph, tempfile.TemporaryDirectory() as state:
            adapter = ros2.Ros2Adapter(bare_ros2_config())      # used to raise: 需要在配置 limits 中填写
            runtime = EdgeRuntime(adapter, state, sandbox="inprocess", start_monitor=False)
            try:
                status = runtime.status()
                self.assertEqual((status["safety_limits"]["motion_blocked"], status["mode"]), (True, "ros2"))
                self.assertEqual(_support.wait_until(lambda: "base.odometry" in _read(runtime), 5.0), True)
                for cap, args in (("base.velocity", {"vx": 0.2, "wz": 0.0}), ("base.velocity", {"vx": 0.0, "wz": 0.0}),
                                  ("base.navigate_to", {"goal": GOAL})):
                    payload = {"args": args, "hardware_authorized": True, "request_id": f"ros-{time.monotonic_ns()}"}
                    refusal(self, lambda: runtime.invoke(cap, payload), MISSING, SafetyRejected)
                self.assertEqual(list(graph.published["/cmd_vel"]), [])        # nothing was published
                self.assertEqual(graph.robot().goals, [])
                self.assertEqual(runtime.runner.list(), [])

                view = runtime.device_limits.view()
                self.assertEqual((view["bounds"]["base"]["max_linear_speed"]["body_max"],
                                  view["bounds"]["base"]["max_linear_speed"]["max"]), (None, dl.PLATFORM_MAX_LINEAR_SPEED))
                runtime.device_limits.write({"limits": FILLED, "set_by": WHO})
                runtime.invoke("base.velocity", {"args": {"vx": 5.0, "wz": -9.0}, "hardware_authorized": True,
                                                 "request_id": "ros-after"})
                last = graph.published["/cmd_vel"][-1]
                self.assertEqual((last.linear.x, last.angular.z), (0.4, -0.6))
            finally:
                runtime.supervisor.halt_motion("test teardown")
                runtime.close()


def _read(runtime) -> dict:
    try:
        return runtime.read_state(["base.odometry"])
    except RuntimeApiError:
        return {}


# --------------------------------------------------------------------------- conformance
class ConformanceWithoutLimitsTests(unittest.TestCase):
    def dynamic(self, report):
        return [c for c in report["cases"] if c["id"].startswith(("dry.", "motion."))]

    def test_the_suite_sends_no_motion_call_to_a_body_without_limits_and_says_why(self):
        adapter = RealQuadruped()
        report = conformance.run_adapter(adapter, "motion", True)
        by_id = {c["id"]: c for c in report["cases"]}
        self.assertEqual([c["id"] for c in report["cases"] if c["status"] == "failed"], [])
        self.assertFalse(report["aborted"])
        # what needs no motion command is still tested ...
        for cid in ("dry.system.health.read", "dry.base.odometry.read", "dry.safety.stop.idempotent_latched",
                    "dry.safety.stop.latch_blocks_motion"):
            self.assertEqual(by_id[cid]["status"], "passed", by_id[cid])
        # ... a latched e-stop still answers for itself ...
        self.assertEqual(by_id["dry.safety.stop.latch_blocks_motion"]["evidence"]["latched_rejection"]["code"],
                         "estop_latched")
        # ... and the cases that need a motion call are blocked with the reason, not failed
        for cid in ("dry.base.velocity.zero_setpoint", "dry.base.navigate_to.rejects_invalid_goal"):
            self.assertEqual((by_id[cid]["status"], by_id[cid]["skip_kind"]), ("skipped", "blocked"), by_id[cid])
            self.assertIn("安全限值", by_id[cid]["reason"])
        moving = [c for c in report["cases"] if c["id"].startswith("motion.") and c["capability"] != "safety.stop"]
        self.assertGreater(len(moving), 5)
        for c in moving:
            self.assertEqual((c["status"], c["skip_kind"]), ("skipped", "blocked"), c["id"])
        self.assertEqual((report["dry_ok"], report["motion_ok"]), (False, False))
        self.assertEqual((adapter.cmd, adapter.goal, adapter.posture), ((0.0, 0.0, 0.0), None, "stand"))

    def test_with_limits_from_the_caller_the_dry_level_runs(self):
        report = conformance.run_adapter(RealQuadruped(), "dry",
                                         device_limits={"limits": filled(command_timeout_s=0.5), "set_by": WHO})
        self.assertTrue(report["dry_ok"], [c for c in self.dynamic(report) if c["status"] == "failed"])

    def test_the_motion_level_scales_its_commands_by_the_filled_limits(self):
        # the body declares 1.0 m/s; a person filled 0.4: the suite must not command above a quarter of that
        report = conformance.run_adapter(RealQuadruped(), "motion", True,
                                         device_limits={"limits": filled(command_timeout_s=0.5), "set_by": WHO})
        by_id = {c["id"]: c for c in report["cases"]}
        for cid in ("dry.base.velocity.zero_setpoint", "motion.base.velocity.watchdog",
                    "motion.base.velocity.stop_latency"):
            self.assertEqual(by_id[cid]["status"], "passed", by_id[cid])
        self.assertEqual([c["id"] for c in report["cases"] if "Error" in str(c.get("reason"))], [])
        self.assertLessEqual(by_id["motion.base.velocity.watchdog"]["evidence"]["command"]["vx"], 0.25 * 0.4 + 1e-12)

    def test_a_body_that_declares_no_speed_limits_does_not_pass_the_static_level(self):
        # Filled limits let such a body move, but the capability standard still wants max_vx / max_wz in
        # the manifest: it is not conformant, and the suite does not drive it.
        report = conformance.run_adapter(UndeclaredQuadruped(), "motion", True,
                                         device_limits={"limits": FILLED, "set_by": WHO})
        declaration = next(c for c in report["cases"] if c["id"] == "static.base.velocity.declaration")
        self.assertEqual(declaration["status"], "failed")
        self.assertIn("max_vx", declaration["reason"])
        self.assertEqual((report["static_ok"], report["dry_ok"], report["motion_ok"]), (False, False, False))
        for c in self.dynamic(report):
            self.assertEqual(c["status"], "skipped", c["id"])

    def test_limits_the_body_does_not_allow_are_not_taken_from_the_caller_either(self):
        report = conformance.run_adapter(RealQuadruped(), "dry",
                                         device_limits={"limits": filled(max_linear_speed=2.5), "set_by": WHO})
        self.assertFalse(report["dry_ok"])
        for c in self.dynamic(report):
            self.assertEqual(c["status"], "skipped", c["id"])           # the runtime was never driven
        self.assertIn("无法以该适配器启动运行时", self.dynamic(report)[0]["reason"])


# ----------------------------------------------------------------------------------- CLI
class ServeWithoutLimitsTests(unittest.TestCase):
    def test_serve_starts_warns_and_refuses_motion(self):
        with tempfile.TemporaryDirectory() as state:
            env = {**os.environ, "BRAIN_EDGE_TOKEN": TOKEN,
                   "PYTHONPATH": os.pathsep.join([str(RUNTIME_DIR / "tests"), os.environ.get("PYTHONPATH", "")])}
            proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter",
                                     "_limit_bodies:RealQuadruped", "--port", "0", "--state-dir", state,
                                     "--sandbox", "inprocess"],
                                    cwd=RUNTIME_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                url = _support.readline_within(proc.stdout, 30).strip().split()[-1]
                lines = [_support.readline_within(proc.stdout, 10) for _ in range(2)]
                self.assertTrue(any("安全限值" in line and "拒绝" in line for line in lines), lines)
                status, body = http(url, "/v1/status")
                self.assertEqual((status, body["safety_limits"]["motion_blocked"]), (200, True))
                status, body = http(url, "/v2/capabilities/base.velocity/invoke",
                                    {"args": {"vx": 0.2, "wz": 0.0}, "hardware_authorized": True, "request_id": "cli-1"})
                self.assertEqual((status, body["code"]), (409, MISSING))
            finally:
                proc.terminate()
                proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()


    def test_a_simulator_can_be_started_so_that_it_needs_filled_limits_too(self):
        with tempfile.TemporaryDirectory() as state:
            proc = subprocess.Popen([sys.executable, "-m", "neuxmind_runtime", "serve", "--adapter", "sim_quadruped",
                                     "--port", "0", "--state-dir", state, "--sandbox", "inprocess",
                                     "--require-safety-limits"],
                                    cwd=RUNTIME_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env={**os.environ, "BRAIN_EDGE_TOKEN": TOKEN})
            try:
                url = _support.readline_within(proc.stdout, 30).strip().split()[-1]
                status, body = http(url, "/v2/safety/limits")
                self.assertEqual((status, body["required"], body["motion_blocked"]), (200, True, True))
                status, body = http(url, "/v2/safety/limits", {"limits": FILLED, "set_by": WHO})
                self.assertEqual((status, body["filled"], body["motion_blocked"]), (200, True, False))
            finally:
                proc.terminate()
                proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
