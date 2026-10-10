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

import openai
from fastapi.concurrency import run_in_threadpool
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool

from . import agent, config, local_tools, mcp, notify, voice

logger = logging.getLogger("langbang.ivr")

TOKEN_PATH = os.path.join(config.DATA_DIR, "keys", "ivr_token")
LOG_DIR = os.path.join(config.DATA_DIR, "ivr")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
PINNED_MSGS = 4  # a call's first two exchanges (who's calling, and why) always stay
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
    """The call's history sent to the model: all of it while it fits
    ivr.max_history messages, then the opening exchanges (PINNED_MSGS — the
    caller's name and reason usually come first) plus the most recent ones.
    Spoken exchanges are short (~25 tokens a message), so the default 160
    holds a 30+ minute call in ~4k tokens. Also trims what's kept in RAM.

    Trimmed in chunks, not one exchange per turn: dropping the oldest
    unpinned exchange changes the prompt right after the opening, so the
    backend's prefix cache misses and re-prefills the whole history — every
    turn (measured on the Mac mini: turns 81+ of a call went 1.3 s → 6.5 s).
    Cutting a quarter of the window at once pays that once per ~20 turns."""
    cap = max(PINNED_MSGS + 2, int(cfg.get("max_history") or 160))
    cap -= cap % 2  # whole exchanges: the kept tail must start on a caller turn
    msgs = c["msgs"]
    if len(msgs) > cap:
        slack = max(2, cap // 4)
        slack -= slack % 2
        keep = max(2, cap - slack - PINNED_MSGS)
        msgs = c["msgs"] = msgs[:PINNED_MSGS] + msgs[-keep:]
    return list(msgs)


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


# ---- what the agent knows ----

_kb_cache: dict[str, tuple[float, str]] = {}


def knowledge(cfg: dict) -> str:
    """ivr.knowledge_file's text (relative paths under data/), re-read when
    its mtime changes. A missing file is logged, not fatal: the IVR still
    answers, it just knows nothing about Tyrel."""
    path = os.path.expanduser((cfg.get("knowledge_file") or "").strip())
    if not path:
        return ""
    if not os.path.isabs(path):
        path = os.path.join(config.DATA_DIR, path)
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


# thinking OFF regardless of the provider's template_kwargs checkbox (Qwen
# hybrids think by default when the switch isn't sent — minutes of dead air
# on a phone line). Backends disagree on the switch: sglang/vLLM read
# chat_template_kwargs; Ollama's /v1 ignores that (and its own "think":false
# once tools are in the request) and honours only reasoning_effort "none",
# which vLLM 400s on. So try both, then each alone-ish, then nothing, and
# remember per backend what it accepted — the 400 round trip is paid once.
_TPL_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
_THINKING_OFF = ({**_TPL_OFF, "reasoning_effort": "none"}, _TPL_OFF, None)
_accepts: dict[str, int] = {}  # base_url → index into _THINKING_OFF that worked


def _variants(conn: dict):
    i = _accepts.get(conn["base_url"], 0)
    return [(j, _THINKING_OFF[j]) for j in range(i, len(_THINKING_OFF))]


def _rejected(e: Exception, eb: dict | None) -> bool:
    """A 400 about one of the thinking switches (→ try the next variant)."""
    return eb is not None and isinstance(e, openai.BadRequestError) and \
        any(k in str(e) for k in eb)


def _conns(s: dict, cfg: dict) -> list[dict]:
    """Connections to try, in order: the IVR's own provider/model (ivr.provider
    — a small model on another box keeps phone calls off the chat model's
    concurrency slots), then the chat default if that box is unreachable.
    ivr.provider "" = the chat default only."""
    main = s  # already resolved to the chat default by the caller
    pname = cfg.get("provider") or ""
    if pname and pname in (s.get("providers") or {}):
        own = config.effective(s, {"provider": pname, "model": cfg.get("model") or ""})
        return [own, main] if own["base_url"] != main["base_url"] else [own]
    if pname:
        logger.warning("ivr.provider %r not in providers; using the chat default", pname)
    return [main]


_UNREACHABLE = (openai.APIConnectionError, openai.APITimeoutError)


def _llm(conn: dict, cfg: dict, extra_body: dict | None, streaming: bool):
    return agent.SGlangChatOpenAI(
        model=conn["model"], base_url=conn["base_url"], api_key=conn["api_key"],
        temperature=conn.get("temperature", 0.7), max_tokens=int(cfg.get("max_tokens") or 600),
        timeout=float(cfg.get("llm_timeout_s") or 30), max_retries=1,
        streaming=streaming, extra_body=extra_body)


def _retryable(e: Exception, conn: dict, eb: dict | None) -> Exception:
    """Return e (tagged with its backend) if the next attempt may succeed,
    else re-raise it."""
    if _rejected(e, eb):
        logger.warning("%s rejected %s; IVR trying the next thinking switch",
                       conn["base_url"], ", ".join(eb))
    elif isinstance(e, _UNREACHABLE):
        logger.warning("IVR model at %s unreachable (%s); falling back", conn["base_url"], e)
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


async def prewarm(s: dict) -> None:
    """Startup: pay the first call's cold costs before a caller does —
    the IVR voice (Pocket model + voice state + first synthesis, ~11 s) and
    the IVR model's prompt prefix (system prompt + knowledge + tool schemas,
    ~3k tokens: ~6 s of prefill on the Mac mini, then cached). One throwaway
    turn on a scratch call, take_message disarmed, nothing audited."""
    cfg = s.get("ivr") or {}
    if not cfg.get("enabled"):
        return
    t0 = time.time()
    try:
        await run_in_threadpool(lambda: sum(len(b) for b in speak_pcm("Hello.", s, cfg)))
    except Exception as e:  # noqa: BLE001 — the first real call will say why
        logger.warning("ivr prewarm (voice) failed: %s", e)
    t1 = time.time()
    cid, c = call("_prewarm", cfg)
    c["left"] = MAX_MESSAGES_PER_CALL  # a warm-up must never send a message
    try:
        await reply(cid, c, "Hello?", s, cfg)
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
    """Yield mono s16le @ STREAM_RATE for `text` as it is generated (Pocket),
    or in one piece for the non-streaming providers. Blocking: run in a thread."""
    v = {**s["voice"]}
    if cfg.get("pocket_voice"):
        v["pocket_voice"] = cfg["pocket_voice"]
    clean = voice.prepare(text, {**s, "voice": v})
    if not clean:
        return
    if (v.get("tts_provider") or "gtts") == "pocket":
        yield from voice.pocket_pcm(clean, v, stop)
    else:
        audio, _ = voice.synthesize(clean, {**s, "voice": v})
        yield _ffmpeg(audio, [], ["-f", "s16le", "-ar", str(STREAM_RATE), "-ac", "1"])


class Sentences:
    """Cut streamed model text into speakable sentences. A sentence ends at
    . ! ? (plus closing quotes/brackets) followed by whitespace, or at a
    newline; "3.5" and "e.g." mid-word never split because the next char
    isn't whitespace. Fragments shorter than `min_chars` wait for more text
    so Pocket isn't handed "Sure." and "Okay." as separate chunks.

    Until the first piece is out, a clause ending in , ; : also counts once
    it is `first_clause` chars long: the caller hears audio as soon as there
    is a natural pause to cut at, not after a long first sentence."""
    _END = re.compile(r"[.!?]+[\"')\]]*\s+|\n+")
    _CLAUSE = re.compile(r"[.!?,;:]+[\"')\]]*\s+|\n+")
    _ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|St|Jr|Sr|Inc|Co|No)|\b[A-Z])\.$")

    def __init__(self, min_chars: int = 12, first_clause: int = 40):
        self.buf, self.min, self.first, self.started = "", min_chars, first_clause, False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out, start = [], 0
        if not self.started:
            for m in self._CLAUSE.finditer(self.buf):
                piece = self.buf[:m.end()].strip()
                if self._ABBREV.search(piece):
                    continue
                ends_sentence = bool(self._END.fullmatch(m.group()))
                if len(piece) >= (self.min if ends_sentence else self.first):
                    out.append(piece)
                    start = m.end()
                    self.started = True
                    break
        for m in self._END.finditer(self.buf, start):
            piece = self.buf[start:m.end()].strip()
            if len(piece) >= self.min and not self._ABBREV.search(piece):
                out.append(piece)
                start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        self.started = True
        return [rest] if rest else []


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
