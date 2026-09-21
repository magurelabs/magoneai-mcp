"""A refusal is a failure, and it has to be reported as one.

A tool that returns `{"success": false, "error": "..."}` declined to do the
thing. Reported without `isError`, the platform records a successful tool
call, the model reads "success" and stops, and a refusal written specifically
to tell it what to do instead is never acted on.

v1 recorded the incident that prompted this, and it is not hypothetical:

> render_document refused to replace a seven slide deck with one and said to
> use revise_document update_slide. The agent made no second call and told the
> user the edit could not be done.

The rule is narrow on purpose. Widening it would start marking successful
calls as failures, which is the opposite mistake and a worse one, so the
boundaries get their own tests.
"""

import json

from mcp.types import CallToolResult, TextContent

from magone_mcp_core.results import EXTRA_CONTENT_KEY, is_refusal, to_call_result


class TestIsRefusal:
    def test_a_non_empty_error_string_is_a_refusal(self):
        assert is_refusal({"success": False, "error": "cannot do that"})

    def test_an_error_alongside_other_fields_is_still_a_refusal(self):
        assert is_refusal({"error": "no", "hint": "try update_slide", "code": 3})

    def test_a_whitespace_only_error_is_not_a_refusal(self):
        """An empty message tells the model nothing, and marking the call
        failed without saying why is worse than letting it through."""
        assert not is_refusal({"error": "   "})

    def test_an_empty_error_string_is_not_a_refusal(self):
        assert not is_refusal({"error": ""})

    def test_a_null_error_is_not_a_refusal(self):
        """A tool that always includes `error`, null when fine, is common."""
        assert not is_refusal({"error": None, "data": [1]})

    def test_an_error_of_another_shape_is_not_a_refusal(self):
        """A tool may legitimately carry a structured `error` field that is not
        a refusal message."""
        assert not is_refusal({"error": {"code": 500}})
        assert not is_refusal({"error": ["a", "b"]})
        assert not is_refusal({"error": 404})

    def test_a_success_is_not_a_refusal(self):
        assert not is_refusal({"success": True, "results": []})

    def test_a_list_is_not_a_refusal(self):
        assert not is_refusal([{"error": "x"}])

    def test_a_string_is_not_a_refusal(self):
        assert not is_refusal("error: something")

    def test_none_is_not_a_refusal(self):
        assert not is_refusal(None)


class TestToCallResult:
    def test_a_refusal_is_marked_and_keeps_its_payload(self):
        """Both halves matter. Raising ToolError would also set the flag, and
        would discard the structured result, which is frequently where the
        tool says what to do instead."""
        result = to_call_result({"success": False, "error": "use update_slide"})

        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert "use update_slide" in result.content[0].text
        assert json.loads(result.content[0].text)["success"] is False

    def test_a_success_is_one_text_block_as_v1_sent_it(self):
        """v1: `json.dumps(result, indent=2, default=str)`, one block."""
        payload = {"success": True, "results": [1, 2]}

        result = to_call_result(payload)

        assert result.is_error is False
        assert [b.text for b in result.content] == [json.dumps(payload, indent=2)]

    def test_a_string_is_sent_as_itself(self):
        result = to_call_result("plain text")

        assert [b.text for b in result.content] == ["plain text"]

    def test_a_list_is_one_block_not_one_block_per_item(self):
        """Left to the SDK a list becomes a block per item, and a caller that
        reads the first block (the platform does) sees one row."""
        payload = [{"id": 1}, {"id": 2}, {"id": 3}]

        result = to_call_result(payload)

        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == payload

    def test_structured_content_only_for_a_declared_output_schema(self):
        payload = {"id": 1}

        assert to_call_result(payload).structured_content is None
        assert to_call_result(payload, structured=True).structured_content == payload

    def test_an_existing_call_tool_result_is_not_rewrapped(self):
        original = CallToolResult(content=[TextContent(type="text", text="x")], isError=True)

        assert to_call_result(original) is original


class TestExtraContent:
    def test_the_extra_block_is_appended_and_the_key_removed(self):
        """One tool in the catalogue returns an attachment this way. Dropping
        it turns the attachment into a mention of an attachment."""
        block = {
            "type": "resource",
            "resource": {
                "uri": "attachment://r.pdf",
                "mimeType": "application/pdf",
                "blob": "YmFzZTY0",
            },
        }

        result = to_call_result({"success": True, EXTRA_CONTENT_KEY: block})

        assert isinstance(result, CallToolResult)
        assert len(result.content) == 2
        # Typed, not the raw dict: the SDK validates `content`, so v1's
        # hand-built envelope shape is rejected outright and would take the
        # whole result down with it.
        assert result.content[1].type == "resource"
        assert str(result.content[1].resource.uri) == "attachment://r.pdf"
        assert EXTRA_CONTENT_KEY not in json.loads(result.content[0].text)

    def test_extra_content_on_a_success_does_not_set_is_error(self):
        block = {"type": "text", "text": "extra"}

        result = to_call_result({"success": True, EXTRA_CONTENT_KEY: block})

        assert result.is_error is False

    def test_a_refusal_carrying_extra_content_gets_both(self):
        block = {"type": "text", "text": "extra"}

        result = to_call_result({"error": "partial", EXTRA_CONTENT_KEY: block})

        assert result.is_error is True
        assert len(result.content) == 2

    def test_the_original_dict_is_not_mutated(self):
        """v1 used `result.pop(...)`, which edits the caller's dict. A handler
        that returns a cached or shared structure would find it changed."""
        block = {"type": "text", "text": "extra"}
        payload = {"success": True, EXTRA_CONTENT_KEY: block}

        to_call_result(payload)

        assert EXTRA_CONTENT_KEY in payload

    def test_the_real_catalogue_block_parses(self):
        """The exact shape `outlook_email.download_attachment` returns.

        Pinned because the fallback is silent: a block that fails validation
        becomes text rather than raising, so a regression here would turn an
        attachment into a paragraph describing one and nothing would say so.
        """
        block = {
            "type": "resource",
            "resource": {
                "uri": "attachment://report.pdf",
                "mimeType": "application/pdf",
                "blob": "YmFzZTY0",
            },
        }

        result = to_call_result({"success": True, EXTRA_CONTENT_KEY: block})

        assert result.content[1].type == "resource", "fell back to text"
        assert result.content[1].resource.blob == "YmFzZTY0"

    def test_an_unparseable_block_degrades_to_text_rather_than_raising(self):
        """A tool that worked in v1 must not fail entirely because of the shape
        of an *extra* block. Degraded delivery beats no delivery."""
        result = to_call_result({"success": True, EXTRA_CONTENT_KEY: {"type": "nonsense"}})

        assert result.content[1].type == "text"
        assert "nonsense" in result.content[1].text
