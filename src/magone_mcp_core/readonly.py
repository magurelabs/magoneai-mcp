"""A hard read-only mode, and the per-tool classification it derives from.

Every connector in this catalogue returns text written by someone outside the
trust boundary: a Jira description, a Confluence page, an email body, an
arbitrary web page. That text reaches a model that also holds write tools. An
attacker who can leave a comment can therefore attempt to drive the agent.

No authorization model prevents this, because the caller is legitimately
authorized for both the read and the write. It is a confused deputy in the
agent, not a broken lock on the server. A server cannot solve prompt injection
and should not claim to.

What a server can do is make a deployment that only needs reads incapable of
writing, so it cannot be talked into one. ``MCP_READ_ONLY=1`` filters every
non-read tool out of ``tools/list`` and refuses it on ``tools/call``, and takes
precedence over every other configuration.

Filtering the listing is a convenience. **The call check is the control**: a
client that cached the tool list before the flag was set, or that guessed a
name, still cannot write.
"""

import os
from collections.abc import Iterable

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


class ReadOnlyViolation(ToolError):
    """A write tool was called on a read-only deployment.

    A `ToolError`, so the caller reads the reason. As a plain exception the
    SDK reported it as a bare "Error executing tool X" and logged a full ERROR
    traceback for every refused call, which reads as a crash and trips error
    alerting for a control doing exactly its job.
    """


def read_only_enabled(env: dict | None = None) -> bool:
    """Whether this deployment is hard read-only."""
    source = env if env is not None else os.environ
    return str(source.get("MCP_READ_ONLY", "")).strip().lower() in ("1", "true", "yes")


def is_read_tool(annotations: ToolAnnotations | None) -> bool:
    """Whether a tool only reads, according to its own declaration.

    Derived from `readOnlyHint` so the read-only filter and the annotation a
    client sees can never disagree: they are the same fact.

    A tool with no annotations is treated as a write. The specification
    defaults `destructiveHint` to true when annotations exist but the hint is
    omitted, and the same caution applies here: an unclassified tool in a
    read-only deployment must fail closed, or the control is advisory.

    The attribute is `read_only_hint`; `readOnlyHint` is only the wire alias.
    Reading the alias off the model returns nothing, which would have made
    every tool look like a write and refused the entire catalogue in read-only
    mode. Both spellings are accepted so a plain object or a dict-like stand-in
    behaves the same as the SDK model.
    """
    if annotations is None:
        return False
    for attribute in ("read_only_hint", "readOnlyHint"):
        value = getattr(annotations, attribute, None)
        if value is not None:
            return bool(value)
    return False


def filter_read_only(
    tools: Iterable, annotations_by_name: dict[str, ToolAnnotations | None]
) -> list:
    """Drop every non-read tool from a listing."""
    return [t for t in tools if is_read_tool(annotations_by_name.get(getattr(t, "name", "")))]


def assert_callable(
    name: str, annotations: ToolAnnotations | None, env: dict | None = None
) -> None:
    """Refuse a write tool when the deployment is read-only.

    `env` is the deployment's environment, so enforcement reads the SAME source
    the server was configured from. Without it this consulted `os.environ`
    directly while `ConnectorServer` held its own `_env`, so a server built with
    `env={"MCP_READ_ONLY": "1"}` LOGGED read_only=True at startup and then
    allowed every write: the banner and the control disagreed, and the banner
    is the half a person reads.
    """
    if not read_only_enabled(env):
        return
    if is_read_tool(annotations):
        return
    raise ReadOnlyViolation(
        f"{name!r} is not a read-only tool and this deployment sets MCP_READ_ONLY. "
        "Tools that modify state are refused here regardless of the caller's "
        "permissions."
    )
