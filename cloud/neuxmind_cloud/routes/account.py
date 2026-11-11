"""Auth, current user, members, API keys, wallet, usage, capability registry."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .. import auth, ledger, recovery
from ..gateway import usage_view
from ..mailer import EmailSendFailed, LogEmailSender, recovery_message, sender_not_configured, verification_message
from ..models import Reservation, UsageRecord
from ..spec_bridge import capabilities
from . import client_ip, ctx, principal

router = APIRouter()
log = logging.getLogger("neuxmind_cloud")


def _deliver_verification(request: Request, to: str, token: str | None, purpose: str = "verify") -> dict:
    """Send the verification e-mail *after* the DB transaction committed.

    The dev/test :class:`LogEmailSender` sends nothing; it records the message
    and, so that local tools and tests can finish the flow over HTTP, the
    response carries the token as ``dev_verification_token`` with
    ``simulated: true``. That sender is refused by config outside dev/test,
    so production responses never contain a token.
    """
    sender = ctx(request).email_sender
    if sender is None or token is None:
        return {"status": "unavailable", "code": "email_sender_not_configured", "simulated": False}
    subject, body = verification_message(ctx(request).settings, token) if purpose == "verify" \
        else recovery_message(ctx(request).settings, token, purpose)
    try:
        sender.send(to, subject, body)
    except EmailSendFailed as exc:
        log.warning("verification e-mail to %s failed: %s", to, exc)
        return {"status": "failed", "code": "email_send_failed", "simulated": False}
    out = {"status": "sent", "sender": sender.name, "simulated": bool(sender.simulated)}
    if isinstance(sender, LogEmailSender):
        out[f"dev_{'verification' if purpose == 'verify' else purpose}_token"] = token
        out["notice"] = "开发/测试环境：未真正发送邮件（记录型发送器），令牌直接返回，仅用于开发与测试"
    return out


@router.post("/auth/register", status_code=201)
def register(request: Request, payload: dict = Body(...)):
    c = ctx(request)
    with c.db.transaction() as s:
        result = auth.register(s, payload, c.settings, client_ip(request), can_send_email=c.email_sender is not None)
    if result.get("status") == "claim_sent":
        # The address is held by an unverified account: no new account, no
        # session; a claim token went to the mailbox (see recovery.py).
        delivery = _deliver_verification(request, result["email"], result.pop("_claim_token"), "claim")
        body = {"status": "claim_sent", "code": "email_pending_claim",
                "message": "该邮箱已被一个未验证的账户注册。我们已向该邮箱发送认领令牌，"
                           "持令牌者可设置新密码并获得一个新的已验证账户（POST /auth/claim）",
                "verification": {k: v for k, v in delivery.items() if k != "dev_claim_token"}}
        if "dev_claim_token" in delivery:
            body["dev_claim_token"] = delivery["dev_claim_token"]
        return JSONResponse(body, status_code=202)
    token = result.pop("_verification_token", None)
    verification = _deliver_verification(request, result["user"]["email"], token)
    if "dev_verification_token" in verification:
        result["dev_verification_token"] = verification["dev_verification_token"]
    return {**result, "verification": verification}


@router.post("/auth/verify-email")
def verify_email(request: Request, payload: dict = Body(...)):
    """``{"token"}`` → ``{email_verified, user, sessions_revoked, api_keys_revoked}``.

    Requires the login session of the account the token was issued to
    (rereview N-01); 403 ``verification_wrong_account`` otherwise."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s)
        revoked = auth.verify_email(s, p, payload, client_ip(request))
        return {"email_verified": True, "user": auth.user_view(p.user), **revoked}


@router.post("/auth/password-reset/request", status_code=202)
def password_reset_request(request: Request, payload: dict = Body(...)):
    """``{email}`` → 202 always (no account enumeration). Verified accounts get a reset
    token, unverified accounts a claim token. 503 when the server cannot send e-mail."""
    c = ctx(request)
    if c.email_sender is None:
        raise sender_not_configured()
    with c.db.transaction() as s:
        issued = recovery.request_reset(s, payload, c.settings, client_ip(request))
    body = {"status": "accepted", "message": "如果该邮箱已注册，我们已发送重设密码或认领邮件"}
    if issued is not None:
        email, token, purpose = issued
        delivery = _deliver_verification(request, email, token, purpose)
        body.update({k: v for k, v in delivery.items() if k.startswith("dev_")})
    return body


@router.post("/auth/password-reset")
def password_reset(request: Request, payload: dict = Body(...)):
    """``{token, password}`` for a reset token (also the create-operator / verify-email setup
    token) or a claim token → new session. A reset deletes every other session."""
    with ctx(request).db.transaction() as s:
        return recovery.redeem(s, payload, ctx(request).settings, client_ip(request))


@router.post("/auth/claim")
def claim(request: Request, payload: dict = Body(...)):
    """``{token, password, name?}`` → a fresh verified account for the mailbox owner; the
    unverified account that held the address is disabled. Same handler as password-reset."""
    with ctx(request).db.transaction() as s:
        return recovery.redeem(s, payload, ctx(request).settings, client_ip(request))


@router.post("/auth/verify-email/resend")
def resend_verification(request: Request):
    """Session only. 503 ``email_sender_not_configured`` when the server cannot send e-mail."""
    c = ctx(request)
    if c.email_sender is None:
        with c.db.transaction() as s:
            p = principal(request, s).require_session()
            if p.user.email_verified:
                return {"email_verified": True}
        raise sender_not_configured()
    with c.db.transaction() as s:
        p = principal(request, s)
        token = auth.request_verification(s, p, c.settings)
        email = p.user.email
    verification = _deliver_verification(request, email, token)
    return {"email_verified": False, "verification": verification,
            **({"dev_verification_token": verification["dev_verification_token"]}
               if "dev_verification_token" in verification else {})}


@router.post("/auth/login")
def login(request: Request, payload: dict = Body(...)):
    with ctx(request).db.transaction() as s:
        return auth.login(s, payload, ctx(request).settings, client_ip(request))


@router.post("/auth/logout")
def logout(request: Request):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require_session()
        auth.logout(s, auth.bearer_token(request.headers.get("authorization")))
        return {"logged_out": True, "user_id": p.user.id}


@router.get("/me")
def me(request: Request):
    with ctx(request).db.transaction() as s:
        return auth.me(s, principal(request, s))


@router.post("/org/members")
def add_member(request: Request, payload: dict = Body(...)):
    with ctx(request).db.transaction() as s:
        return auth.add_member(s, principal(request, s), payload)


@router.get("/org/members")
def list_members(request: Request):
    with ctx(request).db.transaction() as s:
        return {"members": auth.list_members(s, principal(request, s))}


@router.delete("/org/members/{user_id}")
def remove_member(request: Request, user_id: str):
    with ctx(request).db.transaction() as s:
        return auth.remove_member(s, principal(request, s), user_id)


# ------------------------------------------------------------------ API keys
@router.post("/api-keys", status_code=201)
def create_key(request: Request, payload: dict = Body(default={})):
    with ctx(request).db.transaction() as s:
        return auth.create_api_key(s, principal(request, s), payload)


@router.get("/api-keys")
def list_keys(request: Request):
    with ctx(request).db.transaction() as s:
        return {"api_keys": auth.list_api_keys(s, principal(request, s))}


@router.delete("/api-keys/{key_id}")
def revoke_key(request: Request, key_id: str):
    with ctx(request).db.transaction() as s:
        return auth.revoke_api_key(s, principal(request, s), key_id)


# -------------------------------------------------------------------- wallet
def reservation_view(r: Reservation) -> dict:
    return {"id": r.id, "org_id": r.org_id, "amount": r.amount, "status": r.status, "purpose": r.purpose,
            "model_id": r.model_id, "pricing": r.pricing, "settled_amount": r.settled_amount,
            "evidence_ref": r.evidence_ref, "note": r.note, "created_at": r.created_at, "updated_at": r.updated_at}


def entry_view(e) -> dict:
    return {"id": e.id, "delta": e.delta, "kind": e.kind, "reference": e.reference, "reason": e.reason,
            "actor": e.actor, "detail": e.detail, "created_at": e.created_at}


@router.get("/wallet")
def wallet(request: Request):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("wallet:read")
        return {"org_id": p.org_id, "unit": "credits", **ledger.balance(s, p.org_id)}


@router.get("/wallet/ledger")
def wallet_ledger(request: Request, limit: int = 200):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("wallet:read")
        return {"entries": [entry_view(e) for e in ledger.entries(s, p.org_id, min(max(limit, 1), 1000))]}


@router.get("/wallet/reservations")
def wallet_reservations(request: Request, status: str | None = None):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("wallet:read")
        query = select(Reservation).where(Reservation.org_id == p.org_id)
        if status:
            query = query.where(Reservation.status == status)
        rows = s.execute(query.order_by(Reservation.created_at.desc()).limit(500)).scalars()
        return {"reservations": [reservation_view(r) for r in rows]}


# --------------------------------------------------------------------- usage
@router.get("/usage")
def usage(request: Request, limit: int = 200, model: str | None = None):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("usage:read")
        query = select(UsageRecord).where(UsageRecord.org_id == p.org_id)
        if model:
            query = query.where(UsageRecord.model_id == model)
        rows = list(s.execute(query.order_by(UsageRecord.created_at.desc()).limit(min(max(limit, 1), 1000))).scalars())
        return {"usage": [usage_view(r) for r in rows],
                "totals": {"credits": sum(r.credits or 0 for r in rows), "total_tokens": sum(r.total_tokens or 0 for r in rows)}}


# ------------------------------------------------------------- capabilities
@router.get("/capabilities/registry")
def capability_registry():
    """Public: the standard capability registry, straight from ``spec/``."""
    return capabilities.registry()
