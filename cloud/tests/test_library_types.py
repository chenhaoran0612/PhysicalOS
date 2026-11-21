"""Issue #33: the type checks and the storage paths, without HTTP. All inputs are made up here."""
import bz2
import gzip
import io
import os
import shutil
import stat
import struct
import tempfile
import unittest
import zipfile
from unittest import mock

import library_support as lib
from neuxmind_cloud import library_types as types
from neuxmind_cloud.library_store import LibraryStore

ORG = "org_" + "a" * 20
UPLOAD = "upl_" + "b" * 20


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nxlib-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def file(self, data: bytes) -> str:
        path = os.path.join(self.tmp, "candidate")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def accepted(self, fmt_id: str, data: bytes) -> bool:
        try:
            types.check_file(types.BY_ID[fmt_id], self.file(data), len(data))
        except types.Unsupported:
            return False
        return True


class NameTest(unittest.TestCase):
    def test_the_longest_known_suffix_decides(self):
        self.assertEqual(types.clean_filename("driver.tar.gz")[1].id, "tar.gz")
        self.assertEqual(types.clean_filename("driver.TAR.GZ"), ("driver.tar.gz", types.BY_ID["tar.gz"]))
        self.assertEqual(types.clean_filename("notes.tar")[1].id, "tar")
        self.assertIsNone(types.clean_filename("driver.tar.gz.exe")[1])
        self.assertIsNone(types.clean_filename("archive.gz")[1])

    def test_invisible_and_direction_changing_characters_are_dropped(self):
        # U+202E would make "gpj.exe" read as "exe.jpg" in a file list.
        self.assertEqual(types.clean_filename("report‮gpj.pdf")[0], "reportgpj.pdf")
        self.assertEqual(types.clean_filename("a​b﻿.pdf")[0], "ab.pdf")

    def test_device_names_are_not_kept_as_they_are(self):
        self.assertEqual(types.clean_filename("CON.pdf")[0], "_CON.pdf")
        self.assertEqual(types.clean_filename("nul.txt")[0], "_nul.txt")

    def test_every_suffix_belongs_to_exactly_one_format(self):
        self.assertEqual(len(types.EXTENSIONS), len(set(types.EXTENSIONS)))
        for extension in types.EXTENSIONS:
            self.assertEqual(extension, extension.lower())
            self.assertTrue(extension.startswith("."))
        for forbidden in (".html", ".htm", ".svg", ".js", ".sh", ".exe", ".py", ".docm", ".xlsm"):
            self.assertNotIn(forbidden, types.EXTENSIONS)

    def test_the_sentence_names_every_suffix(self):
        sentence = types.supported_text()
        for extension in types.EXTENSIONS:
            self.assertIn(extension, sentence)


class ContentTest(TempDirCase):
    def test_text_may_be_cut_anywhere_between_blocks(self):
        data = ("中文说明，每个字三个字节。" * 400_000).encode()            # > FILE_HEAD_BYTES, so it is streamed
        self.assertGreater(len(data), types.FILE_HEAD_BYTES)
        self.assertTrue(self.accepted("text", data))
        self.assertTrue(self.accepted("text", data.decode().encode("gb18030")))

    def test_a_binary_byte_far_into_a_text_file_is_found(self):
        data = bytearray(b"plain line\n" * 500_000)
        self.assertGreater(len(data), types.FILE_HEAD_BYTES)
        data[-5] = 0
        self.assertFalse(self.accepted("text", bytes(data)))
        data[-5] = 0xff
        self.assertFalse(self.accepted("text", bytes(data)))

    def test_a_text_file_must_end_on_a_whole_character(self):
        self.assertTrue(self.accepted("text", "国标编码".encode("gb18030")))
        # Ends in the first byte of a two-byte character: complete in neither encoding.
        self.assertFalse(self.accepted("text", "国标编码".encode("gb18030") + b"\x81"))

    def test_utf8_with_a_byte_order_mark_is_text(self):
        self.assertTrue(self.accepted("json", b"\xef\xbb\xbf{\"a\": 1}"))
        self.assertTrue(self.accepted("urdf", b"\xef\xbb\xbf" + lib.urdf_bytes()))

    def test_an_office_document_with_a_huge_directory_is_refused_before_it_is_read(self):
        many = io.BytesIO()
        with zipfile.ZipFile(many, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<w:document/>")
            for i in range(5001):
                archive.writestr(f"pad/{i}", b"")
        self.assertFalse(self.accepted("docx", many.getvalue()))
        self.assertTrue(self.accepted("zip", many.getvalue()))            # as a plain package it is only recognised

    def test_an_office_document_claiming_zip64_is_refused(self):
        data = lib.docx_bytes()
        end = data.rfind(b"PK\x05\x06")
        locator = b"PK\x06\x07" + struct.pack("<IQI", 0, 0, 1)
        self.assertTrue(self.accepted("docx", data))
        self.assertFalse(self.accepted("docx", data[:end] + locator + data[end:]))

    def test_a_truncated_archive_is_refused(self):
        self.assertFalse(self.accepted("zip", lib.zip_bytes()[:-30]))
        self.assertFalse(self.accepted("docx", lib.docx_bytes()[:-30]))
        self.assertFalse(self.accepted("tar.gz", lib.tar_gz_bytes()[:20]))

    def test_a_compressed_tar_is_judged_by_its_first_512_bytes_only(self):
        # 200 MB of zeros compress to a few hundred kB; the check must not inflate them.
        bomb = bz2.compress(lib.tar_bytes(b"x") + bytes(200 * 1024 * 1024))
        self.assertTrue(self.accepted("tar.bz2", bomb))
        self.assertTrue(self.accepted("tar.gz", lib.tar_gz_bytes(os.urandom(300_000))))
        self.assertTrue(self.accepted("tar.xz", lib.tar_xz_bytes(os.urandom(300_000))))

    def test_the_first_chunk_alone_never_condemns_a_valid_file(self):
        data = lib.tar_bz2_bytes(os.urandom(600_000))                     # the first bzip2 block is larger than the head
        head = data[:64 * 1024]
        types.check_head(types.BY_ID["tar.bz2"], head, whole=False)       # undecided, not refused
        with self.assertRaises(types.Unsupported):
            types.check_head(types.BY_ID["tar.bz2"], head, whole=False, strict=True)
        self.assertTrue(self.accepted("tar.bz2", data))

    def test_the_decompressor_is_never_asked_for_more_than_one_tar_header(self):
        asked, real = [], bz2.BZ2Decompressor

        class Recording:
            def __init__(self):
                self.inner = real()

            def decompress(self, data, max_length=-1):
                asked.append(max_length)
                return self.inner.decompress(data, max_length)
        bomb = bz2.compress(lib.tar_bytes(b"x") + bytes(50 * 1024 * 1024))
        with mock.patch.object(types.bz2, "BZ2Decompressor", Recording):
            self.assertTrue(self.accepted("tar.bz2", bomb))
        self.assertTrue(asked)
        self.assertTrue(all(0 < n <= 512 for n in asked), asked)

    def test_a_gzip_file_made_of_several_members_is_read_across_them(self):
        tar = lib.tar_bytes(os.urandom(2000))
        joined = gzip.compress(tar[:100], mtime=0) + gzip.compress(tar[100:], mtime=0)
        self.assertTrue(self.accepted("tar.gz", joined))
        self.assertFalse(self.accepted("tar.gz", gzip.compress(b"short", mtime=0) * 40))   # many members, no tar

    def test_text_in_utf16_with_a_byte_order_mark_is_text(self):
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            mark = b"" if encoding == "utf-16" else {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}[encoding]
            with self.subTest(encoding):
                self.assertTrue(self.accepted("text", mark + "记事本另存的说明\r\n".encode(encoding)))
        self.assertTrue(self.accepted("urdf", lib.urdf_bytes().decode().encode("utf-16")))
        self.assertTrue(self.accepted("json", '{"a": 1}'.encode("utf-16")))
        self.assertFalse(self.accepted("json", "<a/>".encode("utf-16")))
        self.assertFalse(self.accepted("text", "带一个空字符\x00的文字".encode("utf-16")))
        self.assertFalse(self.accepted("text", "没有标记的 UTF-16".encode("utf-16-le")))
        long = ("很长的一份说明。" * 600_000).encode("utf-16")               # streamed, cut between blocks
        self.assertGreater(len(long), types.FILE_HEAD_BYTES)
        self.assertTrue(self.accepted("text", long))

    def test_a_dos_end_of_file_mark_is_allowed_in_text(self):
        self.assertTrue(self.accepted("text", b"old batch notes\r\n\x1a"))

    def test_a_pdf_may_have_a_few_bytes_before_its_header(self):
        self.assertTrue(self.accepted("pdf", b"\x00\x01junk from a scanner\n" + lib.pdf_bytes()))
        self.assertFalse(self.accepted("pdf", b"x" * 2000 + lib.pdf_bytes()))

    def test_an_office_document_whose_member_names_use_backslashes_is_accepted(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("xl\\workbook.xml", b"<workbook/>")
        self.assertTrue(self.accepted("xlsx", out.getvalue()))

    def test_a_urdf_with_a_long_comment_before_the_robot_element(self):
        data = b"<?xml version=\"1.0\"?>\n<!-- " + b"licence text " * 6000 + b"-->\n<robot name=\"demo\"/>\n"
        self.assertGreater(data.index(b"<robot"), 64 * 1024)
        types.check_head(types.BY_ID["urdf"], data[:64 * 1024], whole=False)      # the first chunk alone: undecided
        self.assertTrue(self.accepted("urdf", data))
        self.assertFalse(self.accepted("urdf", data.replace(b"<robot", b"<other")))

    def test_an_old_office_file_needs_its_stream_in_the_directory(self):
        self.assertTrue(self.accepted("doc", lib.doc_bytes()))
        self.assertTrue(self.accepted("xls", lib.ole_bytes("Book")))
        # The words alone, somewhere in the file, are not a directory entry.
        loose = lib.ole_bytes("Other") + b"\x00" * 7 + "WordDocument".encode("utf-16-le") + b"\x00\x00" + bytes(200)
        self.assertFalse(self.accepted("doc", loose))


class StoreTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = LibraryStore(os.path.join(self.tmp, "library"))

    def test_paths_are_built_only_from_checked_ids(self):
        digest = lib.sha(b"x")
        self.assertEqual(self.store.object_path(ORG, digest),
                         os.path.join(self.tmp, "library", "objects", ORG, digest[:2], digest))
        for org, sha in ((ORG, "../../etc/passwd"), ("../" + ORG, digest), (ORG, digest.upper()), (ORG + "/x", digest),
                         ("", digest), (ORG, digest + "\x00"), (None, digest)):
            with self.subTest(org=org, sha=sha), self.assertRaises(ValueError):
                self.store.object_path(org, sha)
        for upload in ("../x", "upl_" + "b" * 19, UPLOAD + "/..", "", UPLOAD.upper()):
            with self.subTest(upload=upload), self.assertRaises(ValueError):
                with self.store.staging(upload, exclusive=False, create=True):
                    pass
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))

    def test_staged_and_stored_files_are_private_and_never_executable(self):
        data = b"content"
        with self.store.staging(UPLOAD, exclusive=True, create=True) as staging:
            staging.write(0, data)
            self.assertEqual(stat.S_IMODE(os.stat(staging.path).st_mode), 0o600)
            self.assertEqual((staging.size(), staging.sha256()), (len(data), lib.sha(data)))
            target = self.store.promote(staging, ORG, lib.sha(data))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o440)
        for directory in ("", "staging", "objects", f"objects/{ORG}"):
            mode = stat.S_IMODE(os.stat(os.path.join(self.store.root, directory)).st_mode)
            self.assertEqual(mode, 0o700, directory)
        self.assertEqual(self.store.object_size(ORG, lib.sha(data)), len(data))
        self.store.delete_object(ORG, lib.sha(data))
        self.assertIsNone(self.store.object_size(ORG, lib.sha(data)))

    def test_a_link_planted_in_the_staging_directory_is_not_followed(self):
        os.makedirs(os.path.join(self.store.root, "staging"))
        victim = self.file(b"keep me")
        os.symlink(victim, os.path.join(self.store.root, "staging", UPLOAD + ".part"))
        with self.assertRaises(OSError):
            with self.store.staging(UPLOAD, exclusive=False, create=True) as staging:
                staging.write(0, b"overwritten")
        with open(victim, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")

    def test_storing_the_same_content_again_keeps_the_first_file(self):
        data = b"same content"
        for upload in (UPLOAD, "upl_" + "c" * 20):
            with self.store.staging(upload, exclusive=True, create=True) as staging:
                staging.write(0, data)
                self.store.promote(staging, ORG, lib.sha(data))
        self.assertEqual(os.listdir(os.path.join(self.store.root, "staging")), [])
        with open(self.store.object_path(ORG, lib.sha(data)), "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_old_staging_files_are_listed_for_clean_up(self):
        with self.store.staging(UPLOAD, exclusive=False, create=True) as staging:
            staging.write(0, b"x")
        with open(os.path.join(self.store.root, "staging", "stranger.txt"), "wb"):
            pass
        self.assertEqual(self.store.stale_staging(3600), [])
        self.assertEqual(self.store.stale_staging(-1), [UPLOAD])


if __name__ == "__main__":
    unittest.main()
