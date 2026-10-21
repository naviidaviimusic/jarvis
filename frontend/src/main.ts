/**
 * JARVIS — Main entry point (enhanced).
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

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;
const responseTextEl = document.getElementById("response-text")!;
const feedbackBar = document.getElementById("feedback-bar")!;
const micCanvas = document.getElementById("mic-canvas") as HTMLCanvasElement;
const historyPanel = document.getElementById("history-panel")!;
const historyList = document.getElementById("history-list")!;
const shortcutHint = document.getElementById("shortcut-hint")!;

// Conversation history for display
const displayHistory: Array<{ role: string; content: string }> = [];
let lastJarvisText = "";
let streamingText = "";
let feedbackTimer: ReturnType<typeof setTimeout> | null = null;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => { errorEl.style.opacity = "0"; }, 5000);
}

function showToast(msg: string) {
  const toast = document.createElement("div");
  toast.className = "trigger-toast";
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3700);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "", listening: "listening...", thinking: "thinking...", speaking: "",
  };
  statusEl.textContent = labels[state];
  statusEl.classList.toggle("thinking", state === "thinking");
}

// ---------------------------------------------------------------------------
// History panel
// ---------------------------------------------------------------------------

function addToHistory(role: string, content: string) {
  if (!content.trim()) return;
  displayHistory.push({ role, content });
  if (displayHistory.length > 40) displayHistory.shift();

  const entry = document.createElement("div");
  entry.className = `history-entry ${role}`;
  entry.innerHTML = `
    <div class="history-role">${role === "user" ? "YOU" : "JARVIS"}</div>
    <div class="history-content">${content.substring(0, 300)}${content.length > 300 ? "…" : ""}</div>
  `;
  historyList.appendChild(entry);
  historyList.scrollTop = historyList.scrollHeight;
}

// ---------------------------------------------------------------------------
// Streaming text display
// ---------------------------------------------------------------------------

function startStreaming() {
  streamingText = "";
  responseTextEl.textContent = "";
  responseTextEl.classList.add("visible", "streaming");
  responseTextEl.classList.remove("speaking");
}

function appendToken(token: string) {
  streamingText += token;
  responseTextEl.textContent = streamingText;
}

function finishStreaming(finalText?: string) {
  responseTextEl.classList.remove("streaming");
  if (finalText) {
    responseTextEl.textContent = finalText;
    responseTextEl.classList.add("speaking");
  }
}

function clearResponseText(delay = 3000) {
  setTimeout(() => {
    responseTextEl.classList.remove("visible", "streaming", "speaking");
  }, delay);
}

// ---------------------------------------------------------------------------
// Feedback bar
// ---------------------------------------------------------------------------

function showFeedback() {
  if (feedbackTimer) clearTimeout(feedbackTimer);
  feedbackBar.classList.add("visible");
  feedbackTimer = setTimeout(() => {
    feedbackBar.classList.remove("visible");
  }, 8000);
}

function hideFeedback() {
  feedbackBar.classList.remove("visible");
  if (feedbackTimer) { clearTimeout(feedbackTimer); feedbackTimer = null; }
}

// ---------------------------------------------------------------------------
// Mic level visualizer
// ---------------------------------------------------------------------------

let micAnimFrame: number | null = null;

function startMicViz(analyser: AnalyserNode) {
  micCanvas.classList.add("active");
  const ctx = micCanvas.getContext("2d")!;
  const data = new Uint8Array(analyser.frequencyBinCount);

  function draw() {
    micAnimFrame = requestAnimationFrame(draw);
    analyser.getByteFrequencyData(data);
    const avg = data.reduce((a, b) => a + b, 0) / data.length;
    const level = avg / 255;

    micCanvas.width = window.innerWidth;
    ctx.clearRect(0, 0, micCanvas.width, 3);

    const gradient = ctx.createLinearGradient(0, 0, micCanvas.width, 0);
    gradient.addColorStop(0, "transparent");
    gradient.addColorStop(0.2, `rgba(14, 165, 233, ${level * 0.6})`);
    gradient.addColorStop(0.5, `rgba(14, 165, 233, ${level * 0.9})`);
    gradient.addColorStop(0.8, `rgba(14, 165, 233, ${level * 0.6})`);
    gradient.addColorStop(1, "transparent");

    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, micCanvas.width * level, 3);
  }
  draw();
}

function stopMicViz() {
  micCanvas.classList.remove("active");
  if (micAnimFrame) { cancelAnimationFrame(micAnimFrame); micAnimFrame = null; }
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      voiceInput.setBargeInMode(false);
      if (!isMuted) voiceInput.resume();
      stopMicViz();
      clearResponseText(2000);
      setTimeout(showFeedback, 300);
      break;
    case "listening":
      voiceInput.setBargeInMode(false);
      if (!isMuted) voiceInput.resume();
      startMicViz(audioPlayer.getAnalyser());
      hideFeedback();
      clearResponseText(500);
      break;
    case "thinking":
      voiceInput.setBargeInMode(false);
      voiceInput.pause();
      stopMicViz();
      hideFeedback();
      startStreaming();
      break;
    case "speaking":
      if (!isMuted) voiceInput.setBargeInMode(true);
      else voiceInput.pause();
      stopMicViz();
      break;
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    audioPlayer.stop();
    voiceInput.setBargeInMode(false);
    addToHistory("user", text);
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  (msg: string) => { showError(msg); },
  () => {
    audioPlayer.stop();
    socket.send({ type: "barge_in" });
    transition("listening");
  },
);

audioPlayer.onFinished(() => {
  finishStreaming(lastJarvisText);
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "token") {
    // Real-time token streaming — show text before audio arrives
    appendToken((msg.text as string) || "");
  } else if (type === "audio") {
    const audioData = msg.data as string;
    if (msg.text) {
      lastJarvisText = (msg.text as string);
      finishStreaming(lastJarvisText);
      addToHistory("assistant", lastJarvisText);
    }
    if (audioData) {
      if (!started) {
        pendingAudio.push({ data: audioData, text: (msg.text as string) || "" });
        return;
      }
      if (currentState !== "speaking") transition("speaking");
      audioPlayer.enqueue(audioData);
    } else {
      transition("idle");
    }
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") transition("thinking");
    else if (state === "working") { transition("thinking"); statusEl.textContent = "working..."; }
    else if (state === "idle") transition("idle");
  } else if (type === "text") {
    lastJarvisText = (msg.text as string) || "";
    finishStreaming(lastJarvisText);
    addToHistory("assistant", lastJarvisText);
  } else if (type === "barge_in_ack") {
    audioPlayer.stop();
    transition("listening");
  } else if (type === "trigger_alert") {
    // Proactive trigger fired
    showToast((msg.message as string) || "Alert triggered, sir.");
  } else if (type === "task_spawned") {
    console.log("[task] spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task] complete:", msg.task_id, msg.status, msg.summary);
  }
});

// ---------------------------------------------------------------------------
// Keyboard shortcut — ⌘+K to toggle listening
// ---------------------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  if (e.metaKey && e.key === "k") {
    e.preventDefault();
    if (!started) return;
    shortcutHint.classList.add("active");
    setTimeout(() => shortcutHint.classList.remove("active"), 600);

    if (currentState === "listening") {
      voiceInput.pause();
      transition("idle");
    } else if (currentState === "idle") {
      transition("listening");
    } else if (currentState === "speaking") {
      audioPlayer.stop();
      socket.send({ type: "barge_in" });
      transition("listening");
    }
  }
});

// ---------------------------------------------------------------------------
// Kick off — require user click to unlock AudioContext (browser policy)
// ---------------------------------------------------------------------------

const startOverlay = document.getElementById("start-overlay")!;
let started = false;
const pendingAudio: Array<{ data: string; text: string }> = [];

function startJarvis() {
  if (started) return;
  started = true;

  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  ctx.resume().then(() => {
    for (const item of pendingAudio) {
      if (currentState !== "speaking") transition("speaking");
      audioPlayer.enqueue(item.data);
      if (item.text) { lastJarvisText = item.text; finishStreaming(item.text); addToHistory("assistant", item.text); }
    }
    pendingAudio.length = 0;
  });

  navigator.mediaDevices.getUserMedia({ audio: true })
    .then((stream) => {
      stream.getTracks().forEach(t => t.stop());
      voiceInput.start();
      transition("listening");
    })
    .catch(() => {
      voiceInput.start();
      transition("listening");
    });

  startOverlay.classList.add("hidden");
  setTimeout(() => { startOverlay.style.display = "none"; }, 700);
}

startOverlay.addEventListener("click", startJarvis, { once: true });
startOverlay.addEventListener("touchstart", startJarvis, { once: true });

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;
const btnHistory = document.getElementById("btn-history")!;
const btnHistoryClose = document.getElementById("btn-history-close")!;
const btnThumbsUp = document.getElementById("btn-thumbs-up")!;
const btnThumbsDown = document.getElementById("btn-thumbs-down")!;

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  if (isMuted) { voiceInput.pause(); transition("idle"); }
  else { voiceInput.resume(); transition("listening"); }
});

btnHistory.addEventListener("click", (e) => {
  e.stopPropagation();
  historyPanel.classList.toggle("open");
});

btnHistoryClose.addEventListener("click", () => historyPanel.classList.remove("open"));

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
  } catch { statusEl.textContent = "restart failed"; }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

btnThumbsUp.addEventListener("click", () => {
  hideFeedback();
  fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: 1, text: lastJarvisText }) }).catch(() => {});
  showToast("Noted, sir.");
});

btnThumbsDown.addEventListener("click", () => {
  hideFeedback();
  fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: -1, text: lastJarvisText }) }).catch(() => {});
  showToast("I'll do better, sir.");
});

const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

setTimeout(() => { checkFirstTimeSetup(); }, 2000);
