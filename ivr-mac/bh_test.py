"""Loopback check: a tone burst played into each BlackHole device must come
back on that device's input, and only there (no cross-talk)."""
import time
import numpy as np
import sounddevice as sd

SR = 48000
print(sd.query_devices())
tone = (0.3 * np.sin(2 * np.pi * 1000 * np.arange(int(0.5 * SR)) / SR)).astype(np.float32)
burst = np.concatenate([np.zeros(int(0.2 * SR), np.float32), tone, np.zeros(int(0.5 * SR), np.float32)])

def db(x):
    return 20 * np.log10(max(np.sqrt(np.mean(x ** 2)), 1e-9))

for play_dev in ("BlackHole 2ch", "BlackHole 16ch"):
    caps = {}
    streams = []
    for rec_dev in ("BlackHole 2ch", "BlackHole 16ch"):
        buf = []
        s = sd.InputStream(device=rec_dev, channels=1, samplerate=SR, dtype="float32",
                           callback=lambda d, f, t, st, b=buf: b.append(d[:, 0].copy()))
        s.start(); streams.append(s); caps[rec_dev] = buf
    time.sleep(0.2)
    t0 = time.time()
    sd.play(burst, SR, device=play_dev, blocking=True)
    time.sleep(0.3)
    for s in streams: s.stop(); s.close()
    for rec_dev, buf in caps.items():
        x = np.concatenate(buf) if buf else np.zeros(1, np.float32)
        on = np.where(np.abs(x) > 0.05)[0]
        print(f"play -> {play_dev:15s} | heard on {rec_dev:15s}: {db(x):6.1f} dB rms, "
              f"peak {np.abs(x).max():.2f}, tone samples {len(on)}")
