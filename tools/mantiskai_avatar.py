"""
MantisKAI avatar tab for the server GUI (replaces the standalone Mouth-poser.py).

  * Frame & Points: load artwork for each of the 5 poses (Base + 4 moods), frame it to the 320x240
    screen, place the shared mouth box and each pose's own mouth control points (dashed guides).
  * Tune & Preview: the exact device rendering (same warp, same mouth envelope, same subtitle line
    timing), driven by the real Piper TTS and the server's own chunking + mood choice.
  * Export: writes m5_asymmetric_poses.bin (format POS2) for the SD card root.

Everything marked DEVICE-MATCH must stay identical to src/main.cpp.
"""
import base64
import json
import math
import struct
import threading
import time
import tkinter as tk
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

SCREEN_W, SCREEN_H = 320, 240
POSES = ["Base", "Accomplished", "Excited", "Sad", "Confused"]  # device pose order (server mood names, lowercase)
MAX_POINTS = 32
EDIT_SCALE = 2          # editor canvas = 640x480
ENV_STEP_MS = 20        # DEVICE-MATCH: mouth envelope resolution
HEADER_H = 28           # DEVICE-MATCH: avatar screen layout
SUB_H = 44

DEFAULT_PARAMS = {      # DEVICE-MATCH: defaults when a POSE (v1) file is loaded
    "attack": 0.45,     # envelope rise per 20 ms step
    "release": 0.12,    # envelope fall per 20 ms step
    "flutter": 0.35,    # lively wobble while speaking
    "gain": 2.8,        # loudness -> mouth opening
    "pull_lower": 8.8,  # px the lower lip moves at full opening (down)
    "pull_upper": -2.8, # px the upper lip moves at full opening (negative = up)
    "falloff": 900,     # d^2 beyond which control-point influence fades
    "lookahead_ms": 40, # read the audio slightly ahead to cover speaker latency
    "line_chars": 34,   # subtitle wrap width (characters)
    "width_pull": 6.0,  # px the mouth corners move at full wide/round (viseme mode)
}
PARAM_SPECS = [  # key, label, min, max, step
    ("attack", "Mouth opens (attack)", 0.05, 1.0, 0.01),
    ("release", "Mouth closes (release)", 0.02, 1.0, 0.01),
    ("flutter", "Flutter", 0.0, 1.0, 0.01),
    ("gain", "Loudness gain", 0.5, 6.0, 0.1),
    ("pull_lower", "Lower lip travel (px)", 0.0, 30.0, 0.1),
    ("pull_upper", "Upper lip travel (px)", -15.0, 5.0, 0.1),
    ("falloff", "Point reach (falloff)", 100, 4000, 10),
    ("lookahead_ms", "Audio lookahead (ms)", 0, 200, 5),
    ("line_chars", "Subtitle line width (chars)", 20, 40, 1),
    ("width_pull", "Mouth corner travel (px)", 0.0, 20.0, 0.1),
]

# ---- mouth shapes from Piper phoneme alignments (DEVICE-MATCH order and defaults) ----
VISEMES = ["rest", "M B P", "F V", "L TH", "W OO", "O", "EE", "AH", "consonant", "uh"]
DEFAULT_VISEMES = [  # [open 0..1 (negative = lips pressed), width -1 round .. +1 wide]
    [0.0, 0.0], [-0.08, -0.05], [0.12, 0.15], [0.30, 0.10], [0.25, -0.70],
    [0.55, -0.45], [0.35, 0.55], [0.90, 0.15], [0.28, 0.20], [0.40, 0.0]]
_PH = {}
for _v, _chars in ((1, "pbm"), (2, "fv"), (3, "θðlɫ"), (4, "wuʊʍ"), (5, "oɔɒ"), (6, "iɪeɛjɨɘ"),
                   (7, "aæɑʌɐɶ"), (9, "əɜɚɝᵻ")):
    for _c in _chars:
        _PH[_c] = _v
_REST = set(" ,.;:!?^$_-\n")
_INHERIT = set("ˈˌːˑ̃ʲʰ")


def phoneme_viseme(ph):
    """Mouth shape for one Piper phoneme; None = belongs to the previous sound (stress/length)."""
    if ph in _INHERIT or ph == " ":
        return None
    if ph in _REST or not ph.strip():
        return 0
    return _PH.get(ph, 8)


def encode_visemes(alignments, rate):
    """[(phoneme, num_samples)] -> base64 of (shape, duration in 10 ms) byte pairs (X-Visemes)."""
    segs = []
    for ph, n in alignments:
        v = phoneme_viseme(ph)
        if v is None or (segs and segs[-1][0] == v):
            if segs:
                segs[-1][1] += n
            else:
                segs.append([0, n])
            continue
        segs.append([v, n])
    out, t, emitted = bytearray(), 0.0, 0
    for v, n in segs:
        t += n * 100.0 / rate          # cumulative, so rounding never drifts from the audio
        units = int(round(t)) - emitted
        while units > 255:
            out += bytes([v, 255]); units -= 255; emitted += 255
        if units > 0:
            out += bytes([v, units]); emitted += units
    return base64.b64encode(bytes(out)).decode("ascii")


def decode_visemes(b64):
    """-> list of (start_ms, end_ms, shape)."""
    raw = base64.b64decode(b64) if b64 else b""
    segs, t = [], 0
    for i in range(0, len(raw) - 1, 2):
        segs.append((t, t + raw[i + 1] * 10, raw[i]))
        t += raw[i + 1] * 10
    return segs


def viseme_target(segs, t_ms, table, lookahead):
    tt = t_ms + lookahead
    for a, b, v in segs:
        if a <= tt < b:
            return table[v]
    return table[0]


def viseme_smooth(cur, target, dt_ms, rate):
    """Frame-rate independent ease toward the shape (rate = fraction per 20 ms)."""
    k = 1.0 - (1.0 - rate) ** max(0.0, dt_ms / ENV_STEP_MS)
    return cur + (target - cur) * k


# ====================================================================== DEVICE-MATCH math
def displacement_field(points_local, split_y, w, h, p):
    """Per-pixel vertical displacement at full mouth opening (intensity 1.0).
    Device: same sum, computed once per pose at load; each frame uses sy = y - trunc(I * D)."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    tw = np.zeros((h, w), np.float32)
    acc = np.zeros((h, w), np.float32)
    for (px, py) in points_local:
        d2 = (xs - px) ** 2 + (ys - py) ** 2 + np.float32(0.65)
        wgt = 1.0 / d2
        far = d2 > p["falloff"]
        wgt[far] *= np.float32(p["falloff"]) / d2[far]
        k = p["pull_lower"] if py > split_y else p["pull_upper"]
        tw += wgt
        acc += wgt * np.float32(k)
    return acc / (tw + np.float32(1e-5))


def displacement_field_x(points_local, w, h, p):
    """Horizontal displacement at full width (+1): mouth corners move outward, IDW like the vertical."""
    xs_pts = [px for px, _ in points_local]
    cx = (min(xs_pts) + max(xs_pts)) / 2.0
    half = max(1.0, (max(xs_pts) - min(xs_pts)) / 2.0)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    tw = np.zeros((h, w), np.float32)
    acc = np.zeros((h, w), np.float32)
    for (px, py) in points_local:
        d2 = (xs - px) ** 2 + (ys - py) ** 2 + np.float32(0.65)
        wgt = 1.0 / d2
        far = d2 > p["falloff"]
        wgt[far] *= np.float32(p["falloff"]) / d2[far]
        k = np.float32((px - cx) / half * p["width_pull"])
        tw += wgt
        acc += wgt * k
    return acc / (tw + np.float32(1e-5))


CAVITY_RGB = (32, 4, 8)    # DEVICE-MATCH: 0x2021
TEETH_RGB = (230, 226, 214)  # DEVICE-MATCH: 0xE71A


def warp_shape(crop_rgb, fy, fx, open_, width, split, minx, maxx):
    """Viseme mode: open/close + wide/round, and the gap between the lips shows the mouth interior."""
    h, w = fy.shape
    ys = np.arange(h, dtype=np.int32)[:, None]
    xs = np.arange(w, dtype=np.int32)[None, :]
    sy = np.clip(ys - np.trunc(open_ * fy).astype(np.int32), 0, h - 1)
    sx = np.clip(xs - np.trunc(width * fx).astype(np.int32), 0, w - 1)
    out = crop_rgb[sy, sx].copy()
    inside = (xs >= minx) & (xs <= maxx)
    low = (ys > split) & (sy <= split) & inside
    up = (ys <= split) & (sy > split) & inside
    out[low] = CAVITY_RGB
    out[up] = TEETH_RGB if open_ > 0.3 else CAVITY_RGB
    return out


def warp_crop(crop_rgb, field, intensity):
    """crop_rgb: HxWx3 uint8. Returns the warped mouth (device: renderWorkspace)."""
    h, w = field.shape
    sy = np.arange(h, dtype=np.int32)[:, None] - np.trunc(intensity * field).astype(np.int32)
    sy = np.clip(sy, 0, h - 1)
    return crop_rgb[sy, np.arange(w)[None, :]]


def loudness_steps(pcm, rate, p):
    """Raw loudness per 20 ms step: (rms*0.75 + peak*0.25) * gain over a 40 ms window."""
    pcm = np.asarray(pcm, dtype=np.float32) / 32768.0
    step = max(1, int(rate * ENV_STEP_MS / 1000))
    win = step * 2
    n = max(1, (len(pcm) + step - 1) // step)
    out = np.zeros(n, np.float32)
    for i in range(n):
        seg = pcm[i * step:i * step + win]
        if len(seg) < 8:
            continue
        rms = math.sqrt(float(np.mean(seg * seg)))
        pk = float(np.max(np.abs(seg)))
        out[i] = min(1.0, (rms * 0.75 + pk * 0.25) * p["gain"])
    return out


def smooth_envelope(raw, p, start=0.0):
    """Attack/release smoothing, one update per 20 ms step (frame-rate independent)."""
    env = np.zeros(len(raw), np.float32)
    s = start
    for i, t in enumerate(raw):
        s += (t - s) * (p["attack"] if t > s else p["release"])
        env[i] = s
    return env


def intensity_at(env, t_ms, p):
    """Mouth opening at time t_ms into a chunk (lookahead + flutter), clamped 0..1."""
    i = int((t_ms + p["lookahead_ms"]) // ENV_STEP_MS)
    base = float(env[min(max(i, 0), len(env) - 1)]) if len(env) else 0.0
    flutter = 0.0
    if base > 0.15:
        flutter = (math.sin(t_ms * 0.055) * 0.5 + 0.5) * p["flutter"] * base
    return max(0.0, min(1.0, base + flutter))


def wrap_lines(text, n):
    """Subtitle lines: whole words up to n characters; longer words are split."""
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        while len(w) > n:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:n])
            w = w[n:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= n:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def line_index_at(lines, t_ms, dur_ms):
    """Which subtitle line is being spoken: lines share the chunk's time by character count."""
    total = sum(len(l) + 1 for l in lines) or 1
    acc = 0
    for i, l in enumerate(lines):
        acc += len(l) + 1
        if t_ms < dur_ms * acc / total:
            return i
    return len(lines) - 1


def to_rgb565_be(img):
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return v.astype(">u2").tobytes()


def quantize565(img):
    """What the RGB565 panel actually shows (for an honest preview)."""
    a = np.asarray(img.convert("RGB"), dtype=np.uint8)
    q = np.empty_like(a)
    q[..., 0] = (a[..., 0] & 0xF8) | (a[..., 0] >> 5)
    q[..., 1] = (a[..., 1] & 0xFC) | (a[..., 1] >> 6)
    q[..., 2] = (a[..., 2] & 0xF8) | (a[..., 2] >> 5)
    return Image.fromarray(q, "RGB")


# ====================================================================== project model
def default_points(box, n):
    """Points on an ellipse inside the mouth box: first half upper lip, second half lower lip."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    rx, ry = (x2 - x1) * 0.38, (y2 - y1) * 0.30
    pts, top = [], n // 2
    for i in range(top):
        a = math.pi + math.pi * (i + 0.5) / top
        pts.append([cx + rx * math.cos(a), cy + ry * math.sin(a)])
    bot = n - top
    for i in range(bot):
        a = math.pi * (i + 0.5) / bot
        pts.append([cx + rx * math.cos(a), cy + ry * math.sin(a)])
    return pts


class AvatarProject:
    def __init__(self):
        self.box = [110, 150, 210, 200]          # shared mouth box (device px)
        self.num_points = 8
        self.params = dict(DEFAULT_PARAMS)
        self.visemes = [list(v) for v in DEFAULT_VISEMES]
        self.poses = [self._empty_pose() for _ in POSES]

    def _empty_pose(self):
        return {"path": "", "zoom": 1.0, "ox": None, "oy": None,
                "points": default_points(self.box, self.num_points), "split": None}

    # ---- persistence
    def to_json(self):
        return {"box": self.box, "num_points": self.num_points, "params": self.params,
                "visemes": self.visemes, "poses": self.poses}

    @classmethod
    def from_json(cls, d):
        p = cls()
        p.box = list(d.get("box", p.box))
        p.num_points = int(d.get("num_points", p.num_points))
        p.params.update(d.get("params", {}))
        vis = d.get("visemes")
        if isinstance(vis, list) and len(vis) == len(DEFAULT_VISEMES):
            p.visemes = [list(v) for v in vis]
        poses = d.get("poses", [])
        for i in range(len(POSES)):
            if i < len(poses):
                p.poses[i].update(poses[i])
            if len(p.poses[i]["points"]) != p.num_points:
                p.poses[i]["points"] = default_points(p.box, p.num_points)
        return p

    # ---- geometry
    def split_y(self, i):
        """Lip line (device px). Points below it are lower lip. Auto = middle of the points."""
        pose = self.poses[i]
        if pose.get("split") is not None:
            return pose["split"]
        ys = [q[1] for q in pose["points"]]
        return (min(ys) + max(ys)) / 2 if ys else (self.box[1] + self.box[3]) / 2


class ImageCache:
    def __init__(self):
        self._c = {}

    def get(self, path):
        if not path:
            return None
        if path not in self._c:
            try:
                self._c[path] = Image.open(path).convert("RGB")
            except Exception:
                self._c[path] = None
        return self._c[path]


def framed_screen(img, pose):
    """The 320x240 screen image for a pose (artwork scaled to cover, zoomed and panned)."""
    out = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    if img is None:
        return out
    cover = max(SCREEN_W / img.width, SCREEN_H / img.height)
    s = cover * float(pose.get("zoom", 1.0))
    w, h = max(1, round(img.width * s)), max(1, round(img.height * s))
    ox = pose["ox"] if pose.get("ox") is not None else (SCREEN_W - w) / 2
    oy = pose["oy"] if pose.get("oy") is not None else (SCREEN_H - h) / 2
    out.paste(img.resize((w, h), Image.LANCZOS), (round(ox), round(oy)))
    return out


# ====================================================================== the tab
class AvatarTab:
    """deps: dict with synth_wav(text)->bytes|None, split_chunks(text)->list, build_moods(user, reply)->list,
    pygame (module or None), project_path (Path)."""

    def __init__(self, parent, deps):
        self.deps = deps
        self.parent = parent
        self.project_path = Path(deps["project_path"])
        self.cache = ImageCache()
        self.project = self._load_project()
        self.cur = 0
        self.drag = None
        self.preview_job = None
        self.speech = None          # running TTS preview state
        self._fields = {}           # pose -> (key, field) cache
        self._screen_cache = {}

        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)
        self.edit_tab = ttk.Frame(nb, padding=6)
        self.prev_tab = ttk.Frame(nb, padding=6)
        nb.add(self.edit_tab, text="1. Frame & Points")
        nb.add(self.prev_tab, text="2. Tune & Preview")
        self._build_edit()
        self._build_preview()
        self._redraw_edit()

    # ------------------------------------------------------------------ persistence
    def _load_project(self):
        try:
            return AvatarProject.from_json(json.loads(self.project_path.read_text()))
        except Exception:
            return AvatarProject()

    def _save_project(self):
        try:
            self.project_path.parent.mkdir(parents=True, exist_ok=True)
            self.project_path.write_text(json.dumps(self.project.to_json(), indent=1))
        except Exception as e:
            print(f"[AVATAR] save failed: {e}")

    def _changed(self):
        self._fields.clear()
        self._screen_cache.clear()
        self._save_project()

    # ------------------------------------------------------------------ edit tab
    def _build_edit(self):
        left = ttk.Frame(self.edit_tab)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.canvas = tk.Canvas(left, width=SCREEN_W * EDIT_SCALE, height=SCREEN_H * EDIT_SCALE,
                                bg="#000000", highlightthickness=1, highlightbackground="#007373")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        for b in ("<ButtonPress-3>", "<ButtonPress-2>"):
            self.canvas.bind(b, self._on_pan_start)
        for b in ("<B3-Motion>", "<B2-Motion>"):
            self.canvas.bind(b, self._on_pan)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(e, 1.05))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(e, 1 / 1.05))
        ttk.Label(left, text=("Left-drag: points, lip line (dashed), mouth box (drag inside to move, corner to "
                              "resize).  Right-drag: move artwork.  Wheel: zoom artwork."),
                  wraplength=640).pack(anchor="w", pady=(4, 0))

        side = ttk.Frame(self.edit_tab, padding=(10, 0))
        side.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(side, text="Pose", style="Header.TLabel").pack(anchor="w")
        self.pose_var = tk.IntVar(value=0)
        for i, name in enumerate(POSES):
            ttk.Radiobutton(side, text=f"{i}. {name}" + ("  (resting face)" if i == 0 else ""),
                            variable=self.pose_var, value=i, command=self._on_pose).pack(anchor="w")
        ttk.Button(side, text="Load artwork for this pose...", command=self._load_art).pack(fill=tk.X, pady=(8, 2))
        ttk.Button(side, text="Fit artwork (cover the screen)", command=self._fit_art).pack(fill=tk.X, pady=2)
        ttk.Button(side, text="Copy points from Base", command=self._copy_base_points).pack(fill=tk.X, pady=2)
        ttk.Button(side, text="Reset points to an oval", command=self._reset_points).pack(fill=tk.X, pady=2)
        ttk.Button(side, text="Auto lip line", command=self._auto_split).pack(fill=tk.X, pady=2)
        row = ttk.Frame(side)
        row.pack(fill=tk.X, pady=(8, 2))
        ttk.Label(row, text="Control points (all poses):").pack(side=tk.LEFT)
        self.npts_var = tk.IntVar(value=self.project.num_points)
        sp = ttk.Spinbox(row, from_=4, to=MAX_POINTS, width=4, textvariable=self.npts_var, command=self._on_npts)
        sp.pack(side=tk.LEFT, padx=4)
        self.edit_info = ttk.Label(side, text="", wraplength=380, justify=tk.LEFT)
        self.edit_info.pack(anchor="w", pady=(10, 0))
        ttk.Label(side, text=("Tips: points are numbered in order; the first half usually traces the upper lip and "
                              "the second half the lower lip. Points below the dashed lip line move down when the "
                              "mouth opens, points above it move up. Each mood has its own points; the mouth box is "
                              "shared, so frame each mood so its mouth sits inside the same box."),
                  wraplength=380, justify=tk.LEFT).pack(anchor="w", pady=(10, 0))

    def _on_pose(self):
        self.cur = self.pose_var.get()
        self._redraw_edit()

    def _load_art(self):
        path = filedialog.askopenfilename(title=f"Artwork for {POSES[self.cur]}",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")])
        if not path:
            return
        pose = self.project.poses[self.cur]
        pose.update({"path": path, "zoom": 1.0, "ox": None, "oy": None})
        self._changed()
        self._redraw_edit()

    def _fit_art(self):
        self.project.poses[self.cur].update({"zoom": 1.0, "ox": None, "oy": None})
        self._changed()
        self._redraw_edit()

    def _copy_base_points(self):
        self.project.poses[self.cur]["points"] = [list(q) for q in self.project.poses[0]["points"]]
        self.project.poses[self.cur]["split"] = self.project.poses[0].get("split")
        self._changed()
        self._redraw_edit()

    def _reset_points(self):
        self.project.poses[self.cur]["points"] = default_points(self.project.box, self.project.num_points)
        self.project.poses[self.cur]["split"] = None
        self._changed()
        self._redraw_edit()

    def _auto_split(self):
        self.project.poses[self.cur]["split"] = None
        self._changed()
        self._redraw_edit()

    def _on_npts(self):
        n = max(4, min(MAX_POINTS, int(self.npts_var.get())))
        if n == self.project.num_points:
            return
        if not messagebox.askyesno("Control points", f"Use {n} points? Every pose's points are reset to an oval."):
            self.npts_var.set(self.project.num_points)
            return
        self.project.num_points = n
        for pose in self.project.poses:
            pose["points"] = default_points(self.project.box, n)
            pose["split"] = None
        self._changed()
        self._redraw_edit()

    def screen(self, i):
        pose = self.project.poses[i]
        key = (pose["path"], pose["zoom"], pose["ox"], pose["oy"])
        c = self._screen_cache.get(i)
        if c and c[0] == key:
            return c[1]
        img = framed_screen(self.cache.get(pose["path"]), pose)
        self._screen_cache[i] = (key, img)
        return img

    def _redraw_edit(self):
        cv, S, pr = self.canvas, EDIT_SCALE, self.project
        cv.delete("all")
        scr = self.screen(self.cur).resize((SCREEN_W * S, SCREEN_H * S), Image.NEAREST)
        self._edit_photo = ImageTk.PhotoImage(scr)
        cv.create_image(0, 0, anchor="nw", image=self._edit_photo)
        # device chrome guides: header and subtitle bar cover these strips while speaking
        cv.create_rectangle(0, 0, SCREEN_W * S, HEADER_H * S, outline="#5d005d", dash=(3, 5))
        cv.create_rectangle(0, (SCREEN_H - SUB_H) * S, SCREEN_W * S, SCREEN_H * S, outline="#5d005d", dash=(3, 5))
        x1, y1, x2, y2 = pr.box
        cv.create_rectangle(x1 * S, y1 * S, x2 * S, y2 * S, outline="#00ff00", dash=(6, 4), width=2)
        cv.create_rectangle(x2 * S - 6, y2 * S - 6, x2 * S + 6, y2 * S + 6, fill="#00ff00", outline="")
        sy = pr.split_y(self.cur)
        cv.create_line(x1 * S, sy * S, x2 * S, sy * S, fill="#ffcc00", dash=(4, 4), width=2)
        pts = pr.poses[self.cur]["points"]
        if len(pts) > 1:
            flat = [c * S for q in pts + [pts[0]] for c in q]
            cv.create_line(*flat, fill="#00e5ff", dash=(5, 3), width=2)
        for n, (px, py) in enumerate(pts):
            lower = py > sy
            col = "#ff4fd8" if lower else "#00e5ff"
            cv.create_oval(px * S - 6, py * S - 6, px * S + 6, py * S + 6, fill=col, outline="#000000")
            cv.create_text(px * S + 10, py * S - 10, text=str(n + 1), fill="#ffffff", font=("Arial", 9, "bold"))
        pose = pr.poses[self.cur]
        name = Path(pose["path"]).name if pose["path"] else "no artwork yet"
        missing = [POSES[i] for i, p in enumerate(pr.poses) if not p["path"]]
        self.edit_info.config(text=(
            f"{POSES[self.cur]}: {name}\nMouth box {x2 - x1}x{y2 - y1} at ({x1},{y1})  |  lip line y={sy:.0f}\n"
            f"Cyan = upper lip points, pink = lower lip points.\n"
            + (f"Still missing artwork: {', '.join(missing)}" if missing else "All 5 poses have artwork.")))

    # ---- mouse handling (device coordinates = canvas / EDIT_SCALE)
    def _dev(self, e):
        return e.x / EDIT_SCALE, e.y / EDIT_SCALE

    def _on_press(self, e):
        x, y = self._dev(e)
        pr = self.project
        for n, (px, py) in enumerate(pr.poses[self.cur]["points"]):
            if (px - x) ** 2 + (py - y) ** 2 <= 5 ** 2:
                self.drag = ("point", n)
                return
        x1, y1, x2, y2 = pr.box
        if abs(x - x2) <= 5 and abs(y - y2) <= 5:
            self.drag = ("resize", None)
            return
        if x1 <= x <= x2 and abs(y - pr.split_y(self.cur)) <= 3:
            self.drag = ("split", None)
            return
        if x1 <= x <= x2 and y1 <= y <= y2:
            self.drag = ("move", (x, y))
            return
        self.drag = None

    def _on_drag(self, e):
        if not self.drag:
            return
        x, y = self._dev(e)
        pr = self.project
        kind, data = self.drag
        x1, y1, x2, y2 = pr.box
        if kind == "point":
            pr.poses[self.cur]["points"][data] = [min(max(x, x1), x2 - 1), min(max(y, y1), y2 - 1)]
        elif kind == "resize":
            pr.box = [x1, y1, int(min(SCREEN_W, max(x1 + 16, x))), int(min(SCREEN_H, max(y1 + 12, y)))]
        elif kind == "split":
            pr.poses[self.cur]["split"] = min(max(y, y1), y2)
        elif kind == "move":
            dx, dy = round(x - data[0]), round(y - data[1])
            dx = max(-x1, min(SCREEN_W - x2, dx))
            dy = max(-y1, min(SCREEN_H - y2, dy))
            if dx or dy:
                pr.box = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
                for pose in pr.poses:  # the points ride along with the box, in every pose
                    pose["points"] = [[q[0] + dx, q[1] + dy] for q in pose["points"]]
                    if pose.get("split") is not None:
                        pose["split"] += dy
                self.drag = ("move", (data[0] + dx, data[1] + dy))
        self._redraw_edit()

    def _on_release(self, _e):
        if self.drag:
            self._changed()
        self.drag = None

    def _on_pan_start(self, e):
        self._pan_from = self._dev(e)

    def _on_pan(self, e):
        x, y = self._dev(e)
        pose = self.project.poses[self.cur]
        img = self.cache.get(pose["path"])
        if img is None:
            return
        self._ensure_offsets(pose, img)
        pose["ox"] += x - self._pan_from[0]
        pose["oy"] += y - self._pan_from[1]
        self._pan_from = (x, y)
        self._screen_cache.pop(self.cur, None)
        self._redraw_edit()
        self._schedule_save()

    def _ensure_offsets(self, pose, img):
        if pose.get("ox") is None or pose.get("oy") is None:
            cover = max(SCREEN_W / img.width, SCREEN_H / img.height) * pose["zoom"]
            pose["ox"] = (SCREEN_W - img.width * cover) / 2
            pose["oy"] = (SCREEN_H - img.height * cover) / 2

    def _on_wheel(self, e):
        self._zoom_at(e, 1.05 if e.delta > 0 else 1 / 1.05)

    def _zoom_at(self, e, f):
        pose = self.project.poses[self.cur]
        img = self.cache.get(pose["path"])
        if img is None:
            return
        self._ensure_offsets(pose, img)
        x, y = self._dev(e)
        nz = min(8.0, max(0.25, pose["zoom"] * f))
        f = nz / pose["zoom"]
        pose["ox"] = x - (x - pose["ox"]) * f   # keep the point under the cursor still
        pose["oy"] = y - (y - pose["oy"]) * f
        pose["zoom"] = nz
        self._screen_cache.pop(self.cur, None)
        self._redraw_edit()
        self._schedule_save()

    def _schedule_save(self):
        if getattr(self, "_save_job", None):
            self.parent.after_cancel(self._save_job)
        self._save_job = self.parent.after(600, self._changed)

    # ------------------------------------------------------------------ preview tab
    def _build_preview(self):
        left = ttk.Frame(self.prev_tab)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.pcanvas = tk.Canvas(left, width=SCREEN_W * 2, height=SCREEN_H * 2, bg="#000000",
                                 highlightthickness=1, highlightbackground="#007373")
        self.pcanvas.pack()
        ttk.Label(left, text="Exactly what the Core2 shows (2x, RGB565 colours).").pack(anchor="w")
        row = ttk.Frame(left)
        row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row, text="Mood:").pack(side=tk.LEFT)
        self.prev_pose = tk.IntVar(value=0)
        for i, n in enumerate(POSES):
            ttk.Radiobutton(row, text=n, variable=self.prev_pose, value=i,
                            command=self._render_static).pack(side=tk.LEFT)
        row2 = ttk.Frame(left)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="Manual mouth opening:").pack(side=tk.LEFT)
        self.manual = tk.DoubleVar(value=0.0)
        ttk.Scale(row2, from_=0.0, to=1.0, variable=self.manual, length=300,
                  command=lambda _v: self._render_static()).pack(side=tk.LEFT, padx=6)

        side = ttk.Frame(self.prev_tab, padding=(10, 0))
        side.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(side, text="Mouth & subtitle tuning", style="Header.TLabel").pack(anchor="w")
        self.param_vars = {}
        for key, label, lo, hi, step in PARAM_SPECS:
            f = ttk.Frame(side)
            f.pack(fill=tk.X)
            ttk.Label(f, text=label, width=26).pack(side=tk.LEFT)
            v = tk.DoubleVar(value=self.project.params[key])
            val = ttk.Label(f, width=7)
            val.pack(side=tk.RIGHT)

            def on(_x, k=key, var=v, lab=val, st=step):
                x = round(var.get() / st) * st
                self.project.params[k] = int(round(x)) if st >= 1 else round(x, 3)
                lab.config(text=f"{self.project.params[k]}")
                self._fields.clear()
                self._render_static()
                self._schedule_save()
            ttk.Scale(f, from_=lo, to=hi, variable=v, command=on).pack(side=tk.LEFT, fill=tk.X, expand=True)
            val.config(text=f"{self.project.params[key]}")
            self.param_vars[key] = v
        ttk.Button(side, text="Reset tuning to defaults", command=self._reset_params).pack(anchor="w", pady=(4, 8))

        ttk.Label(side, text="Mouth shapes (from Piper's phonemes)", style="Header.TLabel").pack(anchor="w")
        vf = ttk.Frame(side)
        vf.pack(fill=tk.X)
        self.vis_sel = tk.StringVar(value=VISEMES[7])
        cb = ttk.Combobox(vf, textvariable=self.vis_sel, values=VISEMES, state="readonly", width=12)
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._load_vis_sliders())
        self.vis_open = tk.DoubleVar()
        self.vis_width = tk.DoubleVar()
        ttk.Label(vf, text=" open").pack(side=tk.LEFT)
        ttk.Scale(vf, from_=-0.3, to=1.0, variable=self.vis_open, length=110,
                  command=lambda _v: self._set_vis()).pack(side=tk.LEFT)
        ttk.Label(vf, text=" round/wide").pack(side=tk.LEFT)
        ttk.Scale(vf, from_=-1.0, to=1.0, variable=self.vis_width, length=110,
                  command=lambda _v: self._set_vis()).pack(side=tk.LEFT)
        self.vis_info = ttk.Label(side, text="")
        self.vis_info.pack(anchor="w", pady=(0, 8))
        self._load_vis_sliders()

        ttk.Label(side, text="Test with the real TTS", style="Header.TLabel").pack(anchor="w")
        ttk.Label(side, text="User message (sets the opening mood):").pack(anchor="w")
        self.test_user = tk.Entry(side, bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff")
        self.test_user.insert(0, "How do volcanoes work?")
        self.test_user.pack(fill=tk.X)
        ttk.Label(side, text="Reply to speak (split into TTS chunks like the device):").pack(anchor="w", pady=(4, 0))
        self.test_reply = tk.Text(side, height=5, bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff", wrap=tk.WORD)
        self.test_reply.insert("1.0", "Great question! Volcanoes form where melted rock rises through cracks in the "
                                      "crust. Sorry, I can't show you a real one, but the pressure builds until it "
                                      "erupts. Isn't that amazing?")
        self.test_reply.pack(fill=tk.X)
        b = ttk.Frame(side)
        b.pack(fill=tk.X, pady=4)
        ttk.Button(b, text="Speak", command=self._speak).pack(side=tk.LEFT)
        ttk.Button(b, text="Stop", command=self._stop).pack(side=tk.LEFT, padx=4)
        self.speak_info = ttk.Label(side, text="", wraplength=420, justify=tk.LEFT)
        self.speak_info.pack(anchor="w")

        ttk.Label(side, text="Export for the SD card", style="Header.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Button(side, text="Export m5_asymmetric_poses.bin...", command=self._export).pack(anchor="w")
        self.export_info = tk.Text(side, height=7, bg="#1e1e1e", fg="#00ff00", wrap=tk.WORD, relief=tk.FLAT)
        self.export_info.pack(fill=tk.X, pady=(4, 0))
        self.export_info.insert("1.0", "Export writes one file. Copy it to the ROOT of the SD card as\n"
                                       "  /m5_asymmetric_poses.bin\n"
                                       "(next to slots_words.json), then restart MantisKAI.")
        self.export_info.config(state=tk.DISABLED)
        self.parent.after(200, self._render_static)

    def _vis_idx(self):
        return VISEMES.index(self.vis_sel.get()) if self.vis_sel.get() in VISEMES else 0

    def _load_vis_sliders(self):
        o, w = self.project.visemes[self._vis_idx()]
        self._vis_loading = True
        self.vis_open.set(o)
        self.vis_width.set(w)
        self._vis_loading = False
        self._set_vis()

    def _set_vis(self):
        i = self._vis_idx()
        if not getattr(self, "_vis_loading", False):
            self.project.visemes[i] = [round(self.vis_open.get(), 2), round(self.vis_width.get(), 2)]
            self._schedule_save()
        o, w = self.project.visemes[i]
        self.vis_info.config(text=f"{VISEMES[i]}: open {o:+.2f}  width {w:+.2f}")
        if not self.speech and hasattr(self, "pcanvas"):
            lines = wrap_lines(f"Shape: {VISEMES[i]}", int(self.project.params["line_chars"]))
            self._show(self.render_device(self.prev_pose.get(), 0.0, lines, 0, shape=(o, w)))

    def _reset_params(self):
        self.project.params = dict(DEFAULT_PARAMS)
        for k, v in self.param_vars.items():
            v.set(self.project.params[k])
        self._fields.clear()
        self._changed()
        self._render_static()

    # ---- rendering (DEVICE-MATCH layout)
    def _field(self, i):
        pr = self.project
        x1, y1, x2, y2 = pr.box
        key = (tuple(map(tuple, pr.poses[i]["points"])), pr.split_y(i), tuple(pr.box),
               pr.params["pull_lower"], pr.params["pull_upper"], pr.params["falloff"])
        c = self._fields.get(i)
        if c and c[0] == key:
            return c[1]
        local = [(round(px - x1), round(py - y1)) for px, py in pr.poses[i]["points"]]
        f = displacement_field(local, round(pr.split_y(i) - y1), x2 - x1, y2 - y1, pr.params)
        self._fields[i] = (key, f)
        return f

    def _field_x(self, i):
        pr = self.project
        x1, y1, x2, y2 = pr.box
        key = (tuple(map(tuple, pr.poses[i]["points"])), tuple(pr.box), pr.params["width_pull"], pr.params["falloff"])
        c = getattr(self, "_fields_x", {}).get(i)
        if c and c[0] == key:
            return c[1]
        local = [(round(px - x1), round(py - y1)) for px, py in pr.poses[i]["points"]]
        f = displacement_field_x(local, x2 - x1, y2 - y1, pr.params)
        self._fields_x = getattr(self, "_fields_x", {})
        self._fields_x[i] = (key, f)
        return f

    def _shape_mouth(self, i, crop, open_, width):
        pr = self.project
        x1, y1 = pr.box[0], pr.box[1]
        xs = [round(px - x1) for px, _ in pr.poses[i]["points"]]
        split = round(pr.split_y(i) - y1)
        return warp_shape(crop, self._field(i), self._field_x(i), open_, width, split, min(xs), max(xs))

    def _font(self):
        if not hasattr(self, "_pfont"):
            try:
                self._pfont = ImageFont.truetype("DejaVuSans.ttf", 13)
            except Exception:
                try:
                    self._pfont = ImageFont.truetype("arial.ttf", 13)
                except Exception:
                    self._pfont = ImageFont.load_default()
        return self._pfont

    def render_device(self, pose_idx, intensity, lines=None, line_idx=0, shape=None):
        pr = self.project
        x1, y1, x2, y2 = pr.box
        scr = quantize565(self.screen(pose_idx)).copy()
        crop = np.asarray(scr.crop((x1, y1, x2, y2)))
        if lines is not None:  # speaking UI: header + subtitle bar, as drawn by drawAvatarSpeakingUI()
            if shape is not None:  # viseme mode: (open, width)
                mouth = self._shape_mouth(pose_idx, crop, shape[0], shape[1])
            else:
                mouth = warp_crop(crop, self._field(pose_idx), intensity)
            scr.paste(Image.fromarray(np.ascontiguousarray(mouth)), (x1, y1))
            d = ImageDraw.Draw(scr)
            d.rectangle((0, 0, SCREEN_W, HEADER_H - 1), fill=(16, 16, 16))
            d.text((8, 7), f"MantisKAI  [{POSES[pose_idx]}]", fill=(255, 255, 255), font=self._font())
            sy = SCREEN_H - SUB_H
            d.rectangle((0, sy, SCREEN_W, SCREEN_H), fill=(8, 8, 8))
            d.line((0, sy, SCREEN_W, sy), fill=(0, 115, 115))
            cur = lines[line_idx] if lines else ""
            nxt = lines[line_idx + 1] if line_idx + 1 < len(lines) else ""
            d.text((SCREEN_W // 2, sy + 12), cur, fill=(255, 255, 255), font=self._font(), anchor="mm")
            d.text((SCREEN_W // 2, sy + 31), nxt, fill=(110, 110, 110), font=self._font(), anchor="mm")
        else:
            mouth = warp_crop(crop, self._field(pose_idx), intensity)
            scr.paste(Image.fromarray(np.ascontiguousarray(mouth)), (x1, y1))
        return scr

    def _show(self, img):
        self._prev_photo = ImageTk.PhotoImage(img.resize((SCREEN_W * 2, SCREEN_H * 2), Image.NEAREST))
        self.pcanvas.delete("all")
        self.pcanvas.create_image(0, 0, anchor="nw", image=self._prev_photo)

    def _render_static(self):
        if self.speech:
            return
        self._show(self.render_device(self.prev_pose.get(), float(self.manual.get()),
                                      lines=wrap_lines("This is how subtitles look on the device.",
                                                       int(self.project.params["line_chars"])), line_idx=0))

    # ---- real TTS run, chunk by chunk like fetchAndPlayTTS()
    def _speak(self):
        self._stop()
        reply = self.test_reply.get("1.0", tk.END).strip()
        user = self.test_user.get().strip()
        if not reply:
            return
        chunks = self.deps["split_chunks"](reply)
        moods = self.deps["build_moods"](user, reply)
        names = [m.lower() for m in POSES]
        mood_idx = [names.index(m) if m in names else 0 for m in moods]
        self.speak_info.config(text=f"Synthesizing {len(chunks)} chunk(s)...")
        token = object()
        self._speak_token = token

        def work():
            import wave
            audio = []
            for c in chunks:
                vis = None
                if self.deps.get("synth_tts_chunk"):  # same call /api/tts makes: PCM + X-Visemes
                    pcm_b, vis, rate = self.deps["synth_tts_chunk"](c)
                    if pcm_b:
                        buf = BytesIO()
                        with wave.open(buf, "wb") as wf:
                            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate); wf.writeframes(pcm_b)
                        wav = buf.getvalue()
                    else:
                        wav = None
                else:
                    wav = self.deps["synth_wav"](c)
                if not wav or len(wav) < 44:
                    self.parent.after(0, lambda: self.speak_info.config(
                        text="TTS failed: is the Piper voice loaded? (Settings tab)"))
                    return
                with wave.open(BytesIO(wav)) as wf:
                    rate = wf.getframerate()
                    pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
                audio.append((c, wav, pcm, rate, vis))
            if self._speak_token is token:
                self.parent.after(0, lambda: self._start_speech(audio, mood_idx))
        threading.Thread(target=work, daemon=True).start()

    def _start_speech(self, audio, mood_idx):
        p = self.project.params
        plan = []
        for i, (text, wav, pcm, rate, vis) in enumerate(audio):
            env = smooth_envelope(loudness_steps(pcm, rate, p), p)
            dur = len(pcm) * 1000.0 / rate
            plan.append({"text": text, "wav": wav, "env": env, "dur": dur, "vis": decode_visemes(vis) if vis else None,
                         "mood": mood_idx[min(i, len(mood_idx) - 1)] if mood_idx else 0,
                         "lines": wrap_lines(text, int(p["line_chars"]))})
        mode = "mouth shapes from Piper phonemes" if all(c["vis"] for c in plan) else "loudness only (no alignments)"
        self.speak_info.config(text="Moods per chunk: " + ", ".join(POSES[c["mood"]] for c in plan) + f"\nMouth: {mode}")
        self.speech = {"plan": plan, "i": -1, "t0": 0, "sound": None}
        self._next_chunk()

    def _next_chunk(self):
        s = self.speech
        if not s:
            return
        s["i"] += 1
        if s["i"] >= len(s["plan"]):
            self._finish_speech()
            return
        c = s["plan"][s["i"]]
        pg = self.deps.get("pygame")
        if pg:
            try:
                snd = pg.mixer.Sound(BytesIO(c["wav"]))
                snd.play()
                s["sound"] = snd
            except Exception as e:
                print(f"[AVATAR] audio: {e}")
        s["t0"] = time.perf_counter()
        s["last"] = 0.0
        s["shape"] = [0.0, 0.0]
        self._tick()

    def _tick(self):
        s = self.speech
        if not s:
            return
        c = s["plan"][s["i"]]
        t = (time.perf_counter() - s["t0"]) * 1000.0
        if t >= c["dur"]:
            self._next_chunk()
            return
        p = self.project.params
        inten = intensity_at(c["env"], t, p)
        li = line_index_at(c["lines"], t, c["dur"])
        shape = None
        if c["vis"]:  # DEVICE-MATCH: same targets, easing and loudness emphasis as the firmware
            dt, s["last"] = t - s["last"], t
            tgt = viseme_target(c["vis"], t, self.project.visemes, p["lookahead_ms"])
            s["shape"][0] = viseme_smooth(s["shape"][0], tgt[0], dt, p["attack"])
            s["shape"][1] = viseme_smooth(s["shape"][1], tgt[1], dt, p["attack"])
            i0 = int((t + p["lookahead_ms"]) // ENV_STEP_MS)
            env = float(c["env"][min(max(i0, 0), len(c["env"]) - 1)]) if len(c["env"]) else 0.0
            shape = (s["shape"][0] * (0.6 + 0.4 * env), s["shape"][1])
        self._show(self.render_device(c["mood"], inten, c["lines"], li, shape=shape))
        self.preview_job = self.parent.after(33, self._tick)

    def _finish_speech(self):
        self.speech = None
        self._render_static()

    def _stop(self):
        self._speak_token = None
        if self.preview_job:
            self.parent.after_cancel(self.preview_job)
            self.preview_job = None
        if self.speech and self.speech.get("sound"):
            try:
                self.speech["sound"].stop()
            except Exception:
                pass
        self.speech = None
        self._render_static()

    # ------------------------------------------------------------------ export (format POS2)
    def _export(self):
        pr = self.project
        if not pr.poses[0]["path"]:
            messagebox.showerror("Export", "Load artwork for the Base pose first.")
            return
        missing = [POSES[i] for i, p in enumerate(pr.poses) if not p["path"]]
        if missing and not messagebox.askyesno(
                "Export", f"No artwork for: {', '.join(missing)}.\nUse the Base pose for those moods?"):
            return
        folder = filedialog.askdirectory(title="Choose a folder for the SD card file")
        if not folder:
            return
        out = Path(folder) / "m5_asymmetric_poses.bin"
        try:
            out.write_bytes(self.build_bin())
        except Exception as e:
            messagebox.showerror("Export", str(e))
            return
        self._changed()
        self.export_info.config(state=tk.NORMAL)
        self.export_info.delete("1.0", tk.END)
        self.export_info.insert("1.0", (
            f"Saved {out}  ({out.stat().st_size // 1024} KB)\n\n"
            "Put it on the MantisKAI SD card:\n"
            "  1. Copy m5_asymmetric_poses.bin to the ROOT of the card\n"
            "     -> /m5_asymmetric_poses.bin  (same place as slots_words.json)\n"
            "  2. Replace the old file if there is one.\n"
            "  3. Put the card back and restart MantisKAI.\n"
            "The mouth tuning and subtitle width travel inside the file."))
        self.export_info.config(state=tk.DISABLED)

    def build_bin(self):
        """POS2 (big-endian):
        "POS2" | w h targetX targetY numPoints (u16) |
        attack release flutter (u16 x1000) gain (u16 x100) pullLower (i16 x100) pullUpper (i16 x100)
        falloff lookaheadMs lineChars (u16) widthPull (u16 x100) reserved x2 (u16) |
        5 x { splitY (u16, mouth-local) | points (numPoints x u16 x,y, mouth-local) |
              mouth crop RGB565 BE (w*h) | full screen RGB565 BE (320*240) } |
        "VIS1" + 10 x (open, width i16 x1000) + cavity, teeth (RGB565 u16)   <- ignored by older firmware"""
        pr, p = self.project, self.project.params
        x1, y1, x2, y2 = [int(v) for v in pr.box]
        w, h = x2 - x1, y2 - y1
        if w < 8 or h < 8:
            raise ValueError("Mouth box is too small")
        blob = bytearray(b"POS2")
        blob += struct.pack(">HHHHH", w, h, x1, y1, pr.num_points)
        blob += struct.pack(">HHHHhhHHHHHH",
                            int(round(p["attack"] * 1000)), int(round(p["release"] * 1000)),
                            int(round(p["flutter"] * 1000)), int(round(p["gain"] * 100)),
                            int(round(p["pull_lower"] * 100)), int(round(p["pull_upper"] * 100)),
                            int(p["falloff"]), int(p["lookahead_ms"]), int(p["line_chars"]),
                            int(round(p["width_pull"] * 100)), 0, 0)
        for i in range(len(POSES)):
            src = i if pr.poses[i]["path"] else 0
            scr = self.screen(src)
            pts = pr.poses[src]["points"]
            blob += struct.pack(">H", int(round(min(max(pr.split_y(src) - y1, 0), h - 1))))
            for px, py in pts:
                blob += struct.pack(">HH", int(min(max(round(px - x1), 0), w - 1)),
                                    int(min(max(round(py - y1), 0), h - 1)))
            blob += to_rgb565_be(scr.crop((x1, y1, x2, y2)))
            blob += to_rgb565_be(scr)
        # mouth-shape table: "VIS1" + 10 x (open, width as i16 x1000) + cavity, teeth (RGB565)
        blob += b"VIS1"
        for o, w in pr.visemes:
            blob += struct.pack(">hh", int(round(o * 1000)), int(round(w * 1000)))
        c565 = lambda r, g, b: ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        blob += struct.pack(">HH", c565(*CAVITY_RGB), c565(*TEETH_RGB))
        return bytes(blob)
