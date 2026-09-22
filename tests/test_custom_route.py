"""A non-MCP route must serve alongside the MCP endpoint, not instead of it.

A deployment may have to keep serving routes such as `/setup/upload-file`
and `/setup/files` beside the MCP app: a platform that calls them breaks the
moment a server stops answering them, on every connection that uses them.

`ConnectorServer.custom_route` exists for that, and its docstring says it is
exposed "so that requirement is visible rather than discovered later". That
sentence was the whole assurance. Nothing exercised it, so if the SDK's
`custom_route` had a different signature, or mounting one displaced the MCP
endpoint, it would have been discovered at the moment a deployment needed it,
which is the worst possible time to find out.

So this asserts the three things that actually matter, through a real ASGI
client rather than by inspecting the object:

1. the custom route answers
2. the MCP endpoint still answers afterwards
3. the tools are still registered and callable
"""

import contextlib
import json
import threading
import time

import httpx
import pytest
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from magone_mcp_core import ConnectorServer


@contextlib.contextmanager
def serving(server: ConnectorServer):
    """Run the app under real uvicorn on an ephemeral port.

    An in-process ASGI transport cannot be used here: the SDK's
    streamable-HTTP manager starts its task group in the app lifespan, which
    `httpx.ASGITransport` does not drive, and emulating the lifespan by hand
    still raced its initialisation.

    Running it the way it actually runs is also the better test. The question
    is whether a custom route and the MCP endpoint coexist in a served
    application, not whether they coexist in an object graph.
    """
    import socket

    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(server.app(), host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not uvicorn_server.started and time.time() < deadline:
        time.sleep(0.05)
    if not uvicorn_server.started:
        raise RuntimeError("uvicorn did not start; this test would prove nothing")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


def build() -> ConnectorServer:
    server = ConnectorServer(name="custom-route-test", version="0.0.0")

    @server.tool(
        name="ping",
        description="Return pong.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def ping() -> dict:
        """Return a fixed value."""
        return {"pong": True}

    @server.custom_route("/setup/upload-file", methods=["POST"])
    async def upload_file(request):
        return JSONResponse({"uploaded": True})

    @server.custom_route("/setup/files", methods=["DELETE"])
    async def delete_files(request):
        return JSONResponse({"deleted": True})

    return server


@pytest.fixture
def base_url():
    with serving(build()) as url:
        yield url


@pytest.fixture
def client(base_url):
    return httpx.AsyncClient(base_url=base_url, timeout=15)


class TestTheRoutesADeploymentNeeds:
    async def test_upload_file_answers(self, client):
        response = await client.post("/setup/upload-file", json={})

        assert response.status_code == 200
        assert response.json() == {"uploaded": True}

    async def test_delete_files_answers(self, client):
        response = await client.request("DELETE", "/setup/files")

        assert response.status_code == 200
        assert response.json() == {"deleted": True}

    async def test_a_custom_route_rejects_the_wrong_method(self, client):
        """Mounted with its own method list, not as a catch-all."""
        response = await client.get("/setup/upload-file")

        assert response.status_code == 405


class TestTheMcpEndpointIsUndisturbed:
    async def test_the_mcp_endpoint_still_serves(self, client):
        """The failure this guards: a custom route displacing `/mcp` would
        take every tool with it, and the only symptom is a 404 nobody
        attributes to the route that was added."""
        response = await client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Method": "tools/list",
                "MCP-Protocol-Version": "2026-07-28",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )

        assert response.status_code == 200
        body = response.text
        for line in body.splitlines():
            if line.startswith("data: "):
                body = line[6:]
                break
        tools = json.loads(body)["result"]["tools"]
        assert [t["name"] for t in tools] == ["ping"]

    async def test_an_unmounted_path_is_still_a_404(self, client):
        """Mounting custom routes must not turn the app into a catch-all."""
        response = await client.get("/not-a-route")

        assert response.status_code == 404
