"""HTTP routers. Shared helpers live here."""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from ..app import AppContext
from ..auth import Principal, resolve


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def client_ip(request: Request) -> str:
    """Peer address; behind a trusted reverse proxy, the right-most untrusted X-Forwarded-For hop.

    Only proxies listed in ``NEUXMIND_TRUSTED_PROXIES`` are believed, so a
    client cannot pick its own address (and dodge per-ip throttles) by sending
    the header itself.
    """
    peer = request.client.host if request.client else ""
    trusted = ctx(request).settings.trusted_proxies
    if peer not in trusted:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in trusted:
            return hop[:64]
    return peer


def principal(request: Request, session: Session) -> Principal:
    return resolve(session, request.headers.get("authorization"), request.headers.get("x-neuxmind-org"),
                   ctx(request).settings)
