# Contributing

A connector here wraps somebody else's API. If you use one and it is missing a
tool you need, adding it is the shortest path to having it. Bug reports,
documentation fixes and reviews are just as welcome as code.

This guide covers everything a pull request needs. If something here is wrong
or unclear, that is a bug: open an issue or a pull request against this file.

- [The boundary rule](#the-boundary-rule)
- [Getting set up](#getting-set-up)
- [Running the tests and every gate](#running-the-tests-and-every-gate)
- [Adding a tool](#adding-a-tool)
- [Adding a connector, step by step](#adding-a-connector-step-by-step)
- [Changing an existing tool's schema](#changing-an-existing-tools-schema)
- [Commit messages](#commit-messages)
- [Sign-off (DCO)](#sign-off-dco)
- [Pull requests and review](#pull-requests-and-review)
- [Labels](#labels)
- [A note on logging](#a-note-on-logging)
- [Code of conduct](#code-of-conduct)

Related reading: [docs/architecture.md](docs/architecture.md) for how the
framework works, [docs/writing-a-connector.md](docs/writing-a-connector.md)
for a worked example, and [docs/configuration.md](docs/configuration.md) for
every environment variable.

## The boundary rule

One question decides whether something belongs in this repository:

> Can an outside developer use this connector against **their own account**
> and **their own storage**?

If yes, it belongs here. If no, it belongs somewhere else, however useful it
is.

In practice that means a connector here:

- talks to a third-party API with credentials the **caller** supplies on each
  request, never with a key baked into the server for everyone;
- keeps any state it needs in a place the caller owns and names (their
  database, their bucket), never in storage that belongs to whoever hosts the
  server;
- works the same on a laptop as inside any platform that runs it.

And it means none of these belong here:

- a tool coupled to a specific hosted platform's object store, database,
  internal API or webhook relay;
- a tool that reads a tenant or project id from a request header and trusts
  it;
- a tool built for one customer's systems, data or naming, rather than for
  anyone who holds an account with the vendor.

`scripts/boundary_gate.py` refuses the reach-throughs it can recognise, and it
will say so on your pull request. It is deliberately a blunt grep. Passing it
does not mean a connector meets the rule; reviewers apply the rule itself.

Keep code, tests and fixtures vendor-neutral and customer-neutral as well:
vendor names appear only to say which API a connector talks to (see
[NOTICE](NOTICE)), and no customer, private hostname or internal ticket
reference appears anywhere.

## Getting set up

Requires Python 3.11 or later and git. Node.js 22 is needed only to run the
MCP conformance suite locally.

```bash
git clone https://github.com/magurelabs/magoneai-mcp
cd magoneai-mcp
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest tests/ connectors/ -q
```

`[dev]` alone installs the framework's test tools without any connector's
third-party libraries; with it, run `pytest tests/ -q -m "not requires_connectors"`.
`[all]` adds every connector's extra so the whole suite can run.

The whole suite runs **with no credentials and no network**. That is
deliberate: you should be able to run everything CI runs before you open a
pull request, and CI should be able to judge a fork the same way it judges a
branch.

To try a connector against your own account:

```bash
pip install -e ".[web_search]"
MCP_MODE=local python -m connectors.web_search
```

It serves MCP over Streamable HTTP at `http://127.0.0.1:8000/mcp`. Point any
MCP client at that URL, for example the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## Running the tests and every gate

Every check CI runs is a script you can run locally the same way. The
authoritative list, with the reason each one exists, is
[`.github/workflows/ci.yml`](.github/workflows/ci.yml); this is the same list
in the same order:

```bash
# Static gates: no credentials, no network, most need nothing installed.
python scripts/dependency_gate.py
python scripts/golden_schema_gate.py --check
python scripts/compile_gate.py
python scripts/gen_server_json.py --check
python scripts/gen_tool_reference.py --check
python scripts/publication_gate.py
python scripts/credential_headers_gate.py
python scripts/permission_group_gate.py
REPO_VISIBILITY=public python scripts/runner_label_gate.py --require-visibility
python scripts/boundary_gate.py
python scripts/tool_contract_lint.py --max-undescribed 152   # the allowance CI uses
ruff check .
ruff format --check .
python scripts/code_health_gate.py
python scripts/port_status.py --check

# Tests.
pytest tests/ -q -m "not requires_connectors"   # framework, needs only [dev]
pytest tests/ -q -m "requires_connectors"       # framework tests that import every connector
pytest connectors/ -q                           # each connector's own suite
```

| Gate | What it means if it fails |
|---|---|
| Dependencies | a connector imports a library its extra in `pyproject.toml` does not declare |
| Golden schema | a tool's wire contract changed (see [below](#changing-an-existing-tools-schema)) |
| Compile | a module parses but would fail at import |
| Server manifests | a `server.json` no longer matches its connector's `SERVER_NAME` / `SERVER_VERSION` |
| Tool reference | `docs/TOOLS.md` is stale; run `python scripts/gen_tool_reference.py` |
| Publication | a connector has no entry in `connectors/publication.json`, or a blocked one carries a `server.json` |
| Credential headers | a tool's credential headers stayed a comment instead of `credential_headers=` |
| Permission groups | a tool's permission group stayed a comment instead of `permission_group=` |
| Runner labels | a workflow would run on a self-hosted runner in a public repository |
| Boundary | a connector reached into platform infrastructure |
| Tool contract | a missing or contradictory annotation, an undescribed declared property, a logged credential, a stale docstring census, or a URL fetched without the SSRF check |
| Lint / Format | `ruff check` / `ruff format --check`, pinned to the version in `pyproject.toml`. Ruff skips `connectors/` (`extend-exclude` in `pyproject.toml`); Code health and Port status check that code instead |
| Code health | an undefined name, a function defined twice, a constant reassigned, an unresolvable relative import |
| Port status | a ported handler refers to a name that does not resolve |
| Framework / Connector tests | a test failed |
| Conformance | a standard MCP client could not talk to the server |
| DCO | a commit has no `Signed-off-by` line matching its author |

### Conformance, locally

CI runs the official MCP conformance suite against every connector a change
can affect. To run it for one connector:

```bash
MCP_MODE=local PORT=8111 python -m connectors.linear &
npx -y @modelcontextprotocol/conformance@0.2.0-alpha.11 server \
  --url http://127.0.0.1:8111/mcp --requirements 2026-07-28 \
  --expected-failures tests/conformance/expected-failures.yaml
```

Use the version `ci.yml` pins. The expected-failures file lists the scenarios
a tools-only connector does not satisfy (prompts, resources and so on); any
other failure is a real one.

## Adding a tool

```python
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field


@server.tool(
    name="list_widgets",
    description="List widgets in a workspace, newest first.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=["X-Example-Api-Key"],
    permission_group="read_widgets",
)
async def list_widgets(
    credentials: dict,
    workspace: Annotated[str, Field(description="Workspace slug, for example 'acme'.")],
    limit: Annotated[int, Field(description="How many to return, 1 to 100.")] = 20,
) -> dict:
    """Return widgets."""
    ...
```

The schema comes from your type hints. That is right for a new tool. The
rules every tool follows:

- **Credentials arrive per call, as headers.** List every header the tool may
  receive in `credential_headers=`. The framework builds the `credentials`
  dict from those headers only (`X-Example-Api-Key` becomes
  `credentials["example_api_key"]`) and hides the parameter from the
  advertised schema. A header you did not declare never reaches the tool.
  Never read a credential from a module-level constant meant for all callers.
- **Annotations are mandatory.** Declare both `readOnlyHint` and
  `destructiveHint`. `readOnlyHint` is what `MCP_READ_ONLY=1` keys on, so a
  tool without it is treated as a write and refused on read-only deployments.
  `readOnlyHint=True` with `destructiveHint=True` is a contradiction and fails
  the linter.
- **Every property is described.** Use `Annotated[..., Field(description=...)]`
  for a type-hinted tool, or a `description` on every property of a declared
  schema. The description is what the model reads to decide what to pass; a
  property without one is a guess.
- **Declare a permission group** with `permission_group=` (and
  `required_scopes=` when the vendor uses scopes). A host that enforces
  selection can then offer a connection one group, for example only the reads.
  A tool that names no group falls in `default`.
- **Fetch a caller-supplied URL only through `validate_url`** from
  `magone_mcp_core`. The linter fails a tool that takes a `url`-like parameter
  and never calls it.
- **Report a refusal as a refusal.** Return a dict with a non-empty `error`
  string, or raise `ValueError`, `TypeError` or `KeyError` with a message
  written for the model. Both reach the caller with `isError` set. Any other
  exception is masked as a generic failure.

## Adding a connector, step by step

[docs/writing-a-connector.md](docs/writing-a-connector.md) walks through a
complete minimal connector with its test. The checklist:

1. **Check the boundary rule** [above](#the-boundary-rule). If you are unsure,
   open a connector request issue first and ask.
2. **Create the package** `connectors/<name>/`, using underscores (it is
   imported as `connectors.<name>`):
   - `__init__.py` re-exporting `server`;
   - `__main__.py` that calls `server.run(...)` with `MCP_HOST` and `PORT`
     (copy an existing one);
   - `server.py` defining `SERVER_NAME`, `SERVER_VERSION` and
     `server = ConnectorServer(...)`, then the tools;
   - `tests/__init__.py` and at least one test module.
3. **Declare third-party dependencies** as an extra named after the connector
   in `pyproject.toml`, and add it to the `all` extra. Add the extra to the
   `pip install` line of the Conformance job in `ci.yml` too.
   `dependency_gate.py` fails on anything imported and undeclared.
4. **Write the tools** following [Adding a tool](#adding-a-tool): per-call
   credentials through `credential_headers=`, both annotations, described
   properties, a permission group.
5. **Write tests that need no credentials and no network.** Stand the vendor
   in with `httpx.MockTransport` or a fake client, as the existing connectors
   do. Test the behaviour an agent depends on: the request that is sent, the
   vendor's error text surviving, a missing credential naming the header to
   send.
6. **Record the publication status** in `connectors/publication.json`. A new
   connector starts as `blocked` with a `reason`, normally that it has not
   been verified end to end against a live account. It becomes `ready` in a
   later pull request, once someone has run it end to end; at that point add a
   one-sentence description to `DESCRIPTIONS` in `scripts/gen_server_json.py`
   and run `python scripts/gen_server_json.py` to write its `server.json`.
   A blocked connector must not carry one.
7. **Add the connector to the conformance scope**: the `ALL` list in the
   `scope` job of `ci.yml`.
8. **Approve the new tools in the golden baseline**:
   `python scripts/golden_schema_gate.py --update`, committed on its own.
9. **Regenerate the tool reference**: `python scripts/gen_tool_reference.py`.
10. **Run every gate and the tests** as listed
    [above](#running-the-tests-and-every-gate), then open the pull request.

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
moved and nothing else. Say in the pull request why the contract had to
change, and record it under `Changed` in [CHANGELOG.md](CHANGELOG.md).

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <summary in the imperative, lower case, no full stop>

<optional body: what changed and why, wrapped at 72 columns>

Signed-off-by: Your Name <you@example.com>
```

- `type` is one of `feat`, `fix`, `docs`, `test`, `refactor`, `perf`,
  `build`, `ci`, `chore`.
- `scope` is a connector directory (`linear`, `outlook_email`), `core` for
  `magone-mcp-core`, or a tool area (`gates`, `ci`). Leave it out when the
  change is repository-wide.
- A change that breaks a tool's wire contract or a public framework API says
  so with `!` after the scope and a `BREAKING CHANGE:` footer.

Examples:

```text
feat(linear): add list_widgets
fix(core): refuse a write tool listed before MCP_READ_ONLY was set
docs: explain the boundary rule in CONTRIBUTING
test(golden): approve the new linear tools
```

Keep each commit about one thing. Several small commits are easier to review
than one large one, and a golden-baseline update is always its own commit.

## Sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. Every commit carries a `Signed-off-by` line, which says you have
the right to submit the work under this repository's licence. That is all it
says. There is nothing to print and nothing to post.

Sign a commit with `-s`:

```bash
git commit -s -m "feat(linear): add list_widgets"
```

which adds a trailer using your configured `user.name` and `user.email`:

```text
Signed-off-by: Your Name <you@example.com>
```

The DCO check on every pull request requires each commit to carry a
`Signed-off-by` line that **exactly matches its author**. If the check fails:

```bash
# Forgot -s on the last commit:
git commit --amend --no-edit -s

# Forgot it on several commits in your branch:
git rebase --signoff main

# Signed with a different name or email than the author:
git config user.name "Your Name"
git config user.email "you@example.com"
git rebase --exec 'git commit --amend --no-edit --reset-author -s' main
```

then `git push --force-with-lease` to update the pull request.

To sign off automatically, add `-s` to an alias (`git config alias.cs "commit -s"`)
or use a `prepare-commit-msg` hook.

## Pull requests and review

1. **Open an issue first for anything large**: a new connector, a framework
   change, a schema change. A short conversation saves a rewrite.
2. **Branch from `main`** in your fork. Keep a pull request to one change.
3. **Fill in the template.** Say what changed and why, and paste the commands
   you ran. Tick only the checklist items that are true.
4. **CI must be green.** All of it runs without secrets, so a pull request
   from a fork gets the same verdict as one from a branch. If a check fails
   for a reason that has nothing to do with your change, say so in the pull
   request rather than working around it.
5. **Review.** At least one maintainer approval is required. Paths listed in
   [CODEOWNERS](CODEOWNERS) (the framework, the gates, the workflows, the
   golden baseline, licensing and security policy) always need a maintainer
   from that file. We aim to give a first response within five working days.
   If a pull request has had no response in that time, a polite ping on it is
   welcome.
6. **Address feedback with new commits** while the review is open, so the
   reviewer can see what changed; they may ask you to tidy history before
   merge. Resolve a conversation only when the reviewer's point is addressed.
7. **Merge.** A maintainer merges once CI is green and the review is
   approved, normally with a squash merge whose message follows the commit
   convention above.

What reviewers look at, roughly in this order: does it meet the boundary
rule; does it change any existing tool's contract; are the annotations true
(a wrong `readOnlyHint` is worse than a missing one); are credentials taken
per call and never logged; do the tests exercise the behaviour an agent
depends on; is the documentation still true.

A pull request that has had no activity from its author for 30 days after a
review may be closed. It can always be reopened.

### After a merge

Magure builds its own MagOneAI images from this repository in a private
downstream build. When a commit lands on `main` or `prod`
(both protected, so a commit reaches them only through a reviewed merge),
[`notify-platform.yml`](.github/workflows/notify-platform.yml) sends that
private build the commit SHA and branch name, and nothing else. The private
build checks for itself that the commit is on one of those branches before it
builds anything.

You do not need to do anything about it. It never runs on a pull request, it
holds the only credential involved in a protected environment, and in a fork
the credential does not exist, so the job reports that and passes. The
workflow file explains each of those safeguards.

## Labels

| Label | Used for |
|---|---|
| `bug` | something does not do what it says (set by the bug template) |
| `connector-request` | a new connector or tool (set by the request template) |
| `question` | a usage question (set by the question template) |
| `dependencies` | an automated dependency update |
| `good first issue` | small, well-scoped, a good place to start |
| `help wanted` | maintainers would welcome a pull request |

## A note on logging

Tool arguments are logged by key, never by value, and the framework does that
for you. If you are tempted to log a credential or an upstream error body
verbatim: a vendor's 401 frequently echoes the token that failed. Use
`magone_mcp_core.redact.upstream_error`.

## Code of conduct

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). How the project is run is described in
[GOVERNANCE.md](GOVERNANCE.md).
