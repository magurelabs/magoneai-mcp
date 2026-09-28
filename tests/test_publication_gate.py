"""Publishing cannot be undone, so the check that prevents it has to work.

Every other mistake in this programme is a rollback. Publishing a connector
that should not have gone out is a thing that exists on the internet
afterwards, whatever is done next.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "publication_gate.py"
MANIFEST = REPO_ROOT / "connectors" / "publication.json"
CONNECTORS = REPO_ROOT / "connectors"


def run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE)], capture_output=True, text=True, cwd=REPO_ROOT
    )


def declared() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["connectors"]


def test_the_repository_passes_as_it_stands():
    result = run()

    assert result.returncode == 0, result.stdout + result.stderr


def test_every_connector_package_is_classified():
    """An unclassified connector is one nobody decided about."""
    packages = {
        p.name
        for p in CONNECTORS.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")) and p.name != "__pycache__"
    }

    assert packages <= set(declared()), f"unclassified: {sorted(packages - set(declared()))}"


def test_every_blocked_connector_states_why():
    """A block with no reason gets removed by the next person who reads it."""
    for name, entry in declared().items():
        if entry["status"] == "blocked":
            assert entry.get("reason"), f"{name} is blocked with no reason"


def test_a_blocked_connector_carrying_a_publication_manifest_fails(tmp_path):
    """`server.json` is what a registry pipeline reads, so its presence on a
    blocked connector is the thing that would actually publish it."""
    target = CONNECTORS / "google_drive" / "server.json"
    assert declared()["google_drive"]["status"] == "blocked"
    assert not target.exists(), "already present; this test would prove nothing"

    try:
        target.write_text('{"name": "io.github.example/google_drive"}\n', encoding="utf-8")
        assert target.exists(), "tamper did not land"

        result = run()

        assert result.returncode == 1, "the gate passed a blocked connector set up to publish"
        assert "google_drive" in result.stdout
        # The reason is printed beside the failure, so whoever hits it learns
        # why the block exists without going to find the manifest.
        assert "Not yet verified against a live deployment" in result.stdout
    finally:
        target.unlink(missing_ok=True)

    assert run().returncode == 0


def test_a_new_unclassified_connector_fails():
    package = CONNECTORS / "zz_gate_probe"
    assert not package.exists(), "already present; this test would prove nothing"

    try:
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        assert package.is_dir(), "tamper did not land"

        result = run()

        assert result.returncode == 1, "an unclassified connector defaulted to publishable"
        assert "zz_gate_probe" in result.stdout
    finally:
        for child in package.glob("*"):
            child.unlink()
        package.rmdir()

    assert run().returncode == 0


def test_a_ready_connector_carries_a_publication_manifest():
    """The gate blocks the blocked ones. It must not obstruct the rest.

    This test used to create the manifest itself and assert the file was
    absent first, because when it was written no connector had one. Every
    publishable connector carries one now, so the precondition inverted: the
    thing to assert is that the gate is content with the real tree, not that
    it tolerates a temporary file.
    """
    target = CONNECTORS / "zoom" / "server.json"
    assert declared()["zoom"]["status"] == "ready"
    assert target.exists(), "a ready connector should carry a manifest; run gen_server_json.py"

    assert run().returncode == 0


def test_every_ready_connector_has_one_and_every_blocked_one_does_not():
    """The two halves of the same decision, checked against the whole tree
    rather than a single sampled connector."""
    statuses = declared()
    missing = []
    forbidden = []
    for name, entry in statuses.items():
        has_manifest = (CONNECTORS / name / "server.json").exists()
        if entry["status"] == "ready" and not has_manifest:
            missing.append(name)
        if entry["status"] != "ready" and has_manifest:
            forbidden.append(name)

    assert not missing, f"marked ready but carry no manifest: {missing}"
    assert not forbidden, f"blocked from publication but carry a manifest: {forbidden}"
