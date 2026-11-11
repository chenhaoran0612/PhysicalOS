"""Account recovery: claiming an address, password reset, server-side operator accounts.

Rereview finding N-01: an attacker registers someone else's address (e.g. an
allow-listed operator address) with a password of their choice; the owner
cannot register (409) and has no password reset. Iteration-01 verification
then handed the attacker's account a verified address. The rules now:

* **Claim** — registering an address held by an *unverified* account mails a
  claim token to the address. ``POST /auth/claim`` (or
  ``/auth/password-reset``) with that token and a new password creates a
  **fresh, verified** account for the token holder and disables the old one:
  its address is renamed to ``claimed+<id>@invalid``, its password made
  unusable, every session deleted, every API key revoked, its operator flag
  cleared. Earlier claim tokens stay valid (so a squatter re-registering
  cannot cancel the owner's token).
* **Password reset** — ``POST /auth/password-reset/request {email}`` always
  answers 202 (no account enumeration). Verified accounts get a reset token;
  unverified accounts get a claim token. Redeeming a reset token sets the
  password and deletes **all** sessions of the account. API keys are kept
  (robots depend on them); the owner should review ``/api-keys`` afterwards.
* **create-operator** (management command) creates a verified operator
  account with an unusable password and returns a one-time set-password
  token; nobody ever chose that account's password except the token holder.
* **verify-email** (management command) verifies an address out of band, but
  because the account's password may have been chosen by someone else, it
  also makes the password unusable, deletes sessions and revokes API keys
  (``--force`` is required when any exist) and returns a set-password token
  for the verified owner.

All tokens are stored as SHA-256 only, are single-use and expire
(``email_verify_ttl_seconds``).
"""
from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import audit
from .auth import (_email, _issue_session, _password, _throttle, active_credentials, create_account, revoke_credentials,
                   user_view)
from .config import Settings
from .errors import ApiError, bad_request, conflict, not_found
from .models import EmailVerificationToken, User, now
from .security import hash_password, new_email_token, token_hash

PURPOSES = ("verify", "reset", "claim")


def unusable_password() -> str:
    """Never verifies (not in ``scrypt$...`` format)."""
    return "!unusable!" + secrets.token_hex(16)


def _issue(session: Session, user: User, purpose: str, settings: Settings, *, replace: bool) -> str:
    if replace:
        session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id,
                                                             EmailVerificationToken.purpose == purpose))
    token = new_email_token()
    session.add(EmailVerificationToken(token_hash=token_hash(token), user_id=user.id, email=user.email,
                                       purpose=purpose, expires_at=now() + settings.email_verify_ttl_seconds))
    audit.record(session, f"auth.{purpose}_issued", "system", target=user.email)
    return token


def issue_claim(session: Session, user: User, settings: Settings, ip: str = "") -> str:
    _throttle(session, "claim_send", user.email, settings.email_verify_max_sends_per_hour, 3600,
              "该邮箱的认领邮件发送过于频繁，请稍后再试")
    return _issue(session, user, "claim", settings, replace=False)


def request_reset(session: Session, payload: dict, settings: Settings, ip: str = "") -> tuple[str, str, str] | None:
    """Returns ``(email, token, purpose)`` to send, or None. The caller always answers 202."""
    email = _email((payload or {}).get("email") if isinstance(payload, dict) else None)
    _throttle(session, "reset_ip", ip[:64] or "-", settings.register_ip_max_per_hour, 3600,
              "该网络地址找回密码过于频繁，请稍后再试")
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        return None
    if not user.email_verified:
        return email, issue_claim(session, user, settings, ip), "claim"
    _throttle(session, "reset_send", user.id, settings.email_verify_max_sends_per_hour, 3600,
              "找回密码邮件发送过于频繁，请稍后再试")
    return email, _issue(session, user, "reset", settings, replace=True), "reset"


def redeem(session: Session, payload: dict, settings: Settings, ip: str = "") -> dict:
    """``{token, password, name?}`` for a reset or claim token → new login session for the resulting account."""
    if not isinstance(payload, dict):
        raise bad_request("请求体须为对象")
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip() or len(token) > 200:
        raise bad_request("需要令牌 token", code="recovery_token_invalid")
    password = _password(payload.get("password"))
    row = session.get(EmailVerificationToken, token_hash(token.strip()))
    if row is None or row.used_at is not None or row.purpose not in ("reset", "claim"):
        raise bad_request("令牌无效或已使用", code="recovery_token_invalid")
    if row.expires_at <= now():
        raise bad_request("令牌已过期，请重新申请", code="recovery_token_expired")
    old = session.get(User, row.user_id)
    if old is None or old.email != row.email:
        raise bad_request("令牌与账户邮箱不符", code="recovery_token_invalid")
    row.used_at = now()
    if row.purpose == "reset":
        if not old.email_verified:
            raise bad_request("该账户邮箱未验证，请改用认领", code="recovery_token_invalid")
        old.password_hash = hash_password(password)
        session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == old.id,
                                                             EmailVerificationToken.purpose == "reset"))
        revoked = revoke_sessions_only(session, old)
        audit.record(session, "auth.password_reset", f"user:{old.id}", target=old.email, ip=ip, detail=revoked)
        return {**_issue_session(session, old, settings), "user": user_view(old), "purpose": "reset", **revoked}
    # claim: the old account must still be unverified (if its holder verified it
    # from their own session in the meantime, it really was theirs).
    if old.email_verified:
        raise conflict("该邮箱已被验证，认领令牌失效", code="recovery_token_invalid")
    email = old.email
    name = payload.get("name") or email.split("@")[0]
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise bad_request("名称须为 1–100 个字符")
    revoked = disable_account(session, old, reason="claimed")
    session.flush()
    user, org = create_account(session, email, name.strip(), hash_password(password), verified=True)
    audit.record(session, "auth.email_claimed", f"user:{user.id}", org_id=org.id, target=email, ip=ip,
                 detail={"disabled_user": old.id, **revoked})
    return {**_issue_session(session, user, settings), "user": user_view(user), "org_id": org.id,
            "purpose": "claim", "disabled_user_id": old.id}


def revoke_sessions_only(session: Session, user: User) -> dict:
    from .models import AuthSession
    count = session.execute(delete(AuthSession).where(AuthSession.user_id == user.id)).rowcount or 0
    return {"sessions_revoked": count}


def disable_account(session: Session, user: User, *, reason: str) -> dict:
    revoked = revoke_credentials(session, user)
    session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id))
    user.email = f"claimed+{user.id}@invalid"
    user.password_hash = unusable_password()
    user.is_platform_operator = False
    audit.record(session, "auth.account_disabled", "system", target=user.id, detail={"reason": reason, **revoked})
    return revoked


# ----------------------------------------------------------- management commands
def create_operator(session: Session, email: str, settings: Settings, name: str | None = None,
                    actor: str = "cli") -> tuple[User, str]:
    """``python3 -m neuxmind_cloud create-operator <email>``: a verified operator account whose
    password only the holder of the returned one-time token can set."""
    email = _email(email)
    if session.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise conflict("该邮箱已有账户。若是本人已验证的账户，用 grant-operator；"
                       "若是未验证账户（可能被抢注），用 verify-email --force 收回后再 grant-operator",
                       code="email_taken")
    user, _org = create_account(session, email, (name or email.split("@")[0]).strip()[:100], unusable_password(),
                                verified=True, operator=True)
    token = _issue(session, user, "reset", settings, replace=True)
    audit.record(session, "platform.create_operator", actor, target=user.id)
    return user, token


def admin_verify(session: Session, email: str, settings: Settings, *, force: bool = False,
                 actor: str = "cli") -> tuple[User, str, dict]:
    """``verify-email`` command: verify out of band **and** take the account away from whoever set its password."""
    user = session.execute(select(User).where(User.email == _email(email))).scalar_one_or_none()
    if user is None:
        raise not_found("用户不存在")
    active = active_credentials(session, user)
    if (active["sessions"] or active["api_keys"]) and not force:
        raise ApiError(409, "credentials_exist",
                       f"该账户有 {active['sessions']} 个登录会话、{active['api_keys']} 个 API Key；"
                       "它们可能属于设置了该账户密码的其他人。确认后加 --force（会全部吊销）", **active)
    revoked = revoke_credentials(session, user)
    user.password_hash = unusable_password()
    if not user.email_verified:
        user.email_verified, user.email_verified_at = True, now()
    session.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id))
    token = _issue(session, user, "reset", settings, replace=True)
    audit.record(session, "platform.verify_email", actor, target=user.id, detail={**revoked, "forced": force})
    return user, token, revoked
