import importlib
collector_admin_mod = importlib.import_module("connectors.surveymonkey.server")
contacts_mod = importlib.import_module("connectors.surveymonkey.server")
folders_mod = importlib.import_module("connectors.surveymonkey.server")
messages_mod = importlib.import_module("connectors.surveymonkey.server")
pages_mod = importlib.import_module("connectors.surveymonkey.server")
questions_mod = importlib.import_module("connectors.surveymonkey.server")
survey_authoring_mod = importlib.import_module("connectors.surveymonkey.server")
users_mod = importlib.import_module("connectors.surveymonkey.server")
webhooks_mod = importlib.import_module("connectors.surveymonkey.server")
from connectors.surveymonkey.server import surveymonkey_add_contacts_bulk, surveymonkey_add_message_recipients, surveymonkey_add_page, surveymonkey_add_question, surveymonkey_create_folder, surveymonkey_create_message, surveymonkey_create_survey, surveymonkey_create_webhook, surveymonkey_delete_collector, surveymonkey_delete_survey, surveymonkey_delete_webhook, surveymonkey_get_message_stats, surveymonkey_list_contacts, surveymonkey_update_collector, surveymonkey_update_survey, surveymonkey_update_webhook, surveymonkey_whoami

"""Tests for the survey-authoring, page/question, contact, collector-message,
webhook-admin, and identity tools added after the initial 10-tool build -
same fake-`request()` pattern as test_surveymonkey.py, no real HTTP."""
import asyncio
import pytest

def _run(coro):
    return asyncio.run(coro)

class _RecordingRequest:

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, credentials, method, path, params=None, json_body=None):
        self.calls.append({'credentials': credentials, 'method': method, 'path': path, 'params': params, 'json_body': json_body})
        return self.response

def test_whoami_surfaces_granted_and_available_scopes(monkeypatch):
    fake = _RecordingRequest({'id': '1', 'username': 'u', 'email': 'u@example.com', 'account_type': 'free', 'scopes': {'granted': ['surveys_read'], 'available': ['surveys_read', 'responses_read_detail']}})
    monkeypatch.setattr(users_mod, 'request', fake)
    res = _run(surveymonkey_whoami({}))
    assert res['account_type'] == 'free'
    assert res['scopes_granted'] == ['surveys_read']
    assert 'responses_read_detail' in res['scopes_available']

def test_create_survey_rejects_both_template_and_survey_source():
    with pytest.raises(ValueError, match='mutually exclusive'):
        _run(surveymonkey_create_survey({}, from_template_id='1', from_survey_id='2'))

def test_create_survey_posts_compact_body(monkeypatch):
    fake = _RecordingRequest({'id': '9', 'title': 'New Survey'})
    monkeypatch.setattr(survey_authoring_mod, 'request', fake)
    res = _run(surveymonkey_create_survey({}, title='Test'))
    assert res == {'success': True, 'survey': {'id': '9', 'title': 'New Survey'}}
    assert fake.calls[0] == {'credentials': {}, 'method': 'POST', 'path': '/surveys', 'params': None, 'json_body': {'title': 'Test'}}

def test_update_survey_requires_at_least_one_field():
    with pytest.raises(ValueError, match='at least one field'):
        _run(surveymonkey_update_survey({}, survey_id='1'))

def test_delete_survey_calls_delete(monkeypatch):
    fake = _RecordingRequest(None)
    monkeypatch.setattr(survey_authoring_mod, 'request', fake)
    res = _run(surveymonkey_delete_survey({}, survey_id='1'))
    assert res == {'success': True, 'deleted_survey_id': '1'}
    assert fake.calls[0]['method'] == 'DELETE'
    assert fake.calls[0]['path'] == '/surveys/1'

def test_add_page_posts_to_survey(monkeypatch):
    fake = _RecordingRequest({'id': 'p1', 'title': 'Page 1'})
    monkeypatch.setattr(pages_mod, 'request', fake)
    res = _run(surveymonkey_add_page({}, survey_id='1', title='Page 1'))
    assert res == {'success': True, 'page': {'id': 'p1', 'title': 'Page 1'}}
    assert fake.calls[0]['path'] == '/surveys/1/pages'

def test_add_question_rejects_unknown_type():
    with pytest.raises(ValueError, match='type must be one of'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='essay_mode'))

def test_add_question_requires_heading_for_non_advanced_types():
    with pytest.raises(ValueError, match='heading is required'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='short_text'))

def test_add_question_single_choice_requires_choices():
    with pytest.raises(ValueError, match='at least one option'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='single_choice', heading='Pick one'))

def test_add_question_builds_single_choice_body(monkeypatch):
    fake = _RecordingRequest({'id': 'q1'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    res = _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='single_choice', heading='Pick one', choices=['A', 'B']))
    assert res == {'success': True, 'question': {'id': 'q1'}}
    body = fake.calls[0]['json_body']
    assert body['family'] == 'single_choice'
    assert body['subtype'] == 'vertical'
    assert body['headings'] == [{'heading': 'Pick one'}]
    assert body['answers'] == {'choices': [{'text': 'A'}, {'text': 'B'}]}

def test_add_question_multiple_choice_switches_family(monkeypatch):
    fake = _RecordingRequest({'id': 'q1'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='multiple_choice', heading='Pick any', choices=['A']))
    assert fake.calls[0]['json_body']['family'] == 'multiple_choice'

def test_add_question_required_adds_block(monkeypatch):
    fake = _RecordingRequest({'id': 'q1'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='short_text', heading='Name?', required=True))
    assert fake.calls[0]['json_body']['required'] == {'text': 'Please answer: Name?', 'type': 'all', 'amount': '0'}

def test_add_question_long_text_is_essay(monkeypatch):
    fake = _RecordingRequest({'id': 'q2'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='long_text', heading='Comments?'))
    body = fake.calls[0]['json_body']
    assert body['family'] == 'open_ended'
    assert body['subtype'] == 'essay'
    assert 'answers' not in body

def test_add_question_short_text_is_single(monkeypatch):
    fake = _RecordingRequest({'id': 'q2'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='short_text', heading='Name?'))
    assert fake.calls[0]['json_body']['subtype'] == 'single'

def test_add_question_rating_scale_builds_single_row_matrix(monkeypatch):
    fake = _RecordingRequest({'id': 'q3'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='rating_scale', heading='Rate us', scale_max=5))
    body = fake.calls[0]['json_body']
    assert body['family'] == 'matrix'
    assert body['subtype'] == 'rating'
    assert body['answers']['rows'] == [{'text': '', 'position': 1}]
    assert [c['weight'] for c in body['answers']['choices']] == [1, 2, 3, 4, 5]
    assert 'display_options' not in body

def test_add_question_rating_scale_labels_only_the_ends(monkeypatch):
    fake = _RecordingRequest({'id': 'q3'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='rating_scale', heading='How likely to recommend?', scale_max=10, low_label='Not likely', high_label='Very likely'))
    choices = fake.calls[0]['json_body']['answers']['choices']
    assert choices[0]['text'] == 'Not likely'
    assert choices[-1]['text'] == 'Very likely'
    assert choices[1]['text'] == '2'

def test_add_question_rating_scale_star_style_adds_display_options(monkeypatch):
    fake = _RecordingRequest({'id': 'q3'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='rating_scale', heading='Rate us', style='star'))
    assert fake.calls[0]['json_body']['display_options'] == {'display_type': 'emoji', 'display_subtype': 'star'}

def test_add_question_rating_scale_rejects_bad_style():
    with pytest.raises(ValueError, match='style must be one of'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='rating_scale', heading='Rate us', style='laser_eyes'))

def test_add_question_rating_scale_rejects_scale_max_below_two():
    with pytest.raises(ValueError, match='at least 2'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='rating_scale', heading='Rate us', scale_max=1))

def test_add_question_advanced_requires_question_object():
    with pytest.raises(ValueError, match='question .* is required'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='advanced'))

def test_add_question_advanced_requires_family_and_subtype():
    with pytest.raises(ValueError, match='family and question.subtype'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='advanced', question={'headings': [{'heading': 'Q'}]}))

def test_add_question_advanced_requires_headings():
    with pytest.raises(ValueError, match='headings is required'):
        _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='advanced', question={'family': 'open_ended', 'subtype': 'essay'}))

def test_add_question_advanced_posts_question_as_is(monkeypatch):
    fake = _RecordingRequest({'id': 'q1'})
    monkeypatch.setattr(questions_mod, 'request', fake)
    question = {'family': 'demographic', 'subtype': 'us', 'headings': [{'heading': 'Where do you live?'}]}
    res = _run(surveymonkey_add_question({}, survey_id='1', page_id='2', type='advanced', question=question))
    assert res == {'success': True, 'question': {'id': 'q1'}}
    assert fake.calls[0]['path'] == '/surveys/1/pages/2/questions'
    assert fake.calls[0]['json_body'] == question

def test_list_contacts_rejects_bad_status():
    with pytest.raises(ValueError, match='status must be one of'):
        _run(surveymonkey_list_contacts({}, contact_list_id='1', status='ghosted'))

def test_add_contacts_bulk_requires_email_or_phone():
    with pytest.raises(ValueError, match='email or phone_number'):
        _run(surveymonkey_add_contacts_bulk({}, contact_list_id='1', contacts=[{'first_name': 'A', 'last_name': 'B'}]))

def test_add_contacts_bulk_posts_contacts(monkeypatch):
    fake = _RecordingRequest({'succeeded': [{'id': 'c1'}], 'invalids': [], 'existing': []})
    monkeypatch.setattr(contacts_mod, 'request', fake)
    contacts = [{'first_name': 'A', 'last_name': 'B', 'email': 'a@example.com'}]
    res = _run(surveymonkey_add_contacts_bulk({}, contact_list_id='1', contacts=contacts))
    assert res['succeeded'] == [{'id': 'c1'}]
    assert fake.calls[0]['path'] == '/contact_lists/1/contacts/bulk'
    assert fake.calls[0]['json_body']['contacts'] == contacts

def test_create_message_rejects_unknown_type():
    with pytest.raises(ValueError, match='type must be one of'):
        _run(surveymonkey_create_message({}, collector_id='1', type='carrier_pigeon'))

def test_add_message_recipients_requires_at_least_one_source():
    with pytest.raises(ValueError, match='at least one of'):
        _run(surveymonkey_add_message_recipients({}, collector_id='1', message_id='2'))

def test_add_message_recipients_posts_contact_ids(monkeypatch):
    fake = _RecordingRequest({'succeeded': [{'id': 'r1'}], 'invalids': [], 'bounced': [], 'opted_out': []})
    monkeypatch.setattr(messages_mod, 'request', fake)
    res = _run(surveymonkey_add_message_recipients({}, collector_id='1', message_id='2', contact_ids=['9']))
    assert res['succeeded'] == [{'id': 'r1'}]
    assert fake.calls[0]['path'] == '/collectors/1/messages/2/recipients/bulk'

def test_get_message_stats_falls_back_to_collector_wide_path(monkeypatch):
    fake = _RecordingRequest({'recipients': 5})
    monkeypatch.setattr(messages_mod, 'request', fake)
    _run(surveymonkey_get_message_stats({}, collector_id='1'))
    assert fake.calls[0]['path'] == '/collectors/1/stats'

def test_update_collector_requires_at_least_one_field():
    with pytest.raises(ValueError, match='at least one field'):
        _run(surveymonkey_update_collector({}, collector_id='1'))

def test_delete_collector_calls_delete(monkeypatch):
    fake = _RecordingRequest(None)
    monkeypatch.setattr(collector_admin_mod, 'request', fake)
    res = _run(surveymonkey_delete_collector({}, collector_id='1'))
    assert res == {'success': True, 'deleted_collector_id': '1'}
    assert fake.calls[0]['method'] == 'DELETE'

def test_update_webhook_rejects_unknown_event_type():
    with pytest.raises(ValueError, match='event_type must be one of'):
        _run(surveymonkey_update_webhook({}, webhook_id='1', event_type='response_liked'))

def test_create_webhook_requires_object_type_and_ids_for_non_survey_created():
    with pytest.raises(ValueError, match='required for every event_type except survey_created'):
        _run(surveymonkey_create_webhook({}, name='x', event_type='response_completed', subscription_url='https://example.com/hook'))

def test_create_webhook_rejects_object_type_with_survey_created():
    with pytest.raises(ValueError, match='must be omitted for survey_created'):
        _run(surveymonkey_create_webhook({}, name='x', event_type='survey_created', subscription_url='https://example.com/hook', object_type='survey', object_ids=['1']))

def test_delete_webhook_calls_delete(monkeypatch):
    fake = _RecordingRequest(None)
    monkeypatch.setattr(webhooks_mod, 'request', fake)
    res = _run(surveymonkey_delete_webhook({}, webhook_id='1'))
    assert res == {'success': True, 'deleted_webhook_id': '1'}
    assert fake.calls[0]['path'] == '/webhooks/1'

def test_create_folder_posts_title(monkeypatch):
    fake = _RecordingRequest({'id': 'f1', 'title': 'Q3'})
    monkeypatch.setattr(folders_mod, 'request', fake)
    res = _run(surveymonkey_create_folder({}, title='Q3'))
    assert res == {'success': True, 'folder': {'id': 'f1', 'title': 'Q3'}}
    assert fake.calls[0]['path'] == '/survey_folders'
