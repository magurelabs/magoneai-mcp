"""A credentials file wins over request headers.

A header used to override the file one key at a time. A caller who sent only
`X-Db-Host` and `X-Db-Port` therefore made the server connect to *their* host
with the password from the file, and `X-Allowed-Mailboxes` could widen the
mailbox allowlist the operator had set. The same shape existed for the Trino
host and the R2 endpoint URL.
"""

import json

import pytest

from magone_mcp_core.credentials import build, is_target_key

DB_HEADERS = [
    "X-Db-Host",
    "X-Db-Port",
    "X-Db-Name",
    "X-Db-Username",
    "X-Db-Password",
    "X-Db-Connection-String",
]


@pytest.fixture
def write_file(tmp_path):
    def write(data) -> str:
        path = tmp_path / "creds.json"
        path.write_text(json.dumps(data))
        return str(path)

    return write


class TestExfiltration:
    def test_host_and_port_headers_cannot_redirect_the_files_password(self, write_file):
        path = write_file({"db_password": "s3cret", "db_host": "db.internal", "db_port": "5432"})

        creds = build(
            {"X-Db-Host": "attacker.example", "X-Db-Port": "9999"}, DB_HEADERS, file_path=path
        )

        assert creds == {"db_password": "s3cret", "db_host": "db.internal", "db_port": "5432"}

    def test_a_secret_only_file_still_refuses_a_host_header(self, write_file):
        """The file sets the password and leaves the host to the caller: the
        exact setup the attack needs. Refused, not filled."""
        path = write_file({"db_password": "s3cret"})

        creds = build(
            {"X-Db-Host": "attacker.example", "X-Db-Port": "9999"}, DB_HEADERS, file_path=path
        )

        assert creds == {"db_password": "s3cret"}

    def test_a_connection_string_header_is_refused_when_the_file_has_a_secret(self, write_file):
        path = write_file({"db_password": "s3cret"})

        creds = build(
            {"X-Db-Connection-String": "postgresql://u:p@attacker.example/db"},
            DB_HEADERS,
            file_path=path,
        )

        assert creds == {"db_password": "s3cret"}

    def test_a_connection_string_in_the_file_counts_as_a_secret(self, write_file):
        """It embeds the password, though its key does not look secret."""
        path = write_file({"db_connection_string": "postgresql://u:p@db.internal/app"})

        creds = build({"X-Db-Host": "attacker.example"}, DB_HEADERS, file_path=path)

        assert creds == {"db_connection_string": "postgresql://u:p@db.internal/app"}

    @pytest.mark.parametrize(
        ("header", "file_key", "file_value"),
        [
            ("X-Trino-Host", "trino_password", "pw"),
            ("X-R2-Endpoint-Url", "r2_secret_access_key", "sk"),
            ("X-Llm-Base-Url", "llm_api_key", "key"),
            ("X-Trino-Http-Scheme", "trino_password", "pw"),
            ("X-Db-Trust-Server-Certificate", "db_password", "pw"),
        ],
    )
    def test_other_endpoint_headers_cannot_take_the_files_secret(
        self, write_file, header, file_key, file_value
    ):
        path = write_file({file_key: file_value})

        creds = build({header: "https://attacker.example"}, [header], file_path=path)

        assert creds == {file_key: file_value}

    def test_the_mailbox_allowlist_cannot_be_widened(self, write_file):
        path = write_file(
            {"microsoft_client_secret": "cs", "allowed_mailboxes": "shared@example.com"}
        )

        creds = build(
            {"X-Allowed-Mailboxes": "ceo@example.com,shared@example.com"},
            ["X-Allowed-Mailboxes"],
            file_path=path,
        )

        assert creds["allowed_mailboxes"] == "shared@example.com"

    def test_the_mailbox_allowlist_cannot_be_added_when_the_file_has_a_secret(self, write_file):
        path = write_file({"microsoft_client_secret": "cs"})

        creds = build(
            {"X-Allowed-Mailboxes": "ceo@example.com"}, ["X-Allowed-Mailboxes"], file_path=path
        )

        assert "allowed_mailboxes" not in creds


class TestTheFileWins:
    def test_a_header_cannot_override_a_key_the_file_sets(self, write_file):
        path = write_file({"linear_api_key": "from-file"})

        creds = build({"X-Linear-Api-Key": "from-header"}, ["X-Linear-Api-Key"], file_path=path)

        assert creds == {"linear_api_key": "from-file"}

    def test_a_header_fills_a_key_the_file_leaves_unset(self, write_file):
        path = write_file({"db_password": "s3cret", "db_host": "db.internal"})

        creds = build(
            {"X-Db-Name": "analytics", "X-Db-Username": "reader"}, DB_HEADERS, file_path=path
        )

        assert creds == {
            "db_password": "s3cret",
            "db_host": "db.internal",
            "db_name": "analytics",
            "db_username": "reader",
        }

    def test_a_file_with_no_secret_leaves_target_headers_working(self, write_file):
        """Nothing baked in that could be redirected, so nothing is guarded."""
        path = write_file({"allowed_paths": "reports"})

        creds = build({"X-Db-Host": "db.example.com"}, DB_HEADERS, file_path=path)

        assert creds == {"allowed_paths": "reports", "db_host": "db.example.com"}

    def test_an_undeclared_header_is_still_never_passed(self, write_file):
        path = write_file({"linear_api_key": "k"})

        assert build({"X-Other": "v"}, ["X-Linear-Api-Key"], file_path=path) == {
            "linear_api_key": "k"
        }

    def test_with_no_headers_the_file_is_returned(self, write_file):
        path = write_file({"linear_api_key": "k"})

        assert build(None, ["X-Linear-Api-Key"], file_path=path) == {"linear_api_key": "k"}


class TestHeaderOnlyModeIsUnchanged:
    def test_headers_supply_everything_including_targets(self):
        headers = {"X-Db-Host": "db.example.com", "X-Db-Port": "5432", "X-Db-Password": "pw"}

        assert build(headers, DB_HEADERS) == {
            "db_host": "db.example.com",
            "db_port": "5432",
            "db_password": "pw",
        }

    @pytest.mark.parametrize("path", [None, "/nonexistent/creds.json"])
    def test_a_missing_file_is_header_only(self, path):
        creds = build({"X-Db-Host": "h", "X-Db-Password": "pw"}, DB_HEADERS, file_path=path)

        assert creds == {"db_host": "h", "db_password": "pw"}

    def test_an_empty_file_is_header_only(self, write_file):
        creds = build({"X-Db-Host": "h"}, DB_HEADERS, file_path=write_file({}))

        assert creds == {"db_host": "h"}

    def test_a_file_of_blank_values_is_header_only(self, write_file):
        path = write_file({"db_password": ""})

        assert build({"X-Db-Host": "h"}, DB_HEADERS, file_path=path) == {
            "db_password": "",
            "db_host": "h",
        }


class TestTargetKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "db_host",
            "db_port",
            "trino_host",
            "r2_endpoint_url",
            "llm_base_url",
            "r2_public_base_url",
            "db_connection_string",
            "trino_http_scheme",
            "allowed_mailboxes",
            "allowed_paths",
            "trino_allowed_catalogs",
        ],
    )
    def test_targets_and_scopes(self, key):
        assert is_target_key(key) is True

    @pytest.mark.parametrize(
        "key", ["db_password", "linear_api_key", "db_name", "zoom_client_id", "trino_user"]
    )
    def test_secrets_and_ordinary_fields_are_not(self, key):
        assert is_target_key(key) is False
