"""Steering queue — user speech that arrives while a turn owns the transcript.

Hearth allows one turn per conversation (see ``cancel.py``), and until now anything sent
during it was refused with a 409: "a turn is already running in this chat". That made a
mistake unfixable and a new thought unutterable until the turn ended, which on a long agent
turn is minutes. A queued steering message instead lands as ordinary user speech at the next
point where nothing is being written — between tool rounds — so the model reads it on its
VERY NEXT request without the user cancelling work that is already useful.

Why the seam is the whole design
--------------------------------
Appending a user message to an agent transcript is only safe when no tool call is awaiting
its result. Mid-round, ``assistant.tool_calls`` exists without its matching ``tool`` rows,
and inserting speech there produces exactly the malformed history llama-server rejects (and
which small models answer by hallucinating fake tool output). The orchestrator drains this
queue at the top of each agent-loop iteration: every previous round's tool results are
already appended, and no request is in flight. That single rule is why delivery lives in the
loop and not in a route.

Threading and lifetime
----------------------
* Purely in-process, keyed by conversation — same assumption as ``cancel.py`` and the rate
  limiter. A restart drops it; nothing durable is implied.
* One lock around every mutation. The GUI-equivalent here is any request handler appending
  while the loop drains, so a queue call must never race with a drain.
* Draining takes the whole queue under the lock, then writes messages OUTSIDE it: appending
  touches the DB and can be slow, and must not hold a lock a web request needs to enqueue.
* A queued message belongs to a CONVERSATION, not to a turn: if the current turn ends before
  a drain point is reached, the next turn in that conversation picks it up (the loop drains
  at the top of its first iteration too). Deleting the conversation drops the queue.
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
# conversation_id -> list of {"text": str, "attachments": list[dict]}
_queues: dict[str, list[dict]] = {}

# A queued message is stored as the client sent it (text + already-uploaded artifact refs).
# Cap both so a stuck or hostile client cannot grow an unbounded in-process queue.
MAX_QUEUE_PER_CONV = 20
MAX_TEXT_CHARS = 8000


def queue(conv_id: str, text: str, attachments: list | None = None) -> int:
    """Append one message to this conversation's steering queue.

    Returns the queue depth after appending, or -1 when the append was refused (empty text
    with no attachments, or the queue is full). Refusing rather than silently dropping is the
    point: the caller tells the user it did not land."""
    text = (text or "").strip()[:MAX_TEXT_CHARS]
    atts = list(attachments or [])
    if not text and not atts:
        return -1
    with _lock:
        q = _queues.setdefault(conv_id, [])
        if len(q) >= MAX_QUEUE_PER_CONV:
            return -1
        q.append({"text": text, "attachments": atts})
        return len(q)


def preview(conv_id: str) -> list[str]:
    """One short line per queued message, in delivery order — for the composer's row."""
    with _lock:
        return [(q["text"] or "(attachment)").splitlines()[0][:80]
                for q in _queues.get(conv_id, [])]


def pending_count(conv_id: str) -> int:
    with _lock:
        return len(_queues.get(conv_id, []))


def withdraw(conv_id: str, index: int) -> bool:
    """Remove one queued message before the model sees it.

    False when the index is stale — normal and simply means it already went through, so the
    caller should refresh rather than report a failure."""
    with _lock:
        q = _queues.get(conv_id) or []
        if 0 <= index < len(q):
            q.pop(index)
            if not q:
                _queues.pop(conv_id, None)
            return True
        return False


def drain(conv_id: str) -> list[dict]:
    """Take everything queued for this conversation (caller appends them as user messages).

    Returns the items and leaves the queue empty. The caller is the orchestrator at its safe
    seam — nothing else should call this, because delivery outside that seam can orphan the
    tool protocol."""
    with _lock:
        items = _queues.pop(conv_id, None) or []
        return items


def clear(conv_id: str) -> int:
    """Drop everything queued for a conversation (it was deleted, or the user signed out)."""
    with _lock:
        return len(_queues.pop(conv_id, None) or [])
