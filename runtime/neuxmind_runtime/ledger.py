"""``request_id`` idempotency ledger (same semantics as brainos/edge.py).

* same id + same content  → the recorded result (or the recorded rejection)
* same id + other content → rejected (``request_id_reused``)
* a request that was in flight when the process died → ``unknown``; it is
  never replayed automatically, the operator must check the robot.
* a request that failed *before any side effect* (see ``effects.py``) is
  recorded as a rejection, never as ``unknown``; only a failure after a
  possible side effect (adapter call, run thread started, files written)
  is ``unknown``.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Callable

from . import effects
from .errors import AdapterFault, BadRequest, Conflict, RuntimeApiError, UnknownOutcome
from .store import Store

RUNNING, COMPLETED, REJECTED, UNKNOWN = "running", "completed", "rejected", "unknown"


def fingerprint(operation: str, payload: dict) -> str:
    data = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


class Ledger:
    def __init__(self, store: Store):
        self._store = store
        self._lock = threading.Lock()     # guards check-and-insert only

    def recover(self) -> list[str]:
        marked = []
        for record in self._store.list("requests"):
            if record["status"] == RUNNING:
                record["status"] = UNKNOWN
                self._store.put("requests", record)
                marked.append(record["id"])
        return marked

    def execute(self, operation: str, payload: dict, fn: Callable[[], dict]) -> dict:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise BadRequest("该操作需要 1–200 字符的 request_id", code="request_id_required")
        try:
            fp = fingerprint(operation, payload)
        except (TypeError, ValueError):
            raise BadRequest("请求内容不是合法 JSON（不允许 NaN/Infinity）") from None
        with self._lock:
            existing = self._store.get("requests", request_id)
            if existing is not None:
                return self._replay(existing, fp)
            record = {"id": request_id, "operation": operation, "fingerprint": fp,
                      "status": RUNNING, "created_at": time.time()}
            self._store.put("requests", record)
        effects.reset()
        try:
            response = fn()
            json.dumps(response, allow_nan=False)
        except AdapterFault as exc:
            if effects.happened():
                record.update(status=UNKNOWN, error=exc.to_dict())
            else:
                record.update(status=REJECTED, error=exc.to_dict(), http_status=exc.status)
            self._store.put("requests", record)
            raise
        except RuntimeApiError as exc:
            # Rejected before any effect: record so a retry gets the same answer.
            record.update(status=REJECTED, error=exc.to_dict(), http_status=exc.status)
            self._store.put("requests", record)
            raise
        except Exception as exc:
            if not effects.happened():
                err = RuntimeApiError(f"请求未执行（运行时内部错误：{type(exc).__name__}）",
                                      code="internal_error", status=500)
                record.update(status=REJECTED, error=err.to_dict(), http_status=500)
                self._store.put("requests", record)
                raise err from None
            record.update(status=UNKNOWN)
            self._store.put("requests", record)
            raise AdapterFault("设备端执行未确认；不会自动重试，请检查设备状态") from None
        finally:
            effects.reset()
        record.update(status=COMPLETED, response=response, completed_at=time.time())
        self._store.put("requests", record)
        return response

    @staticmethod
    def _replay(existing: dict, fp: str) -> dict:
        if existing["fingerprint"] != fp:
            raise Conflict("request_id 已用于不同请求", code="request_id_reused")
        status = existing["status"]
        if status == COMPLETED:
            return existing["response"]
        if status == REJECTED:
            err = dict(existing.get("error") or {})
            message = err.pop("error", "请求曾被拒绝")
            code = err.pop("code", "rejected")
            raise RuntimeApiError(message, code=code, status=existing.get("http_status", 400), extra=err)
        if status == RUNNING:
            raise Conflict("同一请求正在执行", code="in_progress")
        raise UnknownOutcome("上次请求结果未知，不会重复执行；请现场核对")
