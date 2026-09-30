"""Every connector's standalone entrypoint must be usable from a container.

`ConnectorServer.run` defaults to `0.0.0.0`, and `effective_bind_host` clamps
that back to loopback in `local` mode unless `MCP_ALLOW_INSECURE_BIND=1`. That
guard is the right design: the framework knows the mode, so the framework
decides.

Every one of the 23 `__main__.py` files defaulted `MCP_HOST` to `127.0.0.1`,
which overrode the default before the guard ever saw it. The published image
bound loopback inside the container and was unreachable on its published port
**in every mode**, including `platform`. Measured by building the image and
curling it: connection refused, while the log cheerfully read
`Uvicorn running on http://127.0.0.1:8000`.

The guard's own docstring says connectors "pass 0.0.0.0 because that is what a
container needs". None of them did. A safety default applied one layer too low
silently disabled the layer above it and broke the artifact, and nothing was
red.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"

ENTRYPOINTS = sorted(CONNECTORS.glob("*/__main__.py"))
HOST_DEFAULT = re.compile(r'os\.getenv\(\s*"MCP_HOST"\s*,\s*"([^"]+)"\s*\)')


def test_there_are_entrypoints_to_check():
    """A glob that matches nothing makes every test below vacuously pass."""
    assert len(ENTRYPOINTS) >= 20


def test_no_entrypoint_defaults_to_loopback():
    offenders = []
    for path in ENTRYPOINTS:
        match = HOST_DEFAULT.search(path.read_text(encoding="utf-8"))
        if match and match.group(1) != "0.0.0.0":  # noqa: S104
            offenders.append(f"{path.parent.name}: defaults MCP_HOST to {match.group(1)}")
    assert not offenders, (
        "these entrypoints bind loopback before the framework can decide, "
        "which makes the container unreachable in every mode: " + "; ".join(offenders)
    )


def test_every_entrypoint_still_honours_the_env_var():
    """The default changed; the override must not have been lost with it."""
    for path in ENTRYPOINTS:
        assert HOST_DEFAULT.search(path.read_text(encoding="utf-8")), (
            f"{path.parent.name}: MCP_HOST is no longer read, so an operator "
            "cannot pin the bind address"
        )


def test_every_entrypoint_parses_and_reads_port():
    for path in ENTRYPOINTS:
        source = path.read_text(encoding="utf-8")
        ast.parse(source)
        assert 'os.getenv("PORT"' in source, f"{path.parent.name}: PORT is not configurable"


def test_the_framework_still_clamps_local_mode():
    """The whole point of moving the default: the guard, not the entrypoint,
    is what keeps an unauthenticated server off every interface."""
    from magone_mcp_core.server import ConnectorServer

    server = ConnectorServer(name="t", version="0.0.0", env={"MCP_MODE": "local"})

    assert server.effective_bind_host("0.0.0.0") == "127.0.0.1"  # noqa: S104


def test_the_override_still_works():
    from magone_mcp_core.server import ConnectorServer

    server = ConnectorServer(
        name="t",
        version="0.0.0",
        env={"MCP_MODE": "local", "MCP_ALLOW_INSECURE_BIND": "1"},
    )

    assert server.effective_bind_host("0.0.0.0") == "0.0.0.0"  # noqa: S104


def test_a_platform_mode_server_binds_what_it_was_given():
    from magone_mcp_core.server import ConnectorServer

    server = ConnectorServer(name="t", version="0.0.0", env={"MCP_MODE": "platform"})

    assert server.effective_bind_host("0.0.0.0") == "0.0.0.0"  # noqa: S104
