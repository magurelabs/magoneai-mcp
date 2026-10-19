## What this changes

<!-- One or two sentences: what, and why. Link the issue if there is one ("Fixes #123"). -->

## How it was tested

<!-- The commands you ran and what they showed. The whole suite runs without credentials or network:
     pytest tests/ connectors/ -q
     Every gate is listed in CONTRIBUTING.md under "Running the tests and every gate". -->

## Checklist

<!-- Tick only what is true. Strike through (~~like this~~) anything that does not apply. -->

- [ ] Every commit is signed off (`git commit -s`) by its author, as the DCO check requires
- [ ] Commit messages follow the convention in CONTRIBUTING.md (`type(scope): summary`)
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest tests/ connectors/ -q` passes, and new behaviour has a test that needs no credentials or network
- [ ] Every gate in CONTRIBUTING.md passes locally
- [ ] New or changed tools take credentials per call through `credential_headers=`, carry both `readOnlyHint` and `destructiveHint`, declare a `permission_group`, and every schema property has a description
- [ ] If a tool's schema changed or tools were added, `python scripts/golden_schema_gate.py --update` was run and the baseline is in its own commit
- [ ] If tools were added, removed or re-described, `python scripts/gen_tool_reference.py` was run
- [ ] A new connector meets the boundary rule, has an entry in `connectors/publication.json` (new connectors start `blocked` with a reason), and is in the conformance scope in `ci.yml`
- [ ] Documentation and `CHANGELOG.md` (under `Unreleased`) are updated where behaviour changed
- [ ] No credential, token, private URL, internal hostname, customer name or personal data appears in code, tests, fixtures or this description
