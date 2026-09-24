"""Shared Linear GraphQL client: request helper, resolvers, and serializers.

All tools talk to Linear's GraphQL API (https://api.linear.app/graphql) using a
personal API key. Inputs are human-friendly — issues by identifier ("ENG-123"),
teams by key or name, users by email/name/"me", states and labels by name —
and are resolved to Linear UUIDs here. Outputs are normalized flat dicts so
every tool returns issues/projects/comments in the same shape.
"""

import os
import re
from typing import Any

import httpx

LINEAR_GRAPHQL_URL = os.getenv("LINEAR_GRAPHQL_URL", "https://api.linear.app/graphql")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Issue identifiers look like "ENG-123" (team key, dash, number).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")

# Linear priorities: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low.
PRIORITY_NAMES = {
    "none": 0,
    "no priority": 0,
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}

ISSUE_FIELDS = """
id
identifier
title
description
priority
priorityLabel
estimate
url
dueDate
createdAt
updatedAt
state { id name type }
assignee { id name displayName email }
creator { id name }
team { id key name }
project { id name }
labels { nodes { id name } }
parent { id identifier title }
"""

PROJECT_FIELDS = """
id
name
description
state
progress
startDate
targetDate
url
createdAt
updatedAt
lead { id name email }
teams { nodes { id key name } }
"""


def is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))


def is_issue_identifier(value: str) -> bool:
    return bool(_IDENTIFIER_RE.match(value or ""))


def get_api_key(credentials: dict[str, str]) -> str:
    return credentials.get("linear_api_key") or os.getenv("LINEAR_API_KEY", "")


def parse_priority(priority: Any) -> tuple[int | None, str | None]:
    """Accept 0-4 or a name ('urgent', 'high', ...). Returns (value, error)."""
    if priority is None:
        return None, None
    if isinstance(priority, bool):
        return None, "priority must be 0-4 or one of: none, urgent, high, medium, low"
    if isinstance(priority, int):
        if 0 <= priority <= 4:
            return priority, None
        return None, "priority must be between 0 (none) and 4 (low)"
    if isinstance(priority, str):
        name = priority.strip().lower()
        if name.isdigit() and 0 <= int(name) <= 4:
            return int(name), None
        if name in PRIORITY_NAMES:
            return PRIORITY_NAMES[name], None
    return None, "priority must be 0-4 or one of: none, urgent, high, medium, low"


async def gql(
    credentials: dict[str, str],
    query: str,
    variables: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST a GraphQL query to Linear. Returns (data, error) — exactly one is set."""
    api_key = get_api_key(credentials)
    if not api_key:
        return None, (
            "LINEAR_API_KEY not configured. Provide it via credentials or env. "
            "Create a personal API key at linear.app → Settings → Security & access → API keys."
        )

    # Personal API keys are sent raw; OAuth access tokens need a Bearer prefix.
    auth = api_key if api_key.startswith("lin_api_") else f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                LINEAR_GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
    except httpx.ConnectError:
        return None, "Cannot reach the Linear API. Check your internet connection."
    except httpx.TimeoutException:
        return None, "Linear API request timed out. Try again."

    if resp.status_code in (401, 403):
        return None, (
            "Linear rejected the API key (HTTP "
            f"{resp.status_code}). Check the key at linear.app → Settings → API keys."
        )
    if resp.status_code == 429:
        return None, "Linear rate limit exceeded. Try again in a minute."

    try:
        body = resp.json()
    except ValueError:
        return None, f"Linear returned a non-JSON response (HTTP {resp.status_code})."

    if body.get("errors"):
        messages = "; ".join(
            e.get("message", "unknown error") for e in body["errors"] if isinstance(e, dict)
        )
        return None, f"Linear API error: {messages}"

    return body.get("data") or {}, None


# ---------------------------------------------------------------------------
# Resolvers: human-friendly values → Linear UUIDs
# ---------------------------------------------------------------------------


async def resolve_team(
    credentials: dict[str, str], value: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a team by id, key ('ENG'), or name. Returns (team, error)."""
    if is_uuid(value):
        return {"id": value}, None
    data, error = await gql(credentials, "{ teams(first: 100) { nodes { id key name } } }")
    if error:
        return None, error
    teams = data["teams"]["nodes"]
    needle = value.strip().lower()
    for team in teams:
        if team["key"].lower() == needle or team["name"].lower() == needle:
            return team, None
    available = ", ".join(f"{t['key']} ({t['name']})" for t in teams) or "none"
    return None, f"Team '{value}' not found. Available teams: {available}"


async def resolve_user(
    credentials: dict[str, str], value: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a user by id, email, name, or 'me'. Returns (user, error)."""
    if is_uuid(value):
        return {"id": value}, None
    if value.strip().lower() == "me":
        data, error = await gql(credentials, "{ viewer { id name email } }")
        if error:
            return None, error
        return data["viewer"], None
    data, error = await gql(
        credentials,
        "{ users(first: 250) { nodes { id name displayName email active } } }",
    )
    if error:
        return None, error
    needle = value.strip().lower()
    for user in data["users"]["nodes"]:
        if needle in {
            (user.get("email") or "").lower(),
            (user.get("name") or "").lower(),
            (user.get("displayName") or "").lower(),
        }:
            return user, None
    return None, f"User '{value}' not found. Use an email, full name, display name, or 'me'."


async def resolve_state(
    credentials: dict[str, str], team_id: str, name: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a workflow state by name within a team. Returns (state, error)."""
    if is_uuid(name):
        return {"id": name}, None
    data, error = await gql(
        credentials,
        """
        query($teamId: ID) {
          workflowStates(first: 100, filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name type }
          }
        }
        """,
        {"teamId": team_id},
    )
    if error:
        return None, error
    states = data["workflowStates"]["nodes"]
    needle = name.strip().lower()
    for state in states:
        if state["name"].lower() == needle:
            return state, None
    available = ", ".join(s["name"] for s in states) or "none"
    return None, f"State '{name}' not found for this team. Available states: {available}"


async def resolve_labels(
    credentials: dict[str, str], team_id: str | None, names: list[str]
) -> tuple[list[str] | None, str | None]:
    """Resolve label names to ids (team labels + workspace labels). Returns (ids, error)."""
    data, error = await gql(
        credentials,
        "{ issueLabels(first: 250) { nodes { id name team { id } } } }",
    )
    if error:
        return None, error
    labels = data["issueLabels"]["nodes"]
    ids: list[str] = []
    for name in names:
        if is_uuid(name):
            ids.append(name)
            continue
        needle = name.strip().lower()
        # Prefer a label scoped to the issue's team, fall back to workspace-level.
        match = next(
            (
                lbl
                for lbl in labels
                if lbl["name"].lower() == needle
                and (lbl.get("team") or {}).get("id") in (team_id, None)
            ),
            None,
        ) or next((lbl for lbl in labels if lbl["name"].lower() == needle), None)
        if not match:
            return None, f"Label '{name}' not found."
        ids.append(match["id"])
    return ids, None


async def resolve_project(
    credentials: dict[str, str], value: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a project by id or name. Returns (project, error)."""
    if is_uuid(value):
        return {"id": value}, None
    data, error = await gql(credentials, "{ projects(first: 250) { nodes { id name } } }")
    if error:
        return None, error
    needle = value.strip().lower()
    for project in data["projects"]["nodes"]:
        if project["name"].lower() == needle:
            return project, None
    return None, f"Project '{value}' not found."


async def fetch_issue_node(
    credentials: dict[str, str], ref: str, selection: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch one issue by UUID or identifier ('ENG-123') with the given field selection.

    Linear's `issue(id:)` query only resolves UUIDs, so identifiers are looked
    up via a team-key + issue-number filter instead.
    """
    if is_uuid(ref):
        data, error = await gql(
            credentials,
            f"query($id: String!) {{ issue(id: $id) {{ {selection} }} }}",
            {"id": ref},
        )
        if error:
            return None, error
        node = data.get("issue")
        if not node:
            return None, f"Issue '{ref}' not found."
        return node, None

    if not is_issue_identifier(ref):
        return None, (
            f"'{ref}' is not a valid issue reference. "
            "Use the issue identifier (e.g. 'ENG-123') or its UUID."
        )

    team_key, number = ref.rsplit("-", 1)
    data, error = await gql(
        credentials,
        f"""
        query($key: String!, $number: Float!) {{
          issues(
            first: 1
            filter: {{ team: {{ key: {{ eqIgnoreCase: $key }} }}, number: {{ eq: $number }} }}
          ) {{
            nodes {{ {selection} }}
          }}
        }}
        """,
        {"key": team_key, "number": float(number)},
    )
    if error:
        return None, error
    nodes = data["issues"]["nodes"]
    if not nodes:
        return None, (
            f"Issue '{ref}' not found. Check the team key ('{team_key}') with "
            "list_teams and the issue number."
        )
    return nodes[0], None


async def resolve_issue_id(
    credentials: dict[str, str], value: str
) -> tuple[str | None, str | None]:
    """Resolve an issue reference (UUID or identifier like 'ENG-123') to its UUID."""
    if is_uuid(value):
        return value, None
    node, error = await fetch_issue_node(credentials, value, "id")
    if error:
        return None, error
    return node["id"], None


# ---------------------------------------------------------------------------
# Serializers: raw GraphQL nodes → normalized flat dicts
# ---------------------------------------------------------------------------


def issue_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    state = node.get("state") or {}
    assignee = node.get("assignee") or {}
    return {
        "id": node.get("id"),
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        "description": node.get("description"),
        "state": state.get("name"),
        "state_type": state.get("type"),
        "priority": node.get("priority"),
        "priority_label": node.get("priorityLabel"),
        "assignee": assignee.get("displayName") or assignee.get("name"),
        "assignee_email": assignee.get("email"),
        "creator": (node.get("creator") or {}).get("name"),
        "team": (node.get("team") or {}).get("key"),
        "project": (node.get("project") or {}).get("name"),
        "labels": [lbl["name"] for lbl in (node.get("labels") or {}).get("nodes", [])],
        "estimate": node.get("estimate"),
        "due_date": node.get("dueDate"),
        "parent": (node.get("parent") or {}).get("identifier"),
        "url": node.get("url"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
    }


def project_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "description": node.get("description"),
        "state": node.get("state"),
        "progress": node.get("progress"),
        "lead": (node.get("lead") or {}).get("name"),
        "teams": [t["key"] for t in (node.get("teams") or {}).get("nodes", [])],
        "start_date": node.get("startDate"),
        "target_date": node.get("targetDate"),
        "url": node.get("url"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
    }


def comment_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    user = node.get("user") or {}
    return {
        "id": node.get("id"),
        "body": node.get("body"),
        "author": user.get("displayName") or user.get("name"),
        "author_email": user.get("email"),
        "url": node.get("url"),
        "created_at": node.get("createdAt"),
    }
