"""magone-mcp-core: a thin layer over the official MCP Python SDK.

The SDK owns the protocol. This owns the things a connector catalogue needs and
the SDK has no opinion about:

- `Budget`          - a bound on what a single tool may return
- `redact`          - logging that cannot leak a credential by accident
- `readonly`        - a deployment that cannot be talked into a write
- `provenance`      - a marker for text from outside the trust boundary
"""

from .budget import Budget, BudgetExceeded, apply_budget, budget_call_result
from .provenance import mark_result, wrap
from .readonly import ReadOnlyViolation, is_read_tool, read_only_enabled
from .redact import redact_mapping, scrub_text, upstream_error

__all__ = [
    "Budget",
    "BudgetExceeded",
    "ReadOnlyViolation",
    "apply_budget",
    "budget_call_result",
    "is_read_tool",
    "mark_result",
    "read_only_enabled",
    "redact_mapping",
    "scrub_text",
    "upstream_error",
    "wrap",
]

__version__ = "0.1.0"
