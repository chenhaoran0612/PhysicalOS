"""The open platform for third parties (issue #43): ``/open/*``. Logic in :mod:`neuxmind_cloud.skill_calls`.

Every route needs an API key with the scope ``skills:invoke`` and only ever sees the robots and calls
of that key's organisation."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .. import skill_calls
from ..auth import _throttle
from . import client_ip, ctx, principal

router = APIRouter(prefix="/open")
RATE_WINDOW_SECONDS = 60


@router.get("/robots")
def list_robots(request: Request):
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.list_robots(s, principal(request, s), context.settings)


@router.get("/robots/{device_id}/skills")
def robot_skills(request: Request, device_id: str):
    """The skills installed on the robot, each with the parameters it takes and whether its owner allows remote calls."""
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.robot_skills(s, principal(request, s), device_id, context.settings)


def _count_call(request: Request) -> None:
    """The per-key bound on placing calls, in a transaction of its own: a refused call counts too."""
    context = ctx(request)
    with context.db.transaction() as s:
        p = skill_calls.caller(principal(request, s))
        _throttle(s, "skill_call", p.actor, context.settings.skill_call_rate_per_minute, RATE_WINDOW_SECONDS,
                  "调用过于频繁，请稍后再试")


@router.post("/robots/{device_id}/skills/{skill_id}/calls")
def place_call(request: Request, device_id: str, skill_id: str, payload=Body(...)):
    """``{request_id, parameters?, timeout_s?, version?}`` → 202 ``{call}``; 200 with the same call when
    the ``request_id`` was used before."""
    _count_call(request)
    context = ctx(request)
    with context.db.transaction() as s:
        answer, created = skill_calls.place_call(s, principal(request, s), device_id, skill_id, payload,
                                                 context.settings, ip=client_ip(request))
    return JSONResponse(answer, status_code=202 if created else 200)


@router.get("/calls")
def list_calls(request: Request, device_id: str | None = None, status: str | None = None, limit: str | None = None,
               before: str | None = None):
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.list_calls(s, principal(request, s), context.settings, device_id=device_id, status=status,
                                      limit=limit, before=before)


@router.get("/calls/{call_id}")
def get_call(request: Request, call_id: str):
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.get_call(s, principal(request, s), call_id, context.settings)


@router.post("/calls/{call_id}/cancel")
def cancel_call(request: Request, call_id: str):
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.cancel_call(s, principal(request, s), call_id, context.settings, ip=client_ip(request))


@router.post("/robots/{device_id}/stop", status_code=202)
def request_stop(request: Request, device_id: str, payload=Body(default=None)):
    """``{reason?}`` → 202 ``{stop_id, delivered}``. Best effort over a network; never a substitute for the stop on site."""
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.request_stop(s, principal(request, s), device_id, payload, context.settings,
                                        ip=client_ip(request))


@router.get("/stops/{stop_id}")
def get_stop(request: Request, stop_id: str):
    context = ctx(request)
    with context.db.transaction() as s:
        return skill_calls.get_stop(s, principal(request, s), stop_id, context.settings)
