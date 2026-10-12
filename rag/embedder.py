"""Embedding backends. Every backend maps text/images/audio into the same
EmbeddingGemma 2 768d space; vectors are unit-normalized float32.

STBackend      sentence-transformers in-process (CPU here; Atlas runs the same
               weights in bf16 on the GB10 — parity measured at cos 1.0000 for
               text/query/image, 2026-10-11)
RemoteBackend  a peer's embed API (Atlas /v1/embeddings + /api/embed/*): bulk
               indexing on a CPU-only box offloads to a GPU peer
LlamaCppBackend  placeholder for the unsloth GGUF via llama-server
               (--embeddings --pooling mean); text-only until llama.cpp's
               mmproj embedding path is proven

Text MUST carry the task prefix for its role. Sending a document with the query
prefix (Atlas's /v1/embeddings defaults mode="query") silently degrades
retrieval — the pre-LangBang rag.db was built exactly that way.
"""
import base64
import contextlib
import io
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import numpy as np

QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: none | text: "
DIM = 768
AUDIO_WINDOW = 8.0  # seconds; Atlas's audio_positions
MAX_IMAGE_SIDE = 2048
MAX_TEXT_CHARS = 32_000  # ~8192 tokens: the model truncates there anyway


def prefixed(texts, mode):
    if mode not in ("query", "document"):
        raise ValueError(f"mode must be query|document, not {mode!r}")
    p = QUERY_PREFIX if mode == "query" else DOC_PREFIX
    return [p + t for t in texts]


def audio_positions(length):
    """Consecutive 8 s windows covering the whole clip; the last one overlaps
    backwards so it is a full window (Atlas media_index.audio_positions)."""
    if length <= 0:
        return []
    pos = [i * AUDIO_WINDOW for i in range(math.ceil(length / AUDIO_WINDOW))]
    pos[-1] = min(pos[-1], max(0.0, length - AUDIO_WINDOW))
    return pos


def ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        v = float(out)
        return v if math.isfinite(v) else 0.0
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def load_image(data_or_path):
    """PIL RGB image, exif-transposed, longest side <= 2048 (Atlas parity)."""
    from PIL import Image, ImageOps
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        return im.convert("RGB")


WAV16K_BYTES_PER_S = 32_000  # 16 kHz mono s16le
MIN_CLIP_S = 0.5


class PriorityLock:
    """The model lock, with queries first: indexing workers (4 threads that
    re-take the lock back to back) must not starve a waiting search — a plain
    Lock isn't fair and searches measured 17-20 s during a rescan."""

    def __init__(self):
        self._c = threading.Condition()
        self._busy = False
        self._urgent = 0

    def acquire(self, urgent):
        with self._c:
            if urgent:
                self._urgent += 1
            try:
                while self._busy or (not urgent and self._urgent):
                    self._c.wait()
            finally:
                if urgent:
                    self._urgent -= 1
            self._busy = True

    def release(self):
        with self._c:
            self._busy = False
            self._c.notify_all()


class QuietGate:
    """While a search is waiting on a peer (federation holds the gate), index
    calls to that peer hold off: Atlas runs one model, so a search queued
    behind 4 workers' embed batches took 8 s+ (2026-10-12). Capped so a
    stuck search can't stall indexing for long."""

    def __init__(self):
        self._c = threading.Condition()
        self._n = 0

    @contextlib.contextmanager
    def hold(self):
        with self._c:
            self._n += 1
        try:
            yield
        finally:
            with self._c:
                self._n -= 1
                self._c.notify_all()

    def wait(self, cap=20.0):
        with self._c:
            self._c.wait_for(lambda: self._n == 0, timeout=cap)


quiet = QuietGate()


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1, n)


class STBackend:
    name = "st"

    def __init__(self, model="google/embeddinggemma-2", threads=None):
        self.model_id = model
        self.threads = threads
        self._m = None
        self._load_lock = threading.Lock()
        self._lock = PriorityLock()
        self._urgent = threading.local()

    def urgent(self):
        """Context: encodes on this thread jump the queue (search queries)."""
        backend = self

        class _U:
            def __enter__(self):
                backend._urgent.on = True

            def __exit__(self, *a):
                backend._urgent.on = False
        return _U()

    def model(self):
        if self._m is None:
            with self._load_lock:
                if self._m is None:
                    import torch
                    from sentence_transformers import SentenceTransformer
                    if self.threads:
                        torch.set_num_threads(int(self.threads))
                    self._m = SentenceTransformer(self.model_id, device="cpu")
        return self._m

    def _enc(self, inputs, batch_size=8):
        m = self.model()
        self._lock.acquire(getattr(self._urgent, "on", False))  # one encode at a time per model
        try:
            return _norm(m.encode(inputs, batch_size=batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True))
        finally:
            self._lock.release()

    def embed_text(self, texts, mode):
        texts = prefixed([(t or "")[:MAX_TEXT_CHARS] for t in texts], mode)
        if getattr(self._urgent, "on", False) or len(texts) <= 4:
            return self._enc(texts, batch_size=16)
        # bulk (index fallback): small encodes, so a waiting search gets the
        # model between slices instead of after a minutes-long batch
        return np.concatenate([self._enc(texts[i:i + 4], batch_size=4) for i in range(0, len(texts), 4)])

    def embed_image(self, image):
        """image: PIL image (see load_image) -> (768,)"""
        return self._enc([{"image": image}], batch_size=1)[0]

    def embed_audio(self, path):
        """Whole file in 8 s windows -> [(vec, start_seconds)]."""
        length = ffprobe_duration(path)
        out = []
        with tempfile.TemporaryDirectory(prefix="lbrag-aud-") as td:
            for i, pos in enumerate(audio_positions(length)):
                clip = os.path.join(td, f"c{i}.wav")
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{pos}", "-i", str(path),
                                    "-t", f"{AUDIO_WINDOW}", "-ac", "1", "-ar", "16000", clip],
                                   capture_output=True, timeout=120)
                if r.returncode or not os.path.exists(clip):
                    continue
                if os.path.getsize(clip) < MIN_CLIP_S * WAV16K_BYTES_PER_S:
                    break  # past the real end: ffprobe over-reports some AAC/VBR durations
                out.append((self._enc([{"audio": clip}], batch_size=1)[0], float(pos)))
        return out


class RemoteBackend:
    """Atlas-compatible embed API. Auth: Basic (Atlas) or Bearer (LangBang node)."""
    name = "remote"

    def __init__(self, url, auth_header="", timeout=600):
        self.url = url.rstrip("/")
        self.auth = auth_header
        self.timeout = timeout
        # One AUDIO call in flight: the peer's own searches share its model,
        # and 4 index workers each posting audio made Atlas searches take
        # 15-30 s (2026-10-12). Text/image calls are short; they stay parallel.
        self._audio_one = threading.Lock()

    def _post(self, path, body, ctype):
        h = {"Content-Type": ctype}
        if self.auth:
            h["Authorization"] = self.auth
        req = urllib.request.Request(self.url + path, data=body, method="POST", headers=h)
        quiet.wait()
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def embed_text(self, texts, mode):
        # Atlas 400s on any blank input and on >32 inputs / >131072 chars per
        # call. The model reads at most 8192 tokens, so 32k chars loses nothing.
        texts = [(t or "").strip()[:MAX_TEXT_CHARS] or "(empty)" for t in texts]
        out, batch, size = [], [], 0

        def flush():
            # The API adds the prefix itself from `mode` — never pre-prefix here.
            d = self._post("/v1/embeddings", json.dumps({"input": batch, "mode": mode}).encode(),
                           "application/json")
            out.extend(r["embedding"] for r in sorted(d["data"], key=lambda r: r["index"]))
        for t in texts:
            if batch and (len(batch) == 32 or size + len(t) > 120_000):
                flush()
                batch, size = [], 0
            batch.append(t)
            size += len(t)
        if batch:
            flush()
        return _norm(out)

    def embed_image(self, image):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=95)
        d = self._post("/api/embed/image", buf.getvalue(), "image/jpeg")
        return _norm(d["data"][0]["embedding"])

    def embed_audio(self, path):
        """Converted to 16 kHz mono WAV and sent in 2-minute segments (short
        calls let the peer's searches interleave; Atlas caps uploads at 25 MB);
        window positions are re-offset per segment (a multiple of 8 s)."""
        length = ffprobe_duration(path)
        out = []
        seg = 120.0
        with tempfile.TemporaryDirectory(prefix="lbrag-aud-") as td:
            start = 0.0
            while start < length:
                wav = os.path.join(td, "seg.wav")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start}", "-i", str(path),
                                "-t", f"{seg}", "-ac", "1", "-ar", "16000", wav],
                               capture_output=True, timeout=300, check=True)
                with open(wav, "rb") as f:
                    data = f.read()
                got = max(0, len(data) - 44) / WAV16K_BYTES_PER_S  # decoded seconds
                if got < MIN_CLIP_S:
                    break  # ffprobe over-reported the length (AAC/VBR): the audio ended
                with self._audio_one:
                    d = self._post("/api/embed/audio", data, "audio/wav")
                out += [(_norm(w["embedding"]), start + float(w.get("position") or 0)) for w in d["data"]]
                if got < seg - 1:
                    break
                start += seg
        return out


class LlamaCppBackend:
    """Future: unsloth/embeddinggemma-2-GGUF behind llama-server. Text only."""
    name = "llamacpp"

    def __init__(self, url, model="embeddinggemma2"):
        self.url = url.rstrip("/")
        self.model_id = model

    def embed_text(self, texts, mode):
        body = json.dumps({"model": self.model_id, "input": prefixed(texts, mode),
                           "encoding_format": "float"}).encode()
        req = urllib.request.Request(self.url + "/v1/embeddings", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        return _norm([x["embedding"] for x in sorted(d["data"], key=lambda r: r["index"])])

    def embed_image(self, image):
        raise NotImplementedError("llama.cpp backend is text-only")

    def embed_audio(self, path):
        raise NotImplementedError("llama.cpp backend is text-only")


def basic_auth(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class Embedder:
    """Routes calls: queries go to `query` (local, low latency), bulk index
    work to `index` (may be a GPU peer), falling back to `query` if the remote
    side fails — a dead Atlas must slow indexing down, not stop it."""

    def __init__(self, query, index=None, log=print):
        self.query = query
        self.index = index or query
        self.log = log

    RETRY_DELAYS = (3, 10, 30)

    def _with_fallback(self, fn, *a):
        """Retry the index backend before falling back: a peer restart lasts
        seconds, while this box's CPU embeds ~10x slower than Atlas's GPU
        (a 4 s Atlas restart once sent 17 files to the CPU, 2026-10-12)."""
        if self.index is self.query:
            return getattr(self.query, fn)(*a)
        err = None
        for delay in (0,) + tuple(self.RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                return getattr(self.index, fn)(*a)
            except urllib.error.HTTPError as e:
                err = e
                if e.code < 500:
                    break  # the peer rejects this input: retrying won't help
            except Exception as e:  # noqa: BLE001
                err = e
        self.log(f"index backend {self.index.name} failed ({type(err).__name__}: {err}); local fallback")
        return getattr(self.query, fn)(*a)

    def _urgent(self):
        u = getattr(self.query, "urgent", None)
        return u() if u else contextlib.nullcontext()

    def query_text(self, text):
        with self._urgent():
            return self.query.embed_text([text], "query")[0]

    def query_image(self, image):
        with self._urgent():
            return self.query.embed_image(image)

    def docs(self, texts):
        return self._with_fallback("embed_text", texts, "document")

    def image(self, image):
        return self._with_fallback("embed_image", image)

    def audio(self, path):
        return self._with_fallback("embed_audio", path)
