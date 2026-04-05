"""
JARVIS Screen Awareness — see what's on the user's screen.

Windows-compatible rewrite. Uses platform-specific methods:
- Windows: PowerShell for window enumeration, PIL/mss for screenshots (optional)
- macOS: retained as fallback via screencapture + osascript
- Linux: wmctrl / xdotool for window list

Screenshot on Windows requires either:
  pip install mss Pillow   (fast, no extra setup)
OR falls back gracefully to window-list-only mode.
"""

import asyncio
import base64
import logging
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional

log = logging.getLogger("jarvis.screen")

_OS = platform.system()  # "Windows", "Darwin", "Linux"


# ---------------------------------------------------------------------------
# Window / App List
# ---------------------------------------------------------------------------

async def get_active_windows() -> List[Dict[str, Any]]:
    """Get list of visible windows with app name, window title, frontmost flag.

    Returns list of {"app": str, "title": str, "frontmost": bool}.
    Falls back to [] on any failure — never raises.
    """
    if _OS == "Windows":
        return await _get_windows_windows()
    elif _OS == "Darwin":
        return await _get_windows_macos()
    else:
        return await _get_windows_linux()


async def get_running_apps() -> List[str]:
    """Get list of running application names (visible only).

    Returns [] on failure.
    """
    if _OS == "Windows":
        return await _get_apps_windows()
    elif _OS == "Darwin":
        return await _get_apps_macos()
    else:
        return await _get_apps_linux()


# -- Windows ------------------------------------------------------------------

async def _get_windows_windows() -> List[Dict[str, Any]]:
    """Enumerate visible top-level windows via PowerShell."""
    # PowerShell script using .NET Win32 API — works on all Win10/11
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
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        windows = []
        for line in stdout.decode(errors="replace").strip().splitlines():
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                windows.append({
                    "app": parts[0].strip(),
                    "title": parts[1].strip(),
                    "frontmost": parts[2].strip().lower() == "true",
                })
        return windows
    except asyncio.TimeoutError:
        log.warning("get_active_windows (Windows) timed out")
        return []
    except Exception as e:
        log.warning(f"get_active_windows (Windows) error: {e}")
        return []


async def _get_apps_windows() -> List[str]:
    """List visible process names on Windows via PowerShell."""
    script = (
        "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | "
        "Select-Object -ExpandProperty ProcessName | Sort-Object -Unique"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
        return [
            line.strip()
            for line in stdout.decode(errors="replace").splitlines()
            if line.strip()
        ]
    except Exception as e:
        log.warning(f"get_running_apps (Windows) error: {e}")
        return []


# -- macOS --------------------------------------------------------------------

async def _get_windows_macos() -> List[Dict[str, Any]]:
    """Enumerate windows via osascript on macOS."""
    script = """
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return []
        windows = []
        for line in stdout.decode().strip().split("\n"):
            parts = line.strip().split("|||")
            if len(parts) >= 3:
                windows.append({
                    "app": parts[0].strip(),
                    "title": parts[1].strip(),
                    "frontmost": parts[2].strip().lower() == "true",
                })
        return windows
    except Exception as e:
        log.warning(f"get_active_windows (macOS) error: {e}")
        return []


async def _get_apps_macos() -> List[str]:
    script = """
tell application "System Events"
    set appNames to name of every application process whose visible is true
    set output to ""
    repeat with a in appNames
        set output to output & a & linefeed
    end repeat
    return output
end tell
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return [a.strip() for a in stdout.decode().strip().split("\n") if a.strip()]
        return []
    except Exception as e:
        log.warning(f"get_running_apps (macOS) error: {e}")
        return []


# -- Linux --------------------------------------------------------------------

async def _get_windows_linux() -> List[Dict[str, Any]]:
    """Use wmctrl if available on Linux."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "wmctrl", "-l", "-x",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        windows = []
        for line in stdout.decode().splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                windows.append({
                    "app": parts[2].split(".")[0],
                    "title": parts[4].strip(),
                    "frontmost": False,
                })
        return windows
    except Exception:
        return []


async def _get_apps_linux() -> List[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "wmctrl", "-l",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        apps = set()
        for line in stdout.decode().splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                apps.add(parts[4].strip().split(" - ")[-1])
        return sorted(apps)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

async def take_screenshot(display_only: bool = True) -> Optional[str]:
    """Take a screenshot and return base64-encoded PNG.

    Windows: tries mss (pip install mss) then PIL ImageGrab.
    macOS: uses screencapture.
    Linux: uses scrot or gnome-screenshot.

    Returns base64-encoded PNG string or None on failure.
    """
    if _OS == "Windows":
        return await _screenshot_windows()
    elif _OS == "Darwin":
        return await _screenshot_macos(display_only)
    else:
        return await _screenshot_linux()


async def _screenshot_windows() -> Optional[str]:
    """Capture primary display on Windows using mss (preferred) or PIL."""
    # Try mss first — fastest, no GUI dependency
    try:
        import mss
        import mss.tools

        def _capture():
            with mss.mss() as sct:
                # Monitor 1 = primary display
                monitor = sct.monitors[1]
                sshot = sct.grab(monitor)
                return mss.tools.to_png(sshot.rgb, sshot.size)

        png_bytes = await asyncio.to_thread(_capture)
        if png_bytes is None:
            return None
        log.info(f"Screenshot captured via mss: {len(png_bytes)} bytes")
        return base64.b64encode(png_bytes).decode()

    except ImportError:
        log.debug("mss not installed — trying PIL ImageGrab")
    except Exception as e:
        log.warning(f"mss screenshot failed: {e}")

    # Fallback: PIL ImageGrab
    try:
        from PIL import ImageGrab
        import io

        def _capture_pil():
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        png_bytes = await asyncio.to_thread(_capture_pil)
        if png_bytes is None:
            return None
        log.info(f"Screenshot captured via PIL: {len(png_bytes)} bytes")
        return base64.b64encode(png_bytes).decode()

    except ImportError:
        log.debug("PIL not installed — screenshot unavailable on Windows")
    except Exception as e:
        log.warning(f"PIL screenshot failed: {e}")

    return None


async def _screenshot_macos(display_only: bool) -> Optional[str]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    try:
        cmd = ["screencapture", "-x"]
        if display_only:
            cmd.append("-m")
        cmd.append(tmp_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0 or not Path(tmp_path).exists():
            return None
        data = Path(tmp_path).read_bytes()
        return base64.b64encode(data).decode()
    except Exception as e:
        log.warning(f"Screenshot (macOS) error: {e}")
        return None
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


async def _screenshot_linux() -> Optional[str]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    try:
        for cmd in [["scrot", tmp_path], ["gnome-screenshot", "-f", tmp_path]]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0 and Path(tmp_path).exists():
                data = Path(tmp_path).read_bytes()
                return base64.b64encode(data).decode()
    except Exception as e:
        log.warning(f"Screenshot (Linux) error: {e}")
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Describe Screen
# ---------------------------------------------------------------------------

async def describe_screen(_unused: Any = None) -> str:
    """Describe what's on the user's screen.

    Tries screenshot + vision first, falls back to window list summary.
    The _unused parameter is kept for call-site compatibility (previously
    took an anthropic client). All LLM calls now go through Gemini in server.py.

    Returns a text description suitable for voice output.
    """
    screenshot_b64 = await take_screenshot()

    if screenshot_b64:
        # Vision path — server.py _do_screen_lookup() handles the actual
        # Gemini call when it receives the base64 string. Here we just
        # return a window-based description as fallback.
        pass  # fall through to window list for the text description

    windows = await get_active_windows()
    apps = await get_running_apps()

    if not windows and not apps:
        os_hint = ""
        if _OS == "Windows":
            os_hint = (
                " On Windows, ensure PowerShell execution policy allows scripts "
                "(Set-ExecutionPolicy RemoteSigned -Scope CurrentUser)."
            )
        return f"I wasn't able to see your screen, sir.{os_hint}"

    if windows:
        active = next((w for w in windows if w["frontmost"]), None)
        unique_apps = set(w["app"] for w in windows if w["app"])
        result = f"You have {len(windows)} windows open across {len(unique_apps)} apps."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result

    return f"Running apps: {', '.join(apps[:8])}. Couldn't read window titles, sir."


def format_windows_for_context(windows: List[Dict[str, Any]]) -> str:
    """Format window list as context string for the LLM."""
    if not windows:
        return ""
    lines = ["Currently open on your desktop:"]
    for w in windows:
        marker = " (active)" if w["frontmost"] else ""
        app = w["app"] or "Unknown"
        lines.append(f"  - {app}: {w['title']}{marker}")
    return "\n".join(lines)


__all__ = [
    "get_active_windows",
    "get_running_apps",
    "take_screenshot",
    "describe_screen",
    "format_windows_for_context",
]

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
describe_screen signature – Parameter renamed from anthropic_client to _unused. Callers should pass None or remove the argument. (The server already passes None or ignores it – check _do_screen_lookup in server.py which calls _do_screen_lookup with no argument – that’s fine because the default is _unused=None.)

Bug Fixes
Deprecated asyncio.get_event_loop().run_in_executor – Replaced with asyncio.to_thread (Python 3.9+). Now cleaner and more idiomatic.

Improvements
Type hints – Added full type annotations.

__all__ – Explicitly exported public symbols.

Logging – Minor improvements (no functional change).

Removed / Deprecated
Claude remnant – Removed anthropic_client parameter name from describe_screen. The function no longer accepts a client; all vision is handled in server.py.

Integration note:
Replace your existing screen.py with this version. In server.py, ensure any call to describe_screen does not pass a client argument. The current _do_screen_lookup in server.py calls await describe_screen() with no arguments – that’s fine. If there is a call like await describe_screen(client), change it to await describe_screen().
"""