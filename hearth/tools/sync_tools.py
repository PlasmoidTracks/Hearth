"""Tools for working on a linked project folder (DEC-049 / CONTRACT v1.3).

Two tools, both deliberately narrow:

  sync_preview   read-only; reports what an apply would change and what blocks it
  sync_apply     writes the diff to the real folder; approval 'ask' and never 'auto'

The agent does NOT need these to edit files. Once a project is imported its files live in
the workspace under ``project/`` as ordinary jailed files, so fs_read / fs_edit / fs_write /
fs_search work on them unchanged — that is the whole point of importing rather than mounting.
These two tools exist only to answer "what differs from the real folder?" and to move an
approved result back.

The model never learns or needs the absolute path; it refers to files as ``project/...``.
"""
from __future__ import annotations

from .base import Tool, ToolResult
from .. import project_link as pl
from .. import project_apply as pa


def _fmt_paths(paths: list[str], limit: int = 25) -> str:
    if not paths:
        return "(none)"
    shown = "\n".join(f"  {p}" for p in paths[:limit])
    if len(paths) > limit:
        shown += f"\n  … and {len(paths) - limit} more"
    return shown


class SyncPreviewTool(Tool):
    name = "sync_preview"
    label = "Project sync: preview"
    description = ("Show how the workspace copy of the linked project (under project/) differs "
                   "from the real folder on disk: which files would be added, which modified, and "
                   "which are CONFLICTED because the user changed them in the real folder since "
                   "import. Read-only — it writes nothing anywhere. Conflicts must be resolved by "
                   "the user (re-import to adopt their version, or revert your edit) before "
                   "sync_apply will write anything.")
    gated = False      # read-only; admin can still flip tools.sync_preview.approval
    enable_key = "tools.sync_preview"

    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    async def run(self, args, ctx):
        try:
            link = pl.get_link(ctx.user_id)
            if not link:
                return ToolResult(False, "No project folder is linked to your workspace yet. "
                                         "An admin links one under Admin → Project.")
            cs = pl.diff_project(ctx.user_id, link)
        except pl.LinkError as e:
            return ToolResult(False, f"refused: {e}")

        from .. import audit
        audit.log(ctx.user_id, "sync_preview", {"target": link["target_abs"],
                                                 "summary": cs.summary()})
        body = (f"Linked folder: {link['target_abs']}\n"
                f"Workspace copy prefix: {link['rel_prefix']}/\n"
                f"{cs.summary()}  ({cs.unchanged} unchanged)\n\n"
                f"Would ADD ({len(cs.added)}):\n{_fmt_paths(cs.added)}\n\n"
                f"Would MODIFY ({len(cs.modified)}):\n{_fmt_paths(cs.modified)}\n\n"
                f"CONFLICTS — the real folder changed since import, applying would overwrite "
                f"the user's edits ({len(cs.conflicts)}):\n{_fmt_paths(cs.conflicts)}\n\n"
                f"Deletions in your copy are never applied ({len(cs.deleted)}): the tool only "
                f"adds and updates files.")
        if cs.conflicts:
            body += ("\n\nNothing can be applied until the conflicts are resolved. Ask the user "
                     "whether to re-import (take their version) or discard your edit.")
        elif not cs.applyable:
            body += "\n\nThe copy matches the real folder; there is nothing to apply."
        return ToolResult(True, body, meta={"conflicts": len(cs.conflicts),
                                            "added": len(cs.added),
                                            "modified": len(cs.modified)})


class SyncApplyTool(Tool):
    name = "sync_apply"
    label = "Project sync: apply to folder"
    description = ("Write the workspace copy's changes back to the user's real linked folder. "
                   "This is the ONLY action that modifies files outside the workspace, so it "
                   "always requires the user to approve this specific call. Call sync_preview "
                   "first and tell the user what will change. If any file conflicts (changed in "
                   "the real folder since import) nothing is written at all — resolve conflicts "
                   "instead of retrying. Every overwritten file is backed up first, and the "
                   "result reports a backup id the user can revert from.")
    gated = True       # always interrupt for approval; approval mode is not offered as 'auto'
    enable_key = "tools.sync_apply"

    def parameters(self):
        return {"type": "object", "properties": {
            "reason": {"type": "string",
                       "description": "what these changes do, in one line (shown to the user)"}},
            "required": []}

    async def run(self, args, ctx):
        try:
            link = pl.get_link(ctx.user_id)
            if not link:
                return ToolResult(False, "No project folder is linked to your workspace.")
            res = pa.apply_changes(ctx.user_id, ctx.settings(), link)
        except pl.LinkError as e:
            return ToolResult(False, f"refused: {e}")

        from .. import audit
        audit.log(ctx.user_id, "sync_apply", {"target": (link or {}).get("target_abs", ""),
                                              "ok": res.ok, "written": res.written[:40],
                                              "refused": res.refused[:20],
                                              "backend": res.backend})
        lines = [res.note]
        if res.written:
            lines.append(f"\nWritten ({len(res.written)}):\n{_fmt_paths(res.written)}")
        if res.refused:
            lines.append(f"\nRefused ({len(res.refused)}):\n{_fmt_paths(res.refused)}")
        if res.backup_id and res.ok:
            lines.append(f"\nBackup id: {res.backup_id}")
        return ToolResult(res.ok, "\n".join(lines),
                          meta={"written": len(res.written), "refused": len(res.refused),
                                "backup_id": res.backup_id, "backend": res.backend})
