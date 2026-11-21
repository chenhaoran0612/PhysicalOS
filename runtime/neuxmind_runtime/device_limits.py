"""Safety limits that a person fills in for this device (issue #22, requirement R1-06).

The platform does not guess how fast a body may go. A person fills in, per device:

* ``max_linear_speed``   (m/s)   the fastest the chassis may translate, in any direction;
* ``max_angular_speed``  (rad/s) the fastest it may turn;
* ``command_timeout_s``  (s)     how long a velocity command lasts without being refreshed.

Until that is done the safety supervisor refuses **every** motion capability with
``409 safety_limits_missing`` -- the runtime still starts, reports its state and obeys stop.

Rules
-----
* A body that does not call itself simulated always needs filled limits. A simulated body does
  only when its manifest declares a chassis without ``max_vx`` / ``max_wz`` (for example the ROS 2
  bridge on a simulator, started without limits in its configuration): a simulator that brings
  its own limits keeps working without anybody filling anything in.
* Filled values never loosen what the body declares: the supervisor enforces the lower of the two.
* A value above the platform maximum, or above what the body itself declares, is refused when it
  is written. The command timeout stays inside the supervisor's 0.05–2 s.
* The record lives in the runtime's store on the device, with who filled it and when, a revision
  counter, the last :data:`HISTORY_KEPT` changes and the body it was filled for
  (:func:`body_signature`: identity and declared chassis limits). A record that cannot be read, is
  out of range or was filled for another body (a state directory copied from another robot, a
  different adapter, changed declared limits) counts as *not filled* (fail closed). Two robots
  with the same ``instance_id``, model and declared limits cannot be told apart: give every robot
  its own ``instance_id``.
* The limits are not part of the adapter's manifest: the manifest must not change while the
  runtime runs (``manifest_changed``), these may.

Only the chassis group (``base``) exists so far. Arm limits are not defined yet; they get their own
group in :data:`GROUPS` (and their own use in the supervisor's limiters) once they are.

Nothing here is on the stop path: stop, halt and the velocity watchdog never read the limits,
never take :class:`DeviceLimits`' lock and never wait for the store.
"""
from __future__ import annotations

import copy
import math
import threading
import time
import unicodedata
from collections.abc import Mapping
from typing import Any

from . import effects
from .errors import BadRequest, Conflict, RuntimeApiError

SCHEMA = "neuxmind.device_limits.v1"
RECORD_KIND, RECORD_ID = "safety", "device_limits"
MISSING_CODE = "safety_limits_missing"
HISTORY_KEPT = 20
SET_BY_MAX_CHARS = 200    # room for an account's e-mail address when it has no display name
WRITE_WAIT_S = 2.0        # how long a write waits for another write (e.g. one stuck on a slow disk)

# The most the platform accepts, whatever the body is. Not an estimate of any robot: a ceiling that
# catches a slipped decimal point. A body that declares less is held to what it declares.
PLATFORM_MAX_LINEAR_SPEED = 3.0       # m/s
PLATFORM_MAX_ANGULAR_SPEED = 3.0      # rad/s
# The velocity watchdog's range (safety.MIN/MAX_VELOCITY_TIMEOUT_S are these two values).
MIN_COMMAND_TIMEOUT_S = 0.05
MAX_COMMAND_TIMEOUT_S = 2.0

BASE = "base"
# field -> (unit, minimum, whether the minimum itself is excluded, platform maximum)
BASE_FIELDS = {
    "max_linear_speed": ("m/s", 0.0, True, PLATFORM_MAX_LINEAR_SPEED),
    "max_angular_speed": ("rad/s", 0.0, True, PLATFORM_MAX_ANGULAR_SPEED),
    "command_timeout_s": ("s", MIN_COMMAND_TIMEOUT_S, False, MAX_COMMAND_TIMEOUT_S),
}
GROUPS = {BASE: BASE_FIELDS}
FIELD_NAMES = {"max_linear_speed": "最大线速度", "max_angular_speed": "最大角速度",
               "command_timeout_s": "运动指令超时"}

UNFILLED_REASON = ("这台设备还没有填写安全限值（最大线速度、最大角速度、运动指令超时），"
                   "所有运动指令都会被拒绝；停止始终可用。请在控制台里为这台设备填写")
MOVING_REASON = "有运行尚未结束，或速度指令仍在生效：这时不能修改安全限值。请等它结束，或先停止"
NOT_SAVED_REASON = "安全限值没有保存成功（设备上的存储写入失败），限值保持原样；请检查设备的磁盘后重新填写"
BUSY_REASON = "另一次安全限值的保存还没有结束（设备上的存储可能很慢）；限值保持原样，请稍后重新读取再填写"
OTHER_DEVICE_REASON = ("记录是为另一台本体（{theirs}）填写的，这台是 {ours}；"
                       "本体的身份或它自己声明的底盘上限变了，都要由人重新填写")
# Characters that reorder or hide text in a name (bidirectional controls): refused in ``set_by``.
BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
VALIDATE_CASE = "安全限值已填写"       # name of the /v1/validate check


class LimitsError(BadRequest):
    """A limits document that is malformed (``limits_invalid``) or outside what may be filled in
    (``limits_out_of_range``). ``extra`` names the field."""

    def __init__(self, message: str, *, code: str = "limits_invalid", field: str | None = None, **extra: Any):
        super().__init__(message, code=code, extra={**({"field": field} if field else {}), **extra})


def _number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:               # an int too large for a float
        return False


def _positive(value: Any) -> float | None:
    return float(value) if _number(value) and value > 0 else None


# ----------------------------------------------------------------------------------- rules
def required_for(simulated: Any, velocity_params: Mapping | None) -> bool:
    """Whether a body cannot move until a person has filled in its limits.

    ``simulated``: the manifest's flag (only a literal ``True`` counts). ``velocity_params``: the
    ``params`` of its ``base.velocity`` capability, ``None`` when it has no such capability."""
    if simulated is not True:
        return True
    if velocity_params is None:
        return False
    return velocity_params.get("max_vx") is None or velocity_params.get("max_wz") is None


def body_ceilings(velocity_params: Mapping | None, safety: Mapping | None) -> dict:
    """What the body's own manifest allows at most, per fillable field (``None``: not declared).

    Linear speed is the fastest the supervisor would let the chassis translate on the manifest
    alone: ``max_vx`` (combined with ``max_vy`` on a holonomic body), capped by
    ``safety.max_linear_speed`` when that is declared. So "filled value <= this" means the filled
    value is what is enforced, on every axis and for the combined speed."""
    params, safety = velocity_params or {}, safety or {}
    candidates = [_positive(safety.get("max_linear_speed"))]
    max_vx = _positive(params.get("max_vx"))
    if max_vx is not None:
        sideways = (_positive(params.get("max_vy")) or 0.0) if params.get("holonomic") is True else 0.0
        candidates.append(math.hypot(max_vx, sideways))
    declared = [value for value in candidates if value is not None]
    return {"max_linear_speed": min(declared) if declared else None,
            "max_angular_speed": _positive(params.get("max_wz")), "command_timeout_s": None}


def bounds(ceilings: Mapping) -> dict:
    """The range each field accepts on this body, and where its maximum comes from."""
    out: dict = {}
    for group, fields in GROUPS.items():
        out[group] = {}
        for name, (unit, low, exclusive, platform_max) in fields.items():
            body_max = ceilings.get(name)
            out[group][name] = {"unit": unit, "min": low, "min_exclusive": exclusive,
                                "max": platform_max if body_max is None else min(platform_max, body_max),
                                "platform_max": platform_max, "body_max": body_max}
    return out


def _range_error(name: str, value, low: float, exclusive: bool, top: float, bound: str) -> LimitsError:
    unit = BASE_FIELDS[name][0]
    floor = f"大于 {low:g}" if exclusive else f"不小于 {low:g}"
    source = "这台本体自己声明的上限" if bound == "body" else "平台允许的上限"
    return LimitsError(f"{FIELD_NAMES[name]}填的是 {value!r} {unit}，允许的范围是{floor}、不超过 {top:g} {unit}"
                       f"（{source}）", code="limits_out_of_range", field=name, value=value, min=low, max=top, bound=bound)


def normalize(limits: Any, ceilings: Mapping | None = None) -> dict:
    """Check a limits document and return it as plain floats: ``{"base": {field: value}}``.

    Every field of every group must be there, a finite number inside the platform's range and --
    when ``ceilings`` (see :func:`body_ceilings`) is given -- not above what the body declares.
    Raises :class:`LimitsError`."""
    if not isinstance(limits, Mapping):
        raise LimitsError('安全限值须为对象，形如 {"base": {"max_linear_speed": …, "max_angular_speed": …, '
                          '"command_timeout_s": …}}')
    unknown = sorted(str(k) for k in set(limits) - set(GROUPS))
    if unknown:
        raise LimitsError(f"这一版只能填写底盘（base）的限值；还没有定义的分组：{unknown}", field=unknown[0])
    out: dict = {}
    for group, fields in GROUPS.items():
        values = limits.get(group)
        if not isinstance(values, Mapping):
            raise LimitsError(f"缺少 {group} 分组的限值", field=group)
        extra = sorted(str(k) for k in set(values) - set(fields))
        if extra:
            raise LimitsError(f"{group} 分组里没有这些项目：{extra}", field=extra[0])
        out[group] = {}
        for name, (unit, low, exclusive, platform_max) in fields.items():
            if name not in values:
                raise LimitsError(f"缺少{FIELD_NAMES[name]}（{name}）：三项都要由人填写，平台不替你取值", field=name)
            value = values[name]
            if not _number(value):
                raise LimitsError(f"{FIELD_NAMES[name]}（{name}）须为有限数值，单位 {unit}", field=name)
            body_max = (ceilings or {}).get(name)
            top, bound = platform_max, "platform"
            if body_max is not None and body_max < platform_max:
                top, bound = float(body_max), "body"
            if value <= low if exclusive else value < low:
                raise _range_error(name, value, low, exclusive, top, "platform")
            if value > top:
                raise _range_error(name, value, low, exclusive, top, bound)
            out[group][name] = float(value)
    return out


def effective(limits: Mapping, ceilings: Mapping) -> dict:
    """What is enforced: the lower of each filled value and what the body declares."""
    return {group: {name: float(value) if ceilings.get(name) is None else min(float(value), float(ceilings[name]))
                    for name, value in values.items()}
            for group, values in limits.items()}


def clean_set_by(value: Any) -> str:
    """Who filled the limits in: a non-empty single-line name. Raises ``set_by_required`` / :class:`LimitsError`."""
    if not isinstance(value, str) or not value.strip():
        raise BadRequest("需要填写人（set_by）：安全限值由人填写，设备会记下是谁填的", code="set_by_required")
    text = value.strip()
    # The name ends up in events, logs and the console: no control characters (Cc, incl. C1), no
    # surrogates, no line or paragraph separators, nothing that reorders the text. Joiners used by
    # scripts and emoji stay allowed (and the rule does not depend on the Unicode tables' version).
    odd = any(unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp") or ch in BIDI_CONTROLS for ch in text)
    if len(text) > SET_BY_MAX_CHARS or odd:
        raise LimitsError(f"填写人不能超过 {SET_BY_MAX_CHARS} 个字符，也不能含换行、控制字符或改变文字方向的字符",
                          field="set_by")
    return text


def _entry(raw: Any) -> dict:
    """One ``{revision, limits, set_by, set_at}`` as stored. Raises on anything out of place."""
    if not isinstance(raw, Mapping):
        raise ValueError("记录不是对象")
    revision, set_at = raw.get("revision"), raw.get("set_at")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("修订号无效")
    if not _number(set_at) or set_at < 0:
        raise ValueError("填写时间无效")
    try:
        return {"revision": revision, "limits": normalize(raw.get("limits")),
                "set_by": clean_set_by(raw.get("set_by")), "set_at": float(set_at)}
    except RuntimeApiError as exc:
        raise ValueError(str(exc)) from None


def body_signature(manifest: Mapping) -> dict:
    """What a limits record is bound to: who the body says it is and the chassis limits it declares.
    Limits filled in for one body are not carried over to another, nor past a change of these."""
    velocity = next((cap.get("params") or {} for cap in manifest.get("capabilities", [])
                     if cap.get("id") == "base.velocity"), None)
    declared = None if velocity is None else {name: velocity.get(name) for name in ("max_vx", "max_vy", "max_wz",
                                                                                   "holonomic")}
    return {"instance_id": str(manifest.get("instance_id")), "vendor": str(manifest.get("vendor")),
            "model": str(manifest.get("model")), "family": str(manifest.get("family")),
            "base_velocity": declared, "max_linear_speed": (manifest.get("safety") or {}).get("max_linear_speed")}


def checked_record(raw: Any, body: Mapping) -> dict:
    """A stored record, checked again before it is trusted (it may have been written by an older
    version, by hand, damaged, or copied here from another robot). ``body``: this body's
    :func:`body_signature`. Raises ``ValueError``. History entries that do not check out are
    dropped: they are only shown, never enforced."""
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ValueError(f"记录格式不是 {SCHEMA}")
    theirs = raw.get("body")
    if theirs != body:
        name = theirs.get("instance_id") if isinstance(theirs, Mapping) else None
        raise ValueError(OTHER_DEVICE_REASON.format(theirs=str(name)[:100], ours=body["instance_id"]))
    record = _entry(raw)
    history = []
    for item in raw.get("history") if isinstance(raw.get("history"), list) else []:
        try:
            history.append(_entry(item))
        except ValueError:
            continue
    return {**record, "history": history[:HISTORY_KEPT]}


# --------------------------------------------------------------------------------- manager
class DeviceLimits:
    """Reads, checks, stores and applies this device's limits for an :class:`~.core.EdgeRuntime`.

    The supervisor holds what is enforced (``supervisor.device_limits``); this class holds the
    record around it (who, when, revision, history) and the only way to change it."""

    def __init__(self, runtime):
        self._rt = runtime
        self._lock = threading.Lock()      # one writer at a time; never taken by stop, halt or a motion check
        self._record: dict | None = None
        self._invalid: str | None = None   # why the stored record was not used, if it was not
        self._body = body_signature(runtime.manifest)
        self._load()

    def _load(self) -> None:
        try:
            stored = self._rt.store.get(RECORD_KIND, RECORD_ID)
            if stored is None:
                return
            record = checked_record(stored, self._body)
            self._rt.supervisor.set_device_limits(record["limits"])
        except Exception as exc:           # unreadable or out of range: not filled (fail closed)
            self._invalid = str(exc) or type(exc).__name__
            self._rt.supervisor.set_device_limits(None)
            self._rt.events.emit("safety.limits_invalid", error=self._invalid)
            return
        self._record = record

    def require(self) -> None:
        """Make this runtime need filled limits even on a simulator that brings its own (there is
        no way back while the runtime runs)."""
        self._rt.supervisor.require_device_limits()

    def blocked_reason(self) -> str | None:
        """Why nothing may move, or ``None`` when the limits do not stand in the way."""
        supervisor = self._rt.supervisor
        if not supervisor.device_limits_required or supervisor.device_limits is not None:
            return None
        if self._invalid:
            return f"设备上保存的安全限值无效（{self._invalid}），已按没有填写处理。{UNFILLED_REASON}"
        return UNFILLED_REASON

    def view(self) -> dict:
        """``GET /v2/safety/limits``."""
        supervisor, record = self._rt.supervisor, self._record
        applied = supervisor.device_limits
        limits = {group: dict(values) for group, values in applied.items()} if applied is not None else None
        known = record if (record is not None and limits is not None) else None
        reason = self.blocked_reason()
        return {"schema": SCHEMA, "required": supervisor.device_limits_required, "filled": limits is not None,
                "motion_blocked": reason is not None, "reason": reason, "limits": limits,
                "set_by": known["set_by"] if known else None, "set_at": known["set_at"] if known else None,
                "revision": known["revision"] if known else 0,
                "effective": effective(limits, supervisor.base_ceilings) if limits is not None else None,
                "bounds": bounds(supervisor.base_ceilings), "groups": list(GROUPS),
                "history": copy.deepcopy(known["history"]) if known else [], "invalid": self._invalid}

    def summary(self) -> dict:
        """The part of :meth:`view` that ``/v1/status`` carries."""
        view = self.view()
        return {key: view[key] for key in ("required", "filled", "motion_blocked", "reason", "limits",
                                           "set_by", "set_at")}

    def write(self, payload: dict) -> dict:
        """``POST /v2/safety/limits`` ``{limits: {base: {...}}, set_by, request_id?}`` -> the new view.

        Shape, range and "something is moving" are answered before the request id is recorded, so
        the same id can be sent again once the value is corrected or the body has stopped."""
        rt = self._rt
        rt._check_lease(payload, "safety.limits")
        limits = normalize(payload.get("limits"), rt.supervisor.base_ceilings)
        set_by = clean_set_by(payload.get("set_by"))
        self._refuse_while_moving()
        # One write at a time, and none queues for long behind one that is stuck on a slow disk:
        # "busy" is answered here, before the request id is recorded or the store is touched.
        if not self._lock.acquire(timeout=WRITE_WAIT_S):
            raise RuntimeApiError(BUSY_REASON, code="busy", status=503)
        try:
            if "request_id" in payload:
                return rt.ledger.execute("safety.limits", payload, lambda: self._apply(limits, set_by))
            return self._apply(limits, set_by)
        finally:
            self._lock.release()

    def _refuse_while_moving(self) -> None:
        """Limits change while the body is at rest: no run that may command motion (a run that
        cannot, such as a model install or a read-only skill, does not count), no velocity setpoint
        in effect. A command that is past its check at this very moment still goes out under the
        limits it was checked against, for at most the timeout it was given."""
        rt = self._rt
        if rt.supervisor.velocity_active or rt.runner.motion_owner() is not None:
            raise Conflict(MOVING_REASON, code="runs_active")

    def _apply(self, limits: dict, set_by: str) -> dict:
        """Store and put in force. The caller holds ``_lock``."""
        rt = self._rt
        self._refuse_while_moving()
        previous = self._record
        entry = {"revision": (previous["revision"] if previous else 0) + 1, "limits": limits,
                 "set_by": set_by, "set_at": time.time()}
        history = [copy.deepcopy(entry)] + (previous["history"] if previous else [])
        record = {**entry, "history": history[:HISTORY_KEPT]}
        try:
            # Disk first: limits that are enforced are always the ones a restart will find.
            rt.store.put(RECORD_KIND, {"id": RECORD_ID, "schema": SCHEMA, "body": self._body, **record})
        except Exception:
            raise RuntimeApiError(NOT_SAVED_REASON, code="limits_not_saved", status=500) from None
        effects.mark()
        rt.supervisor.set_device_limits(limits)
        self._record, self._invalid = record, None
        rt.events.emit("safety.limits_set", revision=entry["revision"], limits=limits, set_by=set_by,
                       previous=previous["limits"] if previous else None)
        return {"accepted": True, **self.view()}
