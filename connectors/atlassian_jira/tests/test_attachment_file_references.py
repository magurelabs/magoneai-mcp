"""`addJiraIssueAttachment` takes a platform file reference, not model base64.

The parameter exists because the alternative is the model hand-encoding the
bytes, which arrives as a file that opens and is silently corrupt. A calling
platform resolves a `file_id` into `{filename, content_base64, content_type}`
before the call reaches this connector, so the connector holds no platform
credentials and the capability belongs in the public tree.

Such a resolver keys on a parameter literally named `attachments` and
early-returns without it, which is why dropping
the parameter disabled the whole path silently - the tool kept working for
base64. These tests pin the parameter's presence and shape as much as the decode.
"""

import base64

import pytest

from connectors.atlassian_jira.server import (
    MAX_ATTACHMENTS,
    _attachment_file,
    _decode_base64,
    server,
)

CONTENT = b"%PDF-1.4 quarterly numbers"
B64 = base64.b64encode(CONTENT).decode()


def test_the_tool_still_declares_an_attachments_parameter():
    """If this fails, platform file hydration silently stops happening."""
    tool = server.mcp._tool_manager._tools["addJiraIssueAttachment"]
    props = tool.parameters["properties"]
    assert "attachments" in props, "the platform resolver keys on this exact name"
    assert props["attachments"]["maxItems"] == MAX_ATTACHMENTS
    # Only the issue is mandatory: a caller passes attachments OR the single-file
    # fields, and requiring either of the latter would break the array path.
    assert tool.parameters["required"] == ["issueIdOrKey"]


def test_the_description_steers_to_attachments_not_model_base64():
    """The tool description is what a model reads first. It once said "Content
    is base64-encoded", which invites the model to write the base64 itself:
    output tokens cap that at a few tens of KB, and a hand-encoded file arrives
    corrupt. It must name `attachments` and keep base64 for direct callers."""
    description = server.mcp._tool_manager._tools["addJiraIssueAttachment"].description
    assert "`attachments`" in description
    assert "file_id" in description
    assert "only for a direct caller that already holds the bytes" in description
    assert "Content is base64-encoded" not in description


def test_a_resolved_item_becomes_name_bytes_and_mime():
    name, content, mime = _attachment_file(
        {"filename": "report.pdf", "content_base64": B64, "content_type": "application/pdf"}, 0
    )
    assert (name, content, mime) == ("report.pdf", CONTENT, "application/pdf")


def test_a_resolved_item_without_a_filename_gets_a_placeholder():
    name, content, mime = _attachment_file({"content_base64": B64}, 0)
    assert name == "attachment" and content == CONTENT and mime is None


def test_an_unresolved_file_reference_says_why_rather_than_uploading_nothing():
    """A bare file_id means no resolver ran - a direct MCP call, not a platform
    one. There are no bytes, and the error has to say that rather than produce an
    opaque type error."""
    with pytest.raises(ValueError, match="unresolved file reference"):
        _attachment_file({"file_id": "9f1c2e3a-0000-0000-0000-000000000000"}, 0)
    with pytest.raises(ValueError, match="unresolved file reference"):
        _attachment_file("exports/proj/report.pdf", 2)


def test_the_error_tells_a_direct_caller_what_to_send():
    """Called without a host that resolves references, the fix is to send the
    bytes inline; the message has to say so rather than only name a host."""
    with pytest.raises(ValueError, match="send the file's bytes inline"):
        _attachment_file({"file_id": "9f1c2e3a-0000-0000-0000-000000000000"}, 0)


def test_the_error_names_the_position_so_one_bad_item_is_findable():
    with pytest.raises(ValueError, match=r"attachments\[3\]"):
        _attachment_file({"filename": "x"}, 3)


def test_a_data_uri_prefix_and_mime_line_breaks_are_tolerated():
    """Producers wrap base64 both ways; a strict decode alone would reject them."""
    wrapped = "data:application/pdf;base64," + "\n".join(
        [B64[i : i + 16] for i in range(0, len(B64), 16)]
    )
    assert _decode_base64(wrapped, "attachments[0]") == CONTENT


def test_invalid_base64_is_rejected_rather_than_silently_truncated():
    with pytest.raises(ValueError, match="not valid base64"):
        _decode_base64("this is not base64!!", "attachments[0]")


def test_empty_content_is_rejected():
    with pytest.raises(ValueError, match="content is empty"):
        _decode_base64(base64.b64encode(b"").decode(), "attachments[0]")
