"""Per-user workspace jail (DEC-005). Blocks traversal & symlink escapes.

Every filesystem tool call resolves the requested path against the user's root
and verifies the *canonical* result stays inside it. Symlinks that point outside
are rejected because we canonicalize before containment-checking, and refuse to
follow symlinks whose target escapes the jail.
"""
from __future__ import annotations
import os
from pathlib import Path

from . import paths
from . import crypto


def _read_blob(p: Path, max_bytes: int) -> bytes:
    """Read a file and transparently decrypt when encryption is on.

    We always read the full file (ciphertext framing makes byte caps meaningless
    pre-decryption); callers cap the *decoded* result afterwards.
    """
    with open(p, "rb") as fh:
        data = fh.read()
    if crypto.encryption_enabled() or crypto.is_encrypted_blob(data):
        data = crypto.decrypt_bytes(data)   # raises on wrong key — fail loudly
    return data


def _write_blob(p: Path, data: bytes) -> None:
    """Write raw bytes to a jail path, encrypting when enabled."""
    if crypto.encryption_enabled():
        data = crypto.encrypt_bytes(data)
    with open(p, "wb") as fh:
        fh.write(data)


class JailEscape(Exception):
    pass


def root_for(user_id: str) -> Path:
    root = paths.workspace_root(user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_in_jail(user_id: str, rel_path: str, *, allow_missing: bool = True) -> Path:
    """Return canonical path guaranteed to be under the user's root.

    - Rejects absolute paths and any '..' traversal.
    - For existing targets, resolves symlinks and re-checks containment.
    """
    root = root_for(user_id)
    rel = (rel_path or "").strip()
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise JailEscape("absolute paths are not allowed")
    # normalize separators; drop empty parts
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise JailEscape("path escapes workspace")
    # symlink check for existing targets
    if candidate.exists():
        real = Path(os.path.realpath(candidate))
        if real != root and root not in real.parents:
            raise JailEscape("symlink escapes workspace")
    elif not allow_missing:
        raise FileNotFoundError(str(candidate))
    return candidate


def rel_from_jail(user_id: str, abs_path: Path) -> str:
    root = root_for(user_id)
    return os.path.relpath(abs_path, root)


def list_tree(user_id: str, rel_path: str = "", max_entries: int = 500) -> list[dict]:
    p = resolve_in_jail(user_id, rel_path or ".", allow_missing=False)
    out: list[dict] = []
    for entry in sorted(os.scandir(p), key=lambda e: (e.is_file(), e.name.lower())):
        if len(out) >= max_entries:
            break
        # skip symlinked dirs pointing outside
        rp = Path(os.path.realpath(entry.path))
        root = root_for(user_id)
        if not (rp == root or root in rp.parents):
            continue
        st = entry.stat(follow_symlinks=False)
        out.append({
            "name": rel_from_jail(user_id, Path(entry.path)),
            "is_dir": entry.is_dir(follow_symlinks=False),
            "size": st.st_size if entry.is_file(follow_symlinks=False) else None,
        })
    return out


def read_text(user_id: str, rel_path: str, max_bytes: int = 256_000) -> str:
    p = resolve_in_jail(user_id, rel_path, allow_missing=False)
    data = _read_blob(p, max_bytes)
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", "replace")
    return text + ("\n…[truncated]" if truncated else "")


def read_bytes(user_id: str, rel_path: str) -> bytes:
    """Full-file binary read (decrypted). Used for downloads/attachments."""
    p = resolve_in_jail(user_id, rel_path, allow_missing=False)
    with open(p, "rb") as fh:
        blob = fh.read()
    if crypto.encryption_enabled() or crypto.is_encrypted_blob(blob):
        blob = crypto.decrypt_bytes(blob)
    return blob


def write_text(user_id: str, rel_path: str, content: str) -> str:
    p = resolve_in_jail(user_id, rel_path)  # allow_missing True (creating)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_blob(p, content.encode("utf-8"))
    return rel_from_jail(user_id, p)


def write_bytes(user_id: str, rel_path: str, data: bytes) -> str:
    p = resolve_in_jail(user_id, rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_blob(p, data)
    return rel_from_jail(user_id, p)


def seal_bytes(dest: Path, plain: bytes) -> str:
    """Encrypt-on-write (if enabled) an already-resolved jail path from plaintext bytes.

    Used by the upload route: stream to a temp file, then hand the bytes here so
    encryption stays a workspaces.py concern and never leaks into route code.
    Returns the absolute destination path as a string.
    """
    _write_blob(dest, plain)
    return str(dest)


def append_text(user_id: str, rel_path: str, content: str) -> str:
    p = resolve_in_jail(user_id, rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if crypto.encryption_enabled():
        # read-modify-write (whole file re-sealed under a fresh nonce)
        existing = read_bytes(user_id, rel_from_jail(user_id, p)) if p.exists() else b""
        _write_blob(p, existing + content.encode("utf-8"))
    else:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(content)
    return rel_from_jail(user_id, p)


def edit_text(user_id: str, rel_path: str, old_text: str, new_text: str,
              replace_all: bool = False) -> tuple[int, str]:
    """Exact search-and-replace inside one jailed text file.

    Returns (replacements, rel_path). Raises ValueError when the pattern is
    missing or (unless replace_all) matches more than once — mirroring the
    unique-match discipline of coding-agent edit tools so models must quote
    enough context to disambiguate.
    """
    p = resolve_in_jail(user_id, rel_path, allow_missing=False)
    with open(p, "rb") as fh:
        blob = fh.read()
    if crypto.encryption_enabled() or crypto.is_encrypted_blob(blob):
        blob = crypto.decrypt_bytes(blob)
    text = blob.decode("utf-8", "replace")
    if not old_text:
        raise ValueError("old_text must not be empty")
    n = text.count(old_text)
    if n == 0:
        raise ValueError("old_text not found — re-read the file and match it exactly (whitespace included)")
    if n > 1 and not replace_all:
        raise ValueError(f"old_text matches {n} places — add surrounding context or set replace_all=true")
    updated = text.replace(old_text, new_text)
    _write_blob(p, updated.encode("utf-8"))
    return (n if replace_all else 1), rel_from_jail(user_id, p)


def delete_path(user_id: str, rel_path: str) -> bool:
    """Delete a single file inside the jail. Directories are refused for now."""
    p = resolve_in_jail(user_id, rel_path, allow_missing=False)
    if not p.is_file():
        return False
    os.remove(p)
    return True


def move_path(user_id: str, from_rel: str, to_rel: str, overwrite: bool = False) -> str:
    src = resolve_in_jail(user_id, from_rel, allow_missing=False)
    dst = resolve_in_jail(user_id, to_rel)
    if dst.exists() and not overwrite:
        raise FileExistsError(f"destination exists: {to_rel} (set overwrite=true to replace)")
    if src == dst:
        return rel_from_jail(user_id, src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return rel_from_jail(user_id, dst)
