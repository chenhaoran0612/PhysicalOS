"""Platform-operator endpoints (``/admin/*``). Session login + operator status required."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from sqlalchemy import select

from .. import audit, catalog, ledger, payments
from ..auth import require_platform_operator
from ..errors import bad_request, conflict, not_found
from ..errors import ApiError
from ..models import AuditLog, CreditPack, ModelCard, Order, Organization, OverageRecord, Reservation, new_id, now
from . import client_ip, ctx, principal
from .account import reservation_view

router = APIRouter(prefix="/admin")


def _operator(request: Request, s):
    return require_platform_operator(principal(request, s))


def _reason(payload: dict) -> str:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise bad_request("需要 1–500 字符的操作原因", code="reason_required")
    return reason.strip()


# -------------------------------------------------------------------- models
@router.get("/models")
def list_models(request: Request):
    with ctx(request).db.transaction() as s:
        _operator(request, s)
        rows = s.execute(select(ModelCard).order_by(ModelCard.id)).scalars()
        return {"models": [catalog.admin_view(c) for c in rows]}


@router.post("/models", status_code=201)
def publish_model(request: Request, payload: dict = Body(...)):
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        data = catalog.validate_card(payload, allow_insecure_upstream=ctx(request).settings.allow_insecure_upstream,
                                     test_env=ctx(request).settings.is_test_env)
        if s.get(ModelCard, data["id"]) is not None:
            raise conflict("模型 ID 已存在；请用 PATCH 修改", code="model_exists")
        card = ModelCard(**data, status="published")
        s.add(card)
        s.flush()
        audit.record(s, "admin.model_publish", op.actor, target=card.id, detail={"pricing": card.pricing}, ip=client_ip(request))
        return catalog.admin_view(card)


@router.patch("/models/{model_id}")
def update_model(request: Request, model_id: str, payload: dict = Body(...)):
    """Edit a card (including price). Existing reservations keep their price
    snapshot and settled ledger entries are immutable, so history is unaffected."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        card = s.get(ModelCard, model_id)
        if card is None:
            raise not_found("模型不存在")
        before = dict(card.pricing)
        data = catalog.validate_card(payload, allow_insecure_upstream=ctx(request).settings.allow_insecure_upstream,
                                     existing=card, test_env=ctx(request).settings.is_test_env)
        for key, value in data.items():
            setattr(card, key, value)
        card.updated_at = now()
        audit.record(s, "admin.model_update", op.actor, target=card.id,
                     detail={"pricing_before": before, "pricing_after": card.pricing, "fields": sorted(payload)},
                     ip=client_ip(request))
        return catalog.admin_view(card)


@router.post("/models/{model_id}/status")
def set_model_status(request: Request, model_id: str, payload: dict = Body(...)):
    """``{"status": "published" | "unlisted"}`` — 上架 / 下架."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        card = s.get(ModelCard, model_id)
        if card is None:
            raise not_found("模型不存在")
        if payload.get("status") not in ("published", "unlisted"):
            raise bad_request("status 须为 published 或 unlisted")
        card.status = payload["status"]
        card.updated_at = now()
        audit.record(s, "admin.model_status", op.actor, target=card.id, detail={"status": card.status})
        return catalog.admin_view(card)


# --------------------------------------------------------------- credit packs
@router.post("/credit-packs", status_code=201)
def create_pack(request: Request, payload: dict = Body(...)):
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        pack_id = payload.get("id")
        if not isinstance(pack_id, str) or not catalog._MODEL_ID.match(pack_id):
            raise bad_request("套餐 ID 只能包含小写字母、数字、点、下划线和连字符")
        if s.get(CreditPack, pack_id) is not None:
            raise conflict("套餐 ID 已存在")
        credits = catalog._int(payload.get("credits"), "credits", 1, 10 ** 12)
        price = catalog._int(payload.get("price_minor"), "price_minor", 1, 10 ** 12)
        currency = payload.get("currency")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
            raise bad_request("currency 须为 3 位币种代码，如 CNY / USD")
        pack = CreditPack(id=pack_id, name=catalog._text(payload.get("name"), "名称", 100), credits=credits,
                          price_minor=price, currency=currency.upper(), active=True)
        s.add(pack)
        audit.record(s, "admin.credit_pack_create", op.actor, target=pack_id, detail={"credits": credits, "price_minor": price})
        return {"id": pack.id, "name": pack.name, "credits": pack.credits, "price_minor": pack.price_minor,
                "currency": pack.currency, "active": True}


# -------------------------------------------------------------------- wallet
@router.post("/wallets/{org_id}/adjust")
def adjust_wallet(request: Request, org_id: str, payload: dict = Body(...)):
    """``{"delta": int, "reason": str, "reference"?: str}`` — booked as an operator_adjust ledger entry."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        if s.get(Organization, org_id) is None:
            raise not_found("组织不存在")
        reason = _reason(payload)
        reference = payload.get("reference") or new_id("adj")
        if not isinstance(reference, str) or len(reference) > 150:
            raise bad_request("reference 无效")
        entry = ledger.adjust(s, org_id, payload.get("delta"), reason, op.actor, f"adjust:{reference}")
        audit.record(s, "admin.wallet_adjust", op.actor, org_id=org_id, target=entry.id,
                     detail={"delta": entry.delta, "reason": reason}, ip=client_ip(request))
        return {"entry_id": entry.id, "delta": entry.delta, "reference": entry.reference, **ledger.balance(s, org_id)}


# --------------------------------------------------------------- reservations
@router.get("/reservations")
def list_reservations(request: Request, status: str = "reconciliation_required", older_than_seconds: int = 0):
    """``status``: reconciliation_required (default) | held | settled | released | all.
    ``older_than_seconds`` filters by age, e.g. ``?status=held&older_than_seconds=900`` for stuck holds."""
    with ctx(request).db.transaction() as s:
        _operator(request, s)
        query = select(Reservation)
        if status != "all":
            query = query.where(Reservation.status == status)
        if older_than_seconds > 0:
            query = query.where(Reservation.created_at < now() - older_than_seconds)
        rows = s.execute(query.order_by(Reservation.created_at).limit(1000)).scalars()
        return {"reservations": [{**reservation_view(r), "age_seconds": int(now() - r.created_at)} for r in rows]}


@router.post("/maintenance/sweep")
def run_sweep(request: Request):
    """Run the clean-up now (lost calls → reconciliation, expired sessions closed, orphan holds flagged)."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
    counts = ctx(request).maintenance.run()
    with ctx(request).db.transaction() as s:
        audit.record(s, "admin.maintenance_sweep", op.actor, detail=counts, ip=client_ip(request))
    return counts


@router.post("/reservations/{reservation_id}/resolve")
def resolve_reservation(request: Request, reservation_id: str, payload: dict = Body(...)):
    """Reconcile from the provider's billing record.

    ``{"action": "settle", "credits": int, "evidence_ref": str, "reason": str}`` or
    ``{"action": "release", "reason": str}``. Credits above the held amount are
    refused — book an extra charge with ``/admin/wallets/{org}/adjust`` instead.
    """
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        reason = _reason(payload)
        action = payload.get("action")
        current = s.get(Reservation, reservation_id)
        if current is None:
            raise not_found("预留不存在")
        if current.status == "held":
            # Review finding C-11: a held reservation may belong to a call that is
            # still running; resolving it would make that call's settlement fail.
            raise ApiError(409, "reservation_in_flight",
                           "该预留仍处于 held（可能有推理在进行）；只能处理 reconciliation_required 的预留。"
                           "卡住的预留请先运行 /admin/maintenance/sweep")
        if action == "settle":
            reservation = ledger.settle(s, reservation_id, payload.get("credits"), payload.get("evidence_ref"),
                                        op.actor, detail={"reason": reason})
        elif action == "release":
            reservation = ledger.release(s, reservation_id, op.actor, note=reason)
        else:
            raise bad_request("action 须为 settle 或 release")
        audit.record(s, f"admin.reservation_{action}", op.actor, org_id=reservation.org_id, target=reservation.id,
                     detail={"reason": reason, "credits": reservation.settled_amount}, ip=client_ip(request))
        return reservation_view(reservation)


# ------------------------------------------------------------------ overages
def overage_view(o: OverageRecord) -> dict:
    return {"id": o.id, "org_id": o.org_id, "reservation_id": o.reservation_id, "usage_id": o.usage_id,
            "session_id": o.session_id, "model_id": o.model_id, "upstream_ref": o.upstream_ref,
            "reported_tokens": o.reported_tokens, "charged_tokens": o.charged_tokens,
            "overage_tokens": o.overage_tokens, "overage_credits": o.overage_credits, "status": o.status,
            "resolution": o.resolution, "created_at": o.created_at, "updated_at": o.updated_at}


@router.get("/overages")
def list_overages(request: Request, status: str = "open"):
    """Usage reported above a reservation: the user was charged the cap; the excess waits here."""
    with ctx(request).db.transaction() as s:
        _operator(request, s)
        query = select(OverageRecord)
        if status != "all":
            query = query.where(OverageRecord.status == status)
        rows = s.execute(query.order_by(OverageRecord.created_at).limit(1000)).scalars()
        return {"overages": [overage_view(o) for o in rows]}


@router.post("/overages/{overage_id}/resolve")
def resolve_overage(request: Request, overage_id: str, payload: dict = Body(...)):
    """``{"action": "waive", "reason"}`` or ``{"action": "charge", "credits": int, "reason"}``.

    ``charge`` books ``-credits`` (≤ the recorded excess, ≤ the org's available
    credits) as ledger entry ``overage:<id>``; the platform never charges more
    than the reserved cap without this explicit operator step."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        reason = _reason(payload)
        # Row lock (rereview N-08): concurrent waive/charge must not both succeed.
        row = s.execute(select(OverageRecord).where(OverageRecord.id == overage_id).with_for_update()).scalar_one_or_none()
        if row is None:
            raise not_found("超额记录不存在")
        if row.status != "open":
            raise conflict(f"超额记录状态为 {row.status}", code="overage_resolved")
        action = payload.get("action")
        if action == "waive":
            row.status, row.resolution = "waived", {"by": op.actor, "reason": reason}
        elif action == "charge":
            credits = payload.get("credits")
            # Rereview N-07: an excess worth 0 credits can only be waived.
            if row.overage_credits < 1:
                raise bad_request("该超额折算为 0 点，只能 waive", code="nothing_to_charge")
            if isinstance(credits, bool) or not isinstance(credits, int) or not 1 <= credits <= row.overage_credits:
                raise bad_request(f"credits 须为 1–{row.overage_credits} 的整数")
            entry = ledger.adjust(s, row.org_id, -credits, f"超额用量 {row.id}：{reason}", op.actor, f"overage:{row.id}")
            row.status, row.resolution = "charged", {"by": op.actor, "reason": reason, "credits": credits,
                                                     "ledger_entry": entry.id}
        else:
            raise bad_request("action 须为 waive 或 charge")
        row.updated_at = now()
        audit.record(s, f"admin.overage_{action}", op.actor, org_id=row.org_id, target=row.id,
                     detail=row.resolution, ip=client_ip(request))
        return overage_view(row)


# -------------------------------------------------------------------- orders
@router.get("/orders")
def list_orders(request: Request, status: str | None = None):
    with ctx(request).db.transaction() as s:
        _operator(request, s)
        query = select(Order)
        if status:
            query = query.where(Order.status == status)
        rows = s.execute(query.order_by(Order.created_at.desc()).limit(1000)).scalars()
        return {"orders": [payments.order_view(o) for o in rows]}


@router.post("/orders/{order_id}/confirm")
def confirm_manual_order(request: Request, order_id: str, payload: dict = Body(...)):
    """Manual channel: operator confirms money was received. ``{"payment_reference", "reason"}``."""
    with ctx(request).db.transaction() as s:
        op = _operator(request, s)
        reason = _reason(payload)
        reference = payload.get("payment_reference")
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
            raise bad_request("需要线下到账凭证号 payment_reference", code="evidence_required")
        order = s.get(Order, order_id)
        if order is None:
            raise not_found("订单不存在")
        if order.provider != "manual":
            raise conflict("只有 manual 渠道订单可由运营确认", code="provider_mismatch")
        order, credited = payments.fulfill_credit_order(s, order_id, "manual", reference.strip(), op.actor)
        audit.record(s, "admin.order_confirm", op.actor, org_id=order.org_id, target=order.id,
                     detail={"reason": reason, "payment_reference": reference, "credited": credited}, ip=client_ip(request))
        return {"order": payments.order_view(order), "credited": credited}


# --------------------------------------------------------------------- audit
@router.get("/audit")
def audit_log(request: Request, action: str | None = None, limit: int = 200):
    with ctx(request).db.transaction() as s:
        _operator(request, s)
        query = select(AuditLog)
        if action:
            query = query.where(AuditLog.action == action)
        rows = s.execute(query.order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000))).scalars()
        return {"audit": [{"id": a.id, "org_id": a.org_id, "actor": a.actor, "action": a.action, "target": a.target,
                           "detail": a.detail, "ip": a.ip, "created_at": a.created_at} for a in rows]}
