"""Model-facing error hints — one place that decides what to tell the model when a
tool call fails.

Why this exists: every tool used to invent its own failure prose at the raise site, so
the same class of failure ("your old_text matched twice") was worded three different
ways and two of them named no next action. A small local model that gets
`old_text matched 3 times` and nothing else almost always retries *the identical call* —
which Hearth then has to suppress (see `orchestrator._call_key`, DEC-045) — while
`old_text matched 3 times; quote a longer fragment including surrounding lines` gives it
something to do instead. The hint is the cheap fix at the point of failure, before the
repeat guard has to catch it.

Rules that keep this useful:
* **A hint states the next action, not an apology.** Every string here answers "so what
  do I do now?" in one sentence. No "unfortunately", no restating the error.
* **Never promise a retry of the same call.** The whole point is to change the arguments.
* **`hint()` keeps the original message FIRST.** Tools embed it in prose the operator has
  already tuned; the hint is appended, so existing assertions about failure text keep
  working and nothing a model quotes back can pick up the advice as file content.
* **Idempotent.** `hint()` will not stack two hints onto one message, because error
  strings here get passed through more than once on some paths (a tool result re-reported
  by the orchestrator, an approval-card prompt built from a stored payload).

Keep this module dependency-light (stdlib only) and free of DB/settings access: it is
imported by every tool and by the jail.
"""
from __future__ import annotations

# The marker every hint carries. Exported so tests and the UI can recognise an advised
# error without duplicating the prose.
HINT_PREFIX = "Hint:"

# Failure class -> the one action that actually helps. Keys are stable identifiers, not
# prose: tools reference these, so renaming a value is safe and renaming a key is not.
ERROR_HINTS = {
    # A path could not be resolved. The most common cause is a guessed or mistyped name,
    # and the workspace listing in the system prompt may be stale after another chat's
    # writes — so the advice is to look, not to guess again.
    "file_not_found": "Call fs_list on the folder (or check the spelling) before reading "
                      "again — do not guess another path.",
    # fs_edit: old_text appeared more than once and unique matching refused.
    "multi_match": "Include more surrounding lines in old_text so it matches exactly once, "
                   "or pass unique=false only if you really mean to change every occurrence.",
    # fs_edit: old_text is not in the file at all (usually quoted from memory, or the
    # file changed since the read).
    "no_match": "Re-read the file with fs_read and copy a SHORTER exact fragment from that "
                "output. Do not repeat this call unchanged.",
    # The request is well-formed but too big to serve as asked.
    "too_large": "Read it in parts with start_line/end_line instead of asking for the whole "
                 "file again.",
    # Jail escape attempt (also the answer to a genuine '../' typo).
    "escape_root": "Use paths relative to your workspace only — no '..' and no absolute "
                   "paths. fs_list '.' shows what is available.",
    # Tool exists but is switched off by the admin for this user.
    "disabled_tool": "This tool is disabled in Admin -> Tools. Do not retry it; answer "
                     "without it or tell the user which setting to enable.",
    # The call succeeded and legitimately produced nothing.
    "empty_result": "Try a broader pattern, check spelling, or fs_list the folder to "
                    "explore instead of repeating this search.",
    # Malformed tool-call JSON (raised at the provider boundary, see llm/llama_server.py).
    "malformed_args": "Resend the call with valid JSON. If a string value is large, split "
                      "it across several smaller fs_write/fs_append calls instead of one.",
    # The model asked for a line range past the end of the file.
    "bad_range": "Read from line 1, or omit start_line/end_line to read the whole file.",
}


def hint(kind: str, message: str = "") -> str:
    """Compose a failure message with its next-action hint appended.

    `message` stays first and unchanged — tools have tuned prose that tests assert on, and
    the hint must never be mistaken for part of it. Returns just the hint when no message
    is given, and returns `message` untouched when the kind is unknown or a hint is
    already present (so repeated passes cannot stack advice).
    """
    advice = ERROR_HINTS.get(kind)
    msg = (message or "").strip()
    if not advice or HINT_PREFIX in msg:
        return msg or (advice or "")
    if not msg:
        return f"{HINT_PREFIX} {advice}"
    return f"{msg}\n{HINT_PREFIX} {advice}"
