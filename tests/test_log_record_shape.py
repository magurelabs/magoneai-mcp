"""Redaction must not break a formatter that reads the record's arguments.

Found on the local stack (T-API): uvicorn's access formatter unpacks
`record.args` as (client, method, path, version, status). The redaction
factory set `args = ()` on every record, so every access line failed with
"not enough values to unpack" and the access log was lost. A record now keeps
its shape unless a secret spans the message and an argument.
"""

import logging

from uvicorn.logging import AccessFormatter

from magone_mcp_core.logging_setup import _scrubbing_record_factory

FACTORY = _scrubbing_record_factory(logging.LogRecord)


def record(msg: str, args: tuple) -> logging.LogRecord:
    return FACTORY("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


def test_an_access_line_still_formats() -> None:
    access = record('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5", "POST", "/mcp", "1.1", 200))

    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(access)

    assert line == '10.0.0.1:5 - "POST /mcp HTTP/1.1" 200 OK'


def test_a_secret_in_an_argument_is_scrubbed_and_the_shape_kept() -> None:
    access = record(
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5", "GET", "/cb?access_token=ya29.a0Secret123456789", "1.1", 200),
    )

    line = AccessFormatter("%(request_line)s %(status_code)s").format(access)

    assert "ya29.a0Secret123456789" not in line
    assert line.endswith(" 200 OK") and isinstance(access.args, tuple) and len(access.args) == 5


def test_a_secret_split_across_message_and_argument_is_still_scrubbed() -> None:
    split = record("Authorization: Bearer %s", ("sk_live_abcdefghijklmnop1234",))

    assert "sk_live_abcdefghijklmnop1234" not in split.getMessage()
