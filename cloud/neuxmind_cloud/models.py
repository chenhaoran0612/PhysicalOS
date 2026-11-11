"""ORM tables. Every business row carries ``org_id`` and is queried by it.

Money columns are integers in platform credits ("token 点"). Currency amounts
(credit packs, orders) are integers in minor units (e.g. cents / 分).
"""
from __future__ import annotations

import secrets
import time

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ identity
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    # Platform operator flag. Only settable by the management command; the env
    # allow-list NEUXMIND_OPERATOR_EMAILS is checked separately at request time.
    is_platform_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set only after the owner of the mailbox presents a one-time token (or by
    # the ``verify-email`` management command). Platform-operator status and
    # spending (purchases, gateway) require it.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class EmailVerificationToken(Base):
    """One-time e-mail verification token. Only the SHA-256 is stored."""

    __tablename__ = "email_verification_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # The address the token was sent to; verification fails if the user's
    # address no longer matches.
    email: Mapped[str] = mapped_column(String(200))
    # verify (prove the mailbox from the account's own session) | reset (set a
    # new password; also the create-operator setup link) | claim (take an
    # address back from an unverified account someone else registered).
    purpose: Mapped[str] = mapped_column(String(10), default="verify")
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class RateEvent(Base):
    """Generic throttling counter rows (registrations per ip, verification e-mails per user)."""

    __tablename__ = "rate_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    at: Mapped[float] = mapped_column(Float, default=now, index=True)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("org"))
    name: Mapped[str] = mapped_column(String(100))
    personal: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mem"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | admin | developer | operator
    created_at: Mapped[float] = mapped_column(Float, default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[float] = mapped_column(Float, default=now, index=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("key"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_prefix: Mapped[str] = mapped_column(String(20))
    scopes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    last_used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


# ------------------------------------------------------------------- wallet
class Wallet(Base):
    """Lock anchor for an org's money. It holds **no balance**: balance is
    always ``SUM(ledger_entries.delta)``."""

    __tablename__ = "wallets"
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class LedgerEntry(Base):
    """Append-only. Never updated or deleted by application code."""

    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("led"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    delta: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))  # purchase | usage | license | operator_adjust
    # Idempotency key: the same business event can only ever be booked once.
    reference: Mapped[str] = mapped_column(String(200), unique=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rsv"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # credits held
    # held | settled | released | reconciliation_required
    status: Mapped[str] = mapped_column(String(30), index=True, default="held")
    purpose: Mapped[str] = mapped_column(String(30))  # infer | session | manual
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Price snapshot taken at reservation time; later price changes never touch it.
    pricing: Mapped[dict] = mapped_column(JSON, default=dict)
    settled_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


# ------------------------------------------------------------------ catalog
class ModelCard(Base):
    __tablename__ = "model_cards"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50), default="1.0.0")
    source: Mapped[str] = mapped_column(String(10))  # native | open | closed
    kind: Mapped[str] = mapped_column(String(10))  # llm | vla | vision | speech | policy
    description: Mapped[str] = mapped_column(Text, default="")
    license: Mapped[str] = mapped_column(Text, default="")
    pricing: Mapped[dict] = mapped_column(JSON)
    package: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Operator-only: provider type, base_url, upstream model name, credential_ref.
    upstream: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requires_capabilities: Mapped[list] = mapped_column(JSON, default=list)
    families: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="published")  # published | unlisted
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class CreditPack(Base):
    __tablename__ = "credit_packs"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    credits: Mapped[int] = mapped_column(BigInteger)
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Entitlement(Base):
    __tablename__ = "entitlements"
    __table_args__ = (UniqueConstraint("org_id", "model_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ent"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_cards.id"))
    order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------- purchases
class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ord"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))  # credit_pack | model_license
    item_id: Mapped[str] = mapped_column(String(100))
    credits: Mapped[int] = mapped_column(BigInteger, default=0)  # credits granted (packs) or spent (licenses)
    price_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # pending | awaiting_manual_confirmation | paid | failed | cancelled
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    paid_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(200))
    order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[float] = mapped_column(Float, default=now)


# ------------------------------------------------------------------ gateway
class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("org_id", "request_id"), Index("ix_usage_org_created", "org_id", "created_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("use"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    request_id: Mapped[str] = mapped_column(String(200))
    request_digest: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(100))
    reservation_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    api_key_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # pending | succeeded | failed | reconciliation_required | overage
    status: Mapped[str] = mapped_column(String(30), default="pending")
    # Session calls: tokens this call holds in the session's inflight budget
    # (lets the sweeper release a lost call's hold).
    hold_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upstream_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class GatewaySession(Base):
    """Metered session compatible with the desktop ``/v1/agent-sessions`` protocol."""

    __tablename__ = "gateway_sessions"
    __table_args__ = (UniqueConstraint("org_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("gws"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(200))
    request_digest: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(100))
    reservation_id: Mapped[str] = mapped_column(String(40))
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    max_tokens: Mapped[int] = mapped_column(Integer)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    inflight_tokens: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    unknown_calls: Mapped[int] = mapped_column(Integer, default=0)
    # Calls whose provider-reported usage exceeded their hold (charged at the
    # hold, output withheld, excess recorded in overage_records).
    overage_calls: Mapped[int] = mapped_column(Integer, default=0)
    overage_tokens: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="open")  # open | closed | reconciliation_required
    expires_at: Mapped[float] = mapped_column(Float)
    close_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    close_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class OverageRecord(Base):
    """Provider-reported usage above what was reserved.

    The user is charged exactly the reserved cap; the excess is recorded here
    for operator review (``/admin/overages``): waive it or charge it from the
    org's available credits. Never auto-charged beyond the reservation.
    """

    __tablename__ = "overage_records"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ovg"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    reservation_id: Mapped[str] = mapped_column(String(40))
    usage_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_id: Mapped[str] = mapped_column(String(100))
    upstream_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reported_tokens: Mapped[int] = mapped_column(Integer)
    charged_tokens: Mapped[int] = mapped_column(Integer)
    overage_tokens: Mapped[int] = mapped_column(Integer)
    # Excess at the reservation's price snapshot (informational).
    overage_credits: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | waived | charged
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


# ------------------------------------------------------------------- fleet
class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (UniqueConstraint("org_id", "instance_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rbt"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    family: Mapped[str] = mapped_column(String(30))
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest_digest: Mapped[str] = mapped_column(String(64))
    capabilities: Mapped[list] = mapped_column(JSON)
    model_runtimes: Mapped[list] = mapped_column(JSON, default=list)
    manifest: Mapped[dict] = mapped_column(JSON)
    # Latest conformance.report.v1 summary (§6.4; neuxmind_cloud.conformance). Never acceptance evidence.
    conformance: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


# -------------------------------------------------------------------- audit
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
