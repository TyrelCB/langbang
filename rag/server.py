"""LangBang RAG node HTTP API (uvicorn rag.server:app, :8900).

Read routes (search, media, stats, UI) are open to the LAN like the
standalone :8900 they replace. Write/export routes (/index, /replicate,
/api/rescan, /api/config) need loopback or `Authorization: Bearer <token>`
(data/keys/rag_token, generated on first start).
"""
import asyncio
import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import config
from .embedder import STBackend, RemoteBackend, Embedder, load_image
from .federation import Federation, auth_header, merge, vec_from_b64
from .indexer import Indexer, add_excluded
from .store import Store, name_terms

S = config.load()
os.makedirs(config.DATA_DIR, exist_ok=True)
THUMB_DIR = os.path.join(config.DATA_DIR, "thumbnails")
os.makedirs(THUMB_DIR, exist_ok=True)
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def log(msg):
    print(f"[rag {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _token():
    tok = config.read_key("rag_token")
    if not tok:
        os.makedirs(config.KEYS_DIR, exist_ok=True)
        tok = secrets.token_hex(24)
        p = os.path.join(config.KEYS_DIR, "rag_token")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
    return tok


TOKEN = _token()
store = Store(os.path.join(config.DATA_DIR, "rag.db"), S["node"])
_local = STBackend(S["model"], threads=S.get("threads"))


def _index_backend():
    name = S.get("index_backend", "local")
    if name == "local":
        return _local
    p = next((x for x in S.get("peers", []) if x["name"] == name), None)
    if not p:
        log(f"index_backend peer {name!r} not configured; using local")
        return _local
    return RemoteBackend(p["url"], auth_header(p).get("Authorization", ""))


emb = Embedder(_local, _index_backend(), log=log)
indexer = Indexer(store, emb, S, log=log)
fed = Federation(store, S, log=log)
jobs: "queue.Queue[tuple]" = queue.Queue()
app = FastAPI(title="langbang-rag")


def _worker():
    while True:
        kind, payload = jobs.get()
        try:
            if kind == "paths":
                n = indexer.index_paths(payload)
            else:
                n = indexer.index_docs(payload)
            log(f"indexed {kind}: {len(payload)} items -> {n} chunks")
        except Exception as e:  # noqa: BLE001
            log(f"index job failed: {type(e).__name__}: {e}")
        finally:
            jobs.task_done()


def _replicator():
    while True:
        time.sleep(30)
        if any(p.get("replicate") for p in fed.peers()):
            fed.replicate_all()
            time.sleep(max(30, float(S.get("replicate_interval", 600)) - 30))


HHMM = __import__("re").compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _sched_next(now=None):
    """Next scheduled start (epoch) for `rescan_at`, or None when off."""
    at = (S.get("rescan_at") or "").strip()
    if not HHMM.match(at):
        return None
    now = now or time.time()
    lt = time.localtime(now)
    h, m = map(int, at.split(":"))
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    today = time.strftime("%Y-%m-%d", lt)
    if t <= now and store.kv_get("rescan_auto_day") != today:
        return t  # due now (or a catch-up after downtime)
    return t if t > now else time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, m, 0, 0, 0, -1))


def _scheduler():
    """Nightly windowed rescan at node.json `rescan_at` — owned by the node
    (it used to be a systemd timer nobody could see from LangBang). Missed
    while down → runs on start if still inside `rescan_window` (the same
    catch-up systemd's Persistent= gave); outside it, waits for tomorrow."""
    while True:
        time.sleep(30)
        try:
            nxt = _sched_next()
            if nxt is None or nxt > time.time():
                continue
            store.kv_set("rescan_auto_day", time.strftime("%Y-%m-%d"))
            if not indexer.in_window():
                log("schedule: today's rescan window already closed — next run tomorrow")
                continue
            if indexer.state["running"]:
                continue
            log(f"schedule: nightly rescan (window {S.get('rescan_window') or 'any time'})")
            store.kv_set("rescan_auto_last", str(time.time()))
            indexer.rescan(windowed=True)
        except Exception as e:  # noqa: BLE001 - never kill the scheduler thread
            log(f"schedule: {type(e).__name__}: {e}")


@app.on_event("startup")
def _startup():
    threading.Thread(target=_worker, daemon=True, name="rag-index").start()
    threading.Thread(target=_scheduler, daemon=True, name="rag-schedule").start()
    threading.Thread(target=_replicator, daemon=True, name="rag-replicate").start()

    def warm():
        t = time.time()
        store.cache.refresh()
        log(f"index loaded: {store.cache.n} vectors in {time.time() - t:.1f}s")
        t = time.time()
        _local.embed_text(["warm"], "query")
        log(f"model ready in {time.time() - t:.1f}s")
    threading.Thread(target=warm, daemon=True, name="rag-warm").start()


def _require_auth(request: Request):
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    h = request.headers.get("authorization", "")
    if not secrets.compare_digest(h, "Bearer " + TOKEN):
        raise HTTPException(401, "bearer token required")


# ---- result shaping -------------------------------------------------------
def _shape_local(r):
    r["origin"] = "local" if r["node"] == S["node"] else r["node"]
    if r["category"] in ("image", "video", "audio") and r["path"].startswith("/"):
        if r["node"] == S["node"]:
            r["url"] = "/media?" + urllib.parse.urlencode({"path": r["path"]})
            if r["category"] != "audio":
                r["thumb"] = "/thumbnail?" + urllib.parse.urlencode(
                    {"path": r["path"], "position": r["position"]})
        else:  # replicated row: previews still come from the owning node
            p = fed.peer(r["node"])
            if p:
                r["url"] = "/proxy?" + urllib.parse.urlencode(
                    {"node": r["node"], "p": "/media?" + urllib.parse.urlencode({"path": r["path"]})})
                if r["category"] != "audio":
                    r["thumb"] = "/proxy?" + urllib.parse.urlencode(
                        {"node": r["node"], "p": "/thumbnail?" + urllib.parse.urlencode(
                            {"path": r["path"], "position": r["position"]})})
    r.pop("id", None)
    return r


def categories(category):
    """"image,audio" (or a list) -> ["image", "audio"]; ""/None -> None (all)."""
    if not category:
        return None
    vals = category.split(",") if isinstance(category, str) else list(category)
    vals = [c.strip() for c in vals if c and c.strip()]
    return vals or None


async def _run_search(qvec, *, k, category=None, source=None, path_prefix=None, scope="fleet",
                      query=None, image_bytes=None, image_ctype="image/jpeg", terms=None):
    category = categories(category)
    filters = dict(category=category, source=source or None, path_prefix=path_prefix or None)
    tl = terms if terms is not None else name_terms(query)
    local_task = anyio.to_thread.run_sync(lambda: store.search(qvec, k, terms=tl, **filters))
    if scope == "fleet" and fed.peers():
        local, remote = await asyncio.gather(local_task, fed.search(
            qvec=qvec, query=query, image_bytes=image_bytes, image_ctype=image_ctype,
            k=k, category=category, source=source))
    else:
        local, remote = await local_task, []
    if path_prefix:
        remote = [r for r in remote if (r.get("path") or "").startswith(path_prefix)]
    return merge([_shape_local(r) for r in local] + remote, k)


# ---- embeddings -----------------------------------------------------------
class EmbedRequest(BaseModel):
    model: str = "embeddinggemma-2"
    input: str | list[str]
    mode: str | None = None  # query | document; None = raw (legacy :8900 behaviour)
    dimensions: int | None = None
    prompt_name: str | None = None


@app.post("/v1/embeddings")
async def embeddings(req: EmbedRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts or len(texts) > 64:
        raise HTTPException(400, "1-64 inputs")

    def run():
        import numpy as np
        if req.mode:
            v = _local.embed_text(texts, req.mode)
        else:
            m = _local.model()
            with _local._lock:
                v = m.encode(texts, prompt_name=req.prompt_name, batch_size=16,
                             normalize_embeddings=True, convert_to_numpy=True)
        if req.dimensions:
            if req.dimensions not in (128, 256, 512, 768):
                raise HTTPException(400, "dimensions must be 128|256|512|768")
            v = v[:, :req.dimensions]
            v = v / np.linalg.norm(v, axis=1, keepdims=True)
        return v
    vecs = await anyio.to_thread.run_sync(run)
    return {"object": "list", "model": req.model,
            "data": [{"object": "embedding", "index": i, "embedding": [float(x) for x in e]}
                     for i, e in enumerate(vecs)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


@app.post("/api/embed/{modality}")
async def embed_media(modality: str, request: Request):
    data = await request.body()
    if not data or len(data) > 50_000_000:
        raise HTTPException(413, "empty or too large")
    import tempfile

    def run():
        if modality == "image":
            return [{"index": 0, "embedding": _local.embed_image(load_image(data)).tolist(), "position": None}]
        if modality == "audio":
            with tempfile.NamedTemporaryFile(suffix=".bin") as f:
                f.write(data); f.flush()
                return [{"index": i, "embedding": v.tolist(), "position": p}
                        for i, (v, p) in enumerate(_local.embed_audio(f.name))]
        raise HTTPException(404, "image|audio")
    return {"model": "embeddinggemma-2-multimodal", "modality": modality,
            "data": await anyio.to_thread.run_sync(run)}


# ---- search ---------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str | None = None
    vector: str | None = None  # base64 float32[768] from a federating peer
    image_path: str | None = None
    top_k: int = 10
    category: str | None = None
    source: str | None = None
    path_prefix: str | None = None
    scope: str | None = None  # local | fleet
    atlas: bool | None = None  # legacy :8900 UI flag: false == scope local
    model_fp: str | None = None
    terms: list[str] | None = None  # a federating peer's name_terms(query)


@app.post("/search")
async def search(req: SearchRequest):
    k = max(1, min(int(req.top_k), 100))
    scope = req.scope or ("local" if req.atlas is False else "fleet")
    image_bytes = None
    if req.vector:
        if req.model_fp and req.model_fp != S["model_fp"]:
            raise HTTPException(409, f"model_fp mismatch: mine {S['model_fp']}")
        q = vec_from_b64(req.vector)
        scope = "local"  # federated sub-query: never fan out again
    elif req.query:
        q = await anyio.to_thread.run_sync(emb.query_text, req.query)
    elif req.image_path:
        with open(req.image_path, "rb") as f:
            image_bytes = f.read()
        q = await anyio.to_thread.run_sync(lambda: emb.query_image(load_image(image_bytes)))
    else:
        raise HTTPException(400, "query, vector or image_path required")
    results = await _run_search(q, k=k, category=req.category, source=req.source,
                                path_prefix=req.path_prefix, scope=scope, query=req.query,
                                image_bytes=image_bytes, terms=req.terms[:8] if req.terms else None)
    return {"results": results, "indexed": store.cache.n, "node": S["node"], "model_fp": S["model_fp"],
            "peers": {n: {"ok": s.get("ok"), "ms": s.get("ms")} for n, s in fed.status.items()}
            if scope == "fleet" else {}}


@app.post("/search-image")
async def search_image(file: UploadFile, top_k: int = 24, category: str | None = None,
                       atlas: bool = True, scope: str | None = None, source: str | None = None):
    data = await file.read()
    try:
        img = load_image(data)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "invalid image") from None
    q = await anyio.to_thread.run_sync(emb.query_image, img)
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    results = await _run_search(q, k=max(1, min(top_k, 100)), category=category, source=source,
                                scope=scope or ("fleet" if atlas else "local"),
                                image_bytes=buf.getvalue())
    return {"results": results, "indexed": store.cache.n}


# ---- media ----------------------------------------------------------------
def _own_file(path):
    if not store.is_indexed(path) or not os.path.isfile(path):
        raise HTTPException(404, "not indexed")


@app.get("/media")
def media(path: str):
    _own_file(path)
    return FileResponse(path)


@app.get("/thumbnail")
def thumbnail(path: str, position: float = 0):
    _own_file(path)
    st = os.stat(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}:
        raise HTTPException(404, "no visual thumbnail")
    key = hashlib.sha1(f"{path}:{st.st_mtime_ns}:{st.st_size}:{position}".encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key + ".jpg")
    if not os.path.exists(out):
        pre = [] if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else ["-ss", f"{position:.2f}"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", *pre, "-i", path, "-frames:v", "1",
                        "-vf", "scale=480:-2", out], timeout=60, capture_output=True)
        if not os.path.exists(out):
            raise HTTPException(500, "thumb failed")
    return FileResponse(out)


@app.get("/proxy")
async def proxy(node: str, p: str, request: Request):
    """Stream a peer's /media or /thumbnail with the peer's credentials."""
    peer = fed.peer(node)
    if not peer:
        raise HTTPException(404, "unknown peer")
    if not (p.startswith("/media?") or p.startswith("/thumbnail?")):
        raise HTTPException(400, "only /media and /thumbnail are proxied")
    headers = auth_header(peer)
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=60)
    try:
        r = await client.send(client.build_request("GET", peer["url"].rstrip("/") + p, headers=headers),
                              stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        return Response(f"{type(e).__name__}: {e}", status_code=502)

    async def close():
        await r.aclose()
        await client.aclose()
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code,
                             media_type=r.headers.get("content-type", "application/octet-stream"),
                             headers={h: r.headers[h] for h in ("content-length", "content-range", "accept-ranges")
                                      if h in r.headers},
                             background=BackgroundTask(close))


class RemoveRequest(BaseModel):
    path: str
    delete_source: bool = False


@app.post("/api/remove")
def api_remove(req: RemoveRequest):
    """UI action: drop a file from the index (excluded from rescans) and
    optionally delete the source. Own-node files only."""
    if not store.is_indexed(req.path):
        return JSONResponse({"error": "not indexed here"}, status_code=404)
    add_excluded(req.path)
    n = store.delete_path(req.path)
    deleted = False
    if req.delete_source and os.path.isfile(req.path):
        os.remove(req.path)
        deleted = True
    return {"removed_chunks": n, "source_deleted": deleted}


# ---- indexing -------------------------------------------------------------
class IndexRequest(BaseModel):
    paths: list[str] = []
    docs: list[dict] = []
    wait: bool = False


@app.post("/index")
async def index(req: IndexRequest, request: Request):
    _require_auth(request)
    if req.paths:
        jobs.put(("paths", req.paths))
    if req.docs:
        jobs.put(("docs", req.docs))
    if req.wait:
        await anyio.to_thread.run_sync(jobs.join)
    return {"queued": len(req.paths) + len(req.docs), "pending": jobs.qsize()}


@app.delete("/index")
def unindex(request: Request, path: str | None = None, source: str | None = None,
            tid: str | None = None, path_prefix: str | None = None):
    _require_auth(request)
    if path:
        return {"removed": store.delete_path(path)}
    return {"removed": store.delete_where(source=source, tid=tid, path_prefix=path_prefix)}


@app.post("/api/rescan")
def rescan(request: Request, categories: str = "text,image,audio,video", limit: int = 0,
           windowed: bool = False):
    """windowed=1 (the nightly timer): index only inside node.json
    `rescan_window`, pausing at its end. Manual calls run until done."""
    _require_auth(request)
    if indexer.state["running"]:
        return {"started": False, "state": indexer.state}
    threading.Thread(target=indexer.rescan, args=(tuple(categories.split(",")), limit, windowed),
                     daemon=True, name="rag-rescan").start()
    return {"started": True}


@app.get("/api/rescan")
def rescan_state():
    return {**indexer.state, "pending_jobs": jobs.qsize()}


def _sched_view():
    last = store.kv_get("rescan_auto_last")
    return {"at": S.get("rescan_at") or "", "window": S.get("rescan_window") or "",
            "next": _sched_next(), "last": float(last) if last else None,
            "in_window": indexer.in_window(), "state": indexer.state}


class SchedIn(BaseModel):
    at: str | None = None      # "HH:MM", "" = off
    window: str | None = None  # "HH:MM-HH:MM", "" = any time


@app.get("/api/schedule")
def schedule_get():
    return _sched_view()


@app.put("/api/schedule")
def schedule_put(request: Request, body: SchedIn):
    _require_auth(request)
    patch = {}
    if body.at is not None:
        at = body.at.strip()
        if at and not HHMM.match(at):
            raise HTTPException(400, "at: HH:MM (24 h) or empty for off")
        patch["rescan_at"] = at
    if body.window is not None:
        w = body.window.strip()
        parts = w.split("-") if w else []
        if w and (len(parts) != 2 or not all(HHMM.match(x) for x in parts)):
            raise HTTPException(400, "window: HH:MM-HH:MM or empty for any time")
        patch["rescan_window"] = w
    if patch:
        config.save(patch)
        S.update(patch)
    return _sched_view()


@app.post("/api/rescan/stop")
def rescan_stop(request: Request):
    """Stops a running rescan after the files in flight (mtime-resumable)."""
    _require_auth(request)
    if indexer.state["running"]:
        indexer.state["stopped"] = "stopped by user"
    return {**indexer.state}


@app.get("/replicate/export")
def export(request: Request, since: int = 0, limit: int = 2000):
    _require_auth(request)
    rows = store.export(since, min(int(limit), 5000))
    return Response("".join(json.dumps(r) + "\n" for r in rows), media_type="application/x-ndjson")


# ---- status ---------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "node": S["node"], "model": S["model"], "model_fp": S["model_fp"],
            "vectors": store.cache.n, "model_loaded": _local._m is not None}


@app.get("/api/stats")
def api_stats():
    rows = store.stats()
    files = {}
    for r in rows:
        if r["node"] == S["node"]:
            files[r["category"]] = files.get(r["category"], 0) + r["files"]
    excl = os.path.join(config.DATA_DIR, "excluded-media.txt")
    return {"node": S["node"], "files": files, "vectors": sum(r["vectors"] for r in rows),
            "breakdown": rows,
            "excluded": sum(1 for _ in open(excl)) if os.path.exists(excl) else 0,
            "rescan": indexer.state, "pending_jobs": jobs.qsize()}


@app.get("/api/peers")
def api_peers():
    out = []
    for p in S.get("peers", []):
        out.append({"name": p["name"], "url": p["url"], "kind": p.get("kind", "langbang"),
                    "replicate": bool(p.get("replicate")), "disabled": bool(p.get("disabled")),
                    "status": fed.status.get(p["name"]),
                    "last_sync": float(store.kv_get(f"last_sync:{p['name']}", 0) or 0) or None,
                    "cursor": int(store.kv_get(f"cursor:{p['name']}", 0) or 0)})
    return {"node": S["node"], "peers": out}


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(WEB, "index.html")) as f:
        return f.read()
