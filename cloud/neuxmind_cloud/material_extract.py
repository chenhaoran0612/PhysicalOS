"""The rules part of parsing a material (issue #34), run as its own process with hard limits.

This file is executed by ``material_sandbox`` as a script, ``python -I -S
material_extract.py <mode> <limits>``, with the material's file as standard
input and the result as one JSON object on standard output. It imports nothing
but the standard library (``-I -S``: no site packages, no script directory), so
nothing an attacker could plant next to it is loaded.

The input is untrusted. What the process does about that:

* Before reading a byte it caps its own CPU time, address space (Linux), file
  size (it writes no file at all) and open files. The parent adds a wall-clock
  timeout and a cap on the output, and kills the whole process group when either
  is exceeded. A crash, a hang or an exhausted limit fails this one material.
* Nothing is extracted to disk and nothing is executed. Archive member names
  are only looked at: names that would leave the archive (``../``, absolute
  paths, links pointing outside) are reported, never followed.
* Decompression is bounded: every decompressed byte counts against
  ``unpacked``, xz decoders have a memory limit, and member data is read only
  where it is needed (a metadata file of at most 1 MiB, the first 20 bytes of a
  program to see its machine type) and skipped otherwise.
* XML (URDF, ``package.xml``, Office relationship parts) is read with expat and
  any DOCTYPE is refused outright: no entity can be declared, so neither
  external entities nor entity expansion exist. Depth and element counts are
  capped as well.
* The tar reader is written out here instead of using ``tarfile``, so that a
  header claiming gigabytes of extended attributes is refused instead of read.
"""
from __future__ import annotations

import bz2
import io
import json
import lzma
import os
import posixpath
import re
import resource
import struct
import subprocess
import sys
import zipfile
import zlib
from xml.parsers import expat

BLOCK = 64 * 1024
META_BYTES = 1024 * 1024
ELF_PROBES = 300
EXAMPLES = 5
CONTROL_FIELDS = ("Package", "Version", "Architecture", "Description", "Homepage", "Depends", "Source")
ELF_MACHINES = {0x03: "i386", 0x28: "armhf", 0x3E: "amd64", 0xB7: "arm64", 0xF3: "riscv64", 0x08: "mips",
                0x15: "ppc64el", 0x16: "s390x"}
XZ_MEMORY = 128 * 1024 * 1024
CONTROL_UNPACKED = 32 * 1024 * 1024
CONTROL_PACKED = 64 * 1024 * 1024


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------------ limits
def apply_limits(limits: dict) -> None:
    """Cap this process before it reads anything. Address space is only enforced on Linux."""
    cpu = int(limits.get("cpu", 30))
    caps = [(resource.RLIMIT_CPU, (cpu, cpu + 5)), (resource.RLIMIT_FSIZE, (0, 0)), (resource.RLIMIT_CORE, (0, 0)),
            (resource.RLIMIT_NOFILE, (64, 64))]
    if sys.platform.startswith("linux"):
        memory = int(limits.get("memory", 512 * 1024 * 1024))
        caps.append((resource.RLIMIT_AS, (memory, memory)))
    for which, value in caps:
        try:
            resource.setrlimit(which, value)
        except (ValueError, OSError):
            pass                            # a hard limit already lower than ours stays as it is


def _shown(name: str) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in name)[:200]


# ------------------------------------------------------------------ safe XML
class SafeXml:
    """expat with every DOCTYPE refused, and depth and element counts capped."""

    def __init__(self, limits: dict, on_start, on_end=None, on_text=None):
        self.parser = expat.ParserCreate(encoding="UTF-8")
        self.parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        self.parser.StartDoctypeDeclHandler = self._doctype
        self.parser.EntityDeclHandler = self._doctype
        self.parser.ExternalEntityRefHandler = self._external
        self.parser.StartElementHandler = self._start
        self.parser.EndElementHandler = self._end
        if on_text:
            self.parser.CharacterDataHandler = on_text
        self.max_depth, self.max_elements = int(limits.get("depth", 64)), int(limits.get("elements", 200_000))
        self.depth = self.count = 0
        self.on_start, self.on_end = on_start, on_end

    @staticmethod
    def _doctype(*_args):
        raise Refused("xml_doctype", "资料里有 DOCTYPE 声明（这种文件用不到它，它可以被用来读取服务器上的文件或耗尽内存），出于安全没有解析")

    @staticmethod
    def _external(*_args):
        raise Refused("xml_doctype", "资料引用了外部实体，出于安全没有解析")

    def _start(self, name, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > self.max_depth:
            raise Refused("xml_too_deep", f"XML 嵌套超过 {self.max_depth} 层，没有解析")
        if self.count > self.max_elements:
            raise Refused("xml_too_large", f"XML 元素超过 {self.max_elements} 个，没有解析")
        self.on_start(name, attrs)

    def _end(self, name):
        self.depth -= 1
        if self.on_end:
            self.on_end(name)

    def parse(self, data: bytes) -> None:
        try:
            self.parser.Parse(_xml_utf8(data), True)
        except expat.ExpatError as exc:
            raise Refused("xml_invalid", f"XML 格式不对（第 {exc.lineno} 行）：{expat.ErrorString(exc.code)}") from None


def _xml_utf8(data: bytes) -> bytes:
    """The document as UTF-8 (the parser is told so; the declaration's own encoding is then ignored)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", "replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError:
                raise Refused("xml_encoding", "XML 不是 UTF-8、GB 18030 或 UTF-16 编码") from None
    return text.encode("utf-8", "replace")


# ------------------------------------------------------------------ URDF
def parse_urdf(data: bytes, limits: dict) -> dict:
    robot, links, joints, warnings = {"name": None}, [], [], []
    stack: list[str] = []
    current: dict | None = None
    includes = 0

    def start(tag, attrs):
        nonlocal current, includes
        parent = stack[-1] if stack else None
        if parent is None:
            if tag != "robot":
                raise Refused("urdf_root", f"根元素是 <{_shown(tag)}>，不是 URDF 的 <robot>")
            robot["name"] = attrs.get("name")
        skipped = any(t in ("transmission", "gazebo") for t in stack)
        if tag == "xacro:include":
            includes += 1
        elif tag == "link" and parent in ("robot", "xacro:macro") and attrs.get("name"):
            links.append(attrs["name"])
        elif tag == "joint" and "type" in attrs and not skipped and current is None:
            current = {"name": attrs.get("name") or "", "type": attrs.get("type", ""), "parent": "", "child": "",
                       "raw": {}}
        elif current is not None and parent == "joint" and tag in ("parent", "child"):
            current[tag] = attrs.get("link", "")
        elif current is not None and parent == "joint" and tag == "limit":
            for key in ("lower", "upper", "velocity", "effort"):
                if key in attrs:
                    current[key] = attrs[key]
                    current["raw"][key] = attrs[key]
        stack.append(tag)

    def end(tag):
        nonlocal current
        stack.pop()
        if tag == "joint" and current is not None and "joint" not in stack:
            joints.append(current)
            current = None

    SafeXml(limits, start, end).parse(data)
    if includes:
        warnings.append(f"这份 URDF 用 xacro:include 引用了 {includes} 个别的文件，没有去读；那些文件里的关节和连杆不在下面")
    expressions = sum(1 for j in joints for v in j["raw"].values() if "${" in v or "$(" in v)
    if expressions:
        warnings.append(f"有 {expressions} 个上限写的是 xacro 表达式（${{…}}），没有求值，按没填处理；请手工填数值")
    for joint in joints:
        if not joint["raw"]:
            joint.pop("raw")
    return {"robot_name": robot["name"], "links": links, "joints": joints, "warnings": warnings}


# ------------------------------------------------------------------ names inside archives
def unsafe_name(name: str) -> bool:
    """Would extracting a member of this name write outside the target directory?"""
    clean = name.replace("\\", "/")
    if "\x00" in clean or clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        return True
    return ".." in clean.split("/")


def link_escapes(name: str, target: str, symbolic: bool) -> bool:
    if unsafe_name(target) and (not symbolic or target.replace("\\", "/").startswith("/")):
        return True
    if not symbolic:
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(name.replace("\\", "/")), target.replace("\\", "/")))
    return joined == ".." or joined.startswith("../") or joined.startswith("/")


def elf_machine(head: bytes) -> str | None:
    if len(head) < 20 or not head.startswith(b"\x7fELF"):
        return None
    order = "<" if head[5] == 1 else ">"
    return ELF_MACHINES.get(struct.unpack(order + "H", head[18:20])[0], "other")


class Members:
    """What the names and kinds of an archive's members say, without extracting anything."""

    def __init__(self, limits: dict):
        self.limits = limits
        self.max_members = int(limits.get("members", 100_000))
        self.count = 0
        self.unsafe, self.links_out, self.problems, self.nested = [], [], [], []
        self.devices = self.bombs = self.encrypted = self.probes = 0
        self.machines: dict[str, int] = {}
        self.metadata: dict = {}

    def add(self, name: str) -> None:
        self.count += 1
        if self.count > self.max_members:
            raise Refused("too_many_members", f"包里的成员超过 {self.max_members} 个，没有解析完")
        if unsafe_name(name) and len(self.unsafe) < 1000:
            self.unsafe.append(_shown(name))

    def link(self, name: str, target: str, symbolic: bool) -> None:
        if link_escapes(name, target, symbolic) and len(self.links_out) < 1000:
            self.links_out.append(f"{_shown(name)} → {_shown(target)}")

    def wants_probe(self, name: str, size: int) -> bool:
        return self.probes < ELF_PROBES and size >= 64 and not unsafe_name(name)

    def probe(self, head: bytes) -> None:
        self.probes += 1
        machine = elf_machine(head)
        if machine:
            self.machines[machine] = self.machines.get(machine, 0) + 1

    def wants_metadata(self, name: str, size: int) -> str | None:
        if unsafe_name(name):
            return None
        parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
        if not parts or size > META_BYTES:
            return None
        if parts[-1] == "package.xml" and len(parts) <= 4 and "package.xml" not in self.metadata:
            return "package.xml"
        if parts[-1] == "control" and len(parts) >= 2 and parts[-2] == "DEBIAN" and "control" not in self.metadata:
            return "control"
        if parts[-1].endswith(".deb") and len(self.nested) < 20:
            self.nested.append(_shown("/".join(parts)))
        return None

    def read_metadata(self, which: str, data: bytes) -> None:
        try:
            self.metadata[which] = control_fields(data) if which == "control" else package_xml(data, self.limits)
        except Refused as exc:
            self.problems.append(f"{which}：{exc.message}")

    def facts(self) -> dict:
        warnings = []
        if self.unsafe:
            warnings.append(f"包里有 {len(self.unsafe)} 个成员的名字指向包外或是绝对路径（例如 {'、'.join(self.unsafe[:EXAMPLES])}）。"
                            "解析时只看了名字，没有解开；装到设备上时必须拒绝这样的包或这些成员")
        if self.links_out:
            warnings.append(f"包里有 {len(self.links_out)} 个链接指向包外（例如 {'、'.join(self.links_out[:EXAMPLES])}）")
        if self.devices:
            warnings.append(f"包里有 {self.devices} 个设备文件或管道")
        if self.bombs:
            warnings.append(f"包里有 {self.bombs} 个成员的压缩比异常高（解开后可能大得多），没有解开")
        if self.encrypted:
            warnings.append(f"包里有 {self.encrypted} 个加了密的成员，没有读")
        warnings.extend(f"读包里的说明文件时出错：{problem}" for problem in self.problems[:EXAMPLES])
        if self.nested:
            warnings.append(f"包里还有 deb 安装包：{'、'.join(self.nested[:EXAMPLES])}（没有打开）")
        return {"members": self.count, "unsafe_names": self.unsafe[:EXAMPLES], "unsafe_count": len(self.unsafe),
                "links_out": len(self.links_out), "machines": self.machines, "metadata": self.metadata,
                "warnings": warnings}


def control_fields(data: bytes) -> dict:
    """The fields of a Debian control file that say what the package is (first paragraph only)."""
    fields, last = {}, None
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            if fields:
                break
            continue
        if line[0] in " \t":
            if last and last != "Description":
                fields[last] = (fields[last] + " " + line.strip())[:2000]
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        last = key if sep and key in CONTROL_FIELDS and key not in fields else None
        if last:
            fields[last] = value.strip()[:2000]
    return fields


def package_xml(data: bytes, limits: dict) -> dict:
    """``name``, ``version`` and ``description`` of a ROS package manifest."""
    stack, texts = [], {"name": [], "version": [], "description": []}

    def start(tag, _attrs):
        stack.append(tag)

    def end(_tag):
        stack.pop()

    def text(value):
        if len(stack) == 2 and stack[0] == "package" and stack[1] in texts and sum(map(len, texts[stack[1]])) < 2000:
            texts[stack[1]].append(value)

    SafeXml(limits, start, end, text).parse(data)
    return {key: " ".join("".join(parts).split())[:500] for key, parts in texts.items() if "".join(parts).strip()}


# ------------------------------------------------------------------ bounded decompression
class Unpacked:
    """Sequential reader of a stream that may be gzip, xz or bzip2 compressed (several streams one
    after the other included). Every byte it produces counts against ``cap``."""

    def __init__(self, raw, kind: str, cap: int, xz_memory: int = XZ_MEMORY):
        self.raw, self.kind, self.cap, self.xz_memory = raw, kind, cap, xz_memory
        self.total, self.buffer, self.pending = 0, b"", b""
        self.decoder = self._new_decoder()

    def _new_decoder(self):
        if self.kind == "gz":
            return zlib.decompressobj(wbits=31)
        if self.kind == "xz":
            return lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=self.xz_memory)
        if self.kind == "bz2":
            return bz2.BZ2Decompressor()
        return None

    def _input(self) -> bytes:
        """What to give the decoder next: its own leftover, or fresh input."""
        d = self.decoder
        if self.kind == "gz" and d.unconsumed_tail:
            return d.unconsumed_tail
        if self.kind in ("xz", "bz2") and not d.needs_input:
            return b""
        source, self.pending = self.pending or self.raw.read(BLOCK), b""
        if not source:
            raise Refused("archive_truncated", "压缩数据不完整")
        return source

    def _next_piece(self) -> bytes:
        if self.decoder is None:
            return self.raw.read(BLOCK)
        while True:
            if self.decoder.eof:
                rest = self.decoder.unused_data or self.raw.read(BLOCK)
                if not rest:
                    return b""
                self.decoder, self.pending = self._new_decoder(), rest     # a further stream follows
            try:
                out = self.decoder.decompress(self._input(), BLOCK)
            except lzma.LZMAError as exc:
                if "memory" in str(exc).lower():
                    raise Refused("unpack_memory", "解压这个包需要的内存超过上限，没有解析") from None
                raise Refused("archive_invalid", "压缩数据损坏") from None
            except (zlib.error, OSError, EOFError, ValueError):
                raise Refused("archive_invalid", "压缩数据损坏") from None
            if out:
                return out

    def read(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            if not self.buffer:
                self.buffer = self._next_piece()
                if not self.buffer:
                    break
                self.total += len(self.buffer)
                if self.total > self.cap:
                    raise Refused("unpack_limit", f"解开后超过 {self.cap} 字节（可能是解压炸弹），没有解析完")
            take = size - len(out)
            out += self.buffer[:take]
            self.buffer = self.buffer[take:]
        return bytes(out)

    def skip(self, count: int) -> None:
        while count > 0:
            piece = self.read(min(count, BLOCK))
            if not piece:
                raise Refused("archive_truncated", "包不完整：成员的数据比头部写的短")
            count -= len(piece)


# ------------------------------------------------------------------ tar
TAR_BLOCK = 512


def _tar_number(field: bytes) -> int:
    if field and field[0] & 0x80:                       # base-256 (GNU) for large values
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.split(b"\x00", 1)[0].strip(b" \x00")
    if not text:
        return 0
    try:
        return int(text, 8)
    except ValueError:
        raise Refused("archive_invalid", "tar 头部的数字无效") from None


def _tar_checksum_ok(header: bytes) -> bool:
    try:
        value = _tar_number(header[148:156])
    except Refused:
        return False
    rest = header[:148] + b" " * 8 + header[156:]
    return value in (sum(rest), sum(b - 256 if b > 127 else b for b in rest))


def _pax_records(data: bytes) -> dict:
    out, rest = {}, data
    while rest:
        length, sep, _ = rest.partition(b" ")
        try:
            size = int(length)
        except ValueError:
            break
        if not sep or size <= len(length) + 1 or size > len(rest):
            break
        record, rest = rest[:size], rest[size:]
        key, eq, value = record[len(length) + 1:-1].partition(b"=")
        if eq:
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _tar_text(field: bytes) -> str:
    return field.split(b"\x00", 1)[0].decode("utf-8", "replace")


def scan_tar(stream: Unpacked, members: Members) -> None:
    extended: dict = {}
    empty = 0
    while True:
        header = stream.read(TAR_BLOCK)
        if len(header) < TAR_BLOCK:
            if members.count == 0:
                raise Refused("archive_invalid", "不是 tar 包")
            return                           # ended without the two empty blocks: what was read stands
        if header == b"\x00" * TAR_BLOCK:
            empty += 1
            if empty == 2:
                return
            continue
        empty = 0
        if not _tar_checksum_ok(header):
            raise Refused("archive_invalid", "tar 头部的校验和不对，包已损坏")
        kind = header[156:157]
        size = _tar_number(header[124:136])
        if "size" in extended:
            try:
                size = int(extended["size"])
            except ValueError:
                raise Refused("archive_invalid", "tar 扩展头里的大小无效") from None
        if size < 0:
            raise Refused("archive_invalid", "tar 头部的大小无效")
        padded = size + (-size % TAR_BLOCK)
        if kind in (b"x", b"g", b"L", b"K"):
            if size > META_BYTES:
                raise Refused("archive_invalid", "tar 包里有超过 1 MiB 的扩展头，出于安全没有读")
            data = stream.read(padded)[:size]
            if kind == b"x":
                extended.update(_pax_records(data))
            elif kind == b"L":
                extended["path"] = _tar_text(data)
            elif kind == b"K":
                extended["linkpath"] = _tar_text(data)
            continue
        name = _tar_text(header[0:100])
        if header[257:262] == b"ustar" and _tar_text(header[345:500]):
            name = _tar_text(header[345:500]) + "/" + name
        name = extended.get("path") or name
        target = extended.get("linkpath") or _tar_text(header[157:257])
        extended = {}
        members.add(name)
        if kind in (b"1", b"2"):
            members.link(name, target, symbolic=kind == b"2")
            stream.skip(padded)
            continue
        if kind in (b"3", b"4", b"6"):
            members.devices += 1
        consumed = 0
        if kind in (b"0", b"\x00", b"7"):
            wanted = members.wants_metadata(name, size)
            if wanted:
                data = stream.read(size)
                consumed = len(data)
                members.read_metadata(wanted, data)
            elif members.wants_probe(name, size):
                head = stream.read(20)
                consumed = len(head)
                members.probe(head)
        stream.skip(padded - consumed)


# ------------------------------------------------------------------ zip
_EOCD, _ZIP64_LOCATOR, _ZIP64_EOCD = b"PK\x05\x06", b"PK\x06\x07", b"PK\x06\x06"
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, OSError, EOFError, ValueError, struct.error,
               NotImplementedError, RuntimeError, KeyError)


def zip_bounds(handle, size: int, max_members: int) -> None:
    """Refuse a ZIP whose directory is too large before ``zipfile`` reads it."""
    length = min(size, 22 + 65535 + 20)
    handle.seek(size - length)
    tail = handle.read(length)
    end = tail.rfind(_EOCD)
    if end < 0 or len(tail) - end < 22:
        raise Refused("archive_invalid", "不是完整的 ZIP 包")
    entries, directory = struct.unpack("<HI", tail[end + 10:end + 16])
    locator = tail.rfind(_ZIP64_LOCATOR, 0, end)
    if locator >= 0 and end - locator >= 20:
        offset = struct.unpack("<Q", tail[locator + 8:locator + 16])[0]
        if offset + 56 > size:
            raise Refused("archive_invalid", "ZIP64 目录的位置无效")
        handle.seek(offset)
        record = handle.read(56)
        if not record.startswith(_ZIP64_EOCD):
            raise Refused("archive_invalid", "ZIP64 目录无效")
        entries, directory = struct.unpack("<QQ", record[32:48])
    if entries > max_members or directory > 64 * 1024 * 1024:
        raise Refused("too_many_members", f"ZIP 包的目录过大（{entries} 个成员），没有解析")


def _open_zip(handle, size: int, max_members: int, what: str) -> tuple[zipfile.ZipFile, list]:
    zip_bounds(handle, size, max_members)
    handle.seek(0)
    try:
        archive = zipfile.ZipFile(handle)
        return archive, archive.infolist()
    except _ZIP_ERRORS:
        raise Refused("archive_invalid", f"{what}的 ZIP 结构已损坏") from None


def _read_member(archive, info, cap: int) -> bytes:
    with archive.open(info) as member:
        return member.read(cap + 1)[:cap]


def scan_zip(handle, size: int, members: Members) -> None:
    archive, infos = _open_zip(handle, size, members.max_members, "包")
    for info in infos:
        members.add(info.filename)
        try:
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                target = _read_member(archive, info, 4096).decode("utf-8", "replace") if not info.flag_bits & 1 else ""
                members.link(info.filename, target, symbolic=True)
                continue
            if info.flag_bits & 0x1:
                members.encrypted += 1
                continue
            if info.compress_size and info.file_size > 64 * 1024 * 1024 and info.file_size / info.compress_size > 200:
                members.bombs += 1
                continue
            wanted = members.wants_metadata(info.filename, info.file_size)
            if wanted:
                members.read_metadata(wanted, _read_member(archive, info, META_BYTES))
            elif members.wants_probe(info.filename, info.file_size):
                members.probe(_read_member(archive, info, 20))
        except _ZIP_ERRORS:
            members.problems.append(f"{_shown(info.filename)}：读不出来")


# ------------------------------------------------------------------ deb (ar)
def _ar_members(handle):
    if handle.read(8) != b"!<arch>\n":
        raise Refused("archive_invalid", "不是 deb 安装包")
    for _ in range(16):
        header = handle.read(60)
        if len(header) < 60:
            return
        if header[58:60] != b"`\n":
            raise Refused("archive_invalid", "deb 包的成员头部无效")
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            raise Refused("archive_invalid", "deb 包的成员大小无效") from None
        start = handle.tell()
        yield name, size
        handle.seek(start + size + (size % 2))


def _control_with_dpkg(name: str, limits: dict) -> dict:
    """A control archive this Python cannot decompress (zstd): dpkg-deb writes it out uncompressed (under
    the same limits; ``--ctrl-tarfile`` needs no temporary files), and the tar reader here reads it."""
    try:
        process = subprocess.Popen(["dpkg-deb", "--ctrl-tarfile", "/dev/stdin"], stdin=0, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
    except OSError:
        raise Refused("deb_zstd", f"{name} 用 zstd 压缩，这台云端没有能读它的工具（dpkg-deb）；请手工填写名称、版本和架构") from None
    data = b""
    try:
        data = process.stdout.read(CONTROL_UNPACKED + 1)
    finally:
        process.stdout.close()                 # a writer still going gets SIGPIPE
        if len(data) > CONTROL_UNPACKED and process.poll() is None:
            process.kill()
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = -1
    if len(data) > CONTROL_UNPACKED:
        raise Refused("unpack_limit", "deb 包的 control 部分解开后大得不正常，没有解析")
    if code != 0 or not data:
        raise Refused("archive_invalid", "dpkg-deb 读不出这个包的 control 部分")
    return _control_in_tar(Unpacked(io.BytesIO(data), "", CONTROL_UNPACKED), limits)


def _control_in_tar(stream: Unpacked, limits: dict) -> dict:
    """The ``./control`` file of a deb's control archive."""
    inner = Members(limits)
    inner.wants_metadata = lambda name, size: (
        "control" if name.replace("\\", "/").lstrip("./") == "control" and size <= META_BYTES
        and "control" not in inner.metadata else None)
    scan_tar(stream, inner)
    return inner.metadata.get("control") or {}


def parse_deb(handle, limits: dict) -> dict:
    count = 0
    for name, size in _ar_members(handle):
        count += 1
        if not name.startswith("control.tar"):
            continue
        suffix = name[len("control.tar"):].lstrip(".")
        if suffix == "zst":
            return {"control": _control_with_dpkg(name, limits), "members": count}
        if suffix not in ("", "gz", "xz", "bz2"):
            raise Refused("archive_invalid", f"不认识 deb 包里的 {_shown(name)}")
        if size > CONTROL_PACKED:
            raise Refused("archive_invalid", "deb 包的 control 部分大得不正常，没有解析")
        stream = Unpacked(io.BytesIO(handle.read(size)), suffix, CONTROL_UNPACKED)
        return {"control": _control_in_tar(stream, limits), "members": count}
    raise Refused("archive_invalid", "deb 包里没有 control 部分")


# ------------------------------------------------------------------ Office documents
_VBA_STREAMS = ("_VBA_PROJECT", "Macros", "VBA")
MAX_RELATIONSHIP_PARTS = 300
# Old binary Office files: what fetches content from elsewhere when the file is opened or converted.
# Word keeps a field's code right after the field-begin mark 0x13, so only text there counts (the same
# words in a manual's own text do not); a HYPERLINK field only shows a link. Excel stores the newer
# functions under their "_xlfn." names. Field names are case-insensitive; both 8-bit and UTF-16 text.
_WORD_FIELDS = ("INCLUDEPICTURE", "INCLUDETEXT", "DDEAUTO", "DDE", "LINK", "IMPORT")
_EXCEL_FUNCTIONS = ("_xlfn.WEBSERVICE", "_xlfn.FILTERXML")


def _utf16(pattern: str) -> bytes:
    """A bytes regex matching ``pattern``'s characters as UTF-16LE (letters case-insensitive, spaces as any run)."""
    out = []
    for ch in pattern:
        if ch == " ":
            out.append(rb"(?:[ \t]\x00)+")
        elif ch.isalpha():
            out.append(b"[" + ch.upper().encode() + ch.lower().encode() + rb"]\x00")
        else:
            out.append(re.escape(ch.encode()) + rb"\x00")
    return b"".join(out)


def _ole_patterns(mode: str) -> list[tuple[str, re.Pattern]]:
    if mode == "doc":
        names = "|".join(_WORD_FIELDS)
        plain = re.compile(rb"\x13[ \t]*(" + names.encode() + rb")\b", re.IGNORECASE)
        wide = re.compile(rb"\x13\x00(?:[ \t]\x00)*(" + b"|".join(_utf16(n) for n in _WORD_FIELDS) + rb")(?![A-Za-z]\x00)")
        return [("字段", plain), ("字段", wide)]
    plain = re.compile(b"|".join(re.escape(n.encode()) for n in _EXCEL_FUNCTIONS), re.IGNORECASE)
    wide = re.compile(b"|".join(_utf16(n) for n in _EXCEL_FUNCTIONS))
    return [("函数", plain), ("函数", wide)]


def inspect_ooxml(handle, size: int, limits: dict) -> dict:
    """Macros and external references of a .docx / .xlsx (its ZIP directory and relationship parts)."""
    archive, infos = _open_zip(handle, size, 5000, "文档")
    macros, external, hyperlinks, parts = False, [], 0, 0
    for info in infos:
        lowered = info.filename.replace("\\", "/").lower()
        if lowered.endswith("vbaproject.bin") or (lowered.endswith(".bin") and "activex" in lowered):
            macros = True
        if lowered.startswith("xl/externallinks/") or lowered == "xl/connections.xml":
            external.append(f"外部数据（{_shown(info.filename)}）")
        try:
            if lowered == "[content_types].xml" and b"macroenabled" in _read_member(archive, info, META_BYTES).lower():
                macros = True
            if not lowered.endswith(".rels"):
                continue
            parts += 1
            if parts > MAX_RELATIONSHIP_PARTS:            # never stop early: what comes after would go unchecked
                raise Refused("too_many_parts", f"文档里的关系部件超过 {MAX_RELATIONSHIP_PARTS} 个，多得不正常，出于安全没有检查完")
            data = _read_member(archive, info, 2 * META_BYTES)
        except _ZIP_ERRORS:
            raise Refused("archive_invalid", f"文档里的 {_shown(info.filename)} 读不出来") from None
        for target_type, target in _external_targets(data, limits):
            if target_type.endswith("/hyperlink"):
                hyperlinks += 1
            elif len(external) < 50:
                external.append(f"{target_type.rsplit('/', 1)[-1] or '引用'}：{_shown(target)}")
    return {"macros": macros, "external": external, "hyperlinks": hyperlinks}


def _external_targets(data: bytes, limits: dict) -> list[tuple[str, str]]:
    found = []

    def start(tag, attrs):
        if tag.rsplit(":", 1)[-1] == "Relationship" and attrs.get("TargetMode", "").lower() == "external":
            found.append((attrs.get("Type", ""), attrs.get("Target", "")))

    SafeXml(limits, start).parse(data)
    return found


def inspect_ole(handle, mode: str) -> dict:
    """A .doc / .xls: a macro project (a directory entry named like one), and field codes (Word) or
    functions (Excel) that fetch content from elsewhere. External workbook links of old Excel files
    (SUPBOOK records) are not recognised."""
    data = handle.read()
    macros = False
    for name in _VBA_STREAMS:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        position = data.find(raw)
        while position >= 0 and not macros:
            macros = position % 128 == 0 and data[position + 64:position + 66] == struct.pack("<H", len(raw))
            position = data.find(raw, position + 2)
    external = []
    for what, pattern in _ole_patterns(mode):
        for found in pattern.finditer(data):
            name = found.group(0).replace(b"\x00", b"").lstrip(b"\x13 \t").decode("ascii", "replace").upper()
            label = f"{what} {name.replace('_XLFN.', '')}"
            if label not in external:
                external.append(label)
            if len(external) >= 10:
                break
    return {"macros": macros, "external": external, "hyperlinks": 0}


# ------------------------------------------------------------------ entry point
TAR_MODES = {"tar": "", "tar.gz": "gz", "tar.xz": "xz", "tar.bz2": "bz2"}


def run(mode: str, limits: dict, handle) -> dict:
    size = os.fstat(handle.fileno()).st_size
    if mode == "urdf":
        if size > int(limits.get("urdf_bytes", 32 * 1024 * 1024)):
            raise Refused("too_large", "URDF 文件太大，没有解析")
        return parse_urdf(handle.read(), limits)
    if mode == "deb":
        return parse_deb(handle, limits)
    if mode == "zip":
        members = Members(limits)
        scan_zip(handle, size, members)
        return members.facts()
    if mode in TAR_MODES:
        members = Members(limits)
        scan_tar(Unpacked(handle, TAR_MODES[mode], int(limits.get("unpacked", 4 * 1024 ** 3)),
                          int(limits.get("xz_memory", XZ_MEMORY))), members)
        return members.facts()
    if mode in ("docx", "xlsx"):
        return inspect_ooxml(handle, size, limits)
    if mode in ("doc", "xls"):
        if size > 256 * 1024 * 1024:
            raise Refused("too_large", "文档太大，没有检查")
        return inspect_ole(handle, mode)
    raise Refused("unknown_mode", f"不认识的解析方式 {mode}")


def main(argv: list[str]) -> int:
    limits = json.loads(argv[2]) if len(argv) > 2 else {}
    apply_limits(limits)
    refused = None
    try:
        handle = os.fdopen(0, "rb", closefd=False)
        result = {"ok": True, "facts": run(argv[1], limits, handle)}
    except Refused as exc:
        refused = exc
    except MemoryError:
        refused = Refused("out_of_memory", "解析需要的内存超过上限")
    except RecursionError:
        refused = Refused("too_deep", "资料的结构嵌套太深")
    if refused is not None:
        result = {"ok": False, "code": refused.code, "error": refused.message}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
