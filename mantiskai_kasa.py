"""
MantisKAI local TP-Link Kasa HS100 control.

Finds the configured plug by MAC address using LAN discovery, so its IP can
change without requiring a code edit. Requires: python -m pip install python-kasa
"""
import asyncio
import re
from flask import jsonify, request

KASA_TARGET_MAC = "D8:0D:17:AE:1D:E6"


def _normalize_mac(value):
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


async def _find_kasa_device():
    try:
        from kasa import Discover
    except ImportError as exc:
        raise RuntimeError(
            "Kasa support needs python-kasa. Install it with: "
            "python -m pip install python-kasa"
        ) from exc

    target = _normalize_mac(KASA_TARGET_MAC)
    devices = await Discover.discover()
    for ip, device in devices.items():
        try:
            await device.update()
            mac = getattr(device, "mac", None)
            if _normalize_mac(mac) == target:
                return ip, device
        except Exception as exc:
            print(f"[KASA] Could not inspect discovered device {ip}: {exc}")

    raise LookupError(
        f"HS100 with MAC {KASA_TARGET_MAC} was not found by LAN discovery. "
        "Check that it is powered on and on the same LAN as this PC."
    )


async def _perform_kasa_action(action):
    ip, device = await _find_kasa_device()
    if action == "on":
        await device.turn_on()
    elif action == "off":
        await device.turn_off()

    # Refresh state after any command; status is read from the plug itself.
    await device.update()
    is_on = bool(device.is_on)
    alias = getattr(device, "alias", None) or "Kasa HS100"
    return {
        "status": "success",
        "device": alias,
        "mac": KASA_TARGET_MAC,
        "ip": str(ip),
        "is_on": is_on,
        "state": "on" if is_on else "off",
        "action": action,
    }


def kasa_action(action):
    """Synchronous bridge for Flask handlers."""
    try:
        result = asyncio.run(_perform_kasa_action(action))
        print(f"[KASA] {result['device']} ({result['ip']}) → {result['state']}")
        return result, 200
    except LookupError as exc:
        print(f"[KASA] Not found: {exc}")
        return {"status": "error", "error": str(exc)}, 404
    except Exception as exc:
        print(f"[KASA] {action} failed: {exc}")
        return {"status": "error", "error": str(exc)}, 502


def handle_kasa_chat(message):
    """
    Return a short response for explicit Kasa plug commands, or None when the
    message is not a Kasa command (so normal MantisKAI chat proceeds).
    """
    text = (message or "").strip().lower()
    has_device = any(word in text for word in ("kasa", "plug", "outlet", "smart plug", "hs100"))
    if not has_device:
        return None

    if any(p in text for p in (
        "status", "state", "is it on", "is it off", "check the plug",
        "check the outlet", "check kasa", "what state"
    )):
        action = "status"
    elif any(p in text for p in (
        "turn on", "switch on", "power on", "plug on", "outlet on",
        "kasa on", "switch the plug on", "enable the plug"
    )):
        action = "on"
    elif any(p in text for p in (
        "turn off", "switch off", "power off", "plug off", "outlet off",
        "kasa off", "switch the plug off", "disable the plug"
    )):
        action = "off"
    else:
        return None

    result, code = kasa_action(action)
    if code != 200:
        return f"I couldn't control the Kasa plug: {result.get('error', 'unknown error')}"
    if action == "on":
        return "Kasa outlet is on."
    if action == "off":
        return "Kasa outlet is off."
    return f"Kasa outlet is {result['state']}."


def register_kasa_routes(app):
    """Register direct LAN endpoints, in addition to chat commands."""
    @app.route("/api/kasa/on", methods=["GET", "POST"])
    def api_kasa_on():
        result, code = kasa_action("on")
        return jsonify(result), code

    @app.route("/api/kasa/off", methods=["GET", "POST"])
    def api_kasa_off():
        result, code = kasa_action("off")
        return jsonify(result), code

    @app.route("/api/kasa/status", methods=["GET", "POST"])
    def api_kasa_status():
        result, code = kasa_action("status")
        return jsonify(result), code
