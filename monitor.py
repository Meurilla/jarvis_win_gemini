#!/usr/bin/env python3
"""
JARVIS Conversation Monitor

Watches the JARVIS server logs in real-time, analyzes conversation quality,
and reports issues that need fixing. Run alongside the JARVIS server.

Usage:
    # Pipe from running server
    python monitor.py

    # Monitor a log file (follows like tail -f)
    python monitor.py --log-file server.log

    # Quiet mode (only report issues)
    python monitor.py --quiet
"""

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ANSI color codes (only if output is a terminal)
COLORS = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
    "reset": "\033[0m",
}


def colorize(text: str, color: str, use_color: bool) -> str:
    if use_color:
        return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"
    return text


class ConversationMonitor:
    def __init__(self, use_color: bool = True, quiet: bool = False):
        self.messages: list[dict] = []
        self.issues: list[str] = []
        self.last_report_time = time.time()
        self.report_interval = 30  # Report every 30 seconds if issues found
        self.use_color = use_color
        self.quiet = quiet
        self.max_history = 1000

    def add_message(self, role: str, text: str):
        self.messages.append({
            "role": role,
            "text": text,
            "time": datetime.now().isoformat(),
        })
        # Cap history
        if len(self.messages) > self.max_history:
            self.messages = self.messages[-self.max_history:]
        self.analyze_latest()

    def analyze_latest(self):
        if len(self.messages) < 2:
            return

        latest = self.messages[-1]
        # Get previous user message (skip assistant messages)
        prev_user = None
        for m in reversed(self.messages[:-1]):
            if m["role"] == "user":
                prev_user = m
                break

        # ── Check JARVIS responses ──
        if latest["role"] == "jarvis":
            text = latest["text"]

            # Too long for voice?
            sentences = text.split(". ")
            if len(sentences) > 4:
                self.flag(f"JARVIS response too long for voice ({len(sentences)} sentences): {text[:80]}...")

            # Generic AI patterns that JARVIS shouldn't use
            bad_patterns = [
                ("How can I help", "JARVIS doesn't ask 'how can I help' — he just acts"),
                ("Is there anything else", "JARVIS doesn't ask 'is there anything else'"),
                ("I'd be happy to", "Too corporate — JARVIS says 'Will do, sir' or just does it"),
                ("Absolutely!", "JARVIS doesn't use filler enthusiasm"),
                ("Great question", "JARVIS never says 'great question'"),
                ("I don't have access", "JARVIS should say 'I'm afraid I don't have that information, sir'"),
                ("As an AI", "JARVIS never breaks character"),
                ("I cannot", "JARVIS says 'I'm afraid that's beyond my current capabilities, sir'"),
                ("I apologize", "JARVIS doesn't apologize — he states facts"),
                ("Certainly", "JARVIS never says 'certainly'"),
                ("Of course", "JARVIS never says 'of course'"),
            ]
            for pattern, issue in bad_patterns:
                if pattern.lower() in text.lower():
                    self.flag(f"BAD PATTERN: '{pattern}' detected. {issue}")

            # Not using "sir" enough?
            jarvis_msgs = [m for m in self.messages if m["role"] == "jarvis"]
            if len(jarvis_msgs) >= 5:
                recent = jarvis_msgs[-5:]
                sir_count = sum(1 for m in recent if "sir" in m["text"].lower())
                if sir_count < 1:
                    self.flag("JARVIS hasn't said 'sir' in the last 5 responses — should use it more")

            # Forgot context?
            if prev_user:
                user_text = prev_user["text"].lower()
                if any(w in user_text for w in ["earlier", "before", "you said", "we talked about", "remember"]):
                    if "I don't recall" in text or "I'm not sure what" in text:
                        self.flag("JARVIS failed to recall earlier conversation — memory issue")

            # Response references Samantha (from Her) — JARVIS should never mention her
            if "samantha" in text.lower():
                self.flag("JARVIS referenced 'Samantha' — should never mention her, he IS the assistant")

        # ── Check user messages for complaints ──
        if latest["role"] == "user":
            text = latest["text"].lower()
            complaint_patterns = [
                "you forgot", "you don't remember", "i already told you",
                "that's wrong", "no that's not right", "you're not listening",
                "i said", "what i meant was", "can you hear me",
                "that doesn't work", "you can't do that",
            ]
            for pattern in complaint_patterns:
                if pattern in text:
                    self.flag(f"USER COMPLAINT detected: '{pattern}' — review JARVIS's previous response")

    def flag(self, issue: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {issue}"
        self.issues.append(entry)
        # Always print to stderr with color
        color = "red" if "COMPLAINT" in issue or "ERROR" in issue else "yellow"
        print(colorize(f"\n⚠️  {entry}", color, self.use_color), file=sys.stderr)

    def report(self):
        if not self.issues:
            return

        now = time.time()
        if now - self.last_report_time < self.report_interval:
            return

        self.last_report_time = now
        print("\n" + colorize("=" * 60, "cyan", self.use_color), file=sys.stderr)
        print(colorize(f"MONITOR REPORT — {len(self.issues)} issues found", "cyan", self.use_color), file=sys.stderr)
        print(colorize("=" * 60, "cyan", self.use_color), file=sys.stderr)
        for issue in self.issues[-10:]:  # Last 10
            print(f"  {issue}", file=sys.stderr)
        print(colorize("=" * 60, "cyan", self.use_color), file=sys.stderr)

    def echo_message(self, role: str, text: str):
        """Print a formatted message to stdout (if not quiet)."""
        if self.quiet:
            return
        if role == "user":
            prefix = colorize("👤", "green", self.use_color)
        else:
            prefix = colorize("🤖", "blue", self.use_color)
        # Truncate long messages for display
        display = text[:120] + ("..." if len(text) > 120 else "")
        print(f"{prefix} {display}")


def tail_file(filepath: Path, monitor: ConversationMonitor):
    """Follow a log file (like tail -f)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                process_line(line, monitor)
    except FileNotFoundError:
        print(f"Error: Log file '{filepath}' not found.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


def process_line(line: str, monitor: ConversationMonitor):
    line = line.strip()
    if not line:
        return

    # Match patterns: with or without timestamp
    # Typical log: "2024-01-01 12:00:00 [jarvis] User: hello"
    # Simpler: look for "User: " or "JARVIS: " anywhere
    user_match = re.search(r"User:\s*(.+)$", line)
    if user_match:
        text = user_match.group(1).strip()
        monitor.echo_message("user", text)
        monitor.add_message("user", text)

    jarvis_match = re.search(r"JARVIS:\s*(.+)$", line)
    if jarvis_match:
        text = jarvis_match.group(1).strip()
        monitor.echo_message("jarvis", text)
        monitor.add_message("jarvis", text)

    # Error detection
    if "error" in line.lower() or "Error" in line:
        if any(key in line.lower() for key in ["llm error", "tts error", "websocket error", "exception"]):
            monitor.flag(f"SERVER ERROR: {line[:150]}")

    monitor.report()


def main():
    parser = argparse.ArgumentParser(description="JARVIS Conversation Monitor")
    parser.add_argument("--log-file", type=Path, help="Log file to monitor (tails like tail -f)")
    parser.add_argument("--quiet", action="store_true", help="Suppress real-time message echo")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    args = parser.parse_args()

    use_color = not args.no_color and sys.stderr.isatty()
    monitor = ConversationMonitor(use_color=use_color, quiet=args.quiet)

    # Print startup info to stderr
    print(colorize("🔍 JARVIS Conversation Monitor", "cyan", use_color), file=sys.stderr)
    if args.log_file:
        print(f"   Following log file: {args.log_file}", file=sys.stderr)
    else:
        print("   Reading from stdin (pipe from server output)", file=sys.stderr)
    print("   Press Ctrl+C to stop\n", file=sys.stderr)

    try:
        if args.log_file:
            tail_file(args.log_file, monitor)
        else:
            # Check if stdin is a terminal (no pipe)
            if sys.stdin.isatty():
                print("Error: No input. Pipe server output or use --log-file.", file=sys.stderr)
                print("\nExample: python server.py | python monitor.py", file=sys.stderr)
                sys.exit(1)
            for line in sys.stdin:
                process_line(line, monitor)
    except KeyboardInterrupt:
        print("\n\nMonitor stopped.", file=sys.stderr)
        if monitor.issues:
            print(f"\nTotal issues found: {len(monitor.issues)}", file=sys.stderr)
            for issue in monitor.issues:
                print(f"  {issue}", file=sys.stderr)


if __name__ == "__main__":
    main()

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
None. The script is standalone; no API changes.

Bug Fixes
Unused subprocess import – Removed.

Stdin blocking when no pipe – Added check for sys.stdin.isatty(); shows usage error instead of hanging.

Message history unbounded – Capped to 1000 entries.

Pattern matching – Made more flexible with regex that matches "User: " or "JARVIS: " anywhere in the line (not just at start).

Output pollution – Status messages now go to stderr; only message echoes go to stdout (or suppressed with --quiet).

Improvements
Argument parsing – Added --log-file to tail a log file directly, --quiet to suppress echo, --no-color to disable ANSI colors.

Color support – Optional colored output for better readability (auto-detects terminal).

Additional bad patterns – Added "I apologize", "Certainly", "Of course".

Error detection – More specific keywords for server errors.

Code structure – Separated line processing, added tail_file function.

Removed / Deprecated
None.
"""