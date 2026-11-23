"""Issue #34: the rules parser in its child process — URDF limits, package metadata, Office checks,
and what it does with hostile files. Every file is made up by the test (parse_support)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import support  # noqa: F401  (puts cloud/ on sys.path)
import parse_support as files
from neuxmind_cloud import material_sandbox as sandbox
from neuxmind_cloud.material_sandbox import ExtractFailed, Limits

LINUX = sys.platform.startswith("linux")


class ExtractCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nx-extract-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, data: bytes, name: str = "material") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def facts(self, mode: str, data: bytes, limits: Limits = Limits()) -> dict:
        return sandbox.extract(mode, self.file(data), limits)

    def refused(self, mode: str, data: bytes, limits: Limits = Limits()) -> ExtractFailed:
        with self.assertRaises(ExtractFailed) as caught:
            self.facts(mode, data, limits)
        return caught.exception


class UrdfTests(ExtractCase):
    def test_every_joint_with_its_limits_and_every_link(self):
        facts = self.facts("urdf", files.URDF_OK)
        self.assertEqual(facts["robot_name"], "demo_arm")
        self.assertEqual(facts["links"], ["base_link", "upper_arm", "forearm", "slider", "wheel"])
        joints = {j["name"]: j for j in facts["joints"]}
        self.assertEqual(sorted(joints), ["elbow", "mount", "rail", "shoulder", "spin"])   # not the transmission's
        self.assertEqual((joints["shoulder"]["lower"], joints["shoulder"]["upper"], joints["shoulder"]["velocity"],
                          joints["shoulder"]["effort"]), ("-1.57", "1.57", "2.0", "40"))
        self.assertEqual((joints["shoulder"]["parent"], joints["shoulder"]["child"]), ("base_link", "upper_arm"))
        self.assertEqual(joints["rail"]["type"], "prismatic")
        self.assertEqual((joints["spin"]["velocity"], joints["spin"]["effort"]), ("10", "5"))
        self.assertNotIn("lower", joints["spin"])
        self.assertEqual(facts["warnings"], [])

    def test_xacro_expressions_are_kept_as_text_and_reported(self):
        facts = self.facts("urdf", files.URDF_GAPS)
        j1 = next(j for j in facts["joints"] if j["name"] == "j1")
        self.assertEqual(j1["raw"]["lower"], "${-pi/2}")
        self.assertTrue(any("xacro 表达式" in w for w in facts["warnings"]))

    def test_a_document_that_is_not_a_robot_is_refused(self):
        self.assertEqual(self.refused("urdf", b'<?xml version="1.0"?><launch/>').code, "urdf_root")

    def test_gb18030_and_utf16_are_read(self):
        text = '<?xml version="1.0" encoding="GB18030"?><robot name="机器人"><link name="底座"/></robot>'
        self.assertEqual(self.facts("urdf", text.encode("gb18030"))["links"], ["底座"])
        self.assertEqual(self.facts("urdf", text.replace("GB18030", "UTF-16").encode("utf-16"))["robot_name"], "机器人")


class HostileXmlTests(ExtractCase):
    def test_external_entity_is_refused_and_nothing_is_read(self):
        failure = self.refused("urdf", files.XXE)
        self.assertEqual(failure.code, "xml_doctype")
        self.assertNotIn("root", failure.message)

    def test_entity_expansion_is_refused(self):
        started = time.monotonic()
        self.assertEqual(self.refused("urdf", files.LAUGHS).code, "xml_doctype")
        self.assertLess(time.monotonic() - started, 10)

    def test_deep_nesting_is_refused(self):
        self.assertEqual(self.refused("urdf", files.deep_xml(200)).code, "xml_too_deep")

    def test_too_many_elements_are_refused(self):
        self.assertEqual(self.refused("urdf", files.many_elements(200_001)).code, "xml_too_large")

    def test_a_doctype_in_an_office_relationship_part_is_refused(self):
        rels = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "http://169.254.169.254/">]><Relationships/>'
        self.assertEqual(self.refused("docx", files.docx_with(rels)).code, "xml_doctype")

    def test_package_xml_with_a_doctype_is_reported_not_followed(self):
        data = files.zip_bytes({"pkg/package.xml": files.XXE.replace(b"robot", b"package")})
        facts = self.facts("zip", data)
        self.assertNotIn("package.xml", facts["metadata"])
        self.assertTrue(any("DOCTYPE" in w for w in facts["warnings"]))


class PackageTests(ExtractCase):
    def test_deb_control_fields(self):
        for compression in ("gz", "xz", ""):
            with self.subTest(compression=compression):
                control = self.facts("deb", files.deb_bytes(compression=compression))["control"]
                self.assertEqual((control["Package"], control["Version"], control["Architecture"]),
                                 ("demo-driver", "2.3.1-1", "arm64"))
                self.assertEqual(control["Homepage"], "https://example.test/driver")
                self.assertNotIn("Maintainer", control)

    @unittest.skipIf(shutil.which("dpkg-deb"), "dpkg-deb present: the real zstd case below runs instead")
    def test_deb_with_zstd_control_without_a_reader_says_so(self):
        failure = self.refused("deb", files.deb_bytes(compression="zst"))
        self.assertEqual(failure.code, "deb_zstd")
        self.assertIn("手工填写", failure.message)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb (the cloud image has it)")
    def test_deb_with_zstd_control_is_read_with_dpkg_deb(self):
        root = os.path.join(self.tmp, "pkg")
        os.makedirs(os.path.join(root, "DEBIAN"))
        os.makedirs(os.path.join(root, "usr", "bin"))
        with open(os.path.join(root, "DEBIAN", "control"), "wb") as handle:
            handle.write(b"Package: zst-demo\nVersion: 1.0\nArchitecture: amd64\nMaintainer: t <t@example.test>\n"
                         b"Description: demo\n")
        with open(os.path.join(root, "usr", "bin", "demo"), "wb") as handle:
            handle.write(b"#!/bin/sh\n")
        built = os.path.join(self.tmp, "demo.deb")
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zzstd", "--build", root, built], check=True,
                       capture_output=True)
        with open(built, "rb") as handle:
            control = self.facts("deb", handle.read())["control"]
        self.assertEqual((control["Package"], control["Version"], control["Architecture"]), ("zst-demo", "1.0", "amd64"))

    def test_zip_with_package_xml_and_programs(self):
        data = files.zip_bytes({"demo_driver/package.xml": files.package_xml(), "demo_driver/lib/libdemo.so": files.elf(0xB7),
                                "demo_driver/bin/demo": files.elf(0xB7), "demo_driver/README.md": b"# readme\n" * 20})
        facts = self.facts("zip", data)
        self.assertEqual(facts["metadata"]["package.xml"], {"name": "demo_driver", "version": "0.4.2",
                                                            "description": "a demo driver"})
        self.assertEqual(facts["machines"], {"arm64": 2})
        self.assertEqual(facts["unsafe_count"], 0)

    def test_tar_gz_with_debian_control(self):
        data = files.tar_bytes({"demo/DEBIAN/control": files.CONTROL, "demo/usr/bin/demo": files.elf(0x3E)})
        import gzip
        facts = self.facts("tar.gz", gzip.compress(data, mtime=0))
        self.assertEqual(facts["metadata"]["control"]["Package"], "demo-driver")
        self.assertEqual(facts["machines"], {"amd64": 1})

    def test_tar_xz_bz2_and_plain_tar(self):
        import bz2
        import lzma
        data = files.tar_bytes({"x/package.xml": files.package_xml("x_driver", "1.2.3")})
        for mode, packed in (("tar.xz", lzma.compress(data)), ("tar.bz2", bz2.compress(data)), ("tar", data)):
            with self.subTest(mode=mode):
                self.assertEqual(self.facts(mode, packed)["metadata"]["package.xml"]["version"], "1.2.3")

    def test_long_names_in_gnu_and_pax_form(self):
        import tarfile
        name = "d/" + "n" * 150 + "/package.xml"
        for fmt in (tarfile.GNU_FORMAT, tarfile.PAX_FORMAT):
            with self.subTest(fmt=fmt):
                facts = self.facts("tar", files.tar_bytes({name: files.package_xml()}, fmt=fmt))
                self.assertEqual(facts["members"], 1)


class HostileArchiveTests(ExtractCase):
    def test_names_leading_out_of_the_archive_are_reported_and_never_written(self):
        outside = os.path.join(self.tmp, "evil.sh")
        data = files.zip_bytes({"../evil.sh": b"#!/bin/sh\n", "/etc/cron.d/x": b"x", "ok/file.txt": b"ok"},
                               symlinks={"ok/link": "../../../etc/passwd"})
        facts = self.facts("zip", data)
        self.assertEqual(facts["unsafe_count"], 2)
        self.assertEqual(facts["links_out"], 1)
        self.assertTrue(any("指向包外" in w for w in facts["warnings"]))
        self.assertFalse(os.path.exists(outside))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["material"])

    def test_tar_links_and_traversal(self):
        data = files.tar_bytes({"../../tmp/x": b"x", "a/b": b"y"}, links={"a/up": "../../../root", "a/ok": "b"})
        facts = self.facts("tar", data)
        self.assertEqual((facts["unsafe_count"], facts["links_out"]), (1, 1))

    def test_zip_bomb_is_not_unpacked(self):
        started = time.monotonic()
        facts = self.facts("zip", files.zip_bomb(100))
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(any("压缩比异常高" in w for w in facts["warnings"]))
        self.assertEqual(facts["metadata"]["package.xml"]["name"], "demo_driver")

    def test_tar_gz_bomb_stops_at_the_unpacked_limit(self):
        failure = self.refused("tar.gz", files.zero_gzip_members(64), Limits(unpacked_bytes=8 * 1024 * 1024))
        self.assertEqual(failure.code, "unpack_limit")

    def test_a_huge_extended_header_is_refused_not_read(self):
        self.assertEqual(self.refused("tar", files.huge_pax_tar()).code, "archive_invalid")

    def test_xz_needing_more_memory_than_allowed(self):
        import lzma
        data = lzma.compress(files.tar_bytes({"a": b"x"}), preset=6)
        self.assertEqual(self.refused("tar.xz", data, Limits(xz_memory_bytes=1024 * 1024)).code, "unpack_memory")

    def test_too_many_members(self):
        data = files.zip_bytes({f"f{i}": b"" for i in range(60)})
        self.assertEqual(self.refused("zip", data, Limits(members=50)).code, "too_many_members")

    def test_corrupt_archives_fail_only_this_file(self):
        for mode, data in (("zip", b"PK\x03\x04" + os.urandom(500)), ("tar.gz", b"\x1f\x8b\x08" + os.urandom(500)),
                           ("deb", b"!<arch>\n" + os.urandom(200)), ("tar", os.urandom(2048))):
            with self.subTest(mode=mode):
                self.assertIn(self.refused(mode, data).code, ("archive_invalid", "archive_truncated"))

    def test_wall_clock_limit_kills_the_parser(self):
        data = files.zero_gzip_members(4096)
        started = time.monotonic()
        failure = self.refused("tar.gz", data, Limits(wall_seconds=0.5, unpacked_bytes=16 * 1024 ** 3))
        self.assertEqual(failure.code, "parse_timeout")
        self.assertLess(time.monotonic() - started, 10)

    def test_cpu_limit_stops_the_parser(self):
        data = files.zero_gzip_members(4096)
        failure = self.refused("tar.gz", data, Limits(cpu_seconds=1, wall_seconds=60, unpacked_bytes=16 * 1024 ** 3))
        self.assertEqual(failure.code, "parse_timeout")

    @unittest.skipUnless(LINUX, "address-space limits are enforced on Linux only")
    def test_memory_limit_stops_the_parser(self):
        data = b'<robot name="m">' + b"<link name='x'/>" * (30 * 1024 * 1024 // 16) + b"</robot>"
        failure = self.refused("urdf", data, Limits(memory_bytes=96 * 1024 * 1024))
        self.assertIn(failure.code, ("out_of_memory", "parse_crashed"))


class SandboxTests(ExtractCase):
    def worker(self, body: str) -> None:
        path = os.path.join(self.tmp, "worker.py")
        with open(path, "w") as handle:
            handle.write(body)
        original = sandbox.WORKER
        sandbox.WORKER = path
        self.addCleanup(setattr, sandbox, "WORKER", original)

    def test_a_crash_fails_only_this_material(self):
        self.worker("import os\nos._exit(3)\n")
        self.assertEqual(self.refused("urdf", b"<robot/>").code, "parse_crashed")

    def test_too_much_output_is_cut_off(self):
        self.worker("import sys\nsys.stdout.write('x' * (8 * 1024 * 1024))\n")
        self.assertEqual(self.refused("urdf", b"<robot/>").code, "parse_output")

    def test_the_child_cannot_write_files_and_starts_in_root(self):
        self.worker("import json, os, sys\nsys.path.insert(0, %r)\nimport material_extract as m\n"
                    "m.apply_limits({'cpu': 5})\n"
                    "try:\n    os.write(os.open(%r, os.O_WRONLY | os.O_CREAT, 0o600), b'x'); wrote = True\n"
                    "except OSError:\n    wrote = False\n"
                    "print(json.dumps({'ok': True, 'facts': {'wrote': wrote, 'cwd': os.getcwd(), "
                    "'env': sorted(os.environ)}}))\n" % (os.path.dirname(sandbox.WORKER), os.path.join(self.tmp, "probe")))
        facts = self.facts("urdf", b"<robot/>")
        self.assertFalse(facts["wrote"])
        self.assertEqual(facts["cwd"], "/")
        self.assertNotIn("DATABASE_URL", facts["env"])

    def test_the_file_is_not_followed_through_a_symlink(self):
        target = self.file(files.URDF_OK, "real.urdf")
        link = os.path.join(self.tmp, "link.urdf")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            sandbox.extract("urdf", link)


class OfficeTests(ExtractCase):
    def test_macros_are_found(self):
        self.assertTrue(self.facts("docx", files.docx_with(macro=True))["macros"])
        self.assertTrue(self.facts("doc", files.ole_with("_VBA_PROJECT"))["macros"])
        self.assertFalse(self.facts("doc", files.ole_with("Data"))["macros"])

    def test_external_references_are_found_and_hyperlinks_are_not_counted_as_such(self):
        facts = self.facts("docx", files.docx_with(files.rels(("hyperlink", "https://example.test/manual"),
                                                              ("image", "http://192.0.2.1/x.png"),
                                                              ("attachedTemplate", "file:///share/t.dotm"))))
        self.assertEqual(facts["hyperlinks"], 1)
        self.assertEqual(len(facts["external"]), 2)
        self.assertTrue(any("192.0.2.1" in e for e in facts["external"]))

    def test_spreadsheet_external_data(self):
        data = files.zip_bytes({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>",
                                "xl/externalLinks/externalLink1.xml": b"<externalLink/>"})
        self.assertEqual(len(self.facts("xlsx", data)["external"]), 1)

    def test_padding_with_relationship_parts_does_not_skip_the_check(self):
        padding = {f"pad/_rels/p{i}.xml.rels": b"<Relationships/>" for i in range(301)}
        data = files.docx_with(files.rels(("image", "http://192.0.2.1/x.png")), macro=True, extra=padding)
        self.assertEqual(self.refused("docx", data).code, "too_many_parts")

    def test_old_format_field_codes_that_fetch_content(self):
        fetching = [b"\x13 INCLUDEPICTURE \"http://192.0.2.1/a.png\"", b"\x13 includepicture \"http://192.0.2.1/a.png\"",
                    "\x13 IncludeText \"\\\\host\\x.doc\"".encode("utf-16-le"), b"\x13LINK  Excel.Sheet.8 \"c:\\x.xls\"",
                    b"\x13 DDEAUTO c:\\x", "\x13  ddeauto x".encode("utf-16-le")]
        for marker in fetching:
            with self.subTest(marker=marker[:24]):
                self.assertTrue(self.facts("doc", files.ole_with("Data") + marker)["external"])
        harmless = [b"\x13 HYPERLINK \"https://example.test\"", b"see IMPORT settings and LINK Word docs",
                    "导入 IMPORT 和 LINK Excel 的说明".encode("utf-16-le"), b"\x13 LINKS are listed"]
        for text in harmless:
            with self.subTest(text=text[:24]):
                self.assertEqual(self.facts("doc", files.ole_with("Data") + text)["external"], [])

    def test_old_spreadsheet_functions_that_fetch_content(self):
        self.assertTrue(self.facts("xls", files.ole_with("Workbook") + b"_xlfn.WEBSERVICE")["external"])
        self.assertTrue(self.facts("xls", files.ole_with("Workbook") + "_XLFN.FilterXML".encode("utf-16-le"))["external"])
        self.assertEqual(self.facts("xls", files.ole_with("Workbook") + b"WEBSERVICE in a cell")["external"], [])

    def test_plain_document(self):
        self.assertEqual(self.facts("docx", files.docx_with()), {"macros": False, "external": [], "hyperlinks": 0})


if __name__ == "__main__":
    unittest.main()
