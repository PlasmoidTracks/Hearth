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
import json
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


def _workspace_context(user_id: str) -> str:
    """A compact list of files in the user's workspace so the model knows what exists."""
    try:
        entries = workspaces.list_tree(user_id, ".", max_entries=80)
    except Exception:  # noqa: BLE001
        return ""
    if not entries:
        return "\n\n# Workspace files\n(your workspace is currently empty)"
    lines = []
    for e in entries:
        size = f" ({e['size']} B)" if (not e["is_dir"] and e.get("size") is not None) else ""
        lines.append(f"- {'[dir] ' if e['is_dir'] else ''}{e['name']}{size}")
    return ("\n\n# Workspace files (ls -l equivalent; read with fs_read, list a folder with fs_list)\n"
            "Files under uploads/ are ones the user attached to this chat.\n"
            + "\n".join(lines))


def _attachment_content(user_id: str, att_meta: list, text: str):
    """Build OpenAI multimodal `content` for a user message with attachments.

    Images become {type:image_url, image_url:{url:data:...;base64,...}} parts so a
    vision-capable model actually sees them (never raw bytes dumped as text). Other
    files are represented by the text we already stored. Returns a str if there are
    no images (plain content), else a list of content parts.
    """
    parts = [{"type": "text", "text": text}]
    has_image = False
    for att in att_meta:
        mime = att.get("mime") or ""
        if not mime.startswith("image/"):
            continue
        try:
            import base64
            b64 = base64.b64encode(
                workspaces.read_bytes(user_id, att["rel_path"])[:8 * 1024 * 1024]).decode()
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
            has_image = True
        except Exception:  # noqa: BLE001 — skip unreadable image, keep the text part
            continue

    return parts if has_image else text


def build_messages(conv_id: str, user_id: str = "") -> list[dict]:
    """Assemble OpenAI-style messages including a system prompt.

    Reconstructs the tool-calling protocol from stored history so a real
    llama-server sees valid assistant.tool_calls + tool.tool_call_id pairs:
      - assistant rows carry meta.tool_calls (with ids).
      - each following `tool` row carries meta.tool_call_id matching one call;
        we attach it to the most recent assistant message's tool_calls.
    User messages with image attachments are sent as multimodal content parts.
    A workspace file listing is appended to the system prompt so the model knows
    which files exist in this conversation's user space.
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
    if user_id:
        system += _workspace_context(user_id)
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
            if meta.get("tool_calls"):
                entry["tool_calls"] = _clean_tool_calls(meta["tool_calls"])
                last_assistant_with_calls = entry
            messages.append(entry)
        elif m["role"] == "tool":
            entry = {"role": "tool", "content": m["content"]}
            if meta.get("tool_call_id") and last_assistant_with_calls is not None:
                entry["tool_call_id"] = meta["tool_call_id"]
            messages.append(entry)
        elif m["role"] == "user" and meta.get("attachments") and user_id:
            content = _attachment_content(user_id, meta["attachments"], m["content"])
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
                   cancel: CancelHandle | None = None) -> AsyncIterator[dict]:
    """pending_resume: (PendingAction row data) if this turn continues a gated action.
    gen: per-turn generation overrides ({no_tools, max_tokens}).
    cancel: handle from cancel.begin(); None means "cannot be aborted".
    """
    ctx = ToolContext(user_id, settings, download_store, conversation_id=conv_id)
    gen = dict(gen or {})

    # If resolving a gated action, execute it now and append the tool result.
    try:
        if pending_resume is not None:
            async for ev in _resume_pending(pending_resume, ctx, provider, settings,
                                            download_store, gen, cancel):
                yield ev
            return

        async for ev in _agent_loop(user_id, conv_id, provider, settings, ctx, gen, cancel):
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


async def _stream_checked(provider, messages, specs, params, cancel):
    """Forward provider events, but bail out promptly if the turn is aborted.

    IDLE timeout, reset on every event: the guard fires only when the stream
    produced NOTHING for `stream_idle_timeout_s`. It used to wrap the whole
    completion in one `asyncio.timeout(deadline)`, which — despite the docstring —
    was a hard wall-clock cap per round, NOT an idle timer. A too-small
    HEARTH_STREAM_IDLE_TIMEOUT_S (or `.env` leftover) then killed every request
    at exactly N seconds regardless of progress: llama-server logged
    `stop: cancel task` 1.000 s after each launch and the UI showed
    "connection lost (ReadError)". Waiting per-event with asyncio.wait_for is a
    true idle timer AND works on Python 3.10, so the old 3.11 feature-detect
    (`asyncio.timeout`) is gone; `stream_idle_guard=false` still disables it.
    """
    from .config import get_boot_settings
    bs = get_boot_settings()
    idle_s = float(bs.stream_idle_timeout_s)
    use_timeout = bs.stream_idle_guard and idle_s > 0

    agen = provider.stream(messages, specs, params).__aiter__()

    async def _next():
        """One event from the provider, or StopAsyncIteration when it ends."""
        return await agen.__anext__()

    timed_out = False
    aborted = False
    try:
        while True:
            # Race three outcomes: the next provider event, an idle timeout, and a
            # user abort. The abort branch is what makes Stop take effect *now* —
            # previously cancellation was only observed after __anext__() returned,
            # so during a long prefill or tool call (no events) Stop appeared to do
            # nothing until the round finished, and llama-server kept decoding.
            next_task = asyncio.ensure_future(_next())
            waiters = [next_task]
            cancel_task = None
            if cancel is not None:
                cancel_task = asyncio.ensure_future(cancel.wait_cancelled())
                waiters.append(cancel_task)
            timeout = idle_s if use_timeout else None
            try:
                done, _pending = await asyncio.wait(waiters, timeout=timeout,
                                                    return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # Our own task was cancelled (client disconnect / shutdown): release
                # everything and propagate so the route tears down the stream.
                next_task.cancel()
                if cancel_task is not None:
                    cancel_task.cancel()
                raise

            if cancel_task is not None and cancel_task in done:
                aborted = True
                next_task.cancel()
                # Surface a provider error raised concurrently, if any, before aborting.
                if not next_task.cancelled():
                    try:
                        next_task.result()
                    except Exception:  # noqa: BLE001 — we are aborting anyway
                        pass
            elif next_task in done:
                try:
                    ev = next_task.result()
                except StopAsyncIteration:
                    break
                yield ev
            else:
                # Neither completed before the idle timeout.
                timed_out = True
                next_task.cancel()
                if cancel_task is not None:
                    cancel_task.cancel()
            # Defensive: if cancel got set by some other path mid-loop, stop now.
            if cancel is not None and cancel.cancelled:
                aborted = True
                break
    finally:
        # Release the provider generator on every exit path (abort, timeout, error).
        # aclose() throws GeneratorExit into it so llama_server.stream()'s `finally`
        # runs and the HTTP connection is returned/closed — which is what tells
        # llama-server to cancel the in-flight task instead of decoding to completion.
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001 — closing a dead stream is not our problem
            pass
    if aborted:
        raise TurnAborted()
    if timed_out:
        yield StreamEvent("error", text=f"model produced no output for {int(idle_s)}s — "
                                        f"stream timed out")
        yield StreamEvent("done", data={"finish": "timeout"})


async def _agent_loop(user_id, conv_id, provider, settings, ctx,
                      gen: dict | None = None, cancel=None, round_offset: int = 0) -> AsyncIterator[dict]:
    """Run model round-trips until the model stops asking for tools.

    `round_offset` shifts the ids used to correlate progress/pending chips so a resumed
    gated turn (`_resume_pending`) gets its own chip namespace instead of colliding with
    rounds retired before the approve/deny card appeared.
    """
    gen = gen or {}
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
    for loop in range(max_steps):
        if cancel is not None and cancel.cancelled:
            raise TurnAborted()
        # A turn can outlive its conversation (deleted while a tool ran, or a stale
        # client id). Stop before asking the model anything rather than failing on
        # the first write. Checked once per loop, not per token — cheap and enough.
        if not conversation_exists(conv_id):
            raise ConversationGone(conv_id)
        messages = build_messages(conv_id, user_id)
        params = {k.split(".", 1)[1]: v for k, v in settings.items() if k.startswith("model.")}
        if gen.get("max_tokens"):
            try:
                params["max_tokens"] = int(gen["max_tokens"])
            except (TypeError, ValueError):
                pass
        assistant_text_parts: list[str] = []
        tool_requests = []
        # Per-round bookkeeping for usage + progress chips. `round_id` is unique per
        # agent-loop iteration and lets the client tell apart chips belonging to
        # different rounds (a re-stream of the same tool must not reuse a dead chip).
        round_id = f"r{loop + round_offset}"
        gen_chars = 0                       # content chars + tool-argument chars
        prompt_tokens_est = _estimate_prompt_tokens(messages)
        stream_t0 = time.time()
        async for ev in _stream_checked(provider, messages, specs, params, cancel):
            if ev.type == "token":
                assistant_text_parts.append(ev.text)
                gen_chars += len(ev.text or "")
                yield {"type": "token", "text": ev.text}
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
                yield {"type": "error", "text": ev.text}
            elif ev.type == "tool_request" and ev.tool is not None:
                tool_requests.append(ev.tool)
                gen_chars += len(getattr(ev.tool, "args_raw", "") or "")
            elif ev.type == "done":
                break

        text = "".join(assistant_text_parts)

        if not tool_requests:
            if text:
                append_message(conv_id, "assistant", text)
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
                       "preview": content[:800], "meta": {},
                       "step": steps_used, "of": max_steps, "round": round_id}
                continue
            result = await _run_tool(t, ctx)
            calls_done[key] = result.content
            append_message(conv_id, "tool", result.content,
                           meta=json.dumps(_tool_row_meta(t.name, result, t.call_id)))
            yield {"type": "tool_result", "name": t.name, "ok": result.ok,
                   "preview": result.content[:800], "meta": _ui_meta(t.name, result),
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
    """
    conv_id = pending["conversation_id"]
    tool = TOOLS_BY_NAME.get(pending["tool_name"])
    args = json.loads(pending.get("payload", "{}"))
    call_id = pending.get("call_id") or "call_resume"

    if pending["kind"] == "ask_user":
        # answer already in args["answer"] set at resolve time
        result = await _run_tool(_mk_request(tool, args, call_id), ctx)
        append_message(conv_id, "tool", result.content,
                       meta=json.dumps({"tool": "ask_user", "tool_call_id": call_id}))
        yield {"type": "tool_result", "name": "ask_user", "ok": True,
               "preview": result.content[:800], "meta": {}}
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
            result = await _run_tool(_mk_request(tool, args, call_id), ctx)
            append_message(conv_id, "tool", result.content,
                           meta=json.dumps(_tool_row_meta(tool.name, result, call_id)))
            yield {"type": "tool_result", "name": tool.name, "ok": result.ok,
                   "preview": result.content[:800], "meta": _ui_meta(tool.name, result),
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


def _tool_row_meta(tool_name: str, result, call_id: str = "") -> dict:
    """The `meta` JSON stored on a persisted tool message row.

    Carries the OpenAI pairing fields plus — for web tools — the structured
    source info under key "web". Persisting it (instead of dropping the meta as
    before) is what lets the chat UI re-render source links when the
    conversation is reloaded, not just while it streams live.
    """
    meta = {"tool": tool_name}
    if call_id:
        meta["tool_call_id"] = call_id
    wm = _web_meta(result)
    if wm:
        meta["web"] = wm
    return meta


def _ui_meta(tool_name: str, result) -> dict:
    """The `meta` dict on the live SSE tool_result event.

    Same web info under "web" as the persisted row (so live and reloaded views
    render identically), plus everything else the tool returned (download links).
    """
    out = dict(result.meta or {})
    wm = _web_meta(result)
    if wm:
        out["web"] = wm
    return out


# Appended to the `arguments` string of a tool call we are replaying to the model, so
# its own history states what has already been done this turn. Kept as a trailing
# marker (not inside the JSON) because arguments is an opaque string on the wire; see
# _strip_done_note, which removes it again wherever those arguments are parsed.
_DONE_MARK = "[NOTE: you have ALREADY completed"
_DONE_NOTE = "\n\n" + _DONE_MARK + " these calls this turn — {done}]"


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
    try:
        res = await tool.run(treq.args, ctx)
        if treq.name == "exec":
            audit.log(ctx.user_id, "exec", {"cmd": treq.args.get("command"), "ok": res.ok})
        return res
    except Exception as e:  # noqa: BLE001
        from .tools.base import ToolResult
        return ToolResult(False, f"tool error: {str(e)[:200]}")


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
