"""
JARVIS Clipboard — system clipboard via pbcopy/pbpaste.
"""

import asyncio
import logging

log = logging.getLogger("jarvis.clipboard")


async def read_clipboard() -> str:
    """Read current clipboard contents as text."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbpaste",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception as e:
        log.warning(f"read_clipboard failed: {e}")
        return ""


async def write_clipboard(text: str) -> bool:
    """Write text to the system clipboard."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(
            proc.communicate(input=text.encode("utf-8")),
            timeout=3,
        )
        return proc.returncode == 0
    except Exception as e:
        log.warning(f"write_clipboard failed: {e}")
        return False


def format_clipboard_for_voice(content: str, max_chars: int = 200) -> str:
    if not content:
        return "The clipboard is empty, sir."
    truncated = content[:max_chars].replace("\n", " ").strip()
    if len(content) > max_chars:
        truncated += f"... ({len(content)} characters total)"
    return f"Clipboard contains: {truncated}"
