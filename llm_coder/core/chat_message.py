"""
Chat message data model — the single source of truth for what has been said,
in what order, whether it's part of the agent's context, at what verbosity
level it's visible, and whether it's currently collapsed to a one-line
summary or expanded to full text.

Replaces the old dict-based `_chat_history` list in main_window.py and the
implicit state spread across `_live_response_text` / `_live_reasoning_text` /
`_stream_div_opened` / etc. One ChatMessage == one on-screen bubble == one
(optional) context entry.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Optional

_id_counter = itertools.count(1)


def bump_id_counter(last_used: int) -> None:
    """Advance the module id counter past ``last_used``.

    ``from_dict`` restores a saved id, and the shared counter knows nothing
    about it — without this, the next ``ChatMessage()`` could be handed an id
    that a just-restored bubble already holds, and the panel (which keys its
    widgets by id) would silently collapse the two into one.
    """
    global _id_counter
    try:
        last_used = int(last_used)
    except (TypeError, ValueError):
        return
    if last_used < 0:
        return
    _id_counter = itertools.count(max(last_used, next(_id_counter)) + 1)


VERBOSITY_CHAT_ONLY = 0
VERBOSITY_TOOL_SUMMARY = 1
VERBOSITY_FULL_TRACE = 2

VERBOSITY_LABELS = {
    "Chat only": VERBOSITY_CHAT_ONLY,
    "Tool summary": VERBOSITY_TOOL_SUMMARY,
    "Full trace": VERBOSITY_FULL_TRACE,
}
VERBOSITY_NAMES = {v: k for k, v in VERBOSITY_LABELS.items()}

# kind -> (prefix, color, mono, min_verbosity, default_in_context, default_collapsed)
# default_collapsed: whether a FINALIZED (non-streaming) message of this kind
# starts collapsed to a one-line summary. Tool calls/results default to
# collapsed (they're usually large and repetitive); everything else defaults
# to expanded but remains collapsible if the user wants to tidy the view.
KIND_DEFAULTS = {
    "user":         ("You",          "#e6e6e6", False, VERBOSITY_CHAT_ONLY,    True,  False),
    "assistant":    ("Agent",        "#cdeecb", False, VERBOSITY_CHAT_ONLY,    True,  False),
    "reasoning":    ("Thinking",     "#5591c7", False, VERBOSITY_CHAT_ONLY,    False, False),
    "thought":      ("Thought",      "#d19900", False, VERBOSITY_CHAT_ONLY,    True,  False),
    "tool_call":    ("Tool call",    "#d19900", True,  VERBOSITY_FULL_TRACE,   True,  True),
    "tool_result":  ("Tool result",  "#3fa34d", True,  VERBOSITY_FULL_TRACE,   True,  True),
    "tool_error":   ("Tool error",   "#e05a5a", True,  VERBOSITY_TOOL_SUMMARY, True,  False),
    "system":       ("System",       "#8a8a8a", False, VERBOSITY_TOOL_SUMMARY, False, False),
    "time_barrier": ("System",       "#8a8a8a", False, VERBOSITY_TOOL_SUMMARY, True,  False),
    # ---- deployed-subagent bubbles ----
    # The worker runs on its own model, and its words are NOT an assistant
    # message in this conversation: only its final answer ever reaches the raw
    # history, wrapped in the deploy_subagent tool result. So both kinds are
    # display-only (in_context=False), which also keeps them out of the
    # bubble<->raw pairing in core/edit_tracking.py — an extra "assistant"
    # bubble here would shift every later pairing by one and mis-target edits.
    # Visible at VERBOSITY_FULL_TRACE, the same level as the tool trace they
    # belong to.
    "subagent":         ("Subagent",        "#b48ead", False, VERBOSITY_FULL_TRACE,   False, False),
    "subagent_reason":  ("Subagent thinking", "#8f7aa8", False, VERBOSITY_FULL_TRACE, False, False),
}

# Kinds whose in_context flag the user is allowed to toggle in the UI.
# Tool calls/results/errors are structurally tied to the model's own
# tool-calling protocol — toggling them out of context independently would
# break the assistant/tool_call/tool_result adjacency the API requires, so
# they're intentionally NOT user-editable here (context compaction is the
# correct, adjacency-safe way to remove them — see context_utils.py).
EDITABLE_CONTEXT_KINDS = {"reasoning", "system", "thought"}

# Length threshold above which a message is worth showing a collapse toggle
# for, even if its kind defaults to expanded. Short messages (e.g. "Applied:
# file.py") gain nothing from a toggle and it would just be visual clutter.
_COLLAPSE_TOGGLE_MIN_LEN = 120
_SUMMARY_MAX_LEN = 140


@dataclass
class ChatMessage:
    """One bubble in the chat view."""
    kind: str
    text: str = ""
    role_label: Optional[str] = None
    color: Optional[str] = None
    in_context: bool = True
    min_verbosity: int = VERBOSITY_CHAT_ONLY
    streaming: bool = False
    # None = not explicitly toggled by the user yet -> fall back to the
    # kind's default_collapsed. True/False = explicit user choice, which
    # always wins once set (until the user toggles it again).
    collapsed: Optional[bool] = None
    # Scaffolding for the (not-yet-implemented) "scroll through generated
    # response variants" feature, mirroring modern LLM roleplay apps. When a
    # retry/regenerate produces an alternate response, sibling assistant
    # messages for the same turn can share a turn anchor and carry an
    # incrementing variant_index so the UI can group them and let the user
    # cycle between "take 1", "take 2", etc. For now every message is simply
    # variant 0; the field exists so the data model and persistence already
    # round-trip it and a later pass can build the cycling UI on top without
    # changing the on-disk schema.
    variant_index: int = 0
    id: int = field(default_factory=lambda: next(_id_counter))
    created_at: float = field(default_factory=time.time)

    @property
    def editable_context(self) -> bool:
        return self.kind in EDITABLE_CONTEXT_KINDS

    @property
    def is_mono(self) -> bool:
        return KIND_DEFAULTS.get(self.kind, KIND_DEFAULTS["system"])[2]

    @property
    def _default_collapsed(self) -> bool:
        return KIND_DEFAULTS.get(self.kind, KIND_DEFAULTS["system"])[5]

    @property
    def is_collapsed(self) -> bool:
        """Never collapsed while actively streaming — you always see live
        progress. Once finalized, an explicit user toggle always wins;
        otherwise falls back to the kind's default."""
        if self.streaming:
            return False
        if self.collapsed is not None:
            return self.collapsed
        return self._default_collapsed

    @property
    def show_collapse_toggle(self) -> bool:
        """Whether this message is worth showing a collapse/expand control
        for at all — short one-liners don't need one."""
        return self._default_collapsed or len(self.text or "") > _COLLAPSE_TOGGLE_MIN_LEN

    def toggle_collapsed(self):
        self.collapsed = not self.is_collapsed

    def summary_text(self) -> str:
        """One-line summary shown while collapsed."""
        text = (self.text or "").strip()
        lines = text.splitlines()
        first = lines[0] if lines else ""
        extra_lines = len(lines) - 1
        if len(first) > _SUMMARY_MAX_LEN:
            first = first[:_SUMMARY_MAX_LEN].rstrip() + "…"
        elif extra_lines > 0:
            first = first + "…"
        if extra_lines > 0:
            first += f"  ({extra_lines} more line{'s' if extra_lines != 1 else ''})"
        return first or "(empty)"

    @classmethod
    def create(cls, kind: str, text: str = "", **overrides) -> "ChatMessage":
        prefix, color, _mono, min_verb, in_ctx, _default_collapsed = KIND_DEFAULTS.get(
            kind, KIND_DEFAULTS["system"]
        )
        msg = cls(
            kind=kind,
            text=text,
            role_label=overrides.pop("role_label", prefix),
            color=overrides.pop("color", color),
            in_context=overrides.pop("in_context", in_ctx),
            min_verbosity=overrides.pop("min_verbosity", min_verb),
            streaming=overrides.pop("streaming", False),
            collapsed=overrides.pop("collapsed", None),
        )
        for k, v in overrides.items():
            setattr(msg, k, v)
        return msg

    def append(self, piece: str):
        self.text += piece

    def duplicate(self) -> "ChatMessage":
        """A copy of this bubble as a NEW message — fresh id and created_at.

        Needed because the panel keys its widgets by ``message.id``
        (``ChatPanel._widgets``), so re-inserting the *same* object a second
        time would overwrite the first entry's widget and leave one of the two
        bubbles unresponsive. Everything the user cares about — text, kind,
        colour, in-context flag, collapse state, variant index — is carried
        over; identity is not.
        """
        clone = ChatMessage(
            kind=self.kind,
            text=self.text,
            role_label=self.role_label,
            color=self.color,
            in_context=self.in_context,
            min_verbosity=self.min_verbosity,
            streaming=False,
            collapsed=self.collapsed,
            variant_index=self.variant_index,
        )
        return clone

    def visible_at(self, verbosity_level: int) -> bool:
        return verbosity_level >= self.min_verbosity

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "text": self.text,
            "role_label": self.role_label, "color": self.color,
            "in_context": self.in_context, "min_verbosity": self.min_verbosity,
            "collapsed": self.collapsed, "variant_index": self.variant_index,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        msg = cls.create(
            kind=d.get("kind", "system"),
            text=d.get("text", ""),
            role_label=d.get("role_label"),
            color=d.get("color"),
            in_context=d.get("in_context", True),
            min_verbosity=d.get("min_verbosity", VERBOSITY_CHAT_ONLY),
            collapsed=d.get("collapsed"),
        )
        # Old session files predate variant_index; default to 0 so they load.
        msg.variant_index = d.get("variant_index", 0)
        msg.created_at = d.get("created_at", time.time())
        # Restore the saved id. The panel keys its widgets by message.id
        # (ChatPanel._widgets), so a replayed session whose bubbles were handed
        # fresh ids could no longer address them by the id the session file
        # carries — to_dict wrote it, from_dict just never read it back.
        # Only override when a sane id is present, so hand-written or legacy
        # payloads without one keep a freshly allocated (unique) id.
        saved_id = d.get("id")
        if isinstance(saved_id, int) and not isinstance(saved_id, bool):
            msg.id = saved_id
            bump_id_counter(saved_id)
        return msg
