#!/usr/local/bin/python3.11
"""
JARVIS File Watcher
─────────────────────────
Monitors ~/Downloads and ~/Desktop for new files.
When a new file appears, classifies it and sends a proactive alert via WebSocket.
"""

import asyncio
import logging
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

log = logging.getLogger("jarvis.file_watcher")

WATCH_DIRS = [Path.home() / "Downloads", Path.home() / "Desktop"]

# Temp/partial-download extensions to ignore
_TEMP_SUFFIXES = {".crdownload", ".part", ".tmp"}

# File type → proactive message template
FILE_MESSAGES: dict[str, str] = {
    ".pdf":  "New PDF arrived: {name}. Want me to summarize it, sir?",
    ".py":   "New Python file: {name}. Shall I open it in the editor, sir?",
    ".js":   "New JavaScript file: {name}. Want me to review it, sir?",
    ".ts":   "New TypeScript file: {name}. Want me to review it, sir?",
    ".zip":  "New archive downloaded: {name}. Want me to extract it, sir?",
    ".csv":  "New data file: {name}. Want me to analyze it, sir?",
    ".mp4":  "New video: {name}.",
    ".mp3":  "New audio file: {name}.",
    ".png":  "New image: {name}.",
    ".jpg":  "New image: {name}.",
    ".docx": "New document: {name}. Want me to summarize it, sir?",
    ".xlsx": "New spreadsheet: {name}. Want me to analyze it, sir?",
}
DEFAULT_MESSAGE = "New file: {name}."

# Queue populated by the watchdog thread, drained by the async loop
_new_files: asyncio.Queue  # set by start_watcher()


class JarvisFileHandler(FileSystemEventHandler):
    """Watchdog event handler that queues newly created files."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        super().__init__()
        self._loop = loop
        self._queue = queue
        self._seen: set[str] = set()

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return

        path = Path(event.src_path)
        name = path.name

        # Skip hidden files
        if name.startswith("."):
            return

        # Skip partial/temporary download files
        if path.suffix.lower() in _TEMP_SUFFIXES:
            return

        str_path = str(path)

        # Debounce: skip if already seen
        if str_path in self._seen:
            return
        self._seen.add(str_path)

        ext = path.suffix.lower()
        template = FILE_MESSAGES.get(ext, DEFAULT_MESSAGE)
        message = template.format(name=name)

        log.debug("File watcher queuing: %s", str_path)
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, (str_path, message)
        )


async def start_watcher(ws, synthesize_fn) -> None:
    """
    Start the file system watcher and proactively alert via WebSocket.

    Parameters
    ----------
    ws           : WebSocket — FastAPI WebSocket connection to the browser client.
    synthesize_fn: Callable[[str], Awaitable[bytes]] — async TTS function that
                   accepts text and returns audio bytes (or None on error).
    """
    global _new_files
    _new_files = asyncio.Queue()

    loop = asyncio.get_event_loop()
    handler = JarvisFileHandler(loop, _new_files)

    observer = Observer()
    for watch_dir in WATCH_DIRS:
        if watch_dir.exists():
            observer.schedule(handler, str(watch_dir), recursive=False)
            log.info("File watcher monitoring: %s", watch_dir)
        else:
            log.warning("Watch directory does not exist, skipping: %s", watch_dir)

    observer.start()
    log.info("File watcher started.")

    last_alert_time: float = 0.0
    cooldown_seconds = 10.0

    try:
        while True:
            # ── Drain the queue with a short timeout so CancelledError is checked ──
            try:
                path_str, message = await asyncio.wait_for(
                    _new_files.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            # Wait 2 seconds to let the download/copy finish
            await asyncio.sleep(2)

            # Verify the file still exists and is non-empty
            path = Path(path_str)
            try:
                if not path.exists() or path.stat().st_size == 0:
                    log.debug("File gone or empty, skipping alert: %s", path_str)
                    continue
            except OSError:
                continue

            # Enforce per-file uniqueness and global cooldown
            now = time.monotonic()
            if now - last_alert_time < cooldown_seconds:
                log.debug(
                    "Alert cooldown active (%.1fs remaining), skipping: %s",
                    cooldown_seconds - (now - last_alert_time),
                    path_str,
                )
                continue

            last_alert_time = now

            log.info("File watcher alert: %s", message)

            try:
                # Signal the frontend that JARVIS is about to speak
                await ws.send_json({"type": "status", "state": "speaking"})

                # Synthesise speech and stream audio back to the browser
                audio = await synthesize_fn(message)
                if audio:
                    await ws.send_bytes(audio)
            except Exception as e:
                log.warning("Failed to send file watcher alert: %s", e)

    except asyncio.CancelledError:
        log.info("File watcher task cancelled — stopping observer.")
    finally:
        observer.stop()
        observer.join()
        log.info("File watcher stopped.")
