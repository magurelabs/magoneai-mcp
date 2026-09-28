"""Generate each publishable connector's `server.json`, and check it stays true.

`server.json` is the registry publication manifest. This writes one for every
connector marked `ready` in `connectors/publication.json`, and writes none for
the `blocked` ones - `scripts/publication_gate.py` fails if a blocked
connector ever acquires one, so the two scripts are the same decision seen
from both sides.

**Writing the file is not publishing.** Nothing here is pushed to the registry;
the manifest exists so that a publication, when someone decides to do one, is
a pipeline step rather than a scramble to invent metadata.

Everything is derived rather than typed. `name` and `version` come from the
connector's own `SERVER_NAME` and `SERVER_VERSION`, so the manifest cannot
describe a server other than the one that runs. Only the description is
curated, because the module docstring's first line is "The X connector." and a
registry listing needs a sentence a human reads.

## The constraints, taken from the schema rather than remembered

Fetched from `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`:

- required: `name`, `description`, `version`. **`description` is required**,
  which the prose reference describes as optional. The schema wins.
- `description` `maxLength: 100`
- `name` `pattern: ^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$`, `maxLength: 200`
- `version` `maxLength: 255`

Those are asserted here, so a description that drifts past 100 characters
fails locally instead of at a registry.

    python scripts/gen_server_json.py
    python scripts/gen_server_json.py --check
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"
PUBLICATION = CONNECTORS / "publication.json"

SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
NAMESPACE = "io.github.magurelabs"
REPOSITORY_URL = "https://github.com/magurelabs/magoneai-mcp"

NAME_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
DESCRIPTION_MAX = 100
NAME_MAX = 200
VERSION_MAX = 255

# One sentence per connector, under 100 characters. Vendor names are used
# nominatively, to say which API the connector talks to; that is the same
# licence README.md takes and no vendor endorses anything here.
DESCRIPTIONS: dict[str, str] = {
    "atlassian_confluence": (
        "Read and write Confluence pages, blog posts, comments, labels and attachments."
    ),
    "atlassian_jira": "Search, create and transition Jira issues, worklogs, links and attachments.",
    "cloudflare_r2": "Upload files to your own Cloudflare R2 bucket and get a shareable link.",
    "database": "Query and modify PostgreSQL, MySQL, SQL Server, Oracle or MongoDB databases.",
    "docusign": "Create, send, track and void Docusign envelopes, and read their documents.",
    "google_calendar": "List, create, update and delete Google Calendar events.",
    "google_gmail": "List, read and send Gmail messages.",
    "kapso_whatsapp": "Send WhatsApp text, media and template messages, and read conversations.",
    "linear": "Search, create and update Linear issues, projects, comments and labels.",
    "microsoft_sharepoint": (
        "Browse SharePoint sites, document libraries and files, and download them."
    ),
    "microsoft_teams": (
        "Read Teams chats and channel messages, and send messages or create meetings."
    ),
    "otp_verification": "Generate and verify one-time passcodes backed by your own database.",
    "outlook_calendar": (
        "List, create, update and delete Outlook calendar events, and read free/busy."
    ),
    "outlook_email": "Read, search, send and organise Outlook mail, including attachments.",
    "trino": "Run read-only SQL against Trino and browse its catalogs, schemas and tables.",
    "twitter_intel": "Search tweets, read threads and follow an account's recent activity.",
    "web_search": "Search the web and fetch a page's readable text.",
    "zoom": "Create, read and delete Zoom meetings.",
}


def declared(package: Path) -> tuple[str, str]:
    """`SERVER_NAME` and `SERVER_VERSION` as the connector itself declares them."""
    tree = ast.parse((package / "server.py").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("SERVER_NAME", "SERVER_VERSION"):
                value = getattr(node.value, "value", None)
                if isinstance(value, str):
                    found[target.id] = value
    missing = {"SERVER_NAME", "SERVER_VERSION"} - found.keys()
    if missing:
        raise SystemExit(f"{package.name}: cannot derive {sorted(missing)} from server.py")
    return found["SERVER_NAME"], found["SERVER_VERSION"]


def manifest_for(package: Path) -> dict:
    server_name, version = declared(package)
    description = DESCRIPTIONS.get(package.name)
    if not description:
        raise SystemExit(
            f"{package.name} is publishable but has no description here. "
            "A registry listing needs a sentence a human reads, and the module "
            'docstring says only "The X connector."'
        )

    name = f"{NAMESPACE}/{server_name}"
    problems = []
    if len(description) > DESCRIPTION_MAX:
        problems.append(f"description is {len(description)} chars, schema allows {DESCRIPTION_MAX}")
    if len(name) > NAME_MAX:
        problems.append(f"name is {len(name)} chars, schema allows {NAME_MAX}")
    if not NAME_PATTERN.match(name):
        problems.append(f"name {name!r} does not match the schema's pattern")
    if len(version) > VERSION_MAX:
        problems.append(f"version is {len(version)} chars, schema allows {VERSION_MAX}")
    if problems:
        raise SystemExit(f"{package.name}: " + "; ".join(problems))

    return {
        "$schema": SCHEMA_URL,
        "name": name,
        "description": description,
        "version": version,
        "websiteUrl": f"{REPOSITORY_URL}/tree/main/connectors/{package.name}",
        "repository": {
            "url": REPOSITORY_URL,
            "source": "github",
            "subfolder": f"connectors/{package.name}",
        },
    }


def publishable() -> list[str]:
    data = json.loads(PUBLICATION.read_text(encoding="utf-8"))
    return sorted(
        name for name, entry in data["connectors"].items() if entry.get("status") == "ready"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any manifest has drifted")
    args = parser.parse_args()

    ready = publishable()
    drifted: list[str] = []
    written = 0

    for name in ready:
        package = CONNECTORS / name
        target = package / "server.json"
        expected = json.dumps(manifest_for(package), indent=2) + "\n"

        if args.check:
            if not target.exists():
                drifted.append(f"{name}: no server.json, but it is marked ready")
            elif target.read_text(encoding="utf-8") != expected:
                drifted.append(f"{name}: server.json does not match the connector")
            continue

        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            target.write_text(expected, encoding="utf-8")
            written += 1

    if args.check:
        if drifted:
            print("server.json check FAILED\n")
            for problem in drifted:
                print(f"  {problem}")
            print("\nRun: python scripts/gen_server_json.py")
            return 1
        print(f"server.json: {len(ready)} publishable connectors, every manifest current")
        return 0

    print(f"server.json: {written} written or updated, {len(ready)} publishable connectors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
