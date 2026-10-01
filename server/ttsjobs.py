"""Streaming Pocket TTS clips: speech starts ~0.1 s after 🔊, not after the
whole reply is synthesized (a 1.5k-char answer is ~20 s of CPU for ~90 s of
audio).

A Job is keyed by main._tts_key (text + voice knobs) — the same key as the
disk cache, so a finished stream simply IS the cache entry. One producer
thread pipes model PCM into ffmpeg (CBR MP3, so browsers can compute the
duration and Range-seek the cached file later); a reader thread collects
ffmpeg's output into an in-memory chunk list + a `.part` file. Any number of
async followers stream the chunks from byte 0 (same idea as runs.Hub, but
the producer is a thread, so followers poll instead of awaiting a
Condition). On success `.part` is renamed into the cache; on STOP — no
follower for ABANDON_S — the model is told to stop and `.part` is dropped.

Generation is serialized by voice._pk_gen (the model isn't thread-safe), so
a second job queues behind the first; its follower just waits for bytes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from typing import AsyncIterator

from . import voice

logger = logging.getLogger("langbang.ttsjobs")

ABANDON_S = 20.0   # never-joined job (POST but no GET yet) → cancel after this
LEFT_S = 1.0       # every listener left (released / closed tab) → cancel after this
LINGER_S = 120.0   # finished/failed jobs stay joinable this long
POLL_S = 0.05
# input side: no probing/analysis buffer — ffmpeg otherwise sat on the first
# ~1 s of PCM before emitting a byte (measured 1.15 s vs the model's 0.15 s)
FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error",
          "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
          "-f", "s16le", "-ar", str(voice.POCKET_RATE), "-ac", "1", "-i", "pipe:0",
          "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3",
          "-flush_packets", "1", "pipe:1"]


class Job:
    def __init__(self, key: str, text: str, v: dict, final: str) -> None:
        self.key, self.text, self.v, self.final = key, text, dict(v), final
        self.part = final + ".part"
        self.chunks: list[bytes] = []
        self.done = False
        self.error: str | None = None
        self.cancelled = False
        self.stop = threading.Event()
        self.followers = 0
        self.ever_followed = False
        self.preempted = False  # a newer clip was requested after every listener left
        self.last_seen = time.time()
        self.t0 = time.time()
        self.done_ts = 0.0
        self._lock = threading.Lock()

    # -- producer side (worker threads) --
    def _abandoned(self) -> bool:
        with self._lock:
            # a listener that LEFT means the UI released this clip (another
            # 🔊 started, tab closed): free the single synthesis slot fast so
            # the clip the user asked for isn't queued behind this one
            grace = LEFT_S if self.ever_followed else ABANDON_S
            if self.followers == 0 and self.preempted:
                return True
            return self.followers == 0 and time.time() - self.last_seen > grace

    def run(self) -> None:
        proc = None
        try:
            os.makedirs(os.path.dirname(self.final), exist_ok=True)
            # bufsize=0: PCM goes straight to ffmpeg, not into an 8 KB Python buffer
            proc = subprocess.Popen(FFMPEG, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
            reader.start()
            for pcm in voice.pocket_pcm(self.text, self.v, stop=self.stop):
                if self._abandoned():
                    self.stop.set()  # model stops generating new frames
                    self.cancelled = True
                    break
                proc.stdin.write(pcm)
            proc.stdin.close()
            reader.join()
            rc = proc.wait()
            if self.cancelled:
                self._drop_part()
            elif rc != 0:
                self.error = f"ffmpeg exited {rc}"
                self._drop_part()
            else:
                os.replace(self.part, self.final)  # the stream IS the cache entry now
        except Exception as e:  # noqa: BLE001 - followers see a truncated stream; log why
            self.error = f"{type(e).__name__}: {e}"
            logger.warning("pocket tts job %s failed: %s", self.key[:12], self.error)
            if proc and proc.poll() is None:
                proc.kill()
            self._drop_part()
        finally:
            self.done = True
            self.done_ts = time.time()

    def _read(self, proc) -> None:
        with open(self.part, "wb") as fh:
            while True:
                b = proc.stdout.read1(16384) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not b:
                    return
                fh.write(b)
                self.chunks.append(b)  # list.append is atomic; followers only read

    def _drop_part(self) -> None:
        try:
            os.remove(self.part)
        except FileNotFoundError:
            pass

    # -- follower side (event loop) --
    async def follow(self) -> AsyncIterator[bytes]:
        with self._lock:
            self.followers += 1
            self.ever_followed = True
            self.last_seen = time.time()
        i = 0
        try:
            while True:
                n = len(self.chunks)
                if i < n:
                    for b in self.chunks[i:n]:
                        yield b
                    i = n
                    continue
                if self.done:
                    return
                await asyncio.sleep(POLL_S)
        finally:
            with self._lock:
                self.followers -= 1
                self.last_seen = time.time()


JOBS: dict[str, Job] = {}
_worker = None  # single-thread pool: jobs queue in order (voice._pk_gen also serializes)


def _sweep() -> None:
    now = time.time()
    for k, j in list(JOBS.items()):
        if j.done and now - j.done_ts > LINGER_S:
            del JOBS[k]


def get(key: str) -> Job | None:
    _sweep()
    j = JOBS.get(key)
    return None if j is None or j.cancelled or j.error else j


def start(key: str, text: str, v: dict, final: str) -> Job:
    """Join a live job for this key or queue a new one."""
    global _worker
    _sweep()
    j = JOBS.get(key)
    if j and not j.cancelled and not j.error:
        with j._lock:
            j.last_seen = time.time()  # a fresh request keeps it alive
        return j
    # a NEW clip is the user moving on: any running job nobody listens to
    # anymore yields the single synthesis slot now, not after LEFT_S
    for other in JOBS.values():
        if not other.done and other.ever_followed and other.followers == 0:
            other.preempted = True
    if _worker is None:
        from concurrent.futures import ThreadPoolExecutor
        _worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocket-tts")
    j = Job(key, text, v, final)
    JOBS[key] = j
    _worker.submit(j.run)
    return j
