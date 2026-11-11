"""Orders, payment providers and webhook handling.

Two ways to pay:

* **Credit packs** (buying platform credits) are paid in money through a
  :class:`PaymentProvider`. Credits enter the ledger only when the provider
  confirms payment: an operator confirmation (``manual``) or a signed webhook.
* **Model licenses** are paid in credits from the wallet, atomically, in the
  same transaction that creates the entitlement.

No provider ever reports success it did not receive. Stripe / Alipay /
WeChat Pay are skeletons that answer ``provider_not_configured``.
"""
from __future__ import annotations

import hmac
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, ledger
from .config import Settings
from .db import lock_wallet
from .errors import ApiError, bad_request, conflict, insufficient_credits, not_found
from .models import CreditPack, Entitlement, ModelCard, Order, WebhookEvent, now
from .security import hmac_sha256_hex

SIGNATURE_HEADER = "X-NeuxMind-Signature"


def provider_not_configured(name: str) -> ApiError:
    return ApiError(503, "provider_not_configured", f"支付渠道 {name} 尚未配置，订单未扣款、未入账")


# ---------------------------------------------------------------- providers
class PaymentProvider:
    """Interface. ``checkout`` returns what the client should do next;
    ``parse_webhook`` verifies the signature and returns a normalised event."""

    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    def available(self) -> bool:
        return False

    def checkout(self, order: Order) -> dict:
        raise provider_not_configured(self.name)

    def parse_webhook(self, body: bytes, headers: dict) -> dict:
        raise provider_not_configured(self.name)


def verify_hmac_signature(secret: str, body: bytes, header: str | None, tolerance: int) -> None:
    """Verify ``X-NeuxMind-Signature: t=<unix>,v1=<hex(HMAC_SHA256(secret, f"{t}.{body}"))>``."""
    if not header:
        raise ApiError(400, "signature_missing", "缺少 webhook 签名")
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        raise ApiError(400, "signature_invalid", "webhook 签名格式无效") from None
    if abs(time.time() - timestamp) > tolerance:
        raise ApiError(400, "signature_expired", "webhook 签名已过期")
    expected = hmac_sha256_hex(secret, f"{timestamp}.".encode() + body)
    if not hmac.compare_digest(expected, parts.get("v1", "")):
        raise ApiError(400, "signature_invalid", "webhook 签名校验失败")


def sign_webhook(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Produce the signature header (used by tests and by the sandbox tool)."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={hmac_sha256_hex(secret, f'{ts}.'.encode() + body)}"


class ManualProvider(PaymentProvider):
    """Offline payment (bank transfer, invoice). An operator confirms receipt
    via ``POST /admin/orders/{id}/confirm`` with a reference; only then are
    credits booked."""

    name = "manual"

    def available(self) -> bool:
        return True

    def checkout(self, order: Order) -> dict:
        return {"provider": "manual", "status": "awaiting_manual_confirmation", "order_id": order.id,
                "instructions": "请按运营提供的对公账户线下付款，并在备注中填写订单号；运营确认到账后入账。",
                "simulated": False}

    def parse_webhook(self, body: bytes, headers: dict) -> dict:
        raise ApiError(400, "webhook_unsupported", "manual 渠道没有 webhook，到账由运营确认")


class SandboxProvider(PaymentProvider):
    """**TEST ONLY — NOT A REAL PAYMENT.** Available only when NEUXMIND_ENV is
    dev/test. Checkout returns a fake URL; payment is simulated by posting a
    webhook signed with ``NEUXMIND_WEBHOOK_SECRET_SANDBOX``."""

    name = "sandbox"

    def available(self) -> bool:
        return self.settings.is_test_env and bool(self.settings.webhook_secrets.get("sandbox"))

    def checkout(self, order: Order) -> dict:
        return {"provider": "sandbox", "status": "pending", "order_id": order.id, "simulated": True,
                "checkout_url": f"{self.settings.public_base_url}/sandbox/checkout/{order.id}",
                "notice": "沙箱支付：非真实支付，仅用于开发与测试"}

    def parse_webhook(self, body: bytes, headers: dict) -> dict:
        verify_hmac_signature(self.settings.webhook_secrets["sandbox"], body, headers.get(SIGNATURE_HEADER.lower()),
                              self.settings.webhook_tolerance_seconds)
        try:
            event = json.loads(body)
        except ValueError:
            raise bad_request("webhook 内容不是 JSON") from None
        if not isinstance(event, dict):
            raise bad_request("webhook 内容须为对象")
        for key in ("event_id", "type", "order_id"):
            if not isinstance(event.get(key), str) or not event[key]:
                raise bad_request(f"webhook 缺少 {key}")
        return event


class _UnconfiguredProvider(PaymentProvider):
    """Skeleton for a real channel. Integration must implement the channel's own
    signature scheme (Stripe-Signature, Alipay RSA2, WeChat Pay v3 AEAD) before
    ``available`` may return True."""

    def available(self) -> bool:
        return False


class StripeProvider(_UnconfiguredProvider):
    name = "stripe"


class AlipayProvider(_UnconfiguredProvider):
    name = "alipay"


class WechatPayProvider(_UnconfiguredProvider):
    name = "wechatpay"


PROVIDERS = {cls.name: cls for cls in (ManualProvider, SandboxProvider, StripeProvider, AlipayProvider, WechatPayProvider)}


def get_provider(name: str, settings: Settings) -> PaymentProvider:
    cls = PROVIDERS.get(name)
    if cls is None:
        raise not_found(f"未知支付渠道：{name}", code="unknown_provider")
    provider = cls(settings)
    if not provider.available():
        if name == "sandbox":
            raise ApiError(403, "sandbox_disabled", "沙箱支付仅在 dev/test 环境可用（且需配置签名密钥）")
        raise provider_not_configured(name)
    return provider


# ------------------------------------------------------------------- orders
def order_view(order: Order) -> dict:
    return {"id": order.id, "org_id": order.org_id, "kind": order.kind, "item_id": order.item_id,
            "credits": order.credits, "price_minor": order.price_minor, "currency": order.currency,
            "status": order.status, "provider": order.provider, "provider_reference": order.provider_reference,
            "created_at": order.created_at, "paid_at": order.paid_at}


def create_credit_pack_order(session: Session, org_id: str, user_id: str, pack_id) -> Order:
    pack = session.get(CreditPack, pack_id) if isinstance(pack_id, str) else None
    if pack is None or not pack.active:
        raise not_found("token 套餐不存在或已下架")
    order = Order(org_id=org_id, created_by=user_id, kind="credit_pack", item_id=pack.id, credits=pack.credits,
                  price_minor=pack.price_minor, currency=pack.currency, status="pending")
    session.add(order)
    session.flush()
    return order


def buy_model_license(session: Session, org_id: str, user_id: str, model_id) -> tuple[Order, Entitlement]:
    """Pay a model license from wallet credits and grant the entitlement atomically.

    license_once models cost ``pricing.credits``; usage-priced and free models
    get a 0-credit entitlement (usage is billed per call through the gateway).
    Re-buying an existing license returns the existing entitlement.
    """
    card = session.get(ModelCard, model_id) if isinstance(model_id, str) else None
    if card is None or card.status != "published":
        raise not_found("模型不存在或已下架")
    lock_wallet(session, org_id)
    existing = session.execute(select(Entitlement).where(Entitlement.org_id == org_id, Entitlement.model_id == card.id,
                                                         Entitlement.revoked_at.is_(None))).scalar_one_or_none()
    if existing is not None:
        raise conflict("已拥有该模型授权", code="already_entitled")
    price = card.pricing["credits"] if card.pricing["type"] == "license_once" else 0
    available = ledger.balance(session, org_id)["available"]
    if available < price:
        raise insufficient_credits(available, price)
    order = Order(org_id=org_id, created_by=user_id, kind="model_license", item_id=card.id, credits=price,
                  status="paid", provider="wallet", paid_at=now())
    session.add(order)
    session.flush()
    if price:
        ledger.post_entry(session, org_id, -price, "license", f"order:{order.id}", actor=f"user:{user_id}",
                          reason=f"模型授权 {card.id}", detail={"model_id": card.id, "pricing": card.pricing})
    entitlement = Entitlement(org_id=org_id, model_id=card.id, order_id=order.id)
    session.add(entitlement)
    session.flush()
    return order, entitlement


def fulfill_credit_order(session: Session, order_id: str, provider: str, reference: str, actor: str,
                         *, amount_minor: int | None = None, currency: str | None = None) -> tuple[Order, bool]:
    """Book the credits of a paid credit-pack order exactly once.

    Returns ``(order, newly_credited)``. Amount/currency, when supplied by the
    provider, must match the order or nothing is booked.
    """
    order = session.get(Order, order_id)
    if order is None or order.kind != "credit_pack":
        raise not_found("订单不存在")
    lock_wallet(session, order.org_id)
    session.refresh(order)
    if order.status == "paid":
        return order, False
    if order.status not in ("pending", "awaiting_manual_confirmation"):
        raise conflict(f"订单状态为 {order.status}，不能入账", code="order_not_payable")
    if order.provider and order.provider != provider:
        raise conflict("订单的支付渠道与回调来源不符", code="provider_mismatch")
    if amount_minor is not None and (amount_minor != order.price_minor or (currency or "").upper() != order.currency):
        raise conflict("支付金额或币种与订单不符，未入账", code="amount_mismatch")
    ledger.post_entry(session, order.org_id, order.credits, "purchase", f"order:{order.id}", actor=actor,
                      reason=f"token 套餐 {order.item_id}", detail={"provider": provider, "provider_reference": reference})
    order.status = "paid"
    order.provider = provider
    order.provider_reference = reference
    order.paid_at = now()
    session.flush()
    return order, True


def handle_webhook(session: Session, provider: PaymentProvider, event: dict, ip: str = "") -> dict:
    """Apply a verified webhook event idempotently (per provider + event_id and per order)."""
    try:
        with session.begin_nested():
            record = WebhookEvent(provider=provider.name, event_id=event["event_id"], order_id=event.get("order_id"),
                                  outcome="processing")
            session.add(record)
            session.flush()
    except IntegrityError:
        return {"received": True, "duplicate": True}
    if event["type"] != "payment.succeeded":
        record.outcome = f"ignored:{event['type']}"[:40]
        return {"received": True, "ignored": event["type"]}
    amount = event.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise bad_request("webhook 缺少整数 amount_minor")
    order, credited = fulfill_credit_order(session, event["order_id"], provider.name,
                                           str(event.get("provider_reference") or event["event_id"]),
                                           f"webhook:{provider.name}", amount_minor=amount, currency=event.get("currency"))
    record.outcome = "credited" if credited else "already_paid"
    audit.record(session, "payment.webhook_credited" if credited else "payment.webhook_replay", f"webhook:{provider.name}",
                 org_id=order.org_id, target=order.id, detail={"event_id": event["event_id"]}, ip=ip)
    return {"received": True, "order_id": order.id, "credited": credited}
