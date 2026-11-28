"""Issue #78: the public library (公共资料库).

The user's decision of 2026-11-28: the collected robot models and device materials go into one public
library that every account sees. An operator puts them there with ``library-import``; every account
reads them (list, open, download, parse result); only platform operators change them."""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import library_support as lib
import parse_support as files
from support import API
from test_library_model import MEMBERS
from test_material_parse import ParseCase

from neuxmind_cloud import library_public
from neuxmind_cloud.__main__ import main as cli
from neuxmind_cloud.library_models import Material
from neuxmind_cloud.models import Organization

GO = {"body_type": "quadruped", "vendor": "宇树科技", "model": "Go2", "version": "unitree_ros@157ff95"}
ARM = {"body_type": "manipulator", "vendor": "宇树科技", "model": "R1-7A", "version": "2026-11"}


MODEL_ZIP = files.zip_bytes(MEMBERS)      # built once: a zip carries the time it was made


def model_zip() -> bytes:
    return MODEL_ZIP


class PublicCase(ParseCase):
    weknora = False

    def setUp(self):
        super().setUp()
        self.data = Path(tempfile.mkdtemp(dir=self.tmp))
        self.entries = []

    def add(self, name: str, data: bytes, **labels) -> None:
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.entries.append({"file": name, **labels})

    def write_manifest(self, **extra) -> None:
        (self.data / "manifest.json").write_text(json.dumps({"version": 1, "materials": self.entries, **extra}, ensure_ascii=False))

    def run_import(self, **kwargs) -> dict:
        self.write_manifest()
        return library_public.import_dir(self.db, self.settings, self.data, **kwargs)

    def seed(self) -> dict:
        self.add("models/unitree-go2.zip", model_zip(), kind="model", description="来源 unitree_ros", **GO)
        self.add("R1-7A/manual.pdf", lib.pdf_bytes("R1-7A 机械臂说明书"), **ARM)
        self.add("R1-7A/sdk.zip", lib.zip_bytes(b"sdk source"), **ARM)
        return self.run_import()

    def listed(self, user, query: str = "") -> list[dict]:
        return self.ok(self.get("/library/materials" + query, user))["materials"]

    def public_one(self, user, name: str) -> dict:
        return next(m for m in self.listed(user) if m["filename"] == name)


class ImportTest(PublicCase):
    def test_import_stores_each_listed_file_once(self):
        result = self.seed()
        self.assertEqual((result["stored"], result["exists"], result["refused"], result["checked"]), (3, 0, [], 3))
        again = self.run_import()
        self.assertEqual((again["stored"], again["exists"], again["refused"]), (0, 3, []))
        with self.db.transaction() as s:
            rows = s.query(Material).filter(Material.org_id == library_public.PUBLIC_ORG_ID).all()
            self.assertEqual(sorted((m.kind, m.status, m.created_by) for m in rows),
                             [("document", "parsing", library_public.IMPORTER), ("model", "parsing", library_public.IMPORTER),
                              ("package", "parsing", library_public.IMPORTER)])
        self.assertTrue(any(f.startswith(f"objects/{library_public.PUBLIC_ORG_ID}/") for f in self.stored_files()))
        self.assertFalse(any(f.startswith("staging/") for f in self.stored_files()))

    def test_another_file_under_a_taken_name_is_left_alone(self):
        self.seed()
        self.entries = []
        self.add("R1-7A/manual.pdf", lib.pdf_bytes("另一份内容"), **ARM)
        result = self.run_import()
        self.assertEqual(result["stored"], 0)
        self.assertEqual([r["file"] for r in result["refused"]], ["R1-7A/manual.pdf"])
        self.assertIn("version_exists", result["refused"][0]["reason"])

    def test_a_file_the_library_does_not_take_is_reported_and_the_rest_imported(self):
        self.add("tools/flash.exe", b"MZ" + bytes(200), **ARM)
        self.add("R1-7A/manual.pdf", lib.pdf_bytes(), **ARM)
        self.add("R1-7A/fake.pdf", b"not a pdf at all " * 20, **ARM)
        self.add("R1-7A/unlabelled.pdf", lib.pdf_bytes("x"), body_type="manipulator", vendor="宇树科技")
        result = self.run_import()
        self.assertEqual(result["stored"], 1)
        reasons = {r["file"]: r["reason"] for r in result["refused"]}
        self.assertEqual(set(reasons), {"tools/flash.exe", "R1-7A/fake.pdf", "R1-7A/unlabelled.pdf"})
        self.assertIn("unsupported_type", reasons["tools/flash.exe"])
        self.assertIn("unsupported_type", reasons["R1-7A/fake.pdf"])
        self.assertIn("型号", reasons["R1-7A/unlabelled.pdf"])

    def test_a_dry_run_checks_and_stores_nothing(self):
        self.add("models/unitree-go2.zip", model_zip(), kind="model", **GO)
        self.add("tools/flash.exe", b"MZ" + bytes(200), **ARM)
        result = self.run_import(dry_run=True)
        self.assertEqual((result["stored"], result["passed"], [r["file"] for r in result["refused"]]),
                         (0, 1, ["tools/flash.exe"]))
        self.assertEqual(self.stored_files(), [])
        with self.db.transaction() as s:
            self.assertIsNone(s.get(Organization, library_public.PUBLIC_ORG_ID), "a dry run writes nothing")

    def test_a_manifest_that_points_outside_its_folder_imports_nothing(self):
        outside = Path(self.tmp) / "secret.pdf"
        outside.write_bytes(lib.pdf_bytes())
        self.entries = [{"file": "../" + outside.name, **ARM}]
        self.write_manifest()
        with self.assertRaises(library_public.ManifestError):
            library_public.import_dir(self.db, self.settings, self.data)
        link = self.data / "link.pdf"
        os.symlink(outside, link)
        self.entries = [{"file": "link.pdf", **ARM}]
        self.write_manifest()
        with self.assertRaises(library_public.ManifestError):
            library_public.import_dir(self.db, self.settings, self.data)
        (self.data / "manifest.json").write_text(json.dumps({"materials": []}))
        with self.assertRaises(library_public.ManifestError):
            library_public.import_dir(self.db, self.settings, self.data)
        self.entries = [{"file": "a\u0000.pdf", **ARM}]
        self.write_manifest()
        with self.assertRaises(library_public.ManifestError):
            library_public.import_dir(self.db, self.settings, self.data)
        self.assertEqual(self.stored_files(), [])

    def test_the_command_line(self):
        self.add("R1-7A/manual.pdf", lib.pdf_bytes(), **ARM)
        self.write_manifest()
        env = {"DATABASE_URL": self.settings.database_url, "NEUXMIND_LIBRARY_DIR": self.library_dir}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli(["library-import", "--dir", str(self.data)])
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(json.loads(out.getvalue())["stored"], 1)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli(["library-import", "--dir", str(self.data / "nowhere")]), 2)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class OperatorMoves(PublicCase):
    def test_a_move_checks_the_public_library_for_a_clash_not_the_operators_own(self):
        self.add("a/a.pdf", lib.pdf_bytes("一"), **ARM)
        self.add("b/a.pdf", lib.pdf_bytes("二"), **{**ARM, "model": "R1-7B"})
        self.run_import()
        operator = self.operator()
        move = lambda material, model: self.client.put(f"{API}/library/materials/{material['id']}/product",
                                                       json={"vendor": "宇树科技", "model": model}, headers=operator["headers"])
        other = next(m for m in self.listed(operator) if m["model"] == "R1-7B")
        self.error(move(other, "R1-7A"), 409, "version_exists")      # was a 500: the clash was looked for in the operator's own
        self.upload(operator, "c.pdf", lib.pdf_bytes("三"), **ARM)    # the operator's own, same labels as the move below
        self.entries = []
        self.add("c/c.pdf", lib.pdf_bytes("四"), **{**ARM, "model": "R1-7C"})
        self.run_import()
        third = next(m for m in self.listed(operator) if m["model"] == "R1-7C")
        self.assertEqual(self.ok(move(third, "R1-7A"))["model"], "R1-7A", "the operator's own c.pdf is no clash for the public one")


class EveryAccountReads(PublicCase):
    def test_every_account_lists_the_public_materials_next_to_its_own(self):
        self.seed()
        own = self.upload(self.owner, "own.urdf", files.URDF_OK, **GO)
        stranger = self.register("someone@else.test")
        for user, expected in ((self.owner, 4), (stranger, 3)):
            rows = self.listed(user)
            self.assertEqual(len(rows), expected)
            for row in rows:
                self.assertEqual(row["public"], row["id"] != own["id"])
                self.assertEqual(row["read_only"], row["id"] != own["id"])
        by_product = self.listed(stranger, "?vendor=%E5%AE%87%E6%A0%91%E7%A7%91%E6%8A%80&model=go2&kind=model")
        self.assertEqual([m["filename"] for m in by_product], ["unitree-go2.zip"])

    def test_a_public_material_opens_downloads_and_shows_its_parse_result(self):
        self.seed()
        self.parse_all()
        stranger = self.register("someone@else.test")
        model = self.public_one(stranger, "unitree-go2.zip")
        detail = self.ok(self.get(f"/library/materials/{model['id']}", stranger))
        self.assertEqual((detail["public"], detail["read_only"], detail["status"]), (True, True, "parsed"))
        response = self.content(stranger, model["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, model_zip())
        result = self.result(model["id"], stranger)
        self.assertEqual((result["public"], result["read_only"], result["editable"]), (True, True, True))
        self.assertTrue(result["values"]["joints"])

    def test_only_an_operator_changes_a_public_material(self):
        self.seed()
        self.parse_all()
        stranger = self.register("someone@else.test")
        model = self.public_one(stranger, "unitree-go2.zip")
        result = self.result(model["id"], stranger)
        self.error(self.confirm(model["id"], {"revision": result["revision"]}, stranger), 403, "public_read_only")
        self.error(self.put(model["id"], {"revision": result["revision"], "values": result["values"]}, stranger), 403, "public_read_only")
        self.error(self.delete(f"/library/materials/{model['id']}", stranger), 403, "public_read_only")
        self.error(self.client.put(f"{API}/library/materials/{model['id']}/product", json={"vendor": "x", "model": "y"},
                                   headers=stranger["headers"]), 403, "public_read_only")
        operator = self.operator()
        self.assertFalse(self.public_one(operator, "unitree-go2.zip")["read_only"])
        self.assertFalse(self.result(model["id"], operator)["read_only"])
        confirmed = self.ok(self.confirm(model["id"], {"revision": result["revision"]}, operator))
        self.assertEqual(confirmed["status"], "usable")
        self.assertEqual(self.public_one(stranger, "unitree-go2.zip")["status"], "usable")
        manual = self.public_one(operator, "manual.pdf")
        self.ok(self.delete(f"/library/materials/{manual['id']}", operator))
        self.assertNotIn("manual.pdf", [m["filename"] for m in self.listed(stranger)])

    def test_another_accounts_own_materials_stay_private(self):
        own = self.upload(self.owner, "own.urdf", files.URDF_OK, **GO)
        stranger = self.register("someone@else.test")
        self.assertEqual(self.listed(stranger), [])
        self.error(self.get(f"/library/materials/{own['id']}", stranger), 404)
        self.assertEqual(self.content(stranger, own["id"]).status_code, 404)

    def test_an_account_may_keep_its_own_copy_of_a_public_file(self):
        self.seed()
        data = model_zip()
        own = self.upload(self.owner, "unitree-go2.zip", data, kind="model", **GO)
        self.assertFalse(own.get("public", False))
        self.assertEqual(sorted(m["public"] for m in self.listed(self.owner, "?kind=model")), [False, True])


if __name__ == "__main__":
    import unittest
    unittest.main()
