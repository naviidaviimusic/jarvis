#!/usr/local/bin/python3.11
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

ADVISERS_MEM = (Path.home() / "Documents/AI-Advisers/memory")
DATA_DIR = Path(__file__).parent / "data"
BRIEFING_FILE = DATA_DIR / "morning_briefing.txt"
MAX_AGE_HOURS = 12

_CLOSERS = [
    "Do try to have a productive day, sir.",
    "I shall monitor the markets so you need not.",
    "The machines are watching. As am I.",
    "Your portfolio will not manage itself, unfortunately.",
    "I suggest caffeine before the open, sir.",
]


def _load(filename: str) -> dict:
    try:
        return json.loads((ADVISERS_MEM / filename).read_text())
    except Exception:
        return {}


def _pct(val) -> str:
    try:
        return f"{int(val)}%"
    except Exception:
        return "N/A"


def _top_narrative(narratives: dict) -> tuple[str, str]:
    """Return (name, stage) for the narrative with most mentions."""
    best = None
    best_mentions = -1
    for name, data in narratives.items():
        m = data.get("mentions", 0)
        if m > best_mentions:
            best_mentions = m
            best = (name.replace("_", " ").title(), data.get("stage", "UNKNOWN"))
    return best or ("Unknown", "UNKNOWN")


def _next_risk_event(risk: dict) -> str:
    high_48h = risk.get("high_impact_48h", 0)
    if high_48h and high_48h > 0:
        return f"{high_48h} high-impact release(s) in the next 48 hours demand your attention."
    next_vh = risk.get("next_very_high")
    if next_vh:
        name = next_vh.get("name", "Unknown event")
        when = next_vh.get("when", "")
        return f"Next major catalyst: {name} {when}."
    return "The calendar is mercifully quiet for now."


def _closer(conviction: str) -> str:
    import hashlib
    day_seed = int(hashlib.md5(datetime.now().strftime("%Y-%m-%d").encode()).hexdigest(), 16)
    return _CLOSERS[day_seed % len(_CLOSERS)]


def generate_briefing() -> str:
    syn = _load("synthesis_report.json")
    scen = _load("scenario_report.json")
    flows = _load("fund_flow_report.json")
    insider = _load("insider_trading_report.json")
    fed = _load("fed_funds_report.json")
    breadth = _load("market_breadth_report.json")
    narr = _load("narrative_intelligence_report.json")
    opts = _load("options_sentiment_report.json")
    cal = _load("economic_calendar_report.json")

    conviction = syn.get("conviction", "NEUTRAL")
    conviction_pct = syn.get("conviction_pct", 0)
    total_score = syn.get("total_score", 0)
    breadth_regime = breadth.get("regime", "UNKNOWN")
    vix = opts.get("vol_surface", {}).get("vix", {}).get("current", "N/A")
    opts_regime = opts.get("regime", "UNKNOWN")

    scenarios = scen.get("scenarios", [])
    top_scen = scenarios[0] if scenarios else {}
    scen_name = top_scen.get("name", "UNKNOWN").replace("_", " ").title()
    scen_prob = _pct(top_scen.get("probability", 0))

    flows_regime = flows.get("regime", "MIXED")
    insider_bsr = insider.get("buy_sell_ratio", {})
    insider_sentiment = insider_bsr.get("sentiment", "NEUTRAL")
    insider_buys = insider_bsr.get("buys", 0)
    insider_sells = insider_bsr.get("sells", 0)

    fed_rate = fed.get("current_rate", "N/A")
    cut_prob = _pct(fed.get("cut_prob", 0) * 100 if isinstance(fed.get("cut_prob"), float) and fed.get("cut_prob", 0) <= 1 else fed.get("cut_prob", 0))
    next_fomc = fed.get("next_meeting", "unknown date")

    narratives = narr.get("narratives", {})
    top_narr_name, top_narr_stage = _top_narrative(narratives)

    risk = cal.get("risk", {})
    risk_line = _next_risk_event(risk)

    closer = _closer(conviction)

    briefing = (
        f"Good morning, sir. "
        f"The market conviction is {conviction} at {conviction_pct} points, "
        f"with breadth reading {breadth_regime.replace('_', ' ').title()} and VIX at {vix} — "
        f"options regime {opts_regime.lower()}. "
        f"The dominant scenario is {scen_name} at {scen_prob} probability. "
        f"Fund flows are {flows_regime.lower()}, and insiders are {insider_sentiment.lower()} "
        f"with {insider_buys} buys against {insider_sells} sells in recent filings. "
        f"The Fed sits at {fed_rate}%, with a {cut_prob} cut probability; "
        f"next FOMC is {next_fomc}. "
        f"The leading narrative is {top_narr_name}, currently {top_narr_stage.lower()}. "
        f"{risk_line} "
        f"{closer}"
    )
    return briefing


def save_briefing() -> str:
    DATA_DIR.mkdir(exist_ok=True)
    briefing = generate_briefing()
    now = datetime.now()
    header = f"[JARVIS Morning Briefing — {now.strftime('%A, %d %B %Y %H:%M')}]\n\n"
    BRIEFING_FILE.write_text(header + briefing + "\n")
    return briefing


def get_briefing() -> str | None:
    try:
        if not BRIEFING_FILE.exists():
            return None
        content = BRIEFING_FILE.read_text()
        age_h = (time.time() - BRIEFING_FILE.stat().st_mtime) / 3600
        if age_h > MAX_AGE_HOURS:
            return None
        lines = content.strip().splitlines()
        # Return everything after the header line and blank line
        for i, line in enumerate(lines):
            if line.startswith("[JARVIS Morning Briefing"):
                body = "\n".join(lines[i + 2:]).strip()
                return body if body else None
        return content.strip() or None
    except Exception:
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS Morning Briefing Generator")
    parser.add_argument("--auto", action="store_true", help="Run in scheduled/silent mode")
    args = parser.parse_args()

    text = save_briefing()
    if not args.auto:
        print(text)
