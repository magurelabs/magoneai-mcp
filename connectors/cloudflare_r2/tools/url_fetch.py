"""The error the source-URL fetch raises.

The fetch itself lives in `server.py` and decides addresses with the shared
check in `magone_mcp_core.net`. This module used to hold a second full copy
of the fetch and of the blocked-range list, which nothing imported; only the
exception type was used.
"""


class RemoteFetchError(Exception):
    """A caller-supplied source URL was refused or could not be fetched."""
