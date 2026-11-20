"""Run the playwright connector as a standalone server.

    python -m connectors.playwright

Binds every interface, because that is what a container needs. `local` mode
is unauthenticated, so the framework clamps it back to loopback unless
MCP_ALLOW_INSECURE_BIND=1.
"""

import os

from .server import server

if __name__ == "__main__":
    server.run(host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))  # noqa: S104
