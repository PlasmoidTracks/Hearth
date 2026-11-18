"""In-flight turn registry — cooperative cancellation for chat turns.

Each running agent turn registers a CancelHandle under its conversation id. The
Abort button hits POST /api/chat/abort, which sets the handle's event; the
orchestrator checks `cancelled()` at safe points (between provider events and
before each tool runs) and unwinds by raising TurnAborted.

Design notes:
  * One active turn per CONVERSATION (not per user): aborting is always about a
    specific chat, and it stops a runaway loop without touching other chats.
  * Starting a new turn on the same conversation replaces the handle, so a stale
    registration can never wedge a chat into a permanently aborted state.
  * Purely in-process state (same lifetime assumption as the rate limiter). A
    restart drops everything anyway.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field


class TurnAborted(Exception):
    """Raised inside run_turn() when the user aborted this turn."""


@dataclass
class CancelHandle:
    user_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def is_set(self) -> bool:
        """`threading.Event`-compatible spelling of :attr:`cancelled`.

        Two cancel interfaces coexist in this codebase: asyncio callers use ``.cancelled``,
        while worker-thread code that was written against a plain threading.Event polls
        ``.is_set()`` — the captioner's chunk loop and its streamed HTTP read (see
        captioner._cancel_requested). Callers that hand THIS handle straight to such a helper
        — orchestrator.prepare_captions does exactly that for a directly-attached recording,
        whereas the describe_audio tool passes a ToolContext adapter instead — need the method
        to exist, or the caption dies with AttributeError before describing anything.

        Safe to poll from any thread: asyncio.Event.is_set() only inspects state and never
        touches the loop.
        """
        return self.event.is_set()

    async def wait_cancelled(self) -> None:
        """Await until cancel() is called.

        Lets a stream loop race "next provider event" against "user pressed Stop"
        so an abort takes effect immediately, rather than only at the next token
        (during a long prefill or tool there are no events to check between).
        """
        await self.event.wait()


_registry: dict[str, CancelHandle] = {}


def begin(conversation_id: str, user_id: str) -> CancelHandle:
    """Register a new turn for this conversation (replacing any previous one)."""
    h = CancelHandle(user_id=user_id)
    _registry[conversation_id] = h
    return h


def end(conversation_id: str, handle: CancelHandle) -> None:
    """Unregister, but only if it is still OUR handle (see begin())."""
    if _registry.get(conversation_id) is handle:
        _registry.pop(conversation_id, None)


def abort_conversation(conversation_id: str, user_id: str) -> bool:
    """Abort the in-flight turn for this conversation, if it belongs to user_id.

    Returns True when something was actually cancelled — callers use that to
    tell "aborted" apart from "nothing was running".
    """
    h = _registry.get(conversation_id)
    if not h or h.user_id != user_id:
        return False
    h.cancel()
    # Cooperative cancel cannot interrupt a blocking subprocess, and two tools sit in one
    # for minutes (the captioner's llama-mtmd-cli / HTTP read, sd-cli's generation). Setting
    # the handle alone would leave the machine busy after the user pressed Stop — so each
    # runner is asked to terminate its own child. Best-effort and never fatal: a failure
    # here must not stop the chat turn from aborting.
    for label, killer in (("captioner", _stop_captioner), ("diffusion", _stop_diffusion)):
        try:
            killer()
        except Exception:  # noqa: BLE001 — teardown of a helper model never blocks the abort
            pass
    return True


def _stop_captioner() -> None:
    from .captioner import get_captioner
    get_captioner().stop_active()


def _stop_diffusion() -> None:
    from .diffusion import get_diffusion
    get_diffusion().stop_active()


def is_running(conversation_id: str, user_id: str) -> bool:
    h = _registry.get(conversation_id)
    return bool(h and h.user_id == user_id)
