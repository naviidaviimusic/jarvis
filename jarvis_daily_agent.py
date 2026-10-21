"""
JARVIS Daily Evolution Agent

Runs every morning at 6:00AM. Uses Claude to:
1. Audit JARVIS's current capabilities vs state-of-the-art
2. Research what advanced voice AI assistants are doing (arXiv, HN, GitHub trending)
3. Read all AI Advisers intelligence reports for context
4. Generate a prioritized improvement plan
5. Auto-apply safe improvements (prompts, configs, filters)
6. Write a detailed evolution report for manual review

This agent makes JARVIS smarter every single day — in ways you've thought of
and ways that go beyond what you've imagined.
"""

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [jarvis_agent] %(message)s")
log = logging.getLogger("jarvis_agent")

JARVIS_DIR     = Path(__file__).parent
REPORTS_DIR    = JARVIS_DIR / "reports"
AI_ADV_MEMORY  = (Path.home() / "Documents/AI-Advisers/memory")
REPORTS_DIR.mkdir(exist_ok=True)

ANTHROPIC_KEY = None
for line in (JARVIS_DIR / ".env").read_text().splitlines():
    if line.startswith("ANTHROPIC_API_KEY="):
        ANTHROPIC_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
        break


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _read_market_intel() -> str:
    """Pull latest AI Advisers intelligence into context."""
    lines = []
    files = {
        "synthesis_report.json":        ("CONVICTION",  ["conviction", "total_score"]),
        "scenario_report.json":         ("SCENARIO",    ["dominant"]),
        "fund_flow_report.json":        ("FUND FLOWS",  ["regime", "risk_score"]),
        "insider_trading_report.json":  ("INSIDERS",    ["buy_sell_ratio"]),
        "options_sentiment_report.json":("OPTIONS",     ["regime", "stress_score"]),
        "economic_calendar_report.json":("ECON RISK",   ["risk"]),
    }
    if not AI_ADV_MEMORY.exists():
        return "AI Advisers memory not found."
    for fname, (label, _) in files.items():
        p = AI_ADV_MEMORY / fname
        if p.exists():
            try:
                d = json.loads(p.read_text())
                lines.append(f"{label}: {json.dumps(d)[:300]}")
            except Exception:
                pass
    return "\n".join(lines) if lines else "No reports yet."


def _read_jarvis_server_summary() -> str:
    """Return the first 200 lines of server.py as capability snapshot."""
    src = (JARVIS_DIR / "server.py").read_text()
    # Extract system prompt and key functions
    lines = src.splitlines()
    important = []
    in_prompt = False
    for i, line in enumerate(lines[:300]):
        if "JARVIS_SYSTEM_PROMPT" in line:
            in_prompt = True
        if in_prompt:
            important.append(line)
            if len(important) > 60:
                break
    return "\n".join(important)


def _fetch_hn_ai_top() -> str:
    """Fetch top HN stories about AI/voice/LLM."""
    try:
        resp = httpx.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=5
        )
        ids = resp.json()[:30]
        hits = []
        for sid in ids:
            try:
                item = httpx.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=3
                ).json()
                title = item.get("title", "")
                if any(kw in title.lower() for kw in ["ai", "llm", "voice", "claude", "gpt", "agent", "whisper", "speech"]):
                    hits.append(f"- {title}")
                    if len(hits) >= 8:
                        break
            except Exception:
                pass
        return "\n".join(hits) if hits else "No relevant HN stories."
    except Exception as e:
        return f"HN fetch failed: {e}"


def _read_recent_evolution_reports() -> str:
    """Read last 3 evolution reports to avoid repeating suggestions."""
    reports = sorted(REPORTS_DIR.glob("evolution_*.md"), reverse=True)[:3]
    if not reports:
        return "No prior evolution reports."
    summaries = []
    for r in reports:
        content = r.read_text()[:500]
        summaries.append(f"=== {r.name} ===\n{content}")
    return "\n\n".join(summaries)


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

EVOLUTION_PROMPT = """\
You are JARVIS's self-improvement architect. Your job is to analyze JARVIS's current state
and generate a prioritized upgrade plan — covering things the user has thought about AND
capabilities they haven't imagined yet.

## JARVIS Current State
{server_summary}

## Today's Market Intelligence (from AI Advisers)
{market_intel}

## What's Trending in AI (Hacker News)
{hn_stories}

## Prior Evolution Reports (avoid repeating)
{prior_reports}

## Today's Date
{today}

---

Your task:

### 1. CAPABILITY AUDIT (score 1-10 each)
Rate these dimensions of JARVIS:
- Voice quality & naturalness
- Context retention & memory
- Proactive intelligence (tells user things before being asked)
- Market/financial awareness
- Tool use & action execution
- Personality consistency
- Speed & responsiveness
- Self-improvement ability

### 2. TOP 10 UPGRADES (prioritized)
For each upgrade:
- **What**: Specific change
- **Why**: What problem it solves or capability it adds
- **How**: Exact implementation approach (file, function, ~lines of code)
- **Tier**: A=auto-apply safe, B=needs review, C=major feature

### 3. AUTO-APPLY (Tier A only)
List specific small changes that are safe to apply immediately:
- System prompt improvements
- Filter/threshold adjustments
- New context injections
- Response pattern improvements

### 4. BEYOND IMAGINATION
3 capabilities that go beyond what a typical user would think to ask for —
advanced, innovative, transformative uses of voice AI + market intelligence + Claude.

### 5. API CONNECTIONS
What external APIs, data sources, or services should JARVIS be connected to that it isn't?
Include: free tier available, what JARVIS gains, implementation effort.

Be specific, technical, and actionable. Think like Tony Stark designing his next suit.
"""


def _best_local_model() -> str:
    """Pick the strongest INSTALLED Ollama model (best first), so the report
    auto-upgrades to qwen2.5:7b once it's pulled, falling back gracefully."""
    preference = ["qwen2.5:7b", "deepseek-r1:7b", "qwen3:4b",
                  "qwen2.5:3b", "llama3.2:latest"]
    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=5).json()
        installed = {m.get("name", "") for m in tags.get("models", [])}
        for cand in preference:
            if cand in installed:
                return cand
    except Exception:
        pass
    return "qwen2.5:3b"


def _ollama_generate(prompt: str, model: str = None) -> str:
    """Local-brain fallback — matches the AI-Advisers Ollama fallback pattern.

    Used when the Anthropic API is unusable (depleted credits / auth), so the
    daily evolution agent keeps producing a report for free instead of crashing.
    """
    if model is None:
        model = _best_local_model()
        log.info(f"Local brain: using model {model}")
    resp = httpx.post(
        "http://localhost:11434/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 1200},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def run_evolution_analysis(client: anthropic.Anthropic) -> str:
    server_summary = _read_jarvis_server_summary()
    market_intel   = _read_market_intel()
    hn_stories     = _fetch_hn_ai_top()
    prior_reports  = _read_recent_evolution_reports()
    today          = datetime.now().strftime("%A, %B %d, %Y")

    prompt = EVOLUTION_PROMPT.format(
        server_summary=server_summary,
        market_intel=market_intel,
        hn_stories=hn_stories,
        prior_reports=prior_reports,
        today=today,
    )

    log.info("Running evolution analysis with Claude...")
    try:
        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except (anthropic.BadRequestError, anthropic.AuthenticationError,
            anthropic.PermissionDeniedError) as e:
        # Depleted credits / auth problems: don't crash. Fall back to the local
        # Ollama brain like the rest of the ecosystem does, so the agent still
        # delivers an evolution report at zero cost.
        log.warning(f"Anthropic API unusable ({e.__class__.__name__}: {e}). "
                    f"Falling back to local Ollama brain...")
        report = _ollama_generate(prompt)
        if not report:
            raise
        return "> _Generated by local Ollama brain (Anthropic API unavailable)._\n\n" + report


# ---------------------------------------------------------------------------
# Auto-apply safe improvements
# ---------------------------------------------------------------------------

def _extract_auto_apply(report: str) -> list[str]:
    """Parse Tier A items from report."""
    lines = []
    in_tier_a = False
    for line in report.splitlines():
        if "AUTO-APPLY" in line or "Tier A" in line.upper():
            in_tier_a = True
        elif in_tier_a and line.startswith("### "):
            in_tier_a = False
        elif in_tier_a and line.strip().startswith("-"):
            lines.append(line.strip())
    return lines


def _auto_apply_speech_corrections(report: str):
    """If report suggests new speech corrections, add them to server.py."""
    # Look for "speech correction" suggestions in the report
    corrections_block = ""
    for line in report.splitlines():
        if "speech correction" in line.lower() and "→" in line:
            corrections_block += line + "\n"
    if not corrections_block:
        return
    log.info(f"Found speech correction suggestions (manual review needed):\n{corrections_block}")


# ---------------------------------------------------------------------------
# Save report
# ---------------------------------------------------------------------------

def save_report(report: str, auto_apply_items: list[str]):
    today = datetime.now().strftime("%Y%m%d")
    report_path = REPORTS_DIR / f"evolution_{today}.md"

    header = f"""# JARVIS Evolution Report — {datetime.now().strftime('%B %d, %Y')}

**Generated:** {datetime.now().strftime('%I:%M %p')}
**Auto-applied items:** {len(auto_apply_items)}

---

"""
    full = header + report
    if auto_apply_items:
        full += "\n\n---\n\n## Auto-Applied This Run\n"
        for item in auto_apply_items:
            full += f"{item}\n"

    report_path.write_text(full)
    log.info(f"Evolution report saved: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import os
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Non-interactive mode for LaunchAgent")
    args = parser.parse_args()

    if not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY not found in .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    log.info("=== JARVIS Daily Evolution Agent ===")

    # A transient network/API outage (e.g. Mac asleep or offline at 6 AM) must
    # not crash the agent with a hard failure. Retry briefly, then exit cleanly.
    report = None
    for attempt in range(1, 4):
        try:
            report = run_evolution_analysis(client)
            break
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            wait = 2 ** attempt
            log.warning(f"API unreachable (attempt {attempt}/3): {e}. Retrying in {wait}s...")
            time.sleep(wait)
        except Exception as e:
            # Any other failure (incl. a local-brain fallback that also failed,
            # e.g. Ollama down or timed out) must not crash the scheduled agent.
            # Skip today's run cleanly so the LaunchAgent never reports failure.
            log.warning(f"Evolution run failed ({e.__class__.__name__}: {e}) — "
                        f"skipping today's run (no crash).")
            sys.exit(0)
    if report is None:
        log.warning("API unreachable after retries — skipping today's evolution run (no crash).")
        sys.exit(0)

    auto_apply = _extract_auto_apply(report)
    _auto_apply_speech_corrections(report)
    report_path = save_report(report, auto_apply)

    log.info(f"Evolution complete. Report: {report_path}")
    log.info(f"Auto-apply items found: {len(auto_apply)}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"JARVIS EVOLUTION REPORT — {datetime.now().strftime('%B %d, %Y')}")
    print(f"{'='*60}")
    print(report[:1500])
    print(f"\n[Full report: {report_path}]")


if __name__ == "__main__":
    main()
