"""`on_shutdown` hooks run when the served application stops.

v1 closed database pools in `@app.on_event("shutdown")`; v2 had no
equivalent. Driven through the real ASGI lifespan protocol, the way uvicorn
drives it, because the SDK app is built with its own lifespan and would ignore
a Starlette `on_shutdown` handler.
"""

import asyncio

from magone_mcp_core import ConnectorServer


async def drive_lifespan(app) -> list[str]:
    sent: list[str] = []
    messages = asyncio.Queue()
    await messages.put({"type": "lifespan.startup"})
    await messages.put({"type": "lifespan.shutdown"})

    async def receive():
        return await messages.get()

    async def send(message):
        sent.append(message["type"])

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    return sent


async def test_hooks_run_before_shutdown_completes():
    server = ConnectorServer(name="hooks", env={})
    order: list[str] = []

    @server.on_shutdown
    async def close_pools():
        order.append("closed")

    sent = await drive_lifespan(server.app())

    assert order == ["closed"]
    assert sent[-1] == "lifespan.shutdown.complete"


async def test_a_failing_hook_does_not_stop_the_others():
    server = ConnectorServer(name="hooks", env={})
    order: list[str] = []

    @server.on_shutdown
    def broken():
        raise RuntimeError("pool stuck")

    @server.on_shutdown
    def still_runs():
        order.append("second")

    sent = await drive_lifespan(server.app())

    assert order == ["second"]
    assert "lifespan.shutdown.complete" in sent
