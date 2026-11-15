"""Fail if a connector imports something nothing declares.

Installing the package into an empty virtualenv and starting each connector
once showed **10 of 23 came up**.
`web_search` failed on `trafilatura`, which appeared in no `pyproject.toml`,
no requirements file, and no documentation anywhere in this repository.

The pre-port catalogue had 27 per-server `requirements.txt` files. The port
carried the code across and left every one of them behind, so the dependency
information existed and was simply dropped.

This is the failure mode for the person this repository is being published
for: they install the package, run the connector they came for, and get a
`ModuleNotFoundError` naming a library they have never heard of.

Each connector needing a third-party library declares it as an extra, so it
installs by name from a checkout:

    pip install -e ".[web_search]"

The gate compares what each connector **imports** against what its extra
**declares**, and fails on anything imported and undeclared.

Static, like the others: it reads imports with `ast` and needs nothing
installed. That matters because the gate is about what an install would need,
and running it inside an environment that already has everything would be the
one place it could never notice.

    python scripts/dependency_gate.py
"""

import ast
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Provided by `magone-mcp-core` itself, or not a distribution at all.
ALWAYS_AVAILABLE = {
    "mcp",
    "mcp_types",
    "structlog",
    "httpx",
    "pydantic",
    "anyio",
    "uvicorn",
    "starlette",
    "magone_mcp_core",
    "connectors",
    "typing_extensions",
    "certifi",
    "pytest",
    "fastapi",
    "dotenv",
}

# Names left over from the pre-port layout. They are undefined references
# rather than missing packages, and `scripts/port_status.py` is what counts
# them; reporting them here as well would make two gates fail for one cause.
PORT_RESIDUE = {"base", "tools", "client", "shared"}

# Import name -> distribution name, where they differ.
DISTRIBUTION = {
    "docx": "python-docx",
    "bson": "pymongo",
    "sqlalchemy": "SQLAlchemy",
    "dateutil": "python-dateutil",
    "yaml": "PyYAML",
    "PIL": "pillow",
}


def imported_by(package: Path) -> set[str]:
    """Top-level modules a connector's source imports."""
    found: set[str] = set()
    for path in package.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found - set(sys.stdlib_module_names) - ALWAYS_AVAILABLE - PORT_RESIDUE


def declared() -> dict[str, set[str]]:
    """Distribution names each connector's extra declares, lowercased."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    out: dict[str, set[str]] = {}
    for name, requirements in extras.items():
        names = set()
        for requirement in requirements:
            head = requirement.split(";")[0].strip()
            for separator in ("[", ">", "<", "=", "!", "~", " "):
                head = head.split(separator)[0]
            names.add(head.strip().lower().replace("_", "-"))
        out[name] = names
    return out


def main() -> int:
    if not CONNECTORS.exists():
        print(f"no connectors directory at {CONNECTORS}; nothing to check")
        return 0

    extras = declared()
    problems: list[str] = []
    checked = 0

    for package in sorted(CONNECTORS.iterdir()):
        if not package.is_dir() or package.name.startswith((".", "_")):
            continue
        checked += 1
        available = extras.get(package.name, set())
        for module in sorted(imported_by(package)):
            distribution = DISTRIBUTION.get(module, module).lower().replace("_", "-")
            if distribution not in available:
                problems.append(
                    f"{package.name}: imports `{module}` but no extra declares "
                    f"`{distribution}`; `pip install magone-mcp-core[{package.name}]` "
                    "would not install it"
                )

    if not problems:
        print(f"dependency gate: {checked} connectors, every import declared")
        return 0

    print("dependency gate FAILED\n")
    for problem in problems:
        print(f"  {problem}")
    print(
        "\nAdd the distribution to that connector's extra in pyproject.toml. "
        "Someone installing this package gets a ModuleNotFoundError otherwise, "
        "naming a library they have never heard of."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
