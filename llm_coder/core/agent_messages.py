"""
Pure helpers over the raw OpenAI-style message list.

These were private static/module helpers on ``Agent`` in core/agent.py. They
are extracted because none of them touch Agent state: they are functions of a
message dict (or of a completion's prefix/overlap), which makes them testable
on their own and keeps core/agent.py focused on the turn loop.

``INJECTED_USER_PREFIXES`` used to live on the Agent class purely so
``_is_user_turn`` could reach it; it is a module constant here for the same
reason.
"""
from typing import Any, Dict, List


def message_text(msg: Dict[str, Any]) -> str:
    """Flatten a raw message's ``content`` (string or multimodal part list) to
    plain text. Tolerates a missing/None content so callers can inspect odd or
    hand-edited histories without guarding every call site."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def flatten_content(content) -> str:
    """Join the text parts of a message's content into one string.

    Accepts a plain string, a multimodal part list, or anything else (which
    yields ""), so callers never need to type-check before summarising."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def ends_turn(msg: Dict[str, Any], control_tool_names) -> bool:
    """True when this assistant message's tool calls are only control tools
    (i.e. ``end_turn``), which means the message closes a turn."""
    calls = msg.get("tool_calls") or []
    if not calls:
        return False
    names = [(c.get("function") or {}).get("name") for c in calls if isinstance(c, dict)]
    return bool(names) and all(n in control_tool_names for n in names)


# Leading markers of every user-role message the agent injects itself.
INJECTED_USER_PREFIXES = (
    "Patch review results:",
    "[System information]:",
    "[System notice]",
    "[The user has added",
    "You have used up all available tool-call rounds",
)


def is_injected_user_message(msg: Dict[str, Any]) -> bool:
    """Whether a role="user" message is one the agent wrote itself rather than
    the user speaking.

    Injected user messages (patch-review results, time barriers, the notice
    after a rejected image) are mechanically role="user" but are not the user,
    and they never follow an end_turn block -- they are appended at the very end
    of a turn. Matching on the marker keeps a barrier message from being
    mistaken for the end of a block."""
    return message_text(msg).lstrip().startswith(INJECTED_USER_PREFIXES)


# Ceiling on how much of a restatement is ever removed. If the model echoes
# more than this, it has not continued the message at all -- it has begun a
# new one -- and that is reported rather than papered over.
MAX_MERGE_OVERLAP = 400


def trim_partial_word(completion: str, matched: int) -> str:
    """Drop the first ``matched`` whitespace-collapsed chars of
    ``completion``, keeping the boundary clean.

    The character counts of the raw and normalised strings diverge (runs of
    spaces, newlines, tabs), so the walk has to consume the raw string while
    counting only non-whitespace. When the cut lands inside a word the word
    is dropped entirely -- half a word spliced onto the prefix is worse than
    a slightly short continuation.
    """
    i = 0
    seen = 0
    while i < len(completion) and seen < matched:
        if not completion[i].isspace():
            seen += 1
        i += 1
    # ``i`` now points just past the last matched character. If the next
    # character is a letter, the echo stopped mid-word.
    while i < len(completion) and completion[i].isalnum():
        i += 1
    return completion[i:].lstrip()


def merge_continuation(prefix: str, completion: str) -> str:
    """Join a continued message to what the model wrote next.

    Returns only the genuinely new text. A model that was correctly prefilled
    with the open assistant turn continues seamlessly and the completion
    starts fresh, so this returns ``completion`` unchanged.

    It exists because a prefilled model sometimes retypes a few of the words
    it was handed -- most often at a sentence boundary, or when a chat
    template makes the assistant turn ambiguous. Naively concatenating bakes
    that echo into the stored history, where it survives the session and is
    re-sent on every later turn. So the longest overlap between the tail of
    the prefix and the head of the completion is dropped.

    Matching is whitespace-insensitive and case-insensitive: the server
    re-tokenises the prefix, so the echo commonly comes back with a nudge in
    spacing ("here is" -> "here is  the rest") that an exact compare would
    miss and leave behind as a visible duplicate.
    """
    if not completion or not prefix:
        return completion
    comp = completion.lstrip()
    if not comp:
        return ""

    norm_prefix = "".join(prefix.split())
    norm_comp = "".join(comp.split())
    limit = min(len(norm_prefix), len(norm_comp), MAX_MERGE_OVERLAP)

    # Longest overlap first: the model usually restates a whole clause, and a
    # shorter match would leave the remainder of it behind.
    for n in range(limit, 0, -1):
        if norm_prefix.endswith(norm_comp[:n]):
            return trim_partial_word(comp, n)
    return completion


def has_orphaned_tool_result(messages: List[Dict[str, Any]],
                             tail: List[Dict[str, Any]]) -> bool:
    """Whether ``tail`` holds a tool result whose parent call is not in
    ``messages``.

    The tail after a continuation anchor may only be dropped if it contains no
    live tool protocol. An end_turn wrapper plus its results is pure
    bookkeeping and is safe to discard; a bare tool result would be orphaned by
    the discard, so in that case the caller keeps the tail and appends the
    continued message instead of rewriting history.

    ``messages`` is the full history (it still contains the anchor and every
    earlier assistant message); ``tail`` is ``messages[index + 1:]``.
    """
    for m in tail:
        if m.get("role") != "tool":
            continue
        call_id = m.get("tool_call_id")
        defined = any(
            c.get("id") == call_id
            for other in messages for c in (other.get("tool_calls") or [])
        )
        if not defined:
            return True
    return False
