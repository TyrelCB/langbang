#!/usr/bin/env python3
"""ivr_loop.py — listen/answer loop for the Google Voice IVR rig.

The caller's audio (downlink) is tapped from webrtc_render.monitor by ONE
ffmpeg process for the whole call, so nothing is lost between turns. Each
utterance goes to LangBang's streaming IVR endpoint, which does STT + the
sandboxed agent + Pocket TTS and streams the reply back as it is generated:

  POST /api/ivr/start                        -> call_id + greeting (wav)
  POST /api/ivr/turn/stream?call_id=&fmt=wav -> NDJSON: transcript, text…,
                                                audio… (s16le 24 kHz), done
       …&reply=0                             -> transcript only (answer detection)
  POST /api/ivr/hangup?call_id=

Reply audio is played as it streams in (pipewire: `pacat -d voice_prompt`
-> mic_mix -> ivr_mic -> the call; mac: BlackHole 16ch -> Chrome's mic; see
ivr_audio_io.py), so the caller hears the first sentence while the rest is
still being written. If the caller talks over a reply (barge-in) playback
stops, the stream is dropped (the server stops synthesizing) and what they
said becomes the next turn.

Answer detection is WORD-gated: an utterance only counts as the callee
answering when STT finds words in it, so ringback and the ~0.5 s service
beeps never trigger the greeting. The callee's "Hello?" gets the greeting —
it is not sent to the agent as a question.

Usage: ivr_loop.py [--max-turns N] [--watch-secs N] [--answer-secs N] [--no-barge-in]
Env: IVR_BASE (default https://ai.tyrel.cloud), IVR_TOKEN_FILE, IVR_DOWNLINK,
     IVR_PLAY_SINK, IVR_SAVE_DIR (keep every uploaded utterance as a wav),
     IVR_AUDIO (pipewire|mac, default by platform), IVR_NO_RECORD=1,
     IVR_NO_GV=1 (no browser: skip the GV hang-up), IVR_NO_REPORT=1.
"""
import argparse
import base64
import collections
import io
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from array import array

import ivr_audio_io as aio

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("IVR_BASE", "https://ai.tyrel.cloud")
TOKEN_FILE = os.environ.get("IVR_TOKEN_FILE", os.path.join(HERE, ".ivr_token"))
DOWNLINK = os.environ.get("IVR_DOWNLINK", aio.DEFAULT_DOWNLINK)     # what the caller says
PLAY_SINK = os.environ.get("IVR_PLAY_SINK", aio.DEFAULT_PLAY_SINK)  # what the caller hears from us
SAVE_DIR = os.environ.get("IVR_SAVE_DIR", os.path.join(HERE, "recordings", "utterances"))
RECORD_DIR = os.environ.get("IVR_RECORD_DIR", os.path.join(HERE, "recordings"))

SR = 16000                  # downlink capture / upload rate
FRAME_S = 0.02              # VAD frame
# "voice" = frame RMS this far above the line's noise floor, clamped to a range:
# a dead-quiet digital line (WebRTC sends silence as zeros) would otherwise make
# any click speech, and a hissy one would never let speech through
SPEECH_OVER_FLOOR = 15.0
SPEECH_DB_MIN, SPEECH_DB_MAX = -55.0, -40.0
START_WIN_S = 0.3           # an utterance starts when, within this window…
START_S = 0.16              # …this much is voiced (syllable gaps don't reset it; clicks don't count)
END_S = 1.2                 # this much quiet ends it. 0.8 (2026-10-16) and 1.0 (2026-10-26:
                            # "Okay, let's...", "It's breaking up a little bit. Is that...")
                            # both cut callers off mid-thought and replied over them
PREROLL_S = 0.3             # audio kept from before the start, so onsets aren't clipped
MIN_UTTER_S = 0.4
MAX_UTTER_S = 15.0
# Barge-in wants ~1 s of sustained speech: at 0.42 s in 0.6 s, room noise,
# our own echo off the caller's phone and a bare "hello?" all cut replies
# mid-sentence (22 of 36 replies on 2026-10-16), and the agent then answered
# the fragment. A real interruption is longer than that.
BARGE_WIN_S = 1.0           # talking over a reply: within this window…
BARGE_S = 0.8               # …this much voiced, 6 dB above the speech threshold
BARGE_OVER = 6.0
BARGE_GRACE_S = 0.8         # a reply can't be interrupted this soon after it starts: a caller
                            # who said "hello?" just as we began stops once they hear us
TARGET_PEAK_DB = -3.0       # quiet utterances are normalized up to this before STT
MAX_GAIN_DB = 24.0
REPLY_RATE = 24000          # streamed reply audio: s16le mono

FRAME_BYTES = int(SR * FRAME_S) * 2


def now():
    return time.strftime("%H:%M:%S")


def log(*a):
    print(now(), *a, flush=True)


def token():
    with open(TOKEN_FILE) as fh:
        return fh.read().strip()


def frame_db(frame: bytes) -> float:
    s = array("h", frame)
    if not s:
        return -120.0
    rms = math.sqrt(sum(x * x for x in s) / len(s))
    return 20 * math.log10(max(rms, 1.0) / 32768.0)


# ---- full-call recorder ----------------------------------------------------

# ---- downlink tap ----------------------------------------------------------

class Downlink:
    """One ffmpeg tap on the caller's audio for the whole call. Frames
    (bytes, dB) go into a queue; the loop decides what is speech."""

    def __init__(self, source: str):
        self.q: queue.Queue = queue.Queue()
        self.p = aio.capture(source, SR, FRAME_BYTES * 2)
        # recent frames: pre-roll for a barge-in (covers the BARGE_S that triggered it)
        self.ring = collections.deque(maxlen=int((PREROLL_S + BARGE_WIN_S) / FRAME_S))
        self.floor = -60.0          # running noise-floor estimate (dB)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            f = self.p.read(FRAME_BYTES)
            if len(f) < FRAME_BYTES:
                self.q.put(None)
                return
            self.q.put((f, frame_db(f)))

    def frame(self, timeout: float):
        """Next (bytes, dB), or None on timeout. Raises EOFError if the tap died."""
        try:
            item = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise EOFError("downlink tap ended")
        self.ring.append(item[0])
        # floor: follows quiet frames down fast, creeps up slowly through speech
        db = max(item[1], -90.0)
        self.floor = db if db < self.floor else self.floor + 0.002 * (db - self.floor)
        return item

    def threshold(self, over: float = 0.0) -> float:
        return min(max(self.floor + SPEECH_OVER_FLOOR, SPEECH_DB_MIN), SPEECH_DB_MAX) + over

    def drain(self) -> list[float]:
        """Drop frames queued while we weren't listening (keep them as pre-roll).
        Returns their dB levels, oldest first."""
        dbs = []
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return dbs
            if item is None:
                raise EOFError("downlink tap ended")
            self.ring.append(item[0])
            dbs.append(item[1])

    def close(self):
        self.p.close()


def listen(dl: Downlink, deadline_s: float, started_with: list | None = None):
    """Return PCM of the next utterance, or None if nobody spoke in time.
    `started_with`: frames already captured (barge-in) — continue that utterance."""
    end = time.time() + deadline_s
    listen.peak = -120.0                     # loudest frame while waiting (for "why no speech?")
    speech: list[bytes] = list(started_with or [])
    win = collections.deque(maxlen=int(START_WIN_S / FRAME_S))   # recent (frame, voiced)
    quiet = 0
    while True:
        if not speech and time.time() > end:
            return None
        item = dl.frame(0.2)
        if item is None:
            continue
        f, db = item
        listen.peak = max(listen.peak, db)
        voiced = db > dl.threshold()
        if not speech:                       # waiting for a start
            win.append((f, voiced))
            if sum(v for _, v in win) * FRAME_S >= START_S:
                pre = list(dl.ring)[-int((START_WIN_S + PREROLL_S) / FRAME_S):]
                speech, quiet = pre, 0
                win.clear()
            continue
        speech.append(f)
        quiet = 0 if voiced else quiet + 1
        dur = len(speech) * FRAME_S
        if quiet * FRAME_S >= END_S or dur >= MAX_UTTER_S:
            if dur - quiet * FRAME_S >= MIN_UTTER_S:
                return b"".join(speech)
            speech = []                      # a blip: keep waiting


GREET_QUIET_S = 0.5   # after GV's short "connected" tone, this much quiet = the line is ours
# no tone heard by then: it played before the tap opened. Was 2.5: on 17 of
# 23 calls (10-16..10-23) no tone came at all and the caller sat through the
# full 2.5 s of dead air; a tone that does come starts the GREET_QUIET_S wait.
GREET_NO_TONE_S = 1.0
GREET_MAX_S = 10.0


def wait_line_quiet(dl: Downlink) -> float:
    """Inbound, just accepted: wait until GV's connect tone (or any prompt)
    is over — GREET_QUIET_S of quiet after it, or GREET_NO_TONE_S if no tone
    comes (it already played), at most GREET_MAX_S — and return how long
    that took. The tone's time varies: ~1 s after the tap on one call,
    ~1.9 s on another, before the tap on a third; a fixed 1.5 s greeted over
    it (call 888ec25aa339: "the voice sounded off"). Waiting for a full
    utterance end plus 1.2 s of silence instead put ~2.4 s of dead air after
    it (call cadf0304590c)."""
    # timed in frames since the tap opened (20 ms each, real time): frames
    # queued while the greeting was synthesized count too
    n = last = 0
    heard = False
    while True:
        t, quiet = n * FRAME_S, (n - last) * FRAME_S
        if t >= GREET_MAX_S or (heard and quiet >= GREET_QUIET_S) or (not heard and t >= GREET_NO_TONE_S):
            return t
        item = dl.frame(0.5)
        if item is None:
            continue
        n += 1
        if item[1] > dl.threshold():
            last, heard = n, True


def sounds_like_speech(pcm: bytes, thr: float) -> bool:
    """A burst STT found no words in: speech or a tone? Measured on real
    downlink recordings: ringback/service beeps are fully voiced at a steady
    level (loudness std 0.8–4.2 dB); speech has gaps and swings (std 5–11 dB).
    Lets a bare "Hello?" — which free STT often returns nothing for — count
    as the callee answering."""
    dbs = [frame_db(pcm[i:i + FRAME_BYTES]) for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    voiced = [d for d in dbs if d > thr]
    if len(voiced) < int(0.2 / FRAME_S):
        return False
    mean = sum(voiced) / len(voiced)
    std = math.sqrt(sum((d - mean) ** 2 for d in voiced) / len(voiced))
    return len(voiced) / len(dbs) < 0.9 or std >= 5.0


def normalize(pcm: bytes) -> bytes:
    """Peak-normalize a quiet utterance (up to MAX_GAIN_DB): phone-band audio
    at -40 dB is where free STT starts returning nothing."""
    s = array("h", pcm)
    peak = max((abs(x) for x in s), default=0)
    if not peak:
        return pcm
    gain = min(10 ** ((TARGET_PEAK_DB - 20 * math.log10(peak / 32768)) / 20), 10 ** (MAX_GAIN_DB / 20))
    if gain <= 1.0:
        return pcm
    return array("h", (max(-32768, min(32767, int(x * gain))) for x in s)).tobytes()


def wav_bytes(pcm: bytes, rate: int = SR) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---- LangBang --------------------------------------------------------------

def request(path: str, body: bytes = b"", ctype: str = "application/json", timeout: float = 90):
    """POST and return the open response. Retries only failures that happen
    before the server could have acted (auth race, busy, connection refused)."""
    last = None
    for attempt in range(3):
        req = urllib.request.Request(BASE + path, data=body, headers={
            "Authorization": "Bearer " + token(), "content-type": ctype})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (401, 503):
                raise
        except (urllib.error.URLError, ConnectionRefusedError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last


def api(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    with request(path, json.dumps(payload or {}).encode(), timeout=timeout) as r:
        return json.load(r)


def open_turn(cid: str, pcm: bytes | None = None, text: str | None = None, reply: bool = True):
    """Start a streamed turn: returns (response, transcript event)."""
    q = f"/api/ivr/turn/stream?call_id={cid}&fmt=wav" + ("" if reply else "&reply=0")
    if text is not None:
        r = request(q, json.dumps({"text": text}).encode())
    else:
        body = wav_bytes(normalize(pcm))
        if SAVE_DIR:
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(os.path.join(SAVE_DIR, f"{time.strftime('%H%M%S')}-{cid}.wav"), "wb") as fh:
                fh.write(body)
        r = request(q, body, ctype="application/octet-stream")
    first = json.loads(r.readline())
    return r, first


# ---- playback --------------------------------------------------------------

def play(dl: Downlink, source, barge_in: bool, prefix: bytes = b""):
    """Play reply audio while watching the downlink for barge-in.
    `source` is a streamed-turn response (NDJSON) or raw PCM bytes; `prefix`
    (raw PCM) is played first, while the stream is already being read.
    Returns (done-event or {}, first_audio_time, barge frames or None)."""
    player = aio.playback(PLAY_SINK, REPLY_RATE)
    result: dict = {}
    events: queue.Queue = queue.Queue()

    def pump():
        try:
            for i in range(0, len(prefix), 9600):
                events.put(("audio", prefix[i:i + 9600]))
            if isinstance(source, bytes):
                for i in range(0, len(source), 9600):
                    events.put(("audio", source[i:i + 9600]))
            else:
                for line in source:
                    ev = json.loads(line)
                    if ev["type"] == "audio":
                        events.put(("audio", base64.b64decode(ev["pcm"])))
                    elif ev["type"] == "done":
                        events.put(("done", ev))
        except Exception as e:  # noqa: BLE001 - dropped stream ends the turn
            events.put(("done", {"error": f"stream: {e}"}))
        finally:
            events.put(("eof", None))

    threading.Thread(target=pump, daemon=True).start()
    dl.drain()
    win = collections.deque(maxlen=int(BARGE_WIN_S / FRAME_S))
    eof = False
    while True:
        while True:                          # move whatever arrived into the player
            try:
                kind, val = events.get_nowait()
            except queue.Empty:
                break
            if kind == "audio":
                player.write(val)
            elif kind == "done":
                result = val
            else:
                eof = True
                player.finish()
        if eof and player.done():
            play.ended = time.time()
            return result, player.started, None
        item = dl.frame(0.02)
        if item is None or not barge_in or player.started is None or \
                time.time() - player.started < BARGE_GRACE_S:
            continue
        win.append(item[1] > dl.threshold(BARGE_OVER))
        if sum(win) * FRAME_S >= BARGE_S:
            player.stop()
            if not isinstance(source, bytes):
                source.close()               # server sees the disconnect and stops TTS
            play.ended = time.time()
            return result, player.started, list(dl.ring)


play.ended = 0.0                             # when our last reply audio stopped (echo timing)


def wav_pcm(b64: str) -> bytes:
    """Greeting wav (asked for at REPLY_RATE) -> raw PCM."""
    with wave.open(io.BytesIO(base64.b64decode(b64))) as w:
        if w.getframerate() != REPLY_RATE or w.getnchannels() != 1:
            raise ValueError(f"greeting is {w.getframerate()} Hz x{w.getnchannels()}, want {REPLY_RATE} mono")
        return w.readframes(w.getnframes())


# ---- the call --------------------------------------------------------------

HELLO_WORDS = {"hello", "hi", "hey", "yes", "yeah", "yep", "speaking", "this", "is", "it's", "who's",
               "whos", "there", "good", "morning", "afternoon", "evening", "oh", "um", "uh", "ok", "okay"}


def is_question(text: str) -> bool:
    """Did the callee's answering utterance carry more than "hello, this is X"?"""
    words = [w.strip(".,!?'\"").lower() for w in text.split()]
    return len([w for w in words if w and w not in HELLO_WORDS]) >= 3


ECHO_STOPWORDS = set(
    "i me my we our you your he she it its they them this that these those "
    "a an the is are was were be been am do does did done have has had having "
    "will would can could should may might must shall not no yes and or but if "
    "then so as at of in on to for from by with about about what when where who "
    "how why all any some just very really okay yeah yes uh um oh ah hi hello "
    "there here please thanks thank".split())


def same_words(a: str, b: str) -> bool:
    """Is transcript `a` mostly our own reply `b` coming back (echo)?

    Content-word comparison only: a caller question like "do you have
    experience with switching and ethernet?" shares nothing but function
    words with a reply that ends "do you have any questions about..." —
    counting function words made that REAL question look like 66% echo and
    the caller heard dead air (call 79772a3889d1, 11:24:37). A genuine
    echo carries our content words back, so require >=2 shared content
    words AND >=80% content-word coverage. 60% threw away a caller
    answering in our own words: "Yeah, tell me about your background and
    skills." after "...questions about my background or skills?" is 2 of 3
    (call 9e95a05395cc, ignored twice). Callers do that all the time; an
    echo brings back nearly every word."""
    # word chars only: "EVPN-VXLAN" in our reply comes back as "EVPN VXLAN"
    ca = {w for w in re.findall(r"[a-z0-9']+", a.lower()) if w not in ECHO_STOPWORDS}
    cb = set(re.findall(r"[a-z0-9']+", b.lower()))
    if len(ca) < 2:
        return False
    shared = ca & cb
    return len(shared) >= 2 and len(shared) / len(ca) >= 0.8


# echo returns while we speak (a few hundred ms behind us); an utterance
# that started later than this after our audio stopped is the caller,
# whatever its words. 1.5 dropped "You build generative artificial
# intelligence end to end now?" — the caller quoting our reply back as a
# question 1.5 s after it (suite replay of c8d18315086e, 10-26).
ECHO_TAIL_S = 0.8


def is_echo(said: str, began: float, spoken: list) -> bool:
    """`said` started at `began` (epoch s): our own recent reply coming back?"""
    return began < play.ended + ECHO_TAIL_S and any(same_words(said, s) for s in spoken[-3:])


import re
FAREWELL_RE = re.compile(
    r"\b(goodbye|bye[- ]?bye|bye for now|good night|that(?:'s| is) all\b"
    r"|talk to you later|i(?:'m| am) (?:all )?done|hang(?:ing)? up now)\b", re.I)
# sign-offs that only end a call as the LAST words: "Okay, thanks. Bye." (call
# 7a425e53a2c4 kept going after it), but not "bye" or "have a good one" mid-sentence
SIGNOFF_RE = re.compile(
    r"\b(bye|have a (?:good|great|nice) (?:one|day|night|evening|weekend))\W*$", re.I)


# GV's inbound screening announcement ("Call from Jenny. To accept, press 1…")
# plays on the downlink before the caller is connected — never a caller turn
# "Plus one, seven, five." = GV reading the caller ID (call 28a38a02a121)
GV_SCREEN_RE = re.compile(r"^\W*(?:incoming )?call from\b|\bpress (?:1|one)\b|to accept (?:this|the) call|"
                          r"send (?:it |them )?to voicemail|"
                          r"^\W*plus one(?:\W+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|\d+))+\W*$",
                          re.I)


def says_goodbye(text: str) -> bool:
    """Did the caller try to END the call? Matched on their transcript only
    (never on our own reply), and only near the end of the sentence, so a
    mid-conversation 'goodbye' in a sentence like 'before you say goodbye...'
    doesn't kill the call."""
    m = FAREWELL_RE.search(text)
    return bool(m and m.start() >= len(text) - 40) or bool(SIGNOFF_RE.search(text))


def respond(dl: Downlink, r, heard: float, spoken: list, barge: bool, prefix: bytes = b""):
    """Play one streamed reply (after `prefix` audio, if any) and log it;
    returns barge-in frames or None."""
    done, first_audio, carry = play(dl, r, barge, prefix)
    tm = done.get("timings", {})
    lag = f"{first_audio - heard:.2f}s" if first_audio else "no audio"
    spoken.append(done.get("reply") or "")
    log(f"said: {(done.get('reply') or '')[:120]!r}" + (" [interrupted]" if carry else ""))
    log(f"  first audio {lag} after they stopped talking "
        f"(server: stt {tm.get('stt_s')} text {tm.get('first_text_s')} audio {tm.get('first_audio_s')} "
        f"total {tm.get('total_s')}s) tools {done.get('tools')}" + (f" ERROR {done['error']}" if done.get("error") else ""))
    return carry


def answer_loop(dl: Downlink, cid: str, g: dict, spoken: list, barge: bool,
                args, carry, heard: float = 0.0, opening: bool = False):
    """The conversation phase shared by outbound (after answer-detection) and
    inbound (right after the greeting). `carry` = barge-in frames to prepend
    to the first listen; `heard` = when the caller's opening line ended;
    `opening` = the caller hasn't spoken yet (inbound, just greeted)."""
    turns = 0
    nudges = 0                       # blank-STT "can you repeat that?" budget
    checked = False                  # asked "are you still there?" and still waiting
    while turns < args.max_turns:
        utt = listen(dl, args.watch_secs, started_with=carry)
        carry = None
        if utt is None and not checked:
            # quiet: check in once. A caller who hung up leaves the line silent
            # (GV gives no hang-up signal here), so this also ends dead calls in
            # ~2x watch_secs instead of the 90 s of dead air it used to take
            log(f"{args.watch_secs}s of quiet (loudest {listen.peak:.0f} dB, speech is > {dl.threshold():.0f}) — asking if they're still there")
            r, _ = open_turn(cid, text="(The caller has been silent for a while. Briefly ask "
                                       "whether they are still there, in a few words.)")
            carry = respond(dl, r, time.time(), spoken, barge)
            checked = True
            continue
        if utt is None:
            log(f"no answer to the check-in after {args.watch_secs}s — saying goodbye, hanging up")
            r, _ = open_turn(cid, text="(The caller didn't answer. Say a brief, friendly goodbye.)")
            play(dl, r, barge_in=False)
            return
        checked = False
        heard = time.time()
        try:
            r, tr = open_turn(cid, utt)
        except urllib.error.HTTPError as e:
            log("turn http", e.code, "(all lines busy)" if e.code == 503 else e.read()[:160])
            continue
        said = (tr.get("text") or "").strip()
        if not said and opening and turns == 0 and nudges == 0 and \
                len(utt) / 2 / SR < 2.0 and sounds_like_speech(utt, dl.threshold()):
            # the caller's first sound after our greeting, too short for STT:
            # a bare "hello?". Answer it as one — "could you repeat that?" as
            # the opening reply derails the call (2c8d76525477, 2026-10-16)
            r.close()
            log(f"caller: {len(utt) / 2 / SR:.1f}s STT had no words for — taking it as 'Hello?'")
            r, _ = open_turn(cid, text="Hello?")
            carry = respond(dl, r, heard, spoken, barge)
            turns += 1
            continue
        if not said:
            r.close()
            # A burst with no words: stay silent for noise/beeps, but if it
            # swung like speech the caller IS talking and deserves an answer —
            # dead air after a question reads as a broken line (call
            # 79772a3889d1: quiet question, whisper blank, caller: "are you
            # still there?"). Capped at 2 per call so echo/noise loops can't
            # make us nag.
            if nudges < 2 and sounds_like_speech(utt, dl.threshold()):
                nudges += 1
                r, _ = open_turn(
                    cid, text="(The caller said something the speech "
                              "recognizer could not hear. Politely ask them "
                              "to repeat it, in one short sentence.)")
                respond(dl, r, heard, spoken, barge)
            else:
                log("no words in that (noise/beep) — still listening")
            continue
        if GV_SCREEN_RE.search(said):
            r.close()
            log(f"Google Voice's own call-screening prompt ({said!r}) — ignoring")
            continue
        # utt = pre-roll + speech + the END_S of quiet that closed it
        began = heard - len(utt) / 2 / SR + PREROLL_S
        if is_echo(said, began, spoken):
            r.close()
            log(f"echo of our own reply ({said!r}) — ignoring")
            continue
        log(f"caller: {said!r}")
        carry = respond(dl, r, heard, spoken, barge)
        turns += 1
        if says_goodbye(said):
            # they tried to end it: the reply above was our goodbye, so
            # return -> teardown frees the session AND hangs up the GV leg
            log("caller said goodbye — closing the call")
            return
    log("turn limit reached — saying goodbye")
    r, _ = open_turn(cid, text="(We're out of time for this call. Say a brief, friendly goodbye.)")
    play(dl, r, barge_in=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--watch-secs", type=int, default=10,
                    help="quiet this long mid-call: ask if they're still there; as long again with no answer: hang up")
    ap.add_argument("--answer-secs", type=int, default=60, help="wait this long for someone to answer")
    ap.add_argument("--no-barge-in", action="store_true")
    ap.add_argument("--greet-first", action="store_true",
                    help="inbound call already answered: skip answer-detection, "
                         "play the greeting now and start the conversation")
    ap.add_argument("--await-go", action="store_true",
                    help="started at the ring: set up the call, then wait for a "
                         "'go' line on stdin (accepted); anything else drops it")
    args = ap.parse_args()
    barge = not args.no_barge_in

    # tap first: the line is heard (GV's connect tone timed) while the
    # greeting is synthesized, instead of after it
    dl = Downlink(DOWNLINK)
    # IVR_CALL_ID: tests name their calls (suite-…) so the call log can tell them apart
    cid_q = f"&call_id={os.environ['IVR_CALL_ID']}" if os.environ.get("IVR_CALL_ID") else ""
    g = api(f"/api/ivr/start?out=wav&rate={REPLY_RATE}{cid_q}")
    cid = g["call_id"]
    greeting = wav_pcm(g["audio_b64"]) if g.get("audio_b64") else b""
    log("call", cid, f"(greeting synth {g.get('tts_s')}s) — tapping {DOWNLINK}, replies -> {PLAY_SINK}")
    if args.await_go:
        # ivr_answer.py started us at the Answer click: everything above ran
        # while GV played its press-1 menu. Hold here until it says accepted.
        log("ready — waiting for the accept")
        cmd = sys.stdin.readline().strip()
        if cmd != "go":
            log(f"not accepted ({cmd or 'eof'}) — dropping call {cid}")
            dl.close()
            try:
                api(f"/api/ivr/hangup?call_id={cid}", timeout=15)
            except Exception as e:  # noqa: BLE001
                log("hangup warn:", e)
            return
        # the menu audio is GV's, not the caller's: drop it, and log how
        # recently it was sounding so GREET_NO_TONE_S can be tuned on data
        pre = dl.drain()
        voiced = [i for i, db in enumerate(pre) if db > dl.threshold()]
        last = f"{(len(pre) - 1 - voiced[-1]) * FRAME_S:.1f}s before" if voiced else "none in"
        log(f"accepted — dropped {len(pre) * FRAME_S:.1f}s of menu audio (last sound {last} it)")
    rec = None
    if os.environ.get("IVR_NO_RECORD") != "1":
        rec = aio.Recorder(os.path.join(
                               RECORD_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{cid}.m4a"),
                           DOWNLINK, PLAY_SINK)
        log("recording ->", rec.path)
    spoken: list[str] = []
    try:
        # 0. inbound (answered by ivr_answer.py): no ringback to wait out —
        # but GV itself is still talking through its connect/press-1 prompts
        # on the remote leg; speak only once the line goes quiet (or 10s in)
        if args.greet_first:
            waited = wait_line_quiet(dl)
            log(f"line quiet after {waited:.1f}s — greeting")
            if greeting:
                _, _, carry0 = play(dl, greeting, barge)
                spoken.append(g.get("reply") or "")
                log("greeted (inbound)" + (" (caller talked over it)" if carry0 else ""))
                answer_loop(dl, cid, g, spoken, barge, args, carry0, opening=True)
                return
            answer_loop(dl, cid, g, spoken, barge, args, None, opening=True)
            return
        # 1. wait for a human: words, not ringback
        deadline = time.time() + args.answer_secs
        while True:
            utt = listen(dl, max(0.0, deadline - time.time()))
            if utt is None:
                log(f"nobody answered (loudest {listen.peak:.0f} dB) — ending")
                return
            heard = time.time()
            r, tr = open_turn(cid, utt, reply=False)
            r.close()
            if tr.get("text"):
                answer = tr["text"]
                log(f"answered: {answer!r} (stt {tr.get('stt_s')}s)")
                break
            if sounds_like_speech(utt, dl.threshold()):
                answer = ""
                log(f"answered: {len(utt) / 2 / SR:.1f}s of speech STT had no words for (a bare hello?)")
                break
            log(f"{len(utt) / 2 / SR:.1f}s burst, no words, steady tone (ringback/beep) — still waiting")

        # 2. greet, then converse
        if answer and is_question(answer):
            log(f"caller: {answer!r} (their opening line)")
            r, _ = open_turn(cid, text=answer)
            spoken.append(g.get("reply") or "")
            carry = respond(dl, r, heard, spoken, barge, prefix=greeting)
            answer_loop(dl, cid, g, spoken, barge, args, carry, heard)
            return
        carry = None
        if greeting:
            _, _, carry = play(dl, greeting, barge)
            spoken.append(g.get("reply") or "")
            log("greeted" + (" (caller talked over it)" if carry else ""))
        answer_loop(dl, cid, g, spoken, barge, args, carry)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        dl.close()
        try:
            api(f"/api/ivr/hangup?call_id={cid}", timeout=15)
            log("hung up")
        except Exception as e:  # noqa: BLE001
            log("hangup warn:", e)
        # the server session is gone, but the phone leg is GV's: click its
        # "Hang up call" button in the agent browser or the caller sits on a
        # dead line (unconditional and no-op-safe; see ivr_hangup.py)
        if os.environ.get("IVR_NO_GV") != "1":
            subprocess.run([sys.executable, os.path.join(HERE, "ivr_hangup.py")],
                           timeout=15, capture_output=True)
            log("GV leg teardown done")
        closed = rec is not None and rec.close()
        if closed:
            log("recording closed:", rec.path)
        if closed and os.environ.get("IVR_NO_REPORT") != "1":
            # detached: emailing an 8-minute call took 54 s, and while it ran
            # ivr_answer couldn't pick up — a second call rang out (2026-10-16)
            try:
                with open(rec.path + ".report.log", "w") as out:
                    subprocess.Popen([sys.executable, os.path.join(HERE, "ivr_report.py"), cid, rec.path],
                                     stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
                log("call report started in the background ->", rec.path + ".report.log")
            except Exception as e:  # noqa: BLE001
                log("report warn:", e)

if __name__ == "__main__":
    main()
