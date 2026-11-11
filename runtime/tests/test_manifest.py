import unittest

import _support  # noqa: F401
from neuxmind_spec import manifest as spec_manifest

from neuxmind_runtime.adapters import load_adapter
from neuxmind_runtime.adapters.sim_arm import SimArm
from neuxmind_runtime.adapters.sim_quadruped import SimQuadruped
from neuxmind_runtime.bus import CapabilityBus
from neuxmind_runtime.hal import RobotAdapter


class BrokenAdapter(RobotAdapter):
    """Missing the mandatory safety.stop capability."""

    def manifest(self):
        return {"schema": "robot.adapter.v2", "instance_id": "x", "name": "broken", "family": "mobile",
                "adapter_version": "0.1.0", "capabilities": [{"id": "system.health"}]}

    def read(self, cap_id):
        return {"ok": True, "mode": "x"}

    def invoke(self, cap_id, args):
        return {}

    def start_action(self, cap_id, args):
        raise NotImplementedError

    def stop(self):
        pass


class ManifestTests(unittest.TestCase):
    def test_sim_manifests_are_valid_and_marked_simulated(self):
        for adapter in (SimQuadruped(), SimArm()):
            m = spec_manifest.validate_manifest_v2(adapter.manifest())
            self.assertTrue(m["simulated"])
            self.assertIn("Sim", m["name"])
            ids = {c["id"] for c in m["capabilities"]}
            self.assertTrue({"safety.stop", "system.health"} <= ids)

    def test_quadruped_and_arm_capability_sets(self):
        quad = {c["id"] for c in SimQuadruped().manifest()["capabilities"]}
        arm = {c["id"] for c in SimArm().manifest()["capabilities"]}
        # iter04 review: the quadruped reports localization.pose (sim map = odom) and the arm gripper.state,
        # so the conformance suite can verify navigation and gripper stops (Q4-04, Q4-11).
        self.assertEqual(quad, {"safety.stop", "system.health", "sensor.battery", "sensor.imu", "base.odometry",
                                "localization.pose", "base.velocity", "base.navigate_to", "legged.posture"})
        self.assertEqual(arm, {"safety.stop", "system.health", "joint.state", "arm.joint_position",
                               "gripper.position", "gripper.state"})

    def test_v1_view_is_accepted_by_desktop_contract(self):
        from brainos.contracts import validate_manifest
        for adapter in (SimQuadruped(), SimArm()):
            v1 = spec_manifest.manifest_v1_view(spec_manifest.validate_manifest_v2(adapter.manifest()))
            self.assertEqual(validate_manifest(v1), v1)   # identity → digests agree
            # The v1 view carries the simulation flag and vendor/model (iter01-fix, R-08),
            # so the desktop manifest digest distinguishes simulation from hardware.
            self.assertIs(v1["simulated"], True)
            self.assertEqual((v1["vendor"], v1["model"]), (adapter.manifest()["vendor"], adapter.manifest()["model"]))

    def test_bus_rejects_invalid_manifest(self):
        with self.assertRaises(ValueError):
            CapabilityBus(BrokenAdapter())

    def test_load_adapter_by_name_and_module(self):
        self.assertIsInstance(load_adapter("sim_arm", time_scale=5), SimArm)
        quad = load_adapter("neuxmind_runtime.adapters.sim_quadruped:SimQuadruped")
        self.assertEqual(quad.manifest()["family"], "quadruped")
        with self.assertRaises(ValueError):
            load_adapter("no_such_adapter")


if __name__ == "__main__":
    unittest.main()
