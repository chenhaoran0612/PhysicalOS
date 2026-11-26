"""Issue #51 (cloud side): a material belongs to a brand and model of robot, not to one device.

Until now (issue #34) a material belonged to the one device it was uploaded for. The user's decision of
2026-11-26: the library is bound to the brand and model, robots of the same brand and model share it,
and the materials kept for single devices move to their device's brand and model. Only a bundle (a
robot's programs and scene data, issue #43) still belongs to one device."""
from __future__ import annotations

import library_support as lib
import parse_support as files
from support import API
from test_material_parse import ParseCase

from neuxmind_cloud import library
from neuxmind_cloud.library_models import Material

DOG_A = {"device_id": "robot-aaaaaaaaaaaa", "device_name": "一号狗"}
DOG_B = {"device_id": "robot-bbbbbbbbbbbb", "device_name": "二号狗"}
PRODUCT = {"vendor": "Acme Robotics", "model": "Go-7"}


class OwnedByBrandAndModel(ParseCase):
    weknora = False

    def test_the_device_an_upload_names_no_longer_separates_it(self):
        first = self.upload(self.owner, "arm.urdf", files.URDF_OK, **DOG_A)
        self.assertEqual((first["device_id"], first["device_name"]), ("", ""))
        self.error(self.start_raw(self.owner, "arm.urdf", files.URDF_OK, **DOG_B), 409, "material_exists")
        self.assertEqual(self.ok(self.get("/library/materials?device_id=robot-aaaaaaaaaaaa", self.owner))["materials"], [])

    def test_robots_of_one_brand_and_model_find_the_same_materials_whatever_the_spelling(self):
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK, **PRODUCT)
        other = self.upload(self.owner, "arm.urdf", files.URDF_OK, vendor="Acme Robotics", model="Go-8")
        listed = self.ok(self.get("/library/materials?vendor=acme%20robotics&model=GO-7", self.owner))["materials"]
        self.assertEqual([m["id"] for m in listed], [material["id"]])
        self.assertNotEqual(material["id"], other["id"])

    def test_a_bundle_still_belongs_to_one_device(self):
        data = lib.tar_gz_bytes(b"walk program and its places")
        first = self.upload(self.owner, "walk.tar.gz", data, kind="bundle", **DOG_A)
        second = self.upload(self.owner, "walk.tar.gz", data, kind="bundle", **DOG_B)
        self.assertEqual((first["device_id"], second["device_id"]), (DOG_A["device_id"], DOG_B["device_id"]))


class MoveToBrandAndModel(ParseCase):
    """``POST /library/materials/rebind``: what was kept for one device goes to that device's brand and model."""
    weknora = False

    def legacy(self, filename: str, data: bytes, device: dict, **labels) -> dict:
        """A material as issue #34 stored it: bound to one device, with whatever brand and model was typed."""
        material = self.upload(self.owner, filename, data, vendor=f"typed for {device['device_id']}", model="typed", **labels)
        with self.db.transaction() as s:
            row = s.get(Material, material["id"])
            row.device_id, row.device_name = device["device_id"], device["device_name"]
            row.label_key = library._digest(row.body_type, row.vendor.casefold(), row.model.casefold(), row.version.casefold(),
                                            "device", row.device_id)
        return material

    def rebind(self, device: dict, user=None, **product) -> dict:
        body = {"device_id": device["device_id"], **(product or PRODUCT)}
        return self.ok(self.post("/library/materials/rebind", body, user or self.owner))

    def materials(self, query: str = "") -> list[dict]:
        return self.ok(self.get("/library/materials" + query, self.owner))["materials"]

    def test_a_devices_materials_move_to_its_brand_and_model(self):
        manual = self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        other = self.legacy("leg.urdf", files.URDF_GAPS, DOG_B)
        result = self.rebind(DOG_A)
        self.assertEqual((result["moved"], result["merged"], result["kept"]), (1, 0, []))
        self.assertIs(result["more"], False)
        moved = self.ok(self.get(f"/library/materials/{manual['id']}", self.owner))
        self.assertEqual((moved["vendor"], moved["model"], moved["device_id"], moved["device_name"]),
                         ("Acme Robotics", "Go-7", "", ""))
        self.assertEqual([m["id"] for m in self.materials("?vendor=acme%20robotics&model=go-7")], [manual["id"]])
        untouched = self.ok(self.get(f"/library/materials/{other['id']}", self.owner))
        self.assertEqual(untouched["device_id"], DOG_B["device_id"])
        self.assertEqual(self.rebind(DOG_A)["moved"], 0)                      # again: nothing left to move
        # from now on the file is a duplicate under that brand and model
        self.error(self.start_raw(self.owner, "arm.urdf", files.URDF_OK, **PRODUCT), 409, "material_exists")

    def test_the_same_file_kept_for_two_devices_becomes_one_and_the_confirmed_copy_stays(self):
        first = self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        second = self.legacy("arm.urdf", files.URDF_OK, DOG_B)
        self.parse_all(rounds=2)
        result = self.result(second["id"])
        self.ok(self.confirm(second["id"], {"revision": result["revision"]}))
        self.assertEqual(self.rebind(DOG_A)["moved"], 1)
        merged = self.rebind(DOG_B)
        self.assertEqual((merged["moved"], merged["merged"]), (1, 1))
        left = self.materials()
        self.assertEqual([(m["id"], m["status"], m["vendor"], m["device_id"]) for m in left],
                         [(second["id"], "usable", "Acme Robotics", "")])
        self.assertEqual(self.content(self.owner, second["id"]).content, files.URDF_OK)   # the file is still there
        self.assertEqual(self.get(f"/library/materials/{first['id']}", self.owner).status_code, 404)

    def test_an_unconfirmed_duplicate_gives_way_to_the_one_already_there(self):
        first = self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        second = self.legacy("arm.urdf", files.URDF_OK, DOG_B)
        self.rebind(DOG_A)
        merged = self.rebind(DOG_B)
        self.assertEqual((merged["moved"], merged["merged"]), (0, 1))
        self.assertEqual([m["id"] for m in self.materials()], [first["id"]])
        self.assertEqual(self.get(f"/library/materials/{second['id']}", self.owner).status_code, 404)

    def test_a_file_of_the_same_name_with_other_content_stays_with_its_device_and_is_named(self):
        self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        clash = self.legacy("arm.urdf", files.URDF_GAPS, DOG_B)
        self.rebind(DOG_A)
        result = self.rebind(DOG_B)
        self.assertEqual((result["moved"], result["merged"]), (0, 0))
        self.assertEqual([(k["id"], k["filename"]) for k in result["kept"]], [(clash["id"], "arm.urdf")])
        self.assertEqual(self.ok(self.get(f"/library/materials/{clash['id']}", self.owner))["device_id"], DOG_B["device_id"])

    def test_the_same_content_under_another_name_does_not_free_a_name_that_is_taken(self):
        """A device's copy of file A is also there as B; the name A is there with other content: nothing is merged over it."""
        self.upload(self.owner, "b.urdf", files.URDF_OK, **PRODUCT)
        self.upload(self.owner, "arm.urdf", files.URDF_GAPS, **PRODUCT)
        clash = self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        result = self.rebind(DOG_A)
        self.assertEqual((result["moved"], result["merged"], [k["id"] for k in result["kept"]]), (0, 0, [clash["id"]]))
        self.assertEqual(len(self.materials()), 3)

    def test_a_confirmed_material_of_the_device_stays_confirmed(self):
        material = self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        self.parse_all(rounds=2)
        self.ok(self.confirm(material["id"], {"revision": self.result(material["id"])["revision"]}))
        self.rebind(DOG_A)
        self.assertEqual(self.ok(self.get(f"/library/materials/{material['id']}", self.owner))["status"], "usable")

    def test_a_bundle_is_not_moved(self):
        bundle = self.upload(self.owner, "walk.tar.gz", lib.tar_gz_bytes(b"walk"), kind="bundle", **DOG_A)
        self.assertEqual(self.rebind(DOG_A)["moved"], 0)
        kept = self.ok(self.get(f"/library/materials/{bundle['id']}", self.owner))
        self.assertEqual((kept["device_id"], kept["vendor"]), (DOG_A["device_id"], lib.LABELS["vendor"]))

    def test_who_may_move_and_what_is_refused(self):
        self.legacy("arm.urdf", files.URDF_OK, DOG_A)
        operator = self.member(self.owner, "operator@parse.test", "operator")
        self.error(self.post("/library/materials/rebind", {**DOG_A, **PRODUCT}, operator), 403)
        stranger = self.register("stranger@parse.test")
        self.assertEqual(self.rebind(DOG_A, user=stranger)["moved"], 0)       # its own organisation has nothing
        for body in ({**PRODUCT}, {"device_id": "../etc", **PRODUCT}, {**DOG_A, "vendor": "Acme"},
                     {**DOG_A, "vendor": "", "model": ""}, {**DOG_A, "vendor": "x" * 101, "model": "y"}, []):
            with self.subTest(body=body):
                self.error(self.post("/library/materials/rebind", body, self.owner), 400)
        self.assertEqual(self.rebind(DOG_A)["moved"], 1)


class MoveOne(ParseCase):
    """``PUT /library/materials/{id}/product``: a single material goes to another brand and model."""
    weknora = False

    def move(self, material_id, user=None, **product):
        return self.client.put(f"{API}/library/materials/{material_id}/product", json=product or PRODUCT,
                               headers=(user or self.owner)["headers"])

    def test_a_material_goes_to_the_brand_and_model_a_person_names(self):
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK, vendor="typed wrongly", model="x")
        moved = self.ok(self.move(material["id"]))
        self.assertEqual((moved["id"], moved["vendor"], moved["model"]), (material["id"], "Acme Robotics", "Go-7"))
        listed = self.ok(self.get("/library/materials?vendor=acme%20robotics&model=go-7", self.owner))["materials"]
        self.assertEqual([m["id"] for m in listed], [material["id"]])
        self.assertEqual(self.ok(self.move(material["id"]))["vendor"], "Acme Robotics")       # again: nothing changes

    def test_a_confirmed_material_is_looked_at_again_for_its_new_brand_and_model(self):
        """What a review found (2026-11-26): 「可用」 was confirmed for one brand and model and must not carry over to another."""
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK, vendor="first", model="x")
        self.parse_all(rounds=2)
        self.ok(self.confirm(material["id"], {"revision": self.result(material["id"])["revision"]}))
        same = self.ok(self.move(material["id"], vendor="FIRST", model="X"))               # the same brand and model, spelled differently
        self.assertEqual(same["status"], "usable")
        moved = self.ok(self.move(material["id"]))
        self.assertEqual((moved["status"], moved["vendor"]), ("parsed", "Acme Robotics"))
        result = self.result(material["id"])
        self.assertEqual((result["status"], result["confirmed_by"], result["confirmed_at"]), ("parsed", "", None))
        self.assertIn("重新核对", result["status_detail"])
        self.assertEqual(self.ok(self.confirm(material["id"], {"revision": result["revision"]}))["status"], "usable")

    def test_what_is_already_there_is_not_replaced(self):
        there = self.upload(self.owner, "arm.urdf", files.URDF_OK, **PRODUCT)
        same = self.upload(self.owner, "arm.urdf", files.URDF_OK, vendor="other", model="x")
        named = self.upload(self.owner, "arm.urdf", files.URDF_GAPS, vendor="third", model="x")
        self.assertEqual(self.error(self.move(same["id"]), 409, "material_exists")["material_id"], there["id"])
        self.assertEqual(self.error(self.move(named["id"]), 409, "version_exists")["material_id"], there["id"])
        self.assertEqual(self.ok(self.get(f"/library/materials/{same['id']}", self.owner))["vendor"], "other")

    def test_a_bundle_stays_with_its_device_and_only_a_writer_of_the_organisation_may_move(self):
        bundle = self.upload(self.owner, "walk.tar.gz", lib.tar_gz_bytes(b"walk"), kind="bundle", **DOG_A)
        self.error(self.move(bundle["id"]), 409, "bundle_needs_device")
        material = self.upload(self.owner, "arm.urdf", files.URDF_OK)
        self.error(self.move(material["id"], user=self.member(self.owner, "operator@parse.test", "operator")), 403)
        self.error(self.move(material["id"], user=self.register("stranger@parse.test")), 404)
        self.error(self.move("mat_" + "0" * 20), 404)
        for product in ({"vendor": "Acme"}, {"vendor": "", "model": ""}):
            self.error(self.client.put(f"{API}/library/materials/{material['id']}/product", json=product,
                                       headers=self.owner["headers"]), 400)
