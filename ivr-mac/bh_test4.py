"""The IVR's real shape, two processes:
  loop   (ivr_loop):  records BlackHole 2ch (caller leg), plays 500 Hz into BlackHole 16ch (bot leg)
  chrome (stand-in):  plays 1 kHz into BlackHole 2ch, records BlackHole 16ch
Each side should hear only the other's tone, at the level it was sent."""
import subprocess, sys, time
import numpy as np
import sounddevice as sd

SR = 48000
def tone(hz, secs=1.0):
    return (0.3 * np.sin(2 * np.pi * hz * np.arange(int(secs * SR)) / SR)).astype(np.float32)
def freq(x):
    x = x - x.mean()
    if np.abs(x).max() < 1e-4:
        return 0
    return int(np.argmax(np.abs(np.fft.rfft(x))) * SR / len(x))

def side(rec_dev, play_dev, hz, label):
    got = []
    with sd.InputStream(device=rec_dev, channels=1, samplerate=SR, dtype="float32",
                        callback=lambda d, f, t, st: got.append(d[:, 0].copy())), \
         sd.OutputStream(device=play_dev, channels=1, samplerate=SR, dtype="float32") as out:
        time.sleep(0.5)
        t0 = time.time()
        out.write(tone(hz).reshape(-1, 1))
        time.sleep(1.0)
    x = np.concatenate(got)
    loud = x[np.abs(x) > 0.05]
    print(f"{label}: recorded {len(x)/SR:.1f}s from {rec_dev}, peak {np.abs(x).max():.3f}, "
          f"dominant {freq(x)} Hz, loud samples {len(loud)}", flush=True)

if len(sys.argv) > 1 and sys.argv[1] == "chrome":
    side("BlackHole 16ch", "BlackHole 2ch", 1000, "chrome stand-in (expects 500 Hz)")
else:
    p = subprocess.Popen([sys.executable, __file__, "chrome"])
    time.sleep(0.2)
    side("BlackHole 2ch", "BlackHole 16ch", 500, "ivr_loop stand-in (expects 1000 Hz)")
    p.wait()
