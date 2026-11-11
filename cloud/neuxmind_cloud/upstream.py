"""Upstream inference providers.

Each provider returns an :class:`UpstreamResult` carrying the *provider's own*
usage numbers — those are the metering evidence for settlement. Errors are
split into two classes, because billing depends on the difference:

* :class:`UpstreamRejected` — the provider definitely did not perform billable
  work: the request never left (DNS failure, connection refused, TLS
  certificate rejected, missing operator credential) or the provider answered
  a 4xx other than 408 (bad request, auth, 429 rate limit). The reservation is
  released.
* :class:`UpstreamUnknown` — we cannot tell whether work happened: 5xx (often
  a load balancer / gateway timeout in front of a model that may have finished
  and billed), 408, timeouts, connections dropped mid-request,
  malformed/missing usage. The reservation is kept and flagged
  ``reconciliation_required``.

Every error carries a short **public** message (what users see in API errors,
``/usage`` and reservation notes) and a **detail** string for the audit log
and server log only — operator env-var names, hostnames and TLS messages never
reach users (review finding C-06).
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

# One SSE line from a provider may not exceed this (a single delta is tiny;
# a line this long means a broken or hostile upstream).
MAX_SSE_LINE_BYTES = 1_000_000


class UpstreamError(Exception):
    def __init__(self, public: str, detail: str | None = None):
        super().__init__(public)
        self.public = public
        self.detail = detail or public


class UpstreamRejected(UpstreamError):
    """No billable work was done."""


class UpstreamUnknown(UpstreamError):
    """Outcome unknown; never guess the cost."""


@dataclass
class UpstreamResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    upstream_ref: str
    raw: dict = field(default_factory=dict)
    simulated: bool = False


def _usage_int(usage: dict, *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    raise UpstreamUnknown("上游未返回可用的 usage 计量")


def classify_http_status(code: int) -> UpstreamError:
    """4xx except 408 → Rejected (the provider refused before doing billable work);
    408 and 5xx → Unknown (a proxy/gateway error may hide a finished, billed completion)."""
    if 400 <= code < 500 and code != 408:
        return UpstreamRejected("上游拒绝了请求，未扣费", f"HTTP {code}")
    return UpstreamUnknown("上游返回错误，结果未知，预留保留待核对", f"HTTP {code}")


def classify_connection_error(reason) -> UpstreamError:
    """Errors raised before the request could reach the provider are Rejected."""
    if isinstance(reason, socket.gaierror):
        return UpstreamRejected("上游地址无法解析，请求未发出", f"DNS: {reason}")
    if isinstance(reason, ConnectionRefusedError):
        return UpstreamRejected("上游拒绝连接，请求未发出", f"refused: {reason}")
    if isinstance(reason, ssl.SSLCertVerificationError):
        return UpstreamRejected("上游 TLS 证书校验失败，请求未发出", f"TLS: {reason}")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return UpstreamUnknown("上游超时，结果未知", f"timeout: {reason}")
    return UpstreamUnknown("上游连接中断，结果未知", f"connection: {type(reason).__name__}: {reason}")


class UpstreamStream:
    """Raw SSE lines of one streamed provider response (§4.3).

    ``lines()`` yields ``bytes`` lines (with their line ending) and raises
    :class:`UpstreamUnknown` if the transport breaks mid-stream (timeout,
    reset, truncated chunked body, over-long line). ``close()`` is idempotent
    and must always be called; it drops the connection, which tells a
    well-behaved provider to stop generating.
    """

    simulated = False

    def lines(self) -> Iterator[bytes]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        pass

    def abort(self) -> None:
        """Called from another thread (the stream watchdog): make a blocked
        ``lines()`` return or raise promptly. Default: ``close()``."""
        self.close()


class _HttpUpstreamStream(UpstreamStream):
    def __init__(self, response):
        self._response = response
        self._closed = False

    def lines(self) -> Iterator[bytes]:
        # Not ``readline()``: http.client's buffered readline goes through
        # ``peek()``, which swallows IncompleteRead, so a chunked body cut
        # mid-stream would look like a clean end. ``read1()`` raises it.
        buffer = b""
        while True:
            try:
                data = self._response.read1(65536)
            except (TimeoutError, socket.timeout) as exc:
                raise UpstreamUnknown("上游流式响应超时，结果未知", f"stream timeout: {exc}") from None
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise UpstreamUnknown("上游流式连接中断，结果未知",
                                      f"stream broken: {type(exc).__name__}: {exc}") from None
            if not data:
                remaining = getattr(self._response, "length", None)
                if remaining:   # Content-Length body ended early
                    raise UpstreamUnknown("上游流式连接中断，结果未知", f"stream ended {remaining} bytes early")
                if buffer:
                    yield buffer
                return
            buffer += data
            while True:
                cut = buffer.find(b"\n")
                if cut < 0:
                    break
                line, buffer = buffer[:cut + 1], buffer[cut + 1:]
                yield line
            if len(buffer) > MAX_SSE_LINE_BYTES:
                raise UpstreamUnknown("上游流式响应格式无效", "stream line too long")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._response.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def abort(self) -> None:
        # Shut the socket down (not just close the file object): a recv()
        # blocked in another thread returns immediately.
        try:
            sock = self._response.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # pragma: no cover - best effort (already closed etc.)
            pass


class OpenAICompatibleProvider:
    """Calls an OpenAI-compatible ``/chat/completions`` or ``/responses`` endpoint.

    Credentials come only from the operator's environment (``credential_ref``
    names an env var); they are never stored in the database or returned to users.
    """

    simulated = False

    def __init__(self, base_url: str, model: str, api: str = "chat_completions",
                 credential_ref: str | None = None, timeout: float = 60, output_param: str = "max_tokens"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api = api
        self.credential_ref = credential_ref
        self.timeout = timeout
        # Name(s) of the chat output-limit field this upstream honours:
        # "max_tokens" | "max_completion_tokens" | "both".
        self.output_param = output_param

    def _open(self, path: str, body: dict, accept: str):
        headers = {"Content-Type": "application/json", "Accept": accept}
        if self.credential_ref:
            secret = os.environ.get(self.credential_ref)
            if not secret:
                raise UpstreamRejected("上游服务暂不可用", f"operator credential env var {self.credential_ref} is not set")
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise classify_http_status(exc.code) from None
        except urllib.error.URLError as exc:
            raise classify_connection_error(exc.reason) from None
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamUnknown("上游超时，结果未知", f"timeout: {exc}") from None
        except OSError as exc:
            raise classify_connection_error(exc) from None

    def _post(self, path: str, body: dict) -> dict:
        response = self._open(path, body, "application/json")
        try:
            with response:
                raw = response.read(8_000_000)
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamUnknown("上游超时，结果未知", f"timeout: {exc}") from None
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamUnknown("上游连接中断，结果未知", f"read: {type(exc).__name__}: {exc}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise UpstreamUnknown("上游返回了无法解析的响应") from None
        if not isinstance(data, dict):
            raise UpstreamUnknown("上游响应格式无效")
        return data

    def complete(self, messages: list[dict], max_output_tokens: int) -> UpstreamResult:
        if self.api == "responses":
            data = self._post("/responses", {"model": self.model, "input": messages, "max_output_tokens": max_output_tokens})
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            prompt = _usage_int(usage, "input_tokens", "prompt_tokens")
            completion = _usage_int(usage, "output_tokens", "completion_tokens")
            text = data.get("output_text") or ""
            if not text:
                for item in data.get("output") or []:
                    for part in (item.get("content") or []) if isinstance(item, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            text += part["text"]
        else:
            data = self._post("/chat/completions", {"model": self.model, "messages": messages,
                                                    **chat_output_limit(self.output_param, max_output_tokens)})
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            prompt = _usage_int(usage, "prompt_tokens", "input_tokens")
            completion = _usage_int(usage, "completion_tokens", "output_tokens")
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
        total = usage.get("total_tokens")
        if not isinstance(total, int) or isinstance(total, bool) or total < prompt + completion:
            total = prompt + completion
        ref = data.get("id") if isinstance(data.get("id"), str) else ""
        if not ref:
            raise UpstreamUnknown("上游响应缺少 id，无法作为计量证据")
        return UpstreamResult(text=text, prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                              upstream_ref=f"openai_compatible:{ref}", raw=data)

    def passthrough(self, path: str, body: dict) -> tuple[dict, UpstreamResult]:
        """Forward a session call (``/chat/completions`` or ``/responses``) and extract usage.

        ``body`` has already been reduced to the allow-listed parameters by the
        gateway, with the output limit set by the server (see gateway.py)."""
        body = dict(body, model=self.model)
        data = self._post(path, body)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt = _usage_int(usage, "prompt_tokens", "input_tokens")
        completion = _usage_int(usage, "completion_tokens", "output_tokens")
        ref = data.get("id") if isinstance(data.get("id"), str) else ""
        if not ref:
            raise UpstreamUnknown("上游响应缺少 id，无法作为计量证据")
        return data, UpstreamResult(text="", prompt_tokens=prompt, completion_tokens=completion,
                                    total_tokens=prompt + completion, upstream_ref=f"openai_compatible:{ref}", raw=data)

    def open_stream(self, path: str, body: dict) -> UpstreamStream:
        """Start a streamed session call (``body`` already has ``stream: true``).

        Errors before the first byte are classified exactly like a
        non-streamed call (so a 4xx is released, a 5xx is unknown); errors
        after that surface from ``lines()`` as :class:`UpstreamUnknown`."""
        response = self._open(path, dict(body, model=self.model), "text/event-stream")
        return _HttpUpstreamStream(response)


class EchoProvider:
    """**TEST DOUBLE — NOT A MODEL.** Echoes the input back and reports a fake
    word-count "usage". Only available when ``NEUXMIND_ENV`` is ``dev``/``test``;
    every result is marked ``simulated=True``. ``behavior`` lets tests force
    failure modes: ``timeout`` / ``error`` / ``overuse`` / ``no_usage``.
    """

    simulated = True

    def __init__(self, behavior: str = "ok", stream_delay: float = 0.0, tool_name: str | None = None,
                 tool_arguments: dict | None = None):
        self.behavior = behavior
        self.stream_delay = stream_delay
        self.tool_name = tool_name
        self.tool_arguments = tool_arguments
        self.calls = 0

    def _result(self, messages: list[dict], max_output_tokens: int) -> UpstreamResult:
        self.calls += 1
        if self.behavior == "timeout":
            raise UpstreamUnknown("[echo] 模拟上游超时")
        if self.behavior == "error":
            raise UpstreamRejected("[echo] 模拟上游错误响应")
        if self.behavior == "no_usage":
            raise UpstreamUnknown("[echo] 模拟上游缺少 usage")
        prompt_text = " ".join(str(m.get("content", "")) for m in messages)
        prompt = max(1, len(prompt_text.split()))
        words = ("echo: " + prompt_text).split()
        completion = min(len(words), max_output_tokens)
        if self.behavior == "overuse":
            completion = max_output_tokens * 10 + 10_000_000
        text = " ".join(words[:completion])
        return UpstreamResult(text=text, prompt_tokens=prompt, completion_tokens=completion,
                              total_tokens=prompt + completion, upstream_ref=f"echo:{self.calls}",
                              raw={"simulated": True}, simulated=True)

    def complete(self, messages: list[dict], max_output_tokens: int) -> UpstreamResult:
        return self._result(messages, max_output_tokens)

    def _prompt_and_limit(self, path: str, body: dict) -> tuple[list[dict], int]:
        if path == "/responses":
            raw_input = body.get("input")
            messages = raw_input if isinstance(raw_input, list) else [{"role": "user", "content": str(raw_input or "")}]
            return messages, int(body.get("max_output_tokens") or 256)
        return body.get("messages") or [], int(body.get("max_tokens") or body.get("max_completion_tokens") or 256)

    def open_stream(self, path: str, body: dict) -> UpstreamStream:
        """**TEST DOUBLE** streaming: one word per delta event, in the chat
        (``data: {chunk}`` … usage chunk … ``data: [DONE]``) or responses
        (``response.created`` … ``response.output_text.delta`` …
        ``response.completed`` with usage) wire format. Behaviours:
        ``ok`` · ``overuse`` (ignores the output limit) · ``stream_cut``
        (connection drops before usage) · ``no_usage`` (ends normally without
        usage) · ``timeout`` (unknown before the first byte) · ``error``
        (rejected before the first byte) · ``usage_overrun`` (honours the
        limit in deltas but reports far more usage) · ``tools`` (responses only:
        a function-calling tool loop with complete output items, see
        :meth:`_responses_tool_loop`)."""
        self.calls += 1
        if self.behavior == "timeout":
            raise UpstreamUnknown("[echo] 模拟上游超时")
        if self.behavior == "error":
            raise UpstreamRejected("[echo] 模拟上游错误响应")
        if self.behavior == "tools" and path == "/responses":
            return _EchoStream(self._responses_tool_loop(body), None, float(self.stream_delay))
        messages, limit = self._prompt_and_limit(path, body)
        prompt_text = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        prompt = max(1, len(prompt_text.split()))
        words = ("echo: " + prompt_text).split()
        if self.behavior == "overuse":
            words = (words * (limit * 2 + 5))[: limit * 2 + 5]
        else:
            words = words[:limit]
        completion = len(words) * (1000 if self.behavior == "usage_overrun" else 1)
        usage_chat = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        ref = f"stream-{self.calls}"
        lines: list[bytes] = []
        if path == "/responses":
            lines += _sse({"type": "response.created", "simulated": True,
                           "response": {"id": ref, "status": "in_progress"}}, "response.created")
            for i, word in enumerate(words):
                lines += _sse({"type": "response.output_text.delta", "simulated": True, "output_index": 0,
                               "content_index": 0, "delta": (" " if i else "") + word}, "response.output_text.delta")
            usage = {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion}
            if self.behavior != "no_usage":
                lines += _sse({"type": "response.completed", "simulated": True,
                               "response": {"id": ref, "status": "completed", "usage": usage}}, "response.completed")
            else:
                lines += _sse({"type": "response.completed", "simulated": True,
                               "response": {"id": ref, "status": "completed"}}, "response.completed")
        else:
            base = {"id": ref, "object": "simulated.echo.chunk", "simulated": True}
            lines += _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            for i, word in enumerate(words):
                lines += _sse({**base, "choices": [{"index": 0, "delta": {"content": (" " if i else "") + word}}]})
            lines += _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if self.behavior != "no_usage":
                lines += _sse({**base, "choices": [], "usage": usage_chat})
            lines += _sse("[DONE]")
        cut = None
        if self.behavior == "stream_cut":
            cut = max(3, len(lines) // 2)
        return _EchoStream(lines, cut, float(self.stream_delay))

    def _responses_tool_loop(self, body: dict) -> list[bytes]:
        """**TEST DOUBLE** for a tool-calling agent runtime (round 03, §5.3):
        a complete Responses event stream with output items, like OpenAI's.

        * no ``function_call_output`` in the input yet and at least one
          function tool offered → one ``function_call`` item (``tool_name`` from
          the model card, default the first top-level function tool;
          ``tool_arguments`` default ``{}``);
        * otherwise → one assistant ``message`` echoing the last input text.

        Usage: prompt = words of the input, completion = words of the output
        (≥ the number of forwarded deltas). Every event carries ``simulated``."""
        items = body.get("input") if isinstance(body.get("input"), list) else [
            {"role": "user", "content": str(body.get("input") or "")}]
        limit = int(body.get("max_output_tokens") or 256)
        texts = []
        for item in items:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, str):
                texts.append(content)
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            if isinstance(item, dict) and isinstance(item.get("output"), str):
                texts.append(item["output"])
        prompt = max(1, len(" ".join(texts).split()))
        functions = [t["name"] for t in body.get("tools") or [] if isinstance(t, dict) and t.get("type") == "function"]
        answered = any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in items)
        ref = f"resp_echo_{self.calls}"
        if functions and not answered:
            name = self.tool_name if self.tool_name else functions[0]
            arguments = json.dumps(self.tool_arguments or {}, ensure_ascii=False)
            item = {"type": "function_call", "id": f"fc_echo_{self.calls}", "call_id": f"call_echo_{self.calls}",
                    "name": name, "arguments": arguments, "status": "completed"}
            deltas = [{"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": 0,
                       "delta": arguments}]
            done = [{"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": 0,
                     "arguments": arguments}]
            added = {**item, "arguments": "", "status": "in_progress"}
            completion = max(1, len(arguments.split()))
        else:
            words = ("echo: " + (texts[-1] if texts else "")).split()[:max(1, limit)]
            text = " ".join(words)
            item = {"type": "message", "id": f"msg_echo_{self.calls}", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            deltas = [{"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                       "content_index": 0, "delta": (" " if i else "") + w} for i, w in enumerate(words)]
            done = [{"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                     "content_index": 0, "text": text}]
            added = {**item, "status": "in_progress", "content": []}
            completion = len(words)
        usage = {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
        response = {"id": ref, "object": "response", "status": "completed", "model": "echo", "output": [item],
                    "usage": usage}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.added", "output_index": 0, "item": added},
                  *deltas, *done,
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": response}]
        lines: list[bytes] = []
        for event in events:
            lines += _sse({**event, "simulated": True}, event["type"])
        return lines

    def passthrough(self, path: str, body: dict) -> tuple[dict, UpstreamResult]:
        if path == "/responses":
            raw_input = body.get("input")
            messages = raw_input if isinstance(raw_input, list) else [{"role": "user", "content": str(raw_input or "")}]
            limit = body.get("max_output_tokens") or 256
        else:
            messages = body.get("messages") or []
            limit = body.get("max_tokens") or body.get("max_completion_tokens") or 256
        result = self._result(messages, int(limit))
        usage = {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                 "total_tokens": result.total_tokens}
        if path == "/responses":
            # Responses-shaped (the openai library's ``response.output_text`` reads ``output``).
            data = {"id": result.upstream_ref, "object": "response", "status": "completed", "simulated": True,
                    "output_text": result.text,
                    "output": [{"type": "message", "id": f"msg_{result.upstream_ref}", "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": result.text, "annotations": []}]}],
                    "usage": {**usage, "input_tokens": result.prompt_tokens,
                              "output_tokens": result.completion_tokens}}
        else:
            data = {"id": result.upstream_ref, "object": "simulated.echo", "simulated": True,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text}}],
                    "usage": usage}
        return data, result


class _EchoStream(UpstreamStream):
    """**TEST DOUBLE.** Pre-built SSE lines in the provider's wire format; every
    event carries ``"simulated": true``."""

    simulated = True

    def __init__(self, lines: list[bytes], cut_after: int | None, delay: float):
        self._lines, self._cut_after, self._delay = lines, cut_after, delay
        self.closed = False

    def lines(self) -> Iterator[bytes]:
        for i, line in enumerate(self._lines):
            if self.closed:
                return
            if self._cut_after is not None and i >= self._cut_after:
                raise UpstreamUnknown("[echo] 模拟上游流式连接中断", "echo: simulated stream cut")
            if self._delay and line == b"\n":
                time.sleep(self._delay)
            yield line

    def close(self) -> None:
        self.closed = True


def _sse(data, event: str | None = None) -> list[bytes]:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return ([f"event: {event}\n".encode()] if event else []) + [f"data: {text}\n".encode(), b"\n"]


def chat_output_limit(output_param: str, tokens: int) -> dict:
    if output_param == "both":
        return {"max_tokens": tokens, "max_completion_tokens": tokens}
    return {output_param if output_param in ("max_tokens", "max_completion_tokens") else "max_tokens": tokens}


def build_provider(upstream: dict, *, test_env: bool, timeout: float):
    """Instantiate the provider for a model card's upstream binding."""
    if upstream["provider"] == "echo":
        if not test_env:
            raise UpstreamRejected("EchoProvider 仅在 dev/test 环境可用")
        return EchoProvider(upstream.get("behavior", "ok"), float(upstream.get("stream_delay", 0) or 0),
                            upstream.get("tool_name"), upstream.get("tool_arguments"))
    return OpenAICompatibleProvider(upstream["base_url"], upstream["model"], upstream.get("api", "chat_completions"),
                                    upstream.get("credential_ref"), timeout, upstream.get("output_param", "max_tokens"))
