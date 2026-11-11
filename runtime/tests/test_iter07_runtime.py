"""Round-07 runtime regressions (docs/evolution/reviews/2026-11-09-iter06-review.md).

* Q6-01: the latch guard tolerates realistic feedback noise (1–2 mm position, 1 mrad yaw, 5–8 mm/s
  velocity, 1–2 mrad joints) and slow IMU yaw drift / odometry random walk on a body that is truly
  at rest, while the Q5-01 creeps (0.015 / 0.019 m/s) are still re-stopped within bounded
  displacement; events are aggregated (``count``) so the 5000-event ring is not flooded; the
  thresholds are declarable in the manifest (``safety.latch_guard``) and overridable at runtime.
* Q6-04: a driver that keeps moving after many re-stops is escalated once: ``safety.stop_not_obeyed``.
* Q6-05: ``stop_settle_s`` declarable in the manifest / ROS 2 config / ``serve --latch-guard``.
* Q6-02/06/08: min/max on object/pose elements, ``minimum``/``maximum`` aliases, min/max on types
  that cannot honour them refused at install, typed choices, NaN/inf, deep nesting -> 400.
"""
import json
import math
import random
import unittest

import _support  # noqa: F401
from _support import TOKEN, http

from neuxmind_spec import manifest as spec_manifest
from neuxmind_runtime import cli
from neuxmind_runtime.adapters import ros2
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped, yaw_to_quat
from neuxmind_runtime.clock import ManualClock
from neuxmind_runtime.errors import BadRequest, RuntimeApiError
from neuxmind_runtime.gateway import GatewayServer
from neuxmind_runtime.latch_guard import GuardConfig, parse_overrides
from neuxmind_runtime.skills import check_parameters

from test_iter05_runtime import events_of, make_runtime
from test_iter06_runtime import Creeper, tick_for


class NoisyQuad(SimQuadruped):
    """Truly still body; only the *reported* odometry is noisy / drifting (r7/noise.py)."""

    def __init__(self, pos=0.0, yaw=0.0, vel=0.0, yaw_drift=0.0, walk=0.0, seed=7, **kw):
        super().__init__(**kw)
        self.p, self.ys, self.vs, self.yd, self.walk = pos, yaw, vel, yaw_drift, walk
        self.rng = random.Random(seed)
        self.bx = self.by = 0.0

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "base.odometry":
            g = self.rng.gauss
            if self.walk:
                self.bx += g(0, self.walk * math.sqrt(0.1))
                self.by += g(0, self.walk * math.sqrt(0.1))
            yaw = self.yaw + self.yd * self.clock.now() + g(0, self.ys)
            out = {"pose": [self.x + self.bx + g(0, self.p), self.y + self.by + g(0, self.p), 0.0] + yaw_to_quat(yaw),
                   "velocity": [g(0, self.vs), g(0, self.vs), g(0, self.vs)]}
        return out


class NoisyArm(SimArm):
    def __init__(self, q=0.0, qd=0.0, **kw):
        super().__init__(**kw)
        self.qs, self.qds = q, qd
        self.rng = random.Random(7)

    def read(self, cap_id):
        out = super().read(cap_id)
        if cap_id == "joint.state":
            g = self.rng.gauss
            out = dict(out)
            out["position"] = [q + g(0, self.qs) for q in out["position"]]
            if out.get("velocity") is not None:
                out["velocity"] = [v + g(0, self.qds) for v in out["velocity"]]
        return out


DRIFT_05_DEG_MIN = math.radians(0.5) / 60


# ====================================================================== Q6-01 noise / drift
class NoisyStillBody(unittest.TestCase):
    def _still(self, adapter, clock, seconds):
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, adapter, clock, seconds)
        return runtime, runtime.latch_guard.describe()

    def test_noise_and_yaw_drift_no_false_alarm(self):
        cases = {"pos2mm_yaw1mrad_vel8mm": dict(pos=0.002, yaw=0.001, vel=0.008),
                 "yaw_drift_0.5deg_min": dict(yaw_drift=DRIFT_05_DEG_MIN),
                 "pos2mm+drift+walk": dict(pos=0.002, yaw=0.001, vel=0.008, yaw_drift=DRIFT_05_DEG_MIN, walk=0.0005)}
        for name, kw in cases.items():
            with self.subTest(case=name):
                clock = ManualClock()
                if "yaw_drift" in kw:
                    # round 07 second fix pass (Q7b-01): with the default yaw_total_rad (0.05 rad,
                    # as v0.13.0) an IMU drift is re-stopped every 0.05 rad -- bounded, never
                    # escalated; the field calibration (yaw_total_rad) makes it 0
                    runtime, lm = self._still(NoisyQuad(clock=clock, **kw), clock, 900.0)
                    self.assertLessEqual(lm["restops"], 3, lm["last_evidence"])
                    self.assertEqual(set(lm["last_evidence"]), {"base_drift_yaw_total_rad"})
                    self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))
                    clock = ManualClock()
                    runtime = make_runtime(self, NoisyQuad(clock=clock, **kw), start_monitor=False,
                                           latch_guard_overrides={"yaw_total_rad": 0.3})
                    runtime.emergency_stop("operator")
                    tick_for(runtime, runtime.adapter, clock, 900.0)
                    lm = runtime.latch_guard.describe()
                else:
                    runtime, lm = self._still(NoisyQuad(clock=clock, **kw), clock, 900.0)
                self.assertEqual(lm["restops"], 0, lm["last_evidence"])
                self.assertFalse(events_of(runtime, "safety.motion_while_latched"))
                self.assertTrue(runtime.supervisor.latched)
        self.assertGreater(lm["rebases"], 10)                 # slow drift re-based, not reported
        self.assertGreater(lm["noise_used"]["m"], 0.0015)     # measured on the feedback itself

    def test_noisy_arm_no_false_alarm(self):
        clock = ManualClock()
        _rt, lm = self._still(NoisyArm(q=0.002, qd=0.01, clock=clock), clock, 600.0)
        self.assertEqual(lm["restops"], 0, lm["last_evidence"])

    def test_creep_still_caught_under_noise(self):
        class NoisyCreeper(Creeper):
            rng = random.Random(3)

            def read(self, cap_id):
                out = super().read(cap_id)
                if cap_id == "base.odometry":
                    out["pose"][0] += self.rng.gauss(0, 0.002)
                    out["pose"][1] += self.rng.gauss(0, 0.002)
                return out
        # Round 07 third fix pass (Q7c-01): with the default configuration the silent precautionary
        # stops replicate v0.13.0's noise-driven re-stops every 0.5 s, so this driver (resumes 2 s
        # after each stop) never gets going at all -- 0 m, as under v0.13.0. The guard's own counted
        # rule is checked with the precautionary stops off, unchanged.
        from dataclasses import replace
        from neuxmind_runtime.latch_guard import DEFAULT_CONFIG
        NoisyCreeper.rng = random.Random(3)
        clock = ManualClock()
        adapter = NoisyCreeper(0.015, clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        x0 = adapter.x
        tick_for(runtime, adapter, clock, 120.0)
        self.assertLess(abs(adapter.x - x0), 0.003)
        self.assertGreater(runtime.latch_guard.describe()["precautionary_stops"], 0)

        NoisyCreeper.rng = random.Random(3)
        clock = ManualClock()
        adapter = NoisyCreeper(0.015, clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False,
                               latch_guard=replace(DEFAULT_CONFIG, precaution_interval_s=0.0))
        runtime.emergency_stop("operator")
        x0, first = adapter.x, []
        tick_for(runtime, adapter, clock, 120.0,
                 lambda i: first or not runtime.latch_guard.restops or first.append((i * 0.1, adapter.x - x0)))
        self.assertTrue(first)
        self.assertLess(first[0][0], 7.0)
        self.assertLess(first[0][1], 0.07)
        self.assertLess(adapter.x - x0, 0.8)                   # unguarded: ~1.8 m in 120 s


# ====================================================================== Q5-01 regression + Q6-04
class CreepAndEscalation(unittest.TestCase):
    def _creep(self, speed, seconds, delay=2.0, **guard):
        clock = ManualClock()
        adapter = Creeper(speed, delay=delay, clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False,
                               **({"latch_guard": GuardConfig(**guard)} if guard else {}))
        runtime.emergency_stop("operator")
        x0 = adapter.x
        tick_for(runtime, adapter, clock, seconds)
        return runtime, adapter, adapter.x - x0

    def test_resuming_driver_escalated_once(self):
        runtime, _ad, total = self._creep(0.019, 600.0)
        lm = runtime.latch_guard.describe()
        self.assertLess(total, 1.5)
        nob = events_of(runtime, "safety.stop_not_obeyed")
        self.assertEqual(len(nob), 1)
        self.assertIn("硬件急停", nob[0]["note"])
        self.assertGreaterEqual(nob[0]["motion_restops"], 10)
        self.assertGreaterEqual(nob[0]["since_s"], 30.0)
        self.assertIs(lm["stop_not_obeyed"], True)                       # desktop contract: bool
        self.assertIsInstance(lm["stop_not_obeyed_at"], float)
        self.assertIn("驱动不服从停止", lm["stop_not_obeyed_detail"]["message"])
        self.assertIn("驱动不服从停止", nob[0]["message"])
        self.assertIsInstance(nob[0]["ts"], float)
        self.assertGreater(nob[0]["displacement_m"], 0.05)
        self.assertEqual(nob[0]["restops"], lm["stop_not_obeyed_detail"]["restops"])
        # aggregated: ~1 event per 10 s instead of one per re-stop (271 re-stops)
        ev = events_of(runtime, "safety.motion_while_latched")
        self.assertLess(len(ev), 70)
        self.assertGreater(lm["restops"], 200)
        self.assertEqual(sum(e["count"] for e in ev) + runtime.latch_guard._ev["motion"]["pending"], lm["restops"])
        self.assertEqual(lm["events_suppressed"], lm["restops"] - sum(1 for e in ev if not e.get("summary")))

    def test_never_obeying_driver_ring_not_flooded(self):
        runtime, _ad, _total = self._creep(0.3, 3600.0, delay=0.0)
        lm = runtime.latch_guard.describe()
        self.assertGreater(lm["restops"], 5000)
        self.assertLess(len(events_of(runtime, "safety.motion_while_latched")), 400)
        self.assertEqual(len(events_of(runtime, "safety.stop_not_obeyed")), 1)

    def test_controlled_sit_down_not_escalated(self):
        from test_iter06_runtime import SitOnStop
        clock = ManualClock()
        adapter = SitOnStop(3.0, clock=clock)           # re-plans on every stop()
        runtime = make_runtime(self, adapter, start_monitor=False)
        runtime.emergency_stop("operator")
        tick_for(runtime, adapter, clock, 60.0)
        self.assertFalse(events_of(runtime, "safety.stop_not_obeyed"))

    def test_new_counted_stop_rearms_escalation(self):
        runtime, adapter, _ = self._creep(0.019, 60.0)
        self.assertEqual(len(events_of(runtime, "safety.stop_not_obeyed")), 1)
        runtime.emergency_stop("operator again")
        clock = adapter.clock
        tick_for(runtime, adapter, clock, 1.0)
        self.assertIs(runtime.latch_guard.describe()["stop_not_obeyed"], False)
        self.assertIsNone(runtime.latch_guard.describe()["stop_not_obeyed_at"])


# ====================================================================== calibration
class DeclaresGuard(SimQuadruped):
    latch_guard = {"pos_m": 0.02, "noise_m": 0.002, "settle_s": 3.0, "escalate_after": 5}

    def manifest(self):
        m = super().manifest()
        m["safety"] = {**m["safety"], "latch_guard": dict(self.latch_guard)}
        return m


class Calibration(unittest.TestCase):
    def test_spec_normalize(self):
        out, notes = spec_manifest.normalize_latch_guard({"pos_m": 5, "settle_s": 0.2, "noise_m": 0.002})
        self.assertEqual(out, {"pos_m": 0.2, "stop_settle_s": 1.0, "noise_m": 0.002})
        self.assertEqual(len(notes), 2)
        out, notes = spec_manifest.normalize_latch_guard({"pos_m": 0.01, "noise_m": 0.005})
        self.assertEqual(out["pos_m"], 0.03)                 # >= 6 x declared noise
        for bad in ({"pos_m": True}, {"pos_m": float("nan")}, {"pos_m": "0.1"}, {"bogus": 1},
                    {"escalate_after": 2.5}, [], {"stop_settle_s": 2, "settle_s": 3}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                spec_manifest.normalize_latch_guard(bad)

    def test_manifest_declaration_used(self):
        clock = ManualClock()
        runtime = make_runtime(self, DeclaresGuard(clock=clock), start_monitor=False)
        lm = runtime.status()["latched_motion"]
        self.assertEqual(lm["config"]["pos_m"], 0.02)
        self.assertEqual(lm["config"]["noise_m"], 0.002)
        self.assertEqual(lm["config"]["escalate_after"], 5)
        self.assertEqual(lm["settle_s"], 3.0)
        self.assertEqual(lm["config_source"]["pos_m"], "manifest")
        self.assertEqual(lm["config_source"]["yaw_rad"], "default")

    def test_manifest_clamped_and_invalid_refused(self):
        class Wild(DeclaresGuard):
            latch_guard = {"pos_m": 9.0, "stop_settle_s": 60}
        runtime = make_runtime(self, Wild(), start_monitor=False)
        lm = runtime.latch_guard.describe()
        self.assertEqual((lm["config"]["pos_m"], lm["settle_s"]), (0.2, 5.0))
        self.assertEqual(len(lm["config_notes"]), 2)

        class Bad(DeclaresGuard):
            latch_guard = {"pos_m": "far"}
        with self.assertRaises(ValueError):
            make_runtime(self, Bad(), start_monitor=False)

    def test_runtime_override_wins(self):
        runtime = make_runtime(self, DeclaresGuard(), start_monitor=False,
                               latch_guard_overrides={"pos_m": 0.08, "stop_settle_s": 2.0})
        lm = runtime.latch_guard.describe()
        self.assertEqual((lm["config"]["pos_m"], lm["settle_s"]), (0.08, 2.0))
        self.assertEqual(lm["config_source"]["pos_m"], "runtime")
        self.assertEqual(lm["config_source"]["noise_m"], "manifest")
        with self.assertRaises(ValueError):
            make_runtime(self, SimQuadruped(), start_monitor=False, latch_guard_overrides={"pos_m": float("inf")})

    def test_declared_bound_changes_detection(self):
        clock = ManualClock()
        adapter = Creeper(0.015, clock=clock)
        runtime = make_runtime(self, adapter, start_monitor=False, latch_guard_overrides={"pos_m": 0.02})
        runtime.emergency_stop("operator")
        x0, first = adapter.x, []
        tick_for(runtime, adapter, clock, 20.0,
                 lambda i: first or not runtime.latch_guard.restops or first.append(adapter.x - x0))
        self.assertLessEqual(first[0], 0.03)

    def test_cli_parse(self):
        self.assertEqual(parse_overrides(["pos_m=0.08", "escalate_after=12", '{"noise_m": 0.002}']),
                         {"pos_m": 0.08, "escalate_after": 12, "noise_m": 0.002})
        for bad in (["pos_m"], ["pos_m=abc"], ['{"pos_m": NaN}'], ["[1]"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_overrides(bad)
        import io
        import contextlib
        from unittest import mock
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict("os.environ", {"BRAIN_EDGE_TOKEN": TOKEN}):
            rc = cli.main(["serve", "--adapter", "sim_quadruped", "--latch-guard", "pos_m=oops"])
        self.assertEqual(rc, 2)
        self.assertIn("--latch-guard", err.getvalue())

    def test_ros2_config_carries_calibration(self):
        config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
        config["limits"].update({"max_vx": 0.5, "max_wz": 1.0})
        config["latch_guard"] = {"stop_settle_s": 3.0, "noise_m": 0.002}
        m = spec_manifest.validate_manifest_v2(ros2.build_manifest(config))
        self.assertEqual(m["safety"]["latch_guard"], {"stop_settle_s": 3.0, "noise_m": 0.002})
        config["latch_guard"] = {"stop_settle_s": "long"}
        with self.assertRaises(ValueError):
            ros2.build_manifest(config)


# ====================================================================== Q6-02/06/08 parameters
def code_of(given, decl):
    try:
        check_parameters(decl, given)
    except BadRequest as exc:
        body = exc.to_dict()
        return body.get("code"), body.get("param")
    return None


class ParameterConstraints(unittest.TestCase):
    def test_object_and_pose_elements(self):
        wp = [{"name": "waypoints", "type": "object", "min": -5, "max": 5}]
        self.assertEqual(code_of({"waypoints": [[100, 0]]}, wp), ("param_out_of_range", "waypoints"))
        self.assertEqual(code_of({"waypoints": {"a": [[0, -5.5]]}}, wp), ("param_out_of_range", "waypoints"))
        self.assertIsNone(code_of({"waypoints": [[1, 2], [-5, 5]], }, wp))
        # Q7-07 (round 07 fix pass): with min/max declared only JSON numbers are allowed inside
        self.assertEqual(code_of({"waypoints": [["label", True]]}, wp), ("param_type", "waypoints"))
        pose = [{"name": "p", "type": "pose", "min": -1, "max": 1}]
        self.assertEqual(code_of({"p": [50, 0, 0, 0, 0, 0, 1]}, pose), ("param_out_of_range", "p"))

    def test_minimum_maximum_aliases(self):
        decl = [{"name": "speed", "type": "float", "minimum": 0.1, "maximum": 0.5}]
        self.assertEqual(code_of({"speed": 9.0}, decl), ("param_out_of_range", "speed"))
        with self.assertRaises(BadRequest) as ctx:
            check_parameters(decl, {"speed": 9.0})
        self.assertEqual((ctx.exception.to_dict()["min"], ctx.exception.to_dict()["max"]), (0.1, 0.5))
        self.assertEqual(check_parameters(decl, {"speed": 0.3}), {"speed": 0.3})
        self.assertEqual(code_of({}, [{"name": "s", "type": "float", "min": 0, "minimum": 1}])[0],
                         "param_declaration_invalid")

    def test_min_max_on_unsupported_types_refused(self):
        for decl in ({"name": "s", "type": "str", "min": 1, "max": 3},
                     {"name": "b", "type": "bool", "max": 1},
                     {"name": "e", "type": "enum", "choices": [1, 2], "min": 1},
                     {"name": "x", "type": "float", "minimum": 2, "maximum": 1},
                     {"name": "c", "type": "str", "choices": ["a", float("nan")]}):
            with self.subTest(decl=decl):
                self.assertEqual(code_of({}, [decl])[0], "param_declaration_invalid")

    def test_choices_typed(self):
        enum = [{"name": "m", "type": "enum", "choices": [1, 2]}]
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                self.assertEqual(code_of({"m": value}, enum), ("param_choice", "m"))
        self.assertIsNone(code_of({"m": 1}, enum))
        obj = [{"name": "o", "type": "object", "choices": [0, 1, [1, 2]]}]
        self.assertEqual(code_of({"o": False}, obj), ("param_choice", "o"))
        self.assertEqual(code_of({"o": [True, 2]}, obj), ("param_choice", "o"))
        self.assertIsNone(code_of({"o": [1, 2]}, obj))

    def test_non_finite_and_deep(self):
        obj = [{"name": "o", "type": "object"}]
        self.assertEqual(code_of({"o": [1, [float("inf")]]}, obj), ("param_type", "o"))
        deep = []
        for _ in range(5000):
            deep = [deep]
        self.assertEqual(code_of({"o": deep}, obj), ("param_type", "o"))

    def test_deep_json_over_http_is_400(self):
        clock = ManualClock()
        runtime = make_runtime(self, SimQuadruped(clock=clock))
        server = GatewayServer(runtime, TOKEN, port=0).start()
        self.addCleanup(server.close)
        import urllib.request
        import urllib.error
        body = '{"request_id":"d1","hardware_authorized":true,"parameters":{"o":%s}}' % ("[" * 200000 + "]" * 200000)
        req = urllib.request.Request(server.url + "/v2/skills/x/run", data=body.encode(),
                                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read())
        self.assertEqual(body["code"], "bad_request")
        self.assertIn("嵌套过深", body["error"])
        self.assertEqual(http(server.url, "/v1/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
