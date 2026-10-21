#!/usr/local/bin/python3.11
import json
import time
from datetime import datetime
from pathlib import Path

SESSION_FILE = Path(__file__).parent / "data" / "session.json"
MAX_AGE_HOURS = 24


def save_session(history: list, session_summary: str, last_response: str):
    SESSION_FILE.parent.mkdir(exist_ok=True)
    data = {
        "ts": time.time(),
        "date": datetime.now().isoformat(),
        "history": history[-30:],
        "session_summary": session_summary,
        "last_response": last_response,
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2))


def load_session() -> dict | None:
    try:
        if not SESSION_FILE.exists():
            return None
        data = json.loads(SESSION_FILE.read_text())
        age_h = (time.time() - data.get("ts", 0)) / 3600
        if age_h > MAX_AGE_HOURS:
            return None
        return data
    except Exception:
        return None


def clear_session():
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
