"""Streamed session calls (contract §4.3): SSE pass-through with server-side metering.

Life of a streamed call (``stream: true`` on ``{base_url}/chat/completions`` or
``{base_url}/responses``)::

    request thread   ── reserve txn (short; wallet lock; N-05 one call in flight)
                     ── open the upstream stream (status/headers only)
                        errors here → ordinary JSON error, same rules as non-stream
    producer thread  ── read upstream SSE line by line, meter every event,
                        forward it to the client channel           (NO transaction,
                                                                     NO wallet lock)
                     ── settle txn (short; wallet lock)
                     ── one terminal gateway event: ``neuxmind.metering`` or ``error``
    event loop       ── ASGI response: pull from the channel, write to the socket,
                        watch for ``http.disconnect``

Metering (:class:`StreamMeter`):

* The provider's usage event is the billing evidence (chat: the
  ``stream_options.include_usage`` chunk the gateway asks for; responses:
  ``response.completed`` / ``incomplete`` / ``failed``).
* Lower bound: every forwarded *non-empty output delta* counts as ≥ 1 output
  token (OpenAI and the common compatible servers emit output token by token,
  so a non-empty delta is at least one token). Billing uses
  ``max(reported, lower bound)``.
* Truncation: before forwarding an event that would push the lower bound
  above this call's server-set output limit, the gateway stops reading,
  drops the upstream connection and ends with ``event: error`` /
  ``code: output_limit_reached``. An upstream that honours the limit can never
  trigger it (lower bound ≤ real tokens ≤ limit).
* No usage (stream cut, error event, truncation, deadline): the call is
  counted at its full hold in the session's ``used_tokens`` (so the remaining
  budget can never be over-spent) and flagged unknown → the session closes as
  ``reconciliation_required`` (ADR-012).

Client disconnect: the producer stops forwarding but keeps *draining* the
upstream (still metered, still truncated, still bounded by the deadline) to
obtain the provider's usage, then settles exactly; if draining fails the
"no usage" rule applies. The session stays busy (409 ``session_busy``) until
the producer has settled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import anyio
from starlette.responses import Response

from .upstream import UpstreamError, UpstreamStream, UpstreamUnknown

log = logging.getLogger("neuxmind_cloud.streaming")

MAX_EVENT_DATA_BYTES = 4_000_000
CHANNEL_CAPACITY = 64

# Responses API delta events whose payload is generated output tokens.
RESPONSES_OUTPUT_DELTAS = ("response.output_text.delta", "response.refusal.delta",
                           "response.function_call_arguments.delta", "response.reasoning_text.delta")
RESPONSES_TERMINAL = ("response.completed", "response.incomplete", "response.failed")
# Chat delta fields that carry generated text.
CHAT_TEXT_FIELDS = ("content", "refusal", "reasoning_content", "reasoning")


# ------------------------------------------------------------------ SSE parse
@dataclass
class SSEEvent:
    event: str | None
    data: str

    def encode(self) -> bytes:
        head = f"event: {self.event}\n" if self.event else ""
        body = "".join(f"data: {line}\n" for line in self.data.split("\n"))
        return (head + body + "\n").encode()


def split_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Split arbitrary byte chunks into lines on CRLF, LF **or a lone CR** (WHATWG
    SSE). Yields lines without their terminator; a trailing partial line at end
    of stream is yielded too (the parser then discards the incomplete event).
    Review Q2-03: splitting on LF only let a lone CR inside ``data`` reach
    clients that (correctly) treat it as a line break — an injection vector."""
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while True:
            i_cr, i_lf = buffer.find(b"\r"), buffer.find(b"\n")
            if i_cr < 0 and i_lf < 0:
                break
            if i_cr >= 0 and (i_lf < 0 or i_cr < i_lf):
                if i_cr == len(buffer) - 1:
                    break                      # wait: CR may be the first half of CRLF
                line, rest = buffer[:i_cr], buffer[i_cr + 1:]
                if rest.startswith(b"\n"):
                    rest = rest[1:]
            else:
                line, rest = buffer[:i_lf], buffer[i_lf + 1:]
            buffer = rest
            yield line
        if len(buffer) > MAX_EVENT_DATA_BYTES:
            raise UpstreamUnknown("上游流式响应格式无效", "sse line too long")
    if buffer.endswith(b"\r"):
        buffer = buffer[:-1]
    if buffer:
        yield buffer


def parse_sse(chunks: Iterable[bytes]) -> Iterator[SSEEvent]:
    """Minimal WHATWG-style SSE parser. Comments, ``id`` and ``retry`` are dropped;
    an incomplete event at end of stream is discarded (as the spec requires).
    Values can never contain CR or LF (they come from single lines), so
    re-encoding cannot create new fields or events."""
    event: str | None = None
    data: list[str] = []
    size = 0
    for raw in split_lines(chunks):
        line = raw.decode("utf-8", errors="replace")
        if line == "":
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data, size = None, [], 0
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value[:200] or None
        elif name == "data":
            size += len(value)
            if size > MAX_EVENT_DATA_BYTES:
                raise UpstreamUnknown("上游流式事件过大", "sse event too large")
            data.append(value)


# Event names the gateway reserves for itself (review Q2-03): the terminal
# ``neuxmind.metering`` / ``error`` event must be unique and unforgeable.
GATEWAY_EVENT_PREFIX = "neuxmind."
UPSTREAM_ERROR_EVENT = "upstream.error"


def _reserved_data_type(data: str) -> bool:
    try:
        obj = json.loads(data)
    except ValueError:
        return False
    kind = obj.get("type") if isinstance(obj, dict) else None
    return isinstance(kind, str) and kind.lower().startswith(GATEWAY_EVENT_PREFIX)


def outbound_event(ev: SSEEvent) -> SSEEvent | None:
    """Map an upstream event to what the client receives.

    * ``event: error`` → ``event: upstream.error`` (data unchanged);
    * any event named ``neuxmind.*`` (case-insensitive), or whose JSON ``type``
      is ``neuxmind.*`` → dropped (counted in the call's evidence);
    * everything else unchanged."""
    name = ev.event
    if name is not None and name.lower().startswith(GATEWAY_EVENT_PREFIX):
        return None
    if _reserved_data_type(ev.data):
        return None
    if name is not None and name.lower() == "error":
        return SSEEvent(UPSTREAM_ERROR_EVENT, ev.data)
    return ev


def encode_event(event: str, payload: dict) -> bytes:
    return SSEEvent(event, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))).encode()


# ------------------------------------------------------------------ metering
def _nonneg_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class StreamUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class StreamMeter:
    """Meters one upstream stream. :meth:`feed` returns ``"forward"`` or ``"truncate"``."""

    def __init__(self, path: str, output_limit: int):
        self.chat = path != "/responses"
        self.output_limit = output_limit
        self.lower_bound = 0          # forwarded non-empty output deltas (each ≥ 1 token)
        self.usage: StreamUsage | None = None
        self.upstream_id = ""
        self.done = False             # upstream sent its terminal event
        self.upstream_error = False   # upstream sent an error event
        self.events = 0
        self.forwarded_bytes = 0

    # -- usage -------------------------------------------------------------
    def _take_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        prompt = _nonneg_int(usage.get("prompt_tokens"))
        if prompt is None:
            prompt = _nonneg_int(usage.get("input_tokens"))
        completion = _nonneg_int(usage.get("completion_tokens"))
        if completion is None:
            completion = _nonneg_int(usage.get("output_tokens"))
        if prompt is None or completion is None:
            return
        total = _nonneg_int(usage.get("total_tokens"))
        self.usage = StreamUsage(prompt, completion, max(total or 0, prompt + completion))

    # -- per event -----------------------------------------------------------
    def _chat(self, ev: SSEEvent) -> int:
        if ev.data.strip() == "[DONE]":
            self.done = True
            return 0
        try:
            obj = json.loads(ev.data)
        except ValueError:
            return 0
        if not isinstance(obj, dict):
            return 0
        if obj.get("error") is not None or ev.event == "error":
            self.upstream_error = True
        if not self.upstream_id and isinstance(obj.get("id"), str):
            self.upstream_id = obj["id"][:200]
        if obj.get("usage") is not None:
            self._take_usage(obj["usage"])
        units = 0
        choices = obj.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            units += sum(1 for f in CHAT_TEXT_FIELDS if isinstance(delta.get(f), str) and delta[f])
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and any(isinstance(fn.get(k), str) and fn[k] for k in ("name", "arguments")):
                    units += 1
        return units

    def _responses(self, ev: SSEEvent) -> int:
        try:
            obj = json.loads(ev.data)
        except ValueError:
            return 0
        if not isinstance(obj, dict):
            return 0
        kind = obj.get("type") if isinstance(obj.get("type"), str) else (ev.event or "")
        response = obj.get("response") if isinstance(obj.get("response"), dict) else None
        if response is not None and not self.upstream_id and isinstance(response.get("id"), str):
            self.upstream_id = response["id"][:200]
        if kind == "error" or ev.event == "error":
            self.upstream_error = True
            return 0
        if kind in RESPONSES_TERMINAL:
            self.done = True
            if response is not None:
                self._take_usage(response.get("usage"))
            return 0
        # Review Q2-06: every ``*.delta`` event with a non-empty string delta is
        # generated output (text, refusal, function arguments, reasoning text
        # and summaries, …); counting all of them keeps the bound a lower bound.
        if kind.endswith(".delta") and isinstance(obj.get("delta"), str) and obj["delta"]:
            return 1
        return 0

    def feed(self, ev: SSEEvent) -> str:
        units = self._chat(ev) if self.chat else self._responses(ev)
        reported = self.usage.completion_tokens if self.usage else 0
        if units and max(self.lower_bound + units, reported) > self.output_limit:
            return "truncate"
        self.lower_bound += units
        self.events += 1
        return "forward"


# ------------------------------------------------------------------ outcome
@dataclass
class StreamOutcome:
    """What the producer observed; handed to the gateway's settle function."""

    termination: str              # completed | output_limit | interrupted | usage_missing | deadline | internal
    usage: StreamUsage | None
    upstream_ref: str
    lower_bound: int
    events: int
    forwarded_bytes: int
    client_disconnected: bool
    upstream_error: bool
    simulated: bool
    detail: str = ""


# ------------------------------------------------------------------ channel
class _Channel:
    """Producer thread → event loop. Bounded (backpressure on the upstream),
    never blocks the event loop, never holds a thread-pool worker."""

    def __init__(self, loop: asyncio.AbstractEventLoop, capacity: int, idle_seconds: float):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self.slots = threading.Semaphore(capacity)
        self.gone = threading.Event()
        self.idle_seconds = idle_seconds

    def _enqueue(self, item) -> bool:
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)
            return True
        except RuntimeError:          # loop closed: the client is certainly gone
            self.gone.set()
            return False

    def put(self, item: bytes) -> bool:
        deadline = time.monotonic() + self.idle_seconds
        while not self.gone.is_set():
            if self.slots.acquire(timeout=0.1):
                return self._enqueue(item)
            if time.monotonic() > deadline:   # the client reads nothing: treat as gone
                self.gone.set()
        return False

    def finish(self) -> None:
        """End of stream; bypasses the capacity so the consumer always wakes up."""
        self._enqueue(None)

    async def get(self):
        item = await self.queue.get()
        if item is not None:
            self.slots.release()
        return item


class SessionStream:
    """One streamed session call: owns the upstream stream and the producer thread.

    ``settle(outcome) -> (event_name, payload)`` is the gateway's settlement
    function (one short transaction) that returns the terminal gateway event.
    """

    def __init__(self, upstream: UpstreamStream, meter: StreamMeter, settle: Callable[[StreamOutcome], tuple[str, dict]],
                 *, max_seconds: float, idle_seconds: float, headers: dict[str, str],
                 upstream_idle_seconds: float = 60.0):
        self.upstream = upstream
        self.meter = meter
        self._settle = settle
        self.max_seconds = max_seconds
        self.idle_seconds = idle_seconds
        self.headers = headers
        self.channel: _Channel | None = None
        # Silence limit for the upstream (no line at all, comments included).
        self.upstream_idle_seconds = upstream_idle_seconds
        self._abort_reason: str | None = None
        self._last_activity = time.monotonic()
        self.dropped_events = 0
        self.finished = threading.Event()   # set after settlement (tests / diagnostics)
        self.outcome: StreamOutcome | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.channel = _Channel(loop, CHANNEL_CAPACITY, self.idle_seconds)
        self._thread = threading.Thread(target=self._run, name="neuxmind-stream", daemon=True)
        self._thread.start()

    def client_gone(self) -> None:
        """Called by the ASGI response when it ends for any reason. Non-blocking."""
        if self.channel is not None:
            self.channel.gone.set()

    def _emit(self, chunk: bytes) -> None:
        if not self.channel.gone.is_set():
            self.channel.put(chunk)

    # -- producer ------------------------------------------------------------------
    def _guarded(self, chunks: Iterable[bytes], deadline: float) -> Iterator[bytes]:
        """Line-level checks (review Q2-02): every raw chunk — keep-alive comments
        and blank lines included — counts as activity, but none extends the
        absolute deadline."""
        for chunk in chunks:
            if self._abort_reason is not None:
                raise _Aborted(self._abort_reason)
            self._last_activity = time.monotonic()
            if self._last_activity > deadline:
                raise _Aborted("deadline")
            yield chunk
        if self._abort_reason is not None:
            raise _Aborted(self._abort_reason)

    def _watchdog(self, deadline: float, done: threading.Event) -> None:
        """Wall clock, independent of what (if anything) the upstream sends: at the
        deadline, or after ``upstream_idle_seconds`` of total silence, abort the
        upstream connection so a blocked read returns."""
        while not done.wait(0.05):
            now = time.monotonic()
            reason = "deadline" if now > deadline else (
                "upstream_idle" if now - self._last_activity > self.upstream_idle_seconds else None)
            if reason is not None:
                self._abort_reason = reason
                try:
                    self.upstream.abort()
                except Exception:  # pragma: no cover - best effort
                    log.exception("upstream abort failed")
                return

    def _run(self) -> None:
        meter = self.meter
        deadline = time.monotonic() + self.max_seconds
        self._last_activity = time.monotonic()
        done = threading.Event()
        threading.Thread(target=self._watchdog, args=(deadline, done), name="neuxmind-stream-watchdog",
                         daemon=True).start()
        termination, detail = "usage_missing", ""
        # Round 03 (§5.3): the upstream's terminal event (responses
        # ``response.completed`` …, chat ``[DONE]``) is forwarded only *after*
        # settlement. A tool-loop client (Codex app-server) sends its next call
        # as soon as it sees that event; forwarding it first left a window in
        # which the next call hit 409 ``session_busy`` (ADR-016 serialisation).
        terminal_chunk: bytes | None = None
        try:
            for ev in parse_sse(self._guarded(self.upstream.lines(), deadline)):
                out = outbound_event(ev)
                if out is None:
                    # Reserved name: neither forwarded nor metered (a forged
                    # "usage" inside it must not count either).
                    self.dropped_events += 1
                    continue
                if meter.feed(ev) == "truncate":
                    termination = "output_limit"
                    break
                chunk = out.encode()
                meter.forwarded_bytes += len(chunk)
                if meter.done:
                    terminal_chunk = chunk
                    termination = "completed"
                    break
                self._emit(chunk)
            else:
                termination = "completed"   # end of stream; usage decides below
        except _Aborted as exc:
            termination, detail = _ABORT_TERMINATION[exc.reason]
        except UpstreamError as exc:
            if self._abort_reason is not None:
                termination, detail = _ABORT_TERMINATION[self._abort_reason]
            else:
                termination, detail = "interrupted", exc.detail[:500]
        except Exception as exc:  # pragma: no cover - defensive: never leave a call unsettled
            log.exception("stream producer failed")
            termination, detail = "internal", f"{type(exc).__name__}: {exc}"[:500]
        finally:
            done.set()
            self.upstream.close()
        if self.dropped_events:
            detail = (detail + f"; dropped {self.dropped_events} upstream event(s) using reserved names").strip("; ")
        if termination == "completed" and meter.usage is None:
            termination = "usage_missing"
        self.outcome = StreamOutcome(
            termination=termination, usage=meter.usage, upstream_ref=meter.upstream_id,
            lower_bound=meter.lower_bound, events=meter.events, forwarded_bytes=meter.forwarded_bytes,
            client_disconnected=self.channel.gone.is_set(), upstream_error=meter.upstream_error,
            simulated=bool(getattr(self.upstream, "simulated", False)), detail=detail)
        try:
            name, payload = self._settle(self.outcome)
        except Exception as exc:  # pragma: no cover - the sweeper will pick the pending call up
            log.exception("stream settlement failed")
            name, payload = "error", error_payload("internal_error", "网关计量失败，本次调用将由清理任务转人工核对", None)
        if terminal_chunk is not None:
            self._emit(terminal_chunk)
        self._emit(encode_event(name, payload))
        self.channel.finish()
        self.finished.set()


class _Aborted(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_ABORT_TERMINATION = {
    "deadline": ("deadline", "stream exceeded the maximum duration (wall clock)"),
    "upstream_idle": ("interrupted", "upstream sent nothing (not even keep-alives) for too long"),
}


def error_payload(code: str, message: str, metering: dict | None) -> dict:
    """In-stream error: readable by plain SSE clients (``code``), by the OpenAI
    SDKs (``error.code`` → APIError) and by the NeuxMind SDK (``neuxmind``)."""
    return {"type": "error", "code": code, "message": message,
            "error": {"type": "neuxmind_gateway_error", "code": code, "message": message},
            **({"neuxmind": metering} if metering is not None else {})}


# ------------------------------------------------------------------ ASGI
class MeteredSSEResponse(Response):
    """ASGI response for a :class:`SessionStream`.

    Not Starlette's StreamingResponse: we always watch ``http.disconnect`` (on
    every ASGI spec version) and *always* tell the producer when the response
    ends, whatever the reason (finished, disconnect, cancellation, send error).
    (Subclasses ``Response`` only so FastAPI returns it as is.)
    """

    media_type = "text/event-stream"

    def __init__(self, stream: SessionStream):
        self.stream = stream
        self.status_code = 200
        self.background = None
        self.body = b""
        self.init_headers(stream.headers)

    async def __call__(self, scope, receive, send) -> None:
        stream = self.stream
        headers = [(b"content-type", b"text/event-stream; charset=utf-8"), (b"cache-control", b"no-cache"),
                   (b"x-accel-buffering", b"no")]
        headers += [(k.lower().encode(), v.encode()) for k, v in stream.headers.items()]
        try:
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            async with anyio.create_task_group() as tg:
                async def pump():
                    while True:
                        chunk = await stream.channel.get()
                        if chunk is None:
                            break
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    tg.cancel_scope.cancel()

                async def watch():
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            tg.cancel_scope.cancel()
                            return

                tg.start_soon(pump)
                tg.start_soon(watch)
        except OSError:
            pass   # the client went away while we were writing
        finally:
            stream.client_gone()
