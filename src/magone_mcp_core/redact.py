"""Logging that cannot leak a credential by accident.

Twenty connectors hold a credential for somebody's third-party account,
and the code is about to be readable by anyone. The v1 framework already did
the right things in two places: it redacted `last_error` on the health endpoint
and logged tool arguments by key rather than by value. Those were conventions,
discoverable only by reading the code that happened to follow them, and nothing
enforced either.

This makes them properties of the framework, so a connector author leaks a
credential only by going out of their way.

The case most often missed is the third one: a vendor's 401 body frequently
echoes the token that failed. Logging an upstream error verbatim is the most
natural thing an author does when debugging, and it is how a credential ends up
in a log aggregator.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[redacted]"

# Markers that make a field secret wherever they appear in its name, matched
# case-insensitively after `-` is folded to `_` (so `X-Api-Key` is `x_api_key`).
SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "private_key",
    "cookie",
    "signature",
)

# Markers that make a field secret only as a whole word of its name. As
# substrings they caught `author` and `pwdless_login`. (`session_count` is
# still redacted: "session" is a word of it, and over-redacting a count is
# the cheap direction to be wrong in.)
SECRET_KEY_WORDS = ("auth", "session", "pwd")

_KEY_PATTERN = "|".join(
    [m.replace("_", "[-_]?") for m in SECRET_KEY_MARKERS]
    + [rf"(?<![a-z]){w}(?![a-z])" for w in SECRET_KEY_WORDS]
)

# Shapes that are a credential wherever they appear, for scrubbing free text
# where there is no key to inspect. Order matters: a quoted value is taken
# whole before the unquoted form can stop at its first space.
_VALUE_PATTERNS = (
    # "password": "correct horse battery" - the whole quoted value.
    re.compile(rf"(?i)([\"']?[\w-]*(?:{_KEY_PATTERN})[\w-]*[\"']?\s*[:=]\s*)([\"'])(.*?)\2"),
    # Authorization header values.
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # JWTs.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    # Well-known key shapes: OpenAI/Stripe-style, AWS access key ids, GitHub,
    # Slack. Recognisable by prefix, so found even with no key name nearby.
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # Credentials in URL userinfo: scheme://user:PASSWORD@host
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)"),
    # key=value, key: value and header lines naming a secret.
    re.compile(
        rf"(?i)([\"']?[\w-]*(?:{_KEY_PATTERN})[\w-]*[\"']?\s*[:=]\s*[\"']?)"
        rf"([^\s,;&\"'}}]{{4,}})"
    ),
)

# Upstream error bodies are truncated before they reach a log line. Long enough
# to identify the failure, short enough not to paste a page of HTML.
MAX_UPSTREAM_ERROR_CHARS = 500


def is_secret_key(key: str) -> bool:
    normalised = str(key).lower().replace("-", "_")
    if any(marker in normalised for marker in SECRET_KEY_MARKERS):
        return True
    words = set(re.split(r"[^a-z0-9]+", normalised))
    return any(word in words for word in SECRET_KEY_WORDS)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a mapping with every secret-looking value replaced.

    Recurses, because a credential one level down is still a credential.

    String values are also scrubbed, not just returned. A secret-looking KEY is
    only one of the two ways a credential appears in structured data; the other
    is a credential-shaped VALUE under an innocent key, and those were passing
    through untouched:

        {"headers": ["Authorization: Bearer sk-live-..."]}   -> unchanged
        {"note": "the token is eyJhbGciOi..."}               -> unchanged

    `scrub_text` already recognised both shapes. It simply was not reached from
    here, so the two halves of this module protected different things and the
    gap between them was the interesting case.

    Scrubbing benign strings costs nothing: the patterns match credential
    shapes, so ordinary text comes back unchanged.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if is_secret_key(key):
            out[key] = REDACTED
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [_redact_item(v) for v in value]
        elif isinstance(value, str):
            out[key] = scrub_text(value)
        else:
            out[key] = value
    return out


def _redact_item(value: Any) -> Any:
    """One element of a sequence, at the same standard as a mapping value."""
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_redact_item(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_text(text: str) -> str:
    """Remove credential-shaped substrings from free text."""
    if not text:
        return text
    out = text
    for pattern in _VALUE_PATTERNS:
        if pattern.groups == 3 and pattern.pattern.endswith(r"\2"):
            # Quoted value: keep the quotes, replace what is between them.
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", out)
        elif pattern.groups == 3:
            # URL userinfo: keep scheme, user and the `@`.
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", out)
        elif pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def argument_keys(arguments: Mapping[str, Any]) -> Iterable[str]:
    """Tool arguments are logged by key only, never by value.

    An argument value is user content: a query, a document body, an email
    address. None of it belongs in a log line, and some of it is a credential
    the caller passed as an ordinary parameter.
    """
    return sorted(str(k) for k in arguments)


def upstream_error(body: Any, *, limit: int = MAX_UPSTREAM_ERROR_CHARS) -> str:
    """Make a vendor's error body safe to log.

    Scrubbed and truncated, in that order. Truncating first would leave a
    partial token that is still a usable prefix for a guessing attack, and
    would leave the scrubber nothing to match.

    An operator who needs the unscrubbed body has `MCP_LOG_RAW_UPSTREAM_ERRORS`
    for a local debugging session. It is deliberately not something a
    deployment turns on and forgets: see `raw_upstream_errors_enabled`.
    """
    text = body if isinstance(body, str) else str(body)
    scrubbed = scrub_text(text)
    if len(scrubbed) <= limit:
        return scrubbed
    return f"{scrubbed[:limit]}... [{len(scrubbed) - limit} more characters]"


def raw_upstream_errors_enabled(env: Mapping[str, str]) -> bool:
    """Whether unscrubbed upstream bodies may be logged.

    Gated on the local mode as well as the flag. Scrubbing loses detail exactly
    when an operator most wants it, so there has to be a way to get it back;
    allowing it in a deployment that serves other people would undo the point.
    """
    flag = str(env.get("MCP_LOG_RAW_UPSTREAM_ERRORS", "")).strip().lower() in ("1", "true", "yes")
    return flag and str(env.get("MCP_MODE", "")).strip().lower() == "local"
