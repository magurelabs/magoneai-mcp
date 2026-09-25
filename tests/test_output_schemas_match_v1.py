"""Every connector tool publishes v1's output schema, or none where v1 had none.

v1 declared `output_schema` on 101 tools and published it in `tools/list`; the
platform reads it to offer a TOOL node's output fields to the steps after it.
The port carried every input schema across byte for byte and dropped every
output one, and the SDK filled the gap with a `{"result": ...}` wrapper
describing nothing. The input golden gate could not see either, because it
only ever looked at inputs.

`golden/output-schemas.json` is v1's declared output schema for each public
tool, taken from the pre-port baseline, not from this repository. It is the
expected value, so a tool that drifts, loses its declaration, or regains the
SDK wrapper fails here by name.
"""

import importlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_connectors

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED = json.loads((REPO_ROOT / "tests" / "golden" / "output-schemas.json").read_text())
PACKAGES = sorted(
    p.name for p in (REPO_ROOT / "connectors").iterdir() if (p / "server.py").exists()
)


def advertised(package: str) -> dict:
    module = importlib.import_module(f"connectors.{package}.server")
    tools = module.server.mcp._tool_manager._tools
    return {name: tool.output_schema for name, tool in tools.items()}


def test_the_expected_set_is_not_empty():
    assert len(EXPECTED) >= 74


@pytest.mark.parametrize("package", PACKAGES)
def test_output_schemas_match_v1(package):
    wrong = {}
    for name, schema in advertised(package).items():
        expected = EXPECTED.get(f"{package}/{name}")
        if schema != expected:
            wrong[name] = {"expected": expected, "advertised": schema}
    assert not wrong, json.dumps(wrong, indent=2)[:3000]


def test_every_expected_tool_exists():
    """A renamed or removed tool must not pass by being absent."""
    present = {f"{p}/{n}" for p in PACKAGES for n in advertised(p)}
    missing = sorted(set(EXPECTED) - present)
    assert not missing, missing
