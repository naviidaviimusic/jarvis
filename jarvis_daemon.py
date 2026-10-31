#!/usr/local/bin/python3.11
"""
JARVIS Wake Word Daemon
─────────────────────────
Always-on background listener. Captures mic with pyaudio, runs VAD,
transcribes with faster-whisper, detects "jarvis" wake word,
POSTs transcript to https://localhost:8340/api/transcript (self-signed local cert).
Plays TTS response via afplay (server returns audio file path or base64).
"""

import base64
import logging
import os
import subprocess
import time
from pathlib import Path

import sys

# torch 2.2.2 (installed 2026-10-09 for doc tools) is NumPy-1 built and ships its own
# libiomp5 — if ctranslate2 imports it, OpenMP aborts the daemon (SIGABRT crash loop).
# faster-whisper doesn't need torch, so hide it from this process only.
sys.modules["torch"] = None

import numpy as np
import pyaudio
import requests
from requests.adapters import HTTPAdapter
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # localhost self-signed cert
from faster_whisper import WhisperModel

# ─── Paths ────────────────────────────────────────────────────────────────────
LOG_DIR = (Path.home() / "jarvis/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "daemon.log"

TMP_AUDIO_IN = "/tmp/jarvis_daemon_input.wav"
TMP_AUDIO_RESP = "/tmp/jarvis_daemon_resp.mp3"

# ─── Audio constants ───────────────────────────────────────────────────────────
CHUNK = 1024
RATE = 16000
CHANNELS = 1
FORMAT = pyaudio.paInt16

# ─── VAD / recording tuning ───────────────────────────────────────────────────
ENERGY_THRESHOLD = 500          # RMS threshold to consider chunk as "voice"
SPEECH_ONSET_CHUNKS = 3         # consecutive above-threshold chunks to start recording
SILENCE_DURATION_S = 1.5        # seconds of silence to end recording
SILENCE_CHUNKS = int(SILENCE_DURATION_S * RATE / CHUNK)  # ~23 chunks
MAX_RECORD_S = 15.0             # hard cap — a voice command never needs longer;
MAX_RECORD_CHUNKS = int(MAX_RECORD_S * RATE / CHUNK)  # stops music/noise runaways

# ─── Behaviour ────────────────────────────────────────────────────────────────
SERVER_URL = os.environ.get("JARVIS_SERVER_URL", "https://localhost:8340/api/transcript")
SERVER_CERT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert.pem")
POST_TIMEOUT_S = 120            # headless replies come from the local CPU brain
POST_RETRIES = 3
POST_RETRY_DELAY_S = 1.0
COOLDOWN_S = 2.0                # silence after playback to prevent echo

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DAEMON] %(levelname)s %(message)s",
    # LaunchAgent already routes stdout/stderr into LOG_FILE, so only add a
    # console handler when run interactively — otherwise every line doubles.
    handlers=[logging.FileHandler(LOG_FILE)]
             + ([logging.StreamHandler()] if sys.stderr.isatty() else []),
)
log = logging.getLogger("jarvis_daemon")


# ─── Model (loaded once at startup) ───────────────────────────────────────────
def load_model() -> WhisperModel:
    log.info("Loading Whisper tiny model …")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    log.info("Whisper model ready.")
    return model


# ─── RMS energy ───────────────────────────────────────────────────────────────
def rms(chunk_bytes: bytes) -> float:
    arr = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr ** 2)))


# ─── Transcription ────────────────────────────────────────────────────────────
def transcribe(model: WhisperModel, audio_bytes: bytes) -> str:
    """Convert raw PCM bytes → float32 array → transcription string."""
    audio_arr = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if audio_arr.size == 0:
        return ""
    try:
        segments, _info = model.transcribe(audio_arr, beam_size=1,
                                          language="en", vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        return text
    except Exception as exc:
        log.warning("Transcription error: %s", exc)
        return ""


# ─── Wake-word stripping ──────────────────────────────────────────────────────
_WAKE_PREFIXES = ("hey jarvis", "jarvis")

def extract_command(transcript: str) -> str | None:
    """
    Returns the command text if the wake word was detected, else None.
    Strips the wake-word prefix before returning.
    """
    lower = transcript.lower().strip()
    if "jarvis" not in lower:
        return None
    for prefix in _WAKE_PREFIXES:
        if lower.startswith(prefix):
            cmd = transcript[len(prefix):].strip().lstrip(",").lstrip(":").strip()
            return cmd
    # Wake word found somewhere in the middle — return the full transcript cleaned
    return transcript.strip()


# ─── Server POST ──────────────────────────────────────────────────────────────
class _LocalCertAdapter(HTTPAdapter):
    """Trust only JARVIS's own certificate; it predates hostname entries, so skip that one check."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_hostname"] = False
        return super().init_poolmanager(*args, **kwargs)


_server_session = requests.Session()
_server_session.mount("https://localhost:", _LocalCertAdapter())


def post_transcript(text: str) -> dict | None:
    payload = {"text": text, "source": "daemon"}
    for attempt in range(1, POST_RETRIES + 1):
        try:
            resp = _server_session.post(SERVER_URL, json=payload, timeout=POST_TIMEOUT_S, verify=SERVER_CERT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ReadTimeout:
            log.warning("Server took >%ss to answer — not resending the command", POST_TIMEOUT_S)
            return None
        except requests.exceptions.ConnectionError:
            log.warning("Server down (attempt %d/%d) — retrying in %ss",
                        attempt, POST_RETRIES, POST_RETRY_DELAY_S)
            if attempt < POST_RETRIES:
                time.sleep(POST_RETRY_DELAY_S)
        except Exception as exc:
            log.warning("POST error (attempt %d/%d): %s", attempt, POST_RETRIES, exc)
            if attempt < POST_RETRIES:
                time.sleep(POST_RETRY_DELAY_S)
    return None


# ─── Playback ─────────────────────────────────────────────────────────────────
def play_response(data: dict) -> None:
    """Play audio returned by the server (path or base64)."""
    if not data:
        return

    audio_path = data.get("audio_path")
    audio_b64 = data.get("audio_b64")

    if audio_path:
        _afplay(audio_path)
    elif audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            with open(TMP_AUDIO_RESP, "wb") as fh:
                fh.write(audio_bytes)
            _afplay(TMP_AUDIO_RESP)
        except Exception as exc:
            log.warning("Failed to play base64 audio: %s", exc)
        finally:
            try:
                os.remove(TMP_AUDIO_RESP)
            except OSError:
                pass


def _afplay(path: str) -> None:
    try:
        subprocess.run(["afplay", path], check=True)
    except subprocess.CalledProcessError as exc:
        log.warning("afplay error: %s", exc)
    except FileNotFoundError:
        log.warning("afplay not found — skipping playback")


# ─── Main listen loop ─────────────────────────────────────────────────────────
def listen_loop(model: WhisperModel) -> None:
    pa = pyaudio.PyAudio()

    try:
        stream = pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
    except pyaudio.PyAudioError as exc:
        log.error("Could not open audio stream: %s", exc)
        pa.terminate()
        return

    log.info("Listening for wake word …")

    try:
        while True:
            # ── Phase 1: detect speech onset ──────────────────────────────────
            above_count = 0
            onset_buffer: list[bytes] = []

            while above_count < SPEECH_ONSET_CHUNKS:
                try:
                    chunk = stream.read(CHUNK, exception_on_overflow=False)
                except Exception as exc:
                    log.warning("Stream read error: %s", exc)
                    chunk = b"\x00" * CHUNK * 2

                onset_buffer.append(chunk)
                if len(onset_buffer) > SPEECH_ONSET_CHUNKS * 2:
                    onset_buffer.pop(0)  # keep a small pre-roll

                energy = rms(chunk)
                if energy >= ENERGY_THRESHOLD:
                    above_count += 1
                else:
                    above_count = 0

            # ── Phase 2: record until silence ─────────────────────────────────
            log.debug("Speech onset detected — recording …")
            recorded = list(onset_buffer)  # include pre-roll
            silence_count = 0

            while silence_count < SILENCE_CHUNKS and len(recorded) < MAX_RECORD_CHUNKS:
                try:
                    chunk = stream.read(CHUNK, exception_on_overflow=False)
                except Exception as exc:
                    log.warning("Stream read error: %s", exc)
                    chunk = b"\x00" * CHUNK * 2

                recorded.append(chunk)
                energy = rms(chunk)
                if energy < ENERGY_THRESHOLD:
                    silence_count += 1
                else:
                    silence_count = 0  # voice resumed — reset silence counter

            audio_bytes = b"".join(recorded)
            log.debug("Recorded %d bytes (%.1fs)", len(audio_bytes),
                      len(audio_bytes) / (RATE * 2))

            # ── Phase 3: transcribe ───────────────────────────────────────────
            transcript = transcribe(model, audio_bytes)
            if not transcript:
                continue
            log.info("Transcript: %r", transcript)

            # ── Phase 4: wake word check ──────────────────────────────────────
            command = extract_command(transcript)
            if command is None:
                log.debug("No wake word — discarding.")
                continue

            log.info("Wake word detected. Command: %r", command)

            # ── Phase 5: POST to server ───────────────────────────────────────
            response_data = post_transcript(command)
            if response_data is None:
                log.warning("No response from server — skipping playback.")
            else:
                play_response(response_data)
                log.debug("Playback done. Cooling down for %ss …", COOLDOWN_S)
                time.sleep(COOLDOWN_S)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down.")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()
        log.info("Audio stream closed.")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("JARVIS Wake Word Daemon starting …")

    # Startup chime
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"])

    model = load_model()

    listen_loop(model)

    log.info("Daemon exited.")
