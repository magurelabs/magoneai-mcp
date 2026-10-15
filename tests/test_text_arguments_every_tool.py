"""Gate: no tool in any connector JSON-decodes an argument its schema calls text.

`test_declared_text_stays_text.py` proves the framework rule on tools written
for the purpose. This proves it holds for every tool actually shipped, so a
tool registered some other way (straight on `server.mcp`, bypassing
`register_tool`), or a schema shape the rule does not recognise, fails here
instead of quietly turning `"null"` into None in production again.

For every property whose published schema admits a string, each of a set of
strings that are also valid JSON is put through the SDK's own call path,
`pre_parse_json` and then `validate_arguments`, and must come out as the same
`str`. The property selection is deliberately written independently of
`magone_mcp_core.text_args.admits_string`, so a gap in that function is not
also a gap in the gate.

`text_decoding_violations` takes a server rather than a package so a
downstream repository can run the same gate over its own connectors.
"""

import importlib
from pathlib import Path
from typing import Any

import pytest
from test_every_tool_is_callable import sample

pytestmark = pytest.mark.requires_connectors

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"
PACKAGES = sorted(p.name for p in CONNECTORS.iterdir() if (p / "server.py").exists())

# Every one of these is valid JSON, which is what made the SDK decode it.
PROBES = ("null", '{"a":1}', "[1]", "123", "true", "1.5", '"quoted"', "[]", "{}")

_CONSTRAINING = ("type", "anyOf", "oneOf", "allOf", "enum", "const", "$ref", "not")


def _schema_allows_string(schema: Any) -> bool:
    """Independent of `admits_string`, and narrower: only shapes that clearly do."""
    if schema is True:
        return True
    if not isinstance(schema, dict):
        return False
    if not any(k in schema for k in _CONSTRAINING):
        return True
    kind = schema.get("type")
    if kind == "string" or (isinstance(kind, list) and "string" in kind):
        return not any(k in schema for k in ("anyOf", "oneOf", "allOf", "enum", "const"))
    if "type" not in schema:
        for keyword in ("anyOf", "oneOf"):
            branches = schema.get(keyword) or []
            if any(isinstance(b, dict) and b.get("type") == "string" for b in branches):
                return True
    return False


def text_decoding_violations(server: Any, prefix: str = "") -> tuple[list[str], int]:
    """Every (tool, property, probe) whose text did not survive the call path."""
    violations: list[str] = []
    checked = 0
    for tool in server.mcp._tool_manager.list_tools():
        schema = tool.parameters or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for prop, prop_schema in props.items():
            if not _schema_allows_string(prop_schema):
                continue
            checked += 1
            for probe in PROBES:
                where = f"{prefix}{tool.name}.{prop} given {probe!r}"
                pre = tool.fn_metadata.pre_parse_json({prop: probe})
                if pre.get(prop) != probe or type(pre.get(prop)) is not str:
                    violations.append(f"{where}: pre-parse produced {pre.get(prop)!r}")
                    continue
                arguments = {r: sample(props.get(r, {}), r) for r in required}
                arguments[prop] = probe
                try:
                    validated = tool.fn_metadata.validate_arguments(arguments)
                except Exception as exc:  # noqa: BLE001
                    violations.append(f"{where}: refused ({type(exc).__name__})")
                    continue
                value = _validated_value(tool, validated, prop)
                if value != probe or type(value) is not str:
                    violations.append(f"{where}: handler would receive {value!r}")
    return violations, checked


def _validated_value(tool: Any, validated: dict, prop: str) -> Any:
    """`validate_arguments` returns keys by alias where one exists."""
    if prop in validated:
        return validated[prop]
    for name, field in tool.fn_metadata.arg_model.model_fields.items():
        if prop in (name, field.alias):
            return validated.get(field.alias or name, validated.get(name))
    return None


@pytest.mark.parametrize("package", PACKAGES)
def test_no_tool_decodes_an_argument_declared_as_text(package):
    module = importlib.import_module(f"connectors.{package}.server")
    violations, _ = text_decoding_violations(module.server, prefix=f"{package}/")
    assert not violations, "\n".join(violations)


def test_the_gate_is_not_vacuous():
    """A gate that checks nothing reports the same green as one that passes."""
    total = 0
    for package in PACKAGES:
        module = importlib.import_module(f"connectors.{package}.server")
        total += text_decoding_violations(module.server)[1]
    # 582 when written. A large drop means the selection broke, not the catalogue.
    assert total > 400, total
