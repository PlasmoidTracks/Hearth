"""SQLAlchemy engine + session management (SQLite, sync ORM).

Streaming routes open short-lived sessions rather than holding one across a
long turn (see DEC-010). `session_scope()` is a context manager that commits on
success and rolls back on error. SQLite is configured with WAL for concurrent
readers under multi-user load.
"""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import get_boot_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_boot_settings().resolved_db_url
        _engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):  # WAL + busy timeout for concurrency
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)
    get_engine()
    Base.metadata.create_all(_engine)   # creates any NEW tables (e.g. artifacts)
    _migrate_add_columns()
    _migrate_artifacts_drop_fk()


def _migrate_add_columns() -> None:
    """Idempotent column additions for pre-existing DBs (create_all won't ALTER).

    SQLite supports ADD COLUMN but not conditional adds, so we inspect and add only
    what's missing. Keeps restarts safe on already-provisioned installs.
    """
    from sqlalchemy import inspect, text
    insp = inspect(_engine)
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        with _engine.begin() as conn:
            if "language" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'en'"))
            if "prefs" not in cols:
                # Behavioral UI preferences blob (see routes_chat /api/prefs). All
                # consumers treat missing keys as defaults, so '{}' is a safe default.
                conn.execute(text("ALTER TABLE users ADD COLUMN prefs TEXT DEFAULT '{}'"))
            if "system_prompt" not in cols:
                # Per-user system prompt (chat sidebar → Prompt panel). Empty means
                # "no personal instructions"; the base persona applies unchanged.
                conn.execute(text("ALTER TABLE users ADD COLUMN system_prompt TEXT DEFAULT ''"))
    if "conversations" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("conversations")}
        with _engine.begin() as conn:
            if "device_granted" not in cols:
                # Existing chats start un-granted; the user presses the button again.
                conn.execute(text("ALTER TABLE conversations "
                                  "ADD COLUMN device_granted INTEGER DEFAULT 0"))


def _migrate_artifacts_drop_fk() -> None:
    """Rebuild `artifacts` without the user_id FK if an old FK-bearing version exists.

    Early versions created artifacts.user_id as a hard FK to users.id. That is wrong:
    workspaces (and thus artifacts) are keyed by user-id string and can exist without a
    users row, so the FK silently dropped artifact inserts for orphan ids. SQLite can't
    drop a constraint via ALTER, so we rebuild the table in place when one is detected.
    Idempotent: only runs while an FK on user_id is present.
    """
    from sqlalchemy import inspect, text
    insp = inspect(_engine)
    if "artifacts" not in insp.get_table_names():
        return
    has_fk = any(fk["referred_table"] == "users"
                 for fk in insp.get_foreign_keys("artifacts"))
    if not has_fk:
        return
    with _engine.begin() as conn:
        # Preserve rows; recreate without the FK, then swap.
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("ALTER TABLE artifacts RENAME TO artifacts_old"))
        conn.execute(text(
            "CREATE TABLE artifacts ("
            "id VARCHAR NOT NULL PRIMARY KEY, "
            "user_id VARCHAR NOT NULL, "
            "conversation_id VARCHAR DEFAULT '', "
            "kind VARCHAR DEFAULT 'produced', "
            "filename VARCHAR DEFAULT 'file', "
            "rel_path VARCHAR DEFAULT '', "
            "size INTEGER DEFAULT 0, "
            "mime VARCHAR DEFAULT 'application/octet-stream', "
            "created_at INTEGER DEFAULT 0)"))
        conn.execute(text(
            "INSERT INTO artifacts (id,user_id,conversation_id,kind,filename,rel_path,size,mime,created_at) "
            "SELECT id,user_id,conversation_id,kind,filename,rel_path,size,mime,created_at FROM artifacts_old"))
        conn.execute(text("DROP TABLE artifacts_old"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_artifacts_user_id ON artifacts (user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_artifacts_created_at ON artifacts (created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_artifacts_conversation_id ON artifacts (conversation_id)"))
        conn.execute(text("PRAGMA foreign_keys=ON"))



@contextmanager
def session_scope():
    get_engine()
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def new_session():
    """A raw session the caller must close (for FastAPI deps)."""
    get_engine()
    return _SessionLocal()
