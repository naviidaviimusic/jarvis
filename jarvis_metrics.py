#!/usr/local/bin/python3.11
"""
JARVIS Metrics — measured health for the daily evolution report
───────────────────────────────────────────────────────────────
The local brain invents scores, files and commands when it has nothing real to
go on. This gathers facts from JARVIS's own logs so the report is grounded:

    - replies: count, speed (p50/p90), channel, which brain answered
    - failures: offline/fallback replies, deadlines hit, Ollama errors
    - top server errors (normalized), daemon delivery problems
    - user feedback (thumbs up/down)
    - the real list of modules and [ACTION:X] tags

    python3.11 jarvis_metrics.py          # print the summary
"""

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

JARVIS_DIR = Path(__file__).parent
INTERACTION_LOG = JARVIS_DIR / "data" / "interaction_log.jsonl"
SERVER_LOG = JARVIS_DIR / "logs" / "server.log"
DAEMON_LOG = JARVIS_DIR / "logs" / "daemon.log"

FAILURE_REPLIES = ("offline mode", "trouble connecting", "local mode", "api unavailable",
                   "something went wrong", "having trouble reaching")
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _tail_lines(path: Path, max_bytes: int = 5_000_000) -> list[str]:
    """Last max_bytes of a log, as lines (logs grow; never read them whole)."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "replace").splitlines()


def _recent_log_lines(path: Path, since: datetime) -> list[str]:
    """Log lines stamped at/after `since` (continuation lines follow their entry)."""
    out, keep = [], False
    for line in _tail_lines(path):
        m = _LOG_TS.match(line)
        if m:
            keep = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= since
        if keep:
            out.append(line)
    return out


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def interaction_stats(since: datetime) -> dict:
    replies, feedback = [], Counter()
    for line in _tail_lines(INTERACTION_LOG):
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e.get("ts", "")) < since:
                continue
        except Exception:
            continue
        if e.get("type") == "feedback":
            feedback["up" if e.get("rating", 0) > 0 else "down"] += 1
        else:
            replies.append(e)

    lat = [e["latency_ms"] / 1000 for e in replies if e.get("latency_ms")]
    failed = [e for e in replies
              if any(f in str(e.get("jarvis", "")).lower() for f in FAILURE_REPLIES)]
    return {
        "replies": len(replies),
        "by_channel": dict(Counter(e.get("channel", "browser") for e in replies)),
        "by_brain": dict(Counter(e.get("brain", "unknown") for e in replies)),
        "latency_p50_s": round(_pct(lat, 0.5), 1),
        "latency_p90_s": round(_pct(lat, 0.9), 1),
        "slow_over_30s": sum(1 for v in lat if v > 30),
        "failed_replies": len(failed),
        "failed_examples": [e.get("user", "")[:80] for e in failed[-3:]],
        "feedback": dict(feedback),
    }


def server_stats(since: datetime) -> dict:
    lines = _recent_log_lines(SERVER_LOG, since)
    errors = Counter()
    for line in lines:
        if "credit balance is too low" in line:
            continue  # counted separately below
        if "[jarvis" in line and (" ERROR " in line or "error:" in line.lower()):
            msg = line.split("] ", 1)[-1]
            msg = re.sub(r"req_[A-Za-z0-9]+|\d+(\.\d+)?", "#", msg)[:110]
            errors[msg] += 1
    return {
        "tracebacks": sum(1 for l in lines if l.startswith("Traceback")),
        "deadline_hits": sum(1 for l in lines if "deadline — returning partial" in l),
        "ollama_failures": sum(1 for l in lines if "Ollama generate failed" in l),
        "claude_credit_errors": sum(1 for l in lines if "credit balance is too low" in l),
        "top_errors": errors.most_common(5),
    }


def daemon_stats(since: datetime) -> dict:
    lines = _recent_log_lines(DAEMON_LOG, since)
    return {
        "commands_sent": sum(1 for l in lines if "Wake word detected" in l),
        "server_down": sum(1 for l in lines if "Server down" in l),
        "no_response": sum(1 for l in lines if "No response from server" in l),
        "slow_server": sum(1 for l in lines if "not resending the command" in l),
    }


def real_modules() -> list[str]:
    return sorted(p.name for p in JARVIS_DIR.glob("*.py") if not p.name.startswith("test_"))


def real_action_tags() -> list[str]:
    src = (JARVIS_DIR / "server.py").read_text(errors="replace")
    return sorted(t for t in set(re.findall(r"\[ACTION:([A-Z_]+)", src)) if len(t) > 1)


def build_summary(days: int = 7) -> str:
    """Plain-text block for the evolution prompt."""
    since = datetime.now() - timedelta(days=days)
    i, s, d = interaction_stats(since), server_stats(since), daemon_stats(since)
    lines = [
        f"Window: last {days} days (since {since:%Y-%m-%d %H:%M}).",
        f"Replies: {i['replies']} | by channel {i['by_channel'] or '{}'} | by brain {i['by_brain'] or '{}'}",
        f"Reply time: p50 {i['latency_p50_s']}s, p90 {i['latency_p90_s']}s, {i['slow_over_30s']} replies over 30s",
        f"Failed/fallback replies: {i['failed_replies']}"
        + (f" (e.g. {'; '.join(i['failed_examples'])})" if i["failed_examples"] else ""),
        f"User feedback: {i['feedback'] or 'none recorded'}",
        f"Server: {s['tracebacks']} tracebacks, {s['deadline_hits']} local-brain deadline hits, "
        f"{s['ollama_failures']} Ollama failures, {s['claude_credit_errors']} Claude credit errors",
    ]
    if s["top_errors"]:
        lines.append("Top server errors:")
        lines += [f"  - {n}x {msg}" for msg, n in s["top_errors"]]
    lines.append(f"Wake-word daemon: {d['commands_sent']} commands, {d['server_down']} server-down retries, "
                 f"{d['no_response']} no-response, {d['slow_server']} too-slow replies")
    if i["replies"] == 0:
        lines.append("NOTE: no conversations in this window — there is no usage data to score.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_summary())
    print("\nModules:", ", ".join(real_modules()))
    print("\nAction tags:", ", ".join(real_action_tags()))
