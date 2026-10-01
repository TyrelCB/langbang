"""Voice: read-aloud (TTS) + speech-to-text via the free Google paths
telemarketing used — gTTS (Translate TTS endpoint) and SpeechRecognition's
recognize_google. Both are keyless but unofficial/gray-ToS (fine for a
personal LAN tool; occasional 429s possible). "gcloud" providers behind a
service-account key (data/keys/, gitignored) are the supported escape hatch
— configure in CONFIG → VOICE.

All providers block on network; main.py MUST call synthesize()/transcribe()
via run_in_threadpool so the /api/chat SSE stream never starves. Providers
lazy-import, so a default install never touches google.cloud.*.

Error taxonomy: ValueError = client-fixable (bad audio body -> 400);
RuntimeError = provider/config trouble (missing key/lib, quota, network -> 502).
"""
import io
import json
import os
import re
import threading
import time
import wave

MAX_TTS_CHARS = 20_000
_KEYLESS_HINT = "shared free-tier endpoint; try again shortly or switch provider in CONFIG → VOICE"


def _voice_cfg(cfg: dict) -> dict:
    return cfg.get("voice") or {}


def _key_path(key: str) -> str:
    """Service-account key file, relative paths resolved against repo root."""
    p = os.path.expanduser(key)
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), p)
    if not os.path.isfile(p):
        raise RuntimeError(f"gcloud key file not found: {key} (put it under data/keys/)")
    return p


def speakable(text: str) -> str:
    """Markdown → prose for TTS. Code blocks become a spoken placeholder;
    markers, links and table chrome disappear; hard cap at a sentence end."""
    t = text or ""
    t = re.sub(r"```.*?```", " … code block omitted … ", t, flags=re.S)
    t = re.sub(r"```.*$", " … code block omitted … ", t, flags=re.S)  # unterminated fence
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)          # [text](url) -> text
    t = re.sub(r"https?://\S+", " link ", t)                 # bare URLs -> "link"
    t = re.sub(r"^\s*[-*_]{3,}\s*$", "", t, flags=re.M)      # hr rules
    t = re.sub(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", "", t, flags=re.M)  # table sep rows
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # heading markers
    t = re.sub(r"^\s{0,3}>\s?", "", t, flags=re.M)           # blockquotes
    t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+(?=\S)", "", t, flags=re.M)  # bullets / numbered items
    # every line is its own sentence: without a period, a bullet list collapses
    # into one run-on — TTS prosody flattens and pocket-tts's chunker
    # overflows ("Chunk has 51 tokens (max 50), may skip words")
    t = re.sub(r"([^\s.!?:;,…])[ \t]*(?=\n|$)", r"\1.", t)
    t = re.sub(r"[ \t]*\|[ \t]*", " ", t)                    # table pipes
    t = t.replace("**", "").replace("__", "").replace("~~", "")
    t = re.sub(r"`+", "", t)                                 # inline code ticks
    t = re.sub(r"\s+", " ", t).strip()
    if t.replace("… code block omitted …", "").strip() == "":
        return ""  # code fences / markdown chrome, no actual prose to say
    if len(t) > MAX_TTS_CHARS:
        cut = t.rfind(".", 0, MAX_TTS_CHARS)
        t = t[: cut + 1] if cut > MAX_TTS_CHARS // 2 else t[:MAX_TTS_CHARS]
    return t


def synthesize(text: str, cfg: dict) -> tuple[bytes, str]:
    """(mp3_bytes, media_type). ValueError -> bad text (400);
    RuntimeError -> provider/config trouble (502), message shown verbatim."""
    v = _voice_cfg(cfg)
    provider = v.get("tts_provider") or "gtts"
    if provider == "gcloud":
        return _synthesize_gcloud(text, v)
    if provider == "pocket":
        return _synthesize_pocket_wav(text, v)
    return _synthesize_gtts(text, v)


def _synthesize_gtts(text: str, v: dict) -> tuple[bytes, str]:
    try:
        from gtts import gTTS
    except ImportError as e:
        raise RuntimeError("gtts not installed — run: uv add gtts") from e
    buf = io.BytesIO()
    try:
        gTTS(
            text=text,
            lang=v.get("tts_lang") or "en",
            tld=v.get("tts_tld") or "com",  # accent: com=US, co.uk=UK, co.in=IN
        ).write_to_fp(buf)
    except Exception as e:  # noqa: BLE001 - readable one-liner beats a traceback
        raise RuntimeError(
            f"gTTS failed ({type(e).__name__}: {e}) — {_KEYLESS_HINT}; "
            "or set tts_provider=gcloud with a key file in CONFIG → VOICE"
        ) from e
    return buf.getvalue(), "audio/mpeg"


def _synthesize_gcloud(text: str, v: dict) -> tuple[bytes, str]:
    if not v.get("gcloud_key_file"):
        raise RuntimeError("gcloud TTS needs gcloud_key_file set in CONFIG → VOICE")
    try:
        from google.cloud import texttospeech
    except ImportError as e:
        raise RuntimeError(
            "gcloud TTS needs: uv pip install google-cloud-texttospeech"
        ) from e
    try:
        client = texttospeech.SpeechClient.from_service_account_json(
            _key_path(v["gcloud_key_file"])
        )
        audio = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=v.get("gcloud_tts_lang") or "en-US",
                name=v.get("gcloud_tts_voice") or "en-US-Wavenet-J",
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=0.95,
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Google Cloud TTS failed ({e}) — check key file / voice name") from e
    return audio.audio_content, "audio/mpeg"


# ---- Pocket TTS (kyutai/pocket-tts): local, CPU-only ----
# 100M-param model; benchmarked on this box (i9-12900HK): first audio in
# ~0.1 s, ~4-5x realtime, no gain past 2 threads, ~1.3 GB RSS once loaded.
# Preset voices load from kyutai's UNGATED no-cloning weights (no HF token);
# a custom voice file needs the gated repo (terms + `hf auth login`).
# Streaming lives in ttsjobs.py; this module owns the model.
POCKET_VOICES = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette",
    "daan", "eponine", "estelle", "eve", "fantine", "george", "giovanni", "jane",
    "javert", "jean", "juergen", "lola", "marius", "mary", "michael", "paul",
    "peter_yearsley", "rafael", "stuart_bell", "vera",
)
POCKET_LANGS = ("english", "french", "german", "portuguese", "italian", "spanish", "dutch")
POCKET_RATE = 24_000  # model.sample_rate (mimi); asserted at load
_pk_load = threading.Lock()  # model/voice loading
_pk_gen = threading.Lock()   # ONE generation at a time: the model is documented not thread-safe
_pk_models: dict = {}
_pk_states: dict = {}
_POCKET_GATED_HINT = (
    "custom voice files need Pocket TTS's gated weights: accept the terms at "
    "https://huggingface.co/kyutai/pocket-tts, run `.venv/bin/hf auth login`, "
    "then restart LangBang — or pick a preset voice in CONFIG → VOICE")


def pocket_ready(v: dict):
    """(model, voice_state) for the configured language/voice, loading and
    caching on first use (~6 s model, ~0.6 s voice). RuntimeError -> 502."""
    try:
        import torch
        from pocket_tts import TTSModel
    except ImportError as e:
        raise RuntimeError("pocket-tts not installed — run: uv add pocket-tts") from e
    torch.set_num_threads(max(1, int(v.get("pocket_threads") or 2)))
    lang = v.get("pocket_language") or "english"
    if lang not in POCKET_LANGS:
        raise RuntimeError(f"unknown Pocket TTS language {lang!r} (one of: {', '.join(POCKET_LANGS)})")
    voice = (v.get("pocket_voice") or "alba").strip()
    with _pk_load:
        model = _pk_models.get(lang)
        if model is None:
            try:
                model = TTSModel.load_model(language=lang)
            except Exception as e:  # noqa: BLE001 - download/HF trouble, readable
                raise RuntimeError(f"Pocket TTS model load failed ({type(e).__name__}: {e})") from e
            if model.sample_rate != POCKET_RATE:
                raise RuntimeError(f"unexpected Pocket TTS sample rate {model.sample_rate}")
            _pk_models[lang] = model
        st = _pk_states.get((lang, voice))
        if st is None:
            saved = _saved_path(voice)
            if voice in POCKET_VOICES:
                pass
            elif saved:
                # a cloned voice saved as a voice-state .safetensors: loads on
                # the plain (ungated, no-cloning) model like the presets do
                meta = _saved_meta(voice)
                if meta.get("language") and meta["language"] != lang:
                    raise RuntimeError(
                        f"voice {voice!r} was cloned for {meta['language']}; switch Pocket "
                        f"language back or clone it again for {lang}")
                from pathlib import Path
                voice = Path(saved)
            else:
                path = os.path.expanduser(voice)
                if not os.path.isfile(path):
                    raise RuntimeError(f"Pocket TTS voice {voice!r} is neither a preset, a saved voice, nor an audio file")
                if not getattr(model, "has_voice_cloning", False):
                    raise RuntimeError(_POCKET_GATED_HINT)
                voice = path
            try:
                st = model.get_state_for_audio_prompt(voice)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Pocket TTS voice load failed ({type(e).__name__}: {e})") from e
            _pk_states[(lang, (v.get("pocket_voice") or "alba").strip())] = st
    return model, st


# ---- saved (cloned) voices: data/voices/<name>.safetensors + <name>.json ----
VOICES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "voices")
VOICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_pk_clone = threading.Lock()


def _saved_path(name: str) -> str | None:
    if not VOICE_NAME_RE.match(name or ""):
        return None
    p = os.path.join(VOICES_DIR, name + ".safetensors")
    return p if os.path.isfile(p) else None


def _saved_meta(name: str) -> dict:
    try:
        with open(os.path.join(VOICES_DIR, name + ".json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def saved_voices() -> list[dict]:
    if not os.path.isdir(VOICES_DIR):
        return []
    out = []
    for f in sorted(os.listdir(VOICES_DIR)):
        if f.endswith(".safetensors"):
            name = f[:-12]
            out.append({"name": name, **{k: v for k, v in _saved_meta(name).items() if k != "name"}})
    return out


def _forget_voice(name: str) -> None:
    for k in [k for k in _pk_states if k[1] == name]:
        _pk_states.pop(k, None)


def cloning_ready() -> bool:
    """Best-effort: an HF token is present (the gated download needs one;
    accepting the model terms is still on the user)."""
    try:
        from huggingface_hub import get_token
        return bool(get_token())
    except Exception:  # noqa: BLE001
        return False


def clone_voice(name: str, audio_path: str, language: str, source: str = "") -> dict:
    """Encode up to 30 s of `audio_path` into a voice state and save it as
    data/voices/<name>.safetensors. Needs Pocket's GATED weights (they hold
    the audio encoder): loaded just for this and released after, so the
    resident model stays the light no-cloning one. RuntimeError -> 502."""
    if not VOICE_NAME_RE.match(name or ""):
        raise ValueError("voice name: 1-40 letters, digits, _ or - (start with a letter/digit)")
    if name in POCKET_VOICES:
        raise ValueError(f"{name!r} is a preset voice name — pick another")
    if language not in POCKET_LANGS:
        raise ValueError(f"unknown language {language!r}")
    try:
        import gc

        from pocket_tts import TTSModel
        from pocket_tts.models.model_state import export_model_state
    except ImportError as e:
        raise RuntimeError("pocket-tts not installed — run: uv add pocket-tts") from e
    from pathlib import Path

    with _pk_clone:
        t0 = time.time()
        try:
            model = TTSModel.load_model(language=language)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Pocket TTS model load failed ({type(e).__name__}: {e})") from e
        try:
            if not getattr(model, "has_voice_cloning", False):
                raise RuntimeError(
                    "voice cloning needs Pocket TTS's gated weights: accept the terms at "
                    "https://huggingface.co/kyutai/pocket-tts, then run `.venv/bin/hf auth login` "
                    "on the LangBang host (no restart needed) and try again")
            try:
                st = model.get_state_for_audio_prompt(Path(audio_path), truncate=True)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"could not encode that audio ({type(e).__name__}: {e})") from e
            os.makedirs(VOICES_DIR, exist_ok=True)
            tmp = os.path.join(VOICES_DIR, f".{name}.tmp.safetensors")
            export_model_state(st, tmp)
            os.replace(tmp, os.path.join(VOICES_DIR, name + ".safetensors"))
            meta = {"name": name, "language": language, "created": time.time(),
                    "source": source[:200], "clone_s": round(time.time() - t0, 1)}
            with open(os.path.join(VOICES_DIR, name + ".json"), "w") as fh:
                json.dump(meta, fh)
            _forget_voice(name)  # re-cloned under the same name → drop the stale state
            return meta
        finally:
            del model
            gc.collect()
            # glibc keeps the freed ~1 GB clone model in its arenas otherwise:
            # measured +141 MB resident after del+gc, +36 MB after trim
            try:
                import ctypes
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except (OSError, AttributeError):
                pass  # not glibc — nothing to trim


def delete_voice(name: str) -> bool:
    p = _saved_path(name)
    if not p:
        return False
    os.remove(p)
    try:
        os.remove(os.path.join(VOICES_DIR, name + ".json"))
    except FileNotFoundError:
        pass
    _forget_voice(name)
    return True


def pocket_pcm(text: str, v: dict, stop: threading.Event | None = None):
    """Yield mono PCM16 little-endian @ POCKET_RATE as it's generated. Holds
    the generation lock for the whole utterance (callers queue behind it)."""
    import torch

    model, st = pocket_ready(v)
    with _pk_gen:
        for ch in model.generate_audio_stream(st, text, stop=stop):  # copy_state=True: voice reusable
            yield (ch.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()


def _synthesize_pocket_wav(text: str, v: dict) -> tuple[bytes, str]:
    """Whole-clip WAV — the no-ffmpeg fallback (main.py streams MP3 otherwise)."""
    pcm = b"".join(pocket_pcm(text, v))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(POCKET_RATE)
        w.writeframes(pcm)
    return buf.getvalue(), "audio/wav"


def prewarm(cfg: dict) -> None:
    """Load the model + voice in the background at startup when Pocket is
    the active provider, so the first 🔊 doesn't pay ~7 s of loading."""
    v = _voice_cfg(cfg)
    if (v.get("tts_provider") or "gtts") != "pocket":
        return
    try:
        pocket_ready(v)
    except Exception as e:  # noqa: BLE001 - surfaced again on first real use
        import logging
        logging.getLogger("langbang.voice").warning("pocket prewarm failed: %s", e)


def transcribe(data: bytes, cfg: dict) -> str:
    """WAV (16 kHz mono PCM16) or bare PCM16 @16 kHz -> text. '' on silence.
    ValueError = bad audio body (400); RuntimeError = provider/config trouble (502)."""
    v = _voice_cfg(cfg)
    pcm = _pcm16_mono_16k(data)
    provider = v.get("stt_provider") or "sr"
    if provider == "gcloud":
        return _transcribe_gcloud(pcm, v)
    return _transcribe_sr(pcm, v)


def _pcm16_mono_16k(data: bytes) -> bytes:
    if data[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                if (
                    w.getnchannels() != 1
                    or w.getsampwidth() != 2
                    or w.getframerate() != 16_000
                ):
                    raise ValueError("audio must be 16 kHz mono PCM16 WAV")
                return w.readframes(w.getnframes())
        except wave.Error as e:
            raise ValueError(f"bad WAV: {e}") from e
    if len(data) % 2:
        raise ValueError("audio must be 16-bit samples (even byte count)")
    return data  # no RIFF header: treat as bare PCM16 @16 kHz mono


def _transcribe_sr(pcm: bytes, v: dict) -> str:
    try:
        import speech_recognition as sr
    except ImportError as e:
        raise RuntimeError("speechrecognition not installed — run: uv add speechrecognition") from e
    audio = sr.AudioData(pcm, sample_rate=16_000, sample_width=2)
    try:
        return sr.Recognizer().recognize_google(
            audio, language=v.get("stt_lang") or "en-US"
        )
    except sr.UnknownValueError:
        return ""  # silence / unintelligible — not an error
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Google STT failed ({type(e).__name__}: {e}) — {_KEYLESS_HINT}") from e


def _transcribe_gcloud(pcm: bytes, v: dict) -> str:
    if not v.get("gcloud_key_file"):
        raise RuntimeError("gcloud STT needs gcloud_key_file set in CONFIG → VOICE")
    try:
        from google.cloud import speech
    except ImportError as e:
        raise RuntimeError("gcloud STT needs: uv pip install google-cloud-speech") from e
    try:
        client = speech.SpeechClient.from_service_account_json(
            _key_path(v["gcloud_key_file"])
        )
        resp = client.recognize(
            config={
                "encoding": speech.RecognitionConfig.AudioEncoding.LINEAR16,
                "sample_rate_hertz": 16_000,
                "language_code": v.get("stt_lang") or "en-US",
            },
            audio=speech.RecognitionAudio(content=pcm),
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Google Cloud STT failed ({e}) — check key file") from e
    return " ".join(r.alternatives[0].transcript for r in resp.results if r.alternatives)
