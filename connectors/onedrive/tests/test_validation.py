"""ID validation across every tool that interpolates one into a URL path.

Each of these tools builds an endpoint like `/me/drive/items/{id}/children`
by f-string. An unvalidated id containing `/` or `..` would let a caller
retarget the request at a different Graph resource, so `validate_id` guards
every one of them. The table is deliberately exhaustive: a new tool that
interpolates an id without validating it should fail here.
"""

import pytest

from ._harness import FakeGraph, constants, run

ID_BEARING_TOOLS = [
    ("onedrive_list_files", {}, "folder_id"),
    ("onedrive_get_file", {}, "item_id"),
    ("onedrive_download_file", {}, "item_id"),
    ("onedrive_delete_item", {}, "item_id"),
    ("onedrive_create_folder", {"name": "New"}, "parent_folder_id"),
    ("onedrive_copy_item", {}, "item_id"),
    ("onedrive_copy_item", {"item_id": "OK1"}, "destination_folder_id"),
    ("onedrive_move_item", {"destination_folder_id": "D1"}, "item_id"),
    ("onedrive_move_item", {"item_id": "S1"}, "destination_folder_id"),
    ("onedrive_create_share_link", {}, "item_id"),
    ("onedrive_upload_file", {"filename": "a.txt", "content": "x"}, "folder_id"),
]

HOSTILE_IDS = ["../../etc/passwd", "a/b", "root:/Documents", "X1/children", ".."]


@pytest.mark.parametrize("tool,base,field", ID_BEARING_TOOLS)
@pytest.mark.parametrize("hostile", HOSTILE_IDS)
async def test_path_bearing_ids_are_rejected_before_any_request(
    monkeypatch, tool, base, field, hostile
):
    graph = FakeGraph()
    result = await run(monkeypatch, tool, graph, **{**base, field: hostile})
    assert result["success"] is False, f"{tool}.{field} accepted {hostile!r}"
    assert "invalid characters" in result["error"]
    assert graph.requests == [] and graph.token_requests == []


@pytest.mark.parametrize("tool,base,field", ID_BEARING_TOOLS)
async def test_path_bearing_ids_are_length_capped(monkeypatch, tool, base, field):
    graph = FakeGraph()
    overlong = "x" * (constants.MAX_ID_LENGTH + 1)
    result = await run(monkeypatch, tool, graph, **{**base, field: overlong})
    assert result["success"] is False
    assert "too long" in result["error"]
    assert graph.requests == []


@pytest.mark.parametrize(
    "tool,base,field",
    [t for t in ID_BEARING_TOOLS if t[2] in ("item_id", "destination_folder_id")],
)
async def test_required_ids_are_rejected_when_blank(monkeypatch, tool, base, field):
    """Blank optional ids mean 'use the root'; blank required ids are an error."""
    graph = FakeGraph()
    result = await run(monkeypatch, tool, graph, **{**base, field: "   "})
    assert result["success"] is False
    assert "cannot be empty" in result["error"]
    assert graph.requests == []


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_validate_id_rejects_blank(value):
    assert constants.validate_id(value, "item_id") == "item_id cannot be empty"


@pytest.mark.parametrize("value", ["a/b", "..", "a/../b", "/leading", "trailing/"])
def test_validate_id_rejects_separators_and_traversal(value):
    assert constants.validate_id(value, "item_id") == "item_id contains invalid characters"


def test_validate_id_accepts_a_value_exactly_at_the_cap():
    assert constants.validate_id("x" * constants.MAX_ID_LENGTH, "item_id") is None


@pytest.mark.parametrize(
    "value", ["01ABCDEFGHIJKLMNOPQRSTUVWXYZ", "F1", "a1b2-c3d4_e5", "item.with.dots", "AAAA!BBBB"]
)
def test_validate_id_accepts_legitimate_ids(value):
    assert constants.validate_id(value, "item_id") is None


def test_validate_id_names_the_offending_field():
    """The message reaches the model, which needs to know which argument to fix."""
    assert constants.validate_id("a/b", "destination_folder_id").startswith(
        "destination_folder_id "
    )
