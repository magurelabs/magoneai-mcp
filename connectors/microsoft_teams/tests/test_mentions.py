from connectors.microsoft_teams.server import MAX_MENTIONS, build_message_payload, validate_mentions

"""Tests for @-mention support in Teams message payloads."""
import pytest
USER_ID = '8f14e45f-ceea-467a-9f8a-1cb4d2f2e001'

def test_no_mentions_payload_unchanged():
    assert build_message_payload('hello', 'text') == {'body': {'contentType': 'text', 'content': 'hello'}}
    assert build_message_payload('<b>hi</b>', 'html', []) == {'body': {'contentType': 'html', 'content': '<b>hi</b>'}}

def test_single_mention_promotes_text_to_html():
    payload = build_message_payload('can you look at this?', 'text', [{'id': USER_ID, 'name': 'Jordan Lee'}])
    assert payload == {'body': {'contentType': 'html', 'content': '<at id="0">Jordan Lee</at> can you look at this?'}, 'mentions': [{'id': 0, 'mentionText': 'Jordan Lee', 'mentioned': {'user': {'id': USER_ID, 'displayName': 'Jordan Lee', 'userIdentityType': 'aadUser'}}}]}

def test_text_content_is_escaped_but_at_tags_are_not():
    payload = build_message_payload('R&D <script> review', 'text', [{'id': USER_ID, 'name': 'Ann'}])
    content = payload['body']['content']
    assert content == '<at id="0">Ann</at> R&amp;D &lt;script&gt; review'
    assert '&lt;at' not in content

def test_caller_placed_tag_is_left_in_place():
    payload = build_message_payload('hey <at id="0">Ann</at> please review', 'html', [{'id': USER_ID, 'name': 'Ann'}])
    assert payload['body']['content'] == 'hey <at id="0">Ann</at> please review'
    assert len(payload['mentions']) == 1

def test_two_mentions_indexed_and_ordered():
    payload = build_message_payload('standup?', 'text', [{'id': 'id-a', 'name': 'Ann'}, {'id': 'id-b', 'name': 'Bob'}])
    assert payload['body']['content'] == '<at id="0">Ann</at> <at id="1">Bob</at> standup?'
    assert [m['id'] for m in payload['mentions']] == [0, 1]
    assert [m['mentioned']['user']['id'] for m in payload['mentions']] == ['id-a', 'id-b']

@pytest.mark.parametrize('mentions', [[{'id': '', 'name': 'Ann'}], [{'name': 'Ann'}], [{'id': USER_ID, 'name': '  '}], [{'id': USER_ID, 'name': 'Ann'}] * (MAX_MENTIONS + 1), 'not-a-list'])
def test_invalid_mentions_return_error(mentions):
    assert validate_mentions(mentions) is not None

def test_valid_mentions_pass_validation():
    assert validate_mentions(None) is None
    assert validate_mentions([]) is None
    assert validate_mentions([{'id': USER_ID, 'name': 'Ann'}]) is None

def test_oversized_payload_after_mentions_raises():
    with pytest.raises(ValueError):
        build_message_payload('&' * 20000, 'text', [{'id': USER_ID, 'name': 'Ann'}])

def test_newlines_survive_the_promotion_to_html():
    """HTML collapses newlines, so a multi-line message arrived as one run-on
    line the moment a mention was added. Adding a mention must not reformat
    the body the sender approved."""
    payload = build_message_payload('Standup:\n- one\n- two', 'text', [{'id': 'u1', 'name': 'Ann'}])
    assert payload['body']['content'] == '<at id="0">Ann</at> Standup:<br>- one<br>- two'

def test_windows_newlines_do_not_double_up():
    payload = build_message_payload('a\r\nb', 'text', [{'id': 'u1', 'name': 'Ann'}])
    assert payload['body']['content'].endswith('a<br>b')

def test_a_caller_placed_tag_in_a_text_body_is_honoured_not_escaped():
    """`placed` used to be read from the ESCAPED body, so a caller-placed tag
    was never detected: the recipient got a second mention prepended and the
    original as visible &lt;at&gt; text."""
    payload = build_message_payload('hey <at id="0">Ann</at> ping', 'text', [{'id': 'u1', 'name': 'Ann'}])
    content = payload['body']['content']
    assert content == 'hey <at id="0">Ann</at> ping'
    assert content.count('<at ') == 1
    assert '&lt;at' not in content

def test_text_around_a_placed_tag_is_still_escaped():
    """Carrying the tag through must not carry everything else through."""
    payload = build_message_payload('a & b <at id="0">Ann</at> c < d', 'text', [{'id': 'u1', 'name': 'Ann'}])
    content = payload['body']['content']
    assert 'a &amp; b ' in content
    assert 'c &lt; d' in content
    assert '<at id="0">Ann</at>' in content

def _members(raw):
    """The projection list_chats / get_unread_messages / list_channel_members
    all now share. Kept in the test as a literal so a change to any one of
    them has to be a deliberate change to this shape."""
    return [{'id': m.get('userId'), 'name': m.get('displayName')} for m in raw if m.get('displayName')]

def test_a_member_without_a_user_id_reports_no_id_rather_than_the_membership_id():
    """The membership id is not an Entra user id. Falling back to it produced a
    mention Graph accepts, binds to nobody, and renders as inert text — the one
    failure mode that looks like success to the sender."""
    out = _members([{'id': 'membership-abc', 'displayName': 'Ann'}])
    assert out == [{'id': None, 'name': 'Ann'}]

def test_a_member_with_a_user_id_uses_it():
    out = _members([{'id': 'membership-abc', 'userId': 'entra-123', 'displayName': 'Ann'}])
    assert out == [{'id': 'entra-123', 'name': 'Ann'}]
