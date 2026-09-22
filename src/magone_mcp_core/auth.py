"""Verified identity for a tool call, in three deployment modes.

The rule this module exists to enforce: **no tool ever reads a raw header.**
A tool receives an ``AuthContext`` whose tenancy has been verified, or the call
does not reach the tool.

That is a change of kind rather than degree. Tenancy used to be an unverified
``X-Project-Id`` header copied into a credentials dict and, in at least one
connector, straight into a SQL predicate. Anything holding the shared service
token could name any tenant.

Two normative rules from the specification are implemented here rather than
left to each connector, because a ``TokenVerifier`` without them is the shape
of this design with its point removed:

1. **Audience validation.** A token that does not name this resource in its
   audience is rejected (RFC 8707, RFC 9068). A token valid for some other
   resource is not valid here. This has been required since revision
   ``2025-06-18``, so it was always a gap rather than a new obligation.
2. **No token passthrough.** The token a server receives from its caller is
   never forwarded upstream. The credential presented to a vendor API is a
   separate credential, obtained per subject.

The three modes are deliberately the same seam, so a connector cannot behave
differently depending on who deployed it:

- ``platform``  - the platform vouches for the caller. See PlatformTokenVerifier.
- ``hosted``    - the connector is an OAuth 2.1 resource server in its own right.
- ``local``     - a developer running it against their own account.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier


class AuthError(Exception):
    """A caller could not be authenticated, or is not allowed here."""


@dataclass(frozen=True)
class AuthContext:
    """Who is calling, established rather than asserted.

    ``subject`` and ``tenant`` come from a verified token. A connector that
    scopes data by tenant reads ``tenant`` here and nowhere else; there is no
    header for a caller to set.
    """

    subject: str
    tenant: str | None = None
    scopes: frozenset = field(default_factory=frozenset)
    mode: str = "local"
    # Credentials for the upstream vendor API, resolved per subject. Never the
    # inbound token: see the no-passthrough rule above.
    credentials: Mapping[str, str] = field(default_factory=dict)

    def require_scopes(self, *required: str) -> None:
        """Raise unless every named scope is present."""
        missing = [s for s in required if s not in self.scopes]
        if missing:
            raise AuthError(f"missing required scope(s): {', '.join(sorted(missing))}")

    def has_scopes(self, *required: str) -> bool:
        return all(s in self.scopes for s in required)


def validate_audience(claims: Mapping[str, Any], resource: str) -> None:
    """Reject a token that does not name this resource in its audience.

    ``aud`` may be a string or a list of strings (RFC 7519). A token with no
    audience at all is rejected: an unscoped bearer is exactly the ambient
    authority this design removes.
    """
    aud = claims.get("aud")
    if aud is None:
        raise AuthError("token has no audience claim, so it is not scoped to this resource")
    audiences = [aud] if isinstance(aud, str) else list(aud)
    if resource not in audiences:
        raise AuthError(
            "token audience does not name this resource; a token valid elsewhere is not valid here"
        )


def _scopes_of(claims: Mapping[str, Any]) -> frozenset:
    """Scopes from a token, accepting both the space-delimited and list forms."""
    raw = claims.get("scope") or claims.get("scopes") or ""
    if isinstance(raw, str):
        return frozenset(s for s in raw.split() if s)
    return frozenset(str(s) for s in raw)


class PlatformTokenVerifier(TokenVerifier):
    """The platform vouches for the caller.

    A platform's own authorization server is **one implementation** of this
    seam, not an alternative to it. The seam stays because a published
    connector must also run for someone who has never heard of that platform;
    the platform mode is where such a server plugs in.

    The shared fleet-wide service token is not accepted here. It proves only
    that the caller holds a string, never which tenant they are, which is the
    defect this replaces.
    """

    def __init__(self, resource: str, introspect: Any) -> None:
        self._resource = resource
        self._introspect = introspect

    async def verify_token(self, token: str) -> AccessToken | None:
        """The SDK's contract: an AccessToken, or None for "not valid here".

        Returning None is what makes the SDK answer 401 with the
        resource-metadata challenge. Raising instead surfaced as a 500, so a
        wrong-audience token looked like a server fault rather than a
        rejected credential.
        """
        claims = await self._introspect(token)
        if not claims or not claims.get("active", True):
            return None
        try:
            validate_audience(claims, self._resource)
        except AuthError:
            return None
        subject = claims.get("sub")
        if not subject:
            return None
        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or subject),
            scopes=sorted(_scopes_of(claims)),
            # Both are checked by the SDK itself once set: `resource` against
            # the server's own URL, `expires_at` against the clock.
            resource=self._resource,
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
        )

    async def context(self, token: str, credentials: Mapping[str, str]) -> AuthContext:
        claims = await self._introspect(token)
        if not claims or not claims.get("active", True):
            raise AuthError("token is not active")
        validate_audience(claims, self._resource)
        return AuthContext(
            subject=str(claims["sub"]),
            # Tenancy comes from the token, never from a header.
            tenant=claims.get("tenant") or claims.get("org"),
            scopes=_scopes_of(claims),
            mode="platform",
            credentials=dict(credentials),
        )


class LocalAuthContextFactory:
    """A developer running a connector against their own account.

    There is no token and no tenant: the only caller is the person who started
    the process. Credentials come from the environment they configured.

    This mode is why the framework refuses to bind a non-loopback interface
    without an explicit override. An unauthenticated server is fine on
    loopback and is an open tool endpoint anywhere else.
    """

    def __init__(self, credentials: Mapping[str, str] | None = None) -> None:
        self._credentials = dict(credentials or {})

    def context(self, scopes: Sequence[str] = ()) -> AuthContext:
        return AuthContext(
            subject="local",
            tenant=None,
            scopes=frozenset(scopes),
            mode="local",
            credentials=dict(self._credentials),
        )


def assert_no_passthrough(inbound_token: str | None, outbound: dict[str, str]) -> None:
    """Fail loudly if the caller's own token is about to be sent upstream.

    Cheap to call, and it turns a specification MUST into something a test can
    assert. Token passthrough is the failure that turns one compromised
    connector into access to every upstream the caller could reach.
    """
    if not inbound_token:
        return
    for name, value in outbound.items():
        if inbound_token in value:
            raise AuthError(
                f"refusing to forward the inbound access token upstream in {name!r}; "
                "the upstream credential must be obtained separately"
            )


SERVICE_TOKEN_HEADER = "x-service-token"  # noqa: S105 - a header name, not a secret


def require_service_token(app: Any, expected: str, protected_path: str = "/mcp") -> Any:
    """Refuse `protected_path` unless the caller presents the shared service token.

    The check v1's platform mode made on every call, carried across so a v2
    connector is not more open than the server it replaces. It matters most for
    a connector deployed with platform credentials: without it any workload
    that could reach one could call its tools.

    This is an interim control, not the end state:
    a shared secret proves the caller holds a string, never which tenant it
    is. A `TokenVerifier` with an audience-bound token replaces it. Compared
    in constant time. Paths other than `protected_path`, such as `/healthz`
    for a probe, are not affected.
    """
    import hmac

    from starlette.responses import JSONResponse

    expected_bytes = expected.encode()

    async def guarded(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith(protected_path):
            provided = b""
            for name, value in scope.get("headers") or []:
                if name.decode("latin-1").lower() == SERVICE_TOKEN_HEADER:
                    provided = value
                    break
            if not provided or not hmac.compare_digest(provided, expected_bytes):
                response = JSONResponse({"detail": "Invalid service token"}, status_code=403)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded
