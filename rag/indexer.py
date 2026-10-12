"""File indexing: walk + extract + chunk + embed into the Store.

Port of ~/projects/embeddinggemma-2/indexer2.py (same prune/skip lists, text
chunker, size caps, video keyframes) with sources, tombstoning re-indexes and
text embedded in DOCUMENT mode.
"""
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from .embedder import ffprobe_duration, load_image

CODE = {".py", ".js", ".ts", ".tsx", ".jsx", ".dart", ".java", ".kt", ".cs", ".cpp", ".cc", ".c",
        ".go", ".rs", ".rb", ".sh", ".html", ".css", ".scss", ".swift"}
DOC = {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".xml"}
IMG = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUD = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
VID = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
MAX_TEXT_BYTES = 2_000_000
MAX_IMG_BYTES = 40_000_000  # decoded + downscaled locally, so Atlas's 12 MB cap no longer binds
MAX_AUD_BYTES = 100_000_000
MAX_VID_BYTES = 200_000_000
MAX_CHUNKS = 40


def category_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE or ext in DOC or ext in {".pdf", ".docx"}:
        return "text"
    if ext in IMG:
        return "image"
    if ext in AUD:
        return "audio"
    if ext in VID:
        return "video"
    return None


def chunk_text(text):
    """~60-line blocks merged up to >=800 chars (indexer2 parity)."""
    lines = text.splitlines()
    out, cur = [], []
    for ln in lines:
        cur.append(ln)
        if len(cur) >= 60:
            out.append("\n".join(cur)); cur = []
    if cur:
        out.append("\n".join(cur))
    merged, buf = [], ""
    for c in out:
        buf = (buf + "\n" + c).strip() if buf else c
        if len(buf) >= 800:
            merged.append(buf); buf = ""
    if buf:
        merged.append(buf)
    return merged


def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import pymupdf
            with pymupdf.open(path) as doc:
                if doc.page_count > 200:
                    return None
                return "\n".join(page.get_text() for page in doc)
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(path).paragraphs)
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "r", errors="ignore") as f:
            return f.read()
    except Exception:  # noqa: BLE001 - unreadable files are skipped, not fatal
        return None


def excluded_set():
    p = os.path.join(config.DATA_DIR, "excluded-media.txt")
    try:
        with open(p) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()


def add_excluded(path):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(os.path.join(config.DATA_DIR, "excluded-media.txt"), "a") as f:
        f.write(path + "\n")


def walk(s):
    skip = [x.rstrip("/") for x in s["skip"]]
    prune = set(s["prune_dirs"])
    hidden_ok = [x.rstrip("/") for x in s.get("include_hidden", [])]
    excluded = excluded_set()

    def allowed_hidden(p):
        return any(p == h or p.startswith(h + "/") or h.startswith(p + "/") for h in hidden_ok)
    roots = list(dict.fromkeys(list(s["roots"]) + hidden_ok))
    seen = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if any(dirpath == x or dirpath.startswith(x + "/") for x in skip):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in prune
                           and (not d.startswith(".") or allowed_hidden(os.path.join(dirpath, d)))]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if fn.startswith(".") or p in excluded or p in seen:
                    continue
                seen.add(p)
                yield p


class Indexer:
    def __init__(self, store, embedder, settings, log=print):
        self.store = store
        self.emb = embedder
        self.s = settings
        self.log = log
        self.state = {"running": False, "done": 0, "todo": 0, "errors": 0, "started": None,
                      "finished": None, "current": None}

    # ---- one file -------------------------------------------------------
    def index_file(self, path, force=False):
        """(Re)index one path. Returns chunks written (0 = skipped/failed)."""
        path = os.path.abspath(os.path.expanduser(path))
        cat = category_for(path)
        if cat is None or not os.path.isfile(path) or path in excluded_set():
            return 0
        st = os.stat(path)
        if not force:
            known = self.store.path_mtime(path)
            if known is not None and abs(known - st.st_mtime) < 1:
                return 0
        src = config.source_for(path, self.s)
        kw = dict(category=cat, source=src, mtime=st.st_mtime, size=st.st_size,
                  model_fp=self.s["model_fp"])
        if cat == "text":
            text = extract_text(path)
            if not text or len(text.strip()) < 40:
                return 0
            chunks = chunk_text(text)[:MAX_CHUNKS]
            vecs = self.emb.docs(chunks)
            return self.store.put_file(path, [(i, c, v, 0) for i, (c, v) in enumerate(zip(chunks, vecs))], **kw)
        name = os.path.basename(path)
        caption = self._sidecar_caption(path)
        label = (name + " — " + caption) if caption else name
        if cat == "image":
            if st.st_size > MAX_IMG_BYTES:
                return 0
            v = self.emb.image(load_image(path))
            rows = [(0, label, v, 0.0)]
            if caption:  # prompt text from LangBang's JSON sidecar: searchable as text too
                rows.append((1, label, self.emb.docs([caption])[0], 0.0))
            return self.store.put_file(path, rows, **kw)
        if cat == "audio":
            if st.st_size > MAX_AUD_BYTES:
                return 0
            wins = self.emb.audio(path)
            return self.store.put_file(path, [(i, label, v, p) for i, (v, p) in enumerate(wins)], **kw) if wins else 0
        if cat == "video":
            if st.st_size > MAX_VID_BYTES:
                return 0
            rows = self._video_rows(path, label)
            return self.store.put_file(path, rows, **kw) if rows else 0
        return 0

    def _sidecar_caption(self, path):
        """comfy.py writes <image>.json with the prompt beside each output."""
        import json
        for side in (os.path.splitext(path)[0] + ".json", path + ".json"):
            try:
                with open(side) as f:
                    d = json.load(f)
                p = d.get("prompt") if isinstance(d, dict) else None
                if isinstance(p, str) and p.strip():
                    return p.strip()[:1000]
            except (OSError, ValueError):
                continue
        return ""

    def _video_rows(self, path, label):
        length = ffprobe_duration(path)
        if length <= 0:
            return []
        n = max(2, min(int(self.s.get("video_keyframes", 8)), int(length // 2) or 2))
        rows = []
        with tempfile.TemporaryDirectory(prefix="lbrag-vid-") as td:
            for i in range(n):
                pos = length * (i + 0.5) / n
                frame = os.path.join(td, f"f{i}.jpg")
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{pos:.2f}", "-i", path,
                                    "-frames:v", "1", "-vf", "scale='min(512,iw)':-2", frame],
                                   capture_output=True, timeout=120)
                if r.returncode or not os.path.exists(frame):
                    continue
                rows.append((i, label, self.emb.image(load_image(frame)), pos))
        return rows

    def index_paths(self, paths, force=True):
        n = 0
        for p in paths:
            try:
                n += self.index_file(p, force=force)
            except Exception as e:  # noqa: BLE001
                self.log(f"index {p}: {type(e).__name__}: {e}")
        return n

    def index_docs(self, docs):
        """Non-file documents (chat threads, notes): [{path, text, source, meta}].
        `path` is a stable synthetic id such as lb-thread:<tid>/<turn>."""
        n = 0
        for d in docs:
            chunks = chunk_text(d["text"])[:MAX_CHUNKS] or [d["text"]]
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                continue
            vecs = self.emb.docs(chunks)
            n += self.store.put_file(d["path"], [(i, c, v, 0) for i, (c, v) in enumerate(zip(chunks, vecs))],
                                     category="text", source=d.get("source", "note"),
                                     mtime=d.get("mtime") or time.time(), meta=d.get("meta"),
                                     model_fp=self.s["model_fp"])
        return n

    # ---- full rescan ----------------------------------------------------
    def in_window(self, now=None):
        """`rescan_window` "HH:MM-HH:MM" (local, may wrap midnight): bulk
        indexing runs on the index backend (Atlas), and while it does, Atlas's
        own searches slow from ~0.1 s to 3-39 s — so the backlog runs at night
        and resumes the next night (rescans are mtime-incremental)."""
        w = (self.s.get("rescan_window") or "").strip()
        if not w:
            return True
        a, b = ((int(x.split(":")[0]) * 60 + int(x.split(":")[1])) for x in w.split("-"))
        t = time.localtime(now)
        m = t.tm_hour * 60 + t.tm_min
        return a <= m < b if a <= b else (m >= a or m < b)

    def rescan(self, categories=("text", "image", "audio", "video"), limit=0, windowed=False):
        if self.state["running"]:
            return self.state
        self.state.update(running=True, done=0, errors=0, started=time.time(), finished=None,
                          stopped=None)
        try:
            known = self.store.known_mtimes()
            todo, live = [], set()
            for p in walk(self.s):
                cat = category_for(p)
                if cat is None or cat not in categories:
                    continue
                live.add(p)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if p in known and abs(known[p] - mt) < 1:
                    continue
                todo.append((p, cat))
            # files that vanished from disk: tombstone (synthetic docs have no path on disk)
            gone = [p for p in known if p.startswith("/") and p not in live
                    and any(p.startswith(r.rstrip("/") + "/") for r in self.s["roots"])
                    and not os.path.exists(p)]
            for p in gone:
                self.store.delete_path(p)
            # cheap + most-searched first; audio last: it is hours of Atlas GPU
            # time (8 s windows, serial ffmpeg) and slows the fleet's searches
            order = {"text": 0, "image": 1, "video": 2, "audio": 3}
            todo.sort(key=lambda t: order[t[1]])
            if limit:
                todo = todo[:limit]
            self.state["todo"] = len(todo)
            self.log(f"rescan: {len(todo)} to index, {len(gone)} removed")
            t0 = time.time()

            def work(item):
                if self.state.get("stopped") or (windowed and not self.in_window()):
                    if not self.state.get("stopped"):
                        self.state["stopped"] = f"outside rescan_window {self.s.get('rescan_window')}"
                        self.log(f"rescan: paused at {self.state['done']}/{len(todo)} "
                                 f"({self.state['stopped']}); resumes next run")
                    return
                self.state["current"] = item[0]
                try:
                    self.index_file(item[0], force=True)
                except Exception as e:  # noqa: BLE001
                    self.state["errors"] += 1
                    self.log(f"  skip {item[0]}: {type(e).__name__}: {e}")
                self.state["done"] += 1
                if self.state["done"] % 200 == 0:
                    rate = self.state["done"] / max(1e-6, time.time() - t0)
                    self.log(f"  {self.state['done']}/{len(todo)} files, {rate:.2f} f/s")
            with ThreadPoolExecutor(max_workers=int(self.s.get("workers", 4))) as ex:
                list(ex.map(work, todo))
        finally:
            self.state.update(running=False, finished=time.time(), current=None)
        return self.state
