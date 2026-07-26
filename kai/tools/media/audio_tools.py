"""
Audio analysis tools — transcribe and understand audio from any source.

  audio.transcribe — extract speech from any audio or video file/URL

Uses faster-whisper (local Whisper) for transcription.
Uses ffmpeg to extract audio from video files before transcribing.
"""

import io
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import httpx

from kai.tools.registry import registry

_MAX_DURATION_SEC = 7200  # 2 hours max
_MAX_FILE_BYTES = 500 * 1024 * 1024  # 500 MB


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _extract_audio_to_wav(input_path: str) -> bytes:
    """
    Use ffmpeg to convert any audio/video to 16kHz mono WAV (Whisper's preferred format).
    Returns raw WAV bytes.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vn",  # drop video stream
            "-acodec",
            "pcm_s16le",  # 16-bit PCM
            "-ar",
            "16000",  # 16kHz sample rate
            "-ac",
            "1",  # mono
            "-f",
            "wav",
            "pipe:1",  # output to stdout
        ],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"ffmpeg failed: {stderr}")
    return result.stdout


def _download_file(url: str) -> tuple[bytes, str]:
    """Download a file from a URL. Returns (bytes, suggested_filename)."""
    parsed = urllib.parse.urlparse(url)
    filename = Path(parsed.path).name or "audio_file"

    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.content

    if len(data) > _MAX_FILE_BYTES:
        raise ValueError(
            f"File too large ({len(data) // 1024 // 1024} MB). "
            f"Limit is {_MAX_FILE_BYTES // 1024 // 1024} MB."
        )
    return data, filename


def _transcribe_wav_bytes(wav_bytes: bytes, language: str = "en") -> dict:
    """Run faster-whisper on WAV bytes. Returns {text, language, duration}."""
    from kai.audio import _get_whisper

    model = _get_whisper()

    buf = io.BytesIO(wav_bytes)
    segments, info = model.transcribe(
        buf,
        beam_size=5,
        language=language if language != "auto" else None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    parts = []
    last_end = 0.0
    for seg in segments:
        parts.append(seg.text.strip())
        last_end = seg.end

    return {
        "text": " ".join(p for p in parts if p),
        "language": info.language,
        "duration_sec": round(last_end, 1),
    }


# ── audio.transcribe ───────────────────────────────────────────────────────────


@registry.tool(
    name="audio.transcribe",
    description=(
        "Transcribe speech from any audio or video file — local files or URLs. "
        "Supports: MP3, WAV, MP4, MKV, WebM, OGG, M4A, FLAC, AVI, MOV, and more. "
        "Use this to: transcribe a podcast, extract dialogue from a video, "
        "get the lyrics from a song with vocals, or read what someone said in a recording. "
        "For YouTube or web videos, download the file first or provide a direct media URL. "
        "Returns the full transcript with detected language and duration."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": (
                "File path to a local audio/video file (e.g. /home/user/video.mp4) "
                "OR a direct URL to an audio/video file. "
                "For YouTube, provide the direct video URL after downloading with yt-dlp."
            ),
        },
        "language": {
            "type": "string",
            "description": (
                "Language code for transcription (e.g. 'en', 'es', 'fr', 'de', 'ja'). "
                "Use 'auto' to let Whisper detect the language automatically. "
                "Defaults to 'en'."
            ),
        },
    },
)
def transcribe_media(source: str, language: str = "en") -> str:
    if not _ffmpeg_available():
        return (
            "ffmpeg is not installed. Install it with: sudo apt-get install ffmpeg\n"
            "ffmpeg is required to extract audio from media files."
        )

    source = source.strip()
    if not source:
        return "No source provided. Pass a file path or URL."

    tmp_input = None
    try:
        # ── Source: URL ───────────────────────────────────────────────────────
        if source.startswith(("http://", "https://")):
            data, filename = _download_file(source)
            suffix = Path(filename).suffix or ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(data)
                tmp_input = f.name
            input_path = tmp_input

        # ── Source: local file ────────────────────────────────────────────────
        else:
            path = Path(source)
            if not path.exists():
                return f"File not found: {source}"
            if not path.is_file():
                return f"Not a file: {source}"
            if path.stat().st_size > _MAX_FILE_BYTES:
                return (
                    f"File too large ({path.stat().st_size // 1024 // 1024} MB). "
                    f"Limit is {_MAX_FILE_BYTES // 1024 // 1024} MB."
                )
            input_path = str(path)

        # ── Extract audio with ffmpeg ─────────────────────────────────────────
        try:
            wav_bytes = _extract_audio_to_wav(input_path)
        except RuntimeError as e:
            return f"Audio extraction failed: {e}"
        except subprocess.TimeoutExpired:
            return "Audio extraction timed out (5 min limit)."

        if not wav_bytes:
            return "ffmpeg produced no audio output. The file may have no audio track."

        # ── Transcribe ────────────────────────────────────────────────────────
        result = _transcribe_wav_bytes(wav_bytes, language=language)

        text = result["text"]
        lang = result["language"]
        duration = result["duration_sec"]

        if not text:
            return (
                f"No speech detected in the audio "
                f"(duration: {duration:.0f}s, detected language: {lang}). "
                "The file may contain music, silence, or audio Whisper couldn't recognize."
            )

        mins = int(duration) // 60
        secs = int(duration) % 60
        header = f"[Transcription — {mins}m {secs}s — language: {lang}]"
        return f"{header}\n\n{text}"

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Transcription error: {e}"
    finally:
        if tmp_input:
            try:
                Path(tmp_input).unlink()
            except Exception:
                pass
