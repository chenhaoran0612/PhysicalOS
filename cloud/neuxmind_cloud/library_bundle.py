"""Library kind ``bundle`` (issue #43, docs/CLOUD_SKILLS.md §2): a robot's programs and scene data.

Some robots walk by programs on their own computer; those programs and the scene data they use (place
marks, reference maps) are one directory, kept in the material library as one ``.tar.gz``. A bundle
reuses the library's chunked upload, checks, quota and download, with three differences that live here
(``library.py`` and ``material_parse.py`` only call in):

* it is asked for with ``kind: "bundle"`` on ``POST /library/uploads`` and must be a ``.tar.gz`` / ``.tgz``;
* it belongs to one device (``device_id`` and ``device_name`` are required): scene data means something
  only for that robot at that site;
* it is never parsed: stored, it is ``usable`` at once, and the parse routes answer 409 ``not_parsed_kind``.

The cloud never unpacks a bundle. Whoever does (the console, on the robot) treats it as untrusted.
"""
from __future__ import annotations

from dataclasses import replace

from . import library_types as types
from .config import Settings
from .errors import ApiError, bad_request, conflict
from .library_models import USABLE

BUNDLE = types.BUNDLE
BUNDLE_FORMATS = ("tar.gz",)


def requested_format(payload: dict, fmt: types.Format) -> types.Format:
    """The format the upload is stored as. ``kind`` is optional: absent, the file is what its suffix
    says (a document or a driver package); ``"bundle"`` turns a tar.gz into a bundle; anything else
    must agree with the suffix."""
    kind = payload.get("kind")
    if kind is None or kind == fmt.kind:
        return fmt
    if kind != BUNDLE:
        raise bad_request(f"kind 须为 {' / '.join(types.KINDS)} 之一，并与文件类型相符（可以不填）")
    if fmt.id not in BUNDLE_FORMATS:
        raise ApiError(415, "unsupported_type", "程序和场景数据（bundle）只接受 .tar.gz / .tgz",
                       supported=[ext for fmt_id in BUNDLE_FORMATS for ext in types.BY_ID[fmt_id].extensions])
    return replace(fmt, kind=BUNDLE)


def size_limit(fmt: types.Format, settings: Settings, default: int) -> int:
    return settings.library_max_bundle_bytes if fmt.kind == BUNDLE else default


def label(fmt: types.Format, default: str) -> str:
    return "程序和场景数据" if fmt.kind == BUNDLE else default


def require_device(fmt: types.Format, device_id: str, device_name: str) -> None:
    if fmt.kind == BUNDLE and not (device_id and device_name):
        raise bad_request("程序和场景数据必须绑定一台设备：需要 device_id 和 device_name", code="bundle_needs_device")


def on_stored(material) -> None:
    """A bundle is not parsed: it is usable as soon as it is stored."""
    if material.kind == BUNDLE:
        material.status, material.status_detail = USABLE, ""


def refuse_parse(material) -> None:
    if material.kind == BUNDLE:
        raise conflict("程序和场景数据不解析：它入库后就是可用的，没有解析结果", code="not_parsed_kind")


def refuse_other_kind(existing, fields: dict) -> None:
    """The same file with the same labels may be offered once as a driver package and once as a
    bundle. An unfinished upload of one kind is never continued as the other: it would be stored (and
    then parsed, or handed to a robot as a driver) as what the first request said."""
    if existing is not None and existing.kind != fields["kind"]:
        raise ApiError(409, "upload_kind_conflict",
                       f"这个文件已经有一次没完成的上传，类型是 {existing.kind}，这次要的是 {fields['kind']}；"
                       "请先完成或取消那一次上传（DELETE /library/uploads/{id}），再重新开始",
                       upload_id=existing.id, kind=existing.kind)
