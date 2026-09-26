"""Fail if a tool declares credential headers in a comment but not in code.

The codemod records each tool's provenance above its decorator, including the
`credential_headers` list v1 used to decide which request headers that tool is
allowed to receive:

    # credential_headers: ['X-Zoom-Account-Id', 'X-Zoom-Client-Id']
    @server.tool(name='get_meeting', ...)

A comment is not a declaration. If the list stays a comment, the framework has
nothing to build the handler's `credentials` dict from, the tool receives an
empty one, and it fails against its vendor with "missing credentials" - a
failure that looks like a misconfigured connection rather than a port defect.

This gate exists because the repair script got it wrong once, silently. Its
idempotency check was per file rather than per decorator, so a connector whose
carried-across code merely *mentions* `credential_headers=` anywhere was
skipped entirely. Both Atlassian connectors matched on their first line and
all **66** of their tools were left without credentials, while the script
reported success.

That is the second time in this programme a guard has made a tool do nothing
without saying so. The answer to a silent skip is a gate, not more care.

    python scripts/credential_headers_gate.py
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMENT = re.compile(r"^\s*#\s*credential_headers:\s*(\[.*\])\s*$")


def declared_in_comment(lines: list[str], decorator_line: int) -> list | None:
    """The provenance comment above a decorator, if it names any headers."""
    i = decorator_line - 2
    while i >= 0 and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        match = COMMENT.match(lines[i])
        if match:
            try:
                return ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                return None
        i -= 1
    return None


def problems_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    out: list[str] = []
    # A tool with no v1 provenance comment (one added in the port) is not
    # covered by the comparison below. `update_event` was added that way,
    # declared nothing, and received an empty credentials dict on every call.
    tools_without: list[tuple[int, str]] = []
    tools_with = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "tool"):
                continue
            name = next(
                (
                    kw.value.value
                    for kw in dec.keywords
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                ),
                node.name,
            )
            declared = next((kw for kw in dec.keywords if kw.arg == "credential_headers"), None)
            expected = declared_in_comment(lines, dec.lineno)
            takes_credentials = any(a.arg == "credentials" for a in node.args.args)
            if expected is None and declared is None and takes_credentials:
                tools_without.append((dec.lineno, name))
            elif declared is not None:
                tools_with += 1
            if not expected:
                continue
            if declared is None:
                out.append(
                    f"{path.name}:{dec.lineno}: `{name}` needs {expected} but declares "
                    "no credential_headers; it will receive an empty credentials dict"
                )
                continue
            try:
                actual = ast.literal_eval(declared.value)
            except (ValueError, SyntaxError):
                continue  # a non-literal is a deliberate choice, not a lost list
            # `X-Project-Id` is platform context, not a vendor credential. v1
            # injected it into every tool so no v1 list names it; a v2 tool
            # that needs the project now declares it, which is not a drift.
            actual = [h for h in actual if str(h).lower() != "x-project-id"]
            if sorted(actual) != sorted(expected):
                out.append(
                    f"{path.name}:{dec.lineno}: `{name}` declares {actual} but was "
                    f"ported with {expected}"
                )
    if tools_with:
        for lineno, name in tools_without:
            out.append(
                f"{path.name}:{lineno}: `{name}` takes credentials but declares no "
                "credential_headers while its siblings do; it will receive an empty "
                "credentials dict"
            )
    return out


def main() -> int:
    # Further directories of connectors may be named on the command line, so a
    # downstream repository can run this gate over its own.
    roots = [REPO_ROOT / "connectors", *(Path(arg) for arg in sys.argv[1:])]
    problems: list[str] = []
    checked = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/server.py")):
            checked += 1
            problems.extend(problems_in(path))

    if not problems:
        print(f"credential headers gate: {checked} server module(s), every ported list declared")
        return 0

    print("credential headers gate FAILED\n")
    for problem in problems:
        print(f"  {problem}")
    print(
        f"\n{len(problems)} tool(s) would call their vendor with no credentials. "
        "Declare each list with `credential_headers=` on the decorator."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
