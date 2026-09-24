import importlib
contacts_mod = importlib.import_module("connectors.surveymonkey.server")
pages_mod = importlib.import_module("connectors.surveymonkey.server")
question_admin_mod = importlib.import_module("connectors.surveymonkey.server")
recipients_mod = importlib.import_module("connectors.surveymonkey.server")
from connectors.surveymonkey.server import surveymonkey_delete_page, surveymonkey_delete_question, surveymonkey_list_contact_fields, surveymonkey_list_recipients, surveymonkey_update_page, surveymonkey_update_question

"""Tests for the tools that filled real CRUD/clarity gaps found during the
feature audit: page/question update+delete (previously create+list only,
inconsistent with surveys/collectors/webhooks which had full CRUD),
per-recipient status, and contact custom-field labels."""
import asyncio
import pytest

def _run(coro):
    return asyncio.run(coro)

class _RecordingRequest:

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, credentials, method, path, params=None, json_body=None):
        self.calls.append({'method': method, 'path': path, 'params': params, 'json_body': json_body})
        return self.response

def test_update_page_requires_at_least_one_field():
    with pytest.raises(ValueError, match='at least one field'):
        _run(surveymonkey_update_page({}, survey_id='1', page_id='2'))

def test_update_page_patches_survey(monkeypatch):
    fake = _RecordingRequest({'id': '2', 'title': 'New title'})
    monkeypatch.setattr(pages_mod, 'request', fake)
    res = _run(surveymonkey_update_page({}, survey_id='1', page_id='2', title='New title'))
    assert res == {'success': True, 'page': {'id': '2', 'title': 'New title'}}
    assert fake.calls[0] == {'method': 'PATCH', 'path': '/surveys/1/pages/2', 'params': None, 'json_body': {'title': 'New title'}}

def test_delete_page_calls_delete(monkeypatch):
    fake = _RecordingRequest(None)
    monkeypatch.setattr(pages_mod, 'request', fake)
    res = _run(surveymonkey_delete_page({}, survey_id='1', page_id='2'))
    assert res == {'success': True, 'deleted_page_id': '2'}
    assert fake.calls[0]['method'] == 'DELETE'
    assert fake.calls[0]['path'] == '/surveys/1/pages/2'

def test_update_question_requires_at_least_one_field():
    with pytest.raises(ValueError, match='at least one field'):
        _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3'))

def test_update_question_heading_builds_headings_patch(monkeypatch):
    fake = _RecordingRequest({'id': '3'})
    monkeypatch.setattr(question_admin_mod, 'request', fake)
    _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3', heading='New heading'))
    body = fake.calls[0]['json_body']
    assert body == {'headings': [{'heading': 'New heading'}]}
    assert fake.calls[0]['path'] == '/surveys/1/pages/2/questions/3'
    assert fake.calls[0]['method'] == 'PATCH'

def test_update_question_choices_requires_at_least_one():
    with pytest.raises(ValueError, match='at least one option'):
        _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3', choices=[]))

def test_update_question_required_builds_full_block(monkeypatch):
    fake = _RecordingRequest({'id': '3'})
    monkeypatch.setattr(question_admin_mod, 'request', fake)
    _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3', heading='Name?', required=True))
    assert fake.calls[0]['json_body']['required'] == {'text': 'Please answer: Name?', 'type': 'all', 'amount': '0'}

def test_update_question_required_false_clears_block(monkeypatch):
    fake = _RecordingRequest({'id': '3'})
    monkeypatch.setattr(question_admin_mod, 'request', fake)
    _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3', heading='Name?', required=False))
    assert fake.calls[0]['json_body']['required'] is None

def test_update_question_raw_object_bypasses_field_builders(monkeypatch):
    fake = _RecordingRequest({'id': '3'})
    monkeypatch.setattr(question_admin_mod, 'request', fake)
    raw = {'family': 'matrix', 'subtype': 'rating'}
    _run(surveymonkey_update_question({}, survey_id='1', page_id='2', question_id='3', heading='ignored', question=raw))
    assert fake.calls[0]['json_body'] == raw

def test_delete_question_calls_delete(monkeypatch):
    fake = _RecordingRequest(None)
    monkeypatch.setattr(question_admin_mod, 'request', fake)
    res = _run(surveymonkey_delete_question({}, survey_id='1', page_id='2', question_id='3'))
    assert res == {'success': True, 'deleted_question_id': '3'}
    assert fake.calls[0]['path'] == '/surveys/1/pages/2/questions/3'

def test_list_recipients_defaults_to_status_fields(monkeypatch):
    fake = _RecordingRequest({'total': 1, 'data': [{'id': 'r1'}]})
    monkeypatch.setattr(recipients_mod, 'request', fake)
    res = _run(surveymonkey_list_recipients({}, collector_id='1'))
    assert res == {'success': True, 'total': 1, 'recipients': [{'id': 'r1'}]}
    assert fake.calls[0]['params']['include'] == 'survey_response_status,mail_status'

def test_list_recipients_rejects_unknown_include_field():
    with pytest.raises(ValueError, match='unknown field'):
        _run(surveymonkey_list_recipients({}, collector_id='1', include=['favorite_color']))

def test_list_contact_fields_returns_labels(monkeypatch):
    fake = _RecordingRequest({'data': [{'id': '1', 'label': 'Company'}]})
    monkeypatch.setattr(contacts_mod, 'request', fake)
    res = _run(surveymonkey_list_contact_fields({}))
    assert res == {'success': True, 'fields': [{'id': '1', 'label': 'Company'}]}
    assert fake.calls[0]['path'] == '/contact_fields'
