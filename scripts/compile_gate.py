"""Fail if any module in the repository parses but cannot be compiled.

This gate exists because of a specific blind spot, and the blind spot is worth
stating plainly rather than fixing quietly.

Every other gate here reads source with `ast.parse`. That is a deliberate
choice: a gate that needs the whole catalogue importable - with credentials,
network and every vendor dependency installed - is a gate that gets skipped.
`ast.parse` needs none of that.

But `ast.parse` is not the compiler. It accepts source the interpreter refuses.
The case that found this: `from __future__ import annotations` must precede
every statement but the docstring. The codemod carried it across into the
middle of the generated module, where it parses cleanly and fails at import.
Three modules were in that state, among them `connectors/database` and
`connectors/outlook_calendar`, while the
golden-schema gate, the tool-contract linter and the port-status budget were
all green. Two of them were also carrying a nonzero unresolved-name count,
which made them look merely incomplete rather than completely unimportable.

`compile()` catches it, executes nothing, and needs no more than `ast.parse`
did. There is no reason not to use it, which is the whole finding.

    python scripts/compile_gate.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOTS = ("src", "connectors", "scripts", "tests")


def failures(root: Path) -> list[tuple[Path, int, str]]:
    out: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            out.append((path, exc.lineno or 0, exc.msg or "syntax error"))
    return out


def main() -> int:
    bad: list[tuple[Path, int, str]] = []
    checked = 0
    for name in ROOTS:
        root = REPO_ROOT / name
        if not root.exists():
            continue
        checked += sum(1 for p in root.rglob("*.py") if "__pycache__" not in p.parts)
        bad.extend(failures(root))

    if not bad:
        print(f"compile gate: {checked} modules compile")
        return 0

    print("compile gate FAILED: these modules parse but cannot be imported.\n")
    for path, line, msg in bad:
        print(f"  {path.relative_to(REPO_ROOT)}:{line}: {msg}")
    print(
        "\nA module in this state fails at container start, not at call time, "
        "so nothing downstream reports it as a tool problem. If this is a "
        "misplaced `from __future__` import, it must come before every "
        "statement except the module docstring."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
