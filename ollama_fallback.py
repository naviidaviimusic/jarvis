#!/usr/local/bin/python3.11
"""
JARVIS Ollama Fallback
─────────────────────────
When Anthropic API is down/offline, routes to local Ollama.
Uses the best installed local model (qwen2.5:7b first) for conversation.
"""

import logging
from typing import Optional

log = logging.getLogger("jarvis.ollama_fallback")

OLLAMA_BASE = "http://localhost:11434"
PREFERRED_MODELS = [
    "qwen2.5:7b",
    "qwen3:4b",
    "llama3.2:latest",
    "qwen2.5:3b",
    "gemma2:2b",
]

JARVIS_SYSTEM = (
    "You are JARVIS, a British AI butler assistant. "
    "Respond concisely in 1-2 sentences maximum. "
    "Use polite, dry British wit. Address the user as 'sir' occasionally. "
    "Never use emojis or markdown formatting."
)

OFFLINE_RESPONSE = "Offline mode — limited capability available, sir."


# ── Public API ────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """Check if Ollama is running: GET http://localhost:11434/ with 1s timeout."""
    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=1.0) as client:
            resp = client.get(f"{OLLAMA_BASE}/")
            return resp.status_code == 200
    except Exception:
        return False


def get_best_model() -> str:
    """GET /api/tags, pick best available model from preference list.
    Returns first match, or 'qwen2.5:3b' as default.
    """
    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{OLLAMA_BASE}/api/tags")
            resp.raise_for_status()
            data = resp.json()

        installed: set[str] = set()
        for model_entry in data.get("models", []):
            name = model_entry.get("name", "")
            if name:
                installed.add(name)

        for preferred in PREFERRED_MODELS:
            if preferred in installed:
                log.info("Best Ollama model: %s", preferred)
                return preferred

        # Fallback: return first installed model, if any
        if installed:
            first = next(iter(installed))
            log.info("No preferred model found; using %s", first)
            return first

    except Exception as exc:
        log.warning("get_best_model failed: %s", exc)

    return PREFERRED_MODELS[0]


def generate(prompt: str, system: str = "", max_tokens: int = 200) -> str:
    """Generate a response via Ollama.

    Combines system + prompt into a single prompt string.
    POST to /api/generate with stream=false, 30s timeout.
    Returns response text truncated to approx max_tokens words.
    On error: returns OFFLINE_RESPONSE.
    """
    try:
        import httpx  # type: ignore

        model = get_best_model()

        # Combine system and user prompt
        if system:
            full_prompt = f"{system}\n\nUser: {prompt}\nJARVIS:"
        else:
            full_prompt = prompt

        payload = {
            "model": model,
            "prompt": full_prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.7,
                "top_p": 0.9,
            },
        }

        with httpx.Client(timeout=90.0) as client:
            resp = client.post(f"{OLLAMA_BASE}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()

        response_text = data.get("response", "").strip()
        if not response_text:
            return OFFLINE_RESPONSE

        # Truncate to approx max_tokens words
        words = response_text.split()
        if len(words) > max_tokens:
            response_text = " ".join(words[:max_tokens]) + "..."

        return response_text

    except Exception as exc:
        log.error("Ollama generate failed: %s", exc)
        return OFFLINE_RESPONSE


def generate_simple(user_message: str, history: list[dict], extra_context: str = "") -> str:
    """Simplified generate for JARVIS voice responses.

    Formats last 5 history messages + user_message into a prompt.
    Injects live AI Advisers market intel when available.
    Returns response string suitable for TTS.
    """
    try:
        # Pull live market intel to keep Ollama informed
        if not extra_context:
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(__file__))
                from ai_advisers_bridge import get_latest_intelligence
                extra_context = get_latest_intelligence()
            except Exception:
                pass

        system = JARVIS_SYSTEM
        if extra_context:
            system += f"\n\nCURRENT INTELLIGENCE (AI Advisers):\n{extra_context[:600]}"

        # Build conversation context from last 5 history items
        recent = history[-5:] if len(history) > 5 else history
        context_lines: list[str] = []
        for entry in recent:
            role = entry.get("role", "user")
            content = entry.get("content", "").strip()
            if not content:
                continue
            if role == "user":
                context_lines.append(f"User: {content}")
            elif role == "assistant":
                context_lines.append(f"JARVIS: {content}")

        context_lines.append(f"User: {user_message}")
        context_lines.append("JARVIS:")

        conversation = "\n".join(context_lines)
        full_prompt = f"{system}\n\n{conversation}"

        return generate(prompt=full_prompt, system="", max_tokens=60)

    except Exception as exc:
        log.error("generate_simple failed: %s", exc)
        return OFFLINE_RESPONSE
