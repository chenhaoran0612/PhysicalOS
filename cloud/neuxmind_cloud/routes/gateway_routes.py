"""Inference gateway endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Request
from starlette.concurrency import run_in_threadpool

from .. import gateway
from ..errors import not_found
from ..models import GatewaySession, Reservation
from ..streaming import MeteredSSEResponse, SessionStream
from . import ctx, principal

router = APIRouter()


def _principal_only(request: Request):
    with ctx(request).db.transaction() as s:
        return principal(request, s)


@router.post("/gateway/infer")
def infer(request: Request, payload: dict = Body(...)):
    c = ctx(request)
    p = _principal_only(request)
    c.maintenance.maybe_sweep()   # only after authentication (rereview N-11)
    return gateway.infer(c.db, c.settings, p, payload, c.provider_factory)


@router.post("/gateway/sessions", status_code=201)
def open_session(request: Request, payload: dict = Body(...)):
    c = ctx(request)
    p = _principal_only(request)
    c.maintenance.maybe_sweep()   # only after authentication (rereview N-11)
    return gateway.create_session(c.db, c.settings, p, payload)


@router.get("/gateway/sessions/{session_id}")
def get_session(request: Request, session_id: str):
    c = ctx(request)
    with c.db.transaction() as s:
        p = principal(request, s).require("gateway")
        gws = s.get(GatewaySession, session_id)
        if gws is None or gws.org_id != p.org_id:
            raise not_found("会话不存在")
        reservation = s.get(Reservation, gws.reservation_id)
        return {**gateway._session_view(gws, c.settings, None, reservation.amount), "used_tokens": gws.used_tokens,
                "inflight_tokens": gws.inflight_tokens, "calls": gws.calls, "unknown_calls": gws.unknown_calls,
                "overage_calls": gws.overage_calls, "overage_tokens": gws.overage_tokens,
                "close_result": gws.close_result}


@router.post("/gateway/sessions/{session_id}/close")
def close_session(request: Request, session_id: str, payload: dict = Body(...)):
    c = ctx(request)
    p = _principal_only(request)
    c.maintenance.maybe_sweep()   # only after authentication (rereview N-11)
    return gateway.close_session(c.db, p, session_id, payload)


async def _session_call(request: Request, session_id: str, path: str, payload: dict):
    """Session calls run the gateway (DB + upstream) in the thread pool; a streamed
    call's producer thread is started here, where the event loop is known."""
    c = ctx(request)
    result = await run_in_threadpool(gateway.session_call, c.db, c.settings, session_id,
                                     request.headers.get("authorization"), path, payload, c.provider_factory)
    if isinstance(result, SessionStream):
        result.start(asyncio.get_running_loop())
        return MeteredSSEResponse(result)
    return result


@router.post("/gateway/sessions/{session_id}/v1/chat/completions")
async def session_chat(request: Request, session_id: str, payload: dict = Body(...)):
    return await _session_call(request, session_id, "/chat/completions", payload)


@router.post("/gateway/sessions/{session_id}/v1/responses")
async def session_responses(request: Request, session_id: str, payload: dict = Body(...)):
    return await _session_call(request, session_id, "/responses", payload)
