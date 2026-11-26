"""A tool that takes `cursor` declares the paging shape a host can rely on.

`docs/writing-a-connector.md` ("Tools that page through a whole history")
describes the shape: the call takes `cursor`; the result names the next one in
`next_cursor` and carries its items in an array. A host that pages on its own,
without a model reading each result, depends on that shape, so it is checked
here against the approved goldens rather than left to review:

- the tool is read-only (`readOnlyHint`), since a host may call it hundreds
  of times in a row;
- its declared output schema has `next_cursor` and at least one array.

Tools that take a cursor but declare no output schema yet are listed in
`UNDECLARED` and may only leave it: a new one fails here.
"""

import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
TOOLS = json.loads((GOLDEN / "tool-schemas.json").read_text(encoding="utf-8"))
OUTPUTS = json.loads((GOLDEN / "output-schemas.json").read_text(encoding="utf-8"))

# Take a cursor, declare no output schema. Remove a tool once it declares one.
UNDECLARED = frozenset(
    {
        "atlassian_confluence/getConfluenceCommentChildren",
        "atlassian_confluence/getConfluenceFolderChildren",
        "atlassian_confluence/getConfluencePageAttachments",
        "atlassian_confluence/getConfluencePageDescendants",
        "atlassian_confluence/getConfluencePageFooterComments",
        "atlassian_confluence/getConfluencePageInlineComments",
        "atlassian_confluence/getConfluencePageLabels",
        "atlassian_confluence/getConfluenceSpaces",
        "atlassian_confluence/getPagesInConfluenceSpace",
    }
)

PAGED = sorted(
    name
    for name, tool in TOOLS.items()
    if "cursor" in ((tool.get("schema") or {}).get("properties") or {})
)


def _is_array(spec: object) -> bool:
    kind = spec.get("type") if isinstance(spec, dict) else None
    return kind == "array" or (isinstance(kind, list) and "array" in kind)


def test_there_are_paged_tools_to_check():
    assert len(PAGED) > len(UNDECLARED)


@pytest.mark.parametrize("name", PAGED)
def test_a_paged_tool_is_read_only(name):
    assert (TOOLS[name].get("annotations") or {}).get("readOnlyHint") is True


@pytest.mark.parametrize("name", [n for n in PAGED if n not in UNDECLARED])
def test_a_paged_tool_declares_next_cursor_and_its_items(name):
    properties = (OUTPUTS.get(name) or {}).get("properties") or {}

    assert properties, f"{name} takes `cursor` but declares no output schema"
    assert "next_cursor" in properties
    assert any(_is_array(spec) for spec in properties.values()), "no array of items"


def test_the_undeclared_list_only_shrinks():
    """A tool listed here that now declares a schema, or no longer exists, is removed."""
    stale = {n for n in UNDECLARED if n not in TOOLS or OUTPUTS.get(n)}
    assert not stale, f"remove from UNDECLARED: {sorted(stale)}"
