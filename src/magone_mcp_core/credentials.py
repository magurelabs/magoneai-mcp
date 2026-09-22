"""Assemble a tool's credentials from the incoming request.

Every connector here takes its credentials from the caller, per request. The
caller sends them as headers; the tool receives them as a `credentials` dict.
That is the shape the whole catalogue is written against.

**This module exists because its absence made every tool uncallable.** The
port carried the handler signature across unchanged - `async def get_meeting(
credentials, meeting_id)` - and nothing supplied the first argument. The
declared schema correctly does not mention `credentials`, because a client
does not send it, so the SDK's argument model and the advertised schema
disagreed and every call failed:

    Error executing tool search_web: 1 validation error for search_webArguments
    credentials  Field required

267 of 267 tools, on every call. No gate saw it: the schemas are correct, the
modules import, conformance exercises the protocol rather than a real vendor
call, and the ported unit tests pass `credentials=` explicitly because that is
what the pre-port tests did.

## Why headers are declared per tool

`credential_headers` is not a convenience list. It is the statement of which
request headers a given tool is allowed to see. A tool that needs a Zoom
secret has no business receiving a Microsoft refresh token that happens to be
on the same request, and forwarding every `X-` header to every handler would
make that the default.

So an undeclared header is not passed. The cost is that a tool with no
declaration receives only the platform context, which is the safe direction to
fail in.
"""

import os
from collections.abc import Mapping
from typing import Any

# The platform's tenancy header. It is NOT injected into every tool: a tool
# that needs the project declares `X-Project-Id` in `credential_headers` like
# any other header, and no connector in this repository does.
# Injected everywhere, an unverified tenancy claim reached every public
# connector, and a public connector should never see one at all.
# Verified tenancy comes from an `AuthContext` once a token verifier is wired.
PROJECT_HEADER = "X-Project-Id"


def header_to_key(header_name: str) -> str:
    """`X-Google-Refresh-Token` -> `google_refresh_token`.

    Only the **leading** `x-` is stripped. A substring replace would also eat
    the `x-` inside a name like `Pptx-Template-Url` and produce
    `ppttemplate_url`, which is a real bug this rule exists to avoid.

    v1's comment on this same transformation reads
    "X-Google-Refresh-Token -> refresh_token", which its code does not do. The
    code is authoritative here: every ported handler reads
    `credentials["google_refresh_token"]`, so matching the comment would break
    all of them.
    """
    key = header_name.lower()
    if key.startswith("x-"):
        key = key[2:]
    return key.replace("-", "_")


def from_file(path: str | None) -> dict:
    """Credentials baked into the image, for a server that runs standalone.

    Headers override these. A missing or unreadable file is not an error: the
    common deployment supplies everything per request.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        import json

        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build(
    headers: Mapping[str, Any] | None,
    credential_headers: tuple | list = (),
    *,
    file_path: str | None = None,
) -> dict:
    """The `credentials` dict a handler receives.

    File first, then the tool's declared headers, then platform context. Later
    wins, so a per-request header overrides a baked-in default.
    """
    credentials = from_file(file_path)
    if not headers:
        return credentials

    lookup = {str(k).lower(): v for k, v in dict(headers).items()}

    for header_name in credential_headers:
        value = lookup.get(str(header_name).lower())
        if value:
            credentials[header_to_key(header_name)] = value

    return credentials
