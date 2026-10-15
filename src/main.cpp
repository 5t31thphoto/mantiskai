"""
MantisKAI PC Bridge
  POST /api/chat   → {"reply": "...", "lora": "..."}          small JSON
  POST /api/tts    → raw WAV bytes                             binary
  POST /api/stt    → {"text": "..."}                           accepts binary WAV/PCM
  POST /api/image  → {"image_base64": "...", "lora": "..."}
  GET|POST /api/lora
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
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox
from flask import Flask, request, jsonify, Response
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
    "system_prompt": "You are a helpful, friendly AI assistant communicating through an M5Stack Core2 device.",
    "stt_api_base": "",          # optional OpenAI-compatible STT endpoint, e.g. http://localhost:8080/v1
    "stt_api_key": "lm-studio",
}

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
SYSTEM_PROMPT = config["system_prompt"]
STT_API_BASE = config.get("stt_api_base", "")
STT_API_KEY = config.get("stt_api_key", "lm-studio")

active_lora_idx = -1
chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]
client = OpenAI(base_url=API_BASE_URL, api_key=USER_API_KEY)

llm_voice = None
llm_sample_rate = 22050
if PiperVoice:
    try:
        llm_voice = PiperVoice.load(TTS_VOICE_PATH)
        llm_sample_rate = llm_voice.config.sample_rate
    except Exception as e:
        print(f"[TTS] load failed: {e}")

app = Flask(__name__)

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

def generate_comfy_image_mg(user_prompt, model_type="realistic", selected_lora=""):
    full_prompt = HARDCODED_POSITIVE_PREFIX + user_prompt
    target_ckpt = REALISTIC_MODEL if model_type == "realistic" else ANIME_MODEL
    workflow = {
        "3": {
            "inputs": {
                "seed": random.randint(1, 1000000000), "steps": 20, "cfg": 7.0,
                "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                "model": ["10" if selected_lora else "4", 0], "positive": ["6", 0],
                "negative": ["7", 0], "latent_image": ["5", 0]
            },
            "class_type": "KSampler"
        },
        "4": {"inputs": {"ckpt_name": target_ckpt}, "class_type": "CheckpointLoaderSimple"},
        "5": {"inputs": {"width": 640, "height": 480, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "6": {"inputs": {"text": full_prompt, "clip": ["10" if selected_lora else "4", 1]}, "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": "ugly, blurry, distorted, low quality, bad anatomy", "clip": ["10" if selected_lora else "4", 1]}, "class_type": "CLIPTextEncode"},
        "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]}, "class_type": "VAEDecode"},
        "9": {"inputs": {"filename_prefix": "MantisKAI_Temp", "images": ["8", 0]}, "class_type": "SaveImage"}
    }
    if selected_lora:
        workflow["10"] = {
            "inputs": {
                "lora_name": selected_lora,
                "strength_model": float(LORA_STRENGTH),
                "strength_clip": float(LORA_STRENGTH),
                "model": ["4", 0], "clip": ["4", 1]
            },
            "class_type": "LoraLoader"
        }
    try:
        data = json.dumps({"prompt": workflow}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{COMFY_URL}/prompt", data=data,
            headers={"Content-Type": "application/json"})
        response = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            return None, None
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(1.0)
            history = json.loads(
                urllib.request.urlopen(f"http://{COMFY_URL}/history/{prompt_id}", timeout=5)
                .read().decode("utf-8"))
            if prompt_id in history:
                images = history[prompt_id].get("outputs", {}).get("9", {}).get("images", [])
                if images:
                    info = images[0]
                    img_url = (f"http://{COMFY_URL}/view?filename={info['filename']}"
                               f"&subfolder={info['subfolder']}&type={info['type']}")
                    img_data = urllib.request.urlopen(img_url, timeout=10).read()
                    original = Image.open(BytesIO(img_data))
                    img320 = original.resize((320, 240))
                    rgb565 = bytearray()
                    for r, g, b in img320.getdata():
                        val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                        rgb565.append(val & 0xFF)
                        rgb565.append((val >> 8) & 0xFF)
                    return original, bytes(rgb565)
        return None, None
    except Exception as e:
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

    chat_history.append({"role": "user", "content": user_message})
    try:
        response = client.chat.completions.create(
            model=AI_MODEL, messages=chat_history, temperature=0.7)
        reply = response.choices[0].message.content or ""
        chat_history.append({"role": "assistant", "content": reply})
        return jsonify({"reply": reply, "lora": current_lora_name()})
    except Exception as e:
        return jsonify({"error": str(e), "lora": current_lora_name()}), 500

@app.route("/api/tts", methods=["POST"])
def api_tts():
    """
    Return raw PCM16 mono (no WAV header) for easy playRaw on Core2.
    Header X-Sample-Rate carries the real rate from Piper.
    """
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return Response(b"", status=400)
    wav = synthesize_wav_bytes(text)
    if not wav or len(wav) < 44:
        print("[TTS] synth failed or empty")
        return Response(b"", status=503)
    # strip standard 44-byte WAV header → pure PCM16
    pcm = wav[44:]
    rate = int(llm_sample_rate)
    print(f"[TTS] {len(pcm)} pcm bytes @ {rate} Hz")
    return Response(
        pcm,
        mimetype="application/octet-stream",
        headers={
            "Content-Length": str(len(pcm)),
            "X-Sample-Rate": str(rate),
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
    model_type = (data.get("model_type") or "realistic").strip()
    selected = (data.get("lora") or "").strip()
    if not selected and active_lora_idx >= 0:
        selected = current_lora_name()
    if selected == "None":
        selected = ""
    if not prompt:
        return jsonify({"error": "Empty prompt", "lora": current_lora_name()}), 400
    pil, rgb565 = generate_comfy_image_mg(prompt, model_type=model_type, selected_lora=selected)
    if not rgb565:
        return jsonify({"error": "Image generation failed", "lora": current_lora_name()}), 500
    print(f"[IMG] {len(rgb565)} rgb565 bytes, lora={current_lora_name()}")
    return Response(
        rgb565,
        mimetype="application/octet-stream",
        headers={
            "Content-Length": str(len(rgb565)),
            "X-Lora": current_lora_name(),
            "X-Width": "320",
            "X-Height": "240",
        },
    )

# ------------------------------------------------------------------
# GUI (unchanged structure)
# ------------------------------------------------------------------
class Core2ServerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MantisKAI – PC Bridge")
        self.root.geometry("820x760")
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
        nb.add(self.chat_tab, text="Chat & Status")
        nb.add(self.image_tab, text="Picture Mode")
        nb.add(self.settings_tab, text="Settings")
        self.setup_chat()
        self.setup_image()
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
        ttk.Label(ctrl, text="Model:").grid(row=0, column=0, sticky=tk.W)
        self.model_var = tk.StringVar(value="realistic")
        ttk.Radiobutton(ctrl, text="Realistic", variable=self.model_var, value="realistic").grid(row=0, column=1, padx=5)
        ttk.Radiobutton(ctrl, text="Anime", variable=self.model_var, value="anime").grid(row=0, column=2, padx=5)
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
        model = self.model_var.get()
        sel = self.lora_combo.get()
        lora = "" if sel == "None" else sel
        self.img_label.config(text="Generating...")
        def work():
            pil, _ = generate_comfy_image_mg(prompt, model_type=model, selected_lora=lora)
            if pil:
                disp = pil.resize((400, 300))
                self.photo = ImageTk.PhotoImage(disp)
                self.root.after(0, lambda: self.img_label.config(image=self.photo, text=""))
                self.root.after(0, lambda: self.log("Comfy", prompt[:50]))
            else:
                self.root.after(0, lambda: self.img_label.config(text="Failed"))
        threading.Thread(target=work, daemon=True).start()

    def setup_settings(self):
        pad = ttk.Frame(self.settings_tab, padding=15)
        pad.pack(fill=tk.BOTH, expand=True)
        ttk.Label(pad, text="System Prompt:", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        self.prompt_text = scrolledtext.ScrolledText(
            pad, wrap=tk.WORD, height=4, font=("Arial", 10),
            bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff", borderwidth=0)
        self.prompt_text.pack(fill=tk.X, pady=(0, 10))
        self.prompt_text.insert(tk.END, SYSTEM_PROMPT)
        grid = ttk.Frame(pad)
        grid.pack(fill=tk.X)
        ttk.Label(grid, text="AI Model:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.model_entry = tk.Entry(grid, width=45, font=("Arial", 10), bg="#1e1e1e", fg="#ffffff",
                                    insertbackground="#ffffff", relief=tk.FLAT)
        self.model_entry.grid(row=0, column=1, padx=10, ipady=3)
        self.model_entry.insert(0, AI_MODEL)
        ttk.Button(pad, text="Save & Apply", command=self.apply).pack(anchor=tk.E, pady=12)

    def apply(self):
        global AI_MODEL, SYSTEM_PROMPT, client
        update_system_prompt(self.prompt_text.get("1.0", tk.END).strip())
        AI_MODEL = self.model_entry.get().strip()
        client = OpenAI(base_url=API_BASE_URL, api_key=USER_API_KEY)
        config["ai_model"] = AI_MODEL
        config["system_prompt"] = SYSTEM_PROMPT
        save_config(config)
        self.log("SYSTEM", "Settings saved")
        messagebox.showinfo("OK", "Applied")

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
            try:
                res = client.chat.completions.create(model=AI_MODEL, messages=chat_history)
                reply = res.choices[0].message.content or ""
                chat_history.append({"role": "assistant", "content": reply})
                self.root.after(0, lambda: self.log("AI", reply))
                speak_locally(reply)
            except Exception as e:
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
