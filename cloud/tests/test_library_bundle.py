"""Issue #43, part 2 (cloud side): a robot's programs and scene data are kept in the material library
as one ``.tar.gz`` of kind ``bundle``. It is stored and handed back, never parsed, and belongs to one device."""
import library_support as lib
from library_support import LibraryCase

from sqlalchemy import func, select

from neuxmind_cloud import material_parse
from neuxmind_cloud.material_parse_models import MaterialParse

DEVICE = {"device_id": "robot-7", "device_name": "二楼的狗"}


class Bundles(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("owner@example.com")
        self.data = lib.tar_gz_bytes(b"walk program and its places")

    def bundle(self, filename: str = "walk_programs.tar.gz", data: bytes | None = None, **fields) -> dict:
        return self.upload(self.user, filename, self.data if data is None else data, kind="bundle", **{**DEVICE, **fields})

    def test_a_bundle_is_stored_usable_at_once_and_downloads_unchanged(self):
        material = self.bundle()
        self.assertEqual((material["kind"], material["format"], material["status"]), ("bundle", "tar.gz", "usable"))
        self.assertEqual((material["device_id"], material["device_name"], material["sha256"]),
                         ("robot-7", "二楼的狗", lib.sha(self.data)))
        response = self.content(self.user, material["id"])
        self.assertEqual((response.status_code, response.content), (200, self.data))
        self.assertEqual(self.ok(self.get(f"/library/materials/{material['id']}", self.user))["status"], "usable")
        self.assertEqual(self.bundle("scene.tgz", lib.tar_gz_bytes(b"other"))["kind"], "bundle")

    def test_a_bundle_is_never_parsed(self):
        material = self.bundle()
        material_parse.run_pending(self.db, self.settings)
        with self.db.transaction() as s:
            self.assertEqual(s.execute(select(func.count()).select_from(MaterialParse)).scalar_one(), 0)
        self.assertEqual(self.ok(self.get(f"/library/materials/{material['id']}", self.user))["status"], "usable")
        path = f"/library/materials/{material['id']}/parse"
        self.error(self.get(path, self.user), 409, "not_parsed_kind")
        self.error(self.client.put(support_url(path), json={"revision": 0, "values": {}}, headers=self.user["headers"]),
                   409, "not_parsed_kind")
        self.error(self.post(path + "/confirm", {"revision": 0}, self.user), 409, "not_parsed_kind")
        self.error(self.post(path + "/retry", {}, self.user), 409, "not_parsed_kind")
        self.assertEqual(self.ok(self.get(f"/library/materials/{material['id']}", self.user))["status"], "usable")

    def test_a_bundle_belongs_to_one_device(self):
        for fields in ({}, {"device_id": "robot-7"}, {"device_id": "", "device_name": "狗"}):
            with self.subTest(fields=fields):
                self.error(self.start_raw(self.user, "walk.tar.gz", self.data, kind="bundle", **fields), 400,
                           "bundle_needs_device")

    def test_only_tar_gz_is_a_bundle(self):
        for filename, data in (("walk.zip", lib.zip_bytes()), ("walk.tar", lib.tar_bytes()),
                               ("walk.tar.xz", lib.tar_xz_bytes()), ("notes.pdf", lib.pdf_bytes())):
            with self.subTest(filename=filename):
                self.error(self.start_raw(self.user, filename, data, kind="bundle", **DEVICE), 415, "unsupported_type")
        # and the content must be what the name says
        refused = self.first_refusal(self.user, "walk.tar.gz", lib.ELF, kind="bundle", **DEVICE)
        self.error(refused, 415, "unsupported_type")
        self.assertEqual(self.ok(self.get("/library/materials", self.user))["materials"], [])

    def test_a_kind_that_does_not_fit_the_file_is_refused(self):
        self.error(self.start_raw(self.user, "walk.tar.gz", self.data, kind="document"), 400, "invalid_request")
        self.error(self.start_raw(self.user, "walk.tar.gz", self.data, kind=7), 400, "invalid_request")
        package = self.upload(self.user, "driver.tar.gz", self.data, kind="package")
        self.assertEqual((package["kind"], package["status"]), ("package", "parsing"))

    def test_the_same_file_as_a_package_and_as_a_bundle_are_two_uploads(self):
        """The same file, once as a driver package and once as a bundle: the second must not silently
        become a continuation of the first (and so be stored as the wrong kind). Since issue #51 a
        package belongs to its brand and model and a bundle to one device, so the two never meet:
        each is its own upload and is stored as what its request said."""
        for index, (first, second) in enumerate((({}, {"kind": "bundle"}), ({"kind": "bundle"}, {}), ({"kind": "bundle"}, {"kind": "package"}))):
            with self.subTest(first=first, second=second):
                name = f"walk-{index}.tar.gz"
                data = lib.tar_gz_bytes(f"walk {first} {second}".encode())
                opened = self.start(self.user, name, data, **DEVICE, **first)
                other = self.start(self.user, name, data, **DEVICE, **second)
                self.assertNotEqual(other["id"], opened["id"])
                self.assertEqual((other["resumed"], other["kind"]), (False, "bundle" if second.get("kind") == "bundle" else "package"))
                again = self.start(self.user, name, data, expect=200, **DEVICE, **first)   # the same kind continues
                self.assertEqual((again["id"], again["resumed"], again["kind"]), (opened["id"], True, opened["kind"]))
                for upload, fields in ((opened, first), (other, second)):
                    for part_index, part in enumerate(self.parts(upload, data)):
                        self.ok(self.put_chunk(self.user, upload["id"], part_index, part))
                    stored = self.ok(self.complete(self.user, upload["id"]), 201)
                    self.assertEqual(stored["kind"], "bundle" if fields.get("kind") == "bundle" else "package")

    def test_without_the_field_a_tar_gz_is_a_driver_package_as_before(self):
        material = self.upload(self.user, "driver.tar.gz", self.data, **DEVICE)
        self.assertEqual((material["kind"], material["status"]), ("package", "parsing"))
        self.ok(self.get(f"/library/materials/{material['id']}/parse", self.user))

    def test_the_list_filters_by_kind(self):
        bundle = self.bundle()
        package = self.upload(self.user, "driver.zip", lib.zip_bytes())
        listed = self.ok(self.get("/library/materials?kind=bundle", self.user))["materials"]
        self.assertEqual([m["id"] for m in listed], [bundle["id"]])
        listed = self.ok(self.get("/library/materials?kind=package", self.user))["materials"]
        self.assertEqual([m["id"] for m in listed], [package["id"]])
        listed = self.ok(self.get("/library/materials?kind=bundle&device_id=robot-7", self.user))["materials"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.ok(self.get("/library/materials?kind=bundle&device_id=robot-8", self.user))["materials"], [])

    def test_another_organisation_does_not_see_it(self):
        material = self.bundle()
        other = self.register("other@example.com")
        self.assertEqual(self.ok(self.get("/library/materials?kind=bundle", other))["materials"], [])
        self.assertEqual(self.content(other, material["id"]).status_code, 404)
        self.error(self.get(f"/library/materials/{material['id']}/parse", other), 404)

    def test_deleting_a_bundle(self):
        material = self.bundle()
        self.ok(self.delete(f"/library/materials/{material['id']}", self.user))
        self.assertEqual(self.stored_files(), [])


class BundleSize(LibraryCase):
    library_overrides = {"library_max_bundle_bytes": 2000}

    def test_a_bundle_has_its_own_size_limit(self):
        user = self.register("owner@example.com")
        refused = self.error(self.post("/library/uploads", {"filename": "walk.tar.gz", "size": 2001, "sha256": "a" * 64,
                                                            "kind": "bundle", **lib.LABELS, **DEVICE}, user), 413,
                             "file_too_large")
        self.assertEqual((refused["max_bytes"], refused["kind"]), (2000, "bundle"))
        small = lib.tar_gz_bytes(b"tiny")
        self.assertLess(len(small), 2000)
        self.upload(user, "walk.tar.gz", small, kind="bundle", **DEVICE)
        # a driver package of the same size is still fine
        self.ok(self.post("/library/uploads", {"filename": "driver.tar.gz", "size": 2001, "sha256": "b" * 64,
                                               **lib.LABELS}, user), 201)


def support_url(path: str) -> str:
    return lib.API + path
