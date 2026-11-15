# Security policy

Every connector in this repository holds a credential for somebody's
third-party account. If you have found a way to misuse one, we would rather
hear it from you than read it somewhere else.

## Supported versions

Nothing has been released yet. Until the first release, security fixes land
on `main` only. After it, fixes go to the latest release of `magone-mcp-core`
and of each published connector; older versions are not patched, so please
upgrade. Versioning is described in [RELEASING.md](RELEASING.md).

## Reporting a vulnerability

**Please do not open a public issue.**

Use [GitHub's private vulnerability reporting](https://github.com/magurelabs/magoneai-mcp/security/advisories/new)
on this repository (the **Security** tab, then **Report a vulnerability**). It
creates a private thread with the maintainers and gives you a draft advisory
you can review before anything is published.

If you cannot use it, email [security@magureinc.com](mailto:security@magureinc.com).
That address reaches every maintainer in [MAINTAINERS.md](MAINTAINERS.md), and
the same response times below apply to it.

These are the only two channels we monitor for security reports.

Include whatever you have. A rough reproduction is more useful than a polished
report that arrives a week later.

## What to expect

| | |
|---|---|
| Acknowledgement | within 5 working days |
| Initial assessment | within 15 working days |
| Fix or a dated plan | within 60 days for high severity |
| Public disclosure | coordinated with you, by default 90 days after the report |

The project is maintained by a small team, listed in
[MAINTAINERS.md](MAINTAINERS.md). A report reaches all of them, so it does not
wait on any one person.

If we are going to miss one of these, we will tell you before the date rather
than after it.

We will credit you in the advisory unless you would rather we did not. We do
not run a bug bounty, and we will not ask you to sign anything before we will
read your report.

## Scope

In scope:

- `magone-mcp-core`, the framework
- any connector under `connectors/`
- the CI workflows in this repository, including anything that would let a
  fork's pull request reach a secret or publish a package

Out of scope, because they are not vulnerabilities in this repository:

- a vulnerability in a third-party API a connector talks to; report that to the
  vendor
- a vulnerability in the MCP specification or the official SDK; report those
  upstream
- prompt injection through content a connector returns. A connector returns
  text written by someone outside your trust boundary, and a model that reads
  it may act on it. This is a real risk and it is not one a server can fix; see
  the README. Reports of the general problem are not actionable here. A report
  that a specific connector returns third-party text **without marking it**,
  or that `MCP_READ_ONLY=1` fails to refuse a write tool, is in scope and we
  want it.

## Things worth knowing before you test

- `local` mode is unauthenticated by design and is held to loopback for that
  reason. Finding that an unauthenticated server is unauthenticated is not a
  finding; finding that it bound a public interface without
  `MCP_ALLOW_INSECURE_BIND=1` is.
- SSRF protection lives in one place, `magone_mcp_core.net`. If you find an
  address encoding that reaches internal infrastructure through it, that is
  exactly the report we want. IPv4-mapped IPv6, 6to4, Teredo and NAT64 are
  handled; we are interested in what is not.
