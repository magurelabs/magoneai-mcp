"""One SSRF check, because three was the actual defect.

The v1 repository had three independent implementations of "is this address
internal", and they disagreed:

- the connector's matched addresses per family, so an IPv4-mapped IPv6 literal
  matched none of its IPv4 entries and none of its IPv6 ones, and
  ``::ffff:169.254.169.254`` reached cloud instance metadata
- the two ``url_fetch`` validators handled that correctly, and both missed
  carrier-grade NAT and IPv6 site-local, neither of which
  ``ipaddress.is_private`` reports

Nobody wrote a careless check. The defect was that there were three checks, so
a fix to one never reached the others. This is the single implementation.

The technique that matters: an IPv6 literal is **unwrapped** to whatever IPv4
address it encodes before the membership test, rather than enumerating more
networks. Enumerating would pass today's tests and miss the next encoding, and
it would have to be repeated for every range added later.
"""

import ipaddress
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

_BaseAddress = ipaddress._BaseAddress  # noqa: SLF001 - the public name is private


class SSRFBlocked(Exception):
    """A URL resolves to an address inside the trust boundary."""


class InvalidURL(ValueError):
    """A URL is malformed or uses a scheme that is not allowed."""


ALLOWED_SCHEMES = frozenset({"http", "https"})

BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # unspecified / this network
        "10.0.0.0/8",  # RFC 1918
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, and cloud instance metadata
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # benchmarking
        "100.64.0.0/10",  # carrier-grade NAT; NOT reported by is_private
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "::/128",  # unspecified
        "::1/128",  # loopback
        "fc00::/7",  # unique-local
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local; NOT reported by is_private
        "ff00::/8",  # multicast
        "100::/64",  # discard-only (RFC 6666)
        "192.88.99.0/24",  # 6to4 relay anycast, deprecated (RFC 7526)
        "192.0.2.0/24",  # documentation (TEST-NET-1)
        "198.51.100.0/24",  # documentation (TEST-NET-2)
        "203.0.113.0/24",  # documentation (TEST-NET-3)
        "2001:db8::/32",  # documentation
    )
)

# NAT64 (RFC 6052) carries an IPv4 address in the low 32 bits. The prefix
# itself is legitimate - dual-stack hosts resolve into it - so it is the
# embedded address that decides, not the prefix.
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# RFC 8215 local-use NAT64. Listed so the embedded IPv4 is still decoded and
# judged, but deliberately NOT exempt from the check on the address itself:
# "local-use" means a translator inside somebody's network, so the address is
# internal whatever payload it carries. See `is_blocked_address`.
LOCAL_NAT64_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")
# Two older embeddings whose low 32 bits are an IPv4 destination: the
# deprecated IPv4-compatible form (`::a.b.c.d`) and IPv4-translated
# (`::ffff:0:a.b.c.d`, RFC 2765). `::` and `::1` are handled on their own.
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")


def encoded_addresses(ip: _BaseAddress) -> list[_BaseAddress]:
    """Every address a literal can actually reach.

    ``ip in network`` is False whenever the versions differ, so an IPv6 literal
    that encodes an IPv4 destination has to be unwrapped before any membership
    test means anything.

    Four encodings do that: IPv4-mapped (``::ffff:a.b.c.d``, the same host by
    definition), 6to4 (``2002::/16``, routes to the embedded address), Teredo
    (``2001::/32``, carries a relay and a client address) and NAT64.
    """
    out: list[_BaseAddress] = [ip]
    if ip.version != 6:
        return out
    mapped = ip.ipv4_mapped
    if mapped is not None:
        out.append(mapped)
    sixtofour = ip.sixtofour
    if sixtofour is not None:
        out.append(sixtofour)
    teredo = ip.teredo
    if teredo is not None:
        out.extend(teredo)
    low32 = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in NAT64_PREFIX or ip in LOCAL_NAT64_PREFIX or ip in IPV4_TRANSLATED:
        out.append(low32)
    elif ip in IPV4_COMPATIBLE and int(ip) > 1:
        out.append(low32)
    return out


def is_blocked_address(ip: _BaseAddress) -> bool:
    """Whether an address, or anything it encodes, is inside the boundary.

    The explicit list, and the standard library's own classification on top
    of it: a range the library knows is private or reserved and the list
    forgot is still refused. The list exists for the ranges the library does
    NOT report (carrier-grade NAT, site-local, discard-only).
    """
    # Only the WELL-KNOWN prefix is exempt from the check on the address itself.
    # `64:ff9b::/96` is globally assigned and the library classes it non-global,
    # so judging the address would refuse every public host a dual-stack resolver
    # returns over NAT64. None of that is true of RFC 8215's `64:ff9b:1::/48`:
    # local-use names a translator inside somebody's network, so the address is
    # internal whatever it carries. Exempting both let `64:ff9b:1::5db8:d822`
    # through. v1 had already narrowed this, and this is the same narrowing.
    translated = ip.version == 6 and ip in NAT64_PREFIX
    for candidate in encoded_addresses(ip):
        if translated and candidate is ip:
            # A NAT64 address is only as internal as the IPv4 it carries; the
            # library classes the prefix itself as non-global, which would
            # refuse every public host reached over NAT64.
            continue
        if (
            candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or candidate.is_reserved
        ):
            return True
        if any(
            candidate in network
            for network in BLOCKED_NETWORKS
            if candidate.version == network.version
        ):
            return True
    return False


def resolve_addresses(hostname: str, port: int | None) -> Iterable[_BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidURL(f"cannot resolve hostname: {hostname}") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def validate_url(url: str, *, allowed_schemes: Iterable[str] = ALLOWED_SCHEMES) -> str:
    """Check a URL is safe to fetch, and return it unchanged.

    **Every** resolved address is checked, not just the first. A hostname with
    one public and one private A record is a documented SSRF technique, and
    checking only the first record would let it through half the time.

    This does not close the DNS-rebinding window between the check and the
    connection. A caller that needs that guarantee must also verify the peer
    address after connecting; the framework exposes `is_blocked_address` for
    exactly that, and saying so here is better than implying a guarantee this
    function cannot give.
    """
    if not url or not isinstance(url, str):
        raise InvalidURL("a URL is required")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {s.lower() for s in allowed_schemes}:
        raise InvalidURL(f"scheme {parsed.scheme!r} is not allowed")
    hostname = parsed.hostname
    if not hostname:
        raise InvalidURL("URL has no hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        # `http://example.com:99999/` - a bare ValueError here escaped every
        # caller that catches InvalidURL.
        raise InvalidURL(f"invalid port in URL: {exc}") from exc

    # A literal address needs no resolution, and resolving it would be a way to
    # get a different answer than the one that will be connected to.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if is_blocked_address(literal):
            raise SSRFBlocked(f"address is internal: {hostname}")
        return url.strip()

    for address in resolve_addresses(hostname, port):
        if is_blocked_address(address):
            raise SSRFBlocked(f"{hostname} resolves to an internal address ({address})")
    return url.strip()


def reject_blocked_peer(response: Any) -> None:
    """Refuse a response whose connection went to an internal address.

    `validate_url` resolves the name once and the HTTP client resolves it again
    when it connects, so a hostname that answers public to the first lookup and
    internal to the second passes the first and is connected to anyway. This
    reads the address the socket actually reached (the ``network_stream``
    extension httpx exposes) and judges that one. Call it as soon as the
    response headers arrive, before reading the body or following a redirect.

    A transport with no real socket (a test double) exposes no peer, and there
    is nothing to judge then.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return
    server_addr = stream.get_extra_info("server_addr")
    if not server_addr:
        return
    try:
        ip = ipaddress.ip_address(server_addr[0])
    except (ValueError, IndexError):
        return
    if is_blocked_address(ip):
        raise SSRFBlocked(f"connected to an internal address ({ip})")
