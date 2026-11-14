"""Security primitives (stdlib-first; see DEC-003).

- Password hashing: argon2 if `argon2-cffi` is installed, else stdlib scrypt.
  Encoded strings are self-describing so verification picks the right scheme.
- Session tokens: compact HMAC-signed token carrying session id + expiry.
  Revocation is enforced server-side via the sessions table (Session.revoked).
- TOTP: RFC 6238 via hmac/hashlib (no external dep).
- CSRF: opaque per-session token, double-submit cookie + header check.

Never log or store plaintext secrets. scrypt params chosen for interactive login.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

# --- optional argon2 backend -------------------------------------------------
try:  # pragma: no cover - depends on environment
    from argon2 import PasswordHasher as _Argon2
    _argon2 = _Argon2()
except Exception:  # not installed -> use scrypt
    _argon2 = None

# scrypt params: N=16384, r=8, p=1 -> ~16 MiB working memory. Strong interactive
# workfactor and safely under OpenSSL's default 32 MiB maxmem. We also pass an
# explicit maxmem so hashlib never errors or silently falls back on stricter builds.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = (1 << 14), 8, 1


def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    # OpenSSL needs ~128 * N * r * p bytes; add headroom. Floor at 32 MiB.
    return max(32 * 1024 * 1024, 128 * n * r * p + (1 << 20))


# --- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    if _argon2 is not None:
        return _argon2.hash(password)
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32, maxmem=_scrypt_maxmem(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P))
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, encoded: str) -> bool:
    if not encoded:
        return False
    if encoded.startswith("$argon2"):
        if _argon2 is None:
            return False  # hashed with argon2 but lib missing now
        try:
            return _argon2.verify(encoded, password)
        except Exception:
            return False
    try:
        _, n, r, p, salt_b64, dk_b64 = encoded.split("$")
        n, r, p = int(n), int(r), int(p)
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                             dklen=len(want), maxmem=_scrypt_maxmem(n, r, p))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


# --- signed session tokens ---------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64ud(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_token(payload: dict, secret: str) -> str:
    body = _b64u(str(payload).encode())  # small payloads only (ids/expiry)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def verify_token(token: str, secret: str) -> dict | None:
    try:
        body, sig_b64 = token.split(".")
        sig = _b64ud(sig_b64)
        exp = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, exp):
            return None
        import ast
        data = ast.literal_eval(_b64ud(body).decode())
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


# --- TOTP (RFC 6238) ---------------------------------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret_b32: str, at_time: int | None = None, step: int = 30, digits: int = 6) -> str:
    if at_time is None:
        at_time = int(time.time())
    counter = at_time // step
    key = _b32decode(secret_b32)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_verify(secret_b32: str, code: str, window: int = 1, step: int = 30) -> bool:
    if not secret_b32 or not code:
        return False
    now_t = int(time.time())
    for drift in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret_b32, now_t + drift * step), code.strip()):
            return True
    return False


def _b32decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 8)
    return base64.b32decode(s.upper() + pad)


def otpauth_uri(username: str, secret_b32: str, issuer: str = "Hearth") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret_b32}&issuer={quote(issuer)}"
