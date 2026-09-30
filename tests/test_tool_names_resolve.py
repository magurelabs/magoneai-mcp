"""Every name a tool looks up must exist. Twenty-five of them did not.

`test_all_connectors_import.py` was written after eleven connectors were found
not to import, and it tests exactly that. Importing turned out to be necessary
and not sufficient, because Python resolves a global at CALL time:

    outlook_calendar    6 of 6 tools
    microsoft_teams    12 of 12 tools
    outlook_email       6 of 27 tools
    google_drive        1 of 3 tools

    NameError: name '_graph_request' is not defined
      connectors/outlook_calendar/server.py:636, in list_events

v1's Microsoft connectors were mixin classes, so `_graph_request` was a method
and the bodies called it as one. The port flattened those bodies to module
level and carried the method across for only two of the four. The Google
connectors had the identical defect, and there the call sites even carried
`# type: ignore[attr-defined]`, suppressing the one warning anyone would have
seen.

**Why it was invisible.** Every ported body ends in `except Exception`, so the
NameError came back as

    {"success": false, "error": "Failed to list events"}

To an operator, to the agent, and in the logs, that is an expired token or a
Graph outage. These would have shipped, failed on first use, and been triaged
as a Microsoft problem.

**Why this is an AST pass and not a code-object one.** `code.co_names` was the
obvious tool and is the wrong one: it also holds attribute names and keyword
argument names, so `row.get(...)` and `_store(bucket=...)` both read as
undefined globals. That needs an allowlist, and an allowlist is how a real
finding gets waved through. `ast.Name` in a Load context is exactly the set of
bare-name lookups, provided every binding form is collected - which is the
fiddly half and the reason this is written once, here.
"""

import ast
import builtins
import importlib
import pathlib

import pytest

# These import real connectors, so they need the connector dependencies.
# `pip install -e ".[dev]"` deliberately installs only the framework's, so an
# outside contributor can run most of the suite with almost nothing. The
# Connector tests job installs `.[dev,all]` and runs exactly these.
pytestmark = pytest.mark.requires_connectors

REPO = pathlib.Path(__file__).resolve().parents[1]

CONNECTORS = sorted(
    package.name
    for package in (REPO / "connectors").iterdir()
    if package.is_dir() and not package.name.startswith(("_", "."))
)


def bound_names(node: ast.AST) -> set:
    """Every name bound inside a function body.

    Missing any one of these produces a false positive, and a gate that cries
    wolf is one people learn to skip. The non-obvious entries:

    - `ast.arg` covers lambda and nested-function parameters.
    - `ExceptHandler.name` binds `exc` in `except E as exc`.
    - a nested `def`/`class` binds its own name through the NODE, not through
      an `ast.Name`, so a helper defined inside a tool would otherwise read as
      undefined.
    """
    names: set = set()
    arguments = node.args
    for group in (arguments.posonlyargs, arguments.args, arguments.kwonlyargs):
        names.update(argument.arg for argument in group)
    for extra in (arguments.vararg, arguments.kwarg):
        if extra:
            names.add(extra.arg)
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Del)):
            names.add(inner.id)
        elif isinstance(inner, ast.arg):
            names.add(inner.arg)
        elif isinstance(inner, ast.ExceptHandler) and inner.name:
            names.add(inner.name)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            for alias in inner.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(inner, (ast.Global, ast.Nonlocal)):
            names.update(inner.names)
        elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(inner.name)
    return names


def unresolved(package: str) -> dict:
    module = importlib.import_module(f"connectors.{package}.server")
    registered = set(module.server.mcp._tool_manager._tools)
    available = set(vars(module)) | set(dir(builtins))
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")

    found: dict = {}
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in registered:
            continue
        local = bound_names(node)
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load)):
                continue
            if inner.id in local or inner.id in available:
                continue
            found.setdefault(node.name, set()).add(inner.id)
    return found


class TestEveryToolsNamesResolve:
    def test_there_are_connectors_to_check(self):
        """A gate that finds nothing passes. This programme has already shipped
        two of those."""
        assert len(CONNECTORS) >= 20, CONNECTORS

    @pytest.mark.parametrize("package", CONNECTORS)
    def test_no_registered_tool_references_a_missing_name(self, package):
        found = unresolved(package)

        assert not found, (
            f"{package}: these REGISTERED tools reference names that do not "
            "exist, so they raise NameError when called and the surrounding "
            "`except Exception` returns it as a vendor failure: "
            + "; ".join(f"{tool} -> {sorted(names)}" for tool, names in sorted(found.items()))
        )


class TestTheCheckItselfWorks:
    """A gate nobody has seen fail is a gate nobody knows the shape of."""

    def test_it_catches_a_missing_global(self):
        source = "def a_tool():\n    return _not_defined_anywhere\n"
        node = ast.parse(source).body[0]
        local = bound_names(node)
        loads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

        assert "_not_defined_anywhere" in loads - local

    def test_it_does_not_flag_a_local(self):
        source = "def a_tool(x):\n    y = x + 1\n    return y\n"
        node = ast.parse(source).body[0]
        local = bound_names(node)
        loads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

        assert not (loads - local)

    def test_it_does_not_flag_an_attribute_or_a_keyword_argument(self):
        """The two false positives that made `co_names` unusable."""
        source = "def a_tool(row, store):\n    return store(bucket=row.get('b'))\n"
        node = ast.parse(source).body[0]
        local = bound_names(node)
        loads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

        assert not (loads - local), "attribute or keyword name leaked through"

    def test_it_does_not_flag_a_nested_helper(self):
        source = "def a_tool():\n    def _helper():\n        return 1\n    return _helper()\n"
        node = ast.parse(source).body[0]
        local = bound_names(node)
        loads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

        assert not (loads - local), "a nested def read as an undefined global"

    def test_it_does_not_flag_an_except_binding(self):
        source = (
            "def a_tool():\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as exc:\n"
            "        return exc\n"
        )
        node = ast.parse(source).body[0]
        local = bound_names(node)
        loads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

        assert not (loads - local - {"ValueError"})
