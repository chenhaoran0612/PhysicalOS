"""Portable skills: package storage, loading, parameters and the SkillContext.

Trust boundary
--------------
By default (``sandbox="process"``, see ``sandbox.py``) skill code runs in its
own child process and reaches the robot only through a proxy of the
SkillContext defined here: the child's requests are served by a real
SkillContext in the runtime process, so every call is still checked here and
by the capability bus / safety supervisor. ``sandbox="inprocess"`` (debug
only, explicit opt-in) imports the skill into the runtime process, where
nothing stops it from reaching the adapter directly.

Writing a skill
---------------
``entry`` in the package is ``"<file>.py:<name>"``. ``name`` is either a
function ``run(ctx, **parameters)`` or a class whose instances have
``run(self, ctx, **parameters)`` (e.g. a subclass of :class:`Skill`).
"""
from __future__ import annotations

import copy

import hashlib
import importlib.util
import json
import math
import re
import shutil
import sys
import threading
import time
import types
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

from . import effects
from .errors import BadRequest, Conflict, Incompatible, NotFound
from .hal import CANCELLED, SUCCEEDED
from .safety import is_motion, thaw
from .store import Store

MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1536 * 1024
POLL_INTERVAL_S = 0.02
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


# ------------------------------------------------------------ exceptions
def _clamp_progress(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0


class SkillInterrupted(BaseException):
    """Raised inside a skill when it must stop.

    Derives from BaseException (like KeyboardInterrupt) so that a skill's
    ``except Exception:`` does not accidentally swallow a stop.
    """


class SkillStopped(SkillInterrupted):
    """Global stop / e-stop."""


class SkillCancelled(SkillInterrupted):
    """This run was cancelled."""


class SkillTimeout(SkillInterrupted):
    """The run exceeded its timeout."""


class ActionFailed(Exception):
    """An action capability finished unsuccessfully. Skills may catch this."""


class SkillError(Exception):
    """Misuse of the SkillContext (e.g. calling an undeclared capability)."""


class Skill:
    """Optional base class for class-style skills."""

    def run(self, ctx: "SkillContext", **parameters: Any) -> Any:
        raise NotImplementedError


# ------------------------------------------------------------ parameters
def _type_ok(ptype: str, value: Any, decl: dict) -> bool:
    number = (int, float)
    if ptype == "float":
        return not isinstance(value, bool) and isinstance(value, number) and _finite(value)
    if ptype == "int":
        return not isinstance(value, bool) and isinstance(value, int)
    if ptype == "bool":
        return isinstance(value, bool)
    if ptype in ("str", "blob_ref"):
        return isinstance(value, str)
    if ptype == "enum":
        return _one_of(value, decl.get("choices", []))
    if ptype in ("vector", "floats", "pose"):
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, number) or not _finite(v)
                                              for v in value):
            return False
        length = 7 if ptype == "pose" else decl.get("length")
        return length is None or len(value) == length
    return True   # object: any JSON value


def _finite(value: Any) -> bool:
    if isinstance(value, int):
        return True
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def _json_finite(value: Any) -> bool:
    """True if ``value`` contains no NaN/Infinity anywhere (object parameters). Iterative; a value
    nested deeper than :data:`MAX_PARAM_DEPTH` raises ``ValueError``."""
    return all(math.isfinite(v) for v in _walk(value) if isinstance(v, float))


MAX_PARAM_DEPTH = 64          # nesting levels accepted in a parameter value (Q6-08: 5000 levels -> 400, not 500)
RANGE_TYPES = ("float", "int", "vector", "floats", "pose", "object")   # where min/max mean something (Q6-02)
ALIASES = {"minimum": "min", "maximum": "max"}                          # JSON-Schema names the desktop form uses


def _same(a: Any, b: Any) -> bool:
    """Equality that also compares types (Q6-06): ``True`` is not ``1``, ``1.0`` is not ``1``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def _one_of(value: Any, allowed: list) -> bool:
    return any(_same(value, c) for c in allowed)


FLOAT_TYPES = ("float", "vector", "floats", "pose")   # declared as real numbers


def _same_number(a: Any, b: Any) -> bool:
    """Q7b-04: for a parameter declared as real numbers (``float`` and the float-list types) a
    JSON integer *is* that float -- ``1 == 1.0`` (browsers' ``JSON.parse``/``JSON.stringify``
    turn ``1.0`` into ``1``). Still strict for bool vs number and str vs number."""
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_number(x, y) for x, y in zip(a, b))
    if _num(a) and _num(b):
        return float(a) == float(b)
    return _same(a, b)


def _choice_ok(ptype: str, value: Any, allowed: list) -> bool:
    if ptype in FLOAT_TYPES:
        return any(_same_number(value, c) for c in allowed)
    return _one_of(value, allowed)


def _as_float(ptype: str, value: Any) -> Any:
    """A JSON integer given for a ``float`` parameter reaches the skill as a float (Q7b-04)."""
    if ptype == "float" and _num(value) and isinstance(value, int):
        return float(value)
    if ptype in ("vector", "floats", "pose") and isinstance(value, list):
        return [float(v) if _num(v) and isinstance(v, int) else v for v in value]
    return value


def _walk(value: Any):
    """Every scalar inside ``value`` (iterative, so any depth is safe); raises ``ValueError``
    past :data:`MAX_PARAM_DEPTH` levels."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (list, tuple, dict)):
            if depth >= MAX_PARAM_DEPTH:
                raise ValueError(f"嵌套超过 {MAX_PARAM_DEPTH} 层")
            stack.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
        else:
            yield item


def normalize_declaration(decl: dict) -> dict:
    """``minimum``/``maximum`` (JSON-Schema spelling, honoured by the desktop form) are aliases of
    ``min``/``max`` and enforced the same way on the device (Q6-02). Both spellings with different
    values: ``param_declaration_invalid``."""
    if not isinstance(decl, dict) or not any(k in decl for k in ALIASES):
        return decl
    out = dict(decl)
    for alias, key in ALIASES.items():
        if alias in out:
            value = out.pop(alias)
            if key in out and not _same(out[key], value):
                raise _param_error("param_declaration_invalid", decl.get("name"),
                                   f"技能参数 {decl.get('name')} 同时声明了 {key}（{out[key]!r}）与 {alias}（{value!r}）且不一致")
            out[key] = value
    return out


_MISSING = object()


def _num(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and _finite(value)


def _allowed_values(decl: dict) -> list | None:
    """Declared value set: ``choices`` (enum, or any scalar type) or ``options`` (values, or
    ``{"value": ..., "label": ...}`` objects). ``None`` when nothing is declared."""
    if "choices" in decl:
        return list(decl["choices"])
    if "options" in decl:
        return [o["value"] if isinstance(o, dict) else o for o in decl["options"]]
    return None


def _field_extra(decl: dict | None, name: str, problem: str) -> dict:
    """Field-level body agreed with the desktop (ux04): ``param``, ``min``, ``max``, ``choices``
    (``null`` when not declared) plus ``field``/``problem``."""
    decl = decl or {}
    choices = _allowed_values(decl) if isinstance(decl.get("choices", decl.get("options")), list) else None
    return {"param": name, "field": name, "problem": problem,
            "min": decl.get("min") if _num(decl.get("min")) else None,
            "max": decl.get("max") if _num(decl.get("max")) else None,
            "choices": choices}


def _param_error(code: str, name: str, message: str, decl: dict | None = None, **extra: Any) -> BadRequest:
    return BadRequest(message, code=code,
                      extra={**_field_extra(decl, name, code), "message": message, **extra})


def check_parameter_declarations(declared: list[dict]) -> None:
    """Q5-05: refuse a skill whose parameter constraints cannot be enforced (fail closed):
    ``min``/``max`` must be finite numbers with ``min <= max``; ``choices``/``options`` lists;
    ``length``/``min_length``/``max_length`` non-negative integers; a ``default`` must satisfy
    the declaration it belongs to."""
    seen = set()
    for decl in declared:
        decl = normalize_declaration(decl)
        name = decl.get("name")
        if name in seen:
            raise _param_error("param_declaration_invalid", name, f"技能参数 {name} 重复声明")
        seen.add(name)
        for key in ("min", "max"):
            if key in decl and not _num(decl[key]):
                raise _param_error("param_declaration_invalid", name,
                                   f"技能参数 {name} 的 {key} 须为有限数值，收到 {decl[key]!r}")
        if ("min" in decl or "max" in decl) and decl.get("type") not in RANGE_TYPES:
            raise _param_error("param_declaration_invalid", name,
                               f"技能参数 {name} 的类型 {decl.get('type')} 不支持 min/max（设备无法执行该约束）；"
                               f"字符串长度请用 min_length/max_length，取值集合请用 choices")
        for key in ("choices", "options", "default"):
            try:
                finite = key not in decl or _json_finite(decl[key])
            except ValueError:
                finite = False
            if not finite:
                raise _param_error("param_declaration_invalid", name,
                                   f"技能参数 {name} 的 {key} 含 NaN/无穷大或嵌套过深")
        if "min" in decl and "max" in decl and decl["min"] > decl["max"]:
            raise _param_error("param_declaration_invalid", name,
                               f"技能参数 {name} 的 min（{decl['min']}）大于 max（{decl['max']}）")
        for key in ("length", "min_length", "max_length"):
            value = decl.get(key)
            if key in decl and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise _param_error("param_declaration_invalid", name,
                                   f"技能参数 {name} 的 {key} 须为非负整数，收到 {value!r}")
        if ("min_length" in decl and "max_length" in decl and decl["min_length"] > decl["max_length"]):
            raise _param_error("param_declaration_invalid", name,
                               f"技能参数 {name} 的 min_length 大于 max_length")
        for key in ("choices", "options"):
            if key in decl and not isinstance(decl[key], list):
                raise _param_error("param_declaration_invalid", name, f"技能参数 {name} 的 {key} 须为列表")
        if "options" in decl and any(isinstance(o, dict) and "value" not in o for o in decl["options"]):
            raise _param_error("param_declaration_invalid", name, f"技能参数 {name} 的 options 每项须有 value")
        if "default" in decl:
            try:
                _check_one(decl, decl["default"])
            except BadRequest as exc:
                raise _param_error("param_declaration_invalid", name,
                                   f"技能参数 {name} 的默认值不符合声明：{exc}") from None


def _check_one(decl: dict, value: Any) -> None:
    """Type and declared constraints of one parameter value (raises a field-level BadRequest)."""
    name = decl["name"]
    ptype = decl["type"]
    try:
        finite = _json_finite(value)
    except ValueError as exc:
        raise _param_error("param_type", name, f"技能参数 {name} 嵌套过深（最多 {MAX_PARAM_DEPTH} 层）", decl,
                           expected=ptype, constraint={"max_depth": MAX_PARAM_DEPTH}) from None
    if not finite:
        raise _param_error("param_type", name, f"技能参数 {name} 不能包含 NaN 或无穷大", decl, expected=ptype)
    if not _type_ok(ptype, value, decl):
        if ptype in ("vector", "floats", "pose") and isinstance(value, list) and all(_num(v) for v in value):
            want = 7 if ptype == "pose" else decl.get("length")
            raise _param_error("param_length", name, f"技能参数 {name} 的长度须为 {want}，收到 {len(value)}", decl,
                               constraint={"length": want}, value_length=len(value))
        if ptype == "enum":
            raise _param_error("param_choice", name,
                               f"技能参数 {name} 只能取 {decl.get('choices', [])} 之一，收到 {value!r}", decl,
                               constraint={"choices": decl.get("choices", [])})
        # message prefix kept stable for existing clients ("技能参数 X 需要 T")
        raise _param_error("param_type", name, f"技能参数 {name} 需要 {ptype}（有限数值）", decl, expected=ptype)
    allowed = _allowed_values(decl) if ptype != "enum" else None
    if allowed is not None and not _choice_ok(ptype, value, allowed):
        raise _param_error("param_choice", name, f"技能参数 {name} 只能取 {allowed} 之一，收到 {value!r}", decl,
                           constraint={"choices": allowed})
    lo, hi = decl.get("min"), decl.get("max")
    if lo is not None or hi is not None:
        if ptype in ("float", "int"):
            numbers = [value]
        elif ptype == "pose":
            # Q7-07: the range of a pose applies to its position (x, y, z) only, never to the
            # orientation quaternion (whose components are always within -1..1)
            numbers = list(value[:3])
        elif ptype == "object":
            # Q7-07: with min/max declared, every scalar inside must be a JSON number -- a numeric
            # string ("1e3") or a bool would pass the range check and be float()-ed by the skill
            scalars = list(_walk(value))
            bad = next((v for v in scalars if not _num(v)), _MISSING)
            if bad is not _MISSING:
                raise _param_error("param_type", name,
                                   f"技能参数 {name} 声明了 min/max，其中只能是数值，收到 {bad!r}", decl,
                                   expected="number", value=bad)
            numbers = scalars
        else:   # vector / floats: every element (Q6-02)
            numbers = [v for v in _walk(value) if _num(v)]
        for v in numbers:
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                rng = f"{lo if lo is not None else '不限'} – {hi if hi is not None else '不限'}"
                raise _param_error("param_out_of_range", name,
                                   f"技能参数 {name} 超出允许范围：须在 {rng} 之间，收到 {v!r}", decl,
                                   constraint={k: decl[k] for k in ("min", "max") if k in decl}, value=v)
    if isinstance(value, (list, str)):
        n = len(value)
        exact = decl.get("length") if ptype not in ("vector", "floats", "pose") else None
        mn, mx = decl.get("min_length"), decl.get("max_length")
        if (exact is not None and n != exact) or (mn is not None and n < mn) or (mx is not None and n > mx):
            want = exact if exact is not None else f"{mn if mn is not None else 0} – {mx if mx is not None else '不限'}"
            raise _param_error("param_length", name, f"技能参数 {name} 的长度须为 {want}，收到 {n}", decl,
                               constraint={k: decl[k] for k in ("length", "min_length", "max_length") if k in decl},
                               value_length=n)


def check_parameters(declared: list[dict], given: dict | None) -> dict:
    """Apply defaults, reject unknown, ill-typed or out-of-declaration parameters.

    The type of ``given`` is checked first (a string or a list of pairs is a
    client error, not an "unknown outcome"); non-finite numbers are rejected.
    Q5-05: the declared ``min``/``max``, ``choices``/``options`` and
    ``length``/``min_length``/``max_length`` are enforced here, on the device, for every entry
    point (direct run, application steps, on-site test), not only in the desktop form.
    Errors are 400 with the field-level body agreed with the desktop:
    ``{error, message, code, param, min, max, choices, field, problem, ...}``; ``code`` is
    ``param_out_of_range`` / ``param_choice`` / ``param_length`` / ``param_required`` /
    ``param_type`` / ``param_declaration_invalid``. Unknown parameters keep ``code: bad_request``
    (compatibility) with ``problem: param_unknown``.
    """
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise BadRequest(f"parameters 须为对象，收到 {type(given).__name__}")
    given = dict(given)
    check_parameter_declarations(declared)
    declared = [normalize_declaration(d) for d in declared]
    names = {p["name"] for p in declared}
    unknown = sorted(set(given) - names)
    if unknown:
        message = f"技能不接受参数：{unknown}"
        # code kept "bad_request" for compatibility (sdk CLI tests); problem says what it is
        raise BadRequest(message, extra={**_field_extra(None, unknown[0], "param_unknown"),
                                         "fields": unknown, "message": message})
    out = {}
    for decl in declared:
        name = decl["name"]
        if name in given:
            value = given[name]
        elif "default" in decl:
            value = decl["default"]
        elif decl.get("required", False):
            raise _param_error("param_required", name, f"缺少技能参数 {name}", decl)
        else:
            continue
        _check_one(decl, value)
        out[name] = _as_float(decl["type"], value)
    return out


# ------------------------------------------------------------ packages
def package_digest(package: dict) -> str:
    return hashlib.sha256(json.dumps(package, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _safe_relpath(path: Any) -> str:
    if not isinstance(path, str) or not path or len(path) > 200 or "\\" in path or "\x00" in path:
        raise BadRequest(f"技能文件路径无效：{path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BadRequest(f"技能文件路径无效：{path!r}")
    return str(pure)


def _version_key(version: str) -> tuple:
    m = _SEMVER.match(version)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def load_package_dir(path: str | Path) -> dict:
    """Build an installable package dict (skill.json + text files) from a directory."""
    root = Path(path)
    package = json.loads((root / "skill.json").read_text(encoding="utf-8"))
    files = {}
    for file in sorted(root.rglob("*")):
        if file.is_file() and file.name != "skill.json" and "__pycache__" not in file.parts:
            files[file.relative_to(root).as_posix()] = file.read_text(encoding="utf-8")
    package["files"] = files
    return package


def split_entry(entry: str) -> tuple[str, str]:
    module, sep, attr = entry.partition(":")
    if not sep or not module or not attr.isidentifier():
        raise BadRequest("entry 须为 '<文件>.py:<函数或类名>'")
    rel = module if module.endswith(".py") else module.replace(".", "/") + ".py"
    return _safe_relpath(rel), attr


class SkillStore:
    """Installed skill packages. Versions are immutable once installed."""

    def __init__(self, store: Store, root: str | Path, manifest: dict):
        self._store = store
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._manifest = manifest
        self._lock = threading.Lock()

    def _dir(self, skill_id: str, version: str) -> Path:
        return self._root / skill_id / version

    def install(self, package: dict) -> dict:
        if not isinstance(package, dict):
            raise BadRequest("package 须为对象")
        files = package.get("files")
        if not isinstance(files, dict) or not files:
            raise BadRequest("package.files 须为非空对象 {路径: 文本}")
        if len(files) > MAX_FILES:
            raise BadRequest(f"技能文件最多 {MAX_FILES} 个")
        total = 0
        clean_files = {}
        for path, text in files.items():
            rel = _safe_relpath(path)
            if not isinstance(text, str):
                raise BadRequest(f"{rel} 的内容须为文本")
            size = len(text.encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise BadRequest(f"{rel} 超过 {MAX_FILE_BYTES} 字节")
            total += size
            clean_files[rel] = text
        if total > MAX_TOTAL_BYTES:
            raise BadRequest("技能包过大")
        # A path cannot be both a file and a directory ("a.py" and "a.py/b.py").
        all_paths = set(clean_files)
        for rel in clean_files:
            parts = PurePosixPath(rel).parts
            for i in range(1, len(parts)):
                prefix = "/".join(parts[:i])
                if prefix in all_paths:
                    raise BadRequest(f"技能文件路径冲突：{prefix} 既是文件又是目录")
        meta = {k: v for k, v in package.items() if k != "files"}
        try:
            spec = spec_manifest.validate_skill_package(meta)
        except (ValueError, KeyError, TypeError) as exc:
            raise BadRequest(f"技能包无效：{exc}") from None
        try:
            check_parameter_declarations(spec["parameters"])
        except BadRequest as exc:
            raise BadRequest(f"技能包无效：{exc}", code=exc.code, extra=exc.extra) from None
        entry_file, _attr = split_entry(spec["entry"])
        if entry_file not in clean_files:
            raise BadRequest(f"技能入口文件 {entry_file} 不在包内")
        compat = spec_manifest.check_compatibility(spec, self._manifest)
        if not compat["compatible"]:
            raise Incompatible(
                f"技能 {spec['id']} 与本体不兼容：缺少 {compat['missing']}，约束 {compat['violations']}",
                extra={"compatibility": compat},
            )
        digest = package_digest({"package": spec, "files": clean_files})
        key = f"{spec['id']}@{spec['version']}"
        with self._lock:
            existing = self._store.get("skills", key)
            if existing:
                if existing["digest"] != digest:
                    raise Conflict(f"{key} 已安装且内容不同；版本不可变，请提升版本号", code="version_immutable")
                return {**self.describe(existing), "installed": True, "already_installed": True}
            target = self._dir(spec["id"], spec["version"])
            effects.mark()
            staging = target.with_name(target.name + ".staging")
            if staging.exists():
                shutil.rmtree(staging)
            for rel, text in clean_files.items():
                dest = staging / "files" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            (staging / "package.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            record = {"id": key, "skill_id": spec["id"], "version": spec["version"], "package": spec,
                      "digest": digest, "files": sorted(clean_files), "installed_at": time.time()}
            self._store.put("skills", record)
        return {**self.describe(record), "installed": True, "already_installed": False}

    def describe(self, record: dict) -> dict:
        spec = record["package"]
        return {"id": spec["id"], "version": spec["version"], "ref": record["id"], "title": spec["title"],
                "description": spec["description"], "requires": spec["requires"], "optional": spec["optional"],
                "parameters": spec["parameters"], "timeout_s": spec["timeout_s"], "digest": record["digest"],
                "compatibility": spec_manifest.check_compatibility(spec, self._manifest),
                "installed_at": record["installed_at"]}

    def list(self) -> list[dict]:
        return [self.describe(r) for r in self._store.list("skills")]

    def resolve(self, ref: str) -> dict:
        """Resolve ``id`` (highest installed version) or ``id@version``."""
        if not isinstance(ref, str) or not ref:
            raise BadRequest("需要技能 ID")
        if "@" in ref:
            record = self._store.get("skills", ref)
            if not record:
                raise NotFound(f"未安装技能 {ref}", code="skill_not_installed")
            return record
        candidates = [r for r in self._store.list("skills") if r["skill_id"] == ref]
        if not candidates:
            raise NotFound(f"未安装技能 {ref}", code="skill_not_installed")
        return max(candidates, key=lambda r: _version_key(r["version"]))

    def files_dir(self, record: dict) -> Path:
        """Directory holding the installed package's files (inside the 0700 state dir)."""
        return self._dir(record["skill_id"], record["version"]) / "files"

    def load_entry(self, record: dict, tag: str) -> tuple[Callable, Callable[[], None]]:
        """Import the skill entry fresh for one run (``sandbox="inprocess"`` only).

        Returns ``(callable, cleanup)``.
        """
        base = self.files_dir(record)
        rel, attr = split_entry(record["package"]["entry"])
        pkg_name = "_nx_skill_" + re.sub(r"[^0-9a-zA-Z_]", "_", tag)

        def cleanup() -> None:
            for name in [n for n in sys.modules if n == pkg_name or n.startswith(pkg_name + ".")]:
                sys.modules.pop(name, None)

        try:
            root = types.ModuleType(pkg_name)
            root.__path__ = [str(base)]
            sys.modules[pkg_name] = root
            parts = PurePosixPath(rel).with_suffix("").parts
            parent, parent_dir = pkg_name, base
            for part in parts[:-1]:           # namespace modules for sub-directories
                parent_dir = parent_dir / part
                sub = types.ModuleType(f"{parent}.{part}")
                sub.__path__ = [str(parent_dir)]
                sys.modules[sub.__name__] = sub
                parent = sub.__name__
            mod_name = f"{parent}.{parts[-1]}"
            spec = importlib.util.spec_from_file_location(mod_name, base / rel)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            target = getattr(module, attr)
        except Exception:
            cleanup()
            raise
        if isinstance(target, type):
            return target().run, cleanup
        if not callable(target):
            cleanup()
            raise SkillError(f"入口 {attr} 不可调用")
        return target, cleanup


# ------------------------------------------------------------ context
class SkillContext:
    """The API a skill receives. All capability access is safety supervised."""

    def __init__(self, run, bus, allowed: set[str], params: dict, stop_fn: Callable[[str], dict],
                 models: dict | None = None):
        self._run = run
        self._models = copy.deepcopy(models or {})
        self._bus = bus
        self._allowed = set(allowed) | {"safety.stop", "system.health"}
        self._stop_fn = stop_fn
        self._guards: list[Callable[[], None]] = []
        self.params = dict(params)
        self.run_id = run.id

    @property
    def stop_requested(self) -> bool:
        """A stop, cancel or timeout was requested for this run."""
        return self._run.stop_event.is_set()

    def add_guard(self, guard: Callable[[], None]) -> None:
        """Extra "may this run still act?" check (raises SkillInterrupted).

        Runs in :meth:`check_stop`, i.e. before every call/read and again right
        before each adapter dispatch. The process sandbox adds "the skill
        process is still alive" so a request of an exited child is void.
        """
        self._guards.append(guard)

    # ---- data handed to a sandboxed skill process (all public information) ----
    @property
    def declared(self) -> frozenset:
        """Capabilities this skill may use (requires + optional + always-allowed)."""
        return frozenset(self._allowed)

    @property
    def body_capabilities(self) -> frozenset:
        return frozenset(self._bus.capability_ids)

    def all_params(self) -> dict:
        """Every manifest parameter, as plain (thawed) JSON values."""
        return {cap: {name: thaw(value) for name, value in params.items()}
                for cap, params in self._bus.supervisor.params.items()}

    # ---- introspection ----------------------------------------------------
    @property
    def manifest(self) -> dict:
        return self._bus.manifest_copy()

    @property
    def simulated(self) -> bool:
        return self._bus.manifest["simulated"]

    @property
    def models(self) -> dict:
        """Installed on-device models (§5.1), read-only: ``{model_id: {name, runtime,
        sha256, size, file, path}}``. ``path`` is a read-only file the skill may open."""
        return copy.deepcopy(self._models)

    def model_view(self) -> dict:
        return copy.deepcopy(self._models)

    def has(self, cap_id: str) -> bool:
        """True if the body provides ``cap_id`` and this skill declared it."""
        return cap_id in self._allowed and cap_id in self._bus.capability_ids

    def param(self, cap_id: str, name: str, default=None):
        """A manifest parameter, e.g. ``ctx.param("base.velocity", "max_vx")``."""
        value = self._bus.supervisor.params.get(cap_id, {}).get(name, default)
        return thaw(value)        # plain lists; the supervisor's frozen limits stay untouchable

    @property
    def stopped(self) -> bool:
        if (self._run.stop_event.is_set() or self._run.revoked
                or getattr(self._run, "epoch", None) not in (None, self._bus.supervisor.epoch)):
            return True
        try:
            for guard in self._guards:
                guard()
        except SkillInterrupted:
            return True
        return False

    def time_left(self) -> float | None:
        if self._run.deadline is None:
            return None
        return max(0.0, self._run.deadline - self._bus.clock.now())

    # ---- control ------------------------------------------------------------
    def check_stop(self) -> None:
        """Raise SkillStopped / SkillCancelled / SkillTimeout if the run must end."""
        if self._run.revoked:
            raise SkillStopped("该运行已结束，能力访问已撤销")
        if self._run.stop_event.is_set():
            reason = self._run.stop_reason
            if reason == "timeout":
                raise SkillTimeout("技能运行超时")
            if reason == "cancelled":
                raise SkillCancelled("技能运行已取消")
            raise SkillStopped("已收到停止指令")
        epoch = getattr(self._run, "epoch", None)
        if epoch is not None and epoch != self._bus.supervisor.epoch:
            # A stop happened since this run was admitted: the run is void forever,
            # even after a reset.
            raise SkillStopped("该运行开始后发生过停止；运行已失效")
        for guard in self._guards:
            guard()

    def _guard(self) -> None:
        """Passed to the capability bus; re-checked right before each adapter dispatch."""
        self.check_stop()

    @property
    def _epoch(self):
        return getattr(self._run, "epoch", None)

    def sleep(self, seconds: float) -> None:
        """Sleep in robot time; wakes immediately on stop/cancel/timeout."""
        self.check_stop()
        if self._bus.clock.sleep(seconds, self._run.stop_event):
            self.check_stop()
        self.check_stop()

    def log(self, message: str, **data: Any) -> None:
        if self._run.revoked:
            return          # a finalised run's record is immutable
        self._run.add_event("log", message=str(message)[:2000], **data)

    def progress(self, fraction: float, message: str | None = None) -> None:
        if self._run.revoked:
            return
        self._run.progress = max(0.0, min(1.0, float(fraction)))
        if message:
            self._run.add_event("progress", progress=self._run.progress, message=message)

    # ---- capabilities -------------------------------------------------------
    def _check_declared(self, cap_id: str) -> str:
        try:
            cap_id = spec_caps.normalize(cap_id)
        except ValueError as exc:
            raise SkillError(str(exc)) from None
        if cap_id not in self._allowed:
            raise SkillError(f"技能未在 requires/optional 中声明能力 {cap_id}")
        if cap_id not in self._bus.capability_ids:
            raise SkillError(f"本体未提供能力 {cap_id}（可选能力请先用 ctx.has 判断）")
        return cap_id

    def read(self, cap_id: str) -> dict:
        self.check_stop()
        cap_id = self._check_declared(cap_id)
        out = self._bus.read(cap_id)
        self.check_stop()       # a read that blocked across a stop is not returned
        return out

    def call(self, cap_id: str, **args: Any) -> dict:
        """Invoke any capability. Commands return outputs; actions block until done."""
        self.check_stop()
        cap_id = self._check_declared(cap_id)
        if cap_id == "safety.stop":
            result = self._stop_fn(str(args.get("reason", f"skill {self._run.target} 调用 safety.stop")))
            return {"stopped": bool(result.get("stopped", True))}
        kind = self._bus.kind(cap_id)
        if kind in ("state", "stream"):
            out = self._bus.read(cap_id)
            self.check_stop()
            return out
        motion = is_motion(cap_id)
        if motion:
            self._run.used_motion = True
        if kind == "command":
            out = self._bus.invoke(cap_id, args, authorized=self._run.authorized,
                                   epoch=self._epoch, guard=self._guard)
            if motion and self.stopped:
                # Stop/cancel raced with this setpoint: undo it before raising.
                self._bus.supervisor.halt_motion("运行已停止，撤销在途指令")
            self.check_stop()
            return out
        return self._run_action(cap_id, args)

    def _run_action(self, cap_id: str, args: dict) -> dict:
        handle = self._bus.start_action(cap_id, args, authorized=self._run.authorized,
                                        epoch=self._epoch, guard=self._guard, run_id=self.run_id)
        self._run.handles.add(handle)
        try:
            while True:
                status = handle.poll()
                if status.done:
                    break
                detail = {"capability": cap_id, "progress": status.progress}
                if status.feedback:
                    detail["feedback"] = dict(status.feedback)
                self._run.progress_detail = detail
                if self._run.kind == "capability":
                    # A directly invoked action (e.g. model.install): the run's own progress is the
                    # action's (iter03 leftover: it stayed 0 until the end). Skills set theirs via ctx.progress.
                    self._run.progress = _clamp_progress(status.progress)
                if self._run.stop_event.wait(POLL_INTERVAL_S) or self.stopped:
                    handle.cancel()
                    self.check_stop()
        finally:
            self._run.handles.discard(handle)
        if status.state == SUCCEEDED:
            return status.result or {}
        self.check_stop()     # cancelled because of our own stop → interrupt
        if status.state == CANCELLED:
            raise ActionFailed(f"{cap_id} 被取消：{status.error or ''}")
        raise ActionFailed(f"{cap_id} 失败：{status.error or '未知原因'}")
