"""
Voice endpoints — speech-to-text (mic upload) and text-to-speech (streamed).

Self-contained: depends only on kai.audio (loaded lazily) and FastAPI. Mounted
by web.py via app.include_router(voice.router).
"""

import asyncio
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

_log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/voice/transcribe")
async def voice_transcribe(request: Request):
    """
    Receive WAV audio bytes from the browser mic and return transcribed text.
    Browser sends raw WAV (encoded from Web Audio API PCM capture).
    """
    # M1: cap upload size — an unbounded body can exhaust server RAM
    _MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
    audio_bytes = await request.body()
    if not audio_bytes:
        return {"text": "", "error": "No audio received"}
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "Audio upload exceeds 25 MB limit"},
        )
    try:
        import subprocess

        from kai.audio import transcribe

        content_type = request.headers.get("content-type", "audio/wav")
        if "wav" not in content_type:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    "pipe:0",
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    "pipe:1",
                ],
                input=audio_bytes,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                audio_bytes = result.stdout

        text = await asyncio.get_event_loop().run_in_executor(None, transcribe, audio_bytes)
        return {"text": text}
    except asyncio.CancelledError:
        raise
    except Exception:
        # M2: log the real error server-side; never return internal details to client
        _log.exception("Voice transcription failed")
        return {"text": "", "error": "Transcription failed. Check the server log for details."}


@router.post("/voice/tts")
async def voice_tts(request: Request):
    """
    Synthesize text to speech and stream it back as a sequence of framed WAV
    chunks — one per sentence — so playback can start before the whole reply
    is voiced. Wire format: repeated [4-byte big-endian length][WAV bytes].
    Body: {"text": "...", "voice": "af_heart", "speed": 1.0}
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    voice = body.get("voice") or None
    speed = body.get("speed") or None

    if not text:
        return Response(status_code=400, content="No text provided")

    # M4: cap text length to prevent DoS on the TTS engine
    _MAX_TTS_CHARS = 4000
    if len(text) > _MAX_TTS_CHARS:
        return Response(
            status_code=400,
            content=f"Text too long ({len(text)} chars). Maximum is {_MAX_TTS_CHARS}.",
        )

    from kai.audio import synthesize_chunks

    async def _frames():
        loop = asyncio.get_event_loop()
        gen = synthesize_chunks(text, voice=voice, speed=speed)
        while True:
            try:
                wav = await loop.run_in_executor(None, lambda: next(gen, None))
            except Exception as e:
                _log.error(f"[tts] synthesis failed: {e}")
                return
            if wav is None:
                return
            yield len(wav).to_bytes(4, "big") + wav

    return StreamingResponse(
        _frames(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/voice/test")
async def voice_test():
    """Public endpoint — returns a short 440Hz beep WAV. No auth, no Kokoro.
    Use to verify the browser audio pipeline works before debugging TTS."""
    import io
    import math
    import struct

    sr, dur, freq = 22050, 1.0, 440.0
    n = int(sr * dur)
    samples = b"".join(
        struct.pack("<h", int(math.sin(2 * math.pi * freq * i / sr) * 16000)) for i in range(n)
    )
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(samples)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(samples)))
    buf.write(samples)
    wav = buf.getvalue()
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Content-Length": str(len(wav)), "Cache-Control": "no-cache"},
    )


@router.get("/voice/voices")
async def voice_list():
    """
    Return available TTS voice names. Runs off the event loop — listing voices
    triggers Kokoro's lazy load on first call, which can take a while and would
    otherwise freeze every other request for the duration.
    """
    try:
        from kai.audio import list_voices

        voices = await asyncio.get_event_loop().run_in_executor(None, list_voices)
        return {"voices": voices}
    except Exception:
        return {"voices": []}
