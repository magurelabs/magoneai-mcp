"""Fail if a tool's permission group stays a comment instead of a declaration.

The codemod recorded each tool's v1 permission group above its decorator, next
to the credential headers:

    # credential_headers: ['X-Atlassian-Access-Token', 'X-Atlassian-Cloud-Id']
    # permission_group: 'write_jira'  required_scopes: ['write:jira-work']
    @server.tool(name='addCommentToJiraIssue', ...)

A comment is not a declaration - the same lesson as `credential_headers_gate`,
and this is the second half of it. The group is what a connection's selection
is resolved against (`X-MCP-Enabled-Groups`). Left as a comment it narrows
nothing, and the failure is silent in the dangerous direction: every tool is
served to every connection, no call is refused, and no log line says so. All
263 tools in this repository were in exactly that state, while the platform
kept sending the header.

Blast radius when it regresses, measured on the v1 groups: `atlassian-jira` 38
tools over 2 groups, `atlassian-confluence` 24 over 5, `trino` 5. A connection
that selected read-only Jira gets `deleteJiraIssue`.

    python scripts/permission_group_gate.py
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMENT = re.compile(r"^\s*#\s*permission_group:\s*'([^']*)'\s+required_scopes:\s*(\[.*\])\s*$")


def declared_in_comment(lines: list[str], decorator_line: int) -> tuple[str, list] | None:
    """The group and scopes the provenance comment above a decorator records."""
    i = decorator_line - 2
    while i >= 0 and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        match = COMMENT.match(lines[i])
        if match:
            try:
                return match.group(1), ast.literal_eval(match.group(2))
            except (ValueError, SyntaxError):
                return None
        i -= 1
    return None


def literal(keyword: ast.keyword | None) -> object | None:
    if keyword is None:
        return None
    try:
        return ast.literal_eval(keyword.value)
    except (ValueError, SyntaxError):
        return None  # a non-literal is a deliberate choice, not a lost value


def problems_in(path: Path) -> tuple[list[str], int]:
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    out: list[str] = []
    tools = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "tool"):
                continue
            tools += 1
            name = next(
                (
                    kw.value.value
                    for kw in dec.keywords
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                ),
                node.name,
            )
            group_kw = next((kw for kw in dec.keywords if kw.arg == "permission_group"), None)
            scopes_kw = next((kw for kw in dec.keywords if kw.arg == "required_scopes"), None)

            # Every tool declares a group, whether or not it carries a v1
            # comment. Defaulting silently is what made the whole catalogue
            # one undivided surface.
            if group_kw is None:
                out.append(
                    f"{path.name}:{dec.lineno}: `{name}` declares no permission_group, "
                    "so no group selection can narrow it away"
                )
                continue

            expected = declared_in_comment(lines, dec.lineno)
            if expected is None:
                continue  # added in the port: nothing to compare against
            group, scopes = expected
            actual_group = literal(group_kw)
            if actual_group is not None and actual_group != group:
                out.append(
                    f"{path.name}:{dec.lineno}: `{name}` declares permission_group "
                    f"{actual_group!r} but was ported with {group!r}"
                )
            actual_scopes = literal(scopes_kw) or []
            if scopes and sorted(actual_scopes) != sorted(scopes):
                out.append(
                    f"{path.name}:{dec.lineno}: `{name}` declares required_scopes "
                    f"{actual_scopes} but was ported with {scopes}"
                )
    return out, tools


def main() -> int:
    # Further directories of connectors may be named on the command line, so a
    # downstream repository can run this gate over its own.
    roots = [REPO_ROOT / "connectors", *(Path(arg) for arg in sys.argv[1:])]
    problems: list[str] = []
    checked = tools = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/server.py")):
            checked += 1
            found, counted = problems_in(path)
            problems.extend(found)
            tools += counted

    if not problems:
        print(
            f"permission group gate: {tools} tool(s) across {checked} server module(s), "
            "every group declared"
        )
        return 0

    print("permission group gate FAILED\n")
    for problem in problems:
        print(f"  {problem}")
    print(
        f"\n{len(problems)} tool(s) cannot be narrowed by a connection's selection. "
        "A group that is only a comment serves every tool to every connection."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
