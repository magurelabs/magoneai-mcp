import importlib
from connectors.linear.server import issue_to_dict, parse_priority
from connectors.linear.server import build_issue_filter
client_mod = importlib.import_module("connectors.linear.server")
issues_mod = importlib.import_module("connectors.linear.server")
from connectors.linear.server import create_issue, list_issues, update_issue

"""Tests for the Linear issue tools: filter building, serialization, handlers."""
import pytest

@pytest.mark.parametrize('value,expected', [(None, None), (0, 0), (4, 4), ('urgent', 1), ('High', 2), ('MEDIUM', 3), ('low', 4), ('none', 0), ('no priority', 0), ('2', 2)])
def test_parse_priority_valid(value, expected):
    result, error = parse_priority(value)
    assert error is None
    assert result == expected

@pytest.mark.parametrize('value', [5, -1, 'critical', True, 3.5])
def test_parse_priority_invalid(value):
    result, error = parse_priority(value)
    assert result is None
    assert error

def test_filter_empty():
    assert build_issue_filter() == {}

def test_filter_team_key_or_name():
    f = build_issue_filter(team='ENG')
    assert f['team'] == {'or': [{'key': {'eqIgnoreCase': 'ENG'}}, {'name': {'eqIgnoreCase': 'ENG'}}]}

def test_filter_team_uuid():
    uuid = '123e4567-e89b-12d3-a456-426614174000'
    assert build_issue_filter(team=uuid)['team'] == {'id': {'eq': uuid}}

def test_filter_assignee_me():
    assert build_issue_filter(assignee='me')['assignee'] == {'isMe': {'eq': True}}

def test_filter_assignee_unassigned():
    assert build_issue_filter(assignee='unassigned')['assignee'] == {'null': True}

def test_filter_assignee_email():
    f = build_issue_filter(assignee='dev@acme.com')
    assert f['assignee'] == {'email': {'eqIgnoreCase': 'dev@acme.com'}}

def test_filter_state_and_type():
    f = build_issue_filter(state='In Progress', state_type='started')
    assert f['state'] == {'name': {'eqIgnoreCase': 'In Progress'}, 'type': {'eq': 'started'}}

def test_filter_query_searches_title_and_description():
    f = build_issue_filter(query='login bug')
    assert f['or'] == [{'title': {'containsIgnoreCase': 'login bug'}}, {'description': {'containsIgnoreCase': 'login bug'}}]

def test_filter_label_and_priority():
    f = build_issue_filter(label='Bug', priority=1)
    assert f['labels'] == {'some': {'name': {'eqIgnoreCase': 'Bug'}}}
    assert f['priority'] == {'eq': 1}

def _sample_node():
    return {'id': 'uuid-1', 'identifier': 'ENG-42', 'title': 'Fix login', 'description': 'Steps...', 'priority': 2, 'priorityLabel': 'High', 'estimate': 3, 'url': 'https://linear.app/acme/issue/ENG-42', 'dueDate': '2026-08-01', 'createdAt': '2026-07-01T00:00:00.000Z', 'updatedAt': '2026-07-20T00:00:00.000Z', 'state': {'id': 's1', 'name': 'In Progress', 'type': 'started'}, 'assignee': {'id': 'u1', 'name': 'jane', 'displayName': 'Jane Doe', 'email': 'j@a.com'}, 'creator': {'id': 'u2', 'name': 'Bob'}, 'team': {'id': 't1', 'key': 'ENG', 'name': 'Engineering'}, 'project': {'id': 'p1', 'name': 'Q3 Auth'}, 'labels': {'nodes': [{'id': 'l1', 'name': 'Bug'}]}, 'parent': None}

def test_issue_to_dict_normalizes():
    d = issue_to_dict(_sample_node())
    assert d['identifier'] == 'ENG-42'
    assert d['state'] == 'In Progress'
    assert d['state_type'] == 'started'
    assert d['priority_label'] == 'High'
    assert d['assignee'] == 'Jane Doe'
    assert d['assignee_email'] == 'j@a.com'
    assert d['team'] == 'ENG'
    assert d['project'] == 'Q3 Auth'
    assert d['labels'] == ['Bug']
    assert d['parent'] is None

def test_issue_to_dict_handles_missing_relations():
    d = issue_to_dict({'id': 'x', 'identifier': 'ENG-1', 'title': 't'})
    assert d['state'] is None
    assert d['assignee'] is None
    assert d['labels'] == []

def _mock_gql(monkeypatch, responses):
    """Patch gql (in issues and _client) to pop canned (data, error) responses in order."""
    calls = []

    async def fake_gql(credentials, query, variables=None):
        calls.append({'query': query, 'variables': variables})
        return responses.pop(0)
    monkeypatch.setattr(issues_mod, 'gql', fake_gql)
    monkeypatch.setattr(client_mod, 'gql', fake_gql)
    return calls

@pytest.mark.asyncio
async def test_list_issues_normalizes_and_paginates(monkeypatch):
    calls = _mock_gql(monkeypatch, [({'issues': {'nodes': [_sample_node()], 'pageInfo': {'hasNextPage': True, 'endCursor': 'cur-1'}}}, None)])
    result = await list_issues({}, team='ENG', limit=500)
    assert result['count'] == 1
    assert result['issues'][0]['identifier'] == 'ENG-42'
    assert result['next_cursor'] == 'cur-1'
    assert calls[0]['variables']['first'] == 100

@pytest.mark.asyncio
async def test_list_issues_rejects_bad_priority():
    result = await list_issues({}, priority='critical')
    assert 'error' in result

@pytest.mark.asyncio
async def test_create_issue_requires_title():
    result = await create_issue({}, team='ENG', title='   ')
    assert 'error' in result

@pytest.mark.asyncio
async def test_create_issue_rejects_bad_due_date():
    result = await create_issue({}, team='ENG', title='x', due_date='01-08-2026')
    assert 'error' in result
    assert 'YYYY-MM-DD' in result['error']

@pytest.mark.asyncio
async def test_update_issue_requires_a_field(monkeypatch):
    _mock_gql(monkeypatch, [({'issues': {'nodes': [{'id': 'uuid-1', 'team': {'id': 't1'}}]}}, None)])
    result = await update_issue({}, issue='ENG-42')
    assert 'error' in result
    assert 'No fields' in result['error']

@pytest.mark.asyncio
async def test_update_issue_not_found(monkeypatch):
    _mock_gql(monkeypatch, [({'issues': {'nodes': []}}, None)])
    result = await update_issue({}, issue='ENG-999', title='new')
    assert "Issue 'ENG-999' not found" in result['error']

@pytest.mark.asyncio
async def test_update_issue_clears_assignee(monkeypatch):
    calls = _mock_gql(monkeypatch, [({'issues': {'nodes': [{'id': 'uuid-1', 'team': {'id': 't1'}}]}}, None), ({'issueUpdate': {'success': True, 'issue': _sample_node()}}, None)])
    result = await update_issue({}, issue='ENG-42', assignee='unassigned')
    assert 'issue' in result
    assert calls[1]['variables']['input'] == {'assigneeId': None}

@pytest.mark.asyncio
async def test_identifier_lookup_uses_team_key_and_number(monkeypatch):
    """'ENG-42' must be looked up via team-key + number filter, never issue(id:)."""
    calls = _mock_gql(monkeypatch, [({'issues': {'nodes': [{'id': 'uuid-1'}]}}, None)])
    node, error = await client_mod.fetch_issue_node({}, 'ENG-42', 'id')
    assert error is None
    assert node == {'id': 'uuid-1'}
    assert calls[0]['variables'] == {'key': 'ENG', 'number': 42.0}
    assert 'issues(' in calls[0]['query']

@pytest.mark.asyncio
async def test_uuid_lookup_uses_issue_query(monkeypatch):
    uuid = '123e4567-e89b-12d3-a456-426614174000'
    calls = _mock_gql(monkeypatch, [({'issue': {'id': uuid}}, None)])
    node, error = await client_mod.fetch_issue_node({}, uuid, 'id')
    assert error is None
    assert node == {'id': uuid}
    assert calls[0]['variables'] == {'id': uuid}
