"""Fail if a connector that must not be published is set up to be.

Publishing is the one action in this programme that cannot be undone. Every
other mistake here is a rollback; this one is a thing that exists on the
internet afterwards.

Two connectors are blocked for a reason that is not about code quality, and
it is easy to forget at exactly the wrong moment:

- a connector that has never been verified against a live deployment, such
  as `google_drive` or `surveymonkey`

`server.json` is the publication manifest: it is what a registry pipeline
reads and it is the file whose presence means "this is meant to go out". So
the check is simply that a blocked connector does not have one.

Every connector must also have an entry, which makes adding a connector a
decision somebody makes rather than a default somebody inherits. A new package
with no entry fails here rather than silently defaulting to publishable.

    python scripts/publication_gate.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = REPO_ROOT / "connectors"
MANIFEST = CONNECTORS / "publication.json"

PUBLICATION_MANIFEST = "server.json"
VALID = {"ready", "blocked"}


def packages() -> list[str]:
    return sorted(
        path.name
        for path in CONNECTORS.iterdir()
        if path.is_dir() and not path.name.startswith((".", "_")) and path.name != "__pycache__"
    )


def blocked_name_leaks(declared: dict) -> list[str]:
    """A publishable connector must not name a connector that is blocked.

    The names it compares against are the blocked connectors' own directory
    names, which are already in this repository, so referring to them HERE
    leaks nothing new - which is the only reason this check can live in the
    public repository at all.

    A list of the customers themselves must never appear in this file. Writing
    "scrub these customer names" into a public repository publishes the very
    names it is protecting, and would be a worse leak than the one it prevents.
    The full list lives outside this repository and is run by hand before any
    publication; this gate catches the case that a routine change could
    introduce, which is a publishable connector growing a reference to a
    blocked one.
    """
    blocked = sorted(name for name, entry in declared.items() if entry.get("status") != "ready")
    ready = [name for name, entry in declared.items() if entry.get("status") == "ready"]
    if not blocked or not ready:
        return []

    problems: list[str] = []
    for package in sorted(ready):
        directory = CONNECTORS / package
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".md", ".json", ".txt"):
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for other in blocked:
                if other in text:
                    problems.append(
                        f"{path.relative_to(CONNECTORS)}: publishable `{package}` "
                        f"references `{other}`, which is blocked from publication"
                    )
                    break
    return problems


def main() -> int:
    if not CONNECTORS.exists():
        print(f"no connectors directory at {CONNECTORS}; nothing to check")
        return 0
    if not MANIFEST.exists():
        print(f"missing {MANIFEST.relative_to(REPO_ROOT)}")
        return 1

    declared = json.loads(MANIFEST.read_text(encoding="utf-8"))["connectors"]
    found = packages()
    problems: list[str] = blocked_name_leaks(declared)

    for name in found:
        entry = declared.get(name)
        if entry is None:
            problems.append(
                f"{name}: no entry in connectors/publication.json. Every connector "
                "needs a publication status, so that adding one is a decision "
                "rather than a default."
            )
            continue
        status = entry.get("status")
        if status not in VALID:
            problems.append(f"{name}: status {status!r} is not one of {sorted(VALID)}")
            continue
        if status == "blocked":
            if not entry.get("reason"):
                problems.append(f"{name}: blocked with no reason recorded")
            manifest = CONNECTORS / name / PUBLICATION_MANIFEST
            if manifest.exists():
                problems.append(
                    f"{name}: blocked from publication but carries "
                    f"{PUBLICATION_MANIFEST}.\n        reason: {entry.get('reason', '')}"
                )

    for name in sorted(set(declared) - set(found)):
        problems.append(f"{name}: declared in publication.json but no such connector package")

    ready = sum(1 for n in found if declared.get(n, {}).get("status") == "ready")
    blocked = len(found) - ready

    if not problems:
        print(f"publication gate: {ready} ready, {blocked} blocked, {len(found)} classified")
        return 0

    print("publication gate FAILED\n")
    for problem in problems:
        print(f"  {problem}")
    print(
        "\nPublishing is the one step here that cannot be undone. A connector is "
        "either `ready` or `blocked` with a stated reason, and a blocked one "
        f"must not carry {PUBLICATION_MANIFEST}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
