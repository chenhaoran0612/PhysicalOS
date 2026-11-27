import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from neuxmind_spec import capabilities as caps  # noqa: E402
from neuxmind_spec import manifest as mf  # noqa: E402


def quadruped():
    return {"schema": "robot.adapter.v2", "instance_id": "dog-1", "name": "Dog", "family": "quadruped",
            "adapter_version": "0.1.0", "vendor": "acme", "model": "q1",
            "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": True}},
                             {"id": "system.health"},
                             {"id": "base.velocity", "params": {"max_vx": 1.0, "max_vy": 0.5, "max_wz": 1.5, "holonomic": True}},
                             {"id": "legged.posture"}]}


class SpecTest(unittest.TestCase):
    def test_registry_serialisable(self):
        data = caps.registry()
        json.dumps(data)
        self.assertIn("safety.stop", [c["id"] for c in data["capabilities"]])

    def test_manifest_v2_and_v1_view(self):
        m = mf.validate_manifest_v2(quadruped())
        v1 = mf.manifest_v1_view(m)
        self.assertEqual(v1["schema"], "robot.adapter.v1")
        self.assertIn("base.velocity", v1["capabilities"])

    def test_mandatory_required(self):
        bad = quadruped()
        bad["capabilities"] = [c for c in bad["capabilities"] if c["id"] != "safety.stop"]
        with self.assertRaises(ValueError):
            mf.validate_manifest_v2(bad)

    def test_unknown_param_rejected(self):
        bad = quadruped()
        bad["capabilities"][2]["params"]["warp"] = 9
        with self.assertRaises(ValueError):
            mf.validate_manifest_v2(bad)

    def test_param_limits_fail_closed(self):
        for bad_value in (-1.0, 0.0, float("nan"), float("inf"), "1.0", True):
            bad = quadruped()
            bad["capabilities"][2]["params"]["max_vx"] = bad_value
            with self.assertRaises(ValueError, msg=repr(bad_value)):
                mf.validate_manifest_v2(bad)

    def test_joint_limits(self):
        def arm(params):
            m = quadruped()
            m["family"] = "manipulator"
            m["capabilities"] = m["capabilities"][:2] + [{"id": "arm.joint_position", "params": params}]
            return m
        ok = {"dof": 2, "lower": [-1.0, -1.0], "upper": [1.0, 1.0], "max_velocity": 1.0}
        mf.validate_manifest_v2(arm(ok))
        for broken in ({**ok, "lower": [-1.0]}, {**ok, "upper": [1.0, -2.0]}, {k: v for k, v in ok.items() if k != "dof"},
                       {k: v for k, v in ok.items() if k != "upper"}):
            with self.assertRaises(ValueError):
                mf.validate_manifest_v2(arm(broken))

    def test_arm_joint_names(self):            # v1.1 optional param (iter04 R4-01)
        m = quadruped()
        m["family"] = "manipulator"
        ok = {"dof": 2, "lower": [-1.0, -1.0], "upper": [1.0, 1.0], "max_velocity": 1.0}
        m["capabilities"] = m["capabilities"][:2] + [{"id": "arm.joint_position", "params": ok}]
        mf.validate_manifest_v2(copy.deepcopy(m))                                  # still optional
        m["capabilities"][2]["params"]["joint_names"] = ["a", "b"]
        mf.validate_manifest_v2(copy.deepcopy(m))
        for broken in (["a"], ["a", "a"], ["a", ""], "ab", ["a", 1]):
            m["capabilities"][2]["params"]["joint_names"] = broken
            with self.assertRaises(ValueError, msg=repr(broken)):
                mf.validate_manifest_v2(copy.deepcopy(m))

    def test_v1_view_keeps_simulated(self):
        m = quadruped()
        m["simulated"] = True
        self.assertTrue(mf.manifest_v1_view(mf.validate_manifest_v2(m))["simulated"])

    def test_payload_validation(self):
        caps.validate_payload("base.velocity", {"vx": 0.2, "wz": 0.0})
        with self.assertRaises(ValueError):
            caps.validate_payload("base.velocity", {"vx": "fast", "wz": 0})
        with self.assertRaises(ValueError):
            caps.validate_payload("base.velocity", {"vx": 0.1, "wz": 0, "boost": 1})
        with self.assertRaises(ValueError):
            caps.validate_payload("arm.ee_pose", {"pose": [0, 0, 0]})

    def test_compatibility(self):
        m = mf.validate_manifest_v2(quadruped())
        skill = mf.validate_skill_package({"schema": "skill.package.v1", "id": "patrol", "version": "0.1.0",
                                           "entry": "skill.py:run", "requires": ["base.velocity", "safety.stop"],
                                           "constraints": {"base.velocity": {"max_vx": {"min": 0.5}}}})
        self.assertTrue(mf.check_compatibility(skill, m)["compatible"])
        arm_skill = dict(skill, requires=[{"id": "arm.joint_position", "major": 1}])
        r = mf.check_compatibility(arm_skill, m)
        self.assertFalse(r["compatible"])
        self.assertEqual(r["missing"], ["arm.joint_position"])
        fast = dict(skill, constraints={"base.velocity": {"max_vx": {"min": 2.0}}})
        self.assertFalse(mf.check_compatibility(fast, m)["compatible"])

    def test_vendor_extension_is_motion(self):
        self.assertTrue(mf.is_motion("x.acme.dance"))
        self.assertTrue(mf.is_motion("base.velocity"))
        self.assertFalse(mf.is_motion("system.health"))

    def test_legacy_alias_and_vendor_ext(self):
        self.assertEqual(caps.normalize("cmd_vel"), "base.velocity")
        self.assertEqual(caps.normalize("x.acme.dance"), "x.acme.dance")
        with self.assertRaises(ValueError):
            caps.normalize("teleport")



class LatchGuardCalibration(unittest.TestCase):
    """Round 07 (Q6-01/Q6-05): manifest safety.latch_guard is validated and clamped."""

    def test_clamp_alias_and_noise_margin(self):
        out, notes = mf.normalize_latch_guard({"pos_m": 1.0, "settle_s": 9, "noise_rad": 0.01, "escalate_after": 12})
        self.assertEqual(out["pos_m"], 0.2)
        self.assertEqual(out["stop_settle_s"], 5.0)
        self.assertEqual(out["escalate_after"], 12)
        self.assertAlmostEqual(out["yaw_rad"], 0.06)       # raised to 6 x noise_rad
        self.assertAlmostEqual(out["joint_rad"], 0.06)
        self.assertGreaterEqual(len(notes), 4)

    def test_refused(self):
        for bad in ({"pos_m": None}, {"pos_m": True}, {"pos_m": float("inf")}, {"x": 1}, {"escalate_after": 3.5}, "0.1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mf.normalize_latch_guard(bad)

    def test_manifest_carries_normalised_values(self):
        m = {"schema": "robot.adapter.v2", "instance_id": "r", "name": "r", "adapter_version": "0.1.0",
             "family": "mobile", "safety": {"latch_guard": {"settle_s": 3}},
             "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}}, {"id": "system.health"}]}
        out = mf.validate_manifest_v2(m)
        self.assertEqual(out["safety"]["latch_guard"], {"stop_settle_s": 3.0})
        m["safety"]["latch_guard"] = {"pos_m": "far"}
        with self.assertRaises(ValueError):
            mf.validate_manifest_v2(m)


class ResetExpected(unittest.TestCase):
    """Round 07 second fix pass (Q7b-06): safety.reset_expected_s is validated by the spec."""

    def _m(self, value):
        return {"schema": "robot.adapter.v2", "instance_id": "r", "name": "r", "adapter_version": "0.1.0",
                "family": "mobile", "safety": {"reset_expected_s": value},
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}}, {"id": "system.health"}]}

    def test_valid(self):
        for value, expected in ((10, 10.0), (2.5, 2.5), (25, 25.0), (1e-9, 1e-9)):
            with self.subTest(value=value):
                self.assertEqual(mf.validate_manifest_v2(self._m(value))["safety"]["reset_expected_s"], expected)

    def test_refused(self):
        # Q7c-07 (round 07 third fix pass): (0, 25] -- the desktop's expire_s = value + 5 must fit
        # under the runtime's 30 s cap (25.0001 / 26 / 30 refused; 30 was accepted before)
        for bad in (0, -1, 25.0001, 26, 30, 30.5, 45, float("nan"), float("inf"), True, "10", None, [10]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mf.validate_manifest_v2(self._m(bad))


class NamedPlacesAndExplore(unittest.TestCase):
    """Issue #38 (spec 1.2): go to a place the body knows by name, and walk without a map."""

    def _m(self, *capabilities):
        return {"schema": "robot.adapter.v2", "instance_id": "dog-1", "name": "Dog", "adapter_version": "0.1.0",
                "family": "quadruped",
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}},
                                 {"id": "system.health"}, *capabilities]}

    def test_spec_version_is_past_1_2(self):
        self.assertGreaterEqual(tuple(int(x) for x in caps.SPEC_VERSION.split(".")), (1, 2))

    def test_both_are_motion_actions(self):
        for cap_id in ("base.navigate_to_place", "base.explore"):
            with self.subTest(cap_id=cap_id):
                cap = caps.get(cap_id)
                self.assertEqual((cap.kind, cap.safety, cap.major), ("action", "motion", 1))
                self.assertTrue(mf.is_motion(cap_id))

    def test_place_payloads(self):
        caps.validate_payload("base.navigate_to_place", {"place": "会议室"})
        caps.validate_payload("base.navigate_to_place", {"place": "会议室", "max_speed": 0.5})
        caps.validate_payload("base.navigate_to_place", {"reached": True, "place": "会议室"}, "outputs")
        for bad in ({}, {"place": 3}, {"place": "会议室", "goal": [0, 0, 0, 0, 0, 0, 1]},
                    {"place": "会议室", "max_speed": 0}, {"place": "会议室", "max_speed": float("nan")}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    caps.validate_payload("base.navigate_to_place", bad)

    def test_explore_payloads(self):
        caps.validate_payload("base.explore", {"duration_s": 60.0})
        caps.validate_payload("base.explore", {"duration_s": 60, "radius": 5.0, "max_distance": 20.0, "max_speed": 0.4})
        caps.validate_payload("base.explore", {"completed": True, "travelled": 12.5}, "outputs")
        for bad in ({}, {"duration_s": 0}, {"duration_s": -5}, {"duration_s": 60, "radius": 0},
                    {"duration_s": 60, "max_distance": -1}, {"duration_s": "60"}, {"duration_s": 60, "speed": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    caps.validate_payload("base.explore", bad)

    def test_manifest_declares_its_places(self):
        out = mf.validate_manifest_v2(self._m(
            {"id": "base.navigate_to_place", "params": {"places": ["会议室", "二楼"]}},
            {"id": "base.explore", "params": {"max_duration_s": 600.0, "max_radius": 10.0}}))
        declared = {c["id"]: c["params"] for c in out["capabilities"]}
        self.assertEqual(declared["base.navigate_to_place"]["places"], ["会议室", "二楼"])

    def test_places_must_be_a_list_of_distinct_names(self):
        for bad in (None, [], "会议室", ["会议室", "会议室"], ["会议室", ""], ["会议室", 3], {"会议室": [0, 0]},
                    ["  "], ["x" * 101], ["a\nb"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mf.validate_manifest_v2(self._m({"id": "base.navigate_to_place", "params": {"places": bad}}))

    def test_a_place_capability_without_places_is_refused(self):
        with self.assertRaises(ValueError):
            mf.validate_manifest_v2(self._m({"id": "base.navigate_to_place"}))

    def test_explore_limits_fail_closed(self):
        for params in ({"max_duration_s": 0}, {"max_duration_s": float("inf")}, {"max_radius": -1.0}, {"warp": 1}):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    mf.validate_manifest_v2(self._m({"id": "base.explore", "params": params}))

    def test_skill_can_require_them(self):
        skill = mf.validate_skill_package({
            "schema": "skill.package.v1", "id": "goto-place", "version": "0.1.0", "title": "去指定地点",
            "entry": "skill.py:run", "requires": [{"id": "base.navigate_to_place"}, {"id": "safety.stop"}]})
        with_places = self._m({"id": "base.navigate_to_place", "params": {"places": ["会议室"]}})
        self.assertTrue(mf.check_compatibility(skill, with_places)["compatible"])
        self.assertFalse(mf.check_compatibility(skill, self._m())["compatible"])



class TrainedSkillModels(unittest.TestCase):
    """Issue #67 (spec 1.3): a model trained for a body is run by the body, chosen by name."""

    def _m(self, *capabilities):
        return {"schema": "robot.adapter.v2", "instance_id": "duck-1", "name": "Duck", "adapter_version": "0.1.0",
                "family": "humanoid",
                "capabilities": [{"id": "safety.stop", "params": {"hardware_estop": False}},
                                 {"id": "system.health"}, *capabilities]}

    def test_spec_version_is_1_3(self):
        self.assertEqual(caps.SPEC_VERSION, "1.3")

    def test_it_is_a_motion_action(self):
        cap = caps.get("policy.run")
        self.assertEqual((cap.kind, cap.safety, cap.major), ("action", "motion", 1))
        self.assertTrue(mf.is_motion("policy.run"))

    def test_payloads(self):
        caps.validate_payload("policy.run", {"name": "bow"})
        caps.validate_payload("policy.run", {"name": "bow", "seed": 0})
        caps.validate_payload("policy.run", {"name": "bow", "seed": 1700003000})
        caps.validate_payload("policy.run", {"completed": True, "name": "bow", "duration_s": 4.54, "steps": 227}, "outputs")
        caps.validate_payload("policy.run", {"completed": False, "reason": "fell"}, "outputs")
        for bad in ({}, {"name": 3}, {"name": "bow", "seed": -1}, {"name": "bow", "seed": 1.5}, {"name": "bow", "seed": True},
                    {"name": "bow", "speed": 2.0}, {"seed": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    caps.validate_payload("policy.run", bad)

    def test_manifest_declares_its_models(self):
        out = mf.validate_manifest_v2(self._m({"id": "policy.run", "params": {"policies": ["bow", "kick"], "max_duration_s": 30.0}}))
        declared = {c["id"]: c["params"] for c in out["capabilities"]}
        self.assertEqual(declared["policy.run"]["policies"], ["bow", "kick"])

    def test_models_must_be_a_list_of_distinct_names(self):
        for bad in (None, [], "bow", ["bow", "bow"], ["bow", ""], ["bow", 3], {"bow": 1}, ["  "], ["x" * 101], ["a\nb"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mf.validate_manifest_v2(self._m({"id": "policy.run", "params": {"policies": bad}}))

    def test_the_capability_without_models_is_refused(self):
        with self.assertRaises(ValueError):
            mf.validate_manifest_v2(self._m({"id": "policy.run"}))

    def test_limits_fail_closed(self):
        for extra in ({"max_duration_s": 0}, {"max_duration_s": float("inf")}, {"warp": 1}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    mf.validate_manifest_v2(self._m({"id": "policy.run", "params": {"policies": ["bow"], **extra}}))

    def test_a_skill_can_require_it(self):
        skill = mf.validate_skill_package({
            "schema": "skill.package.v1", "id": "bow", "version": "1.0.0", "title": "鞠躬",
            "entry": "skill.py:run", "requires": [{"id": "policy.run"}, {"id": "safety.stop"}]})
        self.assertTrue(mf.check_compatibility(skill, self._m({"id": "policy.run", "params": {"policies": ["bow"]}}))["compatible"])
        self.assertFalse(mf.check_compatibility(skill, self._m())["compatible"])


if __name__ == "__main__":
    unittest.main()
