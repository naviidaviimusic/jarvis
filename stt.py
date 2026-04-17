"""
Speech-to-text using local Whisper via faster-whisper.
Captures from mic, transcribes locally — no Google servers needed.
"""
import asyncio
import io
import logging
import numpy as np
import pyaudio
import queue
import threading
import wave
from typing import Callable, Optional

log = logging.getLogger("jarvis.stt")

SAMPLE_RATE   = 16000
CHANNELS      = 1
CHUNK         = 1024
SILENCE_LIMIT = 1.5   # seconds of silence before sending transcript
MIN_SPEECH    = 0.4   # minimum seconds of speech to process

_model = None

def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper tiny model...")
        _model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        log.info("Whisper model loaded")
    return _model


class MicListener:
    """
    Captures mic audio, detects speech, transcribes with Whisper.
    Calls on_transcript(text) for each final utterance.
    """

    def __init__(self, on_transcript: Callable[[str], None]):
        self.on_transcript = on_transcript
        self._running      = False
        self._paused       = False
        self._thread: Optional[threading.Thread] = None
        self._audio_q: queue.Queue = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._running:
            return
        self._running = True
        self._loop = loop
        # Load model in background so server startup isn't blocked
        threading.Thread(target=_load_model, daemon=True).start()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        log.info("Mic listener started")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False

    def _capture_loop(self):
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
        log.info("Microphone open")

        speech_frames = []
        silent_chunks  = 0
        speaking       = False
        silent_limit   = int(SILENCE_LIMIT * SAMPLE_RATE / CHUNK)
        min_frames     = int(MIN_SPEECH * SAMPLE_RATE / CHUNK)

        try:
            while self._running:
                data = stream.read(CHUNK, exception_on_overflow=False)

                if self._paused:
                    speech_frames = []
                    silent_chunks = 0
                    speaking = False
                    continue

                # Energy-based VAD
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2)))
                is_speech = rms > 300  # threshold — adjust if too sensitive

                if is_speech:
                    speech_frames.append(data)
                    silent_chunks = 0
                    speaking = True
                elif speaking:
                    speech_frames.append(data)
                    silent_chunks += 1
                    if silent_chunks >= silent_limit:
                        # End of utterance — transcribe
                        if len(speech_frames) >= min_frames:
                            self._transcribe(speech_frames[:])
                        speech_frames = []
                        silent_chunks = 0
                        speaking = False
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

    def _transcribe(self, frames: list):
        try:
            model = _load_model()
            # Build WAV in memory
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(b"".join(frames))
            buf.seek(0)

            # Transcribe
            segments, _ = model.transcribe(buf, language="en", vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()

            if text and len(text) > 1:
                log.info(f"Transcript: {text}")
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._dispatch(text), self._loop
                    )
        except Exception as e:
            log.error(f"Transcription error: {e}")

    async def _dispatch(self, text: str):
        try:
            self.on_transcript(text)
        except Exception as e:
            log.error(f"Transcript dispatch error: {e}")
