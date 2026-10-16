# Contributing

A connector here wraps somebody else's API. If you use one and it is missing a
tool you need, adding it is the shortest path to having it.

## The bar

One question decides whether something belongs in this repository:

> Can an outside developer call this tool against their own account and their
> own storage?

If no, it belongs somewhere else. A tool that reads our object store, our
database or a tenant id from a header is not a connector, and `boundary_gate.py`
will say so on your pull request.

## Getting set up

```bash
git clone https://github.com/magurelabs/magoneai-mcp
cd magoneai-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest tests/ connectors/ -q
```

`[dev]` alone installs the framework's test tools without any connector's
third-party libraries; with it, run `pytest tests/ -q -m "not requires_connectors"`.

The whole suite runs **with no credentials and no network**. That is deliberate:
you should be able to run everything CI runs before you open a pull request,
and CI should be able to judge a fork the same way it judges a branch.

## Adding a tool

```python
from mcp.types import ToolAnnotations


@server.tool(
    name="list_widgets",
    description="List widgets in a workspace, newest first.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)
async def list_widgets(credentials: dict, workspace: str, limit: int = 20) -> dict:
    """Return widgets."""
    ...
```

The schema comes from your type hints. That is right for a new tool.

`annotations` is not optional in practice. `readOnlyHint` is what
`MCP_READ_ONLY=1` keys on, so a tool without it is treated as a write and
refused on read-only deployments. `tool_contract_lint.py` fails without it.

Every schema property needs a `description`. The description is what the model
reads to decide what to pass; a property without one is a guess.

## Changing an existing tool's schema

Don't, unless you mean to.

Schemas on ported tools are **declared** with `@declare_schema` rather than
derived, and `golden_schema_gate.py` fails on any diff. That is not
bureaucracy: an agent's saved configuration refers to a tool's parameters, so
changing them changes the behaviour of agents already running, with no error
anywhere.

If the change is intended:

```bash
python scripts/golden_schema_gate.py --update
```

and commit the baseline **on its own**, so a reviewer sees exactly which tools
moved and nothing else.

## Sign-off

We use the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. Sign your commits:

```bash
git commit -s -m "feat(linear): add list_widgets"
```

`-s` adds a `Signed-off-by` line, which says you have the right to submit the
work under this repository's licence. That is all it says. There is nothing to
print and nothing to post.

The DCO check on every pull request requires each commit to carry a
`Signed-off-by` line matching its author. If you forgot, `git rebase --signoff
main` adds it to every commit on your branch.

## What runs on your pull request

| Gate | What it means if it fails |
|---|---|
| Lint | `ruff check` / `ruff format --check`, pinned |
| Framework tests | something in `magone-mcp-core` broke |
| Golden schema | a tool's wire contract changed |
| Boundary | a connector reached into platform infrastructure |
| Tool contract | a missing annotation, a contradictory one, an undescribed property, or a logged credential |
| Conformance | a standard MCP client could not talk to the server |
| DCO | a commit has no `Signed-off-by` line matching its author |

All of it runs without secrets. A pull request from a fork gets the same
verdict as one from a branch.

## A note on logging

Tool arguments are logged by key, never by value, and the framework does that
for you. If you are tempted to log a credential or an upstream error body
verbatim: a vendor's 401 frequently echoes the token that failed. Use
`magone_mcp_core.redact.upstream_error`.

## Code of conduct

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
