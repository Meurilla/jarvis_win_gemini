"""
JARVIS Success Tracker — Track task success rates and usage patterns.

Stores metrics in SQLite for analysis and learning.

Windows-compatible: uses thread-local connections for FastAPI thread safety.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

log = logging.getLogger("jarvis.tracking")

DB_PATH = Path(__file__).parent / "jarvis_data.db"

# Thread-local storage for connections
_local = threading.local()


def _get_db() -> sqlite3.Connection:
    """Get thread-local database connection, creating it if needed."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
        log.debug("Opened new tracking DB connection (thread-local)")
    return _local.conn


def close_thread_connection():
    """Close the thread-local connection if open."""
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


class SuccessTracker:
    """Track task success rates and usage patterns.

    Thread-safe for FastAPI's thread pool.
    """

    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        """Create tables and indexes if they don't exist."""
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                success INTEGER NOT NULL,
                retry_count INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                keyword TEXT,
                count INTEGER DEFAULT 1,
                last_used TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                accepted INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_task_log_type ON task_log(task_type);
            CREATE INDEX IF NOT EXISTS idx_usage_action ON usage_patterns(action_type);
        """)
        conn.commit()

    def log_task(
        self,
        task_type: str,
        prompt: str,
        success: bool,
        retry_count: int = 0,
        duration: float = 0.0,
    ):
        """Log a completed task."""
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO task_log (task_type, prompt, success, retry_count, duration_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_type, prompt[:500], int(success), retry_count, duration, datetime.now().isoformat()),
            )
            conn.commit()
            log.info(f"Logged task: type={task_type}, success={success}, retries={retry_count}")
        except Exception as e:
            log.warning(f"Failed to log task: {e}")
            conn.rollback()

    def log_usage(self, action_type: str, keyword: str = ""):
        """Track usage patterns — what types of requests are made most."""
        conn = _get_db()
        try:
            existing = conn.execute(
                "SELECT id, count FROM usage_patterns WHERE action_type = ? AND keyword = ?",
                (action_type, keyword),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE usage_patterns SET count = count + 1, last_used = ? WHERE id = ?",
                    (datetime.now().isoformat(), existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO usage_patterns (action_type, keyword, count, last_used) VALUES (?, ?, 1, ?)",
                    (action_type, keyword, datetime.now().isoformat()),
                )
            conn.commit()
        except Exception as e:
            log.warning(f"Failed to log usage: {e}")
            conn.rollback()

    def log_suggestion(self, task_id: str, suggestion: str):
        """Log a proactive suggestion."""
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO suggestions (task_id, suggestion, created_at) VALUES (?, ?, ?)",
                (task_id, suggestion, datetime.now().isoformat()),
            )
            conn.commit()
        except Exception as e:
            log.warning(f"Failed to log suggestion: {e}")
            conn.rollback()

    def mark_suggestion_accepted(self, suggestion_id: int):
        """Mark a suggestion as accepted by the user."""
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE suggestions SET accepted = 1 WHERE id = ?",
                (suggestion_id,),
            )
            conn.commit()
        except Exception as e:
            log.warning(f"Failed to mark suggestion: {e}")
            conn.rollback()

    def get_success_rate(self, task_type: Optional[str] = None) -> Dict[str, Any]:
        """Get success rate stats, optionally filtered by task type."""
        conn = _get_db()
        try:
            if task_type:
                rows = conn.execute(
                    "SELECT success, COUNT(*) as cnt FROM task_log WHERE task_type = ? GROUP BY success",
                    (task_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT success, COUNT(*) as cnt FROM task_log GROUP BY success",
                ).fetchall()

            total = sum(r["cnt"] for r in rows)
            passed = sum(r["cnt"] for r in rows if r["success"])

            return {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "rate": (passed / total * 100) if total > 0 else 0.0,
            }
        except Exception as e:
            log.warning(f"Failed to get success rate: {e}")
            return {"total": 0, "passed": 0, "failed": 0, "rate": 0.0}

    def get_top_actions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get the most common action types."""
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT action_type, keyword, count, last_used FROM usage_patterns "
                "ORDER BY count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning(f"Failed to get top actions: {e}")
            return []

    def get_avg_duration(self, task_type: Optional[str] = None) -> float:
        """Get average task duration in seconds."""
        conn = _get_db()
        try:
            if task_type:
                row = conn.execute(
                    "SELECT AVG(duration_seconds) as avg_dur FROM task_log WHERE task_type = ?",
                    (task_type,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT AVG(duration_seconds) as avg_dur FROM task_log",
                ).fetchone()
            return row["avg_dur"] or 0.0
        except Exception as e:
            log.warning(f"Failed to get avg duration: {e}")
            return 0.0

    def close(self):
        """Close the thread-local connection for the current thread."""
        close_thread_connection()


# Module-level singleton — server.py imports and reuses this
success_tracker = SuccessTracker()


__all__ = [
    "SuccessTracker",
    "success_tracker",
    "close_thread_connection",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
None. Public API remains identical.

Bug Fixes
Thread safety – Replaced single global connection with thread‑local connections (same pattern as dispatch_registry.py). Now safe for FastAPI's thread pool.

Transaction handling – Added conn.rollback() on exceptions to prevent inconsistent states.

Improvements
__all__ – Added explicit exports.

Docstrings – Added to all methods.

Type hints – Added full annotations.

WAL mode – Enabled for better concurrency.

Removed / Deprecated
None.
"""