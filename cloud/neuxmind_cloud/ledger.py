"""Credit wallet: append-only ledger + reservations.

Invariants (carried over from ``brainos/commerce.py``):

* Credits are integers. Balance is ``SUM(ledger.delta)`` — there is no stored
  balance column that could drift.
* ``available = balance - SUM(reservations in held / reconciliation_required)``.
  A reservation whose outcome is unknown keeps holding its credits until an
  operator reconciles it.
* A reservation is created *before* any upstream call, and only if
  ``available >= amount``. Every function here that moves money first calls
  :func:`db.lock_wallet`, so the check-then-write is atomic per org.
* Settlement needs server-side metering evidence, may never exceed the held
  amount (overage goes to reconciliation), and is idempotent: the ledger entry
  reference ``settle:<reservation_id>`` is unique.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import lock_wallet
from .errors import ApiError, bad_request, conflict, insufficient_credits, not_found
from .models import LedgerEntry, Reservation, now

HOLDING_STATUSES = ("held", "reconciliation_required")
MAX_CREDITS = 10 ** 12


def _check_amount(value, label: str, *, allow_zero: bool = False, allow_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_request(f"{label}须为整数")
    if abs(value) > MAX_CREDITS:
        raise bad_request(f"{label}超出范围")
    if value == 0 and not allow_zero:
        raise bad_request(f"{label}不能为 0")
    if value < 0 and not allow_negative:
        raise bad_request(f"{label}不能为负数")
    return value


def balance(session: Session, org_id: str) -> dict:
    total = session.execute(select(func.coalesce(func.sum(LedgerEntry.delta), 0)).where(LedgerEntry.org_id == org_id)).scalar_one()
    reserved = session.execute(
        select(func.coalesce(func.sum(Reservation.amount), 0)).where(
            Reservation.org_id == org_id, Reservation.status.in_(HOLDING_STATUSES))
    ).scalar_one()
    return {"balance": int(total), "reserved": int(reserved), "available": int(total) - int(reserved)}


def entries(session: Session, org_id: str, limit: int = 200) -> list[LedgerEntry]:
    return list(session.execute(
        select(LedgerEntry).where(LedgerEntry.org_id == org_id).order_by(LedgerEntry.created_at.desc()).limit(limit)
    ).scalars())


def post_entry(session: Session, org_id: str, delta: int, kind: str, reference: str, *, reason: str = "",
               actor: str = "", detail: dict | None = None) -> tuple[LedgerEntry, bool]:
    """Append a ledger entry once per ``reference``.

    Returns ``(entry, created)``. If an entry with this reference already
    exists with the same org and delta, it is returned unchanged
    (idempotent replay). A different amount under the same reference is a
    conflict and is never silently overwritten. Caller must hold the wallet lock.
    """
    existing = session.execute(select(LedgerEntry).where(LedgerEntry.reference == reference)).scalar_one_or_none()
    if existing is not None:
        if existing.org_id != org_id or existing.delta != delta:
            raise conflict("同一业务引用已存在不同的记账记录，请人工核对", code="ledger_conflict")
        return existing, False
    entry = LedgerEntry(org_id=org_id, delta=delta, kind=kind, reference=reference, reason=reason,
                        actor=actor, detail=detail or {})
    session.add(entry)
    session.flush()
    return entry, True


def reserve(session: Session, org_id: str, amount: int, *, purpose: str, model_id: str | None = None,
            pricing: dict | None = None, note: str = "") -> Reservation:
    """Hold ``amount`` credits. Raises 402 if not enough are available."""
    _check_amount(amount, "预留额度", allow_zero=True)
    lock_wallet(session, org_id)
    current = balance(session, org_id)
    if current["available"] < amount:
        raise insufficient_credits(current["available"], amount)
    reservation = Reservation(org_id=org_id, amount=amount, status="held", purpose=purpose, model_id=model_id,
                              pricing=pricing or {}, note=note)
    session.add(reservation)
    session.flush()
    return reservation


def _locked_reservation(session: Session, reservation_id: str, org_id: str | None = None) -> Reservation:
    reservation = session.get(Reservation, reservation_id)
    if reservation is None or (org_id is not None and reservation.org_id != org_id):
        raise not_found("预留不存在")
    lock_wallet(session, reservation.org_id)
    session.refresh(reservation)
    return reservation


def settle(session: Session, reservation_id: str, credits: int, evidence_ref: str, actor: str,
           *, org_id: str | None = None, detail: dict | None = None) -> Reservation:
    """Charge ``credits`` (<= held amount) against a reservation and release the rest.

    Idempotent: settling an already-settled reservation with the same amount
    and evidence returns it unchanged; anything else is a conflict.
    """
    _check_amount(credits, "结算额度", allow_zero=True)
    if not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 2000:
        raise bad_request("结算需要服务端计量证据引用", code="evidence_required")
    reservation = _locked_reservation(session, reservation_id, org_id)
    if reservation.status == "settled":
        if reservation.settled_amount == credits and reservation.evidence_ref == evidence_ref:
            return reservation
        raise conflict("该预留已按不同用量结算，请核对账本", code="already_settled")
    if reservation.status not in HOLDING_STATUSES:
        raise conflict(f"预留状态为 {reservation.status}，不能结算", code="reservation_not_held")
    if credits > reservation.amount:
        raise ApiError(409, "overage", f"实际用量 {credits} 超过预留 {reservation.amount}；超额需人工核对，不自动扣费")
    if credits > 0:
        post_entry(session, reservation.org_id, -credits, "usage", f"settle:{reservation.id}", actor=actor,
                   reason=f"{reservation.purpose}:{reservation.model_id or ''}",
                   detail={"reservation_id": reservation.id, "evidence_ref": evidence_ref, **(detail or {})})
    reservation.status = "settled"
    reservation.settled_amount = credits
    reservation.evidence_ref = evidence_ref
    reservation.updated_at = now()
    session.flush()
    return reservation


def release(session: Session, reservation_id: str, actor: str, *, org_id: str | None = None, note: str = "") -> Reservation:
    """Return held credits without charging. Only for outcomes known to be unbilled."""
    reservation = _locked_reservation(session, reservation_id, org_id)
    if reservation.status == "released":
        return reservation
    if reservation.status not in HOLDING_STATUSES:
        raise conflict(f"预留状态为 {reservation.status}，不能释放", code="reservation_not_held")
    reservation.status = "released"
    reservation.settled_amount = 0
    reservation.note = (reservation.note + f" released by {actor}: {note}").strip()[:2000]
    reservation.updated_at = now()
    session.flush()
    return reservation


def mark_reconciliation(session: Session, reservation_id: str, note: str) -> Reservation:
    """Outcome unknown: keep holding the credits and flag for operator review."""
    reservation = _locked_reservation(session, reservation_id)
    if reservation.status == "held":
        reservation.status = "reconciliation_required"
        reservation.note = (reservation.note + " " + note).strip()[:2000]
        reservation.updated_at = now()
        session.flush()
    return reservation


def adjust(session: Session, org_id: str, delta: int, reason: str, actor: str, reference: str) -> LedgerEntry:
    """Operator credit adjustment. Negative adjustments may not exceed available credits."""
    _check_amount(delta, "调整额度", allow_negative=True)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise bad_request("调整额度需要 1–500 字符的原因", code="reason_required")
    lock_wallet(session, org_id)
    if delta < 0 and balance(session, org_id)["available"] < -delta:
        raise conflict("扣减额度超过可用额度", code="insufficient_credits")
    entry, _ = post_entry(session, org_id, delta, "operator_adjust", reference, reason=reason.strip(), actor=actor)
    return entry
