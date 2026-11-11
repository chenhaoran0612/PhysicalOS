"""Metered gateway sessions (INTERFACES_V1 §2.1 / §4.3, details in cloud/README.md).

A session reserves credits for ``max_tokens`` up front; every call made with
the session's limited ``access_token`` is metered by the gateway and counted
against that budget; closing the session settles the reservation::

    from neuxmind.cloud import CloudClient

    cloud = CloudClient("http://127.0.0.1:8800", token)
    with cloud.gateway.open_session("echo-llm", max_tokens=2000) as session:
        reply = session.chat([{"role": "user", "content": "你好"}])
        print(reply["choices"][0]["message"]["content"], session.last_metering)

        stream = session.chat([{"role": "user", "content": "讲个故事"}], stream=True)
        for text in stream.text_deltas():
            print(text, end="", flush=True)
        print(stream.metering)                     # the gateway's neuxmind.metering event
    print(session.close_result)                    # settled on exit

Rules this module follows (from the contract):

* Streams are read **until the connection closes**: chat's ``data: [DONE]`` is
  passed through as an ordinary event, the gateway's single terminal event
  (``neuxmind.metering`` or ``error``) comes after it.
* Errors before the stream starts are JSON + HTTP status → :class:`NeuxMindError`.
  An in-stream ``event: error`` → :class:`GatewayStreamError` (``code`` + ``metering``).
* The connection ending without a terminal event → the SDK asks
  ``GET /gateway/sessions/{id}`` (waiting until the server has settled the call)
  and raises :class:`StreamInterrupted` with the recovered metering, or with
  ``code="outcome_unknown"`` when it cannot be determined.
* ``stream_options`` is never sent (the gateway writes it); state-changing
  requests are never retried automatically. The one exception is
  :meth:`GatewaySession.close` after a definite 409 ``session_busy`` (the close
  was refused, not executed): it waits until the call in flight has settled and
  sends the *same* ``request_id`` again (close is idempotent).
* One call in flight per session (ADR-016). A second concurrent call gets 409
  ``session_busy`` with a hint; open several sessions to work in parallel.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

from ._http import open_stream, request_json
from .errors import NeuxMindError

if TYPE_CHECKING:  # pragma: no cover
    from .cloud import CloudClient

METERING_EVENT = "neuxmind.metering"
MAX_LINE_BYTES = 1_000_000
MAX_EVENT_BYTES = 4_000_000


# ------------------------------------------------------------------ metering
@dataclass
class Metering:
    """What one call cost the session.

    ``source``: ``"stream"`` (the gateway's terminal event), ``"response"``
    (the ``neuxmind`` object of a non-streamed reply) or ``"session_query"``
    (recovered with ``GET /gateway/sessions/{id}`` after the stream broke off;
    ``call_tokens`` is then the change of the session's ``used_tokens`` since
    the previous call this object knows about, ``None`` if that is unknown).

    ``status``: ``succeeded`` · ``overage`` · ``reconciliation_required`` ·
    ``call_marked_lost`` (as the gateway reports them).
    """

    session_id: str
    status: str
    call_tokens: int | None
    used_tokens: int | None
    max_tokens: int | None
    output_limit: int | None = None
    hold_tokens: int | None = None
    usage: dict | None = None
    output_lower_bound: int | None = None
    upstream_ref: str | None = None
    client_disconnected: bool | None = None
    simulated: bool = False
    source: str = "stream"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def remaining_tokens(self) -> int | None:
        if self.used_tokens is None or self.max_tokens is None:
            return None
        return self.max_tokens - self.used_tokens

    @classmethod
    def from_event(cls, payload: dict, source: str = "stream") -> "Metering":
        return cls(session_id=payload.get("session_id", ""), status=payload.get("status", ""),
                   call_tokens=payload.get("call_tokens"), used_tokens=payload.get("used_tokens"),
                   max_tokens=payload.get("max_tokens"), output_limit=payload.get("output_limit"),
                   hold_tokens=payload.get("hold_tokens"), usage=payload.get("usage"),
                   output_lower_bound=payload.get("output_lower_bound"), upstream_ref=payload.get("upstream_ref"),
                   client_disconnected=payload.get("client_disconnected"), simulated=bool(payload.get("simulated")),
                   source=source, raw=dict(payload))

    def to_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "raw"}
        out["remaining_tokens"] = self.remaining_tokens
        return out

    def describe(self) -> str:
        """One line of Chinese for people (used by ``nx chat``)."""
        status = {"succeeded": "已计量", "overage": "超额（按预留上限计入，会话已锁定）",
                  "reconciliation_required": "结果未知（按预留上限计入，待运营核对）",
                  "call_marked_lost": "已被判定丢失（待核对）"}.get(self.status, self.status)
        call = "未知" if self.call_tokens is None else f"{self.call_tokens} token"
        source = {"session_query": "（连接中断后查询会话得到）", "response": ""}.get(self.source, "")
        line = f"计量：本次 {call}，会话已用 {self.used_tokens}/{self.max_tokens}，{status}{source}"
        if self.simulated:
            line += "；测试替身（simulated=true）"
        return line


# ------------------------------------------------------------------ errors
class GatewayStreamError(NeuxMindError):
    """The gateway ended a stream with ``event: error`` (e.g. ``output_limit_reached``).

    ``code`` is the gateway's code, ``metering`` its metering object (or ``None``),
    ``event`` the whole error payload. Text received before the error is in
    ``stream.text``.
    """

    def __init__(self, message: str, code: str, metering: Metering | None, event: dict):
        super().__init__(message, code=code, details={"event": event, "hint": STREAM_HINTS.get(code, "")})
        self.metering = metering
        self.event = event


class StreamInterrupted(NeuxMindError):
    """The connection ended before the gateway's terminal event.

    ``code == "stream_interrupted"``: the SDK queried the session after the server
    settled the call; ``metering`` (``source="session_query"``) is the final usage.
    ``code == "outcome_unknown"``: the usage could not be determined (the call
    was still in flight when the SDK gave up waiting, or the query failed);
    ``metering`` is ``None`` — query later with ``session.status()`` /
    ``nx sessions show <id>``. The output in ``stream.text`` is incomplete either way.
    """

    def __init__(self, message: str, code: str, metering: Metering | None, details: dict):
        super().__init__(message, code=code, details=details)
        self.metering = metering


STREAM_HINTS = {
    "output_limit_reached": "上游输出超过本次输出上限，网关已截断：本次按预留上限计入会话用量，会话关闭时待运营核对；"
                            "已收到的文本保留在 stream.text。",
    "upstream_interrupted": "上游在报告用量前中断：本次按预留上限计入会话用量，关闭时待核对。可以重新调用（新的一次调用）。",
    "upstream_stream_error": "上游在流中返回错误且没有用量：本次按预留上限计入，待核对。",
    "usage_missing": "上游没有报告用量：本次按预留上限计入，待核对。",
    "stream_duration_exceeded": "流式调用超过最长时长：本次按预留上限计入，待核对。",
    "usage_exceeds_reservation": "上游用量超过本次预留：按预留上限计费，超出部分待运营处理，会话已锁定，只能关闭（ADR-012）。",
    "call_marked_lost": "清理任务已把这次调用判定为丢失，不再计量，由运营核对。",
    "internal_error": "网关计量失败，本次调用将由清理任务转人工核对。",
}


# ------------------------------------------------------------------ SSE
@dataclass
class StreamEvent:
    """One SSE event of a gateway stream (upstream events are passed through as they are)."""

    event: str | None
    data: str
    json: Any = None

    @property
    def type(self) -> str | None:
        """``event:`` name, else the JSON ``type`` field (``None`` for chat chunks)."""
        if self.event:
            return self.event
        return self.json.get("type") if isinstance(self.json, dict) else None

    @property
    def is_done(self) -> bool:
        """chat's ``data: [DONE]`` — **not** the end of a NeuxMind stream."""
        return self.data.strip() == "[DONE]"

    @property
    def is_metering(self) -> bool:
        return self.event == METERING_EVENT

    @property
    def is_error(self) -> bool:
        return self.event == "error"

    @property
    def text(self) -> str:
        """Generated text in this event ('' if none): chat ``choices[].delta.content``,
        responses ``response.output_text.delta``."""
        obj = self.json
        if not isinstance(obj, dict):
            return ""
        if obj.get("type") == "response.output_text.delta" or self.event == "response.output_text.delta":
            delta = obj.get("delta")
            return delta if isinstance(delta, str) else ""
        choices = obj.get("choices")
        if isinstance(choices, list):
            parts = []
            for choice in choices:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
            return "".join(parts)
        return ""


class _SSEReader:
    """Incremental SSE parser over ``read1`` chunks (WHATWG rules: comments, ``id``
    and ``retry`` ignored; an unterminated event at EOF is discarded)."""

    def __init__(self, response):
        self._resp = response
        self._buf = b""
        self.eof = False

    def _lines(self) -> Iterator[bytes]:
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                yield line.rstrip(b"\r")
                continue
            if len(self._buf) > MAX_LINE_BYTES:
                raise _Broken("SSE 行过长")
            chunk = self._resp.read1(65536)
            if not chunk:
                self.eof = True
                return
            self._buf += chunk

    def events(self) -> Iterator[StreamEvent]:
        event: str | None = None
        data: list[str] = []
        size = 0
        for raw in self._lines():
            line = raw.decode("utf-8", errors="replace")
            if line == "":
                if data:
                    text = "\n".join(data)
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        obj = None
                    yield StreamEvent(event, text, obj)
                event, data, size = None, [], 0
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if name == "event":
                event = value or None
            elif name == "data":
                size += len(value)
                if size > MAX_EVENT_BYTES:
                    raise _Broken("SSE 事件过大")
                data.append(value)


class _Broken(Exception):
    pass


_READ_ERRORS = (OSError, http.client.HTTPException, ValueError, _Broken)   # timeouts, resets, IncompleteRead


class GatewayStream:
    """A streamed session call. Iterate it once (or use :meth:`text_deltas`).

    Yields every :class:`StreamEvent` the gateway sends — upstream events, chat's
    ``[DONE]``, and finally the ``neuxmind.metering`` event — and stops when the
    connection closes. Afterwards ``metering`` is set. An ``event: error``
    terminal event raises :class:`GatewayStreamError`; a connection that ends
    without a terminal event raises :class:`StreamInterrupted`.

    ``text`` accumulates the generated text; ``events`` keeps every event.
    Leaving the loop early (``break`` / :meth:`close`) abandons the stream: the
    gateway keeps reading the upstream and settles on its own; the session stays
    busy until then (``session.close()`` waits for it).
    """

    def __init__(self, session: "GatewaySession", response, kind: str):
        self.session = session
        self.kind = kind
        self._resp = response
        headers = response.headers
        self.session_id = headers.get("X-NeuxMind-Session") or session.session_id
        limit = headers.get("X-NeuxMind-Output-Limit")
        self.output_limit = int(limit) if limit and limit.isdigit() else None
        self.simulated = (headers.get("X-NeuxMind-Simulated") or "").lower() == "true"
        self.events: list[StreamEvent] = []
        self.text = ""
        self.metering: Metering | None = None
        self.error: GatewayStreamError | None = None
        self.interrupted: StreamInterrupted | None = None
        self.abandoned = False
        self.finished = False
        self._started = False

    def __repr__(self) -> str:
        return f"GatewayStream({self.kind}, session={self.session_id!r}, finished={self.finished})"

    def __enter__(self) -> "GatewayStream":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """Stop reading (abandon if not finished) and close the connection."""
        if not self.finished:
            self.abandoned = True
            self.session._invalidate()
        try:
            self._resp.close()
        except Exception:
            pass

    def __iter__(self) -> Iterator[StreamEvent]:
        if self._started:
            raise NeuxMindError("流只能读取一次（已读取的事件在 stream.events）", code="stream_consumed")
        self._started = True
        return self._iterate()

    def _iterate(self) -> Iterator[StreamEvent]:
        terminal: StreamEvent | None = None
        broken: str | None = None
        reader = _SSEReader(self._resp)
        completed = False
        try:
            try:
                for ev in reader.events():
                    if terminal is not None:
                        continue          # nothing may follow the terminal event (contract): ignore
                    self.events.append(ev)
                    if ev.is_metering or ev.is_error:
                        terminal = ev
                        if ev.is_metering:
                            payload = ev.json if isinstance(ev.json, dict) else {}
                            self.metering = Metering.from_event(payload)
                            self.session._after_metering(self.metering)
                            yield ev
                        continue
                    piece = ev.text
                    if piece:
                        self.text += piece
                    yield ev
            except _READ_ERRORS as exc:
                broken = f"{type(exc).__name__}: {exc}"
            completed = True
        finally:
            try:
                self._resp.close()
            except Exception:
                pass
            if not completed and not self.finished:      # the consumer left the loop early
                self.abandoned = True
                self.session._invalidate()
        self.finished = True
        if terminal is not None and terminal.is_error:
            raise self._stream_error(terminal)
        if terminal is None:
            self.interrupted = self.session._recover(self, broken or "连接在终止事件之前关闭")
            raise self.interrupted

    def _stream_error(self, ev: StreamEvent) -> GatewayStreamError:
        payload = ev.json if isinstance(ev.json, dict) else {"message": ev.data}
        code = payload.get("code") or (payload.get("error") or {}).get("code") or "stream_error"
        meter = payload.get("neuxmind")
        metering = Metering.from_event(meter) if isinstance(meter, dict) else None
        if metering is not None:
            self.metering = metering
            self.session._after_metering(metering)
        else:
            self.session._invalidate()
        hint = STREAM_HINTS.get(code, "")
        message = payload.get("message") or "网关在流中返回错误"
        self.error = GatewayStreamError(f"{message}。{hint}" if hint else message, code, metering, payload)
        return self.error

    def text_deltas(self) -> Iterator[str]:
        """Only the generated text pieces (same end-of-stream rules as iterating)."""
        for ev in self:
            piece = ev.text
            if piece:
                yield piece

    def collect(self) -> "GatewayStream":
        """Read to the end; returns ``self`` (``text``, ``metering``)."""
        for _ in self:
            pass
        return self


# ------------------------------------------------------------------ session
_RECOVER_INTERVAL = 0.2


class GatewaySession:
    """An open metered session (from :meth:`CloudClient.gateway.open_session`).

    ``openai_base_url`` + ``access_token`` work with any OpenAI-compatible client
    (``openai.OpenAI(base_url=session.openai_base_url, api_key=session.access_token)``);
    the gateway accepts only allow-listed parameters (cloud/README).
    """

    def __init__(self, client: "CloudClient", view: dict, *, call_timeout: float = 300.0,
                 stream_timeout: float = 120.0, recover_timeout: float = 60.0):
        self._client = client
        self.view = dict(view)
        self.session_id: str = view["session_id"]
        self.model: str | None = view.get("model")
        self.max_tokens: int | None = view.get("max_tokens")
        self.expires_at = view.get("expires_at")
        self.reservation_id = view.get("reservation_id")
        self.reserved_credits = view.get("reserved_credits")
        self.access_token: str | None = view.get("access_token")
        self.server_base_url: str | None = view.get("base_url")
        self.replayed = bool(view.get("replayed"))
        self.call_timeout = call_timeout
        self.stream_timeout = stream_timeout
        self.recover_timeout = recover_timeout
        self.last_metering: Metering | None = None
        self.close_result: dict | None = None
        self.close_error: NeuxMindError | None = None
        self._close_request_id: str | None = None
        self._lock = threading.Lock()
        # Session counters as of the end of the last call this object observed
        # (a fresh session starts at zero); ``None`` = unknown, re-read before a stream.
        self._snapshot: dict | None = None if self.replayed else {
            "used_tokens": 0, "calls": 0, "unknown_calls": 0, "overage_calls": 0}

    def __repr__(self) -> str:
        return f"GatewaySession({self.session_id!r}, model={self.model!r}, max_tokens={self.max_tokens})"

    # -------------------------------------------------------- OpenAI compatibility
    @property
    def openai_base_url(self) -> str:
        """``{api}/gateway/sessions/{id}/v1`` on the address this client already reaches
        (the server's own ``base_url`` uses its configured public address: ``server_base_url``)."""
        return f"{self._client.api}/gateway/sessions/{urllib.parse.quote(self.session_id)}/v1"

    # -------------------------------------------------------- context manager
    def __enter__(self) -> "GatewaySession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.close()
        else:
            try:
                self.close()
            except NeuxMindError as close_exc:     # never mask the original error
                self.close_error = close_exc
        return False

    # -------------------------------------------------------- calls
    def chat(self, messages: list, *, stream: bool = False, **params: Any) -> dict | GatewayStream:
        """``{base_url}/chat/completions``. Returns the upstream JSON (plus ``neuxmind``),
        or a :class:`GatewayStream` with ``stream=True``."""
        return self._call("/chat/completions", "chat", {"messages": messages}, stream, params)

    def responses(self, input: Any, *, stream: bool = False, **params: Any) -> dict | GatewayStream:
        """``{base_url}/responses`` (OpenAI Responses API shape)."""
        return self._call("/responses", "responses", {"input": input}, stream, params)

    def _call(self, path: str, kind: str, body: dict, stream: bool, params: dict):
        if self.close_result is not None:
            raise NeuxMindError("会话已关闭", code="session_closed", details={"session_id": self.session_id})
        if not self.access_token:
            raise NeuxMindError("没有该会话的访问令牌：access_token 只在开会话时返回一次（同一 request_id 重放时为空）；"
                                "请开一个新会话", code="access_token_unavailable",
                                details={"session_id": self.session_id})
        if "stream_options" in params:
            from .cloud import HINTS
            raise NeuxMindError("不要传 stream_options：网关会自己写入（用于计量）。本次请求未发送。"
                                + HINTS["parameter_not_allowed"], code="parameter_not_allowed",
                                details={"parameters": ["stream_options"], "hint": HINTS["parameter_not_allowed"]})
        payload = {**params, **body}
        if stream:
            payload["stream"] = True
        url = self.openai_base_url + path
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if stream:
            if self._snapshot is None:
                self._refresh_snapshot()
            try:
                response = open_stream("POST", url, body=payload, headers=headers, timeout=self.stream_timeout)
            except NeuxMindError as exc:
                self._error(exc)
            return GatewayStream(self, response, kind)
        try:
            data = request_json("POST", url, body=payload, headers=headers, timeout=self.call_timeout)
        except NeuxMindError as exc:
            self._error(exc)
        meter = data.get("neuxmind") if isinstance(data, dict) else None
        if isinstance(meter, dict):
            with self._lock:
                previous = self._snapshot["used_tokens"] if self._snapshot else None
                used = meter.get("used_tokens")
                call_tokens = used - previous if isinstance(used, int) and previous is not None else None
                if self._snapshot is not None and isinstance(used, int):
                    self._snapshot = dict(self._snapshot, used_tokens=used, calls=self._snapshot["calls"] + 1)
                else:
                    self._snapshot = None
            self.last_metering = Metering(session_id=meter.get("session_id", self.session_id), status="succeeded",
                                          call_tokens=call_tokens, used_tokens=used,
                                          max_tokens=meter.get("max_tokens"), output_limit=meter.get("output_limit"),
                                          usage=data.get("usage"), simulated=bool(meter.get("simulated")),
                                          source="response", raw=dict(meter))
        return data

    def _error(self, exc: NeuxMindError):
        """Errors of a session call: add the hint, keep the session id, forget the snapshot."""
        from .cloud import add_hint
        if exc.status is None or exc.status >= 500:
            # Outcome unknown, or the gateway counted something (502 upstream_unknown keeps
            # the hold): the local baseline is no longer reliable. A 4xx was refused before
            # anything was counted (allow-list, budget, session_busy ...): keep it.
            self._invalidate()
        exc.details.setdefault("session_id", self.session_id)
        if exc.code == "outcome_unknown":
            exc.details["hint"] = (f"调用已发出但没有收到响应：结果未知。SDK 不会自动重试；用 session.status() 或 "
                                   f"nx sessions show {self.session_id} 查看会话用量")
            exc.message = f"{exc.message}。{exc.details['hint']}"
            exc.args = (exc.message,)
        add_hint(exc)
        raise exc

    # -------------------------------------------------------- bookkeeping
    def _invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def _after_metering(self, m: Metering) -> None:
        self.last_metering = m
        with self._lock:
            snap = self._snapshot
            if snap is None or not isinstance(m.used_tokens, int):
                self._snapshot = None
                return
            snap = dict(snap, used_tokens=m.used_tokens)
            if m.status == "succeeded":
                snap["calls"] += 1
            elif m.status == "overage":
                snap["calls"] += 1
                snap["overage_calls"] += 1
            elif m.status == "reconciliation_required":
                snap["unknown_calls"] += 1
            else:
                snap = None
            self._snapshot = snap

    def _refresh_snapshot(self) -> None:
        try:
            view = self.status()
        except NeuxMindError:
            return                        # only a baseline for recovery; never blocks the call
        if view.get("inflight_tokens"):
            return                        # someone else's call is in flight: keep unknown
        with self._lock:
            self._snapshot = {k: view.get(k) or 0 for k in ("used_tokens", "calls", "unknown_calls", "overage_calls")}

    def _recover(self, stream: GatewayStream, reason: str) -> StreamInterrupted:
        """The stream ended without a terminal event: wait for the server to settle, then read the session."""
        with self._lock:
            before, self._snapshot = self._snapshot, None
        details = {"session_id": self.session_id, "reason": reason, "received_events": len(stream.events)}
        deadline = time.monotonic() + max(0.0, self.recover_timeout)
        view = None
        try:
            while True:
                view = self.status()
                if not view.get("inflight_tokens") or time.monotonic() >= deadline:
                    break
                time.sleep(_RECOVER_INTERVAL)
        except NeuxMindError as exc:
            details["query_error"] = exc.to_dict()
            return StreamInterrupted(
                f"流式连接在网关的终止事件之前中断（{reason}），查询会话用量也失败：结果未知。"
                f"稍后用 session.status() 或 nx sessions show {self.session_id} 查看", "outcome_unknown", None, details)
        details["session"] = view
        if view.get("inflight_tokens"):
            return StreamInterrupted(
                f"流式连接在网关的终止事件之前中断（{reason}）；等待 {self.recover_timeout:g} 秒后服务端仍未结算这次调用："
                f"结果未知。服务端会继续读完上游并按实际用量结算，稍后用 nx sessions show {self.session_id} 查看",
                "outcome_unknown", None, details)
        status, call_tokens = None, None
        if before is not None:
            counters = {k: (view.get(k) or 0) - before[k] for k in ("calls", "unknown_calls", "overage_calls")}
            if counters["overage_calls"] > 0:
                status = "overage"
            elif counters["unknown_calls"] > 0:
                status = "reconciliation_required"
            elif counters["calls"] > 0:
                status = "succeeded"
            if sum(counters.values()) == 1:
                call_tokens = (view.get("used_tokens") or 0) - before["used_tokens"]
            with self._lock:
                if status is not None and sum(counters.values()) == 1:
                    self._snapshot = {k: view.get(k) or 0
                                      for k in ("used_tokens", "calls", "unknown_calls", "overage_calls")}
        if status is None:
            return StreamInterrupted(
                f"流式连接在网关的终止事件之前中断（{reason}）；会话已结算，但无法确定这次调用的结果"
                f"（会话已用 {view.get('used_tokens')}/{view.get('max_tokens')}）：结果未知",
                "outcome_unknown", None, details)
        metering = Metering(session_id=self.session_id, status=status, call_tokens=call_tokens,
                            used_tokens=view.get("used_tokens"), max_tokens=view.get("max_tokens"),
                            output_limit=stream.output_limit, client_disconnected=True, simulated=stream.simulated,
                            source="session_query", raw=dict(view))
        stream.metering = metering
        self.last_metering = metering
        return StreamInterrupted(
            f"流式连接在网关的终止事件之前中断（{reason}）：输出不完整。服务端已读完上游并结算，"
            f"{metering.describe()}", "stream_interrupted", metering, details)

    # -------------------------------------------------------- status & close
    def status(self) -> dict:
        """``GET /gateway/sessions/{id}`` (uses the CloudClient's login / API key, not the access token)."""
        return self._client.gateway.session(self.session_id)

    def close(self, request_id: str | None = None, *, wait: float = 30.0) -> dict:
        """Close and settle → ``{status: closed|reconciliation_required, usage_tokens, credits_charged, ...}``.

        Idempotent: a second call returns the stored result. If a call is still in
        flight (409 ``session_busy``, e.g. an abandoned stream the gateway is still
        draining) the close was refused; it waits up to ``wait`` seconds for the
        call to settle and sends the same ``request_id`` again.
        """
        if self.close_result is not None:
            return self.close_result
        from .robot import new_request_id
        if self._close_request_id is None:
            self._close_request_id = request_id or new_request_id("close")
        deadline = time.monotonic() + max(0.0, wait)
        path = f"/gateway/sessions/{urllib.parse.quote(self.session_id)}/close"
        while True:
            try:
                result = self._client._request("POST", path, {"request_id": self._close_request_id})
                break
            except NeuxMindError as exc:
                if exc.code != "session_busy" or time.monotonic() >= deadline:
                    raise
            while time.monotonic() < deadline:
                time.sleep(_RECOVER_INTERVAL)
                if not self.status().get("inflight_tokens"):
                    break
        self.close_result = result
        return result
