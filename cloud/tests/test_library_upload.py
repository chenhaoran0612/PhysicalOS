"""Issue #33 acceptance: Word, Excel, PDF, text, URDF and a driver package are accepted;
anything else is refused and the answer says what is supported. The type is decided by
the content, not by the file name."""
import gzip

import library_support as lib
from library_support import ELF, LABELS, LibraryCase, sha


class AcceptanceTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@library.test")

    def test_word_excel_pdf_text_urdf_and_a_driver_package_are_all_accepted(self):
        files = [("操作手册.docx", lib.docx_bytes(), "document", "docx"),
                 ("关节参数表.xlsx", lib.xlsx_bytes(), "document", "xlsx"),
                 ("规格书.pdf", lib.pdf_bytes(), "document", "pdf"),
                 ("安装说明.md", lib.text_bytes(), "document", "markdown"),
                 ("robot.urdf", lib.urdf_bytes(), "document", "urdf"),
                 ("driver-1.0.tar.gz", lib.tar_gz_bytes(), "package", "tar.gz")]
        for name, data, kind, fmt in files:
            with self.subTest(name):
                material = self.upload(self.user, name, data)
                self.assertEqual(material["filename"], name)
                self.assertEqual((material["kind"], material["format"]), (kind, fmt))
                self.assertEqual((material["size"], material["sha256"]), (len(data), sha(data)))
                self.assertEqual({k: material[k] for k in LABELS}, LABELS)
                # Parsing is issue #34; a finished upload waits in 「解析中」.
                self.assertEqual(material["status"], "parsing")
                fetched = self.content(self.user, material["id"])
                self.assertEqual(fetched.status_code, 200, fetched.text)
                self.assertEqual(fetched.content, data)
                self.assertEqual(fetched.headers["x-checksum-sha256"], sha(data))
        listing = self.ok(self.get("/library/materials", self.user))
        self.assertEqual(listing["total"], len(files))
        self.assertEqual({m["filename"] for m in listing["materials"]}, {name for name, *_ in files})

    def test_every_listed_format_is_accepted(self):
        files = [("旧手册.doc", lib.doc_bytes(), "doc"), ("旧表格.xls", lib.xls_bytes(), "xls"),
                 ("notes.txt", "纯文本说明\n".encode(), "text"), ("gbk.txt", "国标编码的说明\n".encode("gb18030"), "text"),
                 ("params.yaml", b"gait:\n  period: 0.5\n", "yaml"), ("limits.yml", b"max_vx: 1.0\n", "yaml"),
                 ("config.json", b'{"joints": 12}\n', "json"), ("layout.xml", b'<?xml version="1.0"?><layout/>', "xml"),
                 ("table.csv", b"joint,limit\nhip,1.2\n", "text"), ("leg.xacro", lib.urdf_bytes("leg"), "urdf"),
                 ("driver.zip", lib.zip_bytes(), "zip"), ("driver.tgz", lib.tar_gz_bytes(), "tar.gz"),
                 ("driver.tar.xz", lib.tar_xz_bytes(), "tar.xz"), ("driver.tar.bz2", lib.tar_bz2_bytes(), "tar.bz2"),
                 ("driver.tar", lib.tar_bytes(), "tar"), ("driver_1.0_arm64.deb", lib.deb_bytes(), "deb")]
        for name, data, fmt in files:
            with self.subTest(name):
                material = self.upload(self.user, name, data)
                self.assertEqual(material["format"], fmt)
                self.assertEqual(self.content(self.user, material["id"]).content, data)

    def test_a_file_of_several_chunks_arrives_whole(self):
        data = lib.random_zip(3 * self.chunk_bytes + 12345)
        upload = self.start(self.user, "big-driver.zip", data)
        self.assertGreaterEqual(upload["chunk_count"], 4)
        self.assertEqual(upload["chunk_bytes"], self.chunk_bytes)
        for index, part in enumerate(self.parts(upload, data)):
            answer = self.ok(self.put_chunk(self.user, upload["id"], index, part))
            self.assertEqual((answer["received_count"], answer["chunk_count"]), (index + 1, upload["chunk_count"]))
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.assertEqual(self.content(self.user, material["id"]).content, data)

    def test_other_types_are_refused_and_the_answer_names_what_is_supported(self):
        for name in ("setup.exe", "page.xhtml", "install.sh", "archive.rar", "photo.png", "macro.docm",
                     "noextension", "driver.tar.gz.exe", "vector.svg"):
            with self.subTest(name):
                body = self.error(self.start_raw(self.user, name, b"x" * 64), 415, "unsupported_type")
                for extension in (".docx", ".xlsx", ".pdf", ".md", ".urdf", ".tar.gz", ".deb"):
                    self.assertIn(extension, body["supported"])
                for word in ("Word", "Excel", "PDF", "URDF", ".zip", ".deb"):
                    self.assertIn(word, body["error"])
        self.assertEqual(self.ok(self.get("/library/materials", self.user))["total"], 0)
        self.assertEqual(self.stored_files(), [])

    def test_the_content_decides_the_type_not_the_file_name(self):
        cases = [("程序.pdf", ELF), ("程序.docx", ELF), ("压缩包.docx", lib.zip_bytes()),
                 ("文档.xlsx", lib.docx_bytes()), ("表格.docx", lib.xlsx_bytes()),
                 ("程序.txt", b"MZ\x90\x00\x03\x00\x00\x00" + bytes(200)), ("程序.md", ELF),
                 ("网页.urdf", b"<html><script>alert(1)</script></html>"), ("文字.json", b"<a/>"),
                 ("文字.xml", b"plain words, no markup"), ("假包.tar.gz", gzip.compress(b"no tar inside" * 60)),
                 ("假包.tgz", b"\x1f\x8b\x08" + b"\xff" * 80), ("假包.tar", b"x" * 1024),
                 ("假包.deb", b"!<arch>\n" + b"something-else   " + bytes(100)), ("文档.zip", lib.pdf_bytes()),
                 ("假包.tar.xz", lib.tar_gz_bytes()), ("假包.tar.bz2", lib.tar_gz_bytes()),
                 ("旧文档.doc", lib.pdf_bytes()), ("旧表格.xls", lib.doc_bytes()), ("旧文档2.doc", lib.xls_bytes())]
        for name, data in cases:
            with self.subTest(name):
                body = self.error(self.first_refusal(self.user, name, data), 415, "unsupported_type")
                self.assertIn(".pdf", body["supported"])
        # Nothing of a refused file stays behind: no record, no open upload, no bytes on disk.
        self.assertEqual(self.ok(self.get("/library/materials", self.user))["total"], 0)
        self.assertEqual(self.stored_files(), [])

    def test_formats_lists_extensions_and_limits(self):
        info = self.ok(self.get("/library/formats", self.user))
        self.assertIn(".tar.gz", info["extensions"])
        self.assertIn(".urdf", info["extensions"])
        self.assertEqual({f["kind"] for f in info["formats"]}, {"document", "package"})
        self.assertEqual(info["limits"]["chunk_bytes"], self.chunk_bytes)
        self.assertEqual(info["limits"]["document_bytes"], 200 * 1024 * 1024)
        self.assertEqual(info["limits"]["package_bytes"], 2 * 1024 ** 3)
        self.assertEqual(info["limits"]["org_quota_bytes"], 20 * 1024 ** 3)
