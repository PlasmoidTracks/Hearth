"""Hearth FastAPI application factory + startup (bootstrap admin, schema init).

Run with:  uvicorn hearth.main:app --host 127.0.0.1 --port 8080
Or:        python -m hearth.main
"""
from __future__ import annotations
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .config import get_boot_settings
from .db import init_db, session_scope
from .models import User
from .security import hash_password
from . import paths
from . import runtime_settings as rs
from .routes_auth import router as auth_router
from .routes_chat import router as chat_router
from .routes_admin import router as admin_router

PKG_DIR = Path(__file__).parent


def _add_security_headers(app: FastAPI) -> None:
    """Attach security headers to every response via pure-ASGI middleware.

    CSP is deliberately strict on SCRIPTS (only 'self' — no inline, no CDN), which
    is the main XSS defense. Styles allow 'unsafe-inline' because Tailwind's
    runtime JIT injects <style>/inline styles; that relaxation is low-risk and a
    known tradeoff of using the Tailwind dev build without a static-CSS pipeline.

    Headers set: CSP, X-Content-Type-Options, Referrer-Policy, X-Frame-Options,
    and HSTS when serving over TLS (cookie_secure implies TLS).
    """
    from .config import get_boot_settings
    bs = get_boot_settings()
    # frame-ancestors 'none' blocks clickjacking; X-Frame-Options for legacy.
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if bs.cookie_secure:  # serving over HTTPS -> enable HSTS (1 year)
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def create_app() -> FastAPI:
    paths.ensure_dirs()
    init_db()
    _bootstrap_admin()
    _seed_docs()

    app = FastAPI(title="Hearth", version="0.1.0")
    _add_security_headers(app)
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(admin_router)

    templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))

    # ---- page routes --------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        from .deps import current_user
        return RedirectResponse("/chat" if current_user(request) else "/login")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request):
        from .deps import current_user
        if not current_user(request):
            return RedirectResponse("/login")
        return templates.TemplateResponse(request, "chat.html", {"user": current_user(request)})

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        from .deps import current_user
        u = current_user(request)
        if not u:
            return RedirectResponse("/login")
        if not u.is_admin:
            return HTMLResponse("Forbidden: admin only", status_code=403)
        return templates.TemplateResponse(request, "admin.html", {"user": u})

    @app.get("/healthz")
    async def healthz():
        """Liveness probe for the chat UI's model badge.

        Uses manager.probe(), NOT status(): status() resolves the configured
        backend and reports the provider chats *would* use, which previously made
        every chat page load start llama-server (DEC-048). The badge only needs to
        know whether a real server answers right now — that is all this reports,
        and it never launches anything.
        """
        from .manager import get_manager
        return JSONResponse({"ok": True, "model": await get_manager().probe()})

    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
    return app


def _bootstrap_admin() -> None:
    bs = get_boot_settings()
    with session_scope() as s:
        if s.query(User).count() > 0:
            return
        pw = bs.bootstrap_admin_password or secrets.token_urlsafe(12)
        s.add(User(username=bs.bootstrap_admin_user, pw_hash=hash_password(pw),
                   is_admin=True, display_name="Administrator"))
        if not bs.bootstrap_admin_password:
            print("=" * 60)
            print(f"[Hearth] First run. Admin user '{bs.bootstrap_admin_user}' password: {pw}")
            print("         Change it after logging in. This is shown only once.")
            print("=" * 60)


def ensure_bootstrap_admin(password: str | None = None) -> str:
    """Idempotently guarantee a usable admin account, and return the password to use.

    Exists for tests. ``_bootstrap_admin()`` deliberately does nothing once *any* user
    exists, which is correct for production but makes HTTP tests order-dependent: an admin
    created by an earlier request has an unknown random password, so login attempts 401 and
    the test silently skips (the pre-existing upload test had this). Here we either create
    the admin with a known password or reset an existing one's password to it, so the caller
    always knows what to sign in with.

    SECURITY: never call this from application code paths. Production boot uses
    ``_bootstrap_admin()`` only. Test entry point, nothing else.
    """
    pw = password or "testpass123"
    bs = get_boot_settings()
    username = bs.bootstrap_admin_user or "admin"
    with session_scope() as s:
        user = s.query(User).filter(User.username == username).first()
        if user is None:
            user = User(username=username, pw_hash=hash_password(pw), is_admin=True,
                        display_name="Administrator")
            s.add(user)
        else:
            user.pw_hash = hash_password(pw)
            user.is_admin = True
    return pw


def _seed_docs() -> None:
    """Create a default system prompt + example protocol if none exist."""
    from .models import Doc
    with session_scope() as s:
        if not s.query(Doc).filter(Doc.kind == "system_prompt").count():
            s.add(Doc(kind="system_prompt", name="Default", is_default=True, visible_to_users=True,
                      content="You are a helpful, concise assistant for a family home server. "
                              "Prefer safe defaults and explain what you did."))
        if not s.query(Doc).filter(Doc.kind == "protocol").count():
            s.add(Doc(kind="protocol", name="Coder", is_default=False, visible_to_users=True,
                      content="# Coder protocol\nYou help with programming. Show minimal runnable "
                              "examples. Use the workspace to save files. Run commands only via the "
                              "gated exec tool after explaining what and why."))


app = create_app()


def main():
    import uvicorn
    bs = get_boot_settings()
    if bs.host not in ("127.0.0.1", "localhost", "::1"):
        print("=" * 60)
        print(f"[Hearth] Binding to {bs.host} — the server is reachable from the local network.")
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))      # no packets sent; just picks the outbound iface
            print(f"         Family can open:  http://{s.getsockname()[0]}:{bs.port}")
            s.close()
        except OSError:
            pass                            # no route to anywhere — skip the hint, not startup
        if not bs.cookie_secure:
            print("         WARNING: plain HTTP on the LAN — traffic and cookies are unencrypted.")
            print("         Prefer a TLS front (caddy / 'tailscale serve') and HEARTH_COOKIE_SECURE=true.")
        # SameSite=Strict blocks a legit request when the same app is reached from another
        # origin than the one that set the cookie (localhost here, LAN IP there). Say so up
        # front instead of leaving the operator with "page loads, every send fails".
        if bs.normalized_cookie_samesite == "strict":
            print("         If a device can load the page but sends fail: HEARTH_COOKIE_SAMESITE=lax")
            print("         (still CSRF-safe). Create accounts under Admin → Users.")
    uvicorn.run("hearth.main:app", host=bs.host, port=bs.port, reload=False)


if __name__ == "__main__":
    main()
