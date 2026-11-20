"""Filesystem tools — all scoped to the current user's jail via workspaces.py.

  fs_read     read a text file (whole file, or a 1-based inclusive line range)
  fs_list     list files/dirs under a relative path
  fs_search   grep/glob across the workspace (regex over text files)
  fs_write    create/overwrite a text file in the workspace
  fs_append   append text to a file (created if missing)
  fs_edit     search-and-replace inside one file (unique match by default)
  fs_delete   delete a file (approval 'ask' by default)
  fs_move     rename/move within the jail
  zip_pack    bundle several files into one .zip artifact
  upload      offer an existing workspace file to the user (the ONLY download button)

The LLM sees only relative paths; absolute/traversal attempts raise JailEscape,
which we convert to a tool error (never a crash). Line ranges are 1-based and
inclusive on both ends (start_line=1,end_line=10 -> first ten lines); omitting
them reads the whole file. Reads are always VERBATIM — no line numbers or other
decoration, because fs_edit matches literally and quoted decoration never matches.

Download policy (DEC-045): writing/appending/editing a file registers it as a
durable artifact (Artifacts panel) but returns no `meta.download`. Only tools whose
whole purpose is handing a file to the user — `upload` and `zip_pack` — return one,
which is what the UI turns into an ⬇ button.
"""
from __future__ import annotations

import fnmatch
import os
import re
import zipfile

from .base import Tool, ToolResult
from ..errors import hint
from .. import workspaces as ws


def _int_setting(ctx, key: str, fallback: int) -> int:
    """Read a positive integer runtime setting, falling back when unset or junk.

    One helper because several filesystem limits moved out of class attributes into
    Admin → Agent, and each needs the same tolerance for a missing/partial settings dict
    (tools are also driven directly from tests with hand-built contexts).
    """
    try:
        v = int(ctx.setting(key) or 0)
    except (TypeError, ValueError, AttributeError):
        return fallback
    return v if v > 0 else fallback


def _fs_list_cap(ctx) -> int:
    """Max entries one fs_list call returns (agent.fs_list_entries, default 500)."""
    return _int_setting(ctx, "agent.fs_list_entries", 500)


class FsReadTool(Tool):
    name = "fs_read"
    label = "Filesystem read"
    description = ("Read a UTF-8 text file from the user's workspace (relative path). Optionally "
                   "limit to a line range: start_line/end_line are 1-based and INCLUSIVE; omit "
                   "both to read the whole file.")
    setting_key = "tools.fs_read"
    enable_key = "tools.fs_read"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "relative path, e.g. notes/todo.md"},
            "start_line": {"type": "integer", "description": "first line to read (1-based). Default 1."},
            "end_line": {"type": "integer", "description": "last line to read (inclusive). Default: end of file."}},
            "required": ["path"]}

    async def run(self, args, ctx):
        try:
            text = ws.read_text(ctx.user_id, args.get("path", ""))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('path')}"))
        sl, el = args.get("start_line"), args.get("end_line")
        if sl is None and el is None:
            return ToolResult(True, text)
        try:
            start = max(1, int(sl or 1))
            end = int(el) if el is not None else 10 ** 12
        except (TypeError, ValueError):
            return ToolResult(False, "start_line/end_line must be integers")
        lines = text.splitlines()
        total = len(lines)
        # Out-of-range start is an ERROR, not an ok empty read. Answering "(range beyond
        # end of file)" with ok=True told the model nothing about how long the file is, so
        # it kept guessing line numbers forever; naming the real size gives it the one fact
        # needed to correct the call.
        if start > total:
            return ToolResult(False, f"start_line {start} is beyond the end of the file "
                                     f"({total} lines). Read from line 1, or omit "
                                     f"start_line/end_line for the whole file.")
        # Clamp instead of failing: an over-large end_line means "to the end", which is a
        # reasonable intent and costs nothing to honour.
        sel = lines[start - 1:min(end, total)]
        # Return the lines VERBATIM. We used to prefix them with their line numbers
        # ("1: int main() {") so the model could quote positions back — but fs_edit
        # matches literally, and models quoted those prefixes straight into old_text,
        # producing a no-match, a re-read, and an infinite read/edit loop on small
        # models (DEC-045). A range read must give exactly what is in the file.
        #
        # Nothing may be spliced into this string — not a header, not a trailing note.
        # `ToolResult.meta` is NOT sent to the model (the orchestrator uses it for UI
        # chips only), so "is there more file?" cannot be communicated in-band safely:
        # any marker we add becomes text a model will quote back into fs_edit. A range
        # read therefore reports exactly the lines asked for and nothing else; knowing
        # the file's size is what `fs_list`/`wc`-style info and the follow-up read are for.
        body = "\n".join(sel)
        return ToolResult(True, body)


class FsListTool(Tool):
    name = "fs_list"
    label = "Filesystem list"
    description = "List files and directories in the user's workspace (relative path; '.' for root)."
    setting_key = "tools.fs_read"      # legacy fallback key
    enable_key = "tools.fs_list"       # granular key (falls back to tools.fs_read)

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "relative dir, default '.'"}},
            "required": []}

    async def run(self, args, ctx):
        # The prompt listing is capped too, so this tool is the fallback the model uses to
        # see the rest. Returning exactly `cap` entries with no note was the second layer of
        # the same lie: on a big folder both the prompt and its escape hatch stopped silently.
        # Whether anything was omitted comes from list_tree's own `.truncated` flag — counting
        # probe entries cannot yield a total (fs_search already announces its cap; this matches).
        cap = _fs_list_cap(ctx)
        try:
            entries = ws.list_tree(ctx.user_id, args.get("path", "."), max_entries=cap)
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('path')}"))
        truncated = bool(getattr(entries, "truncated", False))
        if not entries:
            return ToolResult(True, "(empty)")
        lines = [f"{'DIR ' if e['is_dir'] else '     '}{e['name']}"
                 + (f"  {e['size']}B" if e['size'] is not None else "") for e in entries]
        if truncated:
            lines.append(f"(listing truncated at {cap} entries — more exist here; narrow with a "
                         f"subdirectory path, or raise Admin → Agent → fs_list max entries)")
        return ToolResult(True, "\n".join(lines),
                          meta={"truncated": truncated, "entries": len(entries)})


class FsWriteTool(Tool):
    name = "fs_write"
    label = "Filesystem write/overwrite"
    description = ("Write/overwrite a UTF-8 text file in the user's workspace (relative path). "
                   "WARNING: replaces the entire file — prefer fs_edit for small changes. The file "
                   "is saved and listed under Artifacts, but this tool does NOT give the user a "
                   "download button; call upload with the same path when they asked for a download.")
    gated = False  # writes are within the user's own jail; not gated by default
    setting_key = "tools.fs_write"
    enable_key = "tools.fs_write"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}},
            "required": ["path", "content"]}

    async def run(self, args, ctx):
        try:
            rel = ws.write_text(ctx.user_id, args.get("path", ""), str(args.get("content", "")))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        # Register as a durable artifact so the file shows in the session's Artifacts
        # panel, but do NOT hand back meta.download: writing a file is not offering it.
        # Only the `upload` tool produces the in-transcript ⬇ button (operator policy,
        # DEC-045) — otherwise every write stacked a button the user never asked for.
        # Bookkeeping failure must not fail the write itself, but it must be VISIBLE
        # (surfaced in meta.note + audit) rather than silently swallowed.
        from .. import artifacts
        try:
            artifacts.record(ctx.user_id, rel, conversation_id=ctx.conversation_id,
                             kind="produced")
            return ToolResult(True, f"wrote {rel}")
        except Exception as e:  # noqa: BLE001 — write succeeded; artifact bookkeeping failed
            from .. import audit
            audit.log(ctx.user_id, "artifact_record_failed", {"path": rel, "err": str(e)[:200]})
            return ToolResult(True, f"wrote {rel} (note: not registered as a downloadable artifact)",
                              meta={"note": "artifact_record_failed"})


class UploadTool(Tool):
    name = "upload"
    label = "Offer file for download"
    description = ("Give the user a download button for a file in their workspace — including one "
                   "you just wrote. This is the ONLY tool that produces a download link in the "
                   "chat, so call it whenever (and only when) the user asked to receive a file. "
                   "The file itself stays listed under Artifacts even without this call.")
    setting_key = "tools.fs_write"     # legacy fallback key
    enable_key = "tools.upload"        # granular: offering files != overwriting them

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace-relative file to offer for download"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        try:
            p = ws.resolve_in_jail(ctx.user_id, args.get("path", ""), allow_missing=False)
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('path')}"))
        # Durable artifact row (survives restarts; listed in the artifacts panel).
        from .. import artifacts
        art = artifacts.record(ctx.user_id, ws.rel_from_jail(ctx.user_id, p),
                               conversation_id=ctx.conversation_id, kind="produced")
        return ToolResult(True, f"ready for download: {art['filename']}",
                          meta={"download": {"name": art["filename"], "url": art["download_url"],
                                             "artifact_id": art["id"]}})


# ---- New granular edit tools (harness-style) --------------------------------

class FsAppendTool(Tool):
    name = "fs_append"
    label = "Filesystem append"
    description = ("Append text to the END of an existing workspace file (created if missing). "
                   "Use instead of fs_write when you only want to add content.")
    enable_key = "tools.fs_append"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "text appended verbatim"}},
            "required": ["path", "content"]}

    async def run(self, args, ctx):
        try:
            rel = ws.append_text(ctx.user_id, args.get("path", ""), str(args.get("content", "")))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        # Same policy as fs_write: register the file (Artifacts panel) but offer no
        # download button — only `upload` does that. See DEC-045.
        from .. import artifacts
        try:
            artifacts.record(ctx.user_id, rel, conversation_id=ctx.conversation_id,
                             kind="produced")
        except Exception:  # noqa: BLE001 — append succeeded; bookkeeping is best-effort
            pass
        return ToolResult(True, f"appended to {rel}")


class FsEditTool(Tool):
    name = "fs_edit"
    label = "Filesystem edit (find/replace)"
    description = ("Replace an exact text segment inside one workspace file. The old_text must "
                   "match exactly (whitespace included) and by default must be UNIQUE in the "
                   "file — include more surrounding context if it matches several places. Set "
                   "replace_all=true to replace every occurrence. fs_read returns raw file text "
                   "with NO line-number prefixes, so quote old_text straight from it.")
    enable_key = "tools.fs_edit"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "exact text to find"},
            "new_text": {"type": "string", "description": "replacement text"},
            "replace_all": {"type": "boolean", "description": "replace every match (default false = unique required)"}},
            "required": ["path", "old_text", "new_text"]}

    async def run(self, args, ctx):
        try:
            res = ws.edit_text(ctx.user_id, args.get("path", ""),
                               str(args.get("old_text", "")), str(args.get("new_text", "")),
                               replace_all=bool(args.get("replace_all")))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('path')}"))
        except ValueError as e:
            # no-match / not-unique messages from workspaces.edit_text. Classify by the
            # wording it raises so each gets its own next action: "matches N places" needs
            # MORE context, "not found" needs a fresh read — and both must explicitly say
            # "do not repeat this call unchanged", because re-issuing the identical edit is
            # the loop DEC-045's repeat guard exists to break.
            text = str(e)
            kind = "multi_match" if "matches" in text else "no_match"
            return ToolResult(False, hint(kind, text))
        n, rel = res
        from .. import artifacts
        try:
            artifacts.record(ctx.user_id, rel, conversation_id=ctx.conversation_id, kind="produced")
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(True, f"edited {rel} ({n} replacement{'s' if n != 1 else ''})")


class FsDeleteTool(Tool):
    name = "fs_delete"
    label = "Filesystem delete"
    description = ("Delete a file from the user's workspace. This is irreversible — approval is "
                   "required by default.")
    gated = True   # ask by default (admin can flip tools.fs_delete.approval)
    enable_key = "tools.fs_delete"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string"},
            "reason": {"type": "string", "description": "why it should be deleted (shown to user)"}},
            "required": ["path"]}

    async def run(self, args, ctx):
        try:
            ok = ws.delete_path(ctx.user_id, args.get("path", ""))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        if not ok:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('path')}"))
        from .. import audit
        audit.log(ctx.user_id, "fs_delete", {"path": args.get("path")})
        return ToolResult(True, f"deleted {args.get('path')}")


class FsMoveTool(Tool):
    name = "fs_move"
    label = "Filesystem move/rename"
    description = ("Rename or move a file/folder within the user's workspace (both paths "
                   "workspace-relative). Overwrites the destination only if overwrite=true.")
    enable_key = "tools.fs_move"

    def parameters(self):
        return {"type": "object", "properties": {
            "from_path": {"type": "string"},
            "to_path": {"type": "string"},
            "overwrite": {"type": "boolean", "description": "replace destination if it exists (default false)"}},
            "required": ["from_path", "to_path"]}

    async def run(self, args, ctx):
        try:
            rel = ws.move_path(ctx.user_id, args.get("from_path", ""), args.get("to_path", ""),
                               overwrite=bool(args.get("overwrite")))
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError:
            return ToolResult(False, hint("file_not_found", f"not found: {args.get('from_path')}"))
        except FileExistsError as e:
            return ToolResult(False, str(e))
        return ToolResult(True, f"moved to {rel}")


class FsSearchTool(Tool):
    name = "fs_search"
    label = "Filesystem search"
    description = ("Search file CONTENTS across the workspace with a case-insensitive regex, "
                   "optionally restricted by a glob pattern (e.g. '*.md'). Returns matching "
                   "lines as path:line: text.")
    setting_key = "tools.fs_read"      # rides on read permission legacy-style
    enable_key = "tools.fs_search"

    MAX_FILES = 400
    MAX_MATCHES = 60

    def parameters(self):
        return {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "regex (case-insensitive)"},
            "glob": {"type": "string", "description": "optional filename glob, e.g. *.md"},
            "path": {"type": "string", "description": "subdirectory to search, default '.'"}},
            "required": ["pattern"]}

    async def run(self, args, ctx):
        try:
            rx = re.compile(str(args.get("pattern", "")), re.I)
        except re.error as e:
            return ToolResult(False, f"bad regex: {e}")
        glob_pat = args.get("glob") or "*"
        base = args.get("path") or "."
        try:
            root_abs = ws.resolve_in_jail(ctx.user_id, base, allow_missing=False)
        except (ws.JailEscape, FileNotFoundError) as e:
            return ToolResult(False, f"refused/not found: {e}")
        matches, scanned = [], 0
        for dirpath, dirnames, filenames in os.walk(root_abs):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if scanned >= self.MAX_FILES or len(matches) >= self.MAX_MATCHES:
                    break
                if not fnmatch.fnmatch(fn, glob_pat):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > 512_000:
                        continue
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                scanned += 1
                rel = ws.rel_from_jail(ctx.user_id, p)
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(matches) >= self.MAX_MATCHES:
                            break
            if len(matches) >= self.MAX_MATCHES or scanned >= self.MAX_FILES:
                break
        if not matches:
            return ToolResult(True, hint("empty_result", "(no matches)"))
        # Capped results must SAY so. "60 lines and no note" reads to a model as a complete
        # answer, so it stops looking; naming the cap tells it to narrow the pattern or
        # scope with `path` instead of concluding there is nothing more.
        if len(matches) >= self.MAX_MATCHES:
            return ToolResult(True, "\n".join(matches) +
                              f"\n(truncated at {self.MAX_MATCHES} matches — narrow the "
                              f"pattern or pass 'path' to search one folder)")
        return ToolResult(True, "\n".join(matches))


class ZipPackTool(Tool):
    name = "zip_pack"
    label = "Zip bundling"
    description = ("Bundle several workspace files into one .zip and offer it as a downloadable "
                   "artifact (like attaching a folder). Paths are workspace-relative; the zip is "
                   "written to the workspace root.")
    enable_key = "tools.zip_pack"

    MAX_TOTAL_BYTES = 64 * 1024 * 1024   # simple bomb/quota guard for inputs

    def parameters(self):
        return {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "workspace-relative files to include"},
            "zip_name": {"type": "string", "description": "output zip filename, default 'bundle.zip'"}},
            "required": ["paths"]}

    async def run(self, args, ctx):
        paths = args.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return ToolResult(False, "paths must be a non-empty list")
        zname = os.path.basename(str(args.get("zip_name") or "bundle.zip"))
        if not zname.endswith(".zip"):
            zname += ".zip"
        try:
            resolved = []
            total = 0
            # Anything past the limit used to vanish while the reply still said
            # "packed {len(resolved)} files" — truthful about the zip, misleading about the
            # request. The slice is now a setting and every dropped path is counted in the
            # result so a caller can see that 250 named files did NOT all arrive (#18).
            cap = _int_setting(ctx, "tools.zip_pack.max_files", 200)
            for rel in paths[:cap]:
                # read_bytes decrypts transparently, so zips always contain usable
                # plaintext even when workspace encryption at rest is enabled.
                data = ws.read_bytes(ctx.user_id, str(rel))
                total += len(data)
                if total > self.MAX_TOTAL_BYTES:
                    return ToolResult(False, hint("too_large",
                                                  f"too much data to zip "
                                                  f"(>{self.MAX_TOTAL_BYTES // 1048576} MB)"))
                resolved.append((os.path.normpath(str(rel)), data))
            skipped = len(paths) - cap
        except ws.JailEscape as e:
            return ToolResult(False, hint("escape_root", f"refused: {e}"))
        except FileNotFoundError as e:
            return ToolResult(False, hint("file_not_found", f"not found: {e}"))
        if not resolved:
            return ToolResult(False, hint("empty_result", "none of the given paths are files"))
        import io
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, data in resolved:
                    zf.writestr(arcname, data)
            ws.write_bytes(ctx.user_id, zname, buf.getvalue())
        except OSError as e:
            return ToolResult(False, f"zip failed: {e}")
        from .. import artifacts
        art = artifacts.record(ctx.user_id, zname, conversation_id=ctx.conversation_id,
                               kind="produced")
        msg = f"packed {len(resolved)} files into {zname}"
        if skipped:
            # Name the shortfall in the message itself, not just in meta the model never sees.
            msg += (f" — NOT {len(paths)}: {skipped} path(s) were not included "
                    f"(limit {cap} per call). Bundle the rest in a second zip.")
        return ToolResult(True, msg,
                          meta={"download": {"name": art["filename"], "url": art["download_url"],
                                             "artifact_id": art["id"]},
                                "packed": len(resolved), "skipped": skipped})
