"""Fail if a public repository would run a fork's pull request on a self-hosted runner.

`ci.yml` is `on: pull_request`, so a self-hosted runner label plus a public
repository means any stranger's branch executes on a machine somebody owns,
with whatever that machine can reach in scope. It is the one CI mistake that
cannot be undone quietly, and a comment asking someone to remember is not a
control.

So: while the repository is public, every `runs-on` must be GitHub-hosted.
The two facts are checked together, because neither is wrong on its own - a
self-hosted label is fine on a private repository, and a public repository is
correct and intended. Only the combination is a breach.

Visibility comes from the environment rather than the network, so the gate
needs no token and no API call:

    REPO_VISIBILITY=public python scripts/runner_label_gate.py

In CI that is `${{ github.event.repository.visibility }}`, which both the
`push` and `pull_request` payloads carry. `--require-visibility` makes an
absent value a failure, so the gate cannot pass by not knowing: a gate that
skips reports the same green as one that passed. Run without it locally and it
reports what it found.
"""

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# A GitHub-hosted label names a platform and an image: `ubuntu-latest`,
# `ubuntu-24.04`, `macos-15`, `windows-2022`. Anything else is somebody's own
# machine, including every label that merely looks safe (`self-hosted` is the
# honest one; a custom runner-group label is not).
HOSTED = re.compile(r"^(ubuntu|macos|windows)-[a-z0-9.]+$")
RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*(?:#.*)?$")


def labels_in(path: Path) -> list[tuple[int, str]]:
    """Every `runs-on` label in one workflow, with its line number.

    A matrix or a list value (`runs-on: [self-hosted, linux]`) is split so
    each element is judged on its own; an expression (`${{ ... }}`) is reported
    as unresolvable rather than assumed safe.
    """
    found: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        match = RUNS_ON.match(line)
        if not match:
            continue
        value = match.group(1).strip().strip("\"'")
        for piece in value.strip("[]").split(","):
            label = piece.strip().strip("\"'")
            if label:
                found.append((number, label))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-visibility",
        action="store_true",
        help="treat an absent REPO_VISIBILITY as a failure (use in CI)",
    )
    args = parser.parse_args()

    visibility = os.environ.get("REPO_VISIBILITY", "").strip().lower()

    self_hosted: list[str] = []
    unresolved: list[str] = []
    checked = 0
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        checked += 1
        for number, label in labels_in(path):
            where = f"{path.name}:{number}: runs-on: {label}"
            if "${{" in label:
                unresolved.append(where)
            elif not HOSTED.match(label):
                self_hosted.append(where)

    if not visibility:
        if args.require_visibility:
            print("runner label gate FAILED\n")
            print("  REPO_VISIBILITY is not set, so this gate cannot answer the only")
            print("  question it exists to answer. In a workflow, pass")
            print("  ${{ github.event.repository.visibility }}.")
            return 1
        print(
            f"runner label gate: {checked} workflow(s), "
            f"{len(self_hosted)} self-hosted label(s); visibility unknown, nothing judged"
        )
        for where in self_hosted + unresolved:
            print(f"  would refuse if public: {where}")
        return 0

    if visibility != "public":
        print(
            f"runner label gate: repository is {visibility}, "
            f"{len(self_hosted)} self-hosted label(s) across {checked} workflow(s)"
        )
        for where in self_hosted + unresolved:
            print(f"  allowed while not public: {where}")
        return 0

    if not self_hosted and not unresolved:
        print(
            f"runner label gate: repository is public and every runs-on across "
            f"{checked} workflow(s) is GitHub-hosted"
        )
        return 0

    print("runner label gate FAILED\n")
    for where in self_hosted:
        print(f"  {where}")
    for where in unresolved:
        print(f"  {where}  (an expression: resolve it or pin the label)")
    print(
        f"\nThis repository is public and {len(self_hosted) + len(unresolved)} job(s) do not "
        "name a GitHub-hosted runner.\nci.yml runs on pull_request, so a fork's branch would "
        "execute on a self-hosted\nmachine with whatever it can reach in scope. Set runs-on to "
        "ubuntu-latest."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
