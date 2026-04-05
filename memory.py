"""
JARVIS Memory & Planning — persistent context, tasks, notes, and smart routing.

Three systems:
1. Memory — facts, preferences, project context JARVIS learns from conversations
2. Tasks — to-do items with priority, due dates, project association
3. Notes — freeform context tied to projects, people, or topics

Everything stored in SQLite. Relevant memories injected into every LLM call
so JARVIS gets smarter over time.

Windows-compatible:
- Path handling via pathlib
- Explicit UTF-8 for any file writes (none here, but connections are safe)
- WAL mode for better concurrency
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from google import genai
from google.genai import types as genai_types

log = logging.getLogger("jarvis.memory")

# Gemini model for extraction (lightweight)
MEMORY_EXTRACTION_MODEL = "gemini-2.5-flash-lite"

_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_gemini_client = None

def _get_gemini_client():
    """Lazy init of shared Gemini client."""
    global _gemini_client
    if _gemini_client is None and _GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=_GEMINI_API_KEY)
    return _gemini_client

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"


def _get_db() -> sqlite3.Connection:
    """Get a new database connection. Caller must close it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,          -- 'fact', 'preference', 'project', 'person', 'decision'
                content TEXT NOT NULL,
                source TEXT DEFAULT '',      -- what conversation/context it came from
                importance INTEGER DEFAULT 5, -- 1-10, higher = more important
                created_at REAL NOT NULL,
                last_accessed REAL,
                access_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority TEXT DEFAULT 'medium', -- 'high', 'medium', 'low'
                status TEXT DEFAULT 'open',     -- 'open', 'in_progress', 'done', 'cancelled'
                due_date TEXT,                  -- ISO date string
                due_time TEXT,                  -- HH:MM
                project TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',         -- JSON array
                notes TEXT DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                topic TEXT DEFAULT '',       -- project name, person, or topic
                tags TEXT DEFAULT '[]',      -- JSON array
                created_at REAL NOT NULL,
                updated_at REAL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content, type, source,
                content='memories', content_rowid='id'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS task_fts USING fts5(
                title, description, project, notes,
                content='tasks', content_rowid='id'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
                title, content, topic,
                content='notes', content_rowid='id'
            );
        """)
        conn.commit()
        log.info("Memory database initialized")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Memories — facts JARVIS learns
# ---------------------------------------------------------------------------

def remember(content: str, mem_type: str = "fact", source: str = "", importance: int = 5) -> int:
    """Store a memory. Returns the memory ID."""
    conn = _get_db()
    try:
        cur = conn.execute(
            "INSERT INTO memories (type, content, source, importance, created_at) VALUES (?, ?, ?, ?, ?)",
            (mem_type, content, source, importance, time.time())
        )
        mem_id = cur.lastrowid
        assert mem_id is not None
        # Update FTS
        conn.execute(
            "INSERT INTO memory_fts (rowid, content, type, source) VALUES (?, ?, ?, ?)",
            (mem_id, content, mem_type, source)
        )
        conn.commit()
        log.info(f"Stored memory [{mem_type}]: {content[:60]}")
        return mem_id
    except Exception as e:
        log.error(f"Failed to store memory: {e}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()


def _sanitize_fts_query(query: str) -> str:
    """Clean a query string for FTS5 — remove special characters that break it."""
    # Remove apostrophes and quotes, keep hyphens as they are part of words
    cleaned = query.replace("'", "").replace('"', "").replace("*", "")
    # Take meaningful words only (length > 2)
    words = [w for w in cleaned.split() if len(w) > 2]
    if not words:
        return ""
    # Join with OR for broader matching
    return " OR ".join(words[:5])


def recall(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search memories by relevance. Returns most relevant matches."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT m.id, m.type, m.content, m.importance, m.created_at, m.access_count
            FROM memory_fts f
            JOIN memories m ON f.rowid = m.id
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_query, limit)).fetchall()
    except Exception as e:
        log.warning(f"Recall FTS failed: {e}")
        results = []
    else:
        # Update access counts
        for r in results:
            conn.execute(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                (time.time(), r["id"])
            )
        conn.commit()
    finally:
        conn.close()
    return [dict(r) for r in results]


def get_recent_memories(limit: int = 10) -> List[Dict[str, Any]]:
    """Get most recent memories."""
    conn = _get_db()
    try:
        results = conn.execute(
            "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in results]
    finally:
        conn.close()


def get_important_memories(limit: int = 10) -> List[Dict[str, Any]]:
    """Get highest importance memories."""
    conn = _get_db()
    try:
        results = conn.execute(
            "SELECT * FROM memories ORDER BY importance DESC, access_count DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in results]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def create_task(title: str, description: str = "", priority: str = "medium",
                due_date: str = "", due_time: str = "", project: str = "",
                tags: Optional[List[str]] = None) -> int:
    """Create a task. Returns task ID."""
    conn = _get_db()
    try:
        cur = conn.execute(
            """INSERT INTO tasks (title, description, priority, due_date, due_time,
               project, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, description, priority, due_date, due_time,
             project, json.dumps(tags or []), time.time())
        )
        task_id = cur.lastrowid
        assert task_id is not None
        conn.execute(
            "INSERT INTO task_fts (rowid, title, description, project, notes) VALUES (?, ?, ?, ?, ?)",
            (task_id, title, description, project, "")
        )
        conn.commit()
        log.info(f"Created task [{priority}]: {title}")
        return task_id
    except Exception as e:
        log.error(f"Failed to create task: {e}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()


def get_open_tasks(project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all open/in-progress tasks, optionally filtered by project."""
    conn = _get_db()
    try:
        if project:
            results = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('open','in_progress') AND project LIKE ? ORDER BY "
                "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date",
                (f"%{project}%",)
            ).fetchall()
        else:
            results = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('open','in_progress') ORDER BY "
                "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date"
            ).fetchall()
        return [dict(r) for r in results]
    finally:
        conn.close()


def get_tasks_for_date(date_str: str) -> List[Dict[str, Any]]:
    """Get tasks due on a specific date (YYYY-MM-DD)."""
    conn = _get_db()
    try:
        results = conn.execute(
            "SELECT * FROM tasks WHERE due_date = ? AND status != 'cancelled' ORDER BY "
            "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_time",
            (date_str,)
        ).fetchall()
        return [dict(r) for r in results]
    finally:
        conn.close()


def complete_task(task_id: int):
    """Mark a task as done."""
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (time.time(), task_id)
        )
        conn.commit()
    except Exception as e:
        log.error(f"Failed to complete task {task_id}: {e}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()


def search_tasks(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search tasks by text."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT t.* FROM task_fts f
            JOIN tasks t ON f.rowid = t.id
            WHERE task_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (fts_query, limit)).fetchall()
        return [dict(r) for r in results]
    except Exception as e:
        log.warning(f"Task search failed: {e}")
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def create_note(content: str, title: str = "", topic: str = "", tags: Optional[List[str]] = None) -> int:
    """Create a note. Returns note ID."""
    conn = _get_db()
    try:
        now = time.time()
        cur = conn.execute(
            "INSERT INTO notes (title, content, topic, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, content, topic, json.dumps(tags or []), now, now)
        )
        note_id = cur.lastrowid
        assert note_id is not None
        conn.execute(
            "INSERT INTO note_fts (rowid, title, content, topic) VALUES (?, ?, ?, ?)",
            (note_id, title, content, topic)
        )
        conn.commit()
        log.info(f"Created note: {title or content[:40]}")
        return note_id
    except Exception as e:
        log.error(f"Failed to create note: {e}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()


def search_notes(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search notes by text."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    conn = _get_db()
    try:
        results = conn.execute("""
            SELECT n.* FROM note_fts f
            JOIN notes n ON f.rowid = n.id
            WHERE note_fts MATCH ?
            ORDER BY rank LIMIT ?
        """, (fts_query, limit)).fetchall()
        return [dict(r) for r in results]
    except Exception as e:
        log.warning(f"Note search failed: {e}")
        return []
    finally:
        conn.close()


def get_notes_by_topic(topic: str) -> List[Dict[str, Any]]:
    """Get all notes for a topic/project."""
    conn = _get_db()
    try:
        results = conn.execute(
            "SELECT * FROM notes WHERE topic LIKE ? ORDER BY updated_at DESC",
            (f"%{topic}%",)
        ).fetchall()
        return [dict(r) for r in results]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Context Builder — smart context for LLM calls
# ---------------------------------------------------------------------------

def build_memory_context(user_message: str) -> str:
    """Build relevant context from memories, tasks, and notes for the LLM.

    Searches for relevant memories based on what the user is talking about.
    Fast — runs FTS queries, no heavy computation.
    """
    parts = []

    # Always include: open high-priority tasks
    high_tasks = [t for t in get_open_tasks() if t["priority"] == "high"]
    if high_tasks:
        task_lines = [f"  - [{t['priority']}] {t['title']}" +
                      (f" (due {t['due_date']})" if t["due_date"] else "")
                      for t in high_tasks[:5]]
        parts.append("HIGH PRIORITY TASKS:\n" + "\n".join(task_lines))

    # Search memories relevant to what user is saying
    relevant = []
    if len(user_message) > 5:
        relevant = recall(user_message, limit=3)
        if relevant:
            mem_lines = [f"  - [{m['type']}] {m['content']}" for m in relevant]
            parts.append("RELEVANT MEMORIES:\n" + "\n".join(mem_lines))

    # Recent important memories (always available)
    important = get_important_memories(limit=3)
    if important:
        # Exclude any already shown in relevant
        relevant_contents = {r["content"] for r in relevant}
        imp_lines = [f"  - {m['content']}" for m in important
                     if m["content"] not in relevant_contents]
        if imp_lines:
            parts.append("KEY FACTS:\n" + "\n".join(imp_lines[:3]))

    return "\n\n".join(parts) if parts else ""


def format_tasks_for_voice(tasks: List[Dict[str, Any]]) -> str:
    """Format tasks for voice response."""
    if not tasks:
        return "No tasks on the list, sir."
    count = len(tasks)
    high = [t for t in tasks if t["priority"] == "high"]
    if count == 1:
        t = tasks[0]
        return f"One task: {t['title']}." + (f" Due {t['due_date']}." if t["due_date"] else "")
    result = f"You have {count} open tasks."
    if high:
        result += f" {len(high)} are high priority."
    top = tasks[:3]
    for t in top:
        result += f" {t['title']}."
    if count > 3:
        result += f" And {count - 3} more."
    return result


def format_plan_for_voice(tasks: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> str:
    """Format a day plan combining tasks and calendar events."""
    if not tasks and not events:
        return "Your day looks clear, sir. No events or tasks scheduled."

    parts = []
    if events:
        parts.append(f"{len(events)} events on the calendar")
    if tasks:
        high = [t for t in tasks if t["priority"] == "high"]
        parts.append(f"{len(tasks)} tasks" + (f", {len(high)} high priority" if high else ""))

    result = f"For tomorrow: {', '.join(parts)}. "

    # List events first
    if events:
        for e in events[:3]:
            result += f"{e.get('start', '')} {e['title']}. "

    # Then high priority tasks
    if tasks:
        for t in [t for t in tasks if t["priority"] == "high"][:2]:
            result += f"Priority: {t['title']}. "

    result += "Shall I adjust anything?"
    return result


# ---------------------------------------------------------------------------
# Memory extraction — learn from conversations
# ---------------------------------------------------------------------------

async def extract_memories(user_text: str, jarvis_response: str) -> List[str]:
    """After a conversation turn, extract any facts worth remembering.

    Uses Gemini Flash to decide if anything in the exchange is worth storing.
    Returns list of memory strings stored.
    """
    if len(user_text) < 15:
        return []

    client = _get_gemini_client()
    if not client:
        return []

    try:
        config = genai_types.GenerateContentConfig(
            system_instruction=(
                "Extract facts worth remembering from this conversation. "
                "Only extract CONCRETE facts: preferences, decisions, names, dates, plans, goals. "
                "NOT opinions, greetings, or casual chat. "
                'Return a JSON array of objects: [{"type": "fact|preference|project|person|decision", "content": "...", "importance": 1-10}] '
                "Return [] if nothing worth remembering. Be very selective — most exchanges have nothing."
            ),
            max_output_tokens=200,
        )
        response = await client.aio.models.generate_content(
            model=MEMORY_EXTRACTION_MODEL,
            contents=f"User: {user_text}\nJARVIS: {jarvis_response}",
            config=config,
        )
        text = (response.text or "").strip()

        # Strip markdown fences if Gemini wraps in ```json
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        if text.startswith("["):
            items = json.loads(text)
            stored = []
            for item in items:
                if isinstance(item, dict) and "content" in item:
                    remember(
                        content=item["content"],
                        mem_type=item.get("type", "fact"),
                        source=user_text[:50],
                        importance=item.get("importance", 5),
                    )
                    stored.append(item["content"])
            return stored

    except Exception as e:
        log.debug(f"Memory extraction failed: {e}")

    return []


def close_all_connections():
    """Close any lingering connections (for shutdown)."""
    # sqlite3 connections are closed per call, nothing global to close.
    # This function exists for API consistency.
    pass


# Initialize on import
init_db()

__all__ = [
    "remember",
    "recall",
    "get_recent_memories",
    "get_important_memories",
    "create_task",
    "get_open_tasks",
    "get_tasks_for_date",
    "complete_task",
    "search_tasks",
    "create_note",
    "search_notes",
    "get_notes_by_topic",
    "build_memory_context",
    "format_tasks_for_voice",
    "format_plan_for_voice",
    "extract_memories",
    "close_all_connections",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
extract_memories signature – Removed the unused _unused_client parameter. Callers (e.g., server.py) must update to await extract_memories(user_text, response_text).

Bug Fixes
Wrong Gemini model – Changed gemini-3-flash-preview to gemini-2.5-flash-lite (defined as MEMORY_EXTRACTION_MODEL).

Database connection leaks – All functions now use try/finally to ensure connections are closed, even on exceptions.

build_memory_context logic – Simplified the deduplication of important memories; removed the incorrect 'relevant' in dir() check.

FTS sanitisation – Keeps hyphens (no longer removes them), as they can be part of valid search terms.

Improvements
Shared Gemini client – _get_gemini_client() lazily creates a single client instance, reused across extract_memories calls.

Logging – Added exc_info=True to all error logs.

__all__ – Explicitly exported public symbols.

Type hints – Added full annotations.

close_all_connections() – Stub for consistency with other modules.

Reminders
FTS triggers – Not implemented. If you later add UPDATE or DELETE operations on memories/tasks/notes, you must manually update the FTS tables or add SQLite triggers.

Integration note:
Replace your existing memory.py with this version. Update any calls to extract_memories in server.py (remove the third argument). Example:

python
# Old
await extract_memories(user_text, response_text, None)

# New
await extract_memories(user_text, response_text)
"""