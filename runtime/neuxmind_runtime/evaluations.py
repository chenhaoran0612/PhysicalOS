"""Asynchronous on-site tests (INTERFACES_V1 §4.1).

``POST /v1/evaluate`` with ``async: true`` starts the application run and
returns ``{accepted, evaluation_id, status: "running"}`` at once; the
desktop polls ``GET /v1/evaluations/{id}`` →
``{evaluation_id, status: running|completed|unknown, result?}``. ``result``
has exactly the shape of the synchronous ``/v1/evaluate`` response.

Rules
* Idempotent through the request ledger: the same ``request_id`` + content
  returns the same ``evaluation_id``.
* Completion is recorded by the run's finish hook (the run thread or the
  monitor) -- not by polling -- so a finished test survives a restart.
* A stop / e-stop / cancel during the test ends it **immediately** as
  ``completed`` with ``passed=false`` and the reason in ``cases``: the
  first poll after the stop already returns it, even while the skill
  process is still inside its grace period. Its final status can never
  become "passed" afterwards (the record is written once).
* After a runtime restart an unfinished evaluation is ``unknown`` and is
  never replayed.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

from .errors import NotFound
from .store import Store

KIND = "evaluations"
RUNNING, COMPLETED, UNKNOWN = "running", "completed", "unknown"
STOP_CASE = "现场测试未被停止"
RESTART_NOTE = "运行时重启时该现场测试未结束；结果未知，不会自动重放，请现场核对"


def new_id() -> str:
    return "evaluation-" + uuid.uuid4().hex[:16]


class Evaluations:
    """Evaluation records in the runtime store.

    ``build_result(record, run_record, stop_note)`` produces the synchronous
    ``/v1/evaluate`` response for a finished (or stopped) run; it is owned
    by :class:`~neuxmind_runtime.core.EdgeRuntime` so both modes share it.
    """

    def __init__(self, store: Store, runner, build_result: Callable[[dict, dict | None, str | None], dict]):
        self._store = store
        self._runner = runner
        self._build = build_result
        self._lock = threading.Lock()        # complete-once; never taken by the stop path

    def recover_after_restart(self) -> list[str]:
        marked = []
        for record in self._store.list(KIND):
            if record["status"] == RUNNING:
                record.update(status=UNKNOWN, error=RESTART_NOTE, completed_at=time.time())
                self._store.put(KIND, record)
                marked.append(record["id"])
        return marked

    # ------------------------------------------------------------ lifecycle
    def create(self, evaluation_id: str, request_id: str, application_digest, precheck: list[dict]) -> dict:
        record = {"id": evaluation_id, "status": RUNNING, "request_id": request_id, "run_id": None,
                  "application_digest": application_digest, "precheck": precheck, "created_at": time.time()}
        self._store.put(KIND, record)
        return record

    def attach_run(self, evaluation_id: str, run_id: str) -> None:
        with self._lock:
            record = self._store.get(KIND, evaluation_id)
            if record is not None and record.get("run_id") is None:
                record["run_id"] = run_id     # the status (maybe already completed) is kept as is
                self._store.put(KIND, record)

    def complete(self, evaluation_id: str, run_record: dict | None, stop_note: str | None = None) -> dict | None:
        """Write the result once. Later calls (e.g. the finish hook after a stop) are ignored."""
        with self._lock:
            record = self._store.get(KIND, evaluation_id)
            if record is None or record["status"] != RUNNING:
                return record
            if run_record is not None:
                record["run_id"] = run_record.get("id") or record.get("run_id")
            record.update(status=COMPLETED, result=self._build(record, run_record, stop_note),
                          completed_at=time.time())
            self._store.put(KIND, record)
            return record

    def discard(self, evaluation_id: str) -> None:
        """The request failed before its id was returned: never reported as running."""
        with self._lock:
            record = self._store.get(KIND, evaluation_id)
            if record is not None and record["status"] == RUNNING:
                record.update(status="rejected", completed_at=time.time())
                self._store.put(KIND, record)

    def on_run_finished(self, evaluation_id: str) -> Callable:
        def hook(run) -> None:
            self.complete(evaluation_id, run.to_dict())
        return hook

    # ------------------------------------------------------------ query
    def get(self, evaluation_id: str) -> dict:
        record = self._store.get(KIND, evaluation_id)
        if record is None or record["status"] not in (RUNNING, COMPLETED, UNKNOWN):
            # Internal states (e.g. a "rejected" orphan from a failed submission) are never
            # exposed: §4.1 defines only running/completed/unknown (Q2-05).
            raise NotFound(f"找不到现场测试 {evaluation_id}", code="evaluation_not_found")
        if record["status"] == RUNNING and record.get("run_id"):
            run = self._runner.live_run(record["run_id"])
            if run is not None and run.finalized:
                record = self.complete(evaluation_id, run.to_dict()) or record
            elif run is not None and run.stop_event.is_set():
                # Stopped / cancelled / timed out: end it now, not after the skill's grace period.
                record = self.complete(evaluation_id, run.to_dict(), stop_note_for(run.stop_reason)) or record
        return self.public(record)

    @staticmethod
    def public(record: dict) -> dict:
        out = {"evaluation_id": record["id"], "status": record["status"], "run_id": record.get("run_id"),
               "created_at": record.get("created_at")}
        if record["status"] == COMPLETED:
            out["result"] = record["result"]
            out["completed_at"] = record.get("completed_at")
        if record["status"] == UNKNOWN:
            out["error"] = record.get("error") or RESTART_NOTE
        return out


def stop_note_for(stop_reason: str | None) -> str:
    return {"cancelled": "现场测试被取消，未完成",
            "timeout": "现场测试超时，未完成"}.get(stop_reason or "", "现场测试期间收到停止/急停，未完成")
