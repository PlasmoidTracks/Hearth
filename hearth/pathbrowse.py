"""Server-side folder/file browsing for the admin settings UI (DEC-080).

Admin → Model / Audio / Images ask for absolute paths on the machine *running
Hearth*: a llama-server binary, .gguf weights, an mmproj, an sd-cli build. The
browser cannot help here — `<input type=file>` gives a fake path inside the
browser's sandbox and never the real server path, so typing long paths by hand
(or copy-pasting them from a terminal) is the only option that works today.

This module answers "what is in that directory?" for one authenticated admin.
Deliberately **not** jailed: these files legitimately live anywhere (a models
directory, a build tree, an external drive), and the operator already holds full
filesystem access through Admin → Tools → Command execution. The gate is
`require_admin` + CSRF at the route, not a path allowlist; every listing is
audited so the trail shows what was looked at.

Read-only by construction — there is no write, move, or delete anywhere in this
module, and it never follows a symlink into an action. It only stats entries.

Blocking (`os.scandir`, `stat`) like the rest of the fs layer here; callers wrap
it in `asyncio.to_thread` so a listing of a huge or network-mounted directory
cannot stall the event loop.
"""
from __future__ import annotations
import os
import platform
import re
import string

# How many entries one listing returns before it says so. A model directory with
# 40 GB of GGUFs is normal; a drive root with hundreds of thousands of loose
# files is also normal, and neither should hang the request or silently truncate.
MAX_ENTRIES = 800

# Cap on how many drives we are willing to probe (Windows only).
MAX_DRIVES = 32


def _is_windows() -> bool:
    """OS of the machine running Hearth — what a path refers to, not the browser's."""
    return platform.system() == "Windows"


def is_server_path(key: str) -> bool:
    """Whether a setting key holds an absolute path on THIS machine.

    Derived from the same naming convention the specs already follow rather than
    a hand-maintained list, so a new `*_path` / `*_bin` / `*_dir` setting gets a
    Browse button without anyone remembering to register it. The handful of `str`
    settings that merely *mention* paths in their help text (device lists, extra
    CLI args) do not match — they name devices and flags, not files.
    """
    return key.endswith(("_path", "_bin", "_dir"))


def _parent(path: str) -> str:
    """Directory containing `path`; unchanged when there is nowhere above it."""
    p = os.path.dirname(os.path.abspath(str(path)))
    return p or "/"


def drives() -> list[str]:
    """Top-level places to start from.

    POSIX: just the filesystem root — `/` already shows home, /opt, mounted
    volumes, everything. Windows has no single root, so each drive letter becomes
    an entry (probed for existence; `D:\\` is skipped when there is no D:).
    """
    if not _is_windows():
        return ["/"]
    out: list[str] = []
    for letter in string.ascii_uppercase[:MAX_DRIVES]:
        cand = f"{letter}:\\"
        try:
            if os.path.exists(cand):
                out.append(cand)
        except OSError:
            continue
    return out or ["C:\\"]


def resolve(raw: str) -> str:
    """Canonical absolute path for a user-supplied string.

    Expands `~` and `$HOME` (a pasted shell path is the common case when someone
    types rather than browses), then normalises without resolving symlinks — so
    browsing through a symlink behaves like the shell does instead of teleporting
    to some unrelated real directory. Returns "" for anything that cannot be made
    absolute, which callers report as "not found" rather than guessing cwd.
    """
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    try:
        s = os.path.expanduser(s)
        s = os.path.expandvars(s)
        if not os.path.isabs(s):
            # A bare drive letter on Windows ("C:") means that drive's root, and
            # a relative path is meaningless here — refuse rather than interpret
            # it against Hearth's working directory.
            if _is_windows() and re.match(r"^[A-Za-z]:[\\/]?$", s):
                return s.upper()
            return ""
        norm = os.path.normpath(s)
        return norm
    except Exception:  # noqa: BLE001 — never let a weird string break the panel
        return ""


def _drive_list() -> list[dict]:
    return [{"name": d, "path": d, "is_dir": True, "size": None} for d in drives()]


def browse(raw_path: str = "", q: str = "") -> dict:
    """List one directory on the server. Never raises; `error` explains refusals.

    Keys: ok, path (canonical), parent, entries[{name,path,is_dir,size,mtime}],
    truncated, error, and — for a file or a missing path — `parent_note`, which is
    the folder worth opening instead. That note is what makes "Browse" land in the
    right place when the box already holds a path: pick the folder, not the drive.
    """
    q = str(q or "").strip()
    p = resolve(raw_path)

    # No path yet → start somewhere useful rather than erroring out.
    if not p:
        listing = _browse_drives_or_home(q)
        listing["error"] = "" if listing.get("entries") is not None else \
            "Enter a folder path, or press ↩ to go to the folder of the current value."
        return listing

    # A file in the box (the common "already has a value" case): open its folder.
    if os.path.isfile(p):
        parent = _parent(p)
        out = browse(parent, q)
        # `path` stays the FILE that was asked about — the client keys off
        # parent_note to say "showing the folder of …". Overwriting it with the
        # file path made every caller lose track of which directory it was in.
        out["parent_note"] = parent
        return out

    if not os.path.exists(p):
        # A half-typed or stale path is the normal state of a settings box, so
        # this must still leave the admin somewhere: show the deepest folder that
        # DOES exist (surfaced as parent_note) and fall back to drive/root listing.
        parent = _parent(p)
        if os.path.isdir(parent):
            out = browse(parent, q)
            out["ok"] = False
            out["error"] = f"Path not found: {p}"
            out["parent_note"] = parent
            return out
        out = _browse_drives_or_home(q)
        out["error"] = f"Path not found: {p}"
        return out

    if not os.path.isdir(p):
        return {"ok": False, "path": p, "parent": _parent(p), "entries": [],
                "truncated": False, "error": f"Not a folder: {p}",
                "parent_note": _parent(p)}

    try:
        with os.scandir(p) as it:
            raw_entries = []
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    st = None if is_dir else e.stat(follow_symlinks=False)
                    raw_entries.append({
                        "name": e.name + ("/" if is_dir else ""),
                        "path": os.path.join(p, e.name),
                        "is_dir": is_dir,
                        "size": None if is_dir else (st.st_size if st else None),
                        "mtime": st.st_mtime if st else 0,
                    })
                except OSError:
                    # An entry we cannot stat (broken symlink, permission race)
                    # still deserves to be listed — hiding it would make a real
                    # file undiscoverable.
                    raw_entries.append({"name": e.name, "path": os.path.join(p, e.name),
                                        "is_dir": False, "size": None, "mtime": 0})
    except PermissionError:
        return {"ok": False, "path": p, "parent": _parent(p), "entries": [],
                "truncated": False,
                "error": f"Permission denied reading {p} — Hearth runs as a user who "
                         f"cannot open this folder.",
                "parent_note": _parent(p)}
    except OSError as e:
        return {"ok": False, "path": p, "parent": _parent(p), "entries": [],
                "truncated": False, "error": f"Cannot list {p}: {type(e).__name__}: {str(e)[:160]}",
                "parent_note": ""}

    if q:
        needle = q.lower()
        raw_entries = [e for e in raw_entries if needle in e["name"].lower()]

    # Folders first, then case-insensitive by name — deterministic, and matching
    # how a file manager presents a directory. mtime is sent along for the client
    # to display, never used to reorder (a changing sort is hard to aim at).
    raw_entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    total = len(raw_entries)
    entries = raw_entries[:MAX_ENTRIES]

    # "Up" is the parent folder, except at a Windows drive root where the next
    # thing up is the drive list — handled by passing an empty path.
    parent = _parent(p)
    if _is_windows() and os.path.normcase(parent) == os.path.normcase(p):
        parent = ""

    return {"ok": True, "path": p, "parent": parent, "entries": entries,
            "truncated": total > MAX_ENTRIES,
            "total": total,
            "error": "",
            "parent_note": ""}


def _browse_drives_or_home(q: str = "") -> dict:
    """The 'nowhere to start yet' view: drives on Windows, root elsewhere."""
    if _is_windows():
        return {"ok": True, "path": "", "parent": "", "entries": _drive_list(),
                "truncated": False, "total": len(drives()), "error": "", "parent_note": ""}
    out = browse("/", q)
    out["path"] = ""      # keep the address bar empty rather than showing "/"
    return out
