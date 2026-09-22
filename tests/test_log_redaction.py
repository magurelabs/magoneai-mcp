"""Secrets never reach a log line, on any logging path a connector process uses.

Measured before redaction was wired: a vendor error logged
`api_key=sk_live_...` through structlog, and the SDK's traceback for a failed
tool printed a bearer token and an OAuth access token in full. The patterns
existed in `redact.py` and nothing called them.

Run in a subprocess because logging configuration is process-global and the
point is the whole process's output, stdout and stderr together.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRETS = [
    "sk_live_PLAINARGUMENT0123456789",
    "zoomsecret123456",
    "abcdefghij0123456789TOKEN",
    "ya29.SECRETACCESSTOKENVALUE",
    "hunter2pass",
]

SCRIPT = textwrap.dedent(
    """
    import asyncio, logging, sys
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    from mcp import Client
    from mcp.types import ToolAnnotations
    from magone_mcp_core import ConnectorServer

    server = ConnectorServer(name="redaction-test", env={})

    READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @server.tool(name="boom", description="Fails.", annotations=READ)
    async def boom() -> dict:
        raise RuntimeError(
            "upstream said: Authorization: Bearer abcdefghij0123456789TOKEN "
            "access_token=ya29.SECRETACCESSTOKENVALUE"
        )

    server.logger.warning(
        "vendor_error",
        api_key="sk_live_PLAINARGUMENT0123456789",
        body="401 token=zoomsecret123456",
    )
    logging.getLogger("httpx").error("connect to postgres://admin:hunter2pass@db/x failed")
    try:
        raise ValueError("Bearer abcdefghij0123456789TOKEN rejected")
    except ValueError:
        logging.getLogger("mcp.server").exception("tool failed")

    async def main():
        async with Client(server.mcp) as client:
            await client.call_tool("boom", {})

    asyncio.run(main())
    print("DONE")
    """
)


def run_script() -> str:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    return completed.stdout + completed.stderr


def test_no_secret_reaches_any_log_line():
    output = run_script()

    assert "DONE" in output, output
    leaked = [s for s in SECRETS if s in output]
    assert not leaked, f"leaked {leaked}:\n{output}"


def test_the_logs_still_say_what_happened():
    """Redaction must remove the secret, not the event."""
    output = run_script()

    assert "vendor_error" in output
    assert "tool failed" in output
    assert "[redacted]" in output
