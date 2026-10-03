"""Image generation + editing: Qwen-Image 2.1 (uncensored Q4_K_M GGUF) on the
ComfyUI instance on spark-ee93.

One builder merges the user's two known-good workflows (text-to-image and
"edit-3-images"): they share the model stack and differ only in where the
sampler's latent comes from —
  * no input images  → EmptyLatentImage(width, height)        (t2i)
  * 1–10 input images → TextEncodeQwenImage21's own latent    (edit / compose)
The text encoder takes the references as images.image_1..N; prompts refer
to them as image_1, image_2, … in the order given.

Flow: upload references (/upload/image) → POST /prompt → poll /history →
download outputs (/view) into the local output folder, so answers can cite
the local path (inline media in chat) and FILES can open it. Measured on
spark-ee93 (GB10): 1024², 25 steps ≈ 40 s including a cold model load.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid

import httpx

from . import config

MAX_REFS = 10      # the encoder autogrows to 16; 10 keeps prompts sane
POLL_S = 1.5
TIMEOUT_S = 1200   # ComfyUI may be queued behind the user's video jobs

DEFAULTS = {
    "comfy_url": "http://spark-ee93:8188",
    "unet": "qwen-image-2.1-UC-Q4_K_M.gguf",
    "clip": "qwen3vl_8b_int8_convrot.safetensors",
    "vae": "qwen_image_2.1_vae_bf16.safetensors",
    "steps": 25,
    "out_dir": "~/Pictures/langbang",
}


def settings() -> dict:
    return {**DEFAULTS, **(config.load().get("image_gen") or {})}


def build_workflow(prompt: str, ref_names: list[str], *, negative: str = "",
                   width: int = 1024, height: int = 1024, seed: int, steps: int,
                   prefix: str, s: dict) -> dict:
    """API-format graph. Node ids/params mirror the user's two workflows."""
    wf: dict = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": s["unet"]}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": s["clip"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": s["vae"]}},
        "5": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"prompt": prompt, "negative_prompt": negative, "resolution": 0,
                         "clip": ["2", 0], "vae": ["3", 0]}},
        "6": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 1, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1, "model": ["1", 0],
                         "positive": ["5", 0], "negative": ["5", 1]}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["7", 0]}},
    }
    for i, name in enumerate(ref_names, 1):
        nid = str(100 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["5"]["inputs"][f"images.image_{i}"] = [nid, 0]
    if ref_names:
        wf["6"]["inputs"]["latent_image"] = ["5", 2]      # edit: latent from the references
    else:
        wf["4"] = {"class_type": "EmptyLatentImage",
                   "inputs": {"width": width, "height": height, "batch_size": 1}}
        wf["6"]["inputs"]["latent_image"] = ["4", 0]      # t2i: blank canvas
    return wf


def _snap(v: int) -> int:
    return max(256, min(2048, int(v) // 32 * 32))


async def generate(prompt: str, input_images: list[str] | None = None, *,
                   width: int = 1024, height: int = 1024, seed: int = -1,
                   steps: int | None = None, negative: str = "", progress=None) -> dict:
    """Run one generation; returns {paths, seed, seconds, mode}. Raises
    ValueError (bad input) / RuntimeError (ComfyUI trouble) with readable text."""
    s = settings()
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("empty prompt")
    refs = [os.path.expanduser(p) for p in (input_images or [])]
    if len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} input images")
    for p in refs:
        if not os.path.isfile(p):
            raise ValueError(f"input image not found: {p}")
    seed = random.randint(1, 2**48) if seed is None or seed < 0 else int(seed)
    steps = max(1, min(100, int(steps or s["steps"])))
    base = s["comfy_url"].rstrip("/")
    out_dir = os.path.expanduser(s["out_dir"])
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    async with httpx.AsyncClient(timeout=60) as c:
        names = []
        for p in refs:  # references go to ComfyUI's input/langbang/
            with open(p, "rb") as fh:
                r = await c.post(base + "/upload/image",
                                 files={"image": (f"{uuid.uuid4().hex[:8]}_{os.path.basename(p)}", fh)},
                                 data={"subfolder": "langbang", "type": "input", "overwrite": "true"})
            if r.status_code != 200:
                raise RuntimeError(f"ComfyUI upload failed: HTTP {r.status_code} {r.text[:200]}")
            j = r.json()
            names.append(f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"])
        wf = build_workflow(prompt, names, negative=negative, width=_snap(width),
                            height=_snap(height), seed=seed, steps=steps,
                            prefix=f"langbang/{'edit' if names else 't2i'}_{stamp}", s=s)
        try:
            r = await c.post(base + "/prompt", json={"prompt": wf, "client_id": "langbang"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"ComfyUI unreachable at {base} ({type(e).__name__})") from e
        if r.status_code != 200:
            try:
                err = r.json()
                detail = err.get("error", {}).get("message") or ""
                ne = err.get("node_errors") or {}
                detail += " " + "; ".join(
                    f"{v.get('class_type')}: {e2.get('message')} {e2.get('details', '')}"
                    for v in ne.values() for e2 in v.get("errors", []))
            except ValueError:
                detail = r.text[:300]
            raise RuntimeError(f"ComfyUI rejected the workflow: {detail.strip()[:500]}")
        pid = r.json()["prompt_id"]
        outs = None
        while time.time() - t0 < TIMEOUT_S:
            h = (await c.get(f"{base}/history/{pid}")).json()
            if pid in h:
                st = h[pid].get("status") or {}
                if st.get("status_str") == "error":
                    msgs = [m[1].get("exception_message", "") for m in st.get("messages", [])
                            if m and m[0] == "execution_error"]
                    raise RuntimeError("ComfyUI run failed: " + (msgs[0] if msgs else "unknown error")[:400])
                outs = h[pid].get("outputs") or {}
                break
            if progress:
                await progress(time.time() - t0)
            await asyncio.sleep(POLL_S)
        if outs is None:
            raise RuntimeError(f"timed out after {TIMEOUT_S // 60} min (ComfyUI busy?)")
        paths = []
        for img in (outs.get("8") or {}).get("images", []):
            r = await c.get(base + "/view", params={"filename": img["filename"],
                                                    "subfolder": img.get("subfolder", ""),
                                                    "type": img.get("type", "output")})
            if r.status_code != 200:
                raise RuntimeError(f"couldn't download {img['filename']}: HTTP {r.status_code}")
            local = os.path.join(out_dir, re.sub(r"[^\w.-]", "_", img["filename"]))
            with open(local, "wb") as fh:
                fh.write(r.content)
            paths.append(local)
    if not paths:
        raise RuntimeError("ComfyUI finished but produced no image")
    meta = {"prompt": prompt, "inputs": refs, "seed": seed, "steps": steps,
            "mode": "edit" if refs else "t2i", "seconds": round(time.time() - t0, 1)}
    for p in paths:  # sidecar: how this image was made (re-run / tweak later)
        with open(os.path.splitext(p)[0] + ".json", "w") as fh:
            json.dump(meta, fh, indent=1)
    return {"paths": paths, **meta}
