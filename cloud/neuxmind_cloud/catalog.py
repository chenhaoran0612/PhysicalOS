"""Model marketplace: model-card validation, public views and capability filtering.

Capability IDs and manifest checks come from ``spec/neuxmind_spec`` — the
cloud never keeps its own list.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import bad_request
from .models import ModelCard
from .spec_bridge import capabilities, manifest

SOURCES = ("native", "open", "closed")
KINDS = ("llm", "vla", "vision", "speech", "policy")
PRICING_TYPES = ("per_1k_tokens", "per_call", "license_once", "free")
GATEWAY_PRICING = ("per_1k_tokens", "per_call", "free")
UPSTREAM_PROVIDERS = ("openai_compatible", "echo")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,100}$")


def _text(value, label: str, max_len: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > max_len:
        raise bad_request(f"{label}需要 {'1' if required else '0'}–{max_len} 个字符")
    return value.strip()


def _int(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise bad_request(f"{label}须为 {low}–{high} 的整数")
    return value


def validate_pricing(p) -> dict:
    if not isinstance(p, dict) or p.get("type") not in PRICING_TYPES:
        raise bad_request(f"pricing.type 须为 {PRICING_TYPES} 之一")
    if p["type"] == "free":
        return {"type": "free", "credits": 0}
    return {"type": p["type"], "credits": _int(p.get("credits"), "pricing.credits", 1, 10 ** 9)}


def validate_capabilities(items) -> list[str]:
    """Normalise to standard capability IDs; unknown IDs are rejected by the spec."""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 50:
        raise bad_request("requires_capabilities 须为列表（≤50 项）")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise bad_request("requires_capabilities 每项须为能力 ID 字符串")
        try:
            cid = capabilities.normalize(item)
            if not cid.startswith("x."):
                capabilities.get(cid)
        except ValueError as exc:
            raise bad_request(str(exc), code="unknown_capability") from None
        if cid not in out:
            out.append(cid)
    return out


def validate_upstream(u, *, allow_insecure: bool, test_env: bool = False) -> dict:
    if not isinstance(u, dict) or u.get("provider") not in UPSTREAM_PROVIDERS:
        raise bad_request(f"upstream.provider 须为 {UPSTREAM_PROVIDERS} 之一")
    if u["provider"] == "echo":
        if not test_env:
            # Review finding C-09: refuse the test double at publish time, not only at call time.
            raise bad_request("echo 是测试替身上游，只能在 dev/test 环境上架", code="upstream_not_allowed")
        behavior = u.get("behavior", "ok")
        if behavior not in ("ok", "timeout", "error", "overuse", "no_usage", "stream_cut", "usage_overrun", "tools"):
            raise bad_request("echo.behavior 无效")
        tool = {}
        if u.get("tool_name") is not None:
            name = u["tool_name"]
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                raise bad_request("echo.tool_name 须为 1–64 字符的函数名")
            tool["tool_name"] = name
        if u.get("tool_arguments") is not None:
            if not isinstance(u["tool_arguments"], dict) or len(json.dumps(u["tool_arguments"])) > 4000:
                raise bad_request("echo.tool_arguments 须为 ≤4000 字节的对象")
            tool["tool_arguments"] = u["tool_arguments"]
        delay = u.get("stream_delay", 0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 5:
            raise bad_request("echo.stream_delay 须为 0–5 秒")
        return {"provider": "echo", "behavior": behavior, **({"stream_delay": delay} if delay else {}), **tool}
    base_url = _text(u.get("base_url"), "upstream.base_url", 500)
    parsed = urlparse(base_url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise bad_request("upstream.base_url 无效")
    if parsed.scheme != "https" and not (loopback or allow_insecure):
        raise bad_request("非回环上游地址必须使用 HTTPS")
    api = u.get("api", "chat_completions")
    if api not in ("chat_completions", "responses"):
        raise bad_request("upstream.api 须为 chat_completions 或 responses")
    credential_ref = u.get("credential_ref")
    if credential_ref is not None and (not isinstance(credential_ref, str) or not _ENV_NAME.match(credential_ref)):
        raise bad_request("upstream.credential_ref 须为环境变量名（大写字母、数字、下划线）")
    if "api_key" in u or "token" in u:
        raise bad_request("上游凭据不得写入模型卡；请设置环境变量并用 credential_ref 引用", code="secret_in_card")
    # Which chat output-limit field the upstream honours. The gateway writes it
    # (from the remaining budget) on every call and strips the other one.
    output_param = u.get("output_param", "max_tokens")
    if output_param not in ("max_tokens", "max_completion_tokens", "both"):
        raise bad_request("upstream.output_param 须为 max_tokens / max_completion_tokens / both")
    return {"provider": "openai_compatible", "base_url": base_url.rstrip("/"), "api": api,
            "model": _text(u.get("model"), "upstream.model", 200), "credential_ref": credential_ref,
            "output_param": output_param}


def validate_card(p: dict, *, allow_insecure_upstream: bool, existing: ModelCard | None = None,
                  test_env: bool = False) -> dict:
    """Validate an operator-supplied model card. ``existing`` enables partial updates."""
    if not isinstance(p, dict):
        raise bad_request("模型卡须为对象")
    merged = {}
    if existing is not None:
        merged = {"id": existing.id, "name": existing.name, "version": existing.version, "source": existing.source,
                  "kind": existing.kind, "description": existing.description, "license": existing.license,
                  "pricing": existing.pricing, "package": existing.package, "upstream": existing.upstream,
                  "requires_capabilities": existing.requires_capabilities, "families": existing.families}
        if "id" in p and p["id"] != existing.id:
            raise bad_request("模型 ID 不可修改")
    merged.update(p)
    model_id = _text(merged.get("id"), "模型 ID", 100)
    if not _MODEL_ID.match(model_id):
        raise bad_request("模型 ID 只能包含小写字母、数字、点、下划线和连字符")
    if merged.get("source") not in SOURCES:
        raise bad_request(f"source 须为 {SOURCES} 之一")
    if merged.get("kind") not in KINDS:
        raise bad_request(f"kind 须为 {KINDS} 之一")
    out = {"id": model_id, "name": _text(merged.get("name"), "名称", 100),
           "version": _text(merged.get("version", "1.0.0"), "版本", 50),
           "source": merged["source"], "kind": merged["kind"],
           "description": _text(merged.get("description", ""), "说明", 4000, required=False),
           "license": _text(merged.get("license"), "许可", 2000),
           "pricing": validate_pricing(merged.get("pricing")),
           "requires_capabilities": validate_capabilities(merged.get("requires_capabilities")),
           "families": merged.get("families") or [], "package": None, "upstream": None}
    if not isinstance(out["families"], list) or any(f not in manifest.FAMILIES for f in out["families"]):
        raise bad_request(f"families 须为 {manifest.FAMILIES} 的子集")
    package = merged.get("package")
    if out["source"] in ("native", "open") and package is not None:
        if not isinstance(package, dict):
            raise bad_request("package 须为对象 {ref, sha256, runtime}")
        sha = _text(package.get("sha256"), "package.sha256", 64).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise bad_request("package.sha256 须为 64 位十六进制 SHA-256")
        out["package"] = {"ref": _text(package.get("ref"), "package.ref", 1000), "sha256": sha,
                          "runtime": _text(package.get("runtime"), "package.runtime", 100)}
    elif out["source"] == "closed" and package is not None:
        raise bad_request("闭源模型不提供部署包")
    upstream = merged.get("upstream")
    if upstream is not None:
        out["upstream"] = validate_upstream(upstream, allow_insecure=allow_insecure_upstream, test_env=test_env)
    if out["source"] == "closed" and out["upstream"] is None:
        raise bad_request("闭源模型必须绑定上游网关（upstream）")
    if out["source"] in ("native", "open") and out["package"] is None and out["upstream"] is None:
        raise bad_request("自研/开源模型需要部署包（package）或托管上游（upstream）")
    if out["upstream"] is not None and out["pricing"]["type"] not in GATEWAY_PRICING:
        raise bad_request("网关模型的计价须为 per_1k_tokens / per_call / free")
    return out


def public_view(card: ModelCard) -> dict:
    """What any authenticated user may see. Upstream binding is never exposed."""
    view = {"id": card.id, "name": card.name, "version": card.version, "source": card.source, "kind": card.kind,
            "description": card.description, "license": card.license, "pricing": card.pricing,
            "requires_capabilities": card.requires_capabilities, "families": card.families,
            "gateway": card.upstream is not None, "status": card.status}
    if card.package is not None:
        view["package"] = {"sha256": card.package["sha256"], "runtime": card.package["runtime"]}
    return view


def admin_view(card: ModelCard) -> dict:
    view = public_view(card)
    view["package"] = card.package
    view["upstream"] = card.upstream  # only credential_ref (an env var name), never the secret
    return view


def published(session: Session) -> list[ModelCard]:
    return list(session.execute(select(ModelCard).where(ModelCard.status == "published").order_by(ModelCard.id)).scalars())


def manifest_digest(m2: dict) -> str:
    """Digest of a validated manifest, as stored on fleet records (``robots.manifest_digest``)."""
    import hashlib
    return hashlib.sha256(json.dumps(m2, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compatible_with(card: ModelCard, manifest_v2: dict) -> bool:
    """A model fits a robot if the robot provides every required capability,
    its family is allowed, and (for packages) the robot has the runtime."""
    have = manifest.capability_ids(manifest_v2)
    if not set(card.requires_capabilities) <= have:
        return False
    if card.families and manifest_v2.get("family") not in card.families:
        return False
    if card.package is not None and card.upstream is None:
        if card.package["runtime"] not in (manifest_v2.get("model_runtimes") or []):
            return False
    return True


# ---------------------------------------------------------------- pricing
INPUT_TOKEN_OVERHEAD = 64


def input_token_bound(text_bytes: int, messages: int = 1) -> int:
    """Upper bound on prompt tokens.

    Byte-level BPE tokenizers (GPT/Llama/Qwen families) never emit more tokens
    than input bytes, so ``bytes + per-message template overhead`` is a safe
    ceiling for the reservation. Tokenizers that violate this produce an
    overage, which goes to reconciliation instead of being auto-charged.
    """
    return text_bytes + INPUT_TOKEN_OVERHEAD * max(1, messages)


def cost_for_tokens(pricing: dict, total_tokens: int) -> int:
    if pricing["type"] == "free":
        return 0
    if pricing["type"] == "per_call":
        return pricing["credits"]
    if pricing["type"] == "per_1k_tokens":
        return math.ceil(total_tokens * pricing["credits"] / 1000)
    raise ValueError("该计价方式不能用于网关计量")
