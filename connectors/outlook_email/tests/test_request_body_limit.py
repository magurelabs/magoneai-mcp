"""outlook_email declares v1's 75 MiB request body cap.

send_email, create_draft and reply_email accept up to 10 base64 attachments
per call. The framework's 10 MiB default refused those calls before any tool
ran; v1 ran this connector at 75 MB. The cap itself is exercised over a real
socket in tests/test_request_body_limit.py and in the Jira connector's tests.
"""

import importlib

import pytest

pytestmark = pytest.mark.requires_connectors

module = importlib.import_module("connectors.outlook_email.server")


def test_outlook_email_declares_seventy_five_mebibytes(monkeypatch):
    monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    assert module.server._default_max_body_bytes == 75 * 1024 * 1024
