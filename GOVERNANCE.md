# Governance

This document describes how the magoneai-mcp project is run: who decides
what, how, and how that set of people grows. It is deliberately small, to
match a young project, and is expected to change as the contributor base
does. Changes to it follow the process for significant decisions below.

## Roles

### Users

Anyone who runs a connector or builds on `magone-mcp-core`. Users are the
reason the project exists, and their bug reports and requests shape what it
works on.

### Contributors

Anyone who contributes anything: an issue, a review, documentation, a pull
request, help for another user. There is no sign-up. Contributions are made
under the [Developer Certificate of Origin](https://developercertificate.org/)
as described in [CONTRIBUTING.md](CONTRIBUTING.md).

### Maintainers

Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md). They:

- review and merge pull requests, and are the required reviewers for the
  paths in [CODEOWNERS](CODEOWNERS);
- triage issues and keep labels and milestones meaningful;
- decide whether a connector meets the
  [boundary rule](CONTRIBUTING.md#the-boundary-rule) and whether it is
  `ready` or `blocked` for publication;
- cut releases, following [RELEASING.md](RELEASING.md);
- handle security reports privately, following [SECURITY.md](SECURITY.md);
- enforce the [Code of Conduct](CODE_OF_CONDUCT.md).

A maintainer is expected to act in the interest of the project and its users
rather than of any employer or customer, and to declare a conflict of
interest when one exists.

## Decision making

Most decisions are made in the open, on the issue or pull request they
concern, by **lazy consensus**: a proposal that has been visible for a
reasonable time and has drawn no sustained objection from a maintainer is
accepted.

- **Routine changes** (a bug fix, a new tool, documentation) need the review
  described in [CONTRIBUTING.md](CONTRIBUTING.md): CI green and at least one
  maintainer approval.
- **Significant changes** need an issue opened first, labelled for
  discussion, and left open for at least five working days before work is
  merged. Significant means any of:
  - a breaking change to a tool's wire contract or to a public
    `magone-mcp-core` API;
  - a new connector, or moving a connector to `ready` for publication;
  - weakening or removing a CI gate, or changing what a gate enforces;
  - a change to this document, to the licence, or to the security policy;
  - adding or removing a maintainer.
- **When consensus fails**, maintainers vote on the issue. A change passes
  with a simple majority of maintainers who vote, with at least half of all
  maintainers voting. While the project has fewer than three maintainers,
  decisions that cannot reach consensus are made by the maintainers listed in
  MAINTAINERS.md, recorded on the issue with the reasoning.

Security fixes are the exception to working in the open: they are prepared
privately under [SECURITY.md](SECURITY.md) and disclosed when a fix is
available.

## Becoming a maintainer

Maintainership is earned by sustained, high-quality participation, not by
volume or by employer. A contributor is a good candidate when they have, over
several months:

- landed a number of non-trivial pull requests, and reviewed other people's;
- shown good judgment about the boundary rule, tool contracts and security;
- helped users in issues;
- shown that they work well with others, in line with the Code of Conduct.

Any maintainer may nominate a candidate, with the candidate's agreement, by
opening a pull request that adds them to MAINTAINERS.md. The nomination is a
significant change and is accepted by lazy consensus of the existing
maintainers after five working days, or by vote if anyone objects. New
maintainers get write access and are added to CODEOWNERS.

## Stepping down and removal

A maintainer may step down at any time by opening a pull request that moves
them to the emeritus list in MAINTAINERS.md. A maintainer who has been
inactive for six months may be moved to emeritus by the others, after asking
them. Emeritus maintainers are thanked, keep their place in the history, and
can return through a lighter version of the nomination process.

A maintainer may be removed for a serious or repeated breach of the Code of
Conduct or of this document, by a vote of the other maintainers.

## Stewardship

The project is stewarded by Magure Tech Middle East Ltd, which holds the
copyright notice in [NOTICE](NOTICE) and administers the GitHub organisation.
Stewardship does not override the decision process above for the project's
technical direction.
