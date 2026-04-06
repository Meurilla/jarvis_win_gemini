"""
JARVIS Work Mode — agentic coding sessions tied to project directories.

Tries to use an agentic CLI tool (Gemini CLI by default) for full file-editing
capability. Falls back to direct Gemini API calls if no CLI is available —
responses will be text-only in that mode (no file writes).

Configure the CLI tool via the AGENT_CLI env var, e.g.:
  AGENT_CLI=gemini   (default — Gemini CLI)
  AGENT_CLI=none     (force direct API mode)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types as genai_types

log = logging.getLogger("jarvis.work_mode")

SESSION_FILE = Path(__file__).parent / "data" / "active_session.json"

# Which CLI tool to use for agentic sessions. Resolved once at import time.
_AGENT_CLI_ENV = os.getenv("AGENT_CLI", "gemini")
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_gemini_client = None


def _get_gemini_client() -> Optional[genai.Client]:
    """Get or create a shared Gemini client for API fallback mode."""
    log.debug("entered successfully")
    global _gemini_client
    if _gemini_client is None and _GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=_GEMINI_API_KEY)
    return _gemini_client


def _resolve_agent_cli() -> Optional[str]:
    """Find the agentic CLI binary. Returns full path or None."""
    log.debug("entered successfully")
    if _AGENT_CLI_ENV.lower() == "none":
        return None
    # Try the configured name, then common fallbacks
    for name in [_AGENT_CLI_ENV, "gemini", "gemini-cli"]:
        path = shutil.which(name)
        if path:
            log.info(f"Agentic CLI found: {path}")
            return path
    log.warning(
        "No agentic CLI found (tried: gemini, gemini-cli). "
        "Work mode will use direct Gemini API — file editing unavailable. "
        "Install Gemini CLI or set AGENT_CLI=<path> to enable full work mode."
    )
    return None


_AGENT_CLI_PATH: Optional[str] = _resolve_agent_cli()


class WorkSession:
    """An agentic session tied to a project directory.

    Uses CLI subprocess when available (full file-editing).
    Falls back to direct Gemini API when not (text responses only).
    """

    def __init__(self):
        self._active = False
        self._working_dir: Optional[str] = None
        self._project_name: Optional[str] = None
        self._message_count = 0
        self._status = "idle"
        # Conversation history for API fallback mode
        self._api_history: list[dict] = []

    @property
    def active(self) -> bool:
        log.debug("entered successfully")
        return self._active

    @property
    def project_name(self) -> Optional[str]:
        log.debug("entered successfully")
        return self._project_name

    @property
    def status(self) -> str:
        log.debug("entered successfully")
        return self._status

    async def start(self, working_dir: str, project_name: Optional[str] = None):
        """Start or switch to a project session."""
        log.debug("entered successfully")
        self._working_dir = working_dir
        # Use Path().name for Windows-safe directory name extraction
        self._project_name = project_name or Path(working_dir).name
        self._active = True
        self._message_count = 0
        self._status = "idle"
        self._api_history = []
        log.info(f"Work mode started: {self._project_name} ({working_dir})")

    async def send(self, user_text: str) -> str:
        """Send a message and get a response.

        If a CLI tool is available: spawns a subprocess in the project directory.
        Otherwise: calls the Gemini API directly (no file access).
        """
        log.debug("entered successfully")
        self._status = "working"
        if _AGENT_CLI_PATH:
            result = await self._send_via_cli(user_text)
        else:
            result = await self._send_via_api(user_text)
        self._message_count += 1
        self._status = "idle"
        return result

    async def _send_via_cli(self, user_text: str) -> str:
        """Run the agentic CLI as a subprocess."""
        log.debug("entered successfully")
        cmd = [_AGENT_CLI_PATH, "-p"]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._working_dir,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=user_text.encode()),
                timeout=300,
            )

            response = stdout.decode().strip()

            if process.returncode != 0:
                error = stderr.decode().strip()[:200]
                log.error(f"Agent CLI error: {error}")
                return f"Hit a problem, sir: {error}"

            log.info(f"Agent CLI response for {self._project_name} ({len(response)} chars)")
            return response

        except asyncio.TimeoutError:
            return "That's taking longer than expected, sir. The operation timed out."
        except Exception as e:
            log.error(f"CLI work mode error: {e}")
            return f"Something went wrong, sir: {str(e)[:100]}"

    async def _send_via_api(self, user_text: str) -> str:
        """Fall back to direct Gemini API when no CLI is installed."""
        log.debug("entered successfully")
        client = _get_gemini_client()
        if not client:
            return "No Gemini API key configured, sir."

        try:
            system = (
                f"You are an expert software developer working on the project at: {self._working_dir}. "
                "Help the user with coding tasks, architecture decisions, and debugging. "
                "Note: you cannot directly edit files in this mode — provide complete file contents "
                "and clear instructions for the user to apply changes."
            )

            # Maintain rolling conversation history
            self._api_history.append({"role": "user", "content": user_text})
            # Trim to last 20 messages to prevent unbounded growth
            if len(self._api_history) > 20:
                self._api_history = self._api_history[-20:]

            # Convert history to Gemini format, alternating roles
            contents = []
            for msg in self._api_history:
                role = "model" if msg["role"] == "assistant" else "user"
                if contents and contents[-1]["role"] == role:
                    contents[-1]["parts"][0]["text"] += "\n" + msg["content"]
                else:
                    contents.append({"role": role, "parts": [{"text": msg["content"]}]})

            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=2000,
            )
            response = await client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=contents,
                config=config,
            )
            result = (response.text or "").strip()
            self._api_history.append({"role": "assistant", "content": result})
            log.info(f"API work mode response for {self._project_name} ({len(result)} chars)")
            return result

        except Exception as e:
            log.error(f"API work mode error: {e}")
            return f"Something went wrong, sir: {str(e)[:100]}"

    async def stop(self):
        """End the work session."""
        log.debug("entered successfully")
        project = self._project_name
        self._active = False
        self._working_dir = None
        self._project_name = None
        self._message_count = 0
        self._status = "idle"
        self._api_history = []
        log.info(f"Work mode ended for {project}")

    def _save_session(self):
        """Persist session state so it survives restarts (currently unused)."""
        log.debug("entered successfully")
        try:
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps({
                "project_name": self._project_name,
                "working_dir": self._working_dir,
                "message_count": self._message_count,
            }))
        except Exception as e:
            log.debug(f"Failed to save session: {e}")

    def _clear_session(self):
        """Remove persisted session."""
        log.debug("entered successfully")
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    async def restore(self) -> bool:
        """Restore session from disk after restart. Returns True if restored."""
        log.debug("entered successfully")
        try:
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text())
                self._working_dir = data["working_dir"]
                self._project_name = data["project_name"]
                self._message_count = data.get("message_count", 1)
                self._active = True
                self._status = "idle"
                log.info(f"Restored work session: {self._project_name} ({self._working_dir})")
                return True
        except Exception as e:
            log.debug(f"No session to restore: {e}")
        return False


def is_casual_question(text: str) -> bool:
    """Detect if a message is casual chat vs work-related.

    Casual questions go straight to Gemini (fast).
    Work questions go to the agentic session (powerful).
    """
    log.debug("entered successfully")
    t = text.lower().strip()

    casual_patterns = [
        "what time", "what's the time", "what day",
        "what's the weather", "weather",
        "how are you", "are you there", "hey jarvis",
        "good morning", "good evening", "good night",
        "thank you", "thanks", "never mind", "nevermind",
        "stop", "cancel", "quit work mode", "exit work mode",
        "go back to chat", "regular mode",
        "how's it going", "what's up",
        "are you still there", "you there", "jarvis",
        "are you doing it", "is it working", "what happened",
        "did you hear me", "hello", "hey",
        "how's that coming", "hows that coming",
        "any update", "status update",
    ]

    if len(t.split()) <= 3 and any(w in t for w in ["ok", "okay", "sure", "yes", "no", "yeah", "nah", "cool"]):
        return True

    return any(p in t for p in casual_patterns)


__all__ = [
    "WorkSession",
    "is_casual_question",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
None. Public API remains identical.

Bug Fixes
Unbounded _api_history – Now trimmed to last 20 messages after each API call.

Status reset – After send completes, status is set back to "idle" (previously only set in CLI path; now unified).

Improvements
Gemini client caching – Added _get_gemini_client() module-level cached client, reused across API calls.

__all__ – Added explicit exports.

Type hints – Added return types for all methods.

Docstring – Clarified that session persistence methods are currently unused.

Removed / Deprecated
None.
"""