"""cloudflare_r2 decides addresses with the shared check, not a private copy.

Its own list missed IPv4-translated and IPv4-compatible encodings of the
metadata address, among others: one of three SSRF implementations in the v1
catalogue with three different gap sets.
"""

import importlib
import ipaddress

import pytest

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.cloudflare_r2.server")


@pytest.mark.parametrize("address", ["::ffff:0:a9fe:a9fe", "::a9fe:a9fe", "64:ff9b:1::a9fe:a9fe"])
def test_encodings_the_private_copy_missed_are_blocked(address):
    assert module._is_blocked_ip(ipaddress.ip_address(address)) is True


def test_the_connector_uses_the_framework_check():
    from magone_mcp_core.net import is_blocked_address

    assert module.is_blocked_address is is_blocked_address
