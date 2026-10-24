# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, connecting to your Apple Calendar, Mail, Notes, and can spawn Claude Code sessions for development tasks.

## Quick Start
When a user clones this repo and starts Claude Code, help them:
1. Copy .env.example to .env
2. Get an Anthropic API key from console.anthropic.com
3. Get a Fish Audio API key from fish.audio
4. Install Python dependencies: pip install -r requirements.txt
5. Install frontend dependencies: cd frontend && npm install
6. Generate SSL certs: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
7. Run the backend: python server.py
8. Run the frontend: cd frontend && npm run dev
9. Open Chrome to http://localhost:5173
10. Click to enable audio, speak to JARVIS

## Architecture
- **Backend**: FastAPI + Python (server.py, ~4200+ lines)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb, barge-in support)
- **Communication**: WebSocket (JSON messages + binary audio) + HTTP REST
- **AI**: Claude Sonnet/Opus via Anthropic SDK, streaming TTS sentence-by-sentence
- **TTS**: edge-tts (en-GB-RyanNeural) — British voice
- **STT**: Chrome Web Speech API (browser) + always-on Whisper daemon (background)
- **System**: AppleScript for Calendar, Mail, Notes, Terminal + system_control.py

## Key Files
### Core
- `server.py` — Main server, WebSocket handler, LLM integration, all action dispatch
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + barge-in + AudioContext audio player
- `frontend/src/main.ts` — Frontend state machine (idle/listening/thinking/speaking)
- `memory.py` — SQLite memory system with FTS5 search, tasks, notes
- `session_store.py` — Cross-restart session persistence (30 messages)
- `response_cache.py` — 5-min TTL cache for common queries

### Intelligence
- `portfolio.py` — Personal holdings tracker with AI Advisers signal cross-reference
- `morning_briefing.py` — Daily 8:30AM voice-ready market briefing
- `jarvis_self_improve.py` — Weekly self-improvement from interaction log (Sundays 6AM)
- `jarvis_daily_agent.py` — Daily evolution agent (6AM) — audits capabilities, writes reports/

### System Control
- `system_control.py` — Volume, brightness, apps, stats, lock, battery, WiFi, iMessage
- `home_control.py` — Home Assistant smart home (needs HOME_ASSISTANT_URL in .env)
- `notification_bridge.py` — macOS notifications + system TTS fallback when browser closed
- `file_watcher.py` — watchdog on ~/Downloads + ~/Desktop, proactive file alerts
- `screen_intel.py` — Passive screenshot analysis (needs JARVIS_SCREEN_INTEL=1)
- `jarvis_daemon.py` — **Always-on wake word daemon** (LaunchAgent, KeepAlive)

### macOS Integrations
- `calendar_access.py`, `mail_access.py` (READ-ONLY), `notes_access.py`
- `contacts_access.py`, `reminders_access.py`, `spotify_access.py`, `clipboard_access.py`
- `github_access.py`, `actions.py`, `browser.py`, `work_mode.py`

## Environment Variables
- `ANTHROPIC_API_KEY` (optional) — Claude API access. Without it (or with no credits) JARVIS runs free on the local Ollama brain (`ollama_fallback.py`: gemma3:4b for voice, qwen2.5:7b for reports)
- `JARVIS_SERVER_MIC=0` — server's own Whisper mic off (set in com.jarvis.server; the wake-word daemon owns the mic)
- `JARVIS_VOICE_MODEL` / `JARVIS_DEEP_MODEL` (optional) — override the local models
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails
- `HOME_ASSISTANT_URL` + `HOME_ASSISTANT_TOKEN` (optional) — Smart home control
- `JARVIS_SCREEN_INTEL=1` (optional) — Enable passive screenshot analysis

## LaunchAgents
- `com.jarvis.server` — server.py on https://localhost:8340 (UI + API), starts at login, KeepAlive; logs/server.log. Restart: `launchctl kickstart -k gui/$(id -u)/com.jarvis.server`
- `com.jarvis.daemon` — Always-on wake word listener (starts at login, auto-restarts); posts to https://localhost:8340/api/transcript — answered headless (spoken via afplay) when no browser tab is open
- `com.jarvis.daily_evolution` — 6AM evolution report (Claude, or the free local brain)
- Both always-on agents are watched by AI-Advisers `ecosystem_fixer.py` (restart if down / health check fails)
- `com.jarvis.self_improve` — Sundays 6AM self-improvement analysis

## Action Tags
All [ACTION:X] tags JARVIS can emit:
BUILD, BROWSE, RESEARCH, OPEN_TERMINAL, PROMPT_PROJECT, ADD_TASK, ADD_NOTE,
COMPLETE_TASK, REMEMBER, CREATE_NOTE, READ_NOTE, SCREEN, CREATE_EVENT, GITHUB,
REMINDER, COMPLETE_REMINDER, CONTACT, SPOTIFY, CLIPBOARD_READ, CLIPBOARD_WRITE,
MARKET, MARKET_SUMMARY, ADD_HOLDING, REMOVE_HOLDING, PORTFOLIO_STATUS,
HOME_LIGHT, HOME_TEMP, HOME_SCENE,
SYS_VOLUME, SYS_BRIGHTNESS, SYS_LAUNCH, SYS_QUIT, SYS_STATS, SYS_LOCK,
SYS_MUTE, SYS_UNMUTE, SYS_BATTERY, SYS_WIFI, SEND_IMESSAGE

## iOS Shortcuts Bridge
POST `https://MAC_IP:8340/api/siri` with `{"text": "your query"}` → `{"response": "..."}`.
Works from iPhone Shortcuts app, Apple Watch, CarPlay.

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: [ACTION:BUILD], [ACTION:BROWSE], [ACTION:RESEARCH], etc.
- AppleScript for all macOS integrations (no OAuth needed)
- Read-only for Mail (safety by design)
- SQLite for all local data storage
- Python: always use `/usr/local/bin/python3.11`

# Ideas Tracking

At the **start of every conversation**, check if `.claude/ideas.md` exists in this project. If it does:
1. Read it silently
2. If any open ideas are relevant to what the user is about to work on, mention them naturally (e.g. "By the way, you had an idea about X — want to tackle that too?")
3. If no ideas are relevant, don't mention them unless the user has no clear task yet — then suggest picking one up

After **completing a task**, check the ideas list again:
1. If any open ideas relate to what was just finished, suggest picking one up next
2. If you realize an open idea was just implemented as part of the work, mark it done in `.claude/ideas.md` and mention it
3. Otherwise, briefly remind the user they have open ideas if they seem to be wrapping up

Use `/ideas` to capture new ideas mid-session without interrupting flow.
