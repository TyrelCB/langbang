"""LangBang server: FastAPI app with a llama.cpp-style web UI."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, mcp, runs, schedule, sfxgen, ttsjobs, voice

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class _NoStoreJSON(JSONResponse):
    """Same reason _NoCacheStatic exists for statics: without validators
    browsers apply heuristic caching to these GETs, and reopening a thread
    serves a stale /messages from cache (verified with headless chromium —
    a finished run's transcript was invisible until a hard reload)."""

    def init_headers(self, headers=None):
        super().init_headers(headers)
        self.headers["Cache-Control"] = "no-store"


app = FastAPI(title="LangBang", default_response_class=_NoStoreJSON)


@app.on_event("startup")
async def _startup():
    await agent.init()
    await schedule.init()
    schedule.start_loop()
    agent.start_sweeper()  # hourly: delete abandoned empty "New chat"s
    _prune_tts_cache()     # TTS disk cache is a replay aid, not an archive
    # Pocket TTS active → load model + voice off the event loop now, so the
    # first 🔊 after a restart doesn't wait ~7 s (no-op for other providers)
    asyncio.get_running_loop().run_in_executor(None, voice.prewarm, config.load())


@app.on_event("shutdown")
async def _shutdown():
    # aiosqlite threads are non-daemon: left open, interpreter shutdown joins
    # them forever (graceful stop hangs). The scripted restart SIGKILLs, which
    # is the only reason this never bit before.
    await agent.shutdown()


# ---- settings & health ----

def _ui_version() -> str:
    """Fingerprint of the served frontend (name + mtime + size of web/'s
    top-level files; a few stats per call). A tab remembers the value it
    booted with and offers a reload when it changes — statics are no-store,
    but a tab left open all day never re-requests them on its own."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(WEB_DIR)):
        p = os.path.join(WEB_DIR, name)
        if os.path.isfile(p):
            st = os.stat(p)
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()[:12]


@app.get("/api/health")
async def health():
    s = config.load()
    up = False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(s["base_url"].rstrip("/").removesuffix("/v1") + "/health")
            up = r.status_code < 500
    except Exception:
        pass
    return {
        "model": s["model"],
        "base_url": s["base_url"],
        "backend_up": up,
        "supports_vision": bool((s.get("capabilities") or {}).get("vision")),
        "ui_version": _ui_version(),
    }


@app.get("/api/settings")
async def get_settings():
    return config.load()


class SettingsIn(BaseModel):
    patch: dict


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    return config.save({**config.load(), **body.patch})


class McpTestIn(BaseModel):
    config: dict


@app.post("/api/mcp/test")
async def mcp_test(body: McpTestIn):
    # Always 200: ok/error are data, rendered inline by the CONFIG UI.
    # Same trust domain as run_bash — this can spawn stdio commands.
    return await mcp.probe_server(body.config)


# ---- soundboard (CONFIG → SOUNDBOARD) ----

@app.get("/api/sounds/slots")
async def sounds_slots():
    return sfxgen.slots_view()


class SfxRegenIn(BaseModel):
    slot: str
    seed: int | None = None


@app.post("/api/sounds/regen")
async def sfx_regen(body: SfxRegenIn):
    # Fire-and-poll: the all-media queue can sit on minutes; never hold the POST.
    return sfxgen.regen_start(body.slot.strip(), seed=body.seed)


@app.get("/api/sounds/regen/{slot}")
async def sfx_regen_status(slot: str):
    return sfxgen.regen_status(slot)


# ---- threads ----

@app.get("/api/threads")
async def threads():
    return await agent.list_threads()


class ThreadIn(BaseModel):
    title: str = "New chat"


@app.post("/api/threads")
async def new_thread(body: ThreadIn):
    return await agent.create_thread(body.title)


@app.delete("/api/threads/{tid}")
async def del_thread(tid: str):
    runs.cancel(tid)  # a detached run must not outlive its thread's row
    await agent.delete_thread(tid)
    return {"ok": True}


@app.post("/api/threads/{tid}/touch")
async def touch_thread(tid: str):
    """Resume-bump: opening a thread moves it to the top of the sidebar."""
    await agent.touch_thread(tid)
    return {"ok": True}


@app.get("/api/threads/{tid}/messages")
async def messages(tid: str):
    return await agent.history(tid)


@app.get("/api/threads/{tid}/todos")
async def thread_todos(tid: str):
    """Checkpointed todo-list state (the write_todos card's source of truth
    when a reopened/second browser can't reconstruct it from /messages).
    Unknown thread or read trouble -> null, same tone as /trajectory."""
    try:
        return {"todos": await agent.current_todos(tid)}
    except Exception:  # noqa: BLE001 - the card is a nicety, never an error page
        return {"todos": None}


@app.get("/api/threads/{tid}/trajectory")
async def trajectory(tid: str):
    """Run-tracker rows + totals for the Trajectory tab / stats bar. Unknown
    thread → empty/zeros rather than 404 (matches the app's endpoint tone)."""
    return await agent.trajectory(tid)


class ThreadRenameIn(BaseModel):
    title: str


@app.patch("/api/threads/{tid}")
async def rename_thread(tid: str, body: ThreadRenameIn):
    try:
        title = await agent.rename_thread(tid, body.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if title is None:
        raise HTTPException(404, "no such thread")
    return {"title": title}


@app.post("/api/threads/{tid}/retitle")
async def retitle_thread(tid: str):
    """⚡ LLM-generated title from a transcript slice (manual names are
    never special — re-running just overwrites, orig still anchors ⟲)."""
    try:
        title = await agent.auto_title(tid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # model down / empty output — say so, don't 500 silently
        raise HTTPException(502, f"title generation failed: {e}")
    if title is None:
        raise HTTPException(404, "no such thread")
    return {"title": title}


@app.post("/api/threads/{tid}/revert-title")
async def revert_title_thread(tid: str):
    title = await agent.revert_title(tid)
    if title is None:
        raise HTTPException(404, "no such thread")
    return {"title": title}


@app.post("/api/threads/{tid}/recap")
async def recap_thread(tid: str):
    """✦ cold-resume recap; the model call takes a few seconds (Spark)."""
    try:
        out = await agent.recap_thread(tid)
    except Exception as e:
        raise HTTPException(502, f"recap generation failed: {e}")
    if out is None:
        raise HTTPException(404, "no such thread")
    return out


# ---- scheduled tasks ----


class SchedIn(BaseModel):
    title: str
    prompt: str
    cron: str


class SchedPatch(BaseModel):
    title: str | None = None
    prompt: str | None = None
    cron: str | None = None
    enabled: bool | None = None


def _sched_view(row: dict) -> dict:
    return {**row, "human": schedule.humanize(row["cron"])}


@app.get("/api/schedules")
async def list_schedules():
    return [_sched_view(r) for r in await schedule.list_schedules()]


@app.get("/api/schedules/next")
async def schedule_next(cron: str = ""):
    """Live editor preview for a cron string."""
    return schedule.preview(cron.strip())


@app.post("/api/schedules")
async def new_schedule(body: SchedIn):
    try:
        row = await schedule.create(body.title, body.prompt, body.cron)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _sched_view(row)


@app.put("/api/schedules/{sid}")
async def put_schedule(sid: str, body: SchedPatch):
    try:
        row = await schedule.update(sid, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not row:
        raise HTTPException(404, "no such schedule")
    return _sched_view(row)


@app.delete("/api/schedules/{sid}")
async def del_schedule(sid: str):
    if not await schedule.delete(sid):
        raise HTTPException(404, "no such schedule")
    return {"ok": True}


@app.post("/api/schedules/{sid}/run")
async def run_schedule(sid: str):
    if not await schedule.run_now(sid):
        raise HTTPException(404, "no such schedule")
    return {"ok": True}


# ---- chat search ----

SEARCH_LIMIT = 50
PER_THREAD_LIMIT = 3  # short queries ("sm") hit dozens of messages per thread;
# without a cap one chatty thread eats every slot and the list is useless


@app.get("/api/search")
async def search(q: str = ""):
    """Case-insensitive substring scan over every thread's full transcript
    (compaction archives included), newest threads first, max PER_THREAD_LIMIT
    hits per thread so a broad query still surfaces breadth. `pos` is the
    message's index among rendered messages — exactly the chat DOM child
    index the UI renders, so a click can scroll straight to the hit."""
    q = q.strip().lower()
    if len(q) < 2:
        return []
    out = []
    for t in await agent.list_threads():
        hits = []
        pos = -1
        for m in await agent.history(t["id"]):
            if m["role"] == "system":
                continue
            if (
                m["role"] == "ai"
                and not str(m.get("content") or "").strip()
                and not m.get("tool_calls")
                and not m.get("thinking")
            ):
                continue  # renderHistory draws no node for these — keep pos aligned
            pos += 1  # counts rendered msgs only == client chat DOM child index
            text = await agent.search_text(m)
            i = text.lower().find(q)
            if i < 0:
                continue
            a = max(0, i - 40)
            b = min(len(text), i + len(q) + 120)
            snippet = ("… " if a else "") + " ".join(text[a:b].split()) + (" …" if b < len(text) else "")
            hits.append({"thread_id": t["id"], "title": t["title"], "pos": pos,
                         "role": m["role"], "snippet": snippet})
            if len(hits) >= PER_THREAD_LIMIT:
                break
        # title row only when the thread has no message hits — otherwise the
        # title already shows on every hit row and this is pure noise
        if not hits and q in t["title"].lower():
            hits.append({"thread_id": t["id"], "title": t["title"], "pos": -1,
                         "role": "title", "snippet": t["title"]})
        out.extend(hits)
        if len(out) >= SEARCH_LIMIT:
            return out[:SEARCH_LIMIT]
    return out


# ---- file attachments (non-image; the agent opens them from disk) ----

MAX_FILES = 6
MAX_FILE_BYTES = 20 * 1024 * 1024
UPLOADS_DIR = os.path.join(config.DATA_DIR, "uploads")


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Store attached files under data/uploads/<token>/ and return their
    read_file-able paths. Non-image attachments never travel inline to the
    model: the chat message carries the server-side path instead, and the
    agent opens the file with its file tools (or ffprobe etc. via run_bash)."""
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(400, f"1..{MAX_FILES} files per message")
    token = uuid.uuid4().hex[:12]
    out_dir = os.path.join(UPLOADS_DIR, token)
    out = []
    try:
        for f in files:
            # browser-supplied basename only; strip separators/traversal so the
            # write can never escape the (already-unique) token dir
            raw = (f.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            name = re.sub(r"[^\w.\- ]+", "_", raw).strip() or "file"
            dest = os.path.join(out_dir, name)
            os.makedirs(out_dir, exist_ok=True)
            size = 0
            with open(dest, "wb") as fh:
                while chunk := await f.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(
                            413,
                            f"{name} too large (max {MAX_FILE_BYTES // 1048576} MB)",
                        )
                    fh.write(chunk)
            out.append({"name": name, "path": dest, "size": size})
    except HTTPException:
        shutil.rmtree(out_dir, ignore_errors=True)  # leave no half-written batch
        raise
    return {"files": out}


@app.get("/api/media")
async def media(path: str):
    """Serve an asset by absolute path so answers can show their own
    artifacts (image/video/audio players inline in the chat). This is NOT a
    new capability: /api/shell already hands out any file on this box to the
    same LAN/tailnet audience (README ⚠ Security) — range/206 comes from
    Starlette's FileResponse, which is what makes <video> seeking work."""
    if not os.path.isabs(path):
        raise HTTPException(400, "path must be absolute")
    if not os.path.isfile(path):
        raise HTTPException(404, "not a file")
    return FileResponse(path, headers={"Cache-Control": "private, no-cache"})


# ---- file editor (✎ FILES) ----
# Same trust posture as /api/media and /api/shell: a LAN/tailnet-only personal
# tool whose agent already has an unsandboxed shell (README ⚠ Security).
# Writes are optimistic-concurrency guarded: the client sends back the
# `version` it loaded (sha256 of the bytes on disk), so an agent edit landing
# while the buffer is open is a 409 the UI resolves (reload / overwrite)
# instead of a silent clobber. Content hash, not mtime: two writes inside one
# timestamp tick share an mtime (the headless suite caught the miss), and an
# mtime_ns JSON number is past JS's 2^53 anyway.

EDIT_MAX_BYTES = 2_000_000
FS_LIST_MAX = 2000


def _version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _abs(path: str) -> str:
    p = os.path.expanduser(path or "~")
    if not os.path.isabs(p):
        raise HTTPException(400, "path must be absolute (or start with ~)")
    return os.path.normpath(p)


@app.get("/api/fs/list")
async def fs_list(path: str = "~"):
    p = _abs(path)
    if not os.path.isdir(p):
        raise HTTPException(404, "not a directory")
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()  # follows symlinks: a link to a dir browses
                    st = e.stat() if not is_dir else None
                except OSError:  # dangling symlink, dead fuse mount (spark-ee93)
                    is_dir, st = False, None
                entries.append({"name": e.name, "dir": is_dir,
                                "size": st.st_size if st else None})
    except PermissionError:
        raise HTTPException(403, "permission denied")
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": p, "parent": os.path.dirname(p) if p != "/" else None,
            "entries": entries[:FS_LIST_MAX], "truncated": len(entries) > FS_LIST_MAX}


FIND_BUDGET = 2.5      # seconds of rg streaming per search — ~ is huge (ComfyUI, SDKs)
FIND_MAX = 60
FIND_PRUNE = ("node_modules", ".git", ".venv", "venv", "__pycache__", ".cache",
              ".npm", ".cargo", "site-packages")


_SEP_RE = re.compile(r"[_\-\s]+")


def _norm(s: str) -> str:
    """Case- and separator-insensitive: `Jane_Doe_` meets
    `Jane Doe - Resume.pdf`."""
    return _SEP_RE.sub(" ", s.lower())


def _find_score(rel: str, terms: list[str]) -> int | None:
    """Every term must appear in the relative path (case/separator-
    insensitive); hits in the basename, at its start, and short paths rank
    first."""
    low = _norm(rel)
    if not all(t in low for t in terms):
        return None
    base = low.rsplit("/", 1)[-1]
    score = 0
    for t in terms:
        if base.startswith(t):
            score += 300
        elif t in base:
            score += 150
    return score - len(rel) - 20 * rel.count("/")


@app.get("/api/fs/find")
async def fs_find(root: str = "~", q: str = "", hidden: bool = False):
    """Recursive name search for the ✎ FILES box: streams `rg --files` under
    `root` (respects .gitignore, prunes FIND_PRUNE) and ranks files AND
    their parent dirs by the query terms. Time-boxed: whatever matched when
    FIND_BUDGET runs out is the answer (`partial: true`)."""
    base = _abs(root)
    terms = _norm(q).split()
    if not terms:
        return {"root": base, "results": [], "partial": False}
    if not os.path.isdir(base):
        raise HTTPException(404, "not a directory")
    cmd = ["rg", "--files", "--no-messages", "--max-depth", "12"]
    if hidden:
        cmd.append("--hidden")
    for d in FIND_PRUNE:
        cmd += ["-g", f"!**/{d}/**"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=base, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    hits: dict[str, tuple[int, bool]] = {}
    seen_dirs: set[str] = set()
    deadline = time.monotonic() + FIND_BUDGET
    partial = False
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                partial = True
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
            except asyncio.TimeoutError:
                partial = True
                break
            if not line:
                break
            rel = line.decode("utf-8", "replace").rstrip("\n")
            if rel.startswith("./"):
                rel = rel[2:]
            sc = _find_score(rel, terms)
            if sc is not None:
                hits[rel] = (sc, False)
            # folders only surface through the files under them
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                sc = _find_score(d, terms)
                if sc is not None:
                    hits[d] = (sc + 50, True)  # a matching folder beats files inside it
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    top = sorted(hits.items(), key=lambda kv: -kv[1][0])[:FIND_MAX]
    return {
        "root": base,
        "partial": partial,
        "results": [{"rel": rel, "path": os.path.join(base, rel), "dir": is_dir}
                    for rel, (_, is_dir) in top],
    }


@app.get("/api/fs/read")
async def fs_read(path: str):
    p = _abs(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "not a file")
    st = os.stat(p)
    if st.st_size > EDIT_MAX_BYTES:
        raise HTTPException(413, f"file too large to edit ({st.st_size:,} bytes; max {EDIT_MAX_BYTES:,})")
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except PermissionError:
        raise HTTPException(403, "permission denied")
    if b"\0" in raw[:8192]:
        raise HTTPException(415, "binary file — not editable as text")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "not UTF-8 text — not editable here")
    # <textarea> normalizes CRLF to LF; remember it so a save round-trips
    eol = "crlf" if "\r\n" in text else "lf"
    return {"path": p, "content": text.replace("\r\n", "\n") if eol == "crlf" else text,
            "eol": eol, "version": _version(raw), "size": st.st_size,
            "writable": os.access(p, os.W_OK)}


class FsWriteIn(BaseModel):
    path: str
    content: str
    version: str | None = None  # None = creating a new file
    eol: str = "lf"
    force: bool = False


@app.put("/api/fs/write")
async def fs_write(body: FsWriteIn):
    shown = _abs(body.path)
    p = os.path.realpath(shown)  # write THROUGH symlinks, not over them
    exists = os.path.exists(p)
    if exists and not os.path.isfile(p):
        raise HTTPException(400, "not a regular file")
    if not os.path.isdir(os.path.dirname(p)):
        raise HTTPException(400, "parent directory does not exist")
    if not body.force:
        if body.version is None and exists:
            raise HTTPException(409, "file already exists on disk")
        if body.version is not None:
            if not exists:
                raise HTTPException(409, "file was deleted on disk")
            with open(p, "rb") as fh:
                if _version(fh.read()) != body.version:
                    raise HTTPException(409, "file changed on disk since it was opened")
    text = body.content.replace("\n", "\r\n") if body.eol == "crlf" else body.content
    data = text.encode("utf-8")
    if len(data) > EDIT_MAX_BYTES:
        raise HTTPException(413, "content too large")
    # atomic replace (a crash mid-save never leaves half a file), keeping mode
    tmp = f"{p}.lb-save-{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        if exists:
            shutil.copymode(p, tmp)
        os.replace(tmp, p)
    except PermissionError:
        raise HTTPException(403, "permission denied")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"path": shown, "version": _version(data), "size": len(data)}


# ---- chat (SSE stream) ----

MAX_IMAGES = 4
MAX_IMG_B64 = 7_000_000  # ~5 MB decoded; images re-prefill every turn on Spark
DATA_IMG_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$")


class ChatIn(BaseModel):
    thread_id: str
    text: str = ""
    images: list[str] = []
    plan: bool = False  # plan mode: read-only investigation → exit_plan_mode


@app.post("/api/chat")
async def chat(body: ChatIn):
    s = config.load()
    text = body.text.strip()
    if not text and not body.images:
        raise HTTPException(400, "empty message")
    if body.images:
        if not (s.get("capabilities") or {}).get("vision"):
            raise HTTPException(
                400, "model does not support vision (enable it in CONFIG)"
            )
        if len(body.images) > MAX_IMAGES:
            raise HTTPException(400, f"max {MAX_IMAGES} images per message")
        for du in body.images:
            m = DATA_IMG_RE.match(du)
            if not m:
                raise HTTPException(400, "images must be base64 data URLs (png/jpeg/webp/gif)")
            if len(m.group(2)) > MAX_IMG_B64:
                raise HTTPException(400, "image too large (max ~5 MB)")

    # A message typed while the agent waits at a gate IS the answer: resume
    # the interrupt with it (free text) rather than stacking a new turn on a
    # paused graph. Plan gates read it as revision notes — approval is the
    # card's explicit button only.
    resume = None
    if not runs.get(body.thread_id) or runs.get(body.thread_id).done:
        gates = await agent.pending_gates(body.thread_id)
        if gates:
            if body.images:
                raise HTTPException(409, "the agent is waiting for an answer — reply in text first")
            resume = {"id": gates[0]["id"], "value": {"text": text}}

    # the run is server-owned (runs.Hub): this response is just a follower.
    # Closing it (phone sleep, refresh, app switch) no longer cancels the run
    # — GET /api/threads/{tid}/stream?since=N reattaches and replays.
    try:
        hub = runs.start(
            body.thread_id,
            agent.run_chat(body.thread_id, text, s, images=body.images,
                           resume=resume, plan=body.plan),
        )
    except runs.Busy as e:
        raise HTTPException(409, str(e))
    except runs.TooMany as e:
        raise HTTPException(429, str(e))
    return _sse(runs.follow(hub, 0))


def _sse(gen):
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/threads/{tid}/gate")
async def thread_gate(tid: str):
    """Pending human gates (ask_user questions / a plan awaiting review) —
    how a reopened tab or second device finds a paused thread."""
    try:
        return {"gates": await agent.pending_gates(tid)}
    except Exception:  # noqa: BLE001 - unknown thread / read trouble: nothing pending
        return {"gates": []}


class ResumeIn(BaseModel):
    id: str
    value: dict
    plan: bool = False


def _answer_text(v: dict) -> str:
    """Trajectory/title text for a gate answer (the tool result carries the
    real, model-facing formatting)."""
    if "approved" in v:
        return ("✓ PLAN APPROVED" if v.get("approved") else "↺ PLAN REVISION") + (
            f": {v.get('feedback')}" if v.get("feedback") else "")
    if v.get("text"):
        return str(v["text"])
    return " | ".join(
        "; ".join([*(a.get("selected") or []), *([a["text"]] if a.get("text") else [])])
        for a in v.get("answers") or [] if isinstance(a, dict)) or "[answer]"


@app.post("/api/threads/{tid}/resume")
async def thread_resume(tid: str, body: ResumeIn):
    """Answer a pending gate: resumes the paused graph (Command(resume=…))
    as a normal hub-backed run. Approving a plan ends plan mode for the rest
    of that run — the agent goes straight on to implement."""
    gates = await agent.pending_gates(tid)
    g = next((g for g in gates if g["id"] == body.id), None)
    if g is None:
        raise HTTPException(409, "that question is no longer pending")
    kind = (g["value"] or {}).get("kind") if isinstance(g["value"], dict) else None
    plan = body.plan and not (kind == "plan" and body.value.get("approved"))
    s = config.load()
    try:
        hub = runs.start(tid, agent.run_chat(
            tid, _answer_text(body.value), s,
            resume={"id": body.id, "value": body.value}, plan=plan))
    except runs.Busy as e:
        raise HTTPException(409, str(e))
    except runs.TooMany as e:
        raise HTTPException(429, str(e))
    return _sse(runs.follow(hub, 0))


@app.get("/api/threads/{tid}/stream")
async def thread_stream(tid: str, since: int = 0):
    """Reattach to a server-owned run (page reload, phone wake, second tab).
    `since` = events already seen; falling behind the ring buffer sends one
    {"type":"gap"} note and resumes at the window head."""
    hub = runs.get(tid)
    if not hub:
        raise HTTPException(404, "no run for this thread")
    return _sse(runs.follow(hub, max(0, since)))


@app.post("/api/threads/{tid}/cancel")
async def thread_cancel(tid: str):
    """STOP, the real way: cancels the detached task (a dropped SSE stream is
    no longer a cancellation signal)."""
    if not runs.cancel(tid):
        raise HTTPException(404, "not running")
    return {"ok": True}


@app.get("/api/runs")
async def list_runs():
    """Active runs (interactive AND scheduler-fired) → sidebar indicators."""
    return runs.active_map()


# ---- shell mode (`!cmd` from the chat box) ----


class ShellIn(BaseModel):
    thread_id: str
    command: str


@app.post("/api/shell")
async def shell(body: ShellIn):
    """User-initiated shell run: executes on this box in the same unsandboxed
    shell as run_bash (see README ⚠ Security) and lands in the thread context
    for the agent's NEXT turn — but never calls the model itself."""
    cmd = body.command.strip()
    if not cmd:
        raise HTTPException(400, "empty command")
    # hub-backed like chat: `! sleep 300` survives a dead phone (the client
    # seals its card from the shell_result event whenever it arrives)
    try:
        hub = runs.start(body.thread_id, runs.shell_events(body.thread_id, cmd))
    except runs.Busy as e:
        raise HTTPException(409, str(e))
    except runs.TooMany as e:
        raise HTTPException(429, str(e))
    return _sse(runs.follow(hub, 0))


# ---- voice (TTS / STT) ----
# Both providers block on network (gTTS/recognize_google hit Google over the
# internet; gcloud uses gRPC) — run_in_threadpool keeps them off the event
# loop so an in-flight /api/chat SSE stream never starves behind one.

MAX_STT_BYTES = 10_000_000  # ~5 min of 16 kHz mono PCM16


class TTSIn(BaseModel):
    text: str
    # CONFIG ▶ preview: speak with this Pocket voice (and language) without
    # saving settings — forces the pocket provider for this one request
    voice: str | None = None
    language: str | None = None


TTS_CACHE_DIR = os.path.join(config.DATA_DIR, "tts")
TTS_CACHE_TTL = 30 * 86400  # clips are replay artifacts, not an archive


def _tts_key(clean: str, s: dict) -> str:
    """Cache identity = speakable text + the voice knobs that change audio.
    gTTS lang/tld and gcloud voice name all bite; the key FILE path rides
    along harmlessly (synthesis doesn't depend on where it lives)."""
    v = s.get("voice") or {}
    tag = "|".join(
        str(v.get(k) or "")
        for k in ("tts_provider", "tts_lang", "tts_tld",
                  "gcloud_key_file", "gcloud_tts_lang", "gcloud_tts_voice")
        # pocket knobs only for pocket: adding them unconditionally would
        # re-key (orphan) every existing gTTS/gcloud cache entry
        + (("pocket_voice", "pocket_language") if v.get("tts_provider") == "pocket" else ()))
    return hashlib.sha256(f"{clean}|{tag}".encode()).hexdigest()


def _prune_tts_cache() -> None:
    try:
        cutoff = time.time() - TTS_CACHE_TTL
        for name in os.listdir(TTS_CACHE_DIR):
            p = os.path.join(TTS_CACHE_DIR, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except FileNotFoundError:
        pass


@app.post("/api/tts")
async def tts(body: TTSIn):
    if len(body.text) > voice.MAX_TTS_CHARS:
        raise HTTPException(400, f"text too long to speak (max {voice.MAX_TTS_CHARS} chars)")
    clean = voice.speakable(body.text)
    if not clean:
        raise HTTPException(400, "nothing speakable in text")
    s = config.load()
    if body.voice:
        s = {**s, "voice": {**(s.get("voice") or {}), "tts_provider": "pocket",
                            "pocket_voice": body.voice,
                            **({"pocket_language": body.language} if body.language else {})}}
    key = _tts_key(clean, s)
    v = s.get("voice") or {}
    if (v.get("tts_provider") or "gtts") == "pocket" and shutil.which("ffmpeg"):
        # Streaming provider: answer with a URL the <audio> element plays
        # progressively (speech starts in ~0.1 s). A cached clip is the same
        # URL served as a seekable file. Load errors (model/voice) surface
        # here as a readable 502 rather than a silent empty stream.
        clip = f"/api/tts/clip/{key}.mp3"
        if os.path.isfile(os.path.join(TTS_CACHE_DIR, key + ".mp3")):
            return {"stream": clip, "cache": "hit"}
        try:
            await run_in_threadpool(voice.pocket_ready, v)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        ttsjobs.start(key, clean, v, os.path.join(TTS_CACHE_DIR, key + ".mp3"))
        return {"stream": clip, "cache": "miss"}
    path = next((os.path.join(TTS_CACHE_DIR, key + ext)
                 for ext in (".mp3", ".wav")
                 if os.path.isfile(os.path.join(TTS_CACHE_DIR, key + ext))), None)
    if path:
        with open(path, "rb") as fh:
            audio = fh.read()
        mime = "audio/mpeg" if path.endswith(".mp3") else "audio/wav"
        hit = "hit"
    else:
        try:
            audio, mime = await run_in_threadpool(voice.synthesize, clean, s)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # RuntimeError + anything: readable 502 for the UI
            detail = str(e) or f"{type(e).__name__}"
            raise HTTPException(502, detail)
        hit = "miss"
        # store the take so replays never touch the (rate-limited) provider
        ext = ".wav" if mime == "audio/wav" else ".mp3"
        try:
            os.makedirs(TTS_CACHE_DIR, exist_ok=True)
            tmp = os.path.join(TTS_CACHE_DIR, f"{key}{ext}.tmp")
            with open(tmp, "wb") as fh:
                fh.write(audio)
            os.replace(tmp, os.path.join(TTS_CACHE_DIR, f"{key}{ext}"))
        except OSError:
            pass  # cache is best-effort; the audio still reaches the UI
    return Response(content=audio, media_type=mime,
                    headers={"Cache-Control": "no-store", "X-TTS-Cache": hit})


# ---- Pocket voice library (CONFIG → VOICE dropdown, cloning) ----

MAX_CLONE_BYTES = 25_000_000


@app.get("/api/voices")
async def voices_list():
    return {"presets": list(voice.POCKET_VOICES), "languages": list(voice.POCKET_LANGS),
            "saved": voice.saved_voices(), "cloning_ready": voice.cloning_ready()}


@app.post("/api/voices")
async def voices_clone(name: str = Form(...), language: str = Form("english"),
                       consent: bool = Form(False), file: UploadFile = File(...)):
    """Clone a voice from an uploaded clip (first 30 s used) and save it to
    the library. Any audio/video ffmpeg reads is accepted (phone voice memo,
    webm, m4a…); it's normalized to 24 kHz mono WAV first. The consent box
    is required — kyutai's terms forbid cloning a voice without permission."""
    if not consent:
        raise HTTPException(400, "confirm this is your voice or that you have the speaker's permission")
    if not shutil.which("ffmpeg"):
        raise HTTPException(500, "ffmpeg is required to read the uploaded audio")
    work = os.path.join(config.DATA_DIR, "uploads", "clone-" + uuid.uuid4().hex[:12])
    os.makedirs(work, exist_ok=True)
    try:
        src = os.path.join(work, "src")
        size = 0
        with open(src, "wb") as fh:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_CLONE_BYTES:
                    raise HTTPException(413, "audio too large (max 25 MB — 30 s is all it uses)")
                fh.write(chunk)
        wav = os.path.join(work, "voice.wav")
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", "30",
            "-vn", "-ac", "1", "-ar", "24000", wav,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0 or not os.path.isfile(wav):
            raise HTTPException(400, "couldn't read that audio: " + err.decode(errors="replace").strip()[-200:])
        try:
            meta = await run_in_threadpool(voice.clone_voice, name.strip(), wav, language,
                                           file.filename or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return meta
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the source clip is never kept


@app.delete("/api/voices/{name}")
async def voices_delete(name: str):
    if not voice.delete_voice(name):
        raise HTTPException(404, "no such saved voice")
    s = config.load()
    v = s.get("voice") or {}
    if v.get("pocket_voice") == name:  # deleting the active voice → back to a preset
        config.save({**s, "voice": {**v, "pocket_voice": "alba"}})
    return {"ok": True}


_CLIP_RE = re.compile(r"^[0-9a-f]{64}\.mp3$")


@app.get("/api/tts/clip/{name}")
async def tts_clip(name: str):
    """A Pocket TTS clip: the cached file (Range/206 → seekable) once
    finished, else the live stream of its synthesis job (progressive MP3)."""
    if not _CLIP_RE.match(name):
        raise HTTPException(404, "no such clip")
    path = os.path.join(TTS_CACHE_DIR, name)
    if os.path.isfile(path):
        return FileResponse(path, media_type="audio/mpeg",
                            headers={"Cache-Control": "no-store", "X-TTS-Cache": "hit"})
    job = ttsjobs.get(name[:-4])
    if job is None:
        raise HTTPException(404, "clip not cached and not being synthesized")
    return StreamingResponse(job.follow(), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store", "X-TTS-Cache": "stream"})


@app.post("/api/stt")
async def stt(request: Request):
    """Raw 16 kHz mono PCM16 body (optionally RIFF-wrapped) -> {"text": ...}.
    No multipart lib needed; mic UI is deferred (browser needs a secure
    context) but this endpoint is what it will call."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty audio body")
    if len(body) > MAX_STT_BYTES:
        raise HTTPException(413, "audio too large (max 10 MB)")
    s = config.load()
    try:
        text = await run_in_threadpool(voice.transcribe, body, s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e) or type(e).__name__)
    return {"text": text}


# ---- web UI ----

@app.get("/")
async def index():
    # no-store like the statics: a cached index.html pins stale asset refs
    return FileResponse(os.path.join(WEB_DIR, "index.html"),
                        headers={"Cache-Control": "no-store"})


class _NoCacheStatic(StaticFiles):
    """LAN dev tool — always revalidate so frontend edits show up on reload
    instead of being served from a stale heuristic browser cache."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store"
        return resp


app.mount("/static", _NoCacheStatic(directory=WEB_DIR), name="static")
