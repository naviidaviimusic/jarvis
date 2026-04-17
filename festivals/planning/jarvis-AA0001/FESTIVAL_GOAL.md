# JARVIS — Festival Goal

## Primary Goal
Build and maintain JARVIS — a fully functional voice-first AI assistant for macOS that can hear, speak, and execute commands using Claude as its brain.

## Success Criteria
- JARVIS hears the user via local Whisper STT (no Google/Chrome dependency)
- JARVIS responds with a British voice via edge-tts
- JARVIS executes commands: browse web, build software, read calendar/mail/notes
- JARVIS remembers context across sessions via SQLite memory
- JARVIS spawns Claude Code for development tasks on command
- Zero dropped mic sessions — self-healing on any Chrome/macOS error

## Key Components
- server.py — FastAPI backend, WebSocket voice loop, LLM brain (Claude Haiku)
- stt.py — Local Whisper mic listener (faster-whisper tiny.en)
- frontend/ — Vite + TypeScript + Three.js orb visualization
- memory.py — SQLite memory with FTS5 search
- actions.py — System actions (build, browse, research, terminal)
- calendar_access.py, mail_access.py, notes_access.py — macOS integrations

## How to Start
Tab 1: cd ~/jarvis && python3.11 server.py
Tab 2: cd ~/jarvis/frontend && npm run dev
Then open Chrome → https://localhost:5173 → click once → speak.
