"""Input hygiene and skill-parameter checks shared by the skill store, the device link and the open
platform (issue #43).

Two things live here:

* what the database can hold and what nothing downstream should have to escape: text without NUL or
  lone surrogates, JSON without NaN / Infinity, bounded in size and in nesting;
* the parameter rules of a ``skill.package.v1`` declaration (types, ``choices`` / ``options``,
  ``min`` / ``max``, lengths). They follow the device's own checks (``runtime/neuxmind_runtime/
  skills.py``) so that the cloud refuses early what the device would refuse anyway. The device stays
  the authority: it checks every call again.
"""
from __future__ import annotations

import json
import math

from .spec_bridge import capabilities as spec_caps

MAX_DEPTH = 64
MAX_DECLARED = 100              # parameters one skill may declare (device-reported lists)
ALIASES = {"minimum": "min", "maximum": "max"}
RANGE_TYPES = ("float", "int", "vector", "floats", "pose", "object")
FLOAT_LISTS = ("vector", "floats", "pose")
_KEEP_CONTROL = ("\n", "\t")
_MISSING = object()


# ---------------------------------------------------------------- text and JSON the database can hold
def is_control(ch: str) -> bool:
    return ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0


def encodable(text: str) -> bool:
    """UTF-8 encodable (no lone surrogate) and without NUL."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def storable(text: str) -> bool:
    """Encodable and free of control characters other than newline and tab."""
    return encodable(text) and not any(is_control(ch) and ch not in _KEEP_CONTROL for ch in text)


def cleaned(value, limit: int) -> str:
    """A descriptive text (a name, a title): made storable rather than refused."""
    if not isinstance(value, str):
        return ""
    text = value.encode("utf-8", "replace").decode("utf-8")
    return "".join(" " if is_control(ch) and ch not in _KEEP_CONTROL else ch for ch in text).strip()[:limit]


def walk(value, max_depth: int = MAX_DEPTH, keys: bool = False):
    """Every scalar (with ``keys``: and every object key) inside ``value``; iterative, ``ValueError``
    past ``max_depth``."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if depth >= max_depth:
                raise ValueError(f"嵌套超过 {max_depth} 层")
            for key, inner in item.items():
                if keys:
                    yield key
                stack.append((inner, depth + 1))
        elif isinstance(item, (list, tuple)):
            if depth >= max_depth:
                raise ValueError(f"嵌套超过 {max_depth} 层")
            stack.extend((inner, depth + 1) for inner in item)
        else:
            yield item


def json_problem(value, max_bytes: int, max_depth: int = MAX_DEPTH) -> str | None:
    """Why ``value`` cannot be kept as JSON (``None`` when it can): too deep, NaN / Infinity, a NUL or
    a lone surrogate in a string, or more than ``max_bytes`` once written out."""
    try:
        for scalar in walk(value, max_depth, keys=True):
            if isinstance(scalar, float) and not math.isfinite(scalar):
                return "含有 NaN 或无穷大"
            if isinstance(scalar, str) and not encodable(scalar):
                return "含有空字符或无法保存的字符"
            if not isinstance(scalar, (str, int, float, bool, type(None))):
                return "不是 JSON 值"
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as exc:
        return str(exc) or "不是可以保存的 JSON"
    if size > max_bytes:
        return f"过大（{size} 字节，上限 {max_bytes}）"
    return None


# ---------------------------------------------------------------- parameter declarations
def _num(value) -> bool:
    """A finite JSON number that also fits a float (an integer of 400 digits does not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _same(a, b) -> bool:
    """Equality that also compares types: ``True`` is not ``1``, ``1.0`` is not ``1``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _same_number(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_number(x, y) for x, y in zip(a, b))
    if _num(a) and _num(b):
        return float(a) == float(b)
    return _same(a, b)


def normalize(decl: dict) -> dict:
    """``minimum`` / ``maximum`` are aliases of ``min`` / ``max``; both with different values: ``ValueError``."""
    if not any(alias in decl for alias in ALIASES):
        return decl
    out = dict(decl)
    for alias, key in ALIASES.items():
        if alias in out:
            value = out.pop(alias)
            if key in out and not _same(out[key], value):
                raise ValueError(f"参数 {decl.get('name')} 同时声明了 {key} 与 {alias} 且不一致")
            out[key] = value
    return out


def allowed_values(decl: dict) -> list | None:
    if isinstance(decl.get("choices"), list):
        return list(decl["choices"])
    if isinstance(decl.get("options"), list):
        return [option["value"] if isinstance(option, dict) else option for option in decl["options"]]
    return None


def _check_declaration(decl: dict) -> None:
    name = decl.get("name")
    for key in ("min", "max"):
        if key in decl and not _num(decl[key]):
            raise ValueError(f"参数 {name} 的 {key} 须为有限数值")
    if ("min" in decl or "max" in decl) and decl.get("type") not in RANGE_TYPES:
        raise ValueError(f"参数 {name} 的类型 {decl.get('type')} 不支持 min/max")
    if "min" in decl and "max" in decl and decl["min"] > decl["max"]:
        raise ValueError(f"参数 {name} 的 min 大于 max")
    for key in ("length", "min_length", "max_length"):
        value = decl.get(key)
        if key in decl and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"参数 {name} 的 {key} 须为非负整数")
    if "min_length" in decl and "max_length" in decl and decl["min_length"] > decl["max_length"]:
        raise ValueError(f"参数 {name} 的 min_length 大于 max_length")
    for key in ("choices", "options"):
        if key in decl and not isinstance(decl[key], list):
            raise ValueError(f"参数 {name} 的 {key} 须为列表")
    if "options" in decl and any(isinstance(option, dict) and "value" not in option for option in decl["options"]):
        raise ValueError(f"参数 {name} 的 options 每项须有 value")
    if "required" in decl and not isinstance(decl["required"], bool):
        raise ValueError(f"参数 {name} 的 required 须为 true 或 false")
    if "default" in decl:
        problem = value_problem(decl, decl["default"])
        if problem is not None:
            raise ValueError(f"参数 {name} 的默认值不符合声明：{problem[1]}")


def check_declarations(declared) -> list[dict]:
    """The normalised declarations, or ``ValueError`` when a constraint could not be enforced
    (fail closed, as the device does at install)."""
    if not isinstance(declared, list) or len(declared) > MAX_DECLARED:
        raise ValueError(f"parameters 须为不超过 {MAX_DECLARED} 项的列表")
    seen, out = set(), []
    for decl in declared:
        if not isinstance(decl, dict) or decl.get("type") not in spec_caps.FIELD_TYPES:
            raise ValueError("每个参数需要 name 与合法 type")
        name = decl.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or not storable(name):
            raise ValueError("参数名须为 1–100 个字符")
        if name in seen:
            raise ValueError(f"参数 {name} 重复声明")
        seen.add(name)
        decl = normalize(decl)
        _check_declaration(decl)
        out.append(decl)
    return out


# ---------------------------------------------------------------- parameter values
def _type_ok(ptype: str, value, decl: dict) -> bool:
    if ptype == "float":
        return _num(value)
    if ptype == "int":
        return not isinstance(value, bool) and isinstance(value, int)
    if ptype == "bool":
        return isinstance(value, bool)
    if ptype in ("str", "blob_ref"):
        return isinstance(value, str)
    if ptype == "enum":
        return any(_same(value, choice) for choice in decl.get("choices") or [])
    if ptype in FLOAT_LISTS:
        if not isinstance(value, list) or not all(_num(item) for item in value):
            return False
        length = 7 if ptype == "pose" else decl.get("length")
        return length is None or len(value) == length
    return True                                         # object: any JSON value


def _range_numbers(ptype: str, value) -> list | None:
    """The numbers ``min`` / ``max`` apply to; ``None`` when the value holds something that is not a number."""
    if ptype in ("float", "int"):
        return [value]
    if ptype == "pose":
        return list(value[:3])                          # the position only, never the quaternion
    scalars = list(walk(value))
    if ptype == "object" and not all(_num(scalar) for scalar in scalars):
        return None
    return [scalar for scalar in scalars if _num(scalar)]


def value_problem(decl: dict, value) -> tuple[str, str] | None:
    """``(problem code, message)`` when ``value`` does not meet ``decl`` (already normalised)."""
    name, ptype = decl["name"], decl["type"]
    if json_problem(value, 1 << 30) is not None:
        return "param_type", f"参数 {name} 不是可用的 JSON 值（NaN、无穷大、空字符或嵌套过深）"
    if not _type_ok(ptype, value, decl):
        if ptype == "enum":
            return "param_choice", f"参数 {name} 只能取 {decl.get('choices', [])} 之一"
        if ptype in FLOAT_LISTS and isinstance(value, list) and all(_num(item) for item in value):
            return "param_length", f"参数 {name} 的长度须为 {7 if ptype == 'pose' else decl.get('length')}"
        return "param_type", f"参数 {name} 需要 {ptype}"
    allowed = allowed_values(decl) if ptype != "enum" else None
    if allowed is not None:
        same = _same_number if ptype in ("float", *FLOAT_LISTS) else _same
        if not any(same(value, choice) for choice in allowed):
            return "param_choice", f"参数 {name} 只能取 {allowed} 之一"
    low, high = decl.get("min"), decl.get("max")
    if low is not None or high is not None:
        numbers = _range_numbers(ptype, value)
        if numbers is None:
            return "param_type", f"参数 {name} 声明了 min/max，其中只能是数值"
        if any((low is not None and n < low) or (high is not None and n > high) for n in numbers):
            return "param_out_of_range", (f"参数 {name} 须在 {low if low is not None else '不限'} – "
                                          f"{high if high is not None else '不限'} 之间")
    if isinstance(value, (list, str)):
        exact = decl.get("length") if ptype not in FLOAT_LISTS else None
        shortest, longest = decl.get("min_length"), decl.get("max_length")
        size = len(value)
        if ((exact is not None and size != exact) or (shortest is not None and size < shortest)
                or (longest is not None and size > longest)):
            return "param_length", f"参数 {name} 的长度不符合声明"
    return None


def check_values(declared: list[dict], given: dict) -> list[dict]:
    """Every way ``given`` does not meet the declared parameters: ``[{name, problem, message}]``
    (empty when it does). ``problem`` is ``param_unknown`` / ``param_required`` / ``param_type`` /
    ``param_choice`` / ``param_out_of_range`` / ``param_length``, the device's own names."""
    try:
        declared = check_declarations(declared)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return [{"name": "", "problem": "param_declaration_invalid", "message": f"技能的参数声明无效：{exc}"}]
    names = {decl["name"] for decl in declared}
    problems = [{"name": str(name)[:100], "problem": "param_unknown", "message": "技能不接受这个参数"}
                for name in sorted(map(str, set(given) - names))]
    for decl in declared:
        value = given.get(decl["name"], decl.get("default", _MISSING))
        if value is _MISSING:
            if decl.get("required", False):
                problems.append({"name": decl["name"], "problem": "param_required",
                                 "message": f"缺少参数 {decl['name']}"})
            continue
        try:
            found = value_problem(decl, value)
        except (ValueError, TypeError, KeyError, IndexError, ArithmeticError):
            found = ("param_type", f"参数 {decl['name']} 的值无法按声明检查")
        if found is not None:
            problems.append({"name": decl["name"], "problem": found[0], "message": found[1]})
    return problems
