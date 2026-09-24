"""The linear connector.

Ported from the v1 mixin form by a codemod.

14 tools, 9 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_output_schema, declare_schema

SERVER_NAME = "linear"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

import os
import re
from typing import Any
import httpx
import sys
from pathlib import Path
from .tools._client import ISSUE_FIELDS
from .tools._client import PROJECT_FIELDS


LINEAR_GRAPHQL_URL = os.getenv("LINEAR_GRAPHQL_URL", "https://api.linear.app/graphql")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")

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

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_STATE_TYPES = {"triage", "backlog", "unstarted", "started", "completed", "canceled"}

def build_issue_filter(
    team: str | None = None,
    assignee: str | None = None,
    state: str | None = None,
    state_type: str | None = None,
    project: str | None = None,
    label: str | None = None,
    priority: int | None = None,
    query: str | None = None,
    updated_since: str | None = None,
) -> dict[str, Any]:
    """Build a Linear IssueFilter from human-friendly filter values."""
    f: dict[str, Any] = {}
    if team:
        if is_uuid(team):
            f["team"] = {"id": {"eq": team}}
        else:
            f["team"] = {
                "or": [
                    {"key": {"eqIgnoreCase": team}},
                    {"name": {"eqIgnoreCase": team}},
                ]
            }
    if assignee:
        lowered = assignee.strip().lower()
        if lowered == "me":
            f["assignee"] = {"isMe": {"eq": True}}
        elif lowered in {"unassigned", "none", "nobody"}:
            f["assignee"] = {"null": True}
        elif is_uuid(assignee):
            f["assignee"] = {"id": {"eq": assignee}}
        elif "@" in assignee:
            f["assignee"] = {"email": {"eqIgnoreCase": assignee}}
        else:
            f["assignee"] = {
                "or": [
                    {"name": {"eqIgnoreCase": assignee}},
                    {"displayName": {"eqIgnoreCase": assignee}},
                ]
            }
    state_filter: dict[str, Any] = {}
    if state:
        state_filter["name"] = {"eqIgnoreCase": state}
    if state_type:
        state_filter["type"] = {"eq": state_type}
    if state_filter:
        f["state"] = state_filter
    if project:
        if is_uuid(project):
            f["project"] = {"id": {"eq": project}}
        else:
            f["project"] = {"name": {"eqIgnoreCase": project}}
    if label:
        f["labels"] = {"some": {"name": {"eqIgnoreCase": label}}}
    if priority is not None:
        f["priority"] = {"eq": priority}
    if query:
        f["or"] = [
            {"title": {"containsIgnoreCase": query}},
            {"description": {"containsIgnoreCase": query}},
        ]
    if updated_since:
        f["updatedAt"] = {"gte": updated_since}
    return f



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: GraphQL mutation
@server.tool(
    name='create_comment',
    description="Add a comment to a Linear issue. The body is markdown — @mentions, code blocks, and links are supported.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'issue': {'type': 'string',
                              'description': "Issue identifier ('ENG-123') or UUID"},
                    'body': {'type': 'string', 'description': 'Comment body in markdown'}},
     'required': ['issue', 'body']})
@declare_output_schema({'properties': {'comment': {'description': 'The created comment: id, body, url, created_at',
                                                   'type': 'object'}},
                        'type': 'object'})
async def create_comment(credentials: dict[str, str],
    issue: str,
    body: str,
) -> dict[str, Any]:
    if not body or not body.strip():
        return {"error": "body is required"}

    issue_id, error = await resolve_issue_id(credentials, issue)
    if error:
        return {"error": error}

    data, error = await gql(
        credentials,
        """
        mutation($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment {
              id body url createdAt
              user { name displayName email }
            }
          }
        }
        """,
        {"input": {"issueId": issue_id, "body": body}},
    )
    if error:
        return {"error": error}
    payload = data["commentCreate"]
    if not payload.get("success"):
        return {"error": "Linear reported the comment creation failed."}
    return {"comment": comment_to_dict(payload["comment"])}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: GraphQL mutation
@server.tool(
    name='create_issue',
    description="Create a Linear issue. Only `team` and `title` are required. All references are human-friendly: team key or name, assignee email/name/'me', state name, label names, project name, parent issue identifier. Description is markdown.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team': {'type': 'string',
                             'description': "Team key ('ENG'), name, or UUID"},
                    'title': {'type': 'string', 'description': 'Issue title'},
                    'description': {'type': 'string',
                                    'description': 'Issue description in markdown'},
                    'assignee': {'type': 'string',
                                 'description': "Assignee email, name, or 'me'"},
                    'state': {'type': 'string',
                              'description': "Workflow state name, e.g. 'Todo', 'In "
                                             "Progress'. Defaults to the team's default "
                                             'state.'},
                    'priority': {'type': ['integer', 'string'],
                                 'description': '0/none, 1/urgent, 2/high, 3/medium, '
                                                '4/low'},
                    'labels': {'type': 'array',
                               'items': {'type': 'string'},
                               'description': "Label names, e.g. ['Bug', 'Backend']"},
                    'project': {'type': 'string', 'description': 'Project name or UUID'},
                    'due_date': {'type': 'string', 'description': 'Due date as YYYY-MM-DD'},
                    'parent': {'type': 'string',
                               'description': "Parent issue identifier ('ENG-123') to "
                                              'create a sub-issue'},
                    'estimate': {'type': 'integer',
                                 'description': "Estimate in the team's point scale"}},
     'required': ['team', 'title']})
@declare_output_schema({'properties': {'issue': {'description': 'The created issue (normalized), including identifier '
                                                                'and url',
                                                 'type': 'object'}},
                        'type': 'object'})
async def create_issue(credentials: dict[str, str],
    team: str,
    title: str,
    description: str | None = None,
    assignee: str | None = None,
    state: str | None = None,
    priority: int | str | None = None,
    labels: list[str] | None = None,
    project: str | None = None,
    due_date: str | None = None,
    parent: str | None = None,
    estimate: int | None = None,
) -> dict[str, Any]:
    if not title or not title.strip():
        return {"error": "title is required"}
    if due_date and not _DATE_RE.match(due_date):
        return {"error": "due_date must be in YYYY-MM-DD format"}
    priority_value, priority_error = parse_priority(priority)
    if priority_error:
        return {"error": priority_error}

    team_obj, error = await resolve_team(credentials, team)
    if error:
        return {"error": error}

    input_data: dict[str, Any] = {"teamId": team_obj["id"], "title": title.strip()}
    if description:
        input_data["description"] = description
    if priority_value is not None:
        input_data["priority"] = priority_value
    if due_date:
        input_data["dueDate"] = due_date
    if estimate is not None:
        input_data["estimate"] = estimate

    if assignee:
        user, error = await resolve_user(credentials, assignee)
        if error:
            return {"error": error}
        input_data["assigneeId"] = user["id"]
    if state:
        state_obj, error = await resolve_state(credentials, team_obj["id"], state)
        if error:
            return {"error": error}
        input_data["stateId"] = state_obj["id"]
    if labels:
        label_ids, error = await resolve_labels(credentials, team_obj["id"], labels)
        if error:
            return {"error": error}
        input_data["labelIds"] = label_ids
    if project:
        project_obj, error = await resolve_project(credentials, project)
        if error:
            return {"error": error}
        input_data["projectId"] = project_obj["id"]
    if parent:
        parent_id, error = await resolve_issue_id(credentials, parent)
        if error:
            return {"error": error}
        input_data["parentId"] = parent_id

    data, error = await gql(
        credentials,
        f"""
        mutation($input: IssueCreateInput!) {{
          issueCreate(input: $input) {{
            success
            issue {{ {ISSUE_FIELDS} }}
          }}
        }}
        """,
        {"input": input_data},
    )
    if error:
        return {"error": error}
    payload = data["issueCreate"]
    if not payload.get("success"):
        return {"error": "Linear reported the issue creation failed."}
    return {"issue": issue_to_dict(payload["issue"])}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: GraphQL mutation
@server.tool(
    name='create_project',
    description="Create a Linear project attached to one or more teams. Teams accept keys or names; lead accepts email/name/'me'.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'name': {'type': 'string', 'description': 'Project name'},
                    'teams': {'type': 'array',
                              'items': {'type': 'string'},
                              'description': 'Team keys or names the project belongs to, '
                                             "e.g. ['ENG']"},
                    'description': {'type': 'string',
                                    'description': 'Project description in markdown'},
                    'lead': {'type': 'string',
                             'description': "Project lead: email, name, or 'me'"},
                    'start_date': {'type': 'string',
                                   'description': 'Start date as YYYY-MM-DD'},
                    'target_date': {'type': 'string',
                                    'description': 'Target date as YYYY-MM-DD'}},
     'required': ['name', 'teams']})
@declare_output_schema({'properties': {'project': {'description': 'The created project (normalized), including url',
                                                   'type': 'object'}},
                        'type': 'object'})
async def create_project(credentials: dict[str, str],
    name: str,
    teams: list[str],
    description: str | None = None,
    lead: str | None = None,
    start_date: str | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    if not name or not name.strip():
        return {"error": "name is required"}
    if not teams:
        return {"error": "teams is required — pass at least one team key or name"}
    for label, value in (("start_date", start_date), ("target_date", target_date)):
        if value and not _DATE_RE.match(value):
            return {"error": f"{label} must be in YYYY-MM-DD format"}

    team_ids: list[str] = []
    for team in teams:
        team_obj, error = await resolve_team(credentials, team)
        if error:
            return {"error": error}
        team_ids.append(team_obj["id"])

    input_data: dict[str, Any] = {"name": name.strip(), "teamIds": team_ids}
    if description:
        input_data["description"] = description
    if start_date:
        input_data["startDate"] = start_date
    if target_date:
        input_data["targetDate"] = target_date
    if lead:
        user, error = await resolve_user(credentials, lead)
        if error:
            return {"error": error}
        input_data["leadId"] = user["id"]

    data, error = await gql(
        credentials,
        f"""
        mutation($input: ProjectCreateInput!) {{
          projectCreate(input: $input) {{
            success
            project {{ {PROJECT_FIELDS} }}
          }}
        }}
        """,
        {"input": input_data},
    )
    if error:
        return {"error": error}
    payload = data["projectCreate"]
    if not payload.get("success"):
        return {"error": "Linear reported the project creation failed."}
    return {"project": project_to_dict(payload["project"])}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: fetch_issue_node() issues a GraphQL `query`, never a `mutation`.
# Every Linear call is an HTTP POST, so the operation keyword is the
# only thing that separates a read from a write here.
@server.tool(
    name='get_issue',
    description="Get a single Linear issue by identifier (e.g. 'ENG-123') or UUID, including its comments and sub-issues.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'issue': {'type': 'string',
                              'description': "Issue identifier ('ENG-123') or UUID"},
                    'include_comments': {'type': 'boolean',
                                         'description': "Include the issue's comments "
                                                        '(default true)',
                                         'default': True}},
     'required': ['issue']})
@declare_output_schema({'properties': {'comments': {'description': 'Comments oldest-first: body, author, created_at, '
                                                                   'url',
                                                    'items': {'type': 'object'},
                                                    'type': 'array'},
                                       'issue': {'description': 'Normalized issue', 'type': 'object'},
                                       'sub_issues': {'description': 'Child issues: identifier, title, state, '
                                                                     'assignee',
                                                      'items': {'type': 'object'},
                                                      'type': 'array'}},
                        'type': 'object'})
async def get_issue(credentials: dict[str, str],
    issue: str,
    include_comments: bool = True,
) -> dict[str, Any]:
    selection = f"""
    {ISSUE_FIELDS}
    children(first: 50) {{
      nodes {{
        identifier title
        state {{ name }}
        assignee {{ displayName name }}
      }}
    }}
    comments(first: 50) {{
      nodes {{
        id body url createdAt
        user {{ name displayName email }}
      }}
    }}
    """
    node, error = await fetch_issue_node(credentials, issue, selection)
    if error:
        return {"error": error}

    result: dict[str, Any] = {"issue": issue_to_dict(node)}
    result["sub_issues"] = [
        {
            "identifier": child["identifier"],
            "title": child["title"],
            "state": (child.get("state") or {}).get("name"),
            "assignee": (child.get("assignee") or {}).get("displayName")
            or (child.get("assignee") or {}).get("name"),
        }
        for child in (node.get("children") or {}).get("nodes", [])
    ]
    if include_comments:
        comments = [comment_to_dict(c) for c in (node.get("comments") or {}).get("nodes", [])]
        # Linear returns comments newest-first; oldest-first reads as a thread.
        result["comments"] = list(reversed(comments))
    return result



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query { viewer }
@server.tool(
    name='get_viewer',
    description="Get the user the API key belongs to (name, email, id) and their organization. Use to verify credentials or resolve 'me'.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'email': {'type': 'string'},
                                       'id': {'type': 'string'},
                                       'name': {'type': 'string'},
                                       'organization': {'type': 'string'}},
                        'type': 'object'})
async def get_viewer(credentials: dict[str, str]) -> dict[str, Any]:
    data, error = await gql(
        credentials,
        "{ viewer { id name displayName email organization { name urlKey } } }",
    )
    if error:
        return {"error": error}
    viewer = data["viewer"]
    return {
        "id": viewer["id"],
        "name": viewer.get("displayName") or viewer.get("name"),
        "email": viewer.get("email"),
        "organization": (viewer.get("organization") or {}).get("name"),
    }



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: fetch_issue_node() with a comments selection: a GraphQL `query`.
@server.tool(
    name='list_comments',
    description="List the comments on a Linear issue, oldest-first (thread order). get_issue already includes comments — use this when you only need the discussion or more than 50 comments.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'issue': {'type': 'string',
                              'description': "Issue identifier ('ENG-123') or UUID"},
                    'limit': {'type': 'integer',
                              'description': 'Max comments to return (default 50, max 100)',
                              'default': 50}},
     'required': ['issue']})
@declare_output_schema({'properties': {'comments': {'description': 'Comments oldest-first: body, author, created_at, '
                                                                   'url',
                                                    'items': {'type': 'object'},
                                                    'type': 'array'},
                                       'count': {'type': 'integer'}},
                        'type': 'object'})
async def list_comments(credentials: dict[str, str],
    issue: str,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    node, error = await fetch_issue_node(
        credentials,
        issue,
        f"""
        comments(first: {limit}) {{
          nodes {{
            id body url createdAt
            user {{ name displayName email }}
          }}
        }}
        """,
    )
    if error:
        return {"error": error}

    comments = [comment_to_dict(c) for c in (node.get("comments") or {}).get("nodes", [])]
    # Linear returns comments newest-first; oldest-first reads as a thread.
    comments.reverse()
    return {"comments": comments, "count": len(comments)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query
@server.tool(
    name='list_issue_statuses',
    description="List a team's workflow states (e.g. Backlog, Todo, In Progress, Done) in board order. Use these names for the `state` parameter of list/create/update_issue.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team': {'type': 'string',
                             'description': "Team key ('ENG'), name, or UUID"}},
     'required': ['team']})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'statuses': {'description': 'States in board order with: id, name, type '
                                                                   '(triage/backlog/unstarted/started/completed/canceled)',
                                                    'items': {'type': 'object'},
                                                    'type': 'array'}},
                        'type': 'object'})
async def list_issue_statuses(credentials: dict[str, str], team: str) -> dict[str, Any]:
    team_filter: dict[str, Any]
    if is_uuid(team):
        team_filter = {"id": {"eq": team}}
    else:
        team_filter = {
            "or": [
                {"key": {"eqIgnoreCase": team}},
                {"name": {"eqIgnoreCase": team}},
            ]
        }
    data, error = await gql(
        credentials,
        """
        query($filter: WorkflowStateFilter) {
          workflowStates(first: 100, filter: $filter) {
            nodes { id name type position }
          }
        }
        """,
        {"filter": {"team": team_filter}},
    )
    if error:
        return {"error": error}
    states = sorted(data["workflowStates"]["nodes"], key=lambda s: s.get("position") or 0)
    statuses = [{"id": s["id"], "name": s["name"], "type": s["type"]} for s in states]
    if not statuses:
        return {"error": f"No workflow states found — team '{team}' may not exist."}
    return {"statuses": statuses, "count": len(statuses)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query
@server.tool(
    name='list_issues',
    description="List Linear issues, newest-updated first, with optional filters. All filters accept human-friendly values: team key or name ('ENG'), assignee email/name/'me'/'unassigned', state name ('In Progress'), project name, label name. Use `query` for a text search across title and description. Returns normalized issues plus a next_cursor for pagination.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team': {'type': 'string',
                             'description': "Filter by team key ('ENG'), name, or UUID"},
                    'assignee': {'type': 'string',
                                 'description': "Filter by assignee: email, name, 'me' "
                                                "(the API key's user), or 'unassigned'"},
                    'state': {'type': 'string',
                              'description': "Filter by workflow state name, e.g. 'In "
                                             "Progress', 'Done'"},
                    'state_type': {'type': 'string',
                                   'enum': ['backlog',
                                            'canceled',
                                            'completed',
                                            'started',
                                            'triage',
                                            'unstarted'],
                                   'description': 'Filter by state category — use '
                                                  "'started' for all in-progress issues "
                                                  "regardless of the team's custom state "
                                                  'names'},
                    'project': {'type': 'string',
                                'description': 'Filter by project name or UUID'},
                    'label': {'type': 'string',
                              'description': "Filter by label name, e.g. 'Bug'"},
                    'priority': {'type': ['integer', 'string'],
                                 'description': 'Filter by priority: 0/none, 1/urgent, '
                                                '2/high, 3/medium, 4/low'},
                    'query': {'type': 'string',
                              'description': 'Text search across issue titles and '
                                             'descriptions'},
                    'updated_since': {'type': 'string',
                                      'description': 'Only issues updated at or after this '
                                                     'ISO 8601 timestamp, e.g. '
                                                     "'2026-07-01T00:00:00Z'"},
                    'limit': {'type': 'integer',
                              'description': 'Max issues to return (default 25, max 100)',
                              'default': 25},
                    'cursor': {'type': 'string',
                               'description': "Pagination cursor from a previous call's "
                                              'next_cursor'}}})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'issues': {'description': 'Issues with: id, identifier, title, description, '
                                                                 'state, state_type, priority, priority_label, '
                                                                 'assignee, team, project, labels, due_date, url, '
                                                                 'created_at, updated_at',
                                                  'items': {'type': 'object'},
                                                  'type': 'array'},
                                       'next_cursor': {'description': 'Pass as `cursor` to fetch the next page; null '
                                                                      'when done',
                                                       'type': ['string', 'null']}},
                        'type': 'object'})
async def list_issues(credentials: dict[str, str],
    team: str | None = None,
    assignee: str | None = None,
    state: str | None = None,
    state_type: str | None = None,
    project: str | None = None,
    label: str | None = None,
    priority: int | str | None = None,
    query: str | None = None,
    updated_since: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> dict[str, Any]:
    priority_value, priority_error = parse_priority(priority)
    if priority_error:
        return {"error": priority_error}
    if state_type and state_type not in _STATE_TYPES:
        return {"error": f"state_type must be one of: {sorted(_STATE_TYPES)}"}
    limit = max(1, min(limit, 100))

    issue_filter = build_issue_filter(
        team=team,
        assignee=assignee,
        state=state,
        state_type=state_type,
        project=project,
        label=label,
        priority=priority_value,
        query=query,
        updated_since=updated_since,
    )

    data, error = await gql(
        credentials,
        f"""
        query($filter: IssueFilter, $first: Int, $after: String) {{
          issues(
            filter: $filter, first: $first, after: $after, orderBy: updatedAt
          ) {{
            nodes {{ {ISSUE_FIELDS} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """,
        {"filter": issue_filter or None, "first": limit, "after": cursor},
    )
    if error:
        return {"error": error}

    connection = data["issues"]
    issues = [issue_to_dict(node) for node in connection["nodes"]]
    page = connection["pageInfo"]
    return {
        "issues": issues,
        "count": len(issues),
        "next_cursor": page["endCursor"] if page["hasNextPage"] else None,
    }



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query { issueLabels }
@server.tool(
    name='list_labels',
    description="List issue labels (workspace-level and team-scoped). Use these names for the `labels` parameter of create/update_issue.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team': {'type': 'string',
                             'description': 'Optional team key/name/UUID — limits to that '
                                            "team's labels plus workspace-level labels"}}})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'labels': {'description': 'Labels with: id, name, color, team (null = '
                                                                 'workspace-level)',
                                                  'items': {'type': 'object'},
                                                  'type': 'array'}},
                        'type': 'object'})
async def list_labels(credentials: dict[str, str], team: str | None = None
) -> dict[str, Any]:
    data, error = await gql(
        credentials,
        "{ issueLabels(first: 250) { nodes { id name color team { key name } } } }",
    )
    if error:
        return {"error": error}
    needle = (team or "").strip().lower()
    labels = []
    for lbl in data["issueLabels"]["nodes"]:
        lbl_team = lbl.get("team") or {}
        if needle and lbl_team:
            if needle not in {
                lbl_team.get("key", "").lower(),
                lbl_team.get("name", "").lower(),
            }:
                continue
        labels.append(
            {
                "id": lbl["id"],
                "name": lbl["name"],
                "color": lbl.get("color"),
                "team": lbl_team.get("key"),
            }
        )
    return {"labels": labels, "count": len(labels)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query
@server.tool(
    name='list_projects',
    description="List Linear projects with optional team and text filters. Returns normalized projects with state, progress (0-1), lead, teams, and target date.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'team': {'type': 'string',
                             'description': 'Only projects belonging to this team key, '
                                            'name, or UUID'},
                    'query': {'type': 'string',
                              'description': 'Text search across project names'},
                    'limit': {'type': 'integer',
                              'description': 'Max projects to return (default 50, max 100)',
                              'default': 50}}})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'projects': {'description': 'Projects with: id, name, description, state, '
                                                                   'progress, lead, teams, start_date, target_date, '
                                                                   'url',
                                                    'items': {'type': 'object'},
                                                    'type': 'array'}},
                        'type': 'object'})
async def list_projects(credentials: dict[str, str],
    team: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))

    project_filter: dict[str, Any] = {}
    if team:
        if is_uuid(team):
            team_clause: dict[str, Any] = {"id": {"eq": team}}
        else:
            team_clause = {
                "or": [
                    {"key": {"eqIgnoreCase": team}},
                    {"name": {"eqIgnoreCase": team}},
                ]
            }
        project_filter["accessibleTeams"] = {"some": team_clause}
    if query:
        project_filter["name"] = {"containsIgnoreCase": query}

    data, error = await gql(
        credentials,
        f"""
        query($filter: ProjectFilter, $first: Int) {{
          projects(filter: $filter, first: $first) {{
            nodes {{ {PROJECT_FIELDS} }}
          }}
        }}
        """,
        {"filter": project_filter or None, "first": limit},
    )
    if error:
        return {"error": error}
    projects = [project_to_dict(node) for node in data["projects"]["nodes"]]
    return {"projects": projects, "count": len(projects)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query { teams }
@server.tool(
    name='list_teams',
    description="List the workspace's teams with their keys (e.g. 'ENG'). Team keys prefix issue identifiers ('ENG-123') and are accepted by every tool's `team` parameter.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object', 'properties': {}})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'teams': {'description': 'Teams with: id, key, name, description, issue_count',
                                                 'items': {'type': 'object'},
                                                 'type': 'array'}},
                        'type': 'object'})
async def list_teams(credentials: dict[str, str]) -> dict[str, Any]:
    data, error = await gql(
        credentials,
        "{ teams(first: 100) { nodes { id key name description issueCount } } }",
    )
    if error:
        return {"error": error}
    teams = [
        {
            "id": t["id"],
            "key": t["key"],
            "name": t["name"],
            "description": t.get("description"),
            "issue_count": t.get("issueCount"),
        }
        for t in data["teams"]["nodes"]
    ]
    return {"teams": teams, "count": len(teams)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GraphQL query { users }
@server.tool(
    name='list_users',
    description="List the workspace's users (name, email, active). Use to find the right assignee value — every tool's `assignee`/`lead` parameter accepts these emails or names.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'query': {'type': 'string',
                              'description': 'Filter by name/email substring '
                                             '(case-insensitive)'},
                    'include_inactive': {'type': 'boolean',
                                         'description': 'Include deactivated users '
                                                        '(default false)',
                                         'default': False}}})
@declare_output_schema({'properties': {'count': {'type': 'integer'},
                                       'users': {'description': 'Users with: id, name, email, active',
                                                 'items': {'type': 'object'},
                                                 'type': 'array'}},
                        'type': 'object'})
async def list_users(credentials: dict[str, str],
    query: str | None = None,
    include_inactive: bool = False,
) -> dict[str, Any]:
    data, error = await gql(
        credentials,
        "{ users(first: 250) { nodes { id name displayName email active } } }",
    )
    if error:
        return {"error": error}
    needle = (query or "").strip().lower()
    users = []
    for u in data["users"]["nodes"]:
        if not include_inactive and not u.get("active"):
            continue
        name = u.get("displayName") or u.get("name") or ""
        haystack = f"{u.get('name', '')} {name} {u.get('email', '')}".lower()
        if needle and needle not in haystack:
            continue
        users.append(
            {
                "id": u["id"],
                "name": name,
                "email": u.get("email"),
                "active": u.get("active"),
            }
        )
    return {"users": users, "count": len(users)}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: GraphQL mutation issueUpdate. labelIds replaces the whole
# label set, and assignee='unassigned' or an empty dueDate writes null.
# Evidence: GraphQL mutation
@server.tool(
    name='update_issue',
    description="Update a Linear issue — change state (e.g. move to 'Done'), reassign, retitle, edit description, set priority/labels/project/due date. Only the fields you pass are changed. References are human-friendly (state name, assignee email/name/'me', label names). Pass assignee='unassigned' to clear the assignee.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'issue': {'type': 'string',
                              'description': "Issue identifier ('ENG-123') or UUID"},
                    'title': {'type': 'string', 'description': 'New title'},
                    'description': {'type': 'string',
                                    'description': 'New description in markdown (replaces '
                                                   'the old one)'},
                    'state': {'type': 'string',
                              'description': "New workflow state name, e.g. 'In Progress', "
                                             "'Done'"},
                    'assignee': {'type': 'string',
                                 'description': "New assignee email/name/'me', or "
                                                "'unassigned' to clear"},
                    'priority': {'type': ['integer', 'string'],
                                 'description': '0/none, 1/urgent, 2/high, 3/medium, '
                                                '4/low'},
                    'labels': {'type': 'array',
                               'items': {'type': 'string'},
                               'description': "Label names — replaces the issue's current "
                                              'labels'},
                    'project': {'type': 'string',
                                'description': 'Project name or UUID to move the issue '
                                               'into'},
                    'due_date': {'type': 'string',
                                 'description': 'Due date as YYYY-MM-DD, or empty string '
                                                'to clear'},
                    'estimate': {'type': 'integer',
                                 'description': "Estimate in the team's point scale"}},
     'required': ['issue']})
@declare_output_schema({'properties': {'issue': {'description': 'The updated issue (normalized)', 'type': 'object'}},
                        'type': 'object'})
async def update_issue(credentials: dict[str, str],
    issue: str,
    title: str | None = None,
    description: str | None = None,
    state: str | None = None,
    assignee: str | None = None,
    priority: int | str | None = None,
    labels: list[str] | None = None,
    project: str | None = None,
    due_date: str | None = None,
    estimate: int | None = None,
) -> dict[str, Any]:
    if due_date and not _DATE_RE.match(due_date):
        return {"error": "due_date must be in YYYY-MM-DD format (or '' to clear)"}
    priority_value, priority_error = parse_priority(priority)
    if priority_error:
        return {"error": priority_error}

    # Resolve the issue first — state/label resolution needs its team.
    node, error = await fetch_issue_node(credentials, issue, "id team { id }")
    if error:
        return {"error": error}
    issue_id = node["id"]
    team_id = (node.get("team") or {}).get("id")

    input_data: dict[str, Any] = {}
    if title is not None:
        input_data["title"] = title
    if description is not None:
        input_data["description"] = description
    if priority_value is not None:
        input_data["priority"] = priority_value
    if due_date is not None:
        input_data["dueDate"] = due_date or None
    if estimate is not None:
        input_data["estimate"] = estimate

    if state:
        state_obj, error = await resolve_state(credentials, team_id, state)
        if error:
            return {"error": error}
        input_data["stateId"] = state_obj["id"]
    if assignee:
        if assignee.strip().lower() in {"unassigned", "none", "nobody"}:
            input_data["assigneeId"] = None
        else:
            user, error = await resolve_user(credentials, assignee)
            if error:
                return {"error": error}
            input_data["assigneeId"] = user["id"]
    if labels is not None:
        label_ids, error = await resolve_labels(credentials, team_id, labels)
        if error:
            return {"error": error}
        input_data["labelIds"] = label_ids
    if project:
        project_obj, error = await resolve_project(credentials, project)
        if error:
            return {"error": error}
        input_data["projectId"] = project_obj["id"]

    if not input_data:
        return {"error": "No fields to update — pass at least one field to change."}

    data, error = await gql(
        credentials,
        f"""
        mutation($id: String!, $input: IssueUpdateInput!) {{
          issueUpdate(id: $id, input: $input) {{
            success
            issue {{ {ISSUE_FIELDS} }}
          }}
        }}
        """,
        {"id": issue_id, "input": input_data},
    )
    if error:
        return {"error": error}
    payload = data["issueUpdate"]
    if not payload.get("success"):
        return {"error": "Linear reported the issue update failed."}
    return {"issue": issue_to_dict(payload["issue"])}



# credential_headers: ['X-Linear-Api-Key']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: GraphQL mutation projectUpdate: overwrites name,
# description and leadId on an existing project.
# Evidence: GraphQL mutation
@server.tool(
    name='update_project',
    description="Update a Linear project by name or UUID — rename, edit description, change lead, or move start/target dates. Only the fields you pass are changed.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Linear-Api-Key'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'project': {'type': 'string', 'description': 'Project name or UUID'},
                    'name': {'type': 'string', 'description': 'New project name'},
                    'description': {'type': 'string',
                                    'description': 'New description in markdown (replaces '
                                                   'the old one)'},
                    'lead': {'type': 'string',
                             'description': "New lead: email, name, or 'me'"},
                    'start_date': {'type': 'string',
                                   'description': 'Start date as YYYY-MM-DD'},
                    'target_date': {'type': 'string',
                                    'description': 'Target date as YYYY-MM-DD'}},
     'required': ['project']})
@declare_output_schema({'properties': {'project': {'description': 'The updated project (normalized)',
                                                   'type': 'object'}},
                        'type': 'object'})
async def update_project(credentials: dict[str, str],
    project: str,
    name: str | None = None,
    description: str | None = None,
    lead: str | None = None,
    start_date: str | None = None,
    target_date: str | None = None,
) -> dict[str, Any]:
    for label, value in (("start_date", start_date), ("target_date", target_date)):
        if value and not _DATE_RE.match(value):
            return {"error": f"{label} must be in YYYY-MM-DD format"}

    project_obj, error = await resolve_project(credentials, project)
    if error:
        return {"error": error}

    input_data: dict[str, Any] = {}
    if name is not None:
        input_data["name"] = name
    if description is not None:
        input_data["description"] = description
    if start_date:
        input_data["startDate"] = start_date
    if target_date:
        input_data["targetDate"] = target_date
    if lead:
        user, error = await resolve_user(credentials, lead)
        if error:
            return {"error": error}
        input_data["leadId"] = user["id"]

    if not input_data:
        return {"error": "No fields to update — pass at least one field to change."}

    data, error = await gql(
        credentials,
        f"""
        mutation($id: String!, $input: ProjectUpdateInput!) {{
          projectUpdate(id: $id, input: $input) {{
            success
            project {{ {PROJECT_FIELDS} }}
          }}
        }}
        """,
        {"id": project_obj["id"], "input": input_data},
    )
    if error:
        return {"error": error}
    payload = data["projectUpdate"]
    if not payload.get("success"):
        return {"error": "Linear reported the project update failed."}
    return {"project": project_to_dict(payload["project"])}
