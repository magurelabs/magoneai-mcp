"""Fail if a connector reaches back into platform infrastructure.

The boundary this enforces, in one question: **can an outside developer call
this tool against their own account and their own storage?** If a connector
opens the platform's Postgres, writes to the platform's object store, or reads
a tenant id out of a header, the answer is no and it does not belong in the
public repository.

Reaching into a platform's database or storage is often the shortest path
to a working feature. No single reach-through looks wrong when written;
together they make a connector layer that is not a connector layer.

A grep is a blunt instrument and this one is deliberately blunt. It is not
trying to understand the code; it is making a reach-through impossible to add
without someone noticing.
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (pattern, why it is not allowed here). The reason is printed on failure,
# because "boundary_gate failed" teaches nobody anything.
#
# Every pattern here names something that can only be *our* infrastructure. A
# rule broad enough to also catch legitimate connector behaviour is worse than
# no rule, because a gate with false positives is a gate somebody switches off.
# Three earlier rules were withdrawn for exactly that reason and are recorded
# at the bottom so nobody reinstates them.
FORBIDDEN: list[tuple[str, str]] = [
    (r"\bMINIO_[A-Z_]+\b", "object-store credentials belong to the platform, not a connector"),
    (r"\bDATABASE_URL\b", "DATABASE_URL is the platform's own database"),
    (r"\bMAGONE_API_URL\b", "a connector does not call the platform back"),
    (r"\bMCP_SERVICE_TOKEN\b", "a connector must not read the server's own service token"),
    (r"\bfrom\s+minio\b|\bimport\s+minio\b", "storage access goes through a content handle"),
    (r"X-Project-Id", "tenancy comes from a verified token, never from a header"),
    (r"MCP_WEBHOOK_RELAY", "a webhook receiver belongs in the platform, not a connector"),
    # Serving a webhook route, as opposed to calling a vendor's webhook API.
    (
        r"custom_route\(\s*[\"'][^\"']*/webhooks?/|@app\.(post|get)\(\s*[\"'][^\"']*/webhooks?/",
        "a webhook receiver belongs in the platform, not a connector",
    ),
    (
        r"custom_route\(\s*[\"']/credentials|@app\.(post|get)\(\s*[\"']/credentials",
        "credentials arrive per request and are never written down",
    ),
    (r"_internal/prune", "a connector never deletes platform data"),
]

# Withdrawn rules, and why. Each one fired on legitimate connector code.
#
# - `import asyncpg` / `import psycopg`: the `database` and `otp-verification`
#   connectors drive a **caller-supplied** DSN, which is their entire purpose.
#   A database driver is not evidence of a platform reach-through; the
#   platform's own connection string is, and DATABASE_URL still catches that.
# - `import boto3`: a connector may legitimately talk to a customer's own S3.
#   MINIO_* catches our object store specifically.
# - bare `/webhooks/`: SurveyMonkey's REST API has a `/webhooks/` path, so the
#   pattern fired on an ordinary outbound call to a vendor. Narrowed to
#   *serving* a webhook route.

# Directories where a match is documentation or a test fixture rather than a
# reach-through. Kept short: every entry here is a place the gate is blind.
#
# `server.json` is the generated registry manifest. It declares the variables
# the container image is started with, MCP_SERVICE_TOKEN among them, because a
# platform-mode server needs one; that is the server's own configuration, not
# a connector reading it. Its whole content comes from gen_server_json.py, and
# `--check` there fails on any hand edit, so the gate loses nothing it could see.
EXEMPT_PARTS = {"tests", "docs", "__pycache__", ".git", "scripts", "server.json"}

SCANNED_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".sh", ".env", ".example"}


def scan(root: Path) -> list[tuple[Path, int, str, str]]:
    findings: list[tuple[Path, int, str, str]] = []
    compiled = [(re.compile(p), why) for p, why in FORBIDDEN]
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in EXEMPT_PARTS for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            # An explicit waiver has to name itself, so it shows up in review.
            if "boundary-gate: allow" in line:
                continue
            for pattern, why in compiled:
                if pattern.search(line):
                    findings.append((path.relative_to(root), number, line.strip()[:100], why))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT / "connectors"))
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"nothing to scan at {root}")
        return 0

    findings = scan(root)
    if not findings:
        print("boundary gate: no platform reach-through found")
        return 0

    print(f"boundary gate FAILED: {len(findings)} reach-through(s) into platform infrastructure\n")
    for path, number, line, why in findings:
        print(f"  {path}:{number}")
        print(f"    {line}")
        print(f"    why this is not allowed here: {why}\n")
    print(
        "A public connector must work for someone with their own credentials and "
        "their own storage. If this tool genuinely needs platform infrastructure, "
        "it belongs in the platform's own repository, not here."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
