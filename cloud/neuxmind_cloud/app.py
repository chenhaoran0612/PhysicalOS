"""FastAPI application factory: middleware, error format, routers."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import security
from .config import Settings
from .db import Database
from .errors import ApiError
from .mailer import EmailSender, build_sender
from .upstream import build_provider

log = logging.getLogger("neuxmind_cloud")
API_PREFIX = "/api/v1"


@dataclass
class AppContext:
    settings: Settings
    db: Database
    # Maps a model card's upstream binding to a provider. Overridable in tests.
    provider_factory: Callable[[dict], object] = field(default=None)  # type: ignore[assignment]
    # None = no e-mail can be sent (prod/staging default). See mailer.py.
    email_sender: EmailSender | None = None
    maintenance: object = None


# Session model calls (review Q3-07): an agent runtime sends its whole history
# on every request (Codex with store=false), so these two routes get their own,
# larger limit. Every byte still counts towards the call's input bound and is
# reserved against the session budget before anything reaches the upstream.
SESSION_CALL_PATH = re.compile(r"^" + re.escape(API_PREFIX) + r"/gateway/sessions/[A-Za-z0-9_-]{1,100}/v1/(responses|chat/completions)$")


class BodySizeLimitMiddleware:
    """Reject request bodies above ``max_bytes`` (declared or streamed) with 413
    (``session_max_bytes`` for gateway session model calls)."""

    def __init__(self, app, max_bytes: int, session_max_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_bytes
        self.session_max_bytes = session_max_bytes or max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        session_call = scope.get("method") == "POST" and bool(SESSION_CALL_PATH.match(scope.get("path", "")))
        limit = self.session_max_bytes if session_call else self.max_bytes
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = limit + 1
                if declared > limit:
                    return await _too_large(send, limit, session_call)
        # Read the (possibly chunked) body up front, bounded by the limit, then
        # replay it to the app. Bodies are small JSON documents, so buffering is fine.
        chunks: list[bytes] = []
        seen = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            seen += len(body)
            if seen > limit:
                return await _too_large(send, limit, session_call)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        buffered = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return buffered
            return await receive()

        await self.app(scope, replay_receive, send)


async def _too_large(send, limit: int, session_call: bool = False):
    body = {"error": f"请求体过大（上限 {limit} 字节）", "code": "payload_too_large", "max_bytes": limit}
    if session_call:
        body["error"] += "：会话历史过长，请开启新会话继续"
    response = JSONResponse(body, status_code=413)
    await send({"type": "http.response.start", "status": 413, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


def create_app(settings: Settings | None = None, *, provider_factory: Callable[[dict], object] | None = None,
               email_sender: EmailSender | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    security.configure(settings.scrypt_log_n)
    if settings.operator_emails and not settings.operator_allowlist_active:
        log.warning("NEUXMIND_OPERATOR_EMAILS is set but ignored outside dev/test unless "
                    "NEUXMIND_OPERATOR_ALLOWLIST=1; use `create-operator` instead")
    db = Database(settings.database_url)
    db.create_all()
    ctx = AppContext(settings=settings, db=db, email_sender=email_sender or build_sender(settings))
    ctx.provider_factory = provider_factory or (lambda upstream: build_provider(
        upstream, test_env=settings.is_test_env, timeout=settings.upstream_timeout_seconds))
    from .maintenance import Maintenance
    ctx.maintenance = Maintenance(db, settings)

    # Interactive API docs only in dev/test (review finding C-08).
    docs = settings.is_test_env
    app = FastAPI(title="NeuxMind Cloud", version="0.2.0", docs_url=f"{API_PREFIX}/docs" if docs else None,
                  openapi_url=f"{API_PREFIX}/openapi.json" if docs else None, redoc_url=None)
    app.state.ctx = ctx
    if settings.maintenance_interval_seconds > 0:
        ctx.maintenance.start_background(settings.maintenance_interval_seconds)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return JSONResponse({"error": f"请求格式无效：{where} {first.get('msg', '')}".strip(), "code": "invalid_request"},
                            status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return JSONResponse({"error": str(exc.detail), "code": codes.get(exc.status_code, "http_error")},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse({"error": "服务器内部错误", "code": "internal_error"}, status_code=500)

    from .routes import account, admin, commerce, fleet, gateway_routes
    for module in (account, commerce, gateway_routes, fleet, admin):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "env": settings.env}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes,
                       session_max_bytes=settings.session_max_body_bytes)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-NeuxMind-Org"])
    return app
