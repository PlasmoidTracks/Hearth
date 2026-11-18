"""Applying an approved diff to a linked folder, with backups and revert.

DEC-049 / CONTRACT v1.3 (REQ-044..048). Split out from project_link.py because this is the
only module in Hearth that writes outside a user's jail, so it gets its own reviewable
boundary: everything here is either a pure helper or an apply/revert of a previously
previewed change.

Safety model — deliberately *not* path-based (see DEC-049 "Rejected: guard-path list"):
  * the caller must have previewed, and the user must approve the apply (tool-level gate);
  * every target is re-checked against the baseline manifest immediately before writing, so
    a file the user edited elsewhere is refused rather than silently overwritten;
  * every overwritten file is copied into a per-apply backup first, making revert one action;
  * writes go to a temp file in the destination directory then os.replace(), so a crash
    cannot leave a half-written project file;
  * workspace encryption at rest refuses apply outright (writing jailed bytes out raw would
    put ciphertext in the user's project);
  * the OS backend is auto-detected and anything unsupported returns a refusal instead of a
    partial write, mirroring sandbox.detect_backend()'s fail-closed `stub` behaviour.

No path, filename, or project type is treated specially: a linked folder is whatever the
user says it is.
"""
from __future__ import annotations

import os
import platform
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from . import project_link as pl


# ---------------------------------------------------------------------------
# Backend detection (fail-closed, sandbox.py style)
# ---------------------------------------------------------------------------

def detect_backend(preference: str = "auto") -> str:
    """Which apply backend to use. Never falls back to something unsafe.

    'local' writes directly through the filesystem and is correct on Linux/macOS and any
    POSIX-ish system where Hearth and the target share a mount. Windows also uses it (the
    primitives used here — os.replace, temp-file-in-directory — are correct there too).
    Anything else, or an explicitly unsupported preference, becomes 'stub' which refuses.
    """
    if preference != "auto":
        return preference if preference in ("local", "stub") else "stub"
    system = platform.system()
    if system in ("Linux", "Darwin", "Windows"):
        # Android reports Linux but its storage model (scoped storage / FUSE uids) is not a
        # plain shared mount; refuse there rather than half-working.
        if _is_android():
            return "stub"
        return "local"
    return "stub"


def _is_android() -> bool:
    """Best-effort Android detection (Termux reports sys.platform='linux')."""
    if hasattr(os, "getuid") and os.getuid() == 0 and os.environ.get("ANDROID_ROOT"):
        return True
    return bool(os.environ.get("ANDROID_DATA") or os.environ.get("ANDROID_ROOT"))


REFUSE_STUB = (
    "Applying changes to a real folder is not supported on this system "
    "(no usable apply backend was detected). Nothing was written. Hearth can still edit the "
    "project copy inside your workspace — an admin can enable a write path for this platform "
    "by pairing a device helper (see Admin → Tools → Device)."
)

REFUSE_ENCRYPTION = (
    "Applying changes to a linked folder is refused while workspace encryption at rest is "
    "enabled (HEARTH_WORKSPACE_ENCRYPTION), because the files Hearth would copy out are stored "
    "encrypted and writing them raw would put unreadable ciphertext into your project. Turn "
    "workspace encryption off, or work in the workspace copy only. Nothing was written."
)


@dataclass
class ApplyResult:
    ok: bool
    written: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    backup_id: str = ""
    backend: str = "local"
    note: str = ""


# ---------------------------------------------------------------------------
# Backups (per apply) + revert
# ---------------------------------------------------------------------------

BACKUP_PREFIX = ".hearth_backups"


def _backup_root(user_id: str, link: dict) -> Path:
    """Backup dir lives INSIDE the linked folder, prefixed with a dot.

    Inside the target so a revert works even if the jail copy is later re-imported or the
    conversation is deleted; dotted so ordinary project tooling (and our own import, via
    SKIP_DIRS-style dot skipping) ignores it.
    """
    return pl.target_root(link) / BACKUP_PREFIX


def _new_backup_dir(user_id: str, link: dict) -> Path:
    bid = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{os.getpid()}"
    d = _backup_root(user_id, link) / bid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_copy(target_abs: Path, backup_dir: Path, rel: str) -> None:
    dest = backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target_abs, dest)


def list_backups(user_id: str, link: dict | None = None) -> list[dict]:
    link = link or pl.get_link(user_id)
    if not link:
        return []
    root = _backup_root(user_id, link)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        out.append({"id": d.name, "files": len(files),
                    "bytes": sum(p.stat().st_size for p in files)})
    return out


def revert(user_id: str, backup_id: str, link: dict | None = None) -> ApplyResult:
    """Restore every file stored in one backup, byte-for-byte.

    Revert is intentionally unconditional: it puts back exactly what that apply replaced,
    which is the point of having taken the backup. It writes only paths present in the
    backup, so it cannot invent files.
    """
    link = link or pl.get_link(user_id)
    if not link:
        raise pl.LinkError("no project folder is linked")
    if not backup_id or "/" in backup_id or "\\" in backup_id or backup_id.startswith("."):
        raise pl.LinkError("invalid backup id")
    backend = detect_backend("auto")
    if backend != "local":
        return ApplyResult(False, backend=backend, note=REFUSE_STUB)

    bdir = _backup_root(user_id, link) / backup_id
    if not bdir.is_dir():
        raise pl.LinkError(f"no such backup: {backup_id}")

    root = pl.target_root(link)
    restored: list[str] = []
    refused: list[str] = []
    for p in sorted(bdir.rglob("*")):
        if not p.is_file():
            continue
        rel = os.path.relpath(p, bdir).replace(os.sep, "/")
        dest = root / rel
        canon_dest = dest.resolve() if dest.exists() else (dest.parent.resolve() / dest.name)
        if _outside(root, canon_dest):
            refused.append(f"{rel} (escapes the linked folder)")
            continue
        try:
            _atomic_write(canon_dest, p.read_bytes())
            restored.append(rel)
        except OSError as e:
            refused.append(f"{rel}: {str(e)[:120]}")
    return ApplyResult(not refused, written=restored, refused=refused,
                       backup_id=backup_id, backend=backend,
                       note=f"reverted {len(restored)} file(s) from backup {backup_id}")


def _outside(root: Path, candidate: Path) -> bool:
    cand = Path(candidate)
    return not (cand == root or root in cand.parents)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _atomic_write(dest: Path, data: bytes) -> None:
    """Write via temp-file-in-the-same-directory + os.replace().

    Same-directory matters: a cross-device rename would fail (or silently degrade to a copy
    that is not atomic), and a crash mid-write must never leave a truncated project file.
    Existing file mode is preserved where the OS allows, so an executable script stays
    executable instead of becoming 0644 on revert/apply.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.hearth-tmp"
    mode = None
    if dest.exists():
        try:
            mode = dest.stat().st_mode & 0o7777
        except OSError:
            mode = None
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_changes(user_id: str, settings: dict, link: dict | None = None) -> ApplyResult:
    """Write the approved diff to the real folder. Refuses rather than degrade.

    Re-computes the diff and re-checks every target against the baseline immediately before
    writing (REQ-044), so approval of an older preview cannot slip a stale overwrite past a
    file the user edited in the meantime. Conflicts are skipped, never merged; the rest is
    applied only if there are no conflicts at all — a partial apply of an interdependent
    project change is worse than none, so the caller resolves conflicts and retries.
    """
    link = link or pl.get_link(user_id)
    if not link:
        raise pl.LinkError("no project folder is linked yet")

    backend = detect_backend(str(settings.get("project.apply_backend") or "auto"))
    if backend == "stub":
        return ApplyResult(False, backend=backend, note=REFUSE_STUB)

    # REQ-047: never write encrypted-on-disk workspace bytes out to a real project.
    if crypto.encryption_enabled():
        return ApplyResult(False, backend=backend, note=REFUSE_ENCRYPTION)

    # Read the copy through a path that is guaranteed NOT to decrypt: apply must never be
    # the thing that silently writes decrypted-then-reencrypted bytes (or, worse, ciphertext)
    # into the user's project. diff_project() already hashes these same raw bytes, so preview
    # and apply agree on what "the workspace version" is.
    prefix_root = pl.copy_root(user_id, link)

    cs = pl.diff_project(user_id, link)
    if cs.conflicts:
        return ApplyResult(False, refused=[f"CONFLICT {c}" for c in cs.conflicts],
                           backend=backend,
                           note=(f"{len(cs.conflicts)} file(s) changed in the target folder "
                                 f"since import. Nothing was written — re-import to refresh "
                                 f"the baseline, or revert your workspace edits."))

    todo = cs.applyable
    if not todo:
        return ApplyResult(True, backend=backend, note="nothing to apply")

    # Stage brand-new files so their safety condition ("target still absent") is recorded in
    # the same manifest-based check used for modified files, then re-diff. If the user
    # created one of those paths in the target meanwhile, this second pass turns it into a
    # conflict and we bail out before writing anything.
    pl.stage_new_baselines(user_id, cs.added, link)
    cs2 = pl.diff_project(user_id, link)
    if cs2.conflicts:
        return ApplyResult(False, refused=[f"CONFLICT {c}" for c in cs2.conflicts],
                           backend=backend,
                           note=("the target folder changed during apply preparation; nothing "
                                 "was written — preview again"))
    todo = cs2.applyable
    if not todo:
        return ApplyResult(True, backend=backend, note="nothing to apply")

    root = pl.target_root(link)
    bdir = _new_backup_dir(user_id, link)
    manifest = pl.load_manifest(user_id, link)
    baseline: dict = manifest.get("files") or {}

    written: list[str] = []
    refused: list[str] = []
    for rel in todo:
        src_copy = prefix_root / rel
        try:
            data = src_copy.read_bytes()
        except OSError:
            refused.append(f"{rel} (workspace copy unreadable)")
            continue
        dest = root / rel
        canon = dest.resolve() if dest.exists() else (dest.parent.resolve() / dest.name)
        if _outside(root, canon):
            refused.append(f"{rel} (escapes the linked folder)")
            continue
        # Last gate: the target must still be exactly what we imported.
        base = baseline.get(rel) or {}
        if base.get("_absent"):
            if dest.exists():
                refused.append(f"{rel} (target created this file meanwhile)")
                continue
        else:
            if not dest.exists() or pl.sha256_file(dest) != base.get("sha256"):
                refused.append(f"{rel} (target changed since check)")
                continue
            _backup_copy(dest, bdir, rel)
        try:
            _atomic_write(canon, data)
            written.append(rel)
        except OSError as e:
            refused.append(f"{rel}: {str(e)[:120]}")

    if not written:
        note = "nothing was written"
    elif refused:
        note = (f"wrote {len(written)} file(s); {len(refused)} refused. Restore with the "
                f"backup id {bdir.name} if this is not what you wanted.")
    else:
        note = f"wrote {len(written)} file(s). Backup id {bdir.name} restores the previous state."

    # Refresh the baseline for everything we wrote so a follow-up preview is honest.
    _rebaseline(user_id, link, written, root)

    return ApplyResult(not refused, written=written, refused=refused,
                       backup_id=bdir.name if written else "", backend=backend, note=note)


def _rebaseline(user_id: str, link: dict, rels: list[str], root: Path) -> None:
    """Update manifest entries for applied files to the state now on disk."""
    if not rels:
        return
    manifest = pl.load_manifest(user_id, link)
    files: dict = manifest.get("files") or {}
    for rel in rels:
        p = root / rel
        if p.is_file():
            files[rel] = pl.file_entry(p)
    manifest["files"] = files
    pl.save_manifest(user_id, manifest, link)
