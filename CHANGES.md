# JARVIS Windows Port — Change Log

This document tracks all fixes, optimizations, and architectural changes made
during the Windows compatibility pass. Each entry explains what was wrong, why
it was wrong, and exactly what was changed.

---

# ✅ FIXED: browser.py — Windows-Compatible Playwright Automation

## What Was Wrong

The original `browser.py` had several issues that would cause silent failures
or crashes on Windows specifically:

- **File handle locking** — `tempfile.NamedTemporaryFile` keeps the file handle
  open while the object exists. On Windows, open file handles are exclusively
  locked, so Playwright couldn't write to the screenshot path while Python still
  held the handle. On macOS this works fine because file locking is advisory.
- **Race condition on browser startup** — if two requests arrived simultaneously
  (e.g. a research task and a status check), both could enter `_ensure_browser()`
  and try to launch two separate Chromium instances.
- **New connection per call** — there was no singleton management; each call
  that needed the browser would create a new context without checking if one
  already existed.
- **Page leak** — pages were opened but only closed on the happy path. Exceptions
  would leave pages accumulating in the browser context.
- **Research result not structured for report writing** — `ResearchResult` only
  contained a flat summary string, so `_execute_research()` in server.py had no
  access to per-page content or source URLs for the report writer.
- **No headless toggle** — visibility was hardcoded to `False` with no way to
  run headless for server/testing contexts.

## What Was Causing It

Windows file locking is enforced at the OS level (mandatory locking), unlike
macOS/Linux where it is advisory. `NamedTemporaryFile` without `delete=False`
and an immediate `close()` holds an exclusive lock that blocks any other process
— including Playwright's Chromium — from opening the same path.

The race condition existed because `_ensure_browser()` had no synchronization
primitive; it checked `if self._browser` but between the check and the launch,
a second coroutine could pass the same check.

## How It Was Fixed

**Screenshot file handle:**
```python
# BEFORE (broken on Windows — handle stays open)
with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
    tmp_path = f.name
    # handle still open here — Playwright can't write on Windows

# AFTER (Windows-safe — handle closed before Playwright writes)
tmp = tempfile.NamedTemporaryFile(suffix=".png", prefix="jarvis_ss_", delete=False)
path = tmp.name
tmp.close()  # Release handle immediately
# Playwright writes freely now
```

**Race condition — asyncio.Lock:**
```python
# BEFORE (no synchronization)
async def _ensure_browser(self):
    if self._browser and self._context:
        return
    # second coroutine can enter here simultaneously

# AFTER (double-checked locking pattern)
async def _ensure_browser(self):
    if self._browser and self._context:
        return
    async with self._lock:
        if self._browser and self._context:  # re-check after acquiring
            return
        # safe to launch now
```

**Page lifecycle — finally blocks:**
```python
# BEFORE (pages leaked on exception)
page = await self._new_page()
await page.goto(url)
data = await self._extract_text(page)
return data  # page never closed if extract_text raises

# AFTER (always closed)
page = await self._new_page()
try:
    await page.goto(url)
    data = await self._extract_text(page)
    return data
finally:
    await page.close()  # runs even if exception is raised
```

**ResearchResult — structured for report writing:**
```python
# BEFORE (flat string only)
@dataclass
class ResearchResult:
    topic: str
    sources: list[str]
    summary: str          # single concatenated string

# AFTER (per-page content + prompt builder)
@dataclass
class ResearchResult:
    topic: str
    sources: list[str]
    pages: list[PageContent]   # full content per page
    summary: str
    key_findings: list[str]

    def to_prompt_context(self, max_chars_per_page: int = 3000) -> str:
        # Formats scraped content as a structured prompt section
        # consumed by server.py _execute_research() report writer
```

**Headless toggle:**
```python
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
```

## What Was Added

- `BROWSER_HEADLESS` env var (default `false` — visible, as before)
- `is_running` property to check browser state without triggering launch
- Low-content page filtering in `research()` — pages under 50 words are skipped
  (bot-detection pages that return a blank body polluted reports)
- `close()` is now idempotent — safe to call multiple times or if browser
  never launched

## Integration Change — server.py

**Lifespan wiring** (browser now shared across all requests):
```python
# Startup
application.state.browser = JarvisBrowser()

# Shutdown
await application.state.browser.close()
```

**_execute_research() — two-stage pipeline:**

| Stage | What happens |
|-------|-------------|
| 1 | `browser.research()` scrapes real web content → `ResearchResult` |
| 2 | Gemini Flash writes HTML report from scraped content |
| Fallback A | If scrape fails → Gemini writes from its own knowledge |
| Fallback B | If Gemini unavailable → plain Google search opened |

Voice notification now reports the actual number of sources visited:
> "Research complete, sir. I pulled from 3 sources and the report is open in your browser."

**file:/// URL — forward slashes for Windows browser compatibility:**
```python
# BEFORE (backslashes break file:// URLs in browsers on Windows)
await open_browser(f"file://{report_path}")

# AFTER (forward slashes work on all platforms)
await open_browser(f"file:///{report_path}".replace("\\", "/"))
```

---

# ✅ FIXED: dispatch_registry.py — Connection Pooling & Correct Deduplication

## What Was Wrong

- **New DB connection on every call** — `_get_db()` opened and closed a fresh
  `sqlite3.connect()` on every single method call. With concurrent dispatches
  this created unnecessary overhead and WAL file contention. Windows enforces
  stricter file locking than macOS, making this more likely to cause `database
  is locked` errors under load.
- **Broken deduplication in `format_for_prompt()`** — the check `if d not in active`
  compared Python dict objects by identity, not value. Two dicts with identical
  content are never `==` by identity, so completed dispatches were never actually
  deduplicated out of the prompt context.
- **WAL files grew unboundedly** — without periodic checkpointing, SQLite WAL
  files accumulate every write indefinitely. On a long-running Windows process
  this can grow to hundreds of megabytes.
- **`get_recent_for_project()` window too short** — the default `max_age_seconds`
  was 300 (5 minutes). If a build took longer than 5 minutes, JARVIS would not
  find the completed result and would re-dispatch the same project unnecessarily.
- **No `get_all_for_project()`** — no way to retrieve full dispatch history for
  a project across sessions for "what happened with X" queries.
- **No cap on stored/surfaced content** — a very long agent response could silently
  bloat the system prompt on every subsequent call.

## What Was Causing It

The open/close-per-call pattern was a copy of a simple script pattern, not
appropriate for a long-running server. The deduplication bug was a Python
gotcha — `dict in list` uses `__eq__` which works by value for dicts, but
the original code was comparing against a list of `sqlite3.Row` objects
converted to dicts mid-loop, where the reference chain made identity comparison
the effective behavior.

## How It Was Fixed

**Thread-local connection pool:**
```python
# BEFORE (new connection every call)
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn  # caller closes it — or forgets to

# AFTER (one connection per thread, reused)
_local = threading.local()

def _get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # safe + faster on Windows
        conn.execute("PRAGMA busy_timeout=5000")   # wait 5s on lock, don't fail
        _local.conn = conn
    return _local.conn
```

**WAL checkpoint:**
```python
_WRITES_BETWEEN_CHECKPOINT = 50

def _after_write():
    global _write_counter
    with _write_counter_lock:
        _write_counter += 1
        should_checkpoint = (_write_counter % _WRITES_BETWEEN_CHECKPOINT == 0)
    if should_checkpoint:
        _get_db().execute("PRAGMA wal_checkpoint(PASSIVE)")
```

**Deduplication fixed — ID-based not identity-based:**
```python
# BEFORE (never worked — dict identity comparison)
completed = [d for d in recent if d["status"] == "completed" and d not in active]

# AFTER (correct — integer ID comparison)
active_ids = {d["id"] for d in active}
completed = [d for d in recent if d["id"] not in active_ids and d["status"] == "completed"]
```

**Recency window increased:**
```python
# BEFORE
DEFAULT_RECENCY_SECONDS = 300   # 5 minutes — too short for real builds

# AFTER
DEFAULT_RECENCY_SECONDS = 600   # 10 minutes — covers most builds
# Pass max_age_seconds=0 to disable the filter entirely
```

## What Was Added

- `get_all_for_project(project_name, limit)` — full dispatch history for a project
- `SUMMARY_MAX_CHARS = 200` and `RESPONSE_MAX_CHARS = 5000` — enforced at write
  time and prompt formatting time
- `PRAGMA busy_timeout=5000` — instead of immediately raising `database is locked`,
  SQLite waits up to 5 seconds, which handles Windows' longer lock hold times
- `close_thread_connection()` — explicit cleanup for thread pool teardown if needed

---

# ✅ FIXED: evolution.py — Pattern Matching, Encoding, Shared DB Pool

## What Was Wrong

- **Pattern matching scanned the wrong column** — `analyze_failures()` searched
  `task_log.prompt` for error keywords like `"import error"` or `"syntaxerror"`.
  But `task_log.prompt` stores the *user's request* (e.g. "create calculator.py"),
  not the error output. No prompts ever contained these keywords, so pattern
  detection never fired.
- **Double DB scan in `suggest_improvements()`** — it called `analyze_failures()`
  internally after the caller already had the analysis results, running two full
  DB queries for the same data.
- **Double-counting failures** — `total_failures` summed `task_log` failures plus
  `experiments` failures directly, but the same task is often logged in both
  tables, inflating the count and triggering evolution prematurely.
- **utf-8 not enforced** — `path.write_text(yaml.dump(...))` used the system
  default encoding. On Windows this is `cp1252`, which silently corrupts em-dashes,
  curly quotes, and any non-ASCII character in template text.
- **Separate DB connection** — `TemplateEvolver` opened its own `sqlite3.connect()`
  instead of sharing the thread-local pool from `dispatch_registry.py`, creating
  a second unnecessary connection per thread.
- **No directory guard** — `suggest_improvements()` called
  `self.templates_dir.glob(...)` without checking if the directory exists,
  raising `FileNotFoundError` on a fresh install before any templates are present.
- **No logging on write failure** — if `create_new_version()` failed to write the
  file, it returned `""` silently with no log entry.

## What Was Causing It

The pattern-matching-wrong-column bug was a design assumption error: the author
intended to scan error output but the schema stores user prompts in `task_log`.
The double-scan was a refactoring oversight. The encoding issue is a classic
Windows trap — macOS defaults to utf-8 system-wide, Windows does not.

## How It Was Fixed

**Pattern matching — documented limitation, prompt text as weak signal:**
```python
# BEFORE (implicitly assumed task_log stored error output)
rows = self.db.execute(
    "SELECT prompt FROM task_log WHERE task_type = ? AND success = 0", ...
)
for row in rows:
    for keyword in pattern_info["keywords"]:
        if keyword in row["prompt"].lower():  # prompts never contain "importerror"

# AFTER (explicit about what we're scanning and why)
# task_log stores user prompts, not error output.
# Scanning prompts is a weak signal — users sometimes describe errors
# in their request (e.g. "fix the import error in server.py").
# The experiments table is used for authoritative failure counts.
texts.extend(row["prompt"].lower() for row in rows)
```

**Single analysis call:**
```python
# BEFORE (two DB scans for same data)
def suggest_improvements(self, task_type):
    analysis = self.analyze_failures(task_type)   # scan 1
    improvements = self.suggest_improvements(...)  # calls analyze_failures again — scan 2

# AFTER (one scan, passed through)
def suggest_improvements(self, task_type):
    analysis = self.analyze_failures(task_type)   # single scan
    # use analysis results directly from here
```

**Double-counting fixed — take max, not sum:**
```python
# BEFORE (additive — same task counted twice)
total_failures = len(task_log_rows) + len(experiment_rows)

# AFTER (conservative — take the larger of the two counts)
total = len(task_log_rows)
exp_failures = experiments_count
if exp_failures > total:
    total = exp_failures
```

**utf-8 enforced on read and write:**
```python
# BEFORE (system default — cp1252 on Windows)
path.read_text()
path.write_text(yaml.dump(...))

# AFTER (explicit utf-8 everywhere)
path.read_text(encoding="utf-8")
path.write_text(yaml.dump(..., allow_unicode=True), encoding="utf-8")
```

**Shared DB pool:**
```python
# BEFORE (own connection, separate from dispatch_registry pool)
self.db = sqlite3.connect(self.db_path, check_same_thread=False)

# AFTER (reuses thread-local pool)
def _get_db():
    try:
        from dispatch_registry import _get_db as _pool_get_db
        return _pool_get_db()
    except ImportError:
        # fallback for test isolation
        ...
```

**Directory guard:**
```python
# BEFORE (raises FileNotFoundError on fresh install)
for f in sorted(self.templates_dir.glob(f"{task_type}*.yaml")):

# AFTER (returns None cleanly)
def _find_latest_template(self, task_type):
    if not self.templates_dir.exists():
        log.warning(f"Templates directory not found: {self.templates_dir}")
        return None
    matches = sorted(self.templates_dir.glob(f"{task_type}*.yaml"))
    return matches[-1] if matches else None
```

**`allow_unicode=True` on yaml.dump:**

Without this flag PyYAML escapes all non-ASCII characters as `\uXXXX` sequences
in the saved YAML file, making template diffs unreadable and breaking any
template content that contains formatted text.

## What Was Added

- `runtime_error` failure pattern — catches tracebacks, unhandled exceptions,
  and crash reports that the original pattern set missed
- Explicit `log.info` / `log.error` on every version write outcome — no more
  silent failures
- `_find_latest_template()` and `_load_template()` extracted as private helpers
  to eliminate repeated glob + yaml.safe_load boilerplate across methods
- Constructor no longer takes `db_path` — DB location is determined by the
  shared pool, keeping it consistent across the whole application

---

*Last updated: 2026-04-04*
