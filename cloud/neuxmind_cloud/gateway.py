"""Inference gateway: reserve → call upstream → settle on the provider's usage.

Each request runs in up to three short transactions so that no database lock
is held while the upstream model is working:

1. **reserve** — auth, entitlement, idempotency, reservation of the worst-case
   cost, a ``pending`` usage record.
2. *(no transaction)* — upstream call.
3. **settle** — charge the provider-reported usage and release the rest; or
   release on a definite upstream rejection; or keep the hold and flag
   ``reconciliation_required`` when the outcome is unknown or exceeds the hold.

Metered sessions (``/gateway/sessions``) follow the desktop
``/v1/agent-sessions`` protocol (see docs/ENGINEERING_ENGINE.md): the session
reserves the full token budget once, each call through the session's
``base_url`` is reduced to allow-listed parameters (session_params.py) with
the output limit written by the server from the remaining budget, and
``close`` settles the sum.

Overage rule (iteration-01 fix, replaces the "overage is not charged" half of
ADR-007; to be recorded as a new ADR by the lead): when the provider reports more usage than
a call's hold, the call's output is **not returned**, the user is charged
exactly the hold (the reserved cap, never more), and the excess is written to
``overage_records`` for operator review (waive, or charge from available
credits). A session with an overage call refuses further calls.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, catalog, ledger, streaming
from .auth import Principal
from .config import Settings
from .db import Database, lock_wallet
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized
from .models import Entitlement, GatewaySession, ModelCard, OverageRecord, Reservation, UsageRecord, new_id, now
from .security import new_gateway_token, token_hash
from .session_params import MAX_SESSION_TOKENS, sanitize, scoped_cache_key, with_output_limit
from .upstream import UpstreamError, UpstreamRejected, UpstreamResult, UpstreamUnknown

MAX_INFER_TOKENS = 200_000

ProviderFactory = Callable[[dict], object]


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _int(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise bad_request(f"{label}须为 {low}–{high} 的整数")
    return value


def _request_id(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise bad_request("request_id 须为 1–200 个字符", code="request_id_required")
    return value.strip()


def normalize_input(value) -> list[dict]:
    """Accept a plain string or OpenAI-style ``[{role, content}]`` messages."""
    if isinstance(value, str):
        if not value or len(value) > 400_000:
            raise bad_request("input 须为 1–400000 个字符")
        return [{"role": "user", "content": value}]
    if isinstance(value, list) and 0 < len(value) <= 200:
        out = []
        for message in value:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant") \
                    or not isinstance(message.get("content"), str):
                raise bad_request("messages 每项须为 {role: system|user|assistant, content: string}")
            out.append({"role": message["role"], "content": message["content"]})
        return out
    raise bad_request("input 须为字符串或消息列表")


def _gateway_model(session: Session, org_id: str, model_id) -> ModelCard:
    card = session.get(ModelCard, model_id) if isinstance(model_id, str) else None
    if card is None or card.status != "published":
        raise not_found("模型不存在或已下架", code="model_not_found")
    if card.upstream is None:
        raise bad_request("该模型没有云端推理网关，请下载端侧部署包", code="model_not_hosted")
    entitled = session.execute(select(Entitlement).where(Entitlement.org_id == org_id, Entitlement.model_id == card.id,
                                                         Entitlement.revoked_at.is_(None))).scalar_one_or_none()
    if entitled is None:
        raise forbidden("尚未获得该模型授权，请先通过 /purchases 开通", code="entitlement_required")
    return card


def _usage_view(record: UsageRecord) -> dict:
    return {"id": record.id, "request_id": record.request_id, "model": record.model_id, "status": record.status,
            "reservation_id": record.reservation_id, "session_id": record.session_id,
            "usage": {"prompt_tokens": record.prompt_tokens, "completion_tokens": record.completion_tokens,
                      "total_tokens": record.total_tokens},
            "credits": record.credits, "upstream_ref": record.upstream_ref, "simulated": record.simulated,
            "error": record.error, "created_at": record.created_at}


def usage_view(record: UsageRecord) -> dict:
    return _usage_view(record)


# ----------------------------------------------------------------- helpers
def _call_upstream(fn) -> object:
    try:
        return fn()
    except (UpstreamRejected, UpstreamUnknown) as exc:
        return exc
    except Exception as exc:  # unexpected bug after the request may have left: treat as unknown
        return UpstreamUnknown("网关内部错误，结果未知", f"internal: {type(exc).__name__}: {exc}")


def _audit_upstream(s: Session, code: str, principal_actor: str, org_id: str, target: str, outcome) -> None:
    detail = {"detail": outcome.detail[:500]} if isinstance(outcome, UpstreamError) else {}
    audit.record(s, f"gateway.{code}", principal_actor, org_id=org_id, target=target, detail=detail)


def _record_overage(s: Session, *, org_id: str, reservation: Reservation, record: UsageRecord, session_id: str | None,
                    reported: int, charged_tokens: int, upstream_ref: str) -> OverageRecord:
    pricing = reservation.pricing
    excess_credits = max(0, catalog.cost_for_tokens(pricing, reported) - catalog.cost_for_tokens(pricing, charged_tokens)) \
        if pricing.get("type") == "per_1k_tokens" else 0
    row = OverageRecord(org_id=org_id, reservation_id=reservation.id, usage_id=record.id, session_id=session_id,
                        model_id=record.model_id, upstream_ref=upstream_ref, reported_tokens=reported,
                        charged_tokens=charged_tokens, overage_tokens=reported - charged_tokens,
                        overage_credits=excess_credits, status="open")
    s.add(row)
    s.flush()
    audit.record(s, "gateway.overage_recorded", "system:gateway", org_id=org_id, target=row.id,
                 detail={"reported_tokens": reported, "charged_tokens": charged_tokens,
                         "overage_credits": excess_credits, "reservation_id": reservation.id})
    return row


# ----------------------------------------------------------------- /infer
def infer(db: Database, settings: Settings, principal: Principal, payload: dict, provider_factory: ProviderFactory) -> dict:
    principal.require("gateway").require_verified()
    if not isinstance(payload, dict):
        raise bad_request("请求体须为对象")
    messages = normalize_input(payload.get("input"))
    max_tokens = _int(payload.get("max_tokens"), "max_tokens", 1, MAX_INFER_TOKENS)
    request_id = _request_id(payload.get("request_id") or new_id("req"))
    request_digest = digest({"model": payload.get("model"), "input": messages, "max_tokens": max_tokens})
    input_bytes = sum(len(m["content"].encode()) for m in messages)

    # -- 1. reserve ---------------------------------------------------------
    with db.transaction() as s:
        existing = s.execute(select(UsageRecord).where(UsageRecord.org_id == principal.org_id,
                                                       UsageRecord.request_id == request_id)).scalar_one_or_none()
        if existing is not None:
            if existing.request_digest != request_digest:
                raise conflict("request_id 已用于不同的请求", code="request_id_reused")
            if existing.status == "succeeded" and existing.response is not None:
                return {**existing.response, "replayed": True}
            # Unknown/failed outcomes are never replayed automatically.
            raise ApiError(409, "request_not_replayable", f"该请求状态为 {existing.status}，不会自动重放",
                           request_status=existing.status)
        card = _gateway_model(s, principal.org_id, payload.get("model"))
        bound = catalog.input_token_bound(input_bytes, len(messages)) + max_tokens
        amount = catalog.cost_for_tokens(card.pricing, bound)
        reservation = ledger.reserve(s, principal.org_id, amount, purpose="infer", model_id=card.id,
                                     pricing=dict(card.pricing), note=f"request {request_id}; token bound {bound}")
        record = UsageRecord(org_id=principal.org_id, request_id=request_id, request_digest=request_digest,
                             model_id=card.id, reservation_id=reservation.id, api_key_id=principal.api_key_id,
                             status="pending", hold_tokens=bound)
        s.add(record)
        s.flush()
        record_id, reservation_id, upstream, pricing = record.id, reservation.id, dict(card.upstream), dict(card.pricing)

    # -- 2. upstream ----------------------------------------------------------
    outcome = _call_upstream(lambda: provider_factory(upstream).complete(messages, max_tokens))

    # -- 3. settle --------------------------------------------------------------
    with db.transaction() as s:
        record = s.get(UsageRecord, record_id)
        reservation = ledger._locked_reservation(s, reservation_id)
        s.refresh(record)
        if reservation.status not in ledger.HOLDING_STATUSES:
            # An operator already settled/released this hold (after the sweeper
            # flagged it as lost). Keep the late evidence, return nothing.
            if isinstance(outcome, UpstreamResult):
                record.prompt_tokens, record.completion_tokens = outcome.prompt_tokens, outcome.completion_tokens
                record.total_tokens, record.upstream_ref = outcome.total_tokens, outcome.upstream_ref
            record.error = "结果在预留已被人工处理后才到达，未返回、未再计费"
            error = ApiError(409, "reservation_already_resolved", record.error, reservation_id=reservation_id,
                             request_id=request_id)
            _audit_upstream(s, error.code, principal.actor, principal.org_id, reservation_id, outcome)
            response = None
        elif isinstance(outcome, UpstreamRejected):
            ledger.release(s, reservation_id, "gateway", note=outcome.public)
            record.status, record.error = "failed", outcome.public[:2000]
            error = ApiError(502, "upstream_error", f"上游调用失败，未扣费：{outcome.public}", reservation_id=reservation_id)
        elif isinstance(outcome, UpstreamUnknown):
            ledger.mark_reconciliation(s, reservation_id, outcome.public)
            record.status, record.error = "reconciliation_required", outcome.public[:2000]
            error = ApiError(502, "upstream_unknown", f"上游结果未知，预留保留待核对：{outcome.public}",
                             reservation_id=reservation_id, request_id=request_id)
        else:
            error = None
            credits = catalog.cost_for_tokens(pricing, outcome.total_tokens)
            record.prompt_tokens, record.completion_tokens = outcome.prompt_tokens, outcome.completion_tokens
            record.total_tokens, record.upstream_ref, record.simulated = outcome.total_tokens, outcome.upstream_ref, outcome.simulated
            if credits > reservation.amount:
                # Charge the cap, withhold the output, record the excess (new billing ADR).
                ledger.settle(s, reservation_id, reservation.amount, outcome.upstream_ref, "gateway",
                              detail={"total_tokens": outcome.total_tokens, "overage": True,
                                      "simulated": outcome.simulated})
                overage = _record_overage(s, org_id=principal.org_id, reservation=reservation, record=record,
                                          session_id=None, reported=outcome.total_tokens,
                                          charged_tokens=record.hold_tokens or 0, upstream_ref=outcome.upstream_ref)
                record.status, record.credits = "overage", reservation.amount
                record.error = "上游报告用量超过预留：已按预留上限计费，输出未返回，超出部分待核对"
                error = ApiError(502, "usage_exceeds_reservation", record.error, reservation_id=reservation_id,
                                 request_id=request_id, credits_charged=reservation.amount, overage_id=overage.id)
            else:
                ledger.settle(s, reservation_id, credits, outcome.upstream_ref, "gateway",
                              detail={"total_tokens": outcome.total_tokens, "simulated": outcome.simulated})
                record.status, record.credits = "succeeded", credits
                record.response = {
                    "id": record.id, "request_id": request_id, "model": record.model_id,
                    "output": {"text": outcome.text},
                    "usage": {"prompt_tokens": outcome.prompt_tokens, "completion_tokens": outcome.completion_tokens,
                              "total_tokens": outcome.total_tokens},
                    "credits_charged": credits, "credits_reserved": reservation.amount, "reservation_id": reservation_id,
                    "simulated": outcome.simulated}
        if error is not None and error.code != "reservation_already_resolved":
            _audit_upstream(s, error.code, principal.actor, principal.org_id, reservation_id, outcome)
        response = record.response
    if error is not None:
        raise error
    return response


# --------------------------------------------------------------- sessions
def _session_view(gws: GatewaySession, settings: Settings, access_token: str | None, reserved: int) -> dict:
    return {"session_id": gws.id, "request_id": gws.request_id, "model": gws.model_id,
            "base_url": f"{settings.public_base_url}/api/v1/gateway/sessions/{gws.id}/v1",
            "access_token": access_token, "expires_at": gws.expires_at, "max_tokens": gws.max_tokens,
            "reservation_id": gws.reservation_id, "reserved_credits": reserved, "status": gws.status}


def create_session(db: Database, settings: Settings, principal: Principal, payload: dict) -> dict:
    """``POST /gateway/sessions`` ≙ desktop ``POST /v1/agent-sessions``."""
    principal.require("gateway").require_verified()
    if not isinstance(payload, dict):
        raise bad_request("请求体须为对象")
    request_id = _request_id(payload.get("request_id"))
    max_tokens = _int(payload.get("max_tokens"), "max_tokens", 1, MAX_SESSION_TOKENS)
    max_minutes = _int(payload.get("max_minutes", 15), "max_minutes", 1, 120)
    max_units = payload.get("max_units")
    if max_units is not None:
        _int(max_units, "max_units", math.ceil(max_tokens / 1000), 10 ** 9)
    request_digest = digest({"model": payload.get("model"), "max_tokens": max_tokens, "max_minutes": max_minutes,
                             "max_units": max_units})
    with db.transaction() as s:
        existing = s.execute(select(GatewaySession).where(GatewaySession.org_id == principal.org_id,
                                                          GatewaySession.request_id == request_id)).scalar_one_or_none()
        if existing is not None:
            if existing.request_digest != request_digest:
                raise conflict("request_id 已用于不同的会话参数", code="request_id_reused")
            reservation = s.get(Reservation, existing.reservation_id)
            # Same request replayed: same session, but the token was shown once and is not stored.
            return {**_session_view(existing, settings, None, reservation.amount), "replayed": True}
        card = _gateway_model(s, principal.org_id, payload.get("model"))
        if card.pricing["type"] not in ("per_1k_tokens", "free"):
            raise bad_request("计量会话只支持 per_1k_tokens 或 free 计价的模型", code="pricing_not_session_compatible")
        amount = catalog.cost_for_tokens(card.pricing, max_tokens)
        reservation = ledger.reserve(s, principal.org_id, amount, purpose="session", model_id=card.id,
                                     pricing=dict(card.pricing), note=f"session request {request_id}")
        token = new_gateway_token()
        gws = GatewaySession(org_id=principal.org_id, request_id=request_id, request_digest=request_digest,
                             model_id=card.id, reservation_id=reservation.id, access_token_hash=token_hash(token),
                             max_tokens=max_tokens, expires_at=now() + max_minutes * 60, evidence=[])
        s.add(gws)
        s.flush()
        audit.record(s, "gateway.session_open", principal.actor, org_id=principal.org_id, target=gws.id,
                     detail={"max_tokens": max_tokens, "reserved": amount})
        return _session_view(gws, settings, token, amount)


@dataclass
class _SessionCall:
    """A session call that passed the reserve transaction (its hold is in ``inflight_tokens``)."""
    session_id: str
    org_id: str
    record_id: str
    path: str
    hold: int
    out: int
    upstream: dict
    outbound: dict
    stream: bool


def _begin_session_call(db: Database, session_id: str, authorization: str | None, path: str,
                        body: dict) -> _SessionCall:
    """Auth, allow-list, and the short reserve transaction of a session call.

    N-05 (rereview): a session has **at most one call in flight**. A second
    concurrent call (streamed or not) gets 409 ``session_busy``. Together with
    the overage lock this bounds what a session can cost beyond its hold to a
    single call, whatever the client's concurrency.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized("需要会话访问令牌")
    presented = token_hash(authorization[7:].strip())
    forward, input_bound, requested_out, stream = sanitize(path, body)

    with db.transaction() as s:
        gws = s.get(GatewaySession, session_id)
        if gws is None or gws.access_token_hash != presented:
            raise unauthorized("会话访问令牌无效", code="invalid_session_token")
        lock_wallet(s, gws.org_id)  # serialises budget accounting for this org
        s.refresh(gws)
        if gws.status != "open":
            raise conflict("会话已关闭", code="session_closed")
        if gws.expires_at <= now():
            raise ApiError(403, "session_expired", "会话已超过时间上限")
        if gws.overage_calls:
            raise conflict("该会话有一次调用的上游用量超过预留，已锁定；请关闭会话", code="session_overage_locked")
        if gws.inflight_tokens > 0:
            raise conflict("该会话已有一个进行中的调用（同一会话同一时刻只允许一个在途调用），请等它结束后再试",
                           code="session_busy")
        remaining = gws.max_tokens - gws.used_tokens - gws.inflight_tokens
        if remaining <= input_bound:
            raise ApiError(402, "session_budget_exhausted", "会话剩余 token 额度不足以容纳本次输入", remaining=remaining)
        budget_out = remaining - input_bound
        out = min(requested_out or budget_out, budget_out)
        hold = input_bound + out
        card = s.get(ModelCard, gws.model_id)
        if card is None or card.status != "published" or card.upstream is None:
            raise ApiError(409, "model_unavailable", "该模型已下架或不再提供云端推理，请关闭会话")
        gws.inflight_tokens += hold
        upstream, org_id = dict(card.upstream), gws.org_id
        outbound = with_output_limit(path, forward, out, upstream.get("output_param", "max_tokens"), stream)
        if path == "/responses":
            # Review Q3-15: always written (default key per organisation), so
            # requests without a key are not prefix-cached across tenants either.
            outbound["prompt_cache_key"] = scoped_cache_key(org_id, outbound.get("prompt_cache_key") or "default")
        record = UsageRecord(org_id=org_id, request_id=new_id("call"), request_digest=digest(outbound),
                             model_id=gws.model_id, session_id=gws.id, status="pending", hold_tokens=hold)
        s.add(record)
        s.flush()
        return _SessionCall(session_id=session_id, org_id=org_id, record_id=record.id, path=path, hold=hold, out=out,
                            upstream=upstream, outbound=outbound, stream=stream)


def _settle_session_call(db: Database, call: _SessionCall, outcome) -> tuple[ApiError | None, int, int]:
    """Settle a non-streamed call, or a streamed one that failed before its first byte."""
    with db.transaction() as s:
        lock_wallet(s, call.org_id)
        gws = s.get(GatewaySession, call.session_id)
        s.refresh(gws)
        record = s.get(UsageRecord, call.record_id)
        s.refresh(record)
        actor = f"gateway_session:{call.session_id}"
        hold = call.hold
        if record.status != "pending":
            # The sweeper declared this call lost (and already released its
            # hold and counted it as unknown). Keep late evidence, return nothing.
            if isinstance(outcome, UpstreamResult):
                record.prompt_tokens, record.completion_tokens = outcome.prompt_tokens, outcome.completion_tokens
                record.total_tokens, record.upstream_ref = outcome.total_tokens, outcome.upstream_ref
            record.error = "调用结果在被判定丢失后才到达，未返回；已转人工核对"
            _audit_upstream(s, "call_marked_lost", actor, call.org_id, record.id, outcome)
            error = ApiError(409, "call_marked_lost", record.error)
        else:
            gws.inflight_tokens -= hold
            if isinstance(outcome, UpstreamRejected):
                record.status, record.error = "failed", outcome.public[:2000]
                error = ApiError(502, "upstream_error", f"上游调用失败，未计入用量：{outcome.public}")
            elif isinstance(outcome, UpstreamUnknown):
                gws.unknown_calls += 1
                record.status, record.error = "reconciliation_required", outcome.public[:2000]
                error = ApiError(502, "upstream_unknown", f"上游结果未知，会话关闭时将转人工核对：{outcome.public}")
            else:
                record.prompt_tokens, record.completion_tokens = outcome.prompt_tokens, outcome.completion_tokens
                record.total_tokens, record.upstream_ref, record.simulated = outcome.total_tokens, outcome.upstream_ref, outcome.simulated
                gws.calls += 1
                gws.evidence = [*gws.evidence, outcome.upstream_ref]
                if outcome.total_tokens > hold:
                    # Charge the hold, withhold the output, record the excess, lock the session.
                    gws.used_tokens += hold
                    gws.overage_calls += 1
                    gws.overage_tokens += outcome.total_tokens - hold
                    record.status = "overage"
                    record.error = "上游报告用量超过本次调用的预留：已按预留上限计入会话用量，输出未返回，超出部分待核对"
                    reservation = s.get(Reservation, gws.reservation_id)
                    overage = _record_overage(s, org_id=call.org_id, reservation=reservation, record=record,
                                              session_id=gws.id, reported=outcome.total_tokens, charged_tokens=hold,
                                              upstream_ref=outcome.upstream_ref)
                    error = ApiError(502, "usage_exceeds_reservation", record.error, charged_tokens=hold,
                                     reported_tokens=outcome.total_tokens, overage_id=overage.id)
                else:
                    error = None
                    gws.used_tokens += outcome.total_tokens
                    record.status = "succeeded"
            if error is not None:
                _audit_upstream(s, error.code, actor, call.org_id, record.id, outcome)
        return error, gws.used_tokens, gws.max_tokens


# In-stream terminal error codes (see cloud/README.md「会话流式」).
_STREAM_FAILURES = {
    "output_limit": ("output_limit_reached",
                     "上游输出即将超过本次调用的服务端输出上限，网关已截断；本次按预留上限计入会话用量，待核对"),
    "interrupted": ("upstream_interrupted", "上游流式连接在 usage 之前中断；本次按预留上限计入会话用量，待核对"),
    "usage_missing": ("usage_missing", "上游流结束但没有 usage 计量；本次按预留上限计入会话用量，待核对"),
    "deadline": ("stream_duration_exceeded", "流式调用超过最长时长，网关已结束；本次按预留上限计入会话用量，待核对"),
    "internal": ("internal_error", "网关内部错误；本次按预留上限计入会话用量，待核对"),
}


def _settle_stream(db: Database, call: _SessionCall, o: streaming.StreamOutcome) -> tuple[str, dict]:
    """Settlement of a streamed call (one short transaction, run by the producer
    thread after the upstream stream ended). Returns the terminal SSE event."""
    with db.transaction() as s:
        lock_wallet(s, call.org_id)
        gws = s.get(GatewaySession, call.session_id)
        s.refresh(gws)
        record = s.get(UsageRecord, call.record_id)
        s.refresh(record)
        actor = f"gateway_session:{call.session_id}"
        hold = call.hold
        ref = f"openai_compatible:{o.upstream_ref}" if o.upstream_ref and not o.simulated else (
            f"echo:{o.upstream_ref}" if o.upstream_ref else "")
        evidence = {"termination": o.termination, "forwarded_events": o.events, "forwarded_bytes": o.forwarded_bytes,
                    "output_lower_bound": o.lower_bound, "client_disconnected": o.client_disconnected,
                    "upstream_error_event": o.upstream_error, "detail": o.detail}
        usage_view = None
        if o.usage is not None:
            usage_view = {"prompt_tokens": o.usage.prompt_tokens, "completion_tokens": o.usage.completion_tokens,
                          "total_tokens": o.usage.total_tokens}
            record.prompt_tokens, record.completion_tokens = o.usage.prompt_tokens, o.usage.completion_tokens
            record.total_tokens = o.usage.total_tokens
        if ref:
            record.upstream_ref = ref[:300]
        record.simulated = o.simulated

        def metering(status: str, call_tokens: int | None) -> dict:
            return {"type": "neuxmind.metering", "session_id": call.session_id, "status": status,
                    "call_tokens": call_tokens, "used_tokens": gws.used_tokens, "max_tokens": gws.max_tokens,
                    "output_limit": call.out, "hold_tokens": hold, "usage": usage_view,
                    "output_lower_bound": o.lower_bound, "upstream_ref": ref or None,
                    "client_disconnected": o.client_disconnected, "simulated": o.simulated}

        if record.status != "pending":
            record.error = "流式调用在被判定丢失后才结束；未再计量，已转人工核对"
            audit.record(s, "gateway.call_marked_lost", actor, org_id=call.org_id, target=record.id, detail=evidence)
            return "error", streaming.error_payload("call_marked_lost", record.error, metering("call_marked_lost", None))
        gws.inflight_tokens -= hold
        if o.termination == "completed" and o.usage is not None:
            completion = max(o.usage.completion_tokens, o.lower_bound)
            total = max(o.usage.total_tokens, o.usage.prompt_tokens + completion)
            record.total_tokens = total
            gws.calls += 1
            if ref:
                gws.evidence = [*gws.evidence, ref]
            if total > hold:
                # Output already reached the client (it cannot be withheld in a
                # stream); the money rule of ADR-012 still holds: charge the hold,
                # record the excess, lock the session.
                gws.used_tokens += hold
                gws.overage_calls += 1
                gws.overage_tokens += total - hold
                record.status = "overage"
                record.error = "上游报告用量超过本次调用的预留：已按预留上限计入会话用量，超出部分待核对（流式输出已送达，无法撤回）"
                reservation = s.get(Reservation, gws.reservation_id)
                overage = _record_overage(s, org_id=call.org_id, reservation=reservation, record=record,
                                          session_id=gws.id, reported=total, charged_tokens=hold,
                                          upstream_ref=ref or "stream")
                audit.record(s, "gateway.usage_exceeds_reservation", actor, org_id=call.org_id, target=record.id,
                             detail=evidence)
                payload = streaming.error_payload("usage_exceeds_reservation", record.error, metering("overage", hold))
                payload.update(charged_tokens=hold, reported_tokens=total, overage_id=overage.id)
                return "error", payload
            gws.used_tokens += total
            record.status = "succeeded"
            if o.client_disconnected:
                audit.record(s, "gateway.stream_client_disconnected", actor, org_id=call.org_id, target=record.id,
                             detail=evidence)
            return "neuxmind.metering", metering("succeeded", total)
        # No usable usage: count the whole hold (never more), flag for reconciliation (ADR-012).
        code, message = _STREAM_FAILURES.get(o.termination, _STREAM_FAILURES["interrupted"])
        if o.termination in ("interrupted", "usage_missing") and o.upstream_error:
            code = "upstream_stream_error"
            message = "上游在流中返回错误且没有 usage；本次按预留上限计入会话用量，待核对"
        gws.used_tokens += hold
        gws.unknown_calls += 1
        record.status, record.error = "reconciliation_required", message
        audit.record(s, f"gateway.stream_{code}", actor, org_id=call.org_id, target=record.id, detail=evidence)
        return "error", streaming.error_payload(code, message, metering("reconciliation_required", hold))


def session_call(db: Database, settings: Settings, session_id: str, authorization: str | None, path: str,
                 body: dict, provider_factory: ProviderFactory):
    """A model call made with the session's limited access token.

    Only allow-listed parameters reach the upstream; ``n`` is 1; the output
    limit is ``min(client value, remaining budget − input bound)`` and is
    always written by the server. See session_params.py.

    Returns the JSON response, or — for ``stream: true`` — a
    :class:`streaming.SessionStream` whose producer the route starts.
    """
    call = _begin_session_call(db, session_id, authorization, path, body)

    if call.stream:
        def opener():
            provider = provider_factory(call.upstream)
            if not hasattr(provider, "open_stream"):
                raise UpstreamRejected("该上游不支持流式", "provider has no open_stream")
            return provider.open_stream(call.path, call.outbound)

        opened = _call_upstream(opener)
        if isinstance(opened, UpstreamError):
            # Nothing was streamed: same rules (and HTTP errors) as a non-streamed call.
            error, _used, _max = _settle_session_call(db, call, opened)
            raise error
        meter = streaming.StreamMeter(call.path, call.out)
        simulated = bool(getattr(opened, "simulated", False))
        return streaming.SessionStream(
            opened, meter, lambda outcome: _settle_stream(db, call, outcome),
            max_seconds=settings.effective_stream_max_seconds, idle_seconds=settings.stream_idle_seconds,
            upstream_idle_seconds=settings.upstream_timeout_seconds,
            headers={"X-NeuxMind-Session": session_id, "X-NeuxMind-Output-Limit": str(call.out),
                     "X-NeuxMind-Simulated": "true" if simulated else "false"})

    outcome = _call_upstream(lambda: provider_factory(call.upstream).passthrough(call.path, call.outbound))
    data = None
    if isinstance(outcome, tuple):
        data, outcome = outcome
    error, used, maximum = _settle_session_call(db, call, outcome)
    if error is not None:
        raise error
    return {**data, "neuxmind": {"session_id": session_id, "used_tokens": used, "max_tokens": maximum,
                                 "output_limit": call.out, "simulated": outcome.simulated}}


def close_locked(s: Session, gws: GatewaySession, request_id: str, actor: str) -> dict:
    """Close a session whose org wallet is already locked and which has no call in flight.

    Settles ``used_tokens`` (always ≤ max_tokens, so ≤ the reservation) at the
    reservation's price snapshot, or keeps the hold for reconciliation when any
    call's outcome is unknown. Overage calls were charged at their hold and are
    listed in ``overage_records``; they do not block settlement.
    """
    reservation = s.get(Reservation, gws.reservation_id)
    evidence_ref = f"gateway_session:{gws.id}:calls={gws.calls}"
    base = {"session_id": gws.id, "usage_tokens": gws.used_tokens,
            "usage_units": math.ceil(gws.used_tokens / 1000), "usage_evidence_ref": evidence_ref,
            "reservation_id": gws.reservation_id, "overage_calls": gws.overage_calls,
            "overage_tokens": gws.overage_tokens}
    credits = catalog.cost_for_tokens(reservation.pricing, gws.used_tokens)
    if gws.unknown_calls or gws.used_tokens > gws.max_tokens or credits > reservation.amount:
        ledger.mark_reconciliation(s, reservation.id, f"session {gws.id}: unknown={gws.unknown_calls} used={gws.used_tokens}")
        result = {**base, "status": "reconciliation_required", "metering_status": "reconciliation_required",
                  "credits_charged": None}
        gws.status = "reconciliation_required"
    else:
        ledger.settle(s, reservation.id, credits, evidence_ref, "gateway",
                      detail={"session_id": gws.id, "upstream_refs": gws.evidence[-50:],
                              "overage_calls": gws.overage_calls})
        result = {**base, "status": "closed", "metering_status": "settled", "credits_charged": credits}
        gws.status = "closed"
    gws.close_request_id = request_id
    gws.close_result = result
    audit.record(s, "gateway.session_close", actor, org_id=gws.org_id, target=gws.id,
                 detail={"status": result["status"], "used_tokens": gws.used_tokens})
    return result


def close_session(db: Database, principal: Principal, session_id: str, payload: dict) -> dict:
    """``POST /gateway/sessions/{id}/close`` ≙ desktop ``.../agent-sessions/{id}/close``.

    Returns ``status=closed`` with ``usage_units`` (1k tokens, rounded up) and
    ``usage_evidence_ref`` when the server's own metering is complete; otherwise
    ``status=reconciliation_required`` and the reservation stays held.
    Closing does not require a verified e-mail (the money is already reserved).
    """
    principal.require("gateway")
    request_id = _request_id((payload or {}).get("request_id"))
    with db.transaction() as s:
        gws = s.get(GatewaySession, session_id)
        if gws is None or gws.org_id != principal.org_id:
            raise not_found("会话不存在")
        lock_wallet(s, gws.org_id)
        s.refresh(gws)
        if gws.close_result is not None:
            return {**gws.close_result, "replayed": True}
        if gws.inflight_tokens > 0:
            raise conflict("会话仍有进行中的调用，请稍后关闭", code="session_busy")
        return close_locked(s, gws, request_id, principal.actor)
