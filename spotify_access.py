"""
JARVIS Spotify Control — AppleScript + URI scheme.
"""

import asyncio
import logging
from urllib.parse import quote

log = logging.getLogger("jarvis.spotify")


async def _spotify_script(script: str, timeout: float = 5.0) -> tuple[str, bool]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode().strip(), proc.returncode == 0
    except Exception as e:
        log.warning(f"Spotify AppleScript failed: {e}")
        return "", False


async def _is_spotify_running() -> bool:
    out, ok = await _spotify_script(
        'tell application "System Events" to return (name of every process) contains "Spotify"'
    )
    return ok and out.strip().lower() == "true"


async def spotify_get_status() -> str:
    """Returns 'playing', 'paused', or 'stopped'."""
    out, ok = await _spotify_script('tell application "Spotify" to return player state as string')
    return out.lower() if ok else "stopped"


async def spotify_current_track() -> str:
    """Returns 'Song Name by Artist'."""
    out, ok = await _spotify_script(
        'tell application "Spotify" to return name of current track & " by " & artist of current track'
    )
    return out if (ok and out) else "unknown track"


async def spotify_play_pause() -> str:
    out, ok = await _spotify_script('tell application "Spotify" to playpause')
    if not ok:
        return "Spotify doesn't appear to be running, sir."
    await asyncio.sleep(0.3)
    status = await spotify_get_status()
    return f"{'Playing' if status == 'playing' else 'Paused'}, sir."


async def spotify_skip() -> str:
    _, ok = await _spotify_script('tell application "Spotify" to next track')
    if not ok:
        return "Couldn't skip, sir."
    await asyncio.sleep(0.5)
    track = await spotify_current_track()
    return f"Skipped. Now playing {track}, sir."


async def spotify_prev() -> str:
    _, ok = await _spotify_script('tell application "Spotify" to previous track')
    if not ok:
        return "Couldn't go back, sir."
    await asyncio.sleep(0.5)
    track = await spotify_current_track()
    return f"Now playing {track}, sir."


async def spotify_volume(level: int) -> str:
    level = max(0, min(100, level))
    _, ok = await _spotify_script(f'tell application "Spotify" to set sound volume to {level}')
    return f"Volume set to {level}, sir." if ok else "Couldn't adjust Spotify volume, sir."


async def spotify_volume_up() -> str:
    out, ok = await _spotify_script('tell application "Spotify" to return sound volume')
    try:
        current = int(out)
        return await spotify_volume(min(100, current + 20))
    except Exception:
        return await spotify_volume(80)


async def spotify_volume_down() -> str:
    out, ok = await _spotify_script('tell application "Spotify" to return sound volume')
    try:
        current = int(out)
        return await spotify_volume(max(0, current - 20))
    except Exception:
        return await spotify_volume(40)


async def spotify_play_search(query: str) -> str:
    """Open Spotify search URI so the app searches and plays the query."""
    from actions import open_browser
    encoded = quote(query)
    await open_browser(f"spotify:search:{encoded}")
    return f"Searching Spotify for {query}, sir."


async def spotify_play_uri(uri: str) -> str:
    """Play a specific Spotify URI (track/album/playlist)."""
    escaped = uri.replace('"', '\\"')
    _, ok = await _spotify_script(f'tell application "Spotify" to play track "{escaped}"')
    return "Playing now, sir." if ok else "Couldn't play that, sir."


async def get_spotify_info() -> str:
    """Voice-friendly playback status."""
    status = await spotify_get_status()
    if status == "stopped":
        return "Spotify isn't running, sir."
    track = await spotify_current_track()
    if status == "playing":
        return f"Playing {track}, sir."
    return f"Spotify is paused on {track}, sir."


async def handle_spotify_command(command: str) -> str:
    """Dispatch a Spotify command string from [ACTION:SPOTIFY].

    command formats:
      play/pause  — toggle
      skip / next — skip track
      back / prev — previous track
      volume:N    — set volume to N (0–100)
      volume up   — increase
      volume down — decrease
      what's playing — current track
      search:QUERY — search and play
      play:URI    — play Spotify URI
    """
    cmd = command.lower().strip()

    if cmd in ("play", "pause", "play/pause", "toggle"):
        return await spotify_play_pause()
    elif cmd in ("skip", "next", "next track"):
        return await spotify_skip()
    elif cmd in ("back", "prev", "previous", "previous track"):
        return await spotify_prev()
    elif cmd in ("volume up", "louder", "turn up"):
        return await spotify_volume_up()
    elif cmd in ("volume down", "quieter", "turn down"):
        return await spotify_volume_down()
    elif cmd.startswith("volume:"):
        try:
            level = int(cmd.split(":", 1)[1].strip())
            return await spotify_volume(level)
        except ValueError:
            return "Didn't catch the volume level, sir."
    elif cmd in ("what's playing", "whats playing", "current", "now playing", "status"):
        return await get_spotify_info()
    elif cmd.startswith("search:"):
        query = command.split(":", 1)[1].strip()
        return await spotify_play_search(query)
    elif cmd.startswith("play:"):
        uri = command.split(":", 1)[1].strip()
        return await spotify_play_uri(uri)
    else:
        # Treat anything else as a search query
        return await spotify_play_search(command)
