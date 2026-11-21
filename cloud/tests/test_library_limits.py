"""Issue #33: size limits, quotas, rate limits, file names, duplicates and how a file is served."""
import os
import stat

import library_support as lib
from library_support import LABELS, LibraryCase, sha
from support import API


class SizeLimitTest(LibraryCase):
    library_overrides = {"library_max_document_bytes": 1000, "library_max_package_bytes": 5000,
                         "library_org_quota_bytes": 9000}

    def setUp(self):
        super().setUp()
        self.user = self.register("dev@limits.test")

    def declare(self, filename: str, size: int, **fields):
        body = {"filename": filename, "size": size, "sha256": "ab" * 32, **LABELS, **fields}
        return self.post("/library/uploads", body, self.user)

    def test_documents_and_packages_have_their_own_limits(self):
        self.ok(self.declare("ok.pdf", 1000), 201)
        body = self.error(self.declare("big.pdf", 1001), 413, "file_too_large")
        self.assertEqual((body["max_bytes"], body["kind"]), (1000, "document"))
        self.ok(self.declare("ok.zip", 1001), 201)
        body = self.error(self.declare("big.zip", 5001), 413, "file_too_large")
        self.assertEqual((body["max_bytes"], body["kind"]), (5000, "package"))

    def test_an_empty_or_unsized_file_is_refused(self):
        for size in (0, -1, 1.5, "10", None, True):
            with self.subTest(size=size):
                self.error(self.declare("empty.pdf", size), 400, "invalid_request")

    def test_the_organisation_quota_counts_stored_files_and_open_uploads(self):
        stored = lib.zip_bytes(os.urandom(3000))
        self.upload(self.user, "one.zip", stored)
        self.ok(self.declare("two.zip", 4000), 201)                       # reserved while it is open
        body = self.error(self.declare("three.zip", 9000 - len(stored) - 4000 + 1), 409, "library_quota_exceeded")
        self.assertEqual(body["quota_bytes"], 9000)
        self.assertEqual(body["used_bytes"], len(stored) + 4000)
        self.ok(self.declare("three.zip", 9000 - len(stored) - 4000), 201)

    def test_the_declared_size_is_all_that_can_be_sent(self):
        data = lib.pdf_bytes()
        upload = self.start(self.user, "spec.pdf", data)
        self.assertEqual(upload["chunk_count"], 1)
        self.error(self.put_chunk(self.user, upload["id"], 0, data + b"extra"), 400, "invalid_chunk")
        self.error(self.put_chunk(self.user, upload["id"], 1, b"extra"), 400, "invalid_chunk")


class ThrottleTest(LibraryCase):
    library_overrides = {"library_max_open_uploads": 2, "library_uploads_per_hour": 4}

    def setUp(self):
        super().setUp()
        self.user = self.register("dev@throttle.test")

    def test_open_uploads_are_bounded(self):
        first = self.start(self.user, "a.pdf", lib.pdf_bytes("a"))
        self.start(self.user, "b.pdf", lib.pdf_bytes("b"))
        self.error(self.start_raw(self.user, "c.pdf", lib.pdf_bytes("c")), 409, "too_many_uploads")
        self.ok(self.delete(f"/library/uploads/{first['id']}", self.user))
        self.start(self.user, "c.pdf", lib.pdf_bytes("c"))

    def test_starting_uploads_is_rate_limited(self):
        for i in range(4):
            self.upload(self.user, f"{i}.pdf", lib.pdf_bytes(str(i)))
        body = self.error(self.start_raw(self.user, "5.pdf", lib.pdf_bytes("5")), 429, "rate_limited")
        self.assertGreater(body["retry_after"], 0)
        # Another account is not held back by it.
        other = self.register("other@throttle.test")
        self.upload(other, "0.pdf", lib.pdf_bytes("0"))

    def test_sending_the_same_chunk_again_and_again_is_bounded(self):
        data = lib.pdf_bytes()
        upload = self.start(self.user, "spec.pdf", data)
        answers = [self.put_chunk(self.user, upload["id"], 0, data).status_code for _ in range(40)]
        self.assertEqual(answers[0], 200)
        self.assertIn(429, answers)
        last = self.error(self.put_chunk(self.user, upload["id"], 0, data), 429, "chunk_attempts_exceeded")
        self.assertIn("error", last)
        self.ok(self.complete(self.user, upload["id"]), 201)


class BodyLimitTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@body.test")

    def test_a_chunk_body_above_the_chunk_size_is_refused(self):
        data = lib.text_bytes(self.chunk_bytes + 10)
        upload = self.start(self.user, "long.txt", data)
        response = self.put_chunk(self.user, upload["id"], 0, data + b"x" * 5000)
        body = self.error(response, 413, "payload_too_large")
        self.assertEqual(body["max_bytes"], self.chunk_bytes)

    def test_the_other_library_routes_keep_the_small_json_limit(self):
        self.assertGreater(self.chunk_bytes, self.settings.max_body_bytes)
        huge = {"filename": "a.pdf", "size": 10, "sha256": "ab" * 32, **LABELS, "description": "x" * self.chunk_bytes}
        self.error(self.post("/library/uploads", huge, self.user), 413, "payload_too_large")
        # Only PUT …/chunks/<n> gets the chunk bound; a POST to the same address does not.
        upload = self.start(self.user, "spec.pdf", lib.pdf_bytes())
        response = self.client.post(f"{API}/library/uploads/{upload['id']}/chunks/0", content=b"x" * (self.chunk_bytes - 1),
                                    headers={**self.user["headers"], "Content-Type": "application/octet-stream"})
        self.error(response, 413, "payload_too_large")


class NameAndDuplicateTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@names.test")

    def test_file_names_are_cleaned_and_never_become_paths(self):
        cases = [("../../etc/passwd.pdf", "passwd.pdf"), ("/absolute/路径/手册.pdf", "手册.pdf"),
                 ("C:\\Users\\someone\\Desktop\\规格书.pdf", "规格书.pdf"), ("  spaced   name .pdf", "spaced name.pdf"),
                 ("bad\x00name\r\n.pdf", "badname.pdf"), ('a<b>c:d"e|f?g*h.pdf', "a_b_c_d_e_f_g_h.pdf"),
                 ("...hidden.pdf", "hidden.pdf"), ("UPPER.PDF", "UPPER.pdf"), ("x" * 300 + ".pdf", "x" * 146 + ".pdf")]
        for index, (given, stored) in enumerate(cases):
            with self.subTest(given=given):
                material = self.upload(self.user, given, lib.pdf_bytes(str(index)), version=f"v{index}")
                self.assertEqual(material["filename"], stored)
        for name in ("", ".pdf", "..", "../.pdf", "   .pdf", 7, None):
            with self.subTest(name=name):
                self.error(self.start_raw(self.user, name, lib.pdf_bytes("x")), 400, "invalid_request")

    def test_nothing_is_stored_under_the_uploaded_name_or_as_a_program(self):
        data = lib.pdf_bytes()
        self.upload(self.user, "../../evil.pdf", data)
        files = self.stored_files()
        self.assertEqual(files, [os.path.join("objects", self.user["org_id"], sha(data)[:2], sha(data))])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.pdf")))
        mode = os.stat(os.path.join(self.library_dir, files[0])).st_mode
        self.assertEqual(stat.S_IMODE(mode) & 0o133, 0)             # no execute bit, not writable by others

    def test_the_same_material_and_version_is_not_taken_twice(self):
        data = lib.pdf_bytes()
        first = self.upload(self.user, "spec.pdf", data)
        body = self.error(self.start_raw(self.user, "spec.pdf", data), 409, "material_exists")
        self.assertEqual(body["material_id"], first["id"])
        # A different file name does not make the same content a new material …
        body = self.error(self.start_raw(self.user, "renamed.pdf", data), 409, "material_exists")
        self.assertEqual(body["material_id"], first["id"])
        # … and neither does different capitalisation of the labels.
        self.error(self.start_raw(self.user, "spec.pdf", data, vendor="示例厂家 ", model="x1"), 409, "material_exists")
        self.assertEqual(self.ok(self.get("/library/materials", self.user))["total"], 1)

    def test_the_same_name_and_version_with_other_content_is_refused(self):
        first = self.upload(self.user, "spec.pdf", lib.pdf_bytes("one"))
        body = self.error(self.start_raw(self.user, "spec.pdf", lib.pdf_bytes("two")), 409, "version_exists")
        self.assertEqual(body["material_id"], first["id"])
        self.assertIn("版本", body["error"])
        # With its own version number it is a new material.
        self.upload(self.user, "spec.pdf", lib.pdf_bytes("two"), version="1.1")

    def test_the_same_content_under_another_version_or_model_is_kept_once_on_disk(self):
        data = lib.pdf_bytes()
        a = self.upload(self.user, "spec.pdf", data)
        b = self.upload(self.user, "spec.pdf", data, version="2.0")
        c = self.upload(self.user, "spec.pdf", data, model="X2")
        self.assertEqual(len({a["id"], b["id"], c["id"]}), 3)
        self.assertEqual(len(self.stored_files()), 1)
        self.ok(self.delete(f"/library/materials/{a['id']}", self.user))
        self.ok(self.delete(f"/library/materials/{b['id']}", self.user))
        self.assertEqual(self.content(self.user, c["id"]).content, data)
        self.ok(self.delete(f"/library/materials/{c['id']}", self.user))
        self.assertEqual(self.stored_files(), [])
        self.error(self.content(self.user, c["id"]), 404)

    def test_a_duplicate_found_only_when_two_uploads_finish_is_still_refused(self):
        data = lib.pdf_bytes()
        other = self.member(self.user, "colleague@names.test", "developer")
        mine = self.start(self.user, "spec.pdf", data)
        theirs = self.start(other, "spec.pdf", data)
        self.assertNotEqual(mine["id"], theirs["id"])
        self.ok(self.put_chunk(self.user, mine["id"], 0, data))
        self.ok(self.put_chunk(other, theirs["id"], 0, data))
        first = self.ok(self.complete(self.user, mine["id"]), 201)
        body = self.error(self.complete(other, theirs["id"]), 409, "material_exists")
        self.assertEqual(body["material_id"], first["id"])
        self.error(self.get(f"/library/uploads/{theirs['id']}", other), 404)
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(self.content(self.user, first["id"]).content, data)

    def test_labels_are_required_and_checked(self):
        data = lib.pdf_bytes()
        bad = [{"vendor": ""}, {"vendor": None}, {"model": "   "}, {"version": "v" * 51}, {"vendor": "v" * 101},
               {"body_type": "Quadruped Robot"}, {"body_type": "../x"}, {"body_type": ""}, {"vendor": "a\x00b"},
               {"sha256": "xyz"}, {"sha256": sha(data).upper() + "0"}, {"description": "d" * 2001}, {"description": 5}]
        for fields in bad:
            with self.subTest(fields=fields):
                self.error(self.start_raw(self.user, "spec.pdf", data, **fields), 400, "invalid_request")
        kept = self.upload(self.user, "spec.pdf", data, vendor="  示例  厂家 ", description=" 关节限位表 ")
        self.assertEqual((kept["vendor"], kept["description"]), ("示例 厂家", "关节限位表"))


class ServingTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@serving.test")

    def test_a_file_is_only_ever_served_as_a_download(self):
        page = b"<html><script>alert(1)</script></html>\n"
        material = self.upload(self.user, "looks-like-a-page.txt", page)
        response = self.content(self.user, material["id"])
        self.assertEqual(response.content, page)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-checksum-sha256"], sha(page))

    def test_the_download_name_is_the_cleaned_name(self):
        material = self.upload(self.user, '../操作 "手册".pdf', lib.pdf_bytes())
        disposition = self.content(self.user, material["id"]).headers["content-disposition"]
        self.assertNotIn("..", disposition)
        self.assertNotIn("/", disposition.split("filename", 1)[1])
        self.assertIn("UTF-8''", disposition)

    def test_part_of_a_file_can_be_fetched(self):
        data = lib.text_bytes(5000)
        material = self.upload(self.user, "long.txt", data)
        response = self.content(self.user, material["id"], Range="bytes=100-199")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, data[100:200])
        self.assertEqual(response.headers["content-range"], f"bytes 100-199/{len(data)}")

    def test_listing_filters_and_pages(self):
        self.upload(self.user, "a.pdf", lib.pdf_bytes("a"))
        self.upload(self.user, "b.pdf", lib.pdf_bytes("b"), body_type="manipulator", vendor="另一家", model="A7")
        self.upload(self.user, "driver.zip", lib.zip_bytes(), version="2.0")

        def names(query: str) -> list[str]:
            return sorted(m["filename"] for m in self.ok(self.get("/library/materials" + query, self.user))["materials"])
        self.assertEqual(names("?body_type=manipulator"), ["b.pdf"])
        self.assertEqual(names("?kind=package"), ["driver.zip"])
        self.assertEqual(names("?vendor=另一家"), ["b.pdf"])
        self.assertEqual(names("?model=x1"), ["a.pdf", "driver.zip"])
        self.assertEqual(names("?version=2.0"), ["driver.zip"])
        self.assertEqual(names("?status=parsing"), ["a.pdf", "b.pdf", "driver.zip"])
        self.assertEqual(names("?q=DRIVER"), ["driver.zip"])
        self.assertEqual(names("?sha256=" + sha(lib.pdf_bytes("a"))), ["a.pdf"])
        page = self.ok(self.get("/library/materials?limit=2", self.user))
        self.assertEqual((len(page["materials"]), page["total"], page["limit"], page["offset"]), (2, 3, 2, 0))
        rest = self.ok(self.get("/library/materials?limit=2&offset=2", self.user))
        self.assertEqual(len(rest["materials"]), 1)
        self.assertEqual(len({m["id"] for m in page["materials"] + rest["materials"]}), 3)
        self.error(self.get("/library/materials?limit=0", self.user), 400)
        self.error(self.get("/library/materials?kind=virus", self.user), 400)

    def test_deleting_removes_the_record_and_the_bytes(self):
        material = self.upload(self.user, "spec.pdf", lib.pdf_bytes())
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(self.ok(self.delete(f"/library/materials/{material['id']}", self.user)),
                         {"deleted": True, "id": material["id"]})
        self.error(self.get(f"/library/materials/{material['id']}", self.user), 404)
        self.error(self.content(self.user, material["id"]), 404)
        self.assertEqual(self.stored_files(), [])
        self.error(self.delete(f"/library/materials/{material['id']}", self.user), 404)

    def test_ids_that_are_not_ids_are_not_found(self):
        for bad in ("..%2F..%2Fetc", "mat_" + "g" * 20, "upl_" + "0" * 20, "x" * 500):
            with self.subTest(bad=bad):
                self.error(self.get(f"/library/materials/{bad}", self.user), 404)
                self.error(self.content(self.user, bad), 404)
                self.error(self.get(f"/library/uploads/{bad}", self.user), 404)
