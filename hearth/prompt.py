"""System prompt assembly + protocol injection.

The final system prompt is: editable system prompt (the editor text, or DEFAULT_PERSONA
when it is empty) + selected protocol doc + a workspace/tools preamble describing the
user's jail. Protocol and admin-doc prompts are markdown stored in the DB (see
routes_admin). This keeps prompts data-driven & auditable (DEC-007).

The editable prompt is what the sidebar "System prompt" editor edits — see
current_prompt(). Hard-coded text lives here only as a fallback, never as something
prepended on top of the user's text.

One rule this module now enforces: it must never assert a fact about the workspace listing
that the code does not guarantee. See build_workspace_preamble() and AUDIT finding #1/B3.
"""
from __future__ import annotations

DEFAULT_PERSONA = (
    "You are Hearth, a helpful private assistant running on your family's own hardware. "
    "Be concise and practical. You have tools that operate inside the current user's "
    "private workspace directory only."
)

# The listing cap used when no settings dict is supplied to build_workspace_preamble().
# Deliberately a module constant rather than an import: prompt.py must stay importable
# without a DB (see current_prompt()'s lazy import), and runtime_settings imports nothing
# from here but might someday. agent.workspace_listing_entries is the real source in the app.
DEFAULT_LISTING_CAP = 80

# The one line of the preamble that states a FACT about the listing. It used to read
# "shows every file" unconditionally while orchestrator._workspace_context cut the list at
# 80 entries — an active misinstruction, since a model told the list is complete has no
# reason to call fs_list, so a capped listing was invisible to the agent as well as the
# user. The scope is now filled in from the real cap.
_LISTING_CLAIM = (
    "- The workspace listing in this prompt shows {scope} (sizes included); use fs_list for a "
    "fresh or fuller listing and fs_read to open a file. Files under uploads/ are attachments the "
    "user sent you — read them with fs_read before asking them to paste or re-upload anything.\n"
)

# Everything in the preamble except the listing sentence, so it can be recomposed around
# whichever claim is currently true.
_PREAMBLE_HEAD = (
    "\n\n# Environment\n"
    "- You can read/write files ONLY inside this user's workspace (relative paths). "
    "Never attempt to access other users' data or absolute host paths.\n"
    "- Use tools when they help. For command execution, the tool is gated: the user must "
    "approve each command. Ask a clarifying question with ask_user when intent is unclear.\n"
)

_PREAMBLE_TAIL = (
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

# Text kept for backward compatibility (tests and any external reader). It is exactly what
# build_workspace_preamble() produces when no cap is in play — the original wording.
WORKSPACE_PREAMBLE = _PREAMBLE_HEAD + _LISTING_CLAIM.format(scope="every file") + _PREAMBLE_TAIL

# What a capped listing says inside the listing itself, so the cut is visible where the
# files are listed rather than only in a distant sentence.
_TRUNCATION_NOTE = "- …and more (the listing stops here — use fs_list for the rest)"


def listing_truncation_note() -> str:
    """The line appended to a capped prompt listing so the cut is visible in the prompt.

    Deliberately says "and more", not "and N more": a capped listing cannot know how many
    entries it omitted, and an earlier attempt at this fix produced a confidently WRONG
    number by counting one probe entry past the cap (it reported "1 more" for a directory
    with 308 hidden files). Never state a figure the code cannot support.

    Lives here rather than in the orchestrator so this marker and the preamble sentence
    above are stated together and can be checked against each other.
    """
    return _TRUNCATION_NOTE


def build_workspace_preamble(settings: dict | None = None, listing_capped: bool = False) -> str:
    """The environment preamble, worded to match what the listing ACTUALLY contains.

    `settings` supplies `agent.workspace_listing_entries`, the same value
    orchestrator._workspace_context() passes to workspaces.list_tree(); `listing_capped`
    says whether this turn's listing hit it. Three honest outcomes:

      * no cap known  -> "shows every file" (the original claim, still true uncapped)
      * cap, not hit  -> "up to N entries; if a folder is missing, look it up with fs_list"
      * cap, hit      -> "only the first N entries — it may be INCOMPLETE… call fs_list"

    Never says "every file" while a limit exists. This is the whole point of the function:
    the claim and the enforcement are computed from one number in one module.
    """
    cap = None
    if settings is not None:
        try:
            cap = int(settings.get("agent.workspace_listing_entries") or 0) or None
        except (TypeError, ValueError):
            cap = None
    if not cap:
        return WORKSPACE_PREAMBLE
    if listing_capped:
        scope = (f"only the first {cap} entries — it may be INCOMPLETE, so never treat it as "
                 f"the whole workspace; call fs_list before saying what a folder does or does "
                 f"not contain")
    else:
        scope = (f"up to {cap} entries; if something the user mentions is missing from it, look "
                 f"it up with fs_list rather than assuming it is absent")
    return _PREAMBLE_HEAD + _LISTING_CLAIM.format(scope=scope) + _PREAMBLE_TAIL


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


def assemble(system_doc: str | None, protocol_doc: str | None,
             settings: dict | None = None, listing_capped: bool = False) -> str:
    """Persona + protocol doc + environment preamble.

    `settings` / `listing_capped` are forwarded to build_workspace_preamble() so the
    listing claim matches the cap actually applied to this turn's prompt. Omitted, the
    behaviour is exactly what it always was (the uncapped wording).
    """
    parts = [system_doc.strip() if system_doc else DEFAULT_PERSONA]
    if protocol_doc:
        parts.append("\n\n# Active protocol\n" + protocol_doc.strip())
    parts.append(build_workspace_preamble(settings, listing_capped))
    return "".join(parts)
