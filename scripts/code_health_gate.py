"""Static checks for the defect classes the port produced and nothing caught.

Each rule exists because the defect it describes shipped, passed every other
gate, and broke tools at runtime:

1. **Undefined names, whole module** (ruff F821/F822). The earlier name gate
   looked only inside registered tool bodies; the NameErrors in DocuSign,
   SharePoint and Outlook were one call deeper, in helpers.
2. **A function defined twice at module level.** Jira and Confluence carried
   two `resolve_cloud_id`s; the later one, with a different signature,
   shadowed the one every tool called. Ruff does not flag a redefinition of a
   used name.
3. **A module-level constant reassigned with a different value.** SurveyMonkey
   carried two `_DEFAULT_INCLUDE`s from two v1 modules; the second silently
   replaced the first and one tool started asking the vendor for the wrong
   fields. Equal reassignments are allowed (the port left many harmless ones).
4. **A relative import that does not resolve, including one inside a
   function.** Linear imported `comment_to_dict` from a `._client` module that
   does not exist, inside the branch every default call took. The module
   imported fine, so the import gate passed.

Run from the repository root. Exit 1 on any finding. An optional argument
names another directory to check, so a downstream repository can run
these same rules over its own connectors.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "connectors"


def ruff_undefined(target: Path) -> list[str]:
    out = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(target),
            "--select",
            "F821,F822",
            "--no-cache",
            "--output-format",
            "json",
            "--exclude",
            "**/tests/**",
            # Ignore the checked repository's own ruff config: a repository
            # that excludes its generated packages from lint would otherwise
            # hide every undefined name in them from this gate.
            "--isolated",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    try:
        findings = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return [f"ruff did not run: {out.stderr.strip()[:300]}"]
    return [
        f"{_rel(Path(f['filename']))}:{f['location']['row']}: {f['code']} {f['message']}"
        for f in findings
    ]


def literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return ast.dump(node)


def _rel(path: Path) -> Path:
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path


def module_findings(path: Path) -> list[str]:
    rel = _rel(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    findings: list[str] = []

    defs: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in defs:
                findings.append(
                    f"{rel}:{node.lineno}: `{node.name}` defined again (first at line "
                    f"{defs[node.name]}); the later one silently replaces it"
                )
            defs.setdefault(node.name, node.lineno)

    values: dict[str, tuple[int, object]] = {}
    for node in tree.body:
        single = isinstance(node, ast.Assign) and len(node.targets) == 1
        if single and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            value = literal(node.value)
            if name in values and values[name][1] != value and name != "__all__":
                findings.append(
                    f"{rel}:{node.lineno}: `{name}` reassigned with a different value "
                    f"(first at line {values[name][0]})"
                )
            values.setdefault(name, (node.lineno, value))

    package = path.parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package
            for _ in range(node.level - 1):
                base = base.parent
            parts = (node.module or "").split(".") if node.module else []
            target = base.joinpath(*parts) if parts else base
            if not (target.with_suffix(".py").exists() or (target / "__init__.py").exists()):
                findings.append(
                    f"{rel}:{node.lineno}: relative import `{'.' * node.level}{node.module or ''}` "
                    "does not resolve to a module"
                )
    return findings


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else CONNECTORS
    findings = ruff_undefined(target)
    for path in sorted(target.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        findings.extend(module_findings(path))
    if findings:
        print("code health gate: FAILED")
        for f in findings:
            print("  " + f)
        return 1
    print(f"code health gate: {len(list(target.glob('*/server.py')))} modules clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
