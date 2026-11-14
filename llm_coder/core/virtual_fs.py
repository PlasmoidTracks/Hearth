"""
VirtualFS: unified read-side view over a project's real files (Workspace)
and its in-memory pending edits (DraftStore).

Why this exists
----------------
Before this module, every read-oriented tool (read_file, list_files,
list_directory, search_in_files, grep_regex, ...) in ToolExecutor
independently re-implemented its own "does this path have a pending
draft?" check via draft_store.has_draft()/get_draft(). Some methods
(Workspace.compile_check, Workspace.exists, Workspace.file_size) never
got that treatment at all, so the model could get contradictory answers
depending on which tool it happened to call for the same path — e.g.
read_file correctly showed "[DRAFT] ..." content, but a hypothetical
compile-check on the same path would silently compile the STALE on-disk
version instead, and exists() would report the file as absent even
though a create-draft for it existed in memory.

This class is the single place that decides "real disk content or
pending draft content?" for a given path. Every tool that reads project
state should go through this instead of touching Workspace/DraftStore
directly. To the model, a path with a pending draft and a path that's
already on disk look identical except for an explicit "[DRAFT]" marker
telling it the content is unapplied — the actual bytes returned are
otherwise indistinguishable in shape from a normal read.

This module is purely a read-side facade. Writes still go through
ToolExecutor.build_pending_patch() -> DraftStore.set_draft() (staging)
or Workspace.write_file() (applying an accepted patch) exactly as
before — VirtualFS does not change the write/accept pipeline at all.
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from .workspace import Workspace, WorkspaceError


@dataclass
class VirtualFileInfo:
    """Effective (draft-aware) info about a single path."""
    path: str
    exists: bool
    is_draft: bool           # True if content comes from an unapplied draft
    size: Optional[int]      # byte length of the effective content; None if unreadable/binary
    content: Optional[str]   # effective text content; None if binary/unreadable/absent


class VirtualFS:
    """Read-only facade combining Workspace (disk) + DraftStore (RAM).

    draft_store may be None (e.g. no project loaded yet, or drafts
    disabled) — every method degrades gracefully to pure-disk behavior
    in that case, so callers never need their own "if draft_store" guard.
    """

    def __init__(self, workspace: Workspace, draft_store=None):
        self.workspace = workspace
        self.draft_store = draft_store

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _draft(self, path: str):
        if self.draft_store is None:
            return None
        return self.draft_store.get_draft(path)

    # ------------------------------------------------------------------
    # Existence / metadata
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        """True if the path exists either as a pending draft (including a
        brand-new not-yet-created file) or on disk."""
        if self._draft(path) is not None:
            return True
        return self.workspace.exists(path)

    def read_info(self, path: str) -> VirtualFileInfo:
        """Draft-aware metadata + content for a single path, without
        raising — callers that want exceptions should use read() instead."""
        draft = self._draft(path)
        if draft is not None:
            content = draft.resolved_content
            return VirtualFileInfo(
                path=path, exists=True, is_draft=True,
                size=len(content.encode("utf-8", errors="replace")),
                content=content,
            )
        try:
            content = self.workspace.read_file(path)
        except WorkspaceError:
            return VirtualFileInfo(path=path, exists=False, is_draft=False, size=None, content=None)
        try:
            size = self.workspace.file_size(path)
        except Exception:
            size = len(content.encode("utf-8", errors="replace"))
        return VirtualFileInfo(path=path, exists=True, is_draft=False, size=size, content=content)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(self, path: str) -> str:
        """Read the *effective* content of path: the pending draft if one
        exists, otherwise the real on-disk content. Raises WorkspaceError
        if the path has neither."""
        draft = self._draft(path)
        if draft is not None:
            return draft.resolved_content
        return self.workspace.read_file(path)

    def is_draft(self, path: str) -> bool:
        return self._draft(path) is not None

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_files(self, max_files: int = 2000) -> List[str]:
        """All known paths: on-disk files unioned with paths that only
        exist as pending drafts (e.g. a brand-new file not yet accepted)."""
        disk = set(self.workspace.list_files(max_files=max_files))
        if self.draft_store is None:
            return sorted(disk)
        return sorted(disk | set(self.draft_store.list_active()))

    def list_directory(self, rel_path: str = ".", strict: bool = False) -> List[str]:
        """Directory listing merged with draft-only entries under rel_path.
        Mirrors ToolExecutor._merge_draft_entries' prior logic, now the
        single implementation every caller shares.

        ``strict=False`` (default) keeps the long-standing lenient behaviour:
        a missing directory lists as empty, which is what the UI's browser-like
        views want. ``strict=True`` re-raises the WorkspaceError instead, so a
        caller answering the agent can tell "empty directory" apart from "no
        such directory" rather than reporting the latter as the former."""
        try:
            entries = self.workspace.list_directory(rel_path)
        except WorkspaceError:
            if strict:
                raise
            entries = []
        if self.draft_store is None:
            return entries
        return self._merge_draft_entries(rel_path, entries)

    def _merge_draft_entries(self, rel_path: str, entries: List[str]) -> List[str]:
        draft_paths = self.draft_store.list_active()
        dir_prefix = (rel_path.rstrip("/") + "/") if rel_path != "." else ""
        merged = list(entries)
        for dp in draft_paths:
            if dir_prefix and not dp.startswith(dir_prefix):
                continue
            remaining = dp[len(dir_prefix):] if dir_prefix else dp
            if "/" not in remaining:
                if remaining not in merged:
                    merged.append(remaining)
                continue
            sub = remaining.split("/", 1)[0] + "/"
            if sub not in merged:
                merged.append(sub)
        return sorted(merged)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int = 100) -> List[dict]:
        """Substring search across disk files AND active drafts. Draft
        content is searched even for brand-new files that don't exist on
        disk at all yet."""
        results = self.workspace.search(query, max_results=max_results)
        if self.draft_store is None or len(results) >= max_results:
            return results
        query_lower = query.lower()
        for draft_path in self.draft_store.list_active():
            draft = self.draft_store.get_draft(draft_path)
            if draft is None:
                continue
            for i, line in enumerate(draft.resolved_content.splitlines(), start=1):
                if query_lower in line.lower():
                    results.append({"file": draft_path, "line": i, "text": line.strip()})
                    if len(results) >= max_results:
                        return results
        return results

    def search_regex(self, pattern: str, max_results: int = 100) -> List[dict]:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise WorkspaceError(f"Invalid regex pattern: {e}")
        results = self.workspace.search_regex(pattern, max_results=max_results)
        if self.draft_store is None or len(results) >= max_results:
            return results
        for draft_path in self.draft_store.list_active():
            draft = self.draft_store.get_draft(draft_path)
            if draft is None:
                continue
            for i, line in enumerate(draft.resolved_content.splitlines(), start=1):
                if compiled.search(line):
                    results.append({"file": draft_path, "line": i, "text": line.strip()})
                    if len(results) >= max_results:
                        return results
        return results

    # ------------------------------------------------------------------
    # Draft-aware compile check (previously missing entirely)
    # ------------------------------------------------------------------

    def compile_check(self, rel_path: str) -> str:
        """Compile-checks the EFFECTIVE content (draft if pending, else
        disk) — the previous Workspace.compile_check always compiled
        stale disk content even when a draft existed, which could tell
        the model a syntax error was fixed when only the (unread) disk
        version was actually still broken, or vice versa."""
        if not rel_path.endswith(".py"):
            raise WorkspaceError(f"{rel_path} is not a .py file.")
        source = self.read(rel_path)
        note = " (draft — not yet applied to disk)" if self.is_draft(rel_path) else ""
        try:
            compile(source, rel_path, "exec")
            return f"OK — {rel_path} compiles with no syntax errors{note}."
        except SyntaxError as e:
            pointer = ""
            if e.text:
                pointer = e.text.rstrip("\n") + "\n" + " " * max(0, (e.offset or 1) - 1) + "^"
            return f"SyntaxError in {rel_path}{note}, line {e.lineno}, column {e.offset}: {e.msg}\n{pointer}"
