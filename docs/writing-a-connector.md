# Writing a connector

This walks through a complete, minimal connector for a fictional "Example"
widgets API, from an empty directory to a pull request that passes every
gate. The code on this page was run against the framework and its gates
before it was written down.

Read [the boundary rule](../CONTRIBUTING.md#the-boundary-rule) first. A
connector belongs here only if an outside developer can use it against their
own account and their own storage. [architecture.md](architecture.md)
explains what the framework does around each tool.

## 1. Lay out the package

Connector directories use underscores, because the package is imported as
`connectors.<name>`:

```text
connectors/example_widgets/
    __init__.py
    __main__.py
    server.py
    tests/
        __init__.py
        test_widgets.py
```

`__init__.py` re-exports the server:

```python
"""The example-widgets connector."""

from .server import server

__all__ = ["server"]
```

`__main__.py` starts it. Copy this exactly; the framework, not this file,
decides whether the bind is safe for the current mode:

```python
"""Run the example-widgets connector: python -m connectors.example_widgets"""

import os

from .server import server

if __name__ == "__main__":
    server.run(host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))  # noqa: S104
```

## 2. Write the server

`server.py`:

```python
"""The example-widgets connector: list and create widgets in an Example account."""

from typing import Annotated

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from magone_mcp_core import ConnectorServer
from magone_mcp_core.redact import upstream_error

SERVER_NAME = "example-widgets"
SERVER_VERSION = "0.1.0"

API_BASE = "https://api.example.com/v1"

server = ConnectorServer(name=SERVER_NAME, version=SERVER_VERSION)


def _api_key(credentials: dict) -> str:
    key = credentials.get("example_api_key", "")
    if not key:
        # A ValueError's message reaches the caller, scrubbed and capped.
        raise ValueError("No Example API key. Send it as the X-Example-Api-Key header.")
    return key


async def _call(credentials: dict, method: str, path: str, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {_api_key(credentials)}"}
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
    if response.is_error:
        # A refusal: `isError` is set and the model reads the reason.
        # upstream_error scrubs anything credential-shaped from the body.
        return {
            "error": f"Example answered {response.status_code}: {upstream_error(response.text)}"
        }
    return response.json()


@server.tool(
    name="list_widgets",
    description="List widgets in a workspace, newest first.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=["X-Example-Api-Key"],
    permission_group="read_widgets",
    required_scopes=["widgets:read"],
)
async def list_widgets(
    credentials: dict,
    workspace: Annotated[str, Field(description="Workspace slug, for example 'acme'.")],
    limit: Annotated[
        int, Field(description="How many widgets to return, 1 to 100.", ge=1, le=100)
    ] = 20,
) -> dict:
    """Return the newest widgets in a workspace."""
    return await _call(
        credentials, "GET", f"/workspaces/{workspace}/widgets", params={"limit": limit}
    )


@server.tool(
    name="create_widget",
    description="Create a widget in a workspace and return it.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    credential_headers=["X-Example-Api-Key"],
    permission_group="write_widgets",
    required_scopes=["widgets:write"],
)
async def create_widget(
    credentials: dict,
    workspace: Annotated[str, Field(description="Workspace slug, for example 'acme'.")],
    name: Annotated[str, Field(description="Display name of the new widget.")],
) -> dict:
    """Create one widget."""
    return await _call(credentials, "POST", f"/workspaces/{workspace}/widgets", json={"name": name})
```

What each part is doing, and why it is required:

| Part | Why |
|---|---|
| `SERVER_NAME`, `SERVER_VERSION` as plain string constants | `scripts/gen_server_json.py` reads them statically to build the publication manifest, so the manifest cannot describe a different server |
| `credential_headers=["X-Example-Api-Key"]` | the only headers this tool may receive. The framework turns them into `credentials["example_api_key"]` per call and hides `credentials` from the advertised schema. The caller's own key, on every call; never one key for everyone |
| `annotations=` with both hints | `readOnlyHint` decides whether `MCP_READ_ONLY=1` offers the tool. The linter fails a tool missing either hint, or claiming both |
| `Field(description=...)` on every parameter | the description is what the model reads to decide what to pass |
| `permission_group=`, `required_scopes=` | lets a host that enforces selection offer a connection only the reads, and tells a client which vendor scopes a selection needs. Selection is a caller-supplied hint, not a server-side control; `MCP_READ_ONLY=1` is the one that restricts writes |
| `{"error": "..."}` or `ValueError` | reported to the caller with `isError`. Any other exception is masked as a generic failure |
| `upstream_error()` | a vendor's error body often echoes the token that failed |

Things the framework already does, so a connector must not redo them: bound
the result size, log arguments by key only, redact secrets in logs, enforce
read-only mode and tool selection, check the Host header, authenticate the
caller.

If a tool takes a URL from the caller and fetches it, pass it through
`magone_mcp_core.validate_url` first. The linter fails a tool with a
`url`-like parameter that never calls it.

## 3. Test it without credentials or network

`tests/test_widgets.py`:

```python
"""No credentials and no network: the vendor is an httpx.MockTransport."""

import importlib

import httpx
import pytest

# By importlib, not `from connectors.example_widgets import server`: the package
# re-exports the `server` object under that name, which shadows the module.
server_module = importlib.import_module("connectors.example_widgets.server")
create_widget = server_module.create_widget
list_widgets = server_module.list_widgets


@pytest.fixture
def vendor(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sent = request.headers.get("Authorization", "")
        if sent != "Bearer test-key":
            # Vendors often echo the token that failed. It must not survive.
            return httpx.Response(401, json={"message": f"invalid {sent}"})
        return httpx.Response(200, json={"widgets": [{"id": "w1"}]})

    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(server_module.httpx, "AsyncClient", client)
    return seen


async def test_list_widgets_sends_the_key_and_the_limit(vendor):
    result = await list_widgets(
        credentials={"example_api_key": "test-key"}, workspace="acme", limit=5
    )
    assert result == {"widgets": [{"id": "w1"}]}
    assert vendor[0].url.params["limit"] == "5"


async def test_a_vendor_refusal_is_an_error_without_the_token(vendor):
    result = await list_widgets(credentials={"example_api_key": "wrong-key-1234"}, workspace="acme")
    assert result["error"].startswith("Example answered 401")
    assert "wrong-key-1234" not in result["error"]


async def test_a_missing_key_says_which_header_to_send():
    with pytest.raises(ValueError, match="X-Example-Api-Key"):
        await create_widget(credentials={}, workspace="acme", name="first")


async def test_the_tools_are_registered_with_described_properties():
    tools = {t.name: t for t in await server_module.server.mcp.list_tools()}
    assert set(tools) == {"list_widgets", "create_widget"}
    props = tools["list_widgets"].input_schema["properties"]
    assert "credentials" not in props
    assert all(p.get("description") for p in props.values())
```

The decorator returns your original function, so a test calls the handler
directly and passes `credentials=` itself. The framework's own tests cover
what happens over the wire (headers to `credentials`, read-only refusal,
selection, budget); a connector's tests cover the connector.

```bash
pytest connectors/example_widgets -q
```

## 4. Wire it into the repository

Each of these is checked by a gate, so a missed step fails CI with a message
saying what to do.

1. **Dependencies.** This example uses only `httpx`, which the core already
   depends on. A connector that needs a third-party library declares an extra
   named after it in `pyproject.toml`, adds that extra to `all`, and adds it to
   the `pip install` line of the Conformance job in `.github/workflows/ci.yml`:

   ```toml
   example_widgets = ["example-sdk>=1.2"]
   ```

2. **Publication status.** Add an entry to `connectors/publication.json`. A
   new connector is `blocked` until someone has run it end to end against a
   live account:

   ```json
   "example_widgets": {
     "status": "blocked",
     "reason": "New. Not yet verified end to end against a live account."
   }
   ```

   Moving it to `ready` later is its own pull request: change the status, add
   a one-sentence description (100 characters at most) to `DESCRIPTIONS` in
   `scripts/gen_server_json.py`, and run `python scripts/gen_server_json.py`
   to write `connectors/example_widgets/server.json`.

3. **Conformance scope.** Add `example_widgets` to the `ALL` list in the
   `scope` job of `ci.yml`, so the official MCP conformance suite runs
   against it.

4. **Golden baseline.** New tools are a change to the tool contract, so
   approve them, in a commit of their own:

   ```bash
   python scripts/golden_schema_gate.py --update
   git add tests/golden/tool-schemas.json
   git commit -s -m "test(golden): approve the example_widgets tools"
   ```

5. **Tool reference.** `python scripts/gen_tool_reference.py` regenerates
   `docs/TOOLS.md`.

6. **Changelog.** Add a line under `Unreleased` / `Added` in
   [CHANGELOG.md](../CHANGELOG.md).

## 5. Run it

```bash
MCP_MODE=local python -m connectors.example_widgets
```

Then connect an MCP client to `http://127.0.0.1:8000/mcp` and send your key
as the `X-Example-Api-Key` header, or, for a server only you use, put
`{"example_api_key": "..."}` in a file and set `MCP_CREDENTIALS_PATH` to it.

Before opening the pull request, run every gate and the tests as listed in
[CONTRIBUTING.md](../CONTRIBUTING.md#running-the-tests-and-every-gate).

## Porting an existing tool

If you are bringing across a tool whose schema agents already rely on, do
not let the SDK regenerate it from type hints: that invents titles and drops
descriptions. Declare it byte for byte instead:

```python
from magone_mcp_core import declare_schema


@server.tool(name="search_web", description="Search the web.", annotations=...)
@declare_schema(
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "required": ["query"],
    }
)
async def search_web(query: str) -> dict: ...
```

The schema must be a literal, so the golden gate can diff it, and every
property needs a `description`.
