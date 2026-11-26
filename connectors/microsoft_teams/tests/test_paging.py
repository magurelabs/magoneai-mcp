"""The paged read tools page a whole history, and only the history they were asked for.

Through a real MCP client, with Graph mocked at the transport.
"""

import base64
import json

import httpx
import pytest
from mcp import Client

from connectors.microsoft_teams.tests._graph_mock import GRAPH, install, message, module, payload

pytestmark = pytest.mark.requires_connectors

CHAT = "19:abc123@thread.v2"
TEAM = "team-1"
CHANNEL = "19:chan@thread.tacv2"


async def call(tool: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return payload(await client.call_tool(tool, arguments))


def pages(*responses: dict):
    """A router answering each Graph request with the next response, in order."""
    queue = list(responses)

    def router(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=queue.pop(0))

    return router


def forged_cursor(link: str) -> str:
    raw = json.dumps({"link": link}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# -- list_chat_messages -------------------------------------------------------


async def test_chat_pages_until_graph_has_no_next_link(monkeypatch):
    next_link = f"{GRAPH}/chats/{CHAT}/messages?$top=2&$skiptoken=abc"
    seen = install(monkeypatch, pages(
        {"value": [message("3", "2025-11-30T10:00:00Z"), message("2", "2025-11-20T10:00:00Z")],
         "@odata.nextLink": next_link},
        {"value": [message("1", "2025-11-10T10:00:00Z")]},
    ))

    first = await call("list_chat_messages", {"chat_id": CHAT, "page_size": 2})
    assert [m["id"] for m in first["messages"]] == ["3", "2"]
    assert first["complete"] is False and first["next_cursor"]

    second = await call("list_chat_messages", {"chat_id": CHAT, "cursor": first["next_cursor"]})
    assert [m["id"] for m in second["messages"]] == ["1"]
    assert second == {**second, "complete": True, "next_cursor": None, "stopped_reason": "end_of_data"}

    assert seen.requests[0].url.params["$top"] == "2"
    assert seen.requests[0].url.params["$orderby"] == "createdDateTime desc"
    # The second request is Graph's own link, byte for byte.
    assert str(seen.requests[1].url) == str(httpx.URL(next_link))


async def test_chat_upper_bound_is_sent_to_graph(monkeypatch):
    seen = install(monkeypatch, pages({"value": []}))

    await call("list_chat_messages", {"chat_id": CHAT, "created_before": "2025-12-01T00:00:00+05:30"})

    assert seen.requests[0].url.params["$filter"] == "createdDateTime lt 2025-11-30T18:30:00.000000Z"


async def test_chat_stops_at_created_after_even_when_graph_has_more(monkeypatch):
    install(monkeypatch, pages({
        "value": [
            message("3", "2025-11-30T10:00:00Z"),
            message("2", "2025-11-01T00:00:00Z"),
            message("1", "2025-10-31T23:59:59Z"),
        ],
        "@odata.nextLink": f"{GRAPH}/chats/{CHAT}/messages?$skiptoken=more",
    }))

    out = await call("list_chat_messages", {"chat_id": CHAT, "created_after": "2025-11-01T00:00:00Z"})

    assert [m["id"] for m in out["messages"]] == ["3", "2"]
    assert out["next_cursor"] is None
    assert out["stopped_reason"] == "reached_created_after"


async def test_chat_cursor_carries_the_lower_bound(monkeypatch):
    install(monkeypatch, pages(
        {"value": [message("2", "2025-11-05T00:00:00Z")],
         "@odata.nextLink": f"{GRAPH}/chats/{CHAT}/messages?$skiptoken=p2"},
        {"value": [message("1", "2025-10-01T00:00:00Z")],
         "@odata.nextLink": f"{GRAPH}/chats/{CHAT}/messages?$skiptoken=p3"},
    ))

    first = await call("list_chat_messages", {"chat_id": CHAT, "created_after": "2025-11-01"})
    # The caller does not resend created_after; the cursor remembers it.
    second = await call("list_chat_messages", {"chat_id": CHAT, "cursor": first["next_cursor"]})

    assert second["messages"] == []
    assert second["stopped_reason"] == "reached_created_after"


async def test_chat_also_drops_messages_graph_should_have_filtered(monkeypatch):
    install(monkeypatch, pages({"value": [
        message("late", "2025-12-02T00:00:00Z"), message("in", "2025-11-15T00:00:00Z"),
    ]}))

    out = await call("list_chat_messages", {"chat_id": CHAT, "created_before": "2025-12-01T00:00:00Z"})

    assert [m["id"] for m in out["messages"]] == ["in"]


async def test_chat_keeps_deleted_messages_and_drops_system_events(monkeypatch):
    seen = install(monkeypatch, pages({"value": [
        message("gone", "2025-11-03T00:00:00Z", deletedDateTime="2025-11-04T00:00:00Z"),
        message("event", "2025-11-02T00:00:00Z", messageType="systemEventMessage"),
        message("ok", "2025-11-01T00:00:00Z"),
    ]}))

    out = await call("list_chat_messages", {"chat_id": CHAT})

    assert [m["id"] for m in out["messages"]] == ["gone", "ok"]
    assert out["messages"][0]["deletedDateTime"] == "2025-11-04T00:00:00Z"
    assert "Prefer" not in seen.requests[0].headers


async def test_chat_system_events_on_request_ask_graph_for_them(monkeypatch):
    seen = install(monkeypatch, pages({"value": [
        message("event", "2025-11-02T00:00:00Z", messageType="systemEventMessage"),
    ]}))

    out = await call("list_chat_messages", {"chat_id": CHAT, "include_system_messages": True})

    assert [m["id"] for m in out["messages"]] == ["event"]
    assert seen.requests[0].headers["Prefer"] == "include-unknown-enum-members"


async def test_chat_returns_the_graph_message_unchanged(monkeypatch):
    raw = message(
        "1", "2025-11-01T00:00:00Z",
        attachments=[{"id": "a", "contentType": "reference", "name": "spec.docx"}],
        mentions=[{"id": 0, "mentionText": "Ann"}],
        reactions=[{"reactionType": "like"}],
        lastEditedDateTime="2025-11-01T01:00:00Z",
    )
    install(monkeypatch, pages({"value": [raw]}))

    out = await call("list_chat_messages", {"chat_id": CHAT})

    assert out["messages"] == [raw]


@pytest.mark.parametrize("arguments", [
    {"created_after": "2025-11-02T00:00:00Z", "created_before": "2025-11-01T00:00:00Z"},
    {"created_after": "2025-11-01T00:00:00Z", "created_before": "2025-11-01T00:00:00Z"},
    {"created_after": "last tuesday"},
])
async def test_chat_bad_dates_are_refused_before_graph(monkeypatch, arguments):
    seen = install(monkeypatch, pages())

    out = await call("list_chat_messages", {"chat_id": CHAT, **arguments})

    assert out["error_code"] == "invalid_argument"
    assert seen.requests == []


async def test_chat_cursor_from_another_chat_is_refused(monkeypatch):
    install(monkeypatch, pages({"value": [message("1", "2025-11-01T00:00:00Z")],
                                "@odata.nextLink": f"{GRAPH}/chats/{CHAT}/messages?$skiptoken=x"}))
    first = await call("list_chat_messages", {"chat_id": CHAT})
    seen = install(monkeypatch, pages())

    out = await call("list_chat_messages", {"chat_id": "19:other@thread.v2", "cursor": first["next_cursor"]})

    assert out["error_code"] == "invalid_cursor"
    assert seen.requests == []


@pytest.mark.parametrize("link", [
    f"https://attacker.example/v1.0/chats/{CHAT}/messages?$skiptoken=x",
    f"http://graph.microsoft.com/v1.0/chats/{CHAT}/messages?$skiptoken=x",
    f"https://graph.microsoft.com:8443/v1.0/chats/{CHAT}/messages",
    f"https://user@graph.microsoft.com/v1.0/chats/{CHAT}/messages",
    f"https://graph.microsoft.com/beta/chats/{CHAT}/messages",
    f"https://graph.microsoft.com/v1.0/chats/{CHAT}/messages/1/replies",
])
async def test_forged_cursor_never_reaches_the_network(monkeypatch, link):
    seen = install(monkeypatch, pages())

    out = await call("list_chat_messages", {"chat_id": CHAT, "cursor": forged_cursor(link)})

    assert out["error_code"] == "invalid_cursor"
    assert seen.requests == []


@pytest.mark.parametrize("cursor", ["not-base64!!", base64.urlsafe_b64encode(b"[1]").decode(), "x" * 9000])
async def test_garbage_cursor_is_refused(monkeypatch, cursor):
    install(monkeypatch, pages())

    out = await call("list_chat_messages", {"chat_id": CHAT, "cursor": cursor})

    assert out["error_code"] == "invalid_cursor"


async def test_percent_encoded_id_in_graph_link_is_accepted(monkeypatch):
    encoded = f"{GRAPH}/chats/19%3Aabc123%40thread.v2/messages?$skiptoken=x"
    install(monkeypatch, pages({"value": [], "@odata.nextLink": encoded}))

    out = await call("list_chat_messages", {"chat_id": CHAT})

    assert out["next_cursor"]


async def test_next_link_for_another_resource_is_an_error_not_an_end(monkeypatch):
    install(monkeypatch, pages({"value": [message("1", "2025-11-01T00:00:00Z")],
                                "@odata.nextLink": f"{GRAPH}/chats/19:other@thread.v2/messages"}))

    out = await call("list_chat_messages", {"chat_id": CHAT})

    assert out["success"] is False
    assert out["error_code"] == "upstream_error"


# -- list_channel_messages ----------------------------------------------------

THREADS = f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages"
WINDOW = {"created_after": "2025-11-01T00:00:00Z", "created_before": "2025-12-01T00:00:00Z"}


def thread(root_id: str, created: str, replies: list[dict], more: bool = False, **extra) -> dict:
    modified = max([created, *(r["lastModifiedDateTime"] for r in replies)])
    root = message(root_id, created, modified=extra.pop("modified", modified), **extra)
    root["replies"] = replies
    root["replies@odata.context"] = "ctx"
    if more:
        root["replies@odata.nextLink"] = f"{THREADS}/{root_id}/replies?$skiptoken=r"
    return root


def reply(msg_id: str, created: str, root_id: str) -> dict:
    return message(msg_id, created, replyToId=root_id)


async def walk(arguments: dict, limit: int = 20) -> tuple[list[str], list[dict]]:
    """Page until complete; return the ids returned and every page."""
    ids: list[str] = []
    outs: list[dict] = []
    cursor = None
    for _ in range(limit):
        out = await call("list_channel_messages", {**arguments, **({"cursor": cursor} if cursor else {})})
        assert out["success"] is True, out
        outs.append(out)
        ids += [m["id"] for m in out["messages"]]
        if out["complete"]:
            assert out["next_cursor"] is None
            return ids, outs
        cursor = out["next_cursor"]
    raise AssertionError("never completed")


async def test_channel_returns_roots_and_replies_in_the_window_as_one_list(monkeypatch):
    seen = install(monkeypatch, pages(
        {"value": [
            thread("r1", "2025-11-03T00:00:00Z", [
                reply("a", "2025-11-04T00:00:00Z", "r1"),
                reply("late", "2025-12-02T00:00:00Z", "r1"),
            ]),
            # Started before the window: only its reply inside the window counts.
            thread("old", "2025-10-01T00:00:00Z", [reply("b", "2025-11-05T00:00:00Z", "old")]),
        ]},
        {"value": []},  # catch-up
    ))

    ids, outs = await walk({"team_id": TEAM, "channel_id": CHANNEL, **WINDOW})

    assert ids == ["r1", "a", "b"]
    first = seen.requests[0].url
    assert first.params["$expand"] == "replies"
    assert first.params["$top"] == "20"
    message_a = outs[0]["messages"][1]
    assert message_a["replyToId"] == "r1" and "replies" not in outs[0]["messages"][0]


async def test_channel_walks_long_threads_then_the_next_page_then_catches_up(monkeypatch):
    seen = install(monkeypatch, pages(
        {"value": [thread("r1", "2025-11-02T00:00:00Z", [reply("a", "2025-11-03T00:00:00Z", "r1")], more=True)],
         "@odata.nextLink": f"{THREADS}?$skiptoken=p2"},
        # r1's replies, from the start, over two pages.
        {"value": [reply("a", "2025-11-03T00:00:00Z", "r1"), reply("b", "2025-11-04T00:00:00Z", "r1")],
         "@odata.nextLink": f"{THREADS}/r1/replies?$skiptoken=x"},
        {"value": [reply("c", "2025-11-05T00:00:00Z", "r1")]},
        {"value": [thread("r2", "2025-11-06T00:00:00Z", [])]},
        # Catch-up: the first page of threads again, now behind the first call.
        {"value": [thread("r2", "2025-11-06T00:00:00Z", [])]},
    ))

    ids, _ = await walk({"team_id": TEAM, "channel_id": CHANNEL, **WINDOW})

    assert ids == ["r1", "a", "a", "b", "c", "r2", "r2"]
    urls = [r.url for r in seen.requests]
    assert urls[1].path.endswith("/messages/r1/replies") and urls[1].params["$top"] == "50"
    assert urls[2].params["$skiptoken"] == "x"
    assert urls[3].params["$skiptoken"] == "p2"
    assert "$skiptoken" not in urls[4].params and urls[4].params["$expand"] == "replies"
    assert len(urls) == 5


async def test_channel_main_pass_stops_on_a_page_with_nothing_since_created_after(monkeypatch):
    seen = install(monkeypatch, pages(
        {"value": [thread("new", "2025-11-10T00:00:00Z", [])], "@odata.nextLink": f"{THREADS}?$skiptoken=p2"},
        {"value": [thread("stale", "2025-09-01T00:00:00Z", [])], "@odata.nextLink": f"{THREADS}?$skiptoken=p3"},
        {"value": [thread("new", "2025-11-10T00:00:00Z", [])], "@odata.nextLink": f"{THREADS}?$skiptoken=c2"},
    ))

    ids, _ = await walk({"team_id": TEAM, "channel_id": CHANNEL, **WINDOW})

    # p3 is never read; the catch-up's first page is already older than the first call.
    assert ids == ["new", "new"]
    assert len(seen.requests) == 3


async def test_channel_cursor_carries_the_settings_of_the_first_call(monkeypatch):
    install(monkeypatch, pages(
        {"value": [thread("r1", "2025-11-10T00:00:00Z", [])], "@odata.nextLink": f"{THREADS}?$skiptoken=p2"},
        {"value": [thread("out", "2025-12-10T00:00:00Z", []), thread("r2", "2025-11-09T00:00:00Z", [])]},
    ))
    first = await call("list_channel_messages", {"team_id": TEAM, "channel_id": CHANNEL, **WINDOW})

    second = await call("list_channel_messages", {
        "team_id": TEAM, "channel_id": CHANNEL, "cursor": first["next_cursor"],
    })

    assert [m["id"] for m in second["messages"]] == ["r2"]


async def test_channel_keeps_a_deleted_root_and_drops_a_system_root(monkeypatch):
    deleted = thread("d", "2025-11-01T00:00:00Z", [], deletedDateTime="2025-11-02T00:00:00Z")
    event = thread("e", "2025-11-01T00:00:00Z", [], messageType="systemEventMessage")
    install(monkeypatch, pages({"value": [deleted, event]}))

    out = await call("list_channel_messages", {"team_id": TEAM, "channel_id": CHANNEL})

    assert [m["id"] for m in out["messages"]] == ["d"]


async def test_channel_window_must_be_in_order(monkeypatch):
    seen = install(monkeypatch, pages())

    out = await call("list_channel_messages", {
        "team_id": TEAM, "channel_id": CHANNEL,
        "created_after": "2025-12-01T00:00:00Z", "created_before": "2025-11-01T00:00:00Z",
    })

    assert out["error_code"] == "invalid_argument"
    assert seen.requests == []


async def test_channel_replies_link_for_another_message_is_an_error(monkeypatch):
    root = thread("r1", "2025-11-01T00:00:00Z", [])
    root["replies@odata.nextLink"] = f"{THREADS}/zz/replies"
    install(monkeypatch, pages({"value": [root]}))

    out = await call("list_channel_messages", {"team_id": TEAM, "channel_id": CHANNEL})

    assert out["error_code"] == "upstream_error"


async def test_channel_cursor_from_another_channel_is_refused(monkeypatch):
    install(monkeypatch, pages(
        {"value": [thread("r1", "2025-11-10T00:00:00Z", [])], "@odata.nextLink": f"{THREADS}?$skiptoken=p2"},
    ))
    first = await call("list_channel_messages", {"team_id": TEAM, "channel_id": CHANNEL})
    seen = install(monkeypatch, pages())

    out = await call("list_channel_messages", {
        "team_id": TEAM, "channel_id": "19:other@thread.tacv2", "cursor": first["next_cursor"],
    })

    assert out["error_code"] == "invalid_cursor"
    assert seen.requests == []


def channel_cursor(**state) -> str:
    base = {"phase": "main", "link": None, "pending": [], "reply": None,
            "since": "2025-11-01T00:00:00Z", "size": 20, "system": False}
    raw = json.dumps({**base, **state}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize("state", [
    {"link": "https://evil.example/v1.0/teams/team-1/channels/19:chan@thread.tacv2/messages"},
    {"reply": {"id": "r1", "link": "https://evil.example/x"}},
    {"reply": {"id": "r1", "link": f"{THREADS}/r2/replies?$skiptoken=x"}},
    {"pending": ["../me"]},
    {"phase": "somewhere"},
])
async def test_forged_channel_cursor_never_reaches_the_network(monkeypatch, state):
    seen = install(monkeypatch, pages())

    out = await call("list_channel_messages", {
        "team_id": TEAM, "channel_id": CHANNEL, "cursor": channel_cursor(**state),
    })

    assert out["error_code"] == "invalid_cursor"
    assert seen.requests == []
# -- list_channel_message_replies ---------------------------------------------


async def test_replies_continue_from_their_cursor(monkeypatch):
    replies_link = f"{THREADS}/r1/replies?$skiptoken=r"
    seen = install(monkeypatch, pages(
        {"value": [message("b", "2025-11-21T00:00:00Z", replyToId="r1")],
         "@odata.nextLink": replies_link},
        {"value": [message("c", "2025-11-22T00:00:00Z", replyToId="r1")]},
    ))
    args = {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "r1"}

    page1 = await call("list_channel_message_replies", args)
    page2 = await call("list_channel_message_replies", {**args, "cursor": page1["next_cursor"]})

    assert str(seen.requests[1].url) == str(httpx.URL(replies_link))
    assert [r["id"] for r in page1["replies"] + page2["replies"]] == ["b", "c"]
    assert page2["complete"] is True


async def test_replies_cursor_for_another_thread_is_refused(monkeypatch):
    install(monkeypatch, pages(
        {"value": [], "@odata.nextLink": f"{THREADS}/r1/replies?$skiptoken=r"},
    ))
    args = {"team_id": TEAM, "channel_id": CHANNEL}
    first = await call("list_channel_message_replies", {**args, "message_id": "r1"})
    seen = install(monkeypatch, pages())

    out = await call("list_channel_message_replies", {
        **args, "message_id": "r2", "cursor": first["next_cursor"],
    })

    assert out["error_code"] == "invalid_cursor"
    assert seen.requests == []


async def test_replies_first_page_asks_graph_directly(monkeypatch):
    seen = install(monkeypatch, pages({"value": [message("a", "2025-11-20T00:00:00Z", replyToId="r1")]}))

    out = await call("list_channel_message_replies", {
        "team_id": TEAM, "channel_id": CHANNEL, "message_id": "r1", "page_size": 99,
    })

    assert seen.requests[0].url.path == f"/v1.0/teams/{TEAM}/channels/{CHANNEL}/messages/r1/replies"
    assert seen.requests[0].url.params["$top"] == "50"
    assert [r["id"] for r in out["replies"]] == ["a"]
