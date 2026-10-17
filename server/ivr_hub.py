"""LangBang as the hub for standalone IVR nodes (server/ivr_node.py).

A node answers calls on its own box (the Mac mini: STT, model, voice and
browser all local) but is managed here: it pulls what the IVR card
configures and pushes its call log back, so calls.jsonl, the IVR card and
ivr_probe see every call wherever it was answered. A node keeps answering on
its last pulled config while this hub is down.

  GET  /api/ivr/node/config         everything a node needs to answer calls
                                    (ETag; If-None-Match -> 304)
  GET  /api/ivr/node/voice/{name}   a saved Pocket voice (.safetensors)
  POST /api/ivr/node/audit          {"node": name, "rows": [...]} -> calls.jsonl

Auth: the IVR bearer token (data/keys/ivr_token), as for /api/ivr/*.
"""
import hashlib
import json
import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from . import config, ivr, voice

router = APIRouter()

# ivr.* keys a node uses; the rest (tools, provider names, knowledge path)
# is resolved here into the fields below
NODE_KEYS = ("enabled", "greeting", "max_concurrent", "max_tokens", "max_spoken_words",
             "max_history", "idle_s", "llm_timeout_s")
MAX_AUDIT_ROWS = 500


def _gate(request: Request) -> tuple[dict, dict]:
    s = config.load()
    cfg = s["ivr"]
    why = ivr.check_auth(request.headers.get("authorization"),
                         request.client.host if request.client else None, cfg)
    if why:
        raise HTTPException(403 if "allow_ips" in why or "disabled" in why else 401, why)
    return s, cfg


def _conn(e: dict) -> dict:
    return {"base_url": e["base_url"], "api_key": e.get("api_key") or "none",
            "model": e["model"], "temperature": e.get("temperature", 0.7)}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def node_config(s: dict, cfg: dict) -> dict:
    v = ivr.ivr_voice(s, cfg)
    name = (v.get("pocket_voice") or "").strip()
    saved = voice._saved_path(name) if name else None
    own = None
    if cfg.get("provider") in (s.get("providers") or {}):
        own = _conn(config.effective(s, {"provider": cfg["provider"], "model": cfg.get("model") or ""}))
    if cfg.get("tools"):
        # a node runs the model without tools: say so rather than silently differ
        ivr.logger.warning("ivr node config: ivr.tools %s are not available on nodes", cfg["tools"])
    return {
        "ivr": {k: cfg.get(k) for k in NODE_KEYS},
        "system_prompt": ivr.system_prompt(cfg),   # base prompt + knowledge document
        "voice": {k: val for k, val in v.items() if not k.startswith("gcloud")},
        "voice_file": ({"name": name, "sha256": _sha256(saved), "size": os.path.getsize(saved),
                        "meta": voice._saved_meta(name)} if saved else None),
        "own": own,                                # the IVR's own model (a node may run it locally)
        "fallback": _conn(config.effective(s)),    # the chat default
    }


@router.get("/api/ivr/node/config")
async def get_node_config(request: Request):
    s, cfg = _gate(request)
    body = json.dumps(node_config(s, cfg), sort_keys=True).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


@router.get("/api/ivr/node/voice/{name}")
async def get_node_voice(request: Request, name: str):
    _gate(request)
    path = voice._saved_path(name)
    if not path:
        raise HTTPException(404, f"no saved voice {name!r}")
    return FileResponse(path, media_type="application/octet-stream")


@router.post("/api/ivr/node/audit")
async def post_node_audit(request: Request):
    """A node's call-log rows, appended to calls.jsonl as they were written
    (their own ts) with the node's name added."""
    _gate(request)
    try:
        d = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(400, "bad JSON body")
    node = str(d.get("node") or "")[:40] or "node"
    rows = d.get("rows") or []
    if not isinstance(rows, list) or len(rows) > MAX_AUDIT_ROWS:
        raise HTTPException(400, f"rows: a list of at most {MAX_AUDIT_ROWS}")
    os.makedirs(ivr.LOG_DIR, exist_ok=True)
    with open(os.path.join(ivr.LOG_DIR, "calls.jsonl"), "a") as fh:
        for r in rows:
            if isinstance(r, dict):
                fh.write(json.dumps({**r, "node": node}) + "\n")
    return {"ok": True, "rows": len(rows)}
