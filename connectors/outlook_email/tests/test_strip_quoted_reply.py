"""`_strip_quoted_reply` leaves only this message's own new content.

Carried across from v1 `tests/test_get_email_strip_quoted_reply.py` (#163),
which was written after a live case: a Gmail reply's Graph body carried the
entire prior thread, compounding with every reply, because nothing truncated at
the quote header.

v1 drove the tool method on a mixin instance. v2 has no mixin, and the part that
would silently regress is the pattern logic, so these exercise the helper
directly. The three tools that call it (`get_email`, `get_email_thread`,
`get_email_bodies`) are covered by the callable gate.

Every "fails open" case matters more than the stripping cases: a false positive
deletes real content, which is worse than leaving a quote in.
"""

from connectors.outlook_email.server import _strip_quoted_reply


def test_text_with_no_quote_marker_is_unchanged():
    assert _strip_quoted_reply("Just a normal message.") == "Just a normal message."


def test_truncates_at_a_gmail_style_on_wrote_header():
    body = (
        "My answer is yes.\n\nOn Mon, 1 Sep 2026 at 10:00, Jane <j@x.com> wrote:\n> the question\n"
    )
    assert _strip_quoted_reply(body) == "My answer is yes."


def test_truncates_at_the_first_header_when_a_thread_has_compounded():
    body = (
        "Third reply.\n\n"
        "On Wed, 3 Sep 2026, A <a@x.com> wrote:\nSecond reply.\n\n"
        "On Tue, 2 Sep 2026, B <b@x.com> wrote:\nFirst message.\n"
    )
    assert _strip_quoted_reply(body) == "Third reply."


def test_truncates_at_the_outlook_original_message_separator():
    body = "Approved.\n\n-----Original Message-----\nFrom: someone\nthe original\n"
    assert _strip_quoted_reply(body) == "Approved."


def test_truncates_at_a_gmail_header_with_crlf_line_endings():
    body = "Answer.\r\n\r\nOn Mon, 1 Sep 2026 at 10:00, Jane <j@x.com> wrote:\r\n> q\r\n"
    assert _strip_quoted_reply(body) == "Answer."


def test_truncates_at_a_plain_text_quote_block():
    body = "See below.\n\n> line one\n> line two\n> line three\n"
    assert _strip_quoted_reply(body) == "See below."


def test_a_single_angle_bracket_line_is_not_a_quote_block():
    body = "Flow is A > B and that is fine."
    assert _strip_quoted_reply(body) == body


def test_two_consecutive_angle_bracket_lines_are_not_stripped():
    body = "Steps:\n> first\n> second\n"
    assert _strip_quoted_reply(body) == body


def test_wrote_inside_a_sentence_does_not_false_positive():
    body = "On the call yesterday, the underwriter wrote: approve it. Please proceed."
    assert _strip_quoted_reply(body) == body


def test_a_quote_header_at_offset_zero_fails_open():
    """Truncating would leave nothing, which is worse than keeping the quote -
    a bottom-posted reply, or a body that is entirely quoted."""
    body = "On Mon, 1 Sep 2026 at 10:00, Jane <j@x.com> wrote:\n> everything\n"
    assert _strip_quoted_reply(body) == body


def test_a_body_that_is_entirely_a_quote_block_fails_open():
    body = "> one\n> two\n> three\n"
    assert _strip_quoted_reply(body) == body


def test_a_gmail_forward_marker_is_not_stripped():
    """The quoted region of a forward IS the payload the caller wants."""
    body = (
        "FYI, see attached claim.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Client <c@x.com>\nSubject: Policy documents\nthe documents\n"
    )
    assert _strip_quoted_reply(body) == body


def test_an_apple_mail_forward_marker_is_not_stripped():
    body = (
        "Passing this on.\n\n"
        "On Mon, 1 Sep 2026, A <a@x.com> wrote:\n\n"
        "Begin forwarded message:\n\nFrom: Client\nthe payload\n"
    )
    assert _strip_quoted_reply(body) == body


def test_an_outlook_forward_header_block_is_not_stripped():
    """Outlook gives a forward and a reply the identical separator + header
    block. The Subject: prefix is the only thing that separates them: a reply's
    starts with RE:, a forward's does not."""
    body = (
        "Please review.\n\n"
        "-----Original Message-----\n"
        "From: Client <c@x.com>\nSent: Monday\nTo: Me\nSubject: Policy documents\n"
        "the documents\n"
    )
    assert _strip_quoted_reply(body) == body


def test_an_outlook_reply_header_block_is_stripped():
    """The same shape, but Subject: RE: - so it is a reply trail, not a payload."""
    body = (
        "Approved.\n\n"
        "-----Original Message-----\n"
        "From: Client <c@x.com>\nSent: Monday\nTo: Me\nSubject: RE: Policy\n"
        "the quoted thread\n"
    )
    assert _strip_quoted_reply(body) == "Approved."
