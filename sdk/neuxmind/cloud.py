"""Client for the NeuxMind Cloud API (INTERFACES_V1 §2, details in cloud/README.md).

Example::

    from neuxmind.cloud import CloudClient

    cloud = CloudClient("http://127.0.0.1:8800")
    cloud.login("me@example.com", "password")          # stores the session token on the client
    print(cloud.wallet()["available"])
    for model in cloud.compatible_models(robot.manifest())["models"]:
        print(model["id"], model["entitled"])
    cloud.purchase_model("echo-llm")
    print(cloud.gateway.infer("echo-llm", "hello")["output"]["text"])

``token`` is either a session token (``nxs_...`` from login) or a developer
API key (``nx_live_...``). API keys cannot buy, manage keys or use ``/admin``.
``org_id`` sets ``X-NeuxMind-Org`` (session tokens only).
"""
from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any

from ._http import request_json
from .errors import NeuxMindError
from .robot import new_request_id

if TYPE_CHECKING:  # pragma: no cover
    from .gateway import GatewaySession

API_PREFIX = "/api/v1"

# Friendly explanations appended to server errors (``details["hint"]``).
HINTS = {
    "email_unverified": "邮箱尚未验证：购买、支付和调用网关都需要先验证邮箱。打开验证邮件里的令牌，运行 "
                        "nx verify-email <令牌>（Python：CloudClient.verify_email(token)）；没收到可 "
                        "nx verify-email --resend。",
    "parameter_not_allowed": "网关只接受白名单参数，不会悄悄丢弃未知参数；请去掉 details.parameters 中列出的参数。"
                             "输出上限由服务端按剩余预算设置，客户端只能调小。",
    "n_unsupported": "网关每次只生成一个结果：n 只能为 1（或不传）。",
    "session_overage_locked": "该计量会话出现过超额调用，已锁定，只能关闭：本次按预留上限计费，超出部分待运营核对。",
    "verification_token_invalid": "验证令牌无效：不存在、已使用、已被重发作废，或与当前邮箱不符。可 nx verify-email --resend 重新获取。",
    "verification_token_expired": "验证令牌已过期，请 nx verify-email --resend 重新获取。",
    "email_sender_not_configured": "服务端没有配置邮件发送，无法重发验证邮件；请联系平台运营人员人工核实后验证。",
    "already_verified": "邮箱已经验证过了。",
    "verification_wrong_account": "这个验证令牌属于另一个账户：请先用收到邮件的那个邮箱登录（nx login），再在本人会话里验证；令牌没有被消耗。"
                                  "如果该邮箱被别人抢先注册，请用 nx register 重新注册以获取认领邮件，或 nx password-reset request。",
    "session_required": "该操作需要登录会话（API Key 不行）：先 nx login。",
    "email_pending_verification": "该邮箱已被一个未验证的账户占用，且服务端无法发送认领邮件；请联系平台运营人员。",
    "email_taken": "该邮箱已被注册并验证；如果是你的账户，请 nx login，忘记密码可 nx password-reset request --email <邮箱>。",
    "recovery_token_invalid": "认领/重设令牌无效（不存在、已使用、与邮箱不符或用途不对）；可 nx password-reset request --email <邮箱> 重新申请。",
    "session_busy": "同一会话同一时刻只允许一个在途调用（流式调用要等网关发出计量事件才算结束；中途断开的流在服务端读完上游、"
                    "结算之前也算在途），所以不能在同一会话上并发调用。需要并行时请为每一路各开一个会话（每个会话各自预留额度）。",
    "session_closed": "会话已关闭，不能再调用；请开一个新会话。",
    "session_budget_exhausted": "会话剩余 token 额度不足以容纳本次输入：请关闭该会话，用更大的 max_tokens 开新会话。",
    "session_expired": "会话已超过时间上限（max_minutes），请关闭并开新会话。",
    "invalid_session_token": "会话访问令牌无效：access_token 只在开会话时返回一次，请使用开会话时拿到的令牌。",
    "recovery_token_expired": "认领/重设令牌已过期，请 nx password-reset request --email <邮箱> 重新申请。",
    "entitlement_required": "尚未获得该模型的授权：先用钱包点数购买（nx models buy <模型 ID>；Python："
                            "CloudClient.purchase_model(id)）。免费与按量计费的模型也需要先开通（0 点）授权。",
    "no_package": "该模型没有端侧部署包，不能装到机器人上；它只能通过云端网关调用（nx infer / nx chat）。",
    "conformance_required": "require_level 需要设备群里该机器人的最新一致性报告覆盖该级别且对应当前 manifest："
                            "nx robot register <robot>，再 nx robot conformance <robot> --level dry --upload。",
    "conformance_manifest_mismatch": "报告是对另一份 manifest 运行的：先 nx robot register <robot> 用当前 manifest 重新登记，"
                                     "再重新运行一致性测试并上传。",
    "conformance_robot_mismatch": "报告不是这台机器人的（instance_id 或 simulated 不一致）。",
    "conformance_outdated": "云端已保存同一 manifest 的更新报告；只保留最新的一份。",
    "device_run_refused": "一致性报告不是现场运行证据，云端拒绝 evidence_kind=device_run 的报告。",
    "field_acceptance_refused": "一致性报告不等于现场验收，云端只接受 field_acceptance=false 的报告。",
}


def add_hint(exc: NeuxMindError) -> NeuxMindError:
    """Attach the Chinese hint for ``exc.code`` (``details["hint"]`` and the message), once."""
    hint = HINTS.get(exc.code)
    if hint and "hint" not in exc.details:
        exc.details["hint"] = hint
        exc.message = f"{exc.message}。{hint}"
        exc.args = (exc.message,)
    return exc


def _api_base(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(API_PREFIX) else base + API_PREFIX


def _check_infer_options(options: dict) -> None:
    if not options:
        return
    if "n" in options:
        if options["n"] in (1, None) and not isinstance(options["n"], bool):
            options = {k: v for k, v in options.items() if k != "n"}
        else:
            raise NeuxMindError(f"n={options['n']!r} 不受支持。{HINTS['n_unsupported']}", code="n_unsupported",
                                details={"hint": HINTS["n_unsupported"]})
    if options:
        names = sorted(options)
        raise NeuxMindError(
            f"/gateway/infer 不接受参数 {names}：只支持 model、input、max_tokens、request_id；"
            "输出上限由服务端按预留设置，max_tokens 只能调小。本次请求未发送。",
            code="parameter_not_allowed", details={"parameters": names, "hint": HINTS["parameter_not_allowed"]})


class _Gateway:
    """``cloud.gateway`` namespace: ``infer``, metered sessions (``open_session``, ``session``)."""

    def __init__(self, client: "CloudClient"):
        self._client = client

    def open_session(self, model: str, max_tokens: int, *, max_minutes: int | None = None,
                     max_units: int | None = None, request_id: str | None = None, call_timeout: float = 300.0,
                     stream_timeout: float = 120.0, recover_timeout: float = 60.0) -> "GatewaySession":
        """``POST /gateway/sessions`` → :class:`~neuxmind.gateway.GatewaySession` (a context manager:
        leaving the ``with`` block closes and settles the session, result in ``close_result``).

        Reserves credits for ``max_tokens`` (input + output of all calls). Needs a verified
        e-mail and an entitlement for ``model``. ``call_timeout``: non-streamed calls;
        ``stream_timeout``: each read of a stream; ``recover_timeout``: how long to wait for
        the server to settle a call whose stream broke off before asking for its usage.
        """
        from .gateway import GatewaySession

        body: dict[str, Any] = {"request_id": request_id or new_request_id("session"), "model": model,
                                "max_tokens": int(max_tokens)}
        if max_minutes is not None:
            body["max_minutes"] = int(max_minutes)
        if max_units is not None:
            body["max_units"] = int(max_units)
        view = self._client._request("POST", "/gateway/sessions", body)
        return GatewaySession(self._client, view, call_timeout=call_timeout, stream_timeout=stream_timeout,
                              recover_timeout=recover_timeout)

    def session(self, session_id: str) -> dict:
        """``GET /gateway/sessions/{id}``: status and usage (``used_tokens``, ``inflight_tokens``,
        ``calls``, ``unknown_calls``, ``overage_calls``, ``close_result`` ...)."""
        return self._client._request("GET", f"/gateway/sessions/{urllib.parse.quote(session_id)}")

    def close_session(self, session_id: str, request_id: str | None = None) -> dict:
        """``POST /gateway/sessions/{id}/close`` (idempotent per ``request_id``)."""
        return self._client._request("POST", f"/gateway/sessions/{urllib.parse.quote(session_id)}/close",
                                     {"request_id": request_id or new_request_id("close")})

    def infer(self, model: str, input: Any, max_tokens: int = 256, *, request_id: str | None = None,
              **options: Any) -> dict:
        return self._client.infer(model, input, max_tokens, request_id=request_id, **options)


class CloudClient:
    def __init__(self, base_url: str, token: str | None = None, *, org_id: str | None = None,
                 timeout: float = 30.0):
        if not base_url:
            raise NeuxMindError("需要云平台地址，如 http://127.0.0.1:8800（nx config set cloud_url ...）",
                                code="config_error")
        self.base_url = base_url.rstrip("/")
        self.api = _api_base(base_url)
        self.token = token
        self.org_id = org_id
        self.timeout = timeout
        self.gateway = _Gateway(self)

    def __repr__(self) -> str:
        return f"CloudClient({self.base_url!r})"

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, *, auth: bool = True,
                 query: dict | None = None) -> Any:
        headers = {}
        if auth:
            if not self.token:
                raise NeuxMindError("尚未登录云平台：请先 nx login（或传入 token / API Key）", code="not_logged_in")
            headers["Authorization"] = f"Bearer {self.token}"
            if self.org_id:
                headers["X-NeuxMind-Org"] = self.org_id
        url = self.api + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        try:
            return request_json(method, url, body=body, headers=headers, timeout=self.timeout)
        except NeuxMindError as exc:
            raise add_hint(exc)

    # ------------------------------------------------------------ account
    def register(self, email: str, password: str, name: str | None = None) -> dict:
        """Create an account; the returned session token is kept on the client.

        The response includes ``verification`` (``status: sent|unavailable|failed``,
        ``simulated``) and, on dev/test servers only, ``dev_verification_token``.
        The e-mail starts unverified: call :meth:`verify_email` (in this session) before
        buying or inferring.

        If the address is held by an *unverified* account, the server answers 202
        ``{"status": "claim_sent", "code": "email_pending_claim", dev_claim_token?}``:
        no account and no session are created; the mailbox owner uses :meth:`claim`.
        """
        body = {"email": email, "password": password}
        if name:
            body["name"] = name
        out = self._request("POST", "/auth/register", body, auth=False)
        if out.get("status") == "claim_sent":
            return out                     # no session: keep whatever token the client had
        self.token = out.get("token")
        return out

    def claim(self, token: str, password: str, name: str | None = None) -> dict:
        """Redeem a claim token: a new *verified* account for the mailbox owner (new password,
        new personal org); the unverified account that held the address is disabled.
        The returned session token is kept on the client."""
        body = {"token": token, "password": password}
        if name:
            body["name"] = name
        out = self._request("POST", "/auth/claim", body, auth=False)
        self.token = out.get("token")
        return out

    def request_password_reset(self, email: str) -> dict:
        """Ask for a password-reset e-mail. Always 202 ``{"status": "accepted"}`` (no account
        enumeration); unverified accounts receive a claim token instead. Dev/test servers
        add ``dev_reset_token`` / ``dev_claim_token``."""
        return self._request("POST", "/auth/password-reset/request", {"email": email}, auth=False)

    def reset_password(self, token: str, password: str) -> dict:
        """Set a new password with a reset token (also the setup token from ``create-operator``).
        All other sessions are deleted; API keys are kept. Returns and keeps a new session."""
        out = self._request("POST", "/auth/password-reset", {"token": token, "password": password}, auth=False)
        self.token = out.get("token")
        return out

    def verify_email(self, token: str) -> dict:
        """Confirm the e-mail with the one-time token (``nxv_...``) **inside the account's own
        login session** (the token must belong to the logged-in user, else
        ``verification_wrong_account`` and the token is not consumed).

        → ``{email_verified: true, user, sessions_revoked, api_keys_revoked}``. On success the
        server deletes the user's *other* sessions and revokes every API key created before
        verification — create new keys afterwards. Unverified users may browse but cannot
        buy, pay or call the gateway (``email_unverified``).
        """
        return self._request("POST", "/auth/verify-email", {"token": token})

    def resend_verification(self) -> dict:
        """Send a new verification e-mail (session only; invalidates the old token).

        → ``{email_verified, verification, dev_verification_token?}``; the dev token
        is only returned by dev/test servers whose mailer does not really send mail.
        """
        return self._request("POST", "/auth/verify-email/resend", {})

    def login(self, email: str, password: str) -> dict:
        out = self._request("POST", "/auth/login", {"email": email, "password": password}, auth=False)
        self.token = out.get("token")
        return out

    def logout(self) -> dict:
        out = self._request("POST", "/auth/logout", {})
        self.token = None
        return out

    def me(self) -> dict:
        return self._request("GET", "/me")

    # ------------------------------------------------------------ API keys
    def create_api_key(self, scopes: list[str], name: str | None = None) -> dict:
        """Create a developer API key. The plaintext ``key`` is returned only this once."""
        body: dict[str, Any] = {"scopes": list(scopes)}
        if name:
            body["name"] = name
        return self._request("POST", "/api-keys", body)

    def api_keys(self) -> list[dict]:
        return self._request("GET", "/api-keys").get("api_keys", [])

    def revoke_api_key(self, key_id: str) -> dict:
        return self._request("DELETE", f"/api-keys/{urllib.parse.quote(key_id)}")

    # ------------------------------------------------------------ wallet & usage
    def wallet(self) -> dict:
        """``{org_id, unit: "credits", balance, reserved, available}``."""
        return self._request("GET", "/wallet")

    def ledger(self, limit: int | None = None) -> list[dict]:
        return self._request("GET", "/wallet/ledger", query={"limit": limit}).get("entries", [])

    def reservations(self, status: str | None = None) -> list[dict]:
        return self._request("GET", "/wallet/reservations", query={"status": status}).get("reservations", [])

    def usage(self, model: str | None = None, limit: int | None = None) -> dict:
        """``{usage: [...], totals}``."""
        return self._request("GET", "/usage", query={"model": model, "limit": limit})

    # ------------------------------------------------------------ catalog
    def capability_registry(self) -> dict:
        return self._request("GET", "/capabilities/registry", auth=False)

    def models(self, *, kind: str | None = None, source: str | None = None, capability: str | None = None,
               robot_id: str | None = None) -> list[dict]:
        return self._request("GET", "/catalog/models", query={"kind": kind, "source": source,
                                                              "capability": capability,
                                                              "robot_id": robot_id}).get("models", [])

    def model(self, model_id: str) -> dict:
        return self._request("GET", f"/catalog/models/{urllib.parse.quote(model_id)}")

    def compatible_models(self, manifest: dict | None = None, *, robot_id: str | None = None,
                          require_level: str | None = None) -> dict:
        """Models a body can use: ``{instance_id, capabilities, models}`` (server-side filter).

        Give the ``manifest`` or the fleet ``robot_id``. ``require_level="dry"`` (or ``"static"``)
        counts only the capabilities that passed that level in the robot's latest uploaded
        conformance report (adds ``usable_capabilities``, ``not_verified``, ``conformance``);
        409 ``conformance_required`` when there is none."""
        if (manifest is None) == (robot_id is None):
            raise NeuxMindError("compatible_models 需要 manifest 或 robot_id 之一", code="bad_request")
        body: dict[str, Any] = {"manifest": manifest} if manifest is not None else {"robot_id": robot_id}
        if require_level is not None:
            body["require_level"] = require_level
        return self._request("POST", "/catalog/models/compatible", body)

    def model_package(self, model_id: str) -> dict:
        """Edge package reference (needs an entitlement); ``install.args`` feed ``model.install``."""
        return self._request("GET", f"/catalog/models/{urllib.parse.quote(model_id)}/package")

    def model_install_args(self, model_id: str) -> dict:
        """``model.install`` arguments ``{package_ref, sha256, runtime}`` for an entitled model.

        Uses the package's ``install.args`` (the cloud may put a short-lived signed
        download URL there); falls back to ``ref``/``sha256``/``runtime``. Raises
        ``entitlement_required`` (buy first) or ``no_package`` with a hint.
        """
        package = self.model_package(model_id)
        install = package.get("install") if isinstance(package.get("install"), dict) else {}
        args = install.get("args") if isinstance(install.get("args"), dict) else {}
        out = {"package_ref": args.get("package_ref") or package.get("ref"),
               "sha256": args.get("sha256") or package.get("sha256"),
               "runtime": args.get("runtime") or package.get("runtime")}
        missing = [k for k, v in out.items() if not isinstance(v, str) or not v]
        if missing:
            raise NeuxMindError(f"云端返回的模型包信息缺少 {missing}", code="bad_response",
                                details={"package": package})
        return out

    def credit_packs(self) -> list[dict]:
        return self._request("GET", "/catalog/credit-packs").get("credit_packs", [])

    def entitlements(self) -> list[dict]:
        return self._request("GET", "/entitlements").get("entitlements", [])

    # ------------------------------------------------------------ purchases & payments
    def purchase_model(self, model_id: str) -> dict:
        """Buy a model license with wallet credits → ``{order, entitlement}``."""
        return self._request("POST", "/purchases", {"kind": "model_license", "model_id": model_id})

    def purchase_credit_pack(self, pack_id: str) -> dict:
        """Create a pending credit-pack order (pay with :meth:`checkout`) → ``{order}``."""
        return self._request("POST", "/purchases", {"kind": "credit_pack", "pack_id": pack_id})

    def purchases(self) -> dict:
        return self._request("GET", "/purchases")

    def purchase(self, order_id: str) -> dict:
        return self._request("GET", f"/purchases/{urllib.parse.quote(order_id)}")

    def checkout(self, provider: str, order_id: str) -> dict:
        """Start payment. Unconfigured providers fail with ``provider_not_configured`` (never faked)."""
        return self._request("POST", f"/payments/{urllib.parse.quote(provider)}/checkout", {"order_id": order_id})

    # ------------------------------------------------------------ gateway
    def infer(self, model: str, input: Any, max_tokens: int = 256, *, request_id: str | None = None,
              **options: Any) -> dict:
        """``POST /gateway/infer`` → ``{output: {text}, usage, credits_charged, simulated, ...}``.

        ``input`` is a string or ``[{role, content}]``. Credits are reserved
        before the upstream call and settled on measured usage. Requires a
        verified e-mail. ``max_tokens`` is the only generation option: other
        keyword options are refused locally (``n_unsupported`` for ``n != 1``,
        ``parameter_not_allowed`` otherwise) so nothing is silently ignored.
        """
        _check_infer_options(options)
        body = {"model": model, "input": input, "max_tokens": int(max_tokens),
                "request_id": request_id or new_request_id("infer")}
        return self._request("POST", "/gateway/infer", body)

    # ------------------------------------------------------------ fleet
    def register_robot(self, manifest: dict) -> dict:
        """Register (or update) a robot by its ``robot.adapter.v2`` manifest."""
        return self._request("POST", "/robots", {"manifest": manifest})

    def robots(self) -> list[dict]:
        return self._request("GET", "/robots").get("robots", [])

    def robot(self, robot_id: str) -> dict:
        return self._request("GET", f"/robots/{urllib.parse.quote(robot_id)}")

    def delete_robot(self, robot_id: str) -> dict:
        return self._request("DELETE", f"/robots/{urllib.parse.quote(robot_id)}")

    def upload_conformance(self, robot_id: str, report: dict) -> dict:
        """Store ``report`` (``conformance.report.v1``) as the fleet robot's latest conformance
        summary → ``{robot_id, conformance}``. The cloud refuses ``field_acceptance`` other than
        false, ``evidence_kind: "device_run"`` and reports that do not match the registered
        manifest (re-register first: :meth:`register_robot`)."""
        return self._request("POST", f"/robots/{urllib.parse.quote(robot_id)}/conformance", {"report": report})

    def check_skill(self, robot_id: str, skill: dict, *, require_level: str | None = None) -> dict:
        """Server-side ``spec.check_compatibility`` of skill metadata against a fleet robot;
        ``require_level="dry"``: only conformance-verified capabilities count."""
        body: dict[str, Any] = {"skill": {k: v for k, v in skill.items() if k != "files"}}
        if require_level is not None:
            body["require_level"] = require_level
        return self._request("POST", f"/robots/{urllib.parse.quote(robot_id)}/skills/check", body)
