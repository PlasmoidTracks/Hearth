"""
AgentPolicy: the tunable behaviour of an Agent turn.

Split out of core/agent.py because it is pure configuration — a dataclass of
knobs read by the turn loop, the UI, and the settings dialog — whereas
core/agent.py is the orchestration itself. It also breaks a dependency:
ui/settings_dialog.py only needs the prompt constants and the patch-mode list,
and should not have to import the whole agent loop to get them.

core/agent.py re-exports this name, so ``from core.agent import AgentPolicy``
keeps working unchanged.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .agent_prompts import DEFAULT_SYSTEM_PROMPT


@dataclass
class AgentPolicy:
    patch_mode: str = "strict_review"
    batch_review_max: int = 5
    max_tool_rounds: int = 6
    context_overflow_policy: str = "warn_and_ask"  # warn_and_ask | auto_expand | auto_compact | abort
    stream_to_chat: bool = True  # whether to stream response text into chat as it's generated
    auto_expand_percent: int = 20
    reasoning_retention: str = "keep_last"
    chat_render_mode: str = "markdown"
    # UI-only knob (read by ui/chat_panel.py, not by the turn loop): follow the
    # conversation to the bottom on every new message / streamed chunk. Off by
    # default, matching the panel's long-standing "never move the user's
    # viewport" behaviour.
    auto_scroll_to_bottom: bool = False
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    enable_thinking: bool = True
    preserve_thinking: bool = False

    script_run_mode: str = "notify"  # "notify" | "always_allow"
    script_timeout: int = 10

    command_run_mode: str = "notify"  # "notify" | "always_allow"
    command_timeout: int = 30
    command_allowlist: List[Dict[str, str]] = field(default_factory=list)

    # Draft store settings
    merge_gap: int = 15           # hunks closer than this (in lines) get merged into one region
    bulk_threshold: float = 0.5   # fraction of file changed; above this, bulk into one patch

    # Time-based auto-injection barriers
    # List of {"seconds": int, "message": str} barriers.
    # When wall-clock time since turn start crosses a barrier, a system message
    # is injected after the turn completes, informing the model of elapsed time
    # and appending the custom message for that barrier.
    time_barriers: List[Dict[str, Any]] = field(default_factory=list)  # [{"seconds": 120, "message": "..."}, ...]
    time_barriers_enabled: bool = False  # master toggle for the feature

    # Prune stale file reads: controls when old read_file/read_multiple_files
    # calls are removed from context once a file is re-read, keeping only the
    # newest read. This saves context at the cost of the model needing to
    # re-read pruned file content.
    # "off" — never prune
    # "end_of_turn" — prune after all tool calls in a turn complete (current default)
    # "after_each_read" — prune immediately after each read_file/read_multiple_files call
    prune_aggression: str = "off"

    # Delete tool returns keep count: controls how many recent tool results to
    # keep per unique key when compacting with delete_tool_returns.
    # 0 = remove all (old behavior), 1 = keep only the latest per file/URL, etc.
    delete_tool_keep_count: int = 1

    # How the "Continue" button extends the agent's last message.
    #
    # There is exactly one mode, because there is exactly one right way to do
    # this: make the message the last turn in the history and let llama.cpp
    # prefill it (``--prefill-assistant``, on by default). The model then
    # continues the text as a pure language-modelling task -- no injected
    # instruction, no "please keep writing" prompt, nothing railroading it.
    #
    # For the prefill to actually work, the request MUST be sent without tools
    # and without chat_template_kwargs (enable_thinking, etc.).  Both of those
    # change how the Jinja chat template renders the assistant turn, which
    # breaks the open-prefix structure that llama.cpp needs to continue the
    # message.  The _call_model closure in step() handles this by sending a
    # bare request (no tools, no kwargs) for the first model call of a
    # continuation turn.
    #
    # This used to offer a "nudge" mode that appended a hidden user message
    # telling the model to carry on. It is gone. It was never what made
    # continuation work (the merge into the anchor ran either way), and quoting
    # the previous message back at the model caused it to echo that quote, which
    # is the "sure, here is sure, here is ..." duplication the button was
    # reported for. If a server refuses to prefill, Continue now says so instead
    # of quietly substituting a prompt trick.
