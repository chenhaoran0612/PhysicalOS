"""The robot connects out to the cloud and fetches the calls third parties made (issue #43).

Nothing connects *to* the robot. A thread of its own (``nx-cloud-link``) sends
``POST /api/v1/device-link/sync`` to the configured cloud every couple of seconds: who this device
is, its safety state in three booleans, the installed skills with the owner's remote switch of each,
and the state of the remote calls the cloud has not settled yet. The answer carries new calls,
cancellations and at most one stop. Calls are carried out by ``remote_calls.py`` under the switches
of ``remote_access.py``; this module is only the wire.

The answer is untrusted input that can move the robot, so it is bounded before anything is done
with it: at most 64 KB, a JSON object, ``accepted: true``, ids of the expected form, lists of
bounded length. An answer that does not fit is refused as a whole and counts as a failed sync --
with one exception: a well-formed ``stop`` in an answer that is otherwise wrong is still carried
out, first, because stopping is the safe direction.

It cannot hold up anything else:

* It only *reads* the safety state and calls the entry points the HTTP gateway calls (run a skill,
  cancel a run, stop). The stop path never calls it, takes none of its locks and waits for neither
  its disk nor its network.
* Every failure -- no network, a cloud that hangs, an answer that is garbage or too large, a key
  without the ``device:link`` scope, a disk that is full -- ends as "not connected" in
  ``/v1/status.cloud_link`` and a later retry. :meth:`CloudLink.tick` never raises.
* The alert reporter (``alert_report.py``) has its own thread and its own connections; the two
  share the configuration file and the HTTPS client code, nothing else.

Configuration: the file of ``serve --cloud-config`` (``url``, ``key``, ``ca_file``; HTTPS only,
certificate and host name verified, no redirects, no proxy from the environment). The link is on
when that file is usable and does not say ``"link": false``. The key needs the scope
``device:link``; without it the cloud answers 403 ``scope_missing`` and the link shows ``error``.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, alerts
from .alert_report import ConfigError, HttpsTransport, ReportConfig, SendError, load_config

SCHEMA = "neuxmind.device_link.v1"
SYNC_PATH = "/api/v1/device-link/sync"
POLL_DEFAULT_S = 2.0                # between syncs when the cloud does not say
POLL_MIN_S, POLL_MAX_S = 1.0, 30.0  # what the cloud may ask for
ACTIVE_POLL_S = 1.0                 # while a remote call is running
ACTIVE_RETRY_MAX_S = 2.0            # ... but never longer than this while a remote call runs (stop, cancel)
MAX_CALL_AGE_S = 150.0              # a call older than this by the cloud's own clock is not started
BACKOFF_MAX_S = 30.0                # after failures: 2, 4, 8, 16, 30 s
REFUSED_RETRY_S = 30.0              # the cloud does not take this key (401 / 403): no faster than this
TIMEOUT_S = 5.0                     # connect and every socket read / write (one sync: three times this)
ONLINE_WITHIN_S = 60.0              # no successful sync for this long is not "online"
LOOP_S = 0.2                        # how often the thread looks whether a sync is due
CLOSE_JOIN_S = 1.0
MAX_CALLS_PER_ANSWER = 10
MAX_CANCEL_PER_ANSWER = 50
MAX_SETTLED_PER_ANSWER = 200
MAX_PARAMETER_BYTES = 16_384
MAX_REQUEST_BYTES = 900_000         # the cloud's default body limit is 1 MB
MAX_REASON = 200
MAX_REJECTED_SHOWN = 20
NOT_CONFIGURED = "未配置云平台地址（serve --cloud-config）：不和云平台连接，第三方调用不了这台机器人"
SWITCHED_OFF = '配置文件里写了 "link": false：不和云平台连接'
SCOPE_MISSING = ("云平台的 key 没有 device:link 权限范围：没有和云平台连上，第三方调用不了这台机器人"
                 "（告警上报不受影响）。请给这台设备的 API Key 加上 device:link")

_CALL_ID = re.compile(r"cal_[A-Za-z0-9_-]{1,80}")
_STOP_ID = re.compile(r"stp_[A-Za-z0-9_-]{1,80}")
_DEVICE_ID = re.compile(r"dlk_[A-Za-z0-9_-]{1,80}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")
MAX_SKILL_ID = 200


class LinkTransport(HttpsTransport):
    """The alert reporter's HTTPS client, pointed at the sync endpoint."""

    def __init__(self, config: ReportConfig):
        super().__init__(config)
        self._path = urlsplit(config.url).path + SYNC_PATH
        self._headers = {**self._headers, "User-Agent": f"neuxmind-runtime/{__version__} cloud-link"}


def _clean(value, limit: int) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in value)[:limit].strip()


def _time(*values) -> float | None:
    """The first of ``values`` that is a time on the cloud's clock (a finite number), or ``None``."""
    for value in values:
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def aged(call: dict, server_time: float | None) -> dict:
    """``call``, refused as ``expired`` when the cloud itself says it was placed more than
    :data:`MAX_CALL_AGE_S` before this answer (both times are the cloud's: the robot's clock plays
    no part). A call without such a time is not judged."""
    placed = call.get("placed_at")
    if server_time is None or placed is None or call.get("problem") or server_time - placed <= MAX_CALL_AGE_S:
        return call
    return {**call, "problem_code": "expired",
            "problem": f"这次调用发出已经 {server_time - placed:.0f} 秒，超过 {MAX_CALL_AGE_S:g} 秒，不再执行"}


def read_stop(answer) -> tuple[str, str] | None:
    """``(stop_id, reason)`` when the answer carries a stop that is one, whatever else it carries."""
    stop = answer.get("stop") if isinstance(answer, dict) else None
    if not isinstance(stop, dict):
        return None
    stop_id, reason = stop.get("stop_id"), stop.get("reason")
    if not isinstance(stop_id, str) or not _STOP_ID.fullmatch(stop_id):
        return None
    return stop_id, _clean(reason, MAX_REASON) if isinstance(reason, str) else ""


def _id_list(value, pattern: re.Pattern, limit: int, what: str) -> list[str]:
    """A list of ids from the answer: entries that are not ids are left out, a list that is not a
    list or is longer than ``limit`` makes the whole answer unusable."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise SendError(f"云平台的答复格式不对（{what}）")
    return [item for item in value if isinstance(item, str) and pattern.fullmatch(item)]


def read_call(raw) -> dict | None:
    """One call of the answer. ``None`` when it has no usable id (it cannot even be refused); with
    ``problem`` set when the id is fine and something else is not (it is rejected, never run)."""
    if not isinstance(raw, dict):
        return None
    call_id = raw.get("call_id")
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
        return None
    skill_id, version = raw.get("skill_id"), raw.get("version")
    parameters, timeout = raw.get("parameters"), raw.get("timeout_s")
    call = {"call_id": call_id, "skill_id": None, "version": None, "digest": None, "parameters": {},
            "timeout_s": None, "problem": None, "placed_at": _time(raw.get("created_at"), raw.get("dispatched_at"))}
    if not isinstance(skill_id, str) or not 0 < len(skill_id) <= MAX_SKILL_ID or not skill_id.isprintable():
        return {**call, "problem": "调用里的技能 ID 无效"}
    call["skill_id"] = skill_id
    if version is not None and (not isinstance(version, str) or not _VERSION.fullmatch(version)):
        return {**call, "problem": "调用里的版本号无效"}
    call["version"] = version
    digest = raw.get("digest")          # the content the caller was shown; checked against what is installed
    if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        return {**call, "problem": "调用里的 digest 无效"}
    call["digest"] = digest
    if parameters is not None and not isinstance(parameters, dict):
        return {**call, "problem": "调用里的 parameters 须为对象"}
    try:
        size = len(json.dumps(parameters or {}, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError):
        return {**call, "problem": "调用里的参数不是合法 JSON"}
    if size > MAX_PARAMETER_BYTES:
        return {**call, "problem": f"调用里的参数过大（超过 {MAX_PARAMETER_BYTES} 字节）"}
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        return {**call, "problem": "调用里的 timeout_s 须为正的有限数值"}
    return {**call, "parameters": parameters or {}, "timeout_s": timeout}


def read_rejected_skills(answer: dict) -> list[dict]:
    """``rejected_skills`` of the answer (skills the cloud did not take, each with the reason), bounded
    and cleaned for the status. Anything that is not such a list is no list."""
    raw = answer.get("rejected_skills")
    out = []
    for item in raw[:MAX_REJECTED_SHOWN] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        skill_id, error = item.get("id"), item.get("error")
        out.append({"id": _clean(skill_id, MAX_SKILL_ID) if isinstance(skill_id, str) else None,
                    "error": _clean(error, 300) if isinstance(error, str) else ""})
    return out


def _count(value, at_least: int) -> int:
    """How many skills the cloud says it refused (its list is capped); never less than the list shows."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        return at_least
    return max(value, at_least)


def read_answer(answer) -> dict:
    """The parts of the cloud's answer that are acted on; ``SendError`` when it is not an answer."""
    if not isinstance(answer, dict):
        raise SendError("云平台的答复格式不对")
    if answer.get("accepted") is not True:
        raise SendError("云平台没有接受这次同步")
    calls = answer.get("calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > MAX_CALLS_PER_ANSWER:
        raise SendError("云平台的答复格式不对（calls）")
    stop = answer.get("stop")
    if stop is not None and read_stop(answer) is None:
        raise SendError("云平台的答复格式不对（stop）")
    poll, device_id = answer.get("poll_after_s"), answer.get("device_id")
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll):
        poll = POLL_DEFAULT_S
    return {"calls": [call for call in map(read_call, calls) if call is not None],
            "cancel": _id_list(answer.get("cancel"), _CALL_ID, MAX_CANCEL_PER_ANSWER, "cancel"),
            "settled": _id_list(answer.get("settled"), _CALL_ID, MAX_SETTLED_PER_ANSWER, "settled"),
            "stop_id": stop["stop_id"] if stop is not None else None,
            "rejected_skills": read_rejected_skills(answer),
            "rejected_skills_total": _count(answer.get("rejected_skills_total"), len(read_rejected_skills(answer))),
            "server_time": _time(answer.get("server_time")),
            "poll_after_s": min(POLL_MAX_S, max(POLL_MIN_S, float(poll))),
            "device_id": device_id if isinstance(device_id, str) and _DEVICE_ID.fullmatch(device_id) else None}


class CloudLink:
    """One round is :meth:`tick`; :meth:`start` runs it on a thread.

    ``clock`` (monotonic seconds) paces the syncs and ``wall`` stamps them; tests pass their own.
    ``transport`` replaces the HTTPS client (tests). With ``config=None`` nothing is ever sent."""

    def __init__(self, runtime, config: ReportConfig | None = None, *, clock=time.monotonic, wall=time.time,
                 transport=None, disabled_reason: str | None = None, loop_s: float = LOOP_S):
        self.config = config
        self._rt, self._clock, self._wall, self._loop_s = runtime, clock, wall, loop_s
        self._access, self._calls = runtime.remote_access, runtime.remote_access.calls
        self._transport, self._reason = None, None
        if config is None:
            self._reason = disabled_reason or NOT_CONFIGURED
        elif config.link is not True:
            self._reason = SWITCHED_OFF
        elif alerts.device_problem(runtime.manifest) is not None:
            self._reason = alerts.device_problem(runtime.manifest)
        elif transport is not None:
            self._transport = transport
        else:
            try:
                self._transport = LinkTransport(dataclasses.replace(config, timeout_s=TIMEOUT_S))
            except Exception as exc:                       # e.g. a CA file that is not a certificate
                text = str(exc).replace(config.ca_file or "\0", "ca_file")[:200]
                self._reason = f"没有和云平台连接：{type(exc).__name__}: {text}"
        self._device = {**alerts.device_view(runtime.manifest), "runtime_version": __version__}
        self._host = urlsplit(config.url).netloc if config is not None else None
        self._next_at: float | None = None
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self.poll_after_s = POLL_DEFAULT_S
        self.device_id: str | None = None
        self.syncs = 0
        self.consecutive_failures = 0
        self.last_attempt_at: float | None = None
        self.last_sync_at: float | None = None
        self.last_error: str | None = None
        self.storage_error: str | None = None
        self.skills_rejected: list = []                    # skills the cloud did not take, with its reason
        self.skills_rejected_total = 0                     # ... and how many in all (the list is capped)
        self.skills_trimmed: list = []                     # skills whose text was shortened to fit one entry
        self.active_call: str | None = None
        self.faults = 0

    # ------------------------------------------------------------ one round
    def tick(self) -> None:
        """Sync if a sync is due. Nothing is raised: whatever goes wrong is a failed sync."""
        if self._closing.is_set() or self._transport is None:
            return
        self._access.flush()                               # an "off" the disk did not take: tried again
        try:
            now = self._clock()
            if self._next_at is not None and now < self._next_at:
                return
            self._sync(now)
        except Exception as exc:                           # a mistake in here must not end the thread
            self.faults += 1
            self._failed(exc)

    def _sync(self, now: float) -> None:
        try:
            request = self._request()
            self.last_attempt_at = self._wall()
            answer = self._transport.send(request)
        except Exception as exc:
            self._failed(exc)
            return
        stop = read_stop(answer)
        if stop is not None:                               # first, and whatever else the answer says
            self._calls.stop(*stop)
        if self._closing.is_set():                         # closed while the request was under way:
            return                                         # a stop still counts, nothing else is applied
        try:
            parts = read_answer(answer)
        except Exception as exc:
            self._failed(exc)
            return
        self._apply(request, parts)
        self.syncs += 1
        self.consecutive_failures = 0
        self.last_sync_at = self._wall()
        self.last_error = None
        self.storage_error = self._calls.storage_error     # the cloud answers, but calls cannot be taken
        active = self._calls.active()
        self.active_call = active["call_id"] if active is not None else None
        self._next_at = self._clock() + (ACTIVE_POLL_S if active is not None else self.poll_after_s)

    def _request(self) -> dict:
        rt = self._rt
        request = {"schema": SCHEMA, "device": self._device,
                   "state": {"estop_latched": bool(rt.supervisor.latched), "busy": bool(rt.runner.active_runs()),
                             "motion_blocked": rt.device_limits.blocked_reason() is not None},
                   "skills": self._access.skills_report(), "calls": self._calls.reports(),
                   "stops": self._calls.stops_report()}
        while request["skills"] and len(json.dumps(request, ensure_ascii=False).encode()) > MAX_REQUEST_BYTES:
            request["skills"] = request["skills"][:len(request["skills"]) // 2]     # never reached in practice
        return request

    def _apply(self, request: dict, parts: dict) -> None:
        calls = self._calls
        calls.stops_acknowledged(request["stops"], parts["stop_id"])
        calls.settle(set(parts["settled"]))
        for call_id in parts["cancel"]:                    # before the calls: a cancelled call never starts
            calls.cancel(call_id)
        for call in parts["calls"]:
            calls.deliver(aged(call, parts["server_time"]), self._open)
        self.poll_after_s = parts["poll_after_s"]
        self.device_id = parts["device_id"] or self.device_id
        self.skills_rejected = parts["rejected_skills"]
        self.skills_rejected_total = parts["rejected_skills_total"]
        self.skills_trimmed = list(self._access.trimmed)

    def _open(self) -> bool:
        return not self._closing.is_set()

    def _failed(self, exc: Exception) -> None:
        self.consecutive_failures += 1
        status = getattr(exc, "status", None)
        text = str(exc) if isinstance(exc, SendError) else f"{type(exc).__name__}: {exc}"
        if status == 403 and "scope_missing" in text:
            text = SCOPE_MISSING
        if self.config is not None:
            text = text.replace(self.config.key, "***")
        self.last_error = text[:300]
        delay = min(BACKOFF_MAX_S, 2.0 ** min(self.consecutive_failures, 5))
        if status in (401, 403):
            delay = REFUSED_RETRY_S
        if self._calls.active() is not None:               # a stop or a cancel must not wait for a long back-off
            delay = min(delay, ACTIVE_RETRY_MAX_S)
        self._next_at = self._clock() + delay

    # ------------------------------------------------------------ thread
    def start(self) -> "CloudLink":
        if self._thread is None and self._transport is not None:
            self._thread = threading.Thread(target=self._loop, name="nx-cloud-link", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._closing.wait(self._loop_s):
            self.tick()

    def close(self) -> None:
        """Stop syncing. Never raises and never waits long: a request in flight gets ``CLOSE_JOIN_S``.
        The flag is set under the lock the book takes calls under, so a call is either taken before
        this returns or not at all; if the lock is not free in time the flag is set anyway, and a
        run the book starts after that is cancelled by the book itself."""
        try:
            held = self._calls.lock.acquire(timeout=CLOSE_JOIN_S)
        except Exception:
            held = False
        self._closing.set()
        if held:
            self._calls.lock.release()
        thread, self._thread = self._thread, None
        try:
            if thread is not None and thread is not threading.current_thread():
                thread.join(CLOSE_JOIN_S)
        except Exception:
            pass

    # ------------------------------------------------------------ status
    def describe(self) -> dict:
        """``/v1/status.cloud_link`` and ``GET /v2/remote`` ``link``: cached values only -- no lock,
        no disk, no network, no key. ``state``: ``off`` | ``connecting`` | ``online`` | ``error``."""
        enabled = self._transport is not None
        error, last = self.last_error or self.storage_error, self.last_sync_at
        if not enabled:
            state = "off"
        elif self.consecutive_failures > 0 or self.storage_error:
            state = "error"
        elif last is None:
            state = "connecting"
        elif self._wall() - last > ONLINE_WITHIN_S:        # the thread is stuck (disk, a mistake): not online
            state, error = "error", error or f"已有 {self._wall() - last:.0f} 秒没有和云平台同步"
        else:
            state = "online"
        return {"configured": self.config is not None, "enabled": enabled, "state": state,
                "reason": None if enabled else self._reason, "host": self._host,
                "last_sync_at": last, "last_attempt_at": self.last_attempt_at, "last_error": error,
                "device_id": self.device_id, "poll_after_s": self.poll_after_s, "syncs": self.syncs,
                "consecutive_failures": self.consecutive_failures, "active_call": self.active_call,
                "skills_rejected": list(self.skills_rejected),
                "skills_rejected_total": self.skills_rejected_total, "skills_trimmed": list(self.skills_trimmed)}


def for_serve(runtime, config_path: str | Path | None) -> CloudLink:
    """The link of a served runtime (not started yet). A missing or unusable configuration file
    never stops the runtime: the link is off, ``describe()["reason"]`` says why."""
    config, reason = None, None
    if config_path is not None:
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            reason = f"云平台配置不能用，没有和云平台连接：{exc}"
    link = CloudLink(runtime, config, disabled_reason=reason)
    runtime.cloud_link = link
    runtime.remote_access.link = link
    return link
