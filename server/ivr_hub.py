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
  POST /api/ivr/node/recording      one finished call's audio file (body), saved
                                    under ~/ivr-recordings
  POST /api/ivr/node/report         {"call_id", "stem"}: transcript + email of a
                                    call whose recordings were uploaded (what
                                    ivr-audio's ivr_report.py does over ssh)

Auth: the IVR bearer token (data/keys/ivr_token), as for /api/ivr/*.
"""
import hashlib
import json
import os
import re
import time
import wave

import httpx

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import config, ivr, voice

router = APIRouter()

# ivr.* keys a node uses; the rest (tools, provider names, knowledge path)
# is resolved here into the fields below
NODE_KEYS = ("enabled", "greeting", "max_concurrent", "max_tokens", "max_spoken_words",
             "max_history", "idle_s", "llm_timeout_s")
MAX_AUDIT_ROWS = 500
REC_DIR = os.path.expanduser("~/ivr-recordings")       # where call reports attach from
REPORT_TO = os.environ.get("IVR_REPORT_TO", "tyrelcb@gmail.com")
GMAIL_MCP = os.environ.get("IVR_GMAIL_MCP", "http://127.0.0.1:4000/mcp")
MAX_RECORDING_BYTES = 200 * 1024 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


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


# ---- call reports (recording + transcript by email) ----

def _stem_path(stem: str, leg: str) -> str:
    if not _SAFE.match(stem or "") or leg not in ("mix", "caller", "agent"):
        raise HTTPException(400, "stem: [A-Za-z0-9._-]; leg: mix|caller|agent")
    return os.path.join(REC_DIR, stem + ("" if leg == "mix" else f".{leg}.wav"))


@router.post("/api/ivr/node/recording")
async def post_node_recording(request: Request, stem: str, leg: str):
    """One file of a finished call: leg=mix (the stereo m4a, saved as <stem>)
    or caller|agent (<stem>.<leg>.wav)."""
    _gate(request)
    path = _stem_path(stem, leg)
    body = await request.body()
    if not body or len(body) > MAX_RECORDING_BYTES:
        raise HTTPException(400, "empty or oversized recording")
    os.makedirs(REC_DIR, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, path)
    return {"ok": True, "path": path, "bytes": len(body)}


def _turns(call_id: str) -> list:
    rows = ivr._tail_jsonl(os.path.join(ivr.LOG_DIR, "calls.jsonl"), 3000)
    return [r for r in rows if r.get("call_id") == call_id and r.get("event") == "turn"]


def _agent_words(path: str, s: dict) -> list:
    """Whisper over the recorded agent leg: what the caller actually heard,
    on the call's own timeline (cut-off replies show up as missing words)."""
    try:
        with wave.open(path) as w:
            pcm = w.readframes(w.getnframes())
        return voice.transcribe_detail(pcm, s).get("words") or []
    except Exception as e:  # noqa: BLE001 — never fails the report
        ivr.logger.warning("report: agent-leg STT failed: %s", e)
        return []


def transcript(call_id: str, ev: list, stem: str, agent_words: list, node: str) -> str:
    """Same layout as ivr-audio's ivr_report.py."""
    lines = [f"IVR call report — call {call_id}" + (f" (node {node})" if node else ""),
             f"when: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"recording: {stem} (stereo: caller L / agent R; both legs also attached as mono wavs)", "",
             "--- conversation (caller words are utterance-relative) ---"]
    for d in ev:
        if d.get("transcript"):
            w = d.get("words") or []
            span = f" [{w[0]['s']:.1f}-{w[-1]['e']:.1f}s]" if w else ""
            lines.append(f"CALLER{span}: {d['transcript']}")
            if w:
                lines.append("    words: " + " ".join(f"{x['w']}@{x['s']:.1f}" for x in w[:40]))
        if d.get("reply"):
            lines.append(f"AGENT (intended):  {d['reply']}")
        if d.get("tools"):
            lines.append(f"TOOLS:  {', '.join(d['tools'])}")
    if len(lines) <= 5:
        lines.append("(no transcribed turns — STT returned nothing this call)")
    if agent_words:
        lines += ["", "--- agent speech as recorded (whisper, call-time offsets) ---"]
        chunk, last_e = [], 0.0
        for x in agent_words:
            if chunk and (x["s"] - last_e > 0.8 or len(chunk) >= 12):
                lines.append(f"AGENT [{chunk[0]['s']:6.1f}-{last_e:6.1f}s] " + " ".join(c["w"] for c in chunk))
                chunk = []
            chunk.append(x)
            last_e = x["e"]
        if chunk:
            lines.append(f"AGENT [{chunk[0]['s']:6.1f}-{last_e:6.1f}s] " + " ".join(c["w"] for c in chunk))
    return "\n".join(lines)


@router.post("/api/ivr/node/report")
async def post_node_report(request: Request):
    s, _ = _gate(request)
    try:
        d = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(400, "bad JSON body")
    call_id, stem, node = str(d.get("call_id") or ""), str(d.get("stem") or ""), str(d.get("node") or "")[:40]
    if not _SAFE.match(call_id):
        raise HTTPException(400, "call_id required")
    files = [p for p in (_stem_path(stem, leg) for leg in ("mix", "caller", "agent")) if os.path.exists(p)]
    if not files:
        raise HTTPException(404, f"no uploaded recording for {stem!r}")
    agent = _stem_path(stem, "agent")
    words = await run_in_threadpool(_agent_words, agent, s) if os.path.exists(agent) else []
    body = transcript(call_id, _turns(call_id), stem, words, node)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "send_email_with_attachments",
                          "arguments": {"to": REPORT_TO, "subject": f"IVR call recording: {call_id}",
                                        "body": body, "attachment_paths": files}}}
    try:
        async with httpx.AsyncClient(timeout=90) as cl:
            r = await cl.post(GMAIL_MCP, json=payload,
                              headers={"Accept": "application/json, text/event-stream"})
        resp = r.text[:300]
    except httpx.HTTPError as e:
        raise HTTPException(502, f"gmail-mcp unreachable: {e}")
    return {"ok": "Message ID" in resp, "resp": resp, "files": files, "turns": body.count("CALLER")}
