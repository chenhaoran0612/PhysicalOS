"""The cloud skill store (issue #43, docs/CLOUD_SKILLS.md §1): skill packages kept per organisation.

A release is one version of one skill (``skill.package.v1`` with its files). It is validated with the
shared spec, bounded like the robot bounds a package at install (``runtime/neuxmind_runtime/skills.py``;
``sdk/neuxmind/package.py`` has the same numbers) and stored with the digest the robot computes when it
installs it, so that whoever downloads a release can tell it is the one that was published.

Releases are immutable: the same ``id@version`` with the same content answers with the first release,
with other content it is refused. Nothing here runs or imports a package; its files are text that is
stored and handed back.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, undefer

from . import audit, skill_params
from .auth import Principal
from .config import Settings
from .db import advisory_lock
from .errors import ApiError, bad_request, conflict, forbidden, not_found
from .skill_models import SkillRelease
from .spec_bridge import manifest as spec_manifest

READ, WRITE = "skills:read", "skills:write"
# The robot's own limits (runtime/neuxmind_runtime/skills.py); a test keeps the two in step.
MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 1536 * 1024
MAX_PATH_CHARS = 200
MAX_SPEC_BYTES = 256 * 1024         # skill.json without the files (the console's own bound)
MAX_NOTES = 2000

_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,99}")
_VERSION = re.compile(r"(\d{1,9})\.(\d{1,9})\.(\d{1,9})([\-+][0-9A-Za-z.\-]{1,40})?")


def invalid_package(message: str) -> ApiError:
    return bad_request(f"技能包无效：{message}", code="invalid_skill_package")


def skill_not_found() -> ApiError:
    return not_found("云端技能库里没有这个技能或这个版本", code="skill_not_found")


def _writer(p: Principal) -> Principal:
    """Publishing and deleting need the write permission and a verified e-mail address (anyone can register)."""
    p.require(WRITE)
    if not p.user.email_verified:
        raise forbidden("请先验证邮箱，之后才能发布或删除技能（POST /auth/verify-email）", code="email_unverified")
    return p


# ---------------------------------------------------------------- the package
def package_digest(spec: dict, files: dict) -> str:
    """The digest the robot records at install: over the validated package and its files."""
    document = {"package": spec, "files": files}
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _safe_path(path) -> str:
    if (not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or "\\" in path
            or not skill_params.storable(path) or "\n" in path or "\t" in path):
        raise ValueError(f"文件路径无效：{str(path)[:80]!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"文件路径无效：{path[:80]!r}")
    return str(pure)


def _clean_files(files) -> tuple[dict, int]:
    """``({path: text}, total bytes)``; ``ValueError`` when the files are not what a robot would install."""
    if not isinstance(files, dict) or not files:
        raise ValueError("files 须为非空对象 {路径: 文本}")
    if len(files) > MAX_FILES:
        raise ValueError(f"技能文件最多 {MAX_FILES} 个")
    clean, total = {}, 0
    for path, text in files.items():
        rel = _safe_path(path)
        if not isinstance(text, str):
            raise ValueError(f"{rel} 的内容须为文本")
        if not skill_params.encodable(text):
            raise ValueError(f"{rel} 含有空字符或不是有效的文本")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{rel} 有 {size} 字节，超过单文件上限 {MAX_FILE_BYTES}")
        if rel in clean:
            raise ValueError(f"文件路径重复：{rel}")
        total += size
        clean[rel] = text
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"技能包共 {total} 字节，超过上限 {MAX_TOTAL_BYTES}")
    for rel in clean:
        parts = PurePosixPath(rel).parts
        if any("/".join(parts[:i]) in clean for i in range(1, len(parts))):
            raise ValueError(f"文件路径冲突：{rel} 的上级既是文件又是目录")
    return clean, total


def _entry_file(entry: str) -> str:
    module, sep, attr = entry.partition(":")
    if not sep or not module or not attr.isidentifier():
        raise ValueError("entry 须为 '<文件>.py:<函数或类名>'")
    return _safe_path(module if module.endswith(".py") else module.replace(".", "/") + ".py")


def is_motion(spec: dict) -> bool:
    """Does the skill use anything that can move the robot (or whose effect is unknown)?"""
    return any(spec_manifest.is_motion(item["id"]) for item in (*spec["requires"], *spec["optional"]))


def check_package(package) -> dict:
    """Validate an uploaded package. Returns what a release stores; refuses with ``invalid_skill_package``."""
    if not isinstance(package, dict):
        raise invalid_package("package 须为对象（skill.package.v1，含 files）")
    meta = {key: value for key, value in package.items() if key != "files"}
    problem = skill_params.json_problem(meta, MAX_SPEC_BYTES)
    if problem is not None:
        raise invalid_package(f"skill.json {problem}")
    try:
        files, total = _clean_files(package.get("files"))
        spec = spec_manifest.validate_skill_package(meta)
        if not _SKILL_ID.fullmatch(spec["id"]) or not is_version(spec["version"]):
            raise ValueError("技能 ID 或版本号的写法云端技能库不接受（ID 最多 100 个字符；版本形如 1.2.3 或 1.2.3-rc.1）")
        skill_params.check_declarations(spec["parameters"])
        if _entry_file(spec["entry"]) not in files:
            raise ValueError(f"入口文件不在包内（entry={spec['entry']}）")
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        raise invalid_package(str(exc)) from None
    return {"skill_id": spec["id"], "version": spec["version"], "title": spec["title"],
            "description": spec["description"], "digest": package_digest(spec, files), "motion": is_motion(spec),
            "size": total, "file_index": [{"path": path, "size": len(files[path].encode("utf-8"))} for path in sorted(files)],
            "spec": spec, "files": files}


def _notes(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_NOTES or not skill_params.storable(value):
        raise bad_request(f"notes 须为不超过 {MAX_NOTES} 个字符的文字，不含控制字符")
    return value.strip()


# ---------------------------------------------------------------- views
def release_view(release: SkillRelease) -> dict:
    spec = release.spec
    return {"skill_id": release.skill_id, "version": release.version, "title": release.title,
            "description": release.description, "digest": release.digest,
            "requires": [item["id"] for item in spec["requires"]], "optional": [item["id"] for item in spec["optional"]],
            "parameters": spec["parameters"], "motion": bool(release.motion), "timeout_s": spec["timeout_s"],
            "files": release.file_index, "size": release.size, "notes": release.notes,
            "created_by": release.created_by, "created_at": release.created_at}


def is_version(version: str) -> bool:
    return len(version) <= 50 and _VERSION.fullmatch(version) is not None


def version_key(version: str) -> tuple:
    """Orders versions as versions (``0.10.0`` after ``0.9.0``); a pre-release sorts before its release."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return (0, 0, 0, 0, version)
    suffix = match.group(4) or ""
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if suffix.startswith("-") else 1, suffix)


def _newest_first(releases: list[SkillRelease]) -> list[SkillRelease]:
    return sorted(releases, key=lambda r: (version_key(r.version), r.created_at), reverse=True)


def _own_release(session: Session, p: Principal, skill_id: str, version: str, *, with_files: bool = False,
                 lock: bool = False) -> SkillRelease:
    if not _SKILL_ID.fullmatch(skill_id) or not _VERSION.fullmatch(version):
        raise skill_not_found()                              # not a name of ours: no query for it
    query = select(SkillRelease).where(SkillRelease.org_id == p.org_id, SkillRelease.skill_id == skill_id,
                                       SkillRelease.version == version)
    if with_files:
        query = query.options(undefer(SkillRelease.files))
    release = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if release is None:
        raise skill_not_found()
    return release


# ---------------------------------------------------------------- operations
def create_release(session: Session, p: Principal, payload, settings: Settings, ip: str = "") -> tuple[dict, bool]:
    """``POST /skills/releases``. ``(release, created)``; the same content again is not created twice."""
    _writer(p)
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象 {package, notes?}")
    notes = _notes(payload.get("notes"))
    fields = check_package(payload.get("package"))
    advisory_lock(session, f"skills:{p.org_id}")
    existing = session.execute(select(SkillRelease).where(
        SkillRelease.org_id == p.org_id, SkillRelease.skill_id == fields["skill_id"],
        SkillRelease.version == fields["version"])).scalar_one_or_none()
    if existing is not None:
        return _existing(existing, fields), False
    count = session.execute(select(func.count()).select_from(SkillRelease)
                            .where(SkillRelease.org_id == p.org_id)).scalar_one()
    if count >= settings.skill_org_max_releases:
        raise conflict(f"本组织的云端技能已达上限（{settings.skill_org_max_releases} 条），请先删除不用的版本",
                       code="skill_quota_exceeded")
    release = SkillRelease(org_id=p.org_id, notes=notes, created_by=p.actor, **fields)
    session.add(release)
    try:
        session.flush()
    except IntegrityError:                                   # another request published it meanwhile
        session.rollback()
        raise conflict("这个版本刚刚已被发布，请重试", code="skill_version_exists") from None
    audit.record(session, "skill.release.create", p.actor, org_id=p.org_id, ip=ip,
                 target=f"{release.skill_id}@{release.version}",
                 detail={"digest": release.digest, "size": release.size, "motion": release.motion})
    return release_view(release), True


def _existing(release: SkillRelease, fields: dict) -> dict:
    if release.digest != fields["digest"]:
        raise ApiError(409, "skill_version_exists",
                       f"{release.skill_id}@{release.version} 已经发布过，内容不同；版本不可变，请提升版本号",
                       digest=release.digest)
    return {**release_view(release), "existing": True}


def list_skills(session: Session, p: Principal) -> dict:
    """``GET /skills``: every skill with its latest release and its versions (newest first), by id."""
    p.require(READ)
    grouped: dict[str, list[SkillRelease]] = {}
    for release in session.execute(select(SkillRelease).where(SkillRelease.org_id == p.org_id)).scalars():
        grouped.setdefault(release.skill_id, []).append(release)
    skills = []
    for skill_id in sorted(grouped):
        releases = _newest_first(grouped[skill_id])
        latest = releases[0]
        skills.append({"id": skill_id, "title": latest.title, "description": latest.description,
                       "latest": release_view(latest), "versions": [r.version for r in releases]})
    return {"skills": skills}


def get_skill(session: Session, p: Principal, skill_id: str) -> dict:
    p.require(READ)
    releases = []
    if _SKILL_ID.fullmatch(skill_id):
        releases = session.execute(select(SkillRelease).where(SkillRelease.org_id == p.org_id,
                                                              SkillRelease.skill_id == skill_id)).scalars().all()
    if not releases:
        raise skill_not_found()
    return {"id": skill_id, "releases": [release_view(r) for r in _newest_first(releases)]}


def get_release(session: Session, p: Principal, skill_id: str, version: str) -> dict:
    p.require(READ)
    return release_view(_own_release(session, p, skill_id, version))


def get_package(session: Session, p: Principal, skill_id: str, version: str) -> dict:
    """``GET …/package``: the release and the package as it installs (the validated ``skill.json`` plus files)."""
    p.require(READ)
    release = _own_release(session, p, skill_id, version, with_files=True)
    return {"release": release_view(release), "package": {**release.spec, "files": release.files}}


def delete_release(session: Session, p: Principal, skill_id: str, version: str, ip: str = "") -> dict:
    _writer(p)
    release = _own_release(session, p, skill_id, version, lock=True)
    session.delete(release)
    audit.record(session, "skill.release.delete", p.actor, org_id=p.org_id, ip=ip,
                 target=f"{release.skill_id}@{release.version}", detail={"digest": release.digest})
    return {"deleted": True}
