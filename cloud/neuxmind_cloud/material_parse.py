"""Parsing the library's materials (issue #34, R1-13), and people checking and confirming the result.

A finished upload (issue #33) leaves a material in 「解析中」 (``parsing``). A
worker thread of the cloud process finds it and runs its job, one step at a time:

* URDF and driver packages: the rules parser reads the file in a child process
  with hard limits (``material_sandbox``) and the result is stored at once.
* Documents (Word, Excel, PDF, text): Office files are first checked by the
  same child process (macros are noted; a document with external references is
  not passed on). Then a copy goes to the knowledge service (``weknora``), which
  parses and indexes it; when it is done, its model is asked three questions
  limited to that one document, and the answers fill the items of R1-13.

Every step is short and can be repeated: a worker holds a job only under a
lease, so a crashed process or a second server process never runs the same step
twice at a time, and a step whose lease ran out is simply run again. A service
that cannot be reached is tried again later (with growing pauses) and after
:data:`MAX_ATTEMPTS` tries the material ends in 「没解析出来」 with the reason.

Results are only ever data: shown to a person, edited, and confirmed. Nothing
here (or anywhere in the cloud) runs an install step. A complete result is
「待确认」 (``parsed``); one that lacks items is 「没解析出来」 (``failed``) and
names them, except a document the knowledge service has indexed, which is
「已入库」 (``stored``, issue #45): the library keeps information, and what is
in the knowledge base can be searched whatever the model could read from it.
Only a person's confirmation makes a result 「可用」 (``usable``). Who
edited and who confirmed, and when, is kept with the result and in the audit log.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from . import audit, library, weknora
from . import material_fields as fields
from . import library_bundle      # issue #43: a bundle is never parsed
from . import library_model       # issue #51: a robot model is read as a URDF, also from an archive
from .config import Settings
from .db import Database
from .errors import ApiError, bad_request, conflict
from .library_models import PARSE_FAILED, PARSED, PARSING, STORED, USABLE, Material
from .library_store import LibraryStore
from .material_parse_models import ASKING, DONE, INDEXING, QUEUED, MaterialParse
from .material_sandbox import ExtractFailed, Limits, extract
from .models import now

log = logging.getLogger("neuxmind_cloud.material_parse")
ACTOR = "system:parser"
MAX_ATTEMPTS = 6
MAX_RESENDS = 2              # the document is sent again at most this often when its copy goes away
RETRY_WAITS = (30, 60, 120, 300, 600, 900)
INDEX_POLLS = (3, 5, 10, 20, 30, 60)
CLEANUP_INTERVAL = 60.0
CLEANUP_BATCH = 50
OFFICE = ("docx", "xlsx", "doc", "xls")
# What the knowledge service is told a file is (it accepts these extensions: WeKnora internal/utils).
SERVICE_EXTENSIONS = {"docx": "docx", "doc": "doc", "xlsx": "xlsx", "xls": "xls", "pdf": "pdf", "markdown": "md",
                      "text": "txt", "yaml": "txt", "json": "txt", "xml": "txt"}
# Issue #45: the knowledge service has no reader for HTML files (WeKnora 0.6.3: only .mhtml and web addresses),
# so a page is sent as the text the rules parser takes out of it, under this extension.
CONVERTED = {"html": "md"}
SERVICE_EXTENSIONS.update(CONVERTED)
NO_VALUE = {"null", "none", "n/a", "na", "无", "没有", "暂无", "未提及", "未说明", "未知", "不详", "-", "—", "未提供",
            "资料中未提及", "资料未提及", "文档未提及"}

QUESTIONS = (
    (("driver_name", "driver_version", "source", "os", "ros2_distro"),
     "只根据这份资料回答，不要猜测，也不要用资料以外的知识。用一个 JSON 对象回答，不要输出别的文字。字段："
     "\"driver_name\"：资料介绍的驱动或软件包的名称；\"driver_version\"：它的版本；"
     "\"source\"：从哪里获取它（网址、软件源、代码仓库或安装包文件名）；"
     "\"os\"：适用的操作系统和版本，字符串数组；\"ros2_distro\"：适用的 ROS 2 发行版（例如 humble），字符串数组。"
     "资料里没有写的字段填 null。"),
    (("install_steps",),
     "只根据这份资料回答，不要猜测。用一个 JSON 对象回答，不要输出别的文字："
     "{\"install_steps\": [按顺序列出安装这个驱动的每一步，每一步是一句话或一条命令，照资料原文]}。资料里没有安装步骤时填 null。"),
    (("topics", "actions"),
     "只根据这份资料回答，不要猜测。用一个 JSON 对象回答，不要输出别的文字："
     "{\"topics\": [驱动装好并运行后应该出现的 ROS 2 话题，每项 {\"name\": 话题名, \"type\": 消息类型}], "
     "\"actions\": [应该出现的 ROS 2 动作，每项 {\"name\": 动作名, \"type\": 动作类型}]}。资料里没有写的填 null。"),
)


# ------------------------------------------------------------------ outcomes of one step
@dataclass
class _Next:
    stage: str
    delay: float = 0.0
    changes: dict = field(default_factory=dict)


@dataclass
class _Finish:
    values: dict
    parser: str
    warnings: list = field(default_factory=list)
    error: str = ""
    extracted: dict = field(default_factory=dict)
    knowledge_id: str | None = None


@dataclass
class _Retry:
    message: str


@dataclass
class _Job:
    material_id: str
    org_id: str
    kind: str
    stage: str
    attempts: int
    stage_since: float
    knowledge_id: str
    format: str
    size: int
    sha256: str
    filename: str
    device_id: str
    material_kind: str = ""          # issue #51


def _limits(settings: Settings) -> Limits:
    return Limits(wall_seconds=settings.library_parse_wall_seconds, cpu_seconds=settings.library_parse_cpu_seconds,
                  memory_bytes=settings.library_parse_memory_bytes,
                  unpacked_bytes=settings.library_parse_unpacked_bytes)


def _lease_seconds(settings: Settings, stage: str) -> float:
    if stage == ASKING:
        return settings.weknora_answer_seconds * (len(QUESTIONS) + 1) + 120
    return settings.library_parse_wall_seconds + 660          # the extraction, or one upload of up to 10 minutes


# ------------------------------------------------------------------ turning findings into values
def _urdf_values(facts: dict) -> tuple[dict, list]:
    joints = []
    for joint in facts.get("joints") or []:
        if not isinstance(joint, dict):
            continue
        raw = joint.get("raw") if isinstance(joint.get("raw"), dict) else {}
        kept = {k: v for k, v in raw.items() if fields.number(v) is None}
        joints.append({**{k: joint.get(k) for k in ("name", "type", "parent", "child", *fields.LIMIT_KEYS)},
                       **({"raw": kept} if kept else {})})
    values = {"robot_name": facts.get("robot_name"), "links": facts.get("links"), "joints": joints}
    return values, [w for w in facts.get("warnings") or [] if isinstance(w, str)]


def _package_values(fmt: str, facts: dict, sha256: str) -> tuple[dict, list]:
    warnings = [w for w in facts.get("warnings") or [] if isinstance(w, str)]
    metadata = facts.get("metadata") if isinstance(facts.get("metadata"), dict) else {}
    control = facts.get("control") if fmt == "deb" else metadata.get("control")
    control = control if isinstance(control, dict) else {}
    manifest = metadata.get("package.xml") if isinstance(metadata.get("package.xml"), dict) else {}
    name = control.get("Package") or manifest.get("name")
    version = control.get("Version") or manifest.get("version")
    machines = facts.get("machines") if isinstance(facts.get("machines"), dict) else {}
    architecture = [control["Architecture"]] if control.get("Architecture") else \
        sorted(m for m in machines if m != "other")
    notes = "；".join(f"{label}：{control[key]}" for key, label in (("Description", "说明"), ("Homepage", "主页"),
                                                                   ("Depends", "依赖")) if control.get(key))
    if not notes and manifest.get("description"):
        notes = f"说明：{manifest['description']}"
    found = "control" if control else "package.xml" if manifest else ""
    if not control and not manifest:
        warnings.append("包里没有找到写明名称和版本的文件（Debian 的 control 或 ROS 的 package.xml），请手工填写")
    if not control.get("Architecture") and architecture:
        warnings.append(f"适用架构是按包里程序的机器类型判断的（{'、'.join(architecture)}），请核对")
    return ({"sha256": sha256, "package_name": name, "package_version": version, "architecture": architecture,
             "notes": notes}, warnings + ([f"名称和版本取自包里的 {found}"] if found else []))


def _answer_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _drop_no_value(value, depth: int = 0):
    """``value`` without the answers that mean "not stated"; nesting deeper than any item has is dropped."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return None if value.strip().lower().strip("。.") in NO_VALUE else value
    if isinstance(value, list):
        return [item for item in (_drop_no_value(v, depth + 1) for v in value[:fields.MAX_ITEMS * 4])
                if item not in (None, "")]
    if isinstance(value, dict):
        return {k: _drop_no_value(v, depth + 1) for k, v in list(value.items())[:20] if isinstance(k, str)}
    return value


# ------------------------------------------------------------------ the steps
def _rules_step(job: _Job, settings: Settings, store: LibraryStore) -> _Finish:
    mode = "urdf" if job.kind == fields.URDF else job.format
    mode = library_model.extract_mode(job.material_kind, job.format, mode)
    try:
        facts = extract(mode, store.object_path(job.org_id, job.sha256), _limits(settings))
    except ExtractFailed as exc:
        if exc.detail:
            log.warning("rules parser for %s: %s (%s)", job.material_id, exc.code, exc.detail[-500:])
        values = {"sha256": job.sha256} if job.kind == fields.PACKAGE else {}
        return _Finish(values, "rules", error=exc.message)
    except FileNotFoundError:
        return _Finish({}, "rules", error="这份资料的文件已经不在云端")
    if job.kind == fields.URDF:
        values, warnings = _urdf_values(facts)
    else:
        values, warnings = _package_values(job.format, facts, job.sha256)
    return _Finish(values, "rules", warnings=warnings, extracted={"facts": facts})


def _inspect_office(job: _Job, settings: Settings, store: LibraryStore) -> tuple[list, str]:
    """``(warnings, refusal)`` of an Office document before it is passed on."""
    if job.format not in OFFICE:
        return [], ""
    try:
        facts = extract(job.format, store.object_path(job.org_id, job.sha256), _limits(settings))
    except ExtractFailed as exc:
        return [], f"检查文档时出错，出于安全没有交给解析服务：{exc.message}"
    except OSError:
        return [], "这份资料的文件已经不在云端，没有解析"
    warnings = ["文档里有宏，没有运行它；解析只读取文字"] if facts.get("macros") else []
    external = [e for e in facts.get("external") or [] if isinstance(e, str)]
    if external:
        return warnings, ("文档里有外部引用（" + "；".join(external[:3]) + (" 等" if len(external) > 3 else "") +
                          "），打开时会去别处取内容，出于安全没有交给解析服务。可以去掉外部引用后重新上传，或手工填写后确认")
    return warnings, ""


def _page_text(job: _Job, settings: Settings, store: LibraryStore):
    """The words of an HTML page (issue #45): ``(text as bytes, warnings)``, or the ``_Finish`` that ends the
    job without sending anything."""
    def refused(why: str) -> _Finish:
        return _Finish({}, "rules", error=why)
    try:
        facts = extract(job.format, store.object_path(job.org_id, job.sha256), _limits(settings))
    except ExtractFailed as exc:
        if exc.detail:
            log.warning("page reader for %s: %s (%s)", job.material_id, exc.code, exc.detail[-500:])
        return refused(f"没有读出这个网页的文字：{exc.message}。可以手工填写后确认")
    except OSError:
        return refused("这份资料的文件已经不在云端")
    text = facts.get("text") if isinstance(facts.get("text"), str) else ""
    if not text.strip():
        return refused("网页里没有可以读出的文字（内容可能是打开网页时才由脚本生成的），没有交给解析服务。"
                       "可以把网页另存为 PDF 或文本后上传，或手工填写后确认")
    data = (text + "\n").encode("utf-8", "replace")
    if len(data) > settings.weknora_max_bytes:
        return refused(f"网页的文字超过解析服务接收的上限（{settings.weknora_max_bytes // (1024 * 1024)} MB），"
                       "没有解析；可以手工填写后确认")
    warnings = [f"网页很长，只解析了前面约 {max(len(text) // 10000, 1)} 万字"] if facts.get("truncated") else []
    return data, warnings


def _page_warnings(job: _Job, settings: Settings, store: LibraryStore) -> list:
    """What was noted when the page was turned into text, for a job that uses a copy sent earlier: the
    copy is the same text, so the same notes hold (the page is read again; nothing is sent)."""
    if job.format not in CONVERTED:
        return []
    page = _page_text(job, settings, store)
    return [] if isinstance(page, _Finish) else page[1]


def _page_file(job: _Job, settings: Settings, store: LibraryStore):
    """An HTML page as the file that is sent instead of it: ``(descriptor, size, warnings)`` of a nameless
    temporary file holding the page's text, or the ``_Finish`` that ends the job without sending."""
    page = _page_text(job, settings, store)
    if isinstance(page, _Finish):
        return page
    data, warnings = page
    handle = tempfile.TemporaryFile()                   # nameless from the start; gone when it is closed
    try:
        handle.write(data)
        handle.flush()
        return os.dup(handle.fileno()), len(data), warnings
    finally:
        handle.close()


def _reusable(db: Database, job: _Job) -> MaterialParse | None:
    """Another material of the organisation with the same content whose questions were already answered."""
    with db.transaction() as s:
        return s.execute(select(MaterialParse).where(
            MaterialParse.org_id == job.org_id, MaterialParse.sha256 == job.sha256,
            MaterialParse.material_id != job.material_id, MaterialParse.stage == DONE,
            MaterialParse.parser == "weknora", MaterialParse.error == "", MaterialParse.knowledge_id != "")
            .order_by(MaterialParse.updated_at.desc()).limit(1)).scalar_one_or_none()


def _upload_step(job: _Job, settings: Settings, store: LibraryStore, service, db: Database):
    warnings, refusal = _inspect_office(job, settings, store)
    if refusal:
        return _Finish({}, "rules", warnings=warnings, error=refusal)
    if job.knowledge_id and service is not None:         # parsed again: the knowledge service still has its copy
        return _Next(INDEXING, 0, {"warnings": warnings + _page_warnings(job, settings, store)})
    if service is None:
        return _Finish({}, "weknora", warnings=warnings, error=(
            "云平台没有配置文档解析服务（WeKnora），这份文档没有解析。管理员配置后可以“重新解析”；也可以现在手工填写后确认"))
    if job.format not in CONVERTED and job.size > settings.weknora_max_bytes:
        return _Finish({}, "weknora", warnings=warnings, error=(
            f"文件有 {job.size // (1024 * 1024)} MB，超过解析服务接收的上限（{settings.weknora_max_bytes // (1024 * 1024)} MB），"
            "没有解析；可以手工填写后确认"))
    earlier = _reusable(db, job)
    if earlier is not None:
        return _Finish(dict(earlier.extracted.get("values") or {}), "weknora",
                       warnings=warnings + _page_warnings(job, settings, store)
                       + ["同一个文件已经解析过（别的品牌型号或版本下），沿用了那次的解析结果"],
                       extracted=dict(earlier.extracted), knowledge_id=earlier.knowledge_id)
    extension = SERVICE_EXTENSIONS[job.format]
    stem = job.filename.rsplit(".", 1)[0] if "." in job.filename else job.filename
    metadata = {"neuxmind_material_id": job.material_id, "neuxmind_org_id": job.org_id}   # issue #51: no device
    size = job.size
    if job.format in CONVERTED:
        page = _page_file(job, settings, store)
        if isinstance(page, _Finish):
            return page
        fd, size, more = page
        warnings = warnings + more
    else:
        try:
            fd = os.open(store.object_path(job.org_id, job.sha256), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return _Finish({}, "weknora", error="这份资料的文件已经不在云端")
    try:
        knowledge = service.upload(fd, size, f"{stem}.{extension}", metadata)
    finally:
        os.close(fd)
    if knowledge.duplicate:
        warnings = warnings + ["解析服务里已经有同一个文件，直接用了它的解析结果"]
    if knowledge.parse_status in weknora.READY:
        return _Next(ASKING, 0, {"knowledge_id": knowledge.id, "warnings": warnings})
    return _Next(INDEXING, INDEX_POLLS[0], {"knowledge_id": knowledge.id, "warnings": warnings})


def _index_step(job: _Job, settings: Settings, service, at: float):
    if service is None:
        return _Finish({}, "weknora", error="云平台的文档解析服务（WeKnora）配置被取消了，这份文档没有解析完")
    try:
        knowledge = service.knowledge(job.knowledge_id)
    except weknora.WeKnoraError as exc:
        if exc.gone:                                      # the copy is gone: send it again
            return _Next(QUEUED, 0, {"knowledge_id": "", "resend": True})
        raise
    if knowledge.parse_status in weknora.READY:
        return _Next(ASKING, 0)
    if knowledge.parse_status in weknora.FAILED:
        reason = knowledge.error or ("解析被取消" if knowledge.parse_status == "cancelled" else "没有说明原因")
        return _Finish({}, "weknora", error=f"解析服务没有解析出这份文档：{reason}。可以手工填写后确认")
    if at - job.stage_since > settings.weknora_parse_seconds:
        return _Finish({}, "weknora", error=(
            f"解析服务 {settings.weknora_parse_seconds // 60} 分钟内没有解析完这份文档。可以稍后“重新解析”，或手工填写后确认"))
    polls = int((at - job.stage_since) // 30)
    return _Next(INDEXING, INDEX_POLLS[min(polls, len(INDEX_POLLS) - 1)])


def _ask_step(job: _Job, service):
    if service is None:
        return _Finish({}, "weknora", error="云平台的文档解析服务（WeKnora）配置被取消了，这份文档没有解析完")
    values, warnings, answers = {}, [], []
    for keys, question in QUESTIONS:
        try:
            answer = service.ask(job.knowledge_id, question)
        except weknora.WeKnoraError as exc:
            if exc.gone:                                  # the copy went away meanwhile: send it again
                return _Next(QUEUED, 0, {"knowledge_id": "", "resend": True})
            raise
        answers.append(answer[:4000])
        found = _answer_object(answer)
        if found is None:
            warnings.append(f"模型对“{'、'.join(f.label for f in fields.FIELDS[fields.DOCUMENT] if f.key in keys)}”"
                            "的回答不是要求的格式，这几项没有填")
            continue
        for key in keys:
            values[key] = _drop_no_value(found.get(key))
    return _Finish(values, "weknora", warnings=warnings, extracted={"values": fields.clean_values(fields.DOCUMENT, values),
                                                                   "answers": answers})


# ------------------------------------------------------------------ running jobs
def _new_jobs(db: Database) -> int:
    """A job for every material that waits in 「解析中」 and has none yet."""
    with db.transaction() as s:
        rows = s.execute(select(Material.id, Material.org_id, Material.sha256, Material.format, Material.kind)
                         .outerjoin(MaterialParse, MaterialParse.material_id == Material.id)
                         .where(Material.status == PARSING, MaterialParse.material_id.is_(None)).limit(200)).all()
    created = 0
    for material_id, org_id, sha256, fmt, material_kind in rows:
        try:
            with db.transaction() as s:
                s.add(MaterialParse(material_id=material_id, org_id=org_id, sha256=sha256,
                                    kind=library_model.result_kind(material_kind, fields.result_kind(fmt)),
                                    stage=QUEUED, next_at=0.0, stage_since=now()))
            created += 1
        except IntegrityError:
            continue                                       # another worker made it first
    return created


def _claim(db: Database, settings: Settings, at: float, kinds=None) -> tuple[_Job | None, str] | None:
    """The next due job (of ``kinds``, or any), held by this worker (``owner``); ``(None, "")`` when another
    worker took it first or its material is gone; ``None`` when nothing is due."""
    with db.transaction() as s:
        due = select(MaterialParse.material_id, MaterialParse.stage).where(
            MaterialParse.stage != DONE, MaterialParse.next_at <= at, MaterialParse.lease_until <= at)
        if kinds:
            due = due.where(MaterialParse.kind.in_(kinds))
        row = s.execute(due.order_by(MaterialParse.next_at).limit(1)).first()
    if row is None:
        return None
    material_id, stage = row
    owner = uuid.uuid4().hex
    with db.transaction() as s:
        taken = s.execute(update(MaterialParse).where(
            MaterialParse.material_id == material_id, MaterialParse.stage == stage, MaterialParse.lease_until <= at)
            .values(lease_until=at + _lease_seconds(settings, stage), lease_owner=owner)).rowcount
        if taken != 1:
            return None, ""
        job = s.get(MaterialParse, material_id)
        material = s.get(Material, material_id)
        if material is None:                               # deleted meanwhile: cleanup() takes it from here
            job.stage, job.lease_until, job.lease_owner = DONE, 0.0, ""
            return None, ""
        snapshot = _Job(material_id, job.org_id, job.kind, job.stage, job.attempts, job.stage_since, job.knowledge_id,
                        material.format, material.size, material.sha256, material.filename, material.device_id,
                        material.kind)
    return snapshot, owner


def _apply(db: Database, job: _Job, owner: str, outcome, at: float) -> None:
    """Store the outcome of a step, if this worker still holds the job."""
    with db.transaction() as s:
        row = s.execute(select(MaterialParse).where(MaterialParse.material_id == job.material_id,
                                                    MaterialParse.lease_owner == owner).with_for_update()).scalar_one_or_none()
        if row is None:
            return
        material = s.get(Material, job.material_id)
        if isinstance(outcome, _Finish) and outcome.knowledge_id is not None:
            row.knowledge_id = outcome.knowledge_id
        if isinstance(outcome, _Next) and "knowledge_id" in outcome.changes:
            row.knowledge_id = outcome.changes["knowledge_id"]
        row.lease_until, row.lease_owner, row.updated_at = 0.0, "", at
        if material is None:
            row.stage = DONE                               # deleted meanwhile: cleanup() takes it from here
            return
        if isinstance(outcome, _Retry):
            row.attempts += 1
            if row.attempts < MAX_ATTEMPTS:
                row.next_at = at + RETRY_WAITS[min(row.attempts - 1, len(RETRY_WAITS) - 1)]
                row.error = outcome.message
                return
            outcome = _Finish({}, row.parser or "weknora", error=f"{outcome.message}；试了 {row.attempts} 次都没有成功。"
                              "可以稍后“重新解析”，或手工填写后确认")
        if isinstance(outcome, _Next) and outcome.changes.get("resend"):
            row.resends += 1
            if row.resends > MAX_RESENDS:
                outcome = _Finish({}, "weknora", error=(f"这份文档在解析服务里的副本丢了 {row.resends} 次，没有解析完。"
                                                         "请检查解析服务（知识库是否被删除），之后“重新解析”，或手工填写后确认"))
        if isinstance(outcome, _Next):
            if outcome.stage != row.stage:
                row.stage_since, row.attempts = at, 0
            row.stage, row.next_at, row.error = outcome.stage, at + outcome.delay, ""
            if "warnings" in outcome.changes:
                row.warnings = list(outcome.changes["warnings"])
            return
        _finish(s, row, material, outcome, at)


def _indexed(row: MaterialParse) -> bool:
    """The knowledge service has this document: it parsed and indexed its copy without an error."""
    return row.kind == fields.DOCUMENT and row.parser == "weknora" and not row.error and bool(row.knowledge_id)


def _state(row: MaterialParse, values: dict, gaps: list[str]) -> tuple[str, str]:
    """The material's status and the words under it, from a result the parser did not fail on. An indexed
    document is a success whatever it lacks (issue #45): the library keeps information, the driver's items
    are an extra."""
    if not gaps:
        return PARSED, ""
    if not _indexed(row):
        return PARSE_FAILED, fields.missing_text(gaps)
    if any(v not in (None, "", []) for v in values.values()):
        return STORED, f"已存入知识库；驱动信息{fields.missing_text(gaps)}（可选，填好并确认后才交给修复助手）"
    return STORED, "已存入知识库；没有读出驱动信息（可选，需要时可以手工填写后确认）"


def adopt_stored(db: Database) -> int:
    """Documents that versions before issue #45 left in 「没解析出来」 only because items were missing: 「已入库」."""
    with db.transaction() as s:
        rows = s.execute(select(MaterialParse, Material).join(Material, Material.id == MaterialParse.material_id).where(
            Material.status == PARSE_FAILED, MaterialParse.stage == DONE, MaterialParse.kind == fields.DOCUMENT,
            MaterialParse.parser == "weknora", MaterialParse.error == "", MaterialParse.knowledge_id != "")).all()
        for row, material in rows:
            values = dict(row.values or {})
            library.set_parse_state(s, material, *_state(row, values, fields.missing(row.kind, values, row.not_applicable)))
        return len(rows)


def _finish(s: Session, row: MaterialParse, material: Material, outcome: _Finish, at: float) -> None:
    kind = row.kind
    values = fields.clean_values(kind, outcome.values)
    if kind == fields.PACKAGE:
        values["sha256"] = material.sha256
    gaps = fields.missing(kind, values)
    row.stage, row.next_at, row.attempts, row.resends, row.stage_since = DONE, 0.0, 0, 0, at
    row.parser, row.values, row.not_applicable = outcome.parser, values, []
    row.extracted = outcome.extracted or {"values": values}
    row.warnings = list(dict.fromkeys([*row.warnings, *outcome.warnings]))[:50]
    row.error, row.revision = outcome.error, row.revision + 1
    row.confirmed_by = row.confirmed_by_email = ""
    row.confirmed_at = None
    status, detail = (PARSE_FAILED, outcome.error) if outcome.error else _state(row, values, gaps)
    if outcome.error and gaps and any(v for v in values.values() if v not in (None, "", [])):
        detail = f"{outcome.error}（{fields.missing_text(gaps)}）"
    library.set_parse_state(s, material, status, detail)
    audit.record(s, "library.parse_finished", ACTOR, org_id=row.org_id, target=row.material_id,
                 detail={"status": status, "parser": outcome.parser, "missing": len(gaps)})


def _step(job: _Job, settings: Settings, store: LibraryStore, service, db: Database, at: float):
    """The outcome of the job's current step. Whatever goes wrong ends as an outcome for this one material."""
    rules = job.kind in (fields.URDF, fields.PACKAGE)
    try:
        if job.kind in (fields.URDF, fields.PACKAGE):
            return _rules_step(job, settings, store)
        if job.stage == QUEUED:
            return _upload_step(job, settings, store, service, db)
        if job.stage == INDEXING:
            return _index_step(job, settings, service, at)
        return _ask_step(job, service)
    except weknora.WeKnoraError as exc:
        if exc.retry:
            return _Retry(exc.message)
        return _Finish({}, "weknora", error=f"{exc.message}。可以稍后“重新解析”，或手工填写后确认")
    except Exception:
        log.exception("parse step of %s failed", job.material_id)
        return _Finish({}, "rules" if rules else "weknora",
                       error="解析时出错（程序问题），这一份没有解析出来；可以“重新解析”，或手工填写后确认")


def run_pending(db: Database, settings: Settings, store: LibraryStore | None = None, service=None, *,
                limit: int = 20, at: float | None = None, kinds=None) -> int:
    """Run the steps that are due (at most ``limit``, of ``kinds`` or any); the number run. ``service``:
    the knowledge service, or ``None`` for the configured one (tests pass their own)."""
    store = store or LibraryStore(settings.effective_library_dir)
    service = service if service is not None else _service(settings)
    _new_jobs(db)
    done = 0
    for _ in range(limit * 3):
        if done >= limit:
            break
        moment = now() if at is None else at
        claimed = _claim(db, settings, moment, kinds)
        if claimed is None:
            break
        job, owner = claimed
        if job is None:
            continue
        outcome = _step(job, settings, store, service, db, moment)
        _apply_safely(db, job, owner, outcome, now() if at is None else at)
        done += 1
    return done


def _apply_safely(db: Database, job: _Job, owner: str, outcome, at: float) -> None:
    """Store an outcome. A database error (a deadlock, a lost connection) is tried once more and otherwise
    left to the lease, so the step runs again; any other error stores a plain failure instead, so that no
    job is left half done."""
    for attempt in (1, 2):
        try:
            return _apply(db, job, owner, outcome, at)
        except DBAPIError:
            log.warning("storing the parse outcome of %s: database error (try %d)", job.material_id, attempt,
                        exc_info=True)
        except Exception:
            log.exception("storing the parse outcome of %s failed", job.material_id)
            break
    else:
        return                                            # the lease runs out and the step is run again
    parser = "rules" if job.kind in (fields.URDF, fields.PACKAGE) else "weknora"
    try:
        _apply(db, job, owner, _Finish({}, parser, error="保存解析结果时出错（程序问题）；可以“重新解析”，或手工填写后确认"), at)
    except Exception:
        log.exception("storing the failure of %s failed too; its lease will run out", job.material_id)


_service_warning = [False]


def _service(settings: Settings):
    try:
        return weknora.from_settings(settings)
    except ValueError as exc:
        if not _service_warning[0]:
            log.error("knowledge service not used: %s", exc)
            _service_warning[0] = True
        return None


def cleanup(db: Database, service) -> int:
    """Rows of deleted materials: remove their copy from the knowledge service (when nothing else uses it), then the row."""
    with db.transaction() as s:
        orphans = s.execute(select(MaterialParse.material_id, MaterialParse.knowledge_id)
                            .outerjoin(Material, Material.id == MaterialParse.material_id)
                            .where(Material.id.is_(None)).limit(CLEANUP_BATCH)).all()
    removed = 0
    for material_id, knowledge_id in orphans:
        if knowledge_id:
            with db.transaction() as s:
                others = s.execute(select(func.count()).select_from(MaterialParse).where(
                    MaterialParse.knowledge_id == knowledge_id, MaterialParse.material_id != material_id)).scalar_one()
            if not others:
                if service is None:
                    continue                               # kept until the service can be reached again
                try:
                    service.delete(knowledge_id)
                except weknora.WeKnoraError as exc:
                    log.warning("knowledge %s not removed yet: %s", knowledge_id, exc.message)
                    continue
                _forget_copy(db, knowledge_id, material_id)
        with db.transaction() as s:
            if s.get(Material, material_id) is None:
                row = s.get(MaterialParse, material_id)
                if row is not None:
                    s.delete(row)
                    removed += 1
    return removed


def _forget_copy(db: Database, knowledge_id: str, removed_for: str) -> None:
    """A job that took the same copy while it was being removed (the knowledge service deduplicates
    files): one still running sends its file again; a finished one keeps its result without the copy."""
    with db.transaction() as s:
        for row in s.execute(select(MaterialParse).where(MaterialParse.knowledge_id == knowledge_id,
                                                         MaterialParse.material_id != removed_for)
                             .order_by(MaterialParse.material_id).with_for_update()).scalars():
            row.knowledge_id = ""
            if row.stage != DONE:
                row.stage, row.next_at, row.attempts, row.stage_since = QUEUED, 0.0, 0, now()


class ParseWorker:
    """Threads that run :func:`run_pending` every ``interval`` seconds; the first also runs :func:`cleanup`.
    With two or more threads the first takes only URDF files and packages (seconds each) and the others
    only documents (which wait on the knowledge service), so a slow service never holds up the rules."""

    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> "ParseWorker":
        for index in range(max(1, self.settings.library_parse_threads)):
            thread = threading.Thread(target=self._loop, args=(index,), name=f"neuxmind-parse-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def _loop(self, index: int) -> None:
        last_cleanup = 0.0
        store = LibraryStore(self.settings.effective_library_dir)
        while not self.stop_event.wait(self.settings.library_parse_interval_seconds):
            try:
                service = _service(self.settings)
                kinds = None
                if self.settings.library_parse_threads > 1:
                    kinds = (fields.URDF, fields.PACKAGE) if index == 0 else (fields.DOCUMENT,)
                run_pending(self.db, self.settings, store, service, kinds=kinds)
                cleaner = min(1, self.settings.library_parse_threads - 1)     # a document thread, when there is one
                if index == cleaner and time.monotonic() - last_cleanup > CLEANUP_INTERVAL:
                    last_cleanup = time.monotonic()
                    cleanup(self.db, service)
            except Exception:  # pragma: no cover - logged, the loop goes on
                log.exception("material parse round failed")

    def stop(self) -> None:
        self.stop_event.set()


def start_worker(db: Database, settings: Settings) -> ParseWorker | None:
    if settings.library_parse_interval_seconds <= 0:
        return None
    try:
        adopt_stored(db)
    except Exception:  # pragma: no cover - logged; the next start tries again
        log.exception("stored documents not adopted")
    return ParseWorker(db, settings).start()


# ------------------------------------------------------------------ what people see and do
def _job(session: Session, material_id: str, lock: bool = False) -> MaterialParse | None:
    query = select(MaterialParse).where(MaterialParse.material_id == material_id)
    return session.execute(query.with_for_update() if lock else query).scalar_one_or_none()


def view(material: Material, job: MaterialParse | None) -> dict:
    kind = library_model.result_kind(material.kind, fields.result_kind(material.format))
    done = job is not None and job.stage == DONE
    values = dict(job.values) if done else fields.clean_values(kind, {})
    if kind == fields.PACKAGE:
        values["sha256"] = material.sha256
    na = list(job.not_applicable) if done else []
    return {"material": library.material_view(material), "kind": kind, "fields": fields.schema(kind),
            "note": fields.NOTES[kind], "stage": job.stage if job else QUEUED, "status": material.status,
            "status_detail": material.status_detail, "values": values, "not_applicable": na,
            "missing": fields.missing(kind, values, na) if done else [],
            "warnings": list(job.warnings) if job else [], "error": job.error if job else "",
            "retrying": bool(job and not done and job.error), "parser": job.parser if job else "",
            "revision": job.revision if job else 0, "edited_by": job.edited_by_email if job else "",
            "edited_at": job.edited_at if job else None, "confirmed_by": job.confirmed_by_email if job else "",
            "confirmed_at": job.confirmed_at if job else None, "editable": done}


def get_result(session: Session, p, material_id: str) -> dict:
    p.require(library.READ)
    material = library._own_material(session, p, material_id, read=True)        # issue #78: also a public one
    library_bundle.refuse_parse(material)
    return library.library_public.result_marks(p, material, view(material, _job(session, material_id)))


def _edit(session: Session, p, material_id: str, payload) -> tuple[Material, MaterialParse, list[str]]:
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象")
    material = library._own_material(session, p, material_id)
    library_bundle.refuse_parse(material)
    job = _job(session, material_id, lock=True)
    if job is None or job.stage != DONE:
        raise conflict("这份资料还在解析，解析完才能修改或确认", code="parse_running")
    if type(payload.get("revision")) is not int or payload["revision"] != job.revision:
        raise ApiError(409, "parse_changed", "这份解析结果刚被修改过（可能是别人，也可能是重新解析），请刷新后再改",
                       revision=job.revision)
    kind = job.kind
    try:
        changes, na = fields.check_edit(kind, payload.get("values", {}), payload.get("not_applicable",
                                                                                     job.not_applicable))
    except fields.Invalid as exc:
        raise bad_request(str(exc), code="invalid_parse_values") from None
    values = fields.clean_values(kind, {**job.values, **changes})
    if kind == fields.PACKAGE:
        values["sha256"] = material.sha256
    gaps = fields.missing(kind, values, na)
    at = now()
    edited = bool(changes) or na != list(job.not_applicable)
    job.values, job.not_applicable, job.revision, job.updated_at = values, na, job.revision + 1, at
    if edited:
        job.edited_by, job.edited_by_email, job.edited_at = p.user.id, p.user.email, at
    job.confirmed_by = job.confirmed_by_email = ""
    job.confirmed_at = None
    library.set_parse_state(session, material, *_state(job, values, gaps))
    return material, job, gaps


def save(db: Database, who, material_id: str, payload) -> dict:
    """A person changes the result. It is 「待确认」 again (or 「没解析出来」 while items are missing)."""
    with db.transaction() as s:
        p = library._writer(who(s))
        material, job, gaps = _edit(s, p, material_id, payload)
        audit.record(s, "library.parse_edited", p.actor, org_id=p.org_id, target=material_id,
                     detail={"revision": job.revision, "missing": len(gaps)})
        return view(material, job)


def confirm(db: Database, who, material_id: str, payload) -> dict:
    """A person confirms the result (with the changes sent along): it becomes 「可用」. Refused while items are missing."""
    with db.transaction() as s:
        p = library._writer(who(s))
        material, job, gaps = _edit(s, p, material_id, payload)
        if gaps:
            raise ApiError(409, "parse_incomplete", f"还不能确认，{fields.missing_text(gaps)}。请填写，或把不适用的项目标为“不适用”",
                           missing=gaps[:100])
        job.confirmed_by, job.confirmed_by_email, job.confirmed_at = p.user.id, p.user.email, now()
        library.set_parse_state(s, material, USABLE, "")
        audit.record(s, "library.parse_confirmed", p.actor, org_id=p.org_id, target=material_id,
                     detail={"revision": job.revision})
        return view(material, job)


def retry(db: Database, who, material_id: str) -> dict:
    """Parse again from the start (what people filled in is replaced by the new result)."""
    with db.transaction() as s:
        p = library._writer(who(s))
        material = library._own_material(s, p, material_id)
        library_bundle.refuse_parse(material)
        job = _job(s, material_id, lock=True)
        if job is not None and job.stage != DONE:
            raise conflict("这份资料正在解析", code="parse_running")
        if job is None:
            job = MaterialParse(material_id=material.id, org_id=material.org_id, sha256=material.sha256,
                                kind=library_model.result_kind(material.kind, fields.result_kind(material.format)))
            s.add(job)
        at = now()
        job.stage, job.next_at, job.attempts, job.resends, job.stage_since = QUEUED, 0.0, 0, 0, at
        job.values, job.extracted, job.not_applicable, job.warnings, job.error = {}, {}, [], [], ""
        job.confirmed_by = job.confirmed_by_email = ""
        job.confirmed_at = None
        job.revision, job.updated_at = (job.revision or 0) + 1, at
        library.set_parse_state(s, material, PARSING, "")
        audit.record(s, "library.parse_retry", p.actor, org_id=p.org_id, target=material_id)
        s.flush()
        return view(material, job)
