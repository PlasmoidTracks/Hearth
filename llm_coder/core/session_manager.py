"""
Session manager: project-scoped session creation, listing, loading, autosave.

Sessions are stored as individual JSON files under
``<project_root>/.agent/sessions/<session_id>.json``.
Each session file contains: id, title, preview, created_at, updated_at, messages.

The session manager is responsible for:
- Creating a new active session when a project is opened
- Autosaving session state after each message turn
- Listing all sessions for the current project (sorted by created_at desc)
- Loading a session's messages back into the agent
- Deleting sessions (including the active one)
"""
import copy
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class Session:
    """Represents a single coding session with full message history and chat trace."""
    id: str
    project_path: str
    title: str
    preview: str
    created_at: float
    updated_at: float
    messages: List[Dict] = field(default_factory=list)
    chat_history: List[Dict] = field(default_factory=list)  # rendered chat entries for full-trace replay
    # Set when this session was branched off another one: the session id it
    # forked from, and how many of its messages the branch kept. Purely
    # informational — nothing reads it to decide behaviour — but it is what
    # makes "where did this branch come from?" answerable later, and what
    # keeps a duplicate's title from silently overwriting itself on the next
    # save (see _update_preview_from_messages).
    parent_id: Optional[str] = None
    fork_message_count: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Session":
        return Session(**data)


class SessionManager:
    """Manages project-scoped sessions. All sessions belong to one project."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.sessions_dir = self.project_root / ".agent" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._current_id: Optional[str] = None
        self._sessions_cache: Dict[str, Session] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, first_message: str = "") -> str:
        """Create a new session and make it the current one.

        Args:
            first_message: optional first user message used as preview/title.

        Returns:
            The session id of the newly created session.
        """
        sid = uuid.uuid4().hex[:12]
        # Test the STRIPPED text for emptiness, like the title below does.
        # Testing the raw string meant a whitespace-only first message ("   ")
        # was truthy and produced an empty preview instead of "(empty)".
        stripped = (first_message or "").strip()
        preview = stripped[:120] if stripped else "(empty)"
        # Use first 80 chars as title, same as preview but shorter
        title = stripped[:80] if stripped else "(untitled)"
        ts = time.time()
        session = Session(
            id=sid,
            project_path=str(self.project_root),
            title=title,
            preview=preview,
            created_at=ts,
            updated_at=ts,
            messages=[],
        )
        self._save_session_file(session)
        self._sessions_cache[sid] = session
        self._current_id = sid
        return sid

    def get_current_id(self) -> Optional[str]:
        """Return the id of the currently active session."""
        return self._current_id

    def set_current_session(self, session_id: str):
        """Set a session as the current one."""
        if session_id in self._sessions_cache:
            self._current_id = session_id
        else:
            # Load from disk
            session = self._load_session_file(session_id)
            if session:
                self._sessions_cache[session_id] = session
                self._current_id = session_id

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session object (from cache, loading from disk if needed)."""
        session = self._sessions_cache.get(session_id)
        if session is None:
            session = self._load_session_file(session_id)
            if session is not None:
                self._sessions_cache[session_id] = session
        return session

    def fork_session(
        self,
        session_id: str,
        keep_messages: Optional[int] = None,
        keep_chat_history: Optional[int] = None,
        title: Optional[str] = None,
        make_current: bool = True,
    ) -> Optional[Session]:
        """Duplicate a session, optionally cutting it short — the "branch" op.

        This is how you continue an alternate version from where you left off:
        the original is left completely untouched (it is read from a deep copy,
        so the branch shares no mutable state with it), and the new session
        becomes the current one.

        Args:
            session_id: the session to duplicate.
            keep_messages: keep only the first N raw agent messages. ``None``
                keeps all of them (a full duplicate).
            keep_chat_history: keep only the first N rendered chat entries.
                Defaults to the same value as ``keep_messages``; pass an
                explicit value because the two lists are NOT the same length —
                the rendered history carries display-only bubbles (tool traces,
                app notices) that have no raw counterpart. The caller resolves
                that mapping; this method just cuts.
            title: the branch's title. Set explicitly so the duplicate does not
                inherit the source's title verbatim and then look identical in
                the list.
            make_current: switch the manager (and the caller's UI, on the next
                ``get_messages()``) over to the branch.

        Returns:
            The new Session, or None if the source could not be loaded.
        """
        source = self.get_session(session_id)
        if source is None:
            return None

        # Deep copy, so the branch and its parent never alias the same dicts:
        # an in-place edit to a tool-call argument in one would otherwise show
        # up in the other, and the two are meant to diverge independently.
        new_session = copy.deepcopy(source)
        new_session.id = uuid.uuid4().hex[:12]

        messages = list(new_session.messages)
        if keep_messages is not None:
            messages = messages[:max(0, keep_messages)]
        new_session.messages = messages

        chat = list(new_session.chat_history)
        cut = keep_chat_history if keep_chat_history is not None else keep_messages
        if cut is not None:
            chat = chat[:max(0, cut)]
        new_session.chat_history = chat

        new_session.parent_id = source.id
        new_session.fork_message_count = len(messages)
        # Title is NOT defaulted here: the UI passes the branch label it
        # settled on (via next_branch_title), and an unconditional title is
        # also what pins the branch's identity against the re-derivation in
        # _update_preview_from_messages.
        ts = time.time()
        # created_at stays at the source's value so a branch sorts next to the
        # conversation it came from rather than jumping to the top of the list.
        new_session.updated_at = ts
        if title is not None:
            new_session.title = title
            new_session.preview = title[:120] if title else "(empty)"

        self._sessions_cache[new_session.id] = new_session
        self._save_session_file(new_session)
        if make_current:
            self._current_id = new_session.id
        return new_session

    def next_branch_title(self, base_title: str) -> str:
        """Pick a branch title that does not collide with an existing session.

        ``"Fix the parser"`` becomes ``"Fix the parser (branch)"``, then
        ``"... (branch 2)"``, ``"... (branch 3)"`` for repeat forks of the same
        conversation — so the sidebar list stays readable no matter how many
        alternate timelines the user spawns.
        """
        self._ensure_cache()
        base = (base_title or "(untitled)").strip() or "(untitled)"
        existing = {s.title for s in self._sessions_cache.values()}

        def _stem(t: str) -> str:
            """Strip a trailing ' (branch)' / ' (branch N)' so branching a
            branch extends the counter instead of stacking suffixes."""
            if t.endswith(")"):
                idx = t.rfind(" (branch")
                if idx != -1:
                    inner = t[idx + len(" (branch"):-1]
                    if inner == "" or inner.strip().isdigit():
                        return t[:idx]
            return t

        stem = _stem(base)
        candidate = f"{stem} (branch)"
        n = 2
        while candidate in existing:
            candidate = f"{stem} (branch {n})"
            n += 1
        return candidate

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def set_messages(self, messages: List[Dict], chat_history: Optional[List[Dict]] = None):
        """Update the messages of the current session and autosave."""
        if self._current_id is None:
            return
        session = self._sessions_cache.get(self._current_id)
        if session is None:
            session = self._load_session_file(self._current_id)
            if session is None:
                return
        session.messages = list(messages)
        if chat_history is not None:
            session.chat_history = list(chat_history)
        session.updated_at = time.time()
        # Update preview/title from messages if available
        self._update_preview_from_messages(session)
        self._sessions_cache[self._current_id] = session
        self._save_session_file(session)

    def get_messages(self) -> List[Dict]:
        """Get the messages for the current session."""
        if self._current_id is None:
            return []
        session = self._sessions_cache.get(self._current_id)
        if session is None:
            session = self._load_session_file(self._current_id)
            if session is None:
                return []
        return list(session.messages)

    def get_session_messages(self, session_id: str) -> List[Dict]:
        """Get messages for a specific session (by id)."""
        session = self._sessions_cache.get(session_id)
        if session is None:
            session = self._load_session_file(session_id)
        if session is None:
            return []
        return list(session.messages)

    def get_session_chat_history(self, session_id: str) -> List[Dict]:
        """Get the rendered chat history for a specific session (by id)."""
        session = self._sessions_cache.get(session_id)
        if session is None:
            session = self._load_session_file(session_id)
        if session is None:
            return []
        return list(session.chat_history)

    # ------------------------------------------------------------------
    # Listing / browsing
    # ------------------------------------------------------------------

    def list_sessions(self) -> List[Session]:
        """Return all sessions for this project, sorted by created_at desc."""
        # Ensure cache is populated
        self._ensure_cache()
        return sorted(
            self._sessions_cache.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )

    def _ensure_cache(self):
        """Rebuild the in-memory cache from disk so all sessions are listed."""
        self._sessions_cache.clear()
        for f in self.sessions_dir.glob("*.json"):
            try:
                session = self._load_session_file(f.stem)
                if session:
                    self._sessions_cache[session.id] = session
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_session(self, session_id: str) -> bool:
        """Delete a session file and remove from cache."""
        file_path = self.sessions_dir / f"{session_id}.json"
        if file_path.exists():
            file_path.unlink()
        self._sessions_cache.pop(session_id, None)
        if self._current_id == session_id:
            self._current_id = None
        return True

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _save_session_file(self, session: Session):
        """Write a session to disk as JSON."""
        path = self.sessions_dir / f"{session.id}.json"
        path.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")

    def _load_session_file(self, session_id: str) -> Optional[Session]:
        """Load a session from disk."""
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Session.from_dict(data)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Preview / title helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _update_preview_from_messages(session: Session):
        """Derive preview and title from the session's messages."""
        for msg in session.messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                text = content.strip()
                if not text:
                    continue
                preview = text[:120]
                title = text[:80]
                if title:
                    session.preview = preview
                    session.title = title
                    break

