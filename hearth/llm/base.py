"""LLM provider abstraction + stream event model (DEC-001, DEC-006).

A Provider yields StreamEvents while generating one assistant turn. Tool calls
are surfaced as `tool_request` events; the orchestrator decides whether to
execute automatically or gate them for user approval.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


# The sentinel key a provider uses when it could NOT parse the model's tool-call
# arguments as JSON (a truncated or malformed blob from an over-large write). Rather than
# dropping the call, the provider stashes the unparseable string under this single key so
# the request still flows through the pipeline with an id and a name. The orchestrator
# detects it via `malformed_args()` and refuses the call with a clear message BEFORE any
# tool runs — otherwise `args.get("path", "")` silently returns "" and the tool fails on
# something unrelated (writing to a directory → "[Errno 21] Is a directory"), which is
# exactly what hid the real cause in the reported bug.
RAW_ARGS_KEY = "_raw"


def malformed_args(args: dict) -> bool:
    """True when `args` is the provider's unparseable-JSON fallback, not real arguments.

    A genuine tool call never carries a key literally named RAW_ARGS_KEY (no schema uses
    it), so its presence — and nothing else about the value — is the marker that the JSON
    never parsed. Checked by the orchestrator before executing any tool.
    """
    return isinstance(args, dict) and RAW_ARGS_KEY in args


@dataclass
class ToolRequest:
    name: str
    args: dict
    call_id: str = ""
    # The raw (possibly truncated / invalid) JSON argument string as it arrived from
    # the provider. Kept so callers can size up what the model generated even when
    # `args` had to fall back to {"_raw": ...}; never shown to the client verbatim.
    args_raw: str = ""


@dataclass
class StreamEvent:
    type: str                       # token | reasoning | tool_request | tool_progress | progress | usage | error | done
    text: str = ""                  # for token/error/reasoning
    tool: ToolRequest | None = None  # for tool_request
    data: dict = field(default_factory=dict)  # progress/usage payloads

    @staticmethod
    def reasoning(text: str) -> "StreamEvent":
        """A chunk of the model's INTERNAL reasoning, separated from the answer.

        Two shapes reach Hearth and both are handled in ``llm/llama_server.py``: llama.cpp
        with ``--reasoning`` streams it as ``delta.reasoning_content`` (already separate),
        and a chat template that emits inline ``<think>…</think>`` gets parsed out of the
        content. It is a distinct event type rather than folded into ``token`` because the
        UI must render it differently (dimmed, collapsible) and the retention policy must be
        able to drop it from history — neither is possible once it is mixed into the answer
        stream. Never re-emitted as ``token``: a model that sees its own thinking presented
        as its answer starts imitating it in prose.
        """
        return StreamEvent("reasoning", text=text)

    @staticmethod
    def tool_progress(name: str, args_so_far: str, first_name: str = "") -> "StreamEvent":
        """Live hint that a tool call is still being GENERATED (its arguments are
        streaming in token by token). `args_so_far` is the raw JSON fragment
        accumulated so far — callers must treat it as untrusted text and only
        derive coarse stats from it (length, a first string value), never run it
        through json.loads. The orchestrator converts this into a `tool_progress`
        SSE event so the UI can show e.g. "writing cool_script.py · 1.4 KB" while
        the model is still composing the call.

        `first_name` is the first COMPLETE string value of the partial JSON — for the
        fs tools that is the path, so the chip can name the file being written instead
        of a bare byte count. It is passed through as-is (already complete, truncated by
        the caller) and is the only value besides name/chars that may reach the client;
        it also disambiguates chips when a model issues several tool calls in one
        completion (index-keyed slots).
        """
        return StreamEvent("tool_progress", data={"name": name, "chars": len(args_so_far),
                                                  "first_name": first_name})


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    async def stream(self, messages: list[dict], tools: list[dict], params: dict) -> AsyncIterator[StreamEvent]:
        """messages: OpenAI-style [{role,content,...}]. tools: JSON tool specs.
        Yields StreamEvents; must yield a final `done` event."""
        raise NotImplementedError

    async def health(self) -> dict:
        return {"ok": True, "provider": self.name}

    def capabilities(self) -> dict:
        """What this backend can actually do, as data rather than by name-matching.

        Callers used to sniff `getattr(provider, "name", "") == "llama-server"` to decide
        whether to tail a server log for prefill progress — a capability test written as
        string equality, which silently does nothing for any backend whose name differs
        (AUDIT Part B, "LLM provider abstraction"). Declare the capability here instead:
        `exposes_log_progress` is True only for a llama-server Hearth itself can read.
        """
        return {"measured_usage": False, "exposes_log_progress": False, "is_stub": False}
