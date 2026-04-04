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
tmp_fd, path = tempfile.mkstemp(suffix=".png", prefix="jarvis_screenshot_")
os.close(tmp_fd)  # Release handle immediately
# Playwright writes freely now
```

**Dead code removed — unreachable sleep in `visit()`:**
```python
# BEFORE (sleep after return — never executes)
return PageContent(...)
await asyncio.sleep(3)  # dead code

# AFTER (sleep before return — actually runs)
await asyncio.sleep(3)
return PageContent(...)
```

**Empty query guard in `search()`:**
```python
# BEFORE (hits DuckDuckGo with blank query)
async def search(self, query: str) -> list[SearchResult]:
    page = await self._new_page()

# AFTER (returns early on empty input)
async def search(self, query: str) -> list[SearchResult]:
    if not query:
        return []
```

**Platform-aware User-Agent:**
```python
# BEFORE (always spoofed macOS Chrome regardless of OS)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)..."

# AFTER (detects OS at runtime, sends matching UA)
_OS = platform.system()
if _OS == "Windows":
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)..."
elif _OS == "Darwin":
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)..."
else:
    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)..."
```

**Cleanup on failed screenshot:**
```python
# AFTER — empty temp file removed on failure
try:
    if path and Path(path).exists() and Path(path).stat().st_size == 0:
        Path(path).unlink()
except Exception:
    pass
```

## What Was Added

- `tempfile.mkstemp()` replacing deprecated `tempfile.mktemp()` — atomic and safe
- Empty query guard in `search()` — returns early rather than hitting DDG blank
- Platform-aware User-Agent — Windows, macOS, and Linux all send correct UA
- Dead code removed — unreachable `asyncio.sleep()` after `return` in `visit()`
- Failed screenshot cleanup — empty temp files removed on error

---

# ✅ FIXED: requirements.txt — Missing and Incorrect Dependencies

## What Was Wrong

- **`anthropic` listed but no longer used** — the Windows port replaced all
  Anthropic SDK calls with `google-genai`. Listing it caused confusion and
  an unnecessary install.
- **`google-genai` missing** — the package imported throughout as
  `from google import genai` was never listed in requirements.
- **`edge-tts` missing** — the TTS engine used by `server.py` was absent,
  meaning a fresh install would fail at runtime with no clear error.
- **`pytest` and `pytest-asyncio` missing** — the test suite in `/tests/`
  requires both; neither was listed.
- **`npm install -g @google/gemini-cli` in requirements.txt** — this is a
  shell command, not a Python package. pip would error trying to install it.

## How It Was Fixed

Removed `anthropic` and the invalid npm line. Added all missing packages:

```
google-genai>=1.0.0
edge-tts>=6.1.9
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

Added a commented `pywin32` line for future reference if Windows COM access
is needed.

---

# ✅ FIXED: Gemini CLI — Work Mode Agent Not Found

## What Was Wrong

`work_mode.py` searches for a `gemini` or `gemini-cli` binary via
`shutil.which()` on startup. The warning on boot:

```
No agentic CLI found (tried: gemini, gemini-cli). Work mode will use
direct Gemini API — file editing unavailable.
```

was caused by Gemini CLI simply not being installed, not a code problem.

## What Was Causing It

The Gemini CLI (`@google/gemini-cli`) is an npm global package that must be
installed separately. It is not bundled with the project and was not documented
clearly as a required step after cloning.

## How It Was Fixed

Installed via npm and authenticated:

```bash
npm install -g @google/gemini-cli
gemini auth login
```

After installation `shutil.which("gemini")` resolves correctly and the warning
is gone on next boot. No code changes were required.

---

# ✅ REFACTORED: conversation.py — Full Rewrite with Clean Server Interface

## What Was Wrong

- **Orphaned module** — `conversation.py` was never imported by `server.py` or
  any other module. All the session tracking infrastructure it contained was
  completely unused.
- **`modify_plan()` used fragile keyword matching** — detecting modifications
  via hardcoded keywords like "instead of", "add", "remove" would break on any
  natural phrasing that didn't match exactly.
- **`SESSION_TIMEOUT_SECONDS = 300`** — five minutes was far too short for a
  real working session. The timeout check ran on property access but nothing
  called it on a timer anyway.
- **`ConversationMode` and `PlanningSession`** — two classes present in the
  original that were orphaned and conflicted with state tracking already in
  `server.py`.
- **No defined interface** — no clear contract between what `conversation.py`
  exposed and what `server.py` would call, making integration guesswork.

## What Was Causing It

The module was written ahead of its integration — the interface was never
finalized and `server.py` was never updated to use it.

## How It Was Fixed

Complete rewrite around a clean, documented interface:

```python
session.add_exchange(role, content)      # called every turn, both sides
session.get_context() -> str             # injected into every Gemini system prompt
session.log_plan(plan)                   # planner handoff on task confirmation
session.modify_plan(user_text, client)   # Gemini-powered plan modification
session.query(user_text, client)         # JARVIS-voiced session memory queries
session.close(reason)                    # clean shutdown on WS disconnect
```

**`modify_plan()` — Gemini replaces keyword matching:**
```python
# BEFORE (broke on any non-matching phrasing)
if "instead of" in answer_lower:
    ...
elif "add" in answer_lower:
    ...

# AFTER (Gemini parses intent, returns structured JSON)
config = genai_types.GenerateContentConfig(system_instruction=system, ...)
response = await client.aio.models.generate_content(
    model="gemini-2.0-flash-preview",
    contents=user_text,
    config=config,
)
data = json.loads(response.text)
# applies modification via _apply_modification()
```

**Session timeout raised:**
```python
# BEFORE
SESSION_TIMEOUT_SECONDS = 300   # 5 minutes — too short

# AFTER
SESSION_TIMEOUT_SECONDS = 1800  # 30 minutes — realistic working session
```

## What Was Added

- `Decision` dataclass — structured key/value log of everything agreed upon
- `PlanSummary` dataclass — living record of the current plan updated as
  planner answers are collected
- `log_plan()` — receives completed `Plan` from `planner.py` and converts
  its answers into structured `Decision` entries
- `log_decision()` — direct decision logging for one-off facts outside planning
- `mark_plan_complete()` — called when a dispatch finishes to update plan status
- `get_context()` — concise formatted string injected into every Gemini call
  so decisions survive message history truncation

---

# ✅ REFACTORED: planner.py — Ported to Gemini, Windows Compatible

## What Was Wrong

- **Still importing and using `anthropic`** — both `detect_planning_mode()` and
  `_classify_request()` took an `anthropic.AsyncAnthropic` client and called
  `claude-haiku-4-5-20251001`. The whole project had been ported to Gemini but
  `planner.py` was missed.
- **`DESKTOP_PATH` missing fallback** — hardcoded to `Path.home() / "Desktop"`
  with no check for whether Desktop exists, unlike `server.py` and `actions.py`
  which both have the three-way fallback.
- **`gather_project_context()` only looked for `CLAUDE.md`** — the Windows port
  uses `TASK.md` in `actions.py` but the context gatherer didn't know about it.
- **`git log` subprocess had bare `except`** — swallowed `FileNotFoundError`
  when git wasn't on PATH, making it impossible to distinguish "git not installed"
  from "not a git repo".
- **No `Plan.to_context_dict()`** — `conversation.py`'s `log_plan()` needs
  specific fields from `Plan`; without a defined method the interface was fragile.

## What Was Causing It

`planner.py` was written before the Gemini port and never updated. The
`DESKTOP_PATH` inconsistency was a copy-paste omission. The `TASK.md` gap
was an oversight during the Windows actions refactor.

## How It Was Fixed

**Gemini port — full replacement of Anthropic client:**
```python
# BEFORE
import anthropic

async def detect_planning_mode(
    user_text: str,
    client: anthropic.AsyncAnthropic = None,
    ...

# AFTER
from google import genai

async def detect_planning_mode(
    user_text: str,
    client: genai.Client = None,
    ...
```

Both `detect_planning_mode()` and `_classify_request()` now call
`gemini-2.0-flash-preview` via `client.aio.models.generate_content()`.

**`DESKTOP_PATH` — matching fallback logic:**
```python
# BEFORE
DESKTOP_PATH = Path.home() / "Desktop"

# AFTER (matches server.py and actions.py exactly)
_desktop_env = os.getenv("PROJECTS_DIR", "")
if _desktop_env:
    DESKTOP_PATH = Path(_desktop_env)
else:
    _default = Path.home() / "Desktop"
    DESKTOP_PATH = _default if _default.exists() else Path(__file__).parent
```

**`TASK.md` support in context gatherer:**
```python
# AFTER — checks both filenames, doesn't overwrite if already found
for filename, key in [
    ("CLAUDE.md", "claude_md"),
    ("TASK.md", "claude_md"),   # Windows port uses TASK.md
    ...
]:
```

**`git log` specific exception handling:**
```python
# BEFORE
except Exception:
    pass  # swallows FileNotFoundError silently

# AFTER
except FileNotFoundError:
    log.debug("git not found on PATH — skipping git log")
except asyncio.TimeoutError:
    log.debug("git log timed out")
except Exception as e:
    log.debug(f"git log failed: {e}")
```

**`Plan.to_context_dict()` added:**
```python
def to_context_dict(self) -> dict:
    return {
        "task_type": self.task_type,
        "original_request": self.original_request,
        "project": self.project or "",
        "project_path": self.project_path or "",
        "answers": self.answers,
    }
```

## What Was Added

- `Plan.to_context_dict()` — clean serialization for `conversation.py` consumption
- `TASK.md` lookup in `gather_project_context()`
- Specific `FileNotFoundError` and `TimeoutError` handling for git subprocess
- `DESKTOP_PATH` three-way fallback consistent with the rest of the codebase

---

# ✅ INTEGRATED: server.py — conversation.py Wired into Voice Loop

## What Was Wrong

`conversation.py` existed but was never imported or called anywhere in
`server.py`. Every WebSocket session had no structured memory of decisions
made during the conversation. If message history was truncated at 20 messages,
any decisions made earlier were invisible to Gemini.

## How It Was Fixed

Eight integration points added to `server.py` (all additive, nothing deleted):

| # | Location | Change |
|---|----------|--------|
| 1 | WS handler init | Instantiate `ConversationSession` |
| 2 | User transcript | `add_exchange("user", user_text)` |
| 3 | `detect_action_fast()` | Session query fast-path detector |
| 4 | Fast action handler | Handle `query_session` action |
| 5 | `generate_response()` | Inject `get_context()` into system prompt |
| 6 | Both `generate_response()` call sites | Pass `conversation_context` |
| 7 | Both planner confirmation paths | Call `log_plan()` before `planner.reset()` |
| 8 | After every JARVIS response | `add_exchange("assistant", response_text)` |
| 9 | WS disconnect `finally` | `conversation_session.close("disconnected")` |

**Session query fast-path — no Gemini call for simple memory questions:**
```python
if any(p in t for p in [
    "what did we decide", "what did we agree", "what was the plan",
    "remind me what", "what have we discussed", "what are we building",
    "what tech stack", "what stack did we",
]):
    return {"action": "query_session"}
```

**Session context injected into every Gemini call:**
```python
# generate_response() signature updated
async def generate_response(
    ...
    conversation_context: str = "",   # new parameter
) -> str:

# Inside generate_response()
if conversation_context:
    system += f"\n\nSESSION DECISIONS & PLAN:\n{conversation_context}"
```

**Planner handoff — decisions survive planner reset:**
```python
# Both confirmation paths, before planner.reset()
conversation_session.log_plan(planner.active_plan)
planner.reset()
```

---

*Last updated: 2026-04-04*