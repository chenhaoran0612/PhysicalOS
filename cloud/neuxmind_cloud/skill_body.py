"""Request-body bounds of the two cloud-skills routes that carry more than the general limit
(issue #43), for the body-size middleware in ``app.py``.

* ``POST /skills/releases``: a skill package with its files (``skill_release_max_body_bytes``, 3 MiB).
* ``POST /device-link/sync``: a robot's installed skills, up to 200 of them
  (``device_link_max_body_bytes``, 2 MiB).

Like the chunk route of the material library, the larger bound is granted only to a caller whose
credential exists: anyone else is answered 401 before a byte of the body is read."""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .errors import unauthorized
from .library import _live_credential
from .security import API_KEY_PREFIX

RELEASE_ROUTE = "/api/v1/skills/releases"
SYNC_ROUTE = "/api/v1/device-link/sync"


async def guard_body(scope, send, default: int) -> int | None:
    """The body bound of this request, or ``None`` when it was answered here (401)."""
    if scope.get("method") != "POST" or scope.get("path", "") not in (RELEASE_ROUTE, SYNC_ROUTE):
        return default
    context = scope["app"].state.ctx
    settings = context.settings
    larger = (settings.skill_release_max_body_bytes if scope["path"] == RELEASE_ROUTE
              else settings.device_link_max_body_bytes)
    header = next((value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"authorization"), None)
    # A robot's sync is only ever accepted from an API key: a login session gets no larger bound for it.
    token = (header or "")[7:].strip() if (header or "").lower().startswith("bearer ") else ""
    if scope["path"] == SYNC_ROUTE and token and not token.startswith(API_KEY_PREFIX):
        return default                                          # the route itself refuses it (403 or 401)
    if await run_in_threadpool(_live_credential, context.db, header):
        return max(default, larger)
    response = JSONResponse(unauthorized().body(), status_code=401)
    await send({"type": "http.response.start", "status": 401, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})
    return None
