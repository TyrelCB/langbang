"""Replay recorded caller legs through ivr_loop.listen() offline; print the
utterance boundaries it produces (seconds into the recording)."""
import collections, glob, importlib.util, json, os, queue, sys, wave
import numpy as np
RIG = os.path.expanduser("~/projects/ivr-mac")
os.environ["IVR_NO_GV"] = "1"          # offline: never ask the live GV tab whether a call is up
sys.path.insert(0, RIG)
spec = importlib.util.spec_from_file_location("ivr_loop", os.environ.get("LOOP_FILE", os.path.join(RIG, "ivr_loop.py")))
L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)

def load(path):
    w = wave.open(path); sr, ch = w.getframerate(), w.getnchannels()
    a = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, ch).mean(1)
    if sr != L.SR:
        a = np.interp(np.arange(0, len(a), sr / L.SR), np.arange(len(a)), a)
    return a.astype(np.int16).tobytes()

class Fake(L.Downlink):
    def __init__(self, pcm):
        self.q = queue.Queue(); self.n = 0
        self.ring = collections.deque(maxlen=int((L.PREROLL_S + L.BARGE_WIN_S) / L.FRAME_S))
        self.floor = -60.0
        self.zeros, self.probed = 0, False
        fb = L.FRAME_BYTES
        for i in range(0, len(pcm) - fb, fb):
            f = pcm[i:i + fb]; self.q.put((f, L.frame_db(f)))
        self.q.put(None)
    def frame(self, timeout):
        it = super().frame(timeout); self.n += 1; return it

def segments(pcm):
    dl, out = Fake(pcm), []
    try:
        while True:
            u = L.listen(dl, 1e9)
            if u is None: break
            t1 = dl.n * L.FRAME_S
            out.append((round(t1 - len(u) / 2 / L.SR, 2), round(t1, 2)))
    except EOFError:
        pass
    return out

if __name__ == "__main__":
    res = {}
    for p in sorted(glob.glob(os.path.join(RIG, "recordings", "*.caller.wav")))[-int(sys.argv[1]):]:
        res[os.path.basename(p).split(".")[0]] = segments(load(p))
    json.dump(res, sys.stdout)
