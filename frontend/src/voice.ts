/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 *
 * Windows-compatible: uses standard Web APIs, no platform-specific code.
 */

// ---------------------------------------------------------------------------
// Speech Recognition
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  isStarted(): boolean;   // Returns true if listening (or should be)
  destroy(): void;        // Clean up resources
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    return {
      start() {},
      stop() {},
      pause() {},
      resume() {},
      isStarted() { return false; },
      destroy() {},
    };
  }

  const recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let shouldListen = false;
  let paused = false;
  let started = false;

  recognition.onresult = (event: any) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        const text = event.results[i][0].transcript.trim();
        if (text) onTranscript(text);
      }
    }
  };

  recognition.onend = () => {
    started = false;
    if (shouldListen && !paused) {
      try {
        recognition.start();
        started = true;
      } catch {
        // Already started
      }
    }
  };

  recognition.onerror = (event: any) => {
    if (event.error === "not-allowed") {
      onError("Microphone access denied. Please allow microphone access.");
      shouldListen = false;
      started = false;
    } else if (event.error === "no-speech") {
      // Normal, just restart
    } else if (event.error === "aborted") {
      // Expected during pause
    } else {
      console.warn("[voice] recognition error:", event.error);
    }
  };

  return {
    start() {
      shouldListen = true;
      paused = false;
      if (!started) {
        try {
          recognition.start();
          started = true;
        } catch (e) {
          console.warn("[voice] start failed:", e);
        }
      }
    },
    stop() {
      shouldListen = false;
      paused = false;
      started = false;
      try {
        recognition.stop();
      } catch {
        // Already stopped
      }
    },
    pause() {
      paused = true;
      started = false;
      try {
        recognition.stop();
      } catch {
        // Already stopped
      }
    },
    resume() {
      paused = false;
      if (shouldListen && !started) {
        try {
          recognition.start();
          started = true;
        } catch {
          // Already started
        }
      }
    },
    isStarted() {
      return started;
    },
    destroy() {
      shouldListen = false;
      paused = false;
      started = false;
      try {
        recognition.stop();
      } catch {}
      // Remove all event listeners (no direct API, just let GC collect)
    },
  };
}

// ---------------------------------------------------------------------------
// Audio Player
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
  destroy(): void;  // Close AudioContext
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }

    isPlaying = true;
    const buffer = queue.shift()!;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(analyser);
    currentSource = source;

    source.onended = () => {
      if (currentSource === source) {
        playNext();
      }
    };

    source.start();
  }

  return {
    async enqueue(base64: string) {
      // Resume audio context (browser autoplay policy)
      if (audioCtx.state === "suspended") {
        await audioCtx.resume();
      }

      try {
        // Decode base64 to binary string, then to Uint8Array, then to ArrayBuffer
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }
        const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        // Skip bad audio, continue
        if (!isPlaying && queue.length > 0) playNext();
      }
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        try {
          currentSource.stop();
        } catch {
          // Already stopped
        }
        currentSource = null;
      }
      isPlaying = false;
    },

    getAnalyser() {
      return analyser;
    },

    onFinished(cb: () => void) {
      finishedCallback = cb;
    },

    destroy() {
      this.stop();
      audioCtx.close().catch(console.warn);
    },
  };
}

/*
Changelog
voice.ts – Version 2.0
Added isStarted() method to VoiceInput interface and implementation (returns true if recognition is active).

Added destroy() method to VoiceInput and AudioPlayer to clean up resources (close AudioContext, stop recognition).

Improved internal state tracking (started flag) for accurate isStarted().

Fixed potential memory leak by closing AudioContext on destroy.
*/