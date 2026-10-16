"""Google Gemini image-generation client.

Calls the Gemini ``generateContent`` REST API and returns the first inline
image part as raw bytes. No SDK dependency: httpx keeps the connector light and
the request shape explicit. The API key is sent as a header and never logged.

Ported from the v1 image-generation server unchanged in
behaviour: same endpoint, same request body, same response parsing, same
caller-facing error messages.
"""

import base64
import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger("mcp.image-generation.gemini")

# Fixed Google endpoint. Deliberately not env-overridable so a misconfigured
# base URL can never turn this into an SSRF vector.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = os.getenv("IMAGE_GEN_MODEL", "gemini-2.5-flash-image")
REQUEST_TIMEOUT = float(os.getenv("IMAGE_GEN_TIMEOUT", "120"))

# Gemini inline-data mime types we know how to return, mapped to extensions.
_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}

# Extension back to the canonical mime type.
EXT_TO_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class GeminiError(Exception):
    """Raised when image generation fails or returns no image."""


def ext_for_mime(mime: str) -> str:
    """Return the file extension for a Gemini image mime type (default png)."""
    return _MIME_TO_EXT.get((mime or "").lower(), "png")


def _extract_image(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Pull the first inline image (bytes, mime) out of a generateContent body.

    Raises GeminiError with a safe, caller-facing message when the response
    carries no image (e.g. blocked by safety filters). REST returns camelCase
    (``inlineData``/``mimeType``); snake_case is tolerated for forward-compat.
    """
    feedback = payload.get("promptFeedback") or {}
    block_reason = feedback.get("blockReason")
    if block_reason:
        raise GeminiError(f"Prompt blocked by safety filters ({block_reason}).")

    candidates = payload.get("candidates") or []
    for candidate in candidates:
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                try:
                    raw = base64.b64decode(inline["data"], validate=True)
                except Exception as exc:  # malformed payload from upstream
                    raise GeminiError("Gemini returned malformed image data.") from exc
                return raw, mime

    finish = candidates[0].get("finishReason") if candidates else ""
    suffix = f" (finish_reason={finish})" if finish else ""
    raise GeminiError(f"Gemini returned no image for the prompt{suffix}.")


async def generate_image_bytes(
    prompt: str,
    api_key: str,
    model: str | None = None,
) -> tuple[bytes, str]:
    """Generate one image from a text prompt. Returns (image_bytes, mime_type).

    Raises GeminiError on any upstream failure or empty result.
    """
    model_name = model or DEFAULT_MODEL
    url = f"{GEMINI_API_BASE}/models/{model_name}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as exc:
            _log.warning("gemini_request_failed", model=model_name, error_type=type(exc).__name__)
            raise GeminiError("Failed to reach the image generation service.") from exc

    if resp.status_code != 200:
        # Log status only. The body can echo the prompt; the key is never logged.
        _log.warning("gemini_non_200", model=model_name, status=resp.status_code)
        raise GeminiError(f"Image generation failed (HTTP {resp.status_code}).")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise GeminiError("Gemini returned a response that is not JSON.") from exc

    image_bytes, mime = _extract_image(payload)
    _log.info("gemini_image_generated", model=model_name, mime=mime, size_bytes=len(image_bytes))
    return image_bytes, mime
