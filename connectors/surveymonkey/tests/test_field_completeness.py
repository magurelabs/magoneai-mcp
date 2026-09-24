import importlib
collector_admin_mod = importlib.import_module("connectors.surveymonkey.server")
collectors_mod = importlib.import_module("connectors.surveymonkey.server")
contacts_mod = importlib.import_module("connectors.surveymonkey.server")
responses_mod = importlib.import_module("connectors.surveymonkey.server")
rollups_mod = importlib.import_module("connectors.surveymonkey.server")
from connectors.surveymonkey.server import surveymonkey_create_collector, surveymonkey_get_response_summary, surveymonkey_get_responses_bulk, surveymonkey_list_collectors, surveymonkey_list_contacts, surveymonkey_list_responses, surveymonkey_update_collector

"""Tests for the fields added to make tools match SurveyMonkey's full,
production field set (not just a minimal happy-path subset): collector
behavior settings, the full response/rollup filter set, and contact
sort/search."""
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

def test_create_collector_accepts_full_settings(monkeypatch):
    fake = _RecordingRequest({'id': 'c1'})
    monkeypatch.setattr(collectors_mod, 'request', fake)
    _run(surveymonkey_create_collector({}, survey_id='1', type='weblink', anonymous_type='fully_anonymous', edit_response_type='never', redirect_type='close', allow_multiple_responses=True, password='secret'))
    body = fake.calls[0]['json_body']
    assert body['anonymous_type'] == 'fully_anonymous'
    assert body['edit_response_type'] == 'never'
    assert body['redirect_type'] == 'close'
    assert body['allow_multiple_responses'] is True
    assert body['password'] == 'secret'

def test_create_collector_rejects_bad_anonymous_type():
    with pytest.raises(ValueError, match='anonymous_type must be one of'):
        _run(surveymonkey_create_collector({}, survey_id='1', type='weblink', anonymous_type='invisible'))

def test_update_collector_accepts_status_and_settings(monkeypatch):
    fake = _RecordingRequest({'id': 'c1'})
    monkeypatch.setattr(collector_admin_mod, 'request', fake)
    _run(surveymonkey_update_collector({}, collector_id='1', status='closed', sender_email='a@example.com'))
    body = fake.calls[0]['json_body']
    assert body['status'] == 'closed'
    assert body['sender_email'] == 'a@example.com'

def test_update_collector_rejects_bad_redirect_type():
    with pytest.raises(ValueError, match='redirect_type must be one of'):
        _run(surveymonkey_update_collector({}, collector_id='1', redirect_type='teleport'))

def test_list_collectors_accepts_sort(monkeypatch):
    fake = _RecordingRequest({'total': 0, 'page': 1, 'data': []})
    monkeypatch.setattr(collectors_mod, 'request', fake)
    _run(surveymonkey_list_collectors({}, survey_id='1', sort_by='date_modified', sort_order='DESC'))
    assert fake.calls[0]['params']['sort_by'] == 'date_modified'
    assert fake.calls[0]['params']['sort_order'] == 'DESC'

def test_list_collectors_rejects_bad_sort_by():
    with pytest.raises(ValueError, match='sort_by must be one of'):
        _run(surveymonkey_list_collectors({}, survey_id='1', sort_by='popularity'))

def test_list_responses_accepts_identity_and_time_filters(monkeypatch):
    fake = _RecordingRequest({'total': 0, 'page': 1, 'data': []})
    monkeypatch.setattr(responses_mod, 'request', fake)
    _run(surveymonkey_list_responses({}, survey_id='1', email='a@example.com', first_name='Jane', total_time_min=30, total_time_units='second', sort_by='date_modified', sort_order='ASC'))
    params = fake.calls[0]['params']
    assert params['email'] == 'a@example.com'
    assert params['first_name'] == 'Jane'
    assert params['total_time_min'] == 30
    assert params['total_time_units'] == 'second'
    assert params['sort_by'] == 'date_modified'
    assert params['sort_order'] == 'ASC'

def test_list_responses_rejects_bad_time_units():
    with pytest.raises(ValueError, match='total_time_units must be one of'):
        _run(surveymonkey_list_responses({}, survey_id='1', total_time_units='fortnight'))

def test_get_responses_bulk_accepts_page_and_question_narrowing(monkeypatch):
    fake = _RecordingRequest({'total': 0, 'page': 1, 'data': []})
    monkeypatch.setattr(responses_mod, 'request', fake)
    _run(surveymonkey_get_responses_bulk({}, survey_id='1', page_ids=['10'], question_ids=['20', '21']))
    params = fake.calls[0]['params']
    assert params['page_ids'] == '10'
    assert params['question_ids'] == '20,21'

def test_get_response_summary_accepts_identity_filters(monkeypatch):
    fake = _RecordingRequest({'data': []})
    monkeypatch.setattr(rollups_mod, 'request', fake)
    _run(surveymonkey_get_response_summary({}, survey_id='1', ip='1.2.3.4', custom='promo-x'))
    params = fake.calls[0]['params']
    assert params['ip'] == '1.2.3.4'
    assert params['custom'] == 'promo-x'

def test_list_contacts_accepts_sort_and_search_by(monkeypatch):
    fake = _RecordingRequest({'total': 0, 'data': []})
    monkeypatch.setattr(contacts_mod, 'request', fake)
    _run(surveymonkey_list_contacts({}, contact_list_id='1', search='acme', search_by='1', sort_by='last_name', sort_order='DESC'))
    params = fake.calls[0]['params']
    assert params['search_by'] == '1'
    assert params['sort_by'] == 'last_name'
    assert params['sort_order'] == 'DESC'

def test_list_contacts_rejects_bad_sort_by():
    with pytest.raises(ValueError, match='sort_by must be one of'):
        _run(surveymonkey_list_contacts({}, contact_list_id='1', sort_by='favorite_color'))
