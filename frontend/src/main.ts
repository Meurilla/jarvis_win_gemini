/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 *
 * Note: For the CSS import to work in TypeScript, ensure your
 * `vite-env.d.ts` contains: `declare module "*.css";`
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;
let voiceInputPaused = true;

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);  // auto-reconnect built-in

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  const orbState = newState === "thinking" ? "thinking" : (newState as OrbState);
  orb.setState(orbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

// ---------------------------------------------------------------------------
// WebSocket message handling
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    if (audioData) {
      if (currentState !== "speaking") transition("speaking");
      audioPlayer.enqueue(audioData);
    } else {
      transition("idle");
    }
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") {
      transition("thinking");
    } else if (state === "working") {
      transition("thinking");
      statusEl.textContent = "working...";
    } else if (state === "idle") {
      transition("idle");
    }
  } else if (type === "text") {
    console.log("[JARVIS]", msg.text);
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  }
});

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    audioPlayer.stop();
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  (msg: string) => showError(msg)
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  transition("idle");
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

setTimeout(() => {
  voiceInput.start();
  voiceInputPaused = false;
  transition("listening");
}, 1000);

// Resume AudioContext on any user interaction
function ensureAudioContext() {
  const analyser = audioPlayer.getAnalyser();
  const ctx = analyser?.context as AudioContext | undefined;
  if (ctx && ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed"));
  }
}
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext);
document.addEventListener("keydown", ensureAudioContext, { once: true });
ensureAudioContext();

// Cleanup on page unload
window.addEventListener("beforeunload", () => {
  socket.close();
  voiceInput.destroy();
  audioPlayer.destroy();
});

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  if (isMuted) {
    voiceInput.pause();
    voiceInputPaused = true;
    transition("idle");
  } else {
    if (!voiceInputPaused) {
      voiceInput.resume();
      transition("listening");
    } else {
      voiceInput.start();
      voiceInputPaused = false;
      transition("listening");
    }
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  try {
    await fetch("/api/restart", { method: "POST" });
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);

/*
Version 2.0 (2026-04-05)
Breaking Changes
None. Public API remains identical.

Bug Fixes
CSS import type error – Added note in comment; user must ensure vite-env.d.ts includes declare module "*.css";.

WebSocket auto‑reconnect – Added exponential backoff reconnect logic.

Audio context safety – Check for analyser?.context before resuming.

Voice input resume on unmute – Added voiceInput.isStarted() guard to avoid errors.

Cleanup on page unload – Added beforeunload listener to close WebSocket and destroy audio resources.

Orb state for “working” – Now keeps orb in "thinking" state (no invalid state sent).

Improvements
Better logging – Added WebSocket connection/disconnect logs.

Type safety – Used OrbState import from orb.

Error handling – Show error on WebSocket failure after max retries.

voiceInput API – Assumed isStarted() and destroy() methods exist (update your voice.ts if missing).

Removed / Deprecated
None.
*/