"""Runtime configuration, read from environment variables.

Every setting has a safe default. In particular ``NEUXMIND_ENV`` defaults to
``prod`` so that test-only features (the sandbox payment provider and the
Echo upstream) are *off* unless someone explicitly opts into ``dev``/``test``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

TEST_ENVS = ("dev", "test")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 须为整数") from None


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    """All tunables of the cloud service. Construct with ``Settings.from_env()``."""

    env: str = "prod"
    database_url: str = "sqlite:///./neuxmind_cloud.sqlite3"
    public_base_url: str = "http://127.0.0.1:8800"
    operator_emails: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)
    max_body_bytes: int = 1_000_000
    # Gateway session model calls only (review Q3-07): agent runtimes resend the
    # whole conversation every turn. Still bounded by the session budget.
    session_max_body_bytes: int = 8_000_000
    session_ttl_seconds: int = 12 * 3600
    login_max_failures: int = 8
    login_window_seconds: int = 60
    # Per-provider webhook signing secrets, e.g. {"sandbox": "..."}.
    webhook_secrets: dict[str, str] = field(default_factory=dict)
    webhook_tolerance_seconds: int = 300
    upstream_timeout_seconds: int = 60
    # Allow plain-HTTP upstreams that are not loopback (never in prod).
    allow_insecure_upstream: bool = False

    # --- login / registration throttling (C-03) -------------------------
    # Failures per (email, ip) before that pair is refused for the window.
    # ``login_max_failures`` is kept as the name for backwards compatibility.
    login_ip_max_failures: int = 50          # failures per ip (any email) per window
    login_email_soft_limit: int = 20         # failures per email (all ips) before progressive delay
    login_max_delay_seconds: int = 30        # cap of the progressive delay
    register_ip_max_per_hour: int = 20
    # Reverse proxies whose X-Forwarded-For is trusted (exact IPs).
    trusted_proxies: list[str] = field(default_factory=list)

    # --- e-mail verification (C-01) -------------------------------------
    # "" = automatic: the in-memory/log sender in dev/test, none in prod/staging.
    # "log" (dev/test only) | "smtp" | "none".
    email_sender: str = ""
    email_verify_ttl_seconds: int = 24 * 3600
    email_verify_max_sends_per_hour: int = 5
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_ref: str = ""              # NAME of the env var holding the SMTP password
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Honour NEUXMIND_OPERATOR_EMAILS? None = automatic: only in dev/test.
    # In prod/staging it must be switched on explicitly (NEUXMIND_OPERATOR_ALLOWLIST=1);
    # the recommended production path is ``create-operator`` (rereview N-01).
    operator_allowlist_enabled: bool | None = None

    # --- maintenance sweeper (C-05) -------------------------------------
    # Background sweep interval; 0 disables the thread (lazy sweeps still run).
    maintenance_interval_seconds: int = 0
    # A gateway call still "pending" after this long is treated as lost.
    stale_call_seconds: int = 600

    # --- session streaming (§4.3) ----------------------------------------
    # Longest a streamed session call may run; 0 = derived so that a stream
    # always ends (and settles) before the sweeper could declare it lost:
    # stale_call_seconds − upstream_timeout_seconds − 30, at least 10 s.
    stream_max_seconds: int = 0
    # A client that reads nothing for this long is treated as disconnected.
    stream_idle_seconds: int = 30

    # --- password hashing (C-07) ----------------------------------------
    scrypt_log_n: int = 17

    @property
    def is_test_env(self) -> bool:
        return self.env in TEST_ENVS

    def __post_init__(self):
        if self.scrypt_log_n < 17 and not self.is_test_env:
            raise ValueError("NEUXMIND_SCRYPT_LOG_N 低于 17 只允许在 dev/test 环境使用")
        if not 10 <= self.scrypt_log_n <= 20:
            raise ValueError("NEUXMIND_SCRYPT_LOG_N 须为 10–20")
        if self.email_sender not in ("", "log", "smtp", "none"):
            raise ValueError("NEUXMIND_EMAIL_SENDER 须为 log / smtp / none")
        if self.email_sender == "log" and not self.is_test_env:
            raise ValueError("记录型邮件发送器（log）只允许在 dev/test 环境使用")

    @property
    def effective_stream_max_seconds(self) -> float:
        derived = max(10, self.stale_call_seconds - self.upstream_timeout_seconds - 30)
        return min(self.stream_max_seconds, derived) if self.stream_max_seconds > 0 else derived

    @property
    def operator_allowlist_active(self) -> bool:
        if self.operator_allowlist_enabled is None:
            return self.is_test_env
        return bool(self.operator_allowlist_enabled)

    @property
    def effective_email_sender(self) -> str:
        if self.email_sender:
            return self.email_sender
        return "log" if self.is_test_env else "none"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get("NEUXMIND_ENV", "prod").strip().lower() or "prod"
        if env not in ("prod", "staging", "dev", "test"):
            raise ValueError("NEUXMIND_ENV 须为 prod / staging / dev / test")
        secrets = {}
        for provider in ("sandbox", "stripe", "alipay", "wechatpay", "manual"):
            value = os.environ.get(f"NEUXMIND_WEBHOOK_SECRET_{provider.upper()}")
            if value:
                secrets[provider] = value
        return cls(
            env=env,
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            public_base_url=os.environ.get("NEUXMIND_PUBLIC_BASE_URL", cls.public_base_url).rstrip("/"),
            operator_emails=[e.lower() for e in _list("NEUXMIND_OPERATOR_EMAILS")],
            cors_origins=_list("NEUXMIND_CORS_ORIGINS"),
            max_body_bytes=_int("NEUXMIND_MAX_BODY_BYTES", cls.max_body_bytes),
            session_max_body_bytes=_int("NEUXMIND_SESSION_MAX_BODY_BYTES", cls.session_max_body_bytes),
            session_ttl_seconds=_int("NEUXMIND_SESSION_TTL_SECONDS", cls.session_ttl_seconds),
            login_max_failures=_int("NEUXMIND_LOGIN_MAX_FAILURES", cls.login_max_failures),
            login_window_seconds=_int("NEUXMIND_LOGIN_WINDOW_SECONDS", cls.login_window_seconds),
            webhook_secrets=secrets,
            webhook_tolerance_seconds=_int("NEUXMIND_WEBHOOK_TOLERANCE_SECONDS", cls.webhook_tolerance_seconds),
            upstream_timeout_seconds=_int("NEUXMIND_UPSTREAM_TIMEOUT_SECONDS", cls.upstream_timeout_seconds),
            allow_insecure_upstream=(env in TEST_ENVS and os.environ.get("NEUXMIND_ALLOW_INSECURE_UPSTREAM") == "1"),
            login_ip_max_failures=_int("NEUXMIND_LOGIN_IP_MAX_FAILURES", cls.login_ip_max_failures),
            login_email_soft_limit=_int("NEUXMIND_LOGIN_EMAIL_SOFT_LIMIT", cls.login_email_soft_limit),
            login_max_delay_seconds=_int("NEUXMIND_LOGIN_MAX_DELAY_SECONDS", cls.login_max_delay_seconds),
            register_ip_max_per_hour=_int("NEUXMIND_REGISTER_IP_MAX_PER_HOUR", cls.register_ip_max_per_hour),
            trusted_proxies=_list("NEUXMIND_TRUSTED_PROXIES"),
            email_sender=os.environ.get("NEUXMIND_EMAIL_SENDER", "").strip().lower(),
            email_verify_ttl_seconds=_int("NEUXMIND_EMAIL_VERIFY_TTL_SECONDS", cls.email_verify_ttl_seconds),
            email_verify_max_sends_per_hour=_int("NEUXMIND_EMAIL_VERIFY_MAX_SENDS_PER_HOUR",
                                                 cls.email_verify_max_sends_per_hour),
            smtp_host=os.environ.get("NEUXMIND_SMTP_HOST", ""),
            smtp_port=_int("NEUXMIND_SMTP_PORT", cls.smtp_port),
            smtp_user=os.environ.get("NEUXMIND_SMTP_USER", ""),
            smtp_password_ref=os.environ.get("NEUXMIND_SMTP_PASSWORD_REF", ""),
            smtp_from=os.environ.get("NEUXMIND_SMTP_FROM", ""),
            smtp_starttls=os.environ.get("NEUXMIND_SMTP_STARTTLS", "1") != "0",
            # The server process sweeps every 60 s by default; tests construct
            # Settings directly and get the dataclass default (0 = off).
            maintenance_interval_seconds=_int("NEUXMIND_MAINTENANCE_INTERVAL_SECONDS", 60),
            stale_call_seconds=_int("NEUXMIND_STALE_CALL_SECONDS", cls.stale_call_seconds),
            stream_max_seconds=_int("NEUXMIND_STREAM_MAX_SECONDS", cls.stream_max_seconds),
            stream_idle_seconds=_int("NEUXMIND_STREAM_IDLE_SECONDS", cls.stream_idle_seconds),
            scrypt_log_n=_int("NEUXMIND_SCRYPT_LOG_N", cls.scrypt_log_n),
            operator_allowlist_enabled=({"1": True, "0": False}.get(os.environ.get("NEUXMIND_OPERATOR_ALLOWLIST", "").strip())),
        )
