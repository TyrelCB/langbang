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
import os
import re
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
    t = re.sub(r"^\s*[-*+]\s+(?=\S)", "", t, flags=re.M)     # bullets
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
