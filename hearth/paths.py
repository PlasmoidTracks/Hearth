"""Filesystem layout for Hearth data. All mutable state lives under DATA_DIR.

Override the base with env HEARTH_DATA_DIR. Subdirectories are created lazily
via ensure_dirs(). Nothing secret belongs in these dirs except the DB (which
holds hashed passwords + signed-token revocations, never plaintext secrets).
"""
from __future__ import annotations
import os
from pathlib import Path

# Default data dir: ./data relative to CWD unless overridden.
DATA_DIR = Path(os.environ.get("HEARTH_DATA_DIR", str(Path.cwd() / "data"))).resolve()

DB_PATH = DATA_DIR / "hearth.sqlite3"
WORKSPACES_DIR = DATA_DIR / "workspaces"          # per-user jails: <uid>/...
PROTOCOLS_DIR = DATA_DIR / "protocols"            # admin .md protocols (mirror)
SYSTEM_PROMPTS_DIR = DATA_DIR / "system_prompts"  # admin .md system prompts
DOWNLOADS_TMP = DATA_DIR / "downloads"            # produced-file staging for uploads
LOGS_DIR = DATA_DIR / "logs"
RUNTIME_DIR = DATA_DIR / "runtime"                # llama-server pid/log files


def workspace_root(user_id: str) -> Path:
    """Root of a user's jail. NOT canonicalized here (callers use workspaces.py)."""
    return WORKSPACES_DIR / str(user_id)


def ensure_dirs() -> None:
    for d in (DATA_DIR, WORKSPACES_DIR, PROTOCOLS_DIR, SYSTEM_PROMPTS_DIR,
              DOWNLOADS_TMP, LOGS_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)
