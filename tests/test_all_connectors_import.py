"""Every connector must import. Eleven of twenty-three did not.

A connector that does not import cannot run, cannot be conformance-tested, and
cannot be containerised. It is not a partially working connector; it is a
directory.

This was invisible because nothing imported them all. The golden-schema gate,
the boundary gate, the compile gate and the tool-contract linter are all
static: they parse, they do not execute. `compile_gate` proves a module *can*
be compiled, which is strictly weaker than proving its imports resolve. So 11
connectors parsed cleanly, passed every gate, published a manifest, and would
have raised `ModuleNotFoundError` the moment anything started them.

What the eleven actually were, because the pattern matters more than the count:

- **Split residue.** `cloudflare_r2` and `outlook_email` still imported names
  that had moved out of this repository. Splitting a package updated the
  package that gained code and not the one that lost it.
- **A `_shared` package that vendored nothing.** Four connectors imported
  constants from a package containing only a docstring promising "vendored
  helpers from the v1 repository-wide shared package".
- **v1-layout absolute imports.** `trino` and `twitter_intel` imported their
  own submodules as if the v1 directory layout still existed.
- **Dead platform coupling.** Both Atlassian connectors imported the retired
  v1 framework for a function that v2 cannot call.
- **An error handler that could not run.** `database` logged a common Oracle
  failure through a name defined 450 lines below, so the handler for the
  likely case raised `NameError` instead.

Importing is a low bar. It is also the bar underneath every other check here,
which is why nothing else caught it.
"""

import importlib
from pathlib import Path

import pytest

# These import real connectors, so they need the connector dependencies.
# `pip install -e ".[dev]"` deliberately installs only the framework's, so an
# outside contributor can run most of the suite with almost nothing. The
# Connector tests job installs `.[dev,all]` and runs exactly these.
pytestmark = pytest.mark.requires_connectors

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"

PACKAGES = sorted(p.name for p in CONNECTORS.iterdir() if (p / "server.py").exists())


def test_there_are_connectors_to_import():
    """A glob that matches nothing makes every test below vacuously pass."""
    assert len(PACKAGES) >= 20, f"expected at least 20 connectors, found {len(PACKAGES)}"


@pytest.mark.parametrize("package", PACKAGES)
def test_connector_imports(package):
    """Imported by name through importlib, never `import connectors.x.server`.

    `__init__.py` re-exports `server`, so the attribute access in
    `import connectors.x.server as m` hands back the ConnectorServer OBJECT
    rather than the module. That shadowing has produced a false result three
    separate times in this programme, including once while writing this file.
    """
    module = importlib.import_module(f"connectors.{package}.server")
    assert module.__name__ == f"connectors.{package}.server"


@pytest.mark.parametrize("package", PACKAGES)
def test_connector_entrypoint_imports(package):
    """`python -m connectors.<x>` is how the container starts one, so the
    entrypoint has to resolve too, not just the server module."""
    importlib.import_module(f"connectors.{package}.__main__")


@pytest.mark.parametrize("package", PACKAGES)
def test_connector_registers_at_least_one_tool(package):
    """Importing is not enough: a server that imports and registers nothing
    starts, answers, and offers an empty catalogue, and nothing else here
    would notice."""
    module = importlib.import_module(f"connectors.{package}.server")
    server = module.server
    tools = server.mcp._tool_manager._tools
    assert tools, f"{package} registers no tools"
