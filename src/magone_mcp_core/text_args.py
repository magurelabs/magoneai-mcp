"""An argument the tool says is text arrives as the text that was sent.

The SDK's tool wrapper pre-parses arguments before validating them
(`FuncMetadata.pre_parse_json`): any string whose Python annotation is not
exactly `str` goes through `json.loads`, and the result replaces it when it is
an object, an array or `null`. That exists for clients that send a structured
argument as JSON text, and for a parameter declared as an object it is
harmless.

For a parameter that is declared as text it is a silent rewrite of what the
caller said. `Optional[str]`, `str | None`, `Any` and unions are all "not
exactly `str`", so across the catalogue:

- the string `"null"` reached the handler as `None`;
- `'{"a": 1}'` and `'[1]'` reached it as a dict and a list, and then either
  failed validation (a hard tool error for a perfectly good string) or got
  through with the wrong type;
- a `set_custom_attribute(value: Any)` tool stored a JSON-text attribute as
  an object, which the workflow reading it back cannot parse.

v1 (the protocol layer of the pre-port catalogue) validated the raw arguments
against the declared input schema with `jsonschema` and handed them to the
handler untouched: it never decoded a string. So this restores v1's semantics
for text, keyed on the same thing v1 keyed on, the tool's published schema:

- a property whose schema admits a string is passed through as sent;
- a property whose schema does not admit a string (object, array, number,
  integer, boolean, null) keeps the SDK's pre-parse exactly as it is today;
- a parameter the schema does not describe at all falls back to its Python
  annotation: unannotated or `Any` is passed through, as v1 did, and anything
  else keeps the SDK's behaviour.

One deliberate difference from v1 remains, in the lenient direction: v1
rejected JSON text sent for an object or array parameter (`jsonschema` saw a
string where it wanted an object), while the SDK's pre-parse accepts it. That
is kept, because removing it would turn calls that work today into errors.

The published `inputSchema` is not touched. Only the call path changes.
"""

from typing import Any

from mcp.server.mcpserver.tools.base import Tool as SDKTool
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata


def admits_string(
    schema: Any, root: dict[str, Any] | None = None, _seen: frozenset = frozenset()
) -> bool:
    """Whether a JSON value that is a string can satisfy `schema`.

    Read conservatively in the direction that keeps text as text: a schema that
    places no constraint on type (`{}`, or only a `description`, as a
    custom-attribute `value` is often declared) admits a string. Every keyword
    present must admit one, since JSON Schema keywords combine as a
    conjunction; `anyOf` and `oneOf` admit a string when any branch does,
    `allOf` when every branch does.
    """
    if schema is False:
        return False
    if not isinstance(schema, dict):
        # `true`, or something that is not a schema at all and so constrains
        # nothing. Either way text is not refused by it.
        return True

    root = root if root is not None else schema

    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if "string" not in kinds:
            return False

    if "enum" in schema and isinstance(schema["enum"], list):
        if not any(isinstance(v, str) for v in schema["enum"]):
            return False

    if "const" in schema and not isinstance(schema["const"], str):
        return False

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            if not any(admits_string(b, root, _seen) for b in branches):
                return False

    branches = schema.get("allOf")
    if isinstance(branches, list) and branches:
        if not all(admits_string(b, root, _seen) for b in branches):
            return False

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in _seen:
        target = _resolve_local_ref(ref, root)
        if target is not None and not admits_string(target, root, _seen | {ref}):
            return False

    return True


def _resolve_local_ref(ref: str, root: dict[str, Any]) -> Any:
    """`#/$defs/X` style references within the same schema, or None."""
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def text_keys(parameters: dict[str, Any] | None, fn_metadata: FuncMetadata) -> frozenset[str]:
    """The argument keys whose string values must reach the handler as sent.

    `parameters` is the tool's published input schema (declared or derived).
    """
    parameters = parameters if isinstance(parameters, dict) else {}
    properties = parameters.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    keys: set[str] = set()
    for field_name, field_info in fn_metadata.arg_model.model_fields.items():
        wire_names = {field_name}
        if field_info.alias:
            wire_names.add(field_info.alias)
        described = [properties[n] for n in wire_names if n in properties]
        if described:
            if any(admits_string(s, parameters) for s in described):
                keys |= wire_names
        elif field_info.annotation is Any or field_info.annotation is None:
            # Not described by the schema. Unannotated parameters reach the
            # arg model as `Any`, and v1 passed both through untouched.
            keys |= wire_names
    # A described property the handler takes through `**kwargs` never reaches
    # `pre_parse_json` (it only looks at model fields), so it needs no entry.
    return frozenset(keys)


class TextPreservingFuncMetadata(FuncMetadata):
    """The SDK's argument metadata, minus the JSON decoding of declared text."""

    text_keys: frozenset[str] = frozenset()

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        parsed = super().pre_parse_json(data)
        for key in self.text_keys:
            if key in data and isinstance(data[key], str):
                parsed[key] = data[key]
        return parsed


def preserve_declared_text(tool: SDKTool) -> SDKTool:
    """Make `tool` pass every argument its schema declares as text through as sent.

    Called by `register_tool` once the published schema and output schema are
    final, so the decision is made against exactly what a client was told.
    Idempotent: re-applying recomputes the keys from the current schema.
    """
    current = tool.fn_metadata
    tool.fn_metadata = TextPreservingFuncMetadata(
        arg_model=current.arg_model,
        output_schema=current.output_schema,
        output_model=current.output_model,
        wrap_output=current.wrap_output,
        text_keys=text_keys(tool.parameters, current),
    )
    return tool
