"""HTTP transport for the edge runtime (INTERFACES_V1 §1, v1 + v2).

Standard library only. Every connection is served by its own thread, so a
stop request is served even while an evaluation blocks another thread.
Stop endpoints stop the robot *before* parsing the request body.

Connection limits (review R-05)
-------------------------------
* At most ``max_connections`` (default 32) ordinary connections are served
  at once. Beyond that a connection gets an immediate ``503 busy`` -- unless
  it is a stop request (see below). No thread is created for rejected
  connections, so idle or slow clients cannot exhaust threads.
* Reading the request line, headers and body must finish within
  ``request_timeout`` seconds in total (default 10 s); each socket operation
  times out after 5 s. A half-sent request (slowloris) is dropped.
* **Stop guarantee path**: ``stop_reserved`` (default 4) extra slots are
  reserved for ``POST /v1/stop`` and ``POST /v2/stop``. When the ordinary
  slots are full, a new connection takes a reserved slot and must present
  its request line within ``stop_sniff_timeout`` (default 1 s); anything
  other than a stop path gets 503. If the reserved slots are full too, the
  oldest reserved connection that has not yet identified itself as a stop
  request is evicted (closed) to make room -- a real stop client sends its
  whole request at once and identifies itself within microseconds, so idle
  connections cannot starve it.
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from neuxmind_spec import capabilities as spec_caps

from .core import EdgeRuntime
from .errors import BadRequest, NotFound, RuntimeApiError

MIN_TOKEN_LENGTH = 24
MAX_BODY_BYTES = 2_000_000
STOP_PATHS = ("/v1/stop", "/v2/stop")
STOP_PLACEHOLDER = "stop"      # latch reason written by a stop before its body (reason) is read

MAX_CONNECTIONS = 32            # ordinary concurrent connections
STOP_RESERVED = 4               # extra slots usable only by stop requests
REQUEST_TIMEOUT_S = 10.0        # whole request (line + headers + body) must arrive within this
SOCKET_IO_TIMEOUT_S = 5.0       # any single socket read/write
STOP_SNIFF_TIMEOUT_S = 1.0      # a reserved-slot connection must send its request within this
LISTEN_BACKLOG = 128

_BUSY_BODY = json.dumps({"error": "运行时连接数已满，请稍后重试", "code": "busy"}, ensure_ascii=False).encode()
_BUSY_RESPONSE = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                  b"Connection: close\r\nContent-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\n\r\n"
                  + _BUSY_BODY)


def _reject_constant(name: str):
    raise ValueError(f"JSON 不允许 {name}")


class _ConnectionAbort(Exception):
    """The client was too slow or went away while sending the request."""


class _DeadlineReader:
    """File-like reader over a socket with an overall deadline.

    ``BaseHTTPRequestHandler`` reads the request line and headers through
    ``rfile.readline``; a plain socket timeout only bounds each recv, so a
    client dripping one byte every few seconds could hold a thread forever.
    This reader bounds the whole request.
    """

    def __init__(self, sock: socket.socket, timeout: float):
        self._sock = sock
        self._buf = bytearray()
        self._eof = False
        self.deadline: float | None = time.monotonic() + timeout

    def _fill(self) -> None:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline exceeded")
            self._sock.settimeout(min(SOCKET_IO_TIMEOUT_S, remaining))
        chunk = self._sock.recv(65536)
        if not chunk:
            self._eof = True
        self._buf += chunk

    def readline(self, limit: int = -1) -> bytes:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0 and (limit < 0 or idx < limit):
                end = idx + 1
                break
            if limit >= 0 and len(self._buf) >= limit:
                end = limit
                break
            if self._eof:
                end = len(self._buf)
                break
            self._fill()
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            raise ValueError("unbounded read not supported")
        while len(self._buf) < n and not self._eof:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def finish(self) -> None:
        """Request fully read: no more deadline; restore the per-operation timeout for writes."""
        self.deadline = None
        try:
            self._sock.settimeout(SOCKET_IO_TIMEOUT_S)
        except OSError:
            pass

    def close(self) -> None:
        pass

_CAP_INVOKE = re.compile(r"^/v2/capabilities/([^/]+)/invoke$")
_SKILL_RUN = re.compile(r"^/v2/skills/([^/]+)/run$")
_RUN = re.compile(r"^/v2/runs/([^/]+)$")
_RUN_CANCEL = re.compile(r"^/v2/runs/([^/]+)/cancel$")
_EVALUATION = re.compile(r"^/v1/evaluations/([^/]+)$")
_RUN_EVENTS = re.compile(r"^/v2/runs/([^/]+)/events$")
_MODEL = re.compile(r"^/v2/models/([^/]+)$")
_CONFORMANCE = re.compile(r"^/v2/conformance/([^/]+)$")


def check_token(token: str | None) -> str:
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"BRAIN_EDGE_TOKEN 至少需要 {MIN_TOKEN_LENGTH} 个字符")
    return token


def make_handler(runtime: EdgeRuntime, token: str):
    token = check_token(token)
    expected = ("Bearer " + token).encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "NeuxMindRuntime/0.1"
        timeout = SOCKET_IO_TIMEOUT_S

        def log_message(self, fmt, *args):   # keep stdout quiet; telemetry has events
            pass

        def setup(self):
            super().setup()
            slot = getattr(self.server, "slot_for", lambda _r: None)(self.request)
            self._slot = slot
            timeout = getattr(self.server, "request_timeout", REQUEST_TIMEOUT_S)
            if slot is not None and slot.stop_only:
                timeout = min(timeout, getattr(self.server, "stop_sniff_timeout", STOP_SNIFF_TIMEOUT_S))
            self.rfile = _DeadlineReader(self.connection, timeout)

        def _admit_path(self, path: str) -> bool:
            """Reserved (stop-only) slots serve nothing but stop requests."""
            slot = self._slot
            if slot is None or not slot.stop_only:
                return True
            if self.command == "POST" and path in STOP_PATHS:
                slot.identified()          # no longer evictable
                return True
            self.close_connection = True
            self._send({"error": "运行时连接数已满，请稍后重试", "code": "busy"}, 503)
            return False

        # ---------------------------------------------------------- io
        def _send(self, obj: dict, status: int = 200) -> None:
            data = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "").encode()
            if not secrets.compare_digest(given, expected):
                self._send({"error": "unauthorized", "code": "unauthorized"}, 401)
                return False
            return True

        def _raw_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise BadRequest("Content-Length 无效") from None
            if length < 0 or length > MAX_BODY_BYTES:
                raise BadRequest("请求大小无效")
            try:
                data = self.rfile.read(length) if length else b""
            except (TimeoutError, OSError):
                raise _ConnectionAbort() from None
            if len(data) != length:
                raise _ConnectionAbort()
            self.rfile.finish()
            return data

        def _json_body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._raw_body()
                raise BadRequest("需要 application/json")
            raw = self._raw_body()
            if not raw:
                return {}
            try:
                payload = json.loads(raw, parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError):
                raise BadRequest("请求不是合法 JSON（不允许 NaN/Infinity）") from None
            except RecursionError:      # Q6-08: absurd nesting is the client's error, not a 500
                raise BadRequest("请求 JSON 嵌套过深（不是合法请求）") from None   # code bad_request: no new code for the desktop
            if not isinstance(payload, dict):
                raise BadRequest("请求须为 JSON 对象")
            return payload

        def _dispatch(self, fn) -> None:
            try:
                self._send(fn())
            except _ConnectionAbort:
                self.close_connection = True
            except RuntimeApiError as exc:
                self._send(exc.to_dict(), exc.status)
            except Exception:
                runtime.events.emit("gateway.internal_error", path=self.path)
                self._send({"error": "运行时内部错误", "code": "internal_error"}, 500)

        # ---------------------------------------------------------- GET
        def do_GET(self):
            url = urlsplit(self.path)
            if not self._admit_path(url.path):
                return
            self.rfile.finish()            # GET has no body
            if not self._authorized():
                return
            path, query = url.path, parse_qs(url.query)
            self._dispatch(lambda: self._get(path, query))

        def _get(self, path: str, query: dict) -> dict:
            if path == "/v1/manifest":
                return runtime.manifest_v1()
            if path == "/v1/status":
                return runtime.status()
            if path == "/v2/manifest":
                return runtime.manifest
            if path == "/v2/capabilities/registry":
                return spec_caps.registry()
            if path == "/v2/state":
                ids = [i for i in ",".join(query.get("ids", [])).split(",") if i]
                return runtime.read_state(ids)
            if path == "/v2/skills":
                return {"skills": runtime.skills.list()}
            if path == "/v2/runs":
                return {"runs": runtime.runner.list()}
            if path == "/v2/telemetry":
                try:
                    since = int(query.get("since", ["0"])[0])
                except ValueError:
                    raise BadRequest("since 须为整数") from None
                return runtime.telemetry(since)
            if path == "/v2/models":
                return runtime.list_models()
            if path == "/v2/conformance":
                return {"conformance": runtime.conformance.list()}
            m = _CONFORMANCE.match(path)
            if m:
                return runtime.get_conformance(unquote(m.group(1)))
            m = _RUN_EVENTS.match(path)
            if m:
                since, limit = self._int_query(query, "since", 0), self._int_query(query, "limit", 1000)
                return runtime.run_events(unquote(m.group(1)), since, max(1, min(limit, 10_000)))
            m = _RUN.match(path)
            if m:
                return runtime.runner.get(unquote(m.group(1)))
            m = _EVALUATION.match(path)
            if m:
                return runtime.get_evaluation(unquote(m.group(1)))
            raise NotFound("未知接口")

        @staticmethod
        def _int_query(query: dict, name: str, default: int) -> int:
            try:
                return int(query.get(name, [str(default)])[0])
            except ValueError:
                raise BadRequest(f"{name} 须为整数") from None

        # ---------------------------------------------------------- DELETE
        def do_DELETE(self):
            path = urlsplit(self.path).path
            if not self._admit_path(path):
                return
            if not self._authorized():
                return
            m = _MODEL.match(path)
            if m:
                self._dispatch(lambda: runtime.delete_model(unquote(m.group(1)), self._json_body_optional()))
                return

            def unknown():
                self._raw_body()
                raise NotFound("未知接口")
            self._dispatch(unknown)

        def _json_body_optional(self) -> dict:
            if not self.headers.get("Content-Length") or self.headers.get("Content-Length") == "0":
                self._raw_body()
                return {}
            return self._json_body()

        # ---------------------------------------------------------- POST
        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._admit_path(path):
                return
            if not self._authorized():
                return
            if path in STOP_PATHS:
                return self._stop(path)
            self._dispatch(lambda: self._post(path, self._json_body()))

        def _stop(self, path: str) -> None:
            # Stop first; the body (reason) is only read afterwards and may be invalid.
            sup = runtime.supervisor
            prior = sup.latch_reason if sup.latched else None    # no IO: read before stopping
            result = runtime.emergency_stop(STOP_PLACEHOLDER, source=path)
            try:
                raw = self._raw_body()
                payload = json.loads(raw, parse_constant=_reject_constant) if raw else {}
                if isinstance(payload, dict) and payload.get("keep_reason") is True and prior:
                    # R4b-08: a re-stop (e.g. conformance --mode client) keeps the existing latch
                    # reason (the operator's); the stop itself already happened and was counted.
                    # R4c-02: compare-and-set -- the prior reason is written back only if no stop
                    # happened after this one and the reason is still this stop's placeholder, so a
                    # slow body can never revert a newer reason (e.g. an operator's).
                    kept = runtime.restore_stop_reason(prior, result.get("stop_count"))
                    result = {**result, "latch_reason_kept": kept}
                    if payload.get("reason"):
                        runtime.events.emit("safety.stop_reason", reason=str(payload["reason"])[:500],
                                            latch_reason_kept=kept)
                elif isinstance(payload, dict) and payload.get("reason"):
                    reason = str(payload["reason"])[:500]
                    runtime.events.emit("safety.stop_reason", reason=reason)
                    runtime.annotate_stop_reason(reason)
            except Exception:
                pass
            try:
                self._send(result)
            except OSError:
                pass                       # robot is stopped; the client went away

        def _post(self, path: str, payload: dict) -> dict:
            if path == "/v1/validate":
                return runtime.v1_validate(payload.get("application"))
            if path == "/v1/evaluate":
                return runtime.v1_evaluate(payload)
            if path == "/v1/deploy":
                return runtime.v1_deploy(payload)
            if path == "/v1/start":
                return runtime.v1_start(payload)
            if path == "/v1/models/install":
                return runtime.v1_install_model(payload)
            if path == "/v2/skills/install":
                return runtime.install_skill(payload)
            if path == "/v2/reset":
                return runtime.reset(payload)
            if path == "/v1/reset":
                return runtime.v1_reset(payload)
            if path == "/v2/conformance":
                return runtime.start_conformance(payload)
            m = _CAP_INVOKE.match(path)
            if m:
                return runtime.invoke(unquote(m.group(1)), payload)
            m = _SKILL_RUN.match(path)
            if m:
                return runtime.run_skill(unquote(m.group(1)), payload)
            m = _RUN_CANCEL.match(path)
            if m:
                return runtime.runner.cancel(unquote(m.group(1)))
            raise NotFound("未知接口")

    return Handler


class _Slot:
    __slots__ = ("sock", "stop_only", "evictable", "evicted")

    def __init__(self, sock, stop_only: bool):
        self.sock = sock
        self.stop_only = stop_only
        self.evictable = stop_only
        self.evicted = False

    def identified(self) -> None:
        self.evictable = False

    def evict(self) -> None:
        self.evicted = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)    # wakes the blocked recv in its thread
        except OSError:
            pass


class BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a connection limit and reserved stop slots."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = LISTEN_BACKLOG

    def __init__(self, address, handler, *, max_connections: int = MAX_CONNECTIONS,
                 stop_reserved: int = STOP_RESERVED, request_timeout: float = REQUEST_TIMEOUT_S,
                 stop_sniff_timeout: float = STOP_SNIFF_TIMEOUT_S, on_reject=None):
        self.max_connections = max_connections
        self.stop_reserved = stop_reserved
        self.request_timeout = request_timeout
        self.stop_sniff_timeout = stop_sniff_timeout
        self._on_reject = on_reject
        self._slots_lock = threading.Lock()      # bookkeeping only; held for microseconds
        self._general = 0
        self._reserved: list[_Slot] = []
        self._by_sock: dict = {}
        self.rejected = 0
        self.evicted = 0
        super().__init__(address, handler)

    def slot_for(self, sock):
        return self._by_sock.get(sock)

    def connection_counts(self) -> dict:
        with self._slots_lock:
            return {"general": self._general, "reserved": len(self._reserved),
                    "rejected": self.rejected, "evicted": self.evicted}

    def _acquire(self, sock) -> _Slot | None:
        with self._slots_lock:
            if self._general < self.max_connections:
                self._general += 1
                slot = _Slot(sock, stop_only=False)
            elif self.stop_reserved > 0:
                if len(self._reserved) >= self.stop_reserved:
                    victim = next((s for s in self._reserved if s.evictable), None)
                    if victim is None:
                        self.rejected += 1
                        return None          # every reserved slot is serving a stop
                    self._reserved.remove(victim)
                    victim.evict()
                    self.evicted += 1
                slot = _Slot(sock, stop_only=True)
                self._reserved.append(slot)
            else:
                self.rejected += 1
                return None
            self._by_sock[sock] = slot
            return slot

    def _release(self, slot: _Slot) -> None:
        with self._slots_lock:
            self._by_sock.pop(slot.sock, None)
            if not slot.stop_only:
                self._general -= 1
            elif slot in self._reserved:
                self._reserved.remove(slot)

    def _reject(self, sock) -> None:
        try:
            sock.setblocking(False)
            sock.send(_BUSY_RESPONSE)
        except OSError:
            pass
        self.shutdown_request(sock)
        if self._on_reject is not None:
            try:
                self._on_reject()
            except Exception:
                pass

    def process_request(self, request, client_address):
        slot = self._acquire(request)
        if slot is None:
            self._reject(request)
            return
        try:
            thread = threading.Thread(target=self._serve_slot, args=(request, client_address, slot),
                                      name="nx-http", daemon=True)
            thread.start()
        except RuntimeError:                 # cannot start a thread: fail fast, never hang
            self._release(slot)
            self._reject(request)

    def _serve_slot(self, request, client_address, slot: _Slot) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass                              # slow/vanished client, evicted, etc.
        finally:
            self._release(slot)
            self.shutdown_request(request)


class GatewayServer:
    """Runs the HTTP gateway in a background thread (used by CLI and tests)."""

    def __init__(self, runtime: EdgeRuntime, token: str, host: str = "127.0.0.1", port: int = 8790, *,
                 max_connections: int = MAX_CONNECTIONS, stop_reserved: int = STOP_RESERVED,
                 request_timeout: float = REQUEST_TIMEOUT_S, stop_sniff_timeout: float = STOP_SNIFF_TIMEOUT_S):
        self.runtime = runtime
        self.httpd = BoundedHTTPServer((host, port), make_handler(runtime, token),
                                       max_connections=max_connections, stop_reserved=stop_reserved,
                                       request_timeout=request_timeout, stop_sniff_timeout=stop_sniff_timeout)
        self.host, self.port = host, self.httpd.server_port
        self.url = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def start(self) -> "GatewayServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="nx-gateway", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def close(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(5)
        self.httpd.server_close()
