"""
JARVIS Dispatch Registry — tracks all active and recent project builds/dispatches.

Persists to SQLite so JARVIS always knows what he's working on,
what just finished, and what the user is likely referring to.

Windows-compatible:
- Thread-safe connection pool (one connection per thread via threading.local)
- WAL mode with periodic checkpointing to prevent unbounded WAL growth
- Atomic directory creation (exist_ok=True)
- Safe string comparisons for dict deduplication in format_for_prompt()
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.dispatch")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"

# How long (seconds) a completed dispatch is considered "recent" for
# re-use before JARVIS re-dispatches. Generous default covers long builds.
DEFAULT_RECENCY_SECONDS = 600  # 10 minutes

# Cap on summary/response text stored and surfaced in prompts
SUMMARY_MAX_CHARS = 200
RESPONSE_MAX_CHARS = 5000

# WAL checkpoint — run after this many write operations to keep WAL file small
_WRITES_BETWEEN_CHECKPOINT = 50


# ---------------------------------------------------------------------------
# Connection Pool (one connection per thread)
# ---------------------------------------------------------------------------

_local = threading.local()
_write_counter = 0
_write_counter_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """
    Return a per-thread SQLite connection, creating it if needed.

    Using thread-local connections avoids the overhead of open/close on every
    call while staying safe under FastAPI's thread pool. WAL mode is set once
    per connection and persists for its lifetime.
    """
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")   # Safe with WAL, faster on Windows
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")    # Wait up to 5s on lock instead of failing
        _local.conn = conn
        log.debug("Opened new DB connection (thread-local)")
    return _local.conn


def _after_write():
    """
    Increment write counter and checkpoint WAL periodically.

    SQLite WAL files grow unboundedly on Windows if never checkpointed.
    A passive checkpoint (non-blocking) runs every N writes.
    """
    global _write_counter
    with _write_counter_lock:
        _write_counter += 1
        should_checkpoint = (_write_counter % _WRITES_BETWEEN_CHECKPOINT == 0)

    if should_checkpoint:
        try:
            _get_db().execute("PRAGMA wal_checkpoint(PASSIVE)")
            log.debug("WAL checkpoint run")
        except Exception as e:
            log.debug(f"WAL checkpoint skipped: {e}")


def close_thread_connection():
    """
    Close the thread-local connection. Call from thread cleanup if needed.
    Not required for the main FastAPI thread — lifespan handles that.
    """
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS dispatches (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name  TEXT    NOT NULL,
        project_path  TEXT    NOT NULL,
        original_prompt TEXT  NOT NULL,
        refined_prompt  TEXT  DEFAULT '',
        status          TEXT  DEFAULT 'pending',
        claude_response TEXT  DEFAULT '',
        summary         TEXT  DEFAULT '',
        created_at      REAL  NOT NULL,
        updated_at      REAL  NOT NULL,
        completed_at    REAL
    );
    CREATE INDEX IF NOT EXISTS idx_dispatch_status  ON dispatches(status);
    CREATE INDEX IF NOT EXISTS idx_dispatch_updated ON dispatches(updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_dispatch_project ON dispatches(project_name);
"""


def _init_db():
    """Create tables and indexes if they don't exist."""
    conn = _get_db()
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class DispatchRegistry:
    """
    Tracks active and recent project dispatches.

    All methods are synchronous and safe to call from both sync and async
    contexts (FastAPI runs sync code in a thread pool automatically).
    """

    def __init__(self):
        _init_db()

    # -- Write operations -----------------------------------------------------

    def register(self, project_name: str, project_path: str, prompt: str) -> int:
        """Register a new dispatch. Returns the dispatch ID."""
        conn = _get_db()
        now = time.time()
        cur = conn.execute(
            """INSERT INTO dispatches
               (project_name, project_path, original_prompt, status, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (project_name, project_path, prompt[:RESPONSE_MAX_CHARS], now, now),
        )
        dispatch_id = cur.lastrowid
        if dispatch_id is None:
            raise RuntimeError("Failed to register dispatch row")
        conn.commit()
        _after_write()
        log.info(f"Registered dispatch #{dispatch_id}: {project_name}")
        return dispatch_id

    def update_status(
        self,
        dispatch_id: int,
        status: str,
        response: Optional[str] = None,
        summary: Optional[str] = None,
    ):
        """Update dispatch status, optionally storing response and summary."""
        conn = _get_db()
        now = time.time()
        is_terminal = status in ("completed", "failed", "timeout")

        # Truncate stored content to avoid bloating the DB and system prompt
        safe_response = (response or "")[:RESPONSE_MAX_CHARS]
        safe_summary = (summary or "")[:SUMMARY_MAX_CHARS]

        if response is not None:
            conn.execute(
                """UPDATE dispatches
                   SET status=?, claude_response=?, summary=?,
                       updated_at=?, completed_at=?
                   WHERE id=?""",
                (
                    status,
                    safe_response,
                    safe_summary,
                    now,
                    now if is_terminal else None,
                    dispatch_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE dispatches SET status=?, updated_at=? WHERE id=?",
                (status, now, dispatch_id),
            )

        conn.commit()
        _after_write()

    # -- Read operations ------------------------------------------------------

    def get_most_recent(self) -> Optional[dict]:
        """Get the most recently updated dispatch."""
        row = _get_db().execute(
            "SELECT * FROM dispatches ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_active(self) -> list[dict]:
        """Get all pending/building dispatches."""
        rows = _get_db().execute(
            """SELECT * FROM dispatches
               WHERE status IN ('pending', 'building', 'planning')
               ORDER BY updated_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_name(self, name: str) -> Optional[dict]:
        """Fuzzy match the most recent dispatch by project name."""
        row = _get_db().execute(
            """SELECT * FROM dispatches
               WHERE project_name LIKE ?
               ORDER BY updated_at DESC LIMIT 1""",
            (f"%{name}%",),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_for_project(
        self,
        project_name: str,
        max_age_seconds: int = DEFAULT_RECENCY_SECONDS,
    ) -> Optional[dict]:
        """
        Return the most recent completed dispatch for a project if within max_age.

        Uses DEFAULT_RECENCY_SECONDS (10 min) so long builds are still found.
        Pass max_age_seconds=0 to disable the time filter entirely.
        """
        conn = _get_db()
        if max_age_seconds > 0:
            cutoff = time.time() - max_age_seconds
            row = conn.execute(
                """SELECT * FROM dispatches
                   WHERE project_name LIKE ?
                     AND status = 'completed'
                     AND completed_at IS NOT NULL
                     AND completed_at >= ?
                   ORDER BY completed_at DESC LIMIT 1""",
                (f"%{project_name}%", cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM dispatches
                   WHERE project_name LIKE ?
                     AND status = 'completed'
                   ORDER BY completed_at DESC LIMIT 1""",
                (f"%{project_name}%",),
            ).fetchone()
        return dict(row) if row else None

    def get_all_for_project(self, project_name: str, limit: int = 10) -> list[dict]:
        """
        Return full dispatch history for a project, newest first.

        Useful for "what happened with X" queries that span multiple sessions.
        """
        rows = _get_db().execute(
            """SELECT * FROM dispatches
               WHERE project_name LIKE ?
               ORDER BY updated_at DESC LIMIT ?""",
            (f"%{project_name}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent(self, limit: int = 5) -> list[dict]:
        """Get the last N dispatches across all projects."""
        rows = _get_db().execute(
            "SELECT * FROM dispatches ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Prompt formatting ----------------------------------------------------

    def format_for_prompt(self) -> str:
        """
        Format active + recent dispatches as context for the LLM system prompt.

        Deduplication is done by dispatch ID (not dict identity) so it's
        guaranteed correct. Summary length is capped to avoid prompt bloat.
        """
        active = self.get_active()
        active_ids = {d["id"] for d in active}

        recent = self.get_recent(limit=3)
        completed = [
            d for d in recent
            if d["id"] not in active_ids and d["status"] == "completed"
        ]

        parts = []

        if active:
            lines = []
            for d in active:
                elapsed = int(time.time() - d["created_at"])
                prompt_preview = d["original_prompt"][:80]
                lines.append(
                    f"  - [{d['status']}] {d['project_name']} "
                    f"({elapsed}s elapsed): {prompt_preview}"
                )
            parts.append("CURRENTLY WORKING ON:\n" + "\n".join(lines))

        if completed:
            lines = []
            for d in completed[:2]:
                summary = d.get("summary", "").strip()
                label = summary[:SUMMARY_MAX_CHARS] if summary else "completed"
                lines.append(f"  - {d['project_name']}: {label}")
            parts.append("RECENTLY COMPLETED:\n" + "\n".join(lines))

        return "\n".join(parts) if parts else "No active or recent dispatches."