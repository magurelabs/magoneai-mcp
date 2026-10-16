## What this changes

<!-- One or two sentences: what, and why. Link the issue if there is one. -->

## How it was tested

<!-- The commands you ran and what they showed. The whole suite runs without credentials or network:
     pytest tests/ connectors/ -q -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`), as the DCO check requires
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest tests/ connectors/ -q` passes
- [ ] New or changed tools carry `readOnlyHint` and `destructiveHint`, and every schema property has a description
- [ ] If a tool's schema changed on purpose, `python scripts/golden_schema_gate.py --update` was run and the baseline is in its own commit
- [ ] If tools were added, removed or re-described, `python scripts/gen_tool_reference.py` was run
- [ ] No credential, token, private URL or personal data appears in code, tests or this description
