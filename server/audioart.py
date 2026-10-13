"""✎ FILES audio viewer: metadata, cover art, and images made FROM a track.

`info()` returns what's known about a track without guessing silently: the
container tags (ffprobe), the caption/lyrics/bpm a ComfyUI song carries in
its `prompt` tag (the executed graph), a LangBang/ComfyUI JSON sidecar, and
an "Artist - Title" split of the file name, which is labeled as a guess.

`imagine()` makes 1-4 images for a track. The image model can't hear, so
the audio reaches it as measured features. librosa reads the tempo, key,
energy and brightness, both for the whole track and for each of the N
equal sections. An LLM then writes one prompt per section from those
features plus the metadata, so N images follow the song's arc (a quiet
intro, then the loud chorus). With `use_cover`, embedded cover art is passed
to Qwen-Image as image_1, so the results keep its look. The results are
recorded in data/audio-art.json, so the viewer shows them as the
visualizer's backdrop while the matching section plays.

Sources are either an absolute local path or a RAG node route (`/media?…`,
`/proxy?…`): fleet hits stream through the node, so ffprobe/ffmpeg read
that URL directly.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

import numpy as np

from . import config

ART_INDEX = os.path.join(config.DATA_DIR, "audio-art.json")
MAX_ANALYZE_S = 600     # decode at most 10 min for features
SR = 22050
_lock = threading.Lock()

KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl-Schmuckler key profiles
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def resolve(path: str = "", rag_p: str = "") -> str:
    """-> what ffmpeg reads: a local file, or the node URL of a fleet hit."""
    if rag_p:
        if not rag_p.startswith(("/media?", "/proxy?")):
            raise ValueError("rag source must be a node /media or /proxy route")
        from . import rag
        return rag.settings()["url"].rstrip("/") + rag_p
    if not path or not os.path.isabs(path):
        raise ValueError("path must be absolute")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return path


def _probe(src: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src],
                       capture_output=True, timeout=30)
    if r.returncode:
        raise RuntimeError("ffprobe: " + r.stderr.decode("utf-8", "replace")[:200])
    return json.loads(r.stdout or b"{}")


def _graph_fields(prompt_tag: str) -> dict:
    """ComfyUI SaveAudio writes the executed graph into the `prompt` tag; the
    text encoder's inputs ARE the song's description (ACE-Step: tags =
    caption, lyrics, bpm, keyscale)."""
    try:
        g = json.loads(prompt_tag)
    except ValueError:
        return {}
    out = {}
    for node in (g.values() if isinstance(g, dict) else []):
        if not isinstance(node, dict):
            continue
        cls = str(node.get("class_type", ""))
        if not re.search(r"TextEncode|AceStep|Prompt", cls):
            continue
        for k, v in (node.get("inputs") or {}).items():
            if isinstance(v, list):  # a link to another node
                continue
            key = {"tags": "caption", "text": "caption", "prompt": "caption"}.get(k, k)
            if key in ("caption", "lyrics") and isinstance(v, str) and v.strip():
                out.setdefault(key, v.strip())
            elif key in ("bpm", "keyscale", "language", "duration") and v not in (None, ""):
                out.setdefault(key, v)
    return out


def _sidecar(path: str) -> dict:
    for side in (os.path.splitext(path)[0] + ".json", path + ".json"):
        try:
            with open(side) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return {k: d[k] for k in ("title", "artist", "prompt", "caption", "tags", "lyrics", "bpm")
                    if isinstance(d.get(k), (str, int, float)) and str(d[k]).strip()}
    return {}


def guess_from_name(name: str) -> dict:
    """"Jon Pardi - Dirt On My Boots Lyric Video.aac" -> artist/title."""
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r"[\[(](official|lyric|lyrics|audio|video|hd|hq|music video)[^\])]*[\])]", "", stem, flags=re.I)
    stem = re.sub(r"\b(official\s+)?(lyric|lyrics|music)?\s*(video|audio)\b", "", stem, flags=re.I)
    stem = re.sub(r"[_]+", " ", stem).strip(" -")
    if re.fullmatch(r"ComfyUI_\d+_?|\d+", stem.replace(" ", "_")):
        return {}
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return {"artist": parts[0].strip(), "title": parts[1].strip()}
    return {"title": stem} if stem else {}


def info(path: str = "", rag_p: str = "") -> dict:
    src = resolve(path, rag_p)
    d = _probe(src)
    fmt = d.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    aud = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    for k, v in (aud.get("tags") or {}).items():
        tags.setdefault(k.lower(), v)
    # an audio file's only picture stream is its cover (attached_pic, or a
    # bare mjpeg/png stream from older taggers)
    cover = any(x.get("codec_type") == "video" for x in d.get("streams", []))
    graph = _graph_fields(tags.pop("prompt")) if str(tags.get("prompt", "")).lstrip().startswith("{") else {}
    tags.pop("workflow", None)  # the UI graph: huge, says nothing about the song
    side = _sidecar(path) if path else {}
    name = path or urllib.parse.unquote(urllib.parse.unquote(rag_p)).rsplit("path=", 1)[-1]
    shown = {k: v for k, v in tags.items()
             if k in ("title", "artist", "album", "album_artist", "date", "year", "genre", "track", "composer",
                      "comment", "description", "lyrics", "unsyncedlyrics", "copyright", "publisher") and str(v).strip()}
    lyrics = shown.pop("lyrics", "") or shown.pop("unsyncedlyrics", "") or graph.get("lyrics") or side.get("lyrics", "")
    out = {
        "src": "node" if rag_p else "local",
        "duration": float(fmt.get("duration") or aud.get("duration") or 0) or None,
        "codec": aud.get("codec_name"), "sample_rate": int(aud.get("sample_rate") or 0) or None,
        "channels": aud.get("channels"), "bit_rate": int(fmt.get("bit_rate") or aud.get("bit_rate") or 0) or None,
        "format": fmt.get("format_long_name"), "size": int(fmt.get("size") or 0) or None,
        "tags": shown, "graph": {k: v for k, v in graph.items() if k != "lyrics"},
        "sidecar": {k: v for k, v in side.items() if k != "lyrics"},
        "lyrics": lyrics[:6000] if isinstance(lyrics, str) else "",
        "cover": cover,
        "guess": {} if shown.get("title") else guess_from_name(name),
        "art": art_for(path or rag_p),
    }
    return out


def cover(path: str = "", rag_p: str = "") -> bytes:
    src = resolve(path, rag_p)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-an", "-map", "0:v:0", "-frames:v", "1",
                        "-vf", "scale='min(1024,iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                       capture_output=True, timeout=60)
    if r.returncode or not r.stdout:
        raise FileNotFoundError("no cover art")
    return r.stdout


# ---- features ---------------------------------------------------------------
def _decode(src: str) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-t", str(MAX_ANALYZE_S), "-vn", "-ac", "1",
                        "-ar", str(SR), "-f", "f32le", "pipe:1"], capture_output=True, timeout=240)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[:200])
    y = np.frombuffer(r.stdout, dtype=np.float32)
    if y.size < SR:
        raise RuntimeError("less than a second of audio decoded")
    return y


def _key(chroma: np.ndarray) -> str:
    c = chroma.mean(axis=1)
    best = (-2.0, "")
    for i in range(12):
        for prof, mode in ((_MAJOR, "major"), (_MINOR, "minor")):
            r = np.corrcoef(c, np.roll(prof, i))[0, 1]
            if r > best[0]:
                best = (r, f"{KEYS[i]} {mode}")
    return best[1]


def _words(rms_rel: float, bright: float, onset: float) -> list[str]:
    w = []
    w.append("very quiet" if rms_rel < 0.35 else "soft" if rms_rel < 0.7 else
             "full" if rms_rel < 1.15 else "loud, intense")
    w.append("dark, warm tone" if bright < 1500 else "balanced tone" if bright < 2800 else "bright, crisp tone")
    w.append("sparse" if onset < 1.5 else "steady" if onset < 3.5 else "busy, driving")
    return w


def features(src: str, sections: int = 1) -> dict:
    """Whole-track tempo/key/energy/brightness + the same per section."""
    import librosa
    y = _decode(src)
    dur = y.size / SR
    tempo, _ = librosa.beat.beat_track(y=y, sr=SR)
    tempo = float(np.atleast_1d(tempo)[0])
    rms = librosa.feature.rms(y=y)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    chroma = librosa.feature.chroma_stft(y=y, sr=SR)
    onsets = librosa.onset.onset_detect(y=y, sr=SR, units="time")
    mean_rms = float(rms.mean()) or 1e-6
    out = {"duration": round(dur, 1), "tempo_bpm": round(tempo), "key": _key(chroma),
           "brightness_hz": round(float(cent.mean())),
           "dynamics": "wide (quiet and loud parts)" if np.percentile(rms, 90) > 3 * max(np.percentile(rms, 20), 1e-6)
           else "even", "sections": []}
    n = max(1, int(sections))
    hop = dur / n
    for i in range(n):
        a, b = i * hop, (i + 1) * hop
        fa, fb = int(a / dur * rms.size), max(int(a / dur * rms.size) + 1, int(b / dur * rms.size))
        seg_rms = float(rms[fa:fb].mean()) / mean_rms
        seg_c = float(cent[fa:fb].mean())
        seg_on = float(((onsets >= a) & (onsets < b)).sum() / max(b - a, 1e-6))
        out["sections"].append({"start": round(a, 1), "end": round(b, 1), "energy_vs_track": round(seg_rms, 2),
                                "brightness_hz": round(seg_c), "onsets_per_s": round(seg_on, 1),
                                "feel": _words(seg_rms, seg_c, seg_on)})
    return out


# ---- art index ----------------------------------------------------------------
def _load_index() -> dict:
    try:
        with open(ART_INDEX) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def art_for(audio: str) -> list[dict]:
    """Images made for `audio` that still exist, oldest first."""
    return [a for a in _load_index().get(audio, []) if os.path.isfile(a.get("path", ""))]


def record_art(audio: str, items: list[dict]) -> None:
    with _lock:
        idx = _load_index()
        idx.setdefault(audio, []).extend(items)
        tmp = ART_INDEX + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f, indent=1)
        os.replace(tmp, ART_INDEX)


# ---- prompts ------------------------------------------------------------------
SYSTEM = """You write prompts for an image model (Qwen-Image) that will illustrate a piece of audio. The image model cannot hear: you get the track's metadata and its measured audio features, and you translate the sound into pictures.

Rules:
- Return ONLY a JSON array of strings, one prompt per requested image, in order. No prose, no code fences.
- Each prompt is 40-90 words: subject, setting, lighting, color palette, composition, and art style.
- Image i illustrates section i of the track. Follow the song's arc: match each section's energy and tone (quiet sections → stiller, softer scenes; loud, busy sections → motion and intensity). Keep one consistent visual world, style and palette family across all images so they read as a set.
- Use what the metadata says (title, artist, genre, caption, lyrics) for the subject matter. If the title or lyrics name things (places, objects, people), show them. If you know the song, stay true to its theme. If the metadata is thin, lean on tempo, key, energy and tone (minor key and dark tone → moodier; major key and bright tone → warmer, sunnier).
- No text, letters, logos or watermarks in the images. Don't depict real, identifiable people: show characters, not celebrities.
- If a reference cover image is provided as image_1, say "in the style and palette of image_1" and keep its look, but make a new scene."""


def _meta_block(meta: dict) -> str:
    lines = []
    g = meta.get("guess") or {}
    for k, v in (meta.get("tags") or {}).items():
        lines.append(f"{k}: {str(v)[:300]}")
    for k, v in g.items():
        lines.append(f"{k} (from the file name): {v}")
    for k, v in {**(meta.get("graph") or {}), **(meta.get("sidecar") or {})}.items():
        lines.append(f"{k}: {str(v)[:1200]}")
    if meta.get("lyrics"):
        lines.append("lyrics (excerpt):\n" + meta["lyrics"][:2500])
    return "\n".join(lines) or "(no metadata)"


def _parse_prompts(text: str, n: int) -> list[str]:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    m = re.search(r"\[.*\]", text, flags=re.S)
    try:
        arr = json.loads(m.group(0)) if m else []
    except ValueError:
        arr = []
    arr = [str(x).strip() for x in arr if str(x).strip()]
    if not arr:
        raise RuntimeError("the model returned no prompts: " + text[:200])
    while len(arr) < n:
        arr.append(arr[-1])
    return arr[:n]


async def write_prompts(meta: dict, feats: dict, n: int, direction: str, has_cover: bool, s: dict) -> list[str]:
    from langchain_core.messages import HumanMessage, SystemMessage

    from . import agent
    feats_txt = json.dumps({k: v for k, v in feats.items() if k != "sections"})
    secs = "\n".join(f"section {i + 1} ({x['start']:.0f}-{x['end']:.0f} s): energy {x['energy_vs_track']}x the "
                     f"track average, {', '.join(x['feel'])}" for i, x in enumerate(feats["sections"]))
    user = (f"Metadata:\n{_meta_block(meta)}\n\nMeasured audio features (whole track): {feats_txt}\n\n"
            f"Sections:\n{secs}\n\n"
            + (f"The user's direction (follow it): {direction.strip()}\n\n" if direction.strip() else "")
            + ("A reference cover image is attached as image_1.\n\n" if has_cover else "")
            + f"Write {n} prompt{'s' if n > 1 else ''} as a JSON array.")
    model = agent.SGlangChatOpenAI(model=s["model"], base_url=s["base_url"], api_key=s["api_key"],
                                   temperature=0.8, max_tokens=2500, **agent.no_think(s))
    msg = await model.ainvoke([SystemMessage(content=SYSTEM), HumanMessage(content=user)])
    content = msg.content if isinstance(msg.content, str) else "".join(
        b.get("text", "") for b in msg.content if isinstance(b, dict))
    return _parse_prompts(content, n)


async def imagine(path: str, rag_p: str, n: int, direction: str, use_cover: bool, emit):
    """Async generator body: `emit(dict)` progress events; returns the items."""
    from . import comfy
    s = config.load()
    n = max(1, min(4, int(n)))
    src = resolve(path, rag_p)
    await emit({"stage": "analyzing", "text": "reading metadata + measuring tempo, key and energy …"})
    meta = await asyncio.to_thread(info, path, rag_p)
    feats = await asyncio.to_thread(features, src, n)
    await emit({"stage": "features", "features": {k: v for k, v in feats.items() if k != "sections"},
                "sections": feats["sections"]})
    refs = []
    if use_cover and meta.get("cover"):
        img = await asyncio.to_thread(cover, path, rag_p)
        out_dir = os.path.expanduser(comfy.settings()["out_dir"])
        os.makedirs(out_dir, exist_ok=True)
        ref = os.path.join(out_dir, f"cover_{time.strftime('%Y%m%d-%H%M%S')}.jpg")
        with open(ref, "wb") as f:
            f.write(img)
        refs = [ref]
    await emit({"stage": "prompts", "text": "writing prompts …"})
    prompts = await write_prompts(meta, feats, n, direction, bool(refs), s)
    await emit({"stage": "prompts", "prompts": prompts})
    items = []
    label = path or rag_p
    for i, p in enumerate(prompts):
        sec = feats["sections"][i]
        t0 = time.time()

        async def prog(el, i=i):
            await emit({"stage": "image", "i": i, "n": n, "seconds": round(el)})
        await emit({"stage": "image", "i": i, "n": n, "seconds": 0})
        r = await comfy.generate(p, refs, width=1024, height=1024, progress=prog,
                                 extra_meta={"audio": label, "section": [sec["start"], sec["end"]]})
        item = {"path": r["paths"][0], "prompt": p, "start": sec["start"], "end": sec["end"],
                "seconds": round(time.time() - t0, 1), "at": time.time()}
        items.append(item)
        await emit({"stage": "done_one", "i": i, "item": item})
    record_art(label, items)
    return items
