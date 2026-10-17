"""ivr_node.py — a standalone, IVR-only turn server for one box (the Mac
mini), federated with a LangBang hub (server/ivr_hub.py).

The node answers calls with nothing but local services: speech-to-text
(mac-stt), the IVR model (Ollama), Pocket TTS in-process, and the telephony
loop (ivr_loop.py) on the same machine. LangBang stays the place the IVR is
managed: the node pulls its config from the hub (prompt + knowledge, voice,
limits, fallback model) and pushes every call-log row back, so the hub's
calls.jsonl and IVR card see all calls. If the hub is down the node keeps
answering on its last pulled config (cached on disk) and spools the log.

A new config is applied only between calls: a prompt change mid-call
throws away the model's prompt cache and costs the caller ~12 s.

Same API as LangBang's IVR, so ivr_loop.py / ivr_probe.py point at either:
  POST /api/ivr/start, /api/ivr/turn/stream, /api/ivr/hangup
  GET  /api/ivr/status          POST /api/stt
No agent framework and no tools: the model is called directly and streamed.

Run:  LANGBANG_DATA_DIR=<node data dir> python -m server.ivr_node
Env:  IVR_NODE_HUB       hub base URL (http://fedora.tailnet.ts.net:8123)
      IVR_NODE_NAME      this node's name in the hub's log (mac-mini)
      IVR_NODE_BIND / IVR_NODE_PORT  (0.0.0.0 / 8130)
      IVR_NODE_LLM_URL   the local OpenAI-compatible model server, used in
                         place of the hub's own-model URL (http://127.0.0.1:11434/v1)
      IVR_NODE_STT_URL   local whisper service (http://127.0.0.1:8020)
      IVR_NODE_TOKEN_FILE  the IVR bearer token, same as the hub's
                         (<data dir>/ivr_token)
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

os.environ.setdefault("LANGBANG_DATA_DIR", os.path.expanduser("~/.ivr-node"))

import httpx  # noqa: E402
import openai  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from . import config, voice  # noqa: E402
from .ivr_core import Sentences, rejected, variants, window  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ivr_node")

DATA = config.DATA_DIR
HUB = os.environ.get("IVR_NODE_HUB", "http://fedora.tailnet.ts.net:8123").rstrip("/")
NAME = os.environ.get("IVR_NODE_NAME", "mac-mini")
LLM_URL = os.environ.get("IVR_NODE_LLM_URL", "http://127.0.0.1:11434/v1")
STT_URL = os.environ.get("IVR_NODE_STT_URL", "http://127.0.0.1:8020").rstrip("/")
TOKEN_FILE = os.environ.get("IVR_NODE_TOKEN_FILE", os.path.join(DATA, "ivr_token"))
CONFIG_CACHE = os.path.join(DATA, "node_config.json")
LOG_PATH = os.path.join(DATA, "ivr", "calls.jsonl")
SPOOL_PATH = os.path.join(DATA, "ivr", "spool.jsonl")
SYNC_S = 60.0
FF = shutil.which("ffmpeg") or next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), "ffmpeg")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
STREAM_RATE = voice.POCKET_RATE
FALLBACK_REPLY = "Sorry, I don't have an answer for that."
ERROR_REPLY = "Sorry, something went wrong on my end. Please try again."


def token() -> str:
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


def _hub_req(path: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(HUB + path, data=data, headers={
        "Authorization": f"Bearer {token()}", **({"Content-Type": "application/json"} if data else {}),
        **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


# ---- config from the hub ----

class Hub:
    """The config this node answers with, pulled from the hub every SYNC_S
    and cached on disk. A changed config waits in `pending` until no call is
    live."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cfg: dict = {}
        self.etag = ""
        self.pending: dict | None = None
        self.last_ok = 0.0
        self.last_error = ""
        try:
            with open(CONFIG_CACHE) as fh:
                cached = json.load(fh)
            self.cfg, self.etag = cached["config"], cached.get("etag", "")
            logger.info("loaded cached hub config (%s)", self.etag)
        except (OSError, ValueError, KeyError):
            logger.warning("no cached hub config yet: %s", CONFIG_CACHE)

    def pull(self) -> bool:
        """True if a new config was fetched (now applied or pending)."""
        try:
            with _hub_req("/api/ivr/node/config", headers={"If-None-Match": self.etag} if self.etag else {}) as r:
                new, etag = json.load(r), r.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                self.last_ok, self.last_error = time.time(), ""
                return False
            self.last_error = f"HTTP {e.code}"
            logger.warning("hub config pull failed: HTTP %s", e.code)
            return False
        except (OSError, ValueError) as e:
            if not self.last_error:
                logger.error("hub %s unreachable (%s); answering on the cached config", HUB, e)
            self.last_error = str(e)
            return False
        if self.last_error:
            logger.warning("hub reachable again")
        self.last_ok, self.last_error = time.time(), ""
        try:
            self._fetch_voice(new)
        except (OSError, ValueError) as e:  # keep the old config until its voice arrives
            logger.error("voice download from the hub failed (%s); will retry", e)
            return False
        with self.lock:
            self.pending = {"config": new, "etag": etag}
        self.apply_if_idle()
        return True

    def _fetch_voice(self, new: dict):
        vf = new.get("voice_file")
        if not vf:
            return
        os.makedirs(voice.VOICES_DIR, exist_ok=True)
        path = os.path.join(voice.VOICES_DIR, vf["name"] + ".safetensors")
        try:
            have = hashlib.sha256(open(path, "rb").read()).hexdigest()
        except OSError:
            have = ""
        if have == vf["sha256"]:
            return
        with _hub_req(f"/api/ivr/node/voice/{vf['name']}", timeout=60) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != vf["sha256"]:
            raise OSError(f"voice {vf['name']} download corrupt")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        with open(os.path.join(voice.VOICES_DIR, vf["name"] + ".json"), "w") as fh:
            json.dump(vf.get("meta") or {"name": vf["name"]}, fh)
        voice._forget_voice(vf["name"])
        logger.info("voice %s updated from the hub (%d bytes)", vf["name"], len(data))

    def apply_if_idle(self) -> bool:
        with self.lock:
            if self.pending is None or live_calls():
                return False
            old_prompt = self.cfg.get("system_prompt")
            self.cfg, self.etag = self.pending["config"], self.pending["etag"]
            self.pending = None
        os.makedirs(DATA, exist_ok=True)
        tmp = CONFIG_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"config": self.cfg, "etag": self.etag, "pulled": time.time()}, fh)
        os.replace(tmp, CONFIG_CACHE)
        logger.warning("applied hub config %s", self.etag)
        if self.cfg.get("system_prompt") != old_prompt and _loop is not None:
            asyncio.run_coroutine_threadsafe(prewarm(with_voice=False), _loop)
        return True

    def loop(self):
        while True:
            try:
                self.pull()
            except Exception:  # noqa: BLE001 — never let the sync thread die
                logger.exception("hub sync failed")
            time.sleep(SYNC_S)


hub = Hub()


def ivr_cfg() -> dict:
    return hub.cfg.get("ivr") or {}


# ---- call log: local file + spooled to the hub ----

_log_lock = threading.Lock()
_log_kick = threading.Event()


def audit(row: dict) -> None:
    line = json.dumps({"ts": round(time.time(), 3), **row}) + "\n"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _log_lock:
            with open(LOG_PATH, "a") as fh:
                fh.write(line)
            with open(SPOOL_PATH, "a") as fh:
                fh.write(line)
        _log_kick.set()
    except OSError:
        logger.exception("ivr audit write failed")


def _push_loop():
    """Ship spooled rows to the hub; on failure keep them and retry."""
    while True:
        _log_kick.wait(30)
        _log_kick.clear()
        with _log_lock:
            try:
                with open(SPOOL_PATH) as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
        rows = []
        for ln in lines[:500]:
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
        if not rows:
            continue
        try:
            with _hub_req("/api/ivr/node/audit", json.dumps({"node": NAME, "rows": rows}).encode()):
                pass
        except (OSError, ValueError) as e:
            logger.warning("call log push to hub failed (%s); %d rows spooled", e, len(lines))
            time.sleep(30)
            continue
        with _log_lock:  # drop what was sent; rows appended meanwhile stay
            with open(SPOOL_PATH) as fh:
                now = fh.read().splitlines()
            with open(SPOOL_PATH, "w") as fh:
                fh.writelines(ln + "\n" for ln in now[len(rows):])
        if len(lines) > len(rows):
            _log_kick.set()


# ---- calls (RAM only) ----

_calls: dict[str, dict] = {}
_sem: asyncio.Semaphore | None = None
_sem_size = 0
_loop: asyncio.AbstractEventLoop | None = None


class Busy(Exception):
    pass


def live_calls() -> list[str]:
    return [cid for cid in _calls if not cid.startswith("_")]


def call(cid: str | None, caller: str = "") -> tuple[str, dict]:
    idle = float(ivr_cfg().get("idle_s") or 1800)
    now = time.time()
    for k in [k for k, v in _calls.items() if now - v["t"] > idle]:
        _calls.pop(k, None)
    cid = (cid or "").strip()[:64] or uuid.uuid4().hex[:12]
    c = _calls.setdefault(cid, {"msgs": [], "t": now, "turns": 0, "caller": ""})
    c["t"] = now
    if caller:
        c["caller"] = caller.strip()[:40]
    return cid, c


def hangup(cid: str) -> bool:
    gone = _calls.pop(cid, None) is not None
    hub.apply_if_idle()
    return gone


def _slot() -> asyncio.Semaphore:
    global _sem, _sem_size
    n = max(1, int(ivr_cfg().get("max_concurrent") or 2))
    if _sem is None or n != _sem_size:
        _sem, _sem_size = asyncio.Semaphore(n), n
    return _sem


# ---- the model ----

_accepts: dict[str, int] = {}
_down: dict[str, float] = {}
_DOWN_S = 60.0
_UNREACHABLE = (openai.APIConnectionError, openai.APITimeoutError)


def _conns() -> list[dict]:
    """The local model first (fast fail), then the hub's chat default."""
    out = []
    own = hub.cfg.get("own")
    if own and time.time() - _down.get(LLM_URL, -_DOWN_S) >= _DOWN_S:
        out.append({**own, "base_url": LLM_URL, "fast_fail": True})
    if hub.cfg.get("fallback"):
        out.append(hub.cfg["fallback"])
    return out


def _client(conn: dict) -> openai.AsyncOpenAI:
    t = float(ivr_cfg().get("llm_timeout_s") or 30)
    return openai.AsyncOpenAI(base_url=conn["base_url"], api_key=conn.get("api_key") or "none",
                              timeout=httpx.Timeout(t, connect=2.0) if conn.get("fast_fail") else t,
                              max_retries=0 if conn.get("fast_fail") else 1)


def _retryable(e: Exception, conn: dict, eb: dict | None) -> Exception:
    if rejected(e, eb):
        logger.warning("%s rejected %s; trying the next thinking switch", conn["base_url"], ", ".join(eb))
    elif isinstance(e, _UNREACHABLE):
        if conn["base_url"] not in _down:
            logger.error("IVR model at %s unreachable (%s); falling back", conn["base_url"], e)
        _down[conn["base_url"]] = time.time()
    else:
        raise e
    e.base_url = conn["base_url"]
    return e


def _recovered(conn: dict):
    if _down.pop(conn["base_url"], None) is not None:
        logger.warning("IVR model at %s reachable again", conn["base_url"])


def _attempts():
    for conn in _conns():
        for j, eb in variants(_accepts, conn["base_url"]):
            yield conn, j, eb


def _messages(hist: list, said: str | None) -> list:
    m = [{"role": "system", "content": hub.cfg.get("system_prompt") or ""}, *hist]
    return m + [{"role": "user", "content": said}] if said is not None else m


async def reply_stream(cid: str, c: dict, said: str, ctl: dict):
    """Yields ("text", delta) as the model writes, then ("done", {...})."""
    sem = _slot()
    if sem.locked():
        raise Busy("all IVR lines busy")
    cfg = ivr_cfg()
    async with sem:
        c["msgs"] = window(c["msgs"], cfg)
        hist = list(c["msgs"])
        parts: list[str] = []
        err = None
        for conn, j, eb in _attempts():
            if err is not None and isinstance(err, _UNREACHABLE) and conn["base_url"] == err.base_url:
                continue
            try:
                stream = await _client(conn).chat.completions.create(
                    model=conn["model"], messages=_messages(hist, said), stream=True,
                    max_tokens=int(cfg.get("max_tokens") or 600),
                    temperature=conn.get("temperature", 0.7), extra_body=eb)
            except Exception as e:  # noqa: BLE001
                err = _retryable(e, conn, eb)
                continue
            try:
                async for chunk in stream:
                    if ctl.get("stop"):
                        break
                    t = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                    if t:
                        parts.append(t)
                        yield ("text", t)
                _accepts[conn["base_url"]] = j
                _recovered(conn)
                break
            except Exception as e:  # noqa: BLE001
                if parts:
                    raise  # the caller heard part of it: no do-over
                err = _retryable(e, conn, eb)
            finally:
                await stream.close()  # cut: cancels the in-flight request
        else:
            raise err or RuntimeError("no model configured (hub config not pulled yet?)")
        cut = bool(ctl.get("stop"))
        text = ((ctl.get("spoken") if cut else "".join(parts)) or "").strip()
        c["msgs"] += [{"role": "user", "content": said}, {"role": "assistant", "content": text or "…"}]
        c["turns"] += 1
        yield ("done", {"reply": text or FALLBACK_REPLY, "tools": [], "fallback": not text, "cut": cut})


# ---- voice ----

def _voice() -> dict:
    return hub.cfg.get("voice") or {"tts_provider": "pocket"}


def speak_pcm(text: str, stop: threading.Event | None = None):
    """mono s16le @ STREAM_RATE for `text`, as generated. Blocking."""
    v = _voice()
    clean = voice.prepare(text, {"voice": v})
    if not clean:
        return
    provider = v.get("tts_provider") or "gtts"
    if provider == "pocket":
        yield from voice.pocket_pcm(clean, v, stop)
    elif provider == "qwen":
        yield from voice.qwen_pcm(clean, v, stop)
    else:
        audio, _ = voice.synthesize(clean, {"voice": v})
        yield _ffmpeg(audio, [], ["-f", "s16le", "-ar", str(STREAM_RATE), "-ac", "1"])


def _ffmpeg(data: bytes, in_args: list, out_args: list) -> bytes:
    p = subprocess.run([FF, "-hide_banner", "-loglevel", "error", *in_args, "-i", "pipe:0",
                        *out_args, "pipe:1"], input=data, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise ValueError("audio conversion failed: " + p.stderr.decode(errors="replace")[-300:])
    return p.stdout


def speak_wav(text: str, rate: int) -> bytes:
    pcm = b"".join(speak_pcm(text))
    if rate != STREAM_RATE:
        pcm = _ffmpeg(pcm, ["-f", "s16le", "-ar", str(STREAM_RATE), "-ac", "1"],
                      ["-f", "s16le", "-ar", str(rate), "-ac", "1"])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def to_pcm16k(data: bytes, fmt: str) -> bytes:
    """Caller audio -> s16le mono 16 kHz. A 16 kHz mono PCM16 wav (what
    ivr_loop sends) skips ffmpeg."""
    if data[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(data)) as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2):
                    return w.readframes(w.getnframes())
        except wave.Error:
            pass
    if fmt == "pcm16k":
        return data
    in_args = {"pcm8k": ["-f", "s16le", "-ar", "8000", "-ac", "1"],
               "ulaw8k": ["-f", "mulaw", "-ar", "8000", "-ac", "1"],
               "alaw8k": ["-f", "alaw", "-ar", "8000", "-ac", "1"]}.get(fmt, [])
    return _ffmpeg(data, in_args, ["-f", "s16le", "-ar", "16000", "-ac", "1"])


# ---- speech-to-text: the local whisper service, the hub as fallback ----

_stt_down = 0.0


def transcribe(pcm: bytes) -> dict:
    global _stt_down
    timeout = 5.0 + len(pcm) / 32000 * 0.5
    if time.time() - _stt_down >= _DOWN_S:
        try:
            req = urllib.request.Request(STT_URL + "/stt", data=pcm,
                                         headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            if _stt_down:
                logger.warning("local STT reachable again")
                _stt_down = 0.0
            return {"text": (d.get("text") or "").strip(), "words": d.get("words") or [], "where": "local"}
        except (OSError, ValueError) as e:
            if not _stt_down:
                logger.error("local STT at %s failed (%s); using the hub's", STT_URL, e)
            _stt_down = time.time()
    with _hub_req("/api/stt", pcm, headers={"Content-Type": "application/octet-stream"},
                  timeout=timeout + 10) as r:
        d = json.load(r)
    return {"text": (d.get("text") or "").strip(), "words": d.get("words") or [], "where": "hub"}


# ---- streamed turn ----

async def stream_turn(cid: str, c: dict, said: str, t0: float):
    """One streamed turn, as LangBang's ivr.stream_turn: the model feeds
    sentences to one speaker thread, so synthesis of sentence 1 overlaps the
    model writing sentence 2."""
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
                for pcm in speak_pcm(text, stop):
                    emit(("audio", pcm))
        except Exception as e:  # noqa: BLE001
            logger.exception("stream TTS failed")
            emit(("tts_error", f"{type(e).__name__}: {e}"))
        finally:
            emit(("spoken", None))

    limit = int(ivr_cfg().get("max_spoken_words") or 0)
    ctl: dict = {"spoken": ""}

    def say(sentence: str) -> bool:
        if limit and ctl["spoken"] and len(ctl["spoken"].split()) + len(sentence.split()) > limit:
            ctl["stop"] = True
            return False
        todo.put(sentence)
        ctl["spoken"] = f"{ctl['spoken']} {sentence}".strip()
        return True

    async def think():
        split, result = Sentences(), None
        try:
            async for kind, val in reply_stream(cid, c, said, ctl):
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
                if ctl.get("stop"):
                    c["msgs"][-1] = {"role": "assistant", "content": ctl["spoken"]}
                    result = {**result, "reply": ctl["spoken"], "cut": True}
            if result["fallback"]:
                todo.put(result["reply"])
        except Exception as e:  # noqa: BLE001
            logger.exception("stream turn failed")
            result = {"reply": ERROR_REPLY, "tools": [], "error": f"{type(e).__name__}: {e}"}
            todo.put(ERROR_REPLY)
        finally:
            todo.put(None)
            await out.put(("thought", result))

    threading.Thread(target=speaker, daemon=True, name=f"tts-{cid}").start()
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
        ev = {"type": "done", "reply": result["reply"], "tools": [], "timings": timings}
        if result.get("cut"):
            ev["cut"] = True
        err = result.get("error") or (f"TTS failed: {tts_error}" if tts_error else None)
        if err:
            ev["error"] = err
        yield ev
    finally:
        stop.set()
        todo.put(None)
        if not task.done():
            task.cancel()


# ---- warm-up ----

async def prewarm(with_voice: bool = True) -> None:
    """Pay cold costs before a caller does: the voice, then the prompt
    prefix — the system prompt ALONE, one token, so Ollama checkpoints its
    cache exactly where a caller's first turn begins (see LangBang's
    ivr.prewarm for why a 'Hello?' warm-up misses)."""
    if not hub.cfg:
        return
    t0 = time.time()
    if with_voice:
        try:
            await run_in_threadpool(lambda: sum(len(b) for b in speak_pcm("Hello.")))
        except Exception as e:  # noqa: BLE001
            logger.warning("prewarm (voice) failed: %s", e)
    t1 = time.time()
    err = None
    for conn, j, eb in _attempts():
        if err is not None and isinstance(err, _UNREACHABLE) and conn["base_url"] == err.base_url:
            continue
        try:
            await _client(conn).chat.completions.create(
                model=conn["model"], messages=_messages([], None), max_tokens=1,
                temperature=conn.get("temperature", 0.7), extra_body=eb)
            _accepts[conn["base_url"]] = j
            _recovered(conn)
            break
        except Exception as e:  # noqa: BLE001
            try:
                err = _retryable(e, conn, eb)
            except Exception as e2:  # noqa: BLE001
                logger.warning("prewarm (model) failed: %s", e2)
                break
    logger.warning("prewarm: voice %.1fs, model %.1fs", t1 - t0, time.time() - t1)


# ---- HTTP ----

app = FastAPI(title="IVR node")


@app.on_event("startup")
async def _startup():
    global _loop
    _loop = asyncio.get_running_loop()
    await run_in_threadpool(hub.pull)       # fresh config if the hub answers; cached otherwise
    threading.Thread(target=hub.loop, daemon=True, name="hub-sync").start()
    threading.Thread(target=_push_loop, daemon=True, name="hub-push").start()
    asyncio.create_task(prewarm())


def _gate(request: Request) -> dict:
    cfg = ivr_cfg()
    if not hub.cfg:
        raise HTTPException(503, "no config from the hub yet")
    if not cfg.get("enabled", True):
        raise HTTPException(403, "IVR disabled (hub ivr.enabled)")
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, token()):
        raise HTTPException(401, "bad or missing bearer token")
    return cfg


async def _hear(request: Request, fmt: str) -> tuple[str, bool, list]:
    body = await request.body()
    if (request.headers.get("content-type") or "").startswith("application/json"):
        try:
            return str(json.loads(body or b"{}").get("text") or "").strip(), False, []
        except ValueError:
            raise HTTPException(400, "bad JSON body")
    if not body:
        raise HTTPException(400, "empty audio body")
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large (max 10 MB)")
    try:
        pcm = await run_in_threadpool(to_pcm16k, body, (fmt or "auto").lower())
        d = await run_in_threadpool(transcribe, pcm)
        return d["text"], True, d.get("words") or []
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"STT failed: {e}")


@app.get("/api/ivr/status")
async def status():
    now = time.time()
    return {"node": NAME, "hub": {"url": HUB, "etag": hub.etag, "pending": hub.pending is not None,
                                  "last_ok": hub.last_ok or None, "error": hub.last_error or None},
            "active": [{"id": cid, "turns": c.get("turns", 0), "caller": c.get("caller", ""),
                        "idle_s": int(now - c["t"])} for cid, c in _calls.items() if not cid.startswith("_")],
            "llm_down": sorted(_down), "stt_down": bool(_stt_down)}


@app.post("/api/ivr/start")
async def ivr_start(request: Request, call_id: str = "", out: str = "wav", rate: int = 8000, caller: str = ""):
    cfg = _gate(request)
    if out != "wav":
        raise HTTPException(400, "this node returns out=wav only")
    cid, _ = call(call_id, caller)
    greet = cfg.get("greeting") or ""
    audit({"call_id": cid, "event": "start", "ip": request.client.host if request.client else None})
    audio = {}
    if greet:
        t = time.time()
        try:
            wav = await run_in_threadpool(speak_wav, greet, rate)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"TTS failed: {e}")
        audio = {"audio_b64": base64.b64encode(wav).decode(), "audio_format": "wav",
                 "sample_rate": rate, "tts_s": round(time.time() - t, 2)}
    asyncio.create_task(prewarm(with_voice=False))
    return {"call_id": cid, "reply": greet, **audio}


@app.post("/api/ivr/turn/stream")
async def ivr_turn_stream(request: Request, call_id: str = "", fmt: str = "auto", caller: str = "",
                          reply: int = 1):
    _gate(request)
    cid, c = call(call_id, caller)
    t0 = time.time()
    said, stt, words = await _hear(request, fmt)
    stt_s = round(time.time() - t0, 2) if stt else None
    if said and reply and _slot().locked():
        raise HTTPException(503, "all IVR lines busy")

    def line(ev: dict) -> bytes:
        return (json.dumps(ev) + "\n").encode()

    async def events():
        yield line({"type": "transcript", "call_id": cid, "text": said, "stt_s": stt_s})
        if said and not reply:
            audit({"call_id": cid, "event": "hear", "transcript": said, "stt_s": stt_s})
            yield line({"type": "done", "reply": "", "tools": [], "timings": {"stt_s": stt_s}})
            return
        if not said:
            audit({"call_id": cid, "event": "turn", "stream": True, "transcript": "", "empty": True,
                   "stt_s": stt_s})
            yield line({"type": "done", "empty": True, "reply": "", "tools": [], "timings": {"stt_s": stt_s}})
            return
        async for ev in stream_turn(cid, c, said, t0):
            if ev["type"] == "audio":
                ev = {"type": "audio", "pcm": base64.b64encode(ev["pcm"]).decode()}
            elif ev["type"] == "done":
                ev["timings"]["stt_s"] = stt_s
                audit({"call_id": cid, "event": "turn", "stream": True, "transcript": said,
                       "reply": ev["reply"], "tools": [], "error": ev.get("error"),
                       **({"words": words[:80]} if words else {}), **ev["timings"]})
            yield line(ev)

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ivr/hangup")
async def ivr_hangup(request: Request, call_id: str = ""):
    _gate(request)
    if not call_id:
        try:
            call_id = str(json.loads(await request.body() or b"{}").get("call_id") or "")
        except ValueError:
            pass
    if not call_id:
        raise HTTPException(400, "call_id required")
    audit({"call_id": call_id, "event": "hangup"})
    return {"ok": hangup(call_id)}


@app.post("/api/stt")
async def stt(request: Request):
    """16 kHz mono PCM16 (bare or RIFF) -> {"text", "words"} (ivr_report)."""
    _gate(request)
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty audio body")
    try:
        d = await run_in_threadpool(transcribe, await run_in_threadpool(to_pcm16k, body, "pcm16k"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    return {"text": d["text"], "words": d["words"], "provider": "whisper"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("IVR_NODE_BIND", "0.0.0.0"),
                port=int(os.environ.get("IVR_NODE_PORT", "8130")), log_level="warning")
