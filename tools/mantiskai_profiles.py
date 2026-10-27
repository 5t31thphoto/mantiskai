"""
MantisKAI ComfyUI profiles.

A profile is a ComfyUI workflow in API format (the same node-id -> {class_type,
inputs} dict the server has always sent to /prompt), plus a few MantisKAI
settings. Profiles live as JSON files in ./profiles and are cycled from the
Core2 with the C button, the same way the B button cycles LoRAs.

Profile file:
{
  "name":          "Realistic",
  "template":      "masterpiece, {prompt}",   # {prompt} = text from the Core2
  "negative":      "blurry, ugly",            # written into the negative prompt
  "width": 640, "height": 480,                # fixed size, always 1 image
  "source_prompt": "...",                     # original prompt from an imported image
  "workflow":      { API-format ComfyUI graph }
}

Every generation, the server copies the workflow and:
  - writes the prompt into the positive text node(s), the negative into the
    negative text node(s)
  - randomizes every seed
  - forces the empty latent to width x height with batch_size 1
  - inserts the selected LoRA right after the model/CLIP loaders, so it stacks
    on top of whatever LoRAs the profile already has
  - saves through one known SaveImage node it can read back

Optional overrides in the JSON if auto-detection picks the wrong node:
  "positive_nodes": [["6", "text"]], "negative_nodes": [["7", "text"]]
"""

import copy
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO

from PIL import Image

PROFILES_DIR = "profiles"
OUTPUT_NODE_ID = "mantis_save"
LORA_NODE_ID = "mantis_lora"
OUTPUT_PREFIX = "MantisKAI_Temp"

# Inputs that hold prompt text on common encoders:
# CLIPTextEncode(text), CLIPTextEncodeSDXL(text_g/text_l),
# CLIPTextEncodeFlux(clip_l/t5xxl), various custom nodes (prompt)
TEXT_KEYS = ("text", "text_g", "text_l", "clip_l", "t5xxl", "prompt")

# Inputs never followed when looking upstream for the prompt text
NON_CONDITIONING_INPUTS = {
    "clip", "model", "vae", "image", "images", "pixels", "mask", "samples",
    "latent", "latent_image", "control_net", "upscale_model", "style_model",
    "clip_vision", "clip_vision_output", "noise", "sampler", "sigmas",
}

# Pixel-space upscalers right before the save node. The Core2 shows 320x240,
# so these only cost time; they are removed on import.
IMAGE_UPSCALERS = {
    "ImageScale", "ImageScaleBy", "ImageUpscaleWithModel",
    "ImageScaleToTotalPixels", "ImageScaleToMaxDimension",
}


class ProfileError(Exception):
    pass


# ------------------------------------------------------------------
# Graph helpers
# ------------------------------------------------------------------
def is_link(v):
    return (isinstance(v, list) and len(v) == 2
            and isinstance(v[0], (str, int)) and isinstance(v[1], int)
            and not isinstance(v[1], bool))


def normalize_graph(graph):
    """Copy the graph with every node id and link target as a string."""
    out = {}
    for nid, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        inputs = {}
        for k, v in (node.get("inputs") or {}).items():
            inputs[k] = [str(v[0]), v[1]] if is_link(v) else copy.deepcopy(v)
        n = {"class_type": node["class_type"], "inputs": inputs}
        if "_meta" in node:
            n["_meta"] = copy.deepcopy(node["_meta"])
        out[str(nid)] = n
    return out


def is_api_graph(data):
    return (isinstance(data, dict) and data
            and all(isinstance(n, dict) and "class_type" in n for n in data.values()))


def ancestors(graph, start_id):
    seen = set()
    stack = [start_id]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in graph:
            continue
        seen.add(nid)
        for v in graph[nid]["inputs"].values():
            if is_link(v):
                stack.append(v[0])
    return seen


def node_title(graph, nid):
    n = graph.get(nid, {})
    title = (n.get("_meta") or {}).get("title")
    return f"{nid} {n.get('class_type', '?')}" + (f" ({title})" if title and title != n.get("class_type") else "")


def _rewire(graph, old_link, new_link, skip=None):
    for nid, n in graph.items():
        if nid == skip:
            continue
        for k, v in n["inputs"].items():
            if is_link(v) and v[0] == old_link[0] and v[1] == old_link[1]:
                n["inputs"][k] = list(new_link)


# ------------------------------------------------------------------
# Reading ComfyUI images / files
# ------------------------------------------------------------------
def read_comfy_file(path):
    """
    Returns an API-format graph from a ComfyUI PNG (embedded "prompt" metadata)
    or from a .json file saved with ComfyUI's "Export (API)".
    """
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "prompt" in data and is_api_graph(data["prompt"]):
            data = data["prompt"]
        if is_api_graph(data):
            return normalize_graph(data)
        if isinstance(data, dict) and "nodes" in data and "links" in data:
            raise ProfileError(
                "This is an editor workflow, not the API format. In ComfyUI use "
                "Workflow > Export (API) and import that file instead.")
        raise ProfileError("This JSON is not a ComfyUI workflow.")

    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        raise ProfileError(f"Could not open image: {e}")
    meta = {}
    meta.update(getattr(img, "info", {}) or {})
    meta.update(getattr(img, "text", {}) or {})

    raw = meta.get("prompt")
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            raise ProfileError(f"The image's 'prompt' metadata is not valid JSON: {e}")
        if is_api_graph(data):
            return normalize_graph(data)
        raise ProfileError("The image's 'prompt' metadata is not a ComfyUI API workflow.")

    if meta.get("workflow"):
        raise ProfileError(
            "This image only has the editor workflow embedded, not the runnable "
            "API prompt. Open it in ComfyUI, use Workflow > Export (API), and "
            "import the .json file.")
    if meta.get("parameters"):
        raise ProfileError("This image was made by Automatic1111/Forge, not ComfyUI.")
    raise ProfileError("No ComfyUI metadata found in this image (it may have been "
                       "re-saved or stripped by an app or website).")


# ------------------------------------------------------------------
# Analysis: find where the prompt, seeds, size, model and output are
# ------------------------------------------------------------------
def _string_source(graph, link):
    """Prompt text supplied by a link (e.g. a string primitive node)."""
    node = graph.get(link[0])
    if not node:
        return None
    for k in ("value", "string", "text", "Text", "prompt"):
        if isinstance(node["inputs"].get(k), str):
            return (link[0], k)
    for k, v in node["inputs"].items():
        if isinstance(v, str):
            return (link[0], k)
    return None


def _find_text(graph, link, seen, depth=0):
    nid = link[0]
    node = graph.get(nid)
    if not node or nid in seen or depth > 16:
        return []
    seen.add(nid)
    found = []
    for k in TEXT_KEYS:
        v = node["inputs"].get(k)
        if isinstance(v, str):
            found.append((nid, k))
        elif is_link(v):
            src = _string_source(graph, v)
            if src:
                found.append(src)
    if found:
        return found
    for k, v in node["inputs"].items():
        if is_link(v) and k not in NON_CONDITIONING_INPUTS:
            found += _find_text(graph, v, seen, depth + 1)
    return found


def _dedupe(pairs):
    out = []
    for p in pairs:
        p = (str(p[0]), str(p[1]))
        if p not in out:
            out.append(p)
    return out


def _trace_root(graph, link, key):
    """Follow `key` inputs upstream (through LoRAs etc.) to the loader output."""
    seen = set()
    while link and link[0] in graph and link[0] not in seen:
        seen.add(link[0])
        nxt = graph[link[0]]["inputs"].get(key)
        if not is_link(nxt):
            return list(link)
        link = nxt
    return list(link) if link else None


def find_output(graph):
    if OUTPUT_NODE_ID in graph:
        return OUTPUT_NODE_ID
    cands = []
    for nid, n in graph.items():
        ct = n["class_type"]
        if is_link(n["inputs"].get("images")) and ("Save" in ct or "Preview" in ct):
            score = (1 if "Save" in ct else 0, len(ancestors(graph, nid)))
            cands.append((score, nid))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def analyze(profile_or_graph):
    profile = profile_or_graph if "workflow" in profile_or_graph else {"workflow": profile_or_graph}
    graph = profile["workflow"]
    info = {"positive": [], "negative": [], "seeds": [], "latents": [],
            "output": find_output(graph), "model_root": None, "clip_root": None,
            "samplers": []}

    pos_links, neg_links, model_links = [], [], []
    for nid, n in graph.items():
        ins = n["inputs"]
        has_cond = is_link(ins.get("positive")) or (
            n["class_type"] == "BasicGuider" and is_link(ins.get("conditioning")))
        if has_cond:
            info["samplers"].append(nid)
            if is_link(ins.get("positive")):
                pos_links.append(ins["positive"])
            elif is_link(ins.get("conditioning")):
                pos_links.append(ins["conditioning"])
            if is_link(ins.get("negative")):
                neg_links.append(ins["negative"])
            if is_link(ins.get("model")):
                model_links.append(ins["model"])
        for k in ("seed", "noise_seed"):
            v = ins.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                info["seeds"].append((nid, k))
        if all(isinstance(ins.get(k), int) for k in ("width", "height", "batch_size")):
            info["latents"].append(nid)

    pos = []
    for l in pos_links:
        pos += _find_text(graph, l, set())
    neg = []
    for l in neg_links:
        neg += _find_text(graph, l, set())
    pos = _dedupe(pos)
    # Flux-style workflows zero out the positive for the negative; never
    # write the negative text into a positive encoder.
    neg = [p for p in _dedupe(neg) if p not in pos]

    if profile.get("positive_nodes"):
        pos = _dedupe(profile["positive_nodes"])
    if profile.get("negative_nodes"):
        neg = _dedupe(profile["negative_nodes"])
    info["positive"], info["negative"] = pos, neg

    roots = [tuple(_trace_root(graph, l, "model")) for l in model_links]
    roots = [r for r in roots if r and r[0] != LORA_NODE_ID]
    if roots:
        info["model_root"] = list(max(set(roots), key=roots.count))
    for nid, _k in pos:
        clip = graph.get(nid, {}).get("inputs", {}).get("clip")
        if is_link(clip):
            info["clip_root"] = _trace_root(graph, clip, "clip")
            break
    return info


def describe(profile):
    """Human-readable report for the GUI."""
    g = profile["workflow"]
    a = analyze(profile)
    lines = []
    lines.append(f"Nodes: {len(g)}   Output: {a['output'] or 'NOT FOUND'}")
    lines.append("Prompt goes into: " + (", ".join(f"{node_title(g, n)}.{k}" for n, k in a["positive"])
                                         or "NOT FOUND - set positive_nodes in the profile file"))
    lines.append("Negative goes into: " + (", ".join(f"{node_title(g, n)}.{k}" for n, k in a["negative"])
                                           or "none (this workflow has no negative prompt)"))
    lines.append(f"Seeds randomized: {len(a['seeds'])}")
    lines.append("Size forced on: " + (", ".join(node_title(g, n) for n in a["latents"])
                                       or "none (no empty latent - size comes from the workflow)"))
    if a["model_root"]:
        clip = node_title(g, a["clip_root"][0]) if a["clip_root"] else "none (model-only LoRA)"
        lines.append(f"LoRA inserted after: model {node_title(g, a['model_root'][0])}, clip {clip}")
    else:
        lines.append("LoRA: no model loader found, LoRAs will be ignored for this profile")
    loaders = [f"{node_title(g, n)}: {v}" for n, node in g.items()
               for k, v in node["inputs"].items()
               if isinstance(v, str) and k in ("ckpt_name", "unet_name", "lora_name", "vae_name")]
    if loaders:
        lines.append("Models: " + "; ".join(loaders))
    return "\n".join(lines)


# ------------------------------------------------------------------
# Building profiles
# ------------------------------------------------------------------
def _choose_size(area):
    """Fixed 4:3 size with roughly the pixel budget the model was used at."""
    if area >= 900_000:
        return 1024, 768
    if area >= 600_000:
        return 896, 672
    return 640, 480


def profile_from_graph(graph, name):
    """
    Turn any API workflow into a single-image MantisKAI profile.
    Returns (profile, list_of_change_notes).
    """
    g = normalize_graph(graph)
    notes = []

    # An image MantisKAI made already contains our LoRA node; take it out so
    # the B-button LoRA is not baked into the profile.
    if LORA_NODE_ID in g:
        lora = g.pop(LORA_NODE_ID)
        m = lora["inputs"].get("model")
        c = lora["inputs"].get("clip")
        if is_link(m):
            _rewire(g, [LORA_NODE_ID, 0], m)
        if is_link(c):
            _rewire(g, [LORA_NODE_ID, 1], c)
        notes.append(f"Removed the MantisKAI LoRA that was used for this image "
                     f"({lora['inputs'].get('lora_name')}).")

    out = find_output(g)
    if not out:
        raise ProfileError("No SaveImage or PreviewImage node found in this workflow.")

    # Drop pixel upscalers between the final decode and the save
    link = g[out]["inputs"]["images"]
    while is_link(link) and g.get(link[0], {}).get("class_type") in IMAGE_UPSCALERS:
        up = g[link[0]]
        notes.append(f"Removed {up['class_type']} (the Core2 shows 320x240).")
        link = up["inputs"].get("image")
    if not is_link(link):
        raise ProfileError("Could not find the image that feeds the save node.")

    # One known output node
    if out != OUTPUT_NODE_ID:
        if g[out]["class_type"] != "SaveImage":
            notes.append(f"Replaced {g[out]['class_type']} with SaveImage.")
        g.pop(out)
    g[OUTPUT_NODE_ID] = {"class_type": "SaveImage",
                         "inputs": {"filename_prefix": OUTPUT_PREFIX, "images": link}}

    # Keep only what the final image depends on (drops extra saves/previews/branches)
    keep = ancestors(g, OUTPUT_NODE_ID)
    dropped = [g[n]["class_type"] for n in g if n not in keep]
    if dropped:
        notes.append(f"Removed {len(dropped)} node(s) not used by the final image: "
                     + ", ".join(sorted(set(dropped))))
    g = {n: v for n, v in g.items() if n in keep}

    # Single image only
    for nid, n in g.items():
        ins = n["inputs"]
        if isinstance(ins.get("batch_size"), int) and ins["batch_size"] != 1:
            notes.append(f"{node_title(g, nid)}: batch_size {ins['batch_size']} -> 1")
            ins["batch_size"] = 1
        if "Batch" in n["class_type"] and isinstance(ins.get("amount"), int) and ins["amount"] != 1:
            notes.append(f"{node_title(g, nid)}: amount {ins['amount']} -> 1")
            ins["amount"] = 1

    profile = {"name": name, "template": "{prompt}", "negative": "",
               "width": 640, "height": 480, "source_prompt": "", "workflow": g}
    a = analyze(profile)

    if a["latents"]:
        ins = g[a["latents"][0]]["inputs"]
        w, h = _choose_size(ins["width"] * ins["height"])
        profile["width"], profile["height"] = w, h
        notes.append(f"Size {ins['width']}x{ins['height']} -> fixed {w}x{h} (4:3 for the Core2).")
    else:
        notes.append("No empty latent found (img2img?). Output size comes from the workflow.")

    def text_of(pairs):
        vals = []
        for nid, k in pairs:
            v = g.get(nid, {}).get("inputs", {}).get(k)
            if isinstance(v, str) and v.strip() and v not in vals:
                vals.append(v)
        return "\n".join(vals)

    profile["source_prompt"] = text_of(a["positive"])
    profile["negative"] = text_of(a["negative"])
    if not a["positive"]:
        notes.append("WARNING: could not find where the prompt goes.")
    return profile, notes


def basic_profile(name, ckpt, template="{prompt}", negative="",
                  width=640, height=480, steps=20, cfg=7.0,
                  sampler="euler", scheduler="normal"):
    """The original MantisKAI txt2img graph, for building a profile from scratch."""
    g = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 1, "steps": steps, "cfg": cfg, "sampler_name": sampler,
            "scheduler": scheduler, "denoise": 1.0, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        OUTPUT_NODE_ID: {"class_type": "SaveImage",
                         "inputs": {"filename_prefix": OUTPUT_PREFIX, "images": ["8", 0]}},
    }
    return {"name": name, "template": template, "negative": negative,
            "width": width, "height": height, "source_prompt": "", "workflow": g}


# ------------------------------------------------------------------
# Generation
# ------------------------------------------------------------------
def apply_lora(graph, info, lora_name, strength):
    root = info.get("model_root")
    if not lora_name or not root:
        return False
    clip = info.get("clip_root")
    if clip:
        node = {"class_type": "LoraLoader", "inputs": {
            "lora_name": lora_name, "strength_model": float(strength),
            "strength_clip": float(strength), "model": list(root), "clip": list(clip)}}
    else:
        node = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "lora_name": lora_name, "strength_model": float(strength), "model": list(root)}}
    _rewire(graph, root, [LORA_NODE_ID, 0])
    if clip:
        _rewire(graph, clip, [LORA_NODE_ID, 1])
    graph[LORA_NODE_ID] = node
    return True


def build_prompt_graph(profile, user_prompt, lora_name="", lora_strength=0.8):
    g = copy.deepcopy(profile["workflow"])
    info = analyze(dict(profile, workflow=g))
    if not info["output"]:
        raise ProfileError("Profile has no output node.")
    if not info["positive"]:
        raise ProfileError("Profile has no prompt node (set positive_nodes).")

    template = profile.get("template") or "{prompt}"
    text = template.replace("{prompt}", user_prompt) if "{prompt}" in template \
        else f"{template}, {user_prompt}" if template.strip() else user_prompt
    for nid, k in info["positive"]:
        g[nid]["inputs"][k] = text
    for nid, k in info["negative"]:
        g[nid]["inputs"][k] = profile.get("negative") or ""
    for nid, k in info["seeds"]:
        g[nid]["inputs"][k] = random.randint(1, 2**48)
    w = int(profile.get("width") or 640)
    h = int(profile.get("height") or 480)
    for nid in info["latents"]:
        g[nid]["inputs"].update(width=w, height=h, batch_size=1)
    # Model-side size hints (e.g. ModelSamplingFlux) must match the latent
    if info["latents"]:
        for n in g.values():
            ins = n["inputs"]
            if (is_link(ins.get("model")) and isinstance(ins.get("width"), int)
                    and isinstance(ins.get("height"), int)):
                ins["width"], ins["height"] = w, h
    apply_lora(g, info, lora_name, lora_strength)
    return g, info["output"]


def _comfy_error_text(body):
    try:
        data = json.loads(body)
    except Exception:
        return body[:300]
    parts = []
    err = data.get("error")
    if isinstance(err, dict):
        parts.append(err.get("message", ""))
        if err.get("details"):
            parts.append(err["details"])
    for nid, ne in (data.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            parts.append(f"node {nid} ({ne.get('class_type', '?')}): "
                         f"{e.get('message', '')} {e.get('details', '')}".strip())
    return " | ".join(p for p in parts if p) or body[:300]


def run_on_comfy(comfy_url, graph, output_id, timeout=140):
    """Queue the graph, wait for it, return the output PIL image."""
    data = json.dumps({"prompt": graph}).encode("utf-8")
    req = urllib.request.Request(f"http://{comfy_url}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ProfileError("ComfyUI rejected the workflow: "
                           + _comfy_error_text(e.read().decode("utf-8", "replace")))
    except Exception as e:
        raise ProfileError(f"Could not reach ComfyUI at {comfy_url}: {e}")
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        raise ProfileError("ComfyUI did not return a prompt id: " + _comfy_error_text(json.dumps(resp)))

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            hist = json.loads(urllib.request.urlopen(
                f"http://{comfy_url}/history/{prompt_id}", timeout=5).read().decode("utf-8"))
        except Exception:
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = [m[1].get("exception_message", "") for m in status.get("messages", [])
                    if isinstance(m, list) and len(m) == 2 and m[0] == "execution_error"]
            raise ProfileError("ComfyUI error: " + (" | ".join(msgs) or "execution failed"))
        images = entry.get("outputs", {}).get(output_id, {}).get("images", [])
        if images:
            info = images[0]
            q = urllib.parse.urlencode({"filename": info["filename"],
                                        "subfolder": info.get("subfolder", ""),
                                        "type": info.get("type", "output")})
            raw = urllib.request.urlopen(f"http://{comfy_url}/view?{q}", timeout=20).read()
            return Image.open(BytesIO(raw)).convert("RGB")
        if status.get("completed"):
            raise ProfileError("Workflow finished but produced no image on the output node.")
    raise ProfileError(f"Timed out after {timeout}s waiting for ComfyUI.")


# ------------------------------------------------------------------
# Store
# ------------------------------------------------------------------
def _slug(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")
    return s or "profile"


class ProfileStore:
    def __init__(self, directory=PROFILES_DIR):
        self.dir = directory
        self.lock = threading.RLock()
        self.profiles = []
        self.active = 0
        os.makedirs(self.dir, exist_ok=True)
        self.reload()

    def reload(self):
        with self.lock:
            active = self.active_name()
            items = []
            for fn in sorted(os.listdir(self.dir)):
                if not fn.lower().endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.dir, fn), "r", encoding="utf-8") as f:
                        p = json.load(f)
                    p["workflow"] = normalize_graph(p["workflow"])
                    p.setdefault("name", fn[:-5])
                    p["_file"] = fn
                    items.append(p)
                except Exception as e:
                    print(f"[PROFILE] skipped {fn}: {e}")
            items.sort(key=lambda p: p["name"].lower())
            self.profiles = items
            self.set_active(active)

    def names(self):
        with self.lock:
            return [p["name"] for p in self.profiles]

    def get(self, name):
        with self.lock:
            for p in self.profiles:
                if p["name"] == name:
                    return copy.deepcopy(p)
        return None

    def active_name(self):
        with self.lock:
            if 0 <= self.active < len(self.profiles):
                return self.profiles[self.active]["name"]
        return ""

    def set_active(self, name):
        with self.lock:
            for i, p in enumerate(self.profiles):
                if p["name"] == name:
                    self.active = i
                    return name
            self.active = 0 if self.profiles else -1
            return self.active_name()

    def cycle(self):
        with self.lock:
            if not self.profiles:
                return ""
            self.active = (self.active + 1) % len(self.profiles)
            return self.active_name()

    def save(self, profile, old_name=None):
        if not (profile.get("name") or "").strip():
            raise ProfileError("Profile needs a name.")
        with self.lock:
            active = self.active_name()
            if old_name and old_name != profile["name"]:
                if self.get(profile["name"]):
                    raise ProfileError(f"A profile named '{profile['name']}' already exists.")
                self.delete(old_name, keep_active=True)
                if active == old_name:
                    active = profile["name"]
            data = {k: v for k, v in profile.items() if not k.startswith("_")}
            fn = _slug(profile["name"]) + ".json"
            existing = next((p for p in self.profiles if p["name"] == profile["name"]), None)
            if existing:
                fn = existing["_file"]
            with open(os.path.join(self.dir, fn), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.reload()
            self.set_active(active or profile["name"])

    def delete(self, name, keep_active=False):
        with self.lock:
            p = next((p for p in self.profiles if p["name"] == name), None)
            if p:
                try:
                    os.remove(os.path.join(self.dir, p["_file"]))
                except OSError:
                    pass
            if not keep_active:
                active = self.active_name()
                self.reload()
                self.set_active(active)
