"""ORM models. Tables are created via db.init_db() (create_all).

Schema versioning is lightweight: SCHEMA_VERSION is recorded in a Settings row;
future migrations bump it and run idempotent ALTERs in db.migrate(). For the
prototype, create_all covers fresh installs.
"""
from __future__ import annotations
import time
import uuid
from sqlalchemy import String, Text, Integer, Boolean, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

SCHEMA_VERSION = 2   # v2: artifacts created_at is a monotonic counter (see db._migrate_artifacts_seq)


def _uid() -> str:
    return uuid.uuid4().hex


def now() -> int:
    return int(time.time())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    pw_hash: Mapped[str] = mapped_column(String, default="")   # scrypt/argon2 encoded
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str] = mapped_column(String, default="")  # base32 (plaintext; optional)
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    # UI language preference ("de" | "en"); see hearth/i18n.py. Default English.
    language: Mapped[str] = mapped_column(String, default="en")
    # Per-user behavioral preferences (JSON object of booleans), e.g.
    # {"enter_sends": true, "agent_mode_default": true, "auto_scroll": true,
    #  "show_timestamps": false}. See /api/prefs in routes_chat.py for the
    # accepted-key whitelist; unknown keys never get stored.
    prefs: Mapped[str] = mapped_column(Text, default="{}")
    # per-user tool toggles (JSON string): {"exec": true, "web": true, ...}
    tool_flags: Mapped[str] = mapped_column(Text, default="{}")
    # The user's personal system prompt (editable from the chat sidebar). Appended to
    # the built-in persona for every chat unless a conversation picks an explicit
    # admin doc. Empty = no personal instructions. See /api/chat/system-prompt.
    system_prompt: Mapped[str] = mapped_column(Text, default="")


class Session(Base):
    """Server-side session for revocable tokens."""
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    expires_at: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str] = mapped_column(String, default="")


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String, default="New chat")
    system_prompt_doc_id: Mapped[str] = mapped_column(String, default="")
    protocol_doc_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    updated_at: Mapped[int] = mapped_column(Integer, default=now)
    # Per-conversation "Allow device access" grant (the chat-header button). Server-side
    # so the model can't be talked past it; see tools/device_tools._gate.
    device_granted: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String)   # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    # for tool messages / assistant tool requests: JSON string
    meta: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class PendingAction(Base):
    """A gated tool call or ask_user awaiting user resolution (DEC-010)."""
    __tablename__ = "pending_actions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)   # exec | ask_user
    tool_name: Mapped[str] = mapped_column(String, default="")
    call_id: Mapped[str] = mapped_column(String, default="")  # OpenAI tool_call id to reply to
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON: args etc.
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|approved|denied|answered
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class Doc(Base):
    """Admin-managed markdown: protocol or system_prompt."""
    __tablename__ = "docs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    kind: Mapped[str] = mapped_column(String, index=True)  # protocol | system_prompt
    name: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    visible_to_users: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=now)


class Setting(Base):
    """Runtime (admin-editable) key/value store."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    # NOTE: SQLite only auto-increments a rowid alias declared as plain INTEGER
    # PRIMARY KEY; BigInteger would not autoincrement and inserts fail. Use Integer.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(Integer, default=now, index=True)
    user_id: Mapped[str] = mapped_column(String, default="")
    action: Mapped[str] = mapped_column(String, default="")   # login|exec|fs_write|...
    detail: Mapped[str] = mapped_column(Text, default="")


class Artifact(Base):
    """A durable file associated with a user (and optionally a conversation).

    Created for BOTH agent-produced files (upload tool / fs_write) and user uploads.
    The workspace-relative path is the source of truth; downloads go through
    /api/artifact/{id} which enforces ownership, so links survive restarts and can be
    re-listed per session (replaces reliance on ephemeral in-memory download tokens).
    """
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    # Owner key (matches the workspace jail dir). NOT a hard FK to users: workspaces
    # are created lazily by user-id string and can exist independent of an account row;
    # an enforced FK here would silently drop artifacts. Ownership isolation is enforced
    # by querying/filtering on this indexed column, not by referential integrity.
    user_id: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String, default="", index=True)
    kind: Mapped[str] = mapped_column(String, default="produced")  # produced | uploaded
    filename: Mapped[str] = mapped_column(String, default="file")
    rel_path: Mapped[str] = mapped_column(String, default="")      # workspace-relative
    size: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String, default="application/octet-stream")
    created_at: Mapped[int] = mapped_column(Integer, default=now, index=True)


class ProjectLink(Base):
    """A user's link to a real folder on disk (DEC-049, REQ-041).

    Generic project-collaboration feature: Hearth copies a bounded subtree of the linked
    folder into the jail together with a baseline manifest, the agent works on that copy
    with the ordinary jailed fs tools, and an explicitly approved `sync_apply` propagates
    the diff back to the real folder. Nothing here restricts WHICH files or project type
    can be linked — the feature is deliberately content-agnostic (see DEC-049 "Rejected:
    guard-path list").

    One active link per user is enforced in code (project_link.set_link), not by a DB
    constraint, so history survives unlinking. `rel_prefix` is the workspace-visible prefix
    the copy lives under (e.g. ``project``); it is stored so preview/apply keep working if
    an operator later changes the default.

    NOT a hard FK to users: like Artifact.user_id, workspaces are keyed by user-id string
    and can exist independent of an account row. Ownership is enforced by filtering on this
    indexed column.
    """
    __tablename__ = "project_links"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String, index=True)
    target_abs: Mapped[str] = mapped_column(String, default="")   # canonical real folder
    rel_prefix: Mapped[str] = mapped_column(String, default="project")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    imported_at: Mapped[int] = mapped_column(Integer, default=0)  # last import (epoch s)
    created_at: Mapped[int] = mapped_column(Integer, default=now, index=True)
