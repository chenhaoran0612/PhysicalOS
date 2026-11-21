"""Device alerts (issue #26, requirement R7-08): receive what devices report, show it, notice silence.

* A device sends ``neuxmind.device_report.v1`` to ``POST /device-reports`` every five minutes with an
  API key whose only scope is ``alerts:report``: the alerts the cloud does not have yet (or none: "still
  here"). Each alert is stored once per ``(organisation, device, the device's alert id)``, so a report
  that is sent again changes nothing. A bad alert is refused on its own, the rest of the report stands.
* People read the alerts with the ``robots`` permission and mark them handled.
* A device whose last report is older than ``device_offline_missed`` (3) intervals of
  ``device_report_interval_seconds`` (300 s) plus ``device_offline_grace_seconds`` (60 s: the third
  report may be a little late) is offline; the cloud raises one "device offline" alert per silence
  (``sweep_offline``: the maintenance sweep, and whenever someone looks).

Trust: the device names itself (``instance_id``) and the key belongs to the organisation, so a key
can report for any device of its own organisation and for no other organisation. Only an API key
reports -- a login session cannot. What a key can make the cloud store is bounded: reports per device
and per organisation per interval, devices per organisation, alerts kept per device and per
organisation, alerts per report, the size and the characters of every text. When room is needed,
alerts someone has marked handled go first, then minor ones; an unhandled critical alert goes last.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from . import audit
from .alert_models import DeviceAlert, DeviceReport
from .auth import Principal, _throttle
from .config import Settings
from .db import Database, advisory_lock
from .errors import ApiError, bad_request, conflict, forbidden, not_found
from .models import Robot, new_id, now

REPORT_SCHEMA = "neuxmind.device_report.v1"
REPORT_SCOPE = "alerts:report"
READ_PERMISSION = "robots"
LEVELS = ("critical", "warning", "info")
OFFLINE_KIND = "device_offline"
OFFLINE_ADVICE = "检查设备的电源和网络，确认管理层在运行；恢复之后，断线期间的告警会自动补发"
MAX_ALERTS_PER_REPORT = 200
MAX_TEXT = 1000                 # reason / advice (the runtime sends at most 500 characters)
MAX_DETAIL_BYTES = 4000
MAX_INSTANCE_ID, MAX_NAME, MAX_FAMILY, MAX_VERSION = 200, 200, 30, 40
MAX_TS = 1e11                   # seconds; far beyond any clock, well inside a float
MAX_PENDING = 1_000_000
MAX_PAGE = 200
DEFAULT_PAGE = 50
SWEEP_ACTOR = "system:sweeper"

_ALERT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_KIND = re.compile(r"[a-z0-9][a-z0-9_.-]{0,59}")
_ROW_ID = re.compile(r"alr_[0-9a-f]{20}")
_CURSOR = re.compile(r"(\d{1,12}(?:\.\d{1,9})?)~(alr_[0-9a-f]{20})")
_ALLOWED_CONTROL = ("\n", "\t")


def invalid_report(message: str) -> ApiError:
    return bad_request(message, code="invalid_report")


# ---------------------------------------------------------------- text the database can hold
def _is_control(ch: str) -> bool:
    return ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0


def _storable(text: str) -> bool:
    """UTF-8 encodable (no lone surrogate) and free of control characters other than newline and tab:
    PostgreSQL refuses NUL in text, and nothing downstream should have to escape the rest."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(_is_control(ch) and ch not in _ALLOWED_CONTROL for ch in text)


def _has_nul(value) -> bool:
    """A NUL character anywhere in a JSON value (its text form cannot tell one from the six characters
    of the escape written out)."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any("\x00" in str(key) or _has_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_nul(item) for item in value)
    return False


def _instance_filter(value: str | None) -> str | None:
    """A device name given in the address: usable as a query value, or 400."""
    if value is None:
        return None
    if not 1 <= len(value) <= MAX_INSTANCE_ID or not _storable(value) or any(ch in value for ch in _ALLOWED_CONTROL):
        raise bad_request(f"instance_id 须为 1–{MAX_INSTANCE_ID} 个字符，不含控制字符")
    return value


def _cleaned(value, limit: int) -> str:
    """A descriptive field (a device's name ...): made storable rather than refused."""
    if not isinstance(value, str):
        return ""
    text = value.encode("utf-8", "replace").decode("utf-8")
    return "".join(" " if _is_control(ch) else ch for ch in text).strip()[:limit]


# ---------------------------------------------------------------- receiving
def _device(payload: dict) -> dict:
    device = payload.get("device")
    if not isinstance(device, dict):
        raise invalid_report("device 须为对象")
    instance_id = device.get("instance_id")
    if (not isinstance(instance_id, str) or not 1 <= len(instance_id) <= MAX_INSTANCE_ID
            or not _storable(instance_id) or any(ch in instance_id for ch in _ALLOWED_CONTROL)):
        raise invalid_report(f"device.instance_id 须为 1–{MAX_INSTANCE_ID} 个字符，不含控制字符")
    return {"instance_id": instance_id, "name": _cleaned(device.get("name"), MAX_NAME) or instance_id,
            "family": _cleaned(device.get("family"), MAX_FAMILY), "simulated": device.get("simulated") is True,
            "runtime_version": _cleaned(device.get("runtime_version"), MAX_VERSION)}


def _pending(payload: dict) -> int:
    value = payload.get("pending")
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PENDING else 0


def _check_alert(item) -> dict:
    """The fields of one reported alert that are kept; ``ValueError`` (with the reason) otherwise."""
    if not isinstance(item, dict):
        raise ValueError("告警须为对象")
    alert_id, kind, level, ts = item.get("id"), item.get("kind"), item.get("level"), item.get("ts")
    reason, advice, detail = item.get("reason"), item.get("advice", ""), item.get("detail", {})
    if not isinstance(alert_id, str) or not _ALERT_ID.fullmatch(alert_id):
        raise ValueError("id 须为 1–100 个字母、数字或 _ . : -")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise ValueError("kind 须为 1–60 个小写字母、数字、下划线、点或连字符")
    if level not in LEVELS:
        raise ValueError(f"level 须为 {' / '.join(LEVELS)}")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not 0 <= ts < MAX_TS:     # NaN fails the comparison
        raise ValueError("ts 须为时间戳（秒）")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_TEXT or not _storable(reason):
        raise ValueError(f"reason 须为 1–{MAX_TEXT} 个字符，不含控制字符")
    if not isinstance(advice, str) or len(advice) > MAX_TEXT or not _storable(advice):
        raise ValueError(f"advice 须为不超过 {MAX_TEXT} 个字符的文字，不含控制字符")
    if not isinstance(detail, dict):
        raise ValueError("detail 须为对象")
    try:
        size = len(json.dumps(detail, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        nul = _has_nul(detail)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("detail 须为可以保存的 JSON") from None
    if size > MAX_DETAIL_BYTES or nul:
        raise ValueError(f"detail 过大（上限 {MAX_DETAIL_BYTES} 字节）或含有空字符")
    return {"alert_id": alert_id, "kind": kind, "level": level, "ts": float(ts), "reason": reason.strip(),
            "advice": advice.strip(), "detail": detail}


def _check_alerts(items: list) -> tuple[list[dict], list[dict]]:
    checked, rejected = [], []
    for index, item in enumerate(items):
        try:
            checked.append(_check_alert(item))
        except ValueError as exc:
            given = item.get("id") if isinstance(item, dict) else None
            usable = isinstance(given, str) and bool(_ALERT_ID.fullmatch(given))
            rejected.append({"index": index, "id": given if usable else None, "error": str(exc)})
    return checked, rejected


def _device_row(session: Session, principal: Principal, device: dict, settings: Settings, at: float,
                ip: str) -> tuple[DeviceReport, str | None]:
    """This device's row, locked, created on its first report; and the robot registered under its id."""
    org_id, instance_id = principal.org_id, device["instance_id"]
    row = session.execute(select(DeviceReport).where(DeviceReport.org_id == org_id,
                                                     DeviceReport.instance_id == instance_id)
                          .with_for_update()).scalar_one_or_none()
    robot_id = session.execute(select(Robot.id).where(Robot.org_id == org_id,
                                                      Robot.instance_id == instance_id)).scalar_one_or_none()
    if row is None:
        known = session.execute(select(func.count()).select_from(DeviceReport)
                                .where(DeviceReport.org_id == org_id)).scalar_one()
        if known >= settings.device_report_max_devices:
            raise conflict(f"这个组织上报过的设备已达上限（{settings.device_report_max_devices} 台）", code="too_many_devices")
        row = DeviceReport(org_id=org_id, instance_id=instance_id, first_report_at=at, reports=0)
        session.add(row)
        audit.record(session, "device.first_report", principal.actor, org_id=org_id, target=instance_id,
                     detail={"robot_id": robot_id}, ip=ip)
    elif row.offline_alert_id is not None:
        _recovered(session, row, at)
    return row, robot_id


def _store(session: Session, org_id: str, device: dict, robot_id: str | None, checked: list[dict],
           at: float) -> tuple[list[str], list[str]]:
    """Insert the alerts the cloud does not have yet. ``(accepted, duplicates)`` ids."""
    accepted, duplicates = [], []
    if not checked:
        return accepted, duplicates
    instance_id = device["instance_id"]
    have = set(session.execute(select(DeviceAlert.alert_id).where(
        DeviceAlert.org_id == org_id, DeviceAlert.instance_id == instance_id,
        DeviceAlert.alert_id.in_([c["alert_id"] for c in checked]))).scalars())
    for fields in checked:
        if fields["alert_id"] in have:
            duplicates.append(fields["alert_id"])
            continue
        have.add(fields["alert_id"])
        session.add(DeviceAlert(org_id=org_id, instance_id=instance_id, robot_id=robot_id, source="device",
                                device_name=device["name"], received_at=at + len(accepted) * 1e-6, **fields))
        accepted.append(fields["alert_id"])
    return accepted, duplicates


def receive(session: Session, principal: Principal, payload, settings: Settings, ip: str = "") -> dict:
    """``POST /device-reports``. Returns ``{accepted, duplicates, rejected, received_at, device}``."""
    principal.require(REPORT_SCOPE)
    if principal.kind != "api_key":
        raise forbidden("设备上报只接受带 alerts:report 的 API Key，不接受登录会话", code="api_key_required")
    if not isinstance(payload, dict) or payload.get("schema") != REPORT_SCHEMA:
        raise invalid_report(f"需要 {REPORT_SCHEMA}")
    device = _device(payload)
    items = payload.get("alerts", [])
    if not isinstance(items, list) or len(items) > MAX_ALERTS_PER_REPORT:
        raise invalid_report(f"alerts 须为不超过 {MAX_ALERTS_PER_REPORT} 条的列表")
    org_id, instance_id = principal.org_id, device["instance_id"]
    window = settings.device_report_interval_seconds
    digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:32]
    # the organisation's counter first: it also puts the reports of one organisation in a row, so two
    # first reports cannot both pass the device limit
    _throttle(session, "device_report_org", org_id, settings.device_report_org_max_per_window, window,
              "这个组织的设备上报过于频繁，请稍后再报")
    advisory_lock(session, f"device-report:{org_id}:{digest}")
    _throttle(session, "device_report", f"{org_id}:{digest}", settings.device_report_max_per_window, window,
              "这台设备上报过于频繁，请稍后再报")
    at = now()
    row, robot_id = _device_row(session, principal, device, settings, at, ip)
    for key in ("name", "family", "simulated", "runtime_version"):
        setattr(row, key, device[key])
    row.last_report_at = at
    row.reports = (row.reports or 0) + 1
    row.pending = _pending(payload)
    checked, rejected = _check_alerts(items)
    accepted, duplicates = _store(session, org_id, device, robot_id, checked, at)
    if accepted:
        session.flush()
        _trim(session, org_id, instance_id, settings)
    if accepted or rejected:
        levels = {level: sum(1 for c in checked if c["level"] == level and c["alert_id"] in accepted)
                  for level in LEVELS}
        audit.record(session, "device.alerts", principal.actor, org_id=org_id, target=instance_id, ip=ip,
                     detail={"accepted": len(accepted), "rejected": len(rejected),
                             "levels": {k: v for k, v in levels.items() if v}})
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "received_at": at,
            "device": {"instance_id": instance_id, "robot_id": robot_id, "state": "online"}}


def _evict(session: Session, where: tuple, excess: int) -> None:
    """Delete ``excess`` alerts: handled ones first, then info, warning, critical; oldest first within each."""
    weight = case((DeviceAlert.level == "info", 0), (DeviceAlert.level == "warning", 1), else_=2)
    ids = session.execute(select(DeviceAlert.id).where(*where)
                          .order_by(DeviceAlert.handled_at.is_(None), weight, DeviceAlert.received_at, DeviceAlert.id)
                          .limit(excess)).scalars().all()
    session.execute(delete(DeviceAlert).where(DeviceAlert.id.in_(ids)))


def _trim(session: Session, org_id: str, instance_id: str, settings: Settings) -> None:
    """Keep at most ``device_alert_keep`` alerts of this device and ``device_alert_org_keep`` of the organisation."""
    for where, keep in (((DeviceAlert.org_id == org_id, DeviceAlert.instance_id == instance_id),
                         settings.device_alert_keep),
                        ((DeviceAlert.org_id == org_id,), settings.device_alert_org_keep)):
        total = session.execute(select(func.count()).select_from(DeviceAlert).where(*where)).scalar_one()
        if total > keep:
            _evict(session, where, total - keep)


# ---------------------------------------------------------------- offline
def offline_after(settings: Settings) -> int:
    """Seconds of silence after which a device is offline: the reports missed, and a little grace."""
    return (settings.device_offline_missed * settings.device_report_interval_seconds
            + settings.device_offline_grace_seconds)


def _recovered(session: Session, row: DeviceReport, at: float) -> None:
    alert = session.execute(select(DeviceAlert).where(DeviceAlert.id == row.offline_alert_id,
                                                      DeviceAlert.org_id == row.org_id)).scalar_one_or_none()
    if alert is not None:
        alert.detail = {**(alert.detail or {}), "recovered_at": at}
    row.offline_alert_id = None


def _offline_alert(row: DeviceReport, robot_id: str | None, settings: Settings, at: float) -> DeviceAlert:
    identity = new_id("alr")              # raised here: its own id serves as the alert id
    last = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row.last_report_at))
    minutes = settings.device_report_interval_seconds / 60
    return DeviceAlert(
        id=identity, org_id=row.org_id, instance_id=row.instance_id, robot_id=robot_id, source="cloud",
        alert_id=identity, kind=OFFLINE_KIND, level="warning", ts=row.last_report_at + offline_after(settings),
        received_at=at, device_name=row.name or row.instance_id,
        reason=(f"设备离线：云平台已连续 {settings.device_offline_missed} 次没有收到这台设备的上报"
                f"（每 {minutes:g} 分钟一次），最后一次在 {last} UTC"),
        advice=OFFLINE_ADVICE,
        detail={"last_report_at": row.last_report_at, "missed": settings.device_offline_missed,
                "interval_s": settings.device_report_interval_seconds})


def mark_offline(session: Session, settings: Settings, at: float, org_id: str | None = None) -> int:
    """Raise the "device offline" alert for every device (of ``org_id``, or of every organisation)
    that has gone silent and has none yet. Returns how many were raised. Idempotent: the row lock and
    ``offline_alert_id`` keep a second sweep from raising it again."""
    query = select(DeviceReport).where(DeviceReport.offline_alert_id.is_(None),
                                       DeviceReport.last_report_at < at - offline_after(settings))
    if org_id is not None:
        query = query.where(DeviceReport.org_id == org_id)
    rows = session.execute(query.order_by(DeviceReport.id).with_for_update()).scalars().all()
    for index, row in enumerate(rows):
        robot_id = session.execute(select(Robot.id).where(Robot.org_id == row.org_id,
                                                          Robot.instance_id == row.instance_id)).scalar_one_or_none()
        alert = _offline_alert(row, robot_id, settings, at + index * 1e-6)
        session.add(alert)
        row.offline_alert_id = alert.id
        audit.record(session, "device.offline", SWEEP_ACTOR, org_id=row.org_id, target=row.instance_id,
                     detail={"alert_id": alert.id, "last_report_at": row.last_report_at})
    if rows:
        session.flush()
    return len(rows)


def sweep_offline(db: Database, settings: Settings, at: float | None = None) -> int:
    """The maintenance sweep's part: every organisation, one transaction."""
    with db.transaction() as session:
        return mark_offline(session, settings, now() if at is None else at)


# ---------------------------------------------------------------- reading
def alert_view(alert: DeviceAlert) -> dict:
    return {"id": alert.id, "alert_id": alert.alert_id, "source": alert.source, "instance_id": alert.instance_id,
            "robot_id": alert.robot_id, "device_name": alert.device_name, "kind": alert.kind, "level": alert.level,
            "ts": alert.ts, "received_at": alert.received_at, "reason": alert.reason, "advice": alert.advice,
            "detail": alert.detail or {}, "handled": alert.handled_at is not None, "handled_at": alert.handled_at,
            "handled_by": alert.handled_by}


def _flag(value: str | None, name: str) -> bool | None:
    if value is None:
        return None
    if value not in ("true", "false"):
        raise bad_request(f"{name} 须为 true 或 false")
    return value == "true"


def _page_size(limit: str | None) -> int:
    try:
        size = DEFAULT_PAGE if limit is None else int(limit)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_PAGE:
        raise bad_request(f"limit 须为 1–{MAX_PAGE}")
    return size


def list_alerts(session: Session, principal: Principal, settings: Settings, *, instance_id: str | None = None,
                level: str | None = None, handled: str | None = None, limit: str | None = None,
                before: str | None = None) -> dict:
    """``GET /alerts``: newest first; ``before`` is ``next_before`` of the previous page."""
    principal.require(READ_PERMISSION)
    size = _page_size(limit)
    if level is not None and level not in LEVELS:
        raise bad_request(f"level 须为 {' / '.join(LEVELS)}")
    is_handled = _flag(handled, "handled")
    instance_id = _instance_filter(instance_id)
    cursor = None if before is None else _CURSOR.fullmatch(before)
    if before is not None and cursor is None:
        raise bad_request("before 须为上一页返回的 next_before")
    mark_offline(session, settings, now(), principal.org_id)
    query = select(DeviceAlert).where(DeviceAlert.org_id == principal.org_id)
    if instance_id is not None:
        query = query.where(DeviceAlert.instance_id == instance_id)
    if level is not None:
        query = query.where(DeviceAlert.level == level)
    if is_handled is not None:
        query = query.where(DeviceAlert.handled_at.is_not(None) if is_handled else DeviceAlert.handled_at.is_(None))
    if cursor is not None:
        at, last_id = float(cursor.group(1)), cursor.group(2)
        query = query.where((DeviceAlert.received_at < at)
                            | ((DeviceAlert.received_at == at) & (DeviceAlert.id < last_id)))
    rows = session.execute(query.order_by(DeviceAlert.received_at.desc(), DeviceAlert.id.desc())
                           .limit(size + 1)).scalars().all()
    page, more = rows[:size], len(rows) > size
    return {"alerts": [alert_view(a) for a in page], "more": more,
            "next_before": f"{page[-1].received_at!r}~{page[-1].id}" if more else None}


def set_handled(session: Session, principal: Principal, alert_id: str, payload) -> dict:
    """``POST /alerts/{id}/handled`` ``{"handled": true|false}`` (default true)."""
    principal.require(READ_PERMISSION)
    handled = payload.get("handled", True) if isinstance(payload, dict) else None
    if not isinstance(handled, bool):
        raise bad_request("handled 须为 true 或 false")
    alert = None
    if _ROW_ID.fullmatch(alert_id):                      # anything else is not an id of ours: no query for it
        alert = session.execute(select(DeviceAlert).where(DeviceAlert.id == alert_id,
                                                          DeviceAlert.org_id == principal.org_id)).scalar_one_or_none()
    if alert is None:
        raise not_found("告警不存在")
    alert.handled_at = now() if handled else None
    alert.handled_by = principal.actor if handled else None
    audit.record(session, "alert.handled" if handled else "alert.unhandled", principal.actor, org_id=principal.org_id,
                 target=alert.id, detail={"instance_id": alert.instance_id, "kind": alert.kind})
    return alert_view(alert)


def list_devices(session: Session, principal: Principal, settings: Settings) -> dict:
    """``GET /alerts/devices``: every device that reports; offline ones first."""
    principal.require(READ_PERMISSION)
    at = now()
    mark_offline(session, settings, at, principal.org_id)
    limit = offline_after(settings)
    open_alerts = dict(session.execute(
        select(DeviceAlert.instance_id, func.count()).where(DeviceAlert.org_id == principal.org_id,
                                                           DeviceAlert.handled_at.is_(None))
        .group_by(DeviceAlert.instance_id)).all())
    robots = dict(session.execute(select(Robot.instance_id, Robot.id).where(Robot.org_id == principal.org_id)).all())
    devices = []
    for row in session.execute(select(DeviceReport).where(DeviceReport.org_id == principal.org_id)
                               .order_by(DeviceReport.instance_id)).scalars():
        offline = row.last_report_at < at - limit
        devices.append({"instance_id": row.instance_id, "robot_id": robots.get(row.instance_id), "name": row.name,
                        "family": row.family, "simulated": row.simulated, "runtime_version": row.runtime_version,
                        "state": "offline" if offline else "online", "first_report_at": row.first_report_at,
                        "last_report_at": row.last_report_at, "reports": row.reports, "offline_after_s": limit,
                        "pending_alerts": row.pending or 0,
                        "unhandled_alerts": open_alerts.get(row.instance_id, 0)})
    ordered = sorted(devices, key=lambda d: d["state"] != "offline")          # stable: offline first, names in order
    return {"devices": ordered, "report_interval_s": settings.device_report_interval_seconds,
            "offline_after_missed": settings.device_offline_missed}


def forget_device(session: Session, principal: Principal, instance_id: str | None, ip: str = "") -> dict:
    """``DELETE /alerts/devices?instance_id=``: stop watching a device that is gone for good (its
    alerts stay). Should it report again, it is on the list again."""
    principal.require(READ_PERMISSION)
    if not instance_id:
        raise bad_request("需要 instance_id")
    row = session.execute(select(DeviceReport).where(DeviceReport.org_id == principal.org_id,
                                                     DeviceReport.instance_id == _instance_filter(instance_id))
                          .with_for_update()).scalar_one_or_none()
    if row is None:
        raise not_found("没有这台设备的上报记录")
    session.delete(row)
    audit.record(session, "device.forgotten", principal.actor, org_id=principal.org_id, target=row.instance_id,
                 detail={"last_report_at": row.last_report_at, "reports": row.reports}, ip=ip)
    return {"forgotten": True, "instance_id": row.instance_id}
