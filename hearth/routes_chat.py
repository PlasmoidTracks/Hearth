"""Chat routes: conversation CRUD, SSE turn streaming, gated-action resolve,
and signed workspace file download.

Endpoints (all require an authenticated user; data is scoped to that user):
  POST /api/chat/conversations            create -> {id}
  GET  /api/chat/conversations            list user's conversations
  GET  /api/chat/conversations/{id}       history messages
  POST /api/chat/stream                   start a turn (SSE). body: {conversation_id, content?,
                                          attachments?, no_tools?}
  POST /api/chat/resolve                  resolve pending action (SSE). body: {pending_id, approved|answer}
  POST /api/chat/abort                    abort the in-flight turn for a conversation
  GET  /api/chat/running                  is a turn streaming for this conversation?
  POST /api/chat/retry                    drop the last reply and re-stream it (SSE)
  GET  /api/chat/context-usage            tokens used vs context window (context bar)
  GET/POST /api/chat/system-prompt        the caller's personal system prompt (sidebar editor)
  POST/GET /api/chat/device-access        grant state for device tools, per conversation
  GET  /api/download/{token}              serve a workspace file via short-lived signed token

Turn streaming goes through `_stream_turn()`, which owns the abort handle so every
exit path (end, abort, exception, client disconnect) releases the conversation.

The download store lives in app state so tokens are process-scoped & ephemeral.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import mimetypes
import os
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response

from .db import session_scope
from .models import Conversation, Message, PendingAction, Doc, User, Artifact, now as _now
from .deps import require_user, check_csrf
from .manager import get_manager
from . import runtime_settings as rs
from . import orchestrator as orch
from . import artifacts
from . import workspaces as ws
from . import audit
from . import project_link as pl
from . import project_apply as pa
from . import crypto

# Max bytes per uploaded file (defense against trivial DoS). The value is an admin
# setting — uploads.max_bytes — and this constant is only the fallback when settings are
# unreadable, plus the number the client is told before any setting exists. It used to be
# a literal with a "Configurable later" comment above it, duplicated in chat.js, so raising
# it meant patching source in two places (finding #8).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def upload_limit_bytes() -> int:
    """The per-file upload cap in force right now (Admin → Uploads → Max upload size)."""
    try:
        v = int(rs.get("uploads.max_bytes") or 0)
    except (TypeError, ValueError):
        return MAX_UPLOAD_BYTES
    return v if v > 0 else MAX_UPLOAD_BYTES


router = APIRouter()


def _sse(ev: dict) -> str:
    """One SSE frame. `default=str` because usage/args metas can hold non-JSON types."""
    return f"data: {json.dumps(ev, default=str)}\n\n"


def _helper_url() -> str:
    """Paired device-helper base URL (admin setting wins, env as fallback), or ''."""
    return ((rs.get("device.helper_url") or "").strip()
            or os.environ.get("HEARTH_DEVICE_HELPER_URL", ""))


def _download_store(request: Request) -> dict:
    store = getattr(request.app.state, "download_tokens", None)
    if store is None:
        store = {}
        request.app.state.download_tokens = store
    return store


def _prune_downloads(store: dict) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if v.get("exp", 0) < now]:
        store.pop(k, None)


@router.post("/api/chat/conversations")
async def new_conversation(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    with session_scope() as s:
        c = Conversation(user_id=user.id, title="New chat")
        s.add(c)
        s.flush()
        return JSONResponse({"id": c.id})


@router.get("/api/chat/conversations")
async def list_conversations(request: Request, user=Depends(require_user)):
    with session_scope() as s:
        rows = s.query(Conversation).filter(Conversation.user_id == user.id) \
            .order_by(Conversation.updated_at.desc()).limit(100).all()
    return JSONResponse([{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows])


@router.get("/api/chat/docs")
async def selectable_docs(request: Request, user=Depends(require_user)):
    """Protocols + system prompts users may attach to a conversation.

    The chat UI no longer offers a system-prompt picker (the personal prompt editor
    replaced it); this endpoint stays as the API surface for admin-managed docs.
    """
    with session_scope() as s:
        rows = s.query(Doc).filter(Doc.visible_to_users == True).order_by(Doc.kind, Doc.name).all()  # noqa: E712
    return JSONResponse([{"id": d.id, "kind": d.kind, "name": d.name} for d in rows])


# ---- Personal system prompt (per user) --------------------------------------
@router.get("/api/chat/system-prompt")
async def get_system_prompt(request: Request, user=Depends(require_user)):
    """The caller's system prompt as it is actually in effect.

    `content` is what the editor edits and what gets sent (personal prompt, else
    the admin default doc, else the built-in persona), so the panel never shows
    empty while a prompt is active behind it. `default` is the fallback text for
    the Clear button.
    """
    from . import prompt as prompt_mod
    with session_scope() as s:
        u = s.get(User, user.id)
        personal = (u.system_prompt if u else "") or ""
    return JSONResponse({"content": personal or prompt_mod.current_prompt(),
                         "personal": personal,
                         "default": prompt_mod.DEFAULT_PERSONA})


@router.post("/api/chat/system-prompt")
async def set_system_prompt(request: Request, user=Depends(require_user),
                            _=Depends(check_csrf)):
    """Save the caller's system prompt (applies to every chat from the next turn).

    The editor text IS the prompt — Hearth no longer prepends a persona on top. To
    fall back to the admin default doc / built-in persona, send an empty string or
    `use_default: true` (the Clear button does the latter).
    """
    body = await request.json()
    use_default = bool(body.get("use_default"))
    content = "" if use_default else str(body.get("content") or "").strip()
    if len(content) > 8000:
        raise HTTPException(400, "system prompt is too long (8000 characters max)")
    with session_scope() as s:
        u = s.get(User, user.id)
        if not u:
            raise HTTPException(404, "not found")
        u.system_prompt = content
    audit.log(user.id, "system_prompt_update", {"chars": len(content)})
    return JSONResponse({"ok": True, "content": content})


@router.post("/api/chat/conversations/{conv_id}/config")
async def configure_conversation(conv_id: str, request: Request, user=Depends(require_user),
                                 _=Depends(check_csrf)):
    body = await request.json()
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        if "protocol_doc_id" in body:
            c.protocol_doc_id = body["protocol_doc_id"] or ""
        if "system_prompt_doc_id" in body:
            c.system_prompt_doc_id = body["system_prompt_doc_id"] or ""
    return JSONResponse({"ok": True})


@router.get("/api/chat/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request, user=Depends(require_user)):
    # Reasoning retention decides what the transcript shows after a reload. Applied here and
    # NOT in the model request path (orchestrator.build_messages never replays reasoning), so
    # this can only ever change what the user sees, never what the model is sent.
    retention = rs.get_all().get("model.reasoning_retention", "keep_last")
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        msgs = s.query(Message).filter(Message.conversation_id == conv_id) \
            .order_by(Message.created_at.asc()).all()
        rows = [{"role": m.role, "content": m.content, "meta": m.meta,
                 "created_at": m.created_at} for m in msgs]
    # Attach stored caption state so a refresh can re-render the 🎧 block that streamed live
    # (DEC-061). Only a name/ok summary is sent here — the description text itself is already
    # inside `meta`, and re-sending it as a second key would double the payload of every
    # audio chat. Rows without captions are untouched, so nothing else grows.
    for r in rows:
        if r.get("role") != "user" or not r.get("meta"):
            continue
        state = orch.caption_state(r["meta"])
        if state:
            r["captions"] = state
    return JSONResponse(_apply_reasoning_retention(rows, retention))


def _apply_reasoning_retention(rows: list[dict], policy: str) -> list[dict]:
    """Attach/drop stored reasoning on a transcript row set according to the admin policy.

    Rows gain a `reasoning` key (parsed out of meta) for the client's collapsible block; the
    policy decides how many survive. Only rows that change are rewritten.

    'keep_last' keeps the LAST assistant row's reasoning (the one a user is still reading) and
    hides earlier ones. 'drop' and 'hide' remove all of them from history — the live one is
    unaffected because a `reasoning` SSE event arrives before any policy could be consulted.
    Unknown values behave like keep_last rather than dropping content nobody asked to lose.

    Applied ONLY here (never in orchestrator.build_messages), so this can change what the user
    sees but never what the model is sent.
    """
    last_ai = -1
    if policy == "keep_last":
        for i, r in enumerate(rows):
            if r.get("role") == "assistant":
                last_ai = i
    for i, r in enumerate(rows):
        if r.get("role") != "assistant" or not r.get("meta"):
            continue
        try:
            meta = json.loads(r["meta"])
        except Exception:  # noqa: BLE001 — non-JSON meta is not ours to touch
            continue
        if not isinstance(meta, dict):
            continue
        text = meta.get("reasoning")
        if not text:
            continue
        if policy == "keep_all" or (policy == "keep_last" and i == last_ai):
            r["reasoning"] = text
            continue
        # Dropped from the transcript view. meta stays untouched in the DB — retention is a
        # display decision, not a deletion, so changing the policy later restores it.
        continue
    return rows


@router.post("/api/chat/conversations/{conv_id}/rename")
async def rename_conversation(conv_id: str, request: Request, user=Depends(require_user),
                              _=Depends(check_csrf)):
    body = await request.json()
    title = (body.get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(400, "title required")
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        c.title = title
    return JSONResponse({"ok": True, "title": title})


@router.post("/api/chat/conversations/{conv_id}/delete")
async def delete_conversation(conv_id: str, request: Request, user=Depends(require_user),
                              _=Depends(check_csrf)):
    """Delete a conversation. Body: {delete_artifacts?: bool}.

    Messages are always removed. Artifacts (files produced/uploaded in this conversation)
    are deleted only when delete_artifacts is true — otherwise their rows AND workspace
    files remain, so the user keeps the files after the chat is gone.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no/invalid body defaults to keeping artifacts
        pass
    delete_artifacts = bool(body.get("delete_artifacts"))
    # Drop any steering queued for this conversation. It lives in the server process keyed by
    # conversation id, so without this a deleted chat's queue would sit in memory forever and
    # could in principle be drained by whoever next reused the id (ids are random, but a
    # deleted conversation must not be able to deliver messages into anything).
    from . import steering as _steering
    _steering.clear(conv_id)
    removed_files = 0
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        arts = s.query(Artifact).filter(Artifact.conversation_id == conv_id,
                                         Artifact.user_id == user.id).all()
        if delete_artifacts:
            for a in arts:
                try:
                    p = ws.resolve_in_jail(user.id, a.rel_path)
                    if os.path.isfile(p):
                        os.remove(p); removed_files += 1
                except (ws.JailEscape, FileNotFoundError):
                    pass
                s.delete(a)
        else:
            # Detach artifacts from the deleted conversation so they show under "all".
            for a in arts:
                a.conversation_id = ""
        s.query(Message).filter(Message.conversation_id == conv_id).delete(synchronize_session=False)
        s.delete(c)
    audit.log(user.id, "conversation_delete", {"id": conv_id, "artifacts": delete_artifacts})
    return JSONResponse({"ok": True, "artifacts_deleted": removed_files if delete_artifacts else 0})


@router.get("/api/chat/search")
async def search_conversations(request: Request, q: str = "", user=Depends(require_user)):
    """Search the caller's conversations.

    Primary match on title; secondary match on message content (case-insensitive LIKE).
    Returns each matching conversation once, with a short snippet of the matched content.
    """
    q = (q or "").strip()
    if not q:
        return JSONResponse([])
    like = f"%{q}%"
    results: dict[str, dict] = {}
    with session_scope() as s:
        for c in s.query(Conversation).filter(Conversation.user_id == user.id,
                                              Conversation.title.ilike(like)).all():
            results[c.id] = {"id": c.id, "title": c.title, "updated_at": c.updated_at,
                             "match": "title", "snippet": ""}
        # secondary: content match (only conversations not already matched by title)
        rows = (s.query(Message, Conversation)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.user_id == user.id, Message.content.ilike(like))
                .order_by(Message.created_at.desc()).limit(200).all())
        for m, c in rows:
            if c.id not in results:
                snippet = (m.content or "")[:120]
                results[c.id] = {"id": c.id, "title": c.title, "updated_at": c.updated_at,
                                 "match": "content", "snippet": snippet}
    out = sorted(results.values(), key=lambda x: x["updated_at"], reverse=True)
    return JSONResponse(out[:50])


@router.post("/api/chat/stream")
async def chat_stream(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    body = await request.json()
    conv_id = body.get("conversation_id")
    content = (body.get("content") or "").strip()
    if not conv_id:
        raise HTTPException(400, "conversation_id required")

    # Per-message protocol attachment (single use): prepended to THIS message only,
    # so it steers one turn and then vanishes from history's influence naturally.
    # Server-side lookup — the client sends an id we own, never the content itself.
    proto_doc_id = (body.get("protocol_doc_id") or "").strip()
    proto_text = ""
    proto_name = ""
    if proto_doc_id:
        with session_scope() as s:
            d = s.get(Doc, proto_doc_id)
            if not d or d.kind != "protocol" or not d.visible_to_users:
                raise HTTPException(404, "unknown protocol")
            proto_text = (d.content or "").strip()
            proto_name = d.name
        if proto_text:
            content = (f"<protocol name=\"{proto_name}\">\n{proto_text}\n</protocol>\n\n"
                       + content) if content else f"<protocol name=\"{proto_name}\">\n{proto_text}\n</protocol>"

    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")

    # Only one turn per conversation at a time: two live streams on the same
    # history would interleave tool_call/tool rows and confuse the model. But refusing
    # outright made a mid-turn thought unutterable for the whole duration of an agent turn,
    # so a send arriving during a running turn is QUEUED as steering instead of rejected —
    # the orchestrator delivers it at the next between-rounds seam (see hearth/steering.py).
    # The reply is plain JSON rather than an SSE stream because nothing streams back: the
    # caller's existing turn keeps running and its own stream carries everything.
    from . import cancel as cancel_mod
    from . import steering as _steering
    if cancel_mod.is_running(conv_id, user.id):
        q_att = _resolve_steer_attachments(user, body.get("attachments") or [])
        depth = _steering.queue(conv_id, content, q_att)
        if depth < 0:
            raise HTTPException(400, "nothing to queue (empty message, or too many queued)")
        return JSONResponse({"ok": True, "queued": True, "queue_depth": depth,
                             "note": "a turn is running — your message will be delivered "
                                     "between tool steps"})

    settings = rs.get_all()
    provider = await get_manager().get_provider()
    store = _download_store(request)
    _prune_downloads(store)

    # Attachments were already uploaded (staged client-side); resolve them to owned
    # artifacts so we never trust a client path. Text files are summarized inline;
    # images become multimodal image_url parts (data URLs) for vision-capable models.
    attachments = body.get("attachments") or []
    att_meta = []
    for a in attachments:
        art = artifacts.get(user.id, a.get("artifact_id")) if a.get("artifact_id") else None
        if not art:
            continue
        att_meta.append({"artifact_id": art["id"], "name": art["filename"],
                         "mime": art["mime"], "rel_path": art["rel_path"]})

    if content or att_meta:
        text_for_msg = content or ""
        if att_meta:
            names = ", ".join(x["name"] for x in att_meta)
            prefix = f"[attached: {names}]"
            text_for_msg = (prefix + ("\n" + text_for_msg if text_for_msg else ""))
        orch.append_message(conv_id, "user", text_for_msg,
                            meta=json.dumps({"attachments": att_meta,
                                             **({"protocol": proto_name} if proto_doc_id else {})})
                            )
        # auto-title from first user message (skip the protocol block when there's
        # no real text yet — a bare protocol send shouldn't title the chat "<prot…").
        with session_scope() as s:
            c = s.get(Conversation, conv_id)
            if c and c.title == "New chat":
                base = content.split("</protocol>")[-1].strip() if proto_doc_id else content
                c.title = (base or att_meta[0]["name"])[:48] if (base or att_meta) else c.title

    settings = _with_device_grant(settings, conv_id)
    gen = _turn_options(body, settings)

    async def gen_sse():
        last_usage = None
        # Audio first, inside the stream: captioning a recording takes real time (ffmpeg plus
        # a model that has to listen), and doing it before the response starts would leave the
        # user staring at silence. Here the "listening…" notice reaches them as it happens.
        # Cancellation is checked around each file so Abort works during this phase too —
        # without it, Stop would appear to do nothing for the length of a recording.
        captions: dict = {}
        if att_meta and bool(settings.get("captioner.enabled")):
            import asyncio as _aio
            from . import cancel as _cancel_mod
            from . import captioner as _cap_mod
            handle = _cancel_mod.begin(conv_id, user.id)
            # prepare_captions runs in a worker thread (ffmpeg + model), so it cannot yield
            # into this generator itself. A queue is the seam: notices are drained here while
            # the task runs, and the abort handle is checked on every drain tick.
            _q: _aio.Queue = _aio.Queue()
            try:
                def _emit(ev, _qq=_q):
                    try:
                        _qq.put_nowait(ev)
                    except Exception:  # noqa: BLE001 — progress must never break captioning
                        pass

                task = _aio.create_task(
                    orch.prepare_captions(user.id, conv_id, att_meta, settings, emit=_emit,
                                           cancel=handle))
                while True:
                    if handle.cancelled:
                        break
                    try:
                        ev = await _aio.wait_for(_q.get(), timeout=0.25)
                        yield _sse(ev)
                    except _aio.TimeoutError:
                        pass
                    if task.done():
                        break
                # Flush any notice queued just before completion.
                while not _q.empty():
                    yield _sse(_q.get_nowait())
                if handle.cancelled and not task.done():
                    task.cancel()
                    # Stop the audio model too, not just the chat. Cancelling the asyncio task
                    # does NOT unblock the worker thread sitting in a blocking httpx read, so
                    # without this llama.cpp keeps decoding the caption to completion (the
                    # "captioner still running after Stop" complaint). stop_active terminates
                    # the Hearth-owned child process; the thread then errors out and unwinds.
                    try:
                        _cap_mod.get_captioner().stop_active()
                    except Exception:  # noqa: BLE001 — best-effort teardown of the audio model
                        pass
                    yield _sse({"type": "aborted",
                                "text": "Aborted while describing audio."})
                    return
                try:
                    captions = await task
                except Exception as ce:  # noqa: BLE001 — never lose the user's message
                    audit.log(user.id, "caption_prep_error", {"err": str(ce)[:200]})
                    captions = {}
                # Persist so a refresh still shows the description and later turns keep it in
                # context (DEC-061). Done here rather than inside prepare_captions because
                # this is where the message row that owns these attachments definitely exists.
                if captions:
                    orch.persist_captions(conv_id, att_meta, captions)
            finally:
                handle.cancel()
                _cancel_mod.end(conv_id, handle)
        try:
            async for ev in _stream_turn(user.id, conv_id, provider, settings, store, gen,
                                         captions=captions):
                if ev.get("type") == "usage":
                    last_usage = ev.get("data")
                yield _sse(ev)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            audit.log(user.id, "chat_stream_error", {"err": str(e)[:200]})
            yield _sse({'type': 'error', 'text': f'Stream error: {type(e).__name__}: {e}'[:300]})
        finally:
            # Runs on every exit path (including client disconnect) so the context
            # bar reflects the tokens the model actually saw, even after an abort.
            # save_turn_usage itself skips a conversation that was deleted mid-turn.
            save_turn_usage(conv_id, last_usage)

    return StreamingResponse(gen_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def save_turn_usage(conv_id: str, usage: dict | None) -> None:
    """Attach the turn's `usage` to the newest assistant row (for the context bar).

    The context bar needs prompt_tokens from a PREVIOUS turn (the current one is
    still being built), so we persist it on the message row instead of keeping it
    in memory. Only text-bearing assistant rows get it — a pure tool-call row has
    no usage to report, and the next turn's prompt size is what matters anyway.

    Safe after an abort (the caller runs it in `finally`); if the conversation was
    deleted mid-turn there is nothing to attach to, so we skip quietly instead of
    touching rows that belong to a gone chat.
    """
    if not usage:
        return
    slim = {k: usage.get(k) for k in
            ("prompt_tokens", "completion_tokens", "tokens_per_sec", "duration_s")}
    # Only persist a real prompt size. `context_usage_payload()` reports any stored
    # prompt_tokens with source:"usage" (measured), so saving a chars/4 figure here
    # would permanently mislabel an estimate as measured — and it would outlive the
    # turn, hiding the fact that the server never reported usage at all. Providers mark
    # their estimates `estimated: True` / omit `measured`; treat anything not asserted
    # measured as un-storable for prompt size. We still save completion/timing, and the
    # endpoint falls back to its own clearly-flagged estimate.
    if not usage.get("measured"):
        slim["prompt_tokens"] = None
    if not any(v for v in slim.values()):
        return
    if not _conversation_exists(conv_id):
        return
    try:
        with session_scope() as s:
            row = (s.query(Message)
                   .filter(Message.conversation_id == conv_id, Message.role == "assistant")
                   .order_by(Message.created_at.desc()).first())
            if row is None:
                return
            meta = {}
            if row.meta:
                try:
                    meta = json.loads(row.meta)
                except Exception:  # noqa: BLE001 — non-JSON meta isn't ours to preserve
                    meta = {}
            meta["usage"] = slim
            row.meta = json.dumps(meta)
    except Exception as e:  # noqa: BLE001 — bookkeeping must never break a stream
        audit.log("", "usage_save_failed", {"conv": conv_id, "err": str(e)[:120]})


def _conversation_exists(conv_id: str) -> bool:
    """Does this conversation row still exist? Used to skip dead-row bookkeeping."""
    from .models import Conversation
    try:
        with session_scope() as s:
            return s.get(Conversation, conv_id) is not None
    except Exception:  # noqa: BLE001 — treat an unreadable row as "gone"
        return False


def _turn_options(body: dict, settings: dict) -> dict:
    """Turn-level generation options from the client.

    `no_tools` remains supported server-side (a client may explicitly ask for a
    tools-free turn), but the chat UI no longer sends it: agent behaviour is always
    on. Honouring it costs nothing security-wise — gated tools stay gated, and
    disabling tools can only reduce what the model is able to do. max_tokens is
    clamped to the admin cap.
    """
    gen: dict = {}
    if bool(body.get("no_tools")):
        gen["no_tools"] = True
    try:
        mt = body.get("max_tokens")
        if mt:
            gen["max_tokens"] = max(16, min(int(mt), int(settings.get("model.max_tokens", 2048))))
    except (TypeError, ValueError):
        pass
    return gen


def _with_device_grant(settings: dict, conv_id: str) -> dict:
    """Return a settings copy carrying this conversation's device-access grant.

    Tools read it via ToolContext.device_granted(). The value comes from OUR DB,
    never from the request body, so a client cannot self-grant device access.
    """
    out = dict(settings)
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        out["_device.granted"] = bool(c and c.device_granted)
    return out


async def _stream_turn(user_id: str, conv_id: str, provider, settings: dict,
                       store: dict, gen: dict | None = None, pending_resume=None,
                       captions: dict | None = None):
    """Run one turn as SSE-ready dicts, wired into the abort registry.

    The generator owns the handle's lifetime: it is registered before the first
    event and removed on any exit path (normal end, abort, exception, or the
    client disconnecting mid-stream), so a conversation can never be left in a
    stale "running" state that blocks Abort or reports a phantom turn.

    `captions` carries audio descriptions prepared before the turn (see
    orch.prepare_captions) so attachments arrive described rather than mute.

    First event may be a `provider_fallback` notice: when Admin set a real backend but it
    is not answering, the turn runs on the stub model. That used to be invisible — canned
    replies simply looked like answers (AUDIT #7).
    """
    from . import cancel as cancel_mod
    note = getattr(get_manager(), "last_fallback_note", "") or ""
    handle = cancel_mod.begin(conv_id, user_id)
    try:
        if note:
            yield {"type": "notice", "kind": "provider_fallback", "text": note}
        async for ev in orch.run_turn(user_id, conv_id, provider, settings, store,
                                      pending_resume=pending_resume, gen=gen, cancel=handle,
                                      captions=captions):
            yield ev
    except asyncio.CancelledError:
        # Client went away (or server shutdown): tell the loop we're done so any
        # in-flight tool call is interrupted rather than running to completion.
        handle.cancel()
        raise
    finally:
        handle.cancel()
        cancel_mod.end(conv_id, handle)


@router.get("/api/chat/steer")
async def steer_pending(request: Request, conversation_id: str = "",
                        user=Depends(require_user)):
    """Queued-but-undelivered steering messages for a conversation.

    The composer shows these so the user can see what is waiting and take it back. Re-read on
    reload because a queue lives in the server process, not the page."""
    from . import steering as _steering
    if not conversation_id:
        return JSONResponse({"ok": True, "queued": []})
    with session_scope() as s:
        c = s.get(Conversation, conversation_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
    return JSONResponse({"ok": True, "queued": _steering.preview(conversation_id)})


@router.post("/api/chat/steer/withdraw")
async def steer_withdraw(request: Request, user=Depends(require_user),
                         _=Depends(check_csrf)):
    """Take back one queued steering message before the model reads it.

    Returns removed=False for a stale index — normal (it already went through), so the client
    refreshes its row rather than reporting an error."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    conv_id = body.get("conversation_id") or ""
    from . import steering as _steering
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
    try:
        idx = int(body.get("index"))
    except (TypeError, ValueError):
        raise HTTPException(400, "index required")
    removed = _steering.withdraw(conv_id, idx)
    return JSONResponse({"ok": True, "removed": removed,
                         "queued": _steering.preview(conv_id)})


def _resolve_steer_attachments(user, attachments: list) -> list[dict]:
    """Resolve queued attachment refs to owned artifacts, exactly as a normal send does.

    Done at ENQUEUE time (not delivery) so the queue holds only ids we have already verified
    belong to this user, and so a message cannot reference an artifact deleted in between —
    build_messages re-resolves by id anyway, so a stale entry degrades to "no file" rather
    than reaching the model as a path."""
    out: list[dict] = []
    for a in (attachments or []):
        art = artifacts.get(user.id, a.get("artifact_id")) if a.get("artifact_id") else None
        if not art:
            continue
        out.append({"artifact_id": art["id"], "name": art["filename"],
                    "mime": art["mime"], "rel_path": art["rel_path"]})
    return out


@router.post("/api/chat/abort")
async def chat_abort(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    """Abort the in-flight turn for a conversation (the Stop/Abort button).

    The orchestrator notices between events and ends the stream with an `aborted`
    event. Returns aborted=False when nothing was running, which is not an error —
    the client just re-enables Send.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    conv_id = body.get("conversation_id") or ""
    from . import cancel as cancel_mod
    aborted = cancel_mod.abort_conversation(conv_id, user.id)
    if aborted:
        audit.log(user.id, "chat_abort", {"conv": conv_id})
    return JSONResponse({"ok": True, "aborted": aborted})


@router.get("/api/chat/running")
async def chat_running(request: Request, conversation_id: str = "",
                       user=Depends(require_user)):
    """Is a turn currently streaming for this conversation? Used by the UI to keep
    the Stop button in sync after a reload or a dropped connection."""
    from . import cancel as cancel_mod
    return JSONResponse({"running": bool(conversation_id)
                         and cancel_mod.is_running(conversation_id, user.id)})


@router.post("/api/chat/retry")
async def chat_retry(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    """Re-run the last user turn: drop everything after it and stream a fresh answer.

    Useful when a reply was cut off, an error/timeout interrupted it, or the model
    went quiet mid-tool. Only assistant/tool rows are deleted — the user message
    (and its attachments) stay untouched. Refuses while a turn is still running so
    we never regenerate underneath a live stream.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    conv_id = body.get("conversation_id") or ""
    if not conv_id:
        raise HTTPException(400, "conversation_id required")
    from . import cancel as cancel_mod
    if cancel_mod.is_running(conv_id, user.id):
        raise HTTPException(409, "a turn is still running — abort it first")

    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        rows = s.query(Message).filter(Message.conversation_id == conv_id) \
            .order_by(Message.created_at.asc()).all()
        last_user = None
        for m in rows:
            if m.role == "user":
                last_user = m
        if last_user is None:
            raise HTTPException(400, "nothing to retry — no user message yet")
        # Rows created at/after the last user message, excluding that message and any
        # later real user message (retrying an older turn must not eat newer ones).
        stale_ids = [m.id for m in rows
                     if m.id != last_user.id and m.role != "user"
                     and m.created_at >= last_user.created_at]
        if stale_ids:
            s.query(Message).filter(Message.id.in_(stale_ids)).delete(synchronize_session=False)
    audit.log(user.id, "chat_retry", {"conv": conv_id, "dropped": len(stale_ids)})

    settings = _with_device_grant(rs.get_all(), conv_id)
    provider = await get_manager().get_provider()
    store = _download_store(request)
    gen = _turn_options(body, settings)

    async def gen_sse():
        last_usage = None
        try:
            async for ev in _stream_turn(user.id, conv_id, provider, settings, store, gen):
                if ev.get("type") == "usage":
                    last_usage = ev.get("data")
                yield _sse(ev)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            audit.log(user.id, "chat_retry_error", {"err": str(e)[:200]})
            yield _sse({'type': 'error', 'text': f'Retry error: {type(e).__name__}: {e}'[:300]})
        finally:
            save_turn_usage(conv_id, last_usage)

    return StreamingResponse(gen_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/chat/context-usage")
async def chat_context_usage(request: Request, conversation_id: str = "",
                             user=Depends(require_user)):
    """Context-window usage for the context bar in the chat header."""
    settings = rs.get_all()
    n_ctx = int(settings.get("model.n_ctx") or 0)
    if not conversation_id:
        return JSONResponse(context_usage_payload(0, None, n_ctx))

    with session_scope() as s:
        c = s.get(Conversation, conversation_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        rows = s.query(Message).filter(Message.conversation_id == conversation_id) \
            .order_by(Message.created_at.asc()).all()
        usage = None
        for m in rows:
            if m.role != "assistant" or not m.meta:
                continue
            try:
                mm = json.loads(m.meta)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(mm.get("usage"), dict):
                usage = mm["usage"]
        total_chars = sum(len(m.content or "") for m in rows)

    # The live server's own window beats the configured setting, but asking for it
    # must not START anything. This endpoint runs on every conversation open (see
    # loadContextUsage in chat.js), so going through get_provider() meant a passive
    # page view called ensure_server() and launched llama-server — resurrecting a
    # model the admin had just stopped. probe() is the read-only half of that pair:
    # it reads /health + /props only, never consults admin settings and never spawns
    # a process (DEC-048). When nothing answers we keep the configured n_ctx above.
    provider_n_ctx = None
    try:
        from .manager import get_manager
        h = await get_manager().probe()
        if h.get("server_reachable"):
            provider_n_ctx = h.get("n_ctx") or None
    except Exception:  # noqa: BLE001 — context bar must never break the page
        pass
    if provider_n_ctx:
        n_ctx = int(provider_n_ctx)

    return JSONResponse(context_usage_payload(total_chars, usage, n_ctx))


def context_usage_payload(total_chars: int, usage: dict | None, n_ctx: int) -> dict:
    """Pure maths for the context bar (unit-testable without FastAPI/DB).

    `usage` is the last real usage report for the conversation (what the model
    actually received); when there is none we estimate from stored characters at
    ~4 chars/token and say so via `source`, so the bar never silently lies.
    """
    out = {"prompt_tokens": None, "completion_tokens": None, "n_ctx": n_ctx,
           "tokens_per_sec": None, "source": "none", "total_chars": total_chars}
    if usage:
        # A stored prompt size counts as measured UNLESS the writer flagged it otherwise
        # (`estimated` was added for exactly this — see _estimate_usage). Older rows carry
        # neither flag, and their prompt_tokens did come from the provider, so defaulting
        # to measured keeps them working. Without this check a heuristic could masquerade
        # as a server-reported number forever, which is what `source` exists to prevent.
        est_flagged = bool(usage.get("estimated")) or usage.get("measured") is False
        pt = None if est_flagged else usage.get("prompt_tokens")
        out.update({
            "prompt_tokens": pt,
            "completion_tokens": usage.get("completion_tokens"),
            "tokens_per_sec": usage.get("tokens_per_sec"),
        })
        # `source` describes the PROMPT measurement, which is what the bar divides by.
        # A row can carry completion/timing with no prompt size (an estimated turn), and
        # claiming "usage" while showing 0 tokens would be the same lie in another form.
        out["source"] = "usage" if pt else ("estimate" if total_chars else "none")
    if out["source"] != "usage" and total_chars:
        # No usable prompt measurement (no usage row at all, or one with only timing):
        # fall back to ~4 chars/token so the bar still moves, clearly flagged estimate.
        out["prompt_tokens"] = total_chars // 4 + 16
        out["source"] = "estimate"

    used = int(out["prompt_tokens"] or 0)
    out["pct"] = round(min(1.0, used / n_ctx), 4) if n_ctx else None
    # Past ~80% the user should know a truncation/summarise is coming.
    out["warning"] = bool(out["pct"] is not None and out["pct"] >= 0.8)
    return out


@router.post("/api/chat/device-access")
async def chat_device_access(request: Request, user=Depends(require_user),
                             _=Depends(check_csrf)):
    """Grant or revoke device access for ONE conversation.

    Kept as the API surface for the device-tool grant (see tools/device_tools._gate);
    the chat UI no longer exposes a switch — grants persist per conversation. It
    deliberately does NOT affect uploads or downloads, which are user-initiated file
    transfers rather than device access.
    """
    body = await request.json()
    conv_id = body.get("conversation_id") or ""
    granted = bool(body.get("granted"))
    if not conv_id:
        raise HTTPException(400, "conversation_id required")
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        c.device_granted = granted
    audit.log(user.id, "device_access_" + ("grant" if granted else "revoke"), {"conv": conv_id})
    return JSONResponse({"ok": True, "granted": granted,
                         "helper_configured": bool(_helper_url())})


@router.get("/api/chat/device-access")
async def chat_device_access_get(request: Request, conversation_id: str = "",
                                 user=Depends(require_user)):
    """Current device-access state for a conversation + whether a helper is paired."""
    if not conversation_id:
        raise HTTPException(400, "conversation_id required")
    with session_scope() as s:
        c = s.get(Conversation, conversation_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "not found")
        granted = bool(c.device_granted)
    return JSONResponse({"granted": granted, "helper_configured": bool(_helper_url()),
                         "required": bool(rs.get("agent.require_device_grant"))})


@router.post("/api/chat/resolve")
async def chat_resolve(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    body = await request.json()
    pid = body.get("pending_id")
    approved = bool(body.get("approved"))
    answer = body.get("answer")
    with session_scope() as s:
        pa = s.get(PendingAction, pid)
        if not pa or pa.user_id != user.id or pa.status != "pending":
            raise HTTPException(404, "no such pending action")
        payload = json.loads(pa.payload or "{}")
        if pa.kind == "ask_user":
            payload["answer"] = answer or ""
            pa.status = "answered"
        else:
            pa.status = "approved" if approved else "denied"
        payload_json = json.dumps(payload)
        resume = {"id": pa.id, "kind": pa.kind, "tool_name": pa.tool_name,
                  "call_id": pa.call_id or "",
                  "conversation_id": pa.conversation_id, "user_id": pa.user_id,
                  "payload": payload_json, "approved": approved,
                  # Carried so _resume_pending can tell a fresh approval from one left
                  # sitting for hours against a workspace that has since moved on (#4).
                  "created_at": pa.created_at}
        audit.log(user.id, f"resolve_{pa.status}", {"tool": pa.tool_name})

    settings = _with_device_grant(rs.get_all(), resume["conversation_id"])
    provider = await get_manager().get_provider()
    store = _download_store(request)
    turn_opts = _turn_options(body, settings)

    # NB: this generator must NOT be named `gen` — it would rebind the name in this
    # scope, so the call below would pass the generator FUNCTION instead of the
    # options dict, and run_turn's `dict(gen or {})` raised
    # "Resolve error: TypeError: 'function' object is not iterable" on every
    # ask_user / gated-tool resolution. (chat_stream/chat_retry name theirs
    # `gen_sse` for exactly this reason.)
    async def gen_sse():
        last_usage = None
        try:
            async for ev in _stream_turn(user.id, resume["conversation_id"], provider,
                                         settings, store, turn_opts, pending_resume=resume):
                if ev.get("type") == "usage":
                    last_usage = ev.get("data")
                yield _sse(ev)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            audit.log(user.id, "chat_resolve_error", {"err": str(e)[:200]})
            yield _sse({'type': 'error', 'text': f'Resolve error: {type(e).__name__}: {e}'[:300]})
        finally:
            save_turn_usage(resume["conversation_id"], last_usage)

    return StreamingResponse(gen_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/download/{token}")
async def download(token: str, request: Request, user=Depends(require_user)):
    store = _download_store(request)
    entry = store.get(token)
    if not entry or entry.get("exp", 0) < time.time():
        raise HTTPException(404, "expired or unknown download")
    path = entry["path"]
    if not os.path.isfile(path):
        raise HTTPException(404, "file gone")
    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    from . import crypto
    with open(path, "rb") as _fh:
        data = _fh.read()
    if crypto.encryption_enabled() or crypto.is_encrypted_blob(data):
        data = crypto.decrypt_bytes(data)   # serve plaintext bytes to the owner only
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition":
                             f'attachment; filename="{os.path.basename(path)}"'})


# ---- User uploads (device -> server) ---------------------------------------
@router.post("/api/upload")
async def upload_files(request: Request, conversation_id: str = Form(""),
                       prefix: str = Form("uploads"),
                       files: list[UploadFile] = File(...),
                       user=Depends(require_user), _=Depends(check_csrf)):
    """Accept one or more uploaded files into the caller's workspace jail.

    Files are streamed to disk in bounded chunks (never fully buffered), size-capped,
    and stored under <prefix>/<safe-name>. Each becomes an Artifact row so it appears in
    the session Artifacts panel. Returns the created artifact list.

    `prefix` picks the destination folder and is NOT free-form: only ``uploads`` (default)
    or the caller's own linked-project prefix is accepted, and the client-supplied name is
    still reduced to a bare basename by _safe_filename — so there is no traversal surface.
    Uploading into the project copy stages files for sync_apply; nothing reaches the real
    folder until an approved apply (DEC-049).

    Re-uploading a name that already exists REPLACES it, and always did. What used to be
    silent is now reported: each artifact carries `replaced` (a path existed) and
    `replaced_different_content` (those bytes differed), so neither the client nor the
    model is ever told "uploaded" about what was actually a destructive overwrite. See
    finding #2 — DEC-040 §2 justified replacing by "each message's attachment stays
    fetchable", which only holds while the bytes are identical.
    """
    if not files:
        raise HTTPException(400, "no files")
    target_prefix = (prefix or "uploads").strip().strip("/")
    if target_prefix != "uploads":
        link = pl.get_link(user.id)
        if not link or target_prefix != link["rel_prefix"]:
            raise HTTPException(400, f"cannot upload to '{target_prefix}' — no such "
                                     f"destination is linked to your workspace")
    created = []
    limit = upload_limit_bytes()
    for f in files:
        safe_name = _safe_filename(f.filename or "upload.bin")
        rel_target = os.path.join(target_prefix, safe_name)
        dest = ws.resolve_in_jail(user.id, rel_target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Before anything is written: does this name already hold something, and will the
        # bytes we are about to stream change it? Hashing the EXISTING file (never the
        # incoming one, which is streamed) keeps this cheap and lets the reply say whether
        # a replacement destroyed different content rather than rewriting identical bytes.
        had_path = dest.exists()
        prior_sha = _sha_of_existing(user.id, rel_target) if had_path else None
        written = 0
        too_big = False
        try:
            # Stream to a temp file (never fully buffered), then seal it through
            # workspaces' write path so encryption-at-rest applies transparently.
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with open(tmp, "wb") as out:
                    while True:
                        chunk = await f.read(1 << 20)   # 1 MiB chunks
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            too_big = True
                            break
                        out.write(chunk)
                new_sha = None
                if not too_big:
                    with open(tmp, "rb") as rd:
                        blob = rd.read()
                    new_sha = hashlib.sha256(blob).hexdigest()
                    ws.seal_bytes(dest, blob)   # encrypts when enabled
            finally:
                try: os.remove(tmp)
                except OSError: pass
        finally:
            await f.close()
        if too_big:
            try: os.remove(dest)
            except OSError: pass
            mb = limit // (1024 * 1024)
            raise HTTPException(413, f"'{safe_name}' is too large (over {mb} MB limit)")
        replaced = had_path and (prior_sha is None or prior_sha != new_sha)
        art = artifacts.record(user.id, ws.rel_from_jail(user.id, dest),
                               conversation_id=conversation_id, kind="uploaded",
                               filename=safe_name, plain_size=written, new_row=True)
        art["replaced"] = had_path
        art["replaced_different_content"] = replaced
        if replaced:
            # The one audit line that distinguishes "attached report.pdf again" from
            # "overwrote report.pdf with different bytes" — invisible in the chat before.
            audit.log(user.id, "upload_replaced", {"path": art["rel_path"],
                                                   "prior_sha256": (prior_sha or "")[:16],
                                                   "new_sha256": (new_sha or "")[:16]})
        created.append(art)
    audit.log(user.id, "upload", {"count": len(created)})
    return JSONResponse({"ok": True, "artifacts": created,
                         "max_bytes": limit,
                         # Surfaced so the client never has to hard-code a second copy of
                         # the limit (finding #8): chat.js reads this instead.
                         "replaced_any": any(a.get("replaced_different_content") for a in created)})


def _sha_of_existing(user_id: str, rel_target: str) -> str | None:
    """sha256 of an existing jailed file's PLAINTEXT, or None if it cannot be read.

    Returns None rather than raising so an unreadable pre-existing file degrades to "we
    could not compare", never to a false claim that content was preserved.
    """
    try:
        return hashlib.sha256(ws.read_bytes(user_id, rel_target)).hexdigest()
    except Exception:  # noqa: BLE001 — comparison is best-effort; the upload itself stands
        return None


def _safe_filename(name: str) -> str:
    """Reduce a client-supplied filename to a safe basename (no traversal)."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*\x00')
    name = name.strip().strip(".") or "upload.bin"
    return name[:120]


# ---- Artifacts list / download / delete ------------------------------------
@router.get("/api/artifacts")
async def list_artifacts(request: Request, conversation_id: str | None = None,
                         user=Depends(require_user)):
    conv = conversation_id if conversation_id else None
    return JSONResponse(artifacts.list_for(user.id, conversation_id=conv))


@router.get("/api/artifact/{artifact_id}")
async def download_artifact(artifact_id: str, request: Request, user=Depends(require_user)):
    art = artifacts.get(user.id, artifact_id)   # ownership enforced here
    if not art:
        raise HTTPException(404, "not found")
    try:
        data = ws.read_bytes(user.id, art["rel_path"])   # decrypts when encryption is on
    except (ws.JailEscape, FileNotFoundError):
        raise HTTPException(404, "file gone")
    return Response(content=data, media_type=art["mime"],
                    headers={"Content-Disposition": f'attachment; filename="{art["filename"]}"'})


@router.post("/api/artifact/{artifact_id}/delete")
async def delete_artifact(artifact_id: str, request: Request,
                          user=Depends(require_user), _=Depends(check_csrf)):
    """Forget an artifact. Body: {purge?: bool}.

    The row always goes. With purge=true the workspace file is deleted too — otherwise
    the bytes stay in the jail with nothing left to list or download them (the original
    row-only behaviour, kept as the default for callers that just want the entry gone).
    Ownership is enforced by artifacts.delete(); the path is resolved through the jail
    before anything is removed.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no/invalid body means row-only, as before
        pass
    art = artifacts.get(user.id, artifact_id)
    if not art:
        return JSONResponse({"ok": False})
    removed_file = False
    if bool(body.get("purge")):
        try:
            p = ws.resolve_in_jail(user.id, art["rel_path"])
            if os.path.isfile(p):
                os.remove(p); removed_file = True
        except (ws.JailEscape, FileNotFoundError):
            pass   # already gone; the row still gets forgotten below
    ok = artifacts.delete(user.id, artifact_id)
    audit.log(user.id, "artifact_delete", {"id": artifact_id, "path": art["rel_path"],
                                           "purged": removed_file})
    return JSONResponse({"ok": ok, "file_deleted": removed_file})


# ---- Language preference ----------------------------------------------------
@router.post("/api/language")
async def set_language(request: Request, language: str = Form(...),
                       user=Depends(require_user), _=Depends(check_csrf)):
    from . import i18n
    lang = i18n.normalize(language)
    with session_scope() as s:
        u = s.get(User, user.id)
        if u:
            u.language = lang
    return JSONResponse({"ok": True, "language": lang})


# ---- Per-user behavioral preferences ----------------------------------------
# Stored as a small JSON blob on the user row (like `language`): syncs across
# devices, readable by any future server-side consumer. Only the keys below are
# accepted; unknown keys are dropped rather than stored, so the blob can't be
# filled with junk from a tampered client. Values must be booleans — anything
# else is rejected per key (400) rather than silently coerced.
# (agent_mode_default was removed: agent mode is always on.)
_PREF_KEYS = {"enter_sends", "auto_scroll", "show_timestamps"}


@router.get("/api/prefs")
async def get_prefs(request: Request, user=Depends(require_user)):
    with session_scope() as s:
        u = s.get(User, user.id)
        raw = (u.prefs if u else "") or "{}"
    try:
        val = json.loads(raw)
    except Exception:  # noqa: BLE001 — corrupt blob falls back to defaults
        val = {}
    return JSONResponse({k: v for k, v in val.items() if k in _PREF_KEYS})


@router.post("/api/prefs")
async def set_prefs(request: Request, user=Depends(require_user), _=Depends(check_csrf)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "object required")
    clean = {}
    for k, v in body.items():
        if k in _PREF_KEYS:
            if not isinstance(v, bool):
                raise HTTPException(400, f"{k} must be a boolean")
            clean[k] = v
    with session_scope() as s:
        u = s.get(User, user.id)
        if not u:
            raise HTTPException(404, "not found")
        try:
            merged = json.loads(u.prefs or "{}")
            if not isinstance(merged, dict):
                merged = {}
        except Exception:  # noqa: BLE001
            merged = {}
        merged.update(clean)
        u.prefs = json.dumps(merged)
    return JSONResponse({"ok": True, "prefs": {k: v for k, v in merged.items() if k in _PREF_KEYS}})


# ---- Linked project folder (DEC-049, user-facing) ---------------------------
# These four operate ONLY on the requesting user's own link (project_link.get_link filters
# by user_id), so a user without a link simply gets "not linked" and a user can never reach
# another user's target folder. Link/unlink stay admin-only because they name an absolute
# path on the server filesystem; nothing here takes a path argument at all.
def _project_summary(user_id: str) -> dict:
    """Cheap-enough view for the chat chip: counts + conflicts, no file contents.

    NOTE ON COST: diff_project() hashes every file in the copy and re-hashes each baseline
    target, so this is O(project size). It is therefore called on demand only (turn end,
    manual refresh, before an apply) — never from a poll timer or from prompt building.
    """
    link = pl.get_link(user_id)
    if not link:
        return {"linked": False}
    out = {"linked": True, "prefix": link["rel_prefix"],
           "imported_at": int(link["imported_at"] or 0),
           "backend": pa.detect_backend("auto"),
           "encryption_blocks_apply": crypto.encryption_enabled()}
    try:
        out["exists"] = pl.target_root(link).is_dir()
    except pl.LinkError:
        out["exists"] = False
    mf = pl.load_manifest(user_id, link)
    out["files"] = len(mf.get("files") or {})
    try:
        cs = pl.diff_project(user_id, link)
        out.update({"added": len(cs.added), "modified": len(cs.modified),
                    "conflicts": [str(c) for c in cs.conflicts],
                    "deleted": len(cs.deleted), "unchanged": cs.unchanged})
        # Only paths that an apply would actually write — shown in the confirm card.
        out["will_write"] = cs.applyable[:200]
    except pl.LinkError as e:
        out["diff_error"] = str(e)
    return out


@router.get("/api/project/status")
async def project_status(user=Depends(require_user)):
    """Whether this user has a linked folder, and what the copy prefix is."""
    link = pl.get_link(user.id)
    if not link:
        return JSONResponse({"linked": False})
    return JSONResponse({"linked": True, "prefix": link["rel_prefix"],
                         "imported_at": int(link["imported_at"] or 0),
                         "backend": pa.detect_backend("auto"),
                         "encryption_blocks_apply": crypto.encryption_enabled()})


@router.get("/api/project/summary")
async def project_summary(user=Depends(require_user)):
    """Pending change counts + conflicts. On demand only (see _project_summary cost note)."""
    if not pl.get_link(user.id):
        return JSONResponse({"linked": False})
    from . import runtime_settings as rs
    try:
        summary = _project_summary(user.id)
    except pl.LinkError as e:
        raise HTTPException(400, str(e))
    summary["apply_enabled"] = (summary.get("backend") != "stub"
                                and not summary.get("encryption_blocks_apply"))
    summary["settings_ok"] = bool(rs.get_all().get("tools.sync_apply", True))
    return JSONResponse(summary)


@router.post("/api/project/apply")
async def project_apply_now(user=Depends(require_user), _=Depends(check_csrf)):
    """User-initiated write-back. The click IS the approval — no model involvement.

    apply_changes() re-computes the diff and re-checks every target against the baseline
    before writing, so a conflict discovered after the user pressed the button still blocks
    the whole apply rather than half-applying.
    """
    link = pl.get_link(user.id)
    if not link:
        raise HTTPException(400, "No project folder is linked to your workspace.")
    from . import runtime_settings as rs
    settings = rs.get_all()
    if not settings.get("tools.sync_apply", True):
        audit.log(user.id, "sync_apply_refused_admin", {"why": "tool disabled"})
        raise HTTPException(403, "Project sync-apply is disabled by an admin "
                                 "(Admin → Tools → Linked project folder).")
    res = pa.apply_changes(user.id, settings, link)
    audit.log(user.id, "sync_apply_ui", {"target": link["target_abs"], "ok": res.ok,
                                         "written": res.written[:40], "refused": res.refused[:20],
                                         "backend": res.backend})
    return JSONResponse({"ok": res.ok, "note": res.note, "written": res.written,
                         "refused": res.refused[:50], "backup_id": res.backup_id,
                         "backend": res.backend}, status_code=200)


@router.post("/api/project/revert")
async def project_revert_by_user(request: Request, user=Depends(require_user),
                                 _=Depends(check_csrf)):
    """Restore the files one apply backed up."""
    body = await request.json()
    backup_id = str(body.get("backup_id") or "")
    try:
        res = pa.revert(user.id, backup_id)
    except pl.LinkError as e:
        raise HTTPException(400, str(e))
    audit.log(user.id, "project_revert_ui", {"backup_id": backup_id,
                                             "restored": res.written[:40],
                                             "refused": res.refused[:20]})
    if not res.ok:
        raise HTTPException(400, res.note)
    return JSONResponse({"ok": True, "note": res.note, "restored": res.written})
