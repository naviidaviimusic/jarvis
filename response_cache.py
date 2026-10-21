#!/usr/local/bin/python3.11
"""
JARVIS Response Cache — 5-minute TTL cache for common/repeated queries.
Saves ~2s latency on repeated market queries, greetings, status checks.
"""

import re
import string
import threading
import time
from datetime import datetime

_cache: dict[str, tuple[str, float]] = {}  # key → (response, timestamp)
_lock = threading.Lock()
_TTL = 300.0  # seconds


def _normalize(text: str) -> str | None:
    # Strip punctuation, lowercase, strip whitespace
    translator = str.maketrans("", "", string.punctuation)
    cleaned = text.translate(translator).lower().strip()
    # Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned)

    # Never cache time queries
    if re.search(r"\bwhat time is it\b", cleaned):
        return None

    # Greeting patterns — key varies by hour so morning/afternoon responses differ
    greeting_patterns = [
        r"\bwhats up\b",
        r"\bhey\b$",
        r"\bsup\b$",
        r"\bwhat up\b",
        r"\bhow are you\b",
    ]
    for pat in greeting_patterns:
        if re.search(pat, cleaned):
            hour = datetime.now().hour
            return f"casual_greeting_{hour}"

    # Market patterns
    market_patterns = [
        r"\bhows the market\b",
        r"\bmarket update\b",
        r"\bmarket summary\b",
        r"\bmarkets\b$",
        r"\bhow are markets\b",
        r"\bmarket\b$",
    ]
    for pat in market_patterns:
        if re.search(pat, cleaned):
            return "market_summary"

    # Briefing patterns
    briefing_patterns = [
        r"\bbriefing\b",
        r"\bmorning briefing\b",
        r"\bdaily briefing\b",
        r"\bwhats happening\b",
    ]
    for pat in briefing_patterns:
        if re.search(pat, cleaned):
            return "briefing"

    # Portfolio patterns
    portfolio_patterns = [
        r"\bportfolio\b$",
        r"\bmy holdings\b",
        r"\bportfolio status\b",
        r"\bhow am i doing\b",
    ]
    for pat in portfolio_patterns:
        if re.search(pat, cleaned):
            return "portfolio_status"

    return None


def get(text: str) -> str | None:
    key = _normalize(text)
    if key is None:
        return None

    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        response, ts = entry
        if time.monotonic() - ts > _TTL:
            del _cache[key]
            return None
        return response


def set(text: str, response: str) -> None:
    key = _normalize(text)
    if key is None:
        return

    with _lock:
        _cache[key] = (response, time.monotonic())


def invalidate(key: str) -> None:
    with _lock:
        _cache.pop(key, None)


def invalidate_all() -> None:
    with _lock:
        _cache.clear()
