"""ComfyUI node: hand the finished image back as base64, never write it.

Drop this file into ComfyUI's custom_nodes folder (a single .py file is enough):

    ComfyUI/custom_nodes/mantis_inline_image.py

then restart ComfyUI. The node appears as "Mantis Inline Image" under the
MantisKAI category, and the bridge picks it up with:

    generate_image(..., image_node="MantisInlineImage")

Unlike PreviewImage (temp folder) and SaveImage (output folder), this node
touches no disk at all. The pixels go straight from the VAE decode tensor to an
in-memory PNG, which is base64-encoded into the node's `ui` payload. ComfyUI
reports that payload under /history/<prompt_id> -> outputs["9"]["mantis_image"],
so the bridge reads it out of the JSON it already fetches and never needs
/view, a temp directory, or a cleanup pass.

The node passes IMAGE straight through, so it can replace the SaveImage /
PreviewImage node at the end of the graph without rewiring anything, and it is
marked as an output node so ComfyUI still executes the branch.

The base64 copy does live in ComfyUI's in-memory history until it is evicted
(history keeps the last N prompts, N is configurable in ComfyUI). Nothing is
written to disk, but if RAM matters, keep the `resize` value small or clear the
queue/history from the UI.

Payload shape, per image in the batch:

    {"data": "<base64 png>", "width": 512, "height": 512,
     "format": "png", "bytes": 128861}
"""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image


def _to_pil(image) -> Image.Image:
    """ComfyUI IMAGE is a [B, H, W, C] float tensor in 0..1."""
    array = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
    if array.ndim == 4:
        array = array[0]
    channels = array.shape[-1]
    mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(channels, "RGB")
    if mode == "RGB" and channels != 3:
        array = array[..., :3]
    return Image.fromarray(array, mode=mode)


class MantisInlineImage:
    """Encode the image to PNG/JPEG/raw RGB565 base64 and pass it through."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                # png is lossless (the bridge downscales with LANCZOS, so keep
                # it); jpeg is smaller; rgb565 is already packed for the panel
                # (little-endian, 5-6-5) and needs no decoding on the device.
                "format": (["png", "jpeg", "rgb565"],),
                # 0 keeps the generated size; otherwise the long side is
                # scaled down with LANCZOS before encoding.
                "resize": ("INT", {"default": 0, "min": 0, "max": 4096}),
                "quality": ("INT", {"default": 92, "min": 1, "max": 100}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "encode"
    CATEGORY = "MantisKAI"
    OUTPUT_NODE = True

    def encode(self, images, format="png", resize=0, quality=92):
        fmt = (format or "png").lower()
        payload = []
        for image in images:
            pil = _to_pil(image)
            if resize and max(pil.size) > int(resize):
                pil.thumbnail((int(resize), int(resize)), Image.LANCZOS)
            if fmt == "rgb565":
                raw = self._rgb565(pil)
            else:
                raw = self._encode(pil, fmt, int(quality))
            payload.append({
                "data": base64.b64encode(raw).decode("ascii"),
                "width": pil.size[0],
                "height": pil.size[1],
                "format": fmt,
                "bytes": len(raw),
            })
        # `ui` is merged into outputs[node_id] of /history/<prompt_id>; the
        # passthrough keeps the graph's shape unchanged.
        return {"ui": {"mantis_image": payload}, "result": (images,)}

    @staticmethod
    def _encode(pil, fmt, quality) -> bytes:
        buffer = io.BytesIO()
        if fmt == "jpeg":
            pil.convert("RGB").save(buffer, format="JPEG", quality=quality)
        else:
            # compress_level 1: fast, the PNG is thrown away after the request.
            pil.save(buffer, format="PNG", compress_level=1)
        return buffer.getvalue()

    @staticmethod
    def _rgb565(pil) -> bytes:
        """Same packing the bridge does: little-endian RGB565, row-major."""
        small = pil.convert("RGB")
        out = bytearray(small.size[0] * small.size[1] * 2)
        offset = 0
        for red, green, blue in small.getdata():
            value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
            out[offset] = value & 0xFF
            out[offset + 1] = (value >> 8) & 0xFF
            offset += 2
        return bytes(out)


NODE_CLASS_MAPPINGS = {"MantisInlineImage": MantisInlineImage}
NODE_DISPLAY_NAME_MAPPINGS = {"MantisInlineImage": "Mantis Inline Image (no disk)"}
