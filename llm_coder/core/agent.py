"""
Agent orchestration: system prompt, message history, tool-calling loop,
patch staging under a configurable patch mode, context overflow handling
with explicit user choice, reasoning extraction, live tool-event reporting,
and activity state reporting for the UI (idle / waiting_for_model / tool_call / etc).
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable

from .llm_client import LLMClient, is_prefill_rejection
from .tools import ToolExecutor, PendingPatch, WRITE_TOOL_NAMES, CONTROL_TOOL_NAMES, EndTurnSignal, COMPUTER_TOOL_NAMES
from .context_utils import (
    split_reasoning, estimate_tokens, prune_stale_file_reads,
    compact_delete_tool_returns,
)
from .draft_store import DraftStore
from .workspace import WorkspaceError
from . import web_tools
from . import computer_tools
from .agent_prompts import DEFAULT_SYSTEM_PROMPT, PATCH_MODES
from .agent_policy import AgentPolicy
from .agent_messages import (
    flatten_content as _flatten_content,
    ends_turn as _ends_turn_msg,
    is_injected_user_message as _is_injected_user_msg,
    merge_continuation as _merge_continuation_text,
    has_orphaned_tool_result as _has_orphaned_tool_result,
)
from .subagent import SubagentRunner


@dataclass
class AgentTurnResult:
    assistant_text: str = ""
    reasoning_text: str = ""
    tool_log: List[str] = field(default_factory=list)
    pending_patches: List[PendingPatch] = field(default_factory=list)
    error: Optional[str] = None
    cancelled: bool = False
    overflow_triggered: bool = False
    overflow_choice_needed: Optional[dict] = None
    ended: bool = False
    turn_generated_tokens: int = 0
    ask_user_request: Optional[dict] = None
    script_approval_request: Optional[dict] = None
    command_approval_request: Optional[dict] = None
    web_fetch_truncation: Optional[dict] = None  # {url, truncated_reasons}
    web_browser_kill_request: Optional[dict] = None  # {browser_type: "playwright"|"selenium"}
    # Timing info from the model response
    prompt_per_second: Optional[float] = None
    predicted_per_second: Optional[float] = None


class Agent:
    def __init__(self, client: LLMClient, executor: ToolExecutor, context_docs: str = "",
                 policy: AgentPolicy = None, context_limit: int = 8192,
                 activity_callback: Callable[[str], None] = None,
                 tool_event_callback: Callable[[str, str, dict], None] = None,
                 draft_store: DraftStore = None,
                 subagent_client_provider: Optional[Callable[[], LLMClient]] = None,
                 subagent_policy_dict: Optional[dict] = None):
        self.client = client
        self.executor = executor
        # Give the executor a handle back to this agent so the Meta tools
        # (get_context_budget, uptime) can read live context/turn-timer state
        # from execute_read_tool. See ToolExecutor.attach_agent.
        self.executor.attach_agent(self)
        self.draft_store = draft_store
        # deploy_subagent plumbing. subagent_client_provider is a callable the UI
        # injects that returns a live LLMClient pointed at the subagent model
        # server, or raises RuntimeError with a user-actionable message when no
        # subagent model is configured/enabled/running. Resolving it lazily (per
        # deployment, not per turn) means the endpoint is only probed when the
        # model actually asks for a worker. subagent_policy_dict carries the
        # persisted subagent knobs (max_rounds, streaming, thinking) so a
        # deployment honours them; kept as a raw dict so main_window can hand it
        # straight over from QSettings without a dataclass round-trip here.
        self.subagent_client_provider = subagent_client_provider
        self.subagent_policy_dict = dict(subagent_policy_dict or {})
        # Populated by _deploy_subagent on each deployment and drained by the
        # tool-dispatch loop into the per-turn tool log. Reset at the start of a
        # deployment so a previous worker's trace can never leak into a later one.
        self._last_subagent_tool_log: List[str] = []
        self.policy = policy or AgentPolicy()
        self.context_limit = context_limit
        self.context_docs = context_docs
        self.messages: List[Dict[str, Any]] = []
        self._cancel_requested = False
        self._pending_batch: List[PendingPatch] = []
        self._auto_applied_patches: List[PendingPatch] = []
        self.activity_callback = activity_callback or (lambda state: None)
        self.tool_event_callback = tool_event_callback or (lambda phase, name, payload: None)
        self.total_generated_tokens = 0
        self.last_known_real_tokens: Optional[int] = None
        # Subagent context metering (see subagent_context_limit /
        # current_subagent_context_tokens). `last_context_tokens` is the peak
        # window occupancy of the most recent deployment — live pings from the
        # runner plus the runner's final figure; `last_context_limit` is the
        # window its server was started with (0 = unknown). `deployments` counts
        # how many times a worker ran this session, so the UI can say whether the
        # figure on screen is the current deployment or the last one.
        self.subagent_last_context_tokens = 0
        self.subagent_last_context_limit = 0
        self.subagent_deployments = 0
        self._subagent_deploy_active = False
        self._last_activity_state = "idle"
        self._response_round: int = 0
        # Vision: set by the UI from the vision policy. When no mmproj projector is
        # loaded, view_image falls back to a local image-to-text model if one is
        # configured (see core.caption_fallback), otherwise images are attached as
        # usual and the server-rejection path below cleans up after the failure.
        self.vision_available = True
        self.captioner = None
        self._last_viewed_media: Optional[tuple] = None  # (path, is_video)
        self._web_fetch_truncation: Optional[dict] = None
        # Time-based auto-injection tracking
        self._turn_start_time: Optional[float] = None  # wall-clock time when current turn started
        self._last_injected_barrier_index: int = -1   # highest barrier index that has been injected this turn
        self._turn_complete: bool = False  # set to True when end_turn is called; prevents auto-reentry
        # Set by prepare_continue(): {"index": int, "prefix": str} pointing at the
        # assistant message whose text the model is being asked to CONTINUE (as
        # opposed to Retry/Respond, which ask it to answer again). Consumed and
        # cleared by the next step(). See continuation_target().
        self._continuation: Optional[Dict[str, Any]] = None
        self.reset(context_docs=context_docs)

    def _set_activity(self, state: str):
        # Remembered for the Context tab's subagent meter, which polls the agent
        # from the UI thread while a deployment blocks this one (see
        # ui/context_tab.py::_subagent_running).
        self._last_activity_state = state
        try:
            self.activity_callback(state)
        except Exception:
            pass

    def _emit_tool_event(self, phase: str, name: str, payload: dict):
        try:
            self.tool_event_callback(phase, name, payload or {})
        except Exception:
            pass

    def _compose_system_message(self) -> Dict[str, Any]:
        """Build the system message from the CURRENT policy system prompt plus
        any project context documents.

        Single source of truth for the leading system message, shared by
        reset() and refresh_system_prompt(). The previous setup read
        policy.system_prompt in exactly one place (reset()), which is why a
        Settings save that changed the prompt had no effect on a live
        conversation — only starting a new session rebuilt message[0]."""
        system_content = self.policy.system_prompt
        if (self.context_docs or "").strip():
            system_content += "\n\n--- Project context documents ---\n" + self.context_docs
        return {"role": "system", "content": system_content}

    def refresh_system_prompt(self) -> bool:
        """Rebuild the leading system message from the current policy without
        touching the conversation itself.

        Call this whenever the system prompt (or the project context docs it is
        composed with) changes mid-session, so the very next request goes out
        with the new prompt instead of the one that happened to be active when
        the session started.

        Returns True when the stored system message actually changed, so
        callers can decide whether to report/autosave.
        """
        new = self._compose_system_message()
        if self.messages and self.messages[0].get("role") == "system":
            if self.messages[0].get("content") == new["content"]:
                return False
            self.messages[0] = new
        else:
            # History has no leading system message (legacy or hand-edited
            # import) — insert the current one rather than silently keeping
            # the model on a prompt-less context.
            self.messages.insert(0, new)
        # The old count was measured against the previous prompt. Dropping it
        # lets the context meter fall back to the char heuristic until the
        # server reports again, instead of sticking at a stale high-water mark.
        self.last_known_real_tokens = None
        return True

    def reset(self, context_docs: str = ""):
        """Reset message history for a new session while preserving policy."""
        self.context_docs = context_docs
        self.messages = [self._compose_system_message()]
        self.last_known_real_tokens = None
        self._pending_batch = []
        self._last_viewed_media = None
        self._web_fetch_truncation = None
        self._continuation = None
        self._set_activity("idle")
        if self.draft_store:
            self.draft_store.clear_session_log()
        # Clear tool log from prior runs for fresh testing
        self._test_tool_log: List[str] = []

    def export_messages(self) -> List[Dict[str, Any]]:
        return self.messages

    def import_messages(self, messages: List[Dict[str, Any]]):
        if messages:
            self.messages = messages

    def rebuild_history(self, conversation: List[Dict[str, Any]]):
        """Replace the raw history with a freshly-rebuilt conversation.

        The leading system message (index 0) of the CURRENT history is kept as
        the authoritative prompt, then ``conversation`` — a coherent
        user/assistant sequence — becomes the body. Used to re-derive the
        model's context directly from the chat panel (the single source of truth
        for what has actually been said, including edits) so the two can never
        drift apart and a generation starts from the wrong message.

        Resets the real token count, since the context just changed.
        """
        sys_msg = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else None
        )
        new_messages: List[Dict[str, Any]] = []
        if sys_msg is not None:
            new_messages.append(sys_msg)
        new_messages.extend(conversation)
        self.messages = new_messages
        self.last_known_real_tokens = None

    def cancel(self):
        self._cancel_requested = True

    def prepare_regenerate(self) -> bool:
        """Trim the trailing agent-side message block so the model can be asked
        to respond again from the last user message.

        An "agent block" is the run of non-user messages at the END of
        ``self.messages`` (assistant content, plus any interleaved tool-call /
        tool-result messages that belong to that turn). This method removes
        that block and clears the ``_turn_complete`` guard so that the next
        ``step()`` call actually invokes the model instead of early-returning.

        It is the single reusable primitive behind both the UI's "Retry" button
        (the last chat message is an agent response -> remove its block and
        regenerate) and "Respond" (the last chat message is a user message ->
        nothing is removed, but the guard is cleared so the model answers the
        pending user message).

        Returns:
            True if there is a user message still present to respond to
            (i.e. the caller should proceed to launch a worker), or False if
            there is nothing to respond to (e.g. a brand-new conversation with
            only the system prompt).

        Notes:
            * Intentionally does NOT clear any staged drafts / pending patches
              from the draft store. A retry is meant to re-produce the agent's
              words; side-effects already staged for review are the user's to
              keep or discard, and we deliberately avoid re-running tools here.
            * Safe to call when the trailing block is empty (last message is
              already a user message): it simply clears the guard.
        """
        # Walk back over the trailing run of non-user messages and drop it.
        cut = len(self.messages)
        while cut > 0 and self.messages[cut - 1].get("role") != "user":
            cut -= 1
        if cut < len(self.messages):
            self.messages = self.messages[:cut]

        # Always allow the next step() to reach the model.
        self._turn_complete = False

        # There is something to respond to only if a user message exists
        # anywhere after the leading system prompt.
        return any(m.get("role") == "user" for m in self.messages)

    def prepare_respond(self) -> bool:
        """Arm a "Respond" turn: answer the pending user message again WITHOUT
        deleting anything from the history.

        This is deliberately NOT ``prepare_regenerate()``. Trimming the trailing
        agent block is exactly what Retry is for; the Respond button historically
        called it anyway, so pressing Respond after an agent reply silently
        discarded that reply from the model's context while its chat bubble
        stayed on screen — the two views then disagreed about what had been said.
        Responding should only lift the "turn already finished" guard and ask the
        model to speak again, leaving the record intact.

        Returns True when there is anything to answer (a user message, or an
        existing agent reply to add to), False for a bare system-prompt-only
        conversation.
        """
        self._turn_complete = False
        self._continuation = None
        return any(m.get("role") in ("user", "assistant") for m in self.messages)

    def continuation_target(self) -> Optional[tuple]:
        """Find the message the "Continue" button can extend.

        Returns ``(index, text)`` for the last assistant message that is a valid
        continuation prefix, or None when there is nothing to continue from.

        This agent ends nearly every turn by calling ``end_turn``, so the raw
        history almost always finishes with an assistant message carrying a tool
        call plus its result, and often with a time-barrier user message on top.
        Skipping just the trailing ``tool`` messages — the previous behaviour —
        still landed on the ``end_turn`` wrapper, whose ``tool_calls`` made this
        return None and greyed the button out in the single most common shape
        there is. So instead:

        * the trailing control block (``end_turn`` and the results of the calls
          it wraps) is skipped as a unit, and
        * the search then walks back to the most recent assistant message that
          carries plain prose, which is the message the user actually wants to
          keep writing.

        Only ``end_turn`` closes the block. A mid-task assistant message that
        carries tool calls is NOT a continuation point: its answer is unfinished
        and the calls after it are still live, so continuing that would splice
        prose into the middle of the tool protocol.
        """
        idx = len(self.messages) - 1
        while idx >= 0:
            msg = self.messages[idx]
            role = msg.get("role")
            if role == "assistant" and _ends_turn_msg(msg, CONTROL_TOOL_NAMES):
                idx = self._skip_control_block(idx)
                continue
            if role == "assistant":
                # Skip wrappers with no prose of their own (a pure tool call),
                # non-string content (multimodal has nothing to append to), and
                # blank answers.
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return idx, content
                idx -= 1
                continue
            idx -= 1
        return None

    def _skip_control_block(self, idx: int) -> int:
        """Step back over an ``end_turn`` message plus the tool results that
        belong to it (and any user notice injected after them).

        Returns the index to inspect next, or -1. The result walk stops at the
        first message that is neither a tool result nor an injected user notice,
        so a real user turn is never swallowed.
        """
        i = idx - 1
        while i >= 0:
            role = self.messages[i].get("role")
            if role == "tool":
                i -= 1
                continue
            if role == "user" and not self._is_user_turn(i):
                i -= 1
                continue
            break
        return i

    def _is_user_turn(self, idx: int) -> bool:
        """Whether the user message at ``idx`` opens a real user turn.

        See :func:`core.agent_messages.is_injected_user_message` for the marker
        list this delegates to.
        """
        return not _is_injected_user_msg(self.messages[idx])

    def continue_anchor_index(self) -> Optional[int]:
        """Index of the message continuation_target() would extend, or None.

        The UI uses this to locate the matching chat bubble so a continuation
        streams INTO the existing message instead of opening a second one.
        """
        target = self.continuation_target()
        return target[0] if target else None

    def prepare_continue(self, prefix_text: Optional[str] = None) -> Optional[str]:
        """Arm a "Continue" turn: keep the agent's last message and have the
        model carry on writing it, instead of answering again from scratch.

        This is what makes "put words into the agent's mouth" work: the user can
        edit the last assistant bubble (chat panel Edit), then Continue picks the
        edit up — either implicitly, because an in-context edit is synced back
        into agent.messages, or explicitly via ``prefix_text``, which the UI
        passes from the bubble it is continuing so what is displayed is exactly
        what the model is made to continue.

        Nothing is deleted: unlike prepare_regenerate(), the previous text stays
        as the first part of the very same message.

        Args:
            prefix_text: the text to continue from. When it differs from the
                stored message content, the stored message is rewritten to match
                (so the model's prefix and the user's screen cannot diverge).

        Returns the prefix the model will continue from, or None when the
        history has no continuable assistant message.
        """
        target = self.continuation_target()
        if target is None:
            return None
        idx, text = target
        if prefix_text is not None and prefix_text != text:
            self.messages[idx]["content"] = prefix_text
            # The old token count was measured against the old text.
            self.last_known_real_tokens = None
        prefix = self.messages[idx]["content"]
        if not prefix.strip():
            return None

        self._continuation = {"index": idx, "prefix": prefix}
        # A continuation is a brand-new generation: lift the guard so step()
        # reaches the model, and restart the turn clock / draft tracking.
        self._turn_complete = False
        self._start_turn_timer()
        self._touched_draft_paths = set()
        return prefix

    def clear_continuation(self) -> None:
        """Forget any armed continuation (used when a turn is abandoned)."""
        self._continuation = None

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    # Video containers are never injected raw (a whole .mp4 as one base64 blob
    # is unreadable to the model) — add_user_message routes them through the
    # same pipeline as view_image: frames sampled for vision, or a text note as
    # last resort.
    VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv",
                  ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".ogv",
                  ".vob", ".3gp", ".3g2", ".divx", ".f4v"}
    # Default number of frames sampled from a video (attached files and the
    # view_video tool alike, when max_frames is not given).
    MEDIA_MAX_FRAMES = 8

    def add_user_message(self, text: str, attachments: Optional[List[str]] = None):
        """Add a user message and reset the turn timer.

        Video attachments are decoded up front — sampled into frames (like
        view_image), never injected as raw binary — and the model is told via a
        system notice which pipeline handled each one."""
        self._turn_complete = False
        self._start_turn_timer()
        # Track which draft paths get touched THIS turn, so post-turn patch
        # regeneration only re-submits drafts that actually changed — see
        # _generate_draft_patches().
        self._touched_draft_paths: set = set()
        if not attachments:
            self.messages.append({"role": "user", "content": text})
            return

        import os
        import base64
        import mimetypes

        content_parts = []
        extra_text_notes = []
        if text:
            content_parts.append({"type": "text", "text": text})

        for path in attachments:
            ext = os.path.splitext(path)[1].lower()
            name = os.path.basename(path)
            try:
                if ext in self.IMAGE_EXTS:
                    with open(path, "rb") as f:
                        raw = f.read()
                    mime = mimetypes.guess_type(path)[0] or "image/png"
                    b64 = base64.b64encode(raw).decode("ascii")
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                elif ext in self.VIDEO_EXTS:
                    note = self._decode_media_attachment(path, content_parts)
                    if note:
                        extra_text_notes.append(note)
                else:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        file_text = f.read()
                    extra_text_notes.append(f"--- Attached file: {name} ---\n{file_text}")
            except Exception as e:
                extra_text_notes.append(f"--- Attached file: {name} (failed to read: {e}) ---")

        if extra_text_notes:
            note_text = "\n\n".join(extra_text_notes)
            if content_parts and content_parts[0].get("type") == "text":
                content_parts[0]["text"] += "\n\n" + note_text
            else:
                content_parts.insert(0, {"type": "text", "text": note_text})

        if not content_parts:
            content_parts = [{"type": "text", "text": text or ""}]

        self.messages.append({"role": "user", "content": content_parts})

    def _decode_media_attachment(self, path: str, content_parts: List[Dict[str, Any]]) -> str:
        """Decode an attached video file at send time, mirroring the view_image
        pipeline instead of injecting the raw binary as text.

        A video with vision available is sampled into evenly-spaced frames and
        embedded as image parts (exactly like view_image). Without vision, the
        captioner fallback describes sampled frames; failing that, the model is
        told the file was skipped.
        Returns the notice text describing what happened ("" if none)."""
        import os
        import base64
        from .workspace import WorkspaceError

        name = os.path.basename(path)

        # 1) Video + vision model: sample frames, exactly like view_image.
        if self.vision_available:
            try:
                # Same extractor the view_image tool uses. The workspace now
                # resolves absolute paths outside the project root directly
                # (via Workspace._resolve_media), so attachments from elsewhere
                # on disk are passed through unchanged.
                frames = self.executor.workspace.extract_video_frames(
                    path, max_frames=self.MEDIA_MAX_FRAMES)
                self._last_viewed_media = (str(Path(path).resolve()), True)
                parts = [{"type": "text", "text": (
                    f"[The user attached the video '{name}'. Like view_image, it was "
                    f"decoded into {len(frames)} evenly-spaced sampled frames, shown "
                    f"as images below.]")}]
                for f_bytes in frames:
                    parts.append({"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(f_bytes).decode("ascii")}})
                content_parts.extend(parts)
                return ""
            except Exception as e:
                return (f"--- Attached video: {name} (could not be decoded into frames: {e}) ---")

        # 2) No vision model: fall back to captioning sampled frames.
        if self.captioner is not None:
            try:
                frames = self.executor.workspace.extract_video_frames(
                    path, max_frames=self.MEDIA_MAX_FRAMES)
                self._last_viewed_media = (str(Path(path).resolve()), True)
                description = self._caption_media(name, frames, True)
                content_parts.append({"type": "text", "text": (
                    f"--- Attached video: {name} ---\n{description}")})
                return ""
            except Exception as e:
                return (f"--- Attached video: {name} (could not be decoded into frames: {e}) ---")

        # 3) Nothing available: tell the model (and the user) plainly.
        return (f"--- Attached video file: {name} was not decoded — no frame "
                f"extraction is available for it. ---")

    def current_context_tokens(self) -> int:
        total_chars = sum(len(_flatten_content(m.get("content"))) for m in self.messages)
        return max(1, total_chars // 4) if total_chars else 0

    def current_context_tokens_real(self) -> Optional[int]:
        """Returns the last known real token count reported by the server, if any."""
        return self.last_known_real_tokens

    # ---- subagent context metering -----------------------------------------

    def subagent_enabled(self) -> bool:
        """True when a subagent model is configured AND switched on — the same
        test the UI applies before offering deploy_subagent
        (ui/main_window.py: SubagentPolicy.is_configured()).

        Read straight from subagent_policy_dict rather than from the executor's
        `subagent_enabled` flag: the Context tab only ever gets a handle on the
        Agent. SubagentPolicy is imported lazily so this adds no import edge for
        callers that never touch the feature.

        Never raises — the meter must not be the thing that breaks a turn."""
        try:
            from .subagent_server import SubagentPolicy
            sp = self.subagent_policy_dict or {}
            return SubagentPolicy.from_dict(sp).is_configured()
        except Exception:
            # Fallback for any import/parse surprise: the two conditions
            # is_configured() itself checks (feature on + a model path set).
            sp = self.subagent_policy_dict or {}
            from .app_settings import _as_bool
            return bool(_as_bool(sp.get("enabled"), False)
                        and str(sp.get("model_path") or "").strip())

    def subagent_context_limit(self) -> int:
        """The subagent model's own context window, in tokens (0 = unknown)."""
        try:
            return max(0, int((self.subagent_policy_dict or {}).get("context_size") or 0))
        except (TypeError, ValueError):
            return 0

    def current_subagent_context_tokens(self) -> int:
        """Peak window occupancy of the most recent deployment (0 = none yet)."""
        return int(self.subagent_last_context_tokens or 0)

    def notify_patch_decisions(self, decisions: List[dict]):
        lines = []
        accepted_paths = set()
        for d in decisions:
            status = "ACCEPTED" if d["approved"] else "REJECTED"
            lines.append(f"{status}: {d['description']}")
            p = d.get("path")
            if p:
                self.executor.clear_staged(p)
                if self.draft_store:
                    self.draft_store.clear(p)
                if d["approved"]:
                    accepted_paths.add(p)
        self.messages.append({
            "role": "user",
            "content": "Patch review results:\n" + "\n".join(lines) +
                        "\n\nContinue with the task, taking these results into account.",
        })
    
    def resolve_ask_user(self, answer: str):
        """Called by the UI after the user answers (or auto-answer fallback fires)."""
        call_id = getattr(self, "_pending_ask_user_call_id", "")
        self.messages.append({
            "role": "tool", "tool_call_id": call_id,
            "name": "ask_user", "content": answer,
        })
        self._pending_ask_user_call_id = None

    def resolve_script_approval(self, approved: bool, path: str, args: list, call_id: str, timeout: int = 10):
        if approved:
            content = self.executor.run_python_script(path, args, timeout=timeout)
        else:
            content = "User denied execution of this script."
        self.messages.append({
            "role": "tool", "tool_call_id": call_id,
            "name": "run_python_script", "content": content,
        })

    def resolve_command_approval(self, approved: bool, command: str, description: str, call_id: str,
                                 timeout: int = 30, deny_reason: str = "", added_to_allowlist: bool = False,
                                 result_callback: Callable = None, precomputed_output: str = ""):
        if approved:
            if precomputed_output:
                # Command was already executed; use the pre-computed result instead of re-running.
                content = precomputed_output
                if result_callback:
                    result_callback(content)
            else:
                content = self.executor.run_command(command, description, timeout=timeout)
                if result_callback:
                    result_callback(content)
                else:
                    # Emit tool event for display when no explicit callback is provided
                    self._emit_tool_event("result", "run_command", {
                        "command": command,
                        "output": content,
                        "summary": content[:300],
                    })
        else:
            content = f"User denied execution of this command.\n{f'Deny reason: {deny_reason}' if deny_reason else ''}"
        # Only append the tool result message if it hasn't already been appended.
        # When denied, always append the denial message regardless.
        if not approved or not precomputed_output:
            self.messages.append({
                "role": "tool", "tool_call_id": call_id,
                "name": "run_command", "content": content,
            })
        if added_to_allowlist:
            self.messages.append({
                "role": "user", "content": f"[The user has added '{self.executor.extract_command_name(command)}' to the command allowlist — future runs of this command (with any arguments) will be auto-approved.]",
            })

    def resolve_overflow_choice(self, choice: str) -> None:
        """Called by UI after user picks an overflow action: 'compact' | 'expand' | 'continue' | 'abort'."""
        if choice == "compact":
            self.compact_history()
        elif choice == "expand":
            self.context_limit = int(self.context_limit * (1 + self.policy.auto_expand_percent / 100))
        # 'continue' and 'abort' are handled by the caller directly

    def _generate_draft_patches(self, only_paths: Optional[set] = None) -> List[PendingPatch]:
        """Generate split-diff ``PendingPatch`` objects from the draft store.

        only_paths: if given, only regenerate patches for these paths — used
        to regenerate ONLY drafts actually touched this turn. Without this,
        every turn re-submitted fresh patches for EVERY still-pending draft
        regardless of whether anything changed, which marked the user's
        already-valid pending cards "superseded" and replaced them with
        identical duplicates purely because some unrelated draft existed.
        """
        if not self.draft_store:
            return []
        all_patches: List[PendingPatch] = []
        # Normalize touched paths the SAME way DraftStore does internally,
        # so this comparison can't mismatch against list_active()'s
        # normalized keys (that mismatch was silently dropping patches).
        norm = lambda p: p.lstrip("/")
        active = set(self.draft_store.list_active())
        paths = {norm(p) for p in only_paths} if only_paths is not None else active
        for draft_path in paths:
            if draft_path not in active:
                continue  # touched earlier this turn but already accepted/cleared since
            try:
                disk_content = self.executor.workspace.read_file(draft_path)
            except WorkspaceError:
                disk_content = ""
            region_patches = self.draft_store.generate_split_patches(
                draft_path, disk_content,
                merge_gap=self.policy.merge_gap if hasattr(self.policy, "merge_gap") else None,
                bulk_threshold=self.policy.bulk_threshold if hasattr(self.policy, "bulk_threshold") else None,
            )
            all_patches.extend(region_patches)
        return all_patches

    def _current_usage_percent(self) -> float:
        real = self.last_known_real_tokens
        tokens = real if real is not None else self.current_context_tokens()
        return 100.0 * tokens / max(1, self.context_limit)

    def _mark_cancelled(self, result: "AgentTurnResult") -> "AgentTurnResult":
        """Mark a turn result as cancelled and surface any active drafts as
        pending patches immediately, rather than relying on the normal
        touched-paths-only regeneration (see _generate_draft_patches).
        Cancellation is a rare, explicit abnormal exit that can happen
        before the end-of-turn patch-flush code ever runs — without this,
        drafts created earlier in the same turn stayed orphaned in memory,
        invisible in the Change Queue, until the app was restarted."""
        result.cancelled = True
        if self.draft_store:
            patches = self._generate_draft_patches()
            if patches:
                result.pending_patches = patches
        self._set_activity("idle")
        return result

    def _summarize_messages(self, messages: List[Dict[str, Any]]) -> str:
        """Ask the model to produce a concise summary of the given messages.
        The summary includes: key files read, actions taken, files changed, and
        a general narrative of the user-agent interaction."""
        convo_text = "\n".join(
            f"[{m['role']}] {_flatten_content(m.get('content'))}" for m in messages
        )
        summary_prompt = [
            {
                "role": "system",
                "content": (
                    "You are summarizing a coding session for future reference. "
                    "Produce a structured summary that covers:\n\n"
                    "1. **Files Read** — enumerate every file you opened or examined, "
                    "briefly noting what each contained and why it mattered.\n"
                    "2. **Actions Taken** — what you did (writes, edits, runs, searches) "
                    "and where the results can be found.\n"
                    "3. **Files Changed** — list every file modified, with a one-line "
                    "description of the change.\n"
                    "4. **Interaction Summary** — a concise narrative of the user's "
                    "requests and your responses, including any unresolved issues.\n\n"
                    "Do NOT include raw code or verbose tool output. Keep it concise "
                    "and actionable. This summary will replace the full conversation "
                    "in your context, so make it useful for continuing the work."
                ),
            },
            {"role": "user", "content": convo_text[:40000]},
        ]
        try:
            resp = self.client.chat(summary_prompt, tools=None)
            return resp.get("content") or "(summary unavailable)"
        except Exception as e:
            return f"(summarization failed: {e})"

    def compact_delete_tool_returns(self) -> str:
        """Remove tool result messages from context, keeping the last N per key.

        The keep count is controlled by policy.delete_tool_keep_count.
        When keep_n == 0, all tool results are removed (old behavior).
        When keep_n >= 1, the last N tool results per unique key (e.g. per file
        for read_file, per URL for fetch_text_url) are preserved.
        """
        self._set_activity("compacting")
        original_count = len(self.messages)
        keep_n = getattr(self.policy, "delete_tool_keep_count", 1)
        self.messages = compact_delete_tool_returns(self.messages, keep_n=keep_n)
        removed = original_count - len(self.messages)
        self.last_known_real_tokens = None
        self._set_activity("idle")
        if keep_n == 0:
            return f"Removed {removed} tool result message(s) from context."
        else:
            return f"Removed {removed} tool result message(s) from context (keeping last {keep_n} per file/URL)."

    def compact_summarize_conversation(self) -> str:
        """Replace the entire conversation history (excluding system prompt) with a model-generated summary."""
        self._set_activity("compacting")
        non_system = self.messages[1:]
        if len(non_system) <= 1:
            self._set_activity("idle")
            return "Nothing to summarize — conversation is too short."
        summary_text = self._summarize_messages(non_system)
        self.messages = [self.messages[0], {"role": "user", "content": f"[Conversation Summary]\n{summary_text}"}]
        self.last_known_real_tokens = None
        self._set_activity("idle")
        return f"Replaced {len(non_system)} messages with a conversation summary."

    def compact_history(self, strategy: Optional[str] = None) -> str:
        """Runs compaction using the specified strategy.
        strategy: "delete_tool_returns" | "summarize_conversation" | None (uses policy default)."""
        strategy = strategy or "delete_tool_returns"
        if strategy == "delete_tool_returns":
            return self.compact_delete_tool_returns()
        elif strategy == "summarize_conversation":
            return self.compact_summarize_conversation()
        else:
            return f"Unknown strategy: {strategy}"

    # ── Time-based auto-injection ──────────────────────────────────────────────

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Format elapsed seconds into a human-readable string."""
        if seconds < 60:
            return f"{int(seconds)} second{'s' if int(seconds) != 1 else ''}"
        minutes = int(seconds // 60)
        remaining_secs = int(seconds % 60)
        if remaining_secs == 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return f"{minutes} minute{'s' if minutes != 1 else ''} and {remaining_secs} second{'s' if remaining_secs != 1 else ''}"

    def _start_turn_timer(self):
        """Start tracking wall-clock time for the current turn."""
        self._turn_start_time = time.monotonic()
        self._last_injected_barrier_index = -1

    def _inject_time_barriers(self) -> Optional[str]:
        """Check if any time barriers have been crossed and inject them.
        
        Returns the injected message text if any barriers were crossed, or None.
        """
        if not self.policy.time_barriers_enabled or not self.policy.time_barriers:
            return None
        if self._turn_start_time is None:
            return None

        elapsed = time.monotonic() - self._turn_start_time
        barriers = sorted(self.policy.time_barriers, key=lambda b: b["seconds"])
        injected = []

        for i, barrier in enumerate(barriers):
            if i <= self._last_injected_barrier_index:
                continue  # already injected this barrier this turn
            if elapsed >= barrier["seconds"]:
                elapsed_str = self._format_elapsed(elapsed)
                msg_lines = [f"[System information]: {elapsed_str} have passed since your turn started."]
                custom_msg = barrier.get("message", "").strip()
                if custom_msg:
                    msg_lines.append(custom_msg)
                injected.append("\n".join(msg_lines))
                self._last_injected_barrier_index = i

        if injected:
            combined = "\n\n".join(injected)
            self.messages.append({"role": "user", "content": combined})
            return combined
        return None

    def _apply_reasoning_retention(self, reasoning_text: str, full_content: str) -> str:
        mode = self.policy.reasoning_retention
        if mode == "keep_all" or not reasoning_text.strip():
            return full_content
        if mode in ("hide", "drop"):
            return split_reasoning(full_content)[0]
        if mode == "keep_last":
            for m in self.messages:
                if m.get("role") == "assistant" and "<think>" in (m.get("content") or ""):
                    m["content"] = split_reasoning(m["content"])[0]
            return full_content
        return full_content

    def _tool_result_or_error(self, name: str, args: dict) -> str:
        return self.executor.execute_read_tool(name, args)

    def _subagent_stream_cb(self, piece: str, phase: str, tool_name: str = "", tokens=None):
        """Forward a deployed subagent's streamed output into the main stream.

        The phases are forwarded UNCHANGED — 'subagent_content' /
        'subagent_reasoning' / 'subagent_tool_args' — and the worker's tool name
        rides along unmodified. They used to be re-mapped onto the ordinary
        'content' / 'reasoning' / 'tool_args' so the UI "wouldn't drop" them,
        and that is exactly what glued the worker's words onto the parent's
        bubble: _on_delta keys its live assistant bubble on the phase, and
        round_num does NOT change across a deployment (it is bumped once per
        *parent* model call, at the top of step()'s loop, while the deployment is
        dispatched mid-iteration). So every worker delta appended to the parent's
        already-open _live_content_msg — the agent literally wrote on top of the
        subagent's output.

        Keeping the distinct phases makes the UI's own bubble the decision point
        instead of a shared counter, and the 'subagent: ' prefix that used to be
        the only differentiator is gone with it: it was cosmetic, and it was
        being read as a real tool name.

        tokens are never touched (the subagent's token usage is rolled up by the
        parent separately, and forwarding it here would double-count live).

        The one exception is 'subagent_usage_total': the worker's own context
        occupancy, which the runner deliberately sends with tokens=None so it
        survives this filter. It is consumed here (stashed for the Context tab,
        which polls every 500 ms while the worker thread runs) and NOT forwarded
        downstream — the UI reads it off the agent rather than off the delta
        stream, so it can never be mistaken for content.

        Wrapped in try/except: on_delta is a UI callback and a failure in it must
        never break a running deployment."""
        if tokens is not None:
            return
        if phase == "subagent_usage_total":
            try:
                self.subagent_last_context_tokens = max(
                    self.subagent_last_context_tokens, int(piece or 0))
            except (TypeError, ValueError):
                pass
            return
        try:
            self._pending_subagent_delta_cb(piece, phase, tool_name or "", None)
        except Exception:
            pass

    def _deploy_subagent(self, fn_args: dict, call_id: str, on_delta) -> str:
        """Run one subagent to completion on its own model and return its final
        answer as the tool-result text.

        Contract with step(): this MUST return a string and MUST NOT raise — the
        caller appends it verbatim as the tool message. Every failure mode
        (no client provider, no model configured, model not loaded, the request
        itself failing, an empty task) comes back as a readable message the main
        agent can relay, exactly like the other side-effecting tools.

        The subagent gets its OWN client (the subagent model server) but SHARES
        this agent's ToolExecutor, so its read/search tools hit the same virtual
        FS as ours. It is capped by max_rounds so a runaway worker can't loop
        forever, and cancellation is polled via self._cancel_requested because
        the parent cannot return to the UI mid-deployment.
        """
        task = (fn_args.get("task") or "").strip()
        if not task:
            return ("Error: deploy_subagent needs a non-empty 'task' — the "
                    "self-contained instructions for the worker. Nothing was deployed.")

        # No provider means the UI never wired a subagent model in: surface that
        # plainly instead of failing deep inside the runner.
        if self.subagent_client_provider is None:
            return (
                "Subagent deployment is not available: no subagent model is "
                "configured. Ask the user to set a small text model and tick "
                "'Enable subagent model' under Settings → Backend, then restart "
                "the server. Continue the task yourself without a subagent."
            )

        # Resolve the client lazily. The provider raises RuntimeError with a
        # user-actionable message when the model is not configured/enabled or
        # could not be brought up; we turn that into a normal tool result.
        try:
            sub_client = self.subagent_client_provider()
        except Exception as e:
            self._emit_tool_event("error", "deploy_subagent", {"error": str(e)})
            return (
                f"Could not deploy a subagent: {e}\n\n"
                "Continue the task yourself without a subagent."
            )

        sp = self.subagent_policy_dict or {}
        try:
            max_rounds = int(fn_args.get("max_rounds") or sp.get("max_rounds") or 8)
        except (TypeError, ValueError):
            max_rounds = 8

        # Surface "a worker is running" in the activity line; the tool-call
        # bubble already marks the deployment in chat.
        self._set_activity(f"subagent_running")

        # A deployment is starting: its window is fresh (the runner builds its
        # own transcript from scratch), so the meter starts at zero rather than
        # showing the previous worker's high-water mark.
        self.subagent_last_context_tokens = 0
        self.subagent_deployments += 1
        # Set before the runner starts and cleared in the finally block below:
        # the Context tab uses it to tell "the deployment on screen is live" from
        # "this is the last one that finished".
        self._subagent_deploy_active = True

        # Hold the real on_delta for the duration of the deployment so the
        # shared stream callback can forward the subagent's words without us
        # having to thread the callback through SubagentRunner's constructor.
        self._pending_subagent_delta_cb = on_delta or (lambda *a, **k: None)
        # Tell the UI the worker is about to speak, so it can close whatever
        # parent bubble is open and give the deployment its own space. Paired
        # with the 'subagent_end' emitted in the finally block below; the UI
        # treats them as open/close markers, not as content.
        try:
            self._pending_subagent_delta_cb("", "subagent_begin", "", None)
        except Exception:
            pass
        try:
            runner = SubagentRunner(
                sub_client,
                self.executor,
                max_rounds=max(1, max_rounds),
                use_streaming=bool(sp.get("use_streaming", True)),
                enable_thinking=bool(sp.get("enable_thinking", True)),
                preserve_thinking=bool(sp.get("preserve_thinking", False)),
                patch_mode=self.policy.patch_mode,
                fetch_policy=getattr(self, "_web_fetch_policy", None),
                on_delta=self._subagent_stream_cb,
                on_tool_event=self._emit_tool_event,
                on_activity=self._set_activity,
                cancel_check=lambda: self._cancel_requested,
                context_limit=self.subagent_context_limit(),
            )
            sub_result = runner.run(
                task=task,
                expected_return=(fn_args.get("expected_return") or "").strip(),
            )
        except Exception as e:
            # Runner.run is written not to raise, but a provider/client blowing up
            # mid-call must still degrade to a message, never crash the turn.
            self._emit_tool_event("error", "deploy_subagent", {"error": str(e)})
            return (
                f"Subagent deployment failed unexpectedly ({type(e).__name__}: {e}). "
                "Continue the task yourself without a subagent."
            )
        finally:
            # The worker is no longer on screen: tell the Context tab so its
            # meter switches from "current deployment" to "last deployment".
            self._subagent_deploy_active = False
            # Close the worker's block BEFORE dropping the callback, and bump
            # _response_round so the parent's next delta is reported under a new
            # round number. Without that bump the parent's post-deployment text
            # would carry the same round_num as the text it emitted before the
            # deployment, and the UI's per-round reset would never fire — the
            # exact condition that let the parent continue writing into a bubble
            # it should have started fresh.
            try:
                self._pending_subagent_delta_cb("", "subagent_end", "", None)
            except Exception:
                pass
            self._pending_subagent_delta_cb = None
            self._response_round += 1
            self._set_activity("idle")

        # Merge the worker's token usage into the parent's accounting so the
        # context meter and per-turn total reflect the whole effort, not just our
        # own calls. (Runner counts its own generated tokens; we do not forward
        # them live to avoid double counting.)
        self.total_generated_tokens += int(getattr(sub_result, "generated_tokens", 0) or 0)

        # Publish the deployment's own context occupancy for the Context tab's
        # subagent meter. This is the authoritative end-of-deployment number;
        # the live pings written to subagent_last_context_tokens during the run
        # can only ever be lower, so take the larger of the two.
        sub_ctx = int(getattr(sub_result, "context_tokens", 0) or 0)
        self.subagent_last_context_tokens = max(
            self.subagent_last_context_tokens, sub_ctx)
        self.subagent_last_context_limit = int(
            getattr(sub_result, "context_limit", 0) or 0)

        # Hand the subagent's tool log back to the dispatch loop via an instance
        # field (the loop owns the per-turn `tool_log` list). It cannot edit
        # files, so no draft paths are recorded — just a readable trace of what
        # the worker looked at.
        self._last_subagent_tool_log = [
            f"subagent: {entry}" for entry in (getattr(sub_result, "tool_log", None) or [])
        ]

        self._emit_tool_event("result", "deploy_subagent", {
            "rounds_used": getattr(sub_result, "rounds_used", 0),
            "exhausted": bool(getattr(sub_result, "exhausted", False)),
            "summary": (getattr(sub_result, "final_text", "") or "")[:400],
        })

        # Cancelled mid-deployment: say so plainly; do not present a partial
        # answer as if it were complete.
        if getattr(sub_result, "cancelled", False):
            partial = (getattr(sub_result, "final_text", "") or "").strip()
            if partial:
                return ("The subagent was cancelled before finishing. Its partial "
                        f"output so far:\n\n{partial}")
            return "The subagent was cancelled before it produced any output."

        final = (getattr(sub_result, "final_text", "") or "").strip()
        err = (getattr(sub_result, "error", None) or "").strip()

        if not final:
            # Nothing usable came back. Report the runner's own error if it set
            # one, otherwise be explicit that the worker returned empty.
            if err:
                return f"The subagent finished without a usable answer. {err}"
            return ("The subagent finished but returned an empty answer. Treat the "
                    "task as not done: either retry with a clearer 'task'/"
                    "'expected_return', or do it yourself.")

        # A partial answer with a recorded error (e.g. rounds exhausted) is still
        # delivered — but flagged, so the parent knows it may be incomplete.
        if err:
            return (f"[Subagent answer (possibly incomplete — it stopped early: "
                    f"{err})]\n\n{final}")
        return f"[Subagent answer]\n\n{final}"

    def _handle_web_tool(self, name: str, args: dict) -> str:
        policy = self.executor.web_policy
        allow = self.executor.web_allowlist
        block = self.executor.web_blocklist
        fetch_policy = getattr(self, "_web_fetch_policy", None)
        try:
            if name == "web_search":
                return web_tools.web_search(
                    args["query"],
                    engine=args.get("engine", "duckduckgo"),
                    max_results=args.get("max_results", 6),
                    policy=policy, allowlist=allow, blocklist=block,
                )
            elif name == "fetch_text_url":
                return web_tools.fetch_text_url(
                    args["url"],
                    timeout=args.get("timeout", 30),
                    policy=policy, allowlist=allow, blocklist=block,
                    max_fetch_policy=fetch_policy,
                )
            elif name == "web_screen_capture":
                result = web_tools.web_screen_capture(
                    args["url"],
                    mode=args.get("mode", "full"),
                    policy=policy, allowlist=allow, blocklist=block,
                )
                return result.get("text", "No screenshot captured."), result.get("images", [])
            elif name == "extract_images":
                result = web_tools.extract_images(
                    args["url"],
                    max_images=args.get("max_images", 5),
                    max_embed_images=args.get("max_embed_images", 3),
                    policy=policy, allowlist=allow, blocklist=block,
                )
                return result.get("text", "No images extracted."), result.get("images", [])
        except web_tools.WebPolicyError as e:
            return f"Error: {e}"
        except _WebBrowserKillNeeded as e:
            # Re-raise with the browser type so the caller knows which one to kill
            raise
        return f"Unknown web tool: {name}"


    def step(self, use_streaming: bool = True, on_delta: Callable[[str, str, str, Optional[int]], None] = None) -> AgentTurnResult:
        # If a previous turn ended via end_turn and no new user message has
        # arrived yet, return immediately so the model isn't called again.
        # The flag is cleared when add_user_message() is called.
        if self._turn_complete:
            result = AgentTurnResult(ended=True)
            self._set_activity("idle")
            return result

        self._cancel_requested = False
        result = AgentTurnResult()
        tool_log = []
        rounds = 0
        batch_collected: List[PendingPatch] = []

        # A "Continue" turn (see prepare_continue) is armed exactly once: the
        # anchor index and prefix are captured here, and only the FIRST model
        # response of this turn is merged back into the message being continued.
        # Later rounds in the same turn are ordinary new assistant messages, and
        # nothing re-applies the merge after a cancel, an error, or a re-entry
        # into step().
        #
        # self._continuation is deliberately NOT cleared here: the prefill
        # fallback below needs it if the server refuses the prefill. It is
        # cleared as soon as the first response is in hand (merge branch), and
        # prepare_continue always overwrites it, so it cannot leak into a later
        # turn.
        cont = self._continuation
        continuing = bool(cont)
        merge_into_anchor = continuing

        def _call_model(with_prefix: bool):
            """Issue one model request, optionally as an assistant prefill.

            Kept as a closure so the prefill retry below sends exactly the same
            payload, with no drift between the streaming and non-streaming paths.

            ``with_prefix`` no longer changes the request body. Prefilling the
            trailing assistant turn is llama.cpp's default and needs no flag --
            what makes it happen is that ``prepare_continue`` left that message
            as the last turn in the history. The argument is retained purely so
            the failure below can say which attempt it is describing.

            CRITICAL: when ``merge_into_anchor`` is True (the first model call
            of a continuation turn), the request must be sent WITHOUT tools and
            WITHOUT chat_template_kwargs.  Both of these change how the Jinja
            chat template renders the prompt:

            * ``tools`` causes the template to add tool-call parsing sections
              to the assistant turn, which changes the token structure after
              the prefilled text and breaks the prefill — the model sees a
              new "assistant:" header instead of continuing the open one.

            * ``enable_thinking: true`` makes thinking-capable templates wrap
              the response in <think>...</think> tags, again changing the
              structure after the prefix and causing the model to start fresh.

            By omitting both, the template renders the trailing assistant
            message as a bare open prefix (no closing tag, no new header), and
            llama.cpp's ``--prefill-assistant`` (on by default) makes the model
            continue that text as a pure language-modelling task.
            """
            if merge_into_anchor:
                # Bare prefill request: no tools, no template kwargs.
                if use_streaming:
                    return self.client.chat_stream(
                        self.messages, tools=None,
                        on_delta=lambda p, phase, tool_name="", tokens=None: on_delta(p, phase, tool_name, tokens, self._response_round),
                        cancel_check=lambda: self._cancel_requested,
                        assistant_prefix=with_prefix,
                    )
                return self.client.chat(
                    self.messages, tools=None,
                    assistant_prefix=with_prefix,
                )
            schemas = self.executor.active_schemas()
            if use_streaming:
                return self.client.chat_stream(
                    self.messages, tools=schemas,
                    on_delta=lambda p, phase, tool_name="", tokens=None: on_delta(p, phase, tool_name, tokens, self._response_round),
                    cancel_check=lambda: self._cancel_requested,
                    enable_thinking=self.policy.enable_thinking,
                    preserve_thinking=self.policy.preserve_thinking,
                    assistant_prefix=with_prefix,
                )
            return self.client.chat(
                self.messages, tools=schemas,
                enable_thinking=self.policy.enable_thinking,
                preserve_thinking=self.policy.preserve_thinking,
                assistant_prefix=with_prefix,
            )

        while rounds < self.policy.max_tool_rounds:
            if self._cancel_requested:
                return self._mark_cancelled(result)
    
            self._set_activity("waiting_for_model")
            # Track the response round so the UI can reset its chunk counters
            # at the start of each new model response (within a multi-round turn).
            self._response_round += 1
            try:
                response = _call_model(continuing)
            except Exception as e:
                # A refused prefill is not something to paper over. Earlier code
                # retried with a hidden "please continue" message, which is how
                # Continue ended up restating the message instead of continuing
                # it. There is no silent fallback now: the user is told their
                # server or model cannot prefill an assistant turn, and can use
                # Retry to regenerate or Respond to answer afresh.
                if continuing and self._continuation is not None and is_prefill_rejection(e):
                    self._continuation = None
                    self._emit_tool_event("result", "continue", {
                        "summary": "The server refused to continue the assistant's "
                                   "message (assistant prefill unavailable).",
                    })
                    result.error = (
                        "Could not continue the message: this llama-server build or "
                        "chat template will not prefill an open assistant turn.\n\n"
                        f"Server said: {e}\n\n"
                        "Check that the server was not started with --no-prefill-assistant. "
                        "Thinking/reasoning models often cannot be prefilled mid-answer -- "
                        "for those, use Retry to regenerate or Respond to answer again."
                    )
                    self._set_activity("idle")
                    return result
                err_str = str(e)
                has_recent_image = any(
                    isinstance(m.get("content"), list) and
                    any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
                    for m in self.messages[-3:]
                )
                if has_recent_image and ("500" in err_str or "400" in err_str):
                    stripped = self._strip_last_image_messages()
                    # The server just proved this model has no vision, regardless of config.
                    self.vision_available = False
                    fallback_description = self._recaption_last_media()
                    if fallback_description:
                        notice = (
                            f"[System notice] The server rejected {stripped} image attachment(s) — "
                            "the currently loaded model does not support vision (no --mmproj loaded). "
                            "The image(s) have been removed from context. As a fallback, a local "
                            "image-to-text model was used instead:\n\n" + fallback_description
                        )
                        log_note = "[view_image failed — no vision support; fell back to text description]"
                    else:
                        notice = (
                            f"[System notice] The server rejected {stripped} image attachment(s) — "
                            "the currently loaded model does not support vision (no --mmproj loaded). "
                            "The image(s) have been removed from context so the conversation can continue. "
                            "Inform the user their model needs a vision-capable GGUF + matching mmproj file "
                            "to use view_image, and continue the task without relying on image content."
                        )
                        log_note = "[view_image failed — no vision support; image stripped from context]"
                    self.messages.append({"role": "user", "content": notice})
                    result.error = None
                    result.tool_log.append(log_note)
                    rounds += 1
                    continue
                result.error = f"Model request failed: {e}"
                self._set_activity("idle")
                return result
    
            content = response.get("content") or ""
    
            # Use real token count from server response if available; otherwise fall back to heuristic.
            if response.total_tokens is not None and response.prompt_tokens is not None:
                turn_tokens = max(0, response.total_tokens - response.prompt_tokens)
            else:
                turn_tokens = max(1, len(content) // 4)
    
            self.total_generated_tokens += turn_tokens
            result.turn_generated_tokens += turn_tokens
    
            real_total = response.get("total_tokens")
            if real_total is not None:
                self.last_known_real_tokens = real_total
            # Propagate timing info to the result for the UI.
            result.prompt_per_second = response.prompt_per_second
            result.predicted_per_second = response.predicted_per_second
            # Report per-iteration (per-call) token count — resets each model response.
            self._set_activity(f"waiting_for_model:{turn_tokens}")
    
            if self._cancel_requested:
                return self._mark_cancelled(result)
    
            tool_calls = response.get("tool_calls") or None
    
            server_reasoning = response.get("reasoning_content")
            if server_reasoning:
                visible_text = content
                reasoning_text = server_reasoning
            else:
                visible_text, reasoning_blocks = split_reasoning(content)
                reasoning_text = "\n".join(reasoning_blocks)
    
            if merge_into_anchor:
                # Prefill continuation: the model kept writing the message that
                # was already open, so extend that very message rather than
                # appending a second assistant turn.
                #
                # The anchor is normally NOT the last message — this agent ends
                # its turns with an end_turn call plus its result, so the tail
                # is only dropped when it is a pure control block (see
                # _ends_turn). Dropping it unconditionally, as this used to,
                # discarded the previous turn's tool protocol AND left the
                # anchor holding both prose and a stale end_turn call, which is
                # exactly the shape llama-server refuses to prefill. When the
                # tail is anything else, keep it and append the continued
                # message instead of rewriting history.
                self._continuation = None
                idx = cont["index"]
                tail = self.messages[idx + 1:]
                # The tail may only be dropped if it contains no live tool
                # protocol. An end_turn wrapper plus its results is pure
                # bookkeeping and is safe to discard; a bare tool result would
                # be orphaned by the discard, so in that case append the
                # continued message instead of rewriting history.
                keep_tail = (not tail) or _has_orphaned_tool_result(self.messages, tail)
                # Strip any words the model restated from the end of the prefix,
                # so the merge appends only what is genuinely new.
                new_text = _merge_continuation_text(cont["prefix"], content)
                if keep_tail:
                    assistant_msg = {"role": "assistant", "content": cont["prefix"] + new_text}
                    self.messages.append(assistant_msg)
                else:
                    self.messages[idx]["content"] = cont["prefix"] + new_text
                    # The message is being written again, so the end_turn call it
                    # used to close with is gone; whether the new text ends the
                    # turn is decided by what the model does this time.
                    self.messages[idx].pop("tool_calls", None)
                    del self.messages[idx + 1:]
                    assistant_msg = self.messages[idx]
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                # Everything downstream (the UI's live bubble included) reports
                # the continuation only, never the prefix -- the prefix is
                # already on screen and already in the message.
                content = new_text
            else:
                assistant_msg = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                self.messages.append(assistant_msg)
            # Only the first response of a continuation turn merges into the
            # anchor; subsequent rounds in this same turn are new messages.
            merge_into_anchor = False
    
            if not tool_calls:
                result.assistant_text = visible_text
                result.reasoning_text = reasoning_text
                break
    
            ended_turn = False
            for call in tool_calls:
                fn_name = call["function"]["name"]
                try:
                    fn_args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
    
                if fn_name in CONTROL_TOOL_NAMES:
                    self._set_activity("idle")
                    summary = fn_args.get("summary", "")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    self._emit_tool_event("result", fn_name, {"summary": summary})
                    self.executor.stats.record_call(fn_name, True, "ended")
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": "Turn ended by agent.",
                    })
                    tool_log.append(f"end_turn: {summary}" if summary else "end_turn")
                    # The continuation prefix is NOT part of this response — the
                    # UI is already showing it and streams the new words onto it.
                    # Only fall back to the summary when this turn produced no
                    # text of its own; otherwise the summary would be appended as
                    # a duplicate second bubble right after the merged message.
                    result.assistant_text = visible_text or ("" if continuing else summary)
                    result.reasoning_text = reasoning_text
                    result.ended = True
                    ended_turn = True
                    self._turn_complete = True
                    break
    
                elif fn_name == "ask_user":
                    self._set_activity("idle")
                    self.executor.stats.record_call(fn_name, True, "asked")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    self._pending_ask_user_call_id = call.get("id", "")
                    result.assistant_text = visible_text
                    result.reasoning_text = reasoning_text
                    result.ask_user_request = {
                        "question": fn_args.get("question", ""),
                        "choices": fn_args.get("choices") or [],
                        "allow_custom_answer": fn_args.get("allow_custom_answer", True),
                        "multi_select": bool(fn_args.get("multi_select", False)),
                    }
                    result.tool_log = tool_log
                    return result
    
                elif fn_name == "deploy_subagent":
                    # Runs a worker on the subagent model to completion and hands
                    # back its final answer as this tool's result. Blocking on
                    # purpose: the parent cannot usefully continue until the
                    # worker reports. _deploy_subagent never raises — every
                    # failure is a readable message — so this branch only has to
                    # record the outcome and feed it back into the transcript.
                    self._set_activity("tool_call:deploy_subagent")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    tool_content = self._deploy_subagent(fn_args, call.get("id", ""), on_delta)
                    # Surface the worker's own tool trace (it read/searched a lot
                    # in its own context) in the parent's log for transparency.
                    tool_log.extend(self._last_subagent_tool_log)
                    self._last_subagent_tool_log = []
                    self.executor.stats.record_call(fn_name)
                    tool_log.append(f"deploy_subagent({(fn_args.get('task', '') or '')[:60]})")
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": tool_content,
                    })

                elif fn_name in WRITE_TOOL_NAMES:
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        patch = self.executor.build_pending_patch(fn_name, fn_args)
                        mode = self.policy.patch_mode
                        auto_apply = False
                        if mode == "full_auto":
                            auto_apply = True
                        elif mode == "auto_approve_new_files" and patch.kind == "create":
                            auto_apply = True
    
                        if auto_apply:
                            self.executor.apply_patch(patch)
                            patch.status = "accepted"
                            self._auto_applied_patches.append(patch)
                            self.executor.stats.record_decision(fn_name, True)
                            if self.draft_store:
                                self.draft_store.clear(patch.path)
                            tool_content = f"Applied automatically (mode={mode}): {patch.path}"
                            self._emit_tool_event("result", fn_name, {
                                "path": patch.path, "kind": patch.kind, "staged": False, "auto_applied": True,
                            })
                        else:
                            batch_collected.append(patch)
                            self.executor.mark_staged(patch.path, patch.kind)
                            self._touched_draft_paths.add(patch.path)
                            tool_content = f"Staged for review: {patch.path} ({patch.kind}). Waiting on user decision."
                            self._emit_tool_event("result", fn_name, {
                                "path": patch.path, "kind": patch.kind, "staged": True, "auto_applied": False,
                            })
                        tool_log.append(f"{fn_name}({fn_args.get('path', fn_args.get('old_path', ''))})")
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        tool_log.append(f"{fn_name} FAILED: {e}")
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": tool_content,
                    })
    
                elif fn_name == "web_screen_capture":
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_result = self._handle_web_tool(fn_name, fn_args)
                        if isinstance(tool_result, tuple):
                            tool_content, images = tool_result
                        else:
                            tool_content = tool_result
                            images = []
                        self._emit_tool_event("result", fn_name, {"url": fn_args.get("url", ""), "summary": tool_content})
    
                        # Embed screenshot images directly as image_url content parts
                        if images:
                            content_parts = [{"type": "text", "text": tool_content}]
                            for img_b64, mime in images:
                                content_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                                })
                            tool_log.append(f"{fn_name}({fn_args.get('url', '')}) — {len(images)} image(s)")
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": content_parts,
                            })
                        else:
                            tool_log.append(f"{fn_name}({fn_args.get('url', '')})")
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": tool_content,
                            })
                    except _WebBrowserKillNeeded as e:
                        result.web_browser_kill_request = {"browser_type": e.browser_type}
                        result.assistant_text = visible_text
                        result.reasoning_text = reasoning_text
                        result.tool_log = tool_log
                        self._set_activity("idle")
                        return result
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                        tool_log.append(f"{fn_name} FAILED: {e}")
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": tool_content,
                        })
    
                elif fn_name in ("web_search", "fetch_text_url", "extract_images"):
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_result = self._handle_web_tool(fn_name, fn_args)
                        if isinstance(tool_result, tuple):
                            tool_content, images = tool_result
                        else:
                            tool_content = tool_result
                            images = []
                        self._emit_tool_event("result", fn_name, {"query_or_url": fn_args.get("query", fn_args.get("url", "")), "summary": tool_content})
    
                        # Detect truncation marker for fetch_text_url
                        if fn_name == "fetch_text_url" and web_tools._TRUNCATION_MARKER in tool_content:
                            url = fn_args.get("url", "")
                            self._web_fetch_truncation = {"url": url}
                        else:
                            self._web_fetch_truncation = None
    
                        # Embed extracted images as image_url content parts
                        if images:
                            content_parts = [{"type": "text", "text": tool_content}]
                            for img_b64, mime in images:
                                content_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                                })
                            tool_log.append(f"{fn_name}({fn_args.get('url', '')}) — {len(images)} image(s)")
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": content_parts,
                            })
                        else:
                            tool_log.append(f"{fn_name}({fn_args.get('url', '')})")
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": tool_content,
                            })
                    except _WebBrowserKillNeeded as e:
                        result.web_browser_kill_request = {"browser_type": e.browser_type}
                        result.assistant_text = visible_text
                        result.reasoning_text = reasoning_text
                        result.tool_log = tool_log
                        self._set_activity("idle")
                        return result
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                        self._web_fetch_truncation = None
                        tool_log.append(f"{fn_name} FAILED: {e}")
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": tool_content,
                        })
                
                elif fn_name in ("view_image", "view_video"):
                    # Media viewer pair: view_image is still/single-frame only,
                    # view_video samples frames from an optional time window.
                    # Both share the delivery pipeline below (the follow-up
                    # user message carrying the image parts) and both degrade
                    # to the captioner when no vision projector is loaded.
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    path = fn_args.get("path", "")
                    try:
                        import os, base64
                        ext = os.path.splitext(path)[1].lower()
                        if fn_name == "view_image":
                            if ext in self.executor.workspace.VIDEO_EXTS:
                                raise ValueError(
                                    f"'{path}' is a video. view_image only shows still images and SVGs — "
                                    "call view_video instead (optionally with start_time/end_time)."
                                )
                            frames = [self.executor.workspace.load_viewable_image(path)]
                            is_video = False
                        else:
                            if ext not in self.executor.workspace.VIDEO_EXTS:
                                raise ValueError(
                                    f"'{path}' is not a video. view_video only accepts video files — "
                                    "use view_image for still images and SVGs."
                                )
                            max_frames = fn_args.get("max_frames", self.MEDIA_MAX_FRAMES)
                            if max_frames is None:
                                max_frames = self.MEDIA_MAX_FRAMES
                            try:
                                max_frames = int(max_frames)
                            except (TypeError, ValueError):
                                raise ValueError(
                                    f"max_frames must be an integer number of frames, got {max_frames!r}.")
                            max_frames = max(1, min(60, max_frames))
                            start_time = fn_args.get("start_time")
                            end_time = fn_args.get("end_time")
                            for label, value in (("start_time", start_time), ("end_time", end_time)):
                                if value is None:
                                    continue
                                try:
                                    float(value)
                                except (TypeError, ValueError):
                                    raise ValueError(
                                        f"{label} must be a number of seconds (or omitted), got {value!r}.")
                            if (start_time is not None and end_time is not None
                                    and float(start_time) >= float(end_time)):
                                raise ValueError(
                                    f"start_time ({start_time}s) must be before end_time ({end_time}s). "
                                    "Omit either one to sample from the beginning/end of the video, and "
                                    "use media_metadata first to check the video's duration.")
                            frames = self.executor.workspace.extract_video_frames(
                                path, max_frames=max_frames,
                                start_time=start_time, end_time=end_time)
                            is_video = True
                        self._last_viewed_media = (path, is_video)
    
                        if not self.vision_available and self.captioner is not None:
                            self._set_activity(f"tool_call:{fn_name}:captioning")
                            description = self._caption_media(path, frames, is_video)
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": description,
                            })
                            tool_log.append(f"{fn_name}({path}) [text description — no vision support]")
                            self._emit_tool_event("result", fn_name, {"path": path, "mode": "caption_fallback"})
                        else:
                            content_parts = [{"type": "text", "text": f"[Here is the content you requested from: {path}]"}]
                            for f_bytes in frames:
                                b64 = base64.b64encode(f_bytes).decode("ascii")
                                content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                            if is_video:
                                tool_note = f"Loaded {len(frames)} sampled frames from video '{path}' — shown to you next."
                            else:
                                tool_note = f"Image '{path}' loaded — shown to you in the next message."
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": tool_note,
                            })
                            self.messages.append({"role": "user", "content": content_parts})
                            tool_log.append(f"{fn_name}({path})")
                            self._emit_tool_event("result", fn_name, {"path": path})
                    except Exception as e:
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": (
                                f"Error viewing '{path}': {e}. If this is a 400/vision error, the currently loaded "
                                "model likely does not support image input (no mmproj vision projector loaded). "
                                "Tell the user their model needs a vision-capable GGUF + mmproj file to view images, "
                                "or that they can enable the image-to-text fallback under Settings -> Vision to at "
                                "least get a rough (unreliable) text description of images."
                            ),
                        })
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                        tool_log.append(f"{fn_name} FAILED: {e}")
    
                elif fn_name in COMPUTER_TOOL_NAMES:
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_result = computer_tools.run_computer_tool(fn_name, fn_args)
                        if isinstance(tool_result, dict) and "image" in tool_result and tool_result["image"]:
                            # Image-bearing tool: embed as image_url
                            content_parts = [{"type": "text", "text": tool_result.get("text", "")}]
                            b64 = tool_result["image"]
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            })
                            self._emit_tool_event("result", fn_name, {
                                "summary": tool_result.get("text", "")[:300],
                                "has_image": True,
                            })
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": content_parts,
                            })
                        else:
                            # Text-only tool result
                            text_result = tool_result.get("text", str(tool_result)) if isinstance(tool_result, dict) else str(tool_result)
                            self._emit_tool_event("result", fn_name, {"summary": text_result[:300]})
                            self.messages.append({
                                "role": "tool", "tool_call_id": call.get("id", ""),
                                "name": fn_name, "content": text_result,
                            })
                    except Exception as e:
                        tool_content = f"Error in {fn_name}: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": tool_content,
                        })

                elif fn_name == "run_python_script":
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    if self.policy.script_run_mode == "always_allow":
                        tool_content = self.executor.run_python_script(
                            fn_args.get("path", ""), fn_args.get("args", []),
                            timeout=self.policy.script_timeout,
                        )
                        self._emit_tool_event("result", fn_name, {"summary": tool_content})
                        tool_log.append(f"run_python_script({fn_args.get('path','')})")
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": tool_content,
                        })
                    else:
                        self._set_activity("idle")
                        result.script_approval_request = {
                            "path": fn_args.get("path", ""),
                            "args": fn_args.get("args", []),
                            "call_id": call.get("id", ""),
                            "timeout": self.policy.script_timeout,
                        }
                        result.assistant_text = visible_text
                        result.reasoning_text = reasoning_text
                        result.tool_log = tool_log
                        return result
    
                elif fn_name == "run_command":
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    cmd = fn_args.get("command", "")
                    desc = fn_args.get("description", "")
                    cmd_timeout = fn_args.get("timeout", self.policy.command_timeout)
                    cmd_name = self.executor.extract_command_name(cmd)
                    
                    # Parse all base commands in the shell command string
                    all_commands = list(dict.fromkeys(self.executor.parse_shell_commands(cmd)))
                    
                    # Check if ALL commands are in the allowlist (not just the first one)
                    cmd_in_allowlist = self.executor.check_commands_allowed(
                        cmd, self.policy.command_allowlist
                    )
                    
                    if self.policy.command_run_mode == "always_allow" or cmd_in_allowlist:
                        tool_content = self.executor.run_command(cmd, desc, timeout=cmd_timeout)
                        self._emit_tool_event("result", fn_name, {
                            "command": cmd,
                            "output": tool_content,
                            "summary": tool_content[:300],
                        })
                        tool_log.append(f"run_command({cmd})")
                        self.messages.append({
                            "role": "tool", "tool_call_id": call.get("id", ""),
                            "name": fn_name, "content": tool_content,
                        })
                    else:
                        self._set_activity("idle")
                        result.command_approval_request = {
                            "command": cmd,
                            "description": desc,
                            "call_id": call.get("id", ""),
                            "timeout": cmd_timeout,
                            "command_name": cmd_name,
                            "all_commands": all_commands,
                        }
                        result.assistant_text = visible_text
                        result.reasoning_text = reasoning_text
                        result.tool_log = tool_log
                        return result
    
                elif fn_name == "think_out_loud":
                    # think_out_loud: emit full thought (no truncation) so the UI
                    # shows the complete reasoning text in the chat.
                    self._set_activity("tool_call:think_out_loud")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_content = self._tool_result_or_error(fn_name, fn_args)
                        self._emit_tool_event("result", fn_name, {"thought": tool_content, "summary": tool_content})
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                    self.executor.stats.record_call(fn_name)
                    tool_log.append(f"{fn_name}: " + ", ".join(str(v)[:40] for v in fn_args.values()))
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": "Thought recorded.",
                    })
    
                elif fn_name in ("read_file", "read_multiple_files"):
                    # Read tools, with optional aggressive pruning.
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_content = self._tool_result_or_error(fn_name, fn_args)
                        # Full content, not a truncated snippet: the UI's
                        # tool_result bubble is collapsible now, so there is
                        # no longer a reason to lose data before it even
                        # reaches the chat — collapsing/expanding is a
                        # display concern, not a data concern.
                        self._emit_tool_event("result", fn_name, {"summary": tool_content})
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                    self.executor.stats.record_call(fn_name)
                    tool_log.append(f"{fn_name}: " + ", ".join(str(v)[:40] for v in fn_args.values()))
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": tool_content,
                    })
                    # Aggressive pruning: remove stale reads immediately after this one.
                    if self.policy.prune_aggression == "after_each_read":
                        self.prune_stale_reads()
    
                else:
                    # Generic fallback for tools not handled above (e.g. think_out_loud).
                    self._set_activity(f"tool_call:{fn_name}")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    try:
                        tool_content = self._tool_result_or_error(fn_name, fn_args)
                        self._emit_tool_event("result", fn_name, {"summary": tool_content})
                    except Exception as e:
                        tool_content = f"Error: {e}"
                        self._emit_tool_event("error", fn_name, {"error": str(e)})
                    self.executor.stats.record_call(fn_name)
                    tool_log.append(f"{fn_name}: " + ", ".join(str(v)[:40] for v in fn_args.values()))
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": tool_content,
                    })
        
            # Check cancellation after all tool calls in this batch have been
            # processed.  This is needed because some tools (run_command,
            # run_python_script, view_image with video) are blocking and do not
            # check _cancel_requested themselves.  Without this check the flag
            # is silently lost when the loop breaks on ended_turn or
            # batch_review_max.
            if self._cancel_requested:
                return self._mark_cancelled(result)
    
            if ended_turn:
                break
            if self.policy.patch_mode == "batch_review" and len(batch_collected) >= self.policy.batch_review_max:
                break
            rounds += 1
        else:
            # Loop exhausted max_tool_rounds without end_turn/ask_user/break.
            handoff_prompt = (
                "You have used up all available tool-call rounds for this turn "
                f"(limit: {self.policy.max_tool_rounds}) before finishing the task. "
                "Stop calling tools now and instead write a plain status report to the user covering: "
                "1) what you changed so far, 2) what you did NOT get to change yet, "
                "3) exactly where you left off / which step was interrupted, "
                "4) what still needs to happen next, 5) anything the user should consider or watch out for, "
                "6) any problems or blockers you ran into. "
                "End by telling the user to reply 'continue' to resume from where you left off."
            )
            self.messages.append({"role": "user", "content": handoff_prompt})
            try:
                schemas = self.executor.active_schemas()
                followup = self.client.chat(self.messages, tools=None,
                                            enable_thinking=self.policy.enable_thinking,
                                            preserve_thinking=self.policy.preserve_thinking)
                followup_content = followup.get("content") or "(ran out of tool rounds; no summary produced)"
            except Exception as e:
                followup_content = f"(ran out of tool rounds; status summary request failed: {e})"
            self.messages.append({"role": "assistant", "content": followup_content})
            visible_text, reasoning_blocks = split_reasoning(followup_content)
            result.assistant_text = visible_text
            result.reasoning_text = "\n".join(reasoning_blocks)
            # Append to `tool_log`, not just to `result.tool_log`: the post-turn
            # block below unconditionally reassigns `result.tool_log = tool_log`,
            # which silently discarded this marker — so a turn that hit the
            # round budget looked, in the log and in the UI, like a clean finish.
            tool_log.append(
                f"[max_tool_rounds={self.policy.max_tool_rounds} reached; requested status handoff]")
            result.tool_log = tool_log

            # Note: patch flushing, auto-applied merge, web truncation, time
            # barriers, and stale reads all happen below (outside the while/else)
            # so they apply to ALL exit paths, not just max_tool_rounds.

        # ── Post-turn operations (always run, regardless of how the loop exited) ──
        # The while...else block above handles the max_tool_rounds exhaustion case
        # (handoff prompt + followup). For other exit paths (end_turn,
        # batch_review_max, no tool_calls), we still need to flush patches, merge
        # auto-applied, propagate truncation, inject time barriers, and prune reads.

        # Flush pending patches — only needed when the else block didn't already set them
        if not result.pending_patches:
            touched = getattr(self, "_touched_draft_paths", set())
            if self.draft_store and touched:
                result.pending_patches = self._generate_draft_patches(only_paths=touched)
            # Always fall back to batch_collected if draft-regeneration produced
            # nothing, rather than only doing so when touched was empty. A
            # path-normalization mismatch (or any other reason regeneration
            # comes back empty) must never silently drop an already-staged
            # patch — that's strictly worse than showing a slightly-stale one.
            if not result.pending_patches and batch_collected:
                result.pending_patches = batch_collected
                batch_collected.clear()

        # Respect batch_review_max even when patches came from the draft store
        if self.policy.patch_mode == "batch_review" and result.pending_patches:
            if len(result.pending_patches) > self.policy.batch_review_max:
                result.pending_patches = result.pending_patches[:self.policy.batch_review_max]

        # Merge in auto-applied patches that still need to appear in the Change Queue
        if self._auto_applied_patches:
            if not result.pending_patches:
                result.pending_patches = []
            result.pending_patches.extend(self._auto_applied_patches)
            self._auto_applied_patches.clear()

        # Propagate web fetch truncation info to the result
        if self._web_fetch_truncation:
            result.web_fetch_truncation = self._web_fetch_truncation
            self._web_fetch_truncation = None

        # Inject time-based barriers after turn completes
        self._inject_time_barriers()

        # Prune stale file reads if the setting is enabled
        if self.policy.prune_aggression in ("end_of_turn", "after_each_read"):
            try:
                prune_stale_file_reads(self.messages)
            except Exception:
                pass  # non-fatal; don't break the turn if pruning fails

        result.tool_log = tool_log
        self._set_activity("idle")
        return result


    def _caption_media(self, path: str, frames: List[bytes], is_video: bool) -> str:
        """Describe already-loaded image/frame bytes with the configured fallback model."""
        from . import caption_fallback
        try:
            return caption_fallback.describe_media(self.captioner, frames, path, is_video)
        except Exception as e:
            return (
                f"Could not view '{path}': no vision projector (mmproj) is loaded, and the "
                f"image-to-text fallback failed: {e}\n"
                "Tell the user the fallback model may still need to be downloaded (Settings -> Vision) "
                "or that its dependencies are missing, and continue without relying on image content."
            )

    def _recaption_last_media(self) -> str:
        """
        Re-describe the most recently viewed media as text. Used after the server
        rejects an image payload: by then the image has been stripped from the
        history, so it is re-read from disk and captioned instead.
        """
        if self.captioner is None or not self._last_viewed_media:
            return ""
        path, is_video = self._last_viewed_media
        try:
            if is_video:
                frames = self.executor.workspace.extract_video_frames(path)
            else:
                frames = [self.executor.workspace.load_viewable_image(path)]
        except Exception:
            return ""
        return self._caption_media(path, frames, is_video)

    def prune_stale_reads(self) -> None:
        """Run stale-file-read pruning on self.messages (in-place)."""
        try:
            prune_stale_file_reads(self.messages)
        except Exception:
            pass  # non-fatal

    def _strip_last_image_messages(self) -> int:
        """Replace any image_url content parts in the message history with a
        text placeholder. Called when the server rejects an image (no vision
        support), so the bad payload doesn't get resent forever on every turn."""
        stripped = 0
        for m in self.messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            new_parts = []
            changed = False
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    new_parts.append({
                        "type": "text",
                        "text": "[image omitted — current model has no vision support (no mmproj loaded)]",
                    })
                    changed = True
                    stripped += 1
                else:
                    new_parts.append(p)
            if changed:
                m["content"] = new_parts
        return stripped


class _WebBrowserKillNeeded(Exception):
    """Raised when a web tool detects an active browser that should be killed first."""
    def __init__(self, browser_type: str):
        self.browser_type = browser_type

