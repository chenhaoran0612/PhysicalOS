"""Minimal JSON-over-HTTP transport on ``urllib`` (standard library only)."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import NeuxMindError

USER_AGENT = "neuxmind-sdk/0.1.0"


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _opener(url: str) -> urllib.request.OpenerDirector:
    # Loopback traffic (local robots, `nx sim up`, local cloud) never goes
    # through an HTTP(S)_PROXY from the environment; everything else does.
    host = urllib.parse.urlsplit(url).hostname
    if _is_loopback(host):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def request_json(method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None,
                 timeout: float = 30.0) -> Any:
    """Send a JSON request and return the decoded JSON response.

    Raises :class:`NeuxMindError`:

    * with the server's ``code`` / ``status`` for HTTP errors (``{"error", "code", ...}``);
    * ``connection_failed`` if the connection could not be made (nothing was processed);
    * ``outcome_unknown`` if a non-GET request was sent but no response came back.
    """
    req = _build(method, url, body, headers, "application/json")
    try:
        with _opener(url).open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise http_error(exc) from None
    except urllib.error.URLError as exc:
        raise _transport_error(method, url, exc.reason) from None
    except (TimeoutError, socket.timeout, ConnectionError) as exc:
        raise _response_error(method, url, exc) from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise NeuxMindError(f"{url} 返回的不是 JSON", code="bad_response") from None


def _build(method: str, url: str, body: Any, headers: dict[str, str] | None, accept: str) -> urllib.request.Request:
    data = None
    all_headers = {"Accept": accept, "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    return urllib.request.Request(url, data=data, headers=all_headers, method=method)


def http_error(exc: urllib.error.HTTPError) -> NeuxMindError:
    """``{"error", "code", ...}`` JSON error body + HTTP status → :class:`NeuxMindError`."""
    try:
        raw = exc.read() or b""
    except (OSError, ValueError):
        raw = b""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"error": str(payload)}
    message = payload.get("error") or payload.get("detail") or exc.reason or f"HTTP {exc.code}"
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False)
    code = payload.get("code") or f"http_{exc.code}"
    details = {k: v for k, v in payload.items() if k not in ("error", "code")}
    return NeuxMindError(message, code=code, status=exc.code, details=details)


def _transport_error(method: str, url: str, reason: Any) -> NeuxMindError:
    """``URLError`` (failed while connecting / sending): only a timeout leaves the outcome open."""
    if isinstance(reason, (TimeoutError, socket.timeout)) and method != "GET":
        return _unknown(method, url, reason)
    return NeuxMindError(f"无法连接 {url}：{reason}", code="connection_failed", details={"url": url})


def _response_error(method: str, url: str, exc: Exception) -> NeuxMindError:
    """The request went out but the response did not come back."""
    if method != "GET":
        return _unknown(method, url, exc)
    return NeuxMindError(f"请求 {url} 失败：{exc}", code="connection_failed", details={"url": url})


def open_stream(method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None,
                timeout: float = 30.0):
    """Send a request that answers with ``text/event-stream`` and return the open response.

    ``timeout`` applies to connecting and to every single socket read afterwards.
    Errors *before* the response starts are raised exactly like :func:`request_json`
    (JSON error body + status → :class:`NeuxMindError`, ``connection_failed``,
    ``outcome_unknown``). The caller owns (and must close) the returned response.
    """
    req = _build(method, url, body, headers, "text/event-stream")
    try:
        return _opener(url).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise http_error(exc) from None
    except urllib.error.URLError as exc:
        raise _transport_error(method, url, exc.reason) from None
    except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
        raise _response_error(method, url, exc) from None


def _unknown(method: str, url: str, exc: Exception) -> NeuxMindError:
    return NeuxMindError(
        f"{method} {url} 已发送但未收到响应（{exc}）：结果未知。请先查询状态；"
        "如需重试，请使用相同的 request_id，切勿用新 id 重放运动指令",
        code="outcome_unknown", details={"url": url})
