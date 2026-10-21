#!/usr/local/bin/python3.11
"""
JARVIS System Control — macOS integration
─────────────────────────────────────────
Controls volume, brightness, apps, system state via AppleScript + shell.
All functions return voice-friendly strings, never raise.
"""

import logging
import subprocess

import psutil

log = logging.getLogger("jarvis.system_control")

# ── Helpers ──────────────────────────────────────────────────────────────────

APP_ALIASES: dict[str, str] = {
    "chrome":   "Google Chrome",
    "google chrome": "Google Chrome",
    "vscode":   "Visual Studio Code",
    "vs code":  "Visual Studio Code",
    "code":     "Visual Studio Code",
    "terminal": "Terminal",
    "slack":    "Slack",
    "spotify":  "Spotify",
    "finder":   "Finder",
    "mail":     "Mail",
    "calendar": "Calendar",
    "notes":    "Notes",
    "safari":   "Safari",
    "messages": "Messages",
    "facetime": "FaceTime",
    "photos":   "Photos",
    "music":    "Music",
    "xcode":    "Xcode",
    "iterm":    "iTerm2",
    "iterm2":   "iTerm2",
    "zoom":     "zoom.us",
    "discord":  "Discord",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "1password": "1Password 7 - Password Manager",
    "notion":   "Notion",
    "figma":    "Figma",
    "postman":  "Postman",
}


def _run_osascript(script: str, timeout: float = 5.0) -> tuple[str, bool]:
    """Run an AppleScript snippet. Returns (stdout, success)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.returncode == 0
    except Exception as e:
        log.warning("osascript error: %s", e)
        return "", False


def _resolve_app_name(app_name: str) -> str:
    """Resolve common aliases to the real application name."""
    return APP_ALIASES.get(app_name.lower().strip(), app_name)


# ── Volume ────────────────────────────────────────────────────────────────────

def set_volume(level: int) -> str:
    """Set system output volume 0-100 via osascript."""
    level = max(0, min(100, level))
    _, ok = _run_osascript(f"set volume output volume {level}")
    if ok:
        return f"Volume set to {level}, sir."
    return "I couldn't adjust the volume, sir."


def get_volume() -> str:
    """Get current system output volume."""
    out, ok = _run_osascript("output volume of (get volume settings)")
    if ok and out:
        return f"Volume is currently at {out}, sir."
    return "I couldn't read the current volume, sir."


def mute() -> str:
    """Mute system audio output."""
    _, ok = _run_osascript("set volume with output muted")
    if ok:
        return "Muted, sir."
    return "I couldn't mute the audio, sir."


def unmute() -> str:
    """Unmute system audio output."""
    _, ok = _run_osascript("set volume without output muted")
    if ok:
        return "Unmuted, sir."
    return "I couldn't unmute the audio, sir."


# ── Brightness ────────────────────────────────────────────────────────────────

def set_brightness(level: int) -> str:
    """Set display brightness 0-100 using the brightness CLI tool."""
    level = max(0, min(100, level))
    value = level / 100.0
    try:
        result = subprocess.run(
            ["brightness", str(value)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return f"Brightness set to {level}%, sir."
        log.warning("brightness CLI returned %d: %s", result.returncode, result.stderr)
        return "I couldn't set the brightness. The brightness tool may not be installed, sir."
    except FileNotFoundError:
        return "Brightness tool not found. Install it with 'brew install brightness', sir."
    except Exception as e:
        log.warning("set_brightness error: %s", e)
        return "I encountered an error setting brightness, sir."


# ── Apps ──────────────────────────────────────────────────────────────────────

def launch_app(app_name: str) -> str:
    """Launch an application by name or common alias."""
    resolved = _resolve_app_name(app_name)
    try:
        result = subprocess.run(
            ["open", "-a", resolved],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            display = resolved if resolved.lower() != app_name.lower() else app_name
            return f"Opening {display}, sir."
        log.warning("open -a '%s' failed: %s", resolved, result.stderr)
        return f"I couldn't find an application called {app_name}, sir."
    except Exception as e:
        log.warning("launch_app error: %s", e)
        return f"Failed to open {app_name}, sir."


def quit_app(app_name: str) -> str:
    """Quit an application gracefully via AppleScript."""
    resolved = _resolve_app_name(app_name)
    script = f'tell application "{resolved}" to quit'
    _, ok = _run_osascript(script)
    if ok:
        return f"Closing {resolved}, sir."
    return f"I couldn't quit {resolved}, sir. It may not be running."


# ── System Stats ──────────────────────────────────────────────────────────────

def get_system_stats() -> str:
    """Return a voice-friendly summary of CPU, RAM, disk, and top processes."""
    try:
        cpu_pct = psutil.cpu_percent(interval=1)

        vm = psutil.virtual_memory()
        ram_used_gb = vm.used / (1024 ** 3)
        ram_total_gb = vm.total / (1024 ** 3)

        disk = psutil.disk_usage("/")
        disk_pct = disk.percent

        # Top 3 CPU-consuming processes
        procs: list[tuple[float, str]] = []
        for proc in psutil.process_iter(["name", "cpu_percent"]):
            try:
                cpu = proc.info["cpu_percent"] or 0.0
                name = proc.info["name"] or "unknown"
                procs.append((cpu, name))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(reverse=True)
        top3 = procs[:3]

        top_str = ", ".join(
            f"{name} {cpu:.0f}%" for cpu, name in top3
        ) if top3 else "none"

        return (
            f"CPU at {cpu_pct:.0f}%, "
            f"{ram_used_gb:.0f} of {ram_total_gb:.0f} GB RAM used, "
            f"disk {disk_pct:.0f}% full. "
            f"Top processes: {top_str}."
        )
    except Exception as e:
        log.warning("get_system_stats error: %s", e)
        return "I couldn't retrieve system statistics, sir."


# ── Screen & Power ────────────────────────────────────────────────────────────

def lock_screen() -> str:
    """Lock the screen by putting the display to sleep immediately."""
    try:
        result = subprocess.run(
            ["pmset", "displaysleepnow"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "Screen locked, sir."
        # Fallback via osascript screen saver
        _, ok = _run_osascript(
            'tell application "System Events" to start current screen saver'
        )
        if ok:
            return "Screen saver activated, sir."
        return "I couldn't lock the screen, sir."
    except Exception as e:
        log.warning("lock_screen error: %s", e)
        return "I couldn't lock the screen, sir."


def get_battery_status() -> str:
    """Return battery percentage and charging status."""
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return "No battery detected. Running on mains power, sir."
        pct = int(battery.percent)
        if battery.power_plugged:
            status = "charging" if pct < 100 else "fully charged"
            return f"Battery at {pct}%, {status}, sir."
        return f"Battery at {pct}%, not charging, sir."
    except Exception as e:
        log.warning("get_battery_status error: %s", e)
        return "I couldn't read the battery status, sir."


# ── Network ───────────────────────────────────────────────────────────────────

def get_wifi_info() -> str:
    """Return the current WiFi SSID."""
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    try:
        result = subprocess.run(
            [airport, "-I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("SSID:"):
                    ssid = stripped.split(":", 1)[1].strip()
                    if ssid:
                        return f"Connected to {ssid}, sir."
            return "WiFi is connected but the network name couldn't be determined, sir."
        return "Not connected to WiFi, sir."
    except FileNotFoundError:
        return "The airport utility wasn't found on this system, sir."
    except Exception as e:
        log.warning("get_wifi_info error: %s", e)
        return "I couldn't retrieve WiFi information, sir."


# ── Focus / Do Not Disturb ────────────────────────────────────────────────────

def set_do_not_disturb(enabled: bool) -> str:
    """Enable or disable Focus (Do Not Disturb) via defaults."""
    value = "true" if enabled else "false"
    try:
        result = subprocess.run(
            [
                "defaults", "write",
                "com.apple.notificationcenterui",
                "doNotDisturb", "-bool", value,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            state = "enabled" if enabled else "disabled"
            return f"Do Not Disturb {state}, sir."
        # Fallback: keyboard shortcut via AppleScript
        script = (
            'tell application "System Events" to key code 64 '
            'using {command down, option down}'
        )
        _, ok = _run_osascript(script)
        if ok:
            state = "toggled"
            return f"Focus mode {state}, sir."
        return "I couldn't change the Focus setting, sir."
    except Exception as e:
        log.warning("set_do_not_disturb error: %s", e)
        return "I couldn't change the Do Not Disturb setting, sir."


# ── Messaging ─────────────────────────────────────────────────────────────────

def send_imessage(contact: str, message: str) -> str:
    """Send an iMessage to a contact via AppleScript."""
    message = message[:500]
    # Escape double-quotes in both strings for AppleScript
    safe_contact = contact.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = (
        f'tell application "Messages" to send "{safe_message}" '
        f'to buddy "{safe_contact}" of service "iMessage"'
    )
    _, ok = _run_osascript(script, timeout=10)
    if ok:
        return f"Message sent to {contact}, sir."
    return f"I couldn't send the message to {contact}, sir. They may not be an iMessage contact."


# ── Maintenance ───────────────────────────────────────────────────────────────

def empty_trash() -> str:
    """Empty the macOS Trash via AppleScript."""
    _, ok = _run_osascript('tell application "Finder" to empty trash')
    if ok:
        return "Trash emptied, sir."
    return "I couldn't empty the trash, sir."
