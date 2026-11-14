"""
Subagent: a second agent instance that runs to completion on a CLEAN context
while the deploying (main) agent blocks inside its own step() loop.

Design notes for maintainers
----------------------------
* This is deliberately NOT Agent. Agent owns per-turn UI/session state
  (_turn_complete, activity reporting, patch flushing, time barriers, session
  autosave). Entangling a nested run with that state is how you get a subturn
  that silently ends the main turn, or patches that never surface. What IS
  reused is the stateless machinery: LLMClient for inference and ToolExecutor
  for tool effects (so a subagent's writes land in the same DraftStore and
  reach the Change Queue exactly like the main agent's).
* Consequences of running nested that callers must handle:
  - the subagent's end_turn ends ONLY the subturn (never sets the parent's
    _turn_complete);
  - cancellation must be polled (see cancel_check) because the parent cannot
    return to the UI mid-deployment;
  - every draft path the subagent touched must be handed back so the parent
    merges it into its own patch-generation set, or those drafts orphan.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .context_utils import split_reasoning
from . import computer_tools
from .tools import (
    CONTROL_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    COMPUTER_TOOL_NAMES,
    SUBAGENT_TOOL_BLOCKLIST,
)

# Standalone default system prompt for deployed subagents. Intentionally NOT
# derived from the main agent's DEFAULT_SYSTEM_PROMPT: a subagent has no user
# to talk to, cannot ask questions, and is judged only on its final message,
# so the main prompt's "ask the user" / "wait for patch approval" guidance
# would actively mislead it.
SUBAGENT_DEFAULT_SYSTEM_PROMPT = (
    "You are a subagent: an autonomous worker spawned to complete ONE specific "
    "task. There is no user present and no way to ask anyone anything — you "
    "have no tool for asking questions, and nobody is reading your intermediate "
    "steps. Work autonomously from start to finish and never stop to ask.\n\n"
    "Your final message is the only thing that survives: it is handed verbatim "
    "to the agent that deployed you, and nothing else you produced is visible "
    "to it. Anything not written into that final message is lost. So the final "
    "message must be complete and self-contained: state the answer, the "
    "evidence you based it on (file paths, line numbers, exact snippets where "
    "relevant), and anything you could not determine.\n\n"
    "Work inside the sandboxed project folder using relative paths only. Use "
    "the tools actually present in your current tool list; if one you wanted is "
    "missing, work around it or report the gap, and never invent results you "
    "did not obtain.\n\n"
    "Stay strictly within the task you were given. When the task is done, "
    "produce your final answer and call end_turn with that answer as the "
    "summary. If the deployment request specifies a required structure for "
    "your answer, match that structure exactly and add nothing outside it."
)

# Appended to the subagent's initial (user) message so the return contract is
# stated where the model is about to act on it, even when a custom system
# prompt replaced the default one.
_RETURN_CONTRACT_TEMPLATE = (
    "\n\n---\n"
    "[Deployment note] You are a subagent working autonomously — there is no "
    "user to reply to you, so do not ask questions; decide and proceed. Your "
    "final message is the ONLY thing returned to the agent that deployed you. "
    "{expected}"
    "Make that final message complete and self-contained, then call end_turn."
)

_EXPECTED_BLOCK_TEMPLATE = (
    "The required shape of your final message is: {expected_return}\n"
    "Match it exactly.\n"
)

# Injected when the subagent burns its rounds without finishing (Q7): a plain
# no-tools call whose whole job is to hand back what it learned, not a status
# report addressed to a human.
_EXHAUSTION_PROMPT = (
    "You have used all tool-call rounds allotted to this deployment without "
    "finishing. Do not call any tools now. Write the single best final answer "
    "you can from the information you have ALREADY gathered — that answer is "
    "all the deploying agent will receive.\n\n"
    "Prioritize, in this order: (1) direct findings that answer the task, with "
    "file paths / line numbers as evidence; (2) partial results and what they "
    "imply; (3) specific things you checked and ruled out; (4) what you did not "
    "manage to check and what would be needed to check it. Omit pleasantries, "
    "apologies, restatements of the task, and requests to continue. "
    "If the deployment specified a required answer structure, follow it and "
    "mark unknown fields explicitly rather than inventing them."
)


@dataclass
class SubagentResult:
    """What a subagent run yields back to its deployer."""
    final_text: str = ""
    reasoning_text: str = ""
    tool_log: List[str] = field(default_factory=list)
    touched_draft_paths: List[str] = field(default_factory=list)
    auto_applied_paths: List[str] = field(default_factory=list)
    rounds_used: int = 0
    exhausted: bool = False          # hit max rounds without end_turn
    cancelled: bool = False
    error: Optional[str] = None
    generated_tokens: int = 0
    # Context-window occupancy of THIS deployment, for the Context tab's
    # subagent meter. `context_tokens` is the largest prompt the worker ever
    # sent (its high-water mark — the deployment is a single fresh window, so
    # the peak is what a user wants to see), and `context_limit` is the window
    # the worker's own server was started with (0 = unknown, e.g. an endpoint
    # adopted from outside the app). Both are per-deployment: the runner never
    # sees a previous deployment's transcript.
    context_tokens: int = 0
    context_limit: int = 0


def build_initial_message(task: str, expected_return: str = "") -> str:
    """Compose the subagent's first user message: the task plus the return
    contract. Kept separate from the system prompt so a custom system prompt
    (e.g. a character card) still gets the contract stated."""
    expected = ""
    if expected_return and expected_return.strip():
        expected = _EXPECTED_BLOCK_TEMPLATE.format(
            expected_return=expected_return.strip()
        )
    return (task or "").rstrip() + _RETURN_CONTRACT_TEMPLATE.format(expected=expected)


class SubagentRunner:
    """Runs one subagent deployment to completion. One instance per deployment.

    The deployer supplies its own client/executor (shared on purpose) plus the
    callbacks it wants forwarded to the UI, so subagent output can stream into
    the same chat distinctly tagged.
    """

    def __init__(self, client, executor, *, max_rounds: int = 8,
                 use_streaming: bool = True,
                 enable_thinking: bool = True,
                 preserve_thinking: bool = False,
                 patch_mode: str = "strict_review",
                 fetch_policy: Optional[Dict[str, Any]] = None,
                 on_delta: Optional[Callable[[str, str, str], None]] = None,
                 on_tool_event: Optional[Callable[[str, str, dict], None]] = None,
                 on_activity: Optional[Callable[[str], None]] = None,
                 cancel_check: Optional[Callable[[], bool]] = None,
                 context_limit: int = 0):
        self.client = client
        self.executor = executor
        # The worker model's own window size, handed over by the deployer (it
        # knows the subagent policy; the runner only knows a base_url). 0 means
        # unknown, and the Context tab then shows the token count without a
        # percentage.
        self.context_limit = int(context_limit or 0)
        # High-water mark of prompt occupancy for this deployment, updated by
        # _note_context_usage and copied onto the result at the end of run().
        self._context_tokens = 0
        self.max_rounds = max(1, int(max_rounds))
        self.use_streaming = use_streaming
        self.enable_thinking = enable_thinking
        self.preserve_thinking = preserve_thinking
        self.patch_mode = patch_mode
        # Passed through to web_tools.fetch_text_url, mirroring the parent's
        # Agent._web_fetch_policy — otherwise a subagent's fetches would
        # silently use different truncation limits than the main agent's.
        # truncation_mode is additionally forced to "silent" at call time
        # (see _fetch_policy_silent) since no popup can be raised mid-run.
        self.fetch_policy = fetch_policy
        self._on_delta = on_delta or (lambda piece, phase, tool_name: None)
        self._on_tool_event = on_tool_event or (lambda phase, name, payload: None)
        self._on_activity = on_activity or (lambda state: None)
        self._cancel_check = cancel_check or (lambda: False)

    # ── callback helpers ────────────────────────────────────────────────────

    def _emit_delta(self, piece: str, phase: str, tool_name: str = ""):
        try:
            self._on_delta(piece, phase, tool_name)
        except Exception:
            pass

    def _emit_tool_event(self, phase: str, name: str, payload: dict):
        try:
            self._on_tool_event(phase, name, payload or {})
        except Exception:
            pass

    def _set_activity(self, state: str):
        try:
            self._on_activity(state)
        except Exception:
            pass

    def _cancelled(self) -> bool:
        try:
            return bool(self._cancel_check())
        except Exception:
            return False

    # ── context accounting ──────────────────────────────────────────────────

    def _note_context_usage(self, messages: List[Dict[str, Any]], response) -> None:
        """Record how full the worker's own context window is.

        prompt_tokens is the server's own count of what was just sent, i.e.
        exactly the occupancy of the window we are metering — NOT total_tokens,
        which includes the reply. When the server reports nothing (older
        llama.cpp, or usage disabled), fall back to the same ~4 chars/token
        estimate the main agent's meter uses so the bar still moves.

        Kept as a high-water mark rather than "the last value": the last round
        of a deployment is often a short answer whose prompt is smaller than
        the round that nearly overflowed.
        """
        prompt_tokens = getattr(response, "prompt_tokens", None) if response else None
        if prompt_tokens:
            used = int(prompt_tokens)
        else:
            chars = 0
            for m in messages:
                content = m.get("content")
                if isinstance(content, str):
                    chars += len(content)
                elif isinstance(content, list):
                    chars += sum(len(str(p)) for p in content)
                chars += len(str(m.get("tool_calls") or ""))
            used = chars // 4
        if used > self._context_tokens:
            self._context_tokens = used
            # Ping the parent so the Context tab's subagent bar moves DURING a
            # deployment, not only after it. Emitted with tokens=None because
            # both hops of the delta path (here and Agent._subagent_stream_cb)
            # drop any delta carrying a token count, to avoid double-counting
            # the worker's generated tokens; the number therefore rides in
            # `piece`, and only a genuine new peak produces a delta.
            try:
                self._emit_delta(str(self._context_tokens), "subagent_usage_total", "")
            except Exception:
                pass

    # ── main loop ───────────────────────────────────────────────────────────

    def run(self, task: str, system_prompt: str = "",
            expected_return: str = "", context_docs: str = "") -> SubagentResult:
        """Deploy: build a clean context, run the tool loop, return the result.

        system_prompt empty/blank → SUBAGENT_DEFAULT_SYSTEM_PROMPT.
        """
        result = SubagentResult()
        sys_prompt = (system_prompt or "").strip() or SUBAGENT_DEFAULT_SYSTEM_PROMPT
        if context_docs and context_docs.strip():
            sys_prompt += "\n\n--- Project context documents ---\n" + context_docs

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": build_initial_message(task, expected_return)},
        ]

        schemas = self.executor.subagent_schemas()
        final_text = ""
        visible_text = ""
        reasoning_text = ""

        rounds = 0
        while rounds < self.max_rounds:
            # Occupancy of the prompt about to be sent. Noted before the
            # request so a cancellation or a server-side rejection mid-round
            # still leaves the meter reflecting how big the context had grown.
            self._note_context_usage(messages, None)
            if self._cancelled():
                result.cancelled = True
                result.rounds_used = rounds
                result.final_text = final_text
                break

            self._set_activity("subagent_thinking")
            try:
                if self.use_streaming:
                    response = self.client.chat_stream(
                        messages, tools=schemas,
                        on_delta=self._stream_cb,
                        cancel_check=self._cancel_check,
                        enable_thinking=self.enable_thinking,
                        preserve_thinking=self.preserve_thinking,
                    )
                else:
                    response = self.client.chat(
                        messages, tools=schemas,
                        enable_thinking=self.enable_thinking,
                        preserve_thinking=self.preserve_thinking,
                    )
            except Exception as e:
                result.error = f"Subagent model request failed: {e}"
                result.rounds_used = rounds
                result.final_text = final_text
                # The prompt that just failed is the best evidence we have of
                # how full the window was when the deployment died; without
                # this the meter reads 0 for exactly the case (an oversized
                # context) a user would most want to see.
                self._note_context_usage(messages, None)
                break

            content = response.get("content") or ""
            self._note_context_usage(messages, response)
            if response.total_tokens is not None and response.prompt_tokens is not None:
                result.generated_tokens += max(0, response.total_tokens - response.prompt_tokens)
            else:
                result.generated_tokens += max(1, len(content) // 4)

            server_reasoning = response.get("reasoning_content")
            if server_reasoning:
                visible_text = content
                reasoning_text = server_reasoning
            else:
                visible_text, blocks = split_reasoning(content)
                reasoning_text = "\n".join(blocks)

            tool_calls = response.get("tool_calls") or None
            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                # No tools requested → this message IS the answer.
                final_text = visible_text or content
                result.rounds_used = rounds + 1
                break

            ended = False
            for call in tool_calls:
                fn_name = call["function"]["name"]
                if fn_name in SUBAGENT_TOOL_BLOCKLIST:
                    # Should be unreachable (not offered), but never let a
                    # hallucinated nested-deploy or user-question stall the run.
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name,
                        "content": (
                            f"Error: {fn_name} is not available to a subagent. "
                            "No user is present to answer, and you may not deploy "
                            "another subagent. Decide from the information you "
                            "have and continue."
                        ),
                    })
                    continue

                try:
                    fn_args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name in CONTROL_TOOL_NAMES:
                    # Ends the SUBturn only — the parent keeps its own guard.
                    summary = fn_args.get("summary", "")
                    self._emit_tool_event("call", fn_name, dict(fn_args))
                    self._emit_tool_event("result", fn_name, {"summary": summary})
                    self.executor.stats.record_call(fn_name, True, "ended")
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id", ""),
                        "name": fn_name, "content": "Subagent turn ended.",
                    })
                    result.tool_log.append(f"end_turn: {summary}" if summary else "end_turn")
                    final_text = (visible_text or "").strip() or summary
                    ended = True
                    break

                tool_content = self._execute_tool(fn_name, fn_args, call, result)
                messages.append({
                    "role": "tool", "tool_call_id": call.get("id", ""),
                    "name": fn_name, "content": tool_content,
                })

            if self._cancelled():
                result.cancelled = True
                result.rounds_used = rounds + 1
                result.final_text = final_text
                break

            rounds += 1
            result.rounds_used = rounds
            if ended:
                # Publish the answer before leaving the loop. `final_text` is a
                # local, and breaking out of the `while` also skips the loop's
                # `else:` clause, so without this the end_turn summary — often
                # the worker's ONLY text, since it streams its report into the
                # summary rather than into prose — never reached the result and
                # the deployment returned an empty answer.
                result.final_text = final_text
                break
        else:
            # Q7: rounds exhausted without end_turn → force a final, no-tools
            # answer whose purpose is to hand back everything relevant.
            # _force_final_answer appends the exhaustion prompt to `messages`
            # itself, so the transcript stays coherent if anything later
            # resumes from it.
            forced = self._force_final_answer(messages)
            result.exhausted = True
            if forced:
                result.final_text = forced
            else:
                result.final_text = final_text
                if not result.error:
                    result.error = (
                        "Subagent exhausted its tool rounds without finishing, "
                        "and produced no answer when forced to summarize."
                    )

        if not (result.final_text or "").strip():
            result.final_text = (visible_text or "").strip()

        # Publish the window occupancy alongside the answer. If the worker was
        # cancelled or errored before ever completing a round, fall back to
        # estimating from the transcript we did build so the meter is not blank
        # for a deployment that died trying to grow too big.
        if not self._context_tokens:
            self._note_context_usage(messages, None)
        result.context_tokens = self._context_tokens
        result.context_limit = self.context_limit

        self._set_activity("idle")
        return result

    # ── streaming adapter ───────────────────────────────────────────────────

    def _stream_cb(self, piece: str, phase: str, tool_name: str = "", tokens=None):
        """Translate LLMClient phases into subagent-tagged phases so the UI can
        render them distinctly instead of into the main agent's bubbles.
        usage/timings are not forwarded: token accounting is rolled up by the
        parent, which would double-count live."""
        if tokens is not None:
            # ...with one exception, smuggled through with tokens=None so this
            # guard still drops the real usage/timings deltas: our own
            # context-occupancy ping, already tagged and already counted on the
            # runner side. See _note_context_usage.
            if phase == "subagent_usage_total":
                self._emit_delta(piece, phase, "", None)
            return
        mapping = {
            "content": "subagent_content",
            "reasoning": "subagent_reasoning",
            "tool_args": "subagent_tool_args",
        }
        mapped = mapping.get(phase)
        if mapped is None:
            return
        self._emit_delta(piece, mapped, tool_name or "")

    # ── forced final answer ─────────────────────────────────────────────────

    def _force_final_answer(self, messages: List[Dict[str, Any]]) -> str:
        """Ask once more with tools disabled, and keep the transcript coherent
        by appending both the exhaustion prompt and the answer. Returns "" on
        failure (caller then reports the exhaustion honestly rather than
        fabricating an answer)."""
        if self._cancelled():
            return ""
        self._set_activity("subagent_finalizing")
        try:
            resp = self.client.chat(
                messages + [{"role": "user", "content": _EXHAUSTION_PROMPT}],
                tools=None,
                enable_thinking=self.enable_thinking,
                preserve_thinking=self.preserve_thinking,
            )
        except Exception as e:
            self._emit_tool_event("error", "deploy_subagent",
                                  {"error": f"forced final answer failed: {e}"})
            return ""
        content = resp.get("content") or ""
        # This prompt (full transcript + exhaustion instruction) is normally the
        # largest of the deployment, so it has to be metered too.
        self._note_context_usage(
            messages + [{"role": "user", "content": _EXHAUSTION_PROMPT}], resp)
        visible, blocks = split_reasoning(content)
        answer = (visible or content).strip()
        if answer:
            messages.append({"role": "user", "content": _EXHAUSTION_PROMPT})
            messages.append({"role": "assistant", "content": content})
            self._emit_delta(answer, "subagent_content", "")
        return answer

    # ── tool execution ──────────────────────────────────────────────────────

    def _execute_tool(self, fn_name: str, fn_args: Dict[str, Any],
                      call: Dict[str, Any], result: SubagentResult) -> str:
        """Execute one subagent tool call and return its tool-result text.

        Effects mirror the main agent's: reads run immediately, writes build a
        PendingPatch against the shared DraftStore honouring patch_mode. The
        wording differs on purpose — a subagent must not be told a user is
        about to review its edit, since review only happens after the parent's
        turn ends.
        """
        self._set_activity(f"tool_call:{fn_name}")
        self._emit_tool_event("call", fn_name, dict(fn_args))

        try:
            if fn_name in WRITE_TOOL_NAMES:
                return self._run_write_tool(fn_name, fn_args, result)
            if fn_name == "run_python_script":
                content = self.executor.run_python_script(
                    fn_args.get("path", ""), fn_args.get("args", []), timeout=30,
                )
                result.tool_log.append(f"run_python_script({fn_args.get('path', '')})")
                self._emit_tool_event("result", fn_name, {"summary": content})
                return content
            if fn_name in ("web_search", "fetch_text_url", "extract_images",
                           "web_screen_capture"):
                return self._run_web_tool(fn_name, fn_args, result)
            if fn_name == "view_image":
                return self._run_view_image(fn_name, fn_args)
            if fn_name == "view_video":
                # Same limitation as view_image, with a hint at what works.
                path = str(fn_args.get("path", ""))
                self._emit_tool_event("call", fn_name, dict(fn_args))
                content = (
                    "Error: view_video cannot show video frames to a subagent — it has "
                    f"no image path for tool results, so '{path}' cannot be seen. "
                    "Do not retry. If the task depends on the video's visual content, "
                    "say so explicitly in your final answer; media_metadata can still "
                    "report the file's technical properties (duration, resolution, codec)."
                )
                self._emit_tool_event("result", fn_name, {"summary": content})
                return content
            if fn_name in COMPUTER_TOOL_NAMES:
                # Desktop tools return images; a subagent cannot receive them.
                try:
                    tool_result = computer_tools.run_computer_tool(fn_name, fn_args)
                except Exception as e:
                    self._emit_tool_event("error", fn_name, {"error": str(e)})
                    return f"Error in {fn_name}: {e}"
                text = tool_result.get("text", str(tool_result)) if isinstance(tool_result, dict) else str(tool_result)
                if isinstance(tool_result, dict) and tool_result.get("image"):
                    text += (
                        "\n\n[A screenshot was captured but cannot be shown to a "
                        "subagent — it has no image path for tool results. Report "
                        "the text summary only.]"
                    )
                result.tool_log.append(f"{fn_name}: " + ", ".join(str(v)[:40] for v in (fn_args or {}).values()))
                self.executor.stats.record_call(fn_name)
                self._emit_tool_event("result", fn_name, {"summary": text[:300]})
                return text
            if fn_name == "run_command":
                content = self.executor.run_command(
                    fn_args.get("command", ""), fn_args.get("description", ""),
                    timeout=fn_args.get("timeout", 30),
                )
                result.tool_log.append(f"run_command({fn_args.get('command', '')})")
                self._emit_tool_event("result", fn_name, {
                    "command": fn_args.get("command", ""), "output": content,
                    "summary": content[:300],
                })
                return content
            # Read/search/observe tools (list_*, read_*, search_*, grep_*,
            # think_out_loud, ...).
            content = self.executor.execute_read_tool(fn_name, fn_args)
            result.tool_log.append(
                f"{fn_name}: " + ", ".join(str(v)[:40] for v in (fn_args or {}).values())
            )
            if fn_name == "think_out_loud":
                # Matches Agent.step(): the thought text is the tool event
                # payload (the UI shows it in full), but the transcript only
                # records "Thought recorded." — echoing the whole thought
                # back as the tool result would double it in context.
                self._emit_tool_event("result", fn_name, {"thought": content, "summary": content})
                tool_content = "Thought recorded."
            else:
                self.executor.stats.record_call(fn_name)
                self._emit_tool_event("result", fn_name, {"summary": content})
                tool_content = content
            return tool_content
        except Exception as e:
            self._emit_tool_event("error", fn_name, {"error": str(e)})
            result.tool_log.append(f"{fn_name} FAILED: {e}")
            return f"Error: {e}"

    def _run_write_tool(self, fn_name: str, fn_args: Dict[str, Any],
                        result: SubagentResult) -> str:
        patch = self.executor.build_pending_patch(fn_name, fn_args)
        auto_apply = False
        if self.patch_mode == "full_auto":
            auto_apply = True
        elif self.patch_mode == "auto_approve_new_files" and patch.kind == "create":
            auto_apply = True

        if auto_apply:
            self.executor.apply_patch(patch)
            patch.status = "accepted"
            result.auto_applied_paths.append(patch.path)
            if self.executor.draft_store:
                self.executor.draft_store.clear(patch.path)
            self._emit_tool_event("result", fn_name, {
                "path": patch.path, "kind": patch.kind,
                "staged": False, "auto_applied": True,
            })
            result.tool_log.append(f"{fn_name}({patch.path}) [auto-applied]")
            return f"Applied automatically (mode={self.patch_mode}): {patch.path}"

        # Staged into the SHARED draft store. Report it as staged — not as
        # "waiting on user decision", which is true only after the deployer's
        # turn finishes and the parent flushes patches to the Change Queue.
        self.executor.mark_staged(patch.path, patch.kind)
        result.touched_draft_paths.append(patch.path)
        self._emit_tool_event("result", fn_name, {
            "path": patch.path, "kind": patch.kind,
            "staged": True, "auto_applied": False,
        })
        result.tool_log.append(f"{fn_name}({patch.path}) [staged]")
        return (
            f"Staged: {patch.path} ({patch.kind}). It is queued for the user's "
            "Change Queue and will be visible there when the current turn ends. "
            "Do not claim it has been applied — mention in your final message "
            "that you staged this change."
        )

    def _run_web_tool(self, fn_name: str, fn_args: Dict[str, Any],
                      result: SubagentResult) -> str:
        """Web tools for a subagent, honouring the shared web policy.

        Three nested-run constraints shape this:
        * Images cannot be delivered — a subagent's tool messages are plain
          strings, so any captured/base64 image would be silently dropped.
          We ask the backend not to fetch them and say so when one slipped
          through, rather than pretending the subagent saw it.
        * WebBrowserKillNeeded (a UI prompt) cannot be answered mid-run, so it
          is caught and reported as a normal tool error.
        * fetch_text_url truncation popups (a UI interaction) are likewise
          unavailable, so truncation mode is forced to 'silent' — the marker
          still reaches the subagent's context, just without a dialog.
        """
        from . import web_tools

        policy = self.executor.web_policy
        allow = self.executor.web_allowlist
        block = self.executor.web_blocklist
        try:
            if fn_name == "web_search":
                content = web_tools.web_search(
                    fn_args["query"], engine=fn_args.get("engine", "duckduckgo"),
                    max_results=fn_args.get("max_results", 6),
                    policy=policy, allowlist=allow, blocklist=block,
                )
            elif fn_name == "fetch_text_url":
                content = web_tools.fetch_text_url(
                    fn_args["url"], timeout=fn_args.get("timeout", 30),
                    policy=policy, allowlist=allow, blocklist=block,
                    max_fetch_policy=self._fetch_policy_silent(),
                )
            elif fn_name == "extract_images":
                res = web_tools.extract_images(
                    fn_args["url"], max_images=fn_args.get("max_images", 5),
                    policy=policy, allowlist=allow, blocklist=block,
                    fetch_images=False,
                )
                content = res.get("text", "No images extracted.")
                content += (
                    "\n\n[Only image URLs/alt text are available to a subagent — "
                    "it cannot receive image data. Use these URLs in your answer, "
                    "or fetch the page text instead.]"
                )
            else:  # web_screen_capture
                res = web_tools.web_screen_capture(
                    fn_args["url"], mode=fn_args.get("mode", "full"),
                    policy=policy, allowlist=allow, blocklist=block,
                )
                n = len(res.get("images") or [])
                content = res.get("text", "No screenshot captured.")
                if n:
                    content += (
                        f"\n\n[{n} screenshot(s) were captured but cannot be shown "
                        "to a subagent — it has no image path for tool results. "
                        "Rely on the text, or use fetch_text_url instead.]"
                    )
            self._emit_tool_event("result", fn_name, {
                "query_or_url": fn_args.get("query", fn_args.get("url", "")),
                "summary": content,
            })
            result.tool_log.append(f"{fn_name}({fn_args.get('query', fn_args.get('url', ''))})")
            self.executor.stats.record_call(fn_name)
            return content
        except Exception as e:
            # Includes web_tools.WebBrowserKillNeeded and WebPolicyError — both
            # are legitimate tool failures from a subagent's point of view.
            self._emit_tool_event("error", fn_name, {"error": str(e)})
            result.tool_log.append(f"{fn_name} FAILED: {e}")
            return f"Error: {e}"

    def _fetch_policy_silent(self) -> dict:
        """The parent's fetch limits with truncation_mode forced to 'silent'.

        The runner has no way to raise the truncation popup (that lives in the
        UI, and the parent's turn is blocked inside this deployment), so a
        non-silent mode would be misleading. Falls back to a sane default when
        the parent never supplied a policy.
        """
        fp = dict(self.fetch_policy or {})
        fp["truncation_mode"] = "silent"
        return fp


    def _run_view_image(self, fn_name: str, fn_args: Dict[str, Any]) -> str:
        """view_image cannot work for a subagent, but the failure is actionable.

        Agent.step() delivers images by injecting a follow-up *user* message
        with image parts — a trick this loop cannot reproduce, because the
        subagent has already produced its next turn by the time a tool result
        exists. Rather than the generic "Unknown read tool" error (which would
        read as a broken tool and invite retries), tell it exactly what it can
        do instead: SVG is plain XML via read_file, and the file's textual
        surroundings via search/read.
        """
        path = fn_args.get("path", "")
        is_svg = str(path).lower().endswith(".svg")
        self._emit_tool_event("call", fn_name, dict(fn_args))
        if is_svg:
            content = (
                f"view_image cannot show images to a subagent, but '{path}' is an "
                f"SVG — that is plain XML text. Call read_file('{path}') to see "
                "its full source instead."
            )
        else:
            content = (
                f"Error: view_image cannot show images to a subagent — it has no "
                f"image path for tool results, so '{path}' cannot be seen. "
                "Do not retry. If the task depends on visual content, say so "
                "explicitly in your final answer and report what you could "
                "determine from the file's name, size, and surrounding text."
            )
        self._emit_tool_event("result", fn_name, {"summary": content})
        return content
