#!/usr/bin/env python3
"""
MantisKAI High-Quality Multi-Pose Packer  (Mouth-poser.py)

1. Map the same control-point sequence on each of the 5 mood images.
2. Preview tab: faithful device-side warp + animated lip-sync + mood switch.
3. Export m5_asymmetric_poses.bin (mouth crop + full 320x240 face per pose).

Binary layout (big-endian):
  [HEADER 14 bytes]  magic "POSE", mouthW, mouthH, targetX, targetY, numPoints
  [5 x POSE BLOCK]
    control points   (numPoints x 2 x uint16)
    mouth crop RGB565 (mouthW x mouthH x 2)
    full-frame RGB565 (320 x 240 x 2)
"""

import struct
import math
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk, ImageDraw
from pathlib import Path

EMOTIONS = ["Base", "Accomplished", "Excited", "Sad", "Confused"]

# Same constants used on the device
AV_ATTACK = 0.45
AV_RELEASE = 0.12
AV_FLUTTER = 0.35
PULL_LOWER = 8.8
PULL_UPPER = -2.8
FALLOFF_D2 = 900.0


def warp_mouth(src_crop, points, intensity, w, h):
    """Exact same IDW + upper/lower bias the firmware uses."""
    out = Image.new("RGB", (w, h))
    pix_src = src_crop.load()
    pix_out = out.load()
    mid = h * 0.48
    for y in range(h):
        for x in range(w):
            tw = 0.0
            cdy = 0.0
            for px, py in points:
                dx = x - px
                dy = y - py
                d2 = dx * dx + dy * dy + 0.65
                weight = 1.0 / d2
                if d2 > FALLOFF_D2:
                    weight *= FALLOFF_D2 / d2
                pull = (intensity * PULL_LOWER) if py > mid else (intensity * PULL_UPPER)
                tw += weight
                cdy += pull * weight
            sy = y - int(cdy / (tw + 1e-5))
            if sy < 0:
                sy = 0
            elif sy >= h:
                sy = h - 1
            pix_out[x, y] = pix_src[x, sy]
    return out


class HQPosePacker:
    def __init__(self, root, image_paths=None, points_per_pose=8,
                 bbox=(90, 125, 230, 195)):
        self.root = root
        self.root.title("MantisKAI Mouth Poser")
        self.root.minsize(700, 520)

        self.points_per_pose = points_per_pose
        self.bbox = {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]}
        self.w = self.bbox["x2"] - self.bbox["x1"]
        self.h = self.bbox["y2"] - self.bbox["y1"]

        self.paths = []
        self.pil_images = [None] * 5
        self.crops = [None] * 5
        self.all_pose_points = [None] * 5
        self.current_map_idx = 0
        self.current_pose_points = []

        self.preview_pose = 0
        self.preview_intensity = 0.0
        self.smooth_intensity = 0.0
        self.animating = False
        self.anim_phase = 0.0
        self.anim_after = None

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.map_frame = ttk.Frame(nb)
        self.prev_frame = ttk.Frame(nb)
        nb.add(self.map_frame, text=" 1. Map Points ")
        nb.add(self.prev_frame, text=" 2. Preview Lip-Sync & Moods ")

        self._build_map_tab()
        self._build_preview_tab()

        if image_paths:
            self.load_images(image_paths)
        else:
            self.prompt_load()

    def _build_map_tab(self):
        top = ttk.Frame(self.map_frame)
        top.pack(fill="x", pady=4)
        self.map_label = ttk.Label(top, text="Load 5 pose images to begin",
                                   font=("Arial", 12, "bold"))
        self.map_label.pack(side="left")
        ttk.Button(top, text="Load 5 Images…", command=self.prompt_load).pack(side="right", padx=4)

        self.map_canvas = tk.Canvas(self.map_frame, width=320, height=240,
                                    bg="#1a1a1a", highlightthickness=1,
                                    highlightbackground="#444")
        self.map_canvas.pack(pady=6)
        self.map_canvas.bind("<Button-1>", self.on_map_click)

        self.map_hint = ttk.Label(
            self.map_frame,
            text="Click order: L-corner → R-corner → Upper-mid → Lower-mid → "
                 "Upper-L → Upper-R → Lower-L → Lower-R",
            font=("Arial", 9), foreground="#888")
        self.map_hint.pack()

        bot = ttk.Frame(self.map_frame)
        bot.pack(fill="x", pady=8)
        self.btn_export = ttk.Button(bot, text="Export m5_asymmetric_poses.bin",
                                     command=self.export_binary, state="disabled")
        self.btn_export.pack(side="right", padx=4)
        ttk.Label(bot, text="After mapping all 5, switch to Preview tab to verify.",
                  font=("Arial", 9)).pack(side="left")

    def prompt_load(self):
        paths = filedialog.askopenfilenames(
            title="Select 5 pose images (Base, Accomplished, Excited, Sad, Confused)",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if len(paths) != 5:
            messagebox.showwarning("Need 5 images",
                                   f"Select exactly 5 images in order:\n"
                                   f"{', '.join(EMOTIONS)}\n(Got {len(paths)})")
            return
        self.load_images(paths)

    def load_images(self, paths):
        self.paths = [Path(p) for p in paths]
        self.all_pose_points = [None] * 5
        self.current_map_idx = 0
        self.current_pose_points = []
        for i, p in enumerate(self.paths):
            img = Image.open(p).convert("RGB").resize((320, 240), Image.LANCZOS)
            self.pil_images[i] = img
            self.crops[i] = img.crop((self.bbox["x1"], self.bbox["y1"],
                                      self.bbox["x2"], self.bbox["y2"]))
        self._show_map_image()
        self._refresh_preview_faces()

    def _show_map_image(self):
        if self.current_map_idx >= 5:
            self.map_label.config(text="All 5 poses mapped — go to Preview tab, then Export")
            self.btn_export.config(state="normal")
            self.map_canvas.delete("all")
            return
        img = self.pil_images[self.current_map_idx]
        self.tk_map = ImageTk.PhotoImage(img)
        self.map_canvas.delete("all")
        self.map_canvas.create_image(0, 0, anchor="nw", image=self.tk_map)
        self.map_canvas.create_rectangle(
            self.bbox["x1"], self.bbox["y1"], self.bbox["x2"], self.bbox["y2"],
            outline="#ff3333", width=2)
        for i, (rx, ry) in enumerate(self.current_pose_points):
            cx = rx + self.bbox["x1"]
            cy = ry + self.bbox["y1"]
            self.map_canvas.create_oval(cx-4, cy-4, cx+4, cy+4,
                                        fill="#ff00ff", outline="white")
            self.map_canvas.create_text(cx, cy-11, text=str(i+1),
                                        fill="white", font=("Arial", 7, "bold"))
        self.map_label.config(
            text=f"Pose {self.current_map_idx+1}/5  —  {EMOTIONS[self.current_map_idx]}   "
                 f"({len(self.current_pose_points)}/{self.points_per_pose} points)")

    def on_map_click(self, event):
        if self.current_map_idx >= 5:
            return
        if len(self.current_pose_points) >= self.points_per_pose:
            return
        rx = max(0, min(event.x - self.bbox["x1"], self.w - 1))
        ry = max(0, min(event.y - self.bbox["y1"], self.h - 1))
        self.current_pose_points.append((rx, ry))
        self._show_map_image()
        if len(self.current_pose_points) == self.points_per_pose:
            self.all_pose_points[self.current_map_idx] = list(self.current_pose_points)
            self.current_map_idx += 1
            self.current_pose_points = []
            self.root.after(300, self._show_map_image)
            self._refresh_preview_faces()

    def _build_preview_tab(self):
        left = ttk.Frame(self.prev_frame)
        left.pack(side="left", fill="y", padx=6, pady=6)

        ttk.Label(left, text="Mood", font=("Arial", 10, "bold")).pack(anchor="w")
        self.mood_var = tk.IntVar(value=0)
        for i, name in enumerate(EMOTIONS):
            ttk.Radiobutton(left, text=name, variable=self.mood_var, value=i,
                            command=self._on_mood_change).pack(anchor="w")

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(left, text="Intensity", font=("Arial", 10, "bold")).pack(anchor="w")
        self.int_var = tk.DoubleVar(value=0.0)
        self.int_scale = ttk.Scale(left, from_=0.0, to=1.0, variable=self.int_var,
                                   orient="vertical", length=160,
                                   command=self._on_intensity)
        self.int_scale.pack(pady=4)
        self.int_label = ttk.Label(left, text="0.00")
        self.int_label.pack()

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=8)
        self.btn_anim = ttk.Button(left, text="▶ Animate Speech",
                                   command=self._toggle_anim)
        self.btn_anim.pack(fill="x", pady=2)
        ttk.Label(left, text="Simulates device attack/release\n+ flutter while open",
                  font=("Arial", 8), foreground="#666").pack()

        right = ttk.Frame(self.prev_frame)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.prev_canvas = tk.Canvas(right, width=320, height=240,
                                     bg="#111", highlightthickness=1,
                                     highlightbackground="#444")
        self.prev_canvas.pack()
        self.prev_status = ttk.Label(right, text="Map all 5 poses first",
                                     font=("Arial", 9))
        self.prev_status.pack(pady=4)

    def _refresh_preview_faces(self):
        self._render_preview()

    def _on_mood_change(self):
        self.preview_pose = self.mood_var.get()
        self._render_preview()

    def _on_intensity(self, _=None):
        if not self.animating:
            self.preview_intensity = self.int_var.get()
            self.smooth_intensity = self.preview_intensity
            self.int_label.config(text=f"{self.preview_intensity:.2f}")
            self._render_preview()

    def _toggle_anim(self):
        if self.animating:
            self.animating = False
            if self.anim_after:
                self.root.after_cancel(self.anim_after)
                self.anim_after = None
            self.btn_anim.config(text="▶ Animate Speech")
            self.preview_intensity = 0.0
            self.smooth_intensity = 0.0
            self.int_var.set(0.0)
            self.int_label.config(text="0.00")
            self._render_preview()
        else:
            if not all(self.all_pose_points):
                messagebox.showinfo("Map first", "Finish mapping all 5 poses first.")
                return
            self.animating = True
            self.anim_phase = 0.0
            self.btn_anim.config(text="■ Stop Animation")
            self._anim_tick()

    def _anim_tick(self):
        if not self.animating:
            return
        self.anim_phase += 0.09
        target = 0.0
        t = self.anim_phase
        cycle = math.fmod(t, 2.8)
        if cycle < 0.35:
            target = 0.15 + 0.7 * (cycle / 0.35)
        elif cycle < 0.55:
            target = 0.85 + 0.1 * math.sin(t * 18)
        elif cycle < 0.95:
            target = 0.85 * (1.0 - (cycle - 0.55) / 0.4)
        elif cycle < 1.4:
            target = 0.05
        elif cycle < 1.7:
            target = 0.4 + 0.35 * ((cycle - 1.4) / 0.3)
        elif cycle < 2.0:
            target = 0.55 * (1.0 - (cycle - 1.7) / 0.3)
        else:
            target = 0.02

        if target > self.smooth_intensity:
            self.smooth_intensity += (target - self.smooth_intensity) * AV_ATTACK
        else:
            self.smooth_intensity += (target - self.smooth_intensity) * AV_RELEASE

        flutter = 0.0
        if self.smooth_intensity > 0.15:
            flutter = (math.sin(t * 11.0) * 0.5 + 0.5) * AV_FLUTTER * self.smooth_intensity
        self.preview_intensity = max(0.0, min(1.0, self.smooth_intensity + flutter))

        self.int_var.set(self.preview_intensity)
        self.int_label.config(text=f"{self.preview_intensity:.2f}")
        self._render_preview()
        self.anim_after = self.root.after(33, self._anim_tick)

    def _render_preview(self):
        idx = self.preview_pose
        if self.pil_images[idx] is None or self.all_pose_points[idx] is None:
            self.prev_status.config(text="Map this pose first")
            return

        full = self.pil_images[idx].copy()
        points = self.all_pose_points[idx]
        intensity = self.preview_intensity

        if intensity > 0.001 and self.crops[idx] is not None:
            warped = warp_mouth(self.crops[idx], points, intensity, self.w, self.h)
            full.paste(warped, (self.bbox["x1"], self.bbox["y1"]))

        draw = ImageDraw.Draw(full)
        draw.rectangle([self.bbox["x1"], self.bbox["y1"],
                        self.bbox["x2"], self.bbox["y2"]],
                       outline="#00ff88", width=1)
        for i, (rx, ry) in enumerate(points):
            cx = rx + self.bbox["x1"]
            cy = ry + self.bbox["y1"]
            draw.ellipse([cx-3, cy-3, cx+3, cy+3], fill="#ff00ff", outline="white")

        draw.rectangle([0, 196, 320, 240], fill="#084108")
        draw.line([0, 196, 320, 196], fill="#03A6A6", width=1)
        draw.text((6, 202), f"[{EMOTIONS[idx]}]  intensity={intensity:.2f}", fill="white")
        draw.text((6, 218), "Subtitle area — reply text appears here on device", fill="#aaa")

        self.tk_prev = ImageTk.PhotoImage(full)
        self.prev_canvas.delete("all")
        self.prev_canvas.create_image(0, 0, anchor="nw", image=self.tk_prev)
        self.prev_status.config(
            text=f"Mood: {EMOTIONS[idx]}   |   Warp matches device firmware")

    def convert_crop_to_rgb565(self, pil_img):
        crop = pil_img.crop((self.bbox["x1"], self.bbox["y1"],
                             self.bbox["x2"], self.bbox["y2"]))
        data = bytearray()
        for y in range(crop.height):
            for x in range(crop.width):
                r, g, b = crop.getpixel((x, y))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                data.extend(struct.pack(">H", rgb565))
        return data

    def convert_full_to_rgb565(self, pil_img):
        data = bytearray()
        for y in range(240):
            for x in range(320):
                r, g, b = pil_img.getpixel((x, y))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                data.extend(struct.pack(">H", rgb565))
        return data

    def export_binary(self):
        if not all(self.all_pose_points):
            messagebox.showerror("Incomplete", "Map all 5 poses before export.")
            return
        out = Path("m5_asymmetric_poses.bin")
        with open(out, "wb") as f:
            f.write(b"POSE")
            f.write(struct.pack(">HHHHH", self.w, self.h,
                                self.bbox["x1"], self.bbox["y1"],
                                self.points_per_pose))
            for idx in range(5):
                img = self.pil_images[idx]
                for px, py in self.all_pose_points[idx]:
                    f.write(struct.pack(">HH", px, py))
                f.write(self.convert_crop_to_rgb565(img))
                f.write(self.convert_full_to_rgb565(img))
        size = out.stat().st_size
        messagebox.showinfo("Exported",
            f"Wrote {out}\n{size:,} bytes\n\nCopy to SD card root.")
        print(f"Exported {out} ({size} bytes)")


if __name__ == "__main__":
    root = tk.Tk()
    import sys
    paths = sys.argv[1:6] if len(sys.argv) >= 6 else None
    HQPosePacker(root, image_paths=paths, points_per_pose=8,
                 bbox=(90, 125, 230, 195))
    root.mainloop()
