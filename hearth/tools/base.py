"""Tool base + result type."""
from __future__ import annotations
import json
import re
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

    When it clips, the chip says so (finding #13). The two budgets are deliberately
    unlinked — the model may have been given far more than 4 000 characters — and that is
    exactly why the display cut must be labelled: without a marker, "why did it say that?"
    has no answer in the transcript, because the user cannot tell a short tool result from
    a truncated one.
    """
    s = str(text or "")
    if len(s) <= UI_PREVIEW_MAX_CHARS:
        return s
    return (s[:UI_PREVIEW_MAX_CHARS]
            + f"\n…(truncated for display — {len(s) - UI_PREVIEW_MAX_CHARS} of "
              f"{len(s)} characters not shown; the model may have seen more)")


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


# Argument shapes that count as "not provided" for a required string argument. A model
# that omits `path` sends nothing; one whose JSON got mangled (see llm/base.RAW_ARGS_KEY)
# sends an empty dict; some send "" or whitespace. All three mean the same thing to the
# tool: it cannot do its job. Normalising them here lets ONE check cover every tool rather
# than each tool rediscovering the difference between absent and empty.
_EMPTY_STRINGS = (None, "", " ")


def _arg_is_absent(value) -> bool:
    """True when a required argument is missing or an empty/whitespace string.

    Deliberately treats 0 / False / [] as PRESENT — those are legitimate values for many
    arguments; only None and blank strings count as absent. Containers are not deep-checked
    here (a tool that needs a non-empty list says so itself); this catches the common,
    silent "the field just wasn't there" case.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def validate_required(tool: "Tool", args: dict) -> str:
    """Return a model-facing error string if a required argument is missing, else "".

    Reads the tool's OWN JSON schema (`parameters()["required"]`) so hardening applies to
    every present and future tool with no per-tool code — add `required: ["path"]` to a
    schema and this catches it. Why centralised: when a provider could not parse a call's
    JSON it fell back to `{_raw: ...}`, so `args.get("path", "")` returned "" and fs_write
    tried to write *at the workspace directory* → "[Errno 21] Is a directory". That OS error
    reached the model and the operator instead of the truth ("no path was given"), and both
    went hunting for a phantom problem in the workspace. A named, required-but-absent field
    is now reported at the seam, before any tool body runs.

    The message names the missing field(s) and points at the schema — enough for the model
    to correct the call rather than repeat it.
    """
    try:
        schema = tool.parameters() or {}
    except Exception:  # noqa: BLE001 — a broken schema must not crash the check
        return ""
    required = schema.get("required") or []
    if not isinstance(required, list):
        return ""
    props = schema.get("properties") or {}
    missing = [k for k in required if _arg_is_absent((args or {}).get(k))]
    if not missing:
        return ""
    from ..errors import hint
    described = ", ".join(missing)
    shapes = ", ".join(f"{k}:{(props.get(k) or {}).get('type', 'string')}" for k in missing)
    return hint("missing_arg",
                f"missing required argument(s): {described}. This tool needs "
                f"{{{shapes}}}; the call ran with none of them.")



def diagnose_truncation(raw: str, tail_chars: int = 400) -> dict:
    """Describe an unparseable tool-call argument blob so the failure is actionable.

    Called when a provider could not parse a call's JSON (llm/base.RAW_ARGS_KEY). The
    operator's question at that moment is always one of two things — "did generation get cut
    off?" or "did the model write invalid JSON?" — and neither can be answered from the raw
    string without looking at it. This derives the cheap signals:

      chars        how much was generated before it stopped
      tail         the last `tail_chars` characters, i.e. exactly where it broke
      stopped_at   "mid-string" (an unterminated JSON value ⇒ cut off) vs
                   "at a structural point" (a complete-looking object that is still invalid
                   ⇒ malformed by the model, not truncated)
      unclosed     count of unbalanced `"` in the tail — nonzero means it died inside a value

    Never raises: this runs on the failure path, and a diagnostic must not become a second
    failure. The blob may hold arbitrary user-file content, so callers put `tail` only where
    an authorised operator will read it (the debug bubble), never into a shared audit row.
    """
    s = str(raw or "")
    tail = s[-tail_chars:] if len(s) > tail_chars else s
    try:
        json.loads(s or "{}")
        parsed_ok = True
    except Exception:  # noqa: BLE001 — by definition this is the unparseable path
        parsed_ok = False
    # Does it end inside a string value? Count quotes outside backslash escapes; an odd
    # number means one string was opened and never closed.
    unescaped = re.sub(r'\\\\.', '', s)          # drop escape pairs like \" \\ \n
    unclosed = unescaped.count('"') % 2
    stopped_at = ("mid-string — generation stopped inside an unterminated value, "
                  "which is what a token cap looks like" if unclosed
                  else "at a structural point — the JSON is complete-looking but invalid, "
                       "so it was malformed rather than cut off")
    return {"chars": len(s), "tail": tail, "stopped_at": stopped_at,
            "unclosed_string": bool(unclosed), "parsed_ok": parsed_ok}


class Tool(ABC):
    name: str = ""
    description: str = ""
    gated: bool = False          # default approval: True == "ask" before running
    setting_key: str = ""        # legacy coarse enable key (fallback below)
    enable_key: str = ""         # granular per-tool enable key (preferred)
    # Human-readable name for Admin → Tools. Belongs on the tool because a label kept in
    # runtime_settings._TOOL_LABELS had to be hand-synced with the registry: add a tool and
    # forget the entry, and the admin UI silently rendered nothing for it (AUDIT #17).
    label: str = ""

    def display_label(self) -> str:
        """The Admin label: `label` if set, else a title-cased name."""
        return self.label or (self.name or "tool").replace("_", " ").title()

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
