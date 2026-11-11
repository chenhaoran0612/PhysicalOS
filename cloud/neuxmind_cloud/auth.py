"""Users, organisations, sessions, API keys and request principals.

Two kinds of credentials reach the API:

* **Session token** (``nxs_...``) from ``/auth/login``. Acts with the user's
  role in the selected org (header ``X-NeuxMind-Org``; default: personal org).
* **API key** (``nx_live_...``). Bound to one org; effective permissions are
  ``key.scopes ∩ permissions(creator's current role)`` so demoting or removing
  the creator immediately narrows the key.

Only SHA-256 hashes of either token are stored.

Platform-operator status is never granted through the API. It requires a
**verified e-mail address** *and* either the ``create-operator`` /
``grant-operator`` management commands (recommended in production) or the
``NEUXMIND_OPERATOR_EMAILS`` allow-list (dev/test by default; prod only with
``NEUXMIND_OPERATOR_ALLOWLIST=1``).

Verification must happen **inside the account's own login session**
(rereview finding N-01): the mailbox owner clicking a token for an account
someone else registered (with a password they chose) verifies nothing.
Instead, re-registering an address held by an *unverified* account sends a
claim token (recovery.py) that gives the mailbox owner a fresh account and
disables the squatter's.

Unverified users can register, log in and browse; buying (credit packs, model
licenses, checkout) and the inference gateway require a verified address.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import audit
from .config import Settings
from .db import advisory_lock
from .errors import ApiError, bad_request, conflict, forbidden, not_found, unauthorized
from .models import (ApiKey, AuthSession, EmailVerificationToken, LoginAttempt, Membership, Organization, RateEvent,
                     User, Wallet, now)
from .security import (API_KEY_PREFIX, SESSION_PREFIX, dummy_password_hash, hash_password, needs_rehash, new_api_key,
                       new_email_token, new_session_token, token_hash, verify_password)

ROLES = ("owner", "admin", "developer", "operator")
API_KEY_SCOPES = ("gateway", "catalog:read", "robots", "wallet:read", "usage:read")
ROLE_PERMISSIONS = {
    "owner": {"members:manage", "keys:manage", "purchases", *API_KEY_SCOPES},
    "admin": {"members:manage", "keys:manage", "purchases", *API_KEY_SCOPES},
    "developer": {"keys:manage", "gateway", "catalog:read", "robots", "wallet:read", "usage:read"},
    # Org-level field operator (runs robots); not the platform operator.
    "operator": {"catalog:read", "robots", "wallet:read", "usage:read"},
}


@dataclass
class Principal:
    kind: str  # "session" | "api_key"
    user: User
    org_id: str
    role: str
    permissions: set[str] = field(default_factory=set)
    api_key_id: str | None = None
    platform_operator: bool = False
    session_hash: str | None = None     # set for session principals

    @property
    def actor(self) -> str:
        return f"key:{self.api_key_id}" if self.api_key_id else f"user:{self.user.id}"

    def require(self, permission: str) -> "Principal":
        if permission not in self.permissions:
            if self.kind == "api_key":
                raise forbidden(f"API Key 缺少权限范围 {permission}", code="scope_missing")
            raise forbidden(f"角色 {self.role} 无权执行此操作（需要 {permission}）")
        return self

    def require_session(self) -> "Principal":
        if self.kind != "session":
            raise forbidden("此操作需要用户登录会话，不能使用 API Key", code="session_required")
        return self

    def require_verified(self) -> "Principal":
        """Spending (purchases, checkout, gateway) needs a verified e-mail.

        For API keys this is the key creator's address."""
        if not self.user.email_verified:
            raise forbidden("请先验证邮箱后再购买或调用模型网关（POST /auth/verify-email）", code="email_unverified")
        return self


# ---------------------------------------------------------------- helpers
def _email(value) -> str:
    if not isinstance(value, str):
        raise bad_request("请输入有效邮箱")
    email = value.strip().lower()
    if "@" not in email or len(email) > 200 or email.startswith("@") or email.endswith("@") or " " in email:
        raise bad_request("请输入有效邮箱")
    return email


def _password(value) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 256:
        raise bad_request("密码须为 10–256 个字符")
    return value


def is_platform_operator(user: User, settings: Settings) -> bool:
    """Verified e-mail AND (granted by the management command OR allow-listed)."""
    if not user.email_verified:
        return False
    return bool(user.is_platform_operator) or (settings.operator_allowlist_active
                                               and user.email in settings.operator_emails)


def revoke_credentials(session: Session, user: User, *, keep_session_hash: str | None = None) -> dict:
    """Delete every login session of ``user`` (except ``keep_session_hash``) and revoke
    every API key the user created (in any org)."""
    query = delete(AuthSession).where(AuthSession.user_id == user.id)
    if keep_session_hash:
        query = query.where(AuthSession.token_hash != keep_session_hash)
    sessions = session.execute(query).rowcount or 0
    keys = 0
    for key in session.execute(select(ApiKey).where(ApiKey.created_by == user.id,
                                                    ApiKey.revoked_at.is_(None))).scalars():
        key.revoked_at = now()
        keys += 1
    return {"sessions_revoked": sessions, "api_keys_revoked": keys}


def active_credentials(session: Session, user: User) -> dict:
    sessions = session.execute(select(func.count()).select_from(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.expires_at > now())).scalar_one()
    keys = session.execute(select(func.count()).select_from(ApiKey).where(
        ApiKey.created_by == user.id, ApiKey.revoked_at.is_(None))).scalar_one()
    return {"sessions": sessions, "api_keys": keys}


def create_account(session: Session, email: str, name: str, password_hash: str, *, verified: bool,
                   operator: bool = False) -> tuple[User, Organization]:
    """User + personal org (owner) + wallet. Shared by register, claim and create-operator."""
    user = User(email=email, name=name, password_hash=password_hash, is_platform_operator=operator,
                email_verified=verified, email_verified_at=now() if verified else None)
    org = Organization(name=f"{name} 的个人空间"[:100], personal=True)
    session.add_all([user, org])
    session.flush()
    session.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    session.add(Wallet(org_id=org.id))
    return user, org


def _throttle(session: Session, kind: str, key: str, limit: int, window: int, message: str) -> None:
    """Count-then-insert under an advisory lock (Postgres) / BEGIN IMMEDIATE (SQLite)."""
    advisory_lock(session, f"rate:{kind}:{key}")
    start = now() - window
    count = session.execute(select(func.count()).select_from(RateEvent).where(
        RateEvent.kind == kind, RateEvent.key == key, RateEvent.at >= start)).scalar_one()
    if count >= limit:
        raise ApiError(429, "rate_limited", message, retry_after=window)
    session.execute(delete(RateEvent).where(RateEvent.kind == kind, RateEvent.key == key, RateEvent.at < start))
    session.add(RateEvent(kind=kind, key=key))


def _issue_session(session: Session, user: User, settings: Settings) -> dict:
    token = new_session_token()
    expires = now() + settings.session_ttl_seconds
    session.execute(delete(AuthSession).where(AuthSession.expires_at < now()))
    session.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
    return {"token": token, "token_type": "bearer", "expires_at": expires}


def user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "email_verified": bool(user.email_verified),
            "created_at": user.created_at}


# ------------------------------------------------------------ registration
def register(session: Session, payload: dict, settings: Settings, ip: str = "", *, can_send_email: bool = False) -> dict:
    """Create a user (e-mail **unverified**) plus a personal organisation (role owner) and its wallet.

    Any role/operator/verification fields in the payload are ignored on purpose.
    When an e-mail sender is configured, a one-time verification token is
    issued; it is returned under the private key ``_verification_token`` for
    the route to send *after* the transaction commits (never to the client,
    except by the dev/test log sender, see routes/account.py).

    If the address belongs to an **unverified** account, nothing is created:
    a claim token is mailed to the address instead (``_claim_token``; see
    recovery.py) and the result has ``status: "claim_sent"``. A verified
    address answers 409 ``email_taken`` (use password reset).
    """
    _throttle(session, "register", ip[:64] or "-", settings.register_ip_max_per_hour, 3600,
              "该网络地址注册过于频繁，请稍后再试")
    email = _email(payload.get("email"))
    password = _password(payload.get("password"))
    name = payload.get("name") or email.split("@")[0]
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise bad_request("名称须为 1–100 个字符")
    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        if existing.email_verified:
            raise conflict("此邮箱已注册，请登录（忘记密码可用 /auth/password-reset/request）", code="email_taken")
        if not can_send_email:
            raise conflict("此邮箱已被一个未验证的账户注册；服务端未配置邮件发送，请联系平台运营员处理",
                           code="email_pending_verification")
        from .recovery import issue_claim
        return {"status": "claim_sent", "email": email, "_claim_token": issue_claim(session, existing, settings, ip)}
    user, org = create_account(session, email, name.strip(), hash_password(password), verified=False)
    audit.record(session, "auth.register", f"user:{user.id}", org_id=org.id, target=user.email, ip=ip)
    issued = _issue_session(session, user, settings)
    result = {**issued, "user": user_view(user), "org_id": org.id}
    if can_send_email:
        result["_verification_token"] = issue_verification(session, user, settings)
    return result


# ------------------------------------------------------- e-mail verification
def issue_verification(session: Session, user: User, settings: Settings) -> str:
    """New one-time verify token for ``user.email``; earlier unused verify tokens are invalidated."""
    session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id,
                                                         EmailVerificationToken.purpose == "verify"))
    token = new_email_token()
    session.add(EmailVerificationToken(token_hash=token_hash(token), user_id=user.id, email=user.email,
                                       purpose="verify", expires_at=now() + settings.email_verify_ttl_seconds))
    audit.record(session, "auth.verification_issued", f"user:{user.id}", target=user.email)
    return token


def request_verification(session: Session, principal: "Principal", settings: Settings) -> str:
    """Resend (session only). Caller must have checked that a sender exists."""
    principal.require_session()
    user = principal.user
    if user.email_verified:
        raise conflict("邮箱已验证", code="already_verified")
    _throttle(session, "verify_send", user.id, settings.email_verify_max_sends_per_hour, 3600,
              "验证邮件发送过于频繁，请稍后再试")
    return issue_verification(session, user, settings)


def verify_email(session: Session, principal: "Principal", payload: dict, ip: str = "") -> dict:
    """Consume a verification token **from the account's own login session** (rereview N-01).

    The token proves the mailbox; the session proves the account. Both must
    belong to the same user, otherwise a mailbox owner clicking a token for an
    account someone else registered would hand that account a verified
    address. On success every *other* session of the user is deleted and every
    API key the user created before verification is revoked.
    """
    principal.require_session()
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip() or len(token) > 200:
        raise bad_request("需要验证令牌 token", code="verification_token_invalid")
    row = session.get(EmailVerificationToken, token_hash(token.strip()))
    if row is None or row.used_at is not None or row.purpose != "verify":
        raise bad_request("验证令牌无效或已使用", code="verification_token_invalid")
    if row.user_id != principal.user.id:
        audit.record(session, "auth.verification_wrong_account", principal.actor, target=row.email, ip=ip)
        raise forbidden("该验证令牌不属于当前登录的账户；请用注册时的账户登录后再验证。"
                        "如果这个邮箱是你的而账户不是你注册的，请重新注册以认领该邮箱",
                        code="verification_wrong_account")
    if row.expires_at <= now():
        raise bad_request("验证令牌已过期，请重新发送验证邮件", code="verification_token_expired")
    user = principal.user
    if user.email != row.email:
        raise bad_request("验证令牌与当前邮箱不符", code="verification_token_invalid")
    row.used_at = now()
    revoked = {"sessions_revoked": 0, "api_keys_revoked": 0}
    if not user.email_verified:
        user.email_verified, user.email_verified_at = True, now()
        revoked = revoke_credentials(session, user, keep_session_hash=principal.session_hash)
        audit.record(session, "auth.email_verified", f"user:{user.id}", target=user.email, ip=ip, detail=revoked)
    session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id,
                                                         EmailVerificationToken.purpose == "verify",
                                                         EmailVerificationToken.token_hash != row.token_hash))
    return revoked


def login(session: Session, payload: dict, settings: Settings, ip: str = "") -> dict:
    """Password login with three throttles (review finding C-03).

    * (email, ip): ``login_max_failures`` failures per window → 429 for that
      pair only. Someone hammering a victim's address from *their* address
      does not lock the victim out elsewhere.
    * ip: ``login_ip_max_failures`` failures across all e-mails → 429
      (credential stuffing from one address).
    * email (all ips): above ``login_email_soft_limit`` failures, attempts are
      spaced by a progressive delay (2^k s, capped at ``login_max_delay_seconds``)
      instead of a hard lock, so a distributed attack slows guessing to a
      trickle without locking the owner out for longer than the cap.

    Counting and inserting run under advisory locks (Postgres) or the SQLite
    write lock, so concurrent requests cannot all pass the count check.
    """
    email = _email(payload.get("email"))
    password = payload.get("password") if isinstance(payload.get("password"), str) else ""
    ip = (ip or "")[:64]
    advisory_lock(session, f"login-ip:{ip}", f"login-email:{email}")
    t = now()
    window_start = t - settings.login_window_seconds
    failed = select(func.count()).select_from(LoginAttempt).where(LoginAttempt.success.is_(False),
                                                                  LoginAttempt.at >= window_start)
    pair = session.execute(failed.where(LoginAttempt.email == email, LoginAttempt.ip == ip)).scalar_one()
    if pair >= settings.login_max_failures:
        raise ApiError(429, "rate_limited", "登录尝试过于频繁，请稍后再试", retry_after=settings.login_window_seconds)
    by_ip = session.execute(failed.where(LoginAttempt.ip == ip)).scalar_one()
    if by_ip >= settings.login_ip_max_failures:
        raise ApiError(429, "rate_limited", "该网络地址登录失败过多，请稍后再试", retry_after=settings.login_window_seconds)
    by_email = session.execute(failed.where(LoginAttempt.email == email)).scalar_one()
    if by_email >= settings.login_email_soft_limit:
        last = session.execute(select(func.max(LoginAttempt.at)).where(
            LoginAttempt.email == email, LoginAttempt.success.is_(False))).scalar_one() or 0
        delay = min(2 ** min(by_email - settings.login_email_soft_limit, 20), settings.login_max_delay_seconds)
        if t - last < delay:
            raise ApiError(429, "rate_limited", "该账户近期登录失败较多，请稍后重试",
                           retry_after=max(1, int(delay - (t - last)) + 1))
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    ok = verify_password(password, user.password_hash if user else dummy_password_hash()) and user is not None
    if not ok:
        session.add(LoginAttempt(email=email, ip=ip, success=False, at=t))
        audit.record(session, "auth.login_failed", "anonymous", target=email, ip=ip)
        # Commit the failed attempt even though we answer with an error.
        session.commit()
        raise unauthorized("邮箱或密码不正确", code="invalid_credentials")
    # Only this (email, ip) pair is cleared: a successful login elsewhere must
    # not reset an attacker's counters.
    session.execute(delete(LoginAttempt).where(LoginAttempt.email == email, LoginAttempt.ip == ip))
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)  # transparent scrypt cost upgrade (C-07)
    audit.record(session, "auth.login", f"user:{user.id}", target=email, ip=ip)
    return {**_issue_session(session, user, settings), "user": user_view(user)}


def logout(session: Session, token: str) -> None:
    session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))


# --------------------------------------------------------------- principal
def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized()
    token = authorization[7:].strip()
    if not token or len(token) > 400:
        raise unauthorized()
    return token


def resolve(session: Session, authorization: str | None, org_header: str | None, settings: Settings) -> Principal:
    token = bearer_token(authorization)
    if token.startswith(API_KEY_PREFIX):
        key = session.execute(select(ApiKey).where(ApiKey.key_hash == token_hash(token))).scalar_one_or_none()
        if key is None or key.revoked_at is not None:
            raise unauthorized("API Key 无效或已吊销", code="invalid_api_key")
        membership = session.execute(select(Membership).where(
            Membership.org_id == key.org_id, Membership.user_id == key.created_by)).scalar_one_or_none()
        if membership is None:
            raise unauthorized("API Key 的创建者已不在该组织", code="invalid_api_key")
        user = session.get(User, key.created_by)
        key.last_used_at = now()
        perms = set(key.scopes) & ROLE_PERMISSIONS[membership.role]
        return Principal("api_key", user, key.org_id, membership.role, perms, api_key_id=key.id,
                         platform_operator=False)
    if not token.startswith(SESSION_PREFIX):
        raise unauthorized("无法识别的凭据", code="invalid_token")
    row = session.get(AuthSession, token_hash(token))
    if row is None or row.expires_at <= now():
        raise unauthorized("会话无效或已过期，请重新登录", code="session_expired")
    user = session.get(User, row.user_id)
    if org_header:
        membership = session.execute(select(Membership).where(
            Membership.org_id == org_header, Membership.user_id == user.id)).scalar_one_or_none()
        if membership is None:
            raise forbidden("你不是该组织成员", code="not_member")
    else:
        membership = session.execute(
            select(Membership).join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user.id).order_by(Organization.personal.desc(), Membership.created_at)
        ).scalars().first()
        if membership is None:
            raise forbidden("账户没有可用组织", code="not_member")
    return Principal("session", user, membership.org_id, membership.role, set(ROLE_PERMISSIONS[membership.role]),
                     platform_operator=is_platform_operator(user, settings), session_hash=row.token_hash)


def require_platform_operator(principal: Principal) -> Principal:
    principal.require_session()
    if not principal.platform_operator:
        raise forbidden("需要平台运营员权限", code="operator_required")
    return principal


def me(session: Session, principal: Principal) -> dict:
    rows = session.execute(select(Membership, Organization).join(Organization, Organization.id == Membership.org_id)
                           .where(Membership.user_id == principal.user.id)).all()
    return {"user": user_view(principal.user), "email_verified": bool(principal.user.email_verified),
            "platform_operator": principal.platform_operator,
            "auth": principal.kind, "current_org_id": principal.org_id, "role": principal.role,
            "permissions": sorted(principal.permissions),
            "organizations": [{"id": org.id, "name": org.name, "personal": org.personal, "role": m.role} for m, org in rows]}


# ------------------------------------------------------------------ members
def add_member(session: Session, principal: Principal, payload: dict) -> dict:
    """Add a member or change a member's role.

    Rules: owner is never granted or changed through the API; only the owner
    grants ``admin`` and only the owner changes an existing admin's role.
    """
    principal.require_session().require("members:manage")
    email = _email(payload.get("email"))
    role = payload.get("role")
    if role not in ("admin", "developer", "operator"):
        raise bad_request("角色须为 admin / developer / operator（owner 不能通过接口授予）")
    if role == "admin" and principal.role != "owner":
        raise forbidden("只有 owner 能授予 admin")
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise not_found("该邮箱尚未注册")
    existing = session.execute(select(Membership).where(Membership.org_id == principal.org_id,
                                                        Membership.user_id == user.id)).scalar_one_or_none()
    if existing is not None:
        if existing.role == "owner":
            raise forbidden("不能修改 owner 的角色")
        if existing.role == "admin" and principal.role != "owner":
            raise forbidden("只有 owner 能修改 admin 的角色")
        existing.role = role
    else:
        session.add(Membership(org_id=principal.org_id, user_id=user.id, role=role))
    audit.record(session, "org.member_set", principal.actor, org_id=principal.org_id, target=user.id, detail={"role": role})
    return {"org_id": principal.org_id, "user_id": user.id, "role": role}


def list_members(session: Session, principal: Principal) -> list[dict]:
    principal.require_session()
    rows = session.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                           .where(Membership.org_id == principal.org_id).order_by(Membership.created_at)).all()
    return [{"user_id": u.id, "email": u.email, "name": u.name, "role": m.role} for m, u in rows]


def remove_member(session: Session, principal: Principal, user_id: str) -> dict:
    """``DELETE /org/members/{user_id}`` (review finding C-10).

    The owner can never be removed. Admin/owner may remove developers and
    operators; only the owner may remove an admin; anyone may leave (remove
    themselves) unless they are the owner. API keys created by the removed
    member stop working immediately (their creator is no longer a member).
    """
    principal.require_session()
    target = session.execute(select(Membership).where(Membership.org_id == principal.org_id,
                                                      Membership.user_id == user_id)).scalar_one_or_none()
    if target is None:
        raise not_found("该用户不是本组织成员")
    leaving = user_id == principal.user.id
    if target.role == "owner":
        raise forbidden("不能移除组织 owner")
    if not leaving:
        principal.require("members:manage")
        if target.role == "admin" and principal.role != "owner":
            raise forbidden("只有 owner 能移除 admin")
    session.delete(target)
    audit.record(session, "org.member_remove", principal.actor, org_id=principal.org_id, target=user_id,
                 detail={"role": target.role, "self": leaving})
    return {"org_id": principal.org_id, "user_id": user_id, "removed": True}


# ----------------------------------------------------------------- API keys
def key_view(key: ApiKey) -> dict:
    return {"id": key.id, "name": key.name, "prefix": key.display_prefix, "scopes": key.scopes, "org_id": key.org_id,
            "created_at": key.created_at, "last_used_at": key.last_used_at, "revoked": key.revoked_at is not None}


def create_api_key(session: Session, principal: Principal, payload: dict) -> dict:
    principal.require_session().require("keys:manage")
    name = payload.get("name", "default")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise bad_request("名称须为 1–100 个字符")
    scopes = payload.get("scopes", ["gateway", "catalog:read"])
    if not isinstance(scopes, list) or not scopes or any(s not in API_KEY_SCOPES for s in scopes):
        raise bad_request(f"scopes 须为 {API_KEY_SCOPES} 的非空子集", code="invalid_scope")
    scopes = sorted(set(scopes))
    beyond = [s for s in scopes if s not in principal.permissions]
    if beyond:
        raise forbidden(f"不能授予超出自身角色的权限：{beyond}", code="scope_escalation")
    secret = new_api_key()
    key = ApiKey(org_id=principal.org_id, created_by=principal.user.id, name=name.strip(), key_hash=token_hash(secret),
                 display_prefix=secret[:12], scopes=scopes)
    session.add(key)
    session.flush()
    audit.record(session, "api_key.create", principal.actor, org_id=principal.org_id, target=key.id, detail={"scopes": scopes})
    # The plaintext key is returned exactly once and never stored.
    return {**key_view(key), "key": secret}


def list_api_keys(session: Session, principal: Principal) -> list[dict]:
    principal.require_session().require("keys:manage")
    keys = session.execute(select(ApiKey).where(ApiKey.org_id == principal.org_id).order_by(ApiKey.created_at)).scalars()
    return [key_view(k) for k in keys]


def revoke_api_key(session: Session, principal: Principal, key_id: str) -> dict:
    principal.require_session().require("keys:manage")
    key = session.get(ApiKey, key_id)
    if key is None or key.org_id != principal.org_id:
        raise not_found("API Key 不存在")
    if principal.role == "developer" and key.created_by != principal.user.id:
        raise forbidden("developer 只能吊销自己创建的 API Key")
    if key.revoked_at is None:
        key.revoked_at = now()
        audit.record(session, "api_key.revoke", principal.actor, org_id=principal.org_id, target=key.id)
    return key_view(key)


def grant_operator(session: Session, email: str, actor: str = "cli") -> User:
    """Management-command only (``python3 -m neuxmind_cloud grant-operator``). Refuses unverified accounts."""
    user = session.execute(select(User).where(User.email == _email(email))).scalar_one_or_none()
    if user is None:
        raise not_found("用户不存在")
    if not user.email_verified:
        # Rereview N-01: never promote an account whose password may have been
        # chosen by someone other than the mailbox owner.
        raise conflict("该账户邮箱未验证，拒绝授予运营员；请改用 create-operator，或让本人在自己的会话中完成验证",
                       code="email_unverified")
    user.is_platform_operator = True
    audit.record(session, "platform.grant_operator", actor, target=user.id)
    return user


def revoke_operator(session: Session, email: str, actor: str = "cli") -> User:
    """Management-command only. Removes the command-granted flag (allow-list entries
    must also be removed from ``NEUXMIND_OPERATOR_EMAILS``)."""
    user = session.execute(select(User).where(User.email == _email(email))).scalar_one_or_none()
    if user is None:
        raise not_found("用户不存在")
    user.is_platform_operator = False
    audit.record(session, "platform.revoke_operator", actor, target=user.id)
    return user
