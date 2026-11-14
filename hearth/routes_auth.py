"""Auth routes: login, logout, TOTP enroll/verify, self-info.

Login flow: rate-limit (ip+username) -> verify password -> if TOTP enrolled,
require a valid code before issuing session. Issues an HMAC-signed cookie that
carries the DB session id; server-side revocation supported. Sets a CSRF cookie.
"""
from __future__ import annotations
import json
import time

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .config import get_boot_settings
from .db import session_scope
from .models import User, Session
from .security import (verify_password, sign_token, new_csrf_token, totp_verify,
                       new_totp_secret, otpauth_uri)
from .ratelimit import get_limiter
from .deps import current_user, require_user, client_ip
from . import audit

router = APIRouter()


def _set_session_cookies(resp: Response, sid: str) -> None:
    bs = get_boot_settings()
    ttl = bs.session_ttl_hours * 3600
    token = sign_token({"sid": sid, "exp": int(time.time()) + ttl}, bs.secret_key)
    # SameSite is configurable (see BootSettings.cookie_samesite): Strict by default,
    # Lax for LAN exposure where the app is reached from more than one origin.
    ss = bs.normalized_cookie_samesite
    resp.set_cookie(bs.cookie_name, token, max_age=ttl, httponly=True,
                    secure=bs.cookie_secure, samesite=ss, path="/")
    resp.set_cookie(bs.csrf_cookie_name, new_csrf_token(), httponly=False,
                    secure=bs.cookie_secure, samesite=ss, path="/")


@router.post("/auth/login")
async def login(request: Request, response: Response, username: str = Form(...),
                password: str = Form(""), totp: str = Form("")):
    ip = client_ip(request)
    limiter = get_limiter()
    locked, remaining = limiter.is_locked(ip, username)
    if locked:
        audit.log(username, "login_locked", {"ip": ip})
        return JSONResponse({"error": f"Too many attempts. Try again in {remaining}s."},
                            status_code=429)

    with session_scope() as s:
        u = s.query(User).filter(User.username == username).one_or_none()
        ok = bool(u and u.is_active and verify_password(password, u.pw_hash))
        uid, totp_secret, is_admin = (u.id if u else "", getattr(u, "totp_secret_enc", ""),
                                       bool(u and u.is_admin)) if u else ("", "", False)

    if not ok:
        limiter.record_failure(ip, username)
        audit.log(username, "login_fail", {"ip": ip})
        return JSONResponse({"error": "Invalid username or password."}, status_code=401)

    if totp_secret and not totp_verify(totp_secret, totp):
        limiter.record_failure(ip, username)
        audit.log(uid, "login_totp_fail", {"ip": ip})
        return JSONResponse({"error": "Invalid 2FA code.", "need_totp": True}, status_code=401)

    limiter.record_success(ip, username)
    with session_scope() as s:
        sess = Session(user_id=uid, expires_at=int(time.time()) + bs_ttl(), user_agent=request.headers.get("user-agent", "")[:200])
        s.add(sess)
        s.flush()
        sid = sess.id

    audit.log(uid, "login_ok", {"ip": ip})
    resp = JSONResponse({"ok": True, "redirect": "/chat", "username": username})
    _set_session_cookies(resp, sid)
    return resp


def bs_ttl() -> int:
    return get_boot_settings().session_ttl_hours * 3600


@router.post("/auth/logout")
async def logout(request: Request):
    user = current_user(request)
    token = request.cookies.get(get_boot_settings().cookie_name)
    if token:
        from .security import verify_token
        data = verify_token(token, get_boot_settings().secret_key) or {}
        with session_scope() as s:
            sess = s.get(Session, data.get("sid"))
            if sess:
                sess.revoked = True
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(get_boot_settings().cookie_name, path="/")
    if user:
        audit.log(user.id, "logout")
    return resp


@router.post("/auth/logout_all")
async def logout_all(request: Request, user: User = Depends(require_user)):
    with session_scope() as s:
        for sess in s.query(Session).filter(Session.user_id == user.id).all():
            sess.revoked = True
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(get_boot_settings().cookie_name, path="/")
    return resp


@router.get("/auth/me")
async def me(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    from . import i18n
    lang = i18n.normalize(user.language)
    try:
        prefs = json.loads(user.prefs or "{}")
        if not isinstance(prefs, dict):
            prefs = {}
    except Exception:  # noqa: BLE001 — corrupt blob behaves like no prefs
        prefs = {}
    return JSONResponse({"authenticated": True, "username": user.username,
                         "display_name": user.display_name, "is_admin": user.is_admin,
                         "has_totp": bool(user.totp_secret_enc), "language": lang,
                         "system_prompt": user.system_prompt or "",
                         "prefs": prefs})


@router.get("/api/i18n")
async def i18n_dict(request: Request):
    """Return the UI string table for the current user's language (public-safe)."""
    from . import i18n
    user = current_user(request)
    lang = i18n.normalize(user.language if user else request.cookies.get("hearth_lang"))
    return JSONResponse({"language": lang, "strings": i18n.dict_for(lang)})


@router.post("/auth/totp/enroll")
async def totp_enroll(request: Request, user: User = Depends(require_user)):
    secret = new_totp_secret()
    with session_scope() as s:
        u = s.get(User, user.id)
        u.totp_secret_enc = secret  # not active until verified below
    return JSONResponse({"secret": secret, "otpauth": otpauth_uri(user.username, secret)})


@router.post("/auth/totp/verify")
async def totp_verify_endpoint(request: Request, code: str = Form(...),
                               user: User = Depends(require_user)):
    # During enrollment the secret is already stored; verify against it.
    if not user.totp_secret_enc:
        return JSONResponse({"error": "Not enrolled. Call enroll first."}, status_code=400)
    if not totp_verify(user.totp_secret_enc, code):
        return JSONResponse({"error": "Invalid code"}, status_code=400)
    audit.log(user.id, "totp_enabled")
    return JSONResponse({"ok": True})
