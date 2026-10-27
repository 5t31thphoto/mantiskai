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
from io import BytesIO
import urllib.request
from PIL import Image, ImageTk, ImageOps
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox, filedialog
from flask import Flask, request, jsonify, Response
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
COMFY_URL = "127.0.0.1:8000"
REALISTIC_MODEL = config["realistic_model"]
ANIME_MODEL = config["anime_model"]
LORA_LIST = [x.strip() for x in config["lora_list"].split(",") if x.strip()]
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

def set_active_profile(name):
    name = profiles.set_active(name)
    config["active_profile"] = name
    save_config(config)
    return name

def cycle_profile():
    name = profiles.cycle()
    config["active_profile"] = name
    save_config(config)
    return name

active_lora_idx = -1
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

def current_lora_name():
    if 0 <= active_lora_idx < len(LORA_LIST):
        return LORA_LIST[active_lora_idx]
    return "None"

def cycle_lora():
    global active_lora_idx
    if not LORA_LIST:
        active_lora_idx = -1
        return "None"
    active_lora_idx += 1
    if active_lora_idx >= len(LORA_LIST):
        active_lora_idx = -1
    return current_lora_name()

def clear_lora():
    global active_lora_idx
    active_lora_idx = -1
    return "None"

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
    global active_lora_idx
    if request.method == "GET":
        return jsonify({"lora": current_lora_name(), "index": active_lora_idx, "list": LORA_LIST})
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    if action == "cycle":
        name = cycle_lora()
    elif action == "clear":
        name = clear_lora()
    else:
        return jsonify({"error": "unknown action"}), 400
    print(f"[LoRA] → {name}")
    return jsonify({"lora": name, "index": active_lora_idx, "list": LORA_LIST})

def profile_state():
    return {"profile": profiles.active_name(), "index": profiles.active,
            "list": profiles.names(), "lora": current_lora_name()}

@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    if request.method == "GET":
        return jsonify(profile_state())
    data = request.json or {}
    action = (data.get("action") or "").strip().lower()
    if action == "cycle":
        name = cycle_profile()
    elif action == "set":
        name = set_active_profile((data.get("name") or "").strip())
    else:
        return jsonify({"error": "unknown action"}), 400
    print(f"[PROFILE] → {name}")
    return jsonify(profile_state())

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    user_message = (data.get("message") or "").strip()
    lower = user_message.lower()
    if lower in ("/lora", "/lora cycle", "lora cycle"):
        return jsonify({"reply": f"LoRA → {cycle_lora()}", "lora": current_lora_name()})
    if lower in ("/lora clear", "lora clear", "/lora none"):
        return jsonify({"reply": f"LoRA → {clear_lora()}", "lora": current_lora_name()})
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
    prompt = (data.get("prompt") or "").strip()
    profile_name = (data.get("profile") or "").strip()
    if not profiles.get(profile_name):
        profile_name = profiles.active_name()
    selected = (data.get("lora") or "").strip()
    if not selected and active_lora_idx >= 0:
        selected = current_lora_name()
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
        print(f"[IMG] {len(rgb565)} rgb565 bytes, profile={profile_name}, lora={current_lora_name()}")
        return Response(
            rgb565,
            mimetype="application/octet-stream",
            headers={
                "Content-Length": str(len(rgb565)),
                "X-Lora": current_lora_name(),
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
        self.root.geometry("1000x820")
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
        self.lora_label = ttk.Label(top, text=f"LoRA: {current_lora_name()}", style="Header.TLabel")
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

    def gui_cycle(self):
        name = cycle_lora()
        self.lora_label.config(text=f"LoRA: {name}")
        self.log("SYSTEM", f"LoRA → {name}")

    def gui_clear(self):
        name = clear_lora()
        self.lora_label.config(text=f"LoRA: {name}")
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
        self.lora_combo = ttk.Combobox(ctrl, values=["None"] + LORA_LIST, width=22, state="readonly")
        self.lora_combo.grid(row=0, column=4)
        self.lora_combo.set(current_lora_name())
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
    def setup_profiles(self):
        self.p_cur = None          # working copy being edited
        self.p_orig_name = None    # its name on disk; None = not saved yet
        self.p_notes = []
        self.p_photo = None
        self.p_last_active = None

        dark = dict(bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff",
                    relief=tk.FLAT, highlightthickness=1)
        outer = ttk.Frame(self.profiles_tab, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # ---- left: list + actions
        left = ttk.Frame(outer)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Profiles (C button order)", style="Header.TLabel").pack(anchor=tk.W)
        self.p_list = tk.Listbox(left, width=26, height=16, font=("Arial", 10),
                                 bg="#1e1e1e", fg="#ffffff", selectbackground="#007373",
                                 exportselection=False, relief=tk.FLAT)
        self.p_list.pack(fill=tk.Y, pady=(4, 8))
        self.p_list.bind("<<ListboxSelect>>", self.p_on_select)
        for text, cmd in (("New (basic)", self.p_new_basic),
                          ("Import PNG / API JSON", self.p_import),
                          ("Duplicate", self.p_duplicate),
                          ("Delete", self.p_delete),
                          ("Use on Core2", self.p_use_on_core2)):
            ttk.Button(left, text=text, command=cmd).pack(fill=tk.X, pady=2)
        self.p_active_label = ttk.Label(left, text="", wraplength=200)
        self.p_active_label.pack(anchor=tk.W, pady=(8, 0))

        # ---- right: editor
        right = ttk.Frame(outer)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        form = ttk.Frame(right)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.p_name = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_name.grid(row=0, column=1, columnspan=4, sticky=tk.EW, pady=2, ipady=2)
        ttk.Label(form, text="Prompt template:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.p_template = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_template.grid(row=1, column=1, columnspan=4, sticky=tk.EW, pady=2, ipady=2)
        ttk.Label(form, text="{prompt} is replaced by what you type or say on the Core2",
                  font=("Arial", 8)).grid(row=2, column=1, columnspan=4, sticky=tk.W)
        ttk.Label(form, text="Negative:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.p_negative = tk.Entry(form, font=("Arial", 10), **dark)
        self.p_negative.grid(row=3, column=1, columnspan=4, sticky=tk.EW, pady=2, ipady=2)
        ttk.Label(form, text="Size (always 1 image):").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.p_width = tk.Entry(form, width=6, font=("Arial", 10), **dark)
        self.p_width.grid(row=4, column=1, sticky=tk.W, pady=2, ipady=2)
        ttk.Label(form, text="x").grid(row=4, column=2, sticky=tk.W, padx=4)
        self.p_height = tk.Entry(form, width=6, font=("Arial", 10), **dark)
        self.p_height.grid(row=4, column=3, sticky=tk.W, pady=2, ipady=2)
        ttk.Label(form, text="4:3 fits the screen; 640x480 for SD1.5, 1024x768 for SDXL/Flux",
                  font=("Arial", 8)).grid(row=4, column=4, sticky=tk.W, padx=8)

        src = ttk.Frame(right)
        src.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(src, text="Prompt in the imported image:").pack(side=tk.LEFT)
        ttk.Button(src, text="Use as template", command=self.p_use_source).pack(side=tk.RIGHT)
        self.p_source = scrolledtext.ScrolledText(right, height=2, wrap=tk.WORD, font=("Arial", 9),
                                                  bg="#1e1e1e", fg="#bbbbbb", borderwidth=0)
        self.p_source.pack(fill=tk.X, pady=(2, 6))

        ttk.Label(right, text="What the server does with this workflow:").pack(anchor=tk.W)
        self.p_report = scrolledtext.ScrolledText(right, height=7, wrap=tk.WORD, font=("Consolas", 9),
                                                  bg="#1e1e1e", fg="#00ff00", borderwidth=0)
        self.p_report.pack(fill=tk.X, pady=(2, 6))

        bottom = ttk.Frame(right)
        bottom.pack(fill=tk.BOTH, expand=True)

        # node settings (left of bottom)
        nodes = ttk.Frame(bottom)
        nodes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(nodes, text="Node settings (sampler, steps, cfg, models...):").pack(anchor=tk.W)
        cols = ("node", "class", "input", "value")
        tree_wrap = ttk.Frame(nodes)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self.p_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=9)
        for c, w in zip(cols, (60, 150, 110, 200)):
            self.p_tree.heading(c, text=c)
            self.p_tree.column(c, width=w, stretch=(c == "value"))
        sb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.p_tree.yview)
        self.p_tree.configure(yscrollcommand=sb.set)
        self.p_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.p_tree.bind("<<TreeviewSelect>>", self.p_on_tree_select)
        edit = ttk.Frame(nodes)
        edit.pack(fill=tk.X, pady=(4, 0))
        self.p_value = tk.Entry(edit, font=("Arial", 10), **dark)
        self.p_value.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2, padx=(0, 6))
        self.p_value.bind("<Return>", lambda e: self.p_set_value())
        ttk.Button(edit, text="Set value", command=self.p_set_value).pack(side=tk.LEFT)
        ttk.Button(nodes, text="Save profile", command=self.p_save).pack(anchor=tk.E, pady=(8, 0))

        # test panel (right of bottom)
        test = ttk.Frame(bottom)
        test.pack(side=tk.LEFT, fill=tk.Y, padx=(12, 0))
        ttk.Label(test, text="Test (uses the unsaved edits too):").pack(anchor=tk.W)
        self.p_test_prompt = tk.Entry(test, width=40, font=("Arial", 10), **dark)
        self.p_test_prompt.pack(fill=tk.X, ipady=3, pady=2)
        self.p_test_prompt.insert(0, "a mantis wearing headphones")
        self.p_test_prompt.bind("<Return>", lambda e: self.p_test())
        trow = ttk.Frame(test)
        trow.pack(fill=tk.X, pady=2)
        ttk.Label(trow, text="LoRA:").pack(side=tk.LEFT)
        self.p_lora_combo = ttk.Combobox(trow, values=["None"] + LORA_LIST, width=20, state="readonly")
        self.p_lora_combo.set("None")
        self.p_lora_combo.pack(side=tk.LEFT, padx=4)
        ttk.Button(trow, text="Test", command=self.p_test).pack(side=tk.RIGHT)
        self.p_img = tk.Label(test, text="320x240 preview\n(what the Core2 shows)",
                              width=320, height=240, bg="#000000", fg="#777777")
        self.p_img.pack(pady=(6, 4))
        self.p_status = ttk.Label(test, text="", wraplength=320)
        self.p_status.pack(anchor=tk.W)

        self.refresh_profile_lists()
        active = profiles.active_name()
        if active:
            self.p_load(profiles.get(active), active)
        self.root.after(2000, self.p_poll)

    # ---- list / state
    def refresh_profile_lists(self, select=None):
        names = profiles.names()
        active = profiles.active_name()
        self.p_list.delete(0, tk.END)
        for n in names:
            self.p_list.insert(tk.END, ("▶ " if n == active else "   ") + n)
        target = select or self.p_orig_name
        if target in names:
            i = names.index(target)
            self.p_list.selection_clear(0, tk.END)
            self.p_list.selection_set(i)
            self.p_list.see(i)
        self.p_active_label.config(text=f"Core2 is using: {active or 'none'}")
        self.img_profile_combo.config(values=names)
        if self.img_profile_combo.get() not in names:
            self.img_profile_combo.set(active)
        self.p_last_active = active

    def p_poll(self):
        # The Core2 can change the active profile/LoRA at any time
        if profiles.active_name() != self.p_last_active:
            self.refresh_profile_lists()
        self.lora_label.config(text=f"LoRA: {current_lora_name()}")
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
        self.p_source.config(state=tk.NORMAL)
        self.p_source.delete("1.0", tk.END)
        self.p_source.insert(tk.END, profile.get("source_prompt", "") or "(none)")
        self.p_source.config(state=tk.DISABLED)
        self.p_fill_tree()
        self.p_refresh_report()
        self.p_status.config(text="" if orig_name else "Not saved yet.")

    def p_fill_tree(self):
        self.p_tree.delete(*self.p_tree.get_children())
        wf = self.p_cur["workflow"]
        def key(nid):
            return (0, int(nid), "") if nid.isdigit() else (1, 0, nid)
        for nid in sorted(wf, key=key):
            node = wf[nid]
            for k, v in node["inputs"].items():
                if mp.is_link(v) or isinstance(v, (dict, list)):
                    continue
                self.p_tree.insert("", tk.END, iid=f"{nid}\x1f{k}",
                                   values=(nid, node["class_type"], k, str(v)))

    def p_collect(self):
        """Profile from the editor fields (raises ValueError on bad size)."""
        p = dict(self.p_cur)
        p["name"] = self.p_name.get().strip()
        p["template"] = self.p_template.get().strip() or "{prompt}"
        p["negative"] = self.p_negative.get().strip()
        w, h = int(self.p_width.get()), int(self.p_height.get())
        if not (64 <= w <= 2048 and 64 <= h <= 2048):
            raise ValueError("Size must be between 64 and 2048.")
        p["width"], p["height"] = w - w % 8, h - h % 8
        return p

    def p_refresh_report(self):
        text = mp.describe(self.p_cur)
        if self.p_notes:
            text += "\n\nImport changes:\n- " + "\n- ".join(self.p_notes)
        self.p_report.config(state=tk.NORMAL)
        self.p_report.delete("1.0", tk.END)
        self.p_report.insert(tk.END, text)
        self.p_report.config(state=tk.DISABLED)

    def p_on_tree_select(self, _event=None):
        sel = self.p_tree.selection()
        if not sel:
            return
        self.p_value.delete(0, tk.END)
        self.p_value.insert(0, self.p_tree.item(sel[0], "values")[3])

    def p_set_value(self):
        sel = self.p_tree.selection()
        if not sel or not self.p_cur:
            return
        nid, k = sel[0].split("\x1f", 1)
        inputs = self.p_cur["workflow"][nid]["inputs"]
        old, raw = inputs[k], self.p_value.get()
        try:
            if isinstance(old, bool):
                new = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                new = int(raw)
            elif isinstance(old, float):
                new = float(raw)
            else:
                new = raw
        except ValueError:
            messagebox.showerror("Bad value", f"{k} needs a {type(old).__name__}.")
            return
        inputs[k] = new
        self.p_tree.item(sel[0], values=(nid, self.p_cur["workflow"][nid]["class_type"], k, str(new)))
        self.p_refresh_report()

    def p_use_source(self):
        src = (self.p_cur or {}).get("source_prompt", "").strip()
        if not src:
            return
        self.p_template.delete(0, tk.END)
        self.p_template.insert(0, "{prompt}, " + src.replace("\n", " "))

    # ---- actions
    def p_new_basic(self):
        if not self.p_discard_ok():
            return
        p = mp.basic_profile("New profile", REALISTIC_MODEL,
                             HARDCODED_POSITIVE_PREFIX + "{prompt}", HARDCODED_NEGATIVE)
        self.p_load(p, None, ["Basic txt2img. Set the checkpoint in node 4 (ckpt_name) "
                              "and the sampler settings in node 3, then save."])

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
            messagebox.showerror("Bad size", str(e))
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
            messagebox.showerror("Bad size", str(e))
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
        self.p_refresh_report()
        self.p_status.config(text="Saved.")
        self.log("SYSTEM", f"Profile saved: {p['name']}")

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
            messagebox.showinfo("Save first", "Save the profile before using it on the Core2.")
            return
        set_active_profile(self.p_orig_name)
        self.refresh_profile_lists(select=self.p_orig_name)
        self.log("SYSTEM", f"Core2 profile → {self.p_orig_name}")

    def p_test(self):
        if not self.p_cur:
            return
        prompt = self.p_test_prompt.get().strip()
        if not prompt:
            return
        try:
            profile = self.p_collect()
        except ValueError as e:
            messagebox.showerror("Bad size", str(e))
            return
        sel = self.p_lora_combo.get()
        lora = "" if sel == "None" else sel
        self.p_status.config(text="Generating...")

        def work():
            global _gpu_busy
            if not _gpu_lock.acquire(blocking=False):
                self.root.after(0, lambda: self.p_status.config(
                    text="Busy: the Core2 or chat is using the GPU. Try again in a moment."))
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

        ttk.Button(pad, text="Save & Apply Settings", command=self.apply).pack(anchor=tk.E, pady=12)

    def apply(self):
        global AI_MODEL, SYSTEM_PROMPT, client, API_BASE_URL
        global REALISTIC_MODEL, ANIME_MODEL, LORA_LIST, LORA_STRENGTH
        global HARDCODED_POSITIVE_PREFIX, HARDCODED_NEGATIVE, COMFY_URL
        global TTS_VOICE_PATH, llm_voice, llm_sample_rate

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
        save_config(config)

        self.lora_label.config(text=f"LoRA: {current_lora_name()}")
        self.lora_combo.config(values=["None"] + LORA_LIST)
        self.p_lora_combo.config(values=["None"] + LORA_LIST)
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
    print(f"[*] LoRAs: {LORA_LIST}  active={current_lora_name()}")
    if get_whisper():
        print("[*] Local Whisper ready")
    elif STT_API_BASE:
        print(f"[*] STT remote: {STT_API_BASE}")
    else:
        print("[*] STT: install faster-whisper or set stt_api_base in config.json")
    root = tk.Tk()
    Core2ServerGUI(root)
    root.mainloop()
