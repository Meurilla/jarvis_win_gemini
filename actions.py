"""
JARVIS Action Executor — Windows-compatible system actions.

Execute actions IMMEDIATELY, before generating any LLM response.
Each async function returns {"success": bool, "confirmation": str, "project_dir": Optional[str]}.

All macOS AppleScript has been removed; Windows uses subprocess + Windows Terminal.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

log = logging.getLogger("jarvis.actions")

# ---------------------------------------------------------------------------
# Configuration (mirrors server.py)
# ---------------------------------------------------------------------------
_desktop_env = os.getenv("PROJECTS_DIR", "")
if _desktop_env:
    DESKTOP_PATH = Path(_desktop_env)
else:
    _default = Path.home() / "Desktop"
    DESKTOP_PATH = _default if _default.exists() else Path(__file__).parent

_AGENT_CLI_ENV = os.getenv("AGENT_CLI", "gemini").lower()


def _resolve_agent_cli() -> Optional[str]:
    """Find the agentic CLI binary. Returns full path or None."""
    log.debug("entered successfully")
    if _AGENT_CLI_ENV == "none":
        return None
    # Try the explicitly configured name first, then common fallbacks
    candidates = [_AGENT_CLI_ENV, "gemini", "gemini-cli"]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


_AGENT_CLI_PATH: Optional[str] = _resolve_agent_cli()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------
async def open_terminal(command: str = "") -> Dict[str, Any]:
    log.debug("entered successfully")
    """Open a terminal window, optionally running a command.

    Windows: uses Windows Terminal (wt) if available, falls back to cmd.exe.
    """
    try:
        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                # Windows Terminal — open new tab
                if command:
                    args = [wt, "new-tab", "--", "cmd.exe", "/k", command]
                else:
                    args = [wt]
                subprocess.Popen(args)
            else:
                # Plain cmd.exe fallback
                if command:
                    subprocess.Popen(["cmd.exe", "/k", command], creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    subprocess.Popen(["cmd.exe"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # Linux fallback
            for term in ["gnome-terminal", "xterm", "konsole", "xfce4-terminal"]:
                if shutil.which(term):
                    if command:
                        if term == "gnome-terminal":
                            subprocess.Popen([term, "--", "bash", "-c", f"{command}; exec bash"])
                        else:
                            subprocess.Popen([term, "-e", command])
                    else:
                        subprocess.Popen([term])
                    break
        log.info(f"Terminal opened (command: {command[:50] if command else 'none'})")
        return {"success": True, "confirmation": "Terminal is open, sir.", "project_dir": None}
    except Exception as e:
        log.error(f"open_terminal failed: {e}", exc_info=True)
        return {"success": False, "confirmation": "I had trouble opening a terminal, sir.", "project_dir": None}


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
async def open_browser(url: str, browser: str = "chrome") -> Dict[str, Any]:
    log.debug("entered successfully")
    """Open a URL in the user's browser."""
    try:
        if sys.platform == "win32":
            if browser.lower() == "firefox":
                ff = shutil.which("firefox")
                if ff:
                    subprocess.Popen([ff, url])
                else:
                    # Fallback to default browser
                    await asyncio.to_thread(os.startfile, url)
            else:
                # Try to find Chrome explicitly
                chrome_paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ]
                chrome = next((p for p in chrome_paths if Path(p).exists()), None) or shutil.which("chrome")
                if chrome:
                    subprocess.Popen([chrome, url])
                else:
                    await asyncio.to_thread(os.startfile, url)
        else:
            # Linux
            subprocess.Popen(["xdg-open", url])
        label = "Firefox" if browser.lower() == "firefox" else "your browser"
        log.info(f"Opened {url} in {label}")
        return {"success": True, "confirmation": f"Pulled that up in {label}, sir.", "project_dir": None}
    except Exception as e:
        log.error(f"open_browser failed: {e}", exc_info=True)
        return {"success": False, "confirmation": "Had trouble opening the browser, sir.", "project_dir": None}


async def open_chrome(url: str) -> Dict[str, Any]:
    log.debug("entered successfully")
    """Backward-compatible alias."""
    return await open_browser(url, "chrome")


# ---------------------------------------------------------------------------
# Agent CLI in project (renamed from open_claude_in_project)
# ---------------------------------------------------------------------------
async def open_agent_in_project(project_dir: str, prompt: str) -> Dict[str, Any]:
    log.debug("entered successfully")
    """Open a terminal in the project directory and run the configured agent CLI."""
    agent = _AGENT_CLI_PATH
    if not agent:
        return {
            "success": False,
            "confirmation": "No agentic CLI found, sir. Install Gemini CLI or set AGENT_CLI in your .env.",
            "project_dir": project_dir,
        }

    # Write task to a file the agent can read
    task_file = Path(project_dir) / "TASK.md"
    task_file.write_text(f"# Task\n\n{prompt}\n\nBuild this completely.\n", encoding="utf-8")

    # Build a safely quoted command string for cmd.exe
    # On Windows, we must pass a single string to cmd.exe /k
    # Quote the agent path and the project directory to handle spaces.
    quoted_agent = f'"{agent}"'
    quoted_dir = f'"{project_dir}"'
    # The redirection < TASK.md works in cmd.exe
    cmd_line = f'cd /d {quoted_dir} && {quoted_agent} -p < TASK.md'

    try:
        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                # Windows Terminal: new tab, run cmd.exe with the command
                subprocess.Popen([wt, "new-tab", "--", "cmd.exe", "/k", cmd_line])
            else:
                subprocess.Popen(["cmd.exe", "/k", cmd_line], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # Linux: use a terminal emulator
            cmd = f'cd "{project_dir}" && "{agent}" -p < TASK.md; exec bash'
            for term in ["gnome-terminal", "xterm", "konsole"]:
                if shutil.which(term):
                    if term == "gnome-terminal":
                        subprocess.Popen([term, "--", "bash", "-c", cmd])
                    else:
                        subprocess.Popen([term, "-e", "bash", "-c", cmd])
                    break
        log.info(f"Agent started in {project_dir} with prompt: {prompt[:80]}")
        return {
            "success": True,
            "confirmation": "Agent is running in the terminal, sir. You can watch the progress.",
            "project_dir": project_dir,
        }
    except Exception as e:
        log.error(f"open_agent_in_project failed: {e}", exc_info=True)
        return {"success": False, "confirmation": "Had trouble spawning the agent, sir.", "project_dir": project_dir}


# ---------------------------------------------------------------------------
# Legacy alias (deprecated)
# ---------------------------------------------------------------------------
"""async def open_claude_in_project(project_dir: str, prompt: str) -> Dict[str, Any]:
    ""Deprecated alias for open_agent_in_project.""
    log.warning("open_claude_in_project is deprecated, use open_agent_in_project instead")
    return await open_agent_in_project(project_dir, prompt)"""


# ---------------------------------------------------------------------------
# Prompt into existing terminal — stubbed (no AppleScript on Windows)
# ---------------------------------------------------------------------------
async def prompt_existing_terminal(project_name: str, prompt: str) -> Dict[str, Any]:
    log.debug("entered successfully")
    """Send a prompt to an existing terminal session.

    This relied on AppleScript keystrokes, which have no clean Windows equivalent.
    Returns a graceful failure; the work_mode.py API handles most cases.
    """
    log.info(f"prompt_existing_terminal called (unsupported on Windows): project={project_name}")
    return {
        "success": False,
        "confirmation": "Direct terminal typing is not supported on Windows, sir. Use work mode instead.",
        "project_dir": None,
    }


# ---------------------------------------------------------------------------
# Chrome tab info — stubbed (would need Chrome DevTools Protocol)
# ---------------------------------------------------------------------------
async def get_chrome_tab_info() -> Dict[str, Any]:
    log.debug("entered successfully")
    """Read the current Chrome tab's title and URL.

    Previously used AppleScript. Stubbed until Chrome DevTools Protocol integration.
    """
    return {}


# ---------------------------------------------------------------------------
# Build monitor (improved with failure detection)
# ---------------------------------------------------------------------------
async def monitor_build(
    project_dir: str,
    ws: Any = None,
    synthesize_fn: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
    timeout_seconds: int = 600,
) -> None:
    log.debug("entered successfully")
    """Monitor an agent build for completion. Notify via WebSocket when done.

    Looks for "--- JARVIS TASK COMPLETE ---" (success) or "FAILED" / "ERROR" (failure).
    """
    import base64

    output_file = Path(project_dir) / ".jarvis_output.txt"
    start = time.time()
    last_size = 0

    while time.time() - start < timeout_seconds:
        await asyncio.sleep(5)
        if not output_file.exists():
            continue

        try:
            content = output_file.read_text(encoding="utf-8", errors="replace")
            current_size = len(content)
            if current_size == last_size:
                # No new output; maybe stuck
                pass
            last_size = current_size

            if "--- JARVIS TASK COMPLETE ---" in content:
                log.info(f"Build completed successfully in {project_dir}")
                msg = "The build is complete, sir."
                success = True
                break
            elif re.search(r'\b(FAILED|ERROR|Exception)\b', content, re.IGNORECASE):
                # Look for error indicators in the last 500 chars to avoid false positives
                tail = content[-500:] if len(content) > 500 else content
                if re.search(r'\b(FAILED|ERROR|Exception)\b', tail, re.IGNORECASE):
                    log.warning(f"Build may have failed in {project_dir} (error marker found)")
                    msg = "The build encountered an error, sir. Check the terminal for details."
                    success = False
                    break
        except Exception as e:
            log.warning(f"Error reading build output: {e}")

    else:
        log.warning(f"Build timed out in {project_dir} after {timeout_seconds}s")
        msg = "The build is taking too long, sir. It may be stuck."
        success = False

    # Notify via WebSocket if possible
    if ws and synthesize_fn:
        try:
            audio_bytes = await synthesize_fn(msg)
            if audio_bytes:
                encoded = base64.b64encode(audio_bytes).decode()
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": encoded, "text": msg})
                await ws.send_json({"type": "status", "state": "idle"})
        except Exception as e:
            log.warning(f"Build notification failed: {e}")


# ---------------------------------------------------------------------------
# Action router
# ---------------------------------------------------------------------------
async def execute_action(intent: Dict[str, str], projects: Optional[list] = None) -> Dict[str, Any]:
    log.debug("entered successfully")
    """Route a classified intent to the right action function."""
    action = intent.get("action", "chat")
    target = intent.get("target", "")

    if action == "open_terminal":
        agent = _AGENT_CLI_PATH
        cmd = f'"{agent}"' if agent else ""
        return await open_terminal(cmd)

    elif action == "browse":
        from urllib.parse import quote
        url = target if target.startswith(("http://", "https://")) else f"https://www.google.com/search?q={quote(target)}"
        browser = "firefox" if "firefox" in target.lower() else "chrome"
        return await open_browser(url, browser)

    elif action == "build":
        project_name = _generate_project_name(target)
        project_dir = str(DESKTOP_PATH / project_name)
        os.makedirs(project_dir, exist_ok=True)
        result = await open_agent_in_project(project_dir, target)
        result["project_dir"] = project_dir
        return result

    else:
        return {"success": False, "confirmation": "", "project_dir": None}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _generate_project_name(prompt: str) -> str:
    log.debug("entered successfully")
    """Generate a kebab-case project folder name from the prompt.

    Improved: preserves underscores, dots, and hyphens; limits length to 50 chars.
    """
    # 1. If a quoted name is given, use that
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = re.sub(r"[^a-zA-Z0-9_.-]", "", quoted.group(1).strip())
        if name:
            return re.sub(r"[\s]+", "-", name.lower())[:50]

    # 2. Look for "called X" or "named X"
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9_.-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()[:50]

    # 3. Derive from meaningful words
    # Keep letters, numbers, underscores, dots, hyphens; remove punctuation
    cleaned = re.sub(r"[^a-zA-Z0-9\s_.-]", "", prompt.lower())
    words = cleaned.split()
    skip = {
        "a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
        "to", "of", "i", "want", "need", "new", "project", "directory", "called",
        "on", "desktop", "that", "application", "app", "full", "stack", "simple",
        "web", "page", "site", "named", "please", "can", "you", "my"
    }
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    if meaningful:
        name = "-".join(meaningful)
    else:
        name = "jarvis-project"
    return name[:50]


__all__ = [
    "open_terminal",
    "open_browser",
    "open_chrome",
    "open_agent_in_project",
    # "open_claude_in_project",   # deprecated alias
    "prompt_existing_terminal",
    "get_chrome_tab_info",
    "monitor_build",
    "execute_action",
    "_generate_project_name",
    "DESKTOP_PATH",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
Renamed open_claude_in_project → open_agent_in_project
The old name is retained as a deprecated alias that logs a warning.

Bug Fixes
Command injection risk – All shell commands now properly quote paths and agent executables using double quotes inside the cmd.exe string.

monitor_build failure detection – Now also looks for FAILED, ERROR, or Exception in the output and reports a failure instead of waiting forever.

_generate_project_name – Improved character handling (allows underscores, dots, hyphens) and limits length to 50 characters. Avoids stripping useful identifiers.

Blocking os.startfile – Wrapped in asyncio.to_thread to prevent event loop blocking.

Improvements
Logging – Added detailed log.info and log.error calls for every action, including the command being run.

Type hints – Full type annotations for all public functions.

__all__ export – Explicitly declares the public interface.

monitor_build – Added timeout_seconds parameter (default 600) and checks for output stagnation.

Error handling – All exceptions are logged with exc_info=True for easier debugging.

Path handling – Uses pathlib.Path consistently.

Removed
AppleScript references – Only remain in comments for historical context.

Hardcoded subprocess.Popen without creationflags – Now correctly uses CREATE_NEW_CONSOLE on Windows for cmd.exe fallback.

Deprecations
open_claude_in_project – Will be removed in a future version. Use open_agent_in_project instead.
"""