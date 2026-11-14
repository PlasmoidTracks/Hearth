"""System prompt assembly + protocol injection.

The final system prompt is: editable system prompt (the editor text, or DEFAULT_PERSONA
when it is empty) + selected protocol doc + a workspace/tools preamble describing the
user's jail. Protocol and admin-doc prompts are markdown stored in the DB (see
routes_admin). This keeps prompts data-driven & auditable (DEC-007).

The editable prompt is what the sidebar "System prompt" editor edits — see
current_prompt(). Hard-coded text lives here only as a fallback, never as something
prepended on top of the user's text.
"""
from __future__ import annotations

DEFAULT_PERSONA = (
    "You are Hearth, a helpful private assistant running on your family's own hardware. "
    "Be concise and practical. You have tools that operate inside the current user's "
    "private workspace directory only."
)

WORKSPACE_PREAMBLE = (
    "\n\n# Environment\n"
    "- You can read/write files ONLY inside this user's workspace (relative paths). "
    "Never attempt to access other users' data or absolute host paths.\n"
    "- Use tools when they help. For command execution, the tool is gated: the user must "
    "approve each command. Ask a clarifying question with ask_user when intent is unclear.\n"
    "- The workspace listing in this prompt shows every file (sizes included); use fs_list for a "
    "fresh listing and fs_read to open a file. Files under uploads/ are attachments the user sent "
    "you — read them with fs_read before asking them to paste or re-upload anything.\n"
    "- Writing a file (fs_write / fs_append) SAVES it in the workspace and lists it under the "
    "user's Artifacts panel, but it does NOT give the user a download button. Only the `upload` "
    "tool offers a file for download. So: when the user asks to receive/download a file you wrote "
    "or edited, call upload with that path — once — and then just say it is ready. Do not offer a "
    "download they did not ask for, and do not describe a download step you did not perform.\n"
    "- fs_read returns the exact bytes of a file (no line numbers, no decoration). Quote old_text "
    "for fs_edit straight from that output. If fs_edit says old_text was not found, do NOT repeat "
    "the same call: re-read the file and copy a shorter, exact fragment, or answer without editing. "
    "Never call the same tool with the same arguments twice in one turn.\n"
)


def current_prompt(user_prompt: str | None = None) -> str:
    """The editable system prompt shown/edited in the sidebar "System prompt" panel.

    Resolution order (first non-empty wins): the user's personal prompt, then the
    admin default system-prompt doc, then DEFAULT_PERSONA. The editor loads this so
    it always shows the text actually in effect — previously it showed only the raw
    personal field (empty for most users), which made the built-in persona look like
    an uneditable prompt hiding behind it.
    """
    if (user_prompt or "").strip():
        return user_prompt.strip()
    try:  # lazy import keeps prompt.py DB-free for pure unit tests
        from .db import session_scope
        from .models import Doc

        with session_scope() as s:
            doc = (s.query(Doc)
                   .filter(Doc.kind == "system_prompt", Doc.is_default == True)  # noqa: E712
                   .order_by(Doc.updated_at.desc())
                   .first())
            if doc and (doc.content or "").strip():
                return doc.content.strip()
    except Exception:  # noqa: BLE001 — no DB context: fall back to the built-in text
        pass
    return DEFAULT_PERSONA


def assemble(system_doc: str | None, protocol_doc: str | None) -> str:
    parts = [system_doc.strip() if system_doc else DEFAULT_PERSONA]
    if protocol_doc:
        parts.append("\n\n# Active protocol\n" + protocol_doc.strip())
    parts.append(WORKSPACE_PREAMBLE)
    return "".join(parts)
