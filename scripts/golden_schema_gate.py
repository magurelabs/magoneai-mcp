"""Fail if any tool's wire contract changed without approval.

The risk this exists for: a tool's input schema is what an agent is told it may
pass. Change one silently and every saved agent configuration that relied on
the old shape starts behaving differently, with no error and no failing health
check. It is the largest single risk in porting a connector catalogue.

The schemas here are declared rather than derived, so the expected diff is
**empty by construction**. That makes this a regression gate rather than a
review queue: any diff at all means something changed that nobody intended, or
somebody changed a schema and has to say so.

Approving a change is deliberate and leaves a record: regenerate the baseline
in its own commit, so the schema diff is the whole diff and a reviewer sees
exactly which tools moved.

    python scripts/golden_schema_gate.py --check      # CI
    python scripts/golden_schema_gate.py --update     # approve, in its own commit
"""

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "tests" / "golden" / "tool-schemas.json"
CONNECTORS = REPO_ROOT / "connectors"


def _decorator_named(node: ast.AST, attr: str = "", name: str = "") -> Any:
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        if attr and isinstance(fn, ast.Attribute) and fn.attr == attr:
            return dec
        if name and isinstance(fn, ast.Name) and fn.id == name:
            return dec
    return None


def collect(root: Path) -> dict[str, dict[str, Any]]:
    """Every registered tool's declared contract, read statically.

    Static rather than by import, so the gate runs with no credentials, no
    network and no connector dependencies installed. A gate that needs the
    whole catalogue importable is a gate that gets skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise SystemExit(f"{path}: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            registration = _decorator_named(node, attr="tool")
            if registration is None:
                continue
            name = None
            annotations: dict[str, Any] = {}
            for kw in registration.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
                if kw.arg == "annotations" and isinstance(kw.value, ast.Call):
                    for akw in kw.value.keywords:
                        try:
                            annotations[akw.arg] = ast.literal_eval(akw.value)
                        except (ValueError, SyntaxError):
                            annotations[akw.arg] = "<non-literal>"
            if not name:
                continue
            schema: Any = None
            declaration = _decorator_named(node, name="declare_schema")
            if declaration is not None and declaration.args:
                try:
                    schema = ast.literal_eval(declaration.args[0])
                except (ValueError, SyntaxError):
                    # A schema that is not a literal cannot be diffed, and a
                    # gate that silently skips it is worse than one that fails.
                    raise SystemExit(
                        f"{path}:{node.lineno}: {name} declares a non-literal schema; "
                        "the golden gate cannot diff it"
                    ) from None
            key = f"{path.relative_to(root).parts[0]}/{name}"
            out[key] = {"schema": schema, "annotations": annotations}
    return out


def render_diff(baseline: dict[str, Any], current: dict[str, Any]) -> str:
    lines = []
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed = sorted(k for k in set(baseline) & set(current) if baseline[k] != current[k])

    for key in removed:
        lines.append(f"  REMOVED  {key}")
        lines.append(
            "           a tool that existed is gone; every saved agent config naming it breaks"
        )
    for key in added:
        lines.append(f"  ADDED    {key}")
    for key in changed:
        lines.append(f"  CHANGED  {key}")
        before, after = baseline[key], current[key]
        if before.get("schema") != after.get("schema"):
            before_schema = before.get("schema") or {}
            after_schema = after.get("schema") or {}
            b = before_schema.get("properties", {})
            a = after_schema.get("properties", {})
            for prop in sorted(set(b) | set(a)):
                if b.get(prop) != a.get(prop):
                    lines.append(f"           property {prop!r}:")
                    lines.append(f"             was: {json.dumps(b.get(prop), sort_keys=True)}")
                    lines.append(f"             now: {json.dumps(a.get(prop), sort_keys=True)}")
            # A changed `required` list is a behaviour change, not a cosmetic
            # one: dropping a name makes a previously mandatory argument
            # optional, and adding one breaks every existing caller. Reporting
            # only property diffs left this showing as a bare "CHANGED" with
            # no detail, which is the least useful thing a gate can say.
            for field in ("required", "type", "additionalProperties"):
                if before_schema.get(field) != after_schema.get(field):
                    lines.append(f"           {field}:")
                    lines.append(
                        f"             was: {json.dumps(before_schema.get(field), sort_keys=True)}"
                    )
                    lines.append(
                        f"             now: {json.dumps(after_schema.get(field), sort_keys=True)}"
                    )
        if before.get("annotations") != after.get("annotations"):
            lines.append(
                f"           annotations: {before.get('annotations')} -> {after.get('annotations')}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on any diff (CI)")
    parser.add_argument("--update", action="store_true", help="approve the current state")
    args = parser.parse_args()

    if not CONNECTORS.exists():
        print(f"no connectors directory at {CONNECTORS}; nothing to check")
        return 0

    current = collect(CONNECTORS)

    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline updated: {len(current)} tools -> {BASELINE.relative_to(REPO_ROOT)}")
        print("commit this on its own, so the schema change is the whole diff")
        return 0

    if not BASELINE.exists():
        print(f"no baseline at {BASELINE.relative_to(REPO_ROOT)}; run --update once to create it")
        return 1

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if baseline == current:
        print(f"golden schema gate: {len(current)} tools, no change")
        return 0

    print("golden schema gate FAILED: the tool contract changed.\n")
    print(render_diff(baseline, current))
    print(
        "\nIf this change is intended, run:\n"
        "    python scripts/golden_schema_gate.py --update\n"
        "and commit the baseline on its own so a reviewer sees exactly which "
        "tools moved. If it is not intended, a schema was regenerated rather "
        "than declared: check that the tool still carries @declare_schema."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
