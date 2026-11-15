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


# Credential keys that say WHERE a secret is sent or WHAT a connection may
# reach, as opposed to the secret itself: a host, a port, an endpoint or base
# URL, a DSN or connection string, a scheme or TLS setting, and an allowlist.
# Matched on the key a header maps to (`header_to_key`), by suffix or prefix so a
# connector added later is covered without anyone remembering to list it:
# `db_host`, `trino_port`, `r2_endpoint_url`, `llm_base_url`,
# `db_connection_string`, `trino_http_scheme`, `allowed_mailboxes`,
# `trino_allowed_catalogs`.
_TARGET_SUFFIXES = (
    "_host",
    "_hostname",
    "_port",
    "_url",
    "_uri",
    "_endpoint",
    "_dsn",
    "_connection_string",
    "_scheme",
    "_trust_server_certificate",
)


def is_target_key(key: str) -> bool:
    """Whether a credential key chooses a destination or widens a scope."""
    key = key.lower()
    return key.endswith(_TARGET_SUFFIXES) or key.startswith("allowed_") or "_allowed_" in key


def _supplies_secret(file_credentials: Mapping[str, Any]) -> bool:
    """Whether the file holds anything that must not be sent to a caller's choice.

    A connection string embeds its password even though its key does not look
    secret, so it counts.
    """
    from .redact import is_secret_key

    return any(
        value and (is_secret_key(key) or key.lower().endswith(("_connection_string", "_dsn")))
        for key, value in file_credentials.items()
    )


def from_file(path: str | None) -> dict:
    """Credentials baked into the image, for a server that runs standalone.

    A request header can fill a key this file leaves unset but never replaces one
    it sets; see `build`. A missing or unreadable file is not an error: the
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

    **Header-only (no credentials file, or one that yields nothing):** each of
    the tool's declared headers present on the request becomes a key, then
    platform context. Unchanged.

    **With a credentials file that yields values, the file wins.** Precedence is
    the reverse of what it once was, because letting a request header override a
    file key one at a time meant a caller who sent only `X-Db-Host` made the
    server send the file's database password to that host:

    - a key the file sets cannot be overridden by a header;
    - a header may fill a key the file does not set;
    - except a target key (host, port, URL, endpoint, DSN or connection string,
      scheme or TLS setting, allowlist such as `X-Allowed-Mailboxes`; see
      `is_target_key`), which never comes from a header once the file supplies
      any secret. Otherwise the caller could point the file's secret at a server
      of their choosing, or widen what the connection is allowed to reach.

    Headers that lose are dropped silently, not reported: the caller of a
    shared server does not learn which keys the operator baked in.
    """
    credentials = from_file(file_path)
    if not headers:
        return credentials

    lookup = {str(k).lower(): v for k, v in dict(headers).items()}
    from_headers: dict[str, Any] = {}
    for header_name in credential_headers:
        value = lookup.get(str(header_name).lower())
        if value:
            from_headers[header_to_key(header_name)] = value

    if not any(credentials.values()):
        # Nothing baked in, so there is nothing for a header to override or
        # redirect: the file is empty or holds only blank values.
        return {**credentials, **from_headers}

    file_keys = {key.lower() for key, value in credentials.items() if value}
    guard_targets = _supplies_secret(credentials)
    for key, value in from_headers.items():
        if key in file_keys or (guard_targets and is_target_key(key)):
            continue
        credentials[key] = value

    return credentials
