#!/usr/bin/env python3
"""build_cases.py — turn past real calls into replayable test cases.

  cases/history.json      every real call's caller lines, in order, from the
                          node's call log (layer 2 replays them as text)
  cases/audio/<call>/*.wav + cases/audio/manifest.json
                          each recorded call's caller side cut into its
                          utterances, with a reference transcript (layer 3
                          plays them into the rig over BlackHole)
  cases/gv_menu.wav       Google Voice reading the caller ID on our leg (call
                          28a38a02a121) — the voice the loop once answered

Utterances are cut with the loop's own VAD numbers. Anything that starts
while our side was talking (or right after) is dropped: on the caller leg
that is mostly our echo, and replaying it out of context makes junk turns.

Re-run after new calls:  .venv/bin/python tests/build_cases.py
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RIG = os.path.dirname(HERE)
sys.path.insert(0, RIG)
os.environ.setdefault("IVR_BASE", "http://127.0.0.1:8130")  # the local node, not the public hub
import ivr_loop as LOOP  # noqa: E402

CASES = os.path.join(HERE, "cases")
NODE_LOG = os.path.expanduser("~/.ivr-node/ivr/calls.jsonl")
RECORDINGS = os.path.join(RIG, "recordings")
SR = 16000
FRAME = int(SR * LOOP.FRAME_S)
NOT_REAL = re.compile(r"^(suite-|prompttest|cachetest)")
AGENT_DB = -45.0          # our side louder than this = we were talking
ECHO_TAIL_S = 0.6         # caller-leg sound starting this soon after us = echo
MAX_CLIPS = 10
GV_MENU = ("20261026-040338-28a38a02a121", 2.4, 6.6)   # recording, start s, end s


def frames_db(pcm: np.ndarray) -> np.ndarray:
    n = len(pcm) // FRAME
    x = pcm[:n * FRAME].astype(np.float64).reshape(n, FRAME)
    rms = np.sqrt((x ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1.0) / 32768.0)


def read16k(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def write16k(path: str, pcm: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes(pcm.astype("<i2").tobytes())


def segments(db: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) frame spans the loop's listen() would have taken."""
    floor = float(np.percentile(db, 10))
    thr = min(max(floor + LOOP.SPEECH_OVER_FLOOR, LOOP.SPEECH_DB_MIN), LOOP.SPEECH_DB_MAX)
    voiced = db > thr
    win, need = int(LOOP.START_WIN_S / LOOP.FRAME_S), int(LOOP.START_S / LOOP.FRAME_S)
    end_q, out, i = int(LOOP.END_S / LOOP.FRAME_S), [], 0
    while i < len(db):
        if voiced[max(0, i - win + 1):i + 1].sum() >= need:
            start, quiet, j = max(0, i - win + 1), 0, i + 1
            while j < len(db) and quiet < end_q and (j - start) * LOOP.FRAME_S < LOOP.MAX_UTTER_S:
                quiet = 0 if voiced[j] else quiet + 1
                j += 1
            end = j - quiet
            if (end - start) * LOOP.FRAME_S >= LOOP.MIN_UTTER_S:
                out.append((start, end))
            i = j
        else:
            i += 1
    return out


def stt(pcm: np.ndarray) -> str:
    req = urllib.request.Request(LOOP.BASE + "/api/stt", data=pcm.astype("<i2").tobytes(), headers={
        "Authorization": "Bearer " + LOOP.token(), "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return (json.load(r).get("text") or "").strip()


def build_history() -> int:
    calls: dict[str, dict] = {}
    with open(NODE_LOG) as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            cid = d.get("call_id") or ""
            if NOT_REAL.match(cid) or d.get("event") != "turn":
                continue
            said = (d.get("transcript") or "").strip()
            if not said or said.startswith("("):   # the loop's own prompts, not the caller
                continue
            c = calls.setdefault(cid, {"call_id": cid, "ts": d["ts"], "turns": []})
            c["turns"].append(said)
    out = sorted((c for c in calls.values() if len(c["turns"]) >= 2), key=lambda c: c["ts"])
    with open(os.path.join(CASES, "history.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return len(out)


def build_audio() -> int:
    adir = os.path.join(CASES, "audio")
    os.makedirs(adir, exist_ok=True)
    manifest = []
    for caller_wav in sorted(glob.glob(os.path.join(RECORDINGS, "*.m4a.caller.wav"))):
        base = caller_wav[:-len(".caller.wav")]
        cid = os.path.basename(base).split("-")[-1].removesuffix(".m4a")
        agent_wav = base + ".agent.wav"
        if not os.path.isfile(agent_wav) or NOT_REAL.match(cid):
            continue
        cpcm, apcm = read16k(caller_wav), read16k(agent_wav)
        cdb, adb = frames_db(cpcm), frames_db(apcm)
        talking = np.zeros(len(cdb), bool)
        tail = int(ECHO_TAIL_S / LOOP.FRAME_S)
        for k in np.nonzero(adb[:len(cdb)] > AGENT_DB)[0]:
            talking[k:k + tail + 1] = True
        clips = []
        for s, e in segments(cdb):
            if talking[s]:
                continue                     # echo of us (or a barge-in we can't replay faithfully)
            pre = int(LOOP.PREROLL_S / LOOP.FRAME_S)
            pcm = cpcm[max(0, s - pre) * FRAME:e * FRAME]
            text = stt(pcm)
            if not text or LOOP.GV_SCREEN_RE.search(text):
                continue
            name = f"{len(clips):02d}.wav"
            os.makedirs(os.path.join(adir, cid), exist_ok=True)
            write16k(os.path.join(adir, cid, name), pcm)
            clips.append({"file": f"{cid}/{name}", "text": text, "secs": round(len(pcm) / SR, 2)})
            if len(clips) >= MAX_CLIPS:
                break
        if len(clips) >= 3:
            manifest.append({"call_id": cid, "recording": os.path.basename(base), "clips": clips})
            print(f"  {cid}: {len(clips)} clips", flush=True)
    with open(os.path.join(adir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    rec, a, b = GV_MENU
    path = os.path.join(RECORDINGS, rec + ".m4a.caller.wav")
    if os.path.isfile(path):
        write16k(os.path.join(CASES, "gv_menu.wav"), read16k(path)[int(a * SR):int(b * SR)])
    return len(manifest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-audio", action="store_true", help="history only (no STT calls)")
    args = ap.parse_args()
    os.makedirs(CASES, exist_ok=True)
    print(f"history: {build_history()} real calls -> cases/history.json", flush=True)
    if not args.no_audio:
        print(f"audio: {build_audio()} calls -> cases/audio/manifest.json", flush=True)


if __name__ == "__main__":
    main()
