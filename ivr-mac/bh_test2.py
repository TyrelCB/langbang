"""Narrower loopback check: one input stream at a time, device's own rate and
channel count, tone played by a separate process (afplay)."""
import subprocess, sys, time, wave
import numpy as np
import sounddevice as sd

for name in ("BlackHole 2ch", "BlackHole 16ch"):
    info = sd.query_devices(name)
    sr = int(info["default_samplerate"])
    print(f"{name}: default rate {sr}, in {info['max_input_channels']}, out {info['max_output_channels']}", flush=True)
    tone = (0.3 * np.sin(2 * np.pi * 1000 * np.arange(sr // 2) / sr) * 32767).astype("<i2")
    with wave.open("/tmp/tone.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(tone.tobytes())
    buf = []
    try:
        with sd.InputStream(device=name, channels=2, samplerate=sr, dtype="float32",
                            callback=lambda d, f, t, st: buf.append(d.copy())):
            time.sleep(0.3)
            # SwitchAudioSource isn't installed; afplay can't pick a device, so play via sounddevice
            sd.play(tone.astype(np.float32) / 32767, sr, device=name, blocking=True)
            time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        print("  input stream failed:", e, flush=True)
        continue
    x = np.concatenate(buf) if buf else np.zeros((1, 2), np.float32)
    print(f"  captured {len(x)/sr:.2f}s, peak {np.abs(x).max():.3f}, nonzero frames {int((np.abs(x).sum(1) > 0).sum())}", flush=True)
