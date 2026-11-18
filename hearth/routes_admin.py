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
  GET  /api/admin/captioner/test         caption a generated 2 s tone — end-to-end wiring check
  GET  /api/admin/diffusion/status       stable-diffusion.cpp binary/model presence + why not
  GET  /api/admin/diffusion/command      the exact `sd-cli …` command, shell-quoted
  POST /api/admin/diffusion/test         draw one tiny test image (nothing stored)
  GET  /api/admin/docs                  list protocols + system prompts
  POST /api/admin/docs                  upsert doc {kind,name,content,is_default}
  POST /api/admin/docs/{id}/delete      delete doc
  GET  /api/admin/audit                 recent audit log (admin view)
  GET  /api/admin/project               linked-folder status: target, diff counts, backups
  POST /api/admin/project/link          link a server-side folder {target}
  POST /api/admin/project/unlink        unlink (optionally delete the workspace copy)
  POST /api/admin/project/import        (re)copy the folder into project/ + refresh baseline
  POST /api/admin/project/revert        restore files from an apply backup {backup_id}

Changing model.* settings flags that a reload is required.
"""
from __future__ import annotations
import asyncio
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import session_scope
from .models import User, Doc, AuditLog, now as _now
from .deps import require_admin, check_csrf
from .security import hash_password
from . import runtime_settings as rs
from .manager import get_manager
from . import audit
from . import project_link as pl
from . import project_apply as pa
from . import crypto

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


@router.get("/api/admin/models")
async def all_models(_: User = Depends(require_admin)):
    """Every model Hearth knows about, in one call: chat model + audio captioner.

    The admin UI's "loaded models" view uses this so a second backend cannot drift out of
    the picture — each entry reports what is CONFIGURED and what is actually RUNNING, with
    separate hints. Read-only and defensive: if the captioner probe fails, its entry says so
    rather than taking the whole listing (and therefore the Model panel) down with it.
    """
    from .captioner import get_captioner
    models = [{"id": "chat", "role": "Main chat model", "kind": "llm"}]
    try:
        st = await get_manager().status()
    except Exception as e:  # noqa: BLE001 — a status probe must never break the panel
        st = {"hint": f"status unavailable: {type(e).__name__}: {str(e)[:120]}"}
    models[0].update(st)

    try:
        cap_st = await get_captioner().status()
    except Exception as e:  # noqa: BLE001
        cap_st = {"enabled": False, "reachable": False, "configured": False,
                  "hint": f"captioner status unavailable: {type(e).__name__}: {str(e)[:120]}"}
    models.append({"id": "captioner", "role": "Audio captioner", "kind": "audio", **cap_st})

    # The third model kind has no server to reach, so its row is about files on disk rather
    # than a live port. Same defensive rule: a broken probe degrades to a hint, never takes
    # the listing (and therefore the Model panel) down with it. `status()` is synchronous and
    # only stats files, so it needs no await (unlike the captioner's network probe).
    try:
        from .diffusion import get_diffusion
        dif_st = get_diffusion().status()
    except Exception as e:  # noqa: BLE001
        dif_st = {"enabled": False, "reachable": False, "configured": False,
                  "hint": f"image generation status unavailable: {type(e).__name__}: "
                          f"{str(e)[:120]}"}
    models.append({"id": "diffusion", "role": "Image generator", "kind": "diffusion", **dif_st})
    return JSONResponse({"models": models})


@router.post("/api/admin/captioner/start")
async def captioner_start(request: Request, admin: User = Depends(require_admin),
                          _: None = Depends(check_csrf)):
    """Start (or restart, with {restart:true}) the captioner's own llama-server.

    Independent of the chat model: this never touches the server chats are using. Weights
    can take minutes to load, so the response carries the real reason when it fails rather
    than a bare not-running flag.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body means plain start
        pass
    from .captioner import get_captioner
    audit.log(admin.id, "captioner_restart" if body.get("restart") else "captioner_start")
    return JSONResponse(await get_captioner().start(force=bool(body.get("restart"))))


@router.post("/api/admin/captioner/stop")
async def captioner_stop(admin: User = Depends(require_admin), _: None = Depends(check_csrf)):
    """Stop a captioner server Hearth launched (an external one is left alone)."""
    from .captioner import get_captioner
    audit.log(admin.id, "captioner_stop")
    return JSONResponse(await get_captioner().stop_server())


@router.get("/api/admin/captioner/test")
async def captioner_test(q: str = "", _: User = Depends(require_admin)):
    """Caption a short synthetic tone so an admin can verify the wiring end to end.

    Why generate rather than ship a sample file: it proves the whole chain — ffmpeg (or its
    absence), the server request, the audio encoder, and a non-empty answer — without any
    user's recording touching the test. A quiet sine wave is also an honest probe: a model
    that confidently describes music over a beep is telling you something worth knowing.
    """
    from .captioner import get_captioner, CaptionError
    import math
    import struct as _struct
    import tempfile as _tempfile
    import wave as _wave

    settings = rs.get_all()
    cap = get_captioner()
    t0 = time.time()
    try:
        with _tempfile.TemporaryDirectory(prefix="hearth-captest-") as tmp:
            path = os.path.join(tmp, "probe.wav")
            rate, secs = 16000, 2
            with _wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                for i in range(rate * secs):
                    wf.writeframes(_struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440.0
                                                                            * i / rate))))
            res = await asyncio.to_thread(cap.caption_sync, path, settings)
    except CaptionError as e:
        return JSONResponse({"ok": False, "seconds": round(time.time() - t0, 1),
                             "note": str(e)})
    except Exception as e:  # noqa: BLE001 — a test endpoint must not 500
        return JSONResponse({"ok": False, "seconds": round(time.time() - t0, 1),
                             "note": f"{type(e).__name__}: {str(e)[:200]}"})
    caption = str(res.get("caption") or "")
    return JSONResponse({"ok": True, "seconds": round(time.time() - t0, 1),
                         "backend": res.get("backend"), "chunks": res.get("chunks"),
                         "caption": caption[:1200],
                         "note": ("the probe is a 2-second 440 Hz sine tone — if the "
                                  "description below claims speech or lyrics, the model is "
                                  "hallucinating and its prompt needs tightening"
                                  if caption else "empty description returned")})


@router.get("/api/admin/captioner/command")
async def captioner_command(_: User = Depends(require_admin)):
    """The exact captioner llama-server command, shell-quoted, for copying out."""
    from .captioner import launch_command_preview
    return JSONResponse(launch_command_preview(rs.get_all()))


# ---- image generation (stable-diffusion.cpp) --------------------------------
@router.get("/api/admin/diffusion/status")
async def diffusion_status(_: User = Depends(require_admin)):
    """Binary/model presence and the reason generation cannot run, if any.

    Cheap and synchronous by design — nothing is launched, so this can be called on every
    render of the Images tab.
    """
    from .diffusion import get_diffusion
    return JSONResponse(get_diffusion().status())


@router.get("/api/admin/diffusion/command")
async def diffusion_command(_: User = Depends(require_admin)):
    """The exact `sd-cli` command Hearth would build, shell-quoted, for copying out."""
    from .diffusion import launch_command_preview
    return JSONResponse(launch_command_preview(rs.get_all()))


@router.post("/api/admin/diffusion/test")
async def diffusion_test(request: Request, admin: User = Depends(require_admin),
                         _: None = Depends(check_csrf)):
    """Draw one tiny test picture to prove the whole chain end to end.

    Why Hearth generates rather than trusting config: it is the only check that a given
    binary + model (+ encoders) combination actually produces a PNG. A missing t5xxl for
    Flux looks exactly like a working setup right up until sd-cli is asked to draw, and
    this is where an admin wants to find out — not from their family's chat.

    Body: `{prompt?: str}`. The image is discarded; the point is the exit code and whether
    bytes appeared, so nothing is written into any workspace.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no body means the default probe
        pass
    from .diffusion import get_diffusion, DiffusionError
    prompt = str(body.get("prompt") or "a red circle on a white background, simple, flat")[:300]
    settings = rs.get_all()
    dif = get_diffusion()
    t0 = time.time()
    try:
        # Small and short on purpose: this verifies wiring, not quality.
        res = await asyncio.to_thread(
            dif.generate_sync, settings, prompt, width=256, height=256, steps=4, batch=1)
    except DiffusionError as e:
        return JSONResponse({"ok": False, "seconds": round(time.time() - t0, 1),
                             "note": str(e)})
    except Exception as e:  # noqa: BLE001 — a test endpoint must not 500
        return JSONResponse({"ok": False, "seconds": round(time.time() - t0, 1),
                             "note": f"{type(e).__name__}: {str(e)[:200]}"})
    images = res.get("images") or []
    audit.log(admin.id, "diffusion_test", {"ok": bool(images), "seconds": res.get("seconds")})
    return JSONResponse({"ok": bool(images), "seconds": res.get("seconds"),
                         "images": len(images),
                         "bytes": [len(d) for _, d in images],
                         "seed": res.get("seed"),
                         "command": " ".join(str(a) for a in (res.get("argv") or [])[:14]),
                         "note": ""if images else "the CLI succeeded but produced no file"})




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


# ---- linked project folder (DEC-049) ----------------------------------------
# Linking is admin-driven (the target is an absolute path on the SERVER's filesystem, so a
# non-admin picking one would be handing them a read of arbitrary server paths). The agent
# only ever sees the resulting copy under project/ inside its own jail.
@router.get("/api/admin/project")
async def project_status(user: User = Depends(require_admin), _=Depends(check_csrf)):
    return JSONResponse(_project_view(user))


def _project_view(user: User) -> dict:
    link = pl.get_link(user.id)
    if not link:
        return {"linked": False, "backend": pa.detect_backend("auto"),
                "encryption_blocks_apply": crypto.encryption_enabled()}
    info = {"linked": True, "target_abs": link["target_abs"], "prefix": link["rel_prefix"],
            "imported_at": link["imported_at"], "backend": pa.detect_backend("auto"),
            "encryption_blocks_apply": crypto.encryption_enabled()}
    try:
        info["exists"] = pl.target_root(link).is_dir()
    except pl.LinkError:
        info["exists"] = False
    mf = pl.load_manifest(user.id, link)
    info["files"] = len(mf.get("files") or {})
    try:
        cs = pl.diff_project(user.id, link)
        info.update({"added": len(cs.added), "modified": len(cs.modified),
                     "conflicts": len(cs.conflicts), "unchanged": cs.unchanged})
    except pl.LinkError as e:
        info["diff_error"] = str(e)
    info["backups"] = pa.list_backups(user.id, link)
    return info


@router.post("/api/admin/project/link")
async def project_link(request: Request, user: User = Depends(require_admin),
                       _=Depends(check_csrf)):
    body = await request.json()
    try:
        link = pl.set_link(user.id, str(body.get("target", "")))
    except pl.LinkError as e:
        raise HTTPException(400, str(e))
    audit.log(user.id, "project_link", {"target": link["target_abs"]})
    return JSONResponse({"ok": True, **_project_view(user)})


@router.post("/api/admin/project/unlink")
async def project_unlink(request: Request, user: User = Depends(require_admin),
                         _=Depends(check_csrf)):
    body = await request.json()
    link = pl.get_link(user.id)
    cleared = pl.clear_link(user.id, delete_copy=bool(body.get("delete_copy")))
    audit.log(user.id, "project_unlink", {"target": (link or {}).get("target_abs", ""),
                                          "deleted_copy": bool(body.get("delete_copy"))})
    return JSONResponse({"ok": cleared})


@router.post("/api/admin/project/import")
async def project_import(user: User = Depends(require_admin), _=Depends(check_csrf)):
    link = pl.get_link(user.id)
    if not link:
        raise HTTPException(400, "link a folder first")
    try:
        res = pl.import_project(user.id, rs.get_all(), link)
    except pl.LinkError as e:
        raise HTTPException(400, str(e))
    audit.log(user.id, "project_import", {"target": res["target_abs"], "files": res["files"],
                                          "copied": res["copied"], "truncated": res["truncated"]})
    return JSONResponse({"ok": True, **res, **_project_view(user)})


@router.post("/api/admin/project/revert")
async def project_revert(request: Request, user: User = Depends(require_admin),
                         _=Depends(check_csrf)):
    body = await request.json()
    backup_id = str(body.get("backup_id") or "")
    try:
        res = pa.revert(user.id, backup_id)
    except pl.LinkError as e:
        raise HTTPException(400, str(e))
    audit.log(user.id, "project_revert", {"backup_id": backup_id, "restored": res.written[:40],
                                          "refused": res.refused[:20]})
    if not res.ok:
        raise HTTPException(400, res.note + ("; " + "; ".join(res.refused[:3]) if res.refused else ""))
    return JSONResponse({"ok": True, "note": res.note, "restored": res.written})
