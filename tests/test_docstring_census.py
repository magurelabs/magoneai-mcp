"""Rule 5 of the tool-contract linter, proved by injecting the drift it exists for.

Every connector opens by stating how many tools it defines and how many are
read-only. Ten of 23 were wrong, from two unrelated causes, and every gate in
the repository was green while they were.

The second cause is the one worth a test. Five docstrings went stale because a
tool moved out of the connector, which is an ordinary refactor oversight.
The other eight went stale because the annotation audit *corrected* nine wrong
`readOnlyHint` values: a change that was right in itself made a sentence
elsewhere false. No reviewer reading that diff would see it, because the diff
was correct.

So the test does not merely check that the rule fires. It reproduces both
causes separately, because a rule that catches one and not the other would
have let eight of the ten through.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINT = REPO_ROOT / "scripts" / "tool_contract_lint.py"

# The repository currently carries this many undescribed schema properties;
# the census rule must be judged independently of that allowance.
ALLOWANCE = "192"


def run_lint() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT), "--max-undescribed", ALLOWANCE],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_every_connector_docstring_matches_its_code_today():
    result = run_lint()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docstring-census" not in result.stdout


def test_a_stale_tool_count_fails(tmp_path):
    """Cause one: a tool moved out and the total was not updated."""
    _assert_tamper_fails(
        connector="cloudflare_r2",
        stale="3 tools, 0 of them proposed read-only.",
        current="2 tools, 0 of them proposed read-only.",
        expected="docstring says 3 tools",
    )


def test_a_stale_read_only_count_fails():
    """Cause two: an annotation was corrected and the census was not.

    This is the case a human reviewer misses, so it is the case that most
    needs a machine to catch it.
    """
    _assert_tamper_fails(
        connector="web_search",
        stale="5 tools, 1 of them proposed read-only.",
        current="5 tools, 5 of them proposed read-only.",
        expected="the module defines 5 tools, 5 read-only",
    )


def _assert_tamper_fails(connector: str, stale: str, current: str, expected: str) -> None:
    """Put the pre-fix docstring back, confirm the linter rejects it, restore.

    Asserting the tamper landed before running the linter is deliberate. A
    negative result from a verification step means nothing unless the step is
    known to have done something, and this programme has produced several
    "identical" measurements that only compared two empty files.
    """
    target = REPO_ROOT / "connectors" / connector / "server.py"
    original = target.read_text(encoding="utf-8")
    assert current in original, f"anchor missing in {connector}; this test would prove nothing"

    tampered = original.replace(current, stale, 1)
    assert tampered != original, "tamper did not land"

    try:
        target.write_text(tampered, encoding="utf-8")
        result = run_lint()
        assert result.returncode == 1, "the linter passed on a docstring that contradicts its code"
        assert "docstring-census" in result.stdout
        assert f"{connector}/server.py" in result.stdout
        assert expected in result.stdout
    finally:
        target.write_text(original, encoding="utf-8")

    # Clean again afterwards, so a failure above cannot leave the tree dirty
    # for every test that runs after it.
    assert run_lint().returncode == 0


def test_a_module_with_no_census_sentence_is_not_invented():
    """The rule reads a sentence that exists; it does not require one.

    Framework modules and support modules carry no census, and a rule that
    demanded one would fail on every file it was never meant to judge.
    """
    result = subprocess.run(
        [sys.executable, str(LINT), "--root", str(REPO_ROOT / "src"), "--max-undescribed", "0"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
