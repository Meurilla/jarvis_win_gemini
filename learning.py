"""
JARVIS Usage Learning — Tracks request patterns and pre-loads context.

Identifies what tasks the user requests most, which projects are active,
and suggests relevant context based on patterns.

Windows-compatible:
- Thread-local SQLite connections
- Path handling via pathlib
- Safe for FastAPI's thread pool
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

log = logging.getLogger("jarvis.learning")

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
        log.debug("Opened new learning DB connection (thread-local)")
    return _local.conn


def close_thread_connection():
    """Close the thread-local connection if open."""
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


@dataclass
class ContextSuggestion:
    suggestion_text: str  # Voice-friendly suggestion
    project_dir: str      # Suggested project directory
    confidence: float     # 0.0 to 1.0

    def to_dict(self) -> dict:
        return asdict(self)


class UsageLearner:
    """Tracks usage patterns and suggests context based on history.

    Thread-safe for FastAPI's thread pool.
    """

    def __init__(self):
        self._ensure_tables()

    def _ensure_tables(self):
        """Ensure required tables exist (created by tracking.py, but be safe)."""
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

            CREATE INDEX IF NOT EXISTS idx_task_log_created ON task_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_keyword ON usage_patterns(keyword);
        """)
        conn.commit()

    def get_frequent_types(self, days: int = 30) -> List[Tuple[str, int]]:
        """Get task type frequency over the specified period."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT task_type, COUNT(*) as cnt FROM task_log "
                "WHERE created_at > ? GROUP BY task_type ORDER BY cnt DESC",
                (cutoff,),
            ).fetchall()
            return [(row["task_type"], row["cnt"]) for row in rows]
        except Exception as e:
            log.warning(f"Failed to get frequent types: {e}", exc_info=True)
            return []

    def get_recent_projects(self, days: int = 7) -> List[str]:
        """Get unique project directories from recent usage patterns."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT DISTINCT keyword FROM usage_patterns "
                "WHERE keyword != '' AND last_used > ? ORDER BY last_used DESC",
                (cutoff,),
            ).fetchall()
            return [row["keyword"] for row in rows]
        except Exception as e:
            log.warning(f"Failed to get recent projects: {e}", exc_info=True)
            return []

    def suggest_context(
        self,
        user_text: str,
        known_projects: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[ContextSuggestion]:
        """Suggest relevant context based on user text and recent patterns.

        Returns a ContextSuggestion if confidence is high enough, None otherwise.
        """
        if not known_projects:
            return None

        user_lower = user_text.lower()
        best_match = None
        best_confidence = 0.0

        for project in known_projects:
            project_name = project.get("name", "")
            if not project_name:
                continue
            project_name_lower = project_name.lower()
            project_path = project.get("path", "")

            # Direct name mention
            if project_name_lower in user_lower:
                return ContextSuggestion(
                    suggestion_text=f"I'll use the {project_name} project directory, sir.",
                    project_dir=project_path,
                    confidence=0.95,
                )

            # Fuzzy match — check if project name words appear in the text
            name_words = project_name_lower.replace("-", " ").replace("_", " ").split()
            matches = sum(1 for w in name_words if w in user_lower and len(w) > 2)
            if name_words and matches > 0:
                confidence = matches / len(name_words) * 0.8
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_match = project

        # Check recency boost — recent projects get higher confidence
        recent_projects = self.get_recent_projects(days=3)
        if best_match and best_match.get("path", "") in recent_projects:
            best_confidence = min(best_confidence + 0.15, 1.0)

        if best_match and best_confidence >= 0.7:
            project_name = best_match.get("name", "that project")
            project_path = best_match.get("path", "")
            return ContextSuggestion(
                suggestion_text=(
                    f"Based on your recent work, shall I use the {project_name} "
                    f"project directory, sir?"
                ),
                project_dir=project_path,
                confidence=best_confidence,
            )

        # Check for tech stack patterns
        frequent_types = self.get_frequent_types(days=14)
        if frequent_types:
            top_type, top_count = frequent_types[0]
            if top_count >= 3:
                # User has a pattern
                type_words = {
                    "build": "building",
                    "fix": "fixing",
                    "refactor": "refactoring",
                    "research": "researching",
                }
                action_word = type_words.get(top_type, top_type)
                # Only suggest if relevant to current request
                if any(kw in user_lower for kw in [top_type, action_word]):
                    return ContextSuggestion(
                        suggestion_text=(
                            f"You've been doing quite a bit of {action_word} lately, sir. "
                            f"Shall I apply the same approach here?"
                        ),
                        project_dir="",
                        confidence=0.6,  # Lower confidence — informational only
                    )

        return None

    def get_session_stats(self) -> Dict[str, Any]:
        """Get overall usage statistics for the current session summary."""
        conn = _get_db()
        try:
            total = conn.execute("SELECT COUNT(*) as cnt FROM task_log").fetchone()["cnt"]
            success = conn.execute(
                "SELECT COUNT(*) as cnt FROM task_log WHERE success = 1"
            ).fetchone()["cnt"]
            recent = conn.execute(
                "SELECT COUNT(*) as cnt FROM task_log WHERE created_at > ?",
                ((datetime.now() - timedelta(days=7)).isoformat(),),
            ).fetchone()["cnt"]

            return {
                "total_tasks": total,
                "success_rate": (success / total * 100) if total > 0 else 0.0,
                "tasks_this_week": recent,
            }
        except Exception as e:
            log.warning(f"Failed to get session stats: {e}", exc_info=True)
            return {"total_tasks": 0, "success_rate": 0.0, "tasks_this_week": 0}

    def close(self):
        """Close the thread-local connection for the current thread."""
        close_thread_connection()


__all__ = [
    "UsageLearner",
    "ContextSuggestion",
    "close_thread_connection",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
None. Public API remains identical.

Bug Fixes
Thread safety – Replaced single global connection with thread‑local connections (like dispatch_registry). Now safe for FastAPI's thread pool.

Missing parent directory – Added DB_PATH.parent.mkdir(parents=True, exist_ok=True) before connecting.

KeyError protection – In suggest_context, used .get() for all project dict accesses to avoid crashes if keys are missing.

Connection leak – Added close_thread_connection() helper and close() method; caller (e.g., server lifespan) should invoke it.

Improvements
Logging – Added exc_info=True to all exception logs.

Indexes – Created indexes on task_log(created_at) and usage_patterns(keyword) for better query performance.

__all__ – Explicitly exported public symbols.

Type hints – Improved with from __future__ import annotations and explicit generics.

WAL mode – Enabled for better concurrency.

Reminders / Integration Notes
usage_patterns table is still not populated – No module writes to it. To make get_recent_projects() useful, you must add logging of project usage elsewhere (e.g., in dispatch_registry or tracking.py).

Close connections – Add to your server's lifespan shutdown:

python
from learning import close_thread_connection
# in lifespan, after yield
close_thread_connection()
"""