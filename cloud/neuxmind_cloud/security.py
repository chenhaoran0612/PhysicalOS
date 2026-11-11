"""Password hashing, opaque token generation and HMAC helpers (stdlib only)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

# scrypt parameters. Production uses N=2^17, r=8, p=1 (~128 MiB per hash, the
# common recommendation). The parameters are stored with every hash, so older
# hashes (N=2^14 from iteration 01) still verify and are re-hashed on the next
# successful login (see :func:`needs_rehash`). dev/test may lower N for speed
# via ``Settings.scrypt_log_n`` (refused outside dev/test by config).
_SCRYPT_R, _SCRYPT_P = 8, 1
_scrypt_n = 2 ** 17
# Bound concurrent scrypt evaluations so a login flood cannot exhaust memory:
# at most 4 × 128 MiB in flight; other requests queue.
_HASH_SLOTS = threading.BoundedSemaphore(4)
_MAX_N = 2 ** 20

API_KEY_PREFIX = "nx_live_"
SESSION_PREFIX = "nxs_"
GATEWAY_SESSION_PREFIX = "nxgw_"
EMAIL_TOKEN_PREFIX = "nxv_"


def configure(log_n: int) -> None:
    """Set the scrypt cost for *new* hashes (called by ``create_app``)."""
    global _scrypt_n, _dummy_hash
    if not 10 <= log_n <= 20:
        raise ValueError("scrypt log_n 须为 10–20")
    if 2 ** log_n != _scrypt_n:
        _scrypt_n = 2 ** log_n
        _dummy_hash = None


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _HASH_SLOTS:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32,
                              maxmem=256 * r * n + 16 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _scrypt_n, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_scrypt_n}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt" or not 1 < int(n) <= _MAX_N or not 0 < int(r) <= 32 or not 0 < int(p) <= 16:
            return False
        digest = _scrypt(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def needs_rehash(stored: str) -> bool:
    try:
        algo, n, r, p, _salt, _digest = stored.split("$")
        return algo != "scrypt" or int(n) < _scrypt_n or int(r) != _SCRYPT_R or int(p) != _SCRYPT_P
    except ValueError:
        return True


_dummy_hash: str | None = None


def dummy_password_hash() -> str:
    """A fixed dummy hash at the current cost, to keep login timing similar for unknown emails."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_hex(16))
    return _dummy_hash


def token_hash(token: str) -> str:
    """Tokens are high-entropy random strings, so a plain SHA-256 is sufficient."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def new_gateway_token() -> str:
    return GATEWAY_SESSION_PREFIX + secrets.token_urlsafe(32)


def new_email_token() -> str:
    return EMAIL_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
