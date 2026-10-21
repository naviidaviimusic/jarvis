#!/usr/local/bin/python3.11
import argparse
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LOG_FILE = DATA_DIR / "interaction_log.jsonl"
REPORT_FILE = DATA_DIR / "self_improve_report.txt"
SUMMARY_FILE = DATA_DIR / "self_improve_summary.json"
MAX_AGE_DAYS = 7

CORRECTION_SIGNALS = {"no", "nope", "wrong", "that's not right", "thats not right",
                       "incorrect", "not quite", "not what i meant", "that's wrong"}
SATISFACTION_SIGNALS = {"thanks", "thank you", "perfect", "great", "exactly",
                         "brilliant", "good", "nice", "well done", "awesome", "got it"}

STOPWORDS = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
              "of", "with", "is", "it", "that", "what", "how", "can", "you", "i",
              "my", "me", "do", "did", "was", "are", "be", "this", "have", "has",
              "just", "please", "jarvis", "hey", "ok", "okay", "yeah", "yes"}

VERBOSE_WORD_THRESHOLD = 80


def _load_log() -> list[dict]:
    entries = []
    if not LOG_FILE.exists():
        return entries
    for line in LOG_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _is_correction(text: str) -> bool:
    t = text.lower().strip().rstrip(".!?")
    return t in CORRECTION_SIGNALS or any(s in t for s in CORRECTION_SIGNALS)


def _is_satisfaction(text: str) -> bool:
    t = text.lower().strip().rstrip(".!?")
    return t in SATISFACTION_SIGNALS or any(s in t for s in SATISFACTION_SIGNALS)


def _extract_topics(entries: list[dict]) -> list[tuple[str, int]]:
    words = []
    for e in entries:
        user_text = e.get("user", "")
        for word in user_text.lower().split():
            clean = word.strip(".,!?;:\"'()-")
            if len(clean) > 3 and clean not in STOPWORDS:
                words.append(clean)
    counter = Counter(words)
    return counter.most_common(10)


def analyze(entries: list[dict]) -> dict:
    corrections = 0
    satisfactions = 0
    latencies = []
    verbose_count = 0

    for i, entry in enumerate(entries):
        user_text = entry.get("user", "")
        jarvis_text = entry.get("jarvis", "")
        latency = entry.get("latency_ms")

        if latency is not None:
            try:
                latencies.append(float(latency))
            except (ValueError, TypeError):
                pass

        if _is_correction(user_text):
            corrections += 1
        if _is_satisfaction(user_text):
            satisfactions += 1

        word_count = len(jarvis_text.split())
        if word_count > VERBOSE_WORD_THRESHOLD:
            verbose_count += 1

    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0.0
    top_topics = [word for word, _ in _extract_topics(entries)]

    recommendation = _make_recommendation(corrections, satisfactions, avg_latency,
                                           verbose_count, len(entries))

    return {
        "corrections": corrections,
        "satisfactions": satisfactions,
        "avg_latency_ms": avg_latency,
        "top_topics": top_topics,
        "verbose_responses": verbose_count,
        "recommendation": recommendation,
    }


def _make_recommendation(corrections: int, satisfactions: int, avg_latency: float,
                          verbose_count: int, total: int) -> str:
    parts = []

    if total == 0:
        return "No interaction data available for analysis."

    correction_rate = corrections / total
    satisfaction_rate = satisfactions / total

    if correction_rate > 0.15:
        parts.append(f"High correction rate ({corrections}/{total}) — review accuracy on common topics.")
    if satisfaction_rate > 0.3:
        parts.append(f"Strong satisfaction signal ({satisfactions}/{total}) — current style is working well.")
    if avg_latency > 2000:
        parts.append(f"Average latency {avg_latency}ms exceeds 2s — investigate response pipeline.")
    if verbose_count > total * 0.2:
        parts.append(f"{verbose_count} verbose responses (>{VERBOSE_WORD_THRESHOLD} words) detected — consider tightening voice outputs.")

    if not parts:
        parts.append("Performance within normal parameters. No immediate changes recommended.")

    return " ".join(parts)


def generate_report(summary: dict, entries: list[dict]) -> str:
    now = datetime.now()
    total = len(entries)
    lines = [
        f"JARVIS Self-Improvement Report",
        f"Generated: {now.strftime('%A, %d %B %Y %H:%M')}",
        f"Interactions analysed: {total}",
        "",
        f"Corrections detected:     {summary['corrections']}",
        f"Satisfactions detected:   {summary['satisfactions']}",
        f"Average latency:          {summary['avg_latency_ms']} ms",
        f"Verbose responses (>{VERBOSE_WORD_THRESHOLD}w): {summary['verbose_responses']}",
        "",
        "Top query topics:",
    ]
    for i, topic in enumerate(summary["top_topics"], 1):
        lines.append(f"  {i:2}. {topic}")
    lines += [
        "",
        "Recommendation:",
        f"  {summary['recommendation']}",
        "",
    ]
    return "\n".join(lines)


def run():
    DATA_DIR.mkdir(exist_ok=True)
    entries = _load_log()
    summary = analyze(entries)
    report_text = generate_report(summary, entries)

    REPORT_FILE.write_text(report_text)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2))
    return summary, report_text


def get_latest_report() -> dict | None:
    try:
        if not SUMMARY_FILE.exists():
            return None
        age_days = (time.time() - SUMMARY_FILE.stat().st_mtime) / 86400
        if age_days > MAX_AGE_DAYS:
            return None
        return json.loads(SUMMARY_FILE.read_text())
    except Exception:
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS Self-Improvement Analyser")
    parser.add_argument("--auto", action="store_true", help="Run in scheduled/silent mode")
    args = parser.parse_args()

    summary, report = run()
    if not args.auto:
        print(report)
