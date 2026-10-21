"""
JARVIS Reminders — Apple Reminders via AppleScript.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("jarvis.reminders")


async def get_reminders(list_name: Optional[str] = None, count: int = 15) -> list[dict]:
    """Get incomplete reminders. Optionally filter by list name."""
    if list_name:
        escaped = list_name.replace('"', '\\"')
        script = f'''
tell application "Reminders"
    set output to ""
    try
        set r to (every reminder of list "{escaped}" whose completed is false)
        repeat with item in r
            set n to name of item
            set d to ""
            try
                set d to due date of item as string
            end try
            set output to output & n & "|||" & d & linefeed
        end repeat
    end try
    return output
end tell'''
    else:
        script = '''
tell application "Reminders"
    set output to ""
    repeat with l in lists
        try
            set r to (every reminder of l whose completed is false)
            repeat with item in r
                set n to name of item
                set listName to name of l
                set d to ""
                try
                    set d to due date of item as string
                end try
                set output to output & n & "|||" & d & "|||" & listName & linefeed
            end repeat
        end try
    end repeat
    return output
end tell'''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if proc.returncode != 0:
            return []

        reminders = []
        for line in stdout.decode().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|||")
            reminders.append({
                "title": parts[0].strip() if len(parts) > 0 else "",
                "due": parts[1].strip() if len(parts) > 1 and parts[1].strip() else None,
                "list": parts[2].strip() if len(parts) > 2 else "Reminders",
            })
        return [r for r in reminders if r["title"]][:count]

    except Exception as e:
        log.warning(f"get_reminders failed: {e}")
        return []


async def create_reminder(
    title: str,
    due_date: Optional[str] = None,
    list_name: str = "Reminders",
) -> bool:
    """Create a reminder. due_date: 'YYYY-MM-DD', 'today', 'tomorrow', or None."""
    due_as: Optional[str] = None
    if due_date:
        try:
            lower = due_date.lower()
            if lower == "tomorrow":
                d = datetime.now() + timedelta(days=1)
                due_as = d.strftime("%B %d, %Y at 9:00:00 AM")
            elif lower == "today":
                d = datetime.now()
                due_as = d.strftime("%B %d, %Y at 6:00:00 PM")
            else:
                d = datetime.strptime(due_date, "%Y-%m-%d")
                due_as = d.strftime("%B %d, %Y at 9:00:00 AM")
        except Exception:
            pass

    escaped_title = title.replace('"', '\\"')
    escaped_list = list_name.replace('"', '\\"')

    if due_as:
        escaped_due = due_as.replace('"', '\\"')
        script = f'''
tell application "Reminders"
    tell list "{escaped_list}"
        make new reminder with properties {{name:"{escaped_title}", due date:date "{escaped_due}"}}
    end tell
end tell'''
    else:
        script = f'''
tell application "Reminders"
    tell list "{escaped_list}"
        make new reminder with properties {{name:"{escaped_title}"}}
    end tell
end tell'''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            log.warning(f"create_reminder failed: {stderr.decode()}")
            return False
        log.info(f"Reminder created: {title}")
        return True
    except Exception as e:
        log.warning(f"create_reminder error: {e}")
        return False


async def complete_reminder(title: str) -> bool:
    """Mark the first reminder matching the title as complete."""
    escaped = title.replace('"', '\\"')
    script = f'''
tell application "Reminders"
    repeat with l in lists
        try
            set matches to (every reminder of l whose name contains "{escaped}" and completed is false)
            if (count of matches) > 0 then
                set completed of item 1 of matches to true
                return "done"
            end if
        end try
    end repeat
    return "not found"
end tell'''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode().strip() == "done"
    except Exception as e:
        log.warning(f"complete_reminder failed: {e}")
        return False


async def get_reminder_lists() -> list[str]:
    """Get all reminder list names."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Reminders" to return name of every list',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return [l.strip() for l in stdout.decode().strip().split(",") if l.strip()]
    except Exception as e:
        log.warning(f"get_reminder_lists failed: {e}")
    return []


def format_reminders_for_voice(reminders: list[dict]) -> str:
    if not reminders:
        return "No pending reminders, sir."
    count = len(reminders)
    titles = ", ".join(r["title"] for r in reminders[:3])
    if count > 3:
        return f"You have {count} reminders. First three: {titles}. And {count - 3} more, sir."
    return f"You have {count} reminder{'s' if count != 1 else ''}: {titles}, sir."
