"""Device alerts (issue #26, requirement R7-08): devices report, people read. Logic in :mod:`neuxmind_cloud.alerts`."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .. import alerts
from . import client_ip, ctx, principal

router = APIRouter()


@router.post("/device-reports")
def device_report(request: Request, payload: dict = Body(...)):
    """``neuxmind.device_report.v1`` from a device (an API key with scope ``alerts:report``; a login
    session is refused) → ``{accepted, duplicates, rejected, received_at, device}``. Sending it again is harmless."""
    context = ctx(request)
    with context.db.transaction() as s:
        return alerts.receive(s, principal(request, s), payload, context.settings, ip=client_ip(request))


@router.get("/alerts")
def list_alerts(request: Request, instance_id: str | None = None, level: str | None = None,
                handled: str | None = None, limit: str | None = None, before: str | None = None):
    """Alerts of the organisation, newest first (permission ``robots``)."""
    context = ctx(request)
    with context.db.transaction() as s:
        return alerts.list_alerts(s, principal(request, s), context.settings, instance_id=instance_id, level=level,
                                  handled=handled, limit=limit, before=before)


@router.get("/alerts/devices")
def list_devices(request: Request):
    """Devices that report, each ``online`` or ``offline`` (permission ``robots``)."""
    context = ctx(request)
    with context.db.transaction() as s:
        return alerts.list_devices(s, principal(request, s), context.settings)


@router.delete("/alerts/devices")
def forget_device(request: Request, instance_id: str | None = None):
    """Stop watching a device that is gone for good; its alerts stay (permission ``robots``)."""
    with ctx(request).db.transaction() as s:
        return alerts.forget_device(s, principal(request, s), instance_id, ip=client_ip(request))


@router.post("/alerts/{alert_id}/handled")
def set_handled(request: Request, alert_id: str, payload: dict | None = Body(default=None)):
    """``{"handled": true|false}`` (default true) → the alert (permission ``robots``)."""
    with ctx(request).db.transaction() as s:
        return alerts.set_handled(s, principal(request, s), alert_id, {} if payload is None else payload)
