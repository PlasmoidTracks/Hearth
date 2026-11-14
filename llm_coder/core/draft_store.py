"""
Draft store: in-memory staging area for the model's proposed file edits.

Drafts live in RAM during a session and are persisted to ``.agent/drafts/``
on disk so they survive across app restarts.  The store intercepts read/write
operations, compounds iterative edits, and at review time splits a draft into
granular, independently-acceptable ``PendingPatch`` objects.

Key concepts
------------
* **base_content** – content of the file on disk at the moment the *first*
  draft was created for it.
* **resolved_content** – the fully-compounded draft after all edits in the
  current session.
* **edit_chain** – ordered list of individual edit operations (write / replace / append / prepend).
  Used for split-diff region detection.
* **Split-diff** – at review time the unified diff between *base* and *resolved*
  is split into contiguous change regions.  Close hunks are merged; if more
  than ``bulk_threshold`` of the file changed the whole file is offered as one
  patch instead.
"""
import difflib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from pathlib import Path

from core.tools import PendingPatch


@dataclass
class EditOp:
    """A single edit operation applied to a draft.

    A ``delete_section`` is recorded as a ``replace`` op with an empty
    ``new_text`` (deleting a section is equivalent to replacing it with "").
    """
    type: str                     # "write" | "replace" | "append" | "prepend"
    old_text: str
    new_text: str
    timestamp: float = field(default_factory=time.time)
    line_start: int = 0
    line_end: int = 0


@dataclass
class DraftEntry:
    """Holds the state of a single file's draft."""
    base_content: str
    resolved_content: str
    edit_chain: List[EditOp] = field(default_factory=list)
    status: str = "active"        # "active" | "deleted" | "renamed"
    source_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    edit_count: int = 0


class DraftStore:
    """In-memory draft store with on-disk persistence."""

    def __init__(self, project_root: str, merge_gap: int = 15,
                 bulk_threshold: float = 0.5):
        self.project_root = Path(project_root).resolve()
        self.drafts_dir = self.project_root / ".agent" / "drafts"
        self.merge_gap = merge_gap
        self.bulk_threshold = bulk_threshold
        self._drafts: Dict[str, DraftEntry] = {}
        self._session_log: List[dict] = []

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def get_draft(self, path: str) -> Optional[DraftEntry]:
        path = self._normalize_path(path)
        entry = self._drafts.get(path)
        if entry and entry.status == "active":
            return entry
        return None

    def has_draft(self, path: str) -> bool:
        return self.get_draft(path) is not None

    def set_draft(self, path: str, base_content: str,
                  new_content: str, op_type: str = "write") -> DraftEntry:
        """Create or update a draft.  If an active draft exists it is
        compounded (resolved_content is overwritten with new_content and an
        EditOp is appended).  Otherwise a brand-new draft is created."""
        path = self._normalize_path(path)
        existing = self._drafts.get(path)
        if existing and existing.status == "active":
            edit = EditOp(type=op_type, old_text=existing.resolved_content,
                          new_text=new_content)
            existing.edit_chain.append(edit)
            existing.resolved_content = new_content
            existing.edit_count += 1
            return existing

        entry = DraftEntry(
            base_content=base_content,
            resolved_content=new_content,
            edit_chain=[EditOp(type=op_type, old_text=base_content,
                               new_text=new_content)],
        )
        self._drafts[path] = entry
        self._session_log.append({
            "action": "created", "path": path,
            "timestamp": time.time(),
        })
        return entry

    def apply_replace_on_draft(self, path: str, old_text: str,
                               new_text: str, replace_all: bool = False) -> bool:
        """Apply a replace_in_file operation on the draft.  Returns True on
        success.

        replace_all=False (default) replaces only the first occurrence — the
        normal replace_in_file case, where uniqueness is verified by the
        caller.  replace_all=True replaces every occurrence (the explicit
        unique=false / delete-all mode).
        """
        path = self._normalize_path(path)
        entry = self.get_draft(path)
        if entry is None:
            return False
        if old_text not in entry.resolved_content:
            return False
        new_resolved = entry.resolved_content.replace(old_text, new_text) if replace_all \
            else entry.resolved_content.replace(old_text, new_text, 1)
        idx = entry.resolved_content.index(old_text)
        edit = EditOp(
            type="replace", old_text=old_text, new_text=new_text,
            line_start=entry.resolved_content[:idx].count("\n") + 1,
            line_end=(entry.resolved_content[:idx].count("\n") +
                      new_text.count("\n") + 1),
        )
        entry.edit_chain.append(edit)
        entry.resolved_content = new_resolved
        entry.edit_count += 1
        self._session_log.append({
            "action": "replace", "path": path,
            "timestamp": time.time(),
        })
        return True

    def append_to_draft(self, path: str, new_content: str) -> bool:
        """Append *new_content* to the draft's resolved_content. Returns True on
        success."""
        path = self._normalize_path(path)
        entry = self.get_draft(path)
        if entry is None:
            return False
        new_resolved = entry.resolved_content + new_content
        edit = EditOp(
            type="append", old_text=entry.resolved_content,
            new_text=new_content,
            line_start=len(entry.resolved_content.splitlines()) + 1,
            line_end=0,
        )
        entry.edit_chain.append(edit)
        entry.resolved_content = new_resolved
        entry.edit_count += 1
        self._session_log.append({
            "action": "append", "path": path,
            "timestamp": time.time(),
        })
        return True

    def prepend_to_draft(self, path: str, new_content: str) -> bool:
        """Prepend *new_content* to the draft's resolved_content. Returns True on
        success."""
        path = self._normalize_path(path)
        entry = self.get_draft(path)
        if entry is None:
            return False
        new_resolved = new_content + entry.resolved_content
        edit = EditOp(
            type="prepend", old_text=entry.resolved_content,
            new_text=new_content,
            line_start=1,
            line_end=0,
        )
        entry.edit_chain.append(edit)
        entry.resolved_content = new_resolved
        entry.edit_count += 1
        self._session_log.append({
            "action": "prepend", "path": path,
            "timestamp": time.time(),
        })
        return True

    def delete_draft(self, path: str) -> bool:
        path = self._normalize_path(path)
        if path in self._drafts:
            self._drafts[path].status = "deleted"
            self._session_log.append({
                "action": "deleted", "path": path,
                "timestamp": time.time(),
            })
            return True
        return False

    def rename_draft(self, old_path: str, new_path: str) -> bool:
        old_path = self._normalize_path(old_path)
        new_path = self._normalize_path(new_path)
        entry = self.get_draft(old_path)
        if entry is None:
            return False
        entry.source_path = old_path
        del self._drafts[old_path]
        self._drafts[new_path] = entry
        self._session_log.append({
            "action": "renamed", "old_path": old_path,
            "new_path": new_path, "timestamp": time.time(),
        })
        return True

    def clear(self, path: str) -> bool:
        """Remove a draft entirely (called after acceptance)."""
        path = self._normalize_path(path)
        if path in self._drafts:
            del self._drafts[path]
            self._session_log.append({
                "action": "cleared", "path": path,
                "timestamp": time.time(),
            })
            return True
        return False

    def list_active(self) -> List[str]:
        return [p for p, e in self._drafts.items()
                if e.status == "active"]

    def _normalize_path(self, path: str) -> str:
        return path.lstrip("/")

    # ------------------------------------------------------------------
    # Split-diff generation
    # ------------------------------------------------------------------

    def generate_split_patches(self, path: str, disk_content: str,
                               merge_gap: int = None,
                               bulk_threshold: float = None
                               ) -> List[PendingPatch]:
        """Split a draft into multiple ``PendingPatch`` objects based on
        distinct change regions between *disk_content* and the draft's
        *resolved_content*.

        Close hunks are merged (controlled by *merge_gap*); if more than
        *bulk_threshold* of lines differ the entire file is returned as a
        single patch.
        """
        mg = merge_gap if merge_gap is not None else self.merge_gap
        bt = bulk_threshold if bulk_threshold is not None else self.bulk_threshold

        entry = self.get_draft(path)
        if entry is None:
            return []

        resolved = entry.resolved_content
        base = disk_content

        if resolved == base:
            return []

        # A brand-new file (nothing on disk yet) must never be split into
        # regions: there is no "old" content to diff against line-by-line,
        # and routing it through accept_hunks() breaks because old_content
        # ends up as "" — which Python's str.replace("", x, 1) inserts
        # rather than substitutes, silently corrupting/no-op'ing the write.
        # Always represent it as a single atomic create patch instead.
        if base == "":
            return [PendingPatch(
                kind="create", path=path, old_content=base,
                new_content=resolved,
                description="new file",
            )]

        hunks = self._parse_hunks(base, resolved)
        if not hunks:
            return []

        regions = self._group_hunks(hunks, mg)
        total_lines = max(len(base.splitlines()), len(resolved.splitlines()), 1)
        all_changed = set()
        for r in regions:
            for line in r.get("changed_old", []):
                all_changed.add(("o", line))
            for line in r.get("changed_new", []):
                all_changed.add(("n", line))
        changed_frac = len(all_changed) / (total_lines * 2)

        # A file with no prior disk content (empty base) is a brand-new
        # file being created, not a modification of existing content.
        # Mislabeling this as "modify" is misleading in the review UI and
        # would break kind-based logic elsewhere (e.g. auto_approve_new_files).
        effective_kind = "create" if base == "" else "modify"

        patches: List[PendingPatch] = []
        if changed_frac > bt:
            # A whole-file rewrite produces a SINGLE diff hunk, so requiring
            # len(regions) > 1 here made the bulk branch unreachable for the
            # very case it exists for: a 100% rewrite fell through and was
            # labelled "region 1 (lines 1-40)" — indistinguishable in the
            # review UI from a one-line touch. The patch shape is the same
            # either way (one whole-file patch); what was lost is the honest
            # description that warns the reviewer nothing here is granular.
            patches.append(PendingPatch(
                kind=effective_kind, path=path, old_content=base,
                new_content=resolved,
                description=f"bulk edit ({changed_frac:.0%} of file changed)",
            ))
        else:
            for i, region in enumerate(regions):
                desc = f"region {i + 1} (lines {region['start_line']}-{region['end_line']})"
                patches.append(PendingPatch(
                    kind=effective_kind, path=path, old_content=region["old_text"],
                    new_content=region["new_text"], description=desc,
                ))
        return patches

    @staticmethod
    def _parse_hunks(base: str, resolved: str) -> List[dict]:
        """Parse a unified diff into a list of hunk dicts with changed/context
        line collections."""
        old_lines = base.splitlines(True)
        new_lines = resolved.splitlines(True)
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
        hunks: List[dict] = []
        cur = None
        for line in diff:
            if line.startswith("@@"):
                if cur is not None:
                    hunks.append(cur)
                parts = line.split()
                old_start = int(parts[1].split(",")[0].lstrip("-"))
                new_start = int(parts[2].split(",")[0].lstrip("+"))
                cur = {
                    "old_start": old_start,
                    "new_start": new_start,
                    "old_lines": [], "new_lines": [],
                    "context_lines": [],
                    "changed_old": [], "changed_new": [],
                }
            elif line.startswith("---") or line.startswith("+++"):
                # Diff header lines before the @@ marker — skip them.
                continue
            elif cur is not None and line.startswith("-"):
                cur["old_lines"].append(line[1:])
                cur["changed_old"].append(line[1:])
            elif cur is not None and line.startswith("+"):
                cur["new_lines"].append(line[1:])
                cur["changed_new"].append(line[1:])
            elif cur is not None and line.startswith(" "):
                cur["context_lines"].append(line[1:])
        if cur is not None:
            hunks.append(cur)
        return [h for h in hunks if h["changed_old"] or h["changed_new"]]

    @staticmethod
    def _group_hunks(hunks: List[dict], merge_gap: int) -> List[dict]:
        """Group nearby hunks into regions."""
        if not hunks:
            return []

        groups: List[List[dict]] = [[hunks[0]]]
        for h in hunks[1:]:
            prev = groups[-1][-1]
            prev_end = (prev["old_start"] + len(prev["old_lines"]) +
                        len(prev["context_lines"]))
            gap = h["old_start"] - prev_end
            if gap <= merge_gap:
                groups[-1].append(h)
            else:
                groups.append([h])

        regions: List[dict] = []
        for group in groups:
            if len(group) == 1:
                h = group[0]
                old_text = "".join(h["old_lines"])
                new_text = "".join(h["new_lines"])
                start = max(1, h["old_start"])
                end = start + len(h["old_lines"]) + len(h["context_lines"]) - 1
            else:
                start = group[0]["old_start"]
                end = (group[-1]["old_start"] +
                       len(group[-1]["old_lines"]) +
                       len(group[-1]["context_lines"]))
                old_text = "".join(
                    l for h in group for l in h["old_lines"] + h["context_lines"]
                )
                new_text = "".join(
                    l for h in group for l in h["new_lines"] + h["context_lines"]
                )
            regions.append({
                "start_line": start, "end_line": end,
                "old_text": old_text, "new_text": new_text,
                "changed_old": [l for h in group for l in h["changed_old"]],
                "changed_new": [l for h in group for l in h["changed_new"]],
            })
        return regions

    # ------------------------------------------------------------------
    # Accept / apply
    # ------------------------------------------------------------------

    def accept_all(self, path: str) -> Optional[str]:
        """Return *resolved_content* to be written to disk.

        Does NOT clear the draft — the caller must call clear(path) only
        AFTER confirming the write to disk actually succeeded. Clearing
        unconditionally here (the previous behavior) meant a failed write
        silently destroyed the approved content with no way to recover it:
        the file never got created/updated AND the draft was already gone,
        so the model would blindly retry from scratch on a later turn —
        this was the root cause of accepted changes not appearing, and of
        stale/duplicated patches reappearing across runs.
        """
        path = self._normalize_path(path)
        entry = self.get_draft(path)
        if entry is None:
            return None
        content = entry.resolved_content
        self._session_log.append({
            "action": "accepted_all", "path": path,
            "timestamp": time.time(),
        })
        return content
    def accept_hunks(self, path: str, accepted_indices: List[int],
                     disk_content: str) -> Optional[str]:
        """Accept only specific change regions.  Splices them back onto
        disk_content.  Returns the new content and clears the draft."""
        path = self._normalize_path(path)
        entry = self.get_draft(path)
        if entry is None:
            return None
        patches = self.generate_split_patches(path, disk_content)
        result = disk_content
        # accepted_indices refers to each patch's position in the ORIGINAL
        # top-down `patches` list (the same order the review UI displayed
        # them in). Pair each patch with that original index BEFORE sorting
        # bottom-up for splicing — otherwise the index check below tests
        # against the wrong (reordered) patch and silently accepts/rejects
        # the wrong region.
        indexed_patches = list(enumerate(patches))
        indexed_patches.sort(
            key=lambda pair: disk_content.index(pair[1].old_content)
                              if pair[1].old_content in disk_content else 0,
            reverse=True,
        )
        for original_idx, patch in indexed_patches:
            if original_idx in accepted_indices:
                if patch.old_content in result:
                    result = result.replace(patch.old_content,
                                            patch.new_content, 1)
        self._session_log.append({
            "action": "accepted_hunks", "path": path,
            "accepted": accepted_indices, "timestamp": time.time(),
        })
        # Does NOT clear the draft — same reasoning as accept_all() above.
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def flush_to_disk(self):
        """Write all active drafts to ``.agent/drafts/`` on disk.

        MUST run even when self._drafts is empty: metadata.json needs to be
        overwritten (not left stale) whenever the LAST remaining draft was
        cleared since the previous flush — otherwise load_from_disk() on
        next launch faithfully resurrects an already-accepted draft as
        pending again, because clear() only removes the in-memory entry and
        never touches the on-disk metadata or per-path draft file.
        """
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        meta_file = self.drafts_dir / "_metadata.json"

        # Discover which per-path draft files exist from a PRIOR flush, so
        # we can remove ones that are no longer active (accepted/rejected
        # since then) — leaving them behind is harmless disk clutter on its
        # own, but load_from_disk() only trusts metadata.json for which
        # paths are active, so this is cleanup rather than a correctness fix.
        previously_known_paths = set()
        if meta_file.exists():
            try:
                previously_known_paths = set(json.loads(meta_file.read_text(encoding="utf-8")).keys())
            except Exception:
                previously_known_paths = set()

        metadata: Dict[str, dict] = {}
        for path, entry in self._drafts.items():
            if entry.status != "active":
                continue
            draft_file = self.drafts_dir / path
            draft_file.parent.mkdir(parents=True, exist_ok=True)
            draft_file.write_text(entry.resolved_content, encoding="utf-8")
            metadata[path] = {
                "base_content": entry.base_content,
                "edit_count": entry.edit_count,
                "edit_chain": [
                    {"type": e.type, "old_text": e.old_text,
                     "new_text": e.new_text, "timestamp": e.timestamp,
                     "line_start": e.line_start, "line_end": e.line_end}
                    for e in entry.edit_chain
                ],
                "status": entry.status,
                "source_path": entry.source_path,
                "created_at": entry.created_at,
            }

        # Always rewrite metadata.json, even to an empty {} — this is the
        # actual fix. Previously an early `if not self._drafts: return`
        # skipped this write entirely, leaving a stale prior-session file.
        meta_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # Remove on-disk draft files for paths that are no longer active.
        for stale_path in previously_known_paths - set(metadata.keys()):
            stale_file = self.drafts_dir / stale_path
            try:
                if stale_file.exists():
                    stale_file.unlink()
            except Exception:
                pass

    def load_from_disk(self):
        """Load drafts from disk into memory."""
        meta_file = self.drafts_dir / "_metadata.json"
        if not meta_file.exists():
            return
        try:
            metadata = json.loads(
                meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return
        stale_paths = []
        for path, data in metadata.items():
            draft_file = self.drafts_dir / path
            if not draft_file.exists():
                continue
            resolved = draft_file.read_text(encoding="utf-8",
                                              errors="replace")
            edit_chain = [EditOp(**e)
                          for e in data.get("edit_chain", [])]
            stored_base = data.get("base_content", "")

            # Revalidate: if the real file on disk changed since this draft
            # was flushed (external edit, git pull, etc.), the stored
            # base_content no longer reflects reality. Rebase onto the
            # current disk content instead of silently diffing against a
            # phantom baseline.
            real_file = self.project_root / path
            actual_disk_content = None
            if real_file.exists():
                try:
                    actual_disk_content = real_file.read_text(
                        encoding="utf-8", errors="replace")
                except Exception:
                    actual_disk_content = None

            base_content = stored_base
            if actual_disk_content is not None and actual_disk_content != stored_base:
                base_content = actual_disk_content
                stale_paths.append(path)

            entry = DraftEntry(
                base_content=base_content,
                resolved_content=resolved,
                edit_chain=edit_chain,
                status=data.get("status", "active"),
                source_path=data.get("source_path"),
                created_at=data.get("created_at", time.time()),
                edit_count=data.get("edit_count", len(edit_chain)),
            )
            self._drafts[path] = entry
        if stale_paths:
            for p in stale_paths:
                self._session_log.append({
                    "action": "rebased_stale_base", "path": p,
                    "timestamp": time.time(),
                })

    # ------------------------------------------------------------------
    # Session history
    # ------------------------------------------------------------------

    def get_session_log(self) -> List[dict]:
        return list(self._session_log)

    def clear_session_log(self):
        self._session_log.clear()
