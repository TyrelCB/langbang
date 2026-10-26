#!/usr/bin/env python3
"""audio_replay.py — layer 3: recorded callers played through the real rig.

Runs in the desktop session (launchd com.tyrel.ivr-suite-audio, started by
run_suite.py audio): macOS returns silence to audio capture from ssh.

Per recorded call (cases/audio/manifest.json, newest first) it starts
ivr_loop.py the way ivr_answer.py does (--greet-first --await-go, then
"go"), plays Google Voice's caller-ID readout right after the accept, then
plays the caller's utterances one at a time, each once the IVR has gone
quiet (as a caller waiting for the reply would), and reads the loop's log:

  dropped   an utterance that never became a caller turn (echo-ignored,
            "no words", lost) — call 9e95a05395cc
  split     one utterance taken as two turns: the caller was cut off
            mid-sentence — END_S 1.0, 10-26
  gv        GV's own voice answered as the caller — call 28a38a02a121
  latency   end of the utterance -> first reply audio, as the caller hears it

Not covered: barge-in (the fake caller never talks over a reply) and the GV
answer/press-1 flow itself (test_logic pins the handoff rule).
"""
import difflib
import json
import os
import queue
import re
import statistics
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
RIG = os.path.dirname(HERE)
sys.path.insert(0, RIG)
import mac_fake_caller as FC  # noqa: E402

CASES = os.path.join(HERE, "cases")
REQUEST = os.path.join(HERE, ".audio_request.json")
LOCK = os.path.join(RIG, ".suite_running")
SAME = 0.5          # word-level similarity: the turn is this utterance
LATENCY_MAX = 4.5   # end of utterance -> first reply audio, as heard (s)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, words(a), words(b)).ratio()


class Loop:
    """ivr_loop.py as ivr_answer.py runs it; its log lines, timestamped."""

    def __init__(self, cid: str):
        env = {**os.environ, "IVR_CALL_ID": cid, "IVR_NO_GV": "1", "IVR_NO_REPORT": "1",
               "IVR_NO_RECORD": "1", "PYTHONUNBUFFERED": "1",
               "IVR_BASE": os.environ.get("IVR_BASE", "http://127.0.0.1:8130")}
        self.p = subprocess.Popen([sys.executable, os.path.join(RIG, "ivr_loop.py"), "--greet-first",
                                   "--await-go", "--max-turns", "100", "--watch-secs", "6"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, env=env)
        self.lines: list[tuple[float, str]] = []
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            line = line.rstrip("\n")
            self.lines.append((time.time(), line))
            self.q.put(line)
            print("   loop|", line, flush=True)

    def wait_for(self, text: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if text in self.q.get(timeout=0.5):
                    return True
            except queue.Empty:
                pass
        return False

    def go(self):
        self.p.stdin.write("go\n")
        self.p.stdin.flush()

    def finish(self, timeout: float):
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.send_signal(2)
            self.p.wait(20)


def play_call(case: dict, stamp: str, ear: "FC.Ear", out: "sd.OutputStream", gv: np.ndarray | None) -> dict:
    cid = f"suite-audio-{stamp}-{case['call_id']}"
    log(f"== {case['call_id']} ({len(case['clips'])} clips) as {cid}")
    loop = Loop(cid)
    why: list[str] = []
    warn: list[str] = []
    if not loop.wait_for("waiting for the accept", 30):
        loop.p.kill()
        return {"name": case["call_id"], "ok": False, "clips": 0, "why": ["loop never got ready"],
                "warn": [], "latency": {}}
    loop.go()
    if gv is not None:
        out.write(gv.reshape(-1, 1))               # GV still reading the caller ID after the accept
    if not loop.wait_for("greeted", 30):
        why.append("never greeted")
    lat, played = [], []
    for c in case["clips"]:
        os.utime(LOCK, None) if os.path.exists(LOCK) else None
        if not ear.wait_quiet(FC.QUIET_S, 60):
            why.append(f"IVR never went quiet before {c['file']}")
        t_start = time.time()
        out.write(FC.clip(os.path.join(CASES, "audio", c["file"])).reshape(-1, 1))
        t_end = time.time()
        first = ear.first_sound(t_end, 30)
        played.append({**c, "t0": t_start, "t1": t_end,
                       "latency": round(first - t_end, 2) if first else None})
        if first:
            lat.append(first - t_end)
    ear.wait_quiet(FC.QUIET_S, 60)
    loop.finish(60)

    # match the loop's caller turns (and drops) to what was played
    heard = [(t, m.group(1)) for t, l in loop.lines
             if (m := re.search(r"caller: ['\"](.*)['\"]$", l))]
    dropped_lines = [l for _, l in loop.lines if "echo of our own reply" in l or "no words in that" in l]
    # a clip owns the caller turns logged from its start until the next clip
    # starts (the loop logs a turn ~2 s after the clip ends, well before the
    # fake caller waits out the reply and plays the next one)
    for k, p in enumerate(played):
        until = played[k + 1]["t0"] if k + 1 < len(played) else p["t1"] + 30
        mine = [s for t, s in heard if p["t0"] < t < until]
        p["heard"] = mine
        best = max((similar(s, p["text"]) for s in mine), default=0.0)
        if not mine:
            why.append(f"dropped: {p['text']!r} (no caller turn)")
        elif len(mine) > 1:
            why.append(f"split: {p['text']!r} -> {mine}")
        elif best < SAME:
            # one turn, other words: speech-to-text varies run to run on
            # phone audio ("jelling" / "John") — worth a look, not a failure
            warn.append(f"misheard: {p['text']!r} -> {mine[0]!r}")
        if p["latency"] and p["latency"] > LATENCY_MAX:
            why.append(f"slow: {p['latency']}s after {p['text']!r}")
    if gv is not None:
        gv_turns = [s for _, s in heard if re.search(r"plus one|seven, five|press (1|one)", s, re.I)]
        if gv_turns:
            why.append(f"answered Google Voice's own voice: {gv_turns}")
    for l in dropped_lines:
        if "echo of our own reply" in l:
            why.append(f"echo filter dropped: {l.split('echo of our own reply', 1)[1].strip()}")
    xs = sorted(lat)
    return {"name": case["call_id"], "ok": not why, "clips": len(played), "why": why, "warn": warn,
            "latency": {"p50": round(statistics.median(xs), 2) if xs else None,
                        "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2) if xs else None,
                        "n": len(xs)},
            "turns": [{k: v for k, v in p.items() if k not in ("t0", "t1")} for p in played]}


def main():
    with open(REQUEST) as fh:
        req = json.load(fh)
    with open(os.path.join(CASES, "audio", "manifest.json")) as fh:
        manifest = json.load(fh)
    cases = sorted(manifest, key=lambda c: c["recording"])[-int(req.get("calls") or 3):]
    gv_path = os.path.join(CASES, "gv_menu.wav")
    gv = FC.clip(gv_path) if os.path.isfile(gv_path) else None
    ear = FC.Ear()
    out = sd.OutputStream(device=FC.TALK_DEV, channels=1, samplerate=FC.SR, dtype="float32")
    out.start()
    time.sleep(1.0)
    res = []
    try:
        for c in cases:
            res.append(play_call(c, req["stamp"], ear, out, gv))
            time.sleep(2)
    finally:
        out.stop()
    xs = [t["latency"] for r in res for t in r.get("turns", []) if t.get("latency")]
    xs.sort()
    result = {"cases": res, "passed": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res),
              "latency": {"p50": round(statistics.median(xs), 2) if xs else None,
                          "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2) if xs else None,
                          "n": len(xs)}}
    tmp = req["out"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=1)
    os.replace(tmp, req["out"])
    log("done:", result["passed"], "passed,", result["failed"], "failed")


if __name__ == "__main__":
    main()
