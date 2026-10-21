#!/usr/local/bin/python3.11
"""
JARVIS Trigger Monitor
─────────────────────────
SQLite-backed conditional triggers. JARVIS stores conditions like:
  "alert me when NVDA drops below 500"
  "remind me at 3pm to call John"
  "alert if VIX spikes above 30"
Checks every 5 minutes, fires via WebSocket or macOS notification.
"""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("jarvis.trigger_monitor")

DB_PATH = Path(__file__).parent / "data" / "triggers.db"


# ── DB setup ──────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            condition_type TEXT,
            target TEXT,
            threshold REAL,
            message TEXT,
            fired INTEGER DEFAULT 0,
            fired_at TEXT
        )
    """)
    conn.commit()
    return conn


# ── Public API ────────────────────────────────────────────────────────────────

def add_trigger(condition_type: str, target: str, threshold: float, message: str) -> int:
    """Add a trigger, return its ID."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO triggers (created_at, condition_type, target, threshold, message, fired) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (datetime.now().isoformat(), condition_type, target, threshold, message),
        )
        conn.commit()
        trigger_id = cur.lastrowid
        log.info("Trigger added: id=%s type=%s target=%s threshold=%s", trigger_id, condition_type, target, threshold)
        return trigger_id
    finally:
        conn.close()


def remove_trigger(trigger_id: int) -> str:
    """Remove a trigger by ID."""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
        conn.commit()
        if cur.rowcount:
            return f"Trigger {trigger_id} removed, sir."
        return f"No trigger found with ID {trigger_id}, sir."
    finally:
        conn.close()


def list_triggers() -> list[dict]:
    """Return all active (unfired) triggers."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE fired = 0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _check_price_trigger(trigger: dict) -> bool:
    """Use yfinance fast_info to check price vs threshold. Returns True if should fire."""
    try:
        import yfinance as yf  # type: ignore
        ticker = yf.Ticker(trigger["target"])
        price = ticker.fast_info.last_price
        if price is None:
            return False
        ctype = trigger["condition_type"]
        threshold = trigger["threshold"]
        if ctype == "price_below":
            return float(price) < threshold
        if ctype == "price_above":
            return float(price) > threshold
        if ctype == "vix_above":
            return float(price) > threshold
    except Exception as exc:
        log.warning("Price check failed for %s: %s", trigger.get("target"), exc)
    return False


def check_all(ws_send_fn: Optional[Callable] = None) -> list[str]:
    """Check all unfired triggers. For each that fires:
    - Mark as fired in DB
    - Return list of messages to speak
    If ws_send_fn provided, it's an async fn — skip it (caller handles async).
    """
    triggers = list_triggers()
    fired_messages: list[str] = []
    now = datetime.now()

    conn = _get_conn()
    try:
        for t in triggers:
            fired = False
            ctype = t["condition_type"]

            if ctype in ("price_below", "price_above", "vix_above"):
                fired = _check_price_trigger(t)

            elif ctype == "time":
                # target is "HH:MM"
                try:
                    target_time = datetime.strptime(t["target"], "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day
                    )
                    # Fire if we've passed the target time (within a 6-minute window)
                    diff = (now - target_time).total_seconds()
                    fired = 0 <= diff <= 360
                except ValueError:
                    log.warning("Invalid time target: %s", t["target"])

            elif ctype == "text_match":
                # text_match triggers are fired externally; skip auto-check
                pass

            if fired:
                conn.execute(
                    "UPDATE triggers SET fired = 1, fired_at = ? WHERE id = ?",
                    (now.isoformat(), t["id"]),
                )
                conn.commit()
                fired_messages.append(t["message"])
                log.info("Trigger fired: id=%s message=%s", t["id"], t["message"])

    finally:
        conn.close()

    return fired_messages


def parse_trigger_from_text(text: str) -> Optional[dict]:
    """Parse natural language into a trigger dict.

    Examples:
      "watch NVDA below 500"        → {type:"price_below", target:"NVDA", threshold:500.0, ...}
      "alert me if VIX goes above 30" → {type:"vix_above", target:"^VIX", threshold:30.0, ...}
      "remind me at 3pm to call John" → {type:"time", target:"15:00", threshold:0, ...}
    Returns None if unable to parse.
    """
    t = text.strip().lower()

    # ── VIX above ─────────────────────────────────────────────────────────────
    m = re.search(r"vix.{0,20}(?:above|over|spike[sd]?\s+(?:above|over)?)\s*([\d.]+)", t)
    if m:
        threshold = float(m.group(1))
        return {
            "condition_type": "vix_above",
            "target": "^VIX",
            "threshold": threshold,
            "message": f"VIX has spiked above {threshold:g}, sir.",
        }

    # ── Price below ───────────────────────────────────────────────────────────
    m = re.search(
        r"(?:watch|alert|notify|warn)[^\n]*?([A-Z]{1,5}|\b[a-z]{1,5}\b).{0,20}(?:below|under|drops?\s+(?:below|under)?)\s*\$?([\d.]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        ticker = m.group(1).upper()
        threshold = float(m.group(2))
        return {
            "condition_type": "price_below",
            "target": ticker,
            "threshold": threshold,
            "message": f"{ticker} has dropped below ${threshold:g}, sir.",
        }

    # ── Price above ───────────────────────────────────────────────────────────
    m = re.search(
        r"(?:watch|alert|notify|warn)[^\n]*?([A-Z]{1,5}|\b[a-z]{1,5}\b).{0,20}(?:above|over|rises?\s+(?:above|over)?)\s*\$?([\d.]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        ticker = m.group(1).upper()
        threshold = float(m.group(2))
        return {
            "condition_type": "price_above",
            "target": ticker,
            "threshold": threshold,
            "message": f"{ticker} has risen above ${threshold:g}, sir.",
        }

    # ── Time reminder ─────────────────────────────────────────────────────────
    m = re.search(
        r"remind(?:er)?\s+(?:me\s+)?at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(?:to\s+)?(.+)",
        t,
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        reminder_text = m.group(4).strip().rstrip(".,!?")
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target_time = f"{hour:02d}:{minute:02d}"
        return {
            "condition_type": "time",
            "target": target_time,
            "threshold": 0.0,
            "message": f"Reminder: {reminder_text}, sir.",
        }

    return None


def format_triggers_for_voice() -> str:
    """Return voice-friendly list of active triggers."""
    triggers = list_triggers()
    if not triggers:
        return "No active triggers, sir."

    parts: list[str] = []
    for t in triggers:
        ctype = t["condition_type"]
        if ctype == "price_below":
            parts.append(f"{t['target']} below ${t['threshold']:g}")
        elif ctype == "price_above":
            parts.append(f"{t['target']} above ${t['threshold']:g}")
        elif ctype == "vix_above":
            parts.append(f"VIX above {t['threshold']:g}")
        elif ctype == "time":
            parts.append(f"reminder at {t['target']}")
        elif ctype == "text_match":
            parts.append(f"text match for '{t['target']}'")
        else:
            parts.append(t["message"])

    return f"You have {len(triggers)} active trigger{'s' if len(triggers) != 1 else ''}: " + "; ".join(parts) + "."
