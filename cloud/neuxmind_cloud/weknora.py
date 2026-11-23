"""The knowledge service that reads documents (issue #34): a WeKnora instance reached over its REST API.

The cloud keeps the original file (issue #33) and hands a copy of each
document to WeKnora, which parses it, cuts it into chunks, embeds them and
answers questions about it with the model configured *in WeKnora*. Which
model that is, and where it runs, is decided in WeKnora, not here: a vendor's
document reaches that model service (see cloud/README.md, 资料解析).

What this module does with what WeKnora sends back: nothing is trusted. Every
response is read with a size cap, no redirect is followed, no proxy is used,
JSON is checked field by field, and an answer of the model is only ever text
that :mod:`material_parse` validates and a person confirms.

Endpoints used (WeKnora 0.9, ``/api/v1``, key in ``X-API-Key``):

* ``POST /knowledge-bases/{kb}/knowledge/file`` — multipart upload; 409 when the
  same file is already in the knowledge base (its record comes back in ``data``).
* ``GET /knowledge/{id}`` — ``parse_status``: pending, processing, finalizing,
  completed, failed, cancelled.
* ``POST /sessions`` and ``POST /knowledge-chat/{session}`` — a question limited to
  one document (``knowledge_ids``), answered as a server-sent event stream.
* ``DELETE /knowledge/{id}``, ``DELETE /sessions/{id}``.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

API = "/api/v1"
MAX_JSON = 2 * 1024 * 1024
MAX_STREAM = 8 * 1024 * 1024
MAX_ANSWER = 64 * 1024
READY = ("completed", "finalizing")
PENDING = ("pending", "processing")
FAILED = ("failed", "cancelled")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")      # never "." or "..": it goes into a URL path


class WeKnoraError(Exception):
    """``retry``: worth trying again later (unreachable, busy, 5xx); otherwise WeKnora said no.
    ``gone``: the document's copy is not in the knowledge service (any more)."""

    def __init__(self, message: str, *, retry: bool, status: int | None = None, gone: bool = False):
        super().__init__(message)
        self.message, self.retry, self.status, self.gone = message, retry, status, gone


class _Watch:
    """A wall-clock limit on one whole HTTP exchange: connecting, the status line and headers, the body,
    and the body of an error. A socket timeout only bounds each single read, so a service that trickles a
    byte now and then could otherwise hold a worker thread for good. When the time is up, every connection
    the exchange opened is shut down, which ends whatever read is waiting on it."""

    def __init__(self, seconds: float):
        self.connections: list = []
        self.fired = False
        self.lock = threading.Lock()
        self.timer = threading.Timer(seconds, self._cut)
        self.timer.daemon = True

    def track(self, sock) -> None:
        """Keep the socket itself: urllib lets go of the connection's reference once the headers are in,
        while the response goes on reading from the same socket."""
        with self.lock:
            self.connections.append(sock)
            late = self.fired
        if late:
            self._shutdown(sock)

    @staticmethod
    def _shutdown(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass

    def _cut(self) -> None:
        with self.lock:
            self.fired = True
            sockets = list(self.connections)
        for sock in sockets:
            self._shutdown(sock)

    def __enter__(self) -> "_Watch":
        self.timer.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.timer.cancel()


_READ_ERRORS = (OSError, ValueError, http.client.HTTPException)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener(watch: _Watch):
    """No redirect, no proxy; every connection is handed to ``watch``."""
    class Plain(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            watch.track(self.sock)

    class Secure(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            watch.track(self.sock)

    class PlainHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(Plain, req)

    class SecureHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(Secure, req, context=self._context)

    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}), PlainHandler, SecureHandler)


def _plain(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(ch if ch.isprintable() else " " for ch in text)[:limit]


def _id(value) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool) and _ID.fullmatch(str(value)):
        return str(value)
    raise WeKnoraError("解析服务返回的编号格式不对", retry=False)


@dataclass(frozen=True)
class Knowledge:
    id: str
    parse_status: str
    error: str = ""
    duplicate: bool = False


class _Body:
    """A multipart body read piece by piece: the head, the file (from its descriptor), the tail."""

    def __init__(self, head: bytes, fd: int, size: int, tail: bytes):
        self.parts, self.fd, self.size, self.offset = [head, None, tail], fd, size, 0
        self.length = len(head) + size + len(tail)

    def read(self, amount: int = 65536) -> bytes:
        while self.parts:
            part = self.parts[0]
            if part is None:
                if self.offset < self.size:
                    block = os.pread(self.fd, min(amount, self.size - self.offset), self.offset)
                    if not block:
                        raise OSError("file shorter than its record")
                    self.offset += len(block)
                    return block
                self.parts.pop(0)
                continue
            self.parts.pop(0)
            if part:
                return part
        return b""


class WeKnora:
    def __init__(self, base_url: str, api_key: str, kb_id: str, *, tenant_id: str = "", timeout: float = 30.0,
                 upload_timeout: float = 600.0, answer_timeout: float = 180.0):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query:
            raise ValueError("NEUXMIND_WEKNORA_URL 须为 http(s)://主机[:端口][/路径]")
        self.base = base_url.rstrip("/")
        self.api_key, self.kb_id, self.tenant_id = api_key, _id(kb_id), tenant_id
        self.timeout, self.upload_timeout, self.answer_timeout = timeout, upload_timeout, answer_timeout

    # ------------------------------------------------------------ transport
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"X-API-Key": self.api_key, "Accept": "application/json", "User-Agent": "neuxmind-cloud"}
        if self.tenant_id:
            headers["X-Tenant-ID"] = self.tenant_id
        headers.update(extra or {})
        return headers

    def _send(self, method: str, path: str, *, body=None, headers=None, timeout=None, limit=MAX_JSON):
        """``(status, JSON object)`` of one request; the whole exchange within twice ``timeout``."""
        request = urllib.request.Request(self.base + API + path, data=body, method=method,
                                         headers=self._headers(headers))
        seconds = timeout or self.timeout
        with _Watch(seconds * 2) as watch:
            try:
                with _opener(watch).open(request, timeout=seconds) as response:
                    return response.status, self._json(response.read(limit + 1), limit)
            except urllib.error.HTTPError as exc:
                return exc.code, self._error_body(exc, limit)
            except (urllib.error.URLError, *_READ_ERRORS) as exc:
                if watch.fired:
                    raise WeKnoraError("解析服务没有在时限内答完", retry=True) from None
                raise WeKnoraError(f"连不上解析服务（{type(exc).__name__}）", retry=True) from None

    def _error_body(self, exc, limit: int) -> dict:
        """The JSON body of an error answer, or ``{}`` when it cannot be read (it is only used for the message)."""
        try:
            return self._json(exc.read(limit + 1), limit, strict=False)
        except (*_READ_ERRORS, WeKnoraError):
            return {}
        finally:
            exc.close()

    @staticmethod
    def _json(raw: bytes, limit: int, strict: bool = True):
        if len(raw) > limit:
            raise WeKnoraError("解析服务的答复大得不正常", retry=False)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError, RecursionError):
            if strict:
                raise WeKnoraError("解析服务的答复不是 JSON", retry=True) from None
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _refusal(status: int, data: dict, doing: str) -> WeKnoraError:
        said = _plain(data.get("message") or data.get("error") or (data.get("data") if isinstance(data.get("data"), str) else ""))
        retry = status >= 500 or status in (408, 425, 429)
        if status in (401, 403):
            return WeKnoraError(f"解析服务拒绝了云平台的密钥（HTTP {status}），请检查 NEUXMIND_WEKNORA_API_KEY", retry=False,
                                status=status)
        return WeKnoraError(f"解析服务{doing}失败（HTTP {status}）" + (f"：{said}" if said else ""), retry=retry,
                            status=status)

    @staticmethod
    def _knowledge(data, duplicate: bool = False) -> Knowledge:
        record = data.get("data") if isinstance(data, dict) else None
        if not isinstance(record, dict):
            raise WeKnoraError("解析服务的答复里没有资料记录", retry=True)
        status = record.get("parse_status") if isinstance(record.get("parse_status"), str) else ""
        return Knowledge(_id(record.get("id")), status, _plain(record.get("error_message")), duplicate)

    # ------------------------------------------------------------ documents
    def upload(self, fd: int, size: int, filename: str, metadata: dict) -> Knowledge:
        """Send the document; a file the knowledge base already holds comes back with ``duplicate``."""
        boundary = "neuxmind-" + uuid.uuid4().hex
        def field(name: str, value: str) -> bytes:
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-120:] or "document"
        head = (field("fileName", _plain(filename, 200).replace('"', "_")) + field("channel", "api")
                + field("metadata", json.dumps(metadata, ensure_ascii=True))
                + f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n".encode())
        tail = f"\r\n--{boundary}--\r\n".encode()
        body = _Body(head, fd, size, tail)
        status, data = self._send("POST", f"/knowledge-bases/{urllib.parse.quote(self.kb_id)}/knowledge/file",
                                  body=body, timeout=self.upload_timeout,
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                           "Content-Length": str(body.length)})
        if status == 409:
            return self._knowledge(data, duplicate=True)
        if not 200 <= status < 300:
            raise self._refusal(status, data, "接收文档")
        return self._knowledge(data)

    def knowledge(self, knowledge_id: str) -> Knowledge:
        status, data = self._send("GET", f"/knowledge/{urllib.parse.quote(_id(knowledge_id))}")
        if not 200 <= status < 300:
            refusal = self._refusal(status, data, "查询解析进度")
            refusal.gone = status == 404
            raise refusal
        return self._knowledge(data)

    def delete(self, knowledge_id: str) -> None:
        status, data = self._send("DELETE", f"/knowledge/{urllib.parse.quote(_id(knowledge_id))}")
        if status != 404 and not 200 <= status < 300:
            raise self._refusal(status, data, "删除文档")

    # ------------------------------------------------------------ questions
    def ask(self, knowledge_id: str, question: str) -> str:
        """The model's answer to ``question``, limited to the one document ``knowledge_id``. A 404 of the
        question itself means the document's copy is gone (``gone``); any other refusal does not."""
        status, data = self._send("POST", "/sessions", body=json.dumps(
            {"title": "neuxmind material parse", "description": ""}).encode(), headers={"Content-Type": "application/json"})
        if not 200 <= status < 300:
            refusal = self._refusal(status, data, "开启问答")
            refusal.retry = refusal.retry and status != 404       # no question interface there: a setting to fix
            raise refusal
        session = data.get("data") if isinstance(data.get("data"), dict) else {}
        session_id = _id(session.get("id"))
        try:
            return self._chat(session_id, knowledge_id, question)
        finally:
            try:
                self._send("DELETE", f"/sessions/{urllib.parse.quote(session_id)}")
            except WeKnoraError:
                pass

    def _chat(self, session_id: str, knowledge_id: str, question: str) -> str:
        payload = {"query": question, "knowledge_base_ids": [self.kb_id], "knowledge_ids": [_id(knowledge_id)],
                   "agent_enabled": False, "web_search_enabled": False, "disable_title": True, "channel": "api"}
        request = urllib.request.Request(
            f"{self.base}{API}/knowledge-chat/{urllib.parse.quote(session_id)}", method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=self._headers({"Content-Type": "application/json", "Accept": "text/event-stream"}))
        deadline = time.monotonic() + self.answer_timeout
        with _Watch(self.answer_timeout + 5) as watch:
            try:
                with _opener(watch).open(request, timeout=self.timeout) as response:
                    return self._read_answer(response, deadline)
            except urllib.error.HTTPError as exc:
                refusal = self._refusal(exc.code, self._error_body(exc, MAX_JSON), "回答问题")
                refusal.gone = exc.code == 404
                raise refusal from None
            except (urllib.error.URLError, *_READ_ERRORS) as exc:
                if watch.fired:
                    raise WeKnoraError("解析服务回答问题超时", retry=True) from None
                raise WeKnoraError(f"连不上解析服务（{type(exc).__name__}）", retry=True) from None

    def _read_answer(self, response, deadline: float) -> str:
        answer, data_lines, seen = [], [], 0
        while True:
            if time.monotonic() > deadline:
                raise WeKnoraError("解析服务回答问题超时", retry=True)
            try:
                line = response.readline(MAX_ANSWER + 1)
            except _READ_ERRORS:
                if time.monotonic() > deadline:
                    raise WeKnoraError("解析服务回答问题超时", retry=True) from None
                raise WeKnoraError("解析服务回答问题时连接中断", retry=True) from None
            seen += len(line)
            if seen > MAX_STREAM:
                raise WeKnoraError("解析服务的回答大得不正常", retry=False)
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("data:"):
                data_lines.append(text[5:].lstrip(" "))
                continue
            if text or not data_lines:
                continue
            event, data_lines = "\n".join(data_lines), []
            try:
                message = json.loads(event)
            except (ValueError, RecursionError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("response_type")
            if kind == "error":
                raise WeKnoraError("解析服务回答问题时出错：" + _plain(message.get("content")), retry=True)
            if kind == "answer" and isinstance(message.get("content"), str):
                answer.append(message["content"])
                if sum(map(len, answer)) > MAX_ANSWER:
                    raise WeKnoraError("模型的回答长得不正常", retry=False)
            if kind in ("answer", "complete") and message.get("done") is True:
                break
        return "".join(answer)


def from_settings(settings) -> WeKnora | None:
    """The configured service, or ``None`` when the cloud has none."""
    if not settings.weknora_url or not settings.weknora_api_key or not settings.weknora_kb_id:
        return None
    return WeKnora(settings.weknora_url, settings.weknora_api_key, settings.weknora_kb_id,
                   tenant_id=settings.weknora_tenant_id, answer_timeout=settings.weknora_answer_seconds)
