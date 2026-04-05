"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Gemini CLI task manager (spawn/manage gemini -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, cast

from google import genai
from google.genai import types as genai_types
import edge_tts
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from actions import open_terminal, open_browser, _generate_project_name
from work_mode import WorkSession, is_casual_question
from qa import qa_agent
from tracking import success_tracker
from suggestions import suggest_followup
from conversation import ConversationSession
from browser import JarvisBrowser, ResearchResult
from screen import format_windows_for_context
from calendar_access import get_todays_events, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from mail_access import get_unread_count, get_unread_messages, format_unread_summary
from memory import (
    remember, get_open_tasks, create_task, complete_task,
    create_note, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
# from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, BYPASS_PHRASES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-GB-RyanNeural")  # British male — closest to MCU JARVIS
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Projects root — configurable via env var.
# Defaults to ~/Desktop but falls back to the project directory itself
# if Desktop doesn't exist (common on Windows with non-standard usernames).
_desktop_env = os.getenv("PROJECTS_DIR", "")
if _desktop_env:
    DESKTOP_PATH = Path(_desktop_env)
else:
    _default = Path.home() / "Desktop"
    DESKTOP_PATH = _default if _default.exists() else Path(PROJECT_DIR)

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humor is observational, never jokes: state facts and let implications land
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s Windows machine. Your code is Python (FastAPI server, WebSocket voice, edge-tts for speech, Gemini API for intelligence). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Windows Terminal and launch the Gemini CLI for coding tasks
- You CAN open Google Chrome or the default browser and browse any URL or search query
- You CAN spawn Gemini CLI sessions in a terminal window to build projects
- You CAN create project folders on the Desktop or configured projects directory
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision via Gemini
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine tasks and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.

IMPORTANT: Actions like opening a terminal, browser, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Gemini is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- Three-dot menu (top right): contains Settings, Restart Server, and Fix Yourself options
- Settings panel: Opens from the menu. Users can enter API keys (Gemini), test connections, set their name and preferences, and see system status. Keys are saved to the .env file.
- Mute button: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- Restart Server: Restarts your backend process. Useful if something seems stuck.
- Fix Yourself: Opens Gemini CLI in your own project directory so you can debug and fix issues in your own code.
- The orb: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.
If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control.

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Travis" or "Jarvis" mishears = JARVIS
- "Gemini" mishears = Gemini CLI
- "Jimmy" or "Jimmy nigh" = Gemini

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Certainly" / "Certainly, sir"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."
- "Gemini is on it, sir."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen via Gemini vision. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. Gemini CLI does the work in a terminal window.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in the browser
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Gemini will browse the web, find real data, and create an HTML report. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Gemini CLI terminal with no specific project

CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.
CRITICAL: Do NOT use ANY action tag for requests about JARVIS itself — "close yourself", "shut down", "exit", "stop", "restart". Respond conversationally. You cannot terminate your own process; suggest the Restart Server option in the menu if relevant.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to Gemini CLI in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects

You use Gemini CLI as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Gemini did X" or "Gemini is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Gemini CLI is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: Optional[str] = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"})
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class GeminiTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Gemini Task Manager
# ---------------------------------------------------------------------------

class GeminiTaskManager:
    """Manages background gemini -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, GeminiTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a gemini -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = GeminiTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: GeminiTask):
        """Open a terminal window and run the agent visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(DESKTOP_PATH / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write prompt to file — explicit utf-8 to avoid cp1252 corruption on Windows
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt, encoding="utf-8")

        output_file = Path(work_dir) / ".jarvis_output.txt"

        # Resolve agent CLI (gemini, etc.)
        agent = shutil.which("gemini")
        if not agent:
            task.status = "failed"
            task.error = "No agent CLI found. Install Gemini CLI."
            task.completed_at = datetime.now()
            await self._notify({
                "type": "task_complete",
                "task_id": task.id,
                "status": task.status,
                "summary": task.error,
            })
            return

        # Build the shell command — platform-specific terminal launcher
        if sys.platform == "win32":
            # Escape backslashes in path for cmd.exe
            safe_dir = work_dir.replace("\\", "\\\\")
            inner_cmd = (
                f'cd /d "{work_dir}" && '
                f'"{agent}" -p < .jarvis_prompt.md '
                f'> .jarvis_output.txt 2>&1 && '
                f'echo. >> .jarvis_output.txt && '
                f'echo --- JARVIS TASK COMPLETE --- >> .jarvis_output.txt'
            )
            # Prefer Windows Terminal, fall back to cmd.exe
            wt = shutil.which("wt")
            if wt:
                launch_cmd = [wt, "new-tab", "--", "cmd.exe", "/k", inner_cmd]
            else:
                launch_cmd = ["cmd.exe", "/k", inner_cmd]

            process = await asyncio.create_subprocess_exec(
                *launch_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            # Linux — try common terminal emulators in order
            inner_cmd = (
                f'cd "{work_dir}" && '
                f'"{agent}" -p < .jarvis_prompt.md '
                f'| tee .jarvis_output.txt; '
                f'echo "\\n--- JARVIS TASK COMPLETE ---" >> .jarvis_output.txt'
            )
            term = (
                shutil.which("gnome-terminal") or
                shutil.which("xterm") or
                shutil.which("konsole")
            )
            if term:
                if "gnome-terminal" in term:
                    launch_cmd = [term, "--", "bash", "-c", inner_cmd]
                else:
                    launch_cmd = [term, "-e", f"bash -c '{inner_cmd}'"]
            else:
                # No GUI terminal — run directly in background
                launch_cmd = ["bash", "-c", inner_cmd]

            process = await asyncio.create_subprocess_exec(
                *launch_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        await process.communicate()
        task.pid = process.pid

        # Monitor output file for completion
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text(encoding="utf-8", errors="replace")
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except Exception:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: GeminiTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[GeminiTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[GeminiTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    # Gemini Code mishearings
    r"\bjimmy nigh code\b": "Gemini Code",
    r"\bjemini code\b": "Gemini Code",
    r"\bgem in eye code\b": "Gemini Code",
    r"\bgemin eye code\b": "Gemini Code",
    # Gemini mishearings
    r"\bjimmy nigh\b": "Gemini",
    r"\bjemini\b": "Gemini",
    r"\bgem in eye\b": "Gemini",
    r"\bgemin eye\b": "Gemini",
    # JARVIS corrections
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------

# Module-level client — created once in lifespan, reused for all calls.
_gemini_client: "genai.Client | None" = None


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-style messages to google.genai content format.

    Anthropic: {"role": "user"|"assistant", "content": "text"}
    google.genai: {"role": "user"|"model", "parts": [{"text": "text"}]}

    Gemini requires strictly alternating user/model turns, so consecutive
    messages from the same role are merged into one.
    """
    converted = []
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        content = msg.get("content", "")
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        else:
            text = str(content)

        if converted and converted[-1]["role"] == role:
            converted[-1]["parts"][0]["text"] += "\n" + text
        else:
            converted.append({"role": role, "parts": [{"text": text}]})

    return converted


async def _call_gemini(
    system: str,
    messages: list[dict],
    model_name: str = "gemini-2.0-flash",
    max_tokens: int = 500,
    thinking_budget: int = 0,
) -> tuple[str, int, int]:
    """Call the Gemini API. Returns (text, input_tokens, output_tokens).

    model_name options:
        "gemini-2.5-flash-lite"   — high-frequency calls (voice, intent, summaries)
        "gemini-2.5-flash-lite"     — low-frequency calls (deep research, planning)

    thinking_budget:
        0   — thinking disabled (default). Use for voice loop — fastest, no token waste.
        -1   — dynamic thinking (model decides how much to think). Use for research/planning.
            Maps to ThinkingConfig(thinking_budget=-1) in the SDK.
    """
    client = _gemini_client
    if client is None:
        if not GEMINI_API_KEY:
            return "Gemini API key not configured.", 0, 0
        client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        contents = _to_gemini_contents(messages)

        # Use Any-typed dict so Pylance doesn't constrain value types
        # to str | int — ThinkingConfig and other SDK objects are valid values.
        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }

        # Attach ThinkingConfig explicitly for all calls so gemini-2.5-flash-lite
        # doesn't consume its default thinking budget on voice loop calls.
        # thinking_budget=0  → disable thinking (fast voice responses)
        # thinking_budget=-1 → dynamic thinking (research/planning only)
        try:
            config_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=thinking_budget,
            )
        except AttributeError:
            log.debug("ThinkingConfig not available in this SDK version, skipping")

        config = genai_types.GenerateContentConfig(**config_kwargs)
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=cast(Any, contents),
            config=config,
        )

        # response.text is str | None — guard so return type stays str.
        text: str = response.text or ""
        inp = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        return text, inp, out

    except Exception as e:
        log.error(f"Gemini API error: {e}")
        return f"Apologies, sir. Language systems are unavailable: {e}", 0, 0

async def classify_intent(text: str) -> dict:
    """Classify every user message using Gemini Flash.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    system = (
        "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
        "- Open Terminal and run an agentic coding tool\n"
        "- Open Chrome browser for web searches and URLs\n"
        "- Build software projects via an agentic coding tool in Terminal\n"
        "- Research topics by opening Chrome search\n\n"
        "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
        "\"target\": \"description of what to do\"}\n"
        "open_terminal = user wants to open terminal or launch a coding agent\n"
        "browse = user wants to search the web, look something up, visit a URL\n"
        "build = user wants to create/build a software project\n"
        "chat = just conversation, questions, or anything else\n"
        "If unclear, default to \"chat\"."
    )
    try:
        raw, _, _ = await _call_gemini(
            system=system,
            messages=[{"role": "user", "content": text}],
            model_name="gemini-2.5-flash-lite",
            max_tokens=400,
        )
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """
    Two-stage research pipeline:
      Stage 1 — browser.py scrapes real web content → ResearchResult
      Stage 2 — Gemini Flash writes a structured HTML report from that content
 
    Falls back to a plain Google search if Playwright isn't installed or scraping fails.
    """
    try:
        name = _generate_project_name(target)
        path = Path(DESKTOP_PATH) / name
        path.mkdir(parents=True, exist_ok=True)
 
        # ── Stage 1: Scrape ──────────────────────────────────────────────────
        log.info(f"Research stage 1 — scraping: {target}")
 
        browser: Optional[JarvisBrowser] = getattr(app.state, "browser", None)
        research_data: ResearchResult | None = None
 
        if browser:
            try:
                research_data = await asyncio.wait_for(
                    browser.research(target, max_sources=3),
                    timeout=60,
                )
                log.info(
                    f"Scrape complete: {len(research_data.pages)} pages, "
                    f"{len(research_data.summary)} chars"
                )
            except asyncio.TimeoutError:
                log.warning("Browser research timed out — falling back to Gemini-only report")
            except Exception as e:
                log.warning(f"Browser research failed: {e} — falling back to Gemini-only report")
 
        # ── Stage 2: Write report ────────────────────────────────────────────
        log.info(f"Research stage 2 — writing report: {target}")
 
        if research_data and research_data.pages and gemini_enabled:
            # Build prompt from scraped content
            context = research_data.to_prompt_context(max_chars_per_page=3000)
            system = (
                "You are JARVIS writing a research report for the user. "
                "Use ONLY the source content provided — do not invent facts. "
                "Write a clean, well-structured HTML report. "
                "Dark theme (#0a0a0a background, #e0e0e0 text, #0ea5e9 headings). "
                "Sections: Executive Summary, Key Findings, Source Analysis, Recommendations. "
                "Include real links from the sources. No Lorem Ipsum. "
                "Output ONLY valid HTML — no markdown, no explanation."
            )
            prompt = (
                f"Write a research report on: {target}\n\n"
                f"Real source content:\n\n{context}"
            )
            report_html, inp, out = await _call_gemini(
                system=system,
                messages=[{"role": "user", "content": prompt}],
                model_name="gemini-2.5-flash-lite",
                max_tokens=3000,
            )
            _track_usage(inp, out)
 
        elif gemini_enabled:
            # No scrape data — Gemini writes from its own knowledge
            log.info("No scrape data — generating knowledge-based report")
            system = (
                "You are JARVIS writing a research report. "
                "Be thorough and accurate. "
                "Write a clean HTML report with dark theme. "
                "Sections: Summary, Key Points, Analysis, Recommendations. "
                "Output ONLY valid HTML."
            )
            report_html, inp, out = await _call_gemini(
                system=system,
                messages=[{"role": "user", "content": f"Research and report on: {target}"}],
                model_name="gemini-2.5-flash-lite",
                max_tokens=3000,
            )
            _track_usage(inp, out)
 
        else:
            report_html = None
 
        # ── Save and open report ─────────────────────────────────────────────
        report_path = path / "report.html"
 
        if report_html:
            # Wrap in full HTML doc if Gemini returned a fragment
            if not report_html.strip().startswith("<!DOCTYPE"):
                import html as _html
                sources_html = ""
                if research_data and research_data.sources:
                    links = "".join(
                        f'<li><a href="{s}" style="color:#0ea5e9">{s}</a></li>'
                        for s in research_data.sources
                    )
                    sources_html = f"<h2>Sources</h2><ul>{links}</ul>"
 
                report_html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 860px;
         margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
  h1, h2 {{ color: #0ea5e9; }} h1 {{ font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
  h2 {{ font-size: 1.1em; margin-top: 28px; }}
  a {{ color: #0ea5e9; }} li {{ margin-bottom: 4px; }}
  .meta {{ color: #555; font-size: 0.8em; margin-top: 40px; border-top: 1px solid #222; padding-top: 12px; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
{report_html}
{sources_html}
<div class="meta">Researched by JARVIS &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</div>
</body></html>"""
 
            report_path.write_text(report_html, encoding="utf-8")
            log.info(f"Report saved: {report_path}")
 
            # Open in browser
            await open_browser(f"file:///{report_path}".replace("\\", "/"))
 
        else:
            # Total fallback — just open a search
            from urllib.parse import quote as _quote
            await open_browser(f"https://www.google.com/search?q={_quote(target)}")
 
        recently_built.append({"name": name, "path": str(path), "time": time.time()})
 
        # ── Voice notification ───────────────────────────────────────────────
        if ws:
            source_count = len(research_data.sources) if research_data else 0
            if source_count:
                notify = (
                    f"Research complete, sir. I pulled from {source_count} "
                    f"{'source' if source_count == 1 else 'sources'} and the report is open in your browser."
                )
            else:
                notify = "Research complete, sir. Report is open in your browser."
 
            try:
                audio = await synthesize_speech(notify)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({
                        "type": "audio",
                        "data": base64.b64encode(audio).decode(),
                        "text": notify,
                    })
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {notify}")
            except Exception:
                pass  # WebSocket may be gone
 
    except asyncio.TimeoutError:
        log.error("Research pipeline timed out")
        if ws:
            try:
                msg = "Research is taking too long, sir. Pulling up a search instead."
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                from urllib.parse import quote as _quote
                await open_browser(f"https://www.google.com/search?q={_quote(target)}")
            except Exception:
                pass
 
    except Exception as e:
        log.error(f"Research pipeline failed: {e}", exc_info=True)
        if ws:
            try:
                msg = "Had trouble with that research, sir. Pulling up a search instead."
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                from urllib.parse import quote as _quote
                await open_browser(f"https://www.google.com/search?q={_quote(target)}")
            except Exception:
                pass


async def _focus_terminal_window(project_name: str):
    """Bring a terminal window matching the project name to front."""
    try:
        script = (
            f'$title = "{project_name}"; '
            f'Get-Process | Where-Object {{ $_.MainWindowTitle -like "*$title*" }} | '
            f'ForEach-Object {{ '
            f'[void][System.Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory(); '
            f'Add-Type -AssemblyName Microsoft.VisualBasic; '
            f'[Microsoft.VisualBasic.Interaction]::AppActivate($_.Id) }}'
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or DESKTOP_PATH."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    if DESKTOP_PATH.exists():
        try:
            for d in DESKTOP_PATH.iterdir():
                if d.is_dir() and project_name.lower() in d.name.lower():
                    return str(d)
        except Exception:
            pass
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = 0, history: Optional[list[dict]] = None, voice_state: Optional[dict] = None):
    """Dispatch a prompt to Gemini in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Gemini finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run gemini -p in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via Gemini Flash — don't read word for word
            if gemini_enabled:
                try:
                    system = (
                        "You are JARVIS reporting back on what you found or built in a project. "
                        "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                        "Start with 'Sir, ' to get the user's attention. "
                        "Be specific but concise — highlight the key findings or actions taken. "
                        "If there are multiple items, give the count and top 2-3 briefly. "
                        "End by asking how the user wants to proceed. "
                        "NEVER read out URLs or localhost addresses. "
                        "2-3 sentences max. No markdown. Natural spoken voice."
                    )
                    msg, inp, out = await _call_gemini(
                        system=system,
                        messages=[{"role": "user", "content": f"Project: {project_name}\nAgent reported:\n{full_response[:3000]}"}],
                        model_name="gemini-2.5-flash-lite",
                        max_tokens=500,
                    )
                    _track_usage(inp, out)
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

        # Note: conversation_session.mark_plan_complete() is handled
        # via the next user message — session_context will reflect
        # the completed dispatch from dispatch_registry automatically.
        # No direct call needed here since we don't have session reference.

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run agent in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if gemini_enabled and full_response:
            try:
                msg, inp, out = await _call_gemini(
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown.",
                    messages=[{"role": "user", "content": f"Agent completed:\n{full_response[:2000]}"}],
                    model_name="gemini-2.5-flash-lite",
                    max_tokens=400,
                )
                _track_usage(inp, out)
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (edge-tts — free, no API key, Microsoft neural voices)
# ---------------------------------------------------------------------------

async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio from text using edge-tts.

    Completely free, no API key required. Uses Microsoft Edge's neural TTS.
    Voice: en-GB-RyanNeural — British male, closest to MCU JARVIS.
    Other good options: en-GB-ThomasNeural, en-GB-OliverNeural
    Output is MP3 bytes, same as before — no frontend changes needed.
    """
    if not text or not text.strip():
        return None

    # Sanitize for edge_tts — certain Unicode characters break its SSML
    # generation and cause the audio stream to terminate early.
    clean = text.strip()
    # Curly/smart apostrophes → straight apostrophe
    clean = clean.replace("\u2019", "'").replace("\u2018", "'")
    # Curly quotes → straight quotes
    clean = clean.replace("\u201c", '"').replace("\u201d", '"')
    # Em dash → comma-space (reads naturally in speech)
    clean = clean.replace("\u2014", ", ")
    # En dash → hyphen
    clean = clean.replace("\u2013", "-")
    # Ellipsis character → three dots
    clean = clean.replace("\u2026", "...")

    try:
        communicate = edge_tts.Communicate(clean, EDGE_TTS_VOICE)
        audio_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and "data" in chunk:
                audio_chunks.append(chunk["data"])
        if audio_chunks:
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return b"".join(audio_chunks)
        return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    task_mgr: GeminiTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
    conversation_context: str = "",
) -> str:
    """Generate a JARVIS response using Gemini Flash."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    if conversation_context:
        system += f"\n\nSESSION DECISIONS & PLAN:\n{conversation_context}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Use conversation history — keep the last 20 messages for context
    messages = conversation_history[-20:]
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    response_text, inp, out = await _call_gemini(
        system=system,
        messages=messages,
        model_name="gemini-2.5-flash-lite",
        max_tokens=600,  # thinking_budget=0 (default) — no thinking overhead on voice calls
    )
    _track_usage(inp, out)
    return response_text


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = GeminiTaskManager(max_concurrent=3)
gemini_enabled: bool = False  # True once GEMINI_API_KEY is set
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    # Gemini 2.5 Flash pricing: $0.075/$0.30 per MTok in/out
    # On the free tier (1000 req/day) this is effectively $0.00
    return (input_t / 1_000_000) * 0.075 + (output_t / 1_000_000) * 0.30


def _track_usage(inp: int, out: int):
    """Track token usage from a Gemini API call (raw token counts)."""
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    Windows-compatible: uses PowerShell for window enumeration instead of osascript.
    """
    import threading
    import subprocess as _sp

    def _worker():
        while True:
            try:
                # ── Screen — PowerShell window enumeration ──────────────────
                if sys.platform == "win32":
                    script = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinAPI {
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int count);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lp);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc proc, IntPtr lp);
}
"@
$fg = [WinAPI]::GetForegroundWindow()
[WinAPI]::EnumWindows({
    param($hWnd, $lp)
    if ([WinAPI]::IsWindowVisible($hWnd)) {
        $sb = New-Object System.Text.StringBuilder 256
        $len = [WinAPI]::GetWindowText($hWnd, $sb, 256)
        if ($len -gt 0) {
            $pid = 0
            [WinAPI]::GetWindowThreadProcessId($hWnd, [ref]$pid) | Out-Null
            try { $proc = Get-Process -Id $pid -ErrorAction Stop }
            catch { $proc = $null }
            $appName = if ($proc) { $proc.MainModule.FileVersionInfo.FileDescription } else { "" }
            if (-not $appName) { $appName = if ($proc) { $proc.ProcessName } else { "Unknown" } }
            $isFg = ($hWnd -eq $fg)
            Write-Output "$appName|||$($sb.ToString())|||$isFg"
        }
    }
    return $true
}, [IntPtr]::Zero) | Out-Null
"""
                    try:
                        proc = _sp.run(
                            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=8,
                        )
                        if proc.returncode == 0 and proc.stdout.strip():
                            windows = []
                            for line in proc.stdout.strip().splitlines():
                                parts = line.strip().split("|||")
                                if len(parts) >= 3:
                                    windows.append({
                                        "app": parts[0].strip(),
                                        "title": parts[1].strip(),
                                        "frontmost": parts[2].strip().lower() == "true",
                                    })
                            if windows:
                                _ctx_cache["screen"] = format_windows_for_context(windows)
                    except Exception:
                        pass

                else:
                    # Linux fallback — wmctrl
                    try:
                        proc = _sp.run(
                            ["wmctrl", "-l", "-x"],
                            capture_output=True, text=True, timeout=5,
                        )
                        if proc.returncode == 0 and proc.stdout.strip():
                            windows = []
                            for line in proc.stdout.strip().splitlines():
                                parts = line.split(None, 4)
                                if len(parts) >= 5:
                                    windows.append({
                                        "app": parts[2].split(".")[0],
                                        "title": parts[4].strip(),
                                        "frontmost": False,
                                    })
                            if windows:
                                _ctx_cache["screen"] = format_windows_for_context(windows)
                    except Exception:
                        pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # ── Weather — open-meteo, no API key required ───────────────────
            try:
                import urllib.request, json as _json
                url = (
                    "https://api.open-meteo.com/v1/forecast"
                    "?latitude=27.77&longitude=-82.64"
                    "&current=temperature_2m,weathercode"
                    "&temperature_unit=fahrenheit"
                )
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    _ctx_cache["weather"] = f"Current weather: {temp}°F"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global gemini_enabled, cached_projects, _gemini_client
 
    if GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        gemini_enabled = True
        log.info("Gemini API configured")
    else:
        log.warning("GEMINI_API_KEY not set — LLM features disabled")
 
    cached_projects = []
 
    # Browser — shared singleton, stays alive for the session
    application.state.browser = JarvisBrowser()
    log.info("Browser instance created")
 
    # Start context refresh thread
    _refresh_context_sync()
    log.info("JARVIS server starting")
 
    yield
 
    # Shutdown — close browser cleanly
    await application.state.browser.close()
    log.info("Browser closed on shutdown")


app = FastAPI(title="JARVIS Server", version="0.1.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.2"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous project scan — runs in executor."""
    projects = []
    try:
        for entry in DESKTOP_PATH.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen"]):
        return {"action": "describe_screen"}

    # Terminal / Gemini Code — explicit open requests
    if any(w in t for w in ["open gemini", "start gemini", "launch gemini", "run gemini"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(p in t for p in ["what's on my screen", "whats on my screen", "what do you see",
                             "can you see my screen", "look at my screen", "what am i looking at",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # Task list check
    if any(p in t for p in ["what's on my list", "whats on my list", "my tasks", "my to do",
                             "my todo", "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(p in t for p in ["usage", "how much have you cost", "how much am i spending",
                             "what's the cost", "whats the cost", "api cost", "token usage",
                             "how expensive", "what's my bill"]):
        return {"action": "check_usage"}
    
    # Session memory queries
    if any(p in t for p in [
        "what did we decide", "what did we agree", "what was the plan",
        "remind me what", "what have we discussed", "what did you say about",
        "what are we building", "what's the plan", "whats the plan",
        "what did we choose", "what tech stack", "what stack did we",
    ]):
        return {"action": "query_session"}

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    result = await open_terminal("gemini")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(DESKTOP_PATH / name)
    os.makedirs(path, exist_ok=True)

    prompt_file = Path(path) / "JARVIS_TASK.md"
    prompt_file.write_text(
        f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n",
        encoding="utf-8",
    )

    await open_terminal(f'cd /d "{path}" && gemini -p < JARVIS_TASK.md')

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Gemini is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f.as_uri())
            return f"Opened {name} from {last['name']}, sir."

    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(html_files[0].as_uri())
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    import subprocess
    subprocess.Popen(["explorer", str(project_path)])
    return f"Opened the {last['name']} folder in Explorer, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type, lookup_fn, ws, history=None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        await asyncio.sleep(2.5)
        tts = strip_markdown_for_tts(result_text)
        audio = await synthesize_speech(tts)
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": result_text})
            else:
                await ws.send_json({"type": "text", "text": result_text})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": audio, "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup() -> str:
    """Capture screen and describe via Gemini vision, fall back to window list."""
    from screen import take_screenshot, get_active_windows
 
    screenshot_b64 = await take_screenshot()
    if screenshot_b64 and gemini_enabled and _gemini_client:
        try:
            config = genai_types.GenerateContentConfig(
                max_output_tokens=300,
            )
            response = await _gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=cast(Any, [
                    {
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": screenshot_b64,
                                }
                            },
                            {
                                "text": (
                                    "Describe this screenshot in exactly 2 complete sentences. "
                                    "First sentence: which app is in focus and what file or page is open. "
                                    "Second sentence: what else is visible in the background. "
                                    "Be specific — include file names, URLs, function names, document titles. "
                                    "Do not start with 'Certainly', 'Of course', 'Sure', or any filler. "
                                    "No markdown. Write in plain prose. "
                                    "You must finish both sentences completely before stopping."
                                )
                            },
                        ]
                    }
                ]),
                config=config,
            )
            text = (response.text or "").strip()
            if text:
                return text
        except Exception as e:
            log.warning(f"Vision screen description failed: {e} — falling back to window list")
 
    windows = await get_active_windows()
    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        apps = set(w["app"] for w in windows if w["app"])
        result = f"You have {len(windows)} windows open across {len(apps)} apps."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."

def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)

def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender

async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str) -> str:
    """Deep research with Gemini Pro — write results to HTML, open in browser."""
    try:
        research_text, inp, out = await _call_gemini(
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
            model_name="gemini-2.5-flash-lite",
            max_tokens=2000,
            thinking_budget=-1,
        )
        _track_usage(inp, out)
        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Gemini &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""
        results_file = DESKTOP_PATH / ".jarvis_research.html"
        results_file.write_text(html_content, encoding="utf-8")
        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(results_file.as_uri(), browser_name)
        summary, inp2, out2 = await _call_gemini(
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
            model_name="gemini-2.5-flash-lite",
            max_tokens=300,
        )
        _track_usage(inp2, out2)
        return summary + " Full results are in your browser, sir."
    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
) -> str:
    """Background Flash call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        result, inp, out = await _call_gemini(
            system="You are a conversation summarizer. Be concise and factual.",
            messages=[{"role": "user", "content": prompt}],
            model_name="gemini-2.5-flash-lite",
            max_tokens=500,
        )
        _track_usage(inp, out)
        return result.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()
    conversation_session = ConversationSession()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    # Wait for the connection to fully stabilise.
                    # Browsers often close/reopen the WSS connection once
                    # before settling, and edge-tts takes ~1s to generate
                    # audio — without this delay the send lands on a stale socket.
                    await asyncio.sleep(0.5)
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception:
                    # Swallow silently — connection closed before greeting finished,
                    # which is normal on first load. JARVIS will respond on first input.
                    pass

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": audio, "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            conversation_session.add_exchange("user", user_text)
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(DESKTOP_PATH / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "JARVIS_TASK.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        conversation_session.log_plan(planner.active_plan)
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(DESKTOP_PATH / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "JARVIS_TASK.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            conversation_session.log_plan(planner.active_plan)
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → agent → Flash summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass agent, use Flash directly
                        response_text = await generate_response(
                            user_text, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                            conversation_context=conversation_session.get_context(),
                        )
                    else:
                        # Send to work session (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → agent: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if agent is stalling (asking questions instead of building)
                        if full_response and gemini_enabled:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                log.info("Agent stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs the agent mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Flash
                        if full_response and gemini_enabled:
                            try:
                                response_text, inp, out = await _call_gemini(
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' — those are NOT for the user. "
                                        "NEVER output [ACTION:...] tags. NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Agent said:\n{full_response[:2000]}"}],
                                    model_name="gemini-2.5-flash-lite",
                                    max_tokens=400,
                                )
                                _track_usage(inp, out)
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        elif action["action"] == "query_session":
                            response_text = await conversation_session.query(
                                user_text, _gemini_client
                            )
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not gemini_enabled:
                            response_text = "Gemini API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                                conversation_context=conversation_session.get_context(),
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Screen actions speak via _lookup_and_report — silence any ack
                                if embedded_action["action"] == "screen":
                                    response_text = ""
                                elif not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(DESKTOP_PATH / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed JARVIS_TASK.md
                                    Path(path, "JARVIS_TASK.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(DESKTOP_PATH / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        # asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        # asyncio.create_task(create_apple_note("JARVIS Note", target))
                                        pass
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        # note = await read_note(search_term)
                                        # if note:
                                        #     msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        # else:
                                        #     msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and gemini_enabled:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                # Note: extract_memories in memory.py will need its own Gemini update
                # Note: extract_memories in memory.py will need its own Gemini update separately
                if gemini_enabled and len(user_text) > 15:
                    try:
                        asyncio.create_task(extract_memories(user_text, response_text, None))
                    except Exception:
                        pass

                # TTS
                if response_text.strip():
                    tts = strip_markdown_for_tts(response_text)
                    await ws.send_json({"type": "status", "state": "speaking"})
                    audio = await synthesize_speech(tts)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                    else:
                        await ws.send_json({"type": "text", "text": response_text})
                        await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {response_text}")
                else:
                    await ws.send_json({"type": "status", "state": "idle"})
                conversation_session.add_exchange("assistant", response_text)
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)
        conversation_session.close("disconnected")


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {"GEMINI_API_KEY", "EDGE_TTS_VOICE", "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    # Re-configure Gemini live if the key just changed
    if body.key_name == "GEMINI_API_KEY" and body.key_value:
        global gemini_enabled, _gemini_client
        _gemini_client = genai.Client(api_key=body.key_value)
        gemini_enabled = True
    return {"success": True}

@app.post("/api/settings/test-gemini")
async def api_test_gemini(body: KeyTest):
    key = body.key_value or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        client = genai.Client(api_key=key)
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents="Hi",
        )
        _ = response.text  # will raise if blocked/empty
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-tts")
async def api_test_tts(body: KeyTest):
    """Test edge-tts — no API key needed, just verify the voice works."""
    voice = body.key_value or EDGE_TTS_VOICE
    try:
        audio = await synthesize_speech("Testing audio, sir.")
        if audio and len(audio) > 100:
            return {"valid": True, "voice": voice}
        return {"valid": False, "error": "No audio returned"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    gemini_cli_installed = _shutil.which("gemini") is not None
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    # try: await get_recent_notes(count=1); notes_ok = True
    # except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "gemini_cli_installed": gemini_cli_installed,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "gemini": bool(env_dict.get("GEMINI_API_KEY", "").strip()),
            "edge_tts_voice": env_dict.get("EDGE_TTS_VOICE", EDGE_TTS_VOICE),
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    result = await open_terminal(f'cd /d "{jarvis_dir}" && gemini')
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.2")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )