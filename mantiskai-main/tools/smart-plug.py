"""TP-Link Kasa smart-plug control for the MantisKAI PC Bridge.

Adds `/api/plugs` and `/api/plug/<name>` to the existing Flask app, exactly like
`openai_compat.py`: a blueprint, so the custom `/api/chat`, `/api/stt`,
`/api/image` routes the Mantis client and the Tk window already use are not
touched.

The plug is the HS100 (Kasa, not Tapo), controlled **locally** through
python-kasa — no TP-Link cloud, no reverse-engineered MQTT. The HS100 speaks
either the original unencrypted protocol (port 9999) or KLAP, depending on
firmware; python-kasa tries both, but KLAP firmware needs the TP-Link account
username/password you use in the Kasa app, so pass those in config if the plug
refuses to answer.

    pip install "python-kasa>=0.7"

Usage in the bridge, after `app = Flask(__name__)`:

    from smart_plug import add_plug_control

    add_plug_control(app, config_getter=lambda: config, log=print)

config.json:

    "plugs": {
      "heater": {"host": "192.168.1.50", "alias": "Heater"},
      "fan":    {"host": "192.168.1.51", "username": "me@example.com",
                 "password": "..."}
    }

`"plugs": {"heater": "192.168.1.50"}` and a bare `"plug_host": "192.168.1.50"`
are accepted too (the latter becomes a plug named "plug").

HTTP:

    GET  /api/plugs                 -> every plug + state (per-plug errors kept)
    GET  /api/plug/heater           -> one plug, 404 if unknown
    POST /api/plug/heater           -> {"action": "on"|"off"|"toggle"|"status"}
                                       (also accepts {"on": true})
    POST /api/plug/heater/on        -> shorthand for the above

Each call opens a connection, does its one thing and disconnects: ~0.1-1 s on a
LAN, and nothing is left holding a socket or an asyncio loop between requests,
which keeps it safe under Flask's threaded server. Give the plug a DHCP
reservation on your router — the HS100 answers on whatever address it was given.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading

from flask import Blueprint, jsonify, request

__all__ = [
    "PlugError",
    "normalize_plugs",
    "PlugManager",
    "add_plug_control",
]

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_ACTIONS = ("on", "off", "toggle", "status")


class PlugError(Exception):
    """A plug problem that maps onto an HTTP status."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def normalize_plugs(raw, log=print):
    """Turn whatever shape config.json has into {name: {host, username, password}}.

    Accepts a dict of dicts, a dict of host strings, a bare host string, or an
    empty/None value. Names are lowercased so lookups are case-insensitive.
    Entries without a usable host are dropped with a warning rather than
    breaking the bridge at startup.
    """
    plugs = {}

    if isinstance(raw, str):
        raw = {"plug": raw}
    if not isinstance(raw, dict):
        return plugs

    for key, value in raw.items():
        if isinstance(value, str):
            value = {"host": value}
        if not isinstance(value, dict):
            log("[PLUG] ignoring %r: expected a host string or an object" % (key,))
            continue
        name = str(key).strip().lower()
        host = str(value.get("host") or "").strip()
        if not _NAME_RE.match(name):
            log("[PLUG] ignoring %r: names are 1-32 chars of a-z, 0-9, '-' or '_'"
                % (key,))
            continue
        if not host:
            log("[PLUG] ignoring %r: no host configured" % (name,))
            continue
        plugs[name] = {
            "host": host,
            "alias": str(value.get("alias") or name).strip(),
            "username": str(value.get("username") or "").strip(),
            "password": str(value.get("password") or ""),
        }
    return plugs


class PlugManager:
    """One connect-action-disconnect cycle per call, serialised by a lock.

    `discover` and `credentials_factory` are swapped out by the tests; the
    module-level imports of `kasa` happen lazily so importing this file on a
    machine without python-kasa still works (the bridge only needs it when a
    plug is actually configured).
    """

    def __init__(self, plugs, *, timeout=6.0, log=print,
                 discover=None, credentials_factory=None):
        self.plugs = dict(plugs)
        self.timeout = timeout
        self.log = log
        self._discover = discover
        self._credentials_factory = credentials_factory
        self._lock = threading.Lock()

    # -- discovery ---------------------------------------------------------

    def _deps(self):
        if self._discover is None or self._credentials_factory is None:
            try:
                from kasa import Discover
                from kasa.credentials import Credentials
            except ImportError as exc:  # pragma: no cover - setup problem
                raise PlugError(
                    'python-kasa is not installed: pip install "python-kasa>=0.7"',
                    500) from exc
            if self._discover is None:
                self._discover = Discover.discover_single
            if self._credentials_factory is None:
                self._credentials_factory = Credentials
        return self._discover, self._credentials_factory

    async def _open(self, entry):
        discover, credentials_factory = self._deps()
        credentials = None
        if entry.get("username") or entry.get("password"):
            credentials = credentials_factory(
                username=entry.get("username") or "",
                password=entry.get("password") or "")
        device = await discover(
            entry["host"],
            credentials=credentials,
            discovery_timeout=int(self.timeout),
            timeout=int(self.timeout),
        )
        if device is None:
            raise PlugError(
                "no response from %s — check the plug is powered, on the same "
                "VLAN, and that its IP is current" % entry["host"])
        return device

    # -- one operation -----------------------------------------------------

    async def _run(self, name, action):
        entry = self.plugs[name]
        device = await self._open(entry)
        try:
            is_on = bool(await _maybe(device.is_on))
            if action == "toggle":
                if is_on:
                    await device.turn_off()
                else:
                    await device.turn_on()
                is_on = not is_on
            elif action == "on" and not is_on:
                await device.turn_on()
                is_on = True
            elif action == "off" and is_on:
                await device.turn_off()
                is_on = False

            if action in ("on", "off", "toggle"):
                # Read back rather than trusting our own flag.
                try:
                    await device.update()
                    is_on = bool(await _maybe(device.is_on))
                except Exception:
                    pass
            return await self._describe(name, entry, device, is_on)
        finally:
            try:
                await device.disconnect()
            except Exception:
                pass

    async def _describe(self, name, entry, device, is_on):
        info = dict(await _maybe(getattr(device, "sys_info", {}) or {}))
        state = dict(await _maybe(getattr(device, "state_information", {}) or {}))
        on_since = None
        for key, value in state.items():
            if str(key).lower().startswith("on since"):
                on_since = str(value)
                break
        return {
            "name": name,
            "host": entry["host"],
            "alias": str(info.get("alias") or entry.get("alias") or name),
            "model": str(info.get("model") or ""),
            "on": is_on,
            "on_since": on_since,
            "rssi": info.get("rssi"),
            "mac": info.get("mac"),
            "firmware": info.get("sw_ver"),
            "hardware": info.get("hw_ver"),
        }

    # -- public API --------------------------------------------------------

    def one(self, name, action="status"):
        name = (name or "").strip().lower()
        action = (action or "status").strip().lower()
        if action not in _ACTIONS:
            raise PlugError("unknown action %r (use %s)"
                            % (action, ", ".join(_ACTIONS)), 400)
        if name not in self.plugs:
            raise PlugError("unknown plug %r" % (name,), 404)
        try:
            with self._lock:
                return asyncio.run(self._run(name, action))
        except PlugError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise PlugError("%s did not answer within %ss"
                            % (self.plugs[name]["host"], self.timeout), 504) from exc
        except OSError as exc:
            raise PlugError("%s unreachable: %s" % (self.plugs[name]["host"], exc),
                            502) from exc
        except Exception as exc:
            text = str(exc) or exc.__class__.__name__
            hint = ""
            if "auth" in text.lower() or "credential" in text.lower():
                hint = (" — this firmware needs the TP-Link account username/"
                        "password from the Kasa app in config.json")
            raise PlugError("%s: %s%s" % (self.plugs[name]["host"], text, hint),
                            502) from exc

    def all(self):
        results = []
        for name in self.plugs:
            try:
                results.append(self.one(name))
            except PlugError as exc:
                results.append({
                    "name": name,
                    "host": self.plugs[name]["host"],
                    "alias": self.plugs[name].get("alias") or name,
                    "error": str(exc),
                })
        return results


# python-kasa has moved some attributes between plain properties and
# coroutine properties across releases; accept either.
async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


def add_plug_control(app, *, config_getter=None, plugs=None, timeout=6.0,
                     log=print, manager=None, blueprint_name="plug_control"):
    """Register the plug endpoints on `app` and return (blueprint, manager).

    config_getter -> callable returning the bridge's config dict; the plug table
                     is re-read per request, so the Settings tab's
                     "Save & Apply" takes effect without a restart.
    manager       -> pre-built PlugManager (tests); overrides the config lookup.
    """
    if manager is None:
        def current_plugs():
            if plugs is not None:
                return normalize_plugs(plugs, log=log)
            cfg = (config_getter() or {}) if config_getter else {}
            table = cfg.get("plugs")
            if not table and cfg.get("plug_host"):
                table = {"plug": cfg["plug_host"]}
            return normalize_plugs(table, log=log)

        manager = _LateBoundManager(current_plugs, timeout=timeout, log=log)

    bp = Blueprint(blueprint_name, __name__)

    def _fail(exc):
        return jsonify({"error": str(exc), "plugs": sorted(manager.plugs)}), exc.status

    @bp.get("/api/plugs")
    def list_plugs():
        return jsonify({"plugs": manager.all()})

    @bp.get("/api/plug/<name>")
    def plug_status(name):
        try:
            return jsonify(manager.one(name, "status"))
        except PlugError as exc:
            return _fail(exc)

    @bp.route("/api/plug/<name>", methods=["POST"])
    @bp.route("/api/plug/<name>/<action>", methods=["POST"])
    def plug_action(name, action=None):
        data = request.get_json(silent=True) or {}
        chosen = action or data.get("action") or request.args.get("action")
        if not chosen and isinstance(data.get("on"), bool):
            chosen = "on" if data["on"] else "off"
        try:
            return jsonify(manager.one(name, chosen or "status"))
        except PlugError as exc:
            return _fail(exc)

    app.register_blueprint(bp)
    log("[PLUG] control ready: /api/plugs, /api/plug/<name>")
    return bp, manager


class _LateBoundManager(PlugManager):
    """Re-reads the plug table from config on every request."""

    def __init__(self, supplier, *, timeout=6.0, log=print):
        self._supplier = supplier
        self._log = log
        super().__init__({}, timeout=timeout, log=log)
        self._refresh()

    def _refresh(self):
        plugs = self._supplier()
        if plugs != self.plugs:
            self.plugs = plugs
            self._log("[PLUG] configured: %s" % (", ".join(sorted(plugs)) or "none"))

    def one(self, name, action="status"):
        self._refresh()
        return super().one(name, action)

    def all(self):
        self._refresh()
        return super().all()
