"""What a parsed material says (issue #34, R1-13): the items of each kind, their checks, what is missing.

Three kinds of result, chosen by the material's format:

* ``document`` — a driver manual or parameter sheet (Word, Excel, PDF, text).
  Read by the knowledge service (WeKnora) and filled by asking its model.
* ``urdf`` — joints and links, and each joint's position, velocity and effort
  limits. Read by rules. The limits are a reference for whoever fills in the
  device's safety limits; nothing here applies them.
* ``package`` — a driver package. Its content is not interpreted: the checksum
  is the library's, name, version and architecture are what the package itself
  states (Debian control fields, a ROS ``package.xml``, the machine type of the
  programs inside); everything else is filled in by a person.

Every value, whether it comes from a parser, a model's answer or a person, goes
through :func:`clean_values`: plain strings without control characters, finite
numbers, bounded lists. Nothing in a result is ever executed by the cloud; the
install steps are text shown to a person (and later to the repair assistant,
which must treat them as untrusted as well).
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from . import library_types as types

DOCUMENT, URDF, PACKAGE = "document", "urdf", "package"
RESULT_KINDS = (DOCUMENT, URDF, PACKAGE)

TEXT, TEXT_LIST, NAMED_LIST, JOINTS = "text", "text_list", "named_list", "joints"
MAX_TEXT, MAX_ITEMS, MAX_STEP = 500, 500, 1000
MAX_JOINTS, MAX_LINKS = 5000, 5000
MAX_NAME = 256
MOVABLE = ("revolute", "prismatic", "continuous")
JOINT_TYPES = ("revolute", "continuous", "prismatic", "fixed", "floating", "planar")
LIMIT_KEYS = ("lower", "upper", "velocity", "effort")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    required: bool
    hint: str = ""
    max_len: int = MAX_TEXT
    max_items: int = MAX_ITEMS
    read_only: bool = False


DOCUMENT_FIELDS = (
    Field("driver_name", "驱动名称", TEXT, True, max_len=200),
    Field("driver_version", "驱动版本", TEXT, True, max_len=100),
    Field("source", "从哪里取驱动", TEXT, True, "网址、软件源、代码仓库或安装包的文件名"),
    Field("os", "适用的系统", TEXT_LIST, True, "例如 Ubuntu 22.04", max_len=100, max_items=20),
    Field("ros2_distro", "适用的 ROS 2 发行版", TEXT_LIST, True, "例如 humble", max_len=50, max_items=20),
    Field("install_steps", "安装步骤", TEXT_LIST, True, "按顺序，每一步一句话或一条命令", max_len=MAX_STEP, max_items=100),
    Field("topics", "装好后应该出现的话题", NAMED_LIST, True, "话题名和消息类型"),
    Field("actions", "装好后应该出现的动作", NAMED_LIST, False, "动作名和动作类型；没有动作的驱动可以不填"),
)
URDF_FIELDS = (
    Field("robot_name", "机器人名称（URDF 里写的）", TEXT, False, max_len=MAX_NAME),
    Field("links", "连杆", TEXT_LIST, True, max_len=MAX_NAME, max_items=MAX_LINKS),
    Field("joints", "关节和上限", JOINTS, True, "位置上限：转动关节是弧度，移动关节是米；速度：rad/s 或 m/s；力矩：N·m 或 N",
          max_items=MAX_JOINTS),
)
PACKAGE_FIELDS = (
    Field("sha256", "校验值（SHA-256）", TEXT, True, max_len=64, read_only=True),
    Field("package_name", "名称", TEXT, True, max_len=200),
    Field("package_version", "版本", TEXT, True, max_len=100),
    Field("architecture", "适用架构", TEXT_LIST, True, "例如 arm64、amd64；不分架构的包填 all", max_len=50, max_items=10),
    Field("os", "适用的系统", TEXT_LIST, False, max_len=100, max_items=20),
    Field("ros2_distro", "适用的 ROS 2 发行版", TEXT_LIST, False, max_len=50, max_items=20),
    Field("install_steps", "安装步骤", TEXT_LIST, False, max_len=MAX_STEP, max_items=100),
    Field("notes", "说明", TEXT, False, max_len=2000),
)
FIELDS = {DOCUMENT: DOCUMENT_FIELDS, URDF: URDF_FIELDS, PACKAGE: PACKAGE_FIELDS}
NOTES = {
    URDF: "关节上限只是填写设备安全限值时的参考，不会自动生效。",
    PACKAGE: "安装包的内容没有被解析：名称、版本和架构是包里写明的，其余由人填写。",
    DOCUMENT: "这些项目由大模型根据资料填写，可能有错；请对照资料核对后再确认。",
}


def result_kind(fmt: str) -> str:
    if fmt == "urdf":
        return URDF
    return PACKAGE if types.BY_ID[fmt].kind == types.PACKAGE else DOCUMENT


def schema(kind: str) -> list[dict]:
    return [{"key": f.key, "label": f.label, "type": f.type, "required": f.required, "hint": f.hint,
             "read_only": f.read_only, "max_items": f.max_items} for f in FIELDS[kind]]


# ------------------------------------------------------------------ cleaning
class Invalid(ValueError):
    """A value a person sent cannot be stored (what is wrong, in words for the page)."""


def _finite(value) -> float | None:
    """``value`` as a finite float, or ``None`` (also for an integer too large for a float)."""
    try:
        number_ = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number_ if math.isfinite(number_) else None


def _text(value, max_len: int) -> str | None:
    """A plain one-line string, or ``None`` when nothing is left. Longer text is cut."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value) if _finite(value) is not None else None
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch) in types.UNPRINTABLE else ch
                   for ch in unicodedata.normalize("NFC", value))
    text = " ".join(text.split())
    return text[:max_len] or None


def number(value) -> float | None:
    """A finite number (also from a string such as ``"1.57"``), else ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return _finite(value.strip()[:100]) if value.strip() else None
    return _finite(value) if isinstance(value, (int, float)) else None


def _items(value, max_items: int) -> list:
    """The raw items of a list value; a few more than may be kept, since some may turn out unusable."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    return list(value)[:max_items * 4] if isinstance(value, (list, tuple)) else []


def _named(value, max_items: int) -> list[dict]:
    out, seen = [], set()
    for item in _items(value, max_items):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), MAX_NAME)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "type": _text(item.get("type"), MAX_NAME) or ""})
    return out[:max_items]


def _joints(value, max_items: int) -> list[dict]:
    out, seen = [], set()
    for item in _items(value, max_items):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), MAX_NAME)
        if not name or name in seen:
            continue
        seen.add(name)
        joint_type = _text(item.get("type"), 20) or ""
        joint = {"name": name, "type": joint_type if joint_type in JOINT_TYPES else "",
                 "parent": _text(item.get("parent"), MAX_NAME) or "", "child": _text(item.get("child"), MAX_NAME) or ""}
        for key in LIMIT_KEYS:
            joint[key] = number(item.get(key))
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        kept = {key: _text(raw.get(key), 200) for key in LIMIT_KEYS if _text(raw.get(key), 200)}
        if kept:
            joint["raw"] = kept
        out.append(joint)
    return out[:max_items]


def clean_value(field: Field, value):
    if field.type == TEXT:
        return _text(value, field.max_len)
    if field.type == TEXT_LIST:
        cleaned = [_text(item, field.max_len) for item in _items(value, field.max_items)]
        return list(dict.fromkeys(item for item in cleaned if item))[:field.max_items]
    if field.type == NAMED_LIST:
        return _named(value, field.max_items)
    return _joints(value, field.max_items)


def clean_values(kind: str, values) -> dict:
    """Every field of ``kind`` with a clean value (``None`` / ``[]`` when there is none). Unknown keys are dropped."""
    values = values if isinstance(values, dict) else {}
    return {f.key: clean_value(f, values.get(f.key)) for f in FIELDS[kind]}


def check_edit(kind: str, values, not_applicable) -> tuple[dict, list[str]]:
    """What a person sends: refused (``Invalid``) when it is not an object of known fields."""
    if not isinstance(values, dict):
        raise Invalid("values 须为对象")
    known = {f.key: f for f in FIELDS[kind]}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise Invalid(f"没有这些项目：{'、'.join(unknown[:10])}")
    if not_applicable is None:
        not_applicable = []
    if not isinstance(not_applicable, list) or not all(isinstance(k, str) for k in not_applicable):
        raise Invalid("not_applicable 须为项目名的数组")
    bad = [k for k in not_applicable if k not in known or not known[k].required]
    if bad:
        raise Invalid(f"这些项目不能标为“不适用”：{'、'.join(bad[:10])}")
    return values, sorted(set(not_applicable))


# ------------------------------------------------------------------ what is missing
def _joint_gaps(joint: dict) -> list[str]:
    """Limits a movable joint should state and does not (as numbers)."""
    if joint["type"] not in MOVABLE:
        return []
    wanted = ("velocity", "effort") if joint["type"] == "continuous" else LIMIT_KEYS
    names = {"lower": "位置下限", "upper": "位置上限", "velocity": "速度上限", "effort": "力矩上限"}
    return [names[key] for key in wanted if joint.get(key) is None]


def missing(kind: str, values: dict, not_applicable=()) -> list[str]:
    """Plain-language list of what a required item lacks (empty: the result is complete)."""
    out = []
    for f in FIELDS[kind]:
        if not f.required or f.key in not_applicable:
            continue
        value = values.get(f.key)
        if value in (None, "", []):
            out.append(f.label)
        elif f.type == JOINTS:
            for joint in value:
                if not joint["type"]:
                    out.append(f"关节 {joint['name']} 的类型")
                gaps = _joint_gaps(joint)
                if gaps:
                    out.append(f"关节 {joint['name']} 的{'、'.join(gaps)}")
    return out


def missing_text(gaps: list[str], limit: int = 12) -> str:
    shown = "、".join(gaps[:limit]) + (f" 等 {len(gaps)} 项" if len(gaps) > limit else "")
    return f"缺：{shown}" if gaps else ""
