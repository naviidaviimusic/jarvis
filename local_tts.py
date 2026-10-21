"""local_tts.py — offline neural TTS for JARVIS (Piper, en_GB-alan-medium).

Sits between edge-tts (needs internet) and macOS `say` (robotic) in every
speech path, so JARVIS keeps a natural British voice with no network at all.

    synth_wav(text) -> bytes | None   WAV for the browser audio player
    speak(text)                       play on the Mac speakers (non-blocking),
                                      falls back to `say -v Daniel`

Piper runs on onnxruntime (no torch → no libiomp clash with faster_whisper).
The voice loads once per process (~1.5 s), then ~1 s per sentence on this CPU.
JARVIS_PIPER=0 disables it.
"""

import io
import logging
import os
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

log = logging.getLogger("jarvis.local_tts")

VOICE = Path(os.environ.get(
    "JARVIS_PIPER_VOICE",
    Path.home() / "Documents" / "AI-Advisers" / ".voices" / "en_GB-alan-medium.onnx"))
MAX_CHARS = 400

_voice = None
_lock = threading.Lock()
_failed = False


def _load():
    global _voice, _failed
    if _voice is not None or _failed:
        return _voice
    if os.environ.get("JARVIS_PIPER") == "0" or not VOICE.exists():
        _failed = True
        return None
    try:
        from piper import PiperVoice
        _voice = PiperVoice.load(str(VOICE))
    except Exception as exc:
        log.warning("piper unavailable: %s", exc)
        _failed = True
    return _voice


def synth_wav(text: str):
    """Return WAV bytes for text, or None if Piper is unavailable."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return None
    with _lock:                       # PiperVoice isn't documented as thread-safe
        voice = _load()
        if voice is None:
            return None
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(text, wf)
            return buf.getvalue() or None
        except Exception as exc:
            log.warning("piper synth error: %s", exc)
            return None


def _play(text: str, say_voice: str):
    audio = synth_wav(text)
    if audio:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis_tts_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(audio)
            subprocess.run(["afplay", path], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        finally:
            Path(path).unlink(missing_ok=True)
    subprocess.run(["say", "-v", say_voice, "-r", "180", text[:300]], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(text: str, say_voice: str = "Daniel") -> None:
    """Speak on the Mac speakers without blocking the caller."""
    if text and text.strip():
        threading.Thread(target=_play, args=(text, say_voice), daemon=True).start()


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "Good evening, sir. Offline voice is operational."
    _play(msg, "Daniel")
