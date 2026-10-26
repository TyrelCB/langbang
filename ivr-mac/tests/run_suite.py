#!/usr/bin/env python3
"""run_suite.py — regression suite for the phone IVR. Certify a change by
running it before and after; a fix for a misfire adds its case first.

Layers (pick any; default: logic convo):
  logic    the loop's decisions on real utterances (test_logic.py) — seconds
  convo    curated misfires from real calls (cases/convo.json) replayed as
           text through the live node + model + TTS, each reply checked
           against the call rules — ~1-2 min
  history  the caller lines of the last N real calls (cases/history.json),
           replayed the same way, call rules only — ~1 min per call
  audio    recorded callers (cases/audio/) played into the full rig over
           BlackHole, in the desktop session (audio_replay.py) — catches cut
           off / dropped / echo-ignored sentences, GV's voice answered, and
           per-turn latency — ~2 min per call

Layers that use the node or the audio devices refuse to start while a real
call is up, and hold tests/.suite_running so ivr_answer.py leaves any ring
during the run to voicemail. Test calls are named suite-… in the call log.

Results: tests/results/<stamp>.json. --save-baseline stores this run's
latencies in tests/baseline.json; later runs flag a >25% slowdown.

Usage: .venv/bin/python tests/run_suite.py [logic] [convo] [history] [audio]
         [--repeat N] [--history-calls N] [--audio-calls N] [--save-baseline]
Rebuild cases after new calls: tests/build_cases.py
"""
import argparse
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RIG = os.path.dirname(HERE)
sys.path.insert(0, RIG)
os.environ.setdefault("IVR_BASE", "http://127.0.0.1:8130")  # the local node, not the public hub
import ivr_loop as LOOP  # noqa: E402

CASES = os.path.join(HERE, "cases")
RESULTS = os.path.join(HERE, "results")
BASELINE = os.path.join(HERE, "baseline.json")
LOCK = os.path.join(RIG, ".suite_running")          # ivr_answer.SUITE_LOCK
AUDIO_JOB = "com.tyrel.ivr-suite-audio"
SLOWER = 1.25                                       # vs baseline: flag beyond this

# Every reply, every layer. Each rule is here because a real call broke it.
RULES = [
    (r"\bhow (can|may) i (help|assist)", "help-desk opener"),
    (r"\banything else i can (help|assist)", "help-desk closer"),
    (r"\bassist you\b", "help-desk phrasing"),
    (r"\btyrel'?s\b", "talks about Tyrel in the third person"),
    (r"do you have (any )?questions? (for me )?(about|regarding) (the|this) (role|company|position)",
     "asks the recruiter about their own role"),
    (r"\b(i can|i'd be happy to|i would be happy to|happy to|let me) take a message", "offers to take a message"),
    (r"[*#`]|https?://", "markdown or URL in speech"),
]
MAX_WORDS = 55


def log(*a):
    print(*a, flush=True)


def rule_violations(reply: str) -> list[str]:
    out = [why for rx, why in RULES if re.search(rx, reply, re.I)]
    if len(reply.split()) > MAX_WORDS:
        out.append(f"{len(reply.split())} words (> {MAX_WORDS})")
    if not reply.strip():
        out.append("empty reply")
    return out


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None


# ---- the line ----------------------------------------------------------------

def line_busy() -> list:
    with urllib.request.urlopen(LOOP.BASE + "/api/ivr/status", timeout=5) as r:
        return json.load(r).get("active") or []


def hold_line():
    """Take the line for a test: no real call up, and rings go to voicemail."""
    busy = line_busy()
    if busy:
        raise SystemExit(f"a call is up ({busy[0].get('id')}) — not testing over it")
    touch_lock()


def touch_lock():
    with open(LOCK, "w") as fh:
        fh.write(str(os.getpid()))


def release_line():
    try:
        os.remove(LOCK)
    except OSError:
        pass


# ---- layer 1 -----------------------------------------------------------------

def run_logic() -> dict:
    spec = importlib.util.spec_from_file_location("test_logic", os.path.join(HERE, "test_logic.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = []
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            res.append({"name": name, "ok": True})
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, AssertionError) else traceback.format_exc(limit=2)
            res.append({"name": name, "ok": False, "why": msg})
    for r in res:
        log(f"  {'PASS' if r['ok'] else 'FAIL'} {r['name']}" + ("" if r["ok"] else "\n      " + r["why"].replace("\n", "\n      ")))
    return {"tests": res, "passed": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res)}


# ---- layers 2 + history ------------------------------------------------------

def say(cid: str, text: str) -> dict:
    """One text turn through the same streamed endpoint a phone turn uses
    (model + spoken-word cap + TTS); returns the done event."""
    r = LOOP.request(f"/api/ivr/turn/stream?call_id={cid}&fmt=wav", json.dumps({"text": text}).encode(),
                     timeout=120)
    done = {}
    with r:
        for line in r:
            ev = json.loads(line)
            if ev.get("type") == "done":
                done = ev
    return done


def converse(cid: str, turns: list[dict]) -> list[dict]:
    LOOP.api(f"/api/ivr/start?out=wav&rate={LOOP.REPLY_RATE}&call_id={cid}")
    out = []
    try:
        for t in turns:
            touch_lock()
            done = say(cid, t["say"])
            reply = done.get("reply") or ""
            why = rule_violations(reply)
            if done.get("error"):
                why.append(f"error: {done['error']}")
            if t.get("match") and not re.search(t["match"], reply, re.I):
                why.append(f"should match /{t['match']}/")
            if t.get("no_match") and re.search(t["no_match"], reply, re.I):
                why.append(f"must not match /{t['no_match']}/")
            out.append({"say": t["say"], "reply": reply, "why": why,
                        "first_audio_s": (done.get("timings") or {}).get("first_audio_s")})
    finally:
        try:
            LOOP.api(f"/api/ivr/hangup?call_id={cid}", timeout=15)
        except Exception:  # noqa: BLE001
            pass
    return out


def show_case(name, turns, verbose):
    bad = [t for t in turns if t["why"]]
    log(f"  {'PASS' if not bad else 'FAIL'} {name}")
    for t in turns:
        if t["why"] or verbose:
            log(f"      caller: {t['say']}\n      ivr:    {t['reply']}")
            for w in t["why"]:
                log(f"      ✗ {w}")
    return not bad


def run_convo(stamp: str, repeat: int, verbose: bool) -> dict:
    with open(os.path.join(CASES, "convo.json")) as fh:
        cases = json.load(fh)
    res = []
    for rep in range(repeat):
        for c in cases:
            turns = converse(f"suite-convo-{stamp}-{c['name']}-{rep}", c["turns"])
            ok = show_case(c["name"] + (f" (run {rep + 1})" if repeat > 1 else ""), turns, verbose)
            res.append({"name": c["name"], "run": rep, "ok": ok, "turns": turns})
    return {"cases": res, "passed": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res),
            "latency": _lat(res)}


def run_history(stamp: str, n: int, verbose: bool) -> dict:
    with open(os.path.join(CASES, "history.json")) as fh:
        calls = json.load(fh)[-n:]
    res = []
    for c in calls:
        turns = converse(f"suite-hist-{stamp}-{c['call_id']}", [{"say": s} for s in c["turns"]])
        ok = show_case(f"{c['call_id']} ({len(turns)} turns)", turns, verbose)
        res.append({"name": c["call_id"], "ok": ok, "turns": turns})
    return {"cases": res, "passed": sum(r["ok"] for r in res), "failed": sum(not r["ok"] for r in res),
            "latency": _lat(res)}


def _lat(res):
    xs = [t["first_audio_s"] for r in res for t in r["turns"] if t.get("first_audio_s")]
    return {"p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "n": len(xs)}


# ---- layer 3 (desktop session) -----------------------------------------------

def run_audio(stamp: str, n: int) -> dict:
    req = os.path.join(HERE, ".audio_request.json")
    out = os.path.join(RESULTS, f"{stamp}-audio.json")
    with open(req, "w") as fh:
        json.dump({"stamp": stamp, "calls": n, "out": out}, fh)
    uid = os.getuid()
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{AUDIO_JOB}"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"can't start {AUDIO_JOB}: {r.stderr.strip()} (install: tests/install_audio_job.sh)")
    log(f"  running in the desktop session ({AUDIO_JOB}); log: tests/results/audio_replay.log")
    end = time.time() + 45 * 60
    while time.time() < end and not os.path.isfile(out):
        touch_lock()
        time.sleep(3)
    if not os.path.isfile(out):
        raise SystemExit("audio layer produced no result in 45 min")
    with open(out) as fh:
        res = json.load(fh)
    for c in res["cases"]:
        log(f"  {'PASS' if c['ok'] else 'FAIL'} {c['name']}  ({c['clips']} clips, latency p50 {c['latency']['p50']}s)")
        for w in c["why"]:
            log(f"      ✗ {w}")
        for w in c.get("warn") or []:
            log(f"      ? {w}")
    return res


# ---- main --------------------------------------------------------------------

def compare(layer: str, lat: dict, base: dict) -> list[str]:
    old = (base.get(layer) or {}).get("p50")
    if not (old and lat and lat.get("p50")):
        return []
    if lat["p50"] > old * SLOWER:
        return [f"{layer}: latency p50 {lat['p50']}s vs baseline {old}s (> {int((SLOWER - 1) * 100)}% slower)"]
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layers", nargs="*", default=["logic", "convo"],
                    choices=["logic", "convo", "history", "audio"])
    ap.add_argument("--repeat", type=int, default=1, help="run each convo case N times (model variance)")
    ap.add_argument("--history-calls", type=int, default=3)
    ap.add_argument("--audio-calls", type=int, default=3)
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every turn, not just failures")
    args = ap.parse_args()
    layers = args.layers or ["logic", "convo"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(RESULTS, exist_ok=True)
    try:
        with open(BASELINE) as fh:
            base = json.load(fh)
    except (OSError, ValueError):
        base = {}
    out: dict = {"stamp": stamp, "layers": {}}
    regress: list[str] = []
    needs_line = any(x in layers for x in ("convo", "history", "audio"))
    try:
        if needs_line:
            hold_line()
        for layer in ("logic", "convo", "history", "audio"):
            if layer not in layers:
                continue
            log(f"== {layer}")
            t0 = time.time()
            if layer == "logic":
                r = run_logic()
            elif layer == "convo":
                r = run_convo(stamp, args.repeat, args.verbose)
            elif layer == "history":
                r = run_history(stamp, args.history_calls, args.verbose)
            else:
                r = run_audio(stamp, args.audio_calls)
            r["secs"] = round(time.time() - t0, 1)
            out["layers"][layer] = r
            regress += compare(layer, r.get("latency"), base)
    finally:
        if needs_line:
            release_line()
    path = os.path.join(RESULTS, f"{stamp}.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    log("\n== summary")
    failed = 0
    for layer, r in out["layers"].items():
        lat = r.get("latency") or {}
        failed += r["failed"]
        log(f"  {layer:8} {r['passed']} passed, {r['failed']} failed"
            + (f"   latency p50 {lat['p50']}s p95 {lat['p95']}s" if lat.get("p50") else "") + f"   ({r['secs']}s)")
    for w in regress:
        log(f"  SLOWER  {w}")
    if args.save_baseline:
        base.update({k: r["latency"] for k, r in out["layers"].items() if r.get("latency")})
        with open(BASELINE, "w") as fh:
            json.dump(base, fh, indent=1)
        log(f"  baseline saved -> {BASELINE}")
    log(f"  details: {path}")
    return 1 if failed or regress else 0


if __name__ == "__main__":
    sys.exit(main())
