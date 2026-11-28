"""Issue #77 (cloud side): a robot model may also be MJCF, SDF or USD, and the library names its brands and models.

* MJCF (MuJoCo's own ``.xml``, the Microduck's format) is read like a URDF: bodies, joints and limits, the way
  MuJoCo reads them (default classes, degrees, includes from the same archive, the effort of the actuators).
* SDF (Gazebo), USD (Isaac Sim) and STP (STEP CAD files) are kept as they are: 「已入库」, never read, nothing to confirm.
* ``GET /library/products``: the brands and models there are materials for.

Every file here is made up by the test."""
from __future__ import annotations

import math

import parse_support as files
from test_library_model import STL, lib_gz
from test_material_extract import ExtractCase
from test_material_parse import ParseCase

# A duck-like MJCF: defaults in two top-level blocks, a childclass, ranges in degrees (MuJoCo's default unit),
# an actuator that gives a joint its effort, a motor (control range × gear), a floor and a light in the world,
# a free joint, a ball joint and an unnamed hinge.
DUCK = b"""<?xml version='1.0' encoding='utf-8'?>
<mujoco model="demo duck">
  <compiler autolimits="true" meshdir="assets"/>
  <default>
    <default class="leg"><joint range="-90 90" actuatorfrcrange="-2 2"/></default>
  </default>
  <default>
    <default class="servo"><position forcerange="-0.9 0.9"/></default>
  </default>
  <asset>
    <mesh name="trunk" file="trunk.stl"/>
    <mesh file="foot.dae"/>
    <texture name="grid" type="2d" builtin="checker" width="8" height="8"/>
  </asset>
  <worldbody>
    <light pos="0 0 3"/>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <body name="trunk_base" pos="0 0 0.3">
      <freejoint name="root"/>
      <geom type="mesh" mesh="trunk"/>
      <body name="hip" childclass="leg">
        <joint name="hip_pitch" axis="0 1 0"/>
        <body name="knee">
          <joint name="knee_pitch" axis="0 1 0" range="-45 0" actuatorfrcrange="-3 4"/>
          <body name="ankle">
            <joint name="ankle" type="ball" range="0 30"/>
            <body name="foot"><joint axis="1 0 0" class="leg"/><geom type="sphere" size="0.01"/></body>
          </body>
        </body>
      </body>
      <body name="head">
        <joint name="neck" axis="0 0 1" range="-0.5 0.5" limited="false"/>
        <body name="slider"><joint name="antenna" type="slide" range="0 0.02"/></body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="neck_servo" joint="neck" class="servo"/>
    <motor joint="antenna" ctrlrange="-1 1" gear="5"/>
  </actuator>
</mujoco>
"""
RADIAN = b"""<mujoco model="arm"><compiler angle="radian"/><worldbody><body name="link1">
<joint name="j1" range="-1.5 1.5"/><geom type="box" size="0.1 0.1 0.1"/></body></worldbody></mujoco>"""
SCENE = b"""<mujoco model="scene"><include file="robot.xml"/><worldbody><geom name="floor" type="plane" size="3 3 .1"/>
</worldbody></mujoco>"""
ROBOT = b"""<mujoco model="robot"><compiler meshdir="meshes" angle="radian"/><asset><mesh file="body.stl"/></asset>
<worldbody><body name="base"><joint name="spin" range="-3 3" actuatorfrcrange="-5 5"/><geom type="mesh" mesh="body"/></body></worldbody></mujoco>"""
SDF = b"""<?xml version="1.0"?><sdf version="1.9"><model name="demo"><link name="base"/></model></sdf>"""
USDA = b"#usda 1.0\n(\n    defaultPrim = \"robot\"\n)\ndef Xform \"robot\" {}\n"
USDC = b"PXR-USDC" + bytes(88)
STEP = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('demo'),'2;1');\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n"


def joints_of(facts):
    return {j["name"]: j for j in facts["joints"]}


class MjcfExtract(ExtractCase):
    def test_a_single_mjcf_is_read_the_way_mujoco_reads_it(self):
        facts = self.facts("model.mjcf", DUCK)
        self.assertEqual((facts["robot_name"], facts["model_format"]), ("demo duck", "mjcf"))
        self.assertEqual(facts["links"], ["trunk_base", "hip", "knee", "ankle", "foot", "head", "slider"])
        joints = joints_of(facts)
        self.assertEqual(list(joints), ["root", "hip_pitch", "knee_pitch", "unnamed_joint_1", "neck", "antenna"])
        self.assertEqual((joints["root"]["type"], joints["root"]["child"]), ("floating", "trunk_base"))
        hip = joints["hip_pitch"]                       # range and effort from its body's childclass, in degrees
        self.assertEqual((hip["type"], hip["parent"], hip["child"]), ("revolute", "trunk_base", "hip"))
        self.assertAlmostEqual(float(hip["upper"]), math.pi / 2, places=5)
        self.assertEqual(hip["effort"], "2")
        knee = joints["knee_pitch"]                     # its own range and effort
        self.assertAlmostEqual(float(knee["lower"]), -math.pi / 4, places=5)
        self.assertEqual((knee["upper"], knee["effort"]), ("0", "4"))
        self.assertEqual(joints["unnamed_joint_1"]["type"], "revolute")
        self.assertEqual(joints["neck"]["type"], "continuous")      # limited="false": no limits
        self.assertNotIn("upper", joints["neck"])
        self.assertEqual(joints["neck"]["effort"], "0.9")           # from its actuator's class
        self.assertEqual((joints["antenna"]["type"], joints["antenna"]["upper"], joints["antenna"]["effort"]), ("prismatic", "0.02", "5"))
        notes = " ".join(facts["warnings"])
        self.assertIn("球关节", notes)
        self.assertIn("地面", notes)
        self.assertIn("单个 .xml 不带它们", notes)             # meshes did not come with a single file
        self.assertEqual(facts["meshes"]["named"], 2)

    def test_radians_when_the_compiler_says_so(self):
        facts = self.facts("model.mjcf", RADIAN)
        self.assertEqual(joints_of(facts)["j1"]["upper"], "1.5")

    def test_an_archive_with_a_scene_that_includes_the_robot_is_one_model(self):
        members = {"duck/scene.xml": SCENE, "duck/robot.xml": ROBOT, "duck/meshes/body.stl": STL, "duck/package.xml": b"<package/>"}
        for mode, data in (("model.zip", files.zip_bytes(members)), ("model.tar.gz", lib_gz(files.tar_bytes(members)))):
            with self.subTest(mode=mode):
                facts = self.facts(mode, data)
                self.assertEqual((facts["model_file"], facts["robot_name"], facts["model_format"]), ("duck/scene.xml", "scene", "mjcf"))
                self.assertEqual([j["name"] for j in facts["joints"]], ["spin"])
                self.assertEqual(facts["meshes"], {"named": 1, "missing": [], "unsupported": []})

    def test_an_include_that_is_not_in_the_archive_or_leads_out_of_it_is_named_not_read(self):
        scene = SCENE.replace(b"robot.xml", b"../robot.xml")
        facts = self.facts("model.zip", files.zip_bytes({"scene.xml": scene}))
        self.assertEqual(facts["joints"], [])
        self.assertTrue(any("../robot.xml" in w for w in facts["warnings"]), facts["warnings"])

    def test_a_urdf_and_an_mjcf_in_one_archive_are_two_models(self):
        refused = self.refused("model.zip", files.zip_bytes({"a.urdf": files.URDF_OK, "b.xml": RADIAN}))
        self.assertEqual(refused.code, "model_many_urdf")
        self.assertIn("b.xml", refused.message)

    def test_an_mjcf_is_held_to_the_same_xml_rules(self):
        self.assertEqual(self.refused("model.mjcf", files.XXE).code, "xml_doctype")
        self.assertEqual(self.refused("model.mjcf", b"<robot name='x'/>").code, "mjcf_root")
        hostile = files.XXE.replace(b"<robot", b"<mujoco").replace(b"</robot>", b"</mujoco>")
        self.assertEqual(self.refused("model.zip", files.zip_bytes({"x.xml": hostile})).code, "xml_doctype")

    # Review of issue #77: what MuJoCo does that the first version did not.
    def test_a_nested_include_is_found_from_the_main_files_folder(self):
        main = b'<mujoco model="main"><include file="sub/b.xml"/></mujoco>'
        sub = b'<mujoco><include file="c.xml"/></mujoco>'
        leaf = b'<mujoco><compiler angle="radian"/><worldbody><body name="base"><joint name="spin" range="-1 1"/></body></worldbody></mujoco>'
        facts = self.facts("model.zip", files.zip_bytes({"main.xml": main, "sub/b.xml": sub, "c.xml": leaf}))
        self.assertEqual((facts["model_file"], [j["name"] for j in facts["joints"]]), ("main.xml", ["spin"]))

    def test_limits_and_efforts_are_those_mujoco_applies(self):
        odd = b"""<mujoco><compiler angle="radian"/><worldbody><body name="a">
          <joint name="flat" range="0 0"/><joint name="backwards" range="1 0"/><joint name="zero_force" range="-1 1" actuatorfrcrange="0 0"/>
          <joint name="unnamed_joint_1" range="-1 1"/><joint range="-1 1"/></body></worldbody>
          <actuator><motor joint="zero_force" ctrlrange="-1 1"/></actuator></mujoco>"""
        joints = joints_of(self.facts("model.mjcf", odd))
        self.assertEqual([(n, joints[n]["type"]) for n in ("flat", "backwards")], [("flat", "continuous"), ("backwards", "continuous")])
        self.assertEqual(joints["zero_force"]["effort"], "1")                  # from the motor, as the simulation does
        self.assertEqual(list(joints)[-2:], ["unnamed_joint_1", "unnamed_joint_2"])   # a made-up name never takes one in use

    def test_a_range_without_limited_under_autolimits_false_is_named(self):
        facts = self.facts("model.mjcf", b'<mujoco><compiler autolimits="false"/><worldbody><body name="a"><joint name="j" range="-1 1"/>'
                                         b'</body></worldbody></mujoco>')
        self.assertEqual(joints_of(facts)["j"]["type"], "continuous")
        self.assertTrue(any("limited" in w and "建不起来" in w for w in facts["warnings"]), facts["warnings"])

    def test_one_file_included_many_times_counts_every_time(self):
        many = b"<mujoco>" + b'<custom><numeric name="n" data="1"/></custom>' * 1000 + b"</mujoco>"
        main = b'<mujoco model="main">' + b'<include file="many.xml"/>' * 100 + b"</mujoco>"
        self.assertEqual(self.refused("model.zip", files.zip_bytes({"main.xml": main, "many.xml": many})).code, "xml_too_large")

    def test_sdf_and_usd_are_kept_not_read(self):
        self.assertEqual(self.facts("model.sdf", SDF)["stored_only"], "sdf")
        self.assertEqual(self.facts("model.usd", USDA)["stored_only"], "usd")
        self.assertEqual(self.facts("model.stp", STEP)["stored_only"], "stp")
        cases = (({"m/model.sdf": SDF, "m/model.config": b"<model/>"}, "sdf"), ({"r.usd": USDC, "r_physics.usda": USDA}, "usd"),
                 ({"a.sdf": SDF, "b.usdz": files.zip_bytes({"x.usdc": USDC})}, "sdf+usd"), ({"cad/robot.step": STEP, "cad/leg.stp": STEP}, "stp"))
        for members, expected in cases:
            with self.subTest(expected=expected):
                facts = self.facts("model.zip", files.zip_bytes(members))
                self.assertEqual(facts["stored_only"], expected)
                self.assertEqual(facts["file_count"], len([n for n in members if not n.endswith(".config")]))

    def test_an_archive_without_any_model_file_is_refused(self):
        refused = self.refused("model.zip", files.zip_bytes({"meshes/a.stl": STL, "notes.xml": b"<notes/>"}))
        self.assertEqual(refused.code, "model_no_urdf")
        self.assertIn("MJCF", refused.message)


class ModelFormatsKind(ParseCase):
    weknora = False

    def model(self, filename: str, data: bytes, **fields) -> dict:
        return self.upload(self.owner, filename, data, kind="model", **fields)

    def test_an_mjcf_is_a_model_read_like_a_urdf_and_confirmed(self):
        material = self.model("duck.xml", DUCK)
        self.assertEqual((material["kind"], material["format"]), ("model", "mjcf"))
        self.parse_all(rounds=2)
        result = self.result(material["id"])
        self.assertEqual((result["kind"], result["status"]), ("urdf", "parsed"))
        self.assertIn("hip_pitch", [j["name"] for j in result["values"]["joints"]])
        self.assertEqual(self.ok(self.confirm(material["id"], {"revision": result["revision"]}))["status"], "usable")

    def test_an_mjcf_archive_is_a_model_too(self):
        data = files.zip_bytes({"scene.xml": SCENE, "robot.xml": ROBOT, "meshes/body.stl": STL})
        material = self.model("duck.zip", data)
        self.parse_all(rounds=2)
        result = self.result(material["id"])
        self.assertEqual((result["status"], result["values"]["robot_name"]), ("parsed", "scene"))

    def test_sdf_and_usd_are_kept_with_words_that_say_the_simulation_cannot_use_them(self):
        cases = (("demo.sdf", SDF, "sdf", "SDF"), ("demo.usda", USDA, "usd", "USD"), ("demo.usdc", USDC, "usd", "USD"),
                 ("demo.usdz", files.zip_bytes({"demo.usdc": USDC}), "usdz", "USD"),
                 ("gazebo.zip", files.zip_bytes({"m/model.sdf": SDF}), "zip", "SDF"), ("demo.stp", STEP, "stp", "STP"),
                 ("demo.step", STEP, "stp", "STP"), ("cad.zip", files.zip_bytes({"cad/robot.step": STEP}), "zip", "STP"))
        for filename, data, fmt, label in cases:
            with self.subTest(filename=filename):
                material = self.model(filename, data, version=filename)
                self.assertEqual((material["kind"], material["format"]), ("model", fmt))
                self.parse_all(rounds=2)
                result = self.result(material["id"])
                self.assertEqual((result["status"], result["editable"], result["kept_only"] != ""), ("stored", False, True))
                self.assertIn(label, result["status_detail"])
                self.assertIn("仿真", result["status_detail"])
                refused = self.error(self.confirm(material["id"], {"revision": result["revision"]}), 409, "model_kept_only")
                self.assertIn("不需要", refused["error"])

    def test_the_content_must_be_what_the_name_says(self):
        cases = (("robot.xml", files.URDF_OK), ("demo.sdf", DUCK), ("demo.usd", b"not usd at all"), ("demo.usdz", USDA),
                 ("demo.stp", USDA))
        for filename, data in cases:
            with self.subTest(filename=filename):
                refused = self.first_refusal(self.owner, filename, data, kind="model")
                self.assertEqual((refused.status_code, refused.json()["code"]), (415, "unsupported_type"), refused.text)

    def test_model_only_formats_are_not_documents(self):
        self.error(self.start_raw(self.owner, "demo.sdf", SDF), 415, "unsupported_type")
        self.error(self.start_raw(self.owner, "demo.usd", USDA), 415, "unsupported_type")
        self.error(self.start_raw(self.owner, "demo.stp", STEP), 415, "unsupported_type")
        document = self.upload(self.owner, "duck.xml", DUCK)
        self.assertEqual((document["kind"], document["format"]), ("document", "xml"))
        refused = self.error(self.start_raw(self.owner, "demo.xacro", files.URDF_OK, kind="model"), 415, "unsupported_type")
        for named in (".urdf", ".xml", ".sdf", ".usd", ".stp"):
            self.assertIn(named, refused["error"])
        formats = self.ok(self.get("/library/formats", self.owner))
        self.assertIn(".sdf", formats["model"]["extensions"])
        self.assertNotIn(".sdf", formats["extensions"])

    def test_the_products_are_the_brands_and_models_there_are_materials_for(self):
        self.upload(self.owner, "manual.txt", b"manual", vendor="Acme", model="D1")
        model = self.model("duck.xml", DUCK, vendor="acme", model="d1")
        self.upload(self.owner, "other.txt", b"other", vendor="Beta", model="B2")
        self.parse_all(rounds=2)
        self.ok(self.confirm(model["id"], {"revision": self.result(model["id"])["revision"]}))
        listed = self.ok(self.get("/library/products", self.owner))["products"]
        by_key = {(p["vendor"].lower(), p["model"].lower()): p for p in listed}
        self.assertEqual(set(by_key), {("acme", "d1"), ("beta", "b2")})
        self.assertEqual((by_key[("acme", "d1")]["materials"], by_key[("acme", "d1")]["usable_models"]), (2, 1))
        self.assertEqual(by_key[("beta", "b2")]["usable_models"], 0)
        self.error(self.get("/library/products"), 401)
