"""Skill package tools: build from a directory, validate locally, explain compatibility.

A skill directory looks like::

    my-skill/
      skill.json          # skill.package.v1 (spec/neuxmind_spec/manifest.py)
      skill.py            # the entry, e.g. "entry": "skill.py:run"
      helpers/*.py        # any other text files the skill needs
      tests/              # NOT shipped (local tests only)
      README.md

:func:`build_package` produces exactly what ``POST /v2/skills/install``
expects: the skill.json fields plus ``files: {relative path: text}``.
Compatibility is decided only by ``neuxmind_spec.manifest.check_compatibility``
(the same rule runtime, cloud and desktop use); :func:`explain_compatibility`
adds human-readable reasons on top of it.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from . import _bootstrap
from .errors import NeuxMindError

# Mirrors the limits enforced by runtime/neuxmind_runtime/skills.py so that
# problems are reported before upload. The runtime remains the authority.
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1536 * 1024
EXCLUDED_DIRS = {"tests", "__pycache__", ".git", ".venv", "node_modules"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def _invalid(message: str) -> NeuxMindError:
    return NeuxMindError(message, code="invalid_package")


def read_skill_json(skill_dir: str | Path) -> dict:
    path = Path(skill_dir) / "skill.json"
    if not path.is_file():
        raise _invalid(f"{Path(skill_dir)} 下没有 skill.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _invalid(f"skill.json 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise _invalid("skill.json 须为 JSON 对象")
    return data


def _shipped(rel: PurePosixPath) -> bool:
    if any(part.startswith(".") for part in rel.parts):
        return False
    if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
        return False
    return rel.suffix not in EXCLUDED_SUFFIXES and str(rel) != "skill.json"


def build_package(skill_dir: str | Path) -> dict:
    """Build an installable package dict (``skill.json`` + text ``files``).

    Excluded: ``skill.json`` itself, ``tests/``, hidden files/directories,
    ``__pycache__`` and compiled Python. Every shipped file must be UTF-8 text.
    """
    root = Path(skill_dir)
    package = read_skill_json(root)
    package.pop("files", None)
    files: dict[str, str] = {}
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        rel = PurePosixPath(file.relative_to(root).as_posix())
        if not _shipped(rel):
            continue
        try:
            files[str(rel)] = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise _invalid(f"{rel} 不是 UTF-8 文本；技能包只能包含文本文件") from None
    package["files"] = files
    return package


def _spec_fields(decl: dict):
    caps = _bootstrap.spec().capabilities
    return caps.Field(name=str(decl.get("name")), type=decl.get("type"), length=decl.get("length"),
                      choices=tuple(decl.get("choices", ())))


def validate_package(package: dict) -> dict:
    """Validate a package locally; return the normalised ``skill.package.v1`` metadata.

    Checks the spec (``validate_skill_package``), the entry format and that the
    entry file is in ``files``, file paths and size limits, and that parameter
    ``default`` values match their declared type (``check_field``).
    """
    spec = _bootstrap.spec()
    if not isinstance(package, dict):
        raise _invalid("技能包须为对象")
    meta = {k: v for k, v in package.items() if k != "files"}
    try:
        skill = spec.manifest.validate_skill_package(meta)
    except (ValueError, KeyError, TypeError) as exc:
        raise _invalid(f"skill.json 无效：{exc}") from None

    module, sep, attr = skill["entry"].partition(":")
    if not sep or not module or not attr.isidentifier():
        raise _invalid("entry 须为 '<文件>.py:<函数或类名>'，如 skill.py:run")
    entry_file = module if module.endswith(".py") else module.replace(".", "/") + ".py"

    files = package.get("files")
    if not isinstance(files, dict) or not files:
        raise _invalid("技能包没有文件（files 为空）")
    if len(files) > MAX_FILES:
        raise _invalid(f"技能文件最多 {MAX_FILES} 个，当前 {len(files)} 个")
    total = 0
    for path, text in files.items():
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if (pure is None or not path or "\\" in path or pure.is_absolute()
                or any(part in ("", ".", "..") for part in pure.parts)):
            raise _invalid(f"技能文件路径无效：{path!r}")
        if not isinstance(text, str):
            raise _invalid(f"{path} 的内容须为文本")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise _invalid(f"{path} 有 {size} 字节，超过单文件上限 {MAX_FILE_BYTES}")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise _invalid(f"技能包共 {total} 字节，超过上限 {MAX_TOTAL_BYTES}")
    if entry_file not in files:
        raise _invalid(f"入口文件 {entry_file} 不在技能包内（entry={skill['entry']}）")

    for decl in skill["parameters"]:
        if "default" in decl:
            try:
                spec.capabilities.check_field(_spec_fields(decl), decl["default"], where="parameters")
            except ValueError as exc:
                raise _invalid(f"参数 {decl['name']} 的默认值与类型不符：{exc}") from None
    return skill


def load_skill(skill_dir: str | Path) -> tuple[dict, dict]:
    """Build and validate a directory → ``(package, normalised_metadata)``."""
    package = build_package(skill_dir)
    return package, validate_package(package)


# ------------------------------------------------------------------ compatibility
def _cap_title(cap_id: str) -> str:
    caps = _bootstrap.spec().capabilities
    cap = caps.CAPABILITIES.get(cap_id)
    return f"{cap_id}（{cap.title}）" if cap else cap_id


def _describe_rule(cap_id: str, pname: str, rule: Any, value: Any) -> str | None:
    """Explain why one constraint fails, or ``None`` if it holds."""
    label = f"{cap_id} 的参数 {pname}"
    if value is None:
        return f"{label}：技能要求 {json.dumps(rule, ensure_ascii=False)}，但本体没有声明这个参数"
    if not isinstance(rule, dict):
        return None
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    problems = []
    if "min" in rule and not (numeric and value >= rule["min"]):
        problems.append(f"需要 ≥ {rule['min']}")
    if "max" in rule and not (numeric and value <= rule["max"]):
        problems.append(f"需要 ≤ {rule['max']}")
    if "equals" in rule and value != rule["equals"]:
        problems.append(f"需要 = {json.dumps(rule['equals'], ensure_ascii=False)}")
    if not problems:
        return None
    return f"{label}{'，'.join(problems)}，本体声明为 {json.dumps(value, ensure_ascii=False)}"


def explain_compatibility(skill: dict, manifest: dict) -> dict:
    """``check_compatibility`` plus human-readable explanations.

    Returns the spec result (``compatible, missing, violations, optional_missing``)
    with extra keys ``skill``, ``robot``, ``simulated``, ``reasons`` (list of str)
    and ``notes`` (non-blocking remarks).
    """
    spec = _bootstrap.spec()
    result = dict(spec.manifest.check_compatibility(skill, manifest))
    have = {c["id"]: c for c in manifest.get("capabilities", []) if isinstance(c, dict)}
    reasons = [f"缺少必需能力 {_cap_title(cid)}" for cid in result["missing"]]
    for cid, rules in (skill.get("constraints") or {}).items():
        cap = have.get(cid)
        if not cap or not isinstance(rules, dict):
            continue
        for pname, rule in rules.items():
            text = _describe_rule(cid, pname, rule, cap.get("params", {}).get(pname))
            if text:
                reasons.append(text)
    if not result["compatible"] and len(reasons) < len(result["missing"]) + len(result["violations"]):
        reasons.extend(v for v in result["violations"] if v not in reasons)   # never hide a spec verdict
    notes = [f"可选能力 {_cap_title(cid)} 本体没有，技能会跳过相关功能" for cid in result["optional_missing"]]
    if manifest.get("simulated"):
        notes.append("这是仿真本体（simulated=true）：结果只证明协议与逻辑，不能作为现场证据")
    result.update(skill=f"{skill.get('id')}@{skill.get('version')}",
                  robot=manifest.get("name"), instance_id=manifest.get("instance_id"),
                  family=manifest.get("family"), simulated=manifest.get("simulated") is True,
                  reasons=reasons, notes=notes)
    return result


def format_compatibility(report: dict, robot_label: str | None = None) -> str:
    """Render :func:`explain_compatibility` output as Chinese text for the CLI."""
    sim = "，仿真本体" if report.get("simulated") else ""
    if robot_label:
        head = f"技能 {report['skill']} → {robot_label}（{report.get('robot')}，{report.get('family')}{sim}）"
    else:
        head = f"技能 {report['skill']} → {report.get('robot')}（{report.get('family')}{sim}）"
    lines = [head, "  结果：兼容" if report["compatible"] else "  结果：不兼容"]
    for reason in report.get("reasons", []):
        lines.append(f"    - {reason}")
    for note in report.get("notes", []):
        lines.append(f"    · {note}")
    return "\n".join(lines)
