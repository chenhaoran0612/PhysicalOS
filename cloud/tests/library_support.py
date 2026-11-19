"""Synthetic files and upload helpers for the material-library tests (issue #33).

Every file here is built in memory from made-up content. No real vendor
document, URDF or driver package is used anywhere in these tests.
"""
from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma
import os
import tarfile
import zipfile

import support  # noqa: F401
from support import API, CloudTestCase

OCTET = "application/octet-stream"
LABELS = {"body_type": "quadruped", "vendor": "示例厂家", "model": "X1", "version": "1.0"}
ELF = b"\x7fELF\x02\x01\x01\x00" + bytes(56) + b"not a document" * 20


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return out.getvalue()


def docx_bytes(text: str = "示例手册") -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<Relationships/>",
                 "word/document.xml": f"<w:document>{text}</w:document>".encode()})


def xlsx_bytes(text: str = "示例参数") -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<Relationships/>",
                 "xl/workbook.xml": f"<workbook>{text}</workbook>".encode()})


def zip_bytes(payload: bytes = b"driver payload") -> bytes:
    return _zip({"driver/install.txt": payload, "driver/lib/readme.txt": b"synthetic"})


def pdf_bytes(text: str = "example") -> bytes:
    return f"%PDF-1.4\n1 0 obj\n<< /Title ({text}) >>\nendobj\ntrailer\n<< >>\n%%EOF\n".encode()


def ole_bytes(stream: str) -> bytes:
    """A made-up compound file: the magic, then a directory sector naming ``stream``."""
    def entry(name: str) -> bytes:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return raw.ljust(64, b"\x00") + len(raw).to_bytes(2, "little") + bytes(62)
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
    return header + entry("Root Entry") + entry(stream) + bytes(256)


def doc_bytes() -> bytes:
    return ole_bytes("WordDocument")


def xls_bytes() -> bytes:
    return ole_bytes("Workbook")


def urdf_bytes(name: str = "demo") -> bytes:
    return (f'<?xml version="1.0"?>\n<robot name="{name}">\n  <link name="base_link"/>\n'
            f'  <joint name="hip" type="revolute"><parent link="base_link"/></joint>\n</robot>\n').encode()


def text_bytes(size: int = 2000, line: str = "关节 {i}: 最大力矩 12.5 N·m\n") -> bytes:
    out, i = [], 0
    total = 0
    while total < size:
        piece = line.format(i=i).encode()
        out.append(piece)
        total += len(piece)
        i += 1
    return b"".join(out)


def tar_bytes(payload: bytes = b"driver payload") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        info = tarfile.TarInfo("driver/install.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def tar_gz_bytes(payload: bytes = b"driver payload") -> bytes:
    return gzip.compress(tar_bytes(payload), mtime=0)


def tar_xz_bytes(payload: bytes = b"driver payload") -> bytes:
    return lzma.compress(tar_bytes(payload))


def tar_bz2_bytes(payload: bytes = b"driver payload") -> bytes:
    return bz2.compress(tar_bytes(payload))


def _ar_member(name: str, data: bytes) -> bytes:
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return header + data + (b"\n" if len(data) % 2 else b"")


def deb_bytes(payload: bytes = b"driver payload") -> bytes:
    return (b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member("control.tar.gz", tar_gz_bytes(b"control"))
            + _ar_member("data.tar.gz", tar_gz_bytes(payload)))


def random_zip(size: int) -> bytes:
    """A stored (uncompressed) archive of about ``size`` random bytes: several chunks of package."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("driver/blob.bin", os.urandom(size))
    return out.getvalue()


class LibraryCase(CloudTestCase):
    """Cloud app with the library on a temp directory and small chunks.

    The chunk size is above the general body limit of the test settings
    (200 000 bytes), so every multi-chunk upload also shows that chunks have
    their own bound."""

    chunk_bytes = 256 * 1024
    library_overrides: dict = {}

    def make_settings(self, **overrides):
        values = dict(library_dir=f"{self.tmp}/library", library_chunk_bytes=self.chunk_bytes)
        values.update(self.library_overrides)
        values.update(overrides)
        return super().make_settings(**values)

    @property
    def library_dir(self) -> str:
        return f"{self.tmp}/library"

    def stored_files(self) -> list[str]:
        found = []
        for root, _dirs, files in os.walk(self.library_dir):
            found.extend(os.path.relpath(os.path.join(root, name), self.library_dir) for name in files)
        return sorted(found)

    # ------------------------------------------------------------ requests
    def start_raw(self, user, filename: str, data: bytes, **fields):
        body = {"filename": filename, "size": len(data), "sha256": sha(data), **LABELS, **fields}
        return self.post("/library/uploads", body, user)

    def start(self, user, filename: str, data: bytes, expect: int = 201, **fields) -> dict:
        return self.ok(self.start_raw(user, filename, data, **fields), expect)

    def parts(self, upload: dict, data: bytes) -> list[bytes]:
        size = upload["chunk_bytes"]
        return [data[i:i + size] for i in range(0, len(data), size)]

    def put_chunk(self, user, upload_id: str, index: int, data: bytes, *, client=None, headers=None, **extra):
        merged = {**(headers if headers is not None else (user["headers"] if user else {})), "Content-Type": OCTET, **extra}
        return (client or self.client).put(f"{API}/library/uploads/{upload_id}/chunks/{index}", content=data, headers=merged)

    def complete(self, user, upload_id: str):
        return self.post(f"/library/uploads/{upload_id}/complete", {}, user)

    def delete(self, path: str, user=None, **kwargs):
        headers = kwargs.pop("headers", None) or (user["headers"] if user else {})
        return self.client.delete(API + path, headers=headers, **kwargs)

    def first_refusal(self, user, filename: str, data: bytes, **fields):
        """Run the whole upload and return the first response that is not a success."""
        response = self.start_raw(user, filename, data, **fields)
        if response.status_code >= 400:
            return response
        upload = response.json()
        for index, part in enumerate(self.parts(upload, data)):
            response = self.put_chunk(user, upload["id"], index, part)
            if response.status_code >= 400:
                return response
        return self.complete(user, upload["id"])

    def upload(self, user, filename: str, data: bytes, **fields) -> dict:
        """The whole flow; every step must succeed. Returns the stored material."""
        upload = self.start(user, filename, data, **fields)
        for index, part in enumerate(self.parts(upload, data)):
            self.ok(self.put_chunk(user, upload["id"], index, part))
        return self.ok(self.complete(user, upload["id"]), 201)

    def content(self, user, material_id: str, **headers):
        merged = {**(user["headers"] if user else {}), **headers}
        return self.client.get(f"{API}/library/materials/{material_id}/content", headers=merged)

    def member(self, owner: dict, email: str, role: str) -> dict:
        """A second account that is a member of ``owner``'s organisation with ``role``."""
        other = self.register(email)
        self.ok(self.post("/org/members", {"email": email, "role": role}, owner))
        other["headers"] = {**other["headers"], "X-NeuxMind-Org": owner["org_id"]}
        return other
