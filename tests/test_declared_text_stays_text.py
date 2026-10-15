"""An argument the schema calls text reaches the handler as the text that was sent.

The SDK's `pre_parse_json` ran `json.loads` on every string argument whose
annotation is not exactly `str`. So for `Optional[str]`, `str | None`, `Any`
and unions, `"null"` arrived as None and `'{"a":1}'` as a dict, which then
failed validation or reached the handler with the wrong type. v1 passed text
through untouched. These calls go over a real MCP client, because the rewrite
happened in the SDK's call path and nowhere a direct function call would see.
"""

import json
from typing import Any

import pytest
from mcp import Client
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema
from magone_mcp_core.text_args import admits_string

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# Strings that are also valid JSON, which is exactly what used to be decoded.
JSON_LOOKING_TEXT = ["null", '{"a":1}', "[1]", "123", "true", '"quoted"', "1.5"]


def _shape(value: Any) -> dict:
    return {"type": type(value).__name__, "value": value}


def build() -> ConnectorServer:
    server = ConnectorServer(name="text-args-test", version="0.0.0", env={"MCP_MODE": "local"})

    @server.tool(name="optional_text", description="Optional text.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Any text."}},
        }
    )
    async def optional_text(text: str | None = None) -> dict:
        """Declared string, annotated Optional[str]: the commonest case."""
        return _shape(text)

    @server.tool(name="nullable_text", description="Nullable text.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"text": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
    )
    async def nullable_text(text: str | None = None) -> dict:
        """anyOf string/null, annotated `str | None`."""
        return _shape(text)

    @server.tool(name="derived_text", description="Derived schema.", annotations=READ)
    async def derived_text(text: str | None = None) -> dict:
        """No declared schema: the SDK derives anyOf string/null."""
        return _shape(text)

    @server.tool(name="text_or_object", description="Either.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"body": {"type": ["string", "object"]}},
            "required": ["body"],
        }
    )
    async def text_or_object(body: Any) -> dict:
        """Admits text, so text stays text; an object stays an object."""
        return _shape(body)

    @server.tool(name="any_value", description="Untyped value.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"value": {"description": "Attribute value: string, number, or boolean"}},
            "required": ["value"],
        }
    )
    async def any_value(value: Any) -> dict:
        """Declared the way a custom-attribute tool commonly declares `value`."""
        return _shape(value)

    @server.tool(name="undescribed_any", description="Not in the schema.", annotations=READ)
    @declare_schema({"type": "object", "properties": {}})
    async def undescribed_any(value: Any = None) -> dict:
        """Not described by the schema at all: `Any` falls back to passthrough."""
        return _shape(value)

    @server.tool(name="object_param", description="An object.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"payload": {"type": "object"}},
            "required": ["payload"],
        }
    )
    async def object_param(payload: dict) -> dict:
        """Declared object: keeps the SDK's pre-parse of JSON text."""
        return _shape(payload)

    @server.tool(name="list_param", description="A list.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
        }
    )
    async def list_param(items: list[str]) -> dict:
        """Declared array: keeps the SDK's pre-parse of JSON text."""
        return _shape(items)

    @server.tool(name="int_param", description="An int.", annotations=READ)
    @declare_schema(
        {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
    )
    async def int_param(n: int) -> dict:
        """Declared integer: still validated."""
        return _shape(n)

    return server


async def call(name: str, arguments: dict) -> Any:
    async with Client(build().mcp) as client:
        return await client.call_tool(name, arguments)


def payload(result) -> dict:
    assert result.is_error is False, result.content[0].text
    return json.loads(result.content[0].text)


class TestDeclaredTextIsNotDecoded:
    @pytest.mark.parametrize("tool", ["optional_text", "nullable_text", "derived_text"])
    async def test_the_string_null_is_four_characters_not_none(self, tool):
        assert payload(await call(tool, {"text": "null"})) == {"type": "str", "value": "null"}

    @pytest.mark.parametrize("tool", ["optional_text", "nullable_text", "derived_text"])
    async def test_json_object_text_stays_text(self, tool):
        out = payload(await call(tool, {"text": '{"a":1}'}))
        assert out == {"type": "str", "value": '{"a":1}'}

    @pytest.mark.parametrize("tool", ["optional_text", "nullable_text", "derived_text"])
    async def test_number_text_stays_text(self, tool):
        assert payload(await call(tool, {"text": "123"})) == {"type": "str", "value": "123"}

    @pytest.mark.parametrize("text", JSON_LOOKING_TEXT)
    async def test_every_json_looking_string_survives(self, text):
        assert payload(await call("optional_text", {"text": text})) == {
            "type": "str",
            "value": text,
        }

    async def test_a_real_null_is_still_none(self):
        assert payload(await call("optional_text", {"text": None})) == {
            "type": "NoneType",
            "value": None,
        }

    async def test_text_or_object_keeps_text_as_text(self):
        out = payload(await call("text_or_object", {"body": '{"a":1}'}))
        assert out == {"type": "str", "value": '{"a":1}'}

    async def test_text_or_object_still_takes_a_real_object(self):
        out = payload(await call("text_or_object", {"body": {"a": 1}}))
        assert out == {"type": "dict", "value": {"a": 1}}


class TestUntypedValuesPassThrough:
    @pytest.mark.parametrize("text", JSON_LOOKING_TEXT)
    async def test_an_untyped_declared_value_receives_text_unchanged(self, text):
        assert payload(await call("any_value", {"value": text})) == {"type": "str", "value": text}

    async def test_a_json_blob_attribute_is_stored_as_text(self):
        blob = json.dumps({"customer": "Ada Lovelace", "stage": "confirming"})
        assert payload(await call("any_value", {"value": blob})) == {"type": "str", "value": blob}

    async def test_an_untyped_value_still_takes_non_strings(self):
        assert payload(await call("any_value", {"value": 7})) == {"type": "int", "value": 7}

    async def test_an_any_parameter_the_schema_does_not_describe(self):
        out = payload(await call("undescribed_any", {"value": "[1]"}))
        assert out == {"type": "str", "value": "[1]"}


class TestStructuredParametersAreUnchanged:
    async def test_an_object_param_accepts_an_object(self):
        out = payload(await call("object_param", {"payload": {"a": 1}}))
        assert out == {"type": "dict", "value": {"a": 1}}

    async def test_an_object_param_still_accepts_json_text(self):
        """The SDK's leniency for structured params is kept, not removed."""
        out = payload(await call("object_param", {"payload": '{"a":1}'}))
        assert out == {"type": "dict", "value": {"a": 1}}

    async def test_a_list_param_still_accepts_json_text(self):
        out = payload(await call("list_param", {"items": '["x","y"]'}))
        assert out == {"type": "list", "value": ["x", "y"]}

    async def test_an_int_param_validates(self):
        assert payload(await call("int_param", {"n": 5})) == {"type": "int", "value": 5}

    async def test_an_int_param_refuses_garbage(self):
        result = await call("int_param", {"n": "not a number"})
        assert result.is_error is True


class TestThePublishedSchemaIsUntouched:
    async def test_declared_schemas_are_advertised_verbatim(self):
        async with Client(build().mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["any_value"].input_schema["properties"]["value"] == {
            "description": "Attribute value: string, number, or boolean"
        }
        assert tools["optional_text"].input_schema["properties"]["text"] == {
            "type": "string",
            "description": "Any text.",
        }


class TestAdmitsString:
    @pytest.mark.parametrize(
        "schema",
        [
            {},
            {"description": "anything"},
            True,
            {"type": "string"},
            {"type": ["string", "null"]},
            {"type": ["null", "string"]},
            {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            {"oneOf": [{"type": "string"}, {"type": "null"}]},
            {"allOf": [{"type": "string"}, {"minLength": 1}]},
            {"enum": ["a", "b"]},
            {"enum": ["a", 1]},
            {"const": "x"},
            {"format": "date-time"},
        ],
    )
    def test_admits(self, schema):
        assert admits_string(schema) is True

    @pytest.mark.parametrize(
        "schema",
        [
            False,
            {"type": "object"},
            {"type": "array"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
            {"type": ["integer", "null"]},
            {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            {"allOf": [{"type": "string"}, {"type": "integer"}]},
            {"enum": [1, 2]},
            {"const": 3},
            {"type": "string", "anyOf": [{"type": "object"}]},
        ],
    )
    def test_refuses(self, schema):
        assert admits_string(schema) is False

    def test_local_refs_are_followed(self):
        root = {
            "$defs": {"Text": {"type": "string"}, "Obj": {"type": "object"}},
            "properties": {"a": {"$ref": "#/$defs/Text"}, "b": {"$ref": "#/$defs/Obj"}},
        }
        assert admits_string(root["properties"]["a"], root) is True
        assert admits_string(root["properties"]["b"], root) is False

    def test_a_self_referencing_ref_terminates(self):
        root = {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}}
        assert admits_string({"$ref": "#/$defs/Loop"}, root) is True
