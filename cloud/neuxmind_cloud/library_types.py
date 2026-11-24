"""Material library (issue #33): which files are accepted, decided by what is inside them.

The file name only says what the uploader *claims* the file is: its suffix
selects one entry of :data:`FORMATS`, and a name without a listed suffix is
refused before a single byte is sent. Whether the file is then accepted is
decided by reading it: :func:`check_head` looks at the first chunk as soon as
it arrives (a program renamed to ``.pdf`` is turned away at once) and
:func:`check_file` looks at the complete file before it is stored.

Nothing here extracts an archive, runs a parser on XML, or trusts a size
field of the file: archives are only recognised (magic numbers, a bounded
look at the first 512 decompressed bytes, the ZIP directory with an entry
limit), so a decompression bomb or a hostile document has nothing to act on.
"""
from __future__ import annotations

import bz2
import codecs
import lzma
import mmap
import re
import struct
import threading
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass

DOCUMENT, PACKAGE = "document", "package"
KINDS = (DOCUMENT, PACKAGE)
# Issue #43: a robot's programs and scene data: a tar.gz that is stored and never parsed (library_bundle.py).
BUNDLE = "bundle"
KINDS = (*KINDS, BUNDLE)


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    kind: str
    extensions: tuple[str, ...]


FORMATS = (
    Format("docx", "Word", DOCUMENT, (".docx",)),
    Format("doc", "Word 97–2003", DOCUMENT, (".doc",)),
    Format("xlsx", "Excel", DOCUMENT, (".xlsx",)),
    Format("xls", "Excel 97–2003", DOCUMENT, (".xls",)),
    Format("pdf", "PDF", DOCUMENT, (".pdf",)),
    Format("markdown", "Markdown", DOCUMENT, (".md", ".markdown")),
    Format("text", "纯文本", DOCUMENT, (".txt", ".rst", ".csv", ".tsv", ".log", ".ini", ".cfg", ".conf", ".toml")),
    Format("yaml", "YAML", DOCUMENT, (".yaml", ".yml")),
    Format("json", "JSON", DOCUMENT, (".json",)),
    Format("xml", "XML", DOCUMENT, (".xml",)),
    Format("urdf", "URDF", DOCUMENT, (".urdf", ".xacro")),
    Format("zip", "ZIP 压缩包", PACKAGE, (".zip",)),
    Format("tar.gz", "tar.gz 压缩包", PACKAGE, (".tar.gz", ".tgz")),
    Format("tar.xz", "tar.xz 压缩包", PACKAGE, (".tar.xz", ".txz")),
    Format("tar.bz2", "tar.bz2 压缩包", PACKAGE, (".tar.bz2", ".tbz2")),
    Format("tar", "tar 包", PACKAGE, (".tar",)),
    Format("deb", "deb 安装包", PACKAGE, (".deb",)),
)
BY_ID = {fmt.id: fmt for fmt in FORMATS}
EXTENSIONS = tuple(ext for fmt in FORMATS for ext in fmt.extensions)
# Longest suffix first, so "driver.tar.gz" is a tar.gz and not something ending in ".gz".
_BY_SUFFIX = sorted(((ext, fmt) for fmt in FORMATS for ext in fmt.extensions), key=lambda item: -len(item[0]))
_GROUPS = (("Word", ("docx", "doc")), ("Excel", ("xlsx", "xls")), ("PDF", ("pdf",)),
           ("文本", ("markdown", "text", "yaml", "json", "xml")), ("URDF", ("urdf",)),
           ("驱动安装包", ("zip", "tar.gz", "tar.xz", "tar.bz2", "tar", "deb")))
TEXT_FORMATS = frozenset(("markdown", "text", "yaml", "json", "xml", "urdf"))


def supported_text() -> str:
    """One sentence naming every accepted type, for refusals and for the upload form."""
    parts = []
    for label, ids in _GROUPS:
        extensions = "、".join(ext for fmt_id in ids for ext in BY_ID[fmt_id].extensions)
        parts.append(f"{label}（{extensions}）")
    return "支持的类型：" + "、".join(parts) + "。文本须为 UTF-8、GB 18030 或带 BOM 的 UTF-16 编码。"


class Unsupported(Exception):
    """The bytes are not what the file name says (or not a supported type at all)."""


# ------------------------------------------------------------------ file names
MAX_NAME_CHARS = 150
_SEPARATORS = re.compile(r"[/\\]")
_FORBIDDEN = re.compile(r'[<>:"|?*]')
_SPACES = re.compile(r"\s+")
# Control, format (zero-width, direction-changing), surrogate, private-use and unassigned characters.
UNPRINTABLE = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
_RESERVED = frozenset(("con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))))


def clean_filename(value) -> tuple[str, Format | None]:
    """``(display name, format)`` for an uploaded file name; ``ValueError`` when no name is left.

    The result is the last path component only, without control or invisible
    characters and without characters a file system treats specially, at most
    :data:`MAX_NAME_CHARS` long, with the suffix in lower case. It is stored
    for display and for the download name. Files are never stored under it
    (see ``library_store``), so no file name can address a path on the server.
    """
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("文件名须为 1–1000 个字符的文字")
    text = "".join(ch for ch in unicodedata.normalize("NFC", value) if unicodedata.category(ch) not in UNPRINTABLE)
    base = _SEPARATORS.split(text)[-1].strip()
    stem, extension, fmt = base, "", None
    lowered = base.lower()
    for suffix, candidate in _BY_SUFFIX:
        if lowered.endswith(suffix):
            stem, extension, fmt = base[:-len(suffix)], suffix, candidate
            break
    stem = _SPACES.sub(" ", _FORBIDDEN.sub("_", stem)).strip(" .")
    if not stem:
        raise ValueError("文件名无效：去掉路径和特殊字符后没有剩下名称")
    if stem.lower() in _RESERVED:
        stem = "_" + stem
    stem = stem[:MAX_NAME_CHARS - len(extension)].rstrip(" .")
    return stem + extension, fmt


# ------------------------------------------------------------------ text
_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
# Tab, line ends, form feed, escape (coloured logs) and Ctrl-Z (the end mark of old DOS text files) may appear.
_ALLOWED_CONTROL = b"\t\n\r\x0b\x0c\x1b\x1a"
_FORBIDDEN_CONTROL = bytes(b for b in range(32) if b not in _ALLOWED_CONTROL) + b"\x7f"
_FORBIDDEN_CHARS = re.compile("[" + re.escape(_FORBIDDEN_CONTROL.decode("latin-1")) + "]")
_ROBOT_TAG = re.compile(rb"<robot[\s>]")
_NOT_TEXT = "内容不是 UTF-8、GB 18030 或带 BOM 的 UTF-16 编码的文本"


class TextCheck:
    """Feed a file block by block: it is text when it has no control characters and
    decodes, start to end, as UTF-8, else as GB 18030 — or, when it starts with a
    UTF-16 byte order mark, as UTF-16."""

    def __init__(self):
        self._decoders = None

    def feed(self, block: bytes, final: bool = False) -> None:
        if self._decoders is None:                      # the first block decides between the two families
            self._utf16 = block.startswith(_UTF16_BOMS)
            names = ("utf-16",) if self._utf16 else ("utf-8-sig", "gb18030")
            self._decoders = [codecs.getincrementaldecoder(name)("strict") for name in names]
        if not self._utf16 and len(block.translate(None, _FORBIDDEN_CONTROL)) != len(block):
            raise Unsupported("内容含有二进制字节，不是文本")
        alive = []
        for decoder in self._decoders:
            try:
                text = decoder.decode(block, final)
            except UnicodeDecodeError:
                continue
            if self._utf16 and _FORBIDDEN_CHARS.search(text):
                raise Unsupported("内容含有控制字符，不是文本")
            alive.append(decoder)
        self._decoders = alive
        if not alive:
            raise Unsupported(_NOT_TEXT)


def _as_bytes_of_ascii(head: bytes) -> bytes:
    """``head`` with its markup characters as single bytes, whatever the encoding (for the checks below)."""
    if head.startswith(_UTF16_BOMS):
        return head[:len(head) - len(head) % 2].decode("utf-16", "ignore").encode("utf-8")
    return head.removeprefix(_BOM)


def _text_head(fmt: Format, head: bytes, whole: bool, strict: bool) -> None:
    TextCheck().feed(head, final=whole)
    plain = _as_bytes_of_ascii(head)
    first = plain.lstrip(b" \t\r\n")[:1]
    if fmt.id == "json" and first not in (b"{", b"["):
        raise Unsupported("内容不是 JSON 对象或数组")
    if fmt.id in ("xml", "urdf") and first != b"<":
        raise Unsupported("内容不是 XML")
    # A long licence comment may come first: only what is certainly the whole start decides.
    if fmt.id == "urdf" and (whole or strict) and not _ROBOT_TAG.search(plain):
        raise Unsupported("内容里没有 URDF 的 <robot> 元素")


# ------------------------------------------------------------------ archives
HEAD_BYTES = 64 * 1024
# How much of a file's start is read again when it is complete: one bzip2 block
# (at most 900 kB of input) must fit, or the first 512 bytes cannot be recovered.
FILE_HEAD_BYTES = 4 * 1024 * 1024
_TAR_BLOCK = 512
_XZ_MEMORY_LIMIT = 128 * 1024 * 1024
_XZ_SLOTS = threading.BoundedSemaphore(4)
_GZIP_MAX_MEMBERS = 16
_ZIP_LOCAL, _ZIP_END, _ZIP64_LOCATOR = b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x07"
_ZIP_END_SIZE, _ZIP_MAX_COMMENT = 22, 65535
_OFFICE_MAX_ENTRIES, _OFFICE_MAX_DIRECTORY = 5000, 4 * 1024 * 1024
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_OLE_ENTRY, _OLE_NAME_FIELD = 128, 64
_OFFICE_MEMBER = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}
_OLE_STREAMS = {"doc": ("WordDocument",), "xls": ("Workbook", "Book")}
_DEB_MAGIC, _DEB_FIRST_MEMBER = b"!<arch>\n", b"debian-binary"
_PDF_HEADER_WINDOW = 1024


def _tar_header_ok(block: bytes) -> bool:
    """Is ``block`` (512 bytes) a tar header? Decided by its checksum field, old and new style alike."""
    if len(block) < _TAR_BLOCK:
        return False
    header = block[:_TAR_BLOCK]
    try:
        stored = int(header[148:156].strip(b"\x00 ").decode("ascii"), 8)
    except ValueError:
        return False
    rest = header[:148] + header[156:]
    unsigned = sum(rest) + 8 * 32
    signed = sum(b - 256 if b > 127 else b for b in rest) + 8 * 32
    return stored in (unsigned, signed)


def _gunzip_start(head: bytes) -> bytes:
    """Up to 512 bytes from the start of a gzip stream (a file may be several members put together)."""
    out, rest = b"", head
    for _ in range(_GZIP_MAX_MEMBERS):
        member = zlib.decompressobj(wbits=31)
        out += member.decompress(rest, _TAR_BLOCK - len(out))
        rest = member.unused_data
        if len(out) >= _TAR_BLOCK or not member.eof or not rest:
            break
    return out


def _decompressed_start(fmt: Format, head: bytes) -> bytes | None:
    """The first 512 bytes inside a compressed tar; ``None`` when ``head`` is too short to tell.
    Never more than those 512 bytes are produced, whatever the archive claims to hold."""
    try:
        if fmt.id == "tar.gz":
            out = _gunzip_start(head)
        elif fmt.id == "tar.xz":
            with _XZ_SLOTS:                 # an xz decoder allocates its dictionary: few at a time
                out = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=_XZ_MEMORY_LIMIT).decompress(head, _TAR_BLOCK)
        else:
            out = bz2.BZ2Decompressor().decompress(head, _TAR_BLOCK)
    except (zlib.error, lzma.LZMAError, OSError, EOFError, ValueError):
        raise Unsupported(f"内容不是有效的 {fmt.id} 压缩数据") from None
    return out if len(out) >= _TAR_BLOCK else None


_MAGIC = {"tar.gz": b"\x1f\x8b\x08", "tar.xz": b"\xfd7zXZ\x00", "tar.bz2": b"BZh"}


def check_head(fmt: Format, head: bytes, *, whole: bool, strict: bool = False) -> None:
    """Refuse (``Unsupported``) when the start of the file contradicts ``fmt``.

    ``whole``: ``head`` is the entire file. ``strict``: also refuse when
    ``head`` is too short to decide (used once the file is complete)."""
    if fmt.id in TEXT_FORMATS:
        return _text_head(fmt, head, whole, strict)
    if fmt.id == "pdf":
        if b"%PDF-" not in head[:_PDF_HEADER_WINDOW]:   # readers accept the header anywhere in the first 1024 bytes
            raise Unsupported("内容不是 PDF")
    elif fmt.id in ("docx", "xlsx", "zip"):
        if not head.startswith(_ZIP_LOCAL):
            raise Unsupported("内容不是 ZIP 结构" if fmt.id == "zip" else f"内容不是 {fmt.label} 文档")
    elif fmt.id in ("doc", "xls"):
        if not head.startswith(_OLE_MAGIC):
            raise Unsupported(f"内容不是 {fmt.label} 文档")
    elif fmt.id == "deb":
        if not head.startswith(_DEB_MAGIC) or head[8:24].rstrip(b" /") != _DEB_FIRST_MEMBER:
            raise Unsupported("内容不是 deb 安装包")
    elif fmt.id == "tar":
        if not _tar_header_ok(head):
            raise Unsupported("内容不是 tar 包")
    else:
        if not head.startswith(_MAGIC[fmt.id]):
            raise Unsupported(f"内容不是 {fmt.id} 压缩包")
        start = _decompressed_start(fmt, head)
        if start is None and (strict or whole):
            raise Unsupported(f"{fmt.id} 压缩包里没有 tar 数据")
        if start is not None and not _tar_header_ok(start):
            raise Unsupported(f"{fmt.id} 压缩包里不是 tar 包")


def _zip_tail(path: str, size: int) -> bytes:
    length = min(size, _ZIP_END_SIZE + _ZIP_MAX_COMMENT + 20)
    with open(path, "rb") as handle:
        handle.seek(size - length)
        return handle.read(length)


def _office_members(fmt: Format, path: str, size: int) -> None:
    """A .docx/.xlsx is a ZIP whose directory lists the part every such document has."""
    tail = _zip_tail(path, size)
    end = tail.rfind(_ZIP_END)
    if end < 0 or len(tail) - end < _ZIP_END_SIZE:
        raise Unsupported(f"内容不是 {fmt.label} 文档")
    entries, directory_size = struct.unpack("<HI", tail[end + 10:end + 16])
    # Bounded before zipfile reads the directory: no ZIP64, no directory of millions of entries.
    if _ZIP64_LOCATOR in tail or entries > _OFFICE_MAX_ENTRIES or directory_size > _OFFICE_MAX_DIRECTORY:
        raise Unsupported(f"内容不是常规的 {fmt.label} 文档")
    try:
        with zipfile.ZipFile(path) as archive:
            names = {name.replace("\\", "/") for name in archive.namelist()}   # some writers use backslashes
    except Exception:                       # zipfile raises many types on hostile input
        raise Unsupported(f"内容不是 {fmt.label} 文档") from None
    if "[Content_Types].xml" not in names or _OFFICE_MEMBER[fmt.id] not in names:
        raise Unsupported(f"内容不是 {fmt.label} 文档")


def _ole_has_stream(path: str, names: tuple[str, ...]) -> bool:
    """Does the compound file's directory hold an entry with one of ``names``?

    Directory entries are 128 bytes and sectors are multiples of 512, so an
    entry starts at a multiple of 128; its name is UTF-16 and its length is
    stored after the 64-byte name field."""
    with open(path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as view:
        for name in names:
            raw = name.encode("utf-16-le") + b"\x00\x00"
            length = struct.pack("<H", len(raw))
            position = view.find(raw)
            while position >= 0:
                if position % _OLE_ENTRY == 0 and view[position + _OLE_NAME_FIELD:position + _OLE_NAME_FIELD + 2] == length:
                    return True
                position = view.find(raw, position + 2)
    return False


def check_file(fmt: Format, path: str, size: int) -> None:
    """The decision for a complete file (``Unsupported`` when it is not a ``fmt``)."""
    with open(path, "rb") as handle:
        head = handle.read(min(size, FILE_HEAD_BYTES))
        check_head(fmt, head, whole=size <= len(head), strict=True)
        if fmt.id in TEXT_FORMATS and size > len(head):
            handle.seek(0)
            check = TextCheck()
            remaining = size
            while remaining > 0:
                block = handle.read(min(remaining, 1024 * 1024))
                if not block:
                    raise Unsupported("文件不完整")
                remaining -= len(block)
                check.feed(block, final=remaining == 0)
    if fmt.id in _OFFICE_MEMBER:
        _office_members(fmt, path, size)
    elif fmt.id == "zip":
        if _ZIP_END not in _zip_tail(path, size):
            raise Unsupported("内容不是完整的 ZIP 压缩包")
    elif fmt.id in _OLE_STREAMS:
        if not _ole_has_stream(path, _OLE_STREAMS[fmt.id]):
            raise Unsupported(f"内容不是 {fmt.label} 文档")
