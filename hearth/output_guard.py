"""Output guard: cap how large a single tool result may be before it reaches the model.

Why this exists: several tools deliberately return *everything* — ``fs_read`` with no line
range, ``exec`` stdout/stderr, ``pdf_read`` text, a whole uploaded file. One careless call
can drop tens of thousands of characters into the context window at once, evicting real
conversation and costing a full re-prefill. The guard is the operator's backstop: a
character budget per tool result, enforced after the tool has run.

Design notes for maintainers
----------------------------
* **Characters, not tokens.** Deliberate: no tokenizer, no model-specific assumption, and
  it is what a person can reason about when they set the number in Admin → Agent.
* **The guard never fails a call.** The tool ran; its work is real. What comes back is the
  head of the output plus a marker stating how much was dropped — losing data is bad, so
  the loss is announced rather than hidden.
* **Only plain-text content is touched.** A multimodal ``content`` list carries base64
  image parts; slicing one would corrupt the payload and break vision delivery outright,
  so lists are returned untouched (and reported as not guarded, so callers never believe an
  image was clipped).
* **Placement matters.** Enforced in ``orchestrator._run_tool`` — the single seam where a
  tool result becomes the message sent to the model. One place covers every current and
  future tool, and tools keep their "describe, don't mutate" contract.
* **It must not fight DEC-045.** ``fs_read`` returns verbatim file bytes so that
  ``fs_edit.old_text`` can be quoted from them. A clipped read therefore says explicitly
  that the tail is missing and how to get it (``start_line``/``end_line``) — otherwise a
  model edits against text it never fully saw. The cap defaults high enough that ordinary
  reads are never touched; only unusually large ones are.

Keep this module dependency-light (stdlib only, no DB/settings import) so it is importable
and testable alone, like ``hearth/errors.py``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# Marker prefix of every guard note. Exported because the UI and tests must be able to
# recognise a guarded result without duplicating prose.
GUARD_MARKER = "[OUTPUT GUARD]"

DEFAULT_MAX_CHARS = 24_000

# Floor on the cap. Below this a guard note would be most of the message and the result
# useless; 200 chars still leaves a usable preview plus the marker.
MIN_MAX_CHARS = 200

DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": True,
    "max_chars": DEFAULT_MAX_CHARS,
    # Tools whose whole purpose is to hand over content verbatim. Empty by default: the
    # operator can un-exempt anything, and exempting fs_read here would hide the fact that
    # a huge read is what filled the window.
    "exempt_tools": [],
}

# The model-facing warning. Tool-agnostic by design: the guard sits below every tool and
# cannot know why the output was large. It states the facts (size, cap) and the two actions
# that actually help — narrow the call, or work from what is here.
_GUARD_ADVICE = (
    "Do not repeat this call expecting more: the tool returned everything it had. Narrow "
    "the original call instead (a line range with start_line/end_line, a tighter pattern, "
    "a subdirectory, a smaller page/limit) or work from what is shown here."
)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        return default
    return bool(value)


def normalize_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a settings slice into a well-formed guard policy.

    Runtime settings round-trip numbers as strings and booleans inconsistently, so every
    field is coerced here rather than trusted at use time. Junk or out-of-range values fall
    back to the defaults instead of producing a guard that clips every call to zero.
    """
    src = dict(policy or {})
    out: Dict[str, Any] = {}
    out["enabled"] = _as_bool(src.get("enabled"), DEFAULT_POLICY["enabled"])
    try:
        max_chars = int(src.get("max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    out["max_chars"] = max_chars if max_chars >= MIN_MAX_CHARS else DEFAULT_MAX_CHARS
    exempt = src.get("exempt_tools")
    if isinstance(exempt, str):
        exempt = [s.strip() for s in exempt.replace("\n", ",").split(",")]
    out["exempt_tools"] = sorted({str(t).strip() for t in (exempt or []) if str(t).strip()})
    return out


def policy_from_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the policy from a Hearth runtime-settings snapshot (``tools.*`` keys)."""
    s = settings or {}
    return normalize_policy({
        "enabled": s.get("agent.tool_output_guard"),
        "max_chars": s.get("agent.tool_output_max_chars"),
        "exempt_tools": s.get("agent.tool_output_exempt_tools"),
    })


def applies(policy: Optional[Dict[str, Any]], tool_name: str = "") -> bool:
    """Whether the guard is active for ``tool_name``. Cheap predicate for callers that
    want to skip the work entirely (and for tests): enabled, and not exempted."""
    p = normalize_policy(policy)
    if not p["enabled"]:
        return False
    return (tool_name or "") not in set(p["exempt_tools"])


def note(max_chars: int, original_len: int, tool_name: str = "") -> str:
    """The truncation notice appended to a clipped result.

    States both numbers the model needs — what it got and what actually existed — plus the
    generic (tool-agnostic) guidance from ``_GUARD_ADVICE``."""
    dropped = max(0, original_len - max_chars)
    return (
        f"\n\n{GUARD_MARKER} Output truncated: showing the first "
        f"{max_chars:,} of {original_len:,} characters ({dropped:,} not shown)."
        + (f" Tool: {tool_name}." if tool_name else "")
        + f" {_GUARD_ADVICE}"
    )


def guard_text(text: str, max_chars: int, tool_name: str = "") -> Tuple[str, bool]:
    """Clip ``text`` to ``max_chars`` characters, appending a notice when clipped.

    Returns ``(content, was_clipped)``. Unclearable cases are left alone: an empty/short
    string, a non-positive cap, or text already carrying a guard note — so re-running a
    stored transcript through the guard can never stack markers onto one another."""
    if not isinstance(text, str) or max_chars <= 0 or len(text) <= max_chars:
        return text, False
    if GUARD_MARKER in text[:2000]:
        return text, False
    return text[:max_chars] + note(max_chars, len(text), tool_name), True


def guard_message_content(content: Any, policy: Optional[Dict[str, Any]],
                          tool_name: str = "") -> Tuple[Any, bool]:
    """Guard one message ``content`` value. Multimodal lists pass through untouched.

    Returns ``(content, was_guarded)``. A list is *never* sliced: its image parts are
    base64 payloads and cutting one corrupts vision delivery — and it is reported as not
    guarded so no caller claims a clip that did not happen."""
    p = normalize_policy(policy)
    if not p["enabled"] or (tool_name or "") in set(p["exempt_tools"]):
        return content, False
    if not isinstance(content, str):
        return content, False
    return guard_text(content, p["max_chars"], tool_name)


def guard_tool_result(result: Any, policy: Optional[Dict[str, Any]],
                      tool_name: str = "") -> Tuple[Any, bool]:
    """Guard a ``ToolResult`` in place-safe fashion: returns a NEW result when clipped.

    Only ``content`` is considered. ``meta`` and ``images`` are structural (download links,
    jail-relative image paths) and never truncated — cutting a path string would produce a
    broken link the UI then renders."""
    p = normalize_policy(policy)
    if not applies(p, tool_name):
        return result, False
    content = getattr(result, "content", None)
    if not isinstance(content, str):
        return result, False
    clipped, was = guard_text(content, p["max_chars"], tool_name)
    if not was:
        return result, False
    try:
        from .base import ToolResult  # local import keeps this module standalone-testable
    except Exception:  # noqa: BLE001 — outside the package (pure unit test): duck-type
        return type(result)(ok=getattr(result, "ok", True), content=clipped,
                            meta=getattr(result, "meta", None),
                            images=getattr(result, "images", None)), True
    return ToolResult(ok=getattr(result, "ok", True), content=clipped,
                      meta=getattr(result, "meta", None),
                      images=getattr(result, "images", None)), True


def log_line(tool_name: str, original_len: int, max_chars: int) -> str:
    """Human-readable audit line for one guarded result."""
    return (f"tool_output_guarded tool={tool_name or '?'} original={original_len} "
            f"cap={max_chars}")
