"""The robots' link to the cloud (issue #43, docs/CLOUD_SKILLS.md §3): ``POST /device-link/sync``.

The robot's management layer calls out every couple of seconds (nothing ever connects *to* a robot).
One request carries what the robot is (its installed skills and, per skill, whether its owner allows
remote calls), its safety state, the state of the remote calls it knows and the stops it has carried
out; the answer carries the calls to start, the calls to cancel and a stop, if one was asked for.

Delivery is at-least-once in both directions and safe to repeat: a call is sent again until the robot
mentions it (the robot runs a ``call_id`` at most once), the robot repeats a final state until the
answer lists it under ``settled``, a stop is sent until the robot lists it under ``stops``.

Trust: the key belongs to an organisation and the robot names itself (``instance_id``), exactly as
with alert reports. A key therefore speaks for any robot of its own organisation and for none of
another: every call and stop is looked up by organisation *and* robot, whatever ids a request names.
Everything a robot sends is bounded and cleaned before it is stored.
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, skill_calls, skill_params, skill_store
from .auth import DEVICE_LINK_SCOPE, Principal
from .config import Settings
from .db import advisory_lock
from .errors import ApiError, bad_request, conflict, forbidden
from .skill_models import (ACTIVE, CANCELLED, DISPATCHED, LOST, QUEUED, REJECTED, REPORTED, RUNNING, SUCCEEDED,
                           DeviceLink, DeviceStop, SkillCall)
from .spec_bridge import capabilities as spec_caps

SCHEMA = "neuxmind.device_link.v1"
MAX_SKILLS = 200
MAX_SKILL_BYTES = 8 * 1024          # one entry of the skills list, written out
MAX_REQUIRES = 100
MAX_REPORTS = 50                    # calls / stops one sync may mention
# The robot discards an answer of more than 64 KB (and with it a stop), so the answer is kept small:
# at most one call to start (its parameters: 16 KiB), MAX_CANCEL ids, MAX_REPORTS settled ids and
# MAX_REJECTED_SHOWN short explanations.
MAX_REJECTED_SHOWN = 20
MAX_REJECTED_ERROR = 120
MAX_CANCEL = 50
MAX_RESULT_BYTES = 16 * 1024
MAX_DETAIL, MAX_MESSAGE = 500, 500
MAX_INSTANCE_ID, MAX_NAME, MAX_FAMILY, MAX_VERSION = 200, 200, 30, 40
RATE_WINDOW_SECONDS = 60

_DIGEST = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,59}")
_RUN_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")


def invalid_sync(message: str) -> ApiError:
    return bad_request(message, code="invalid_sync")


# ---------------------------------------------------------------- what the robot says about itself
def _device(payload: dict) -> dict:
    device = payload.get("device")
    if not isinstance(device, dict):
        raise invalid_sync("device 须为对象")
    instance_id = device.get("instance_id")
    if (not isinstance(instance_id, str) or not 1 <= len(instance_id) <= MAX_INSTANCE_ID
            or not skill_params.storable(instance_id) or "\n" in instance_id or "\t" in instance_id):
        raise invalid_sync(f"device.instance_id 须为 1–{MAX_INSTANCE_ID} 个字符，不含控制字符")
    return {"instance_id": instance_id, "name": skill_params.cleaned(device.get("name"), MAX_NAME) or instance_id,
            "family": skill_params.cleaned(device.get("family"), MAX_FAMILY),
            "simulated": device.get("simulated") is True,
            "runtime_version": skill_params.cleaned(device.get("runtime_version"), MAX_VERSION)}


def _state(payload: dict) -> dict:
    """The robot's safety state. Fail closed: a flag that is missing or not a boolean counts as set
    (latched, busy, blocked), so a sloppy sync can never clear what an earlier one reported."""
    state = payload.get("state", {})
    if not isinstance(state, dict):
        raise invalid_sync("state 须为对象")
    return {key: state.get(key) is not False for key in ("estop_latched", "busy", "motion_blocked")}


def _listed(payload: dict, key: str, limit: int) -> list:
    items = payload.get(key, [])
    if not isinstance(items, list) or len(items) > limit:
        raise invalid_sync(f"{key} 须为不超过 {limit} 项的列表")
    return items


def _capability(item) -> str:
    cap_id = item.get("id") if isinstance(item, dict) else item
    if not isinstance(cap_id, str) or not skill_calls.SKILL_ID.fullmatch(cap_id):
        raise ValueError("requires 的每一项须为能力编号")
    return cap_id


def _moves(cap_id: str) -> bool:
    """Motion, or unknown to this cloud's capability standard (treated as motion: fail closed)."""
    capability = spec_caps.CAPABILITIES.get(cap_id)
    return capability is None or capability.safety == "motion"


def _remote(value) -> dict:
    value = value if isinstance(value, dict) else {}
    set_at = value.get("set_at")
    usable = not isinstance(set_at, bool) and isinstance(set_at, (int, float)) and 0 <= set_at < 1e11
    return {"allowed": value.get("allowed") is True, "set_by": skill_params.cleaned(value.get("set_by"), 200),
            "set_at": float(set_at) if usable else None}


def clean_skill(item) -> dict:
    """One installed skill as the cloud keeps it; ``ValueError`` (with the reason) when it cannot be kept.
    Remote calls are allowed only when the robot says so in so many words (``remote.allowed is True``)."""
    if not isinstance(item, dict):
        raise ValueError("技能须为对象")
    skill_id, version, digest = item.get("id"), item.get("version"), item.get("digest")
    if not isinstance(skill_id, str) or not skill_calls.SKILL_ID.fullmatch(skill_id):
        raise ValueError("id 须为技能编号（小写字母、数字、点、下划线、连字符，最多 100 个字符）")
    if not isinstance(version, str) or not skill_store.is_version(version):
        raise ValueError("version 须为语义化版本，如 0.1.0")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("digest 须为 64 位十六进制")
    parameters = item.get("parameters", [])
    skill_params.check_declarations(parameters)
    requires = item.get("requires", [])
    if not isinstance(requires, list) or len(requires) > MAX_REQUIRES:
        raise ValueError(f"requires 须为不超过 {MAX_REQUIRES} 项的列表")
    requires = [_capability(entry) for entry in requires]
    timeout = item.get("timeout_s", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise ValueError("timeout_s 须在 0–86400 秒之间")
    entry = {"id": skill_id, "version": version, "digest": digest,
             "title": skill_params.cleaned(item.get("title"), 100) or skill_id,
             "description": skill_params.cleaned(item.get("description"), 2000), "parameters": parameters,
             "requires": requires, "motion": item.get("motion") is True or any(_moves(cap) for cap in requires),
             "timeout_s": timeout, "remote": _remote(item.get("remote"))}
    problem = skill_params.json_problem(entry, MAX_SKILL_BYTES)
    if problem is not None:
        raise ValueError(f"这一项{problem}")
    return entry


def clean_skills(items: list) -> tuple[list[dict], list[dict]]:
    """``(kept, rejected)``. A bad entry is refused on its own; of two entries with one id the newer version stays."""
    kept: dict[str, dict] = {}
    rejected = []
    for index, item in enumerate(items):
        try:
            entry = clean_skill(item)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            given = item.get("id") if isinstance(item, dict) else None
            usable = isinstance(given, str) and bool(skill_calls.SKILL_ID.fullmatch(given))
            rejected.append({"index": index, "id": given if usable else None, "error": str(exc)[:MAX_REJECTED_ERROR]})
            continue
        other = kept.get(entry["id"])
        if other is None or skill_store.version_key(entry["version"]) > skill_store.version_key(other["version"]):
            kept[entry["id"]] = entry
    return [kept[skill_id] for skill_id in sorted(kept)], rejected


# ---------------------------------------------------------------- the robot's row
def _link_row(session: Session, p: Principal, device: dict, settings: Settings, at: float, ip: str) -> DeviceLink:
    """This robot's row, locked; created on its first sync (bounded per organisation)."""
    query = select(DeviceLink).where(DeviceLink.org_id == p.org_id, DeviceLink.instance_id == device["instance_id"])
    link = session.execute(query.with_for_update()).scalar_one_or_none()
    if link is not None:
        return link
    advisory_lock(session, f"device-link:{p.org_id}")
    link = session.execute(query.with_for_update()).scalar_one_or_none()         # created while we waited
    if link is not None:
        return link
    known = session.execute(select(func.count()).select_from(DeviceLink).where(DeviceLink.org_id == p.org_id)).scalar_one()
    if known >= settings.device_link_max_devices:
        raise conflict(f"这个组织连到云端的机器人已达上限（{settings.device_link_max_devices} 台）", code="too_many_devices")
    link = DeviceLink(org_id=p.org_id, instance_id=device["instance_id"], first_seen_at=at, last_seen_at=at, syncs=0,
                      skills=[], window_start=at, window_count=0)
    session.add(link)
    session.flush()
    audit.record(session, "device_link.first_sync", p.actor, org_id=p.org_id, target=link.id, ip=ip,
                 detail={"instance_id": link.instance_id})
    return link


def _count_sync(link: DeviceLink, settings: Settings, at: float) -> None:
    """At most ``device_link_max_syncs_per_minute`` per robot. A refused sync changes nothing, so the
    counter stays at the limit until the minute is over."""
    if at - link.window_start >= RATE_WINDOW_SECONDS or at < link.window_start:
        link.window_start, link.window_count = at, 0
    if link.window_count >= settings.device_link_max_syncs_per_minute:
        retry = max(1, int(link.window_start + RATE_WINDOW_SECONDS - at) + 1)
        raise ApiError(429, "rate_limited", "这台机器人同步过于频繁，请稍后再试", retry_after=retry)
    link.window_count += 1


def _allowed(skills: list[dict]) -> set[str]:
    return {entry["id"] for entry in skills if entry["remote"]["allowed"]}


def _remember(session: Session, p: Principal, link: DeviceLink, device: dict, state: dict, skills: list[dict],
              at: float) -> None:
    for key in ("name", "family", "simulated", "runtime_version"):
        setattr(link, key, device[key])
    for key, value in state.items():
        setattr(link, key, value)
    link.last_seen_at, link.syncs = at, (link.syncs or 0) + 1
    digest = hashlib.sha256(json.dumps(skills, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if digest == link.skills_digest:                           # the list is large: written only when it changes
        return
    before, after = _allowed(link.skills or []), _allowed(skills)
    link.skills, link.skills_digest, link.skill_count = skills, digest, len(skills)
    if before != after:                                        # which skills third parties may call: worth a record
        audit.record(session, "device_link.remote_changed", p.actor, org_id=link.org_id, target=link.id,
                     detail={"instance_id": link.instance_id, "opened": sorted(after - before),
                             "closed": sorted(before - after)})


# ---------------------------------------------------------------- what the robot reports
def _call_reports(items: list) -> list[dict]:
    """The entries of ``calls`` that are objects naming a call id; anything else is ignored."""
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("call_id"), str)
            and skill_calls.CALL_ID.fullmatch(item["call_id"])]


def _stop_ids(items: list) -> list[str]:
    return [item for item in items if isinstance(item, str) and skill_calls.STOP_ID.fullmatch(item)]


def _error(report: dict, status: str) -> dict:
    error = report.get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    return {"code": code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else status,
            "message": skill_params.cleaned(error.get("message"), MAX_MESSAGE)}


def _progress(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return 1.0 if value >= 1 else 0.0 if value <= 0 else float(value)


def _open_to_reports(call: SkillCall) -> bool:
    """May a report of the robot still change this call? Only one that was handed to a robot, and not
    after the robot itself reported its end. (``lost`` and a cancellation the cloud assumed are the
    cloud's guesses: what the robot says replaces them.)"""
    if call.dispatched_at is None:
        return False
    return call.status in (DISPATCHED, RUNNING, LOST) or (call.status == CANCELLED and call.reported_at is None)


def _apply_report(session: Session, call: SkillCall, report: dict, at: float) -> None:
    status = report.get("status")
    if status not in REPORTED or not _open_to_reports(call):
        return
    if call.status not in ACTIVE:                              # the cloud had guessed; the robot knows
        call.detail, call.error, call.finished_at = "", None, None
    call.reported_at = at
    run_id = report.get("run_id")
    if isinstance(run_id, str) and _RUN_ID.fullmatch(run_id):
        call.run_id = run_id
    progress = _progress(report.get("progress"))
    if progress is not None:
        call.progress = progress
    if isinstance(report.get("detail"), str):
        call.detail = skill_params.cleaned(report["detail"], MAX_DETAIL)
    call.started_at = call.started_at or (at if status != "rejected" else None)
    if status == RUNNING:
        call.status, call.finished_at, call.error = RUNNING, None, None
        return
    call.status, call.finished_at = status, at
    call.error = None if status == SUCCEEDED else _error(report, status)
    result = report.get("result")
    if result is not None and skill_params.json_problem(result, MAX_RESULT_BYTES) is None:
        call.result = {"value": result}
    elif result is not None:
        call.detail = f"结果没有保存：超过 {MAX_RESULT_BYTES} 字节或不是可保存的 JSON"
    if status == SUCCEEDED:
        call.progress = 1.0
    audit.record(session, "skill.call.finished", f"device:{call.device_id}", org_id=call.org_id, target=call.id,
                 detail={"status": status, "skill_id": call.skill_id, "run_id": call.run_id,
                         "error": (call.error or {}).get("code")})


def _take_reports(session: Session, link: DeviceLink, reports: list[dict], at: float) -> tuple[list[str], set[str]]:
    """Apply the robot's reports to the calls that are this robot's. Returns ``(settled, mentioned)``:
    ids the robot need not report again (final here, or not a call of this robot at all), and the ids
    of its own calls it mentioned."""
    ids = list(dict.fromkeys(report["call_id"] for report in reports))
    own = {call.id: call for call in session.execute(select(SkillCall).where(
        SkillCall.id.in_(ids), SkillCall.org_id == link.org_id, SkillCall.device_id == link.id)
        .order_by(SkillCall.id).with_for_update()).scalars()} if ids else {}
    for report in reports:
        call = own.get(report["call_id"])
        if call is not None:
            _apply_report(session, call, report, at)
    settled = [call_id for call_id in ids if call_id not in own or own[call_id].status not in ACTIVE]
    return settled, set(own)


def _acknowledge_stops(session: Session, link: DeviceLink, stop_ids: list[str], at: float) -> None:
    if not stop_ids:
        return
    for stop in session.execute(select(DeviceStop).where(
            DeviceStop.id.in_(stop_ids), DeviceStop.org_id == link.org_id, DeviceStop.device_id == link.id,
            DeviceStop.acknowledged_at.is_(None))).scalars():
        stop.acknowledged_at, stop.expired_at = at, None
        stop.delivered_at = stop.delivered_at or at
        audit.record(session, "skill.stop.acknowledged", f"device:{link.id}", org_id=link.org_id, target=stop.id,
                     detail={"instance_id": link.instance_id})


# ---------------------------------------------------------------- what the robot is told
def _end(session: Session, call: SkillCall, status: str, at: float, code: str | None, message: str) -> None:
    """End a call by the cloud's own decision, on the record."""
    skill_calls.finish(call, status, at, code, message)
    audit.record(session, "skill.call.finished", skill_calls.CLOUD_ACTOR, org_id=call.org_id, target=call.id,
                 detail={"status": status, "skill_id": call.skill_id, "device_id": call.device_id, "error": code})


def _dispatch_refusal(link: DeviceLink, call: SkillCall) -> tuple[str, str] | None:
    """Why a waiting call is not handed over after all, by what the robot said in this very sync: the
    owner's switch and the latch hold here too, not only on the robot."""
    skill = skill_calls.installed_skill(link, call.skill_id)
    if skill is None:
        return "skill_not_installed", "这个技能已经不在机器人上"
    if skill["version"] != call.version or skill["digest"] != call.digest:
        return "version_mismatch", "机器人上的这个技能在调用发出之后换了内容"
    if not skill["remote"]["allowed"]:
        return "remote_not_allowed", "机器人的主人关闭了这个技能的远程调用"
    if link.estop_latched:
        return "estop_latched", "机器人处于急停锁存状态"
    return None


def _unanswered(session: Session, call: SkillCall, mentioned: bool, settings: Settings, at: float) -> bool:
    """Settle what the robot's silence about a handed-over call means. True when the call ended here.

    * Never mentioned and cancelled since: the robot is told to cancel it and it is not sent again;
      after ``skill_call_pickup_timeout_seconds`` of syncs that do not know it, it never arrived.
    * Never mentioned for ``skill_call_lost_after_seconds``: the robot does not take it; unknown outcome.
    * Mentioned once and then no more for that long: unknown outcome as well."""
    if mentioned:
        return False
    if call.reported_at is None and call.cancel_requested_at is not None:
        if at - max(call.dispatched_at, call.cancel_requested_at) < settings.skill_call_pickup_timeout_seconds:
            return False
        _end(session, call, CANCELLED, at, None, "调用在到达机器人之前被取消")
        return True
    since = call.dispatched_at if call.reported_at is None else call.reported_at
    if since >= at - settings.skill_call_lost_after_seconds:
        return False
    _end(session, call, LOST, at, "lost", "机器人还在同步，但没有（或不再）报告这次调用；结果未知（不等于失败）")
    return True


def _outgoing(session: Session, link: DeviceLink, mentioned: set[str], settings: Settings, at: float) -> dict:
    """Decide what this answer carries: calls to start (one at a time, sent again until the robot
    mentions them), calls to cancel, and a stop."""
    stop = skill_calls.pending_stop(session, link.id)
    calls, cancel = [], []
    locked = session.execute(select(SkillCall).where(SkillCall.device_id == link.id, SkillCall.status.in_(ACTIVE))
                             .order_by(SkillCall.id).with_for_update()).scalars().all()      # locks in id order
    active = sorted(locked, key=lambda call: (call.created_at, call.id))
    pickup = settings.skill_call_pickup_timeout_seconds
    occupied = any(call.status in (DISPATCHED, RUNNING) for call in active)
    for call in active:
        if call.status == QUEUED:
            if stop is not None or occupied:
                continue                                        # nothing new while a stop is on its way or a call runs
            refusal = _dispatch_refusal(link, call)
            if refusal is not None:
                _end(session, call, REJECTED, at, *refusal)
                continue
            call.status, call.dispatched_at, occupied = DISPATCHED, at, True
        elif _unanswered(session, call, call.id in mentioned, settings, at):
            continue
        if (call.cancel_requested_at is None
                and at > call.dispatched_at + call.timeout_s + settings.skill_call_lost_after_seconds):
            # Far past its own time limit and the robot still has not ended it: ask it to.
            call.cancel_requested_at, call.cancel_requested_by = at, skill_calls.CLOUD_ACTOR
            audit.record(session, "skill.call.timeout", skill_calls.CLOUD_ACTOR, org_id=call.org_id, target=call.id,
                         detail={"device_id": call.device_id, "timeout_s": call.timeout_s})
        # Never mentioned and past the pick-up time: it is not sent again (a call must not start late).
        # The robot is told to cancel it in case it did arrive; _unanswered ends it as lost in time.
        overdue = call.reported_at is None and at - call.dispatched_at > pickup
        if call.cancel_requested_at is not None or overdue:
            cancel.append(call.id)
        elif call.reported_at is None and not calls:            # sent again until the robot mentions it
            calls.append({"call_id": call.id, "skill_id": call.skill_id, "version": call.version,
                          "digest": call.digest, "parameters": call.parameters or {}, "timeout_s": call.timeout_s,
                          # With ``server_time`` of the same answer the robot judges the call's age by the
                          # cloud's clock alone and refuses one that is too old, whatever was re-sent.
                          "created_at": call.created_at})
    if stop is not None and stop.delivered_at is None:
        stop.delivered_at = at
    busy = any(call.status in ACTIVE for call in active)
    return {"calls": calls, "cancel": cancel[:MAX_CANCEL],
            "stop": {"stop_id": stop.id, "reason": stop.reason} if stop is not None else None,
            "poll_after_s": settings.device_link_poll_busy_seconds if busy else settings.device_link_poll_seconds}


def sync(session: Session, p: Principal, payload, settings: Settings, ip: str = "") -> dict:
    """``POST /device-link/sync`` → ``{accepted, device_id, poll_after_s, calls, cancel, stop, settled,
    rejected_skills, rejected_skills_total, server_time}``."""
    p.require(DEVICE_LINK_SCOPE)
    if p.kind != "api_key":
        raise forbidden("机器人同步只接受带 device:link 的 API Key，不接受登录会话", code="api_key_required")
    skill_calls.verified(p)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise invalid_sync(f"需要 {SCHEMA}")
    device, state = _device(payload), _state(payload)
    skills, rejected = clean_skills(_listed(payload, "skills", MAX_SKILLS))
    reports = _call_reports(_listed(payload, "calls", MAX_REPORTS))
    stop_ids = _stop_ids(_listed(payload, "stops", MAX_REPORTS))
    at = skill_calls.now()
    link = _link_row(session, p, device, settings, at, ip)
    _count_sync(link, settings, at)
    _remember(session, p, link, device, state, skills, at)
    session.flush()
    skill_calls.refresh(session, settings, at, device_id=link.id)
    settled, mentioned = _take_reports(session, link, reports, at)
    _acknowledge_stops(session, link, stop_ids, at)            # after the calls: one lock order everywhere
    answer = _outgoing(session, link, mentioned, settings, at)
    return {"accepted": True, "device_id": link.id, **answer, "settled": settled,
            "rejected_skills": rejected[:MAX_REJECTED_SHOWN], "rejected_skills_total": len(rejected), "server_time": at}
