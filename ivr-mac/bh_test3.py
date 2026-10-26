import time
import numpy as np
import sounddevice as sd
sr = 48000
tone = (0.3 * np.sin(2 * np.pi * 1000 * np.arange(sr // 2) / sr)).astype(np.float32)
def trial(name, ch, label):
    buf = []
    try:
        with sd.InputStream(device=name, channels=ch, samplerate=sr, dtype="float32",
                            callback=lambda d, f, t, st: buf.append(d.copy())):
            time.sleep(0.3)
            sd.play(tone, sr, device=name, blocking=True)
            time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        print(f"{label}: failed {e}", flush=True); return
    x = np.concatenate(buf) if buf else np.zeros((1, ch), np.float32)
    print(f"{label}: captured {len(x)/sr:.2f}s peak {np.abs(x).max():.3f}", flush=True)
trial("BlackHole 16ch", 16, "16ch x16 (first)")
trial("BlackHole 16ch", 1, "16ch x1")
trial("BlackHole 16ch", 2, "16ch x2")
trial("BlackHole 2ch", 1, "2ch x1")
trial("BlackHole 16ch", 2, "16ch x2 (after 2ch)")
