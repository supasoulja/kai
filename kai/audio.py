"""
AudioManager — singleton for Kai's speech capabilities.

  STT: faster-whisper (local Whisper, downloads model on first use)
  TTS: kokoro-onnx   (local Kokoro, loads from kai/models/ on first use)

Both are lazy-loaded — no startup cost if audio features aren't used.
Thread-safe: inference calls are serialized per engine via locks.
"""
import io
import logging
import re
import struct
import threading
from pathlib import Path
from typing import Iterator

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_RE   = re.compile(r"(?<=[,;:])\s+")

# URL handling for speech. Kokoro reads a full URL out one path-segment at a time
# ("archive dot org slash details slash poweroflogic00laym"), which is unbearable
# to listen to. We collapse links to just their host before synthesis. These run
# ONLY on the copy handed to the TTS engine — the on-screen text keeps the real,
# clickable link.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")              # [label](url) → label
_URL_RE     = re.compile(r"https?://(?:www\.)?([a-z0-9.-]+)(?:[/?#]\S*)?", re.IGNORECASE)
_WWW_RE     = re.compile(r"\bwww\.([a-z0-9.-]+)(?:[/?#]\S*)?", re.IGNORECASE)

log = logging.getLogger(__name__)

# ── Lazy-load state ────────────────────────────────────────────────────────────

_whisper_model  = None
_kokoro_model   = None
_whisper_lock   = threading.Lock()
_kokoro_lock    = threading.Lock()
_whisper_ready  = False
_kokoro_ready   = False


def _get_whisper():
    global _whisper_model, _whisper_ready
    if _whisper_ready:
        return _whisper_model
    with _whisper_lock:
        if _whisper_ready:
            return _whisper_model
        from faster_whisper import WhisperModel
        from kai.config import WHISPER_MODEL
        log.info(f"[audio] loading Whisper {WHISPER_MODEL!r}...")
        # device='auto' uses GPU if available, falls back to CPU
        _whisper_model = WhisperModel(WHISPER_MODEL, device="auto", compute_type="int8")
        _whisper_ready = True
        log.info("[audio] Whisper ready")
        return _whisper_model


def _get_kokoro():
    global _kokoro_model, _kokoro_ready
    if _kokoro_ready:
        return _kokoro_model
    with _kokoro_lock:
        if _kokoro_ready:
            return _kokoro_model
        from kokoro_onnx import Kokoro
        from kai.config import AUDIO_MODELS_DIR
        onnx = AUDIO_MODELS_DIR / "kokoro-v1.0.onnx"
        voices = AUDIO_MODELS_DIR / "voices-v1.0.bin"
        if not onnx.exists() or not voices.exists():
            raise FileNotFoundError(
                f"Kokoro model files not found in {AUDIO_MODELS_DIR}. "
                "Expected kokoro-v1.0.onnx and voices-v1.0.bin."
            )
        log.info("[audio] loading Kokoro TTS...")
        _kokoro_model = Kokoro(str(onnx), str(voices))
        _kokoro_ready = True
        log.info("[audio] Kokoro ready")
        return _kokoro_model


# ── WAV helpers ────────────────────────────────────────────────────────────────

def _samples_to_wav(samples, sample_rate: int) -> bytes:
    """Convert float32 numpy samples to WAV bytes."""
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    data = pcm.tobytes()

    buf = io.BytesIO()
    num_channels  = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(data)
    chunk_size = 36 + data_size

    buf.write(b"RIFF")
    buf.write(struct.pack("<I", chunk_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))           # subchunk size
    buf.write(struct.pack("<H", 1))            # PCM format
    buf.write(struct.pack("<H", num_channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bits_per_sample))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(data)
    return buf.getvalue()


# ── Public API ─────────────────────────────────────────────────────────────────

def transcribe(audio_bytes: bytes) -> str:
    """
    Transcribe WAV audio bytes to text using faster-whisper.
    The browser sends 16kHz mono WAV; Whisper works best at that rate.
    Returns the transcript string (empty string if nothing heard).
    """
    with _whisper_lock:
        model = _get_whisper()
        buf = io.BytesIO(audio_bytes)
        segments, _info = model.transcribe(
            buf,
            beam_size=5,
            language="en",
            vad_filter=True,          # skip silent segments
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


def synthesize(text: str, voice: str | None = None, speed: float | None = None) -> bytes:
    """
    Synthesize text to WAV bytes using Kokoro.
    Returns raw WAV bytes suitable for streaming to the browser.
    """
    from kai.config import TTS_VOICE, TTS_SPEED
    voice = voice or TTS_VOICE
    speed = speed if speed is not None else TTS_SPEED
    text = _strip_urls_for_speech(text)

    with _kokoro_lock:
        kokoro = _get_kokoro()
        samples, sample_rate = kokoro.create(text, voice=voice, speed=speed)

    return _samples_to_wav(samples, sample_rate)


def _strip_urls_for_speech(text: str) -> str:
    """Rewrite links so TTS speaks the host, not the whole path.

    Markdown links ``[label](url)`` collapse to their label; bare ``http(s)://``
    and ``www.`` URLs collapse to their host ("archive.org"). Trailing sentence
    punctuation caught by the host class is trimmed back off. Display text is
    never touched — callers pass a throwaway copy bound for the synth engine.
    """
    def _host(m: "re.Match") -> str:
        return m.group(1).rstrip(".,;:!?")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(_host, text)
    text = _WWW_RE.sub(_host, text)
    return text


def _wrap_at_words(text: str, max_chars: int) -> list[str]:
    """Hard-wrap text at word boundaries, used as a last resort."""
    pieces = []
    while len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def _split_into_chunks(text: str, first_chunk_max: int = 80, max_chars: int = 300) -> list[str]:
    """
    Split text into speakable pieces, front-loaded for a fast start. Kokoro
    runs CPU-only here and synthesis time scales with text length, so a long
    first sentence alone can be several seconds of silence before anything
    plays. Trim just the *first* chunk down to a clause (or word) boundary so
    audio starts almost immediately; later chunks stay whole sentences — by
    then synthesis runs ahead of playback, so naturalness matters more than
    shaving more latency off chunks nobody is waiting on.
    """
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        return []

    chunks: list[str] = []
    head = sentences[0]
    if len(head) > first_chunk_max:
        clauses = [c.strip() for c in _CLAUSE_RE.split(head) if c.strip()]
        if len(clauses) > 1 and len(clauses[0]) <= first_chunk_max:
            chunks.append(clauses[0])
            sentences[0] = " ".join(clauses[1:])
        else:
            pieces = _wrap_at_words(head, first_chunk_max)
            chunks.append(pieces[0])
            sentences[0] = " ".join(pieces[1:])

    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            chunks.append(sentence)
        else:
            chunks.extend(_wrap_at_words(sentence, max_chars))
    return chunks


def synthesize_chunks(text: str, voice: str | None = None, speed: float | None = None) -> Iterator[bytes]:
    """
    Synthesize text incrementally, yielding one complete WAV per sentence-chunk
    as soon as it's ready. Synthesizing the whole reply up front makes the
    listener wait several seconds in silence before anything plays — yielding
    per-chunk lets playback start after the first sentence instead.
    """
    from kai.config import TTS_VOICE, TTS_SPEED
    voice = voice or TTS_VOICE
    speed = speed if speed is not None else TTS_SPEED
    text = _strip_urls_for_speech(text)

    with _kokoro_lock:
        kokoro = _get_kokoro()
        for chunk in _split_into_chunks(text):
            samples, sample_rate = kokoro.create(chunk, voice=voice, speed=speed)
            yield _samples_to_wav(samples, sample_rate)


def list_voices() -> list[str]:
    """Return available Kokoro voice names."""
    try:
        return _get_kokoro().get_voices()
    except Exception:
        return []


def audio_ready() -> dict[str, bool]:
    """Quick status check — which audio engines are loaded."""
    return {"stt": _whisper_ready, "tts": _kokoro_ready}
