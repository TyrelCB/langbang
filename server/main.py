"""LangBang server: FastAPI app with a llama.cpp-style web UI."""
import json
import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, voice

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

app = FastAPI(title="LangBang")


@app.on_event("startup")
async def _startup():
    await agent.init()


# ---- settings & health ----

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
    }


@app.get("/api/settings")
async def get_settings():
    return config.load()


class SettingsIn(BaseModel):
    patch: dict


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    return config.save({**config.load(), **body.patch})


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


@app.get("/api/threads/{tid}/trajectory")
async def trajectory(tid: str):
    """Run-tracker rows + totals for the Trajectory tab / stats bar. Unknown
    thread → empty/zeros rather than 404 (matches the app's endpoint tone)."""
    return await agent.trajectory(tid)


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


# ---- chat (SSE stream) ----

MAX_IMAGES = 4
MAX_IMG_B64 = 7_000_000  # ~5 MB decoded; images re-prefill every turn on Spark
DATA_IMG_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$")


class ChatIn(BaseModel):
    thread_id: str
    text: str = ""
    images: list[str] = []


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

    async def gen():
        async for ev in agent.run_chat(body.thread_id, text, s, images=body.images):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    try:
        return await agent.run_user_shell(body.thread_id, cmd)
    except Exception as e:  # noqa: BLE001 - readable 502 like the voice routes
        raise HTTPException(502, str(e) or type(e).__name__)


# ---- voice (TTS / STT) ----
# Both providers block on network (gTTS/recognize_google hit Google over the
# internet; gcloud uses gRPC) — run_in_threadpool keeps them off the event
# loop so an in-flight /api/chat SSE stream never starves behind one.

MAX_STT_BYTES = 10_000_000  # ~5 min of 16 kHz mono PCM16


class TTSIn(BaseModel):
    text: str


@app.post("/api/tts")
async def tts(body: TTSIn):
    if len(body.text) > voice.MAX_TTS_CHARS:
        raise HTTPException(400, f"text too long to speak (max {voice.MAX_TTS_CHARS} chars)")
    clean = voice.speakable(body.text)
    if not clean:
        raise HTTPException(400, "nothing speakable in text")
    s = config.load()
    try:
        audio, mime = await run_in_threadpool(voice.synthesize, clean, s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # RuntimeError + anything: readable 502 for the UI
        detail = str(e) or f"{type(e).__name__}"
        raise HTTPException(502, detail)
    return Response(content=audio, media_type=mime, headers={"Cache-Control": "no-store"})


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
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


class _NoCacheStatic(StaticFiles):
    """LAN dev tool — always revalidate so frontend edits show up on reload
    instead of being served from a stale heuristic browser cache."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=WEB_DIR), name="static")
