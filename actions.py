"""
JARVIS Action Executor — Windows-compatible system actions.

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.

macOS AppleScript has been replaced with cross-platform subprocess calls.
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
from urllib.parse import quote

log = logging.getLogger("jarvis.actions")

# Mirror the same DESKTOP_PATH logic as server.py so they stay in sync
_desktop_env = os.getenv("PROJECTS_DIR", "")
if _desktop_env:
    DESKTOP_PATH = Path(_desktop_env)
else:
    _default = Path.home() / "Desktop"
    DESKTOP_PATH = _default if _default.exists() else Path(__file__).parent

# Agent CLI configured via env var (same as work_mode.py)
_AGENT_CLI_ENV = os.getenv("AGENT_CLI", "gemini")


def _resolve_agent_cli() -> str | None:
    """Find the agentic CLI binary. Returns full path or None."""
    if _AGENT_CLI_ENV.lower() == "none":
        return None
    for name in [_AGENT_CLI_ENV, "gemini", "gemini-cli"]:
        path = shutil.which(name)
        if path:
            return path
    return None


_AGENT_CLI_PATH: str | None = _resolve_agent_cli()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

async def open_terminal(command: str = "") -> dict:
    """Open a terminal window, optionally running a command.

    Windows: uses Windows Terminal (wt) if available, falls back to cmd.exe.
    """
    try:
        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                # Windows Terminal — open new tab with command
                if command:
                    args = [wt, "new-tab", "--", "cmd.exe", "/k", command]
                else:
                    args = [wt]
            else:
                # Plain cmd.exe fallback
                if command:
                    args = ["cmd.exe", "/k", command]
                else:
                    args = ["cmd.exe"]
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # Linux fallback — try common terminal emulators
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

        return {"success": True, "confirmation": "Terminal is open, sir."}
    except Exception as e:
        log.error(f"open_terminal failed: {e}")
        return {"success": False, "confirmation": "I had trouble opening a terminal, sir."}


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open a URL in the user's browser.

    Uses the OS default open mechanism — no AppleScript needed.
    """
    try:
        if sys.platform == "win32":
            if browser.lower() == "firefox":
                ff = shutil.which("firefox")
                if ff:
                    subprocess.Popen([ff, url])
                else:
                    os.startfile(url)  # fall back to default browser
            else:
                # Try Chrome explicitly, fall back to default browser
                chrome_paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ]
                chrome = next((p for p in chrome_paths if Path(p).exists()), None) or shutil.which("chrome")
                if chrome:
                    subprocess.Popen([chrome, url])
                else:
                    os.startfile(url)
        else:
            # Linux
            subprocess.Popen(["xdg-open", url])

        label = "Firefox" if browser.lower() == "firefox" else "your browser"
        return {"success": True, "confirmation": f"Pulled that up in {label}, sir."}
    except Exception as e:
        log.error(f"open_browser failed: {e}")
        return {"success": False, "confirmation": "Had trouble opening the browser, sir."}


async def open_chrome(url: str) -> dict:
    """Backward-compat alias."""
    return await open_browser(url, "chrome")


# ---------------------------------------------------------------------------
# Agent CLI in project
# ---------------------------------------------------------------------------

async def open_claude_in_project(project_dir: str, prompt: str) -> dict:
    """Open a terminal in the project directory and run the configured agent CLI."""
    agent = _AGENT_CLI_PATH
    if not agent:
        return {
            "success": False,
            "confirmation": "No agentic CLI found, sir. Install Gemini CLI or set AGENT_CLI in your .env.",
        }

    # Write task to a file the agent can read
    task_file = Path(project_dir) / "TASK.md"
    task_file.write_text(f"# Task\n\n{prompt}\n\nBuild this completely.\n", encoding="utf-8")

    try:
        if sys.platform == "win32":
            wt = shutil.which("wt")
            cmd = f'cd /d "{project_dir}" && "{agent}" -p < TASK.md'
            if wt:
                subprocess.Popen([wt, "new-tab", "--", "cmd.exe", "/k", cmd])
            else:
                subprocess.Popen(["cmd.exe", "/k", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            cmd = f'cd "{project_dir}" && "{agent}" -p < TASK.md; exec bash'
            for term in ["gnome-terminal", "xterm", "konsole"]:
                if shutil.which(term):
                    subprocess.Popen([term, "--" if term == "gnome-terminal" else "-e", "bash", "-c", cmd])
                    break

        return {
            "success": True,
            "confirmation": "Agent is running in the terminal, sir. You can watch the progress.",
        }
    except Exception as e:
        log.error(f"open_claude_in_project failed: {e}")
        return {"success": False, "confirmation": "Had trouble spawning the agent, sir."}


# ---------------------------------------------------------------------------
# Prompt into existing terminal — stubbed on Windows
# ---------------------------------------------------------------------------

async def prompt_existing_terminal(project_name: str, prompt: str) -> dict:
    """Send a prompt to an existing terminal session.

    This relied on AppleScript System Events keystrokes, which have no
    clean Windows equivalent. Currently stubbed — returns graceful failure.
    The work_mode.py API fallback handles most cases where this was used.
    """
    log.info(f"prompt_existing_terminal: not supported on Windows (project={project_name})")
    return {
        "success": False,
        "confirmation": f"Direct terminal typing is not supported on Windows, sir. Use work mode instead.",
    }


# ---------------------------------------------------------------------------
# Chrome tab info — stubbed on Windows
# ---------------------------------------------------------------------------

async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL.

    Previously used AppleScript. Stubbed until Chrome DevTools Protocol
    integration is added.
    """
    return {}


# ---------------------------------------------------------------------------
# Build monitor
# ---------------------------------------------------------------------------

async def monitor_build(project_dir: str, ws=None, synthesize_fn=None) -> None:
    """Monitor an agent build for completion. Notify via WebSocket when done."""
    import base64

    output_file = Path(project_dir) / ".jarvis_output.txt"
    start = time.time()
    timeout = 600  # 10 minutes

    while time.time() - start < timeout:
        await asyncio.sleep(5)
        if output_file.exists():
            content = output_file.read_text(encoding="utf-8", errors="replace")
            if "--- JARVIS TASK COMPLETE ---" in content:
                log.info(f"Build complete in {project_dir}")
                if ws and synthesize_fn:
                    try:
                        msg = "The build is complete, sir."
                        audio_bytes = await synthesize_fn(msg)
                        if audio_bytes:
                            encoded = base64.b64encode(audio_bytes).decode()
                            await ws.send_json({"type": "status", "state": "speaking"})
                            await ws.send_json({"type": "audio", "data": encoded, "text": msg})
                            await ws.send_json({"type": "status", "state": "idle"})
                    except Exception as e:
                        log.warning(f"Build notification failed: {e}")
                return

    log.warning(f"Build timed out in {project_dir}")


# ---------------------------------------------------------------------------
# Action router
# ---------------------------------------------------------------------------

async def execute_action(intent: dict, projects: list = None) -> dict:
    """Route a classified intent to the right action function."""
    action = intent.get("action", "chat")
    target = intent.get("target", "")

    if action == "open_terminal":
        agent = _AGENT_CLI_PATH
        cmd = f'"{agent}"' if agent else ""
        result = await open_terminal(cmd)
        result["project_dir"] = None
        return result

    elif action == "browse":
        url = target if target.startswith(("http://", "https://")) else f"https://www.google.com/search?q={quote(target)}"
        browser = "firefox" if "firefox" in target.lower() else "chrome"
        result = await open_browser(url, browser)
        result["project_dir"] = None
        return result

    elif action == "build":
        project_name = _generate_project_name(target)
        project_dir = str(DESKTOP_PATH / project_name)
        os.makedirs(project_dir, exist_ok=True)
        result = await open_claude_in_project(project_dir, target)
        result["project_dir"] = project_dir
        return result

    else:
        return {"success": False, "confirmation": "", "project_dir": None}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", quoted.group(1).strip())
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"