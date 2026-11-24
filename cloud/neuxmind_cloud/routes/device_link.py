"""The robots' link to the cloud (issue #43): ``POST /device-link/sync``. Logic in :mod:`neuxmind_cloud.device_link`."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .. import device_link
from . import client_ip, ctx, principal

router = APIRouter()


@router.post("/device-link/sync")
def sync(request: Request, payload=Body(...)):
    """``neuxmind.device_link.v1`` from a robot (an API key with scope ``device:link``; a login session is
    refused) → the calls to start, the calls to cancel, a stop if one was asked for, and what the
    robot need not report again."""
    context = ctx(request)
    with context.db.transaction() as s:
        return device_link.sync(s, principal(request, s), payload, context.settings, ip=client_ip(request))
