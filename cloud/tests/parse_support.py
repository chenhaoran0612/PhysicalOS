"""Made-up files for the parsing tests (issue #34). Nothing here is a real vendor's material."""
from __future__ import annotations

import gzip
import io
import lzma
import struct
import tarfile
import zipfile
import zlib

URDF_OK = b"""<?xml version="1.0"?>
<robot name="demo_arm">
  <link name="base_link"/>
  <link name="upper_arm"/>
  <link name="forearm"/>
  <link name="slider"/>
  <link name="wheel"/>
  <joint name="shoulder" type="revolute">
    <parent link="base_link"/><child link="upper_arm"/>
    <limit lower="-1.57" upper="1.57" velocity="2.0" effort="40"/>
  </joint>
  <joint name="elbow" type="revolute">
    <parent link="upper_arm"/><child link="forearm"/>
    <limit lower="-2.5" upper="0" velocity="3.1" effort="25.5"/>
  </joint>
  <joint name="rail" type="prismatic">
    <parent link="base_link"/><child link="slider"/>
    <limit lower="0" upper="0.4" velocity="0.2" effort="120"/>
  </joint>
  <joint name="spin" type="continuous">
    <parent link="base_link"/><child link="wheel"/>
    <limit velocity="10" effort="5"/>
  </joint>
  <joint name="mount" type="fixed"><parent link="base_link"/><child link="forearm"/></joint>
  <transmission name="t1"><joint name="shoulder"><hardwareInterface>x</hardwareInterface></joint></transmission>
</robot>
"""

URDF_GAPS = b"""<?xml version="1.0"?>
<robot name="half" xmlns:xacro="http://www.ros.org/wiki/xacro">
  <link name="a"/><link name="b"/>
  <joint name="j1" type="revolute"><parent link="a"/><child link="b"/>
    <limit lower="${-pi/2}" upper="${pi/2}" velocity="1" effort="3"/></joint>
  <joint name="j2" type="revolute"><parent link="a"/><child link="b"/></joint>
</robot>
"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE robot [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<robot name="&secret;"><link name="a"/></robot>
"""

LAUGHS = (b'<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol">'
          + b"".join(f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'.encode()
                     for i in range(1, 10))
          + b']>\n<robot name="&lol9;"/>\n')


def deep_xml(depth: int) -> bytes:
    return b'<robot name="d">' + b"<a>" * depth + b"</a>" * depth + b"</robot>"


def many_elements(count: int) -> bytes:
    return b'<robot name="m">' + b"<link/>" * count + b"</robot>"


# ------------------------------------------------------------------ archives
def tar_bytes(members: dict[str, bytes], *, links: dict[str, str] | None = None, fmt=tarfile.PAX_FORMAT) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=fmt) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return out.getvalue()


def zip_bytes(members: dict[str, bytes], *, symlinks: dict[str, str] | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, target)
    return out.getvalue()


def elf(machine: int) -> bytes:
    """The first bytes of a 64-bit little-endian ELF program for ``machine``, padded."""
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    return ident + struct.pack("<HHI", 2, machine, 1) + bytes(100)


CONTROL = (b"Package: demo-driver\nVersion: 2.3.1-1\nArchitecture: arm64\nMaintainer: Someone <x@example.test>\n"
           b"Depends: libc6 (>= 2.35)\nDescription: demo motion driver\n long text that is not kept\n"
           b"Homepage: https://example.test/driver\n")


def _ar_member(name: str, data: bytes) -> bytes:
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return header + data + (b"\n" if len(data) % 2 else b"")


def deb_bytes(control: bytes = CONTROL, compression: str = "gz") -> bytes:
    inner = tar_bytes({"./control": control, "./md5sums": b""})
    packed = {"gz": lambda d: gzip.compress(d, mtime=0), "xz": lzma.compress, "": lambda d: d,
              "zst": lambda d: b"\x28\xb5\x2f\xfd" + d}[compression]
    name = "control.tar" + (f".{compression}" if compression else "")
    return (b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member(name, packed(inner))
            + _ar_member("data.tar.gz", gzip.compress(tar_bytes({"./usr/bin/demo": elf(0xB7)}), mtime=0)))


def package_xml(name: str = "demo_driver", version: str = "0.4.2") -> bytes:
    return (f'<?xml version="1.0"?>\n<package format="3"><name>{name}</name><version>{version}</version>'
            f"<description>a demo driver</description></package>").encode()


def zero_gzip_members(total_mib: int) -> bytes:
    """A tar.gz of one member holding ``total_mib`` MiB of zeros, built as concatenated gzip members (instant)."""
    size = total_mib * 1024 * 1024
    info = tarfile.TarInfo("big.bin")
    info.size = size
    header = info.tobuf(format=tarfile.USTAR_FORMAT)
    block = gzip.compress(bytes(16 * 1024 * 1024), compresslevel=1, mtime=0)
    return (gzip.compress(header, mtime=0) + block * (size // (16 * 1024 * 1024))
            + gzip.compress(bytes(1024), mtime=0))


def zip_bomb(member_mib: int = 100) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload/zeros.bin", bytes(member_mib * 1024 * 1024))
        archive.writestr("package.xml", package_xml())
    return out.getvalue()


def huge_pax_tar() -> bytes:
    """A tar whose extended header claims 4 GiB."""
    info = tarfile.TarInfo("././@PaxHeader")
    info.type = tarfile.XHDTYPE
    info.size = 4 * 1024 ** 3
    return info.tobuf(format=tarfile.USTAR_FORMAT) + bytes(4096)


# ------------------------------------------------------------------ Office
def docx_with(rels: bytes = b"", *, macro: bool = False, extra: dict[str, bytes] | None = None) -> bytes:
    members = {"[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<Relationships/>",
               "word/document.xml": "<w:document>示例</w:document>".encode(),
               "word/_rels/document.xml.rels": rels or b"<Relationships/>"}
    if macro:
        members["word/vbaProject.bin"] = b"\xd0\xcf\x11\xe0" + bytes(100)
    members.update(extra or {})
    return zip_bytes(members)


def rels(*items: tuple[str, str]) -> bytes:
    body = "".join(f'<Relationship Id="r{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   f'relationships/{kind}" Target="{target}" TargetMode="External"/>' for i, (kind, target) in enumerate(items))
    return ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + body + "</Relationships>").encode()


def ole_with(stream: str) -> bytes:
    def entry(name: str) -> bytes:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return raw.ljust(64, b"\x00") + len(raw).to_bytes(2, "little") + bytes(62)
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504) + entry("Root Entry") + entry("WordDocument") + entry(stream) + bytes(128)


def zlib_ok() -> bool:
    return bool(zlib.ZLIB_VERSION)
