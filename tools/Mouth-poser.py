#!/usr/bin/env python3
"""
MantisKAI High-Quality Multi-Pose Packer
- Click same logical sequence on every pose
- Live intensity preview so you can judge the warp quality
- Exports m5_asymmetric_poses.bin ready for SD card
"""

import struct
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk, ImageDraw
from pathlib import Path
import math

EMOTIONS = ["Base", "Thinking", "Surprised", "Excited", "Sad"]

class HQPosePacker:
    def __init__(self, root, image_paths, points_per_pose=8,
                 bbox=(90, 125, 230, 195)):  # slightly larger mouth region
        self.root = root
        self.root.title("MantisKAI HQ Pose Packer")
        self.paths = [Path(p) for p in image_paths]
        self.emotions = EMOTIONS
        self.points_per_pose = points_per_pose
        self.bbox = {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]}
        self.w = self.bbox["x2"] - self.bbox["x1"]
        self.h = self.bbox["y2"] - self.bbox["y1"]

        self.all_pose_points = []          # list of lists
        self.current_pose_points = []
        self.current_idx = 0
        self.preview_intensity = 0.0
        self.mode = "map"                  # "map" or "preview"

        # UI
        top = tk.Frame(root)
        top.pack(fill="x", padx=6, pady=4)
        self.label = tk.Label(top, text="", font=("Arial", 12, "bold"))
        self.label.pack(side="left")

        self.canvas = tk.Canvas(root, width=320, height=240, bg="#1a1a1a",
                                highlightthickness=0)
        self.canvas.pack(padx=6)
        self.canvas.bind("<Button-1>", self.on_click)

        # Controls
        ctrl = tk.Frame(root)
        ctrl.pack(fill="x", padx=6, pady=6)

        self.intensity_var = tk.DoubleVar(value=0.0)
        tk.Label(ctrl, text="Intensity").pack(side="left")
        self.slider = ttk.Scale(ctrl, from_=0.0, to=1.0, variable=self.intensity_var,
                                orient="horizontal", length=180, command=self.on_slider)
        self.slider.pack(side="left", padx=6)

        self.btn_preview = tk.Button(ctrl, text="Preview Warp", command=self.toggle_preview)
        self.btn_preview.pack(side="left", padx=4)

        self.btn_export = tk.Button(ctrl, text="Export .bin", command=self.export_binary,
                                    state="disabled", font=("Arial", 10, "bold"))
        self.btn_export.pack(side="right")

        self.hint = tk.Label(root,
            text="Recommended order: L-corner → R-corner → Upper-lip mid → Lower-lip mid → "
                 "Upper-left → Upper-right → Lower-left → Lower-right",
            font=("Arial", 8), fg="#888")
        self.hint.pack(pady=2)

        self.load_next_image()

    def load_next_image(self):
        if self.current_idx >= len(self.paths):
            self.label.config(text="All poses mapped — scrub intensity & Export")
            self.btn_export.config(state="normal")
            self.mode = "preview"
            self.show_preview()
            return

        self.current_pose_points = []
        self.mode = "map"
        img = Image.open(self.paths[self.current_idx]).convert("RGB").resize((320, 240), Image.LANCZOS)
        self.current_pil = img
        self.tk_img = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        self.canvas.create_rectangle(self.bbox["x1"], self.bbox["y1"],
                                     self.bbox["x2"], self.bbox["y2"],
                                     outline="#ff3333", width=2)
        self.label.config(
            text=f"Pose {self.current_idx+1}/5  {self.emotions[self.current_idx]}   "
                 f"({len(self.current_pose_points)}/{self.points_per_pose})")

    def on_click(self, event):
        if self.mode != "map":
            return
        if len(self.current_pose_points) >= self.points_per_pose:
            return
        rx = max(0, min(event.x - self.bbox["x1"], self.w - 1))
        ry = max(0, min(event.y - self.bbox["y1"], self.h - 1))
        self.current_pose_points.append((rx, ry))
        # visual
        self.canvas.create_oval(event.x-4, event.y-4, event.x+4, event.y+4,
                                fill="#ff00ff", outline="white", width=1)
        self.canvas.create_text(event.x, event.y-10, text=str(len(self.current_pose_points)),
                                fill="white", font=("Arial", 7, "bold"))
        self.label.config(
            text=f"Pose {self.current_idx+1}/5  {self.emotions[self.current_idx]}   "
                 f"({len(self.current_pose_points)}/{self.points_per_pose})")

        if len(self.current_pose_points) == self.points_per_pose:
            self.all_pose_points.append(list(self.current_pose_points))
            self.current_idx += 1
            self.root.after(350, self.load_next_image)

    def on_slider(self, _=None):
        self.preview_intensity = self.intensity_var.get()
        if self.mode == "preview":
            self.show_preview()

    def toggle_preview(self):
        if len(self.all_pose_points) < 5:
            messagebox.showinfo("Info", "Finish mapping all 5 poses first")
            return
        self.mode = "preview"
        self.show_preview()

    def show_preview(self):
        """Simple CPU preview of the same IDW used on device (Base pose)"""
        if not self.all_pose_points:
            return
        pts = self.all_pose_points[0]  # preview on Base
        src = self.current_pil.crop((self.bbox["x1"], self.bbox["y1"],
                                     self.bbox["x2"], self.bbox["y2"]))
        out = Image.new("RGB", (self.w, self.h))
        intensity = self.preview_intensity

        # Same math as firmware (simplified for preview speed)
        for y in range(self.h):
            for x in range(self.w):
                tw = 0.0
                cdy = 0.0
                for px, py in pts:
                    dx = x - px
                    dy = y - py
                    d2 = dx*dx + dy*dy + 0.8
                    w = 1.0 / d2
                    # upper vs lower lip bias
                    pull = (intensity * 9.0) if py > self.h * 0.48 else (-intensity * 3.2)
                    tw += w
                    cdy += pull * w
                sy = int(y - cdy / tw)
                sx = x
                if 0 <= sx < self.w and 0 <= sy < self.h:
                    out.putpixel((x, y), src.getpixel((sx, sy)))
                else:
                    out.putpixel((x, y), (20, 20, 20))

        # composite back onto full frame for context
        full = self.current_pil.copy()
        full.paste(out, (self.bbox["x1"], self.bbox["y1"]))
        draw = ImageDraw.Draw(full)
        draw.rectangle([self.bbox["x1"], self.bbox["y1"],
                        self.bbox["x2"], self.bbox["y2"]], outline="#00ff88", width=2)
        self.tk_img = ImageTk.PhotoImage(full)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        self.label.config(text=f"PREVIEW  intensity={intensity:.2f}   (Base pose)")

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

    def export_binary(self):
        if len(self.all_pose_points) != 5:
            messagebox.showerror("Error", "Need exactly 5 poses mapped")
            return
        out = Path("m5_asymmetric_poses.bin")
        with open(out, "wb") as f:
            f.write(b"POSE")
            f.write(struct.pack(">HHHHH", self.w, self.h,
                                self.bbox["x1"], self.bbox["y1"],
                                self.points_per_pose))
            for idx, path in enumerate(self.paths):
                img = Image.open(path).convert("RGB").resize((320, 240), Image.LANCZOS)
                for px, py in self.all_pose_points[idx]:
                    f.write(struct.pack(">HH", px, py))
                f.write(self.convert_crop_to_rgb565(img))
        size = out.stat().st_size
        messagebox.showinfo("Done", f"Wrote {out}\n{size:,} bytes\nCopy to SD card root.")
        print(f"Exported {out} ({size} bytes)")

if __name__ == "__main__":
    # ← put your five pose images here (order matters)
    files = [
        "poses/base.png",
        "poses/thinking.png",
        "poses/surprised.png",
        "poses/excited.png",
        "poses/sad.png",
    ]
    root = tk.Tk()
    # Tweak bbox to tightly frame the mouth on your art
    HQPosePacker(root, files, points_per_pose=8,
                 bbox=(90, 125, 230, 195))
    root.mainloop()
