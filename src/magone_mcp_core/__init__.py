"""magone-mcp-core: a thin layer over the official MCP Python SDK.

The SDK owns the protocol. This owns the things a connector catalogue needs and
the SDK has no opinion about:

- `declare_schema`  - keep a hand-written input schema exactly as written
- `AuthContext`     - verified identity, so no tool reads a raw header
- `Budget`          - a bound on what a single tool may return
- `validate_url`    - one SSRF check instead of three that disagree
- `redact`          - logging that cannot leak a credential by accident
- `readonly`        - a deployment that cannot be talked into a write
- `selection`       - the tools one connection selected, and nothing else
- `provenance`      - a marker for text from outside the trust boundary
- `content`         - move bytes without holding storage credentials
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
from .content import (
    ContentHandle,
    ContentHandleError,
    ContentHandleService,
    Direction,
    HandleExpired,
    HandleRedeemed,
    HandleState,
    PresignedMinter,
    PresignedUrlSigner,
    safe_display_name,
)
from .net import InvalidURL, SSRFBlocked, is_blocked_address, validate_url
from .provenance import mark_result, wrap
from .readonly import ReadOnlyViolation, is_read_tool, read_only_enabled
from .redact import redact_mapping, scrub_text, upstream_error
from .schema import (
    declare_output_schema,
    declare_schema,
    declared_output_schema,
    declared_schema,
    register_tool,
)
from .selection import (
    ENABLED_GROUPS_HEADER,
    ENABLED_TOOLS_HEADER,
    NotEnabled,
    assert_enabled,
    enabled_tool_names,
)

__all__ = [
    "ENABLED_GROUPS_HEADER",
    "ENABLED_TOOLS_HEADER",
    "NotEnabled",
    "assert_enabled",
    "enabled_tool_names",
    "AuthContext",
    "AuthError",
    "Budget",
    "BudgetExceeded",
    "ContentHandle",
    "ContentHandleError",
    "ContentHandleService",
    "Direction",
    "HandleExpired",
    "HandleRedeemed",
    "HandleState",
    "PresignedMinter",
    "PresignedUrlSigner",
    "safe_display_name",
    "InvalidURL",
    "LocalAuthContextFactory",
    "PlatformTokenVerifier",
    "ReadOnlyViolation",
    "SSRFBlocked",
    "apply_budget",
    "budget_call_result",
    "assert_no_passthrough",
    "declare_output_schema",
    "declare_schema",
    "declared_output_schema",
    "declared_schema",
    "is_blocked_address",
    "is_read_tool",
    "mark_result",
    "read_only_enabled",
    "redact_mapping",
    "register_tool",
    "scrub_text",
    "upstream_error",
    "validate_audience",
    "validate_url",
    "wrap",
]

__version__ = "0.1.0"
