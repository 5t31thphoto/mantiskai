"""
MantisKAI PC Bridge
  POST /api/chat   → {"reply": "...", "lora": "..."}          small JSON
  POST /api/tts    → raw WAV bytes                             binary
  POST /api/stt    → {"text": "..."}                           accepts binary WAV/PCM
  POST /api/image  → {"image_base64": "...", "lora": "..."}
  GET|POST /api/lora
  GET|POST /api/profile                                        C-button profile cycle
"""

import threading
import time
import random
import wave
import base64
import os
import json
import tempfile
import re
from io import BytesIO
import urllib.request
from PIL import Image, ImageTk, ImageOps
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox, filedialog
from flask import Flask, request, jsonify, Response, has_request_context
from mantiskai_kasa import handle_kasa_chat, register_kasa_routes
import mantiskai_profiles as mp
from openai import OpenAI

try:
    from piper import PiperVoice
    from piper.config import SynthesisConfig
except ImportError:
    PiperVoice = None

try:
    import pygame
    pygame.mixer.init()
except Exception:
    pygame = None

# Local openai-whisper (CUDA if available)
_whisper_model = None
def get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        import torch
        import whisper
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # "base" is fast; change to "small" / "medium" / "large" if you want better accuracy
        _whisper_model = whisper.load_model("base", device=device)
        print(f"[STT] openai-whisper loaded on {device}")
        return _whisper_model
    except Exception as e:
        print(f"[STT] openai-whisper load failed: {e}")
    # optional fallback
    try:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        print("[STT] faster-whisper fallback loaded")
        return _whisper_model
    except Exception:
        pass
    return None

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "api_base_url": "http://localhost:1234/v1",
    "ai_model": "l3_2-rogue-creative-instruct-uncensored-abliterated-7b-d_au",
    "realistic_model": "v1-5-pruned-emaonly.safetensors",
    "anime_model": "animePastelDream_hardV2.safetensors",
    "lora_list": "detailer.safetensors, cyberpunk.safetensors",
    "lora_strength": 0.8,
    "comfy_prefix": "masterpiece, high quality, digital art, 8k, ",
    "comfy_negative": "ugly, blurry, distorted, low quality, bad anatomy",
    "system_prompt": "You are a helpful, friendly AI assistant communicating through an M5Stack Core2 device.",
    "stt_api_base": "",          # optional OpenAI-compatible STT endpoint, e.g. http://localhost:8080/v1
    "stt_api_key": "lm-studio",
    "active_profile": "",
    "hidden_lora_list": "",       # only offered after the hidden-options keyword
    "hidden_keyword": "",         # "keyword for hidden options"; empty = disabled
}

# Sliding window: system + this many non-system messages (user/assistant pairs)
MAX_HISTORY_MESSAGES = 20

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(data)
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass

config = load_config()
API_BASE_URL = config["api_base_url"]
USER_API_KEY = "lm-studio"
AI_MODEL = config["ai_model"]
TTS_VOICE_PATH = "assets/voices/en_GB-vctk-medium.onnx"
TTS_VOLUME = 0.8
FLASK_PORT = 5000
COMFY_URL = config.get("comfy_url", "127.0.0.1:8000")
REALISTIC_MODEL = config["realistic_model"]
ANIME_MODEL = config["anime_model"]
LORA_LIST = [x.strip() for x in config["lora_list"].split(",") if x.strip()]
HIDDEN_LORA_LIST = [x.strip() for x in config.get("hidden_lora_list", "").split(",") if x.strip()]
HIDDEN_KEYWORD = config.get("hidden_keyword", "").strip()
LORA_STRENGTH = float(config["lora_strength"])
HARDCODED_POSITIVE_PREFIX = config["comfy_prefix"]
HARDCODED_NEGATIVE = config.get("comfy_negative", DEFAULT_CONFIG["comfy_negative"])
SYSTEM_PROMPT = config["system_prompt"]
STT_API_BASE = config.get("stt_api_base", "")
STT_API_KEY = config.get("stt_api_key", "lm-studio")

# ComfyUI profiles (C button on the Core2). First run creates Realistic/Anime
# from the old checkpoint settings so nothing changes until you add more.
profiles = mp.ProfileStore(mp.PROFILES_DIR)
if not profiles.names():
    for _name, _ckpt in (("Realistic", REALISTIC_MODEL), ("Anime", ANIME_MODEL)):
        profiles.save(mp.basic_profile(_name, _ckpt, HARDCODED_POSITIVE_PREFIX + "{prompt}",
                                       HARDCODED_NEGATIVE))
profiles.set_active(config.get("active_profile") or "Realistic")

chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]
client = OpenAI(base_url=API_BASE_URL, api_key=USER_API_KEY)

# One heavy GPU/LLM job at a time (chat or image). Extra requests get 503.
_gpu_lock = threading.Lock()
_gpu_busy = False  # informational; lock is the authority


def trim_chat_history(hard=False):
    """Keep system prompt + recent turns. Proven sliding window — no summarize needed."""
    global chat_history
    limit = 8 if hard else MAX_HISTORY_MESSAGES
    if not chat_history:
        chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]
        return
    system = chat_history[0] if chat_history[0].get("role") == "system" else {
        "role": "system", "content": SYSTEM_PROMPT
    }
    rest = [m for m in chat_history if m.get("role") != "system"]
    if len(rest) <= limit:
        chat_history = [system] + rest
        return
    chat_history = [system] + rest[-limit:]
    print(f"[CTX] trimmed history → {len(chat_history)} messages (limit={limit})")

llm_voice = None
llm_sample_rate = 22050
if PiperVoice:
    try:
        llm_voice = PiperVoice.load(TTS_VOICE_PATH)
        llm_sample_rate = llm_voice.config.sample_rate
    except Exception as e:
        print(f"[TTS] load failed: {e}")

app = Flask(__name__)

register_kasa_routes(app)

@app.before_request
def log_request():
    print(f"[HTTP] {request.method} {request.path} from {request.remote_addr}")

def update_system_prompt(new_prompt):
    global chat_history, SYSTEM_PROMPT
    SYSTEM_PROMPT = new_prompt
    chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]

# ------------------------------------------------------------------
# Per-device sessions
# Each Core2 sends a random X-Session id that changes every boot / Wi-Fi
# connect. Its selected profile + LoRA and the hidden-options unlock live
# here, so other devices on the network are unaffected. Idle sessions expire.
# ------------------------------------------------------------------
SESSION_IDLE_SECS = 30 * 60
_sessions = {}
_sessions_lock = threading.RLock()
_last_device_key = None


def session(key=None):
    global _last_device_key
    ip = ""
    if key is None:
        if has_request_context():
            ip = request.remote_addr or ""
            key = request.headers.get("X-Session") or f"ip:{ip}"
        else:
            key = _last_device_key or "gui"
    with _sessions_lock:
        now = time.time()
        for k in [k for k, v in _sessions.items()
                  if k != "gui" and now - v["seen"] > SESSION_IDLE_SECS]:
            del _sessions[k]
            if k == _last_device_key:
                _last_device_key = None
        s = _sessions.get(key)
        if s is None:
            s = {"unlocked": False, "profile": "", "lora": "None", "seen": now, "ip": ip}
            _sessions[key] = s
        if ip:
            s["seen"], s["ip"] = now, ip
            _last_device_key = key
        _sanitize_session(s)
        return s


def visible_loras(s):
    return LORA_LIST + (HIDDEN_LORA_LIST if s["unlocked"] else [])


def visible_profiles(s):
    return [n for n, hidden in profiles.entries() if s["unlocked"] or not hidden]


def _sanitize_session(s):
    names = visible_profiles(s)
    if s["profile"] not in names:
        default = profiles.active_name()
        s["profile"] = default if default in names else (names[0] if names else "")
    if s["lora"] != "None" and s["lora"] not in visible_loras(s):
        s["lora"] = "None"


def check_unlock(text, s):
    """If the hidden-options keyword is in the text, unlock this session.
    Returns the text with the keyword removed (for image prompts)."""
    kw = HIDDEN_KEYWORD.strip()
    if not kw or not text:
        return text
    pat = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
    if not pat.search(text):
        return text
    if not s["unlocked"]:
        s["unlocked"] = True
        print(f"[SESSION] hidden options unlocked for {s.get('ip') or 'this session'}")
    stripped = pat.sub("", text).strip(" ,.;:!?-")
    return stripped or text


def current_lora_name(s=None):
    return (s or session())["lora"]


def cycle_lora(s=None):
    s = s or session()
    names = visible_loras(s)
    i = names.index(s["lora"]) + 1 if s["lora"] in names else 0
    s["lora"] = names[i] if i < len(names) else "None"
    return s["lora"]


def clear_lora(s=None):
    s = s or session()
    s["lora"] = "None"
    return "None"


def cycle_profile(s=None):
    s = s or session()
    names = visible_profiles(s)
    if not names:
        return ""
    i = names.index(s["profile"]) + 1 if s["profile"] in names else 0
    s["profile"] = names[i % len(names)]
    return s["profile"]


def set_active_profile(name):
    """Default profile for new devices; also applied to devices that can see it."""
    name = profiles.set_active(name)
    config["active_profile"] = name
    save_config(config)
    with _sessions_lock:
        for s in _sessions.values():
            if name in visible_profiles(s):
                s["profile"] = name
    return name


def device_status_text():
    with _sessions_lock:
        if not _last_device_key or _last_device_key not in _sessions:
            return "No Core2 connected yet"
        s = _sessions[_last_device_key]
        return f"Core2 {s['ip']}: {s['profile'] or '-'} | LoRA: {s['lora']}"

def split_tts_chunks(text, max_chars=140):
    """
    Bite-sized speakable segments. Prefer break at . ! ? ; :
    Shorten to the break — never lengthen past max_chars.
    """
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    remaining = text
    punct = set(".!?;:")
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining.strip())
            break
        window = remaining[:max_chars]
        best = -1
        # Prefer punctuation in the second half of the window
        for i in range(len(window) - 1, max_chars // 2 - 1, -1):
            if window[i] in punct:
                best = i + 1
                break
        if best < 0:
            # Fall back to last space in window
            for i in range(len(window) - 1, max_chars // 2 - 1, -1):
                if window[i] == " ":
                    best = i
                    break
        if best < 0:
            best = max_chars
        piece = remaining[:best].strip()
        remaining = remaining[best:].lstrip()
        if piece:
            chunks.append(piece)
    return chunks


def synthesize_wav_bytes(text, speaker_id=0):
    if not text or not llm_voice:
        return None
    clean = text.replace('"', '').replace('*', '').replace('#', '').replace('...', '..')
    try:
        cfg = SynthesisConfig()
        cfg.speaker_id = speaker_id
        buf = BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(llm_sample_rate))
            for chunk in llm_voice.synthesize(clean, cfg):
                wf.writeframes(chunk.audio_int16_bytes)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"[TTS] {e}")
        return None

def speak_locally(text):
    if not text or not llm_voice or not pygame:
        return
    data = synthesize_wav_bytes(text)
    if data:
        try:
            sound = pygame.mixer.Sound(BytesIO(data))
            sound.set_volume(TTS_VOLUME)
            sound.play()
        except Exception:
            pass

def pcm16_to_wav(pcm_bytes, sample_rate=16000):
    """Wrap raw PCM16 mono into a WAV file in memory."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    buf.seek(0)
    return buf.read()

def transcribe_audio(wav_or_pcm_bytes, sample_rate=16000):
    """
    Returns transcribed text or raises.
    Accepts either a full WAV or raw PCM16 mono.
    """
    # Detect WAV header
    if len(wav_or_pcm_bytes) > 12 and wav_or_pcm_bytes[:4] == b"RIFF":
        wav_bytes = wav_or_pcm_bytes
    else:
        wav_bytes = pcm16_to_wav(wav_or_pcm_bytes, sample_rate)

    # 1) Local faster-whisper / whisper
    model = get_whisper()
    if model is not None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name
        try:
            # openai-whisper returns a dict; faster-whisper returns (segments, info)
            result = model.transcribe(tmp_path, language="en")
            if isinstance(result, dict):
                text = (result.get("text") or "").strip()
            else:
                # faster-whisper: (segments, info)
                segments = result[0] if isinstance(result, tuple) else result
                text = " ".join(s.text.strip() for s in segments).strip()
            return text
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # 2) Optional remote OpenAI-compatible STT
    if STT_API_BASE:
        try:
            stt_client = OpenAI(base_url=STT_API_BASE, api_key=STT_API_KEY)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_bytes)
                tmp_path = tmp.name
            try:
                with open(tmp_path, "rb") as audio_file:
                    result = stt_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                    )
                return (result.text or "").strip()
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            print(f"[STT] remote failed: {e}")

    raise RuntimeError(
        "No STT available. Install faster-whisper or openai-whisper, "
        "or set stt_api_base in config.json"
    )

last_image_error = ""

def to_rgb565_320x240(img):
    """Center-crop to 4:3, scale to the Core2 screen, pack little-endian RGB565."""
    img320 = ImageOps.fit(img.convert("RGB"), (320, 240), Image.LANCZOS)
    rgb565 = bytearray()
    for r, g, b in img320.getdata():
        val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        rgb565.append(val & 0xFF)
        rgb565.append((val >> 8) & 0xFF)
    return bytes(rgb565)

def generate_with_profile(profile, user_prompt, selected_lora="", timeout=140):
    """Run one profile (dict). Returns PIL image; raises mp.ProfileError."""
    graph, out_id = mp.build_prompt_graph(profile, user_prompt, selected_lora, LORA_STRENGTH)
    print(f"[Comfy] profile={profile['name']} lora={selected_lora or 'None'} "
          f"{profile.get('width')}x{profile.get('height')}")
    return mp.run_on_comfy(COMFY_URL, graph, out_id, timeout=timeout)

def generate_comfy_image_mg(user_prompt, profile_name="", selected_lora=""):
    """Returns (PIL original, rgb565 bytes) or (None, None) with last_image_error set."""
    global last_image_error
    last_image_error = ""
    profile = profiles.get(profile_name) or profiles.get(profiles.active_name())
    if not profile:
        last_image_error = "No profiles. Create one in the Profiles tab."
        return None, None
    try:
        original = generate_with_profile(profile, user_prompt, selected_lora)
        return original, to_rgb565_320x240(original)
    except Exception as e:
        last_image_error = str(e)
        print(f"[Comfy] {e}")
        return None, None

# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
@app.route("/api/lora", methods=["GET", "POST"])
def api_lora():
    s = session()
    if request.method == "POST":
        action = ((request.json or {}).get("action") or "").strip().lower()
        if action == "cycle":
            cycle_lora(s)
        elif action == "clear":
            clear_lora(s)
        else:
            return jsonify({"error": "unknown action"}), 400
        print(f"[LoRA] {s['ip']} → {s['lora']}")
    names = visible_loras(s)
    return jsonify({"lora": s["lora"], "index": names.index(s["lora"]) if s["lora"] in names else -1,
                    "list": names})

def profile_state(s):
    names = visible_profiles(s)
    return {"profile": s["profile"], "index": names.index(s["profile"]) if s["profile"] in names else -1,
            "list": names, "lora": s["lora"]}

@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    s = session()
    if request.method == "POST":
        data = request.json or {}
        action = (data.get("action") or "").strip().lower()
        if action == "cycle":
            cycle_profile(s)
        elif action == "set":
            name = (data.get("name") or "").strip()
            if name in visible_profiles(s):
                s["profile"] = name
        else:
            return jsonify({"error": "unknown action"}), 400
        print(f"[PROFILE] {s['ip']} → {s['profile']}")
    return jsonify(profile_state(s))

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    user_message = (data.get("message") or "").strip()
    sess = session()
    check_unlock(user_message, sess)   # chat text goes to the LLM unchanged
    lower = user_message.lower()
    if lower in ("/lora", "/lora cycle", "lora cycle"):
        return jsonify({"reply": f"LoRA → {cycle_lora(sess)}", "lora": sess["lora"]})
    if lower in ("/lora clear", "lora clear", "/lora none"):
        return jsonify({"reply": f"LoRA → {clear_lora(sess)}", "lora": sess["lora"]})
    if not user_message:
        return jsonify({"error": "Empty message", "lora": current_lora_name()}), 400

    # Local Kasa HS100 commands are handled directly; no LLM call required.
    kasa_reply = handle_kasa_chat(user_message)
    if kasa_reply is not None:
        return jsonify({"reply": kasa_reply, "lora": current_lora_name()})


    # Non-blocking: reject if LLM/Comfy already running (protect 8GB VRAM)
    if not _gpu_lock.acquire(blocking=False):
        print("[CHAT] rejected — GPU/LLM busy")
        return jsonify({"error": "Server busy – wait for current job.", "lora": current_lora_name()}), 503

    global _gpu_busy
    _gpu_busy = True
    try:
        chat_history.append({"role": "user", "content": user_message})
        trim_chat_history()
        try:
            response = client.chat.completions.create(
                model=AI_MODEL, messages=chat_history, temperature=0.7, timeout=100.0)
            reply = response.choices[0].message.content or ""
            chat_history.append({"role": "assistant", "content": reply})
            return jsonify({"reply": reply, "lora": current_lora_name()})
        except Exception as e:
            err = str(e).lower()
            # Context overflow → hard-trim and retry once (proven recovery)
            if any(k in err for k in ("context", "token", "length", "too long", "maximum")):
                print(f"[CTX] overflow, hard-trim + retry: {e}")
                trim_chat_history(hard=True)
                try:
                    response = client.chat.completions.create(
                        model=AI_MODEL, messages=chat_history, temperature=0.7, timeout=100.0)
                    reply = response.choices[0].message.content or ""
                    chat_history.append({"role": "assistant", "content": reply})
                    return jsonify({"reply": reply, "lora": current_lora_name()})
                except Exception as e2:
                    return jsonify({"error": str(e2), "lora": current_lora_name()}), 500
            return jsonify({"error": str(e), "lora": current_lora_name()}), 500
    finally:
        _gpu_busy = False
        _gpu_lock.release()

@app.route("/api/tts", methods=["POST"])
def api_tts():
    """
    Return raw PCM16 mono (no WAV header) for one speakable chunk.
    Body: {"text": "<full reply>", "chunk": 0}
      chunk omitted or 0 = first segment; device increments until X-More is 0.
    Headers:
      X-Sample-Rate, X-Chunk-Index, X-Chunk-Total, X-More (1|0)
    """
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return Response(b"", status=400)

    try:
        chunk_idx = int(data.get("chunk", 0))
    except (TypeError, ValueError):
        chunk_idx = 0
    if chunk_idx < 0:
        chunk_idx = 0

    segments = split_tts_chunks(text)
    total = len(segments)
    if total == 0:
        return Response(b"", status=400)
    if chunk_idx >= total:
        return Response(
            b"",
            status=204,
            headers={
                "X-Chunk-Index": str(chunk_idx),
                "X-Chunk-Total": str(total),
                "X-More": "0",
            },
        )

    piece = segments[chunk_idx]
    wav = synthesize_wav_bytes(piece)
    if not wav or len(wav) < 44:
        print(f"[TTS] synth failed chunk {chunk_idx}/{total}: {piece[:40]!r}")
        return Response(b"", status=503)

    pcm = wav[44:]
    rate = int(llm_sample_rate)
    more = 1 if (chunk_idx + 1) < total else 0
    print(f"[TTS] chunk {chunk_idx + 1}/{total} ({len(piece)} chars) → {len(pcm)} pcm @ {rate} Hz more={more}")
    return Response(
        pcm,
        mimetype="application/octet-stream",
        headers={
            "Content-Length": str(len(pcm)),
            "X-Sample-Rate": str(rate),
            "X-Chunk-Index": str(chunk_idx),
            "X-Chunk-Total": str(total),
            "X-More": str(more),
        },
    )

@app.route("/api/stt", methods=["POST"])
def api_stt():
    """
    Accept binary audio body.
    Content-Type: application/octet-stream  (raw PCM16 mono 16kHz)
                 or audio/wav
    Query/header optional: sample_rate=16000
    Returns {"text": "..."}
    """
    raw = request.get_data()
    if not raw or len(raw) < 100:
        return jsonify({"error": "No audio data"}), 400

    sample_rate = 16000
    if request.args.get("sample_rate"):
        try:
            sample_rate = int(request.args.get("sample_rate"))
        except Exception:
            pass

    try:
        text = transcribe_audio(raw, sample_rate=sample_rate)
        print(f"[STT] → {text[:80]}")
        return jsonify({"text": text})
    except Exception as e:
        print(f"[STT] error: {e}")
        return jsonify({"error": str(e)}), 503

@app.route("/api/image", methods=["POST"])
def api_image():
    """
    Success → raw RGB565 bytes (320x240), headers carry meta.
    Failure → small JSON error.
    """
    data = request.json or {}
    sess = session()
    prompt = check_unlock((data.get("prompt") or "").strip(), sess)
    # Only what this device is allowed to see; otherwise its own selection
    profile_name = (data.get("profile") or "").strip()
    if profile_name not in visible_profiles(sess):
        profile_name = sess["profile"]
    selected = (data.get("lora") or "").strip()
    if selected not in visible_loras(sess):
        selected = sess["lora"]
    if selected == "None":
        selected = ""
    if not prompt:
        return jsonify({"error": "Empty prompt", "lora": current_lora_name()}), 400

    # Same lock as chat — one heavy job on the GPU at a time
    if not _gpu_lock.acquire(blocking=False):
        print("[IMG] rejected — GPU/LLM busy")
        return jsonify({"error": "Server busy – wait for current job.", "lora": current_lora_name()}), 503

    global _gpu_busy
    _gpu_busy = True
    try:
        pil, rgb565 = generate_comfy_image_mg(prompt, profile_name=profile_name, selected_lora=selected)
        if not rgb565:
            return jsonify({"error": last_image_error or "Image generation failed",
                            "lora": current_lora_name()}), 500
        print(f"[IMG] {len(rgb565)} rgb565 bytes, profile={profile_name}, lora={selected or 'None'}")
        return Response(
            rgb565,
            mimetype="application/octet-stream",
            headers={
                "Content-Length": str(len(rgb565)),
                "X-Lora": selected or "None",
                "X-Profile": profile_name,
                "X-Width": "320",
                "X-Height": "240",
            },
        )
    finally:
        _gpu_busy = False
        _gpu_lock.release()

# ------------------------------------------------------------------
# GUI (unchanged structure)
# ------------------------------------------------------------------
class Core2ServerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MantisKAI – PC Bridge")
        self.root.geometry("1120x880")
        self.root.configure(bg="#121212")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#121212", foreground="#ffffff", fieldbackground="#1e1e1e")
        style.configure("TNotebook", background="#121212", borderwidth=0)
        style.configure("TNotebook.Tab", background="#2a2a2a", foreground="#ffffff",
                        padding=[12, 8], font=("Arial", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#007373")])
        style.configure("TFrame", background="#121212")
        style.configure("TLabel", background="#121212", foreground="#ffffff", font=("Arial", 10))
        style.configure("Header.TLabel", background="#121212", foreground="#00ff00",
                        font=("Arial", 11, "bold"))
        style.configure("TButton", background="#5d005d", foreground="#ffffff",
                        font=("Arial", 10, "bold"), borderwidth=0)
        style.map("TButton", background=[("active", "#007373")])
        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True)
        self.chat_tab = ttk.Frame(nb)
        self.image_tab = ttk.Frame(nb)
        self.settings_tab = ttk.Frame(nb)
        self.profiles_tab = ttk.Frame(nb)
        nb.add(self.chat_tab, text="Chat & Status")
        nb.add(self.image_tab, text="Picture Mode")
        nb.add(self.profiles_tab, text="Profiles")
        nb.add(self.settings_tab, text="Settings")
        self.setup_chat()
        self.setup_image()
        self.setup_profiles()
        self.setup_settings()
        self.root.after(500, lambda: threading.Thread(target=self.check_lm, daemon=True).start())

    def setup_chat(self):
        top = ttk.Frame(self.chat_tab, padding=10)
        top.pack(fill=tk.X)
        self.status_label = ttk.Label(top, text="Checking LM Studio...", style="Header.TLabel")
        self.status_label.pack(side=tk.LEFT)
        self.lora_label = ttk.Label(top, text=device_status_text(), style="Header.TLabel")
        self.lora_label.pack(side=tk.RIGHT)
        frame = ttk.Frame(self.chat_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        self.chat_display = scrolledtext.ScrolledText(
            frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#1e1e1e", fg="#00ff00", insertbackground="#ffffff", borderwidth=0)
        self.chat_display.pack(fill=tk.BOTH, expand=True)
        self.chat_display.config(state=tk.DISABLED)
        ctrl = ttk.Frame(self.chat_tab, padding=10)
        ctrl.pack(fill=tk.X)
        self.input_field = tk.Entry(ctrl, font=("Arial", 11), bg="#1e1e1e", fg="#ffffff",
                                    insertbackground="#ffffff", relief=tk.FLAT, highlightthickness=1)
        self.input_field.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 10))
        self.input_field.bind("<Return>", self.send_msg)
        ttk.Button(ctrl, text="Send", command=self.send_msg).pack(side=tk.RIGHT)
        lf = ttk.Frame(self.chat_tab, padding=5)
        lf.pack(fill=tk.X)
        ttk.Button(lf, text="Cycle LoRA", command=self.gui_cycle).pack(side=tk.LEFT, padx=5)
        ttk.Button(lf, text="Clear LoRA", command=self.gui_clear).pack(side=tk.LEFT, padx=5)

    # These act on the Core2 that talked to the server most recently
    def gui_cycle(self):
        name = cycle_lora()
        self.lora_label.config(text=device_status_text())
        self.log("SYSTEM", f"LoRA → {name}")

    def gui_clear(self):
        name = clear_lora()
        self.lora_label.config(text=device_status_text())
        self.log("SYSTEM", f"LoRA → {name}")

    def setup_image(self):
        pad = ttk.Frame(self.image_tab, padding=15)
        pad.pack(fill=tk.BOTH, expand=True)
        ctrl = ttk.Frame(pad)
        ctrl.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(ctrl, text="Profile:").grid(row=0, column=0, sticky=tk.W)
        self.img_profile_combo = ttk.Combobox(ctrl, values=profiles.names(), width=24, state="readonly")
        self.img_profile_combo.grid(row=0, column=1, columnspan=2, padx=5)
        self.img_profile_combo.set(profiles.active_name())
        ttk.Label(ctrl, text="LoRA:").grid(row=0, column=3, padx=(20, 5))
        self.lora_combo = ttk.Combobox(ctrl, values=["None"] + LORA_LIST + HIDDEN_LORA_LIST,
                                       width=22, state="readonly")
        self.lora_combo.grid(row=0, column=4)
        self.lora_combo.set("None")
        inp = ttk.Frame(pad)
        inp.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(inp, text="Prompt:").pack(side=tk.LEFT, padx=(0, 8))
        self.img_input = tk.Entry(inp, font=("Arial", 11), bg="#1e1e1e", fg="#ffffff",
                                  insertbackground="#ffffff", relief=tk.FLAT, highlightthickness=1)
        self.img_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 10))
        self.img_input.bind("<Return>", lambda e: self.gen_image())
        ttk.Button(inp, text="Generate", command=self.gen_image).pack(side=tk.RIGHT)
        self.img_label = ttk.Label(pad, text="No image yet.", anchor=tk.CENTER)
        self.img_label.pack(fill=tk.BOTH, expand=True)

    def gen_image(self):
        prompt = self.img_input.get().strip()
        if not prompt:
            return
        prof = self.img_profile_combo.get()
        sel = self.lora_combo.get()
        lora = "" if sel == "None" else sel
        self.img_label.config(text="Generating...")
        def work():
            pil, _ = generate_comfy_image_mg(prompt, profile_name=prof, selected_lora=lora)
            if pil:
                disp = pil.resize((400, 300))
                self.photo = ImageTk.PhotoImage(disp)
                self.root.after(0, lambda: self.img_label.config(image=self.photo, text=""))
                self.root.after(0, lambda: self.log("Comfy", prompt[:50]))
            else:
                err = last_image_error or "Failed"
                self.root.after(0, lambda: self.img_label.config(text=f"Failed: {err}"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Profiles tab: build, import (ComfyUI PNG / API JSON), edit and test
    # ------------------------------------------------------------------
    NODE_NAMES = {
        "CheckpointLoaderSimple": "Checkpoint", "CheckpointLoader": "Checkpoint",
        "UNETLoader": "Diffusion model", "DualCLIPLoader": "Text encoders",
        "CLIPLoader": "Text encoder", "TripleCLIPLoader": "Text encoders",
        "VAELoader": "VAE", "LoraLoader": "LoRA", "LoraLoaderModelOnly": "LoRA (model only)",
        "KSampler": "Sampler", "KSamplerAdvanced": "Sampler (advanced)",
        "KSamplerSelect": "Sampler type", "BasicScheduler": "Scheduler",
        "CFGGuider": "CFG guider", "FluxGuidance": "Flux guidance",
        "CLIPSetLastLayer": "CLIP skip", "ModelSamplingFlux": "Flux model sampling",
        "ModelSamplingDiscrete": "Model sampling", "LatentUpscale": "Hires: latent upscale",
        "LatentUpscaleBy": "Hires: latent upscale",
    }

    def setup_profiles(self):
        self.p_cur = None          # working copy being edited
        self.p_orig_name = None    # its name on disk; None = not saved yet
        self.p_notes = []
        self.p_fields = []         # (node_id, input, kind, tk variable)
        self.p_photo = None
        self.p_last_status = None
        self.comfy_info = {}

        dark = dict(bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff",
                    relief=tk.FLAT, highlightthickness=1)
        self.p_dark = dark
        style = ttk.Style()
        style.configure("TCombobox", fieldbackground="#1e1e1e", foreground="#ffffff",
                        background="#2a2a2a", arrowcolor="#ffffff")
        style.map("TCombobox", fieldbackground=[("readonly", "#1e1e1e")],
                  foreground=[("readonly", "#ffffff")])
        style.configure("TSpinbox", fieldbackground="#1e1e1e", foreground="#ffffff",
                        arrowcolor="#ffffff")
        style.configure("TCheckbutton", background="#121212", foreground="#ffffff")
        style.configure("Node.TLabel", background="#1a1a1a", foreground="#00e0c0",
                        font=("Arial", 10, "bold"))
        style.configure("Card.TFrame", background="#1a1a1a")
        style.configure("Card.TLabel", background="#1a1a1a", foreground="#ffffff")
        style.configure("Small.TButton", font=("Arial", 9), padding=[6, 1])

        outer = ttk.Frame(self.profiles_tab, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # ---- left column: list, actions, test
        left = ttk.Frame(outer, width=340)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Profiles (C button order)", style="Header.TLabel").pack(anchor=tk.W)
        self.p_list = tk.Listbox(left, width=40, height=9, font=("Arial", 10),
                                 bg="#1e1e1e", fg="#ffffff", selectbackground="#007373",
                                 exportselection=False, relief=tk.FLAT)
        self.p_list.pack(fill=tk.X, pady=(4, 6))
        self.p_list.bind("<<ListboxSelect>>", self.p_on_select)
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X)
        for i, (text, cmd) in enumerate((("New", self.p_new_basic),
                                         ("Import PNG / JSON", self.p_import),
                                         ("Duplicate", self.p_duplicate),
                                         ("Delete", self.p_delete))):
            ttk.Button(btns, text=text, command=cmd).grid(row=i // 2, column=i % 2, sticky=tk.EW,
                                                          padx=2, pady=2)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        ttk.Button(left, text="Make default for new Core2 sessions",
                   command=self.p_use_on_core2).pack(fill=tk.X, pady=(2, 0))
        self.p_active_label = ttk.Label(left, text="", wraplength=330)
        self.p_active_label.pack(anchor=tk.W, pady=(4, 8))

        ttk.Label(left, text="Test (uses unsaved edits):", style="Header.TLabel").pack(anchor=tk.W)
        self.p_test_prompt = tk.Entry(left, font=("Arial", 10), **dark)
        self.p_test_prompt.pack(fill=tk.X, ipady=3, pady=2)
        self.p_test_prompt.insert(0, "a mantis wearing headphones")
        self.p_test_prompt.bind("<Return>", lambda e: self.p_test())
        trow = ttk.Frame(left)
        trow.pack(fill=tk.X, pady=2)
        ttk.Label(trow, text="+ Core2 LoRA:").pack(side=tk.LEFT)
        self.p_lora_combo = ttk.Combobox(trow, values=["None"] + LORA_LIST + HIDDEN_LORA_LIST,
                                         width=22, state="readonly")
        self.p_lora_combo.set("None")
        self.p_lora_combo.pack(side=tk.LEFT, padx=4)
        ttk.Button(trow, text="Test", command=self.p_test).pack(side=tk.RIGHT)
        # A blank image makes width/height pixels (with text only, Tk counts
        # characters and lines, which made this box huge and hid the editor).
        self.p_blank = tk.PhotoImage(width=320, height=240)
        self.p_img = tk.Label(left, image=self.p_blank, compound=tk.CENTER,
                              text="320x240 preview\n(what the Core2 shows)",
                              width=320, height=240, bg="#000000", fg="#777777")
        self.p_img.pack(pady=(6, 4), anchor=tk.W)
        self.p_status = ttk.Label(left, text="", wraplength=330)
        self.p_status.pack(anchor=tk.W)

        # ---- right column: editor
        right = ttk.Frame(outer)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))

        form = ttk.Frame(right)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.p_name = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_name.grid(row=0, column=1, sticky=tk.EW, pady=2, ipady=2)
        self.p_hidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Hidden (needs the keyword)", variable=self.p_hidden_var
                        ).grid(row=0, column=2, sticky=tk.W, padx=(10, 0))
        ttk.Label(form, text="Prompt template:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.p_template = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_template.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=2, ipady=2)
        ttk.Label(form, text="words before  {prompt}  words after   ({prompt} = what the Core2 sends)",
                  font=("Arial", 8)).grid(row=2, column=1, columnspan=2, sticky=tk.W)
        ttk.Label(form, text="Negative prompt:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.p_negative = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_negative.grid(row=3, column=1, columnspan=2, sticky=tk.EW, pady=2, ipady=2)
        size = ttk.Frame(form)
        size.grid(row=4, column=1, columnspan=2, sticky=tk.W, pady=2)
        ttk.Label(form, text="Size:").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.p_width = tk.Entry(size, width=6, font=("Arial", 10), **dark)
        self.p_width.pack(side=tk.LEFT, ipady=2)
        ttk.Label(size, text=" x ").pack(side=tk.LEFT)
        self.p_height = tk.Entry(size, width=6, font=("Arial", 10), **dark)
        self.p_height.pack(side=tk.LEFT, ipady=2)
        ttk.Label(size, text="   always 1 image. 640x480 SD1.5, 1024x768 SDXL/Flux",
                  font=("Arial", 8)).pack(side=tk.LEFT)
        src = ttk.Frame(form)
        src.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(4, 0))
        self.p_source_label = ttk.Label(src, text="", font=("Arial", 8), wraplength=520)
        self.p_source_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(src, text="Use image prompt as template", style="Small.TButton",
                   command=self.p_use_source).pack(side=tk.RIGHT)

        bar = ttk.Frame(right)
        bar.pack(fill=tk.X, pady=(10, 4))
        ttk.Label(bar, text="Workflow settings", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(bar, text="+ Add LoRA", command=self.p_add_lora).pack(side=tk.LEFT, padx=10)
        ttk.Button(bar, text="Reload lists from ComfyUI", style="Small.TButton",
                   command=self.p_fetch_comfy).pack(side=tk.RIGHT)
        self.p_comfy_label = ttk.Label(right, text="", font=("Arial", 8))
        self.p_comfy_label.pack(anchor=tk.W)

        # Save + report pinned to the bottom so they never get pushed off-screen
        ttk.Button(right, text="Save profile", command=self.p_save).pack(
            side=tk.BOTTOM, anchor=tk.E, pady=(6, 0))
        self.p_report = ttk.Label(right, text="", font=("Consolas", 8), wraplength=620,
                                  foreground="#00ff00")
        self.p_report.pack(side=tk.BOTTOM, anchor=tk.W, fill=tk.X, pady=(6, 0))

        # scrollable node cards
        wrap = ttk.Frame(right)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.p_canvas = tk.Canvas(wrap, bg="#121212", highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.p_canvas.yview)
        self.p_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.p_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.p_inner = ttk.Frame(self.p_canvas)
        self.p_inner_id = self.p_canvas.create_window((0, 0), window=self.p_inner, anchor=tk.NW)
        self.p_inner.bind("<Configure>", lambda e: self.p_canvas.configure(
            scrollregion=self.p_canvas.bbox("all")))
        self.p_canvas.bind("<Configure>", lambda e: self.p_canvas.itemconfigure(
            self.p_inner_id, width=e.width))
        def wheel(e):
            delta = -1 if (getattr(e, "num", 0) == 4 or e.delta > 0) else 1
            self.p_canvas.yview_scroll(delta, "units")
        def bind_wheel(_e):
            self.p_canvas.bind_all("<MouseWheel>", wheel)
            self.p_canvas.bind_all("<Button-4>", wheel)
            self.p_canvas.bind_all("<Button-5>", wheel)
        def unbind_wheel(_e):
            self.p_canvas.unbind_all("<MouseWheel>")
            self.p_canvas.unbind_all("<Button-4>")
            self.p_canvas.unbind_all("<Button-5>")
        self.p_canvas.bind("<Enter>", bind_wheel)
        self.p_canvas.bind("<Leave>", unbind_wheel)


        self.refresh_profile_lists()
        active = profiles.active_name()
        if active:
            self.p_load(profiles.get(active), active)
        self.p_fetch_comfy()
        self.root.after(2000, self.p_poll)

    # ---- ComfyUI model / sampler lists
    def p_fetch_comfy(self):
        self.p_comfy_label.config(text=f"Loading model lists from ComfyUI at {COMFY_URL}...")
        def work():
            try:
                info = mp.fetch_object_info(COMFY_URL)
                def done():
                    self.comfy_info = info
                    n_ckpt = len(mp.input_choices(info, "CheckpointLoaderSimple", "ckpt_name") or [])
                    n_lora = len(mp.input_choices(info, "LoraLoader", "lora_name") or [])
                    self.p_comfy_label.config(
                        text=f"ComfyUI: {n_ckpt} checkpoints, {n_lora} LoRAs available.")
                    if self.p_cur:
                        self.p_collect_fields(quiet=True)
                        self.p_build_form()
                self.root.after(0, done)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: self.p_comfy_label.config(
                    text=f"ComfyUI not reachable ({msg}). Names can still be typed; "
                         f"start ComfyUI and press Reload for dropdown lists."))
        threading.Thread(target=work, daemon=True).start()

    def p_choices(self, cls, key, current):
        vals = mp.input_choices(self.comfy_info, cls, key)
        if vals is None and key in ("lora_name",):
            vals = mp.input_choices(self.comfy_info, "LoraLoader", "lora_name")
        if vals is None:
            return None
        if current not in vals:
            vals = [str(current)] + vals
        return vals

    # ---- list / state
    def refresh_profile_lists(self, select=None):
        entries = profiles.entries()
        names = [n for n, _ in entries]
        default = profiles.active_name()
        self.p_list.delete(0, tk.END)
        for n, hidden in entries:
            mark = "▶ " if n == default else "   "
            self.p_list.insert(tk.END, mark + n + ("   (hidden)" if hidden else ""))
        target = select or self.p_orig_name
        if target in names:
            i = names.index(target)
            self.p_list.selection_clear(0, tk.END)
            self.p_list.selection_set(i)
            self.p_list.see(i)
        self.p_active_label.config(text=f"Default for new sessions: {default or 'none'}\n"
                                        f"{device_status_text()}")
        self.img_profile_combo.config(values=names)
        if self.img_profile_combo.get() not in names:
            self.img_profile_combo.set(default)

    def p_poll(self):
        # The Core2 changes its profile / LoRA at any time
        status = device_status_text()
        if status != self.p_last_status:
            self.p_last_status = status
            self.lora_label.config(text=status)
            self.p_active_label.config(text=f"Default for new sessions: "
                                            f"{profiles.active_name() or 'none'}\n{status}")
        self.root.after(2000, self.p_poll)

    def p_discard_ok(self):
        if self.p_cur is not None and self.p_orig_name is None:
            return messagebox.askyesno("Unsaved profile",
                                       "This profile has not been saved. Discard it?")
        return True

    def p_on_select(self, _event=None):
        sel = self.p_list.curselection()
        if not sel:
            return
        name = profiles.names()[sel[0]]
        if name == self.p_orig_name:
            return
        if not self.p_discard_ok():
            self.refresh_profile_lists()
            return
        self.p_load(profiles.get(name), name)

    # ---- editor
    def p_load(self, profile, orig_name, notes=None):
        self.p_cur = profile
        self.p_orig_name = orig_name
        self.p_notes = notes or []
        for entry, val in ((self.p_name, profile.get("name", "")),
                           (self.p_template, profile.get("template", "{prompt}")),
                           (self.p_negative, profile.get("negative", "")),
                           (self.p_width, str(profile.get("width", 640))),
                           (self.p_height, str(profile.get("height", 480)))):
            entry.delete(0, tk.END)
            entry.insert(0, val)
        self.p_hidden_var.set(bool(profile.get("hidden")))
        src = (profile.get("source_prompt") or "").replace("\n", " ").strip()
        self.p_source_label.config(text=f"Image prompt: {src[:220]}" if src else "")
        self.p_build_form()
        self.p_status.config(text="" if orig_name else "Not saved yet.")

    def p_node_order(self, nid):
        cls = self.p_cur["workflow"][nid]["class_type"]
        if mp.is_lora_node(self.p_cur["workflow"][nid]):
            rank = 1
        elif "Loader" in cls:
            rank = 0
        elif any(k in cls for k in ("Sampler", "Scheduler", "Guider", "Guidance")):
            rank = 2
        else:
            rank = 3
        return (rank, int(nid) if nid.isdigit() else 10**9, nid)

    def p_build_form(self):
        for w in self.p_inner.winfo_children():
            w.destroy()
        self.p_fields = []
        wf = self.p_cur["workflow"]
        managed = mp.managed_inputs(self.p_cur)
        shown = 0
        for nid in sorted(wf, key=self.p_node_order):
            node = wf[nid]
            cls = node["class_type"]
            keys = [k for k, v in node["inputs"].items()
                    if (nid, k) not in managed and not mp.is_link(v)
                    and not isinstance(v, (dict, list)) and v is not None]
            is_lora = mp.is_lora_node(node)
            if not keys and not is_lora:
                continue
            shown += 1
            card = ttk.Frame(self.p_inner, style="Card.TFrame", padding=(8, 6))
            card.pack(fill=tk.X, pady=3, padx=(0, 4))
            card.columnconfigure(1, weight=1)
            title = self.NODE_NAMES.get(cls, cls)
            meta = (node.get("_meta") or {}).get("title")
            if meta and meta not in (cls, title):
                title += f" - {meta}"
            ttk.Label(card, text=f"{title}   (node {nid})", style="Node.TLabel"
                      ).grid(row=0, column=0, columnspan=2, sticky=tk.W)
            if is_lora:
                ttk.Button(card, text="Remove", style="Small.TButton",
                           command=lambda n=nid: self.p_remove_lora(n)
                           ).grid(row=0, column=2, sticky=tk.E)
            for r, k in enumerate(keys, start=1):
                self.p_add_field(card, r, nid, cls, k, node["inputs"][k])
        if not shown:
            ttk.Label(self.p_inner, text="No adjustable settings in this workflow.").pack(anchor=tk.W)
        self.p_refresh_report()

    def p_add_field(self, card, row, nid, cls, key, val):
        ttk.Label(card, text=key.replace("_", " "), style="Card.TLabel"
                  ).grid(row=row, column=0, sticky=tk.W, padx=(0, 10), pady=1)
        if isinstance(val, bool):
            var = tk.BooleanVar(value=val)
            ttk.Checkbutton(card, variable=var).grid(row=row, column=1, sticky=tk.W)
            self.p_fields.append((nid, key, "bool", var))
            return
        var = tk.StringVar(value=str(val))
        choices = self.p_choices(cls, key, val) if isinstance(val, str) else None
        if choices is not None:
            w = ttk.Combobox(card, textvariable=var, values=choices, state="readonly", width=48)
            w.grid(row=row, column=1, sticky=tk.EW, pady=1)
            self.p_fields.append((nid, key, "str", var))
            return
        if isinstance(val, (int, float)):
            rng = mp.input_range(self.comfy_info, cls, key)
            lo, hi, step = rng if rng else (None, None, None)
            is_int = isinstance(val, int)
            step = step or (1 if is_int else 0.05)
            lo = -1e9 if lo is None else lo
            hi = 1e9 if hi is None else hi
            w = ttk.Spinbox(card, textvariable=var, from_=lo, to=hi, increment=step, width=12,
                            format="%.0f" if is_int else "%.2f")
            var.set(str(val))
            w.grid(row=row, column=1, sticky=tk.W, pady=1)
            hint = f"{lo:g} to {hi:g}" if rng and rng[0] is not None and rng[1] is not None else ""
            if hint:
                ttk.Label(card, text=hint, style="Card.TLabel", font=("Arial", 8)
                          ).grid(row=row, column=2, sticky=tk.W, padx=6)
            self.p_fields.append((nid, key, "int" if is_int else "float", var))
            return
        w = tk.Entry(card, textvariable=var, font=("Arial", 10), **self.p_dark)
        w.grid(row=row, column=1, columnspan=2, sticky=tk.EW, pady=1, ipady=2)
        self.p_fields.append((nid, key, "str", var))

    def p_collect_fields(self, quiet=False):
        """Write the form values back into the working workflow."""
        wf = self.p_cur["workflow"]
        for nid, key, kind, var in self.p_fields:
            if nid not in wf:
                continue
            raw = var.get()
            try:
                if kind == "bool":
                    val = bool(raw)
                elif kind == "int":
                    val = int(float(str(raw).strip()))
                elif kind == "float":
                    val = float(str(raw).strip())
                else:
                    val = str(raw)
            except ValueError:
                if quiet:
                    continue
                raise ValueError(f"Node {nid} '{key}' needs a number (got '{raw}').")
            wf[nid]["inputs"][key] = val

    def p_collect(self):
        """Full profile from the editor (raises ValueError on bad input)."""
        self.p_collect_fields()
        p = dict(self.p_cur)
        p["name"] = self.p_name.get().strip()
        p["template"] = self.p_template.get().strip() or "{prompt}"
        p["negative"] = self.p_negative.get().strip()
        p["hidden"] = bool(self.p_hidden_var.get())
        w, h = int(self.p_width.get()), int(self.p_height.get())
        if not (64 <= w <= 2048 and 64 <= h <= 2048):
            raise ValueError("Size must be between 64 and 2048.")
        p["width"], p["height"] = w - w % 8, h - h % 8
        return p

    def p_refresh_report(self):
        text = mp.describe(self.p_cur)
        keep = [l for l in text.splitlines() if l.startswith(("Prompt goes", "Negative goes",
                                                              "LoRA", "Size forced"))]
        if self.p_notes:
            keep.append("Import: " + " ".join(self.p_notes))
        self.p_report.config(text="\n".join(keep))

    def p_use_source(self):
        src = (self.p_cur or {}).get("source_prompt", "").strip()
        if not src:
            return
        self.p_template.delete(0, tk.END)
        self.p_template.insert(0, "{prompt}, " + src.replace("\n", " "))

    # ---- LoRAs inside the profile
    def p_add_lora(self):
        if not self.p_cur:
            return
        try:
            self.p_collect_fields()
        except ValueError as e:
            messagebox.showerror("Bad value", str(e))
            return
        choices = mp.input_choices(self.comfy_info, "LoraLoader", "lora_name") or []
        name = choices[0] if choices else (LORA_LIST[0] if LORA_LIST else "")
        try:
            mp.add_profile_lora(self.p_cur["workflow"], name, 1.0)
        except mp.ProfileError as e:
            messagebox.showerror("Can't add LoRA", str(e))
            return
        self.p_build_form()
        self.p_canvas.update_idletasks()
        self.p_status.config(text="LoRA added. Pick it in the list and set its strength.")

    def p_remove_lora(self, nid):
        try:
            self.p_collect_fields()
        except ValueError as e:
            messagebox.showerror("Bad value", str(e))
            return
        mp.remove_lora_node(self.p_cur["workflow"], nid)
        self.p_build_form()

    # ---- actions
    def p_new_basic(self):
        if not self.p_discard_ok():
            return
        ckpts = mp.input_choices(self.comfy_info, "CheckpointLoaderSimple", "ckpt_name") or []
        ckpt = ckpts[0] if ckpts else REALISTIC_MODEL
        p = mp.basic_profile("New profile", ckpt, "{prompt}", HARDCODED_NEGATIVE)
        self.p_load(p, None, ["New text-to-image profile: pick the checkpoint and sampler "
                              "settings below, add LoRAs if you want, then save."])

    def p_import(self):
        if not self.p_discard_ok():
            return
        path = filedialog.askopenfilename(
            title="ComfyUI image or API workflow",
            filetypes=[("ComfyUI image or API JSON", "*.png *.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            graph = mp.read_comfy_file(path)
            name = os.path.splitext(os.path.basename(path))[0][:40]
            profile, notes = mp.profile_from_graph(graph, name)
        except mp.ProfileError as e:
            messagebox.showerror("Import failed", str(e))
            return
        except Exception as e:
            messagebox.showerror("Import failed", f"{type(e).__name__}: {e}")
            return
        self.p_load(profile, None, notes)
        self.log("SYSTEM", f"Imported workflow from {os.path.basename(path)} (not saved yet)")

    def p_duplicate(self):
        if not self.p_cur or not self.p_discard_ok():
            return
        try:
            p = self.p_collect()
        except ValueError as e:
            messagebox.showerror("Bad value", str(e))
            return
        p = json.loads(json.dumps({k: v for k, v in p.items() if not k.startswith("_")}))
        p["name"] = p["name"] + " copy"
        self.p_load(p, None)

    def p_save(self):
        if not self.p_cur:
            return
        try:
            p = self.p_collect()
        except ValueError as e:
            messagebox.showerror("Bad value", str(e))
            return
        if not p["name"]:
            messagebox.showerror("Name needed", "Give the profile a name.")
            return
        if self.p_orig_name is None and profiles.get(p["name"]):
            if not messagebox.askyesno("Replace?", f"Replace the existing profile '{p['name']}'?"):
                return
        try:
            profiles.save(p, old_name=self.p_orig_name)
        except mp.ProfileError as e:
            messagebox.showerror("Save failed", str(e))
            return
        config["active_profile"] = profiles.active_name()
        save_config(config)
        self.p_orig_name = p["name"]
        self.p_cur = profiles.get(p["name"])
        self.p_notes = []
        self.refresh_profile_lists(select=p["name"])
        self.p_build_form()
        self.p_status.config(text="Saved.")
        self.log("SYSTEM", f"Profile saved: {p['name']}" + (" (hidden)" if p.get("hidden") else ""))

    def p_delete(self):
        if self.p_orig_name is None:
            self.p_cur = None
            active = profiles.active_name()
            if active:
                self.p_load(profiles.get(active), active)
            return
        if len(profiles.names()) <= 1:
            messagebox.showinfo("Keep one", "Keep at least one profile.")
            return
        if not messagebox.askyesno("Delete", f"Delete profile '{self.p_orig_name}'?"):
            return
        profiles.delete(self.p_orig_name)
        config["active_profile"] = profiles.active_name()
        save_config(config)
        first = profiles.active_name()
        self.p_load(profiles.get(first), first)
        self.refresh_profile_lists(select=first)

    def p_use_on_core2(self):
        if self.p_orig_name is None:
            messagebox.showinfo("Save first", "Save the profile first.")
            return
        set_active_profile(self.p_orig_name)
        self.refresh_profile_lists(select=self.p_orig_name)
        self.log("SYSTEM", f"Default profile → {self.p_orig_name}")

    def p_test(self):
        if not self.p_cur:
            return
        prompt = self.p_test_prompt.get().strip()
        if not prompt:
            return
        try:
            profile = self.p_collect()
        except ValueError as e:
            messagebox.showerror("Bad value", str(e))
            return
        sel = self.p_lora_combo.get()
        lora = "" if sel == "None" else sel
        self.p_status.config(text="Generating...")

        def work():
            global _gpu_busy
            if not _gpu_lock.acquire(blocking=False):
                self.root.after(0, lambda: self.p_status.config(
                    text="Busy: a Core2 or chat is using the GPU. Try again in a moment."))
                return
            _gpu_busy = True
            t0 = time.time()
            try:
                img = generate_with_profile(profile, prompt, lora, timeout=600)
                shown = ImageOps.fit(img, (320, 240), Image.LANCZOS)
                secs = time.time() - t0
                def show():
                    self.p_photo = ImageTk.PhotoImage(shown)
                    self.p_img.config(image=self.p_photo, text="", width=320, height=240)
                    self.p_status.config(text=f"OK in {secs:.0f}s, {img.width}x{img.height}"
                                              f"{', LoRA ' + lora if lora else ''}")
                self.root.after(0, show)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: self.p_status.config(text=f"Failed: {msg}"))
            finally:
                _gpu_busy = False
                _gpu_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def setup_settings(self):
        pad = ttk.Frame(self.settings_tab, padding=15)
        pad.pack(fill=tk.BOTH, expand=True)

        ttk.Label(pad, text="System Prompt:", font=("Arial", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))
        self.prompt_text = scrolledtext.ScrolledText(
            pad, wrap=tk.WORD, height=4, font=("Arial", 10),
            bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff", borderwidth=0)
        self.prompt_text.pack(fill=tk.X, pady=(0, 10))
        self.prompt_text.insert(tk.END, SYSTEM_PROMPT)

        grid = ttk.Frame(pad)
        grid.pack(fill=tk.X, pady=(0, 8))

        def row(r, label, width=50):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky=tk.W, pady=3)
            e = tk.Entry(grid, width=width, font=("Arial", 10), bg="#1e1e1e", fg="#ffffff",
                         insertbackground="#ffffff", relief=tk.FLAT)
            e.grid(row=r, column=1, sticky=tk.W, padx=10, pady=3, ipady=3)
            return e

        self.api_entry = row(0, "LM Studio API base:")
        self.api_entry.insert(0, API_BASE_URL)
        self.model_entry = row(1, "AI Model:")
        self.model_entry.insert(0, AI_MODEL)
        self.realistic_entry = row(2, "Realistic checkpoint:")
        self.realistic_entry.insert(0, REALISTIC_MODEL)
        self.anime_entry = row(3, "Anime checkpoint:")
        self.anime_entry.insert(0, ANIME_MODEL)
        self.lora_entry = row(4, "LoRA list (comma-sep):")
        self.lora_entry.insert(0, ", ".join(LORA_LIST))
        self.lora_str_entry = row(5, "LoRA strength:")
        self.lora_str_entry.insert(0, str(LORA_STRENGTH))
        self.prefix_entry = row(6, "Comfy positive prefix:")
        self.prefix_entry.insert(0, HARDCODED_POSITIVE_PREFIX)
        self.negative_entry = row(7, "Comfy negative prompt:")
        self.negative_entry.insert(0, HARDCODED_NEGATIVE)
        self.comfy_entry = row(8, "ComfyUI host:port:")
        self.comfy_entry.insert(0, COMFY_URL)
        self.voice_entry = row(9, "Piper voice path:")
        self.voice_entry.insert(0, TTS_VOICE_PATH)
        self.hidden_lora_entry = row(10, "Hidden LoRA list (comma-sep):")
        self.hidden_lora_entry.insert(0, ", ".join(HIDDEN_LORA_LIST))
        self.keyword_entry = row(11, "Keyword for hidden options:")
        self.keyword_entry.insert(0, HIDDEN_KEYWORD)
        ttk.Label(pad, text="Saying or typing the keyword in a Core2 prompt adds hidden profiles and "
                            "hidden LoRAs to that device's C/B rotation until it reboots or "
                            "reconnects. Other devices stay locked. Leave empty to disable.",
                  wraplength=760, font=("Arial", 8)).pack(anchor=tk.W)

        ttk.Button(pad, text="Save & Apply Settings", command=self.apply).pack(anchor=tk.E, pady=12)

    def apply(self):
        global AI_MODEL, SYSTEM_PROMPT, client, API_BASE_URL
        global REALISTIC_MODEL, ANIME_MODEL, LORA_LIST, LORA_STRENGTH
        global HARDCODED_POSITIVE_PREFIX, HARDCODED_NEGATIVE, COMFY_URL
        global TTS_VOICE_PATH, llm_voice, llm_sample_rate
        global HIDDEN_LORA_LIST, HIDDEN_KEYWORD

        update_system_prompt(self.prompt_text.get("1.0", tk.END).strip())
        API_BASE_URL = self.api_entry.get().strip()
        AI_MODEL = self.model_entry.get().strip()
        REALISTIC_MODEL = self.realistic_entry.get().strip()
        ANIME_MODEL = self.anime_entry.get().strip()
        LORA_LIST = [x.strip() for x in self.lora_entry.get().split(",") if x.strip()]
        try:
            LORA_STRENGTH = float(self.lora_str_entry.get().strip())
        except Exception:
            LORA_STRENGTH = 0.8
        HARDCODED_POSITIVE_PREFIX = self.prefix_entry.get()
        HARDCODED_NEGATIVE = self.negative_entry.get().strip() or DEFAULT_CONFIG["comfy_negative"]
        COMFY_URL = self.comfy_entry.get().strip()
        TTS_VOICE_PATH = self.voice_entry.get().strip()
        HIDDEN_LORA_LIST = [x.strip() for x in self.hidden_lora_entry.get().split(",") if x.strip()]
        HIDDEN_KEYWORD = self.keyword_entry.get().strip()

        client = OpenAI(base_url=API_BASE_URL, api_key=USER_API_KEY)

        # reload piper if path changed
        if PiperVoice:
            try:
                llm_voice = PiperVoice.load(TTS_VOICE_PATH)
                llm_sample_rate = llm_voice.config.sample_rate
                self.log("SYSTEM", f"Piper reloaded @ {llm_sample_rate} Hz")
            except Exception as e:
                self.log("SYSTEM", f"Piper reload failed: {e}")

        config["api_base_url"] = API_BASE_URL
        config["ai_model"] = AI_MODEL
        config["system_prompt"] = SYSTEM_PROMPT
        config["realistic_model"] = REALISTIC_MODEL
        config["anime_model"] = ANIME_MODEL
        config["lora_list"] = ", ".join(LORA_LIST)
        config["lora_strength"] = LORA_STRENGTH
        config["comfy_prefix"] = HARDCODED_POSITIVE_PREFIX
        config["comfy_negative"] = HARDCODED_NEGATIVE
        config["hidden_lora_list"] = ", ".join(HIDDEN_LORA_LIST)
        config["hidden_keyword"] = HIDDEN_KEYWORD
        config["comfy_url"] = COMFY_URL
        save_config(config)

        self.lora_label.config(text=device_status_text())
        self.lora_combo.config(values=["None"] + LORA_LIST + HIDDEN_LORA_LIST)
        self.p_lora_combo.config(values=["None"] + LORA_LIST + HIDDEN_LORA_LIST)
        self.log("SYSTEM", "Settings saved")
        messagebox.showinfo("OK", "Configuration applied")

    def log(self, who, msg):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"[{who}] {msg}\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def check_lm(self):
        try:
            client.models.list()
            self.root.after(0, lambda: self.status_label.config(
                text=f"LM Studio OK ({AI_MODEL})", foreground="#00ff00"))
        except Exception:
            self.root.after(0, lambda: self.status_label.config(
                text="LM Studio Offline", foreground="#ff5555"))

    def send_msg(self, event=None):
        text = self.input_field.get().strip()
        if not text:
            return
        self.input_field.delete(0, tk.END)
        self.log("PC", text)
        def work():
            chat_history.append({"role": "user", "content": text})
            trim_chat_history()
            try:
                res = client.chat.completions.create(
                    model=AI_MODEL, messages=chat_history, timeout=100.0)
                reply = res.choices[0].message.content or ""
                chat_history.append({"role": "assistant", "content": reply})
                self.root.after(0, lambda: self.log("AI", reply))
                speak_locally(reply)
            except Exception as e:
                err = str(e).lower()
                if any(k in err for k in ("context", "token", "length", "too long", "maximum")):
                    trim_chat_history(hard=True)
                    try:
                        res = client.chat.completions.create(
                            model=AI_MODEL, messages=chat_history, timeout=100.0)
                        reply = res.choices[0].message.content or ""
                        chat_history.append({"role": "assistant", "content": reply})
                        self.root.after(0, lambda: self.log("AI", reply))
                        speak_locally(reply)
                        return
                    except Exception as e2:
                        self.root.after(0, lambda: self.log("ERR", str(e2)))
                        return
                self.root.after(0, lambda: self.log("ERR", str(e)))
        threading.Thread(target=work, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False),
        daemon=True).start()
    print(f"[*] Flask http://0.0.0.0:{FLASK_PORT}")
    print(f"[*] LoRAs: {LORA_LIST}  hidden: {len(HIDDEN_LORA_LIST)}  profiles: {profiles.names()}")
    if get_whisper():
        print("[*] Local Whisper ready")
    elif STT_API_BASE:
        print(f"[*] STT remote: {STT_API_BASE}")
    else:
        print("[*] STT: install faster-whisper or set stt_api_base in config.json")
    root = tk.Tk()
    Core2ServerGUI(root)
    root.mainloop()
