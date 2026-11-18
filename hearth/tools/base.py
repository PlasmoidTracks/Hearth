"""Tool base + result type."""
from __future__ import annotations
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


# How much of a tool result the chat UI shows in its chip. Deliberately generous: the
# chip body scrolls, so a small cap here only ever *hides* work from the user without
# saving them any space (DEC-061). The model-facing budget is a separate concern —
# agent.tool_output_max_chars in the output guard — and stays as it was.
UI_PREVIEW_MAX_CHARS = 4000


def ui_preview(text) -> str:
    """The text a `tool_result` event carries for display.

    Long enough that a captioner description or a file read is readable in one chip, but
    still bounded so one careless tool call cannot put a megabyte into an SSE frame.
    """
    s = str(text or "")
    return s if len(s) <= UI_PREVIEW_MAX_CHARS else s[:UI_PREVIEW_MAX_CHARS]


class DeltaThrottle:
    """Coalesce streamed progress deltas into a few SSE-sized pieces (DEC-061).

    A captioner emits token by token; forwarding each piece would mean hundreds of tiny
    frames for one description. Emit once we have ~48 chars, or when at least 0.3 s has
    passed since the last flush (so slow output still feels alive), and always drain the
    remainder with `flush()` so a partial caption never ends short.

    One helper rather than inline logic: both the attachment path
    (orchestrator.prepare_captions) and the describe_audio TOOL need exactly this, and two
    copies would drift apart the way the two cancel interfaces did (DEC-058). Lives here
    rather than in hearth/tools/__init__.py because that module imports the tools, so a
    tool importing it back would be a circular import.
    """

    __slots__ = ("_pend", "_last", "_min_chars", "_min_s")

    def __init__(self, min_chars: int = 48, min_s: float = 0.3):
        self._pend: list[str] = []
        self._last = time.time()
        self._min_chars = min_chars
        self._min_s = min_s

    def add(self, piece: str) -> str:
        """Record a delta; return the text ready to emit, or "" if it should keep buffering."""
        if not piece:
            return ""
        self._pend.append(piece)
        now = time.time()
        joined = "".join(self._pend)
        if len(joined) >= self._min_chars or (now - self._last) >= self._min_s:
            self._pend.clear()
            self._last = now
            return joined
        return ""

    def flush(self) -> str:
        """Whatever is still buffered (call before reporting completion)."""
        if not self._pend:
            return ""
        out = "".join(self._pend)
        self._pend.clear()
        return out


@dataclass
class ToolResult:
    ok: bool
    content: str                 # text fed back to the model as the tool message
    meta: dict | None = None     # extra UI info (e.g. download link)
    # Media-tool output: jail-relative image files the model should actually SEE,
    # e.g. [{"path": "media/pdf/scan_p1.png", "mime": "image/png"}]. The text in
    # `content` stays the tool message; the orchestrator delivers these images as a
    # separate multimodal user-role message right after it (DEC-052), because that
    # is the one shape proven to reach an mmproj vision model — hearth's own upload
    # path. Nothing else reads this field, so older tools are unaffected.
    images: list | None = None


class Tool(ABC):
    name: str = ""
    description: str = ""
    gated: bool = False          # default approval: True == "ask" before running
    setting_key: str = ""        # legacy coarse enable key (fallback below)
    enable_key: str = ""         # granular per-tool enable key (preferred)

    def enable_key_or_legacy(self) -> str:
        return self.enable_key or self.setting_key

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }

    @abstractmethod
    def parameters(self) -> dict:
        ...

    @abstractmethod
    async def run(self, args: dict, ctx: "ToolContext") -> ToolResult:  # noqa: F821
        ...
