/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 */

// ---------------------------------------------------------------------------
// Speech Recognition
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  setBargeInMode(enabled: boolean): void;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void,
  onBargeIn?: () => void,
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    return { start() {}, stop() {}, pause() {}, resume() {}, setBargeInMode() {} };
  }

  let recognition = new SR();
  let shouldListen = false;
  let paused = false;
  let bargeInMode = false;  // keep listening while speaking, fire onBargeIn instead
  let restartTimer: ReturnType<typeof setTimeout> | null = null;

  function configureRecognition() {
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          const text = event.results[i][0].transcript.trim();
          if (!text) continue;
          if (bargeInMode && onBargeIn) {
            // Interrupt JARVIS mid-speech — fire barge-in then deliver transcript
            onBargeIn();
            bargeInMode = false;
            setTimeout(() => onTranscript(text), 200);
          } else {
            onTranscript(text);
          }
        }
      }
    };

    recognition.onend = () => {
      if (shouldListen && !paused) {
        scheduleRestart(100);
      }
    };

    recognition.onerror = (event: any) => {
      if (event.error === "not-allowed") {
        // Permission denied — retry after a delay in case user re-grants
        onError("Microphone blocked. Please allow microphone access in Chrome.");
        scheduleRestart(3000);
      } else if (event.error === "no-speech") {
        // Normal silence — recognition restarts via onend
      } else if (event.error === "aborted") {
        // Expected on pause
      } else if (event.error === "network") {
        // Chrome network glitch — rebuild and restart
        console.warn("[voice] network error, rebuilding recognition");
        scheduleRestart(500);
      } else if (event.error === "audio-capture") {
        onError("Microphone not found.");
        scheduleRestart(2000);
      } else {
        console.warn("[voice] recognition error:", event.error);
        scheduleRestart(500);
      }
    };
  }

  function scheduleRestart(delayMs: number) {
    if (restartTimer) clearTimeout(restartTimer);
    restartTimer = setTimeout(() => {
      restartTimer = null;
      if (!shouldListen || paused) return;
      // Rebuild recognition object to clear any Chrome internal error state
      try { recognition.stop(); } catch { /* ignore */ }
      recognition = new SR();
      configureRecognition();
      try { recognition.start(); } catch { /* ignore */ }
    }, delayMs);
  }

  configureRecognition();

  return {
    start() {
      shouldListen = true;
      paused = false;
      try {
        recognition.start();
      } catch {
        scheduleRestart(100);
      }
    },
    stop() {
      shouldListen = false;
      paused = false;
      if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
      try { recognition.stop(); } catch { /* ignore */ }
    },
    pause() {
      // In barge-in mode, keep listening — don't actually pause
      if (bargeInMode) return;
      paused = true;
      if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
      try { recognition.stop(); } catch { /* ignore */ }
    },
    resume() {
      paused = false;
      bargeInMode = false;
      if (shouldListen) {
        try {
          recognition.start();
        } catch {
          scheduleRestart(100);
        }
      }
    },
    setBargeInMode(enabled: boolean) {
      bargeInMode = enabled;
      if (enabled && shouldListen && paused) {
        // Re-enable listening so we can detect barge-in
        paused = false;
        try { recognition.start(); } catch { scheduleRestart(100); }
      }
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
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  // Auto-resume AudioContext if Chrome suspends it (tab switch, focus loss, etc.)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && audioCtx.state === "suspended") {
      audioCtx.resume().catch(() => {});
    }
  });
  document.addEventListener("click", () => {
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  });

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
      // Ensure AudioContext is running
      if (audioCtx.state === "suspended") {
        try { await audioCtx.resume(); } catch { /* ignore */ }
      }
      if (audioCtx.state === "closed") {
        console.error("[audio] AudioContext is closed — cannot play");
        finishedCallback?.();
        return;
      }

      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }

        let audioBuffer: AudioBuffer;
        try {
          audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        } catch (decodeErr) {
          console.error("[audio] decodeAudioData failed:", decodeErr);
          // Fire finished so state returns to idle
          if (!isPlaying) finishedCallback?.();
          return;
        }

        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] enqueue error:", err);
        if (!isPlaying) finishedCallback?.();
      }
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        try {
          currentSource.onended = null; // Prevent playNext from firing
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
  };
}
