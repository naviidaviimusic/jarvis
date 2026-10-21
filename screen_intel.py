#!/usr/local/bin/python3.11
"""
JARVIS Screen Intelligence — passive ambient screen analysis.
Runs as background async task. Opt-in via PASSIVE_SCREEN=true in .env.
Screenshots every 5 minutes, alerts on: errors, notifications, urgent items.
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Callable

import anthropic

log = logging.getLogger("jarvis.screen_intel")

# module-level cooldown tracker: alert_type → last_sent_timestamp
_sent_alerts: dict[str, float] = {}
_COOLDOWN_SECONDS = 3600  # 1 hour per alert type
_LOOP_INTERVAL = 300       # 5 minutes


def is_enabled() -> bool:
    return os.environ.get("PASSIVE_SCREEN", "").lower() == "true"


def take_screenshot() -> bytes | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", tmp_path],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning("screencapture failed: %s", result.stderr.decode())
            return None

        with open(tmp_path, "rb") as f:
            data = f.read()

        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return data if data else None
    except Exception as exc:
        log.error("Screenshot error: %s", exc)
        return None


async def analyze_screenshot(
    image_bytes: bytes,
    client: anthropic.AsyncAnthropic,
) -> dict | None:
    try:
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="You are analyzing a screen for JARVIS voice alerts. Be extremely brief.",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Identify ONLY: Python/terminal errors, urgent notifications, "
                                "deadline items, calendar popups. "
                                'Return JSON: {"has_alert": bool, "alert_type": str, '
                                '"message": str (max 20 words), "urgency": "high"/"medium"/"none"}. '
                                'If nothing notable, return {"has_alert": false}'
                            ),
                        },
                    ],
                }
            ],
        )

        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Could not parse screen analysis JSON: %s", exc)
        return None
    except Exception as exc:
        log.error("Screen analysis error: %s", exc)
        return None


async def passive_screen_loop(
    ws,
    client: anthropic.AsyncAnthropic,
    synthesize_fn: Callable,
    history: list,
) -> None:
    if not is_enabled():
        log.debug("Passive screen intelligence disabled.")
        return

    log.info("Passive screen loop started (interval=%ds).", _LOOP_INTERVAL)

    while True:
        await asyncio.sleep(_LOOP_INTERVAL)

        try:
            image_bytes = take_screenshot()
            if image_bytes is None:
                log.warning("Screenshot returned None, skipping cycle.")
                continue

            result = await analyze_screenshot(image_bytes, client)
            if result is None:
                continue

            has_alert = result.get("has_alert", False)
            urgency = result.get("urgency", "none")
            alert_type = result.get("alert_type", "unknown")
            message = result.get("message", "")

            if not has_alert or urgency not in ("high", "medium"):
                continue

            now = time.monotonic()
            last_sent = _sent_alerts.get(alert_type, 0.0)
            if now - last_sent < _COOLDOWN_SECONDS:
                log.debug(
                    "Alert type '%s' suppressed by cooldown (%.0fs remaining).",
                    alert_type,
                    _COOLDOWN_SECONDS - (now - last_sent),
                )
                continue

            _sent_alerts[alert_type] = now
            alert_text = f"Sir, heads up — {message}"
            log.info("Screen alert [%s/%s]: %s", alert_type, urgency, message)

            try:
                audio = await synthesize_fn(alert_text)
                if audio and ws:
                    await ws.send_bytes(audio)
            except Exception as exc:
                log.error("Failed to synthesize/send screen alert: %s", exc)

        except asyncio.CancelledError:
            log.info("Passive screen loop cancelled.")
            raise
        except Exception as exc:
            log.error("Unexpected error in passive screen loop: %s", exc)
