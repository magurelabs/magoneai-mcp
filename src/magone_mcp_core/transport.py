"""Which Host and Origin headers a served connector accepts.

The SDK protects against DNS rebinding by checking `Host` and `Origin`, but
only when it is told what to allow. Left to itself it does one of two things,
and both are wrong for a connector:

- with no settings and its default `host="127.0.0.1"`, it allows loopback only,
  so a container reached by its service name (`Host: mcp-jira:8000`) is
  answered `421 Misdirected Request` on every call;
- with `host="0.0.0.0"`, the obvious fix, it turns the protection **off**, so
  any Host and any Origin are accepted (python-sdk issue #3562).

So the allowlist is always explicit. Loopback is always allowed, because a
developer's `docker run -p 8000:8000` arrives as `localhost:8000`. Everything
else is named by the deployment:

    MCP_ALLOWED_HOSTS=mcp-jira,mcp-jira.tools.svc.cluster.local
    MCP_ALLOWED_ORIGINS=https://studio.example.com

A bare name allows it with and without a port. `MCP_ALLOWED_HOSTS=*` turns the
check off, and says so in the log, for the deployment that genuinely cannot
enumerate its hostnames. That is an explicit decision rather than a default.
"""

from collections.abc import Mapping

from mcp.server.transport_security import TransportSecuritySettings

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

DISABLED = "*"


def _entries(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _with_any_port(host: str) -> list[str]:
    """`name` and `name:*`, because the SDK matches Host exactly.

    A Host header carries a port whenever the client used a non-default one,
    and not otherwise, so allowing only one form refuses half the callers.
    An entry that already names a port, or a port wildcard, is taken as given.
    """
    if host.endswith("]") or ":" not in host:
        return [host, f"{host}:*"]
    return [host]


def _origin_with_any_port(origin: str) -> list[str]:
    _, _, rest = origin.partition("://")
    if not rest or ":" in rest.rsplit("]", 1)[-1]:
        return [origin]
    return [origin, f"{origin}:*"]


def host_check_disabled(env: Mapping[str, str]) -> bool:
    return str(env.get("MCP_ALLOWED_HOSTS", "")).strip() == DISABLED


def configured_hosts(env: Mapping[str, str]) -> list[str]:
    """The non-loopback hosts the deployment named, before expansion."""
    if host_check_disabled(env):
        return []
    return _entries(env.get("MCP_ALLOWED_HOSTS"))


def transport_security(env: Mapping[str, str]) -> TransportSecuritySettings:
    """Settings for the SDK's Host and Origin check, always explicit."""
    if host_check_disabled(env):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts: list[str] = []
    for host in (*LOOPBACK_HOSTS, *configured_hosts(env)):
        hosts.extend(_with_any_port(host))

    origins: list[str] = []
    for host in LOOPBACK_HOSTS:
        origins.extend(_origin_with_any_port(f"http://{host}"))
    for origin in _entries(env.get("MCP_ALLOWED_ORIGINS")):
        origins.extend(_origin_with_any_port(origin.rstrip("/")))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )
