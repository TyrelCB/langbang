#!/usr/bin/env python3
"""mac_fake_caller.py — a scripted caller for ivr_loop.py on the Mac, no
browser, no phone.

It stands where Chrome will stand: plays caller clips into BlackHole 2ch (the
IVR's downlink) and listens on BlackHole 16ch (what the IVR says). For each
clip it reports how long after the clip ended the first reply audio arrived.
After the script it stays silent so the IVR's "are you still there?" and
hang-up can be checked.

Run it next to ivr_loop.py, both from the desktop session (see
mac_ivr_test.py): audio capture from ssh returns silence on macOS.

Usage: mac_fake_caller.py clip.wav [clip.wav ...] [--linger S]
  clips: 16 kHz mono s16 wavs (bench/clips/*.wav)
"""
import argparse
import math
import threading
import time
import wave

import numpy as np
import sounddevice as sd

SR = 16000
TALK_DEV, EAR_DEV = "BlackHole 2ch", "BlackHole 16ch"
REPLY_DB = -45.0        # ear: louder than this is IVR speech
QUIET_S = 1.5           # ear quiet this long = the IVR finished its reply


def log(*a):
    print(time.strftime("%H:%M:%S"), "caller|", *a, flush=True)


class Ear:
    """Loudness of what the IVR is saying, 20 ms at a time, timestamped."""

    def __init__(self):
        self.frames: list[tuple[float, float]] = []
        self.lock = threading.Lock()
        self.s = sd.InputStream(device=EAR_DEV, channels=1, samplerate=SR, dtype="float32",
                                blocksize=SR // 50, callback=self._cb)
        self.s.start()

    def _cb(self, data, frames, t, status):
        rms = float(np.sqrt(np.mean(data[:, 0] ** 2)))
        with self.lock:
            self.frames.append((time.time(), 20 * math.log10(max(rms, 1e-9))))

    def first_sound(self, after: float, timeout: float) -> float | None:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                hit = next((t for t, db in self.frames if t > after and db > REPLY_DB), None)
            if hit:
                return hit
            time.sleep(0.05)
        return None

    def wait_quiet(self, secs: float, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                recent = [db for t, db in self.frames if t > time.time() - secs]
            if recent and max(recent) < REPLY_DB and time.time() - self.frames[0][0] > secs:
                return True
            time.sleep(0.1)
        return False

    def spoke_between(self, a: float, b: float) -> float:
        with self.lock:
            return sum(0.02 for t, db in self.frames if a < t < b and db > REPLY_DB)


def clip(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--linger", type=float, default=0.0)
    args = ap.parse_args()
    ear = Ear()
    out = sd.OutputStream(device=TALK_DEV, channels=1, samplerate=SR, dtype="float32")
    out.start()
    lat = []
    time.sleep(1.0)
    for i, path in enumerate(args.clips):
        if i and not ear.wait_quiet(QUIET_S, 60):
            log("IVR never went quiet; talking anyway")
        log(f"says {path.rsplit('/', 1)[-1]}")
        x = clip(path)
        out.write(x.reshape(-1, 1))           # blocks for the clip's duration
        ended = time.time()
        first = ear.first_sound(ended, 30)
        if first is None:
            log("  no reply within 30 s")
            continue
        lat.append(first - ended)
        log(f"  reply audio {first - ended:.2f}s after the clip ended")
    if args.linger:
        ear.wait_quiet(QUIET_S, 60)
        t0 = time.time()
        log(f"goes silent for up to {args.linger:.0f}s")
        time.sleep(args.linger)
        log(f"  IVR spoke {ear.spoke_between(t0, time.time()):.1f}s while we were silent")
    if lat:
        log("reply latency: " + ", ".join(f"{x:.2f}s" for x in lat))
    out.stop()


if __name__ == "__main__":
    main()
