"""Enforce the tool contract that reviewers were previously expected to remember.

Six rules, each of which was a silent gap in v1:

1. **Annotations are mandatory.** v1 emitted no `annotations` key at all, so no
   client could tell a read from a delete. `MCP_READ_ONLY` trusts
   `readOnlyHint`, which makes a missing annotation a security gap and not a
   documentation one.
2. **`readOnlyHint` and `destructiveHint` cannot both be true.** A tool that
   claims both is telling a client two incompatible things, and whichever one
   the client believes, the other is a lie.
3. **Every schema property carries a description.** Measured on v1: 196 of
   1,038 properties had none. The description is what the model reads to decide
   what to pass, so an undescribed property is a guess.
4. **No credential value is logged.** A connector that logs `credentials` or an
   argument value puts a token in a log aggregator, and a vendor's 401 body
   frequently echoes the token that failed.
5. **A connector's module docstring matches its own code.** Ten of 23 stated a
   tool count or a read-only count that their own code contradicted, and eight
   of those were made wrong by a change that was itself correct.
6. **A tool taking a URL fetches it through the shared SSRF check.**
   `web_search.scrape_url` took a URL from the model and fetched it with
   redirects followed and no check at all, which reached cloud instance
   metadata and the platform's own API.

Static, so it runs with nothing installed and no network.
"""

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Finding:
    path: str
    line: int
    tool: str
    rule: str
    detail: str


def _call_decorator(node: ast.AST, attr: str = "", name: str = "") -> Any:
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        if attr and isinstance(fn, ast.Attribute) and fn.attr == attr:
            return dec
        if name and isinstance(fn, ast.Name) and fn.id == name:
            return dec
    return None


def check_file(path: Path, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [Finding(str(path), exc.lineno or 0, "-", "parse", str(exc))]

    rel = str(path.relative_to(root))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        registration = _call_decorator(node, attr="tool")
        if registration is None:
            continue

        name = "<unnamed>"
        annotations: dict[str, Any] = {}
        has_annotations = False
        for kw in registration.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = kw.value.value
            if kw.arg == "annotations":
                has_annotations = not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is None
                )
                if isinstance(kw.value, ast.Call):
                    for akw in kw.value.keywords:
                        try:
                            annotations[akw.arg] = ast.literal_eval(akw.value)
                        except (ValueError, SyntaxError):
                            annotations[akw.arg] = None

        # Rule 1
        if not has_annotations:
            findings.append(
                Finding(
                    rel,
                    node.lineno,
                    name,
                    "annotations-required",
                    "no annotations; MCP_READ_ONLY treats an unannotated tool as a write, "
                    "and no client can tell a read from a delete",
                )
            )
        else:
            for hint in ("readOnlyHint", "destructiveHint"):
                if hint not in annotations:
                    findings.append(
                        Finding(
                            rel,
                            node.lineno,
                            name,
                            "annotations-incomplete",
                            f"{hint} is not declared; the specification defaults "
                            "destructiveHint to true when annotations exist and it is omitted",
                        )
                    )
            # Rule 2
            if annotations.get("readOnlyHint") and annotations.get("destructiveHint"):
                findings.append(
                    Finding(
                        rel,
                        node.lineno,
                        name,
                        "annotations-contradictory",
                        "readOnlyHint and destructiveHint are both true",
                    )
                )

        # Rule 3
        declaration = _call_decorator(node, name="declare_schema")
        if declaration is not None and declaration.args:
            try:
                schema = ast.literal_eval(declaration.args[0])
            except (ValueError, SyntaxError):
                schema = None
            if isinstance(schema, dict):
                for prop, spec in (schema.get("properties") or {}).items():
                    described = isinstance(spec, dict) and str(spec.get("description", "")).strip()
                    if not described:
                        findings.append(
                            Finding(
                                rel,
                                node.lineno,
                                name,
                                "property-undescribed",
                                f"property {prop!r} has no description; the description is "
                                "what the model reads to decide what to pass",
                            )
                        )

        # Rule 4
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            logger_call = isinstance(fn, ast.Attribute) and fn.attr in {
                "debug",
                "info",
                "warning",
                "error",
                "exception",
                "critical",
            }
            if not logger_call:
                continue
            for arg in list(inner.args) + [kw.value for kw in inner.keywords]:
                try:
                    rendered = ast.unparse(arg)
                except (ValueError, RecursionError, AttributeError):
                    # An argument this linter cannot render is one it cannot
                    # judge. Skipping is correct; the alternative is a false
                    # positive on every expression ast.unparse chokes on.
                    continue
                if rendered in ("credentials", "creds") or rendered.startswith(
                    ("credentials[", "creds[", "credentials.get(", "creds.get(")
                ):
                    findings.append(
                        Finding(
                            rel,
                            inner.lineno,
                            name,
                            "credential-logged",
                            f"logs {rendered}; credential values must never reach a log line",
                        )
                    )

    findings.extend(_docstring_census(path, rel, tree))
    findings.extend(_url_param_guard(path, rel, tree))
    return findings


CENSUS = re.compile(r"(\d+) tools?, (\d+) of them proposed read-only\.")


def _docstring_census(path: Path, rel: str, tree: ast.Module) -> list[Finding]:
    """Rule 5: a connector's module docstring must match its own code.

    Each connector opens by stating how many tools it has and how many are
    read-only. It is the first thing a reader of the published package sees,
    and nothing checked it.

    Both halves had drifted, for two unrelated reasons:

    - Five connectors had a tool moved out of this repository and kept the
      old total, so `cloudflare_r2` promised 3 tools and defined 2.
    - Eight had their read-only count invalidated by the annotation audit that
      corrected nine wrong `readOnlyHint` values. `web_search` claimed 1
      read-only tool and had 4.

    The second cause is the instructive one: the docstring was made wrong by a
    change that was itself correct. That is precisely the drift a human
    reviewer cannot be relied on to catch, because the diff under review looks
    right. Ten of 23 modules were lying and every gate was green.
    """
    if path.name != "server.py":
        return []
    doc = ast.get_docstring(tree)
    if not doc:
        return []
    match = CENSUS.search(doc)
    if not match:
        return []

    tools = 0
    read_only = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        registration = _call_decorator(node, attr="tool")
        if registration is None:
            continue
        tools += 1
        for kw in registration.keywords:
            if kw.arg != "annotations" or not isinstance(kw.value, ast.Call):
                continue
            for flag in kw.value.keywords:
                if flag.arg == "readOnlyHint" and getattr(flag.value, "value", None) is True:
                    read_only += 1

    claimed = (int(match.group(1)), int(match.group(2)))
    if claimed == (tools, read_only):
        return []
    return [
        Finding(
            rel,
            1,
            "-",
            "docstring-census",
            f"docstring says {claimed[0]} tools, {claimed[1]} read-only; "
            f"the module defines {tools} tools, {read_only} read-only",
        )
    ]


URL_PARAMS = {"url", "uri", "link", "endpoint", "source_url", "webhook_url", "callback_url"}

# Tools that take a URL and hand it to a vendor API rather than fetching it.
# The request is made by the vendor from their own network, so our SSRF
# boundary is not involved and `validate_url` would reject legitimate targets.
# Each entry is a deliberate exemption, which is why it is a list of exact
# tool names and not a pattern.
PASSTHROUGH_URL_TOOLS = {
    # WhatsApp media: the link goes into the Meta Graph payload and Meta
    # fetches it. Verified at connectors/kapso_whatsapp/server.py:350.
    ("kapso_whatsapp", "send_media_message"),
}


def _url_param_guard(path: Path, rel: str, tree: ast.Module) -> list[Finding]:
    """Rule 6: a tool that takes a URL must fetch it through the shared check.

    `net.py` opens by saying it is the single SSRF implementation, because v1
    had three that disagreed. Measured against the connectors, that sentence
    was aspirational: `validate_url` had zero call sites outside its own
    package, and `web_search.scrape_url` - a model-supplied URL fetched with
    `follow_redirects=True` - had no check of any kind. Cloud instance metadata
    and the platform's own API were both reachable, and the body came back to
    the model as page text.

    One tool was affected, which is the point: the gap was invisible precisely
    because it was small. A grep for "does this connector fetch a URL" is the
    kind of thing a reviewer does once and never again, so it is a gate.

    A connector that only passes a URL to a vendor is exempt by name, above.
    """
    if path.name != "server.py":
        return []
    connector = path.parent.name
    source = path.read_text(encoding="utf-8")
    guarded = "validate_url" in source
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        schema = _call_decorator(node, attr="", name="declare_schema")
        if schema is None or not schema.args:
            continue
        try:
            declared = ast.literal_eval(schema.args[0])
        except (ValueError, SyntaxError):
            continue
        properties = (declared or {}).get("properties") or {}
        taken = sorted(p for p in properties if p.lower() in URL_PARAMS)
        if not taken:
            continue
        if (connector, node.name) in PASSTHROUGH_URL_TOOLS:
            continue
        if guarded:
            continue
        findings.append(
            Finding(
                rel,
                node.lineno,
                node.name,
                "url-param-unguarded",
                f"takes {taken} from the model but the connector never calls "
                "validate_url; either guard the fetch or add the tool to "
                "PASSTHROUGH_URL_TOOLS with the line that proves it does not fetch",
            )
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT / "connectors"))
    parser.add_argument(
        "--max-undescribed",
        type=int,
        default=0,
        help="temporary allowance while a ported catalogue is being described",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"nothing to lint at {root}")
        return 0

    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        findings.extend(check_file(path, root))

    undescribed = [f for f in findings if f.rule == "property-undescribed"]
    blocking = [f for f in findings if f.rule != "property-undescribed"]

    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1

    if findings:
        print("tool contract lint:\n")
        for f in blocking:
            print(f"  {f.path}:{f.line}  {f.tool}  [{f.rule}]")
            print(f"    {f.detail}")
        if undescribed:
            print(
                f"\n  {len(undescribed)} undescribed schema properties "
                f"(allowance: {args.max_undescribed})"
            )
            for f in undescribed[:10]:
                print(f"    {f.path}:{f.line}  {f.tool}  {f.detail}")
            if len(undescribed) > 10:
                print(f"    ... and {len(undescribed) - 10} more")
        print(f"\nsummary: {by_rule}")

    if blocking:
        return 1
    if len(undescribed) > args.max_undescribed:
        print(
            f"\nFAILED: {len(undescribed)} undescribed properties exceeds the "
            f"allowance of {args.max_undescribed}. Lower the allowance as they "
            "are described; it exists so a ported catalogue can be improved "
            "incrementally, not so the rule can be ignored."
        )
        return 1
    if not findings:
        print("tool contract lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
