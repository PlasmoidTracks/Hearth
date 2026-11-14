"""
Edit tracking: link a chat bubble to the exact raw message it represents.

Why this exists: the chat view and ``agent.messages`` are two different lists.
The chat view has bubbles with no raw counterpart (app notices, and under
"Full trace" every tool call and tool result), and the raw history has messages
with no bubble of their own (the tool-call wrapper of every call, and the tool
results that "Chat only" verbosity never renders). Lining the two up by counting
positions therefore lands on the wrong message — which is how an edit to one
bubble ended up overwriting an unrelated entry, most often the tool-call JSON of
a neighbouring call, silently corrupting the tool protocol for the next request.

Both lists are append-only and both are grouped by turn, so they can be joined
structurally instead: split the raw history into user turns, then walk the panel
and the turns together in display order — a user bubble opens the next turn, and
an assistant bubble takes the next answer slot within the current turn. Each
assistant candidate is *confirmed* by comparing the bubble's text against it, so
one stale bubble (edited in an earlier session, or a message the panel shows
differently than the raw history stores it) cannot silently slide every later
bubble off by one and mis-target all subsequent edits.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.agent_messages import is_injected_user_message

# ChatMessage kinds that stand for an assistant-role raw message. "reasoning"
# is deliberately absent: thinking text is never stored as its own raw message,
# it lives inside the assistant message it belongs to.
ASSISTANT_BUBBLE_KINDS = ("assistant", "thought")

# Bubbles that stand for a real raw message and therefore take part in the
# turn-by-turn pairing. "reasoning" is excluded (its text lives *inside* the
# assistant message, so it occupies no slot of its own), and so is "system"
# (an app notice with no raw twin). Excluding them is what makes the pairing
# walk survive a "Full trace" panel: assistant bubbles must still line up with
# the turn's raw assistant messages even when reasoning bubbles are interleaved
# between them.
PAIRED_BUBBLE_KINDS = ("user",) + ASSISTANT_BUBBLE_KINDS

# Bubbles that look like a real user turn on screen but have no raw user
# message of their own: they are the label for an approval the agent folded
# into a *tool* result (ask_user, command/script approval). The agent-side
# bubble they answer was already trimmed by the rewind, so the panel can show
# "Command execution was approved" at a point where the raw history holds no
# matching user message at all.
#
# They must be skipped rather than counted: treating one as a user turn makes
# the walk open a turn that does not exist, and the next bubble — the one the
# user actually clicked "Rewind to here"/"Edit" on — then resolves onto the
# turn after the end of the raw history. The matching list is the one the
# agent writes its approval notices with (core/agent.py), so the two lists
# stay in sync.
SYNTHETIC_USER_BUBBLE_TEXTS = frozenset({
    "Command execution was approved",
    "Execution was approved",
})

SYNTHETIC_USER_BUBBLE_PREFIXES = (
    "Command execution was denied",
    "Execution was denied",
)


def _is_synthetic_user_bubble(text: str) -> bool:
    """Whether a "user" bubble is an approval notice rather than the user speaking."""
    t = (text or "").strip()
    return (t in SYNTHETIC_USER_BUBBLE_TEXTS
            or t.startswith(SYNTHETIC_USER_BUBBLE_PREFIXES))


def _is_injected_turn(turn: List[int], raw: List[Dict[str, Any]]) -> bool:
    """Whether a turn is one the agent injected itself (patch-review results,
    time barriers, allowlist notices, tool-continuation notes).

    These are mechanically ``role="user"`` but the model wrote them, so the
    panel has no bubble for them. Counting them as user turns would push the
    walk past the end of the raw history.

    A turn whose *first* message is injected is skipped wholesale: an injected
    notice always closes a turn, so nothing the user said can hide behind it.
    """
    return bool(turn) and is_injected_user_message(raw[turn[0]])


# Kinds that exist only on screen and must never be written back into the raw
# history. Tool traces are part of the assistant/tool-call/tool-result protocol,
# so overwriting one would corrupt the next request rather than reword an answer.
DISPLAY_ONLY_KINDS = ("system", "tool_call", "tool_result", "tool_error")


def _text_of(content: Any) -> str:
    """Flatten a raw OpenAI-style ``content`` (string or part list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _split_user_turns(raw: List[Dict[str, Any]]) -> List[List[int]]:
    """Group raw indices into turns: a leading system block, then one group per
    user message plus everything that follows it.

    Injected user messages (patch-review results, time barriers, the notice
    after a rejected image) each open their own turn, exactly as they do in the
    chat view, so the turn counts of the two lists stay comparable.
    """
    turns: List[List[int]] = []
    if raw and raw[0].get("role") == "system":
        i = 0
        while i < len(raw) and raw[i].get("role") == "system":
            i += 1
        if i:
            turns.append(list(range(i)))
    for j in range(i if raw and raw[0].get("role") == "system" else 0, len(raw)):
        if raw[j].get("role") == "user":
            turns.append([j])
        elif turns:
            turns[-1].append(j)
    return turns


def _answer_indices(turn: List[int], raw: List[Dict[str, Any]]) -> List[int]:
    """Indices of the messages in a turn that can produce an answer bubble.

    Assistant messages count even when their text is empty, because that is
    exactly what a pure tool call looks like — and a pure tool call renders as
    a tool bubble, not an answer bubble. Keeping the empty ones in the list
    preserves the positional relationship between answer bubbles and the raw
    messages they came from.
    """
    return [i for i in turn if raw[i].get("role") == "assistant"]


def resolve_bubble(
    raw: List[Dict[str, Any]], kinds: List[str], texts: List[str], index: int,
) -> Tuple[Optional[int], str]:
    """Find the raw message index a chat bubble was rendered from.

    ``kinds``/``texts`` describe the whole chat panel in display order and
    ``index`` is the bubble being resolved. Returns ``(None, reason)`` when the
    bubble has no raw counterpart, so the caller can say so instead of writing
    into whatever message happened to sit at a guessed position.
    """
    if not raw or index < 0 or index >= len(kinds):
        return None, "not part of the conversation"

    kind = kinds[index]
    text = texts[index] if index < len(texts) else ""

    if kind in DISPLAY_ONLY_KINDS:
        if kind == "system":
            return None, "local notice — not part of the model's context"
        return None, ("tool trace — editing it would desync the tool protocol, "
                      "so it stays display-only")

    turns = _split_user_turns(raw)
    if not turns:
        return None, "no messages in the agent's context yet"

    # User-anchored turns only. The leading system block opens no bubble, so it
    # is dropped here and the walk below counts purely in user turns. Injected
    # turns (agent-written role="user" notices) are dropped too: they open no
    # bubble either, and counting them is what pushed the walk off the end of
    # the raw history when a rewind head sat near the tail of the conversation.
    user_turns = [tr for tr in turns
                  if _first_role(tr, raw, "user") is not None
                  and not _is_injected_turn(tr, raw)]
    if not user_turns:
        return None, "no user message in the agent's context yet"

    # Pair the panel against the raw history by walking BOTH lists in order.
    #
    # Two properties make this robust where the old position counting was not:
    #
    # 1. Only PAIRED_BUBBLE_KINDS advance the cursor. A "reasoning" bubble
    #    shares the raw assistant message of the answer it precedes and a
    #    "system" bubble has no raw twin at all, so neither may consume a slot.
    # 2. An assistant bubble whose text still matches its candidate raw message
    #    is *confirmed* against it; if the text disagrees, the candidate is
    #    re-searched within the same turn. That single check is what stops an
    #    edit made in an earlier session — a bubble whose text no longer matches
    #    its raw twin — from sliding every later bubble off by one. Without it,
    #    one stale bubble silently mis-targets all subsequent edits.
    t = -1                  # current user turn
    answer_slot = 0         # next answer bubble within the current turn
    confirmed = True        # current turn was picked by a text match, not by count

    def _advance():
        """Move to the next user turn, resetting the answer slot."""
        nonlocal t, answer_slot
        t += 1
        answer_slot = 0

    for b in range(0, index + 1):
        k = kinds[b]
        if k not in PAIRED_BUBBLE_KINDS:
            continue
        b_text = texts[b] if b < len(texts) else ""
        if k == "user" and _is_synthetic_user_bubble(b_text):
            # An approval notice has no raw user twin — see the constant. Skip
            # it so it cannot open a turn, and so a rewind head or edit that
            # lands on it falls through to the "no matching user message"
            # reason instead of indexing past the end of the raw history.
            continue
        last = (b == index)

        if k == "user":
            if t < 0 or confirmed:
                # Start a fresh turn on the first user bubble, and after any
                # turn we positively identified. A turn we only *assumed* is
                # left open, so extra agent bubbles inside it cannot push us
                # into the next user message.
                _advance()
            if t >= len(user_turns):
                return None, "no matching user message in the agent's context"
            if last:
                # ``_first_role`` returns a RAW message index, so it is the
                # answer directly — indexing ``user_turns[t]`` with it treated a
                # global index as a position inside the turn's own index list
                # and ran off the end (IndexError) for any turn whose user
                # message is not its first element.
                idx = _first_role(user_turns[t], raw, "user")
                if idx is None:
                    return None, "no matching user message in the agent's context"
                return idx, ""
            continue

        # An agent bubble: it belongs to the current turn.
        if t < 0:
            # Agent bubble before any user bubble (e.g. a restored history that
            # starts with an agent message) — treat it as opening the first turn.
            _advance()
        if t >= len(user_turns):
            return None, "no matching agent message in the agent's context"

        answers = _answer_indices(user_turns[t], raw)
        if not answers:
            return None, "no matching agent message in the agent's context"

        # Candidate: the next unanswered message, but only while it is still
        # consistent with the panel. Otherwise search this turn for the message
        # that actually carries this text.
        cand = answers[answer_slot] if answer_slot < len(answers) else None
        bubble_text = b_text or ""
        if cand is None or _text_of(raw[cand].get("content")) != bubble_text:
            cand = _match_answer(answers, raw, bubble_text)
        if cand is None:
            cand = answers[answer_slot] if answer_slot < len(answers) else answers[-1]
            confirmed = False
        else:
            confirmed = True

        answer_slot += 1
        if last:
            return cand, ""

    return None, "no matching message in the agent's context"


def _match_answer(answers: List[int], raw: List[Dict[str, Any]], text: str) -> Optional[int]:
    """The one answer message in this turn whose text equals the bubble's."""
    for i in answers:
        if _text_of(raw[i].get("content")) == text:
            return i
    return None


def _first_role(turn: List[int], raw: List[Dict[str, Any]], role: str) -> Optional[int]:
    for i in turn:
        if raw[i].get("role") == role:
            return i
    return None


def raw_cut_for_bubble(
    raw: List[Dict[str, Any]], kinds: List[str], texts: List[str], index: int,
) -> Tuple[Optional[int], str]:
    """How many leading raw messages a rewind to ``index`` should keep.

    The same resolution ``resolve_bubble`` does, but expressed as a *length* so
    a caller that is copying rather than trimming — "branch from here" — can cut
    a raw history with exactly the semantics a rewind uses:

      * rewinding to a **user** bubble keeps that user message and nothing of
        the reply that followed it, so the branch is poised to answer it again
        differently;
      * rewinding to an **assistant** bubble keeps that reply too;
      * rewinding to a **display-only** bubble (a tool trace or an app notice,
        which have no raw twin) falls back to keeping through the last user
        message, again without the assistant reply that followed.

    Returns ``(count, reason)``; ``count`` is None only when the bubble's
    position in the raw history could not be established at all, which is the
    case a caller must refuse to guess about rather than hand back a branch
    whose panel and raw history disagree about what was said.
    """
    idx, reason = resolve_bubble(raw, kinds, texts, index)
    if idx is not None:
        return idx + 1, ""
    if not is_unresolvable(reason):
        return None, reason
    if not raw:
        return None, reason
    user_indices = [i for i, m in enumerate(raw) if m.get("role") == "user"]
    if user_indices:
        return user_indices[-1] + 1, ""
    # A conversation that is nothing but its leading system message: keep the
    # prompt, cut the (empty) body.
    head = 0
    while head < len(raw) and raw[head].get("role") == "system":
        head += 1
    return head, ""


def chat_cut_for_raw(
    raw: List[Dict[str, Any]], cut: int,
    kinds: List[str], texts: List[str],
) -> int:
    """The number of leading chat bubbles whose raw counterpart is inside ``raw[:cut]``.

    A branch cut in the raw history needs a matching cut in the rendered chat
    history, and the two lists are not the same length: the rendered one carries
    display-only bubbles (tool traces, app notices) with no raw twin, and the
    raw one carries tool-call wrappers the panel never renders. So ``cut`` cannot
    simply be reused as a bubble index.

    Walks the bubbles with ``resolve_bubble`` and returns the index just past
    the last one that maps into the kept range. Trailing display-only bubbles
    belonging to a kept message (the tool trace under the answer it produced)
    are therefore kept, and everything after the cut is dropped.

    Stops at the first bubble that resolves to nothing — past that point the
    pairing walk has run off the end of the raw history and every later bubble
    is beyond the cut anyway.
    """
    best = 0
    for i in range(len(kinds)):
        idx, _reason = resolve_bubble(raw, kinds, texts, i)
        if idx is None or idx >= cut:
            break
        best = i + 1
    return best


def is_unresolvable(reason: str) -> bool:
    """Whether a resolve failure is a *known* display-only bubble rather than a
    real mismatch.

    ``resolve_bubble`` distinguishes the two by wording: a bubble that
    deliberately has no raw twin (an app notice, a tool trace, or an empty
    conversation) is described by what it is, while every other reason means
    "this bubble's position in the raw history could not be established".
    Callers may safely guess at a cut for the former; for the latter a guess is
    how the panel and the raw history end up disagreeing about what was said.
    """
    r = (reason or "").lower()
    return r.startswith("local notice") or r.startswith("tool trace") or \
        r == "no messages in the agent's context yet"


def edit_target(
    raw: List[Dict[str, Any]], kinds: List[str], texts: List[str], index: int,
) -> Tuple[Optional[int], str]:
    """Which raw message an edited bubble writes to, plus why when it can't."""
    idx, reason = resolve_bubble(raw, kinds, texts, index)
    if idx is None:
        return None, reason
    role = raw[idx].get("role")
    if kinds[index] == "user" and role != "user":
        return None, "no matching user message in the agent's context"
    if role not in ("user", "assistant"):
        return None, f"matched a {role!r} message, which is not editable"
    return idx, ""


def set_raw_text(raw: List[Dict[str, Any]], index: int, text: str) -> None:
    """Write edited text into a raw message, preserving multimodal part lists."""
    content = raw[index].get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = text
                return
        content.insert(0, {"type": "text", "text": text})
        return
    raw[index]["content"] = text


def apply_edit(
    raw: List[Dict[str, Any]], kinds: List[str], texts: List[str], index: int,
    new_text: str,
) -> Tuple[Optional[int], str]:
    """Apply a bubble edit to the raw history.

    An assistant message that carries a tool call keeps its empty text: the
    tool-call protocol needs that message to stay intact, otherwise the tool
    result that follows refers to a call that no longer exists. Typing prose
    into such a bubble is still allowed — it becomes the message's text — but
    blanking it out is not, since that would erase the answer the call belongs
    to.

    Returns ``(index, reason)``; index is None when nothing was written.
    """
    idx, reason = edit_target(raw, kinds, texts, index)
    if idx is None:
        return None, reason
    msg = raw[idx]
    if msg.get("role") == "assistant" and msg.get("tool_calls") and not new_text.strip():
        return None, ("this reply ended in a tool call, so its text cannot be "
                      "emptied — write the wording you want instead")
    set_raw_text(raw, idx, new_text)
    return idx, ""
