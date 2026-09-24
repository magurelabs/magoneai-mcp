"""Error sanitization for database connectors.

Re-exported from ``shared/db_errors.py`` so every MCP server that talks to a
database redacts the same way. Kept as a module here because this server's
connectors and tests import ``connectors.errors``.
"""

from .._shared.db_errors import redact, sanitize_db_error

__all__ = ["sanitize_db_error", "redact"]
