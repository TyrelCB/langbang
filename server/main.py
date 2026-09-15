"""LangBang server: FastAPI app with a llama.cpp-style web UI."""
import json
import os
import re

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config

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


@app.get("/api/threads/{tid}/messages")
async def messages(tid: str):
    return await agent.history(tid)


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


# ---- web UI ----

@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
