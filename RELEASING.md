# Releasing

How versions are numbered and how a release would be published. **Nothing has
been published yet**: the publishing workflow exists and runs as a dry run,
and arming it is a deliberate step described below.

Releases are cut by a maintainer (see [MAINTAINERS.md](MAINTAINERS.md)).

## What gets released

| Artifact | Where | Version source |
|---|---|---|
| `magone-mcp-core` wheel and sdist (the framework only; connectors run from a checkout) | PyPI | `version` in `pyproject.toml`, and `__version__` in `src/magone_mcp_core/__init__.py` |
| One container image per publishable connector, `docker.io/magoneai/mcp-<SERVER_NAME>:<SERVER_VERSION>` | Docker Hub | the connector's `SERVER_VERSION`, the image tag its `server.json` names |
| One MCP Registry entry per publishable connector (`connectors/<name>/server.json`) | MCP Registry | the connector's `SERVER_VERSION`, copied into `server.json` by `scripts/gen_server_json.py` |

A connector is publishable only when `connectors/publication.json` marks it
`ready`; a `blocked` connector has no `server.json` and is never built for
publication.

Connector images go to Docker Hub under the `magoneai` namespace, one
repository per connector named after its `SERVER_NAME` (for example
`magoneai/mcp-web-search`), tagged with its `SERVER_VERSION`. The tag is not
typed anywhere: `scripts/gen_server_json.py` writes it into the connector's
`server.json`, and the publish workflow reads it from there.

## Versioning

Everything follows [Semantic Versioning 2.0.0](https://semver.org/).

**`magone-mcp-core`** is versioned as a library:

- **major**: a breaking change to a public API (anything exported from
  `magone_mcp_core`), to a default that changes behaviour (for example a
  limit or a mode), or to the minimum supported Python or SDK major version;
- **minor**: new, backwards-compatible features;
- **patch**: backwards-compatible fixes.

While the version is `0.y.z`, a breaking change bumps the minor version and
is called out in the changelog.

**Each connector** is versioned independently through its `SERVER_VERSION`,
and its version describes its **tool contract**:

- **major**: a tool removed or renamed, a required parameter added, a
  parameter removed or retyped, or a result shape changed incompatibly. These
  are the changes `golden_schema_gate.py` makes visible;
- **minor**: a tool or an optional parameter added;
- **patch**: a fix that changes no schema.

## Tags

- `vX.Y.Z` tags a `magone-mcp-core` release.
- `<connector>/vX.Y.Z` (for example `linear/v2.1.0`) tags a connector
  release.

Tags are created on `main`, on a commit whose CI is green, and are annotated
(and signed where the maintainer has a signing key).

## Cutting a release

1. Open a pull request that:
   - bumps the version (`pyproject.toml` and `__version__` for the core, or
     `SERVER_VERSION` for a connector);
   - runs `python scripts/gen_server_json.py` if a connector version changed,
     so its `server.json` matches;
   - moves the `Unreleased` entries in [CHANGELOG.md](CHANGELOG.md) under a
     new version heading with the date, and updates the comparison links.
2. Merge it once CI is green and it is approved.
3. Tag the merge commit and push the tag.
4. Run the publish workflow (below) and create a GitHub release from the tag,
   with the changelog section as its notes.

## The publish workflow

[`.github/workflows/publish.yml`](.github/workflows/publish.yml) has two
halves:

- **Dry-run jobs** run on pull requests that touch packaging and on manual
  dispatch. They build the distribution and run `twine check`, build every
  publishable connector's image and verify its label matches its
  `server.json`, and validate every manifest against the published MCP
  server schema. They hold no secret and send nothing anywhere.
- **Publish jobs** run only on manual dispatch (`workflow_dispatch`), only
  after their dry run passed, and only inside the `release` environment,
  which is protected by required reviewers. A branch name is something a
  contributor can choose; an environment approval is not.

Every push step in the publish jobs is commented out today. Arming one takes
two deliberate actions by someone with that authority, and no fewer:

1. uncomment the push step in the publish job being armed, in a reviewed pull
   request;
2. add the credential that step names to the `release` environment's
   secrets, never to repository secrets.

Preferred over long-lived tokens where the registry supports it: PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) through
GitHub OIDC, which needs no stored secret at all.

## After a release

- Check the published artifacts install and start: `pip install
  magone-mcp-core==X.Y.Z`, `docker run` the image with `MCP_MODE=local`.
- Add a fresh, empty `Unreleased` section to the changelog if the release
  pull request did not.

## Yanking

A PyPI version cannot be reused once uploaded, even after it is deleted. If a
release is broken, yank it on PyPI (which keeps pinned installs working) and
release a new patch version. Registry entries and pulled images likewise
cannot be recalled, which is why publishing is gated the way it is.
