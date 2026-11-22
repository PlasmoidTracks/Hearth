"""Orchestrator — the per-turn agent loop (DEC-010).

run_turn() is an async generator yielding dict events for SSE:
  {type: token|tool_call|tool_result|progress|usage|action_required|error|done}

Flow for one turn:
  1. Load conversation history (+ system prompt) from DB.
  2. Ask the provider to stream. Tokens are forwarded live and accumulated.
  3. On tool_request(s):
       - auto tools (fs read/list/write/upload, web_search, device): execute now,
         append assistant(tool_calls)+tool results to history, then loop again so
         the model can use the results (bounded by agent.max_tool_steps).
       - gated tools (exec) and ask_user: persist a PendingAction, emit
         action_required{pending_id}, and STOP this stream. A later /resolve call
         runs the tool and starts a fresh turn with the result appended.
  4. Persist assistant text + usage; emit done.

Cancellation: every turn registers a CancelHandle (see hearth/cancel.py) and
checks it at safe points — between provider events and before each tool runs —
raising TurnAborted so the client sees an `aborted` event instead of a hang.
Turn-level generation options (no_tools / max_tokens override) travel in the
`gen` dict; the UI always sends none of these now, but clients may still opt a
single turn out of tools via the API.

DB writes use short sessions (session_scope) so long streams never hold one open.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import re
import time
from typing import AsyncIterator

from .db import session_scope
from .models import Conversation, Message, PendingAction, Doc
from . import runtime_settings as rs
from . import prompt as prompt_mod
from . import workspaces
from . import audit
from .cancel import CancelHandle, TurnAborted
from .tools import ToolContext, tool_specs, TOOLS_BY_NAME, approval_mode
from .tools.base import ui_preview
from .llm.base import StreamEvent

MAX_TOOL_LOOPS = 6      # fallback cap when agent.max_tool_steps is unset
# Rough overhead per message in a chat template (framing tokens, not content). The
# context bar is explicitly labelled "estimated" when it falls back to chars/4 maths,
# so this constant only ever feeds flagged estimates — same spirit as the +16 used by
# routes_chat.context_usage_payload().
_PROMPT_MSG_OVERHEAD_TOKENS = 8


def load_history(conv_id: str) -> list[dict]:
    with session_scope() as s:
        msgs = s.query(Message).filter(Message.conversation_id == conv_id) \
            .order_by(Message.created_at.asc()).all()
    return [{"role": m.role, "content": m.content, "meta": m.meta} for m in msgs]


def _captioner_hint(settings: dict) -> str:
    """One line telling the model an audio captioner exists — only when it actually does.

    Tool descriptions already explain `describe_audio`; what they cannot say is that a file the
    user attached was *already* listened to, so its description is in the message rather than
    something to go and fetch. Emitted conditionally: claiming the capability while it is switched
    off or unconfigured would invite the model to describe recordings from its imagination, which
    is the one failure mode this feature must not have.
    """
    try:
        if not bool(settings.get("captioner.enabled")):
            return ""
        from . import captioner as cap
        if cap.preflight(settings):
            return ""
    except Exception:  # noqa: BLE001 — a hint is never worth breaking prompt assembly
        return ""
    return ("\n\n# Audio\nAn audio captioner model is available. Attached recordings are described "
            "for you automatically (that description arrives as text in your message, NOT as audio — "
            "you do not hear them). For any other audio file in the workspace use `describe_audio` "
            "(and `audio_info` first for a long one). Treat a caption as another model's report: it "
            "can be wrong about exact words, counts and identities, so never present it as a "
            "transcript unless you are sure. A recording can also lose its description — if you see "
            "only `[attached: name]` with no description text, it is not in your context: say so or "
            "run `describe_audio` on that path rather than speaking from imagination.")


def _workspace_context(user_id: str, settings: dict | None = None) -> tuple[str, bool]:
    """A compact list of files in the user's workspace so the model knows what exists.

    Returns (prompt_section, capped). `capped` is True when the listing hit the entry
    limit, which build_messages() forwards to prompt.assemble() so the preamble's claim
    about the listing matches what is actually in it (finding #1/B3).

    Two silences used to live here, both worse than an error:
      * The cap was hard-coded at 80 with no marker, so a workspace of 312 files was
        presented as a complete "ls -l equivalent". It now reads the admin setting and
        appends prompt.listing_truncation_note() naming how many entries were left out.
      * Any failure (jail error, DB fault) returned "", which the model read as "no
        files" — indistinguishable from a genuinely empty workspace, so it told the user
        their files were gone. It now says the listing FAILED and to call fs_list, and
        audit-logs the real exception (finding #5).
    """
    settings = settings if settings is not None else rs.get_all()
    try:
        cap = int(settings.get("agent.workspace_listing_entries") or 80)
    except (TypeError, ValueError):
        cap = 80
    cap = max(1, cap)
    try:
        probe = workspaces.list_tree(user_id, ".", max_entries=cap)
    except Exception as e:  # noqa: BLE001 — must not break prompt assembly, but must not lie
        _audit_listing_failure(user_id, e)
        return ("\n\n# Workspace files\n(listing FAILED — do NOT conclude the workspace is "
                "empty; use fs_list with '.' to try again)", False)
    entries = probe
    # Whether anything was left out comes from list_tree itself (`.truncated`), not from
    # counting: a capped read cannot report a total, which is exactly why the old listing
    # could only ever say nothing at all about the files it omitted.
    capped = bool(getattr(probe, "truncated", False))
    if not entries:
        return "\n\n# Workspace files\n(your workspace is currently empty)", False
    lines = []
    for e in entries:
        size = f" ({e['size']} B)" if (not e["is_dir"] and e.get("size") is not None) else ""
        lines.append(f"- {'[dir] ' if e['is_dir'] else ''}{e['name']}{size}")
    tail = ("\n" + prompt_mod.listing_truncation_note()) if capped else ""
    return ("\n\n# Workspace files (ls -l equivalent; read with fs_read, list a folder with fs_list)\n"
            "Files under uploads/ are ones the user attached to this chat.\n"
            + "\n".join(lines) + tail, capped)


def _audit_listing_failure(user_id: str, exc: Exception) -> None:
    """Best-effort audit trail for a workspace listing that could not be produced.

    Never raises: an audit failure must not turn a degraded prompt into a failed turn.
    """
    try:
        from . import audit
        audit.log(user_id or "?", "workspace_listing_failed",
                  {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    except Exception:  # noqa: BLE001
        pass


def _project_context(user_id: str) -> str:
    """Tell the model a real folder is linked, and how to move changes back (DEC-049).

    Deliberately CHEAP: one indexed DB read plus a manifest file open. It must never call
    diff_project() — that walks the copy and re-hashes every target file, which per message
    would be far too expensive. Counts belong in the on-demand summary endpoint the UI calls;
    the model is only told enough to know the tools exist and what `project/` means.
    """
    try:
        from . import project_link as pl
        link = pl.get_link(user_id)
        if not link:
            return ""
        prefix = link["rel_prefix"]
        imported = int(link.get("imported_at") or 0)
        # A hint, not a diff: the copy may be stale relative to the real folder.
        age = ""
        if imported:
            days = max(0, int((time.time() - imported) // 86400))
            if days >= 3:
                age = f" (imported {days} days ago — the real folder may have changed since)"
        return (
            f"\n\n# Linked project folder\n"
            f"A real folder on disk is linked to your workspace. Its contents are copied under "
            f"`{prefix}/` and behave like ordinary workspace files: read, search, edit and create "
            f"them with the normal fs tools using `{prefix}/...` paths.{age}\n"
            f"- Your edits stay in the workspace copy. They do NOT touch the real folder on their own.\n"
            f"- `sync_preview` (read-only) reports what differs and lists CONFLICTS — files the user "
            f"changed in the real folder since import.\n"
            f"- `sync_apply` writes the changes back. It always needs the user's approval for that "
            f"specific call, and refuses everything if any file conflicts.\n"
            f"Never claim a change is 'saved to the project' until sync_apply has succeeded. "
            f"Before applying, run sync_preview and tell the user what will change. Do not delete "
            f"files expecting them to disappear from the real folder — deletions are never applied."
        )
    except Exception as e:  # noqa: BLE001 — a broken link must never break prompt building
        # But it must not vanish either: an absent section reads as "no project is linked",
        # so the model tells the user there is nothing to sync while their folder sits there
        # unmentioned. Say what failed instead of saying nothing (finding #5).
        _audit_project_context_failure(user_id, e)
        return ("\n\n# Linked project folder\n(A linked project folder EXISTS but its details "
                "could not be read right now. Do not say no project is linked; call sync_preview "
                "to find out what is there.)")


def _audit_project_context_failure(user_id: str, exc: Exception) -> None:
    """Best-effort audit trail for an unreadable project link. Never raises."""
    try:
        audit.log(user_id or "?", "project_context_failed",
                  {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    except Exception:  # noqa: BLE001
        pass


def _image_parts(user_id: str, images: list):
    """Encode jail-relative image files as OpenAI `image_url` data-URL parts.

    Shared by user uploads (`_attachment_content`) and media tools (pdf_read /
    view_video), so both deliver pixels to the model through exactly the same shape —
    the one shape known to reach an mmproj projector. Items are
    {"path": <workspace-relative>, "mime": "image/png"}; a missing `mime` is guessed.
    Unreadable files are skipped rather than failing the whole message, and each read
    is capped at 8 MiB exactly as uploads are. Returns a list (possibly empty).
    """
    parts = []
    for img in images or []:
        try:
            rel = (img or {}).get("path") or ""
            mime = (img or {}).get("mime") or ""
            if not rel:
                continue
            if not mime.startswith("image/"):
                import mimetypes
                mime = mimetypes.guess_type(rel)[0] or "image/png"
            import base64
            b64 = base64.b64encode(workspaces.read_bytes(user_id, rel)[:8 * 1024 * 1024]).decode()
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
        except Exception:  # noqa: BLE001 — skip an unreadable image, keep the rest
            continue
    return parts


def _display_image_items(user_id: str, display):
    """Split display_media's files into (items the model can see, names of the rest).

    Only genuine images go to the model, because they travel as `image_url` data URLs and
    an mmproj projector is an IMAGE encoder: base64'ing a 40 MB clip into every subsequent
    prompt buys nothing except a rejected request or tens of thousands of wasted tokens.
    The kind is re-derived from the path at replay time rather than trusted from meta, so
    a file renamed since it was shown cannot quietly change what gets encoded.

    Note on the jail check: resolve_in_jail() returns a Path, and bool(Path(".")) is False
    — so it must never be used as a truth value. Existence is checked with .exists(), and
    every read still goes through workspaces.read_bytes(), which re-resolves the path itself.
    """
    from .tools.display_media import media_kind
    images: list = []
    blind: list[str] = []
    for item in display or []:
        rel = str((item or {}).get("path") or "") if isinstance(item, dict) else ""
        if not rel:
            continue
        try:
            abs_path = workspaces.resolve_in_jail(user_id, rel, allow_missing=False)
            if not abs_path.exists():
                continue
        except Exception:  # noqa: BLE001 — gone or escaped: nothing to send, and no reason to
            continue       # fail the whole request over a file that stopped existing
        kind = media_kind(rel)
        if kind == "image":
            mime = str((item or {}).get("mime") or "") or "image/png"
            images.append({"path": rel, "mime": mime})
        else:
            name = str((item or {}).get("name") or rel).strip() or rel
            blind.append(f"{name} ({kind or 'file'})")
    return images, blind


def _attachment_content(user_id: str, att_meta: list, text: str, captions: dict | None = None):
    """Build OpenAI multimodal `content` for a user message with attachments.

    Images become {type:image_url, image_url:{url:data:...;base64,...}} parts so a
    vision-capable model actually sees them (never raw bytes dumped as text). Audio does
    NOT get a part: the chat model generally has no audio encoder, and a base64 blob it
    cannot read is pure context waste — instead `captions` (from the captioner model, see
    hearth/captioner.py) is inserted per file. Other files stay as the text we already
    stored. Returns a str when there are no images (plain content, with any captions
    appended to it), else a list of content parts.
    """
    base_text = text
    extra_texts: list[str] = []
    parts = [{"type": "text", "text": base_text}]
    has_image = False
    captions = captions or {}
    for att in att_meta:
        mime = att.get("mime") or ""
        rel = att.get("rel_path") or ""
        cap_text = captions.get(rel)
        if not mime.startswith("image/"):
            # Non-image attachments stay text. A caption belongs with that text so the
            # plain-string return path (audio, no image — the common case) still carries it.
            if cap_text:
                extra_texts.append(cap_text)
            continue
        if cap_text:
            parts.append({"type": "text", "text": cap_text})
        try:
            import base64
            b64 = base64.b64encode(
                workspaces.read_bytes(user_id, rel)[:8 * 1024 * 1024]).decode()
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
            has_image = True
        except Exception:  # noqa: BLE001 — skip unreadable image, keep the text part
            continue

    if not has_image:
        return "\n\n".join([t for t in [base_text] + extra_texts if t])
    parts[0] = {"type": "text", "text": "\n\n".join(
        [t for t in [base_text] + extra_texts if t])}
    return parts


def _cancel_requested(cancel) -> bool:
    """Whether a cancel-like object is set — for code that may receive either shape.

    The asyncio turn handle (hearth.cancel.CancelHandle) exposes ``.cancelled``, while
    worker-thread helpers such as captioner.describe_file are polled through the
    ``threading.Event`` interface (``.is_set()``). Anything blocking long enough to be worth
    aborting must accept both, so this normalises them instead of assuming one — a wrong
    assumption here previously raised AttributeError mid-caption for directly-attached audio.
    Cheap and never raises: an unknown object reads as "cannot be aborted".
    """
    if cancel is None:
        return False
    try:
        if hasattr(cancel, "is_set"):
            return bool(cancel.is_set())
        return bool(getattr(cancel, "cancelled", False))
    except Exception:  # noqa: BLE001 — a dead handle reads as "not cancelled"
        return False


# Per-conversation caption cache: (workspace rel path -> text block for the model).
# In-memory only, bounded, and keyed by user as well as conversation so one family member's
# recording can never be quoted into another's context. Why cache at all: build_messages runs
# once per agent round, and re-captioning a 3-minute attachment on every round would cost
# minutes per turn for a description that cannot change. A restart simply forgets it and the
# next send pays once — acceptable, and far cheaper than storing audio-derived text in the DB.
_captions: dict[str, dict[str, str]] = {}
_CAPTION_CACHE_MAX_CONVOS = 64
_CAPTION_NOTE_MAX_CHARS = 1200   # a failure note is short; only captions can be long


def _caption_cache_key(user_id: str, conv_id: str) -> str:
    return f"{user_id}:{conv_id}"


def _remember_caption(user_id: str, conv_id: str, rel: str, text: str) -> None:
    key = _caption_cache_key(user_id, conv_id)
    if key not in _captions and len(_captions) >= _CAPTION_CACHE_MAX_CONVOS:
        # Evict the least recently *used* chat, not the least recently created one.
        # It used to rely on plain dict insertion order and said so in a comment ("crude
        # but bounded") — with more than 64 chats in play that dropped the caption of an
        # actively-used conversation, and the model then received a bare `[attached: x.mp3]`
        # while the UI still displayed a description: the user believed the model had heard
        # the recording when it had not (AUDIT #12).
        _evict_caption_lru()
    _captions.setdefault(key, {})[rel] = text[:_CAPTION_NOTE_MAX_CHARS + 4000]


def _evict_caption_lru() -> None:
    """Drop the caption entry with the oldest last-touch time. Never raises."""
    try:
        victim = min(_captions.items(), key=lambda kv: kv[1].get("_used_at", 0))[0]
        del _captions[victim]
    except (KeyError, StopIteration, TypeError, ValueError):
        # Unreadable stamps (an entry written before this existed) — fall back to the old
        # insertion-order eviction rather than letting the cache grow unbounded.
        try:
            del _captions[next(iter(_captions))]
        except StopIteration:
            pass


def get_caption(user_id: str, conv_id: str, rel: str) -> str:
    """A previously produced caption/note for this attachment, or ""."""
    key = _caption_cache_key(user_id, conv_id)
    entry = _captions.get(key) or {}
    if entry:
        # Touch on read so a chat in use is never the eviction victim (see above).
        entry["_used_at"] = time.time()
    return entry.get(rel, "")


# ---- durable captions (DEC-061) ---------------------------------------------------
# The in-memory cache above was the whole story, which caused two real losses: a page
# refresh showed nothing (the caption had no representation in the history endpoint at
# all), and a LATER turn in the same chat sent the model a bare `[attached: x.mp3]` —
# because build_messages only ever receives the CURRENT turn's caption dict. Persisting
# onto the user message's meta fixes both, the same way reasoning survives on
# meta.reasoning: no schema change, and it rides along with the row it belongs to.
#
# Stored as {"path": {"text": ..., "name": ..., "ok": bool}} so the client can render a
# label and say honestly whether a description exists. Kept separate from `text` in the
# model message: meta is never sent to the model, only the folded-in text is (see
# _attachment_content), so persistence cannot inflate a prompt by itself.
def persist_captions(conv_id: str, att_meta: list, captions: dict) -> None:
    """Attach caption blocks to the newest user row that references these attachments.

    Best-effort by design: a caption is derived data, and failing to store one must never
    lose the user's message or break the turn. Rows are matched by artifact id so an older
    message with the same filename is not overwritten.
    """
    if not conv_id or not captions or not att_meta:
        return
    ids = {a.get("artifact_id") for a in att_meta if a.get("artifact_id")}
    if not ids:
        return
    try:
        with session_scope() as s:
            rows = (s.query(Message)
                    .filter(Message.conversation_id == conv_id, Message.role == "user")
                    .order_by(Message.created_at.desc()).limit(5).all())
            for m in rows:
                try:
                    meta = json.loads(m.meta) if m.meta else {}
                except Exception:  # noqa: BLE001 — not our meta to preserve
                    continue
                if not isinstance(meta, dict):
                    continue
                row_atts = meta.get("attachments") or []
                if not any(a.get("artifact_id") in ids for a in row_atts if isinstance(a, dict)):
                    continue
                stored = meta.get("captions")
                if not isinstance(stored, dict):
                    stored = {}
                for a in row_atts:
                    rel = (a or {}).get("rel_path") or ""
                    block = captions.get(rel)
                    if rel and block:
                        stored[rel] = {"text": block, "name": (a or {}).get("name") or rel,
                                       "ok": not str(block).lstrip().startswith("[")
                                             or "NOT analysed" not in str(block)}
                meta["captions"] = stored
                m.meta = json.dumps(meta)
                return
    except Exception as e:  # noqa: BLE001 — derived data; never break a turn over it
        audit.log("", "caption_persist_failed", {"conv": conv_id, "err": str(e)[:120]})


def captions_from_meta(meta_str: str) -> dict:
    """{rel_path: text} from one message's meta JSON, or {} when there is nothing stored."""
    if not meta_str:
        return {}
    try:
        meta = json.loads(meta_str)
    except Exception:  # noqa: BLE001
        return {}
    stored = meta.get("captions") if isinstance(meta, dict) else None
    if not isinstance(stored, dict):
        return {}
    out = {}
    for rel, v in stored.items():
        if isinstance(v, dict) and v.get("text"):
            out[rel] = str(v["text"])
        elif isinstance(v, str) and v:
            out[rel] = v      # tolerate a plain-string shape rather than lose it
    return out


def caption_state(meta_str: str) -> dict:
    """{rel_path: {"name","ok"}} for display — whether a description exists per file.

    Separate from captions_from_meta because the UI needs the label and the honest ok flag,
    not just the text (the model path wants only text).
    """
    if not meta_str:
        return {}
    try:
        meta = json.loads(meta_str)
    except Exception:  # noqa: BLE001
        return {}
    stored = meta.get("captions") if isinstance(meta, dict) else None
    if not isinstance(stored, dict):
        return {}
    out = {}
    for rel, v in stored.items():
        if isinstance(v, dict):
            out[rel] = {"name": v.get("name") or rel, "ok": bool(v.get("ok"))}
        else:
            out[rel] = {"name": rel, "ok": True}
    return out



async def prepare_captions(user_id: str, conv_id: str, att_meta: list,
                           settings: dict, emit=lambda ev: None, cancel=None) -> dict:
    """Caption the audio attachments in `att_meta`; return {rel_path: text block}.

    Called by the chat route BEFORE the turn starts (and by steering delivery), because a
    caption is not free: ffmpeg conversion plus a model that has to listen. So progress is
    emitted as `notice` events while it happens — otherwise a 40-second silence before the
    first token is indistinguishable from a hang, which is the exact complaint this feature
    could otherwise add.

    Never raises and never blocks a message: every failure becomes a short text block saying
    what went wrong and what to do about it, so the main model can tell the user instead of
    inventing what the recording says. Already-captioned files are served from cache, which
    is what makes replaying history on later agent rounds cheap.

    `cancel` (optional) is a CancelHandle: when set, remaining files are skipped and the one
    in flight unwinds via caption_sync's own cancel checks — so Stop abandons a recording
    instead of listening to all of it first. The streamed description pieces are forwarded as
    `caption_chunk` notices (DEC-057), throttled to keep SSE quiet on long captions.
    """
    from . import captioner as cap

    out: dict[str, str] = {}
    if not att_meta or not user_id:
        return out
    candidates = [a for a in att_meta
                  if cap.is_audio(a.get("name") or "", a.get("mime") or "")]
    if not candidates:
        return out

    enabled = bool(settings.get("captioner.enabled"))
    auto = bool(settings.get("captioner.auto_route", True))
    for att in candidates:
        rel = att.get("rel_path") or ""
        name = att.get("name") or rel
        if not rel:
            continue
        # A Stop during a multi-file caption stops the whole batch, not just one file. Checked
        # at the top of each iteration so we never START listening to a new recording after the
        # user asked us to stop.
        if _cancel_requested(cancel):
            break
        cached = get_caption(user_id, conv_id, rel)
        if cached:
            out[rel] = cached
            continue
        if not enabled or not auto:
            # Deliberately NOT cached: enabling the captioner must start working at once.
            why = ("the audio captioner is disabled" if not enabled
                   else "auto-describing attachments is switched off (Admin → Audio)")
            out[rel] = (f"[{name}: audio was attached but NOT analysed — {why}. The assistant "
                        f"can ask for a description with describe_audio once it is available.]")
            continue

        emit({"type": "notice", "kind": "caption_start", "file": name,
              "text": f"listening to {name}…"})
        t0 = time.time()
        # Stream the captioner's own words as they arrive so the wait is visibly working, not
        # just labelled as working. Throttled: one notice per ~48 chars or ~0.3 s (whichever
        # comes first) keeps a long caption from spamming SSE while still feeling live.
        _pend: list[str] = []
        _last_flush = [time.time()]

        def _on_delta(piece: str, _name=name, _pend=_pend, _last=_last_flush) -> None:
            if not piece:
                return
            _pend.append(piece)
            now = time.time()
            joined = "".join(_pend)
            if len(joined) >= 48 or (now - _last[0]) >= 0.3:
                emit({"type": "notice", "kind": "caption_chunk", "file": _name, "text": joined})
                _pend.clear()
                _last[0] = now

        try:
            res = await cap.describe_file(user_id, rel, settings=settings,
                                          on_delta=_on_delta, cancel=cancel)
        except cap.CaptionError as e:
            # Flush whatever streamed before the failure so a partial caption is not lost.
            if _pend:
                emit({"type": "notice", "kind": "caption_chunk", "file": name,
                      "text": "".join(_pend)})
            note = (f"[{name}: could not be described — {e}]")
            _remember_caption(user_id, conv_id, rel, note)
            out[rel] = note
            emit({"type": "notice", "kind": "caption_done", "file": name, "ok": False,
                  "text": f"could not describe {name}"})
            continue
        except Exception as e:  # noqa: BLE001 — a captioner fault must never lose the message
            if _pend:
                emit({"type": "notice", "kind": "caption_chunk", "file": name,
                      "text": "".join(_pend)})
            note = (f"[{name}: describing it failed unexpectedly ({type(e).__name__}: "
                    f"{str(e)[:160]})]")
            _remember_caption(user_id, conv_id, rel, note)
            out[rel] = note
            emit({"type": "notice", "kind": "caption_done", "file": name, "ok": False,
                  "text": f"could not describe {name}"})
            continue

        dur = res.get("duration_s") or 0.0
        chunks = int(res.get("chunks") or 1)
        block = (f"[Captioner description of the attached audio '{name}' "
                 f"(duration {_fmt_secs(dur)}{', ' + str(chunks) + ' timed chunks' if chunks > 1 else ''}, "
                 f"produced by a separate audio model in {int(time.time() - t0)} s — it is NOT "
                 f"your own perception and can be wrong about words, counts or identities):\n\n"
                 + str(res.get("caption") or "").strip())
        for note in (res.get("notes") or []):
            block += f"\nnote: {note}"
        block += "]"
        _remember_caption(user_id, conv_id, rel, block)
        out[rel] = block
        # Flush any tail that did not reach the throttle threshold so the streamed block ends
        # with the complete caption (the client appends chunks; a dropped tail would truncate).
        if _pend:
            emit({"type": "notice", "kind": "caption_chunk", "file": name,
                  "text": "".join(_pend)})
            _pend.clear()
        emit({"type": "notice", "kind": "caption_done", "file": name, "ok": True,
              "text": f"described {name} in {int(time.time() - t0)} s"})
    return out


def _fmt_secs(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if s <= 0:
        return "unknown"
    return f"{s // 60}:{s % 60:02d}"


def build_messages(conv_id: str, user_id: str = "", captions: dict | None = None,
                   settings: dict | None = None) -> list[dict]:
    """Assemble OpenAI-style messages including a system prompt.

    Reconstructs the tool-calling protocol from stored history so a real
    llama-server sees valid assistant.tool_calls + tool.tool_call_id pairs:
      - assistant rows carry meta.tool_calls (with ids).
      - each following `tool` row carries meta.tool_call_id matching one call;
        we attach it to the most recent assistant message's tool_calls.
    User messages with image attachments are sent as multimodal content parts. Audio
    attachments are NOT sent as bytes — their `captions` (workspace-rel-path -> text from the
    captioner model, prepared by prepare_captions()) are inserted instead. A workspace file
    listing is appended to the system prompt so the model knows which files exist in this
    conversation's user space.
    """
    with session_scope() as s:
        conv = s.get(Conversation, conv_id)
        sys_doc = s.get(Doc, conv.system_prompt_doc_id) if conv and conv.system_prompt_doc_id else None
        proto_doc = s.get(Doc, conv.protocol_doc_id) if conv and conv.protocol_doc_id else None
        # No per-conversation doc -> the user's personal system prompt (chat sidebar).
        user_doc = ""
        if sys_doc is None and user_id:
            from .models import User
            u = s.get(User, user_id)
            user_doc = (u.system_prompt or "") if u else ""
    # Resolution mirrors prompt.current_prompt() (what the sidebar editor shows):
    # conv doc > personal prompt > admin default doc > built-in persona. current_prompt
    # opens its own DB session, so it must run outside the `with` block above — hence
    # this flag instead of an inline fallback expression.
    use_current = sys_doc is None and not user_doc
    if use_current:
        resolved = prompt_mod.current_prompt()
    else:
        resolved = (sys_doc.content if sys_doc else None) or user_doc
    system = prompt_mod.assemble(resolved, proto_doc.content if proto_doc else None)
    # The listing claim in the preamble must be worded from the SAME cap that produced the
    # listing below, so assemble() is called a second time when a listing is actually
    # appended: the first call's text cannot know whether the list will be capped.
    _settings = settings or rs.get_all()
    if user_id:
        ws_block, capped = _workspace_context(user_id, _settings)
        system = prompt_mod.assemble(resolved, proto_doc.content if proto_doc else None,
                                     settings=_settings, listing_capped=capped)
        system += ws_block
        system += _project_context(user_id)
    system += _captioner_hint(_settings)
    messages: list[dict] = [{"role": "system", "content": system}]
    last_assistant_with_calls: dict | None = None
    for m in load_history(conv_id):
        meta = {}
        if m.get("meta"):
            try:
                meta = json.loads(m["meta"])
            except Exception:  # noqa: BLE001
                meta = {}
        if m["role"] == "assistant":
            entry = {"role": "assistant", "content": m["content"] or ""}
            # Reasoning is NOT replayed into the request graph at all: it lives on meta for
            # the UI, and re-sending a model its own previous thinking inflates the prompt and
            # invites it to keep "thinking" out loud inside the answer. The retention policy
            # below governs what happens to it in HISTORY (i.e. what the user sees after a
            # reload) — never what the model is sent.
            if meta.get("tool_calls"):
                entry["tool_calls"] = _clean_tool_calls(meta["tool_calls"])
                last_assistant_with_calls = entry
            messages.append(entry)
        elif m["role"] == "tool":
            entry = {"role": "tool", "content": m["content"]}
            if meta.get("tool_call_id") and last_assistant_with_calls is not None:
                entry["tool_call_id"] = meta["tool_call_id"]
            messages.append(entry)
            # Media-tool images (pdf_read / view_video): a follow-up user-role message
            # carrying the actual pixels. Deliberately transient — never stored as its
            # own row, so the transcript shows one tool chip and the DB is not filled
            # with synthetic user messages; regenerated here from meta["images"] on
            # every replay, which keeps ordering right after the tool result it belongs
            # to. See DEC-052: this is the same multimodal shape hearth's upload path
            # uses, i.e. the only one known to reach an mmproj projector. A text-only
            # model simply ignores the parts; the tool's own text still explains them.
            #
            # display_media images are folded into the SAME message (the user sees one
            # tool chip either way), and its audio/video become a short plain-text note —
            # never pixels, see _display_image_items. Both are handled off a single `parts`
            # list so a display-only row (no meta["images"], e.g. a video shown alone) still
            # reaches the model instead of silently vanishing from its view of the chat.
            if user_id and (meta.get("images") or meta.get("display")):
                parts = _image_parts(user_id, meta.get("images") or [])
                shown, blind = _display_image_items(user_id, meta.get("display"))
                if shown:
                    parts = parts + _image_parts(user_id, shown)
                if blind:
                    messages.append({
                        "role": "user",
                        "content": f"[{meta.get('tool') or 'tool'} also put these on the "
                                   f"user's screen: {', '.join(blind)}. You were NOT given "
                                   f"them — you cannot see or hear them; describe them only "
                                   f"from what you know of the file itself.]"})
                if parts:
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text",
                                     "text": f"[{meta.get('tool') or 'tool'} returned these "
                                             f"{len(parts)} image(s) — the visual content of "
                                             f"the files named in the tool result above.]"}]
                                    + parts})
        elif m["role"] == "user" and meta.get("attachments") and user_id:
            # This row's OWN stored captions take precedence over the turn-level map, and
            # fill it in where the map has nothing. Without this, an attachment from an
            # earlier message reached the model as a bare `[attached: x.mp3]` on every
            # later turn (the caption dict only ever covers the message being sent now),
            # and a server restart lost even the current one. DEC-061.
            row_caps = captions_from_meta(m.get("meta") or "")
            if row_caps:
                merged = {**row_caps, **(captions or {})}
            else:
                merged = captions
            content = _attachment_content(user_id, meta["attachments"], m["content"], merged)
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": m["role"], "content": m["content"]})
    return messages


def _clean_tool_calls(tool_calls: list) -> list:
    """tool_calls as stored, with any harness `_DONE_NOTE` stripped from arguments.

    The note is written for the model's benefit at record time; re-sending it verbatim
    would compound it turn after turn (and inflate the token estimate), so we remove it
    on the way back out. Ids/types/names are untouched, keeping every tool_call/tool
    pair valid for llama-server.
    """
    out = []
    for tc in (tool_calls or []):
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str) and _DONE_MARK in args:
            tc = dict(tc)
            tc["function"] = {**fn, "arguments": _strip_done_note(args)}
        out.append(tc)
    return out


class ConversationGone(Exception):
    """The conversation was deleted while its turn was running.

    Raised at write points instead of letting an FK violation surface as a 500
    mid-stream. run_turn converts it into a plain `done` (see the except clause).
    """


def append_message(conv_id: str, role: str, content: str, meta: str = "") -> None:
    """Persist one message and bump the conversation's updated_at.

    Defensive by design: a turn can outlive its own conversation. The user may
    delete the chat while a tool is executing (abort is cooperative, so the tool
    finishes first), or a stale client may stream into an id that no longer
    exists. Writing a message with a dangling conversation_id would hit a FK
    violation and blow up the stream as a 500, so we check first and raise
    ConversationGone — callers in the loop stop, one message is dropped rather
    than crashing the connection.
    """
    with session_scope() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None:
            raise ConversationGone(conv_id)
        s.add(Message(conversation_id=conv_id, role=role, content=content, meta=meta))
        from .models import now
        conv.updated_at = now()


def conversation_exists(conv_id: str) -> bool:
    """Cheap existence probe used by the agent loop to stop writing into a
    conversation that was deleted mid-turn."""
    with session_scope() as s:
        return s.get(Conversation, conv_id) is not None


async def run_turn(user_id: str, conv_id: str, provider, settings: dict,
                   download_store: dict, pending_resume=None, gen: dict | None = None,
                   cancel: CancelHandle | None = None,
                   captions: dict | None = None) -> AsyncIterator[dict]:
    """pending_resume: (PendingAction row data) if this turn continues a gated action.
    gen: per-turn generation overrides ({no_tools, max_tokens}).
    cancel: handle from cancel.begin(); None means "cannot be aborted".
    captions: audio descriptions for this turn's attachments, prepared by the caller via
        prepare_captions() so the wait happens where progress can be shown.
    """
    ctx = ToolContext(user_id, settings, download_store, conversation_id=conv_id)
    # Let long-running tools observe cancellation (see ToolContext.cancel_event). Without
    # this the describe_audio tool would keep listening to a recording after Stop — the
    # repeat-suppress cache means an aborted tool is never re-run, so abort had no effect on
    # it at all. The handle's lifetime is owned by routes/_stream_turn, same as here.
    ctx.attach_cancel(cancel)
    gen = dict(gen or {})

    # If resolving a gated action, execute it now and append the tool result.
    try:
        if pending_resume is not None:
            async for ev in _resume_pending(pending_resume, ctx, provider, settings,
                                            download_store, gen, cancel):
                yield ev
            return

        async for ev in _agent_loop(user_id, conv_id, provider, settings, ctx, gen, cancel,
                                    captions=captions):
            yield ev
    except TurnAborted:
        # The user pressed Abort. History is already consistent (we only abort
        # between events / before tools), so just report it and stop cleanly.
        yield {"type": "aborted", "text": "Turn aborted by user."}
    except ConversationGone:
        # Chat was deleted underneath this turn (e.g. while a tool ran). Nothing
        # left to write or report into — end the stream quietly, flagged so routes
        # skip their post-stream usage bookkeeping for a row that no longer exists.
        yield {"type": "done", "conversation_gone": True}


def _max_steps(settings: dict) -> int:
    """How many model round-trips (tool loops) one turn may take."""
    try:
        return max(1, int(settings.get("agent.max_tool_steps", MAX_TOOL_LOOPS)))
    except (TypeError, ValueError):
        return MAX_TOOL_LOOPS


# Token budget for a round that OFFERS tools. Tool-call arguments are generated text, so
# the same `max_tokens` cap that limits a reply also truncates an in-progress fs_write —
# which is why long writes arrive as unparseable JSON and get refused outright. Admin-
# editable (agent.tool_arg_max_tokens); this constant is only the fallback when the
# settings dict has no value at all, e.g. in tests that hand-build one.
TOOL_ARG_TOKENS = 16384


def _round_max_tokens(admin_value, tools_offered: bool, tool_arg_value=None) -> int:
    """The `max_tokens` to send for one agent round.

    Tool-free rounds get the admin's `model.max_tokens` — that setting means "how long may
    a reply be", and it stays true to that. Rounds WITH tools get
    max(model.max_tokens, agent.tool_arg_max_tokens) so a large file write is not truncated
    into malformed JSON. Never lowers an explicit larger value, and tolerates junk (missing
    or non-numeric → the built-in TOOL_ARG_TOKENS). Kept pure — no n_ctx knowledge here; the
    provider clamps to whatever slot the server actually has.
    """
    try:
        base = int(admin_value or 0)
    except (TypeError, ValueError):
        base = 0
    if not tools_offered:
        # A reply round keeps the admin's value; only fall back when it is missing/junk, and
        # to something modest — this is a chat answer, not a file write.
        return base or 2048
    try:
        budget = int(tool_arg_value or 0)
    except (TypeError, ValueError):
        budget = 0
    return max(base, budget or TOOL_ARG_TOKENS)


_LOG_POLL_S = 0.6   # how often to read new llama-server log lines during a model round


def _progress_event(ev: dict) -> StreamEvent:
    """Turn one parsed log event into a `progress` StreamEvent with an honest ETA.

    Carries llama.cpp's own numbers and the ONE calculation worth doing here:

        remaining tokens / last prefill rate = seconds left

    The rate is what llama.cpp reported for THIS prompt on this very line, never averaged or
    extrapolated; the remaining token count comes from the server's `n_tokens done` +
    `progress fraction` pair (so the whole prompt size is n_done / progress). It is still a
    flat-rate guess over a curve that is not flat (chunked prefill, cache hits), hence "≈".

    Honesty rules that must not be relaxed:
      * Below 1 % progress nothing is said at all. print_timing rounds the fraction to two
        decimals, so dividing a handful of tokens by a hundredth of a prompt invents a huge
        number out of rounding noise.
      * No rate -> no ETA (not zero). format_seconds returns "" for anything under a second,
        so a caller that appends it gets silence rather than a misleading "0s left".
    """
    data = dict(ev or {})
    if data.get("kind") == "prefill":
        n_done = data.get("n_tokens") or 0
        frac = data.get("progress") or 0.0
        rate = data.get("rate_tps")
        data["phase"] = "prefill"
        data["pct"] = max(0.0, min(1.0, float(frac)))
        data["prompt_tokens"] = int(n_done)
        n_left = None
        if n_done and frac and rate:
            n_total = int(round(n_done / frac))
            n_left = max(0, n_total - int(n_done))
            data["prompt_total"] = n_total
        if n_left and rate and frac >= 0.01:
            secs = n_left / float(rate)
            from .perf import format_seconds
            txt = format_seconds(secs)
            if txt:
                data["eta_s"] = round(secs, 1)
                data["eta"] = txt
    elif data.get("kind") == "perf":
        # A pure speed line (decode rate while generating, or a perf-context summary).
        # No percentage to report — the UI must not show a bar it has no basis for.
        data.setdefault("phase", "decode")
    return StreamEvent("progress", data=data)


def _merge_progress(st: dict | None, evs: list) -> tuple[dict, list]:
    """Advance a progress state machine by one batch of log events.

    THE ONLY place the ETA baseline may be computed, which is why it exists at all: a naive
    per-line calculation produced nothing useful, because llama.cpp prints its first
    prompt-processing line before print_timing has any rate to report (rate=None), and each
    later line carries only the CURRENT rate — never the elapsed time needed to work out how
    fast the prompt got from 0 % to here. So: latch the first line's implied total
    (`n_done / progress`) as this prompt's size, then on every subsequent line divide what is
    left of that total by the current rate.

    State is per-prompt and must be reset between prompts (see _agent_loop's cursor reset) —
    a baseline from one prompt says nothing about the next: different size, cache state and
    batch shape all change the rate. Returns (new_state, events_carrying_eta)."""
    st = dict(st or {})
    out = []
    for ev in (evs or []):
        e = dict(ev or {})
        if e.get("kind") != "prefill":
            out.append(e)
            continue
        n = int(e.get("n_tokens") or 0)
        frac = float(e.get("progress") or 0.0)
        rate = e.get("rate_tps")
        total = None
        if n and frac > 0:
            total = int(round(n / frac))
        # First usable line latches the prompt size for the rest of this prompt.
        if st.get("total") is None and total:
            st["total"] = total
        # The server's own total for THIS line is the better estimate once it has read more
        # (early lines round hard: 5000 tokens at progress=0.01 implies 500k). Prefer the
        # larger of the two, so a rounding-noise early line cannot inflate later ETAs.
        if total and st.get("total"):
            st["total"] = max(st["total"], total) if frac < 0.5 else total
        cur_total = st.get("total")
        if cur_total and rate and frac >= 0.01:
            left = max(0, cur_total - n)
            secs = left / float(rate)
            from .perf import format_seconds
            txt = format_seconds(secs)
            # Once the server says we are at (or past) the end, stop guessing: an ETA of "0s"
            # next to 100 % is noise, and llama.cpp keeps printing lines while it wraps up.
            if txt and frac < 0.995:
                e["eta_s"] = round(secs, 1)
                e["eta"] = txt
        out.append(e)
    return st, out


async def _stream_checked(provider, messages, specs, params, cancel, log_src=None):
    """Forward provider events, but bail out promptly if the turn is aborted.

    IDLE timeout, reset on every *provider* event: the guard fires only when the stream
    itself produced NOTHING for `stream_idle_timeout_s`. The deadline is tracked as an
    absolute instant and pushed forward only by real provider activity — never by a
    progress poll, and never recomputed at the top of an iteration. (Both of those were
    bugs: with the log poller armed every `_LOG_POLL_S` the countdown restarted each pass,
    so a wedged stream was never reported at all.) It used to wrap the whole completion in
    one `asyncio.timeout(deadline)`, which — despite the docstring — was a hard wall-clock
    cap per round, NOT an idle timer. A too-small HEARTH_STREAM_IDLE_TIMEOUT_S (or `.env`
    leftover) then killed every request at exactly N seconds regardless of progress:
    llama-server logged `stop: cancel task` 1.000 s after each launch and the UI showed
    "connection lost (ReadError)". `stream_idle_guard=false` still disables it.

    `log_src` (optional) is a zero-arg callable returning ``(events, new_state)`` from
    llama-server's own log — see ModelManager.poll_log_perf. While it is polled the loop also
    yields `progress` events, because during prefill the OpenAI stream sends NOTHING at all:
    without this a 260 k context looks exactly like a hang. The poller is a waiter in the
    same race as the provider event and the abort. Its completion is NOT an error and NOT a
    timeout: it retires nothing, and the round keeps waiting on the SAME provider read task —
    see the "ONE provider read at a time" note below, which is what the crash this loop grew
    was all about.
    """
    from .config import get_boot_settings
    bs = get_boot_settings()
    idle_s = float(bs.stream_idle_timeout_s)
    use_timeout = bs.stream_idle_guard and idle_s > 0

    agen = provider.stream(messages, specs, params).__aiter__()

    # ONE provider read at a time, for the whole model round.
    #
    # An async generator may only have one `__anext__()` in flight; entering it again
    # raises RuntimeError "anext(): asynchronous generator is already running". That is
    # easy to do by accident with a single-shot read task: whenever the log poll (or an
    # abort) completed first, the loop retired that read and created a new one — but
    # *cancelling* a `__anext__()` task does not synchronously unwind the generator
    # frame, and abandoning it without cancelling at all leaves the read live. Either way
    # the next iteration re-entered a still-running generator. (This is exactly what the
    # idle-progress poller exposed: during prefill the provider emits nothing, so the
    # poll won the race every ~0.6 s.)
    #
    # So the read task lives across iterations and is only ever created once, and every
    # exit path closes `agen` FIRST (aclose() drives the pending read out through the
    # provider's own finally) instead of cancelling it — see the teardown note at the
    # bottom. A second creation attempt would be a bug, so it raises loudly rather than
    # corrupting the stream.
    read_task: asyncio.Task | None = None

    def read_once():
        """The single provider-read task (created at most once per round)."""
        nonlocal read_task
        if read_task is not None:
            raise RuntimeError(
                "provider read already in flight — the stream loop must reuse it, not "
                "re-arm it")
        read_task = asyncio.ensure_future(agen.__anext__())
        return read_task

    # Progress bookkeeping for THIS model round (see _merge_progress). Per-round by design:
    # an ETA baseline describes one prompt, and reusing a previous prompt's latched total
    # would estimate from numbers a different prompt produced.
    prog_state: dict | None = None

    async def _poll_log():
        """Sleep a beat, then read new log lines. Returns (events, baseline_state).

        THE reason this loop yields anything during prefill: the OpenAI endpoint streams no
        progress at all while a prompt is being read, so without this a 260 k context looks
        exactly like a hang.

        This is a PLAIN waiter in the race below — sleep, then read — and that shape is not
        incidental. Three earlier designs (single-shot poll cancelled on every provider event;
        re-arm from the delivery branch; a self-driving pump feeding an asyncio.Queue drained
        via `await q.get()`) all delivered NOTHING while every test stayed green: `asyncio.wait`
        does not wake for a bare `Queue.get()` waiter, so the queue filled and never drained.
        A timed sleep is what `asyncio.wait` reliably observes — the same reason the abort
        branch has always worked. Keep this shape; do not reintroduce a queue.

        Empty results are reported as empty and the caller re-arms: no event means "nothing new
        in the log yet", which is the common case between llama.cpp's ~1 s print cadence.
        """
        await asyncio.sleep(_LOG_POLL_S)
        if log_src is None:
            return []
        try:
            # The CALLER owns the byte cursor and passes a ready-made callable; calling it with
            # no args is the whole contract. (An earlier version did `nonlocal log_state` here —
            # which compiled, because that name also exists in _agent_loop, but raised NameError
            # at runtime on every poll. Swallowed by the except below, so live prefill progress
            # silently never appeared while every test stayed green.)
            events, _new = log_src()
            if not events:
                return []
            nonlocal prog_state
            prog_state, enriched = _merge_progress(prog_state, events)
            return [_progress_event(e) for e in enriched]
        except Exception:  # noqa: BLE001 — progress telemetry must never break a turn
            return []

    timed_out = False
    aborted = False
    eof = False
    wait_deadline: float | None = None      # see the idle-deadline note in the loop
    try:
        while True:
            # Race three outcomes: the next provider event, an idle timeout, and a
            # user abort. The abort branch is what makes Stop take effect *now* —
            # previously cancellation was only observed after __anext__() returned,
            # so during a long prefill or tool call (no events) Stop appeared to do
            # nothing until the round finished, and llama-server kept decoding.
            #
            # `read_once()` returns the round's single read task; on iterations after a
            # progress-only wake-up it is the SAME pending task, deliberately — see the
            # "ONE provider read at a time" note above for why re-arming it was fatal.
            next_task = read_once() if read_task is None else read_task
            waiters = [next_task]
            cancel_task = None
            if cancel is not None:
                cancel_task = asyncio.ensure_future(cancel.wait_cancelled())
                waiters.append(cancel_task)
            # Fourth waiter: the log poll. A plain timed sleep + read, re-created every
            # iteration — deliberately NOT a long-lived task or a queue (see _poll_log for what
            # that cost). It resolves after _LOG_POLL_S whether or not the log grew; an empty
            # result just re-arms on the next pass, which is why this shape costs nothing while
            # the model is streaming tokens normally. It is what lets prefill percentage reach
            # the UI during the one phase where the provider emits no events at all.
            prog_task = None
            if log_src is not None:
                prog_task = asyncio.ensure_future(_poll_log())
                waiters.append(prog_task)
            # THE idle deadline: absolute, not per-waiter. `wait_deadline` is the moment
            # the round is allowed to give up, and it is only pushed forward when the
            # PROVIDER produces something (see the delivery branch). It must never be
            # recomputed from "now" at the top of an iteration — with the log poller armed
            # every _LOG_POLL_S, that made the deadline impossible to reach: each poll
            # restarted the countdown, so a genuinely wedged stream polled forever (a 40 s
            # hang stayed live past 25 s with `stream_idle_timeout_s=1.5`) instead of being
            # reported. Progress polls are not evidence of liveness either — they read the
            # server's log, which keeps printing while a request is stuck.
            if use_timeout and wait_deadline is None:
                wait_deadline = time.monotonic() + idle_s
            timeout = (max(0.0, wait_deadline - time.monotonic())
                       if use_timeout else None)
            try:
                done, _pending = await asyncio.wait(waiters, timeout=timeout,
                                                    return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # Our own task was cancelled (client disconnect / shutdown): release the
                # side waiters and propagate so the route tears down the stream. The
                # provider read is deliberately NOT cancelled — see the abort branch: it
                # is unwound by aclose() in `finally`, which is also what closes the HTTP
                # connection. (The generator's frame cannot be dropped while a read owns
                # it, so aclose() must come first.)
                if cancel_task is not None:
                    cancel_task.cancel()
                if prog_task is not None:
                    prog_task.cancel()
                raise

            # Progress delivery, BEFORE the provider branch so a `usage`/`done` chunk arriving
            # in the same batch cannot crowd out the percentage the user is waiting on. The
            # poller is re-created at the top of the next iteration either way — it is stateless
            # apart from prog_state, which _poll_log owns.
            if prog_task is not None and prog_task in done:
                try:
                    for ev in (prog_task.result() or []):
                        yield ev
                except Exception:  # noqa: BLE001 — telemetry only
                    pass

            if cancel_task is not None and cancel_task in done:
                aborted = True
                # Do NOT cancel the provider read here. Cancelling a `__anext__()` task
                # does not unwind the generator frame synchronously, so `agen` would stay
                # RUNNING while `finally` tries to close it — CPython then refuses with
                # "aclose(): asynchronous generator is already running", which escaped as
                # an error event and looked like a crash. Leaving the read pending instead
                # means the GeneratorExit thrown by aclose() below unwinds it through the
                # provider's own `finally` (which is what closes the HTTP connection and
                # tells llama-server to stop decoding).
                if cancel_task is not None:
                    cancel_task.cancel()
            elif next_task in done:
                # The read is finished; allow the next iteration to create a fresh one.
                read_task = None
                # Liveness reset for the idle guard, set BEFORE consulting the result so
                # it applies whatever the read produced — a token, a usage chunk, or an
                # exception. The guard is an IDLE timer (reset on any provider activity),
                # not a wall-clock cap on a round: a model that keeps emitting must never
                # be cut off mid-stream. Poll events do NOT reset it; see the deadline
                # note above for why recomputing it per iteration made the guard
                # unreachable while the poller was armed.
                if use_timeout:
                    wait_deadline = time.monotonic() + idle_s
                try:
                    ev = next_task.result()
                except StopAsyncIteration:
                    eof = True
                    break
                yield ev
            elif prog_task is not None and prog_task in done:
                # Only the log poll fired (the usual case during prefill, where the
                # provider emits no events at all). That is NOT an idle timeout — it is
                # progress. Loop on with the SAME pending read task; creating a second
                # `__anext__()` was the crash.
                #
                # Falling through to the timeout branch here was the original bug: the
                # very first poll (~_LOG_POLL_S) ended the round as "model produced no
                # output for 300s" and cancelled the in-flight read. Cancelling does not
                # unwind the generator frame synchronously, so the next iteration's fresh
                # `__anext__()` re-entered a still-RUNNING generator: RuntimeError
                # "anext(): asynchronous generator is already running", raised inside the
                # read and surfaced at next_task.result().
                continue
            else:
                # Genuine idle timeout: NEITHER a provider event NOR the ~0.6 s log poll
                # completed inside `idle_s`. (Only the poll completing is not a timeout —
                # see the branch above, and the "ONE provider read at a time" note for why
                # re-arming that read used to blow up.)
                #
                # Here we DO cancel the read: unlike an abort, a timeout means the stream
                # may be wedged forever, so leaving it pending would hang `finally` — and
                # aclose() cannot drive a hung frame out either. Then wait for that
                # cancellation to unwind the generator (bounded), because closing while it
                # is still RUNNING raises "aclose(): asynchronous generator is already
                # running". If the provider ignores cancellation entirely, the bounded
                # wait gives up and `finally` falls back to abandoning the frame.
                timed_out = True
                if cancel_task is not None:
                    cancel_task.cancel()
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(next_task), timeout=2.0)
                break
            # Defensive: if cancel got set by some other path mid-loop, stop now.
            if cancel is not None and cancel.cancelled:
                aborted = True
                break
    finally:
        # Release the provider generator on every exit path (abort, timeout, error,
        # disconnect). aclose() throws GeneratorExit into it so llama_server.stream()'s
        # `finally` runs and the HTTP connection is returned/closed — which is what tells
        # llama-server to cancel the in-flight task instead of decoding to completion.
        #
        # The ordering rule this whole function exists around: a pending `__anext__()`
        # must never be cancelled and then abandoned, because that leaves the generator
        # RUNNING (the frame unwinds only when its own task finishes) and CPython refuses
        # to close a running async generator — "aclose(): asynchronous generator is
        # already running". That escaped through _agent_loop/run_turn into the route,
        # where it was misread as a crash. So: aclose() FIRST (it drives the read out
        # through the provider's own finally), and only if CPython still says the
        # generator is busy do we fall back to cancelling the read and retrying once.
        closer = agen.aclose()
        try:
            await asyncio.shield(closer)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — closing a dead stream is not our problem
            if read_task is not None and not read_task.done():
                read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(read_task)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(agen.aclose())
    if aborted:
        raise TurnAborted()
    if timed_out:
        yield StreamEvent("error", text=f"model produced no output for {int(idle_s)}s — "
                                        f"stream timed out")
        yield StreamEvent("done", data={"finish": "timeout"})


async def _run_tool_streaming(t, ctx) -> AsyncIterator[dict]:
    """Await one tool while forwarding anything it reports to the client.

    A tool that takes minutes (describe_audio listening to a recording) has no way to yield
    into this generator itself — it is awaited, not iterated — so it writes events into
    `ctx`'s progress sink instead and we drain them here. The task/queue shape is the same
    one routes_chat uses around prepare_captions: while the tool runs we poll a queue and
    yield each event; cancellation still reaches the tool through ctx.cancel_event(), which
    it polls, so Stop abandons a recording rather than leaving the loop waiting on it.

    Yields progress events and then the ToolResult as its FINAL yielded value — an async
    generator cannot `return` a value, so callers take the result from the last item. This is
    also the only place tool progress can reach the client: `_pending_notices` used to be
    drained only in the steering branch, so anything queued elsewhere was silently dropped.

    Module-level rather than nested in _agent_loop because the gated-resume path needs it
    too: a describe_audio call set to approval 'ask' takes just as long as an auto one, and
    streaming only on the auto path would make the behaviour depend on an admin toggle.
    """
    q: asyncio.Queue = asyncio.Queue()

    def _sink(ev: dict) -> None:
        # Called from the tool, possibly from a worker thread: only enqueue. put_nowait on an
        # unbounded queue cannot block and is safe enough here (worst case a progress line is
        # lost, never the result).
        try:
            q.put_nowait(ev)
        except Exception:  # noqa: BLE001 — progress must never break a tool
            pass

    ctx.attach_progress(_sink)
    task = asyncio.create_task(_run_tool(t, ctx))
    try:
        while True:
            if task.done():
                break
            try:
                yield await asyncio.wait_for(q.get(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        # Drain anything queued between the last event and completion, in order.
        while not q.empty():
            yield q.get_nowait()
        yield task.result()   # final item: the ToolResult itself (see docstring)
    finally:
        ctx.attach_progress(None)
        if task.done():
            await task   # propagate CancelledError / tool exceptions to the caller


async def _agent_loop(user_id, conv_id, provider, settings, ctx,
                      gen: dict | None = None, cancel=None, round_offset: int = 0,
                      captions: dict | None = None) -> AsyncIterator[dict]:
    """Run model round-trips until the model stops asking for tools.

    `round_offset` shifts the ids used to correlate progress/pending chips so a resumed
    gated turn (`_resume_pending`) gets its own chip namespace instead of colliding with
    rounds retired before the approve/deny card appeared.
    `captions` pre-supplies audio descriptions for this turn's attachments (see
    prepare_captions); later rounds read them from the module cache instead of re-listening.
    """
    gen = gen or {}

    _pending_notices: list[dict] = []

    def _emit_notice(ev: dict) -> None:
        # prepare_captions runs in a worker thread and reports progress through a plain
        # callback, but this generator is the only way to reach the client. Queuing here and
        # draining at the next seam keeps the loop single-threaded; it is best-effort by
        # design — losing a "listening…" notice must never affect the caption itself.
        try:
            _pending_notices.append(ev)
        except Exception:  # noqa: BLE001 — progress only
            pass


    specs = [] if gen.get("no_tools") else tool_specs(settings)
    max_steps = _max_steps(settings)
    steps_used = 0
    # Repeat guard: tool name+args -> the result we already gave for it THIS turn.
    # A resumed gated turn continues in a fresh _agent_loop, so its history is what
    # stops repeats there (the approved call itself must always run — see DEC-045).
    # See the guard in the auto-tool loop and _call_key/_DONE_NOTE below.
    calls_done: dict[str, str] = {}
    # Turn-level usage bookkeeping. llama-server only reports `usage` on its final
    # chunk, which we never see when a tool request ends the stream early, so each
    # loop tracks its own prompt size + generated characters and synthesises an
    # estimated event (see _estimate_usage / _merge_usage).
    turn_usage: dict | None = None
    # Live progress source (llama-server log tail). Only for a real llama-server — the stub
    # synthesises its own `progress` events, and an externally-run server writes its log
    # somewhere we cannot see, so passing nothing there is the honest answer rather than a
    # guess. Admin → Model → "Show prefill progress from the server log" turns it off.
    log_src = None
    _caps = {}
    try:
        _caps = provider.capabilities() if hasattr(provider, "capabilities") else {}
    except Exception:  # noqa: BLE001 — a capability probe must never break a turn
        _caps = {}
    if (_caps.get("exposes_log_progress")
            and bool(settings.get("agent.prefill_progress", True))):
        from .manager import get_manager
        _mgr = get_manager()
        # Mutable cell holding the poll CURSOR. Owned here so the round loop can reset it
        # between rounds: progress state describes ONE prompt, and carrying a previous
        # prompt's baseline into the next would estimate from numbers a different prompt
        # produced (different size, cache state and batch shape).
        #
        # The cursor is poll_log_perf()'s OWN dict shape ({"offset": int}), NOT None. Passing
        # None was the bug that killed live progress outright: `state=None` means "begin at
        # END of file", so every reset re-anchored the cursor to current EOF and each new
        # print_timing line was, by definition, already behind it — nothing was ever read.
        # Standalone tests of poll_log_perf passed because they threaded the returned dict
        # through correctly; only this caller used the wrong shape.
        log_state = {"v": {"offset": -1}}

        def log_src(_m=_mgr, _st=log_state):
            return _m.poll_log_perf(_st["v"])
    for loop in range(max_steps):
        if cancel is not None and cancel.cancelled:
            raise TurnAborted()        # A turn can outlive its conversation (deleted while a tool ran, or a stale
        # client id). Stop before asking the model anything rather than failing on
        # the first write. Checked once per loop, not per token — cheap and enough.
        if not conversation_exists(conv_id):
            raise ConversationGone(conv_id)
        # STEERING SEAM. This is the one place inside a turn where the message list is whole
        # again: every tool_call_id from the previous round already has its result appended
        # and no request is in flight, so appending user speech here cannot orphan the tool
        # protocol. Reached at the top of the FIRST iteration too, which is what delivers a
        # message queued while no turn was running (or after this one ended) instead of
        # letting it sit until some later turn. User text is appended as ordinary user rows —
        # the model cannot tell steering from a normal message, and that is the point.
        try:
            from . import steering as _steering
            steered = _steering.drain(conv_id)
        except Exception:  # noqa: BLE001 — a queue fault must never break a turn
            steered = []
        # Steering delivery: the queued item already carries resolved artifact refs (the
        # /api/chat/steer route resolved them against the user's own artifacts at enqueue
        # time, so a queue entry can never smuggle a path). Meta shape is identical to the
        # normal send above, which is what lets build_messages render attachments unchanged.
        for item in steered:
            att_meta = item.get("attachments") or []
            text_for_msg = item.get("text") or ""
            if att_meta:
                names = ", ".join(x["name"] for x in att_meta)
                text_for_msg = (f"[attached: {names}]"
                                + ("\n" + text_for_msg if text_for_msg else ""))
            append_message(conv_id, "user", text_for_msg,
                           meta=json.dumps({"attachments": att_meta}) if att_meta else "")
            # Same audio treatment as a normal send: steered speech carrying a recording must
            # not arrive mute just because it skipped the route's pre-turn captioning. The
            # results are MERGED into this turn's caption map — `captions` was built before
            # the turn started and cannot know about files that arrived mid-turn, so without
            # this the block below would silently drop them and build_messages would render a
            # bare filename for audio the captioner had just listened to.
            if att_meta:
                try:
                    got = await prepare_captions(user_id, conv_id, att_meta, settings,
                                                 emit=_emit_notice, cancel=cancel)
                    if got:
                        captions = {**(captions or {}), **got}
                        # Persist onto the row we just appended, so a steered recording is
                        # not a second-class attachment: it survives refresh and later turns
                        # exactly like one sent through the normal path (DEC-061).
                        persist_captions(conv_id, att_meta, got)
                    # Surface anything the captioner queued while we awaited it.
                    for _n in _pending_notices:
                        yield {"type": "notice", **_n}
                    _pending_notices.clear()
                except Exception:  # noqa: BLE001 — never lose a steered message
                    pass
            yield {"type": "steered", "text": item.get("text") or "",
                   "attachments": [x["name"] for x in att_meta]}
        messages = build_messages(conv_id, user_id, captions=captions, settings=settings)
        # Vision recovery. Once the server has REJECTED a request that carried image parts,
        # the loaded model provably has no projector — regardless of what Admin says is
        # configured (a mismatched mmproj loads as text-only). Stripping the parts for the
        # REST OF THIS TURN keeps the conversation alive instead of failing on every round
        # forever; the notice tells the model why the file it asked for is gone, so it stops
        # asking. Turn-scoped and request-only: nothing is deleted from the DB, and a later
        # turn with a properly loaded vision model sends images again.
        if _strip_images_for.get(conv_id) and any(
                isinstance(m.get("content"), list) for m in messages):
            messages = _strip_image_parts(messages)
        params = {k.split(".", 1)[1]: v for k, v in settings.items() if k.startswith("model.")}
        # `model.max_tokens` is BOTH the per-reply cap and the cap on a tool call's
        # arguments, because those arguments are themselves generated text. A long
        # fs_write/exec therefore gets its JSON cut off mid-string, llama.cpp cannot parse
        # it, and Hearth refuses the call ("arguments were not valid JSON") — nothing is
        # written, and raising the setting only moves the ceiling (32768 tokens ≈ 100 KB of
        # HTML still truncates). Rounds that OFFER tools get a separate, larger budget so a
        # file write is not strangled by the chat-reply limit. Tool-free rounds — the final
        # answer — stay at model.max_tokens, which is what that setting has always meant.
        # The admin value remains the floor, so this can never lower an explicit choice.
        params["max_tokens"] = _round_max_tokens(params.get("max_tokens"), bool(specs),
                                                 settings.get("agent.tool_arg_max_tokens"))
        if gen.get("max_tokens"):
            try:
                params["max_tokens"] = int(gen["max_tokens"])
            except (TypeError, ValueError):
                pass
        assistant_text_parts: list[str] = []
        # The model's own reasoning for THIS round, kept separate from the answer (see the
        # `reasoning` event branch below).
        reasoning_parts: list[str] = []
        tool_requests = []
        # Per-round bookkeeping for usage + progress chips. `round_id` is unique per
        # agent-loop iteration and lets the client tell apart chips belonging to
        # different rounds (a re-stream of the same tool must not reuse a dead chip).
        round_id = f"r{loop + round_offset}"
        gen_chars = 0                       # content chars + tool-argument chars
        prompt_tokens_est = _estimate_prompt_tokens(messages)
        stream_t0 = time.time()
        # Fresh log cursor for THIS round's prompt (see log_src above). Same dict shape as
        # the initial value — {"offset": -1} means "start at EOF", which is what a new prompt
        # wants: its own print_timing lines only, never an earlier prompt's.
        # NOTE: this consumes nothing (the file has not grown since the last poll in the
        # previous round) but it MUST NOT return the priming value into the live state as
        # `None`; poll_log_perf() reads None as "begin at EOF", so assigning a bare None here
        # would re-anchor past every line this round is about to write — the exact bug that
        # made live prefill progress invisible while standalone tests stayed green.
        if log_src is not None:
            try:
                _ev0, _new_state = log_src()
                log_state["v"] = _new_state if isinstance(_new_state, dict) else {"offset": -1}
            except Exception:  # noqa: BLE001 — progress telemetry only
                log_state["v"] = {"offset": -1}
        async for ev in _stream_checked(provider, messages, specs, params, cancel,
                                        log_src=log_src):
            if ev.type == "token":
                assistant_text_parts.append(ev.text)
                gen_chars += len(ev.text or "")
                yield {"type": "token", "text": ev.text}
            elif ev.type == "reasoning":
                # The model's thinking, kept OUT of the answer stream (it is never appended to
                # assistant_text_parts — mixing it in would make the model imitate its own
                # reasoning as prose on later turns). Shown live and persisted on the
                # assistant row's meta so a reload still shows it, and so the retention policy
                # has something to strip from history (see _strip_reasoning_from_history).
                reasoning_parts.append(ev.text)
                gen_chars += len(ev.text or "")
                yield {"type": "reasoning", "text": ev.text}
            elif ev.type == "progress":
                yield {"type": "progress", "data": ev.data}
            elif ev.type == "tool_progress":
                # The model is still GENERATING a tool call (its arguments stream in
                # token by token). Forward it so the UI can show live progress —
                # without this, composing a big fs_write looks like a hang. The raw
                # JSON fragment never crosses the wire; only name + byte count do.
                # `path_hint` is one already-completed string value from the partial
                # JSON (in practice the file path), so the chip can say WHICH file it
                # is writing — without shipping any of the content itself.
                yield {"type": "tool_progress", "name": ev.data.get("name", ""),
                       "chars": int(ev.data.get("chars") or 0),
                       # Wall-clock since this round's request started, so the chip can show
                       # how long the model has been composing. The byte counter alone cannot
                       # distinguish 'still writing a big file' from 'the server went quiet',
                       # which is exactly the question during a long fs_write.
                       "elapsed_s": round(time.time() - stream_t0, 1),
                       "round": round_id,
                       "path_hint": str(ev.data.get("first_name") or "")[:120]}
            elif ev.type == "usage":
                # `data` carries turn-level extras alongside the provider's numbers:
                # n_ctx for the context window (llama-server reports tokens but not its
                # slot size, so the client needs it from us). One event per LOOP — a
                # multi-tool turn streams several rounds, and the route keeps the last.
                turn_usage = _merge_usage(ev.data or {}, turn_usage)
                yield {"type": "usage", "data": _with_context_size(turn_usage, settings)}
            elif ev.type == "error":
                # A 400/500 that smells like a vision rejection, on a round that actually
                # sent image parts: recover instead of reporting a dead end. Only when this
                # round carried images — otherwise it is an unrelated server error and the
                # user needs to see it verbatim.
                if (_looks_like_vision_reject(ev.text) and _sent_images_this_round(messages)
                        and not _strip_images_for.get(conv_id)):
                    _strip_images_for[conv_id] = True
                    append_message(conv_id, "system", _VISION_NOTICE)
                    yield {"type": "notice", "text": _VISION_NOTICE}
                    continue
                yield {"type": "error", "text": ev.text}
            elif ev.type == "tool_request" and ev.tool is not None:
                tool_requests.append(ev.tool)
                gen_chars += len(getattr(ev.tool, "args_raw", "") or "")
            elif ev.type == "done":
                break

        text = "".join(assistant_text_parts)
        reasoning_text = "".join(reasoning_parts).strip()

        if not tool_requests:
            if text or reasoning_text:
                # Reasoning rides on meta, never in `content`: replaying it inside the answer
                # would teach the model to emit its thinking as prose. A row with ONLY
                # reasoning is still stored (it is real output and the UI shows it) even
                # though `text` is empty.
                append_message(conv_id, "assistant", text,
                               meta=json.dumps({"reasoning": reasoning_text})
                               if reasoning_text else "")
            # End of turn: forget a vision rejection taken during it (see _strip_images_for).
            # The next turn re-tries images, because the operator may well have loaded a
            # proper mmproj and restarted the server in between.
            _strip_images_for.pop(conv_id, None)
            yield {"type": "done"}
            return

        # A round that ends in tool calls is still billed. Prefer the provider's own
        # numbers: with `stream_options.include_usage` llama-server reports usage on its
        # final chunk, which we now read even though a tool request ended the stream
        # (see LlamaServerProvider._exchange). Only when the provider reported no prompt
        # size at all do we synthesise one from prompt estimate + generated characters.
        # Previously this branch discarded every measured figure in favour of the
        # estimate, so tool-heavy turns — exactly the expensive ones — never moved the
        # context bar with real numbers.
        if turn_usage is None or not turn_usage.get("measured"):
            est = _estimate_usage(prompt_tokens_est, gen_chars, stream_t0)
            if turn_usage:
                # Keep measured prompt_tokens/completion; borrow only timing from the
                # estimate when the provider gave none.
                est.update({k: v for k, v in turn_usage.items()
                            if v is not None and k != "estimated"})
                est["measured"] = bool(turn_usage.get("measured"))
            turn_usage = est
            yield {"type": "usage", "data": _with_context_size(turn_usage, settings)}

        # We have tool requests. Ensure each has a call id, then record the
        # assistant message with tool_calls meta (OpenAI protocol).
        for i, t in enumerate(tool_requests):
            if not t.call_id:
                t.call_id = f"call_{loop}_{i}"
        # Tell the model what it already did this turn (see the repeat guard below):
        # re-issuing an identical call is the model losing track of its own history,
        # and an explicit note in the arguments it gets back is far cheaper than
        # burning another step. The marker never reaches the user — tool_calls are not
        # rendered as text — and is stripped from anything we parse from history.
        notes = {t.call_id: _DONE_NOTE.format(done="; ".join(list(calls_done.keys())[-12:]))
                 for t in tool_requests if calls_done}
        tc_meta = json.dumps({"tool_calls": [
            {"id": t.call_id, "type": "function",
             "function": {"name": t.name,
                          "arguments": json.dumps(t.args) + notes.get(t.call_id, "")}}
            for t in tool_requests]})
        # Reasoning rides alongside the tool protocol on the same row (a reasoning model that
        # calls a tool still thought first). Merged rather than written twice so one row keeps
        # ONE meta object — see _tool_row_meta's consumers, which parse meta wholesale.
        if reasoning_text:
            try:
                _m = json.loads(tc_meta)
            except Exception:  # noqa: BLE001 — tc_meta is built above, so this is paranoia
                _m = {}
            _m["reasoning"] = reasoning_text
            tc_meta = json.dumps(_m)
        append_message(conv_id, "assistant", text, meta=tc_meta)

        # Partition: approval-gated vs auto (per settings, falling back to Tool.gated).
        gated = [t for t in tool_requests
                 if TOOLS_BY_NAME.get(t.name) and approval_mode(TOOLS_BY_NAME[t.name], settings) == "ask"]
        auto = [t for t in tool_requests if t not in gated]

        # Announce ALL calls up front — including the gated ones that are about to
        # interrupt for approval. Otherwise a gated call (exec, fs_delete) looks
        # exactly like a hang: the model asked for something and nothing appears.
        steps_used += 1
        for t in tool_requests:
            yield {"type": "tool_call", "name": t.name, "args": _redact(t.args),
                   "summary": _args_summary(t), "step": steps_used, "of": max_steps,
                   "round": round_id,
                   "awaiting_approval": t in gated}

        # Execute auto tools now, appending results (with matching tool_call_id).
        for t in auto:
            if cancel is not None and cancel.cancelled:
                raise TurnAborted()
            # Repeat guard. A small model that gets an answer it does not like often
            # re-issues the *identical* call, which previously meant re-running the tool
            # (re-appending, re-editing, re-zipping) once per round until the step budget
            # ran out — the read/edit/read/edit loop in the operator's transcript. The
            # model is also told what it already did (the _DONE_NOTE appended to its own
            # tool_calls), so a second identical request means it is stuck: do NOT execute
            # it again, answer from the recorded result and say so explicitly.
            key = _call_key(t)
            if key in calls_done:
                content = (calls_done[key] +
                           "\n\n[already done — do not repeat this call. Change your "
                           "arguments (for fs_edit: re-read the file and quote a shorter "
                           "exact fragment), use a different tool, or answer the user.]")
                append_message(conv_id, "tool", content,
                               meta=json.dumps({"tool": t.name, "tool_call_id": t.call_id}))
                audit.log(user_id, "tool_repeat_suppressed",
                          {"tool": t.name, "conv": conv_id})
                yield {"type": "tool_result", "name": t.name, "ok": True,
                       "preview": ui_preview(content), "meta": {},
                       "step": steps_used, "of": max_steps, "round": round_id}
                continue
            result = None
            async for _item in _run_tool_streaming(t, ctx):
                # Progress events are dicts; the ToolResult arrives as the last item.
                if isinstance(_item, dict):
                    yield _item
                else:
                    result = _item
            calls_done[key] = result.content
            append_message(conv_id, "tool", result.content,
                           meta=json.dumps(_tool_row_meta(t.name, result, t.call_id)))
            yield {"type": "tool_result", "name": t.name, "ok": result.ok,
                   "preview": ui_preview(result.content), "meta": _ui_meta(t.name, result),
                   # What the model ACTUALLY generated for this call, raw. Only present when
                   # its JSON never parsed — the one case where "what did it try to write?"
                   # has no other answer, and the difference between a token cap and a bad
                   # escape is only visible in these bytes. Bounded (see _raw_args_tail).
                   "raw_args": (_raw_args_tail(t) if not result.ok and
                                 (result.meta or {}).get("malformed") else ""),
                   "step": steps_used, "of": max_steps, "round": round_id}

        # If any gated tools requested -> persist (with call id) + stop.
        if gated:
            for t in gated:
                pid = _persist_pending(conv_id, user_id, t)
                yield {"type": "action_required", "pending_id": pid, "kind": t.name,
                       "args": _redact(t.args),
                       "prompt": _gate_prompt(t)}
            yield {"type": "done", "awaiting": True}
            return

        # otherwise loop again so the model can use auto tool results
    yield {"type": "error",
           "text": f"stopped after {max_steps} tool step{'s' if max_steps != 1 else ''} "
                   f"(Admin → Agent → Max tool steps)"}


async def _resume_pending(pending, ctx, provider, settings, download_store,
                          gen: dict | None = None, cancel=None) -> AsyncIterator[dict]:
    """pending: dict with id/kind/tool_name/payload/call_id/conversation_id/user_id.

    NB: an approved gated call is NOT counted against the turn's tool-step budget —
    the user explicitly authorised it, so `agent.max_tool_steps` must never strand it
    mid-execution. Only the continuing loop below is bounded.

    What the approval covers, though, is what was SHOWN at the moment it was asked for.
    A pending card can sit for hours — an unread browser tab, a phone locked overnight —
    and in that time `sync_apply`'s preview, an `exec` against files since edited, or a
    `fs_delete` of a file recreated under the same name all stop meaning what they said.
    The codebase already refuses exactly this hazard elsewhere (project_apply: "approval
    of an older preview cannot slip a stale overwrite past a [change]"), so resuming an
    approval past agent.pending_max_age_s now says so and re-asks rather than executing
    against a workspace that has moved on (AUDIT #4). Denials always still resolve.
    """
    conv_id = pending["conversation_id"]
    tool = TOOLS_BY_NAME.get(pending["tool_name"])
    args = json.loads(pending.get("payload", "{}"))
    call_id = pending.get("call_id") or "call_resume"

    if pending.get("approved") and _pending_is_stale(pending, settings):
        age = _pending_age_s(pending)
        audit.log(pending.get("user_id") or "", "pending_approval_expired",
                  {"tool": pending.get("tool_name"), "age_s": age, "conv": conv_id})
        append_message(conv_id, "tool",
                       f"Not run: this approval is {int((age or 0) // 60)} minute(s) old and the "
                       f"workspace may have changed since it was shown. Ask {tool.name} again to "
                       f"get a fresh preview.",
                       meta=json.dumps({"tool": pending.get("tool_name", "tool"),
                                        "tool_call_id": call_id}))
        yield {"type": "tool_result", "name": pending.get("tool_name", "tool"), "ok": False,
               "preview": "approval expired — not executed", "meta": {}}
        return

    if pending["kind"] == "ask_user":
        # answer already in args["answer"] set at resolve time
        result = None
        async for _item in _run_tool_streaming(_mk_request(tool, args, call_id), ctx):
            if isinstance(_item, dict):
                yield _item
            else:
                result = _item
        append_message(conv_id, "tool", result.content,
                       meta=json.dumps({"tool": "ask_user", "tool_call_id": call_id}))
        yield {"type": "tool_result", "name": "ask_user", "ok": True,
               "preview": ui_preview(result.content), "meta": {}}
    else:  # any approval-gated tool (exec, fs_delete, device_screenshot, ...): approved/denied
        if not pending.get("approved"):
            append_message(conv_id, "tool", "user denied this action.",
                           meta=json.dumps({"tool": tool.name, "tool_call_id": call_id}))
            yield {"type": "tool_result", "name": tool.name, "ok": False,
                   "preview": "denied by user", "meta": {}}
        else:
            if cancel is not None and cancel.cancelled:
                raise TurnAborted()
            yield {"type": "tool_call", "name": tool.name, "args": _redact(args),
                   "summary": _args_summary(tool, args)}
            # Streamed like the auto path: an approval-gated describe_audio takes exactly as
            # long as an automatic one, so listening silently here would make the live output
            # depend on an admin's approval setting (DEC-061).
            result = None
            async for _item in _run_tool_streaming(_mk_request(tool, args, call_id), ctx):
                if isinstance(_item, dict):
                    yield _item
                else:
                    result = _item
            append_message(conv_id, "tool", result.content,
                           meta=json.dumps(_tool_row_meta(tool.name, result, call_id)))
            yield {"type": "tool_result", "name": tool.name, "ok": result.ok,
                   "preview": ui_preview(result.content), "meta": _ui_meta(tool.name, result),
                   # No `round` on the events above: finishProgress(name) without one
                   # retires any chip for that tool, which is right for a resumed call.
                   }

    # continue the agent loop so the model reacts to the tool result
    # The continuation uses an offset well past any budget the previous turn could have
    # consumed (max_tool_steps tops out at 50), so its round ids cannot collide with
    # rounds whose chips were already retired by the approve/deny card.
    async for ev in _agent_loop(ctx.user_id, conv_id, provider, settings, ctx, gen, cancel,
                                round_offset=1000):
        yield ev


def _mk_request(tool, args, call_id=""):
    from .llm.base import ToolRequest
    return ToolRequest(name=tool.name, args=args, call_id=call_id)


# Structured UI info the web tools produce (search results / fetched page). The
# chat UI renders these as clickable source links. They are deliberately NOT
# sent to the model — the tool's `content` already carries everything it needs.
_WEB_META_KEYS = ("results", "url", "backend", "engine", "query", "links")


def _web_meta(result) -> dict:
    """Extract the web-tool UI meta from a ToolResult, or {} for non-web results."""
    m = getattr(result, "meta", None) or {}
    out = {k: m[k] for k in _WEB_META_KEYS if m.get(k)}
    return out


# Cap on raw tool-call arguments shipped to the browser for a failed call. Large enough to
# see where a long write broke (the interesting part is always the END), small enough that one
# malformed call cannot put megabytes into an SSE frame or the debug panel.
RAW_TAIL_CHARS = 6000


def _raw_args_tail(treq) -> str:
    """The raw generated argument string for a failed call, tail-bounded.

    Only ever sent alongside ok=False on a malformed call. The head of a truncated blob is
    almost pure boilerplate (`{"path": "...", "content": "<!DOCTYPE html>...`); the break is at
    the end, so we keep the tail and label what was dropped rather than silently clipping.
    """
    raw = str(getattr(treq, "args_raw", "") or "")
    if len(raw) <= RAW_TAIL_CHARS:
        return raw
    return (f"[first {len(raw) - RAW_TAIL_CHARS} of {len(raw)} characters omitted — "
            f"showing the tail where it broke]\n" + raw[-RAW_TAIL_CHARS:])


def _tool_row_meta(tool_name: str, result, call_id: str = "") -> dict:
    """The `meta` JSON stored on a persisted tool message row.

    Carries the OpenAI pairing fields plus — for web tools — the structured
    source info under key "web". Persisting it (instead of dropping the meta as
    before) is what lets the chat UI re-render source links when the
    conversation is reloaded, not just while it streams live.

    Media tools add "images": jail-relative image files the model must SEE. They are
    stored here (paths, never base64 — the DB row stays small and encryption at rest
    still applies to the files themselves) and turned into a multimodal message by
    `build_messages` on every replay. See DEC-052 for why they ride on a user-role
    message rather than the tool message.
    """
    meta = {"tool": tool_name}
    if call_id:
        meta["tool_call_id"] = call_id
    wm = _web_meta(result)
    if wm:
        meta["web"] = wm
    imgs = getattr(result, "images", None) or []
    if imgs:
        meta["images"] = [{"path": str(i.get("path", "")), "mime": str(i.get("mime", "") or "")}
                          for i in imgs if isinstance(i, dict) and i.get("path")]
    # display_media puts files on the USER's screen. Kept under its own key rather than
    # folded into "images" because that key is read by build_messages and encoded as
    # image_url data URLs: a video or audio file listed there would be base64'd into every
    # later prompt and handed to an IMAGE projector, costing tens of thousands of tokens to
    # say nothing the model could perceive. "display" is UI-only; replay picks out from it
    # the subset that is genuinely an image (see build_messages).
    media = (getattr(result, "meta", None) or {}).get("media")
    if isinstance(media, list) and media:
        meta["display"] = [{"path": str(m.get("path", "")), "kind": str(m.get("kind", "") or ""),
                            "url": str(m.get("url", "") or ""),
                            "artifact_id": str(m.get("artifact_id", "") or ""),
                            "name": str(m.get("name", "") or "")}
                           for m in media if isinstance(m, dict) and m.get("path")]
        cap = str((getattr(result, "meta", None) or {}).get("caption") or "").strip()
        if cap:
            meta["display_caption"] = cap[:300]
    dl = _download_meta(result)
    if dl:
        # Persist the artifact IDENTITY, not just a filename. History replay used to
        # regex-scrape the name out of the tool message prose and look it up first-wins by
        # filename, so two artifacts sharing a name gave the second one the first's link,
        # and renaming a file lost its download button outright (AUDIT #10). With the id on
        # the row, replay resolves exactly what the live stream offered.
        meta["download"] = dl
    return meta


def _download_meta(result) -> dict:
    """The `download` block of a tool result, if it offers one ({name,url,artifact_id})."""
    dl = (getattr(result, "meta", None) or {}).get("download")
    return dict(dl) if isinstance(dl, dict) and dl.get("url") else {}


def _ui_meta(tool_name: str, result) -> dict:
    """The `meta` dict on the live SSE tool_result event.

    Same web info under "web" as the persisted row (so live and reloaded views
    render identically), plus everything else the tool returned (download links).

    `media` is copied through explicitly for symmetry with the persisted "display" key:
    display_media's files must draw the same bubble while streaming and after a reload,
    exactly as web sources do. It stays off the model's request graph either way — this
    dict never reaches build_messages.
    """
    out = dict(result.meta or {})
    wm = _web_meta(result)
    if wm:
        out["web"] = wm
    # Media from display_media only. NOT `result.images` — that field is the model's pixel
    # channel (pdf_read pages, view_video frames) and those tools' entries carry no `kind`,
    # so the guard below keeps them out: they are rendered for the model, not put on screen
    # for the user. Reading both would draw a bubble per PDF page behind every pdf_read.
    media = (result.meta or {}).get("media") or []
    if isinstance(media, list) and any(isinstance(m, dict) and m.get("kind") for m in media):
        out["media"] = [m for m in media if isinstance(m, dict) and m.get("path")]
    return out


# Appended to the `arguments` string of a tool call we are replaying to the model, so
# its own history states what has already been done this turn. Kept as a trailing
# marker (not inside the JSON) because arguments is an opaque string on the wire; see
# _strip_done_note, which removes it again wherever those arguments are parsed.
_DONE_MARK = "[NOTE: you have ALREADY completed"
_DONE_NOTE = "\n\n" + _DONE_MARK + " these calls this turn — {done}]"


# ── Vision recovery (see the check in _agent_loop) ───────────────────────────
# Conversations whose images must be stripped from the request graph. IN-MEMORY and
# per-turn-cleared: it is a reaction to what THIS server build rejected, not a fact about
# the user's files. Keys are removed at the end of the turn that set them so a later turn —
# possibly against a restarted server with a real mmproj — tries images again.
_strip_images_for: dict[str, bool] = {}

_VISION_NOTICE = (
    "[System notice] The model server rejected the image attachment(s) in this conversation: "
    "the currently loaded model has no vision projector (mmproj), so it cannot see images. "
    "The image content has been removed from context so the conversation can continue. Tell "
    "the user their model needs a vision-capable GGUF plus a matching mmproj file (Admin → "
    "Model → Vision projector path) to read images, and continue without relying on image "
    "content — offer fs_read for text files instead."
)

# What counts as a vision rejection. Deliberately narrow: an HTTP status plus either the
# word for the modality or for the content-part shape, so an unrelated 500 (OOM, context
# overflow) is NOT mistaken for "no vision" and silently answered with stripped images —
# which would hide a real fault and lose the user's picture for nothing.
_VISION_REJECT_RE = re.compile(
    r"(?:400|500).{0,160}?(?:image|vision|mmproj|multimodal)"
    r"|(?:image|vision|mmproj|multimodal).{0,160}?(?:400|500)",
    re.IGNORECASE | re.DOTALL)


def _looks_like_vision_reject(text: str) -> bool:
    """Does this provider error read as 'this model cannot accept images'."""
    return bool(_VISION_REJECT_RE.search(text or ""))


def _sent_images_this_round(messages: list[dict]) -> bool:
    """Did the request we just sent actually carry image parts?

    Recovery is only justified when it did. Without this check any 400/500 in a chat that
    happens to contain an old attachment would flip the conversation into image-blind mode."""
    return any(isinstance(m.get("content"), list)
               and any(isinstance(p, dict) and p.get("type") == "image_url"
                       for p in (m.get("content") or []))
               for m in messages)


def _strip_image_parts(messages: list[dict]) -> list[dict]:
    """Return the message graph with image parts replaced by a text marker.

    Text parts are kept in order; an image becomes one short line so the model still knows
    something was there (and why it cannot use it) rather than silently losing a turn's
    content. Rows that carry ONLY an image keep their role and gain the marker as content —
    an empty content string is what makes some templates drop the message and orphan the
    surrounding tool-call pairing."""
    out: list[dict] = []
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        parts: list[dict] = []
        dropped = 0
        for p in c:
            if isinstance(p, dict) and p.get("type") == "image_url":
                dropped += 1
                continue
            parts.append(p)
        if not dropped:
            out.append(m)
            continue
        marker = {"type": "text",
                  "text": f"[{dropped} image(s) omitted — this model has no vision projector]"}
        # Keep a text part FIRST when there is one, so the message still opens as prose.
        parts.append(marker)
        new = dict(m)
        new["content"] = parts
        out.append(new)
    return out


def _call_key(t) -> str:
    """Identity of a tool call for repeat detection: name + canonical args."""
    try:
        return t.name + " " + json.dumps(t.args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — unserialisable args: fall back to repr
        return t.name + " " + repr(sorted((t.args or {}).items()))


def _strip_done_note(arguments: str) -> str:
    """Remove the harness note from a tool_call arguments string.

    Applied wherever stored `tool_calls` are consumed (the OpenAI message graph and
    the token estimate), so what the model receives is exactly what it asked for and
    our size estimates never count harness text as model output.
    """
    s = str(arguments or "")
    i = s.find(_DONE_MARK)
    return s[:i].rstrip() if i >= 0 else s


async def _run_tool(treq, ctx):
    tool = TOOLS_BY_NAME.get(treq.name)
    if tool is None:
        from .tools.base import ToolResult
        return ToolResult(False, f"unknown tool: {treq.name}")

    # ── Argument hardening (before ANY tool body runs) ──────────────────────────
    # Two failure classes used to reach the filesystem and fail there with a misleading
    # OS error, hiding the real cause from both the model and the operator:
    #   1. The provider could not parse the model's tool-call JSON (a truncated /
    #      malformed blob from an over-large write) and fell back to {_raw: ...}. Then
    #      `args.get("path","")` was "" and fs_write wrote *at the workspace directory*
    #      → "[Errno 21] Is a directory". The model saw that, concluded the WORKSPACE was
    #      broken, and thrashed probing it — chasing a phantom.
    #   2. A required field (path/command/…) was simply absent or blank.
    # Both are now refused here with a message naming the real problem and the fix, so no
    # tool ever runs on arguments it cannot honour. This is the one seam every auto AND
    # resumed-gated call passes through, so it covers all tools without touching each.
    from .tools.base import ToolResult as _TR, validate_required as _validate
    from .errors import hint as _hint
    from .llm.base import malformed_args as _is_malformed
    if _is_malformed(treq.args):
        # Record WHY and HOW MUCH before refusing. Without this the only trace of a
        # truncated write is one sentence in the transcript: no byte count, no finish
        # reason, no tail — so "the JSON broke" cannot be distinguished from "the model
        # emitted an unescaped quote", and the operator has nothing to act on. The raw
        # blob can contain arbitrary user-file content, so only a bounded TAIL (where the
        # break is) plus a size goes to the audit log; the full text stays in memory for
        # the debug bubble. See tools/base.diagnose_truncation().
        from .tools.base import diagnose_truncation as _diag
        raw = str(getattr(treq, "args_raw", "") or "")
        diag = _diag(raw)
        audit.log(ctx.user_id, "tool_args_malformed",
                  {"tool": treq.name, **{k: v for k, v in diag.items() if k != "tail"}})
        return _TR(False, _hint("malformed_args",
                                f"could not run {treq.name}: its arguments were not valid "
                                f"JSON, so no field (path, content, command, …) was read."
                                f"\nGenerated {diag['chars']} characters, which stopped "
                                f"{diag['stopped_at']}."
                                f"\nLast {len(diag['tail'])} characters received:\n{diag['tail']}"),
                   meta={"malformed": diag})
    missing = _validate(tool, treq.args)
    if missing:
        return _TR(False, missing)

    try:
        res = await tool.run(treq.args, ctx)
        if treq.name == "exec":
            audit.log(ctx.user_id, "exec", {"cmd": treq.args.get("command"), "ok": res.ok})
        # THE output-guard seam. Every auto tool result passes through here on its way to
        # becoming the tool message, so one call covers every present and future tool
        # without any tool having to co-operate. Guarding lower (inside each tool) would
        # leave new tools unguarded; guarding higher (in build_messages) would clip results
        # that were never the problem and re-clip on every replay. Gated tools resolve via
        # _resume_pending, which guards there too — both paths are covered deliberately.
        return _guard_result(res, ctx, treq.name)
    except Exception as e:  # noqa: BLE001
        # Last-resort net: a tool that raises instead of returning ToolResult(False, …).
        # The message leads with the exception TYPE so an OS-level error like
        # "[Errno 21] Is a directory" is never the whole story — it reads as a workspace
        # fault when it is really a bad call. Naming IsADirectoryError explicitly points at
        # the path argument, which is the actual fix for the reported confusion.
        from .tools.base import ToolResult as _TR2
        kind = type(e).__name__
        detail = str(e).strip() or kind
        if isinstance(e, IsADirectoryError):
            return _TR2(False, _hint(
                "missing_arg",
                f"the path this tool was given is a DIRECTORY, not a file ({detail}). A "
                f"required file path was missing or empty, so it defaulted to the workspace "
                f"root — pass an explicit file path."))
        return _TR2(False, f"tool error ({kind}): {detail[:200]}")


def _guard_result(result, ctx, tool_name: str):
    """Apply the Admin → Agent output-guard budget to one tool result.

    Returns the same object when nothing was clipped (the overwhelmingly common case — no
    copy, no cost). On a clip, an audit line records that the model did not see everything,
    so "why did it miss that?" has an answer in Admin → Audit.
    """
    try:
        from . import output_guard as og
        policy = og.policy_from_settings(ctx.settings())
        guarded, was = og.guard_tool_result(result, policy, tool_name)
        if was:
            audit.log(getattr(ctx, "user_id", ""), "tool_output_guarded",
                      {"tool": tool_name, "original": len(result.content or ""),
                       "cap": og.normalize_policy(policy)["max_chars"]})
        return guarded
    except Exception:  # noqa: BLE001 — a guard failure must never fail a tool call
        return result


def _pending_age_s(pending) -> float | None:
    """Seconds since the pending card was created, or None when it carries no stamp.

    `created_at` is whole-second epoch time (models.now()). Rows written before this
    check existed, or hand-built dicts from tests, may lack it — those are treated as
    NOT stale rather than deleting a user's approval on a missing field.
    """
    created = pending.get("created_at")
    try:
        return max(0.0, time.time() - float(created))
    except (TypeError, ValueError):
        return None


def _pending_is_stale(pending, settings) -> bool:
    """True when an approval is older than agent.pending_max_age_s (0 disables)."""
    try:
        limit = int(settings.get("agent.pending_max_age_s") or 0)
    except (TypeError, ValueError):
        return False
    if limit <= 0:
        return False
    age = _pending_age_s(pending)
    return age is not None and age > limit


def _persist_pending(conv_id, user_id, treq) -> str:
    with session_scope() as s:
        # PendingAction.conversation_id is a real FK, so a conversation deleted
        # mid-turn would raise here; surface it as ConversationGone like the other
        # write points instead of a 500.
        if s.get(Conversation, conv_id) is None:
            raise ConversationGone(conv_id)
        kind = "ask_user" if treq.name == "ask_user" else ("exec" if treq.name == "exec" else "tool")
        pa = PendingAction(conversation_id=conv_id, user_id=user_id,
                           kind=kind,
                           tool_name=treq.name, call_id=treq.call_id or "",
                           payload=json.dumps(treq.args))
        s.add(pa)
        s.flush()
        return pa.id


def _gate_prompt(treq) -> str:
    if treq.name == "exec":
        reason = treq.args.get("reason", "")
        return f"Run this command in your sandboxed workspace?\n$ {treq.args.get('command')}" + \
               (f"\n\nReason: {reason}" if reason else "")
    if treq.name == "ask_user":
        return treq.args.get("question", "A question:")
    tool = TOOLS_BY_NAME.get(treq.name)
    bits = ", ".join(f"{k}={str(v)[:80]}" for k, v in list((treq.args or {}).items())[:4])
    label = (tool.name if tool else treq.name)
    return f"Allow {label}?{' ' + bits if bits else ''}"


def _redact(args: dict) -> dict:
    # never surface secrets; exec/fs args are fine to show to the owning user
    return args


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Rough prompt size for the context bar when the provider reports no usage.

    ~4 chars/token plus a small per-message framing constant. Flagged as an estimate
    downstream (`context_usage_payload` labels `source:"estimate"`), so it is allowed to
    be wrong — but it must never crash on multimodal content parts, which is why image
    data URLs are counted as one part rather than their base64 length (a 5 MB image is
    not 1.25 M tokens).
    """
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c) // 4
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += len(str(part.get("text", ""))) // 4
                else:
                    total += 64   # an image/other part: a fixed, modest guess
        else:
            total += len(str(c or "")) // 4
        for tc in _clean_tool_calls(m.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            total += (len(str(fn.get("name", ""))) + len(str(fn.get("arguments", "")))) // 4
        total += _PROMPT_MSG_OVERHEAD_TOKENS
    return max(1, total)


def _estimate_usage(prompt_tokens: int, gen_chars: int, t0: float | None) -> dict:
    """Usage dict from character counts, used when the provider sent none.

    Mirrors `LlamaServerProvider._usage_event({})` (same ~4 chars/token heuristic and
    keys) with one addition: `estimated: True`. That flag matters because these numbers
    are NOT comparable to llama-server's real usage — tool-call JSON costs far more than
    4 chars/token, so a turn of fs_write calls looks artificially loaded here. The
    context bar treats any persisted prompt_tokens as measured, so this estimate is
    deliberately never merged into `turn_usage` (it would then be saved and mislabelled);
    it only ever reaches the live client, which ignores unknown keys.
    """
    dur = max(0.001, (time.time() - t0) if t0 else 0.001)
    ctok = max(1, gen_chars // 4) if gen_chars else 0
    return {"prompt_tokens": prompt_tokens or None,
            "eval_tokens": ctok or None,
            "completion_tokens": ctok or None,
            "tokens_per_sec": round(ctok / dur, 1) if ctok else None,
            "duration_s": round(dur, 2),
            "estimated": True}


def _merge_usage(new: dict, prior: dict | None) -> dict:
    """Combine this loop's usage with earlier loops of the same turn.

    `prompt_tokens` deliberately stays the LATEST value (that is what predicts the next
    send and what the context bar means), while completion/timing ACCUMULATE so a
    multi-tool turn reports its whole output instead of only the last round-trip.
    """
    if not prior:
        return dict(new or {})
    out = dict(new or {})
    for k in ("completion_tokens", "eval_tokens"):
        a, b = out.get(k), prior.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = a + b
    try:
        d1 = float(out.get("duration_s") or 0) + float(prior.get("duration_s") or 0)
    except (TypeError, ValueError):
        d1 = 0.0
    if d1:
        out["duration_s"] = round(d1, 2)
        ctok = out.get("completion_tokens") or 0
        if ctok:
            out["tokens_per_sec"] = round(ctok / d1, 1)
    out["estimated"] = bool(out.get("estimated")) or bool(prior.get("estimated"))
    # A measured figure outranks an estimate regardless of arrival order — the
    # provider's count is the only one that includes the system prompt, tool-call JSON
    # and tool results. Without this, a later estimated round could overwrite the real
    # prompt_tokens we already had.
    if prior.get("measured") and not out.get("measured"):
        for k in ("prompt_tokens", "completion_tokens"):
            if prior.get(k) is not None:
                out[k] = prior[k]
        out["estimated"] = bool(prior.get("estimated"))
    out["measured"] = bool(out.get("measured") or prior.get("measured"))
    # llama.cpp's measured rates are LATEST-WINS, never accumulated: they describe the speed
    # of a generation, not a quantity, and averaging across rounds would invent a number
    # that neither server nor user ever saw. A round that reported none (older build, or an
    # estimated round) must not erase one that did — same outranking rule as prompt_tokens.
    for k in ("prefill_tps", "decode_tps", "prompt_ms", "predicted_ms", "perf_source"):
        if out.get(k) is None and prior.get(k) is not None:
            out[k] = prior[k]
    return out


def _with_context_size(data: dict, settings: dict) -> dict:
    """Attach the configured context window so the UI can render a usage bar.

    llama-server's `usage` reports prompt_tokens but not the slot's n_ctx (that is
    a server-start property), and the stub estimate is heuristic — so the client
    always prefers the real value from /api/chat/context-usage.
    """
    out = dict(data or {})
    try:
        out["n_ctx"] = int(settings.get("model.n_ctx") or 0)
    except (TypeError, ValueError):
        pass
    return out


# Human-friendly "what is it doing right now" one-liners per tool. Long values
# (file contents!) are replaced by their size so the chip stays a status line
# rather than dumping the whole payload into the transcript.
_ARG_LABELS = {
    "path": "path", "from_path": "from", "to_path": "to", "pattern": "pattern",
    "glob": "glob", "command": "cmd", "url": "url", "query": "query",
    "zip_name": "zip", "question": "question", "reason": "reason",
}


def _args_summary(tool, args: dict | None = None) -> str:
    """A compact description of a tool call for the live activity chip."""
    args = args if args is not None else {}

    def clip(v, n=90):
        s = str(v).replace("\n", "\\n")
        return (s[: n - 1] + "…") if len(s) > n else s

    name = getattr(tool, "name", "") or ""
    if name in ("fs_write", "fs_append"):
        content = str(args.get("content", ""))
        lines = content.count("\n") + 1 if content else 0
        return (f"{clip(args.get('path', ''))} · {len(content)} chars"
                f"{f' · {lines} lines' if lines > 1 else ''}")
    if name == "fs_edit":
        return (f"{clip(args.get('path', ''))}: "
                f"{clip(args.get('old_text', ''), 40)} → {clip(args.get('new_text', ''), 40)}")
    if name == "zip_pack":
        paths = args.get("paths") or []
        return f"{clip(args.get('zip_name') or 'bundle.zip')} · {len(paths)} files"
    if name == "exec":
        return clip(args.get("command", ""))
    if name in ("web_search", "web_fetch"):
        return clip(args.get("query") or args.get("url") or "")
    bits = []
    for k, v in list(args.items())[:3]:
        if isinstance(v, (dict, list)):
            continue
        bits.append(f"{_ARG_LABELS.get(k, k)}={clip(v, 60)}")
    return " ".join(bits)
