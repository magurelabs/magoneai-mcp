"""`update_event` must survive the migration, because saved agents still use it.

v1 split `update_event` into `update_event_details` and `update_event_time`.
Saved workflows and agent configurations in a running deployment still
contained the exact quoted name `"update_event"`, and none referenced either
replacement.

The two failure modes are not equally visible, which is why this is a blocker
rather than a cleanup item:

- a TOOL node bound to a missing tool hard-fails, loudly
- an AGENT simply loses the capability and is never told, so the only symptom
  is that it stops offering to reschedule anything

So the name survives one release as a dispatcher over the two replacements. It
adds no capability: it accepts the v1 schema exactly and forwards each field
to whichever replacement owns it.

The partial-failure case is the one worth testing hardest. A call that changes
both the time and the guest list makes two Graph requests, and if the second
fails the event is half-updated. Returning only the last result would report
that as a clean failure and leave the time change in place unmentioned.
"""

import importlib

import pytest

# These import real connectors, so they need the connector dependencies.
# `pip install -e ".[dev]"` deliberately installs only the framework's, so an
# outside contributor can run most of the suite with almost nothing. The
# Connector tests job installs `.[dev,all]` and runs exactly these.
pytestmark = pytest.mark.requires_connectors

# `importorskip`, not `import_module`: pytest IMPORTS a test module during
# collection, so `pytestmark` deselects the tests but cannot stop the import.
# The Framework tests job installs only the framework's dependencies, so a
# plain import here fails collection outright and takes the whole job with it.
# Imported at MODULE level, so this must survive collection in an install
# that has only the framework's dependencies. `pytestmark` deselects the tests
# but pytest still imports the module to find them, and `importorskip`
# re-raises an ImportError raised from a transitive dependency rather than
# from the named module. An explicit skip is the version-independent form.
try:
    outlook_calendar = importlib.import_module("connectors.outlook_calendar.server")
except ImportError as exc:  # pragma: no cover - depends on the install
    pytest.skip(
        f"connectors.outlook_calendar.server needs the connector dependencies ({exc}); "
        "the Connector tests job installs them",
        allow_module_level=True,
    )
update_event = outlook_calendar.server.mcp._tool_manager._tools["update_event"].fn


async def call(**kwargs):
    """Call the tool as the wire does and return the handler's payload.

    `fn` is the guarded wrapper: an error return comes back wrapped in a
    `CallToolResult` while a success comes back as the plain dict. Accepting
    both keeps every assertion below about the tool's answer rather than about
    MCP's envelope.
    """
    import json

    result = await update_event(**kwargs)
    if isinstance(result, dict):
        return result
    return json.loads(result.content[0].text)


@pytest.fixture
def dispatched(monkeypatch):
    """Record which replacement was called with what, calling neither for real."""
    calls: dict = {}

    async def fake_time(credentials, **kwargs):
        calls["time"] = kwargs
        return {"success": True, "event_id": kwargs["event_id"], "changed": "time"}

    async def fake_details(credentials, **kwargs):
        calls["details"] = kwargs
        return {"success": True, "event_id": kwargs["event_id"], "changed": "details"}

    monkeypatch.setattr(outlook_calendar, "update_event_time", fake_time)
    monkeypatch.setattr(outlook_calendar, "update_event_details", fake_details)
    return calls


class TestItRoutesEachFieldToItsOwner:
    @pytest.mark.asyncio
    async def test_a_time_change_goes_only_to_update_event_time(self, dispatched):
        await update_event(event_id="e1", start_time="2026-10-01T09:00:00")

        assert "time" in dispatched
        assert "details" not in dispatched
        assert dispatched["time"]["start_time"] == "2026-10-01T09:00:00"

    @pytest.mark.asyncio
    async def test_a_title_change_goes_only_to_update_event_details(self, dispatched):
        await update_event(event_id="e1", title="New title")

        assert "details" in dispatched
        assert "time" not in dispatched
        assert dispatched["details"]["title"] == "New title"

    @pytest.mark.asyncio
    async def test_both_kinds_reach_both(self, dispatched):
        await update_event(event_id="e1", title="New", start_time="2026-10-01T09:00:00")

        assert set(dispatched) == {"time", "details"}

    @pytest.mark.asyncio
    async def test_the_timezone_travels_with_the_time(self, dispatched):
        """`timezone` only means anything to the time call, and sending it to
        the details call would be passing an argument it does not take."""
        await update_event(event_id="e1", start_time="09:00", timezone="Asia/Dubai")

        assert dispatched["time"]["timezone"] == "Asia/Dubai"

    @pytest.mark.asyncio
    async def test_an_empty_attendee_list_is_still_a_change(self, dispatched):
        """`attendees=[]` means "remove everyone", which is a real instruction.
        A truthiness test would drop it and silently leave the guest list."""
        await update_event(event_id="e1", attendees=[])

        assert "details" in dispatched
        assert dispatched["details"]["attendees"] == []

    @pytest.mark.asyncio
    async def test_a_zero_duration_is_still_a_change(self, dispatched):
        """Same trap on the other side: 0 is falsy and is a value."""
        await update_event(event_id="e1", duration_minutes=0)

        assert "time" in dispatched


class TestNothingToDo:
    @pytest.mark.asyncio
    async def test_only_an_event_id_is_refused_rather_than_silently_ignored(self, dispatched):
        result = await call(event_id="e1")

        assert result["success"] is False
        assert not dispatched


class TestPartialFailureIsNamed:
    @pytest.mark.asyncio
    async def test_a_failure_in_the_second_call_is_not_hidden_by_the_first(self, monkeypatch):
        async def ok(credentials, **kwargs):
            return {"success": True, "changed": "time"}

        async def fails(credentials, **kwargs):
            return {"success": False, "error": "Graph rejected the attendee list"}

        monkeypatch.setattr(outlook_calendar, "update_event_time", ok)
        monkeypatch.setattr(outlook_calendar, "update_event_details", fails)

        result = await call(event_id="e1", start_time="09:00", attendees=["a@b.com"])

        assert result["success"] is False
        assert "attendee list" in result["error"]
        assert result["partial"] is True, "the time change went through and the caller is not told"

    @pytest.mark.asyncio
    async def test_a_single_failure_is_not_reported_as_partial(self, monkeypatch):
        async def fails(credentials, **kwargs):
            return {"success": False, "error": "no such event"}

        monkeypatch.setattr(outlook_calendar, "update_event_time", fails)

        result = await call(event_id="e1", start_time="09:00")

        assert result["success"] is False
        assert result.get("partial") is not True


class TestTheContractMatchesV1:
    def test_the_schema_accepts_every_v1_field(self):
        """A binding written against v1 sends these names. One missing turns a
        working call into a validation error at the wire."""
        tool = outlook_calendar.server.mcp._tool_manager._tools["update_event"]
        properties = set((tool.parameters or {}).get("properties", {}))

        assert {
            "event_id",
            "title",
            "start_time",
            "end_time",
            "duration_minutes",
            "description",
            "location",
            "attendees",
            "timezone",
        } <= properties

    def test_only_event_id_is_required(self):
        tool = outlook_calendar.server.mcp._tool_manager._tools["update_event"]

        assert (tool.parameters or {}).get("required") == ["event_id"]

    def test_it_is_not_marked_read_only(self):
        """`MCP_READ_ONLY` trusts `readOnlyHint`, and this writes to a calendar."""
        annotations = outlook_calendar.server.annotations_for("update_event")

        assert annotations.read_only_hint is False


class TestItIsCallableOverTheWire:
    """The unit tests above call the wrapper directly and so could not see that
    the alias declared no credential headers: over MCP it received an empty
    credentials dict and failed on every call."""

    @pytest.mark.asyncio
    async def test_credentials_reach_the_dispatched_tool(self, monkeypatch):
        from mcp import Client

        seen: dict = {}

        async def fake_details(credentials, **kwargs):
            seen["credentials"] = credentials
            return {"success": True, "event_id": kwargs["event_id"], "changed": "details"}

        monkeypatch.setattr(outlook_calendar, "update_event_details", fake_details)
        monkeypatch.setattr(
            "magone_mcp_core.server._request_headers",
            lambda ctx: {"x-microsoft-refresh-token": "rt", "x-microsoft-client-id": "cid"},
        )

        async with Client(outlook_calendar.server.mcp) as client:
            result = await client.call_tool("update_event", {"event_id": "e-1", "title": "New"})

        assert result.is_error is False, result.content[0].text
        assert seen["credentials"].get("microsoft_refresh_token") == "rt"

    @pytest.mark.asyncio
    async def test_it_is_marked_destructive(self):
        from mcp import Client

        async with Client(outlook_calendar.server.mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}

        assert tools["update_event"].annotations.destructive_hint is True
