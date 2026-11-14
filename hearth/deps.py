"""FastAPI dependencies: current user resolution, admin gate, CSRF enforcement.

Session cookie -> signed token -> DB session row (must be unrevoked + unexpired)
-> User row (must be active). Tailscale SSO header is honored ONLY when enabled
AND the request comes from a trusted proxy/tailnet IP (boot config), and it maps
to an existing user by username (auto-provision optional, off by default).
"""
from __future__ import annotations
import hmac

from fastapi import Depends, HTTPException, Request, status

from .config import get_boot_settings
from .db import session_scope
from .models import User, Session
from .security import verify_token


def client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For from trusted proxies.
    fwd = request.headers.get("x-forwarded-for")
    if fwd and _is_trusted(request):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _trusted_ips() -> set[str]:
    bs = get_boot_settings()
    return {ip.strip() for ip in bs.trusted_proxy_ips.split(",") if ip.strip()}


def _is_trusted(request: Request) -> bool:
    return (request.client.host if request.client else "") in _trusted_ips()


def current_user(request: Request) -> User | None:
    bs = get_boot_settings()

    # Tailscale identity SSO (optional, trusted-source only)
    if bs.tailscale_sso_enabled and _is_trusted(request):
        ts_login = request.headers.get("tailscale-user-login") or request.headers.get("remote-user")
        if ts_login:
            username = ts_login.split("@")[0]
            with session_scope() as s:
                u = s.query(User).filter(User.username == username).one_or_none()
                if u and u.is_active:
                    return u

    token = request.cookies.get(bs.cookie_name)
    if not token:
        return None
    data = verify_token(token, bs.secret_key)
    if not data:
        return None
    with session_scope() as s:
        sess = s.get(Session, data.get("sid"))
        if not sess or sess.revoked or sess.expires_at < _now():
            return None
        u = s.get(User, sess.user_id)
        return u if (u and u.is_active) else None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin required")
    return user


def check_csrf(request: Request) -> None:
    """Double-submit CSRF check for state-changing requests.

    The session cookie is SameSite=Strict (primary defense); this adds a token
    header that must match the csrf cookie. Skipped for GET/HEAD/OPTIONS.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    hdr = request.headers.get("x-csrf-token", "")
    ck = request.cookies.get(get_boot_settings().csrf_cookie_name, "")
    if not hdr or not ck or not hmac.compare_digest(hdr, ck):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing/mismatch")


def _now() -> int:
    import time
    return int(time.time())
