"""ivr_audio_io.py — the platform audio layer under ivr_loop.py.

ivr_loop needs three things from the box it runs on:

  capture(source)   the caller's audio, s16le mono 16 kHz, read in frames
  playback(sink)    our reply audio, s16le mono 24 kHz, written as it streams
                    in; stop() cuts it at once (barge-in)
  Recorder(...)     both legs of the call to wav files, joined into a stereo
                    m4a at the end (caller left, agent right)

Two backends, picked by IVR_AUDIO (default: by platform):

  pipewire  dell-fedora-32g. ffmpeg -f pulse taps a monitor source, pacat
            plays into a sink; see README.md for the graph.
  mac       the Mac mini. BlackHole virtual devices, opened with sounddevice:
              BlackHole 2ch   Chrome's speaker output  -> we read the caller
              BlackHole 16ch  we write our replies     -> Chrome's microphone
            One input and one output stream run for the whole process, so
            both recorded legs share one clock. A process may not record a
            BlackHole device it is also playing into (PortAudio -10863), so
            the agent leg is recorded from what we hand the output stream,
            not by reading 16ch back. Audio input needs the desktop session:
            run under a gui/501 LaunchAgent with microphone consent (see
            MAC_MIGRATION.md); from ssh, capture returns silence.
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave

BACKEND = os.environ.get("IVR_AUDIO") or ("mac" if sys.platform == "darwin" else "pipewire")
# a launchd job's PATH has no Homebrew: look there too
FF = shutil.which("ffmpeg") or next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), "ffmpeg")

if BACKEND == "mac":
    DEFAULT_DOWNLINK, DEFAULT_PLAY_SINK = "BlackHole 2ch", "BlackHole 16ch"
else:
    DEFAULT_DOWNLINK, DEFAULT_PLAY_SINK = "webrtc_render.monitor", "voice_prompt"


def _join(path: str, caller_wav: str, agent_wav: str) -> bool:
    """Both legs -> one stereo m4a (caller left, agent right)."""
    joined = subprocess.run(
        [FF, "-v", "error", "-y", "-i", caller_wav, "-i", agent_wav,
         "-filter_complex", "[0:a]aresample=16000[c];[1:a]aresample=16000[a];"
                            "[c][a]join=inputs=2:channel_layout=stereo[s]",
         "-map", "[s]", "-c:a", "aac", "-b:a", "48k", path],
        capture_output=True)
    return joined.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 1000


# ---- pipewire ---------------------------------------------------------------

class _PwCapture:
    def __init__(self, source: str, rate: int, fragment_bytes: int):
        self.p = subprocess.Popen(
            [FF, "-v", "error", "-fragment_size", str(fragment_bytes), "-f", "pulse", "-i", source,
             "-ar", str(rate), "-ac", "1", "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def read(self, n: int) -> bytes:
        return self.p.stdout.read(n)

    def close(self):
        self.p.kill()


class _PwPlayback:
    """pacat into the prompt sink. write() never blocks the caller for long;
    stop() cuts playback immediately (barge-in)."""

    def __init__(self, sink: str, rate: int):
        self.p = subprocess.Popen(
            ["pacat", "--playback", f"--device={sink}", "--format=s16le",
             f"--rate={rate}", "--channels=1", "--latency-msec=60",
             "--client-name=ivr_loop", "--stream-name=reply"],
            stdin=subprocess.PIPE)
        self.started = None

    def write(self, pcm: bytes):
        if self.started is None:
            self.started = time.time()
        try:
            self.p.stdin.write(pcm)
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def finish(self):
        try:
            self.p.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    def done(self) -> bool:
        return self.p.poll() is not None

    def stop(self):
        self.p.kill()
        self.p.wait()


class _PwRecorder:
    """One ffmpeg PER LEG: the pulse muxer silently duplicates BOTH inputs of
    one process onto the first input's source (verified by tone injection
    2026-10-14), so a dual-input capture recorded the same stream twice.
    Single-input captures do honor the device."""

    def __init__(self, path: str, caller_source: str, play_sink: str):
        self.path = path
        self.parts = [f"{path}.caller.wav", f"{path}.agent.wav"]
        self.procs = [subprocess.Popen(
            [FF, "-v", "error", "-f", "pulse", "-i", src,
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-y", part],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for src, part in ((caller_source, self.parts[0]), (play_sink + ".monitor", self.parts[1]))]

    def close(self) -> bool:
        for p in self.procs:
            try:
                p.send_signal(2)   # SIGINT: ffmpeg finalizes the wav cleanly
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        return _join(self.path, *self.parts)


# ---- mac (BlackHole + sounddevice) ------------------------------------------

class _MacEngine:
    """The process's one input stream (caller) and one output stream (our
    replies), running from first use until exit. Callbacks only move bytes;
    file writes happen on a writer thread."""

    BLOCK_S = 0.02

    def __init__(self, in_dev: str, out_dev: str, in_rate: int, out_rate: int):
        import sounddevice as sd  # only the mac backend needs it
        self.in_rate, self.out_rate = in_rate, out_rate
        self.lock = threading.Lock()
        self.readers: list["_MacCapture"] = []
        self.current: "_MacPlayback | None" = None
        self.rec: dict | None = None           # {"caller": wave, "agent": wave}
        self.writes: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer, daemon=True).start()
        self.inp = sd.RawInputStream(device=in_dev, samplerate=in_rate, channels=1, dtype="int16",
                                     blocksize=int(in_rate * self.BLOCK_S), callback=self._in)
        self.out = sd.RawOutputStream(device=out_dev, samplerate=out_rate, channels=1, dtype="int16",
                                      blocksize=int(out_rate * self.BLOCK_S), callback=self._out)
        self.inp.start()
        self.out.start()

    def _in(self, data, frames, t, status):
        b = bytes(data)
        with self.lock:
            for r in self.readers:
                r.feed(b)
            if self.rec:
                self.writes.put((self.rec["caller"], b))

    def _out(self, data, frames, t, status):
        n = frames * 2
        with self.lock:
            p = self.current
            chunk = p.take(n) if p else b""
            if self.rec:
                self.writes.put((self.rec["agent"], chunk + bytes(n - len(chunk))))
        data[:len(chunk)] = chunk
        data[len(chunk):] = bytes(n - len(chunk))

    def _writer(self):
        while True:
            w, b = self.writes.get()
            if w is None:
                b.set()                        # flush marker
                continue
            try:
                w.writeframes(b)
            except Exception:  # noqa: BLE001 — a closed file after stop: drop
                pass

    def start_recording(self, caller_wav: str, agent_wav: str):
        legs = {}
        for leg, path, rate in (("caller", caller_wav, self.in_rate), ("agent", agent_wav, self.out_rate)):
            w = wave.open(path, "wb")
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
            legs[leg] = w
        with self.lock:
            self.rec = legs

    def stop_recording(self):
        with self.lock:
            legs, self.rec = self.rec, None
        done = threading.Event()
        self.writes.put((None, done))
        done.wait(5)
        for w in (legs or {}).values():
            w.close()


_engine: _MacEngine | None = None


def _mac(in_dev: str | None = None, out_dev: str | None = None) -> _MacEngine:
    global _engine
    if _engine is None:
        _engine = _MacEngine(in_dev or DEFAULT_DOWNLINK, out_dev or DEFAULT_PLAY_SINK, 16000, 24000)
    return _engine


class _MacCapture:
    def __init__(self, source: str, rate: int):
        eng = _mac(in_dev=source)
        if rate != eng.in_rate:
            raise ValueError(f"mac capture runs at {eng.in_rate} Hz, asked for {rate}")
        self.buf, self.cv, self.closed = bytearray(), threading.Condition(), False
        with eng.lock:
            eng.readers.append(self)
        self.eng = eng

    def feed(self, b: bytes):
        with self.cv:
            self.buf += b
            self.cv.notify()

    def read(self, n: int) -> bytes:
        with self.cv:
            while len(self.buf) < n and not self.closed:
                self.cv.wait(0.5)
            out, self.buf[:n] = bytes(self.buf[:n]), b""
            return out

    def close(self):
        with self.eng.lock:
            if self in self.eng.readers:
                self.eng.readers.remove(self)
        with self.cv:
            self.closed = True
            self.cv.notify_all()


class _MacPlayback:
    """Queues reply audio for the engine's output stream. write() returns at
    once (the reply streams in faster than real time); done() once all of it
    has been handed to the device and had TAIL_S to play out; stop() drops
    the rest."""

    TAIL_S = 0.15   # device + BlackHole buffering after the last block is handed over

    def __init__(self, sink: str, rate: int):
        self.eng = _mac(out_dev=sink)
        if rate != self.eng.out_rate:
            raise ValueError(f"mac playback runs at {self.eng.out_rate} Hz, asked for {rate}")
        self.buf, self.finished, self.started, self.drained = bytearray(), False, None, None
        with self.eng.lock:
            self.eng.current = self             # a new reply replaces whatever was playing

    def take(self, n: int) -> bytes:            # engine lock held
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def write(self, pcm: bytes):
        if self.started is None:
            self.started = time.time()
        with self.eng.lock:
            self.buf += pcm

    def finish(self):
        with self.eng.lock:
            self.finished = True

    def done(self) -> bool:
        with self.eng.lock:
            if not (self.finished and not self.buf):
                return False
            if self.drained is None:
                self.drained = time.time()
            if time.time() - self.drained < self.TAIL_S:
                return False
            if self.eng.current is self:
                self.eng.current = None
            return True

    def stop(self):
        with self.eng.lock:
            self.buf.clear()
            self.finished = True
            if self.eng.current is self:
                self.eng.current = None


class _MacRecorder:
    def __init__(self, path: str, caller_source: str, play_sink: str):
        self.path = path
        self.parts = [f"{path}.caller.wav", f"{path}.agent.wav"]
        self.raw_agent = f"{path}.agent24k.wav"
        self.eng = _mac(caller_source, play_sink)
        self.eng.start_recording(self.parts[0], self.raw_agent)

    def close(self) -> bool:
        self.eng.stop_recording()
        # the agent leg is written at the reply rate; deliver it at 16 kHz
        # like the caller leg and the pipewire recorder (ivr_report, ivr_probe)
        subprocess.run([FF, "-v", "error", "-y", "-i", self.raw_agent, "-ar", "16000", self.parts[1]],
                       capture_output=True)
        try:
            os.remove(self.raw_agent)
        except OSError:
            pass
        return _join(self.path, *self.parts)


# ---- public -----------------------------------------------------------------

def capture(source: str, rate: int, fragment_bytes: int):
    """The caller's audio as s16le mono at `rate`: .read(n) -> bytes (short at
    end of stream), .close()."""
    if BACKEND == "mac":
        return _MacCapture(source, rate)
    return _PwCapture(source, rate, fragment_bytes)


def playback(sink: str, rate: int):
    """One reply: .write(pcm), .finish(), .done(), .stop(), .started."""
    if BACKEND == "mac":
        return _MacPlayback(sink, rate)
    return _PwPlayback(sink, rate)


def Recorder(path: str, caller_source: str, play_sink: str):  # noqa: N802 — used like a class
    """Both legs of the call: <path>.caller.wav, <path>.agent.wav (16 kHz
    mono) and <path> (stereo m4a, caller left / agent right) on close()."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if BACKEND == "mac":
        return _MacRecorder(path, caller_source, play_sink)
    return _PwRecorder(path, caller_source, play_sink)
