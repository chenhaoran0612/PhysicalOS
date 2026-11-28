"""Issue #51 (cloud side): the robot model of a brand and model is its own kind of material, ``model``.

It is one plain ``.urdf``, or a ``.zip`` / ``.tar.gz`` that holds one ``.urdf`` and the mesh files it
names. It is read by the same rules as a URDF document (links, joints, limits); for an archive the
rules also say which named meshes are missing and which a simulation cannot load. Every file here is
made up by the test."""
from __future__ import annotations

import library_support as lib
import parse_support as files
from test_material_extract import ExtractCase
from test_material_parse import ParseCase

MESHED = b"""<?xml version="1.0"?>
<robot name="demo_dog">
  <link name="trunk">
    <visual><geometry><mesh filename="package://demo_description/meshes/trunk.stl"/></geometry></visual>
    <collision><geometry><box size="0.4 0.2 0.1"/></geometry></collision>
  </link>
  <link name="thigh">
    <visual><geometry><mesh filename="package://demo_description/meshes/thigh.dae"/></geometry></visual>
    <collision><geometry><mesh filename="meshes/thigh_collision.obj"/></geometry></collision>
  </link>
  <joint name="hip" type="revolute">
    <parent link="trunk"/><child link="thigh"/>
    <limit lower="-1" upper="1" velocity="5" effort="30"/>
  </joint>
</robot>
"""
STL = b"solid t\nendsolid t\n"
MEMBERS = {"demo_description/urdf/demo.urdf": MESHED, "demo_description/meshes/trunk.stl": STL,
           "demo_description/meshes/thigh.dae": b"<COLLADA/>", "demo_description/meshes/thigh_collision.obj": b"v 0 0 0\n"}


class ModelExtract(ExtractCase):
    def test_an_archive_gives_the_joints_of_its_one_urdf_and_what_it_knows_of_the_meshes(self):
        for mode, data in (("model.zip", files.zip_bytes(MEMBERS)), ("model.tar.gz", lib_gz(files.tar_bytes(MEMBERS)))):
            with self.subTest(mode=mode):
                facts = self.facts(mode, data)
                self.assertEqual((facts["robot_name"], facts["links"]), ("demo_dog", ["trunk", "thigh"]))
                self.assertEqual([(j["name"], j["type"], j["upper"]) for j in facts["joints"]], [("hip", "revolute", "1")])
                self.assertEqual(facts["urdf"], "demo_description/urdf/demo.urdf")
                self.assertEqual(facts["meshes"], {"named": 3, "missing": [], "unsupported": ["thigh.dae"]})
                self.assertTrue(any("thigh.dae" in w and "仿真" in w for w in facts["warnings"]), facts["warnings"])

    def test_a_named_mesh_that_is_not_in_the_archive_is_reported(self):
        members = {k: v for k, v in MEMBERS.items() if not k.endswith("trunk.stl")}
        facts = self.facts("model.zip", files.zip_bytes(members))
        self.assertEqual(facts["meshes"]["missing"], ["trunk.stl"])
        self.assertTrue(any("trunk.stl" in w and "没有" in w for w in facts["warnings"]), facts["warnings"])

    def test_an_archive_without_a_urdf_or_with_several_is_refused(self):
        none = self.refused("model.zip", files.zip_bytes({"meshes/a.stl": STL}))
        self.assertEqual(none.code, "model_no_urdf")
        several = self.refused("model.zip", files.zip_bytes({"a/one.urdf": MESHED, "b/two.urdf": MESHED}))
        self.assertEqual(several.code, "model_many_urdf")
        self.assertIn("one.urdf", several.message)

    def test_a_urdf_whose_name_leads_out_of_the_archive_is_not_read(self):
        refused = self.refused("model.zip", files.zip_bytes({"../demo.urdf": MESHED}))
        self.assertEqual(refused.code, "model_no_urdf")

    def test_the_urdf_inside_is_held_to_the_same_rules_as_a_single_one(self):
        self.assertEqual(self.refused("model.zip", files.zip_bytes({"x.urdf": files.XXE})).code, "xml_doctype")
        self.assertEqual(self.refused("model.zip", files.zip_bytes({"x.urdf": b"<html/>"})).code, "urdf_root")

    def test_a_single_urdf_says_that_its_meshes_did_not_come_with_it(self):
        facts = self.facts("model.urdf", MESHED)
        self.assertEqual(facts["meshes"], {"named": 3, "missing": ["trunk.stl", "thigh.dae", "thigh_collision.obj"],
                                           "unsupported": []})
        self.assertTrue(any("压缩包" in w for w in facts["warnings"]), facts["warnings"])
        plain = self.facts("model.urdf", files.URDF_OK)
        self.assertEqual((plain["meshes"]["named"], plain["warnings"]), (0, []))


def lib_gz(data: bytes) -> bytes:
    import gzip
    return gzip.compress(data)


class ModelKind(ParseCase):
    weknora = False

    def model(self, filename: str = "demo.urdf", data: bytes = files.URDF_OK, **fields) -> dict:
        return self.upload(self.owner, filename, data, kind="model", **fields)

    def test_a_single_urdf_is_stored_as_a_model_and_read_like_a_urdf(self):
        material = self.model()
        self.assertEqual((material["kind"], material["format"], material["status"]), ("model", "urdf", "parsing"))
        self.parse_all(rounds=2)
        result = self.result(material["id"])
        self.assertEqual((result["kind"], result["status"]), ("urdf", "parsed"))
        self.assertEqual([j["name"] for j in result["values"]["joints"]], ["shoulder", "elbow", "rail", "spin", "mount"])
        confirmed = self.ok(self.confirm(material["id"], {"revision": result["revision"]}))
        self.assertEqual(confirmed["status"], "usable")

    def test_an_archive_with_meshes_is_a_model_too(self):
        for filename, data in (("demo.zip", files.zip_bytes(MEMBERS)), ("demo.tar.gz", lib_gz(files.tar_bytes(MEMBERS)))):
            with self.subTest(filename=filename):
                material = self.model(filename, data)
                self.assertEqual((material["kind"], material["status"]), ("model", "parsing"))
                self.parse_all(rounds=2)
                result = self.result(material["id"])
                self.assertEqual((result["kind"], result["status"]), ("urdf", "parsed"))
                self.assertEqual(result["values"]["robot_name"], "demo_dog")
                self.assertTrue(any("thigh.dae" in w for w in result["warnings"]), result["warnings"])

    def test_an_archive_without_a_urdf_is_not_parsed_and_says_why(self):
        material = self.model("meshes.zip", files.zip_bytes({"meshes/a.stl": STL}))
        self.parse_all(rounds=2)
        result = self.result(material["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn(".urdf", result["status_detail"])

    def test_only_a_plain_urdf_a_zip_or_a_tar_gz_can_be_a_model(self):
        for filename, data in (("demo.xacro", files.URDF_OK), ("demo.tar", lib.tar_bytes()), ("demo.tar.xz", lib.tar_xz_bytes()),
                               ("demo.pdf", lib.pdf_bytes()), ("demo.deb", lib.deb_bytes())):
            with self.subTest(filename=filename):
                refused = self.error(self.start_raw(self.owner, filename, data, kind="model"), 415, "unsupported_type")
                self.assertIn(".urdf", refused["error"])

    def test_without_the_field_nothing_changes(self):
        document = self.upload(self.owner, "demo.urdf", files.URDF_OK)
        self.assertEqual(document["kind"], "document")
        package = self.upload(self.owner, "demo.zip", files.zip_bytes(MEMBERS))
        self.assertEqual(package["kind"], "package")
        self.parse_all(rounds=2)
        self.assertEqual(self.result(package["id"])["kind"], "package")

    def test_the_same_file_as_a_document_and_as_a_model_are_two_materials(self):
        document = self.upload(self.owner, "demo.urdf", files.URDF_OK)
        model = self.model()
        self.assertNotEqual(document["id"], model["id"])
        self.error(self.start_raw(self.owner, "demo.urdf", files.URDF_OK, kind="model"), 409, "material_exists")
        listed = self.ok(self.get("/library/materials?kind=model", self.owner))["materials"]
        self.assertEqual([m["id"] for m in listed], [model["id"]])

    def test_an_unfinished_upload_of_another_kind_is_not_continued_as_a_model(self):
        data = files.zip_bytes({**MEMBERS, "note.txt": b"unfinished"})
        opened = self.start(self.owner, "half.zip", data, kind="model")
        again = self.start(self.owner, "half.zip", data, expect=200, kind="model")
        self.assertEqual((again["id"], again["kind"]), (opened["id"], "model"))

    def test_the_formats_say_what_a_model_may_be(self):
        formats = self.ok(self.get("/library/formats", self.owner))
        # Issue #77: MJCF (.xml), SDF, USD and STP as well.
        self.assertEqual(formats["model"], {"extensions": [".urdf", ".xml", ".sdf", ".usd", ".usda", ".usdc", ".usdz",
                                                           ".stp", ".step", ".zip", ".tar.gz", ".tgz"],
                                            "max_bytes": self.settings.library_max_model_bytes})



class ModelLimits(ParseCase):
    weknora = False
    library_overrides = {"library_max_model_bytes": 100}

    def test_a_model_larger_than_its_limit_is_refused(self):
        refused = self.error(self.start_raw(self.owner, "demo.zip", files.zip_bytes(MEMBERS), kind="model"), 413,
                             "file_too_large")
        self.assertEqual((refused["max_bytes"], refused["kind"]), (100, "model"))
