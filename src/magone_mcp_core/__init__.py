"""magone-mcp-core: a thin layer over the official MCP Python SDK.

The SDK owns the protocol. This owns the things a connector catalogue needs and
the SDK has no opinion about:

- `AuthContext`     - verified identity, so no tool reads a raw header
- `Budget`          - a bound on what a single tool may return
- `validate_url`    - one SSRF check instead of three that disagree
- `redact`          - logging that cannot leak a credential by accident
- `readonly`        - a deployment that cannot be talked into a write
- `provenance`      - a marker for text from outside the trust boundary
"""

from .auth import (
    AuthContext,
    AuthError,
    LocalAuthContextFactory,
    PlatformTokenVerifier,
    assert_no_passthrough,
    validate_audience,
)
from .budget import Budget, BudgetExceeded, apply_budget, budget_call_result
from .net import InvalidURL, SSRFBlocked, is_blocked_address, validate_url
from .provenance import mark_result, wrap
from .readonly import ReadOnlyViolation, is_read_tool, read_only_enabled
from .redact import redact_mapping, scrub_text, upstream_error

__all__ = [
    "AuthContext",
    "AuthError",
    "Budget",
    "BudgetExceeded",
    "InvalidURL",
    "LocalAuthContextFactory",
    "PlatformTokenVerifier",
    "ReadOnlyViolation",
    "SSRFBlocked",
    "apply_budget",
    "budget_call_result",
    "assert_no_passthrough",
    "is_blocked_address",
    "is_read_tool",
    "mark_result",
    "read_only_enabled",
    "redact_mapping",
    "scrub_text",
    "upstream_error",
    "validate_audience",
    "validate_url",
    "wrap",
]

__version__ = "0.1.0"
