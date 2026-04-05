# JARVIS

**Just A Rather Very Intelligent System — Windows & Gemini Edition**

A voice-first AI assistant that runs locally on your Windows machine. Talk to it, and it talks back — with a British accent, dry wit, and an audio-reactive particle orb straight out of the MCU.

JARVIS connects to your system, browses the web, spawns Gemini CLI sessions to build entire software projects, and plans your day — all through natural voice conversation.

> "Will do, sir."

<!-- TODO: Add demo GIF or screenshot here -->
<!-- ![JARVIS Demo](docs/demo.gif) -->

---

## What It Does

- **Voice conversation** — speak naturally, get spoken responses with a JARVIS voice
- **Builds software** — say "build me a landing page" and watch Gemini CLI do the work
- **Browses the web** — "Search for the best restaurants in Austin"
- **Researches topics** — deep multi-source research with a formatted HTML report opened in your browser
- **Sees your screen** — knows what windows are open for context-aware responses, with optional Gemini vision
- **Manages tasks** — "Remind me to call the client tomorrow"
- **Takes notes** — stores session decisions and key facts across the conversation
- **Remembers things** — "I prefer React over Vue" (it remembers next time)
- **Plans your day** — combines tasks and priorities into an organized plan
- **Audio-reactive orb** — a Three.js particle visualization that pulses with JARVIS's voice

### Coming Soon (Disabled / In Progress)
- **Apple Calendar integration** — currently macOS-only via AppleScript; Windows port pending
- **Apple Mail integration** — same situation, read-only access pending cross-platform rewrite
- **Apple Notes integration** — same situation, pending cross-platform rewrite

---

## Requirements

- **Windows 10/11** (primary platform; Linux support is partially present but untested)
- **Python 3.11+**
- **Node.js 18+**
- **Google Chrome** (required for Web Speech API)
- **Google Gemini API key** — powers the AI brain ([get one here](https://aistudio.google.com/api-keys/))
- **Gemini CLI** — for spawning agentic dev tasks ([install instructions below](#gemini-cli))

---

## Quick Start (with Claude Code)

The fastest way to get running:

```bash
git clone https://github.com/yourusername/jarvis.git
cd jarvis
claude
```

Claude Code will read `JARVIS_TASK.md` and walk you through setup step by step.

---

## Manual Setup

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/jarvis.git
cd jarvis

# 2. Set up environment
cp .env.example .env
# Edit .env — add your Gemini API key and optionally your name

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers (for web research)
playwright install chromium

# 5. Install Gemini CLI
npm install -g @google/gemini-cli
gemini auth login

# 6. Install frontend dependencies
cd frontend && npm install && cd ..

# 7. Generate SSL certificates (needed for secure WebSocket + microphone access)
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

# 8. Start the backend (Terminal 1)
python server.py

# 9. Start the frontend (Terminal 2)
cd frontend && npm run dev

# 10. Open Chrome
start https://localhost:5173
```

Click anywhere on the page once to enable audio, then speak. JARVIS will respond.

> **Note:** Chrome requires a secure context (HTTPS) for microphone access. The self-signed certificate will show a browser warning — click "Advanced → Proceed" to continue.

---

## Configuration

Edit your `.env` file:

```env
# Required
GEMINI_API_KEY=your-gemini-api-key-here

# Optional — your name (JARVIS will address you by name)
USER_NAME=Tony

# Optional — override the British TTS voice
# EDGE_TTS_VOICE=en-GB-RyanNeural

# Optional — set a custom projects directory (defaults to ~/Desktop)
# PROJECTS_DIR=C:\Users\You\Projects
```

---

## Architecture

```
Microphone → Web Speech API → WebSocket → FastAPI → Gemini Flash → edge-TTS → WebSocket → Speaker
                                               |
                                               ├── Gemini CLI (agentic builds)
                                               ├── Playwright (web research)
                                               ├── SQLite (memory, tasks, dispatches)
                                               └── Screen awareness (PowerShell / Gemini vision)
```

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python (`server.py`, ~2300 lines) |
| Frontend | Vite + TypeScript + Three.js |
| Communication | WebSocket (JSON messages + binary audio) |
| AI (voice loop) | Gemini 2.5 Flash Lite — fast, low-latency responses |
| AI (research/planning) | Gemini 2.5 Flash — deeper reasoning |
| TTS | edge-tts — free, no API key, Microsoft neural voices |
| Web automation | Playwright (Chromium) |
| Storage | SQLite — memory, tasks, notes, dispatch registry |
| Screen awareness | PowerShell Win32 API (Windows), wmctrl (Linux) |

---

## How the Voice Loop Works

1. You speak into your microphone
2. Chrome's Web Speech API transcribes your speech in real-time
3. The transcript is sent to the server via WebSocket
4. JARVIS detects intent — conversation, action, screen check, build request, etc.
5. For builds: spawns Gemini CLI in a terminal window with a structured `JARVIS_TASK.md`
6. For research: Playwright scrapes real sources, Gemini writes a formatted HTML report
7. Generates a spoken response via Gemini Flash
8. edge-tts converts the response to speech (British male voice, no API key required)
9. Audio streams back to the browser via WebSocket
10. The Three.js orb deforms and pulses in response to your voice and JARVIS's speech
11. Background tasks (builds, research) notify you proactively when they complete

---

## Key Files

| File | Purpose |
|------|---------|
| `server.py` | Main server — WebSocket handler, Gemini integration, action routing |
| `frontend/src/orb.ts` | Three.js particle orb visualization |
| `frontend/src/voice.ts` | Web Speech API + AudioContext playback |
| `frontend/src/main.ts` | Frontend state machine |
| `frontend/src/settings.ts` | Settings panel (API keys, preferences) |
| `memory.py` | SQLite memory system with FTS5 full-text search |
| `actions.py` | System actions — terminal, browser, Gemini CLI launch |
| `browser.py` | Playwright web automation for research |
| `screen.py` | Cross-platform screen awareness and screenshots |
| `work_mode.py` | Persistent Gemini CLI agentic sessions |
| `planner.py` | Conversational task planning with clarifying questions |
| `conversation.py` | Session decision tracking, injected into every Gemini call |
| `dispatch_registry.py` | SQLite tracking of active and completed builds |
| `qa.py` | Gemini-powered QA verification of completed builds |
| `tracking.py` | Task success rate tracking |
| `suggestions.py` | Proactive follow-up suggestions after build completion |
| `templates.py` | Structured prompt templates for agent tasks |
| `ab_testing.py` | Template version A/B testing (async, aiosqlite) |
| `evolution.py` | Template improvement from failure pattern analysis |
| `monitor.py` | Real-time conversation quality monitor (pipe from server) |

---

## Action System

JARVIS uses embedded action tags in its responses to trigger real system actions. You don't need to use these directly — JARVIS decides when to use them based on your request:

| Tag | What it does |
|-----|-------------|
| `[ACTION:BUILD]` | Spawns Gemini CLI in a new terminal to build a project |
| `[ACTION:BROWSE]` | Opens Chrome to a URL or Google search |
| `[ACTION:RESEARCH]` | Multi-source Playwright scrape + Gemini HTML report |
| `[ACTION:SCREEN]` | Describes what's currently on your screen |
| `[ACTION:PROMPT_PROJECT]` | Connects Gemini CLI to an existing project directory |
| `[ACTION:ADD_TASK]` | Creates a tracked task with priority and due date |
| `[ACTION:ADD_NOTE]` | Saves a note to the session memory |
| `[ACTION:REMEMBER]` | Stores a persistent fact for future conversations |
| `[ACTION:COMPLETE_TASK]` | Marks a task as done |
| `[ACTION:OPEN_TERMINAL]` | Opens a fresh terminal with Gemini CLI |

---

## Gemini CLI

The Gemini CLI (`@google/gemini-cli`) is the agentic engine that does the actual file-writing and coding work. JARVIS orchestrates it; Gemini CLI executes.

```bash
# Install
npm install -g @google/gemini-cli

# Authenticate
gemini auth login

# Verify
gemini --version
```

If Gemini CLI is not installed, JARVIS falls back to direct Gemini API calls for work mode — text responses only, no file writes.

You can also override the CLI binary via your `.env`:

```env
AGENT_CLI=gemini
# or
AGENT_CLI=none   # force direct API mode
```

---

## UI Controls

The frontend has a minimal set of controls in the top-right corner:

- **Microphone button** — mute/unmute JARVIS's listening
- **Three-dot menu** — opens the dropdown:
  - **Settings** — API key entry, connection status, user preferences
  - **Restart Server** — restarts the backend process
  - **Fix Yourself** — opens Gemini CLI in JARVIS's own source directory for self-repair

The **Settings panel** slides in from the right and lets you:
- Enter and test your Gemini API key
- Check Gemini CLI installation status
- Set your name and honorific
- View system info (memory count, open tasks, uptime)

---

## Memory System

JARVIS remembers things you tell it using SQLite with FTS5 full-text search. Preferences, decisions, and facts persist across sessions. The memory is automatically searched for relevance on every request and injected into the Gemini system prompt.

Examples of things JARVIS will remember:
- "I prefer React over Vue for frontend projects"
- "The client API key expires in April"
- Decisions made during planning sessions

---

## Build Pipeline

When you ask JARVIS to build something, it runs through a full pipeline:

1. **Planning** — asks 1–2 clarifying questions (or uses smart defaults if you say "just build it")
2. **Dispatch** — writes a structured `JARVIS_TASK.md` and spawns Gemini CLI in a new terminal
3. **Monitoring** — watches for completion markers in the output file
4. **QA** — Gemini verifies the output against the original requirements
5. **Auto-retry** — if QA fails, JARVIS retries with targeted feedback (up to 3 attempts)
6. **Suggestions** — after success, JARVIS proactively suggests follow-ups (tests, README, favicon)
7. **Notification** — speaks the result when the build completes

---

## Speech Recognition Corrections

JARVIS corrects common speech-to-text mishearings automatically:

| You say | JARVIS hears |
|---------|-------------|
| "Travis" / "Jarves" | JARVIS |
| "Jimmy Nigh" / "Jemini" | Gemini |
| "Jimmy Nigh Code" | Gemini Code |

---

## Contributing

Contributions are welcome. Some areas that could use work:

- **Calendar / Mail / Notes on Windows** — replace AppleScript with Windows-native or cross-platform alternatives (EventKit → Windows Calendar API, etc.)
- **Linux polish** — wmctrl-based screen awareness works but could be improved
- **Alternative TTS voices** — add ElevenLabs, OpenAI TTS, or local model support
- **Plugin system** — make it easier to add new actions and integrations
- **Mobile client** — companion app for voice interaction on the go

Please open an issue before submitting large PRs to discuss the approach. See [CONTRIBUTING.md](CONTRIBUTING.md) for more detail.

---

## License

Free for personal, non-commercial use. Commercial use requires a license — visit [ethanplus.ai](https://ethanplus.ai) for inquiries. See [LICENSE](LICENSE) for full terms.

---

## Credits

Originally built by [Ethan](https://ethanplus.ai) for macOS using Anthropic Claude and Fish Audio.

Windows port and Gemini migration by a contributor — replacing AppleScript integrations with Windows-compatible equivalents, porting the AI layer from Anthropic Claude to Google Gemini, and switching TTS from Fish Audio to edge-tts.

Powered by [Google Gemini](https://deepmind.google/technologies/gemini/) and [Microsoft edge-tts](https://github.com/rany2/edge-tts).

Inspired by the AI that started it all — Tony Stark's JARVIS.

> **Disclaimer:** This is an independent fan project and is not affiliated with, endorsed by, or connected to Marvel Entertainment, The Walt Disney Company, or any related entities. The JARVIS name and character are property of Marvel Entertainment.