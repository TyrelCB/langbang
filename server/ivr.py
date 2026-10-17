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

Two turn shapes: /api/ivr/turn returns the whole reply as one audio blob;
/api/ivr/turn/stream (stream_turn below) streams it — the model's text is
cut into sentences as it is written and each sentence goes to Pocket at
once, so the caller hears the first sentence while the rest is still being
written and synthesized.
"""
import asyncio
import io
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import uuid
import wave

import httpx
import openai
from fastapi.concurrency import run_in_threadpool
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from . import agent, config, local_tools, mcp, notify, voice
from .ivr_core import PINNED_MSGS, THINKING_OFF, Sentences, rejected, variants, window  # noqa: F401

logger = logging.getLogger("langbang.ivr")

TOKEN_PATH = os.path.join(config.DATA_DIR, "keys", "ivr_token")
LOG_DIR = os.path.join(config.DATA_DIR, "ivr")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_MESSAGES_PER_CALL = 3  # take_message sends per call
MAX_KNOWLEDGE_CHARS = 40_000  # reference document cap (~10k tokens)

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


def history(c: dict, cfg: dict) -> list:
    """The call's history sent to the model (ivr_core.window); also trims
    what's kept in RAM."""
    c["msgs"] = window(c["msgs"], cfg)
    return list(c["msgs"])


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


def ivr_voice(s: dict, cfg: dict) -> dict:
    """The chat voice settings with the IVR's own overrides: tts_provider
    ("" = the chat provider) and a voice name, which means pocket_voice or
    qwen_voice depending on the provider that ends up speaking."""
    v = {**s["voice"]}
    if cfg.get("tts_provider"):
        v["tts_provider"] = cfg["tts_provider"]
    if cfg.get("pocket_voice"):
        if (v.get("tts_provider") or "gtts") == "qwen":
            v["qwen_voice"] = cfg["pocket_voice"]
        else:
            v["pocket_voice"] = cfg["pocket_voice"]
    return v


def speak(text: str, s: dict, cfg: dict, out: str, rate: int) -> bytes:
    """Reply text → audio in the bridge's format. Same normalization as the
    chat read-aloud (markdown stripped, numbers/tech terms → words)."""
    if out not in OUT_FORMATS:
        raise ValueError(f"unknown output format {out!r} (one of: {', '.join(OUT_FORMATS)})")
    v = ivr_voice(s, cfg)
    clean = voice.prepare(text, {**s, "voice": v})
    if not clean:
        return b""
    tail = [*OUT_FORMATS[out], "-ar", str(rate), "-ac", "1"]
    if (v.get("tts_provider") or "gtts") == "pocket":
        pcm = b"".join(voice.pocket_pcm(clean, v))
        audio = _ffmpeg(pcm, ["-f", "s16le", "-ar", str(voice.POCKET_RATE), "-ac", "1"], tail)
    elif (v.get("tts_provider") or "gtts") == "qwen":
        pcm = b"".join(voice.qwen_pcm(clean, v))
        audio = _ffmpeg(pcm, ["-f", "s16le", "-ar", str(voice.QWEN_RATE), "-ac", "1"], tail)
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


# ---- what the agent knows ----

_kb_cache: dict[str, tuple[float, str]] = {}


def knowledge(cfg: dict) -> str:
    """ivr.knowledge_file's text (relative paths under data/), re-read when
    its mtime changes. A missing file is logged, not fatal: the IVR still
    answers, it just knows nothing about Tyrel."""
    path = knowledge_path(cfg)
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if path not in _kb_cache:
            logger.warning("ivr.knowledge_file %s not found; answering without it", path)
            _kb_cache[path] = (-1.0, "")
        return ""
    hit = _kb_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read(MAX_KNOWLEDGE_CHARS).strip()
    _kb_cache[path] = (mtime, text)
    logger.info("ivr knowledge loaded: %s (%d chars)", path, len(text))
    return text


def system_prompt(cfg: dict) -> str:
    base = cfg.get("system_prompt") or ""
    kb = knowledge(cfg)
    if not kb:
        return base
    return f"{base}\n\n=== REFERENCE DOCUMENT ===\n{kb}\n=== END OF REFERENCE DOCUMENT ==="


def knowledge_path(cfg: dict) -> str:
    path = os.path.expanduser((cfg.get("knowledge_file") or "").strip())
    if path and not os.path.isabs(path):
        path = os.path.join(config.DATA_DIR, path)
    return path


def _tail_jsonl(path: str, n: int) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 256 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


async def status(s: dict) -> dict:
    """CONFIG → IVR card. Never includes the token itself."""
    cfg = s.get("ivr") or {}
    kp = knowledge_path(cfg)
    try:
        st = os.stat(kp) if kp else None
    except OSError:
        st = None
    now = time.time()
    names = {t.name for t in local_tools.LOCAL_TOOLS} | {"take_message"}
    try:
        names |= {t.name for t in await asyncio.wait_for(mcp.get_tools(s.get("mcp_servers") or {}), 8)}
    except Exception as e:  # noqa: BLE001 - a dead MCP server must not break the card
        logger.warning("ivr status: MCP tools unavailable: %s", e)
    turns = [r for r in _tail_jsonl(os.path.join(LOG_DIR, "calls.jsonl"), 400) if r.get("event") == "turn"]
    return {
        "token_file": TOKEN_PATH, "token_exists": os.path.exists(TOKEN_PATH),
        "knowledge": {"path": kp, "exists": st is not None, "size": st.st_size if st else 0,
                      "mtime": st.st_mtime if st else None},
        "active": [{"id": cid, "turns": c.get("turns", 0), "caller": c.get("caller", ""),
                    "idle_s": int(now - c["t"])} for cid, c in _calls.items()
                   if not cid.startswith("_")],  # _prewarm is the startup warm-up, not a caller
        "last_turn": turns[-1].get("ts") if turns else None,
        "messages": _tail_jsonl(os.path.join(LOG_DIR, "messages.jsonl"), 10)[::-1],
        "tools": sorted(names - NEVER),
        "never": sorted(NEVER),
        "defaults": {k: config.DEFAULTS["ivr"][k] for k in ("system_prompt", "greeting")},
    }


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


_accepts: dict[str, int] = {}  # base_url → index into THINKING_OFF that worked


def _variants(conn: dict):
    return variants(_accepts, conn["base_url"])


def _rejected(e: Exception, eb: dict | None) -> bool:
    return rejected(e, eb)


def _conns(s: dict, cfg: dict) -> list[dict]:
    """Connections to try, in order: the IVR's own provider/model (ivr.provider
    — a small model on another box keeps phone calls off the chat model's
    concurrency slots), then the chat default if that box is unreachable.
    ivr.provider "" = the chat default only."""
    main = s  # already resolved to the chat default by the caller
    pname = cfg.get("provider") or ""
    if pname and pname in (s.get("providers") or {}):
        own = config.effective(s, {"provider": pname, "model": cfg.get("model") or ""})
        if own["base_url"] == main["base_url"]:
            return [own]
        if time.time() - _down.get(own["base_url"], -_DOWN_S) < _DOWN_S:
            return [main]  # found dead moments ago: don't pay to rediscover it every turn
        return [{**own, "fast_fail": True}, main]
    if pname:
        logger.warning("ivr.provider %r not in providers; using the chat default", pname)
    return [main]


_UNREACHABLE = (openai.APIConnectionError, openai.APITimeoutError)
# an unreachable IVR box is skipped for this long, then probed again. Each
# probe fails fast (no retry, 2 s connect timeout): the Mac mini sitting at
# the login window after a reboot cost ~1.5 s of retry on every turn of
# calls ccb9b75b8a73/1ce80a04f8af (2026-10-16) before the fallback answered
_DOWN_S = 60.0
_down: dict[str, float] = {}  # base_url -> when it was last found unreachable


def _llm(conn: dict, cfg: dict, extra_body: dict | None, streaming: bool):
    return agent.SGlangChatOpenAI(
        model=conn["model"], base_url=conn["base_url"], api_key=conn["api_key"],
        temperature=conn.get("temperature", 0.7), max_tokens=int(cfg.get("max_tokens") or 600),
        timeout=(httpx.Timeout(float(cfg.get("llm_timeout_s") or 30), connect=2.0)
                 if conn.get("fast_fail") else float(cfg.get("llm_timeout_s") or 30)),
        max_retries=0 if conn.get("fast_fail") else 1,
        streaming=streaming, extra_body=extra_body)


def _recovered(conn: dict) -> None:
    if _down.pop(conn["base_url"], None) is not None:
        logger.warning("IVR model at %s reachable again", conn["base_url"])


def _retryable(e: Exception, conn: dict, eb: dict | None) -> Exception:
    """Return e (tagged with its backend) if the next attempt may succeed,
    else re-raise it."""
    if _rejected(e, eb):
        logger.warning("%s rejected %s; IVR trying the next thinking switch",
                       conn["base_url"], ", ".join(eb))
    elif isinstance(e, _UNREACHABLE):
        if conn["base_url"] not in _down:  # loud once per outage, then quiet until it recovers
            logger.error("IVR model at %s unreachable (%s); calls fall back to the chat "
                         "model (slower) until it is back", conn["base_url"], e)
        _down[conn["base_url"]] = time.time()
    else:
        raise e
    e.base_url = conn["base_url"]
    return e


def _attempts(s: dict, cfg: dict):
    """(conn, variant index, extra_body) in try order; callers `continue` on a
    rejected thinking switch or an unreachable backend, `break` on success."""
    for conn in _conns(s, cfg):
        for j, eb in _variants(conn):
            yield conn, j, eb


async def reply(cid: str, c: dict, said: str, s: dict, cfg: dict) -> dict:
    """One caller utterance → the agent's spoken answer (+ tools it used)."""
    sem = _slot(cfg)
    if sem.locked():
        raise Busy("all IVR lines busy")
    async with sem:
        tools = await _tools(s, cfg, cid, c)
        hist = history(c, cfg)
        err = None
        for conn, j, eb in _attempts(s, cfg):
            if err is not None and isinstance(err, _UNREACHABLE) and conn["base_url"] == err.base_url:
                continue  # that backend is down — skip its other variants
            graph = create_agent(_llm(conn, cfg, eb, streaming=False), tools,
                                 system_prompt=system_prompt(cfg))
            try:
                out = await graph.ainvoke(
                    {"messages": [*hist, HumanMessage(said)]},
                    {"recursion_limit": 2 * int(cfg.get("max_steps") or 6) + 2})
                _accepts[conn["base_url"]] = j
                _recovered(conn)
                break
            except Exception as e:  # noqa: BLE001 — sorted below
                err = _retryable(e, conn, eb)
        else:
            raise err
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


async def _warm_prefix(s: dict, cfg: dict, cid: str, c: dict) -> None:
    """Prefill exactly what every turn starts with — the system prompt and
    the same tool schemas the agent binds — on the backend a turn would use."""
    tools = await _tools(s, cfg, cid, c)
    err = None
    for conn, j, eb in _attempts(s, cfg):
        if err is not None and isinstance(err, _UNREACHABLE) and conn["base_url"] == err.base_url:
            continue
        llm = _llm(conn, {**cfg, "max_tokens": 1}, eb, streaming=False)
        try:
            await (llm.bind_tools(tools) if tools else llm).ainvoke([SystemMessage(system_prompt(cfg))])
            _accepts[conn["base_url"]] = j
            _recovered(conn)
            return
        except Exception as e:  # noqa: BLE001 — sorted by _retryable
            err = _retryable(e, conn, eb)
    raise err


async def prewarm(s: dict, with_voice: bool = True) -> None:
    """Startup: pay the first call's cold costs before a caller does —
    the IVR voice (Pocket model + voice state + first synthesis, ~11 s) and
    the IVR model's prompt prefix (system prompt + knowledge + tool schemas,
    ~3k tokens: ~6 s of prefill on the Mac mini, then cached). One throwaway
    turn on a scratch call, take_message disarmed, nothing audited.

    The model warm-up sends the system prompt + tools ALONE (no user turn)
    and stops after one token. Ollama on the Mac can only reuse a cached
    prefix up to a point it checkpointed, and it checkpoints near the end of
    each prompt: warming with a "Hello?" turn left no checkpoint before the
    caller's own first words, so that turn re-read all ~3.3k tokens anyway
    (13.7 s, call 19ab861b76c9, 2026-10-16). Ending the warm-up where the
    caller's turn begins makes it a cache hit (0.3 s). with_voice=False skips the
    synthesis — at call start the greeting just warmed the voice, and a second
    synthesis competed for fedora's CPU with the caller's first STT (8.3 s and
    3.3 s first-turn STT on 2026-10-16 vs ~1.6 s mid-call)."""
    cfg = s.get("ivr") or {}
    if not cfg.get("enabled"):
        return
    t0 = time.time()
    if with_voice:
        try:
            await run_in_threadpool(lambda: sum(len(b) for b in speak_pcm("Hello.", s, cfg)))
        except Exception as e:  # noqa: BLE001 — the first real call will say why
            logger.warning("ivr prewarm (voice) failed: %s", e)
    t1 = time.time()
    cid, c = call("_prewarm", cfg)
    c["left"] = MAX_MESSAGES_PER_CALL  # a warm-up must never send a message
    try:
        await _warm_prefix(s, cfg, cid, c)
    except Exception as e:  # noqa: BLE001
        logger.warning("ivr prewarm (model) failed: %s", e)
    finally:
        hangup(cid)
    logger.warning("ivr prewarm: voice %.1fs, model %.1fs", t1 - t0, time.time() - t1)


# ---- streaming turn (/api/ivr/turn/stream) ----

STREAM_RATE = voice.POCKET_RATE  # streamed audio: mono s16le @ 24 kHz, Pocket's native rate
FALLBACK_REPLY = "Sorry, I don't have an answer for that."
ERROR_REPLY = "Sorry, something went wrong on my end. Please try again."


def speak_pcm(text: str, s: dict, cfg: dict, stop: threading.Event | None = None):
    """Yield mono s16le @ STREAM_RATE for `text` as it is generated (Pocket,
    or the Qwen service), or in one piece for the non-streaming providers.
    Blocking: run in a thread."""
    v = ivr_voice(s, cfg)
    clean = voice.prepare(text, {**s, "voice": v})
    if not clean:
        return
    provider = v.get("tts_provider") or "gtts"
    if provider == "pocket":
        yield from voice.pocket_pcm(clean, v, stop)
    elif provider == "qwen":
        yield from voice.qwen_pcm(clean, v, stop)
    else:
        audio, _ = voice.synthesize(clean, {**s, "voice": v})
        yield _ffmpeg(audio, [], ["-f", "s16le", "-ar", str(STREAM_RATE), "-ac", "1"])


async def reply_stream(cid: str, c: dict, said: str, s: dict, cfg: dict, ctl: dict | None = None):
    """reply(), streamed: yields ("text", delta) as the model writes what the
    caller will hear, then ("done", {reply, tools, fallback, cut}). Same
    tools, prompt and history rules as reply(). Text the model writes before
    a tool call ("Let me check.") is spoken too — natural filler while the
    tool runs. `ctl`: the consumer sets ctl["stop"] to cancel the model
    mid-answer and ctl["spoken"] to what was actually said, which is then
    what history records."""
    ctl = ctl if ctl is not None else {}
    sem = _slot(cfg)
    if sem.locked():
        raise Busy("all IVR lines busy")
    async with sem:
        tools = await _tools(s, cfg, cid, c)
        hist = history(c, cfg)
        parts: list[str] = []
        final: list = []
        err = None
        for conn, j, eb in _attempts(s, cfg):
            if err is not None and isinstance(err, _UNREACHABLE) and conn["base_url"] == err.base_url:
                continue
            graph = create_agent(_llm(conn, cfg, eb, streaming=True), tools,
                                 system_prompt=system_prompt(cfg))
            last_id = None
            stream = graph.astream(
                {"messages": [*hist, HumanMessage(said)]},
                {"recursion_limit": 2 * int(cfg.get("max_steps") or 6) + 2},
                stream_mode=["messages", "values"])
            try:
                async for mode, data in stream:
                    if ctl.get("stop"):
                        break
                    if mode == "values":
                        final = data.get("messages") or final
                        continue
                    chunk = data[0]
                    if not isinstance(chunk, AIMessageChunk):
                        continue
                    t = _text(chunk.content)
                    if not t:
                        continue
                    if parts and chunk.id != last_id:  # a new model step: keep words apart
                        t = " " + t
                    last_id = chunk.id
                    parts.append(t)
                    yield ("text", t)
                _accepts[conn["base_url"]] = j
                _recovered(conn)
                break
            except Exception as e:  # noqa: BLE001 — sorted below
                if parts or len(final) > len(hist) + 1:
                    raise  # caller heard part of it / a tool already ran: no do-over
                err = _retryable(e, conn, eb)
            finally:
                await stream.aclose()  # cut: cancels the in-flight model request
        else:
            raise err
        new = final[len(hist) + 1:]
        used = [m.name for m in new if isinstance(m, ToolMessage)]
        cut = bool(ctl.get("stop"))
        text = (ctl.get("spoken") if cut else "".join(parts)) or ""
        text = text.strip()
        c["msgs"] += [HumanMessage(said), AIMessage(text or "…")]
        c["turns"] += 1
        yield ("done", {"reply": text or FALLBACK_REPLY, "tools": used, "fallback": not text, "cut": cut})


async def stream_turn(cid: str, c: dict, said: str, s: dict, cfg: dict, t0: float):
    """Drive one streamed turn. Yields event dicts:
      {"type": "text", "text": delta}         model output as written
      {"type": "audio", "pcm": bytes}         s16le mono @ STREAM_RATE, as synthesized
      {"type": "done", "reply", "tools", "timings", ["error"]}
    The model runs as a task feeding sentences to one speaker thread (Pocket
    is single-generation anyway), so synthesis of sentence 1 overlaps the
    model writing sentence 2. Closing the generator (client hung up) stops
    both."""
    loop = asyncio.get_running_loop()
    out: asyncio.Queue = asyncio.Queue()
    todo: queue.Queue = queue.Queue()
    stop = threading.Event()
    marks: dict[str, float] = {}

    def emit(ev):
        loop.call_soon_threadsafe(out.put_nowait, ev)

    def speaker():
        try:
            while (text := todo.get()) is not None:
                if stop.is_set():
                    continue
                for pcm in speak_pcm(text, s, cfg, stop):
                    emit(("audio", pcm))
        except Exception as e:  # noqa: BLE001
            logger.exception("ivr stream TTS failed")
            emit(("tts_error", f"{type(e).__name__}: {e}"))
        finally:
            emit(("spoken", None))

    limit = int(cfg.get("max_spoken_words") or 0)
    ctl: dict = {"spoken": ""}

    def say(sentence: str) -> bool:
        """Queue a sentence unless it would take the answer past the word
        cap (the first sentence always goes, however long)."""
        if limit and ctl["spoken"] and len(ctl["spoken"].split()) + len(sentence.split()) > limit:
            ctl["stop"] = True
            return False
        todo.put(sentence)
        ctl["spoken"] = f"{ctl['spoken']} {sentence}".strip()
        return True

    async def think():
        split, result = Sentences(), None
        try:
            async for kind, val in reply_stream(cid, c, said, s, cfg, ctl):
                if kind == "text":
                    if ctl.get("stop"):
                        continue
                    marks.setdefault("first_text_s", time.time() - t0)
                    await out.put(("text", val))
                    for sentence in split.feed(val):
                        if not say(sentence):
                            break
                else:
                    result = val
            marks["agent_s"] = time.time() - t0
            if not ctl.get("stop"):
                for sentence in split.flush():
                    say(sentence)
                if ctl.get("stop"):  # the cap hit on the last fragment: history must match what was said
                    c["msgs"][-1] = AIMessage(ctl["spoken"])
                    result = {**result, "reply": ctl["spoken"], "cut": True}
            if result["fallback"]:
                todo.put(result["reply"])
        except Exception as e:  # noqa: BLE001
            logger.exception("ivr stream turn failed")
            result = {"reply": ERROR_REPLY, "tools": [], "error": f"{type(e).__name__}: {e}"}
            todo.put(ERROR_REPLY)
        finally:
            todo.put(None)
            await out.put(("thought", result))

    threading.Thread(target=speaker, daemon=True, name=f"ivr-tts-{cid}").start()
    task = asyncio.create_task(think())
    result, spoken, tts_error = None, False, None
    try:
        while not (spoken and result is not None):
            kind, val = await out.get()
            if kind == "text":
                yield {"type": "text", "text": val}
            elif kind == "audio":
                marks.setdefault("first_audio_s", time.time() - t0)
                yield {"type": "audio", "pcm": val}
            elif kind == "thought":
                result = val
            elif kind == "tts_error":
                tts_error = val
            elif kind == "spoken":
                spoken = True
        timings = {k: round(v, 2) for k, v in marks.items()}
        timings["total_s"] = round(time.time() - t0, 2)
        ev = {"type": "done", "reply": result["reply"], "tools": result["tools"], "timings": timings}
        if result.get("cut"):
            ev["cut"] = True
        err = result.get("error") or (f"TTS failed: {tts_error}" if tts_error else None)
        if err:
            ev["error"] = err
        yield ev
    finally:
        stop.set()
        todo.put(None)  # release the speaker if think() never got to
        if not task.done():
            task.cancel()
