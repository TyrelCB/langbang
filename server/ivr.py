"""Phone IVR front door: caller audio → STT → a small tool-limited agent →
TTS audio back. Used by an external telephony bridge (custom Python IVR),
never by the web UI.

Deliberately NOT the chat agent: callers are untrusted (anyone who dials,
plus speech-recognition noise), and the chat agent has an unsandboxed shell,
file writes, email and persistent memory. This agent is built from an
explicit allowlist of tool NAMES (settings ivr.tools, default rag_search
only) — tools are never added-then-filtered, so a tool added to the chat
agent later can't leak in here. No memory, no skills, no checkpointer:
per-call history lives in RAM and expires; every turn is appended to
data/ivr/calls.jsonl for audit.

Auth: Authorization: Bearer <data/keys/ivr_token> (generated on first start,
0600), optionally narrowed by ivr.allow_ips.

Blocking work (ffmpeg, STT, Pocket) runs in the threadpool — see voice.py.
Pocket synthesis shares the one generation lock with the web UI's
read-aloud, so a long 🔊 in a browser delays a caller's reply.
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import subprocess
import time
import uuid
import wave

import openai
from fastapi.concurrency import run_in_threadpool
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from . import agent, config, local_tools, mcp, notify, voice

logger = logging.getLogger("langbang.ivr")

TOKEN_PATH = os.path.join(config.DATA_DIR, "keys", "ivr_token")
LOG_DIR = os.path.join(config.DATA_DIR, "ivr")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TURNS_KEPT = 24
MAX_MESSAGES_PER_CALL = 3  # messages of history sent back to the model per call

# input formats: "auto" = anything ffmpeg can probe (wav/mp3/ogg/…);
# headerless telephony frames must say what they are
IN_FORMATS = {
    "auto": [],
    "pcm16k": ["-f", "s16le", "-ar", "16000", "-ac", "1"],
    "pcm8k": ["-f", "s16le", "-ar", "8000", "-ac", "1"],
    "ulaw8k": ["-f", "mulaw", "-ar", "8000", "-ac", "1"],
    "alaw8k": ["-f", "alaw", "-ar", "8000", "-ac", "1"],
}
# output: container/codec args; sample rate comes from ?rate=
OUT_FORMATS = {
    "wav": ["-f", "s16le"],  # raw from ffmpeg, header added in speak(): a piped
                             # ffmpeg wav can't seek back to fill in its length
    "pcm": ["-f", "s16le"],
    "ulaw": ["-f", "mulaw"],
    "alaw": ["-f", "alaw"],
    "mp3": ["-f", "mp3", "-b:a", "48k"],
}


# never offered to a caller, whatever ivr.tools says: a typo or copy-paste
# from the chat tool list must not hand a phone line a shell or a pen
NEVER = {"run_bash", "write_file", "edit_file", "execute", "create_scheduled_task",
         "update_scheduled_task", "set_scheduled_task_enabled", "run_scheduled_task_now",
         "delete_scheduled_task", "generate_image", "rag_ingest_text", "rag_ingest_url"}


class Busy(Exception):
    pass


# ---- auth ----

def token() -> str:
    """The bearer token, created (0600) on first call."""
    try:
        with open(TOKEN_PATH) as fh:
            t = fh.read().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    t = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(t + "\n")
    logger.info("created IVR token at %s", TOKEN_PATH)
    return t


def check_auth(header: str | None, ip: str | None, cfg: dict) -> str | None:
    """None when allowed, else the reason (→ 401/403 by the caller)."""
    if not cfg.get("enabled", True):
        return "IVR endpoint disabled (settings ivr.enabled)"
    allow = cfg.get("allow_ips") or []
    if allow and ip not in allow:
        return f"address {ip} not in ivr.allow_ips"
    got = (header or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, token()):
        return "bad or missing bearer token"
    return None


# ---- calls (RAM only) ----

_calls: dict[str, dict] = {}
_sem: asyncio.Semaphore | None = None
_sem_size = 0


def _sweep(idle_s: float) -> None:
    now = time.time()
    for cid in [c for c, v in _calls.items() if now - v["t"] > idle_s]:
        _calls.pop(cid, None)


def call(cid: str | None, cfg: dict, caller: str = "") -> tuple[str, dict]:
    """caller: the bridge's caller ID (?caller=), if it has one — attached to
    take_message pushes so a message is never orphaned from its number."""
    _sweep(float(cfg.get("idle_s") or 1800))
    cid = (cid or "").strip()[:64] or uuid.uuid4().hex[:12]
    c = _calls.setdefault(cid, {"msgs": [], "t": time.time(), "turns": 0, "caller": "", "left": 0})
    c["t"] = time.time()
    if caller:
        c["caller"] = caller.strip()[:40]
    return cid, c


def hangup(cid: str) -> bool:
    return _calls.pop(cid, None) is not None


def _slot(cfg: dict) -> asyncio.Semaphore:
    global _sem, _sem_size
    n = max(1, int(cfg.get("max_concurrent") or 2))
    if _sem is None or n != _sem_size:
        _sem, _sem_size = asyncio.Semaphore(n), n
    return _sem


# ---- audio ----

def _ffmpeg(data: bytes, in_args: list[str], out_args: list[str]) -> bytes:
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *in_args, "-i", "pipe:0",
         *out_args, "pipe:1"],
        input=data, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise ValueError("audio conversion failed: " + (p.stderr.decode(errors="replace").strip()[-300:] or "no output"))
    return p.stdout


# container names a bridge may reasonably send: all probe fine as "auto"
IN_ALIASES = {"wav", "wave", "mp3", "ogg", "opus", "webm", "flac", "m4a", ""}


def to_pcm16k(data: bytes, fmt: str) -> bytes:
    fmt = (fmt or "").lower()
    if fmt in IN_ALIASES:
        fmt = "auto"
    if fmt not in IN_FORMATS:
        raise ValueError(f"unknown input format {fmt!r} (one of: {', '.join(IN_FORMATS)})")
    return _ffmpeg(data, IN_FORMATS[fmt], ["-f", "s16le", "-ar", "16000", "-ac", "1"])


def speak(text: str, s: dict, cfg: dict, out: str, rate: int) -> bytes:
    """Reply text → audio in the bridge's format. Same normalization as the
    chat read-aloud (markdown stripped, numbers/tech terms → words)."""
    if out not in OUT_FORMATS:
        raise ValueError(f"unknown output format {out!r} (one of: {', '.join(OUT_FORMATS)})")
    v = {**s["voice"]}
    if cfg.get("pocket_voice"):
        v["pocket_voice"] = cfg["pocket_voice"]
    clean = voice.prepare(text, {**s, "voice": v})
    if not clean:
        return b""
    tail = [*OUT_FORMATS[out], "-ar", str(rate), "-ac", "1"]
    if (v.get("tts_provider") or "gtts") == "pocket":
        pcm = b"".join(voice.pocket_pcm(clean, v))
        audio = _ffmpeg(pcm, ["-f", "s16le", "-ar", str(voice.POCKET_RATE), "-ac", "1"], tail)
    else:
        audio, _ = voice.synthesize(clean, {**s, "voice": v})
        audio = _ffmpeg(audio, [], tail)
    if out != "wav":
        return audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio)
    return buf.getvalue()


# ---- the agent ----

def _take_message(cid: str, c: dict):
    """Per-call tool: the one thing a caller can make happen. Fixed target
    (Tyrel's ntfy topic + data/ivr/messages.jsonl), fixed shape, capped per
    call — the worst a manipulated model can do is a strange message."""
    @tool
    async def take_message(caller_name: str, message: str, callback_number: str = "") -> str:
        """Send the caller's message to Tyrel. Call this ONLY after the caller
        has actually told you their name and their message in this call —
        never with placeholders or guesses; if either is missing, ask the
        caller instead of calling this. caller_name: the name they gave.
        message: what they want Tyrel to know, in their words.
        callback_number: only if they gave one."""
        if c["left"] >= MAX_MESSAGES_PER_CALL:
            return "ERROR: message limit for this call reached; tell the caller it's already been passed on."
        name = " ".join(caller_name.split())[:80] or "unknown caller"
        body = " ".join(message.split())[:500]
        filler = re.compile(r"\b(unknown|placeholder|tbd|n/?a|not (yet )?(given|provided)|"
                            r"still needed|to be (provided|determined)|caller)\b", re.I)
        if not body or len(body.split()) < 2 or filler.search(body) or \
                not re.search(r"[A-Za-z]", name) or filler.fullmatch(name.strip()) or \
                name.lower() in ("unknown caller", "the caller", "anonymous caller"):
            return ("ERROR: not sent — you don't have the caller's name and message yet. "
                    "Ask the caller for them, then call take_message with their actual words.")
        cb = " ".join(callback_number.split())[:40]
        row = {"ts": round(time.time(), 3), "call_id": cid, "caller_id": c["caller"],
               "name": name, "callback": cb, "message": body}
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "messages.jsonl"), "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            logger.exception("ivr message log write failed")
        lines = [body, f"From: {name}"]
        if cb:
            lines.append(f"Callback: {cb}")
        if c["caller"]:
            lines.append(f"Caller ID: {c['caller']}")
        sent = await notify.phone_message(f"📞 Message from {name}", "\n".join(lines))
        c["left"] += 1
        if not sent.get("ok"):
            logger.warning("ivr message %s saved but push failed: %s", cid, sent.get("error"))
        return "Message saved and sent to Tyrel."  # saved either way; push is best-effort
    return take_message


async def _tools(s: dict, cfg: dict, cid: str = "", c: dict | None = None) -> list:
    want = set(cfg.get("tools") or [])
    if want & NEVER:
        logger.warning("ivr.tools: refusing %s (never exposed to callers)", ", ".join(sorted(want & NEVER)))
        want -= NEVER
    if not want:
        return []
    have = [_take_message(cid, c)] if "take_message" in want and c is not None else []
    have += [t for t in local_tools.LOCAL_TOOLS if t.name in want]
    have += [t for t in await mcp.get_tools(s.get("mcp_servers") or {}) if t.name in want]
    missing = want - {t.name for t in have}
    if missing:
        logger.warning("ivr.tools not available this turn: %s", ", ".join(sorted(missing)))
    return have


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


async def reply(cid: str, c: dict, said: str, s: dict, cfg: dict) -> dict:
    """One caller utterance → the agent's spoken answer (+ tools it used)."""
    sem = _slot(cfg)
    if sem.locked():
        raise Busy("all IVR lines busy")
    async with sem:
        tools = await _tools(s, cfg, cid, c)
        hist = c["msgs"][-MAX_TURNS_KEPT:]
        # thinking OFF regardless of the provider's template_kwargs checkbox
        # (Qwen hybrids think by default when the switch isn't sent — minutes
        # of dead air on a phone line). Only a provider that 400s on the
        # extra arg (OpenAI proper) gets the plain request.
        for eb in ({"chat_template_kwargs": {"enable_thinking": False}}, None):
            llm = agent.SGlangChatOpenAI(
                model=s["model"], base_url=s["base_url"], api_key=s["api_key"],
                temperature=s.get("temperature", 0.7), max_tokens=int(cfg.get("max_tokens") or 600),
                streaming=False, extra_body=eb)
            graph = create_agent(llm, tools, system_prompt=cfg.get("system_prompt") or "")
            try:
                out = await graph.ainvoke(
                    {"messages": [*hist, HumanMessage(said)]},
                    {"recursion_limit": 2 * int(cfg.get("max_steps") or 6) + 2})
                break
            except openai.BadRequestError as e:
                if eb is None or "chat_template_kwargs" not in str(e):
                    raise
                logger.warning("provider rejected chat_template_kwargs; IVR asking without it")
        new = out["messages"][len(hist) + 1:]
        used = [m.name for m in new if isinstance(m, ToolMessage)]
        final = next((m for m in reversed(new) if isinstance(m, AIMessage) and not m.tool_calls), None)
        text = _text(final.content).strip() if final else ""
        # history keeps only what was said aloud: tool chatter would bloat
        # every later prefill and the caller never heard it anyway
        c["msgs"] += [HumanMessage(said), AIMessage(text or "…")]
        c["turns"] += 1
        return {"reply": text or "Sorry, I don't have an answer for that.", "tools": used}


def audit(row: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "calls.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), **row}) + "\n")
    except OSError:
        logger.exception("ivr audit write failed")


async def turn_audio(text: str, s: dict, cfg: dict, out: str, rate: int) -> bytes:
    return await run_in_threadpool(speak, text, s, cfg, out, rate)
