"""
Vision tool — lets Kai actually see images using Gemma 4's multimodal input.

  vision.describe — analyze an image from a file path or URL and describe what's in it

Works by sending the image as base64 to Ollama's chat endpoint with gemma4:12b
(always resident, vision-capable). Use after browser.screenshot to analyze pages,
or pass any local image file or image URL directly.
"""

import base64
import json
from pathlib import Path

import httpx

from kai.config import OLLAMA_BASE_URL
from kai.tools.registry import registry

_VISION_MODEL = "gemma4:12b"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB hard limit


def _load_image_b64(source: str) -> tuple[str, str]:
    """
    Load an image from a file path or URL, return (base64_string, mime_type).
    Raises ValueError on failure.
    """
    source = source.strip()

    # URL — download first
    if source.startswith(("http://", "https://")):
        try:
            resp = httpx.get(source, follow_redirects=True, timeout=15.0)
            resp.raise_for_status()
            data = resp.content
            content_type = resp.headers.get("content-type", "image/png")
            mime = content_type.split(";")[0].strip()
        except Exception as e:
            raise ValueError(f"Could not download image from {source}: {e}") from e
    else:
        # Local file path
        path = Path(source)
        if not path.exists():
            raise ValueError(f"File not found: {source}")
        if not path.is_file():
            raise ValueError(f"Not a file: {source}")
        data = path.read_bytes()
        suffix = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(suffix, "image/png")

    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image too large ({len(data) // 1024 // 1024} MB). "
            f"Limit is {_MAX_IMAGE_BYTES // 1024 // 1024} MB."
        )

    return base64.b64encode(data).decode("utf-8"), mime


def _call_vision(image_b64: str, prompt: str) -> str:
    """Send image + prompt to Ollama and return the response text."""
    payload = {
        "model": _VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            timeout=60.0,
        )
        resp.raise_for_status()
        result = resp.json()
        return result["message"]["content"].strip()
    except httpx.TimeoutException:
        return "Vision request timed out (60s). The model may be busy — try again."
    except Exception as e:
        return f"Vision model error: {e}"


# ── vision.describe ────────────────────────────────────────────────────────────


@registry.tool(
    name="vision.describe",
    description=(
        "Analyze an image and describe what's in it using Gemma 4's vision capability. "
        "Accepts a local file path (e.g. from browser.screenshot) or a direct image URL. "
        "Use this to: read text in screenshots, identify UI elements, describe charts or graphs, "
        "analyze photos, or extract information from images that can't be expressed as plain text. "
        "Ask a specific question for best results."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": (
                "File path to the image (e.g. /tmp/kai_screenshot.png) "
                "or a direct URL to an image file (must end in .png, .jpg, etc.)"
            ),
        },
        "question": {
            "type": "string",
            "description": (
                "What to ask about the image. "
                "Examples: 'What does this page show?', 'Read all the text in this image', "
                "'Describe the chart and its data', 'What error message is shown?'. "
                "Defaults to a general description if left empty."
            ),
        },
    },
)
def describe(source: str, question: str = "") -> str:
    try:
        image_b64, mime = _load_image_b64(source)
    except ValueError as e:
        return str(e)

    prompt = (
        question.strip()
        if question.strip()
        else (
            "Describe this image in detail. "
            "If there is text, read it. "
            "If it's a screenshot, describe what the page or application shows."
        )
    )

    return _call_vision(image_b64, prompt)
