"""Measure how far the connector port is from importable, per connector.

The port carries every tool's schema across byte for byte, which is the part
that would silently change live agent behaviour if it were wrong. What it does
not yet do is resolve every name a ported handler body refers to.

That distinction matters and is easy to blur, so this gate states it plainly:

- **schema fidelity is done and gated** by `golden_schema_gate.py`
- **import resolution is in progress** and is counted here

A connector with unresolved names parses, passes the golden gate, and raises
`NameError` the first time the affected code path runs. Counting it is the only
honest way to hold it: a repository that simply excluded `connectors/` from
linting would look finished and would not be.

The budget only ever comes down. Raising it needs a reason in the commit
message, because the only legitimate reason is a connector newly added.

    python scripts/port_status.py            # report
    python scripts/port_status.py --check    # fail if any connector exceeds budget
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUDGET = REPO_ROOT / "tests" / "golden" / "port-budget.json"
CONNECTORS = REPO_ROOT / "connectors"

RUFF = ["ruff", "check", "--select", "F821", "--output-format", "concise", "--no-cache"]


def unresolved_by_connector(root: Path) -> dict:
    """Count F821 (undefined name) per connector package."""
    try:
        # RUFF is a fixed literal and `root` is a path this script computed,
        # never caller input. No shell is involved.
        proc = subprocess.run(  # noqa: S603
            [*RUFF, str(root)], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        print("ruff is not installed; run `pip install -e '.[dev]'`", file=sys.stderr)
        raise SystemExit(2) from None

    counts: dict = {}
    for line in proc.stdout.split("\n"):
        match = re.match(r"^(?:.*/)?connectors/([A-Za-z0-9_]+)/", line)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    if not CONNECTORS.exists():
        print("no connectors to measure")
        return 0

    counts = unresolved_by_connector(CONNECTORS)
    total = sum(counts.values())
    # `not startswith("__")` keeps `__pycache__` out of the inventory. Without it
    # the gate reported 24 connectors against 23, and a `__pycache__` directory is
    # trivially "fully resolved", so the budget this gate defends read one larger
    # than it is.
    clean = sorted(
        p.name
        for p in CONNECTORS.iterdir()
        if p.is_dir() and not p.name.startswith("__") and p.name not in counts
    )

    if args.update:
        BUDGET.parent.mkdir(parents=True, exist_ok=True)
        BUDGET.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"port budget recorded: {total} unresolved names across {len(counts)} connectors")
        return 0

    print(f"connectors fully resolved : {len(clean)}")
    for name in clean:
        print(f"    {name}")
    if counts:
        print(f"\nconnectors with unresolved names ({total} total):")
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {name:26} {count}")

    if not args.check:
        return 0

    if not BUDGET.exists():
        print("\nno budget recorded; run --update once")
        return 1

    budget = json.loads(BUDGET.read_text(encoding="utf-8"))
    regressions = {
        name: (budget.get(name, 0), count)
        for name, count in counts.items()
        if count > budget.get(name, 0)
    }
    if regressions:
        print("\nport status FAILED: unresolved names increased\n")
        for name, (was, now) in sorted(regressions.items()):
            print(f"    {name}: {was} -> {now}")
        print(
            "\nThe budget only comes down. If a connector was newly added, run "
            "--update and say so in the commit message."
        )
        return 1

    improved = {n: (budget[n], counts.get(n, 0)) for n in budget if counts.get(n, 0) < budget[n]}
    if improved:
        print("\nimproved since the recorded budget:")
        for name, (was, now) in sorted(improved.items()):
            print(f"    {name}: {was} -> {now}")
        print("run --update to lock the improvement in")
    print("\nport status: within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
