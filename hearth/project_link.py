"""Linked project folder: link registry, copy-in, baseline manifest, diff/preview.

DEC-049 / CONTRACT v1.3 (REQ-041..043, REQ-048). The model never sees the real path: it
works on a COPY of a bounded subtree that lives inside its own jail under a fixed prefix
(default ``project/``), exactly like any other workspace file. The real folder is touched
only by ``project_apply.apply_changes()`` after explicit user approval.

Why a copy instead of redirecting the jail at the target: writes would otherwise hit the
user's real files immediately (no review point) and, with workspace encryption at rest,
would land ciphertext in their project. Keeping the copy jailed means every existing
guarantee in workspaces.py applies unchanged, and ``sync_preview`` has a stable baseline to
diff against.

Content-agnostic by design: no path/type/deny-list filtering beyond size + directory-name
skips needed for a sane bounded import. See DEC-049 "Rejected: guard-path list".
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .db import session_scope
from .models import ProjectLink

PREFIX_DEFAULT = "project"
MANIFEST_NAME = ".hearth_manifest.json"

# Directories skipped during import. These are VCS/build/venv noise that would eat the
# file budget without being editable project source — not a security filter, and they are
# never deleted from the target (apply only ever writes files present in the copy).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".mypy_cache", ".pytest_cache", ".idea", ".next", "target"}


class LinkError(Exception):
    """Raised for any refused link/import operation, with a user-facing message."""


# ---------------------------------------------------------------------------
# Link registry (one active link per user)
# ---------------------------------------------------------------------------

def get_link(user_id: str) -> dict | None:
    """The user's ACTIVE link as a plain dict, or None.

    Returns only an enabled row: clear_link() disables rather than deletes (so history
    survives), and callers must see "no link" afterwards rather than a stale target.
    """
    with session_scope() as s:
        row = (s.query(ProjectLink)
                .filter(ProjectLink.user_id == user_id, ProjectLink.enabled.is_(True))
                .order_by(ProjectLink.created_at.desc())
                .first())
        if row is None:
            return None
        return _as_dict(row)


def _as_dict(row: ProjectLink) -> dict:
    return {"id": row.id, "user_id": row.user_id, "target_abs": row.target_abs,
            "rel_prefix": row.rel_prefix or PREFIX_DEFAULT, "enabled": bool(row.enabled),
            "imported_at": int(row.imported_at or 0), "created_at": row.created_at}


def set_link(user_id: str, target: str) -> dict:
    """Validate and store the user's link. Any previous link is disabled first.

    The target must be an existing readable directory once canonicalized. We deliberately
    do NOT police what kind of folder it is — see DEC-049 on content agnosticism.
    """
    raw = str(target or "").strip()
    if not raw:
        raise LinkError("no folder given")
    if not os.path.isabs(raw):
        raise LinkError("the folder path must be absolute (e.g. /home/you/projects/thing)")
    try:
        canon = str(Path(raw).expanduser().resolve())
    except OSError as e:
        raise LinkError(f"cannot resolve that path: {e}")
    if not os.path.isdir(canon):
        raise LinkError(f"not a directory or does not exist: {canon}")
    if not os.access(canon, os.R_OK):
        raise LinkError(f"not readable by the Hearth process: {canon}")

    jail_root = _jail_root(user_id)
    if canon == str(jail_root) or jail_root in Path(canon).parents:
        raise LinkError("cannot link a folder inside your own workspace")

    with session_scope() as s:
        for old in s.query(ProjectLink).filter(ProjectLink.user_id == user_id,
                                              ProjectLink.enabled.is_(True)).all():
            old.enabled = False
        row = ProjectLink(user_id=user_id, target_abs=canon,
                          rel_prefix=PREFIX_DEFAULT, enabled=True)
        s.add(row)
        s.flush()
        return _as_dict(row)


def clear_link(user_id: str, *, delete_copy: bool = False) -> bool:
    """Disable the user's active link. Optionally drop the jail copy + manifest."""
    cleared = False
    with session_scope() as s:
        rows = s.query(ProjectLink).filter(ProjectLink.user_id == user_id,
                                          ProjectLink.enabled.is_(True)).all()
        for r in rows:
            r.enabled = False
            cleared = True
    if delete_copy:
        base = copy_root(user_id)
        shutil.rmtree(base / MANIFEST_NAME, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)
    return cleared


def target_root(link: dict) -> Path:
    p = Path(str(link.get("target_abs") or ""))
    if not str(p) or not p.is_dir():
        raise LinkError("the linked folder no longer exists — re-link it")
    return p


# ---------------------------------------------------------------------------
# Jail-side layout helpers
# ---------------------------------------------------------------------------

def _jail_root(user_id: str) -> Path:
    from . import workspaces as ws
    return ws.root_for(user_id)


def copy_root(user_id: str, link: dict | None = None) -> Path:
    """Jail directory holding the imported copy (and its manifest)."""
    link = link or get_link(user_id)
    prefix = (link or {}).get("rel_prefix") or PREFIX_DEFAULT
    root = _jail_root(user_id)
    base = (root / prefix).resolve()
    if base != root and root not in base.parents:
        raise LinkError("internal error: link prefix escapes the workspace")
    base.mkdir(parents=True, exist_ok=True)
    return base


def manifest_path(user_id: str, link: dict | None = None) -> Path:
    return copy_root(user_id, link) / MANIFEST_NAME


def load_manifest(user_id: str, link: dict | None = None) -> dict:
    """The baseline manifest, or an empty one if nothing was imported yet."""
    p = manifest_path(user_id, link)
    if not p.is_file():
        return {"version": 1, "target_abs": "", "files": {}}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"version": 1, "target_abs": "", "files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": 1, "target_abs": "", "files": {}}
    return data


def save_manifest(user_id: str, manifest: dict, link: dict | None = None) -> None:
    p = manifest_path(user_id, link)
    tmp = p.with_suffix(p.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)


def file_entry(abs_path: Path) -> dict:
    """Baseline entry for one file: hash + size + mtime."""
    st = abs_path.stat()
    return {"sha256": sha256_file(abs_path), "size": int(st.st_size),
            "mtime": int(st.st_mtime)}


def sha256_file(abs_path: Path) -> str:
    h = hashlib.sha256()
    with open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(root: Path, max_files: int, max_bytes: int) -> tuple[list[str], dict, dict]:
    """Walk ``root`` collecting relative paths + baseline entries.

    Returns (rel_paths, entries, stats). Stops adding files once either budget is hit and
    reports the truncation in stats so callers can tell the user rather than silently
    presenting a partial project.
    """
    rels: list[str] = []
    entries: dict[str, dict] = {}
    total = 0
    skipped_dirs = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if len(rels) >= max_files or total + size > max_bytes:
                truncated = True
                break
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            rels.append(rel)
            entries[rel] = file_entry(p)
            total += size
        if truncated:
            break
    stats = {"files": len(rels), "bytes": total, "truncated": truncated,
             "skipped_dirs": sorted(SKIP_DIRS)}
    return rels, entries, stats


# ---------------------------------------------------------------------------
# Import: copy a bounded subtree into the jail + record the baseline manifest
# ---------------------------------------------------------------------------

def _limits(settings: dict) -> tuple[int, int]:
    max_files = int(settings.get("project.max_import_files") or 4000)
    max_bytes = int(settings.get("project.max_import_bytes") or 0) or (256 * 1024 * 1024)
    return max_files, max_bytes


def import_project(user_id: str, settings: dict, link: dict | None = None) -> dict:
    """Copy the linked folder into the jail and snapshot it as the baseline.

    Reads the target directly (never through the jail's encrypted blob layer): the target
    holds the user's real plaintext files, while workspace encryption applies only to what
    Hearth stores inside the jail. The copy itself is written via workspaces.write_bytes so
    it is sealed like any other jailed file.

    Existing jailed copies of files no longer present in the target are removed, so the
    copy always mirrors the freshly-imported baseline (deletions in the real folder never
    propagate from the agent side — see project_apply).
    """
    link = link or get_link(user_id)
    if not link:
        raise LinkError("no project folder is linked yet")
    src = target_root(link)
    max_files, max_bytes = _limits(settings)

    rels, entries, stats = scan_tree(src, max_files, max_bytes)
    base = copy_root(user_id, link)

    from . import workspaces as ws
    copied = 0
    failed: list[str] = []
    for rel in rels:
        s_abs = src / rel
        try:
            with open(s_abs, "rb") as fh:
                data = fh.read()
            ws.write_bytes(user_id, f"{link['rel_prefix']}/{rel}", data)
            copied += 1
        except OSError as e:
            failed.append(f"{rel}: {str(e)[:120]}")

    manifest = {"version": 1, "target_abs": str(src), "files": entries}
    save_manifest(user_id, manifest, link)

    # Drop jailed copies that are no longer part of the imported set.
    removed = _prune_copy(user_id, link, {r for r in entries})

    with session_scope() as s:
        row = s.get(ProjectLink, link["id"])
        if row is not None:
            row.imported_at = int(time.time())

    out = {"ok": True, "target_abs": str(src), "prefix": link["rel_prefix"],
           "files": len(entries), "copied": copied, "removed_stale": removed,
           "bytes": stats["bytes"], "truncated": stats["truncated"],
           "skipped_dirs": stats["skipped_dirs"], "failed": failed[:20]}
    return out


def _prune_copy(user_id: str, link: dict, keep: set[str]) -> int:
    """Delete files under the copy root that are not in ``keep`` (manifest keys)."""
    from . import workspaces as ws
    base = copy_root(user_id, link)
    n = 0
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            if rel == MANIFEST_NAME or rel in keep:
                continue
            jail_rel = f"{link['rel_prefix']}/{rel}"
            try:
                ws.delete_path(user_id, jail_rel)
                n += 1
            except (OSError, ws.JailEscape):
                continue
    return n


# ---------------------------------------------------------------------------
# Diff / preview (read-only; never writes outside the jail)
# ---------------------------------------------------------------------------

@dataclass
class ChangeSet:
    """What an apply would do, and what blocks it.

    ``added``/``modified`` are files present in the copy but not (or differently) in the
    baseline. ``deleted`` is reported for information only: deleting real user files on the
    agent's say-so is out of scope, so they are never applied. A path is a ``conflict`` when
    the target no longer matches the baseline we imported — the user edited it elsewhere, so
    applying would silently overwrite their work. Conflicts are refused, never merged.
    """
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def applyable(self) -> list[str]:
        return self.added + self.modified

    def summary(self) -> str:
        bits = [f"{len(self.added)} new", f"{len(self.modified)} modified",
                f"{len(self.conflicts)} conflict(s)", f"{len(self.deleted)} deleted (not applied)"]
        return ", ".join(bits)


def _read_copy(user_id: str, jail_rel: str) -> bytes | None:
    from . import workspaces as ws
    try:
        return ws.read_bytes(user_id, jail_rel)
    except (FileNotFoundError, OSError, ws.JailEscape):
        return None


def diff_project(user_id: str, link: dict | None = None) -> ChangeSet:
    """Compare the jailed copy against the baseline manifest and the real target.

    Purely read-only. Every target hash is taken fresh so a concurrent edit made after an
    earlier preview is still caught at apply time (REQ-044).
    """
    link = link or get_link(user_id)
    if not link:
        raise LinkError("no project folder is linked yet")
    manifest = load_manifest(user_id, link)
    baseline: dict = manifest.get("files") or {}
    prefix = link["rel_prefix"]

    # What the copy contains now (excluding the manifest itself).
    current = _current_copy_paths(user_id, link)
    cs = ChangeSet()

    for rel in sorted(set(current) | set(baseline)):
        jail_rel = f"{prefix}/{rel}"
        base_entry = baseline.get(rel)
        target_abs = target_root(link) / rel

        cur_hash = current.get(rel)
        if cur_hash is None:
            # In the baseline but no longer in the copy -> a deletion the agent "made".
            cs.deleted.append(rel)
            continue

        if base_entry is None:
            # Not in the baseline: brand-new file. It may still collide with a real file the
            # user added out-of-band, so check the target before calling it an addition.
            if target_abs.exists():
                cs.conflicts.append(f"{rel} (target already has this file)")
            else:
                cs.added.append(rel)
            continue

        if base_entry.get("_absent"):
            # A file the agent created: nothing to compare content against, so its safety
            # condition is simply "the target still does not have this path". Checked before
            # existence (it is legitimately absent) and before the missing-target branch.
            if target_abs.exists():
                cs.conflicts.append(f"{rel} (created in the target since import)")
            else:
                cs.added.append(rel)
            continue

        if not target_abs.exists():
            cs.conflicts.append(f"{rel} (missing in the target folder)")
            continue

        cur_bytes = _read_copy(user_id, jail_rel)
        if cur_bytes is None:
            cs.conflicts.append(f"{rel} (unreadable copy)")
            continue
        cur_sha = hashlib.sha256(cur_bytes).hexdigest()

        if cur_sha == base_entry["sha256"]:
            cs.unchanged += 1
            continue

        # Copy changed -> we would write it. Only safe while the target still matches the
        # baseline we imported; anything else is a conflict and gets refused (REQ-044).
        if sha256_file(target_abs) != base_entry["sha256"]:
            cs.conflicts.append(f"{rel} (changed in the target since import)")
        else:
            cs.modified.append(rel)

    return cs


def _current_copy_paths(user_id: str, link: dict) -> dict[str, str | None]:
    """rel -> content hash for every file currently in the jailed copy.

    Reads raw bytes directly rather than through workspaces.read_bytes(): the manifest holds
    PLAINTEXT hashes, so a copy stored under encryption at rest must be decrypted before it
    can be compared, or every encrypted-at-rest file would look permanently modified. The
    apply path refuses outright in that configuration (REQ-047), which keeps this honest:
    preview is accurate, and the write-back that could expose plaintext never runs.

    Hash is None when the file cannot be read; callers treat that as "present but opaque".
    """
    base = copy_root(user_id, link)
    out: dict[str, str | None] = {}
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            if rel == MANIFEST_NAME:
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                out[rel] = None
                continue
            if crypto.is_encrypted_blob(raw):
                try:
                    raw = crypto.decrypt_bytes(raw)
                except Exception:  # noqa: BLE001 — opaque file: report, never crash a preview
                    out[rel] = None
                    continue
            out[rel] = hashlib.sha256(raw).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Staging brand-new files into the baseline (used by sync_apply)
# ---------------------------------------------------------------------------

def stage_new_baselines(user_id: str, rels: list[str], link: dict | None = None) -> int:
    """Record ``_absent`` baseline entries for files an agent created in the copy.

    A brand-new file has no content hash to compare against, so its safety condition is
    "the target still does not have this path". Storing that as a baseline entry means
    preview and apply check new files and modified files through exactly the same code path
    — one rule, no special case at apply time.

    Callers MUST re-run diff_project() afterwards: staging happens immediately before the
    final conflict check so a file created in the target between those two calls is still
    caught and refused rather than clobbering it.
    """
    if not rels:
        return 0
    manifest = load_manifest(user_id, link)
    files: dict = manifest.get("files") or {}
    n = 0
    for rel in rels:
        if rel in files:
            continue
        files[rel] = {"sha256": "", "size": 0, "mtime": 0, "_absent": True}
        n += 1
    manifest["files"] = files
    save_manifest(user_id, manifest, link)
    return n
