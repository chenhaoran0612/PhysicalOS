"""Catalog, entitlements, purchases and payments."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from .. import audit, catalog, conformance, payments
from ..errors import ApiError, bad_request, conflict, forbidden, not_found
from ..models import CreditPack, Entitlement, ModelCard, Order, Robot
from ..spec_bridge import capabilities, manifest
from . import client_ip, ctx, principal

router = APIRouter()


def _entitled_ids(s, org_id: str) -> set[str]:
    return set(s.execute(select(Entitlement.model_id).where(Entitlement.org_id == org_id,
                                                            Entitlement.revoked_at.is_(None))).scalars())


def _with_entitlement(cards, entitled: set[str]) -> list[dict]:
    return [{**catalog.public_view(c), "entitled": c.id in entitled} for c in cards]


@router.get("/catalog/models")
def list_models(request: Request, kind: str | None = None, source: str | None = None,
                capability: str | None = None, robot_id: str | None = None, require_level: str | None = None):
    """Filter by ``kind``, ``source``, ``capability`` (models that need it) or
    ``robot_id`` (models the registered robot can run; with ``require_level=dry`` only the
    capabilities that passed that conformance level count)."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("catalog:read")
        level = conformance.check_require_level(require_level)
        if level and not robot_id:
            raise bad_request("require_level 需要同时给出 robot_id", code="robot_required")
        cards = catalog.published(s)
        if kind:
            cards = [c for c in cards if c.kind == kind]
        if source:
            cards = [c for c in cards if c.source == source]
        if capability:
            try:
                cid = capabilities.normalize(capability)
            except ValueError as exc:
                raise bad_request(str(exc), code="unknown_capability") from None
            cards = [c for c in cards if cid in c.requires_capabilities]
        if robot_id:
            robot = s.get(Robot, robot_id)
            if robot is None or robot.org_id != p.org_id:
                raise not_found("机器人不存在")
            m2 = conformance.restricted_manifest(robot, level)[0] if level else robot.manifest
            cards = [c for c in cards if catalog.compatible_with(c, m2)]
        return {"models": _with_entitlement(cards, _entitled_ids(s, p.org_id))}


@router.post("/catalog/models/compatible")
def compatible_models(request: Request, payload: dict = Body(...)):
    """Given ``{"manifest": robot.adapter.v2}`` (or ``{"robot_id"}`` of a registered robot),
    return the models that body can use.

    ``require_level: "dry"`` (or ``"static"``): only the capabilities that passed that level in
    the robot's latest conformance report count as provided. It needs a registered robot:
    ``robot_id``, or the org's robot with the manifest's ``instance_id`` whose registered manifest
    is this very manifest (otherwise 409 ``conformance_required``)."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("catalog:read")
        level = conformance.check_require_level(payload.get("require_level"))
        robot = None
        if payload.get("robot_id") is not None:
            rid = payload.get("robot_id")
            robot = s.get(Robot, rid) if isinstance(rid, str) else None
            if robot is None or robot.org_id != p.org_id:
                raise not_found("机器人不存在")
            m2 = robot.manifest
        else:
            try:
                m2 = manifest.validate_manifest_v2(payload.get("manifest"))
            except ValueError as exc:
                raise bad_request(str(exc), code="invalid_manifest") from None
            if level:
                robot = s.execute(select(Robot).where(Robot.org_id == p.org_id,
                                                      Robot.instance_id == m2["instance_id"])).scalar_one_or_none()
                if robot is None or catalog.manifest_digest(m2) != robot.manifest_digest:
                    raise ApiError(409, "conformance_required",
                                   "require_level 需要该 manifest 已登记到设备群并上传了一致性报告"
                                   "（nx robot register，然后 nx robot conformance <robot> --level dry --upload）",
                                   require_level=level)
        out: dict = {"instance_id": m2["instance_id"], "capabilities": sorted(manifest.capability_ids(m2))}
        if robot is not None:
            out["robot_id"] = robot.id
        if level:
            m2, usable = conformance.restricted_manifest(robot, level)
            out.update(require_level=level, usable_capabilities=usable,
                       not_verified=sorted(set(out["capabilities"]) - set(usable)),
                       conformance=conformance.brief(robot.conformance, robot))
        cards = [c for c in catalog.published(s) if catalog.compatible_with(c, m2)]
        out["models"] = _with_entitlement(cards, _entitled_ids(s, p.org_id))
        return out


@router.get("/catalog/models/{model_id}")
def get_model(request: Request, model_id: str):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("catalog:read")
        card = s.get(ModelCard, model_id)
        if card is None or card.status != "published":
            raise not_found("模型不存在或已下架")
        return {**catalog.public_view(card), "entitled": card.id in _entitled_ids(s, p.org_id)}


@router.get("/catalog/models/{model_id}/package")
def get_package(request: Request, model_id: str):
    """Edge model package reference. Requires an entitlement."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("catalog:read")
        card = s.get(ModelCard, model_id)
        if card is None or card.status != "published":
            raise not_found("模型不存在或已下架")
        if card.package is None:
            raise not_found("该模型没有端侧部署包", code="no_package")
        if card.id not in _entitled_ids(s, p.org_id):
            raise forbidden("下载端侧模型包需要先获得授权", code="entitlement_required")
        audit.record(s, "catalog.package_access", p.actor, org_id=p.org_id, target=card.id)
        return {"model_id": card.id, "version": card.version, **card.package,
                "install": {"capability": "model.install",
                            "args": {"package_ref": card.package["ref"], "sha256": card.package["sha256"],
                                     "runtime": card.package["runtime"]}}}


@router.get("/catalog/credit-packs")
def list_packs(request: Request):
    with ctx(request).db.transaction() as s:
        principal(request, s).require("catalog:read")
        packs = s.execute(select(CreditPack).where(CreditPack.active.is_(True)).order_by(CreditPack.price_minor)).scalars()
        return {"credit_packs": [{"id": k.id, "name": k.name, "credits": k.credits, "price_minor": k.price_minor,
                                  "currency": k.currency} for k in packs]}


@router.get("/entitlements")
def list_entitlements(request: Request):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("catalog:read")
        rows = s.execute(select(Entitlement).where(Entitlement.org_id == p.org_id, Entitlement.revoked_at.is_(None))).scalars()
        return {"entitlements": [{"id": e.id, "model_id": e.model_id, "order_id": e.order_id, "created_at": e.created_at}
                                 for e in rows]}


# ----------------------------------------------------------------- purchases
@router.post("/purchases", status_code=201)
def create_purchase(request: Request, payload: dict = Body(...)):
    """``{"kind": "credit_pack", "pack_id"}`` → pending order (pay via /payments).
    ``{"kind": "model_license", "model_id"}`` → paid from wallet credits immediately."""
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require_session().require("purchases").require_verified()
        kind = payload.get("kind")
        if kind == "credit_pack":
            order = payments.create_credit_pack_order(s, p.org_id, p.user.id, payload.get("pack_id"))
            audit.record(s, "purchase.create", p.actor, org_id=p.org_id, target=order.id, detail={"kind": kind})
            return {"order": payments.order_view(order)}
        if kind == "model_license":
            order, entitlement = payments.buy_model_license(s, p.org_id, p.user.id, payload.get("model_id"))
            audit.record(s, "purchase.model_license", p.actor, org_id=p.org_id, target=order.id,
                         detail={"model_id": order.item_id, "credits": order.credits})
            return {"order": payments.order_view(order), "entitlement": {"id": entitlement.id, "model_id": entitlement.model_id}}
        raise bad_request("kind 须为 credit_pack 或 model_license")


@router.get("/purchases")
def list_purchases(request: Request):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("purchases")
        rows = s.execute(select(Order).where(Order.org_id == p.org_id).order_by(Order.created_at.desc()).limit(500)).scalars()
        return {"orders": [payments.order_view(o) for o in rows]}


@router.get("/purchases/{order_id}")
def get_purchase(request: Request, order_id: str):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require("purchases")
        order = s.get(Order, order_id)
        if order is None or order.org_id != p.org_id:
            raise not_found("订单不存在")
        return {"order": payments.order_view(order)}


# ------------------------------------------------------------------ payments
@router.post("/payments/{provider_name}/checkout")
def checkout(request: Request, provider_name: str, payload: dict = Body(...)):
    with ctx(request).db.transaction() as s:
        p = principal(request, s).require_session().require("purchases").require_verified()
        provider = payments.get_provider(provider_name, ctx(request).settings)
        order = s.get(Order, payload.get("order_id")) if isinstance(payload.get("order_id"), str) else None
        if order is None or order.org_id != p.org_id or order.kind != "credit_pack":
            raise not_found("订单不存在")
        if order.status not in ("pending", "awaiting_manual_confirmation"):
            raise conflict(f"订单状态为 {order.status}，不能发起支付", code="order_not_payable")
        if order.provider and order.provider != provider.name:
            raise conflict(f"订单已选择支付渠道 {order.provider}", code="provider_mismatch")
        result = provider.checkout(order)
        order.provider = provider.name
        if result.get("status") == "awaiting_manual_confirmation":
            order.status = "awaiting_manual_confirmation"
        audit.record(s, "payment.checkout", p.actor, org_id=p.org_id, target=order.id, detail={"provider": provider.name})
        return {**result, "order": payments.order_view(order)}


@router.post("/payments/{provider_name}/webhook")
async def webhook(request: Request, provider_name: str):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    app_ctx = ctx(request)

    def work():
        provider = payments.get_provider(provider_name, app_ctx.settings)
        try:
            event = provider.parse_webhook(body, headers)
        except ApiError as exc:
            if exc.code.startswith("signature"):
                with app_ctx.db.transaction() as s:
                    audit.record(s, "payment.webhook_rejected", f"webhook:{provider_name}",
                                 detail={"code": exc.code}, ip=client_ip(request))
            raise
        with app_ctx.db.transaction() as s:
            return payments.handle_webhook(s, provider, event, client_ip(request))

    return await run_in_threadpool(work)
