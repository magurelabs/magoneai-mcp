"""The image-generation connector.

Ported from the v1 image-generation server. Turns a text description into
an image with Google Gemini, using the caller's own Gemini API key, and returns
the image to the caller.

1 tool, 1 of them proposed read-only. The input schema is carried across
verbatim from v1 via `declare_schema`, so an agent configured against v1 sends
the same arguments here.

## What comes back

v1 stored the image in the platform's object storage and returned only a
storage key. A connector has no storage of its own to put it in, so this one
returns the image itself, the standard MCP way:

- an MCP `image` content block (`data` is base64, `mimeType` is the image type),
  which any MCP client can render or save; and
- a JSON text block and matching `structuredContent` describing it:
  `filename`, `content_type`, `size_bytes`, `prompt`.

The bytes travel only in the image block, never in the JSON, so the outbound
text budget is not spent on base64 and a client reading `structuredContent`
gets metadata, not megabytes.

## Credentials

One header, per call: `X-Gemini-Api-Key`, read as `credentials["gemini_api_key"]`.
For a standalone run, `GEMINI_API_KEY` in the environment is the fallback; a
per-call header always wins.
"""

import base64
import os
import re
from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

from .gemini import EXT_TO_MIME, GeminiError, ext_for_mime, generate_image_bytes

SERVER_NAME = "image-generation"
SERVER_VERSION = "2.0.0"

logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="Generate images from text descriptions using Google Gemini.",
)

MAX_PROMPT_CHARS = 4000

# Generous ceiling for a single generated image; a Gemini PNG is a few MB.
MAX_IMAGE_BYTES = 30 * 1024 * 1024  # 30 MB


class ImageRequestError(ValueError):
    """The request cannot be sent: the message is written for the caller."""


def slugify(text: str, fallback: str = "image") -> str:
    """A name safe to use as a file name, from free text, at most 60 characters (v1's rule)."""
    slug = re.sub(r"[^\w\s-]", "", (text or fallback).lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:60] or fallback


def clean_prompt(prompt: str | None) -> str:
    """The prompt, stripped, or ImageRequestError with v1's message."""
    cleaned = (prompt or "").strip()
    if not cleaned:
        raise ImageRequestError("prompt is required.")
    if len(cleaned) > MAX_PROMPT_CHARS:
        raise ImageRequestError(f"prompt exceeds the {MAX_PROMPT_CHARS} character limit.")
    return cleaned


def resolve_api_key(credentials: dict | None) -> str:
    """The caller's key from the per-call header, else the standalone env fallback."""
    key = ((credentials or {}).get("gemini_api_key") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise ImageRequestError(
            "Missing Gemini API key. Provide it via the connection "
            "credentials or the GEMINI_API_KEY environment variable."
        )
    return key


async def render(
    credentials: dict | None, prompt: str | None, filename: str | None
) -> tuple[bytes, str, str, str]:
    """Generate the image. Returns (bytes, mime, extension, filename with extension).

    Shared with any composition that stores the image somewhere instead of
    returning it, so validation, key resolution, naming and the size ceiling
    stay one implementation. Raises ImageRequestError or GeminiError, both
    carrying a caller-facing message.
    """
    cleaned = clean_prompt(prompt)
    api_key = resolve_api_key(credentials)
    image_bytes, mime = await generate_image_bytes(cleaned, api_key)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        mb = MAX_IMAGE_BYTES // (1024 * 1024)
        raise ImageRequestError(
            f"Generated image ({len(image_bytes):,} bytes) exceeds the {mb} MB limit."
        )
    extension = ext_for_mime(mime)
    full_name = f"{slugify(filename or cleaned)}.{extension}"
    return image_bytes, mime, extension, full_name


@server.tool(
    name="generate_image",
    description=(
        "Generate an image from a text description using Google Gemini. "
        "Returns the image as an MCP image content block (base64 data plus its "
        "MIME type), and its metadata (filename, content_type, size_bytes, "
        "prompt) as structured output."
    ),
    # Reviewed: it creates nothing and changes nothing on any account. The
    # vendor call spends the caller's quota, which is a cost, not a write.
    # Open world because the result depends on an external model.
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    # The one credential header this connector reads. v1 registered the same
    # name in its auth_config (`gemini_api_key` -> `X-Gemini-Api-Key`), so a
    # caller that already sends it needs no change.
    credential_headers=["X-Gemini-Api-Key"],
    permission_group="default",
)
@declare_schema(
    {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Text description of the image to generate, e.g. "
                    "'a watercolor painting of a lighthouse at sunset'."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional output filename without extension. Defaults to "
                    "a slug derived from the prompt."
                ),
            },
        },
    }
)
@declare_output_schema(
    {
        "type": "object",
        "required": ["filename", "content_type", "size_bytes"],
        "properties": {
            "filename": {
                "type": "string",
                "description": "Filename with extension for the generated image.",
            },
            "content_type": {
                "type": "string",
                "description": "MIME type of the image (e.g. 'image/png').",
            },
            "size_bytes": {
                "type": "integer",
                "description": "Image size in bytes.",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt used to generate the image.",
            },
        },
    }
)
async def generate_image(
    credentials: dict,
    prompt: str,
    filename: str | None = None,
) -> dict[str, Any]:
    try:
        image_bytes, mime, extension, full_name = await render(credentials, prompt, filename)
    except (ImageRequestError, GeminiError) as exc:
        return {"error": str(exc)}

    content_type = EXT_TO_MIME.get(extension, mime)
    return {
        "filename": full_name,
        "content_type": content_type,
        "size_bytes": len(image_bytes),
        "prompt": clean_prompt(prompt),
        "_mcp_content": {
            "type": "image",
            "data": base64.b64encode(image_bytes).decode("ascii"),
            "mimeType": content_type,
        },
    }
