"""Package building/validation, compatibility explanations, templates, local skill testing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support
from _support import REPO, RUNTIME_EXAMPLES, SDK_DIR, make_skill

from neuxmind import NeuxMindError
from neuxmind.package import build_package, explain_compatibility, format_compatibility, load_skill, validate_package
from neuxmind.skill import ActionFailed, Context, Skill, SkillStopped
from neuxmind.skill.testing import SkillHarness, sim_manifest
from neuxmind.templates import TEMPLATES, class_name, create, render


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-pkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_build_matches_runtime_shape_and_skips_tests(self):
        skill_dir = create("patrol", "pkg-patrol", self.tmp / "pkg-patrol")
        (skill_dir / "lib").mkdir()
        (skill_dir / "lib" / "util.py").write_text("X = 1\n")
        (skill_dir / "__pycache__").mkdir()
        (skill_dir / "__pycache__" / "skill.cpython-313.pyc").write_bytes(b"\x00\x01")
        (skill_dir / ".secret").write_text("nope")
        package = build_package(skill_dir)
        self.assertEqual(package["schema"], "skill.package.v1")
        self.assertEqual(sorted(package["files"]), ["README.md", "lib/util.py", "skill.py"])
        self.assertEqual(validate_package(package)["id"], "pkg-patrol")

    def test_runtime_examples_are_valid(self):
        for name in ("patrol", "status_report", "wave"):
            _package, skill = load_skill(RUNTIME_EXAMPLES / name)
            self.assertEqual(skill["id"], name)

    def test_local_validation_errors(self):
        good = build_package(create("blank", "pkg-blank", self.tmp / "pkg-blank"))
        cases = {
            "entry file missing": {**good, "entry": "other.py:run"},
            "bad entry format": {**good, "entry": "skill.py"},
            "bad id": {**good, "id": "Bad ID"},
            "no requires": {**good, "requires": []},
            "default type mismatch": {**good, "parameters": [{"name": "n", "type": "int", "default": "x"}]},
            "path traversal": {**good, "files": {**good["files"], "../evil.py": "x"}},
            "binary content": {**good, "files": {**good["files"], "a.txt": b"x"}},
            "unknown capability": {**good, "requires": [{"id": "teleport.now"}]},
        }
        for label, package in cases.items():
            with self.subTest(label), self.assertRaises(NeuxMindError) as cm:
                validate_package(package)
            self.assertEqual(cm.exception.code, "invalid_package")
        bad = self.tmp / "bin-skill"
        make_skill(self.tmp, "bin-skill", "def run(ctx):\n    return {}\n")
        (bad / "blob.bin").write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(NeuxMindError):
            build_package(bad)

    def test_explain_cross_body(self):
        _p, patrol = load_skill(RUNTIME_EXAMPLES / "patrol")
        dog, arm = sim_manifest("quadruped"), sim_manifest("arm")
        ok = explain_compatibility(patrol, dog)
        self.assertTrue(ok["compatible"])
        self.assertTrue(ok["simulated"])
        no = explain_compatibility(patrol, arm)
        self.assertFalse(no["compatible"])
        self.assertEqual(no["missing"], ["base.navigate_to"])
        self.assertIn("缺少必需能力 base.navigate_to（导航到目标）", no["reasons"])
        self.assertIn("不兼容", format_compatibility(no, "arm"))

    def test_explain_parameter_violation(self):
        _p, wave = load_skill(RUNTIME_EXAMPLES / "wave")
        arm = sim_manifest("arm")
        small = json.loads(json.dumps(arm))
        for cap in small["capabilities"]:
            if cap["id"] == "arm.joint_position":
                cap["params"]["dof"] = 4
        report = explain_compatibility(wave, small)
        self.assertFalse(report["compatible"])
        self.assertEqual(report["missing"], [])
        self.assertEqual(len(report["violations"]), 1)
        self.assertTrue(any("dof" in r and "≥ 6" in r and "4" in r for r in report["reasons"]), report["reasons"])
        undeclared = json.loads(json.dumps(arm))
        for cap in undeclared["capabilities"]:
            if cap["id"] == "arm.joint_position":
                cap["params"].pop("dof")
        report = explain_compatibility(wave, undeclared)
        self.assertFalse(report["compatible"])
        self.assertTrue(any("没有声明" in r for r in report["reasons"]))


class SkillApiTests(unittest.TestCase):
    def test_skill_base_and_exceptions_are_the_runtimes(self):
        from neuxmind_runtime import skills as rt

        self.assertTrue(issubclass(Skill, rt.Skill))
        self.assertIs(ActionFailed, rt.ActionFailed)
        self.assertIs(SkillStopped, rt.SkillStopped)
        self.assertTrue(issubclass(SkillStopped, BaseException) and not issubclass(SkillStopped, Exception))

    def test_runtime_context_satisfies_protocol(self):
        from neuxmind_runtime.skills import SkillContext

        for name in ("call", "read", "check_stop", "sleep", "log", "progress", "has", "param", "time_left"):
            self.assertTrue(callable(getattr(SkillContext, name)), name)
        for prop in ("manifest", "simulated", "stopped"):
            self.assertIsInstance(getattr(SkillContext, prop), property, prop)
        self.assertTrue(hasattr(Context, "call"))

    def test_harness_stop_and_reset(self):
        tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-h-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        skill_dir = create("patrol", "h-patrol", tmp / "h-patrol")
        with SkillHarness(skill_dir, "quadruped") as harness:
            self.assertTrue(harness.compatibility()["compatible"])
            with self.assertRaises(NeuxMindError) as cm:
                harness.run(authorize=False)
            self.assertEqual(cm.exception.code, "hardware_not_authorized")
            harness.stop()
            with self.assertRaises(NeuxMindError) as cm:
                harness.run(waypoints=[[0.5, 0]])
            self.assertEqual(cm.exception.code, "estop_latched")
            harness.reset()
            result = harness.run(waypoints=[[0.5, 0]])
            self.assertTrue(result.succeeded, result.describe())
            self.assertTrue(result.simulated)
            self.assertAlmostEqual(harness.read("base.odometry")["pose"][0], 0.5, delta=0.2)
        with SkillHarness(skill_dir, "arm") as harness, self.assertRaises(NeuxMindError) as cm:
            harness.install()
        self.assertEqual(cm.exception.code, "incompatible")


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nx-sdk-tpl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_every_template_passes_its_own_tests(self):
        env = dict(os.environ, PYTHONPATH=str(SDK_DIR))
        env.pop("NEUXMIND_SPEC_PATH", None)
        for template in TEMPLATES:
            with self.subTest(template=template):
                skill_id = f"t-{template.replace('_', '-')}"
                skill_dir = create(template, skill_id, self.tmp / skill_id)
                self.assertEqual(sorted(p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*")
                                        if p.is_file()),
                                 ["README.md", "skill.json", "skill.py", "tests/test_skill.py"])
                _package, meta = load_skill(skill_dir)
                self.assertEqual(meta["entry"], f"skill.py:{class_name(skill_id)}")
                proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(skill_dir / "tests")],
                                      cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("OK", proc.stderr)

    def test_template_cross_body_expectations(self):
        expectations = {"blank": (True, True), "status": (True, True), "patrol": (True, False),
                        "pick_place": (False, True)}
        dog, arm = sim_manifest("quadruped"), sim_manifest("arm")
        for template, (on_dog, on_arm) in expectations.items():
            skill_dir = create(template, f"x-{template.replace('_', '-')}", self.tmp / template)
            _p, meta = load_skill(skill_dir)
            self.assertEqual(explain_compatibility(meta, dog)["compatible"], on_dog, template)
            self.assertEqual(explain_compatibility(meta, arm)["compatible"], on_arm, template)

    def test_bad_inputs(self):
        with self.assertRaises(NeuxMindError):
            render("nope", "x")
        with self.assertRaises(NeuxMindError):
            render("blank", "Bad Id")
        create("blank", "dup", self.tmp / "dup")
        with self.assertRaises(NeuxMindError) as cm:
            create("blank", "dup", self.tmp / "dup")
        self.assertEqual(cm.exception.code, "exists")
        self.assertEqual(class_name("my-patrol.v2"), "MyPatrolV2")
        self.assertEqual(class_name("3d-scan"), "Skill3dScan")


if __name__ == "__main__":
    unittest.main()
