"""SSRF address blocking.

The corpus is the same one that found 12 live bypasses in the v1 connector. It
lives here so the framework inherits the fix permanently, which is the point of
having one implementation rather than three.
"""

import ipaddress

import pytest

from magone_mcp_core import SSRFBlocked, is_blocked_address, validate_url
from magone_mcp_core.net import InvalidURL, encoded_addresses

BLOCKED = [
    ("169.254.169.254", "cloud instance metadata"),
    ("::ffff:169.254.169.254", "metadata via IPv4-mapped IPv6"),
    ("0:0:0:0:0:ffff:a9fe:a9fe", "the same address, fully expanded"),
    ("2002:a9fe:a9fe::1", "metadata via 6to4, which routes to the embedded IPv4"),
    ("64:ff9b::a9fe:a9fe", "metadata via NAT64"),
    ("::ffff:127.0.0.1", "loopback via IPv4-mapped IPv6"),
    ("::ffff:10.0.0.1", "RFC 1918 via IPv4-mapped IPv6"),
    ("::ffff:192.168.1.1", "RFC 1918 via IPv4-mapped IPv6"),
    ("::ffff:172.16.0.1", "RFC 1918 via IPv4-mapped IPv6"),
    ("100.64.0.1", "carrier-grade NAT, not reported by is_private"),
    ("::ffff:100.64.0.1", "carrier-grade NAT via IPv4-mapped IPv6"),
    ("fec0::1", "deprecated IPv6 site-local, not reported by is_private"),
    ("::", "unspecified IPv6"),
    ("0.0.0.0", "unspecified IPv4"),  # noqa: S104 - corpus data, not a bind
    ("127.0.0.1", "loopback"),
    ("::1", "IPv6 loopback"),
    ("10.0.0.1", "RFC 1918"),
    ("172.16.0.1", "RFC 1918"),
    ("192.168.1.1", "RFC 1918"),
    ("fe80::1", "IPv6 link-local"),
    ("fc00::1", "IPv6 unique-local"),
    ("224.0.0.1", "multicast"),
    ("198.18.0.1", "benchmarking range"),
]

ALLOWED = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "140.82.121.4",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    # NAT64 wrapping a public address: the prefix is legitimate, the embedded
    # address decides. 64:ff9b::8efa:476e is 142.250.71.110.
    "64:ff9b::8efa:476e",
]


@pytest.mark.parametrize("address,reason", BLOCKED, ids=[a for a, _ in BLOCKED])
def test_internal_addresses_blocked(address: str, reason: str) -> None:
    assert is_blocked_address(ipaddress.ip_address(address)) is True, reason


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_allowed(address: str) -> None:
    assert is_blocked_address(ipaddress.ip_address(address)) is False


def test_encodings_are_unwrapped_rather_than_enumerated() -> None:
    """The fix must decode, not list more networks.

    Taking a sample from every IPv4 range in the blocklist and checking its
    mapped form covers ranges added later, which an enumerated list would not.
    """
    from magone_mcp_core.net import BLOCKED_NETWORKS

    ipv4 = [n for n in BLOCKED_NETWORKS if n.version == 4]
    assert ipv4
    for network in ipv4:
        sample = next(network.hosts(), network.network_address)
        assert is_blocked_address(ipaddress.ip_address(f"::ffff:{sample}")) is True


def test_encoded_addresses_reports_what_it_unwrapped() -> None:
    got = encoded_addresses(ipaddress.ip_address("::ffff:169.254.169.254"))
    assert ipaddress.ip_address("169.254.169.254") in got


def test_carrier_grade_nat_boundaries_are_exact() -> None:
    assert is_blocked_address(ipaddress.ip_address("100.63.255.255")) is False
    assert is_blocked_address(ipaddress.ip_address("100.64.0.0")) is True
    assert is_blocked_address(ipaddress.ip_address("100.127.255.255")) is True
    assert is_blocked_address(ipaddress.ip_address("100.128.0.0")) is False


def test_literal_url_is_not_resolved() -> None:
    """A literal address is checked as written.

    Resolving it would be a way to get a different answer from the one that
    will actually be connected to.
    """
    with pytest.raises(SSRFBlocked):
        validate_url("https://[::ffff:169.254.169.254]/latest/meta-data/")


def test_scheme_is_enforced() -> None:
    for url in ("file:///etc/passwd", "gopher://example.com", "ftp://example.com"):
        with pytest.raises(InvalidURL):
            validate_url(url)


def test_missing_hostname_is_rejected() -> None:
    with pytest.raises(InvalidURL):
        validate_url("https://")


# Each of these reached an internal address through the shared check, measured
# against the previous implementation.
@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b:1::a9fe:a9fe",  # RFC 8215 local-use NAT64 of 169.254.169.254
        "::127.0.0.1",  # IPv4-compatible loopback
        "::a9fe:a9fe",  # IPv4-compatible metadata
        "::ffff:0:a9fe:a9fe",  # IPv4-translated metadata
        "100::1",  # discard-only
        "192.88.99.1",  # 6to4 relay anycast
        "198.51.100.7",  # documentation range
    ],
)
def test_newly_blocked_encodings_and_ranges(address):
    assert is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "64:ff9b::808:808", "2606:4700::1111"])
def test_public_addresses_stay_allowed(address):
    assert is_blocked_address(ipaddress.ip_address(address)) is False


def test_an_out_of_range_port_is_an_invalid_url_not_a_bare_value_error():
    with pytest.raises(InvalidURL):
        validate_url("http://example.com:99999/")
