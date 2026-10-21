"""
ai_advisers_bridge.py — Deep integration between JARVIS and the AI Advisers KB.

Gives JARVIS:
  1. Keyword search across 566+ intel items (source articles + agent summaries)
  2. Latest decisions from the DQ agent suite
  3. Latest agent outputs (synthesis, scenarios, multi-hop chains)
  4. A voice-ready context string injected into system prompt
  5. An on-demand query function for [ACTION:QUERY_KB] dispatch
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.kb")

KB_PATH = (Path.home() / "Documents/AI-Advisers/memory/knowledge_base.db")
_CACHE_TTL = 120  # 2-minute cache for search results
_cache: dict[str, tuple[float, str]] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect() -> Optional[sqlite3.Connection]:
    if not KB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(KB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log.debug(f"KB connect error: {e}")
        return None


def _search_intel(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Search intel table by keyword across title + content."""
    words = [w.strip() for w in query.split() if len(w.strip()) > 2]
    if not words:
        return []
    # Build OR LIKE conditions
    conditions = " OR ".join(
        [f"(title LIKE ? OR content LIKE ?)" for _ in words]
    )
    params = []
    for w in words:
        params += [f"%{w}%", f"%{w}%"]
    try:
        rows = conn.execute(
            f"SELECT id, ts, source, title, content FROM intel "
            f"WHERE {conditions} ORDER BY ts DESC LIMIT {limit}",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug(f"KB search error: {e}")
        return []


def _recent_intel(conn: sqlite3.Connection, limit: int = 6) -> list[dict]:
    """Most recent intel items regardless of topic."""
    try:
        rows = conn.execute(
            "SELECT id, ts, source, title, content FROM intel ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _recent_decisions(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT title, decision, domain, confidence, rationale FROM decisions "
            "ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _recent_outputs(conn: sqlite3.Connection, modules: list[str], limit: int = 1) -> list[dict]:
    """Get latest output for each listed module."""
    results = []
    for module in modules:
        try:
            row = conn.execute(
                "SELECT module, ts, output FROM agent_outputs "
                "WHERE module = ? ORDER BY ts DESC LIMIT ?",
                (module, limit),
            ).fetchone()
            if row:
                results.append(dict(row))
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(query: str, limit: int = 5) -> str:
    """Search the KB and return a voice-ready summary string."""
    cache_key = f"search:{query.lower()[:60]}"
    now = time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < _CACHE_TTL:
        return _cache[cache_key][1]

    conn = _connect()
    if not conn:
        return "Knowledge base unavailable, sir."

    try:
        results = _search_intel(conn, query, limit=limit)
        if not results:
            return f"Nothing found in the knowledge base for '{query}', sir."

        parts = []
        for r in results:
            title = r.get("title", "")[:70]
            source = r.get("source", "")
            content = r.get("content", "")[:120]
            parts.append(f"{source}: {title} — {content}")

        result = " | ".join(parts)
        _cache[cache_key] = (now, result)
        return result
    finally:
        conn.close()


def get_latest_intelligence(topics: Optional[list[str]] = None) -> str:
    """
    Return a concise intelligence summary for injection into JARVIS system prompt.
    If topics provided, filters by keyword. Otherwise returns most recent items.
    """
    conn = _connect()
    if not conn:
        return ""

    try:
        lines = []

        # ── Recent decisions from DQ agents ──
        decisions = _recent_decisions(conn, limit=3)
        for d in decisions:
            domain = d.get("domain", "")
            title = d.get("title", "")[:60]
            confidence = d.get("confidence", "")
            decision_text = d.get("decision", "")[:100]
            if title:
                lines.append(f"DQ_DECISION [{domain}|{confidence}]: {title} → {decision_text}")

        # ── Latest agent synthesis outputs ──
        key_modules = [
            "multi_hop_reasoner", "signal_synthesis_engine", "macro_scenario_engine",
            "dq_security_agent", "dq_market_agent", "dq_tech_agent",
            "dq_strategy_agent", "dq_benchmark_agent",
        ]
        outputs = _recent_outputs(conn, key_modules, limit=1)
        for o in outputs:
            module = o.get("module", "")
            output_text = str(o.get("output", ""))
            # Extract first meaningful line
            first_line = next((l.strip() for l in output_text.splitlines() if len(l.strip()) > 20), "")
            if first_line:
                lines.append(f"{module.upper()}: {first_line[:150]}")

        # ── Recent high-signal intel items ──
        if topics:
            items = []
            for topic in topics[:2]:
                items.extend(_search_intel(conn, topic, limit=2))
            items = items[:4]
        else:
            items = _recent_intel(conn, limit=4)

        for item in items:
            source = item.get("source", "")
            title = item.get("title", "")[:70]
            if source and title:
                lines.append(f"KB [{source}]: {title}")

        return "\n".join(lines) if lines else ""
    finally:
        conn.close()


def format_search_for_voice(query: str) -> str:
    """Query the KB and format results as a spoken JARVIS response."""
    conn = _connect()
    if not conn:
        return "The knowledge base is offline, sir."

    try:
        results = _search_intel(conn, query, limit=3)
        if not results:
            # Fall back to decisions search
            words = query.lower().split()
            decisions = _recent_decisions(conn, limit=5)
            relevant = [
                d for d in decisions
                if any(w in (d.get("title", "") + d.get("rationale", "")).lower() for w in words)
            ]
            if relevant:
                d = relevant[0]
                return (
                    f"From our decision log: {d.get('title','?')} — "
                    f"{d.get('decision','?')[:120]} Confidence: {d.get('confidence','?')}."
                )
            return f"I don't have specific knowledge base entries for '{query}', sir."

        top = results[0]
        source = top.get("source", "unknown")
        title = top.get("title", "")
        content = top.get("content", "")[:200]

        reply = f"From {source}: {title}. {content}"
        if len(results) > 1:
            reply += f" I have {len(results) - 1} more related items if you'd like."
        return reply
    finally:
        conn.close()


def get_context_for_prompt() -> str:
    """Return a short KB context block for injection into JARVIS system prompt."""
    intel = get_latest_intelligence()
    if not intel:
        return ""
    return f"KNOWLEDGE BASE (AI Advisers — live):\n{intel}"
