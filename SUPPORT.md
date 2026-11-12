# Getting help

## Read first

- [README](README.md): what is here, the quickstart, credentials, containers.
- [docs/TOOLS.md](docs/TOOLS.md): every tool, generated from the code.
- [docs/configuration.md](docs/configuration.md): every environment variable.
- [docs/architecture.md](docs/architecture.md): how the framework handles a
  request, authentication modes, limits and read-only mode.
- [docs/writing-a-connector.md](docs/writing-a-connector.md) and
  [CONTRIBUTING.md](CONTRIBUTING.md): building and contributing a connector.

## Ask a question

Open an issue using the **Question** template:
<https://github.com/magurelabs/magoneai-mcp/issues/new/choose>.

Search existing issues first; someone may have asked already. Include the
connector, the tool, your `MCP_MODE`, and what you tried. **Never paste a
token, API key or private URL**; redact them.

## Report a bug or ask for a connector

Use the **Bug report** or **Connector or tool request** template on the same
page. A request for a connector is most likely to land if it meets the
[boundary rule](CONTRIBUTING.md#the-boundary-rule) and, better still, comes
with an offer to open the pull request.

## Report a security vulnerability

Not in an issue. Follow [SECURITY.md](SECURITY.md): use GitHub's private
vulnerability reporting, or email
[security@magureinc.com](mailto:security@magureinc.com) if you cannot.

## Problems with a vendor's API

If a vendor's API itself misbehaves (an outage, a changed endpoint, a quota),
the vendor's own support is the right place. If a connector handles that
badly, for example by hiding the vendor's error message, that is a bug here.

## What to expect

This project is maintained on a best-effort basis by the people in
[MAINTAINERS.md](MAINTAINERS.md). We aim to respond to new issues within five
working days. There is no paid support channel and no service-level
agreement.
