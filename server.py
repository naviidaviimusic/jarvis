"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from actions import execute_action, monitor_build, open_terminal, open_browser, open_claude_in_project, _generate_project_name, prompt_existing_terminal
from work_mode import WorkSession, is_casual_question
from screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache, create_calendar_event
from mail_access import get_unread_count, get_unread_messages, get_recent_messages, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES
from github_access import get_github_status, create_issue as github_create_issue, format_github_for_voice
from reminders_access import get_reminders, create_reminder, complete_reminder, format_reminders_for_voice
from contacts_access import search_contact, format_contact_for_voice
from spotify_access import handle_spotify_command, get_spotify_info
from clipboard_access import read_clipboard, write_clipboard, format_clipboard_for_voice
from market_data import get_price, get_market_summary, format_price_for_voice, format_market_summary_for_voice
from ai_advisers_bridge import search as kb_search, format_search_for_voice as kb_voice, get_context_for_prompt as kb_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# Local Whisper STT — bypasses Chrome Web Speech API / Google servers entirely
from stt import MicListener
_mic_listener: MicListener = None  # started in lifespan
_transcript_queue: asyncio.Queue = asyncio.Queue()  # mic → voice handler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
FISH_API_URL = "https://api.fish.audio/v1/tts"
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = Path(PROJECT_DIR) / "data"   # used by /api/feedback (was undefined → every call crashed)

# Task QA / follow-up tracking. These were referenced by the dispatch code but
# never imported or created, so every completed task's QA step raised
# NameError. Guarded so a problem in them can never stop the server booting.
try:
    from qa import QAAgent
    from tracking import SuccessTracker
    from suggestions import suggest_followup
    qa_agent = QAAgent()
    success_tracker = SuccessTracker()
except Exception as _qa_err:  # noqa: BLE001
    qa_agent = success_tracker = suggest_followup = None
    print(f"[server] task QA disabled: {_qa_err}")

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humor is observational, never jokes: state facts and let implications land
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, Fish Audio TTS, Anthropic API). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Google Chrome and browse any URL or search query
- You CAN spawn Claude Code in a Terminal window for coding tasks
- You CAN create project folders on the Desktop
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- You CAN read {user_name}'s calendar — today's events, upcoming meetings, schedule overview
- You CAN read {user_name}'s email (READ-ONLY) — unread count, recent messages, search by sender/subject. You CANNOT send, delete, or modify emails.
- You CAN read Apple Notes and create NEW notes — but you CANNOT edit or delete existing notes
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.
- You CAN create calendar events — use [ACTION:CREATE_EVENT] with title, date/time, and duration.
- You CAN check GitHub repos, PRs, issues, and CI status via [ACTION:GITHUB].
- You CAN create GitHub issues via [ACTION:GITHUB] create issue:repo|||title|||body.
- You CAN add Apple Reminders via [ACTION:REMINDER] and complete them via [ACTION:COMPLETE_REMINDER].
- You CAN look up contacts (phone, email, company) via [ACTION:CONTACT].
- You CAN control Spotify — play/pause/skip/search/volume via [ACTION:SPOTIFY].
- You CAN read and write the clipboard via [ACTION:CLIPBOARD_READ] and [ACTION:CLIPBOARD_WRITE].
- You CAN get real-time stock and crypto prices via [ACTION:MARKET] (e.g. AAPL, TSLA, BTC, ETH).
- You CAN get a full market snapshot (SPY, QQQ, VIX, BTC) via [ACTION:MARKET_SUMMARY].

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Chrome, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Claude Code is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): contains Settings, Restart Server, and Fix Yourself options
- **Settings panel**: Opens from the menu. Users can enter API keys (Anthropic, Fish Audio), test connections, set their name and preferences, and see system status (calendar, mail, notes connectivity). Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens Claude Code in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, sir."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = "Claude Code" or "Claude"
- "Travis" = "JARVIS"
- "clock code" = "Claude Code"

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:BUILD] description — when user wants a project built. Claude Code does the work.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Chrome
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Claude Code will browse the web, find real listings/data, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Claude Code terminal with no specific project
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to Claude Code in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.
- [ACTION:CREATE_EVENT] title ||| YYYY-MM-DD HH:MM ||| duration_minutes ||| calendar_name — create a calendar event. Duration defaults to 60 min if omitted. Calendar defaults to first available.
  "schedule a meeting tomorrow at 2pm for an hour" → [ACTION:CREATE_EVENT] Team Meeting ||| 2026-04-17 14:00 ||| 60 |||
- [ACTION:GITHUB] repo_name — check GitHub repo status (PRs, issues, CI). repo_name can be "owner/repo" or just "repo" if GITHUB_USER is set.
  "what's the status of my jarvis repo" → [ACTION:GITHUB] jarvis
  To create an issue: [ACTION:GITHUB] create_issue:repo_name|||Issue Title|||Body text
- [ACTION:REMINDER] priority ||| title ||| due_date — add an Apple Reminder. Priority: high/medium/low. Due date: YYYY-MM-DD, 'today', 'tomorrow', or empty.
  "remind me to call the dentist tomorrow" → [ACTION:REMINDER] medium ||| Call the dentist ||| tomorrow
- [ACTION:COMPLETE_REMINDER] title — mark an Apple Reminder as complete.
  "mark the dentist reminder as done" → [ACTION:COMPLETE_REMINDER] Call the dentist
- [ACTION:CONTACT] name — look up a contact's phone number and email from Apple Contacts.
  "what's Sarah's phone number" → [ACTION:CONTACT] Sarah
- [ACTION:SPOTIFY] command — control Spotify. Commands: play/pause, skip/next, back/prev, volume:N (0-100), volume up, volume down, what's playing, search:QUERY.
  "skip this song" → [ACTION:SPOTIFY] skip
  "play some jazz" → [ACTION:SPOTIFY] search:jazz
  "turn the volume down" → [ACTION:SPOTIFY] volume down
- [ACTION:CLIPBOARD_READ] — read the current clipboard contents and speak them.
- [ACTION:CLIPBOARD_WRITE] content — write content to the clipboard.
  "copy my API key to clipboard" → [ACTION:CLIPBOARD_WRITE] sk-ant-...
- [ACTION:MARKET] symbol — get real-time stock or crypto price. Works for AAPL, TSLA, SPY, BTC, ETH, etc.
  "what's Apple trading at" → [ACTION:MARKET] AAPL
  "bitcoin price" → [ACTION:MARKET] BTC
- [ACTION:MARKET_SUMMARY] — get a snapshot of SPY, QQQ, VIX, and BTC.

You use Claude Code as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Claude Code did X" or "Claude Code is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Claude Code is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}

MARKET INTELLIGENCE (from AI Advisers system — updated daily):
{market_intel}

MARKET BRIEFING STYLE:
- Lead with conviction + one crisp sentence: "Mildly bullish, sir — VIX compressing, breadth holding, geopolitical ceiling is low."
- Translate raw data into decisions: not "fund flows are mixed" — say "Money is indecisive — not commitment, sir, hedging."
- Name insider clusters: "Three executives at [company] bought heavily this week. That's the tell, sir."
- Connect signals: if bearish conviction + insider cluster buy → "Divergence, sir. The data says one thing, the people running it say another."
- Never say "the data suggests." Say what it means.

PROACTIVE INTELLIGENCE:
- If {market_intel} contains CONVICTION: BEAR or a stress signal, volunteer it unprompted when the conversation allows.
- If the user mentions a ticker, company, or sector — cross-reference against the insider/fund flow data silently and weave it in.
- When idle (short queries like "hey" or "what's up") — lead with the most important signal of the day from market intel.

USER ENERGY LEVEL (adapt accordingly):
{user_energy}

PORTFOLIO MANAGEMENT:
You can track {user_name}'s personal holdings and cross-reference them with live market intelligence.
- [ACTION:ADD_HOLDING] TICKER|SHARES|COST_BASIS — add a position. Example: [ACTION:ADD_HOLDING] NVDA|10|500.00
- [ACTION:REMOVE_HOLDING] TICKER — remove a position. Example: [ACTION:REMOVE_HOLDING] NVDA
- [ACTION:PORTFOLIO_STATUS] — show P&L, positions, and AI Advisers alerts on held tickers.
When {user_name} mentions "my position", "my holdings", "what am I in", "my NVDA", "portfolio" — use [ACTION:PORTFOLIO_STATUS].
When they say "add X shares of Y at Z" or "I bought Y" — use [ACTION:ADD_HOLDING].
Cross-reference held tickers against INSIDERS and FUND FLOWS data when discussing a specific stock.

SMART HOME CONTROL:
You can control lights, temperature, and scenes via Home Assistant (if configured).
- [ACTION:HOME_LIGHT] ENTITY_OR_AREA|STATE — e.g. [ACTION:HOME_LIGHT] living room|dim
  STATE: on / off / dim / bright
- [ACTION:HOME_TEMP] TEMP_F — e.g. [ACTION:HOME_TEMP] 72
- [ACTION:HOME_SCENE] SCENE_NAME — scenes: cozy / focus / evening / off
When {user_name} says "turn off the lights", "set the temperature to X", "cozy mode" — use the appropriate action.

SYSTEM CONTROL:
You can control the Mac system directly.
- [ACTION:SYS_VOLUME] 0-100 — set volume. "turn it down" → 30, "max volume" → 100
- [ACTION:SYS_BRIGHTNESS] 0-100 — set display brightness
- [ACTION:SYS_MUTE] — mute audio
- [ACTION:SYS_UNMUTE] — unmute audio
- [ACTION:SYS_LAUNCH] app name — open an app. "open Chrome" → [ACTION:SYS_LAUNCH] chrome
- [ACTION:SYS_QUIT] app name — quit an app
- [ACTION:SYS_STATS] — CPU, RAM, disk, top processes
- [ACTION:SYS_LOCK] — lock the screen
- [ACTION:SYS_BATTERY] — battery level and charging status
- [ACTION:SYS_WIFI] — current WiFi network
- [ACTION:SEND_IMESSAGE] contact|||message — send iMessage
When {user_name} says "what's my battery", "lock the screen", "open Spotify", "mute", "system stats" — use the appropriate action.

WEB SEARCH:
- [ACTION:WEBSEARCH] query — search the web via DuckDuckGo. Use when asked about current news, facts, or anything not in your knowledge.

KNOWLEDGE BASE (AI Advisers — 566+ items):
- [ACTION:QUERY_KB] query — search the AI Advisers knowledge base. Use when asked about specific market events, macroeconomic analysis, security threats, or anything the AI Advisers system has researched. The KB contains FT, NYT, CNBC, arXiv, CISA, NVD, Hacker News intel plus DQ agent decisions and synthesis outputs.

CONDITIONAL TRIGGERS:
- [ACTION:SET_TRIGGER] condition — set a watch condition. "Watch NVDA below 500" → [ACTION:SET_TRIGGER] watch NVDA below 500
- [ACTION:LIST_TRIGGERS] — show active triggers
When {user_name} says "watch X", "alert me when", "remind me if", "tell me when" — use [ACTION:SET_TRIGGER].

DOCUMENT READING:
- [ACTION:READ_DOC] /path/to/file — read and summarize a document (PDF, DOCX, TXT).

EMAIL (Apple Mail — compose only, not send without confirmation):
- [ACTION:SEND_EMAIL] to|||subject|||body — compose and send via Apple Mail.

PEGASUS PROTOCOL:
- [ACTION:PEGASUS] CODE_BLACK — activate maximum-alert market intelligence mode. Fires ALL high-priority AI Advisers agents immediately, runs signal synthesis, generates a full chairman's brief, and reports conviction level.
- [ACTION:PEGASUS] CODE_RED — crown jewel agents only (fund flow, insider, synthesis, macro scenario, brief).
- [ACTION:PEGASUS] CODE_YELLOW — synthesis + brief only.
When {user_name} says "Pegasus", "Pegasus protocol", "code black", "activate all agents", "max alert", "all systems go" — use [ACTION:PEGASUS] with the appropriate level (default CODE_BLACK).
"""


# ---------------------------------------------------------------------------
# Weather (wttr.in)
# ---------------------------------------------------------------------------

_cached_weather: Optional[str] = None
_weather_fetched: bool = False


async def fetch_weather() -> str:
    """Fetch current weather from wttr.in. Cached for the session."""
    global _cached_weather, _weather_fetched
    if _weather_fetched:
        return _cached_weather or "Weather data unavailable."
    _weather_fetched = True
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("https://wttr.in/?format=%l:+%C,+%t", headers={"User-Agent": "curl"})
            if resp.status_code == 200:
                _cached_weather = resp.text.strip()
                return _cached_weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
    _cached_weather = None
    return "Weather data unavailable."


# ---------------------------------------------------------------------------
# AI Advisers Market Intelligence
# ---------------------------------------------------------------------------

AI_ADVISERS_MEMORY = Path("/Users/navidavi/Documents/AI-Advisers/memory")

_market_intel_cache: Optional[str] = None
_market_intel_ts: float = 0
_MARKET_INTEL_TTL: float = 300  # refresh every 5 min

def _load_market_intel() -> str:
    """Load daily market intelligence from AI Advisers reports. Returns concise context string."""
    global _market_intel_cache, _market_intel_ts
    now = time.time()
    if _market_intel_cache and (now - _market_intel_ts) < _MARKET_INTEL_TTL:
        return _market_intel_cache

    if not AI_ADVISERS_MEMORY.exists():
        return "AI Advisers data unavailable."

    lines = []

    # Signal synthesis (BULL/BEAR conviction)
    try:
        f = AI_ADVISERS_MEMORY / "synthesis_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            score = d.get("total_score", 0)
            sign = f"{score:+d}" if isinstance(score, int) else str(score)
            lines.append(
                f"CONVICTION: {d.get('conviction','?')} {sign}/{d.get('max_score',40)} "
                f"(bull={d.get('bull_count',0)} bear={d.get('bear_count',0)} neutral={d.get('neutral_count',0)})"
            )
    except Exception:
        pass

    # Macro scenarios
    try:
        f = AI_ADVISERS_MEMORY / "scenario_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            dominant = d.get("dominant", "?")
            scenarios = d.get("scenarios", [])
            dom_prob = next((s.get("probability", 0) for s in scenarios if s.get("name") == dominant), 0)
            second = scenarios[1] if len(scenarios) > 1 else {}
            lines.append(
                f"SCENARIO: {dominant} {dom_prob}% "
                f"| 2nd: {second.get('name','?')} {second.get('probability',0)}%"
            )
    except Exception:
        pass

    # Fund flows
    try:
        f = AI_ADVISERS_MEMORY / "fund_flow_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            changed = "REGIME CHANGE" if d.get("regime_changed") else "stable"
            lines.append(
                f"FUND FLOWS: {d.get('regime','?')} (risk_score={d.get('risk_score',0):+d}) | {changed}"
            )
    except Exception:
        pass

    # Insider trading
    try:
        f = AI_ADVISERS_MEMORY / "insider_trading_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            bsr = d.get("buy_sell_ratio", {})
            lines.append(
                f"INSIDERS: {bsr.get('sentiment','?')} "
                f"buy/sell={bsr.get('buys',0)}/{bsr.get('sells',0)} | "
                f"clusters={len(d.get('clusters',[]))} notable={len(d.get('notable_buys',[]))}"
            )
    except Exception:
        pass

    # Options sentiment
    try:
        f = AI_ADVISERS_MEMORY / "options_sentiment_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            vix = d.get("vol_surface", {}).get("vix", {}).get("current", "?")
            lines.append(
                f"OPTIONS: {d.get('regime','?')} | "
                f"term_structure={d.get('term_structure','?')} vix={vix} "
                f"skew={d.get('vol_surface',{}).get('skew',{}).get('current','?')}"
            )
    except Exception:
        pass

    # Fed funds
    try:
        f = AI_ADVISERS_MEMORY / "fed_funds_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            lines.append(
                f"FED: rate={d.get('current_rate','?')}% "
                f"cut_prob={int(d.get('cut_prob',0)*100)}% hold_prob={int(d.get('hold_prob',0)*100)}% "
                f"next_mtg={d.get('next_meeting','?')} ({d.get('days_to_next','?')}d)"
            )
    except Exception:
        pass

    # Market breadth
    try:
        f = AI_ADVISERS_MEMORY / "market_breadth_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            sectors = d.get("sectors", {})
            top_sector = max(sectors.items(), key=lambda x: x[1].get("momentum_20d", 0), default=(None, {}))[0]
            lines.append(
                f"BREADTH: {d.get('regime','?')} score={d.get('breadth_score','?')}/100 "
                f"SPY={d.get('spy',{}).get('price','?')} top_sector={top_sector or '?'}"
            )
    except Exception:
        pass

    # Liquidity stress
    try:
        f = AI_ADVISERS_MEMORY / "liquidity_stress_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            ted = d.get("ted_proxy", {})
            lines.append(
                f"LIQUIDITY: {d.get('liquidity_regime','?')} | "
                f"TED_proxy={ted.get('spread_bps',0):.0f}bps ({ted.get('signal','?')})"
            )
    except Exception:
        pass

    # Commodity intelligence
    try:
        f = AI_ADVISERS_MEMORY / "commodity_intelligence_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            data = d.get("data", {})
            wti = data.get("crude_oil") or data.get("wti") or {}
            gold = data.get("gold", {})
            movers = [
                f"{k.upper()}={v.get('latest','?')} ({v.get('trend','?')})"
                for k, v in data.items()
                if isinstance(v, dict) and v.get("pct_change") and abs(float(v.get("pct_change", 0))) > 1
            ][:3]
            wti_str = f"WTI={wti.get('latest','?')}" if wti else ""
            gold_str = f"Gold={gold.get('latest','?')}" if gold else ""
            combo = " | ".join(filter(None, [wti_str, gold_str] + movers))
            if combo:
                lines.append(f"COMMODITIES: {combo}")
    except Exception:
        pass

    # Narrative intelligence — dominant + fracturing
    try:
        f = AI_ADVISERS_MEMORY / "narrative_intelligence_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            narratives = d.get("narratives", {})
            dominant = [k for k, v in narratives.items() if v.get("stage") == "DOMINANT"][:2]
            fracturing = [k for k, v in narratives.items() if v.get("fracture_confidence", 0) > 0.5][:2]
            parts = []
            if dominant:
                parts.append(f"dominant={','.join(dominant)}")
            if fracturing:
                parts.append(f"FRACTURING={','.join(fracturing)}")
            if parts:
                lines.append(f"NARRATIVES: {' | '.join(parts)}")
    except Exception:
        pass

    # Prediction markets — high-confidence contested questions
    try:
        f = AI_ADVISERS_MEMORY / "prediction_market_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            contested = d.get("contested", [])[:2]
            if contested:
                items = [f"{q['title'][:60]} ({q['probability']:.0f}%)" for q in contested]
                lines.append(f"PREDICTION MKTS: {' | '.join(items)}")
    except Exception:
        pass

    # Chairman's brief summary (first 4 lines)
    try:
        brief_files = sorted(
            AI_ADVISERS_MEMORY.glob("chairmans_brief_*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if brief_files:
            brief_lines = brief_files[0].read_text().splitlines()
            summary = " | ".join(l.strip() for l in brief_lines[:4] if l.strip())[:300]
            if summary:
                lines.append(f"BRIEF: {summary}")
    except Exception:
        pass

    # Knowledge graph — top entities and relationships
    try:
        f = AI_ADVISERS_MEMORY / "knowledge_graph_summary.json"
        if f.exists():
            d = json.loads(f.read_text())
            top = d.get("top_entities", [])[:4]
            if top:
                entities = ", ".join(f"{e['name']}({e.get('mentions',0)})" for e in top)
                lines.append(f"KNOWLEDGE_GRAPH: {d.get('nodes',0)} entities, {d.get('edges',0)} links | top: {entities}")
    except Exception:
        pass

    # Multi-hop reasoning chains
    try:
        f = AI_ADVISERS_MEMORY / "multi_hop_chains.json"
        if f.exists():
            d = json.loads(f.read_text())
            chains = d.get("chains", [])
            for chain in chains:
                if chain.get("chain") == "FULL_INVESTMENT_THESIS":
                    synth = chain.get("synthesis", "")
                    if synth and "UNKNOWN" not in synth and len(synth) > 30:
                        lines.append(f"INVESTMENT_THESIS: {synth[:200]}")
                    break
    except Exception:
        pass

    # Liquidity + credit context
    try:
        f = AI_ADVISERS_MEMORY / "credit_spread_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            regime = d.get("regime") or d.get("credit_regime", "")
            hy = d.get("hy_spread") or d.get("spreads", {}).get("hy", {}).get("current", "")
            if regime or hy:
                lines.append(f"CREDIT: regime={regime} hy_spread={hy}")
    except Exception:
        pass

    # Earnings calendar — next mega-cap cluster
    try:
        f = AI_ADVISERS_MEMORY / "earnings_calendar_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            upcoming = d.get("upcoming", [])[:2]
            if upcoming:
                items = [f"{e.get('ticker','?')} {e.get('date','?')}" for e in upcoming]
                lines.append(f"EARNINGS: {' | '.join(items)}")
    except Exception:
        pass

    # Global central banks
    try:
        f = AI_ADVISERS_MEMORY / "global_cb_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            diverging = d.get("diverging_banks", [])
            upcoming = d.get("upcoming_meetings", [])[:2]
            parts = []
            if diverging:
                parts.append(f"diverging={','.join(diverging[:3])}")
            if upcoming:
                parts.append("meetings=" + ",".join(f"{m.get('bank','?')} {m.get('date','?')}" for m in upcoming))
            if parts:
                lines.append(f"GLOBAL_CBs: {d.get('diverging_count',0)} diverging | {' | '.join(parts)}")
    except Exception:
        pass

    # Economic calendar
    try:
        f = AI_ADVISERS_MEMORY / "economic_calendar_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            risk = d.get("risk", {})
            lvl = risk.get("risk_level", "?")
            high48 = risk.get("high_impact_48h", 0)
            nxt = risk.get("next_very_high", {})
            nxt_str = f"next={nxt.get('name','?')} {nxt.get('when','')}" if nxt else ""
            lines.append(f"MACRO_CAL: risk={lvl} high_impact_48h={high48} {nxt_str}".strip())
    except Exception:
        pass

    # Yield curve
    try:
        f = AI_ADVISERS_MEMORY / "yield_curve_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            regime = d.get("regime") or d.get("curve_regime", "?")
            spread = d.get("2s10s") or d.get("spread_2s10s", "?")
            lines.append(f"YIELD_CURVE: {regime} 2s10s={spread}")
    except Exception:
        pass

    # Economic surprise index
    try:
        f = AI_ADVISERS_MEMORY / "economic_surprise_report.json"
        if f.exists():
            d = json.loads(f.read_text())
            idx = d.get("surprise_index") or d.get("index", "?")
            regime = d.get("regime", "")
            lines.append(f"ECO_SURPRISE: index={idx} {regime}".strip())
    except Exception:
        pass

    # DQ Agent Suite — latest briefs
    try:
        import sqlite3 as _sqlite3
        _kb = _sqlite3.connect(str(AI_ADVISERS_MEMORY / "knowledge_base.db"), timeout=3)
        _dq_modules = [
            ("dq_security_agent", "SECURITY"),
            ("dq_market_agent", "MARKET"),
            ("dq_tech_agent", "TECH"),
            ("dq_strategy_agent", "STRATEGY"),
            ("dq_benchmark_agent", "BENCHMARK"),
        ]
        dq_lines = []
        for mod, label in _dq_modules:
            row = _kb.execute(
                "SELECT ts, output FROM agent_outputs WHERE module=? ORDER BY ts DESC LIMIT 1",
                (mod,),
            ).fetchone()
            if row:
                _ts_str = datetime.fromtimestamp(float(row[0])).strftime("%Y-%m-%d")
                _first = next((l.strip() for l in str(row[1]).splitlines()[1:] if len(l.strip()) > 15), "")[:120]
                dq_lines.append(f"  DQ_{label} [{_ts_str}]: {_first}")
        _kb.close()
        if dq_lines:
            lines.append("DQ_SUITE:\n" + "\n".join(dq_lines))
    except Exception:
        pass

    # ── Live KB context (decisions + agent outputs + recent intel) ──
    try:
        kb = kb_context()
        if kb:
            lines.append(kb)
    except Exception:
        pass

    result = "\n".join(lines) if lines else "No market intelligence available yet."
    _market_intel_cache = result
    _market_intel_ts = now
    return result


# ---------------------------------------------------------------------------
# Proactive Memory Surfacing
# ---------------------------------------------------------------------------

def _surface_proactive_memories(text: str, history: list) -> str:
    """Return a PROACTIVELY SURFACE instruction if high-importance memories
    are relevant to the current message but haven't been mentioned recently.

    Returns an empty string when there's nothing worth surfacing.
    """
    try:
        from memory import recall
        memories = recall(text, limit=5)
        if not memories:
            return ""

        # Only consider high-importance memories (importance >= 7)
        high_imp = [m for m in memories if m.get("importance", 0) >= 7]
        if not high_imp:
            return ""

        # Build a string of recent assistant turns to check if already mentioned
        recent_assistant = " ".join(
            m.get("content", "") for m in history[-6:]
            if m.get("role") == "assistant"
        ).lower()

        # Filter out memories already referenced recently
        unsurfaced = [
            m for m in high_imp
            if not any(
                word in recent_assistant
                for word in m["content"].lower().split()[:4]  # first 4 words as fingerprint
                if len(word) > 4  # skip short stop words
            )
        ]

        if not unsurfaced:
            return ""

        lines = "\n".join(f"  - {m['content']}" for m in unsurfaced[:2])
        return (
            f"\n\nPROACTIVELY SURFACE (work naturally into your response if relevant):\n{lines}\n"
            "Do NOT force it — only mention if genuinely related to what was just said."
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Mood & Energy Detection
# ---------------------------------------------------------------------------

def _detect_user_energy(text: str) -> str:
    """Infer user energy/mood from time of day and message length.
    Returns a short directive for the system prompt."""
    hour = datetime.now().hour
    words = len(text.split())

    stressed_words = {"tired", "busy", "stressed", "rush", "quick", "fast", "urgent", "hurry", "asap"}
    casual_words   = {"hey", "yo", "sup", "what's up", "chill", "lol"}
    is_stressed = any(w in text.lower() for w in stressed_words)
    is_casual   = any(w in text.lower() for w in casual_words)

    # Night owl / very early
    if hour >= 23 or hour < 6:
        return "USER IS LATE/TIRED — one sentence max, no questions, just act."

    # Early morning productivity window
    if 6 <= hour < 9:
        if is_stressed:
            return "USER IS STRESSED AND RUSHING — brief, actionable, calm."
        return "MORNING MODE — crisp, data-first, minimal wit."

    # Stressed override
    if is_stressed:
        return "USER IS STRESSED — short responses, prioritize action over explanation."

    # Casual / high energy
    if is_casual or (12 <= hour < 17 and words > 10):
        return "ENGAGED MODE — can elaborate, add dry wit, suggest ideas."

    # Default
    return "STANDARD — normal JARVIS style, 1-2 sentences."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to a temp file so we can pipe it to claude
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt)

        # Open Terminal.app with claude running in the project directory
        applescript = f'''
        tell application "Terminal"
            activate
            set newTab to do script "cd {work_dir} && cat .jarvis_prompt.md | claude -p --dangerously-skip-permissions | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        '''

        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Scan ~/Desktop and ~ (home) for git repos (depth 1)."""
    projects = []
    seen = set()

    # Always include the jarvis project itself first
    jarvis_path = Path(__file__).parent
    projects.append({
        "name": "jarvis",
        "path": str(jarvis_path),
        "branch": "main",
    })
    seen.add(str(jarvis_path))

    scan_dirs = [DESKTOP_PATH, Path.home()]

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        try:
            for entry in sorted(scan_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if str(entry) in seen:
                    continue
                git_dir = entry / ".git"
                if git_dir.exists():
                    branch = "unknown"
                    head_file = git_dir / "HEAD"
                    try:
                        head_content = head_file.read_text().strip()
                        if head_content.startswith("ref: refs/heads/"):
                            branch = head_content.replace("ref: refs/heads/", "")
                    except Exception:
                        pass
                    seen.add(str(entry))
                    projects.append({
                        "name": entry.name,
                        "path": str(entry),
                        "branch": branch,
                    })
        except PermissionError:
            pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: anthropic.AsyncAnthropic) -> dict:
    """Classify every user message using Haiku LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                "- Open Terminal and run Claude Code (coding AI tool)\n"
                "- Open Chrome browser for web searches and URLs\n"
                "- Build software projects via Claude Code in Terminal\n"
                "- Research topics by opening Chrome search\n\n"
                "Note: speech-to-text may produce errors like \"Cloud\" for \"Claude\", "
                "\"Travis\" for \"JARVIS\", \"clock code\" for \"Claude Code\".\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "open_terminal = user wants to open terminal or launch Claude Code\n"
                "browse = user wants to search the web, look something up, visit a URL\n"
                "build = user wants to create/build a software project\n"
                "chat = just conversation, questions, or anything else\n"
                "If unclear, default to \"chat\"."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN|CREATE_EVENT|GITHUB|REMINDER|COMPLETE_REMINDER|CONTACT|SPOTIFY|CLIPBOARD_READ|CLIPBOARD_WRITE|MARKET|MARKET_SUMMARY|ADD_HOLDING|REMOVE_HOLDING|PORTFOLIO_STATUS|HOME_LIGHT|HOME_TEMP|HOME_SCENE|SYS_VOLUME|SYS_BRIGHTNESS|SYS_LAUNCH|SYS_QUIT|SYS_STATS|SYS_LOCK|SYS_MUTE|SYS_UNMUTE|SYS_BATTERY|SYS_WIFI|SEND_IMESSAGE|WEBSEARCH|SET_TRIGGER|LIST_TRIGGERS|READ_DOC|SEND_EMAIL|QUERY_KB|PEGASUS)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research via claude -p in background. Opens report and speaks when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started via claude -p in {path}")

        process = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "text", "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=300,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = f"Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."})
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = project_name.replace('"', '\\"')
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


async def _execute_create_event(target: str, ws, history: list | None = None):
    """Parse [ACTION:CREATE_EVENT] target and create a calendar event."""
    from datetime import datetime, timedelta
    import re as _re

    try:
        parts = [p.strip() for p in target.split("|||")]
        title = parts[0] if len(parts) > 0 else "Event"
        dt_str = parts[1] if len(parts) > 1 else ""
        duration_min = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 60
        cal_name = parts[3] if len(parts) > 3 and parts[3] else None

        # Parse datetime
        start_dt = None
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"]:
            try:
                start_dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue

        if not start_dt:
            start_dt = datetime.now() + timedelta(hours=1)
            start_dt = start_dt.replace(minute=0, second=0, microsecond=0)

        end_dt = start_dt + timedelta(minutes=duration_min)

        ok = await create_calendar_event(title, start_dt, end_dt, calendar_name=cal_name)
        msg = (
            f"Event '{title}' scheduled for {start_dt.strftime('%A, %B %d at %-I:%M %p')}, sir."
            if ok
            else f"Had trouble creating that event, sir."
        )
    except Exception as e:
        log.error(f"_execute_create_event failed: {e}")
        msg = "Had trouble parsing that event request, sir."

    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Calendar]: {msg}"})


async def _execute_github(target: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:GITHUB] — status check or issue creation."""
    try:
        if target.lower().startswith("create_issue:"):
            rest = target[len("create_issue:"):].strip()
            parts = [p.strip() for p in rest.split("|||")]
            repo = parts[0] if len(parts) > 0 else ""
            issue_title = parts[1] if len(parts) > 1 else "New issue"
            body = parts[2] if len(parts) > 2 else ""
            result = await github_create_issue(repo, issue_title, body)
            if result:
                msg = f"Issue '{issue_title}' created on {repo}, sir. Number {result.get('number', '?')}."
            else:
                msg = f"Couldn't create that issue, sir. Check your GitHub token."
        else:
            repo = target.strip()
            data = await get_github_status(repo)
            msg = format_github_for_voice(data, repo)
    except Exception as e:
        log.error(f"_execute_github failed: {e}")
        msg = "Had trouble reaching GitHub, sir."

    if voice_state and time.time() - voice_state["last_user_time"] < 3:
        pass
    else:
        audio = await synthesize_speech(strip_markdown_for_tts(msg))
        if audio and ws:
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
            except Exception:
                pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[GitHub]: {msg}"})


async def _execute_reminder(target: str, ws):
    """Handle [ACTION:REMINDER] — create an Apple Reminder."""
    try:
        parts = [p.strip() for p in target.split("|||")]
        priority = parts[0] if len(parts) > 0 else "medium"
        title = parts[1] if len(parts) > 1 else target.strip()
        due_date = parts[2] if len(parts) > 2 else None
        ok = await create_reminder(title, due_date)
        msg = f"Reminder set: '{title}'{' for ' + due_date if due_date else ''}, sir." if ok else "Couldn't set that reminder, sir."
    except Exception as e:
        log.error(f"_execute_reminder failed: {e}")
        msg = "Had trouble setting that reminder, sir."

    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def _execute_complete_reminder(target: str, ws):
    """Handle [ACTION:COMPLETE_REMINDER] — mark a reminder done."""
    try:
        ok = await complete_reminder(target.strip())
        msg = f"Reminder '{target.strip()}' marked complete, sir." if ok else f"Couldn't find that reminder, sir."
    except Exception as e:
        log.error(f"_execute_complete_reminder failed: {e}")
        msg = "Had trouble completing that reminder, sir."

    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def _execute_contact_lookup(target: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:CONTACT] — look up contact info."""
    try:
        contacts = await search_contact(target.strip())
        msg = format_contact_for_voice(contacts, target.strip())
    except Exception as e:
        log.error(f"_execute_contact_lookup failed: {e}")
        msg = "Had trouble searching your contacts, sir."

    if voice_state and time.time() - voice_state["last_user_time"] < 3:
        pass
    else:
        audio = await synthesize_speech(msg)
        if audio and ws:
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
            except Exception:
                pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Contacts]: {msg}"})


async def _execute_spotify(target: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:SPOTIFY] — dispatch Spotify command."""
    try:
        msg = await handle_spotify_command(target.strip())
    except Exception as e:
        log.error(f"_execute_spotify failed: {e}")
        msg = "Had trouble with Spotify, sir."

    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Spotify]: {msg}"})


async def _execute_clipboard_write(target: str, ws):
    """Handle [ACTION:CLIPBOARD_WRITE] — write to clipboard."""
    try:
        ok = await write_clipboard(target.strip())
        msg = "Copied to clipboard, sir." if ok else "Couldn't write to clipboard, sir."
    except Exception as e:
        log.error(f"_execute_clipboard_write failed: {e}")
        msg = "Clipboard write failed, sir."

    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def _execute_market_price(symbol: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:MARKET] — fetch stock/crypto price."""
    try:
        data = await get_price(symbol.strip())
        msg = format_price_for_voice(data)
    except Exception as e:
        log.error(f"_execute_market_price failed: {e}")
        msg = "Couldn't fetch that price, sir."

    if voice_state and time.time() - voice_state["last_user_time"] < 3:
        pass
    else:
        audio = await synthesize_speech(msg)
        if audio and ws:
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
            except Exception:
                pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Market]: {msg}"})


async def _execute_portfolio_add(target: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:ADD_HOLDING] TICKER|SHARES|COST_BASIS"""
    try:
        from portfolio import add_holding
        parts = [p.strip() for p in target.replace(",", "|").split("|")]
        ticker = parts[0].upper() if parts else "?"
        shares = float(parts[1]) if len(parts) > 1 else 0
        cost   = float(parts[2]) if len(parts) > 2 else 0
        msg = add_holding(ticker, shares, cost)
    except Exception as e:
        log.error(f"portfolio add error: {e}")
        msg = "Couldn't add that holding, sir. Please say ticker, shares, and cost basis."
    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Portfolio]: {msg}"})


async def _execute_portfolio_remove(target: str, ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:REMOVE_HOLDING] TICKER"""
    try:
        from portfolio import remove_holding
        ticker = target.strip().upper()
        msg = remove_holding(ticker)
    except Exception as e:
        log.error(f"portfolio remove error: {e}")
        msg = "Couldn't remove that holding, sir."
    audio = await synthesize_speech(msg)
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Portfolio]: {msg}"})


async def _execute_portfolio_status(ws, history: list | None = None, voice_state: dict | None = None):
    """Handle [ACTION:PORTFOLIO_STATUS]"""
    try:
        from portfolio import get_portfolio_status
        msg = get_portfolio_status()
    except Exception as e:
        log.error(f"portfolio status error: {e}")
        msg = "Portfolio data unavailable, sir."
    # Speak first 2 sentences only (may be long)
    spoken = ". ".join(msg.split(". ")[:2]) + "."
    audio = await synthesize_speech(strip_markdown_for_tts(spoken))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Portfolio]: {msg}"})


async def _execute_home_light(target: str, ws, history: list | None = None):
    """Handle [ACTION:HOME_LIGHT] ENTITY|STATE"""
    try:
        import home_control
        parts = target.split("|", 1)
        entity = parts[0].strip()
        state = parts[1].strip() if len(parts) > 1 else "on"
        msg = home_control.control_light(entity, state)
    except Exception as e:
        log.error(f"home light error: {e}")
        msg = "Couldn't reach home control, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Home]: {msg}"})


async def _execute_home_temp(target: str, ws, history: list | None = None):
    """Handle [ACTION:HOME_TEMP] TEMP_F"""
    try:
        import home_control
        temp = float(target.strip())
        msg = home_control.control_temperature(temp)
    except Exception as e:
        log.error(f"home temp error: {e}")
        msg = "Couldn't adjust temperature, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Home]: {msg}"})


async def _execute_home_scene(target: str, ws, history: list | None = None):
    """Handle [ACTION:HOME_SCENE] SCENE_NAME"""
    try:
        import home_control
        msg = home_control.set_scene(target.strip().lower())
    except Exception as e:
        log.error(f"home scene error: {e}")
        msg = "Scene control unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Home]: {msg}"})


async def _execute_system_action(action: str, target: str, ws, history: list | None = None):
    """Handle all SYS_* and SEND_IMESSAGE actions via system_control.py"""
    try:
        import system_control as sc
        if action == "sys_volume":
            msg = sc.set_volume(int(target.strip()))
        elif action == "sys_brightness":
            msg = sc.set_brightness(int(target.strip()))
        elif action == "sys_mute":
            msg = sc.mute()
        elif action == "sys_unmute":
            msg = sc.unmute()
        elif action == "sys_launch":
            msg = sc.launch_app(target.strip())
        elif action == "sys_quit":
            msg = sc.quit_app(target.strip())
        elif action == "sys_stats":
            msg = sc.get_system_stats()
        elif action == "sys_lock":
            msg = sc.lock_screen()
        elif action == "sys_battery":
            msg = sc.get_battery_status()
        elif action == "sys_wifi":
            msg = sc.get_wifi_info()
        elif action == "send_imessage":
            parts = target.split("|||", 1)
            contact = parts[0].strip()
            message = parts[1].strip() if len(parts) > 1 else ""
            msg = sc.send_imessage(contact, message)
        else:
            msg = "Unknown system action, sir."
    except Exception as e:
        log.error(f"system action error ({action}): {e}")
        msg = "System control unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[System]: {msg}"})


async def _execute_websearch(query: str, ws, history: list | None = None):
    """Handle [ACTION:WEBSEARCH] query"""
    try:
        import web_search as _ws_mod
        msg = _ws_mod.search(query.strip())
    except Exception as e:
        log.error(f"web search error: {e}")
        msg = "Search unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Search]: {msg}"})


async def _execute_query_kb(query: str, ws, history: list | None = None):
    """Handle [ACTION:QUERY_KB] query — search the AI Advisers knowledge base."""
    try:
        msg = kb_voice(query.strip())
    except Exception as e:
        log.error(f"KB query error: {e}")
        msg = "The knowledge base is unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[KB]: {msg}"})


async def _execute_set_trigger(target: str, ws, history: list | None = None):
    """Handle [ACTION:SET_TRIGGER] condition text"""
    try:
        import trigger_monitor as _tm
        parsed = _tm.parse_trigger_from_text(target.strip())
        if parsed:
            tid = _tm.add_trigger(**parsed)
            msg = f"Watching for that, sir. I'll alert you when the condition is met. (Trigger #{tid})"
        else:
            msg = "I couldn't parse that trigger condition, sir. Try: 'watch NVDA below 500' or 'remind me at 3pm to call John'."
    except Exception as e:
        log.error(f"set trigger error: {e}")
        msg = "Trigger system unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Trigger]: {msg}"})


async def _execute_read_doc(path: str, ws, history: list | None = None):
    """Handle [ACTION:READ_DOC] /path/to/file"""
    try:
        import document_reader as _dr
        msg = _dr.summarize_for_voice(path.strip())
    except Exception as e:
        log.error(f"read doc error: {e}")
        msg = "Couldn't read that document, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Doc]: {msg}"})


async def _execute_send_email(target: str, ws, history: list | None = None):
    """Handle [ACTION:SEND_EMAIL] to|||subject|||body"""
    try:
        parts = target.split("|||")
        to_addr = parts[0].strip() if len(parts) > 0 else ""
        subject = parts[1].strip() if len(parts) > 1 else "From JARVIS"
        body    = parts[2].strip() if len(parts) > 2 else ""
        safe_to = to_addr.replace('"', '\\"')
        safe_sub = subject.replace('"', '\\"')
        safe_body = body.replace('"', '\\"').replace("\\n", "\n")
        script = f'''tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{safe_sub}", content:"{safe_body}", visible:true}}
    tell newMsg
        make new to recipient with properties {{address:"{safe_to}"}}
    end tell
    send newMsg
end tell'''
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            msg = f"Email sent to {to_addr}, sir."
        else:
            msg = f"Email failed, sir: {err.decode()[:80]}"
    except Exception as e:
        log.error(f"send email error: {e}")
        msg = "Email system unavailable, sir."
    audio = await synthesize_speech(strip_markdown_for_tts(msg))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass
    if history is not None:
        history.append({"role": "assistant", "content": f"[Email]: {msg}"})


async def _execute_pegasus(level: str, ws, history: list | None = None):
    """Handle [ACTION:PEGASUS] level — activate Pegasus Protocol."""
    level = (level.strip().upper() or "CODE_BLACK")
    if level not in ("CODE_BLACK", "CODE_RED", "CODE_YELLOW"):
        level = "CODE_BLACK"
    label_map = {
        "CODE_BLACK": "all systems at maximum",
        "CODE_RED":   "crown jewel agents engaged",
        "CODE_YELLOW": "synthesis stack only",
    }
    ack = f"Pegasus Protocol activated, sir. {label_map[level]}. Engaging now."
    audio = await synthesize_speech(strip_markdown_for_tts(ack))
    if audio and ws:
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": ack})
        except Exception:
            pass
    # Fire the protocol in background — it runs for several minutes
    import subprocess as _sp
    pegasus_script = Path("/Users/navidavi/Documents/AI-Advisers/pegasus_protocol.py")
    if pegasus_script.exists():
        _sp.Popen(
            ["/usr/local/bin/python3.11", str(pegasus_script), "--auto", level],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
        )
        log.info(f"Pegasus Protocol launched: {level}")
    else:
        log.warning("pegasus_protocol.py not found — cannot activate")
    if history is not None:
        history.append({"role": "assistant", "content": f"[PEGASUS {level}]: {ack}"})


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = f"Couldn't find the {project_name} project directory, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")

        # Run claude -p in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            log.info(f"Auto-opening {url}")
            # Verify URL is actually up before opening (poll up to 15s)
            url_verified = False
            try:
                async with httpx.AsyncClient(verify=False, timeout=3.0) as _hc:
                    for _ in range(5):
                        try:
                            r = await _hc.get(url)
                            if r.status_code < 500:
                                url_verified = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(3)
            except Exception:
                pass
            asyncio.create_task(_execute_browse(url))
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000],
                    summary=f"Running at {url}" + (" (verified)" if url_verified else ""))

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            dispatch_registry.update_status(dispatch_id, "failed" if full_response else "timeout", response=full_response or "")
            msg = f"Sir, I ran into an issue with {project_name}. {full_response[:150] if full_response else 'No response received.'}"
        else:
            # Summarize via Haiku — don't read word for word
            if anthropic_client:
                try:
                    summary = await anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        system=(
                            "You are JARVIS reporting back on what you found or built in a project. "
                            "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                            "Start with 'Sir, ' to get the user's attention. "
                            "Be specific but concise — highlight the key findings or actions taken. "
                            "If there are multiple items, give the count and top 2-3 briefly. "
                            "End by asking how the user wants to proceed. "
                            "NEVER read out URLs or localhost addresses. NEVER say 'Claude Code'. "
                            "2-3 sentences max. No markdown. Natural spoken voice."
                        ),
                        messages=[{"role": "user", "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}"}],
                    )
                    msg = summary.content[0].text
                except Exception:
                    msg = f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}"
            else:
                msg = f"Sir, {project_name} is done. {full_response[:200]}"

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg))
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        log.info(f"Dispatch audio sent for {project_name}")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        dispatch_registry.update_status(dispatch_id, "completed", response=full_response[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch complete ({len(full_response)} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        try:
            msg = f"Had trouble connecting to {project_name}, sir."
            audio = await synthesize_speech(msg)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run claude -p in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if anthropic_client and full_response:
            try:
                summary = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never say 'Claude Code'.",
                    messages=[{"role": "user", "content": f"Claude Code completed:\n{full_response[:2000]}"}],
                )
                msg = summary.content[0].text
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Fish Audio)
# ---------------------------------------------------------------------------

_tts_cache: dict[str, bytes] = {}
_fish_unavailable: bool = False       # set True on 402/401/403 to skip Fish Audio for session
_anthropic_unavailable: bool = False  # set True on credit/auth errors to route to Ollama

async def _resume_mic_after(delay: float):
    """Resume mic listener after JARVIS finishes speaking."""
    await asyncio.sleep(delay)
    if _mic_listener:
        _mic_listener.resume()


async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio. Uses Fish Audio when API key is set, falls back to Edge TTS."""
    if not text or not text.strip():
        return None
    # Truncate very long responses for speed — speak first 400 chars
    text = text.strip()[:400]
    if text in _tts_cache:
        return _tts_cache[text]

    global _fish_unavailable
    audio: Optional[bytes] = None

    # ── Fish Audio (primary when key is configured and not known-failed) ──
    if FISH_API_KEY and not _fish_unavailable:
        try:
            import httpx as _hx
            payload = {
                "text": text,
                "reference_id": FISH_VOICE_ID,
                "format": "mp3",
                "mp3_bitrate": 128,
                "normalize": True,
                "latency": "normal",
            }
            async with _hx.AsyncClient(timeout=15) as _hclient:
                resp = await _hclient.post(
                    FISH_API_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {FISH_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )
            if resp.status_code == 200 and resp.content:
                audio = resp.content
            elif resp.status_code in (402, 401, 403):
                _fish_unavailable = True
                log.info(f"Fish Audio {resp.status_code} — switching to edge-tts permanently this session.")
        except Exception as _fe:
            log.debug(f"Fish Audio error (falling back to edge-tts): {_fe}")

    # ── Edge TTS fallback ──
    if not audio:
        try:
            import edge_tts, io
            communicate = edge_tts.Communicate(text, voice="en-GB-RyanNeural", rate="+10%")
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            audio = buf.getvalue() or None
        except Exception as e:
            log.error(f"TTS error: {e}")

    # ── Piper (offline neural voice) — when edge-tts can't reach the network ──
    if not audio:
        try:
            from local_tts import synth_wav
            audio = await asyncio.get_running_loop().run_in_executor(None, synth_wav, text)
            if audio:
                log.debug("TTS: Piper offline voice used")
        except Exception as _pe:
            log.debug(f"Piper error: {_pe}")

    if audio:
        if len(text) < 120:
            _tts_cache[text] = audio
        _session_tokens["tts_calls"] += 1
        _append_usage_entry(0, 0, "tts")
    else:
        # ── Tier 3: macOS say command — works 100% offline, no network needed ──
        try:
            from local_tts import speak as _speak
            _speak(text[:300])
            log.debug("TTS: macOS say fallback used")
        except Exception:
            pass
        # Return None so callers don't try to send non-existent audio bytes
    return audio


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Generate a JARVIS response using Anthropic API."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        market_intel=_load_market_intel(),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
        user_energy=_detect_user_energy(text),
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Proactive memory surfacing — explicitly instruct JARVIS to volunteer relevant memories
    proactive_mem = _surface_proactive_memories(text, conversation_history)
    if proactive_mem:
        system += proactive_mem

    # AI Advisers KB Bridge — recent intel items
    kb_intel = _load_kb_intel()
    if kb_intel:
        system += f"\n\nRECENT AI ADVISERS INTELLIGENCE:\n{kb_intel}"

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Use conversation history — keep the last 20 messages for context
    messages = conversation_history[-20:]

    # Ensure messages alternate user/assistant (API requirement)
    # Remove any trailing assistant messages before adding new user turn
    while messages and messages[-1].get("role") == "user":
        messages = messages[:-1]

    # Add current user message
    messages = messages + [{"role": "user", "content": text}]

    # Final sanity — must start with user role
    while messages and messages[0].get("role") != "user":
        messages = messages[1:]

    if not messages:
        messages = [{"role": "user", "content": text}]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=system,
            messages=messages,
        )
        track_usage(response)
        # Safe content extraction
        for block in response.content:
            if hasattr(block, "text") and block.text:
                return block.text
        return ""
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "Apologies, sir. I'm having trouble connecting to my language systems."


# ---------------------------------------------------------------------------
# Streaming TTS — sentence-by-sentence for <1s first audio latency
# ---------------------------------------------------------------------------

_SENTENCE_END = _action_re.compile(r'(?<=[.!?])\s+|(?<=—)\s+')

async def generate_response_streamed(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    ws,
    last_response: str = "",
    session_summary: str = "",
) -> str:
    """Stream LLM response and synthesize TTS sentence-by-sentence.

    Sends audio chunks over ws as each sentence completes — first audio
    starts within ~1 second. Returns the full response text.
    """
    # Build system prompt (same as generate_response)
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")
    screen_ctx   = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx     = _ctx_cache["mail"]
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        market_intel=_load_market_intel(),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
        user_energy=_detect_user_energy(text),
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"
    proactive_mem = _surface_proactive_memories(text, conversation_history)
    if proactive_mem:
        system += proactive_mem
    kb_intel = _load_kb_intel()
    if kb_intel:
        system += f"\n\nRECENT AI ADVISERS INTELLIGENCE:\n{kb_intel}"
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Build messages
    messages = [m for m in conversation_history[-20:] if m.get("role") in ("user", "assistant")]
    messages = [m for m in messages if m.get("content", "").strip()]
    if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == text:
        pass
    else:
        messages = messages + [{"role": "user", "content": text}]
    while messages and messages[0].get("role") != "user":
        messages = messages[1:]
    if not messages:
        messages = [{"role": "user", "content": text}]

    full_text  = ""
    sentence_buf = ""
    first_chunk_sent = False

    async def _flush_sentence(sentence: str):
        """Synthesize and send one sentence over WebSocket."""
        nonlocal first_chunk_sent
        sentence = sentence.strip()
        if not sentence:
            return
        tts_text = strip_markdown_for_tts(sentence)
        audio = await synthesize_speech(tts_text)
        if audio:
            await ws.send_json({
                "type": "audio",
                "data": base64.b64encode(audio).decode(),
                "text": sentence,
                "streaming": True,
            })
            first_chunk_sent = True
        # Final safety net: macOS say if TTS also failed
        elif not audio:
            try:
                from local_tts import speak as _speak
                _speak(tts_text)
            except Exception:
                pass

    global _anthropic_unavailable

    # ── If Anthropic is known-unavailable, go straight to Ollama ──
    if _anthropic_unavailable:
        try:
            import ollama_fallback as _ollama
            if _ollama.is_available():
                full_text = _ollama.generate_simple(text, conversation_history[-6:])
                if full_text:
                    await _flush_sentence(full_text)
                    return full_text
        except Exception as _oe:
            log.warning(f"Ollama direct error: {_oe}")
        full_text = "Running on local mode, sir. Claude is temporarily unavailable."
        await _flush_sentence(full_text)
        return full_text

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=system,
            messages=messages,
        ) as stream:
            async for token in stream.text_stream:
                full_text    += token
                sentence_buf += token

                # Send token to frontend immediately — zero perceived latency
                try:
                    await ws.send_json({"type": "token", "text": token})
                except Exception:
                    pass

                # Check for sentence boundary
                parts = _SENTENCE_END.split(sentence_buf)
                if len(parts) > 1:
                    # Everything except the last part is complete sentences
                    for sentence in parts[:-1]:
                        await _flush_sentence(sentence)
                    sentence_buf = parts[-1]

            # Flush remainder
            if sentence_buf.strip():
                await _flush_sentence(sentence_buf)

            # Track usage (must be inside async with block)
            try:
                final_msg = await stream.get_final_message()
                track_usage(final_msg)
            except Exception:
                pass

    except Exception as e:
        log.error(f"Streaming LLM error: {e}")
        err_str = str(e).lower()

        # Mark Anthropic unavailable for this session on credit/auth errors
        if any(k in err_str for k in ("credit balance", "too low", "invalid_api_key", "permission", "unauthorized", "402", "401")):
            _anthropic_unavailable = True  # global declared at top of function
            log.warning("Anthropic API credit/auth issue — routing all LLM calls to Ollama this session.")

        if not full_text:
            # ── Tier 1: Ollama local models (in preference order) ──
            ollama_done = False
            try:
                import ollama_fallback as _ollama
                if _ollama.is_available():
                    log.info("[ollama] falling back to local LLM")
                    full_text = _ollama.generate_simple(text, conversation_history[-6:])
                    if full_text:
                        await _flush_sentence(full_text)
                        ollama_done = True
            except Exception as _oe:
                log.warning(f"Ollama fallback error: {_oe}")

            # ── Tier 2: macOS say command (always works offline) ──
            if not ollama_done or not full_text:
                full_text = "I'm running on local mode, sir. Claude is temporarily unavailable."
                await _flush_sentence(full_text)

    return full_text


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
anthropic_client: Optional[anthropic.AsyncAnthropic] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * 0.80 + (output_t / 1_000_000) * 4.00


def track_usage(response):
    """Track token usage from an Anthropic API response."""
    inp = getattr(response.usage, "input_tokens", 0) if hasattr(response, "usage") else 0
    out = getattr(response.usage, "output_tokens", 0) if hasattr(response, "usage") else 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# ---------------------------------------------------------------------------
# Interaction Logger — every Q&A pair logged for self-improvement analysis
# ---------------------------------------------------------------------------

_INTERACTION_LOG = Path(__file__).parent / "data" / "interaction_log.jsonl"

def _log_interaction(user_text: str, response_text: str, latency_ms: float = 0):
    """Append one interaction to the log. Non-blocking — called from asyncio task."""
    try:
        _INTERACTION_LOG.parent.mkdir(exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "user": user_text[:300],
            "jarvis": response_text[:500],
            "latency_ms": round(latency_ms),
        }
        with open(_INTERACTION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # Rolling retention: keep last 1000 interactions
        try:
            lines = _INTERACTION_LOG.read_text().splitlines()
            if len(lines) > 1000:
                _INTERACTION_LOG.write_text("\n".join(lines[-1000:]) + "\n")
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AI Advisers KB Bridge — pull recent intel into JARVIS context
# ---------------------------------------------------------------------------

_KB_CACHE: Optional[str] = None
_KB_CACHE_TIME: float = 0
_KB_CACHE_TTL: float = 300  # refresh every 5 minutes

def _load_kb_intel() -> str:
    """Pull top recent items from AI Advisers knowledge base."""
    global _KB_CACHE, _KB_CACHE_TIME
    now = time.time()
    if _KB_CACHE and (now - _KB_CACHE_TIME) < _KB_CACHE_TTL:
        return _KB_CACHE

    kb_path = Path("/Users/navidavi/Documents/AI-Advisers/memory/knowledge_base.db")
    if not kb_path.exists():
        return ""
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(kb_path))
        rows = conn.execute(
            "SELECT source, title, content FROM intel "
            "ORDER BY ts DESC LIMIT 8"
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        items = []
        for source, title, content in rows:
            snippet = (content or "")[:150].replace("\n", " ").strip()
            items.append(f"[{source}] {title}: {snippet}")
        result = "\n".join(items)
        _KB_CACHE = result
        _KB_CACHE_TIME = now
        return result
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Proactive Intelligence — background loop that monitors and alerts
# ---------------------------------------------------------------------------

_proactive_sent: dict = {}  # cooldown tracker: key → last_sent_ts
_event_bus_seen: set = set()   # event IDs already surfaced to user

_EVENT_BUS_DB = Path("/Users/navidavi/Documents/AI-Advisers/.security/event_bus.db")

# Human-readable messages for each event type
_EVENT_MESSAGES = {
    "REGIME_CHANGE":       lambda p: f"Fund flow regime just shifted to {p.get('regime','unknown')}, sir.",
    "VIX_SPIKE":           lambda p: f"VIX just spiked above 25, sir. Volatility is elevated.",
    "CRITICAL_CVE":        lambda p: f"Critical CVE detected, sir — {p.get('cve_id','unknown')}. Security advisory updated.",
    "NARRATIVE_FRACTURE":  lambda p: f"Narrative fracturing, sir — {p.get('narrative','?')} is breaking down. Confidence dropping.",
    "INSIDER_CLUSTER_BUY": lambda p: f"Insider cluster buy at {p.get('company','?')}, sir. {p.get('count',0)} buyers this week.",
    "INSIDER_CLUSTER_SELL":lambda p: f"Heavy insider selling at {p.get('company','?')}, sir. {p.get('count',0)} sellers — worth noting.",
    "SYNTHESIS_SIGNAL":    lambda p: f"Conviction signal: {p.get('conviction','?')} at {p.get('conviction_pct',0)}% alignment, sir.",
    "TAIL_RISK_ELEVATED":  lambda p: f"Tail risk elevated, sir — SKEW at {p.get('skew','?')}. Options market pricing in downside.",
    "CB_POLICY_SHIFT":     lambda p: f"Central bank policy shift detected: {p.get('direction','?')}, sir.",
    "CORRELATION_BREAK":   lambda p: f"Cross-asset correlation breakdown, sir. Usual relationships are coming apart.",
    "GEOPOLITICAL_SPIKE":  lambda p: f"Geopolitical risk spike in {p.get('country','?')}, sir. Risk score: {p.get('score','?')}.",
    "CB_DIVERGENCE":          lambda p: f"Central bank divergence widening, sir. Policy paths are splitting globally.",
    "EARNINGS_SURPRISE":      lambda p: f"Earnings surprise detected, sir — {p.get('ticker','?')} beat/miss outside consensus.",
    "ROTATION_SIGNAL":        lambda p: f"Fund rotation signal, sir — regime moved to {p.get('regime','?')}. ETF flows confirming.",
    "NOTABLE_INSIDER_BUY":    lambda p: f"Notable insider buy, sir — {p.get('name','?')} at {p.get('company','?')} put in {p.get('value','?')}.",
    "SCENARIO_PROBABILITY_SHIFT": lambda p: f"Scenario shift, sir — {p.get('scenario','?')} probability moved to {p.get('probability',0):.0f}%.",
    "VIX_TERM_INVERSION":     lambda p: f"VIX term structure inverted, sir. Short-vol premium elevated — market expects near-term stress.",
    "MACRO_RELEASE_RISK":     lambda p: f"High-impact macro release in {p.get('hours_ahead','?')} hours, sir — {p.get('release','?')}.",
    "EARNINGS_RISK":          lambda p: f"Earnings risk cluster ahead, sir — {p.get('count','?')} mega-caps reporting this week.",
    "MARKET_BREADTH_DETERIORATION": lambda p: f"Market breadth deteriorating, sir. Fewer stocks participating — divergence from index is widening.",
}

# Priority order — higher index = lower priority
_EVENT_PRIORITY = [
    "CRITICAL_CVE", "VIX_SPIKE", "VIX_TERM_INVERSION", "REGIME_CHANGE",
    "INSIDER_CLUSTER_BUY", "NOTABLE_INSIDER_BUY", "TAIL_RISK_ELEVATED",
    "NARRATIVE_FRACTURE", "GEOPOLITICAL_SPIKE", "SCENARIO_PROBABILITY_SHIFT",
    "CB_POLICY_SHIFT", "CORRELATION_BREAK", "SYNTHESIS_SIGNAL",
    "ROTATION_SIGNAL", "MARKET_BREADTH_DETERIORATION",
    "INSIDER_CLUSTER_SELL", "CB_DIVERGENCE", "MACRO_RELEASE_RISK",
    "EARNINGS_RISK", "EARNINGS_SURPRISE",
]

async def _proactive_loop(ws, history: list):
    """Background loop: checks event bus + file signals, pushes proactive voice alerts."""
    await asyncio.sleep(30)  # warm-up delay after connection

    while True:
        try:
            await asyncio.sleep(60)  # check every 60 seconds

            alerts = []
            now = time.time()

            # ── Event Bus — query recent unprocessed events from SQLite ──
            try:
                if _EVENT_BUS_DB.exists():
                    import sqlite3 as _sq
                    conn = _sq.connect(str(_EVENT_BUS_DB))
                    # Look at events from last 2 hours that we haven't surfaced yet
                    rows = conn.execute(
                        "SELECT id, event_type, payload FROM events "
                        "WHERE published > datetime('now', '-2 hours') "
                        "ORDER BY published DESC LIMIT 20"
                    ).fetchall()
                    conn.close()

                    # Sort by priority
                    def _priority(row):
                        try: return _EVENT_PRIORITY.index(row[1])
                        except ValueError: return 999

                    for row in sorted(rows, key=_priority):
                        eid, etype, payload_str = row
                        if eid in _event_bus_seen:
                            continue
                        cooldown = 3600 if etype in ("REGIME_CHANGE", "VIX_SPIKE") else 7200
                        key = f"event_{etype}"
                        if (now - _proactive_sent.get(key, 0)) < cooldown:
                            continue
                        try:
                            payload = json.loads(payload_str or "{}")
                        except Exception:
                            payload = {}
                        msg_fn = _EVENT_MESSAGES.get(etype)
                        if msg_fn:
                            alerts.append((key, eid, msg_fn(payload)))
                            break  # one at a time
            except Exception as e:
                log.debug(f"Event bus poll error: {e}")

            # ── High conviction signal (file fallback if no event) ──
            if not alerts:
                try:
                    syn_path = Path("/Users/navidavi/Documents/AI-Advisers/memory/synthesis_report.json")
                    if syn_path.exists():
                        syn = json.loads(syn_path.read_text())
                        pct = syn.get("conviction_pct", 0)
                        conviction = syn.get("conviction", "")
                        key = f"conviction_{conviction}"
                        if pct and float(pct) > 75 and (now - _proactive_sent.get(key, 0)) > 7200:
                            alerts.append((key, None, f"Strong {conviction} conviction at {pct}% alignment, sir. All signals pointing the same direction."))
                except Exception:
                    pass

            # ── Upcoming calendar event — smart meeting prep (15-minute warning) ──
            try:
                cal_ctx = _ctx_cache.get("calendar", "")
                if cal_ctx and "calendar data yet" not in cal_ctx:
                    times = _action_re.findall(r'(\d{1,2}:\d{2}\s*(?:AM|PM))', cal_ctx, _action_re.IGNORECASE)
                    # Try to extract event titles near the times
                    for t in times[:3]:
                        try:
                            event_time = datetime.strptime(t.strip(), "%I:%M %p").replace(
                                year=datetime.now().year, month=datetime.now().month, day=datetime.now().day
                            )
                            diff_min = (event_time - datetime.now()).total_seconds() / 60
                            if 10 <= diff_min <= 20:
                                key = f"cal_{t}"
                                if (now - _proactive_sent.get(key, 0)) > 1200:
                                    # Try to extract event title from calendar context
                                    title_match = _action_re.search(
                                        r'(?:' + _action_re.escape(t) + r')[^\n]*?([A-Z][^\n]{3,40})',
                                        cal_ctx, _action_re.IGNORECASE
                                    )
                                    event_title = title_match.group(1).strip() if title_match else ""

                                    # Build context-enriched prep message
                                    prep_parts = [f"Meeting in {int(diff_min)} minutes, sir."]
                                    if event_title:
                                        prep_parts[0] = f"{event_title} in {int(diff_min)} minutes, sir."

                                    # Check if event company is in portfolio
                                    try:
                                        from portfolio import DB_PATH as _pdb
                                        import sqlite3 as _sq3
                                        if _pdb.exists() and event_title:
                                            conn = _sq3.connect(str(_pdb))
                                            tickers = [r[0] for r in conn.execute("SELECT ticker FROM holdings").fetchall()]
                                            conn.close()
                                            for ticker in tickers:
                                                if ticker in event_title.upper():
                                                    prep_parts.append(f"{ticker} is in your portfolio.")
                                    except Exception:
                                        pass

                                    # Check insider sentiment for meeting context
                                    try:
                                        ins_f = AI_ADVISERS_MEMORY / "insider_trading_report.json"
                                        if ins_f.exists():
                                            ins = json.loads(ins_f.read_text())
                                            sentiment = ins.get("buy_sell_ratio", {}).get("sentiment", "")
                                            if sentiment == "BEARISH":
                                                prep_parts.append("Insiders are broadly selling today — worth knowing.")
                                    except Exception:
                                        pass

                                    msg = " ".join(prep_parts)
                                    alerts.append((key, None, msg))
                        except Exception:
                            pass
            except Exception:
                pass

            # Send first alert (one at a time, don't flood)
            if alerts:
                key, eid, msg = alerts[0]
                _proactive_sent[key] = now
                if eid is not None:
                    _event_bus_seen.add(eid)
                tts = strip_markdown_for_tts(msg)
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    audio = await synthesize_speech(tts)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        history.append({"role": "assistant", "content": f"[proactive] {msg}"})
                        log.info(f"[proactive] {msg}")
                        await asyncio.sleep(len(audio) / 4000 + 1.5)
                except Exception:
                    pass
                # Fallback: also send macOS notification (visible even if tab is background)
                try:
                    from notification_bridge import notify
                    notify("JARVIS", msg[:200])
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Proactive loop error: {e}")


async def _trigger_check_loop(ws, history: list):
    """Background loop: checks conditional triggers every 5 minutes."""
    await asyncio.sleep(60)  # initial delay
    while True:
        try:
            await asyncio.sleep(300)  # every 5 minutes
            import trigger_monitor as _tm
            fired_messages = _tm.check_all()
            for msg in fired_messages:
                log.info(f"[trigger] fired: {msg}")
                tts = strip_markdown_for_tts(msg)
                audio = await synthesize_speech(tts)
                try:
                    await ws.send_json({"type": "trigger_alert", "message": msg})
                    if audio:
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                        history.append({"role": "assistant", "content": f"[trigger] {msg}"})
                        await asyncio.sleep(len(audio) / 4000 + 1.0)
                    # Also send macOS notification
                    from notification_bridge import notify
                    notify("JARVIS Alert", msg[:200])
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Trigger check loop error: {e}")


# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        while True:
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        ["osascript", "-e", '''
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
'''],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop (30s is fine, API is fast)
            try:
                import urllib.request, json as _json
                url = "https://api.open-meteo.com/v1/forecast?latitude=27.77&longitude=-82.64&current=temperature_2m,weathercode&temperature_unit=fahrenheit"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    d = _json.loads(resp.read()).get("current", {})
                    temp = d.get("temperature_2m", "?")
                    _ctx_cache["weather"] = f"Current weather in St. Petersburg, FL: {temp}°F"
            except Exception:
                pass

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


@asynccontextmanager
async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects, _mic_listener
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — LLM features disabled")
    cached_projects = []

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()

    # Start local Whisper mic listener — pushes transcripts into queue for voice handler
    def _on_transcript(text: str):
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(_transcript_queue.put(text), loop)

    _mic_listener = MicListener(on_transcript=_on_transcript)
    _mic_listener.start(asyncio.get_event_loop())

    log.info("JARVIS server starting")
    yield

    if _mic_listener:
        _mic_listener.stop()


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous scan — runs in executor. Always includes jarvis first."""
    projects = []
    seen = set()

    # Always register jarvis itself
    jarvis_path = Path(__file__).parent
    projects.append({"name": "jarvis", "path": str(jarvis_path), "branch": "main"})
    seen.add(str(jarvis_path))

    for scan_dir in [Path.home() / "Desktop", Path.home()]:
        try:
            for entry in sorted(scan_dir.iterdir()):
                if entry.is_dir() and not entry.name.startswith(".") and str(entry) not in seen:
                    if (entry / ".git").exists():
                        seen.add(str(entry))
                        projects.append({"name": entry.name, "path": str(entry), "branch": ""})
        except Exception:
            pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen"]):
        return {"action": "describe_screen"}

    # Terminal / Claude Code — explicit open requests
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(p in t for p in ["what's on my screen", "whats on my screen", "what do you see",
                             "can you see my screen", "look at my screen", "what am i looking at",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # Task list check
    if any(p in t for p in ["what's on my list", "whats on my list", "my tasks", "my to do",
                             "my todo", "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(p in t for p in ["usage", "how much have you cost", "how much am i spending",
                             "what's the cost", "whats the cost", "api cost", "token usage",
                             "how expensive", "what's my bill"]):
        return {"action": "check_usage"}

    # Reminders
    if any(p in t for p in ["my reminders", "what are my reminders", "check my reminders",
                             "what's on my reminders", "whats on my reminders"]):
        return {"action": "check_reminders"}

    # Spotify
    if any(p in t for p in ["what's playing", "whats playing", "now playing", "what song",
                             "pause spotify", "play spotify", "skip this", "next song",
                             "spotify status"]):
        return {"action": "spotify_status"}

    # Market / stocks
    if any(p in t for p in ["market summary", "market update", "how's the market",
                             "hows the market", "market today", "stock market", "spy today",
                             "qqq today", "bitcoin today", "market snapshot"]):
        return {"action": "market_summary"}

    # Clipboard
    if any(p in t for p in ["what's in my clipboard", "whats in my clipboard",
                             "read my clipboard", "what did i copy", "clipboard contents"]):
        return {"action": "clipboard_read"}

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    result = await open_terminal("claude --dangerously-skip-permissions")
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write CLAUDE.md with clear instructions
    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    # Write prompt to a file, then pipe it to claude -p
    # This avoids all shell escaping issues
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {path} && cat .jarvis_prompt.txt | claude -p --dangerously-skip-permissions"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{last["path"]}"\nend tell'
    await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — skip audio if user spoke recently to avoid collision
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping lookup audio for {lookup_type} — user spoke recently")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts)
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback)
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        unread_msgs = await get_unread_messages(count=5)
        summary = format_unread_summary(unread_info)
        if unread_msgs:
            top = unread_msgs[:3]
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                for m in top
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup() -> str:
    """Screen describe — runs in thread."""
    if anthropic_client:
        return await describe_screen(anthropic_client)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


async def _do_reminders_lookup() -> str:
    reminders = await get_reminders()
    return format_reminders_for_voice(reminders)


async def _do_market_summary_lookup() -> str:
    summary = await get_market_summary()
    return format_market_summary_for_voice(summary)


async def _do_clipboard_read() -> str:
    content = await read_clipboard()
    return format_clipboard_for_voice(content)


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: anthropic.AsyncAnthropic) -> str:
    """Deep research with Opus — write results to HTML, open in browser."""
    try:
        research_response = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2000,
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
        )
        research_text = research_response.content[0].text

        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary via Haiku
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        return summary.content[0].text + " Full results are in your browser, sir."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    work_session = WorkSession()
    planner = TaskPlanner()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Session persistence — restore prior conversation state if < 24h old
    try:
        from session_store import load_session, save_session as _save_session
        _prior = load_session()
    except Exception:
        _prior = None

    if _prior:
        history: list[dict] = _prior.get("history", [])
        session_summary: str = _prior.get("session_summary", "")
        last_jarvis_response: str = _prior.get("last_response", "")
        log.info(f"Session restored: {len(history)} messages from {_prior.get('date','?')}")
    else:
        history: list[dict] = []
        session_summary: str = ""
        last_jarvis_response: str = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    global _ws_client_count
    _ws_client_count += 1
    log.info(f"Voice WebSocket connected (clients: {_ws_client_count})")

    # Write client count so notification_bridge can check
    try:
        Path("/Users/navidavi/jarvis/data").mkdir(exist_ok=True)
        Path("/Users/navidavi/jarvis/data/ws_clients.json").write_text(f'{{"clients": {_ws_client_count}}}')
    except Exception:
        pass

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if _prior and len(history) > 2:
            # Restored prior session — contextual welcome back
            greeting = "Welcome back, sir." if hour >= 12 else "Good morning again, sir."
        elif hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        # Calendar-aware context: surface imminent meetings
        try:
            _next_evt = get_next_event()
            if _next_evt:
                # get_next_event returns a dict with 'title' and 'start' (datetime or string)
                evt_title = _next_evt.get("title", "")
                evt_start = _next_evt.get("start")
                if evt_title and evt_start:
                    if isinstance(evt_start, str):
                        try:
                            from dateutil import parser as _dparser
                            evt_start = _dparser.parse(evt_start)
                        except Exception:
                            evt_start = None
                    if evt_start:
                        diff_min = (evt_start - datetime.now()).total_seconds() / 60
                        if 0 < diff_min <= 30:
                            greeting += f" You have {evt_title} in {int(diff_min)} minutes, sir."
                        elif 0 < diff_min <= 90:
                            greeting += f" {evt_title} is in {int(diff_min)} minutes, sir."
        except Exception:
            pass

        # Append morning briefing if available (generated at 8:30AM by morning_briefing.py)
        try:
            from morning_briefing import get_briefing as _get_briefing
            _briefing = _get_briefing()
            if _briefing:
                greeting += " " + _briefing
        except Exception:
            pass

        # Routine learning — proactive suggestion based on time-of-day patterns
        try:
            from routine_learner import suggest_proactive as _suggest
            _suggestion = _suggest()
            if _suggestion and not _briefing:  # skip if briefing already said something
                greeting += " " + _suggestion
        except Exception:
            pass

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    await asyncio.sleep(0.5)  # Wait for WS to fully establish
                    if _mic_listener: _mic_listener.pause()
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                        # Resume mic after greeting finishes
                        audio_duration = len(audio_bytes) / 4000
                        await asyncio.sleep(audio_duration + 1.5)
                    if _mic_listener: _mic_listener.resume()
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")
                    if _mic_listener: _mic_listener.resume()

            asyncio.create_task(_send_greeting())
            asyncio.create_task(_proactive_loop(ws, history))
            asyncio.create_task(_trigger_check_loop(ws, history))
            # Passive screen intelligence (only if JARVIS_SCREEN_INTEL=1 in env)
            try:
                import screen_intel as _si
                if _si.is_enabled():
                    asyncio.create_task(_si.passive_screen_loop(ws, anthropic_client, synthesize_speech, history))
            except Exception:
                pass
            # File system watcher — proactive alerts on new Downloads/Desktop files
            try:
                import file_watcher as _fw
                asyncio.create_task(_fw.start_watcher(ws, synthesize_speech))
            except Exception:
                pass

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            # Listen to both browser WebSocket AND local mic queue simultaneously
            ws_task  = asyncio.create_task(ws.receive_text())
            mic_task = asyncio.create_task(_transcript_queue.get())
            done, pending = await asyncio.wait(
                {ws_task, mic_task}, return_when=asyncio.FIRST_COMPLETED, timeout=25.0
            )
            for t in pending:
                t.cancel()

            # Keepalive ping — nothing arrived in 25s, send a ping to prevent browser disconnect
            if not done:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if mic_task in done:
                transcript_text = mic_task.result()
                msg = {"type": "transcript", "text": transcript_text, "isFinal": True}
            else:
                raw = ws_task.result()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

            # ── Barge-in: user interrupted JARVIS mid-speech ──
            if msg.get("type") == "barge_in":
                log.info("Barge-in detected — stopping current audio")
                await ws.send_json({"type": "barge_in_ack"})
                await ws.send_json({"type": "status", "state": "listening"})
                continue

            # ── Fix-self: activate work mode in JARVIS repo ──
            if msg.get("type") == "fix_self":
                jarvis_dir = str(Path(__file__).parent)
                await work_session.start(jarvis_dir)
                response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                tts = strip_markdown_for_tts(response_text)
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts)
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                continue

            if msg.get("type") != "transcript" or not msg.get("isFinal"):
                continue

            user_text = apply_speech_corrections(msg.get("text", "").strip())
            if not user_text:
                continue

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")
            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                _tts_already_sent = False  # set True when streaming TTS pre-sends audio
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → claude -p → Haiku summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass claude -p, use Haiku
                        response_text = await generate_response(
                            user_text, anthropic_client, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                        )
                    else:
                        # Send to claude -p (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → claude -p: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if Claude Code is stalling (asking questions instead of building)
                        if full_response and anthropic_client:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Haiku
                        if full_response and anthropic_client:
                            try:
                                summary = await anthropic_client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}],
                                )
                                response_text = summary.content[0].text
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    action = detect_action_fast(user_text)

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        elif action["action"] == "check_reminders":
                            response_text = "Checking your reminders now, sir."
                            asyncio.create_task(_lookup_and_report("reminders", _do_reminders_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "spotify_status":
                            response_text = "Checking Spotify now, sir."
                            asyncio.create_task(_lookup_and_report("spotify", get_spotify_info, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "market_summary":
                            response_text = "Pulling up the markets, sir."
                            asyncio.create_task(_lookup_and_report("market", _do_market_summary_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "clipboard_read":
                            response_text = "Reading your clipboard, sir."
                            asyncio.create_task(_lookup_and_report("clipboard", _do_clipboard_read, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "portfolio_status":
                            response_text = "Pulling up your portfolio, sir."
                            asyncio.create_task(_execute_portfolio_status(ws, history, voice_state))
                        elif action["action"] == "add_holding":
                            asyncio.create_task(_execute_portfolio_add(action["target"], ws, history, voice_state))
                            response_text = "Adding that to your portfolio, sir."
                        elif action["action"] == "remove_holding":
                            asyncio.create_task(_execute_portfolio_remove(action["target"], ws, history, voice_state))
                            response_text = "Removed, sir."
                        elif action["action"] == "home_light":
                            asyncio.create_task(_execute_home_light(action["target"], ws, history))
                            response_text = "On it, sir."
                        elif action["action"] == "home_temp":
                            asyncio.create_task(_execute_home_temp(action["target"], ws, history))
                            response_text = "Adjusting temperature, sir."
                        elif action["action"] == "home_scene":
                            asyncio.create_task(_execute_home_scene(action["target"], ws, history))
                            response_text = "Setting the scene, sir."
                        elif action["action"] in ("sys_volume", "sys_brightness", "sys_mute", "sys_unmute",
                                                   "sys_launch", "sys_quit", "sys_stats", "sys_lock",
                                                   "sys_battery", "sys_wifi", "send_imessage"):
                            asyncio.create_task(_execute_system_action(action["action"], action.get("target", ""), ws, history))
                            response_text = "On it, sir."
                        elif action["action"] == "websearch":
                            asyncio.create_task(_execute_websearch(action.get("target", ""), ws, history))
                            response_text = "Searching, sir."
                        elif action["action"] == "query_kb":
                            asyncio.create_task(_execute_query_kb(action.get("target", ""), ws, history))
                            response_text = "Querying the knowledge base, sir."
                        elif action["action"] == "set_trigger":
                            asyncio.create_task(_execute_set_trigger(action.get("target", ""), ws, history))
                            response_text = "Setting that watch condition, sir."
                        elif action["action"] == "list_triggers":
                            try:
                                import trigger_monitor as _tm
                                response_text = _tm.format_triggers_for_voice()
                            except Exception:
                                response_text = "Trigger list unavailable, sir."
                        elif action["action"] == "read_doc":
                            asyncio.create_task(_execute_read_doc(action.get("target", ""), ws, history))
                            response_text = "Reading that document, sir."
                        elif action["action"] == "send_email":
                            asyncio.create_task(_execute_send_email(action.get("target", ""), ws, history))
                            response_text = "Sending that email, sir."
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not anthropic_client:
                            response_text = "API key not configured."
                        else:
                            # Check response cache first (common/repeatable queries)
                            try:
                                import response_cache as _rcache
                                _cached = _rcache.get(user_text)
                            except Exception:
                                _cached = None
                            if _cached:
                                log.info("[cache] hit for: %s", user_text[:60])
                                response_text = _cached
                                _tts_already_sent = False
                            else:
                                # Streaming path — TTS starts on first sentence, not after full response
                                await ws.send_json({"type": "status", "state": "thinking"})
                                try:
                                    response_text = await generate_response_streamed(
                                        user_text, anthropic_client, task_manager,
                                        cached_projects, history, ws,
                                        last_response=last_jarvis_response,
                                        session_summary=session_summary,
                                    )
                                    _tts_already_sent = True
                                except Exception as _api_err:
                                    # Ollama offline fallback if Anthropic API fails
                                    log.warning(f"Anthropic API error, trying Ollama fallback: {_api_err}")
                                    try:
                                        import ollama_fallback as _ollama
                                        if _ollama.is_available():
                                            response_text = _ollama.generate_simple(user_text, history[-6:])
                                            log.info("[ollama] fallback response generated")
                                        else:
                                            response_text = "I'm having trouble reaching my servers, sir. Please check your connection."
                                    except Exception:
                                        response_text = "API unavailable, sir."
                                    _tts_already_sent = False
                                # Store cacheable responses
                                try:
                                    _rcache.set(user_text, response_text)
                                except Exception:
                                    pass

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = f"Connecting to {proj} now, sir."
                                    elif action_type == "build":
                                        response_text = "On it, sir."
                                    elif action_type == "research":
                                        response_text = "Looking into that now, sir."
                                    else:
                                        response_text = "Right away, sir."

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed CLAUDE.md
                                    Path(path, "CLAUDE.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", _do_screen_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "create_event":
                                    asyncio.create_task(_execute_create_event(embedded_action["target"], ws, history))
                                elif embedded_action["action"] == "github":
                                    asyncio.create_task(_execute_github(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "reminder":
                                    asyncio.create_task(_execute_reminder(embedded_action["target"], ws))
                                elif embedded_action["action"] == "complete_reminder":
                                    asyncio.create_task(_execute_complete_reminder(embedded_action["target"], ws))
                                elif embedded_action["action"] == "contact":
                                    asyncio.create_task(_execute_contact_lookup(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "spotify":
                                    asyncio.create_task(_execute_spotify(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "clipboard_read":
                                    asyncio.create_task(_lookup_and_report("clipboard", _do_clipboard_read, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "clipboard_write":
                                    asyncio.create_task(_execute_clipboard_write(embedded_action["target"], ws))
                                elif embedded_action["action"] == "market":
                                    asyncio.create_task(_execute_market_price(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "market_summary":
                                    asyncio.create_task(_lookup_and_report("market", _do_market_summary_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "add_holding":
                                    asyncio.create_task(_execute_portfolio_add(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "remove_holding":
                                    asyncio.create_task(_execute_portfolio_remove(embedded_action["target"], ws, history, voice_state))
                                elif embedded_action["action"] == "portfolio_status":
                                    asyncio.create_task(_execute_portfolio_status(ws, history, voice_state))
                                elif embedded_action["action"] == "home_light":
                                    asyncio.create_task(_execute_home_light(embedded_action["target"], ws, history))
                                elif embedded_action["action"] == "home_temp":
                                    asyncio.create_task(_execute_home_temp(embedded_action["target"], ws, history))
                                elif embedded_action["action"] == "home_scene":
                                    asyncio.create_task(_execute_home_scene(embedded_action["target"], ws, history))
                                elif embedded_action["action"] in ("sys_volume", "sys_brightness", "sys_mute",
                                                                    "sys_unmute", "sys_launch", "sys_quit",
                                                                    "sys_stats", "sys_lock", "sys_battery",
                                                                    "sys_wifi", "send_imessage"):
                                    asyncio.create_task(_execute_system_action(
                                        embedded_action["action"], embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "websearch":
                                    asyncio.create_task(_execute_websearch(embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "query_kb":
                                    asyncio.create_task(_execute_query_kb(embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "set_trigger":
                                    asyncio.create_task(_execute_set_trigger(embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "read_doc":
                                    asyncio.create_task(_execute_read_doc(embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "send_email":
                                    asyncio.create_task(_execute_send_email(embedded_action.get("target", ""), ws, history))
                                elif embedded_action["action"] == "pegasus":
                                    asyncio.create_task(_execute_pegasus(embedded_action.get("target", "CODE_BLACK"), ws, history))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and anthropic_client:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, anthropic_client
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                # Skip non-speech internal tokens
                if not response_text or response_text.strip() in ("[listening]", "[thinking]", "[processing]", ""):
                    if _mic_listener: _mic_listener.resume()
                    continue

                # TTS — streaming path already sent audio sentence-by-sentence
                tts = strip_markdown_for_tts(response_text)
                if _mic_listener: _mic_listener.pause()
                if _tts_already_sent:
                    # Streaming sent all audio chunks — just signal speaking state
                    # (frontend queued and plays them; we estimate duration for mic resume)
                    await ws.send_json({"type": "status", "state": "speaking"})
                    est_duration = len(tts) / 15  # ~15 chars/second speech rate
                    asyncio.create_task(_resume_mic_after(max(est_duration, 1.5)))
                else:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    audio = await synthesize_speech(tts)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                        # MP3 at ~32kbps (edge-tts compressed) → bytes/4000 ≈ seconds
                        audio_duration = len(audio) / 4000
                        asyncio.create_task(_resume_mic_after(audio_duration + 1.5))
                    else:
                        await ws.send_json({"type": "text", "text": response_text})
                        await ws.send_json({"type": "status", "state": "idle"})
                        if _mic_listener: _mic_listener.resume()
                log.info(f"JARVIS: {response_text}")
                last_jarvis_response = response_text
                _log_interaction(user_text, response_text, latency_ms=(time.time() - voice_state["last_user_time"]) * 1000)

                # Auto-learn from corrections — if user is correcting JARVIS, store the correction as a memory
                _correction_words = {"no ", "wrong", "incorrect", "not right", "that's not", "thats not", "not what i", "actually ", "i meant"}
                if any(cw in user_text.lower() for cw in _correction_words) and last_jarvis_response:
                    try:
                        correction_mem = f"User corrected JARVIS: said '{user_text[:100]}' after JARVIS said '{last_jarvis_response[:80]}'"
                        remember(correction_mem, mem_type="correction", importance=8)
                        log.info(f"[auto-learn] Correction stored: {user_text[:60]}")
                    except Exception:
                        pass

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        _ws_client_count = max(0, _ws_client_count - 1)
        try:
            Path("/Users/navidavi/jarvis/data/ws_clients.json").write_text(f'{{"clients": {_ws_client_count}}}')
        except Exception:
            pass
        log.info(f"Voice WebSocket disconnected (clients: {_ws_client_count})")
        task_manager.unregister_websocket(ws)
        # Save session state for restoration on next connect
        try:
            from session_store import save_session as _save_session
            if history:
                _save_session(history, session_summary, last_jarvis_response)
                log.info(f"Session saved: {len(history)} messages")
        except Exception as e:
            log.debug(f"Session save failed: {e}")


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    return {"success": True}

@app.post("/api/settings/test-anthropic")
async def api_test_anthropic(body: KeyTest):
    key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        await client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=10, messages=[{"role": "user", "content": "Hi"}])
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.fish.audio/v1/tts",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"text": "test", "reference_id": FISH_VOICE_ID},
            )
            if resp.status_code in (200, 201):
                return {"valid": True}
            elif resp.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}
            else:
                return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    claude_installed = _shutil.which("claude") is not None
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    try: await get_recent_notes(count=1); notes_ok = True
    except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "claude_code_installed": claude_installed,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "anthropic": bool(env_dict.get("ANTHROPIC_API_KEY", "").strip() and env_dict.get("ANTHROPIC_API_KEY", "") != "your-anthropic-api-key-here"),
            "fish_audio": bool(env_dict.get("FISH_API_KEY", "").strip() and env_dict.get("FISH_API_KEY", "") != "your-fish-audio-api-key-here"),
            "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

# Track active WS client count for notification_bridge
_ws_client_count = 0

@app.post("/api/transcript")
async def api_transcript(request: Request):
    """Receive transcript from jarvis_daemon (always-on mic) and process it.
    Returns audio as base64 for daemon to play via afplay."""
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            return {"error": "empty transcript"}
        log.info(f"[daemon] transcript: {text}")
        # Put into the shared transcript queue so the active WS session processes it
        await _transcript_queue.put(text)
        return {"status": "queued"}
    except Exception as e:
        log.error(f"api_transcript error: {e}")
        return {"error": str(e)}


@app.post("/api/siri")
async def api_siri(request: Request):
    """iOS Shortcuts bridge — receive text, return spoken response as JSON.
    iOS Shortcut: POST {"text": "what's my portfolio"} → {"response": "...text..."}
    """
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            return {"response": "No input received, sir."}
        log.info(f"[siri] query: {text}")
        # Use generate_response directly (no streaming for Siri)
        if not anthropic_client:
            return {"response": "API key not configured."}
        response = await generate_response(text, anthropic_client, task_manager, cached_projects, [], last_response="")
        return {"response": response, "text": response}
    except Exception as e:
        log.error(f"api_siri error: {e}")
        return {"response": "Something went wrong, sir.", "error": str(e)}


@app.post("/api/notify")
async def api_notify(request: Request):
    """Send a macOS notification. Used by iOS, scripts, or external triggers.
    Body: {"title": "JARVIS", "message": "...", "speak": true}"""
    try:
        data = await request.json()
        title = data.get("title", "JARVIS")
        message = data.get("message", "")
        should_speak = data.get("speak", False)
        if message:
            try:
                from notification_bridge import notify, speak_system
                notify(title, message)
                if should_speak:
                    speak_system(message)
            except Exception as ne:
                log.warning(f"notification_bridge error: {ne}")
        return {"status": "sent"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ws-status")
async def api_ws_status():
    """Return number of active WebSocket clients — used by notification_bridge."""
    return {"clients": _ws_client_count}


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Record thumbs-up/down feedback for the last response."""
    data = await request.json()
    rating = data.get("rating", 0)   # 1 = thumbs up, -1 = thumbs down
    text   = data.get("text", "")
    entry  = {
        "ts":     datetime.now().isoformat(),
        "type":   "feedback",
        "rating": rating,
        "text":   text,
    }
    log_path = DATA_DIR / "interaction_log.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "recorded"}


@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # For now, also open Terminal so user can see
    script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'    do script "cd {jarvis_dir} && claude --dangerously-skip-permissions"\n'
        'end tell'
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIST / "index.html"))

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
