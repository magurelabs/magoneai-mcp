"""The compile gate has to be able to fail, and for the right reason.

A gate that cannot fail is worse than no gate: it turns "we check for this"
into a sentence everybody believes and nobody has tested. This gate was written
after a defect that three other gates were structurally blind to, so proving it
fails on that exact defect is the point of it existing.

The blind spot, stated once: `ast.parse` accepts a misplaced `from __future__`
import; only `compile()` rejects it. These tests assert both halves, so a
future refactor that quietly swaps `compile()` back to `ast.parse` fails here
rather than in a container at start-up.
"""

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "compile_gate.py"

MISPLACED = '''"""A module docstring."""

import os

from __future__ import annotations

VALUE = os.getenv("X", "")
'''

WELL_FORMED = '''"""A module docstring."""

from __future__ import annotations

import os

VALUE = os.getenv("X", "")
'''


def test_ast_parse_accepts_what_the_compiler_rejects():
    """The premise of the whole gate. If this ever fails, the gate is redundant."""
    ast.parse(MISPLACED)  # must not raise

    try:
        compile(MISPLACED, "<test>", "exec")
    except SyntaxError as exc:
        assert "__future__" in (exc.msg or "")
    else:  # pragma: no cover - would mean the premise no longer holds
        raise AssertionError("compile() accepted a misplaced __future__ import")


def test_well_formed_module_compiles():
    ast.parse(WELL_FORMED)
    compile(WELL_FORMED, "<test>", "exec")


def test_gate_passes_on_the_repository_as_it_stands():
    result = subprocess.run(
        [sys.executable, str(GATE)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "modules compile" in result.stdout


def test_gate_fails_on_a_misplaced_future_import(tmp_path, monkeypatch):
    """Inject the defect into a real connector, run the gate, restore.

    Asserting the tamper landed before running the gate is deliberate: a
    negative result from a verification step means nothing unless the step is
    known to have done something. An earlier version of this check reported
    "no change" only because its replacement string never matched.
    """
    target = REPO_ROOT / "connectors" / "zoom" / "server.py"
    original = target.read_text(encoding="utf-8")

    anchor = "from magone_mcp_core import"
    assert anchor in original, "tamper anchor missing; this test would prove nothing"

    tampered = original.replace(anchor, "from __future__ import annotations\n" + anchor, 1)
    assert tampered != original, "tamper did not land"

    try:
        target.write_text(tampered, encoding="utf-8")
        # It must be genuinely misplaced, not accidentally valid at line 1.
        line = next(
            i
            for i, text in enumerate(target.read_text(encoding="utf-8").split("\n"), 1)
            if text.startswith("from __future__")
        )
        assert line > 1, "injected import landed at the top; nothing is being tested"

        result = subprocess.run(
            [sys.executable, str(GATE)], capture_output=True, text=True, cwd=REPO_ROOT
        )
        assert result.returncode == 1, "the gate passed on a module that cannot be imported"
        assert "connectors/zoom/server.py" in result.stdout
        assert "__future__" in result.stdout
    finally:
        target.write_text(original, encoding="utf-8")

    # And clean again afterwards, so a failure here cannot leave the tree dirty
    # for every test that runs after it.
    result = subprocess.run(
        [sys.executable, str(GATE)], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stdout + result.stderr
