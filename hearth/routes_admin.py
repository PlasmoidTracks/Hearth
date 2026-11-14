"""Admin routes (all require admin + CSRF for mutations).

  GET  /api/admin/users                 list users
  POST /api/admin/users                 create user {username,password,is_admin}
  POST /api/admin/users/{id}/toggle     enable/disable
  POST /api/admin/users/{id}/reset_pw   set new password
  POST /api/admin/users/{id}/set_admin  promote/demote
  GET  /api/admin/settings              all runtime settings + specs (labels/help)
  POST /api/admin/settings              update settings {key:value,...}
  GET  /api/admin/model/status          provider/server status
  POST /api/admin/model/reload          re-resolve provider (applies model settings)
  POST /api/admin/model/start           launch llama-server {restart?: bool}
  POST /api/admin/model/stop            stop a Hearth-launched llama-server
  GET  /api/admin/docs                  list protocols + system prompts
  POST /api/admin/docs                  upsert doc {kind,name,content,is_default}
  POST /api/admin/docs/{id}/delete      delete doc
  GET  /api/admin/audit                 recent audit log (admin view)

Changing model.* settings flags that a reload is required.
"""
from __future__ import annotations
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import session_scope
from .models import User, Doc, AuditLog, now as _now
from .deps import require_admin, check_csrf
from .security import hash_password
from . import runtime_settings as rs
from .manager import get_manager
from . import audit

router = APIRouter()


# ---- users ------------------------------------------------------------------
@router.get("/api/admin/users")
async def list_users(_: User = Depends(require_admin)):
    with session_scope() as s:
        rows = s.query(User).order_by(User.username).all()
    return JSONResponse([{"id": u.id, "username": u.username, "display_name": u.display_name,
                          "is_admin": u.is_admin, "is_active": u.is_active,
                          "has_totp": bool(u.totp_secret_enc), "created_at": u.created_at}
                         for u in rows])


@router.post("/api/admin/users")
async def create_user(request: Request, admin: User = Depends(require_admin), _: None = Depends(check_csrf)):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(400, "username required and password >= 8 chars")
    with session_scope() as s:
        if s.query(User).filter(User.username == username).one_or_none():
            raise HTTPException(409, "username exists")
        u = User(username=username, pw_hash=hash_password(password),
                 is_admin=bool(body.get("is_admin")), display_name=body.get("display_name", username))
        s.add(u)
        s.flush()
        uid = u.id
    audit.log(admin.id, "user_create", {"username": username})
    return JSONResponse({"id": uid})


@router.post("/api/admin/users/{uid}/toggle")
async def toggle_user(uid: str, admin: User = Depends(require_admin), _: None = Depends(check_csrf)):
    with session_scope() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(404, "no user")
        u.is_active = not u.is_active
        name = u.username
    audit.log(admin.id, "user_toggle", {"username": name, "active": None})
    return JSONResponse({"ok": True})


@router.post("/api/admin/users/{uid}/reset_pw")
async def reset_pw(uid: str, request: Request, admin: User = Depends(require_admin),
                   _: None = Depends(check_csrf)):
    body = await request.json()
    pw = body.get("password") or ""
    if len(pw) < 8:
        raise HTTPException(400, "password >= 8 chars")
    with session_scope() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(404, "no user")
        u.pw_hash = hash_password(pw)
    audit.log(admin.id, "user_reset_pw", {"username": uid})
    return JSONResponse({"ok": True})


@router.post("/api/admin/users/{uid}/set_admin")
async def set_admin(uid: str, request: Request, admin: User = Depends(require_admin),
                    _: None = Depends(check_csrf)):
    body = await request.json()
    with session_scope() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(404, "no user")
        u.is_admin = bool(body.get("is_admin"))
    audit.log(admin.id, "user_set_admin", {"username": uid})
    return JSONResponse({"ok": True})


# ---- settings ---------------------------------------------------------------
@router.get("/api/admin/settings")
async def get_settings(_: User = Depends(require_admin)):
    values = rs.get_all()
    specs = [{"key": s.key, "label": s.label, "type": s.type, "description": s.description,
              "choices": s.choices, "min": s.min, "max": s.max, "group": s.group,
              "requires_reload": s.requires_reload} for s in rs.ALL_SPECS]
    return JSONResponse({"values": values, "specs": specs})


@router.post("/api/admin/settings")
async def update_settings(request: Request, admin: User = Depends(require_admin),
                          _: None = Depends(check_csrf)):
    body = await request.json()
    rs.set_many(body if isinstance(body, dict) else {})
    needs_reload = any(k.startswith("model.") for k in (body or {}))
    audit.log(admin.id, "settings_update", {"keys": list((body or {}).keys())})
    return JSONResponse({"ok": True, "needs_reload": needs_reload})


@router.get("/api/admin/model/status")
async def model_status(_: User = Depends(require_admin)):
    return JSONResponse(await get_manager().status())


@router.post("/api/admin/model/reload")
async def model_reload(admin: User = Depends(require_admin), _: None = Depends(check_csrf)):
    """Re-resolve the provider (applies saved settings if the server is already up).

    Kept for compatibility — prefer /model/start, which actually launches or
    restarts llama-server so new settings take effect.
    """
    audit.log(admin.id, "model_reload")
    return JSONResponse(await get_manager().status())


@router.post("/api/admin/model/start")
async def model_start(request: Request, admin: User = Depends(require_admin),
                      _: None = Depends(check_csrf)):
    """Start llama-server. body {restart?: bool} — restart=True relaunches a running
    server so changed settings (n_ctx, GPU layers, …) apply. Can take minutes while
    weights load."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body means plain start
        pass
    restart = bool(body.get("restart"))
    audit.log(admin.id, "model_restart" if restart else "model_start")
    return JSONResponse(await get_manager().start(force=restart))


@router.post("/api/admin/model/stop")
async def model_stop(admin: User = Depends(require_admin), _: None = Depends(check_csrf)):
    """Stop a llama-server that Hearth launched (external servers are untouched)."""
    audit.log(admin.id, "model_stop")
    return JSONResponse(await get_manager().stop_server())


@router.get("/api/admin/model/log")
async def model_log(tail: int = 65536, _: User = Depends(require_admin)):
    """Tail of the llama-server log (data/runtime/llama-server.log).

    `tail` = max bytes from the end (capped server-side at 1 MiB). This is the
    raw verbose output — model load progress, VRAM/KV allocation, GGML/CUDA
    errors — exactly what a failed launch prints and nothing else exposes.
    """
    return JSONResponse(get_manager().read_log(max_bytes=int(tail or 65536)))


@router.get("/api/admin/model/command")
async def model_command(_: User = Depends(require_admin)):
    """The exact llama-server command Hearth launches right now, shell-quoted, for
    copying into a terminal to debug why it works elsewhere but not here. Built from
    the same build_argv() the launcher uses; probe-gated flags (--mmproj/--kv-unified)
    reflect this machine's binary."""
    return JSONResponse(get_manager().launch_command_preview())


# ---- docs (protocols + system prompts) --------------------------------------
@router.get("/api/admin/docs")
async def list_docs(_: User = Depends(require_admin)):
    with session_scope() as s:
        rows = s.query(Doc).order_by(Doc.kind, Doc.name).all()
    return JSONResponse([{"id": d.id, "kind": d.kind, "name": d.name, "content": d.content,
                          "is_default": d.is_default, "visible_to_users": d.visible_to_users}
                         for d in rows])


@router.post("/api/admin/docs")
async def upsert_doc(request: Request, admin: User = Depends(require_admin),
                     _: None = Depends(check_csrf)):
    body = await request.json()
    kind = body.get("kind")
    if kind not in ("protocol", "system_prompt"):
        raise HTTPException(400, "kind must be protocol|system_prompt")
    with session_scope() as s:
        doc_id = body.get("id")
        d = s.get(Doc, doc_id) if doc_id else Doc(kind=kind)
        if d is None:
            raise HTTPException(404, "no doc")
        d.kind = kind
        d.name = body.get("name", d.name or "Untitled")
        d.content = body.get("content", d.content)
        d.is_default = bool(body.get("is_default"))
        d.visible_to_users = bool(body.get("visible_to_users", True))
        d.updated_at = _now()
        if d.is_default:
            for other in s.query(Doc).filter(Doc.kind == kind, Doc.id != d.id).all():
                other.is_default = False
        did = d.id
    audit.log(admin.id, "doc_upsert", {"kind": kind, "name": body.get("name")})
    return JSONResponse({"id": did})


@router.post("/api/admin/docs/{doc_id}/delete")
async def delete_doc(doc_id: str, admin: User = Depends(require_admin),
                     _: None = Depends(check_csrf)):
    with session_scope() as s:
        d = s.get(Doc, doc_id)
        if d:
            s.delete(d)
    audit.log(admin.id, "doc_delete", {"id": doc_id})
    return JSONResponse({"ok": True})


# ---- audit ------------------------------------------------------------------
@router.get("/api/admin/audit")
async def get_audit(limit: int = 100, _: User = Depends(require_admin)):
    with session_scope() as s:
        rows = s.query(AuditLog).order_by(AuditLog.ts.desc()).limit(min(limit, 500)).all()
    return JSONResponse([{"ts": a.ts, "user_id": a.user_id, "action": a.action, "detail": a.detail}
                         for a in rows])
