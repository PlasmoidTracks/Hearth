"""Durable artifact records for user files (produced by agent or uploaded by user).

An artifact is a workspace file + metadata row so it can be listed and re-downloaded
later, per session. Downloads are authorized by ownership at /api/artifact/{id}; no
separate secret token is required (the id is opaque but authorization is enforced).
"""
from __future__ import annotations
import mimetypes
import os

from .db import session_scope
from .models import Artifact
from . import workspaces as ws


def record(user_id: str, rel_path: str, *, conversation_id: str = "",
           kind: str = "produced", filename: str | None = None,
           plain_size: int | None = None, new_row: bool = False) -> dict:
    """Create/refresh an artifact record for an existing workspace file. Returns dict.

    `plain_size` overrides the on-disk size when known — with encryption at rest the
    file bytes are ciphertext, so callers that know the plaintext size (uploads) pass
    it in; otherwise we report the decrypted size via workspaces.read_bytes.

    One row per (user, workspace path): a file is one artifact, so re-offering it does
    NOT pile up duplicates — the existing row is refreshed and returned unchanged id
    (see DEC-040). That makes `fs_write` followed by `upload` on the same file list it
    once instead of twice. Pass `new_row=True` when a fresh record really is wanted
    (a user uploading the same filename again is a new transfer with its own size/mime).
    """
    abs_path = ws.resolve_in_jail(user_id, rel_path)
    if plain_size is not None:
        size = plain_size
    elif os.path.isfile(abs_path):
        try:
            size = len(ws.read_bytes(user_id, rel_path))
        except Exception:  # noqa: BLE001 — fall back to on-disk size
            size = os.path.getsize(abs_path)
    else:
        size = 0
    fname = filename or os.path.basename(rel_path) or "file"
    mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    norm = _norm_rel(rel_path)
    with session_scope() as s:
        # Monotonic sequence, not wall-clock seconds (see models.SCHEMA_VERSION): two
        # records made in the same second must still order correctly, otherwise
        # "newest" below is decided by luck.
        seq = _next_seq(s)
        if new_row:
            a = Artifact(user_id=user_id, conversation_id=conversation_id or "", kind=kind,
                         filename=fname, rel_path=norm, size=size, mime=mime, created_at=seq)
            s.add(a)
            s.flush()
            return _to_dict(a)
        existing = (s.query(Artifact)
                    .filter(Artifact.user_id == user_id, Artifact.rel_path == norm)
                    .order_by(Artifact.created_at.desc())
                    .first())
        if existing is not None:
            # Same file, already registered — refresh metadata in place. Keeping the id
            # stable means download links handed out earlier stay valid and point at the
            # same (now updated) content.
            existing.conversation_id = conversation_id or ""
            existing.kind = kind
            existing.filename = fname
            existing.size = size
            existing.mime = mime
            existing.created_at = seq
            return _to_dict(existing)
        a = Artifact(user_id=user_id, conversation_id=conversation_id or "", kind=kind,
                     filename=fname, rel_path=norm, size=size, mime=mime, created_at=seq)
        s.add(a)
        s.flush()
        return _to_dict(a)


def _next_seq(session) -> int:
    """Next value of the artifact ordering sequence.

    `artifacts.created_at` doubles as "sort order in the panel", but `now()` is whole
    seconds — so a write followed immediately by an upload (the exact reported flow)
    produced two rows with identical timestamps and `order_by(created_at desc)` picked
    one at random. This counter, stored on the settings row, guarantees strictly
    increasing values across processes as long as they share this DB.

    It also keeps epoch seconds for new installs: existing rows in a live database carry
    real timestamps, and a counter starting at 1 would sink every new artifact to the
    bottom of the panel. So the sequence is seeded from max(existing created_at).
    """
    from .models import Setting, now as _now
    key = "artifacts.seq"
    row = session.get(Setting, key)
    try:
        cur = int(row.value) if row and row.value else 0
    except (TypeError, ValueError):
        cur = 0
    if not cur:
        # Seed: never below the newest existing row, and never below "now".
        hi = 0
        try:
            top = session.query(Artifact.created_at).order_by(
                Artifact.created_at.desc()).limit(1).scalar()
            if isinstance(top, int):
                hi = top
        except Exception:  # noqa: BLE001 — table may not exist on a very first boot
            hi = 0
        cur = max(hi, _now())
    nxt = cur + 1
    if row is None:
        session.add(Setting(key=key, value=str(nxt)))
    else:
        row.value = str(nxt)
    return nxt


def _norm_rel(rel_path: str) -> str:
    """Canonical form of a workspace-relative path for artifact identity.

    `./a.txt`, `dir//a.txt` and `dir/a.txt` must denote the SAME artifact, otherwise
    dedupe silently fails and duplicates creep back in. Paths are already jail-safe by
    the time they get here; this only normalises separators/segments.
    """
    return (rel_path or "").replace("\\", "/").strip().strip("/")


def list_for(user_id: str, conversation_id: str | None = None, limit: int = 200) -> list[dict]:
    with session_scope() as s:
        q = s.query(Artifact).filter(Artifact.user_id == user_id)
        if conversation_id is not None:
            q = q.filter(Artifact.conversation_id == conversation_id)
        # Secondary sort on id: rows that predate the monotonic sequence (or were written
        # in the same tick) still get a stable, non-random order.
        rows = (q.order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .limit(limit).all())
    return [_to_dict(a) for a in rows]


def get(user_id: str, artifact_id: str) -> dict | None:
    """Fetch an artifact only if it belongs to user_id (ownership enforced)."""
    with session_scope() as s:
        a = s.get(Artifact, artifact_id)
        if not a or a.user_id != user_id:
            return None
        return _to_dict(a)


def delete(user_id: str, artifact_id: str) -> bool:
    """Remove the DB row only (leaves the workspace file in place)."""
    with session_scope() as s:
        a = s.get(Artifact, artifact_id)
        if not a or a.user_id != user_id:
            return False
        s.delete(a)
        return True


def _to_dict(a: Artifact) -> dict:
    return {"id": a.id, "kind": a.kind, "filename": a.filename, "rel_path": a.rel_path,
            "size": a.size, "mime": a.mime, "created_at": a.created_at,
            "conversation_id": a.conversation_id, "download_url": f"/api/artifact/{a.id}"}
