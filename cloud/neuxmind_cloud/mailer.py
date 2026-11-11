"""Outgoing e-mail (verification links).

* :class:`LogEmailSender` — **DEV/TEST ONLY — NO E-MAIL IS SENT.** Keeps the
  last messages in memory (``outbox``) and writes them to the
  ``neuxmind_cloud.mail`` logger. Refused by config outside ``dev``/``test``.
* :class:`SmtpEmailSender` — plain ``smtplib`` with STARTTLS. The password is
  read from the environment variable named by ``NEUXMIND_SMTP_PASSWORD_REF``.
  **Not verified against a real mail server in this iteration.**
* No sender (prod/staging default): registration still works, but
  verification e-mails cannot be sent; the resend endpoint answers 503
  ``email_sender_not_configured`` and operators verify with the
  ``verify-email`` management command.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from collections import deque
from email.message import EmailMessage

from .config import Settings
from .errors import ApiError

log = logging.getLogger("neuxmind_cloud.mail")


class EmailSendFailed(Exception):
    pass


def sender_not_configured() -> ApiError:
    return ApiError(503, "email_sender_not_configured",
                    "服务端未配置邮件发送，无法发送验证邮件；请联系平台运营员用 verify-email 命令验证")


class EmailSender:
    name = "base"
    simulated = False

    def send(self, to: str, subject: str, body: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class LogEmailSender(EmailSender):
    """DEV/TEST ONLY: records messages instead of sending them (``simulated``)."""

    name = "log"
    simulated = True

    def __init__(self, keep: int = 500):
        self.outbox: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def send(self, to: str, subject: str, body: str) -> None:
        with self._lock:
            self.outbox.append({"to": to, "subject": subject, "body": body})
        log.info("[simulated e-mail, not sent] to=%s subject=%s\n%s", to, subject, body)

    def last_to(self, to: str) -> dict | None:
        with self._lock:
            for message in reversed(self.outbox):
                if message["to"] == to:
                    return message
        return None


class SmtpEmailSender(EmailSender):
    name = "smtp"

    def __init__(self, settings: Settings):
        if not settings.smtp_host or not settings.smtp_from:
            raise ValueError("NEUXMIND_EMAIL_SENDER=smtp 需要 NEUXMIND_SMTP_HOST 与 NEUXMIND_SMTP_FROM")
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.smtp_from, to, subject
        message.set_content(body)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                if s.smtp_starttls:
                    smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, os.environ.get(s.smtp_password_ref, "") if s.smtp_password_ref else "")
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("smtp send failed: %s", exc)
            raise EmailSendFailed(type(exc).__name__) from None


def build_sender(settings: Settings) -> EmailSender | None:
    kind = settings.effective_email_sender
    if kind == "log":
        return LogEmailSender()
    if kind == "smtp":
        return SmtpEmailSender(settings)
    return None


def verification_message(settings: Settings, token: str) -> tuple[str, str]:
    body = ("请确认这是你的邮箱，以便在 NeuxMind 购买点数和调用模型网关。\n\n"
            f"验证令牌：{token}\n"
            f"提交方式：POST {settings.public_base_url}/api/v1/auth/verify-email  {{\"token\": \"<上面的令牌>\"}}\n"
            "（或在 nx / 控制台的“验证邮箱”里粘贴令牌）\n\n"
            f"令牌 {settings.email_verify_ttl_seconds // 3600} 小时内有效，只能使用一次。如果不是你本人注册，请忽略本邮件。")
    return "验证你的 NeuxMind 邮箱", body


def recovery_message(settings: Settings, token: str, purpose: str) -> tuple[str, str]:
    hours = settings.email_verify_ttl_seconds // 3600
    if purpose == "claim":
        return "认领你的 NeuxMind 邮箱", (
            "有人用这个邮箱注册了 NeuxMind 账户，但尚未验证。如果这个邮箱是你的，"
            "可以用下面的令牌认领：你会得到一个新的、已验证的账户，原来那个（不是你设的密码的）账户会被停用。\n\n"
            f"认领令牌：{token}\n"
            f"提交方式：POST {settings.public_base_url}/api/v1/auth/claim  "
            "{\"token\": \"<令牌>\", \"password\": \"<你的新密码>\"}\n\n"
            f"令牌 {hours} 小时内有效，只能使用一次。如果你没有操作过，忽略本邮件即可。")
    return "重设你的 NeuxMind 密码", (
        f"重设密码令牌：{token}\n"
        f"提交方式：POST {settings.public_base_url}/api/v1/auth/password-reset  "
        "{\"token\": \"<令牌>\", \"password\": \"<新密码>\"}\n"
        "重设后该账户所有已登录的会话都会失效。\n\n"
        f"令牌 {hours} 小时内有效，只能使用一次。如果不是你本人申请，忽略本邮件即可。")
