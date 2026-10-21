#!/usr/local/bin/python3.11
"""
JARVIS Home Control — Home Assistant integration.
Configure via .env: HOME_ASSISTANT_URL=http://homeassistant.local:8123
                    HOME_ASSISTANT_TOKEN=your_long_lived_token
"""

import os
import logging

import httpx

log = logging.getLogger("jarvis.home_control")

_TIMEOUT = 5.0


def is_configured() -> bool:
    return bool(os.environ.get("HOME_ASSISTANT_URL") and os.environ.get("HOME_ASSISTANT_TOKEN"))


def _headers() -> dict:
    token = os.environ.get("HOME_ASSISTANT_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return os.environ.get("HOME_ASSISTANT_URL", "").rstrip("/")


def format_for_voice(result: str) -> str:
    if len(result) <= 100:
        return result
    return result[:97] + "..."


def control_light(
    entity_id_or_area: str,
    state: str,
    brightness: int = None,
    color_temp: str = None,
) -> str:
    if not is_configured():
        return "Home control not configured, sir."

    state = state.lower()
    if state == "off":
        service = "turn_off"
        payload: dict = {}
    else:
        service = "turn_on"
        payload = {}
        if state == "dim":
            payload["brightness_pct"] = 30
        elif state == "bright":
            payload["brightness_pct"] = 100
        elif brightness is not None:
            payload["brightness_pct"] = brightness

        if color_temp is not None:
            # Accept Kelvin string like "2700K" or plain int string
            try:
                kelvin = int(str(color_temp).upper().replace("K", "").strip())
                payload["color_temp_kelvin"] = kelvin
            except ValueError:
                pass

    # Determine whether entity_id_or_area is a direct entity or area name
    if entity_id_or_area.startswith("light.") or "." in entity_id_or_area:
        payload["entity_id"] = entity_id_or_area
    else:
        payload["area_id"] = entity_id_or_area

    url = f"{_base_url()}/api/services/light/{service}"
    try:
        resp = httpx.post(url, headers=_headers(), json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        action_word = "turned off" if state == "off" else f"set to {state}"
        result = f"Lights in {entity_id_or_area} {action_word}, sir."
        return format_for_voice(result)
    except httpx.HTTPStatusError as exc:
        log.error("Home Assistant light error: %s", exc)
        return format_for_voice(f"Light control failed: {exc.response.status_code}.")
    except Exception as exc:
        log.error("Home Assistant light error: %s", exc)
        return "Could not reach Home Assistant, sir."


def control_temperature(temp_f: float) -> str:
    if not is_configured():
        return "Home control not configured, sir."

    payload = {"temperature": temp_f}
    url = f"{_base_url()}/api/services/climate/set_temperature"
    try:
        resp = httpx.post(url, headers=_headers(), json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = f"Thermostat set to {temp_f:.0f}°F, sir."
        return format_for_voice(result)
    except httpx.HTTPStatusError as exc:
        log.error("Home Assistant climate error: %s", exc)
        return format_for_voice(f"Temperature control failed: {exc.response.status_code}.")
    except Exception as exc:
        log.error("Home Assistant climate error: %s", exc)
        return "Could not reach Home Assistant, sir."


def set_scene(scene_name: str) -> str:
    if not is_configured():
        return "Home control not configured, sir."

    scene_name = scene_name.lower().strip()

    scenes: dict[str, dict] = {
        "cozy": {
            "brightness_pct": 30,
            "color_temp_kelvin": 2700,
            "temp_f": 70.0,
            "label": "Cosy scene activated, sir.",
        },
        "focus": {
            "brightness_pct": 90,
            "color_temp_kelvin": 5000,
            "temp_f": 72.0,
            "label": "Focus scene activated, sir.",
        },
        "evening": {
            "brightness_pct": 40,
            "color_temp_kelvin": 2700,
            "temp_f": 71.0,
            "label": "Evening scene activated, sir.",
        },
        "off": {
            "brightness_pct": None,
            "color_temp_kelvin": None,
            "temp_f": None,
            "label": "All lights off, sir.",
        },
    }

    if scene_name not in scenes:
        known = ", ".join(scenes.keys())
        return format_for_voice(f"Unknown scene. Known scenes: {known}.")

    cfg = scenes[scene_name]
    base = _base_url()
    headers = _headers()
    errors: list[str] = []

    # Lights
    try:
        if scene_name == "off":
            resp = httpx.post(
                f"{base}/api/services/light/turn_off",
                headers=headers,
                json={},
                timeout=_TIMEOUT,
            )
        else:
            light_payload: dict = {
                "brightness_pct": cfg["brightness_pct"],
                "color_temp_kelvin": cfg["color_temp_kelvin"],
            }
            resp = httpx.post(
                f"{base}/api/services/light/turn_on",
                headers=headers,
                json=light_payload,
                timeout=_TIMEOUT,
            )
        resp.raise_for_status()
    except Exception as exc:
        log.error("Scene light error: %s", exc)
        errors.append("lights")

    # Climate (skip for "off" scene)
    if cfg["temp_f"] is not None:
        try:
            resp = httpx.post(
                f"{base}/api/services/climate/set_temperature",
                headers=headers,
                json={"temperature": cfg["temp_f"]},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("Scene climate error: %s", exc)
            errors.append("thermostat")

    if errors:
        failed = " and ".join(errors)
        return format_for_voice(f"Scene partially applied; {failed} failed, sir.")

    return format_for_voice(cfg["label"])


def get_status() -> str:
    if not is_configured():
        return "Home control not configured, sir."

    url = f"{_base_url()}/api/states"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        states = resp.json()
    except Exception as exc:
        log.error("Home Assistant status error: %s", exc)
        return "Could not retrieve home status, sir."

    lights_on: list[str] = []
    climate_info: list[str] = []

    for entity in states:
        eid: str = entity.get("entity_id", "")
        state: str = entity.get("state", "")
        attrs: dict = entity.get("attributes", {})

        if eid.startswith("light.") and state == "on":
            friendly = attrs.get("friendly_name", eid.replace("light.", ""))
            lights_on.append(friendly)

        if eid.startswith("climate."):
            friendly = attrs.get("friendly_name", eid.replace("climate.", ""))
            current = attrs.get("current_temperature", "?")
            target = attrs.get("temperature", "?")
            climate_info.append(f"{friendly}: {current}°/{target}°F")

    parts: list[str] = []
    if lights_on:
        parts.append(f"{len(lights_on)} light(s) on: {', '.join(lights_on[:3])}")
    else:
        parts.append("All lights off")

    if climate_info:
        parts.append("; ".join(climate_info[:2]))

    result = ". ".join(parts) + ", sir." if parts else "No devices found, sir."
    return format_for_voice(result)
