"""
JARVIS Contacts — Apple Contacts via AppleScript.
"""

import asyncio
import logging
from typing import Optional

log = logging.getLogger("jarvis.contacts")


async def search_contact(name: str, max_results: int = 5) -> list[dict]:
    """Search Apple Contacts by name (partial match)."""
    escaped = name.replace('"', '\\"')
    script = f'''
tell application "Contacts"
    set output to ""
    try
        set matches to (every person whose name contains "{escaped}")
        repeat with p in matches
            set n to name of p
            set ph to ""
            set em to ""
            set co to ""
            try
                set phones to phones of p
                if (count of phones) > 0 then
                    set ph to value of item 1 of phones
                end if
            end try
            try
                set emails to emails of p
                if (count of emails) > 0 then
                    set em to value of item 1 of emails
                end if
            end try
            try
                set org to organization of p
                if org is not missing value then
                    set co to org
                end if
            end try
            set output to output & n & "|||" & ph & "|||" & em & "|||" & co & linefeed
        end repeat
    end try
    return output
end tell'''

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return []

        contacts = []
        for line in stdout.decode().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|||")
            contacts.append({
                "name": parts[0].strip() if len(parts) > 0 else "",
                "phone": parts[1].strip() if len(parts) > 1 else "",
                "email": parts[2].strip() if len(parts) > 2 else "",
                "company": parts[3].strip() if len(parts) > 3 else "",
            })
        return [c for c in contacts if c["name"]][:max_results]

    except Exception as e:
        log.warning(f"search_contact({name}) failed: {e}")
        return []


async def get_contact_phones(name: str) -> list[str]:
    """Get all phone numbers for a contact."""
    contacts = await search_contact(name, max_results=1)
    if contacts and contacts[0]["phone"]:
        return [contacts[0]["phone"]]
    return []


def format_contact_for_voice(contacts: list[dict], query: str) -> str:
    if not contacts:
        return f"No contact found matching {query}, sir."
    c = contacts[0]
    parts = [c["name"]]
    if c["company"]:
        parts.append(f"at {c['company']}")
    if c["phone"]:
        parts.append(f"phone {c['phone']}")
    if c["email"]:
        parts.append(f"email {c['email']}")
    if len(contacts) > 1:
        parts.append(f"and {len(contacts) - 1} other match{'es' if len(contacts) > 2 else ''}")
    return ", ".join(parts) + ", sir."
