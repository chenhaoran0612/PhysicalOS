"""The open platform (issue #43, docs/CLOUD_SKILLS.md §4): a third party makes a robot run a skill.

A caller holds an API key with the scope ``skills:invoke``. It sees the robots of its own organisation
that keep a link to the cloud (``device_link.py``), places a call on one of them, watches it, cancels
it, or asks the robot to stop. The cloud never reaches into a robot: a call waits here until the robot
fetches it with its next sync, and a call nobody fetched in time expires instead of running later.

This API moves a physical machine, so everything is refused that can be refused here: a robot that is
offline or latched, a skill that is not installed or whose owner has not switched remote calls on,
parameters the skill does not declare, a second call while one is under way. None of it replaces the
robot's own checks -- the robot decides again when the call arrives.

Life of a call: ``queued`` → ``dispatched`` (handed to the robot) → ``running`` → ``succeeded`` /
``failed`` / ``cancelled`` / ``stopped``; or ``rejected`` (the robot refused it), ``expired`` (nobody
fetched it), ``lost`` (the robot went silent: the outcome is unknown, not a failure).
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, skill_params
from .auth import SKILL_INVOKE_SCOPE, Principal
from .config import Settings
from .db import Database
from .errors import ApiError, bad_request, conflict, forbidden, not_found
from .skill_models import (ACTIVE, CANCELLED, DISPATCHED, EXPIRED, LOST, QUEUED, RUNNING, STATES, STOPPED, DeviceLink,
                           DeviceStop, SkillCall)

# The clock of the cloud skills (device_link.py uses it too). Tests replace it to move time.
clock = time.time

MAX_PARAMETER_BYTES = 16 * 1024
MAX_PARAMETER_DEPTH = 16
MAX_REASON = 200
MAX_PAGE, DEFAULT_PAGE = 200, 50
SWEEP_ACTOR = "system:sweeper"
CLOUD_ACTOR = "system:cloud"
DAY = 24 * 3600

DEVICE_ID = re.compile(r"dlk_[0-9a-f]{20}")
CALL_ID = re.compile(r"cal_[0-9a-f]{20}")
STOP_ID = re.compile(r"stp_[0-9a-f]{20}")
SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,99}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,99}")
_CURSOR = re.compile(r"(\d{1,12}(?:\.\d{1,9})?)~(cal_[0-9a-f]{20})")


def now() -> float:
    return clock()


# ---------------------------------------------------------------- who
def caller(p: Principal) -> Principal:
    """The open platform is for API keys with ``skills:invoke``; a login session is not a third party."""
    p.require(SKILL_INVOKE_SCOPE)
    if p.kind != "api_key":
        raise forbidden("开放平台只接受带 skills:invoke 的 API Key，不接受登录会话", code="api_key_required")
    return p


def verified(p: Principal) -> Principal:
    """Anyone can register: what makes the cloud store things needs a verified e-mail address (the key creator's)."""
    if not p.user.email_verified:
        raise forbidden("请先验证邮箱（POST /auth/verify-email）", code="email_unverified")
    return p


# ---------------------------------------------------------------- views
def is_online(link: DeviceLink, settings: Settings, at: float) -> bool:
    return at - link.last_seen_at <= settings.device_link_offline_seconds


def robot_view(link: DeviceLink, settings: Settings, at: float) -> dict:
    return {"device_id": link.id, "instance_id": link.instance_id, "name": link.name, "family": link.family,
            "simulated": bool(link.simulated), "online": is_online(link, settings, at),
            "estop_latched": bool(link.estop_latched), "busy": bool(link.busy),
            "motion_blocked": bool(link.motion_blocked), "runtime_version": link.runtime_version,
            "last_seen_at": link.last_seen_at, "skills": link.skill_count or 0}


def skill_view(entry: dict) -> dict:
    return {"id": entry["id"], "version": entry["version"], "title": entry["title"],
            "description": entry["description"], "parameters": entry["parameters"], "requires": entry["requires"],
            "motion": entry["motion"], "timeout_s": entry["timeout_s"], "remote_allowed": entry["remote"]["allowed"]}


def call_view(call: SkillCall) -> dict:
    return {"id": call.id, "device_id": call.device_id, "skill_id": call.skill_id, "version": call.version,
            "parameters": call.parameters or {}, "status": call.status, "progress": call.progress,
            "detail": call.detail or "", "result": (call.result or {}).get("value"), "error": call.error,
            "request_id": call.request_id, "created_by": call.created_by, "created_at": call.created_at,
            "dispatched_at": call.dispatched_at, "started_at": call.started_at, "finished_at": call.finished_at,
            "cancel_requested": call.cancel_requested_at is not None}


def stop_view(stop: DeviceStop) -> dict:
    if stop.acknowledged_at is not None:
        status = "acknowledged"
    elif stop.expired_at is not None:
        status = "expired"
    else:
        status = "delivered" if stop.delivered_at is not None else "pending"
    return {"stop_id": stop.id, "device_id": stop.device_id, "status": status, "reason": stop.reason,
            "delivered": stop.delivered_at is not None, "created_by": stop.created_by, "created_at": stop.created_at,
            "delivered_at": stop.delivered_at, "acknowledged_at": stop.acknowledged_at}


# ---------------------------------------------------------------- state that changes with time
def finish(call: SkillCall, status: str, at: float, code: str | None = None, message: str = "") -> None:
    """Put a call into a final state the cloud itself decides (never one the robot reported)."""
    call.status, call.finished_at = status, at
    if code is not None:
        call.error = {"code": code, "message": message}
    if message:
        call.detail = message


def pending_stop(session: Session, device_id: str) -> DeviceStop | None:
    return session.execute(select(DeviceStop).where(
        DeviceStop.device_id == device_id, DeviceStop.acknowledged_at.is_(None), DeviceStop.expired_at.is_(None))
        .order_by(DeviceStop.created_at.desc()).limit(1)).scalar_one_or_none()


def _locked_calls(session: Session, ids: list[str], states: tuple) -> list[SkillCall]:
    """The calls among ``ids`` that still are in one of ``states``, locked in id order (so that two
    refreshes never wait on each other) and read afresh."""
    if not ids:
        return []
    return session.execute(select(SkillCall).where(SkillCall.id.in_(ids), SkillCall.status.in_(states))
                           .order_by(SkillCall.id).with_for_update().execution_options(populate_existing=True)
                           ).scalars().all()


def settle_one(session: Session, call: SkillCall, settings: Settings, at: float) -> str | None:
    """Apply to one call (locked by the caller) what time alone decides: a queued call nobody fetched
    expires, a handed-over call of a silent or forgotten robot is lost. Returns the new state, if any."""
    pickup, silence = settings.skill_call_pickup_timeout_seconds, settings.skill_call_lost_after_seconds
    if call.status == QUEUED and call.created_at < at - pickup:
        finish(call, EXPIRED, at, "expired", f"机器人在 {pickup} 秒内没有取走这次调用，调用作废，不会再执行")
    elif call.status in (DISPATCHED, RUNNING):
        seen = session.execute(select(DeviceLink.last_seen_at).where(DeviceLink.id == call.device_id)).scalar_one_or_none()
        if seen is not None and seen >= at - silence:
            return None
        finish(call, LOST, at, "lost", f"机器人已超过 {silence} 秒没有同步，这次调用的结果未知（不等于失败）")
    else:
        return None
    audit.record(session, f"skill.call.{call.status}", SWEEP_ACTOR, org_id=call.org_id, target=call.id,
                 detail={"device_id": call.device_id, "skill_id": call.skill_id})
    return call.status


def outstanding(*columns):
    """Calls a robot may still be carrying out: handed over and not known to have ended. A ``lost``
    call counts -- "unknown outcome" includes "still running"."""
    return select(*columns).where(SkillCall.status.in_((DISPATCHED, RUNNING))
                                  | ((SkillCall.status == LOST) & SkillCall.dispatched_at.is_not(None)))


def refresh(session: Session, settings: Settings, at: float, *, org_id: str | None = None,
            device_id: str | None = None) -> dict:
    """Apply what time alone decides (for one robot, one organisation, or everything): a queued call
    nobody fetched expires, an unfinished call of a silent (or forgotten) robot is lost, a stop the
    robot never fetched is dropped -- unless that robot may still be carrying out a call. Idempotent.

    Lock order, here and everywhere else: a robot's row first (if at all), then calls in id order,
    then stops."""
    def scoped(query, model):
        if device_id is not None:
            query = query.where(model.device_id == device_id)
        if org_id is not None:
            query = query.where(model.org_id == org_id)
        return query

    counts = {"calls_expired": 0, "calls_lost": 0, "stops_expired": 0}
    pickup, silence = settings.skill_call_pickup_timeout_seconds, settings.skill_call_lost_after_seconds
    stale = scoped(select(SkillCall.id).where(SkillCall.status == QUEUED, SkillCall.created_at < at - pickup), SkillCall)
    silent = scoped(select(SkillCall.id).outerjoin(DeviceLink, DeviceLink.id == SkillCall.device_id).where(
        SkillCall.status.in_((DISPATCHED, RUNNING)),
        DeviceLink.id.is_(None) | (DeviceLink.last_seen_at < at - silence)), SkillCall)
    ids = [*session.execute(stale).scalars(), *session.execute(silent).scalars()]
    for call in _locked_calls(session, ids, ACTIVE):
        outcome = settle_one(session, call, settings, at)
        if outcome is not None:
            counts[f"calls_{outcome}"] += 1
    if any(counts.values()):
        session.flush()
    # A stop is dropped only when the robot never fetched it AND has nothing it may still be doing: a
    # stop for a robot that vanished in the middle of a call is handed over whenever it returns. One
    # that was handed over stays in force (no new calls, sent with every sync) until acknowledged.
    old = scoped(select(DeviceStop).where(DeviceStop.acknowledged_at.is_(None), DeviceStop.expired_at.is_(None),
                                          DeviceStop.delivered_at.is_(None),
                                          DeviceStop.created_at < at - settings.skill_stop_ttl_seconds,
                                          DeviceStop.device_id.not_in(outstanding(SkillCall.device_id))), DeviceStop)
    for stop in session.execute(old.order_by(DeviceStop.id)).scalars():
        stop.expired_at = at
        audit.record(session, "skill.stop.expired", SWEEP_ACTOR, org_id=stop.org_id, target=stop.id,
                     detail={"device_id": stop.device_id})
        counts["stops_expired"] += 1
    if counts["stops_expired"]:
        session.flush()
    return counts


def sweep(db: Database, settings: Settings, at: float | None = None) -> dict:
    """The maintenance sweep's part: time-outs everywhere, then what is past its retention."""
    at = now() if at is None else at
    with db.transaction() as session:
        counts = refresh(session, settings, at)
    keep_after = at - settings.skill_call_retention_days * DAY
    with db.transaction() as session:
        counts["calls_deleted"] = session.execute(delete(SkillCall).where(
            SkillCall.status.not_in(ACTIVE), SkillCall.created_at < keep_after)).rowcount or 0
        session.execute(delete(DeviceStop).where(DeviceStop.created_at < keep_after))
        busy = select(SkillCall.device_id).where(SkillCall.status.in_(ACTIVE))
        counts["devices_forgotten"] = session.execute(delete(DeviceLink).where(
            DeviceLink.last_seen_at < keep_after, DeviceLink.id.not_in(busy))).rowcount or 0
    return counts


def _trim(session: Session, org_id: str, settings: Settings) -> None:
    """Keep at most ``skill_call_org_keep`` calls of the organisation: the oldest finished ones go first."""
    total = session.execute(select(func.count()).select_from(SkillCall).where(SkillCall.org_id == org_id)).scalar_one()
    if total <= settings.skill_call_org_keep:
        return
    ids = session.execute(select(SkillCall.id).where(SkillCall.org_id == org_id, SkillCall.status.not_in(ACTIVE))
                          .order_by(SkillCall.created_at, SkillCall.id)
                          .limit(total - settings.skill_call_org_keep)).scalars().all()
    session.execute(delete(SkillCall).where(SkillCall.id.in_(ids)))


# ---------------------------------------------------------------- lookups
def _own_device(session: Session, p: Principal, device_id: str, lock: bool = False) -> DeviceLink:
    link = None
    if DEVICE_ID.fullmatch(device_id):                         # anything else is not an id of ours: no query
        query = select(DeviceLink).where(DeviceLink.id == device_id, DeviceLink.org_id == p.org_id)
        link = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if link is None:
        raise not_found("没有这台机器人（它还没有连到云端，或不属于本组织）", code="device_not_found")
    return link


def _own_call(session: Session, p: Principal, call_id: str, lock: bool = False) -> SkillCall:
    call = None
    if CALL_ID.fullmatch(call_id):
        query = select(SkillCall).where(SkillCall.id == call_id, SkillCall.org_id == p.org_id)
        call = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if call is None:
        raise not_found("没有这次调用", code="call_not_found")
    return call


def installed_skill(link: DeviceLink, skill_id: str) -> dict | None:
    return next((entry for entry in link.skills or [] if entry["id"] == skill_id), None)


# ---------------------------------------------------------------- reading
def list_robots(session: Session, p: Principal, settings: Settings) -> dict:
    caller(p)
    at = now()
    rows = session.execute(select(DeviceLink).where(DeviceLink.org_id == p.org_id)
                           .order_by(DeviceLink.name, DeviceLink.instance_id)).scalars()
    return {"robots": [robot_view(link, settings, at) for link in rows]}


def robot_skills(session: Session, p: Principal, device_id: str, settings: Settings) -> dict:
    caller(p)
    link = _own_device(session, p, device_id)
    return {"device_id": link.id, "online": is_online(link, settings, now()),
            "skills": [skill_view(entry) for entry in link.skills or []]}


def get_call(session: Session, p: Principal, call_id: str, settings: Settings) -> dict:
    caller(p)
    refresh(session, settings, now(), org_id=p.org_id)
    return {"call": call_view(_own_call(session, p, call_id))}


def _page_size(limit: str | None) -> int:
    try:
        size = DEFAULT_PAGE if limit is None else int(limit)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_PAGE:
        raise bad_request(f"limit 须为 1–{MAX_PAGE}")
    return size


def list_calls(session: Session, p: Principal, settings: Settings, *, device_id: str | None = None,
               status: str | None = None, limit: str | None = None, before: str | None = None) -> dict:
    """``GET /open/calls``: newest first; ``before`` is ``next_before`` of the previous page."""
    caller(p)
    size = _page_size(limit)
    if status is not None and status not in STATES:
        raise bad_request(f"status 须为 {' / '.join(STATES)}")
    if device_id is not None and not DEVICE_ID.fullmatch(device_id):
        raise bad_request("device_id 须为 /open/robots 返回的编号")
    cursor = None if before is None else _CURSOR.fullmatch(before)
    if before is not None and cursor is None:
        raise bad_request("before 须为上一页返回的 next_before")
    refresh(session, settings, now(), org_id=p.org_id)
    query = select(SkillCall).where(SkillCall.org_id == p.org_id)
    if device_id is not None:
        query = query.where(SkillCall.device_id == device_id)
    if status is not None:
        query = query.where(SkillCall.status == status)
    if cursor is not None:
        created, last_id = float(cursor.group(1)), cursor.group(2)
        query = query.where((SkillCall.created_at < created)
                            | ((SkillCall.created_at == created) & (SkillCall.id < last_id)))
    rows = session.execute(query.order_by(SkillCall.created_at.desc(), SkillCall.id.desc())
                           .limit(size + 1)).scalars().all()
    page, more = rows[:size], len(rows) > size
    return {"calls": [call_view(call) for call in page], "more": more,
            "next_before": f"{page[-1].created_at!r}~{page[-1].id}" if more else None}


# ---------------------------------------------------------------- placing a call
def _request(payload) -> dict:
    """The fields of a call request, checked for shape and size (not yet against the skill)."""
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象 {request_id, parameters?, timeout_s?}")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise bad_request("request_id 须为 1–100 个字母、数字或 _ . : -（同一个 request_id 只产生一次调用）")
    parameters = payload.get("parameters")
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict):
        raise ApiError(400, "invalid_parameters", "parameters 须为对象",
                       problems=[{"name": "", "problem": "param_type", "message": "parameters 须为对象"}])
    problem = skill_params.json_problem(parameters, MAX_PARAMETER_BYTES, MAX_PARAMETER_DEPTH)
    if problem is not None or any(not isinstance(name, str) or len(name) > 100 for name in parameters):
        text = f"parameters {problem or '的参数名过长'}"
        raise ApiError(400, "invalid_parameters", text, problems=[{"name": "", "problem": "param_type", "message": text}])
    timeout = payload.get("timeout_s")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 1 <= timeout <= 86400):
        raise bad_request("timeout_s 须为 1–86400 之间的秒数")
    version = payload.get("version")
    if version is not None and (not isinstance(version, str) or not 1 <= len(version) <= 50):
        raise bad_request("version 须为技能的版本号")
    return {"request_id": request_id, "parameters": parameters, "timeout_s": timeout, "version": version}


def _request_hash(device_id: str, skill_id: str, request: dict) -> str:
    document = [device_id, skill_id, request["version"], request["parameters"], request["timeout_s"]]
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _refuse_unless_callable(session: Session, link: DeviceLink, skill_id: str, request: dict, settings: Settings,
                            at: float) -> dict:
    """The cloud's own checks before a call is queued, in the order of the contract. Returns the installed skill."""
    if not is_online(link, settings, at):
        raise conflict("机器人离线：它最近没有和云端同步，调用不会被接收", code="device_offline")
    skill = installed_skill(link, skill_id) if SKILL_ID.fullmatch(skill_id) else None
    if skill is None:
        raise conflict("这台机器人上没有安装这个技能", code="skill_not_installed")
    if request["version"] is not None and request["version"] != skill["version"]:
        raise ApiError(409, "version_mismatch", f"机器人上安装的是 {skill['version']} 版，不是 {request['version']}",
                       installed=skill["version"])
    if not skill["remote"]["allowed"]:
        raise forbidden("机器人的主人没有为这个技能打开“允许远程调用”", code="remote_not_allowed")
    if link.estop_latched:
        raise conflict("机器人处于急停锁存状态，要有人在现场解除之后才能调用", code="estop_latched")
    if pending_stop(session, link.id) is not None:
        raise conflict("这台机器人有一个停止请求还没有执行完，现在不接受新的调用", code="stop_pending")
    problems = skill_params.check_values(skill["parameters"], request["parameters"])
    if problems:
        raise ApiError(400, "invalid_parameters", "参数不符合技能的声明：" + "；".join(p["message"] for p in problems[:5]),
                       problems=problems[:50])
    if request["timeout_s"] is not None and request["timeout_s"] > skill["timeout_s"]:
        raise bad_request(f"timeout_s 不能超过技能自己的时限（{skill['timeout_s']} 秒）")
    return skill


def place_call(session: Session, p: Principal, device_id: str, skill_id: str, payload, settings: Settings,
               ip: str = "") -> tuple[dict, bool]:
    """``POST /open/robots/{device_id}/skills/{skill_id}/calls``. ``(answer, created)``; the same
    ``request_id`` again answers with the call it produced the first time."""
    verified(caller(p))
    link = _own_device(session, p, device_id, lock=True)       # one call at a time is decided per robot
    request = _request(payload)
    at = now()
    refresh(session, settings, at, device_id=link.id)
    digest = _request_hash(link.id, skill_id, request)
    earlier = session.execute(select(SkillCall).where(SkillCall.org_id == p.org_id,
                                                      SkillCall.request_id == request["request_id"])).scalar_one_or_none()
    if earlier is not None:
        if earlier.request_hash != digest:
            raise ApiError(409, "request_id_reused", "这个 request_id 已经用于另一次内容不同的调用", call_id=earlier.id)
        return {"call": call_view(earlier)}, False
    skill = _refuse_unless_callable(session, link, skill_id, request, settings, at)
    running = session.execute(select(SkillCall.id).where(SkillCall.device_id == link.id, SkillCall.status.in_(ACTIVE))
                              .limit(1)).scalar_one_or_none()
    if running is not None:
        raise ApiError(409, "call_in_progress", "这台机器人已经有一个没结束的远程调用", call_id=running)
    call = SkillCall(org_id=p.org_id, device_id=link.id, skill_id=skill["id"], version=skill["version"],
                     digest=skill["digest"], parameters=request["parameters"],
                     timeout_s=request["timeout_s"] or skill["timeout_s"], status=QUEUED,
                     request_id=request["request_id"], request_hash=digest, created_by=p.actor, created_at=at)
    session.add(call)
    try:
        session.flush()
    except IntegrityError:                                     # the same request_id, placed on another robot meanwhile
        session.rollback()
        raise conflict("这个 request_id 刚刚已被使用", code="request_id_reused") from None
    audit.record(session, "skill.call.create", p.actor, org_id=p.org_id, target=call.id, ip=ip,
                 detail={"device_id": link.id, "instance_id": link.instance_id, "skill_id": call.skill_id,
                         "version": call.version, "motion": skill["motion"], "request_id": call.request_id,
                         "parameters_sha256": hashlib.sha256(json.dumps(call.parameters, sort_keys=True).encode()).hexdigest()})
    _trim(session, p.org_id, settings)
    return {"call": call_view(call)}, True


# ---------------------------------------------------------------- cancel and stop
def cancel_call(session: Session, p: Principal, call_id: str, settings: Settings, ip: str = "") -> dict:
    """``POST /open/calls/{id}/cancel``: a queued call is cancelled at once; one the robot may already
    have is cancelled by the robot at its next sync -- also when the robot has gone silent and the
    call counts as ``lost``: should it turn out to be running, the cancel reaches it when the robot
    is back. A call known to have ended stays as it is.

    Only this one call is locked (no ``refresh`` here: that locks other calls, in an order of its own)."""
    caller(p)
    call = _own_call(session, p, call_id, lock=True)
    at = now()
    settle_one(session, call, settings, at)
    maybe_running = call.status == LOST and call.dispatched_at is not None
    if (call.status not in ACTIVE and not maybe_running) or call.cancel_requested_at is not None:
        return {"call": call_view(call)}
    call.cancel_requested_at, call.cancel_requested_by = at, p.actor
    if call.status == QUEUED:
        finish(call, CANCELLED, at, None, "调用方在机器人取走之前取消了这次调用")
    audit.record(session, "skill.call.cancel", p.actor, org_id=p.org_id, target=call.id, ip=ip,
                 detail={"device_id": call.device_id, "skill_id": call.skill_id, "status": call.status})
    return {"call": call_view(call)}


def _reason(payload) -> str:
    if payload is None:
        return ""
    if not isinstance(payload, dict):
        raise bad_request("请求须为对象 {reason?}")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise bad_request("reason 须为文字")
    return skill_params.cleaned(reason, MAX_REASON)


def request_stop(session: Session, p: Principal, device_id: str, payload, settings: Settings, ip: str = "") -> dict:
    """``POST /open/robots/{device_id}/stop``: the robot stops and latches at its next sync. Calls that
    wait here are ended at once; the one the robot runs is ended by the robot. Asking again while a
    stop is still on its way answers with that stop. Best effort over a network: never a substitute
    for the emergency stop on site."""
    caller(p)
    link = _own_device(session, p, device_id, lock=True)
    reason = _reason(payload)
    at = now()
    refresh(session, settings, at, device_id=link.id)
    stop = pending_stop(session, link.id)
    created = stop is None
    if created:
        stop = DeviceStop(org_id=p.org_id, device_id=link.id, reason=reason, created_by=p.actor, created_at=at)
        session.add(stop)
        session.flush()
    affected = []
    concerned = select(SkillCall).where(SkillCall.device_id == link.id, SkillCall.status.in_(ACTIVE) | (
        (SkillCall.status == LOST) & SkillCall.dispatched_at.is_not(None) & SkillCall.cancel_requested_at.is_(None)))
    for call in session.execute(concerned.order_by(SkillCall.id).with_for_update()).scalars():
        affected.append(call.id)
        if call.status == QUEUED:
            finish(call, STOPPED, at, "stopped", "机器人被要求停止，这次调用没有下发")
        elif call.cancel_requested_at is None:
            call.cancel_requested_at, call.cancel_requested_by = at, p.actor
    if created or affected:                                    # asking again for the same stop changes nothing: no new record
        audit.record(session, "skill.stop.request", p.actor, org_id=p.org_id, target=stop.id, ip=ip,
                     detail={"device_id": link.id, "instance_id": link.instance_id, "reason": reason,
                             "calls": affected, "online": is_online(link, settings, at)})
    return {"stop_id": stop.id, "delivered": stop.delivered_at is not None, "device_id": link.id,
            "online": is_online(link, settings, at)}


def get_stop(session: Session, p: Principal, stop_id: str, settings: Settings) -> dict:
    caller(p)
    refresh(session, settings, now(), org_id=p.org_id)
    stop = None
    if STOP_ID.fullmatch(stop_id):
        stop = session.execute(select(DeviceStop).where(DeviceStop.id == stop_id,
                                                        DeviceStop.org_id == p.org_id)).scalar_one_or_none()
    if stop is None:
        raise not_found("没有这个停止请求", code="stop_not_found")
    return {"stop": stop_view(stop)}
