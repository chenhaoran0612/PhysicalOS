"""Remote calls on the device: at most once each, one at a time, never without the owner's switch
(issue #43).

``cloud_link.py`` fetches calls, cancellations and stops from the cloud and hands them to the
:class:`RemoteCalls` book of the runtime. What the cloud sent is untrusted: by the time it gets here
its shape has been checked, and everything that decides whether the robot moves is decided here, on
the device, again:

* **At most once.** A call id is written to the state directory *before* its run is started. A call
  seen before is only reported, never run again -- not after a lost answer, not after a restart.
  The run itself is started through ``EdgeRuntime.run_skill`` with a request id made from the call
  id, so the request ledger refuses a second start as well. A call that was under way when the
  runtime went down is reported ``failed`` / ``runtime_restarted``.
* **One at a time.** While a remote call is not finished, the next one is rejected ``device_busy``.
* **The owner's switch.** The call runs with ``hardware_authorized`` only because the switch of
  that skill is on and bound to the installed content (``remote_access.py``). The parameters are
  checked here against the skill's declaration, and the run passes the safety supervisor like any
  other run.
* **Stop.** A stop from the cloud is the same stop as ``POST /v1/stop`` (latched; released on site).
  It is carried out before anything is written, once per stop id.

Locks: ``_lock`` orders the link thread and the HTTP thread that turns a switch off. The stop path
takes none of this; :meth:`stop` calls ``emergency_stop`` before it takes the lock.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time

from .errors import (BadRequest, Conflict, NotAuthorized, NotFound, RuntimeApiError, SafetyRejected,
                     UnknownOutcome)
from .remote_access import STORE_KIND
from .skills import check_parameters

CALLS_ID = "calls"
STOPS_ID = "stops"
SEEN_KEPT = 2000                 # call ids remembered (newest first out last)
RECENT_KEPT = 50                 # calls kept with their outcome
STOPS_KEPT = 200
MAX_UNACKED_STOPS = 20
MAX_RESULT_BYTES = 16_384        # a result above this is reported as "too large", not sent
MAX_TEXT = 500
MAX_DETAIL = 200
REPORT_TERMINAL_S = 600.0        # an outcome the cloud never settles is reported this long, then dropped
STARTING, RUNNING = "starting", "running"
SUCCEEDED, FAILED, CANCELLED, STOPPED, REJECTED = "succeeded", "failed", "cancelled", "stopped", "rejected"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED, STOPPED, REJECTED)
REQUEST_PREFIX = "remote-"       # the ledger's request id of a remote call is this + the call id

NOT_SAVED = "调用编号没能写进设备的存储（磁盘满或不可写），为了不重复执行，这次调用没有开始"
RESTARTED = "管理层在这次调用进行中重启了；结果未知，不会重新执行，请现场核对"
FORGOTTEN = "这个调用以前收到过，设备上已经没有它的记录；不会重新执行"
SWITCHED_OFF = "主人关闭了这个技能的远程调用，正在进行的调用已取消"


def _text(value, limit: int = MAX_TEXT) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:limit]


def _error(code: str, message) -> dict:
    return {"code": code, "message": _text(message)}


def parameters_digest(parameters: dict) -> str:
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bounded_result(result):
    """The run's result as it is reported: JSON of at most :data:`MAX_RESULT_BYTES`."""
    if result is None:
        return None
    try:
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError):
        return {"omitted": True, "reason": "结果不能序列化"}
    if size > MAX_RESULT_BYTES:
        return {"omitted": True, "reason": f"结果有 {size} 字节，超过远程调用带回的上限；请在设备上查看这次运行"}
    return result


class RemoteCalls:
    def __init__(self, runtime, access, wall=time.time):
        self._rt, self._access, self._wall = runtime, access, wall
        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}
        self._calls: dict[str, dict] = {}       # call id -> record, oldest first
        self._stops: dict[str, float] = {}      # stop ids carried out (written by the link thread only)
        self._unacked: list[str] = []
        self._recent: tuple = ()                # what GET /v2/remote shows; replaced as a whole
        self.storage_error: str | None = None
        self._loaded = False
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self) -> bool:
        """Read the book. Until that has worked once, nothing is known about earlier calls: no call
        is started and nothing is written (a write would wipe what could not be read)."""
        if self._loaded:
            return True
        try:
            stored = self._rt.store.get(STORE_KIND, CALLS_ID) or {}
            seen, recent = stored.get("seen"), stored.get("recent")
            self._seen = {**{k: float(v) for k, v in (seen if isinstance(seen, dict) else {}).items()
                             if isinstance(k, str) and isinstance(v, (int, float))}, **self._seen}
            for record in recent if isinstance(recent, list) else []:
                if not isinstance(record, dict) or not isinstance(record.get("call_id"), str):
                    continue
                self._seen.setdefault(record["call_id"], self._wall())      # seen, whatever else it says
                if record.get("status") in (STARTING, RUNNING, *TERMINAL):
                    self._calls[record["call_id"]] = {**self._new_record(record), **record}
            stops = (self._rt.store.get(STORE_KIND, STOPS_ID) or {}).get("stops")
            self._stops = {**{k: float(v) for k, v in (stops if isinstance(stops, dict) else {}).items()
                              if isinstance(k, str) and isinstance(v, (int, float))}, **self._stops}
        except Exception as exc:
            self.storage_error = f"读不到远程调用的记录：{type(exc).__name__}"
            return False
        self._loaded, self.storage_error = True, None
        self._recover()
        self._publish()
        return True

    def _recover(self) -> None:
        """After a restart no run of an earlier process is alive: a call that was under way gets the
        outcome its run recorded before the end, or ``failed`` / ``runtime_restarted``."""
        changed = False
        for record in self._calls.values():
            if record.get("status") not in (STARTING, RUNNING):
                continue
            run = None
            try:
                if record.get("run_id"):
                    run = self._rt.store.get("runs", record["run_id"])
            except Exception:
                run = None
            if run is not None and run.get("status") in (SUCCEEDED, FAILED, STOPPED):
                self._finish_from_run(record, run)
            else:
                self._finish(record, FAILED, error=_error("runtime_restarted", RESTARTED))
            changed = True
        if changed:
            self._save()

    def _save(self) -> bool:
        """Write the book. ``False`` (and :attr:`storage_error`) when the disk did not take it."""
        if not self._loaded:
            return False
        seen = dict(sorted(self._seen.items(), key=lambda item: item[1])[-SEEN_KEPT:])
        items = list(self._calls.items())        # the newest, and whatever is not finished yet
        calls = dict([item for item in items[:-RECENT_KEPT] if item[1].get("status") not in TERMINAL]
                     + items[-RECENT_KEPT:])
        try:
            self._rt.store.put(STORE_KIND, {"id": CALLS_ID, "seen": seen, "recent": list(calls.values())})
        except Exception as exc:
            self.storage_error = f"远程调用的记录写不进存储：{type(exc).__name__}"
            return False
        self._seen, self._calls, self.storage_error = seen, calls, None
        return True

    def _publish(self) -> None:
        keys = ("call_id", "skill_id", "version", "status", "run_id", "started_at", "finished_at", "error")
        shown = [{**{k: r.get(k) for k in keys}, "status": RUNNING if r.get("status") == STARTING else r.get("status")}
                 for r in self._calls.values()]
        self._recent = tuple(reversed(shown))

    def recent(self, limit: int) -> list[dict]:
        """The newest remote calls, newest first. Memory only, no lock."""
        return [dict(item) for item in self._recent[:limit]]

    # ------------------------------------------------------------ what one sync reports
    def active(self) -> dict | None:
        """The remote call that is not finished, if there is one."""
        return next((r for r in self._calls.values() if r.get("status") in (STARTING, RUNNING)), None)

    def reports(self) -> list[dict]:
        """Every call the cloud has not settled, refreshed from its run."""
        with self._lock:
            self.refresh()
            out = []
            for record in self._calls.values():
                if record.get("settled"):
                    continue
                status = RUNNING if record["status"] == STARTING else record["status"]
                out.append({"call_id": record["call_id"], "status": status, "run_id": record.get("run_id"),
                            "progress": record.get("progress"), "detail": record.get("detail"),
                            "result": record.get("result"), "error": record.get("error")})
            return out[-RECENT_KEPT:]

    def settle(self, call_ids) -> None:
        """The cloud has the final state of these calls: they are no longer reported."""
        with self._lock:
            now, changed = self._wall(), False
            for record in self._calls.values():
                if record.get("settled") or record["status"] not in TERMINAL:
                    continue
                overdue = now - (record.get("finished_at") or now) > REPORT_TERMINAL_S
                if record["call_id"] in call_ids or overdue:
                    record["settled"], changed = True, True
            if changed:
                self._save()

    # ------------------------------------------------------------ stop (first, always)
    def stop(self, stop_id: str, reason: str) -> bool:
        """Carry out a stop from the cloud, once per ``stop_id``. The robot is stopped before any
        lock is taken or anything is written."""
        if stop_id in self._stops:
            self._expect_ack(stop_id)
            return False
        why = _text(f"云端停止（{stop_id}）" + (f"：{reason}" if reason else ""), MAX_TEXT)
        result = self._rt.emergency_stop(why, source="cloud_link")
        self._stops[stop_id] = self._wall()
        self._expect_ack(stop_id)
        try:
            self._rt.record_operation("POST", "/v1/stop", {"reason": why}, status=200, body=result,
                                      operator="cloud-link")
            kept = dict(sorted(self._stops.items(), key=lambda item: item[1])[-STOPS_KEPT:])
            if self._loaded:
                self._rt.store.put(STORE_KIND, {"id": STOPS_ID, "stops": kept})
            self._stops = kept
        except Exception as exc:                # stopped and latched; only "once" is at risk after a restart
            self.storage_error = f"停止编号写不进存储：{type(exc).__name__}"
        return True

    def _expect_ack(self, stop_id: str) -> None:
        if stop_id not in self._unacked:
            self._unacked = [*self._unacked, stop_id][-MAX_UNACKED_STOPS:]

    def stops_report(self) -> list[str]:
        return list(self._unacked)

    def stops_acknowledged(self, reported, still_asked: str | None) -> None:
        """The cloud answered a sync that carried ``reported``: all but the one it still asks for are done."""
        self._unacked = [s for s in self._unacked if s not in reported or s == still_asked]

    # ------------------------------------------------------------ cancel
    def cancel(self, call_id: str) -> None:
        """The cloud cancels a call. One not seen yet is remembered as cancelled: it will never run."""
        with self._lock:
            record = self._calls.get(call_id)
            if record is None and call_id not in self._seen:
                record = self._new_record({"call_id": call_id, "skill_id": None, "version": None})
                self._finish(record, CANCELLED, error=_error("cancelled", "在开始之前被云端取消"))
                self._remember(record)
                self._save()
            elif record is not None and record["status"] in (STARTING, RUNNING):
                self._cancel_run(record, "cancelled", "云端取消了这次调用")
            self._publish()

    def switched_off(self, skill_id: str) -> None:
        """The owner turned the switch of ``skill_id`` off: its running remote call is cancelled."""
        with self._lock:
            record = self.active()
            if record is not None and record.get("skill_id") == skill_id:
                self._cancel_run(record, "remote_not_allowed", SWITCHED_OFF)

    def _cancel_run(self, record: dict, code: str, message: str) -> None:
        record["cancel"] = _error(code, message)
        run_id = record.get("run_id")
        if run_id is None:
            return                               # still starting: deliver() cancels it once it has a run
        try:
            self._rt.runner.cancel(run_id)
        except RuntimeApiError:
            pass                                 # the run is gone; refresh() reports what became of it

    # ------------------------------------------------------------ a call from the cloud
    @property
    def lock(self):
        """The lock :meth:`deliver` decides under; a link sets its closing flag while holding it."""
        return self._lock

    def deliver(self, call: dict, still_open=None) -> None:
        """One call as the cloud sent it (shape checked by the link; ``problem`` set if it was off).
        ``still_open()`` is the link's "not closing": once it says no, nothing is taken, and a run
        that got started while it changed its mind is cancelled at once."""
        with self._lock:
            if still_open is not None and not still_open():
                return                           # not taken, not answered: the cloud keeps the call
            call_id = call["call_id"]
            record = self._calls.get(call_id)
            if record is None and not self._load():
                # Whether this call ran before cannot be known: it is not started, and it is not
                # answered either -- the cloud sends it again, or gives it up as not picked up.
                return
            if record is not None or call_id in self._seen:
                self._seen_again(call_id, record)
            else:
                self._admit(call, still_open)
            self._publish()

    def _seen_again(self, call_id: str, record: dict | None) -> None:
        """Delivered twice (the cloud did not get our report): report it, never run it."""
        if record is None:
            record = self._new_record({"call_id": call_id, "skill_id": None, "version": None})
            self._finish(record, FAILED, error=_error("outcome_unknown", FORGOTTEN), note=False)
            self._calls[call_id] = record
        record["settled"] = False

    def _new_record(self, call: dict) -> dict:
        return {"call_id": call["call_id"], "skill_id": call.get("skill_id"), "version": call.get("version"),
                "status": STARTING, "run_id": None, "progress": 0.0, "detail": None, "result": None, "error": None,
                "received_at": self._wall(), "started_at": None, "finished_at": None, "settled": False,
                "set_by": None, "digest": None, "parameters_digest": None, "parameters_summary": None}

    def _remember(self, record: dict) -> None:
        self._seen[record["call_id"]] = record["received_at"]
        self._calls[record["call_id"]] = record

    def _refusal(self, call: dict) -> tuple[dict | None, dict | None, dict]:
        """``(error, skill record, parameters)``: why this call is rejected, or ``None`` and what to run."""
        rt = self._rt
        if call.get("problem"):
            return _error(call.get("problem_code") or "invalid_parameters", call["problem"]), None, {}
        if rt.supervisor.latched:
            return _error("estop_latched", "急停已锁存；要在现场解除之后才能远程调用"), None, {}
        skill = self._access.installed().get(call["skill_id"])
        if skill is None:
            return _error("skill_not_installed", f"这台机器人没有安装技能 {call['skill_id']}"), None, {}
        if call.get("version") is not None and call["version"] != skill["version"]:
            return _error("version_mismatch", f"机器人上装的是 {skill['version']}，调用要的是 {call['version']}"), None, {}
        if call.get("digest") is not None and call["digest"] != skill["digest"]:
            return _error("version_mismatch", "机器人上这个技能的内容和调用方看到的不一样（重装过）"), None, {}
        if self._access.allowed_by(call["skill_id"], skill["digest"]) is None:
            return _error("remote_not_allowed", "主人没有打开这个技能的远程调用"), skill, {}
        busy = self.active()
        if busy is not None:
            return _error("device_busy", f"远程调用 {busy['call_id']} 还没有结束；同一时间只执行一个"), skill, {}
        try:
            parameters = check_parameters(skill["package"]["parameters"], call.get("parameters"))
            json.dumps(parameters, allow_nan=False)
        except (RuntimeApiError, TypeError, ValueError) as exc:
            return _error("invalid_parameters", exc), skill, {}
        timeout = call.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout <= 0):
            return _error("invalid_parameters", "timeout_s 须为正的有限数值"), skill, {}
        return None, skill, call.get("parameters") or {}

    def _admit(self, call: dict, still_open=None) -> None:
        record = self._new_record(call)
        error, skill, parameters = self._refusal(call)
        if skill is not None:
            record.update(version=skill["version"], digest=skill["digest"],
                          set_by=self._access.allowed_by(call["skill_id"], skill["digest"]))
        if error is not None:
            self._finish(record, REJECTED, error=error)
            self._remember(record)
            self._save()                         # a rejection that is not saved is still reported from memory
            return
        record.update(parameters_digest=parameters_digest(parameters),
                      parameters_summary=_text(json.dumps(parameters, ensure_ascii=False, sort_keys=True), 300))
        self._remember(record)
        if not self._save():                     # the id must be on disk before anything can move
            self._finish(record, FAILED, error=_error("state_not_saved", NOT_SAVED))
            return
        self._start(record, call, parameters)
        if record["status"] == RUNNING and still_open is not None and not still_open():
            self._cancel_run(record, "cancelled", "管理层正在关闭，这次调用已取消")
        self._save()

    def _start(self, record: dict, call: dict, parameters: dict) -> None:
        payload = {"request_id": REQUEST_PREFIX + record["call_id"], "version": record["version"],
                   "parameters": parameters, "hardware_authorized": True}
        if call.get("timeout_s") is not None:
            payload["timeout_s"] = call["timeout_s"]
        try:
            started = self._rt.run_skill(call["skill_id"], payload)
        except Exception as exc:
            status, code = self._start_failure(exc)
            self._finish(record, status, error=_error(code, exc))
            return
        record.update(run_id=started["run_id"], status=RUNNING, started_at=self._wall())
        self._rt.events.emit("remote.call_started", call_id=record["call_id"], skill_id=record["skill_id"],
                             run_id=record["run_id"], set_by=record["set_by"])
        if self._access.allowed_by(record["skill_id"], record["digest"]) is None:
            self._cancel_run(record, "remote_not_allowed", SWITCHED_OFF)   # turned off while it was starting

    def _start_failure(self, exc: Exception) -> tuple[str, str]:
        """``(status, code)`` of a call whose run was refused at the door (nothing moved)."""
        if isinstance(exc, UnknownOutcome):
            return FAILED, "outcome_unknown"
        if isinstance(exc, NotAuthorized):
            return REJECTED, "not_authorized"
        if isinstance(exc, SafetyRejected):
            return (REJECTED, "estop_latched") if self._rt.supervisor.latched else (FAILED, "start_failed")
        if isinstance(exc, Conflict):
            return REJECTED, "device_busy"
        if isinstance(exc, NotFound):
            return REJECTED, "skill_not_installed"
        if isinstance(exc, BadRequest):
            return REJECTED, "invalid_parameters"
        return FAILED, "start_failed"

    # ------------------------------------------------------------ following the run
    def refresh(self) -> None:
        """Bring the unfinished call up to date with its run."""
        with self._lock:
            record = self.active()
            if record is None or record.get("run_id") is None:
                return
            try:
                run = self._rt.runner.get(record["run_id"])
            except NotFound:
                run = None
            if run is None or run.get("status") == "unknown":
                self._finish(record, FAILED, error=_error("runtime_restarted", RESTARTED))
            elif run["status"] in (SUCCEEDED, FAILED, STOPPED):
                self._finish_from_run(record, run)
            else:
                record["progress"] = run.get("progress")
                record["detail"] = self._detail(run)
                return
            self._save()
            self._publish()

    @staticmethod
    def _detail(run: dict) -> str | None:
        for event in reversed(run.get("events") or []):
            if event.get("type") == "progress" and event.get("message"):
                return _text(event["message"], MAX_DETAIL)
        return None

    def _finish_from_run(self, record: dict, run: dict) -> None:
        status, reason = run.get("status"), run.get("stop_reason")
        record["progress"] = run.get("progress")
        if status == SUCCEEDED:
            self._finish(record, SUCCEEDED, result=bounded_result(run.get("result")))
        elif status == STOPPED and reason == "cancelled":
            self._finish(record, CANCELLED, error=record.get("cancel") or _error("cancelled", run.get("error") or "已取消"))
        elif status == STOPPED:
            self._finish(record, STOPPED, error=_error("stopped", run.get("error") or "已停止"))
        else:
            code = "timeout" if reason == "timeout" else "run_failed"
            self._finish(record, FAILED, error=_error(code, run.get("error") or "运行失败"))

    def _finish(self, record: dict, status: str, *, result=None, error: dict | None = None, note: bool = True) -> None:
        record.update(status=status, result=result, error=error, finished_at=self._wall())
        record.pop("cancel", None)
        if not note:
            return
        self._rt.events.emit("remote.call_finished", call_id=record["call_id"], skill_id=record["skill_id"],
                             status=status, code=error["code"] if error else None, run_id=record["run_id"])
        outcome = "ok" if status == SUCCEEDED else ("rejected" if status == REJECTED else "error")
        self._rt.records.operation({
            "operation": "remote.call", "target": record["skill_id"], "operator": "cloud-link",
            "call_id": record["call_id"], "switch_set_by": record["set_by"], "version": record["version"],
            "digest": record["digest"], "parameters_digest": record["parameters_digest"],
            "parameters_summary": record["parameters_summary"], "run_id": record["run_id"],
            "hardware_authorized": record["run_id"] is not None, "status": status, "outcome": outcome,
            "code": error["code"] if error else None, "error": error["message"] if error else None,
            "ts": record["finished_at"]})
