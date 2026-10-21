#!/usr/local/bin/python3.11
"""
JARVIS Notification Bridge
─────────────────────────
Sends macOS notifications and speaks via system audio when the browser tab is closed.
Used by proactive_loop as fallback when no WebSocket clients are connected.
"""

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.notification_bridge")

# ─── ws_clients state file ────────────────────────────────────────────────────
_WS_CLIENTS_FILE = (Path.home() / "jarvis/data/ws_clients.json")

# ─── Markdown / tag stripping ─────────────────────────────────────────────────
_MARKDOWN_RE = re.compile(
    r"""
    \*\*[^*]*\*\*       # **bold**
    | \*[^*]*\*          # *italic*
    | \[ACTION:[^\]]*\]  # [ACTION:...] tags
    | `[^`]*`            # `inline code`
    | ^#{1,6}\s+         # # headers at line start
    """,
    re.VERBOSE | re.MULTILINE,
)


def strip_for_tts(text: str) -> str:
    """Remove markdown, action tags, excess whitespace for clean TTS."""
    try:
        cleaned = _MARKDOWN_RE.sub("", text)
        # Collapse multiple spaces / newlines into single spaces
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned
    except Exception as exc:
        log.warning("strip_for_tts error: %s", exc)
        return text


def notify(title: str, message: str, sound: str = "Glass") -> None:
    """Send macOS Notification Center alert via osascript."""
    try:
        truncated = message[:200]
        # Escape double-quotes in strings passed to AppleScript
        safe_title = title.replace('"', '\\"')
        safe_msg = truncated.replace('"', '\\"')
        safe_sound = sound.replace('"', '\\"')
        script = (
            f'display notification "{safe_msg}" '
            f'with title "{safe_title}" '
            f'sound name "{safe_sound}"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
        )
    except Exception as exc:
        log.warning("notify error: %s", exc)


def speak_system(text: str, voice: str = "Daniel") -> None:
    """Speak text offline: Piper neural voice, macOS `say` as last resort.

    Uses 'say -v Daniel -r 180 "text"' — Daniel is British voice, rate 180 wpm.
    Runs in background (non-blocking) via subprocess.Popen.
    Strips markdown, truncates to 300 chars.
    """
    try:
        clean = strip_for_tts(text)[:300]
        if not clean:
            return
        from local_tts import speak
        speak(clean, say_voice=voice)   # Piper offline voice, `say` if unavailable
    except Exception as exc:
        log.warning("speak_system error: %s", exc)


async def speak_and_notify(text: str, title: str = "JARVIS") -> None:
    """Combined: send notification + speak via system TTS.

    Used when Fish Audio TTS is unavailable or browser is closed.
    """
    try:
        loop = asyncio.get_event_loop()
        # Run both synchronous calls in the default thread pool so we don't
        # block the event loop (notify is fast; speak_system is non-blocking
        # due to Popen, but we wrap for consistency).
        await loop.run_in_executor(None, notify, title, text)
        await loop.run_in_executor(None, speak_system, text)
    except Exception as exc:
        log.warning("speak_and_notify error: %s", exc)


def is_browser_session_active() -> bool:
    """Check if any JARVIS browser tab is currently connected.

    Reads ~/jarvis/data/ws_clients.json — server writes this file
    with count of active WebSocket connections. Returns False if file missing.
    """
    try:
        if not _WS_CLIENTS_FILE.exists():
            return False
        raw = _WS_CLIENTS_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return False
        data = json.loads(raw)
        count = data.get("count", 0)
        return int(count) > 0
    except Exception as exc:
        log.warning("is_browser_session_active error: %s", exc)
        return False
