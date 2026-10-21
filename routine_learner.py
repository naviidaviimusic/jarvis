"""
routine_learner.py — Scans interaction_log.jsonl for time-of-day patterns.
Writes routine_patterns.json used by server.py to pre-warm cache and
proactively offer context at the right time of day.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
LOG_PATH = DATA_DIR / "interaction_log.jsonl"
PATTERNS_PATH = DATA_DIR / "routine_patterns.json"
_PATTERNS_TTL = 3600 * 8   # re-read file at most every 8 hours

_patterns_cache: Optional[dict] = None
_patterns_ts: float = 0.0


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------

def _hour_bucket(iso_ts: str) -> Optional[int]:
    try:
        return datetime.fromisoformat(iso_ts).hour
    except Exception:
        try:
            return datetime.fromtimestamp(float(iso_ts)).hour
        except Exception:
            return None


_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "market_brief":   ["market", "brief", "portfolio", "conviction", "scenario", "stocks", "spy", "vix"],
    "morning_plan":   ["today", "schedule", "plan", "morning", "meetings", "calendar"],
    "code":           ["build", "code", "debug", "error", "fix", "deploy", "git", "pr"],
    "news":           ["news", "headlines", "what happened", "latest", "update"],
    "research":       ["research", "look up", "find", "search", "article"],
    "system_check":   ["stats", "battery", "cpu", "memory", "disk", "how is", "how's"],
}


def _classify_topic(text: str) -> Optional[str]:
    low = text.lower()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            return topic
    return None


def learn_patterns() -> dict:
    """Read interaction_log.jsonl and build hour-bucketed topic frequency map.
    Returns dict: {hour_str: {topic: count}}.
    """
    if not LOG_PATH.exists():
        return {}

    # hour → topic → count
    freq: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("type") == "feedback":
                    continue
                ts = entry.get("ts") or entry.get("timestamp", "")
                text = entry.get("user", "") or entry.get("text", "")
                if not ts or not text:
                    continue
                hour = _hour_bucket(ts)
                if hour is None:
                    continue
                topic = _classify_topic(text)
                if topic:
                    freq[hour][topic] += 1
            except Exception:
                continue
    except Exception:
        return {}

    # Keep only topic/hour combos that appear at least twice (real pattern, not noise)
    patterns: dict[str, dict[str, int]] = {}
    for hour, topics in freq.items():
        strong = {t: c for t, c in topics.items() if c >= 2}
        if strong:
            patterns[str(hour)] = strong

    result = {
        "generated": datetime.now().isoformat(),
        "patterns": patterns,
    }
    try:
        PATTERNS_PATH.write_text(json.dumps(result, indent=2))
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Runtime API — used by server.py at greeting time
# ---------------------------------------------------------------------------

def get_patterns() -> dict[str, dict[str, int]]:
    """Return cached patterns dict, refreshing if stale."""
    global _patterns_cache, _patterns_ts
    now = time.time()
    if _patterns_cache is not None and (now - _patterns_ts) < _PATTERNS_TTL:
        return _patterns_cache

    # Try reading saved file first (fast path)
    if PATTERNS_PATH.exists():
        try:
            data = json.loads(PATTERNS_PATH.read_text())
            _patterns_cache = data.get("patterns", {})
            _patterns_ts = now
            return _patterns_cache
        except Exception:
            pass

    # Fall back to live computation
    data = learn_patterns()
    _patterns_cache = data.get("patterns", {})
    _patterns_ts = now
    return _patterns_cache


def get_likely_topics_now(top_n: int = 3) -> list[str]:
    """Return up to top_n topic names the user most frequently asks about at this hour."""
    hour = datetime.now().hour
    patterns = get_patterns()
    bucket = patterns.get(str(hour), {})
    if not bucket:
        return []
    sorted_topics = sorted(bucket.items(), key=lambda x: x[1], reverse=True)
    return [t for t, _ in sorted_topics[:top_n]]


def suggest_proactive(hour: Optional[int] = None) -> Optional[str]:
    """Return a one-line proactive suggestion string if there's a strong pattern for this hour,
    or None if nothing stands out.
    """
    if hour is None:
        hour = datetime.now().hour
    patterns = get_patterns()
    bucket = patterns.get(str(hour), {})
    if not bucket:
        return None
    top_topic, count = max(bucket.items(), key=lambda x: x[1])
    if count < 3:
        return None

    _TOPIC_PHRASES: dict[str, str] = {
        "market_brief":  "Shall I pull up the market brief, sir?",
        "morning_plan":  "Would you like your schedule for the day, sir?",
        "code":          "Ready for a build session, sir.",
        "news":          "Shall I surface the latest headlines, sir?",
        "research":      "Ready to research, sir. What are we looking into?",
        "system_check":  "System stats are standing by, sir.",
    }
    return _TOPIC_PHRASES.get(top_topic)


# ---------------------------------------------------------------------------
# CLI entry point — run directly to rebuild patterns
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    result = learn_patterns()
    patterns = result.get("patterns", {})
    print(f"Patterns learned from {LOG_PATH}:")
    for hour in sorted(patterns.keys(), key=int):
        topics = patterns[hour]
        print(f"  {int(hour):02d}:00 — {dict(topics)}")
    if not patterns:
        print("  (not enough data yet — need 2+ occurrences per topic/hour)")
    sys.exit(0)
