"""SFX cue rendering: the committed source of truth for web/sounds/.

Each cue's prompt/seed/level live in CUES below — SOUND_DESIGN.md is the human
brief, this is the machine one. Rendering path (same as the original batch):
all-media MCP server (stable-audio-sfx backend) -> async `audio_sfx` job ->
`media_job_wait` -> fetch the /files/ wav -> ffmpeg auto-trim (last frame
above render-peak − 35 dB, +30 ms pad, 15 ms fade) -> peak-normalize -> ogg
vorbis q3 @ 44.1 kHz, written atomically into web/sounds/.

Two front doors: the CONFIG soundboard (POST /api/sounds/regen, always a
fresh seed) and the CLI for deliberate, reproducible passes:

    python -m server.sfxgen click hover          # committed seeds
    python -m server.sfxgen game_death --fresh   # random seed
"""
import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import tempfile

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

from . import config

WEB_SOUNDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "web", "sounds")
DEFAULT_ALLMEDIA_URL = "http://spark-ee93:8005/mcp"
JOB_WAIT_S = 420          # media_job_wait's own cap (covers ComfyUI queue)
RENDER_TIMEOUT_S = 660    # outer ceiling: transport timeout + job + ffmpeg

# slot: file, prompt, committed seed, render duration (s; API minimum is 1.0 —
# short cues are trimmed DOWN from it), trim?, peak target dB
CUES = {
    "boot": dict(file="boot.ogg", duration_s=1.0, seed=111, trim=True, target_db=-3,
                 prompt="SNES-era chiptune power-on sting: quick rising arpeggio zap on a "
                        "bright square-wave synth, heroic ignition feel, fast decay, "
                        "isolated dry sound effect"),
    "click": dict(file="click.ogg", duration_s=1.0, seed=101, trim=True, target_db=-3,
                  prompt="Single short SNES-era chiptune menu blip: one mid-pitched "
                         "square-wave bloop, crisp attack, very fast decay to silence, "
                         "dry, no reverb, no tail"),
    "hover": dict(file="hover.ogg", duration_s=1.0, seed=112, trim=True, target_db=-3,
                  prompt="Extremely soft faint UI tick: one tiny high-pitched digital "
                         "blip, barely there, very quiet, very short, dry"),
    "message_sent": dict(file="message-sent.ogg", duration_s=1.0, seed=113, trim=True,
                         target_db=-3,
                         prompt="Short upward chirp: quick rising square-wave glide, "
                                "retro game 'message sent' confirm blip, crisp, fast "
                                "decay, dry"),
    "thinking": dict(file="thinking-loop.ogg", duration_s=3.0, seed=114, trim=False,
                     target_db=-12,
                     prompt="Quiet low pulsing bed loop: soft deep bass hum with a "
                            "gentle slow rhythmic pulse, minimal dark synth, steady, "
                            "even from start to finish, no melody"),
    "message_received": dict(file="message-received.ogg", duration_s=1.0, seed=114,
                             trim=True, target_db=-3,
                             prompt="Short two-note confirmation chime: two square-wave "
                                    "notes, the second a perfect fourth above the first, "
                                    "retro friendly OK sound, dry"),
    "tool_start": dict(file="tool-start.ogg", duration_s=1.0, seed=115, trim=True,
                       target_db=-3,
                       prompt="Short mechanical servo whir-up: retro robot motor "
                              "spin-up, quick rising metallic whirr, snappy, dry"),
    "tool_end": dict(file="tool-end.ogg", duration_s=1.0, seed=116, trim=True,
                     target_db=-3,
                     prompt="Short mechanical servo whir-down ending in a click: retro "
                            "robot motor spinning down then a final mechanical clack, dry"),
    "error": dict(file="error.ogg", duration_s=1.0, seed=117, trim=True, target_db=-3,
                  prompt="Short descending error buzz: harsh square-wave downward glide, "
                         "retro game damage warning sound, gritty, dry"),
    "thread_new": dict(file="thread-new.ogg", duration_s=1.0, seed=118, trim=True,
                       target_db=-3,
                       prompt="Crisp double blip: two quick bright square-wave menu "
                              "blips, retro UI confirm, dry"),
    "settings_saved": dict(file="settings-saved.ogg", duration_s=1.0, seed=119,
                           trim=True, target_db=-3,
                           prompt="Short satisfying latch: crisp mechanical lock snap, "
                                  "digital bolt click, solid, quick, dry"),
    "game_shoot": dict(file="game-shoot.ogg", duration_s=1.0, seed=119, trim=True,
                       target_db=-3,
                       prompt="Bright laser pew: short retro blaster zap, bright "
                              "square-wave pew with a fast downward whistle, punchy, dry"),
    "game_jump": dict(file="game-jump.ogg", duration_s=1.0, seed=120, trim=True,
                      target_db=-3,
                      prompt="Soft jump whoosh: gentle short airy upward boost, subtle "
                             "fabric-air swoosh, quiet, dry"),
    "game_dash": dict(file="game-dash.ogg", duration_s=1.0, seed=121, trim=True,
                      target_db=-3,
                      prompt="Quick dash whoosh with a tiny afterburner tick at the end, "
                             "retro sprint swoosh, punchy, dry"),
    "game_hurt": dict(file="game-hurt.ogg", duration_s=1.0, seed=122, trim=True,
                      target_db=-3,
                      prompt="Harsh short damage zap: glitchy harsh electric zap hit, "
                             "retro game player damage bite, dry"),
    "game_kill": dict(file="game-kill.ogg", duration_s=1.0, seed=123, trim=True,
                      target_db=-3,
                      prompt="Small explosion crumble: short retro impact burst that "
                             "crumbles into static debris, punchy, dry"),
    "game_death": dict(file="game-death.ogg", duration_s=1.0, seed=124, trim=True,
                       target_db=-3,
                       prompt="Defeat crumble: crumbling explosion fading into a "
                              "descending sad synth whine, retro game over sting, dry"),
    "game_clear": dict(file="game-clear.ogg", duration_s=1.0, seed=125, trim=True,
                       target_db=-3,
                       prompt="Victory arpeggio sting: short triumphant ascending "
                              "chiptune fanfare, bright square-wave, stage-clear energy, "
                              "dry"),
    "game_charge_full": dict(file="game-charge-full.ogg", duration_s=1.0, seed=126,
                             trim=True, target_db=-3,
                             prompt="Bright shimmer ping: sparkling max-charge "
                                    "confirmation ding, glassy shimmer, short, dry"),
}

_lock = asyncio.Lock()  # one render at a time: same backend queue, no file races
_status: dict = {}  # slot -> last/ongoing regen result, polled by the soundboard


def regen_start(slot: str, seed: int | None = None) -> dict:
    """Kick off a render as a background task; the soundboard polls status().
    Returns immediately — the backend queues ~minutes behind other media jobs."""
    if slot not in CUES:
        return {"ok": False, "error": f"unknown cue '{slot}'"}
    if _status.get(slot, {}).get("running"):
        return {"ok": False, "error": f"'{slot}' is already regenerating"}
    _status[slot] = {"running": True}
    asyncio.create_task(_run(slot, seed))
    return {"ok": True, "started": slot}


async def _run(slot, seed):
    _status[slot] = {"running": True}
    try:
        _status[slot] = await render(slot, seed)
    except Exception as e:  # noqa: BLE001 - status is polled, never awaited
        _status[slot] = {"ok": False, "error": str(e) or type(e).__name__}


def regen_status(slot: str) -> dict:
    return _status.get(slot) or {"idle": True}


def allmedia_url() -> str:
    """Prefer the registered (usually parked) all-media config's URL."""
    reg = (config.load().get("mcp_servers") or {}).get("all-media") or {}
    return reg.get("url") or DEFAULT_ALLMEDIA_URL


def slots_view() -> list:
    """Soundboard rows: committed cue table + manifest when/gain + disk state."""
    try:
        with open(os.path.join(WEB_SOUNDS, "manifest.json")) as fh:
            man = json.load(fh)
    except Exception:
        man = {}
    out = []
    for slot, cue in CUES.items():
        path = os.path.join(WEB_SOUNDS, cue["file"])
        exists = os.path.isfile(path)
        m = man.get(slot) or {}
        out.append({
            "slot": slot, "file": cue["file"], "prompt": cue["prompt"],
            "duration_s": cue["duration_s"], "seed": cue["seed"],
            "when": m.get("when", ""), "gain": m.get("gain", 1),
            "exists": exists,
            "bytes": os.path.getsize(path) if exists else 0,
        })
    return out


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _ffmpeg(cmd: list):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + cmd,
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffmpeg: " + p.stderr[-300:])


def _process(src: str, dst: str, trim: bool, target_db: float) -> float:
    """Raw render wav -> audible-content trim + peak normalize -> ogg (dst)."""
    dur = _ffprobe_duration(src)
    ss, t, fades = 0.0, dur, []
    if trim:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", src, "-af",
             "silencedetect=n=-35dB:d=0.05", "-f", "null", "-"],
            capture_output=True, text=True).stderr
        ev = [("start" if k == "start" else "end", float(v))
              for k, v in re.findall(r"silence_(start|end): (-?[\d.]+)", out)]
        starts = [tm for k, tm in ev if k == "start"]
        cs = 0.0
        if ev and ev[0][0] == "start" and ev[0][1] < 0.01 and len(ev) > 1:
            cs = ev[1][1]                      # audio starts after leading silence
        last_end = next((tm for k, tm in reversed(ev) if k == "end"), None)
        # content ends at the LAST silence start unless audio resumed before EOF
        ce = dur if (not starts or (last_end is not None and last_end < dur - 0.005)) \
            else starts[-1]
        ss = max(0.0, cs - 0.005)
        t = min(dur - ss, (ce + 0.03) - ss)
        if cs > 0.01:
            fades.append("afade=t=in:d=0.003")
    # loop-friendly cues (thinking) get symmetric edge fades; trims fade the tail
    fades.append("afade=t=in:d=0.01" if not trim else "afade=t=in:d=0.002")
    fades.append(f"afade=t=out:st={max(0.0, t - 0.015):.3f}:d=0.015")
    mx = float(re.search(
        r"max_volume: (-?[\d.]+) dB",
        subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True).stderr,
    ).group(1))
    fades.append(f"volume={max(-12.0, min(6.0, target_db - mx)):.1f}dB")
    _ffmpeg(["-i", src, "-ss", f"{ss:.3f}", "-t", f"{t:.3f}",
             "-af", ",".join(fades), "-c:a", "libvorbis", "-q:a", "3",
             "-ar", "44100", dst + ".tmp.ogg"])
    os.replace(dst + ".tmp.ogg", dst)
    return _ffprobe_duration(dst)


def _unwrap(e: Exception) -> Exception:
    # anyio TaskGroups wrap the real failure one level down (see mcp.probe_server)
    if isinstance(e, BaseExceptionGroup) and e.exceptions:
        return e.exceptions[0]
    return e


def _as_json(res) -> dict:
    # langchain-mcp-adapters surfaces tool results as MCP content blocks:
    # [{"type": "text", "text": "{json}"}, …] — join those, then parse
    if isinstance(res, list):
        txt = "\n".join(str(b.get("text", "")) for b in res
                        if isinstance(b, dict) and b.get("type") == "text")
    else:
        txt = res if isinstance(res, str) else str(res)
    if not txt:
        raise RuntimeError("empty tool result: " + str(res)[:200])
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("unparsable tool result: " + txt[:200])


async def render(slot: str, seed: int | None = None) -> dict:
    """Regenerate one cue end to end. Always 200-style data: ok/error fields."""
    if slot not in CUES:
        return {"ok": False, "error": f"unknown cue '{slot}'"}
    cue = CUES[slot]
    seed = random.randint(0, 2**31 - 1) if seed is None else seed
    async with _lock:
        try:
            client = MultiServerMCPClient({"__sfx__": {
                "transport": "streamable_http", "url": allmedia_url(),
                "timeout": RENDER_TIMEOUT_S}})
            async with asyncio.timeout(RENDER_TIMEOUT_S):
                tools = await client.get_tools(server_name="__sfx__")
                by = {t.name: t for t in tools}
                for need in ("audio_sfx", "media_job_wait", "media_job_result"):
                    if need not in by:
                        return {"ok": False, "error": f"all-media has no '{need}' tool"}
                j = _as_json(await by["audio_sfx"].ainvoke({
                    "prompt": cue["prompt"], "duration_s": cue["duration_s"],
                    "seed": seed}))
                jid = j.get("job_id")
                if not jid:
                    return {"ok": False, "error": "audio_sfx returned no job_id: " + str(j)[:200]}
                w = _as_json(await by["media_job_wait"].ainvoke({
                    "job_id": jid, "timeout_s": JOB_WAIT_S}))
                if w.get("status") not in (None, "done") and not (w.get("urls") or w.get("files")):
                    return {"ok": False, "error": f"job {w.get('status')}: "
                            + str(w.get("error") or "")[:300]}
                r = _as_json(await by["media_job_result"].ainvoke({"job_id": jid}))
                url = (r.get("urls") or w.get("urls") or [None])[0]
                if not url:
                    return {"ok": False, "error": "job finished with no output file"}
                async with httpx.AsyncClient(timeout=120) as hx:
                    wav = (await hx.get(allmedia_url().removesuffix("/mcp") + url)).content
            dst = os.path.join(WEB_SOUNDS, cue["file"])
            with tempfile.TemporaryDirectory() as td:
                src = os.path.join(td, "in.wav")
                with open(src, "wb") as fh:
                    fh.write(wav)
                # blocking ffmpeg in a thread keeps the SSE chat stream alive
                out_dur = await asyncio.to_thread(
                    _process, src, dst, cue["trim"], cue["target_db"])
            return {"ok": True, "file": cue["file"], "seed": seed,
                    "duration_s": round(out_dur, 2), "bytes": len(open(dst, "rb").read())}
        except TimeoutError:
            return {"ok": False, "error": f"timed out after {RENDER_TIMEOUT_S}s "
                    "(the all-media queue may be busy — try again)"}
        except Exception as e:  # noqa: BLE001 - the error text IS the feature
            cause = _unwrap(e)
            return {"ok": False, "error": str(cause) or type(cause).__name__}


async def _main():
    ap = argparse.ArgumentParser(
        description="Regenerate LangBang SFX cues (web/sounds/) from the committed "
                    "prompt table. Slots listed are regenerated; none listed = all.")
    ap.add_argument("slots", nargs="*", choices=list(CUES), default=[])
    ap.add_argument("--fresh", action="store_true",
                    help="random seeds instead of the committed reproducible ones")
    ap.add_argument("--seed", type=int, help="force one seed for all listed cues")
    args = ap.parse_args()
    for slot in (args.slots or list(CUES)):
        seed = args.seed if args.seed is not None else (
            random.randint(0, 2**31 - 1) if args.fresh else CUES[slot]["seed"])
        r = await render(slot, seed)
        print(slot, "->", json.dumps(r))


if __name__ == "__main__":
    asyncio.run(_main())
