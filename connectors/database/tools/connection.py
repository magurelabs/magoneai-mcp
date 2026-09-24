"""Helpers for resolving database connection details from credentials."""

from __future__ import annotations

from typing import Dict, Tuple
from urllib.parse import quote, urlencode, urlparse

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mongodb": 27017,
    "oracle": 1521,
    "sql server": 1433,
}

# Server-side backstop for the wallet base64 (the UI caps uploads at 256 KB;
# 256 KB raw -> ~342 KB base64). Reject anything larger before it is carried in
# a header and connection string, well under the MCP's 1 MB header buffer. Real
# ADW wallets are tens of KB.
MAX_WALLET_B64_LEN = 400_000

URI_SCHEME_TO_DB_TYPE = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mysql": "mysql",
    "mongodb": "mongodb",
    "mongodb+srv": "mongodb",
    "oracle": "oracle",
    "oracle+thin": "oracle",
    "oracle+thick": "oracle",
    "mssql": "sql server",
}

ALLOWED_CONNECTION_TYPES = {
    "postgresql": {"uri", "basic"},
    "mysql": {"uri", "basic"},
    "mongodb": {"uri", "basic"},
    "oracle": {"uri", "basic", "tns", "wallet"},
    "sql server": {"uri", "basic"},
}

# Header names MUST round-trip: the MCP server derives the credential key from
# the header by stripping the leading "X-" and lower-snake-casing the rest
# (see `magone_mcp_core.credentials`). So "X-Oracle-Tns-Alias" -> "oracle_tns_alias".
# A "X-Db-..." prefix would derive to "db_oracle_tns_alias" and never match
# the field the connection resolver reads, silently breaking platform mode.
ALL_DB_CREDENTIAL_HEADERS = [
    "X-Db-Display-Name",
    "X-Db-Type",
    "X-Connection-Type",
    "X-Db-Connection-String",
    "X-Db-Host",
    "X-Db-Port",
    "X-Db-Name",
    "X-Db-Username",
    "X-Db-Password",
    "X-Db-Trust-Server-Certificate",
    "X-Oracle-Tns-Alias",
    "X-Oracle-Tns-User",
    "X-Oracle-Tns-Password",
    "X-Oracle-Wallet-Alias",
    "X-Oracle-Wallet-User",
    "X-Oracle-Wallet-Db-Password",
    "X-Oracle-Wallet",
    "X-Oracle-Wallet-Password",
    "X-Db-Query-Row-Limit",
]


def _normalize_db_type(raw_db_type: str) -> str:
    value = (raw_db_type or "").strip().lower()
    aliases = {
        "postgres": "postgresql",
        "sqlserver": "sql server",
        "mssql": "sql server",
        "sql_server": "sql server",
    }
    return aliases.get(value, value)


def _normalize_connection_type(raw_type: str) -> str:
    value = (raw_type or "").strip().lower()
    aliases = {
        "connection_string": "uri",
        "url": "uri",
        "dsn": "tns",
        "adw": "wallet",
        "cloud": "wallet",
        "cloud_wallet": "wallet",
        "mtls": "wallet",
    }
    return aliases.get(value, value)


def _clean_value(credentials: Dict[str, str], key: str) -> str:
    return (credentials.get(key) or "").strip()


def _require(value: str, field_label: str) -> Tuple[bool, str]:
    if value:
        return True, ""
    return False, f"Missing required field: {field_label}"


def _infer_connection_type(credentials: Dict[str, str], requested_type: str) -> str:
    if requested_type:
        return requested_type

    if _clean_value(credentials, "oracle_wallet"):
        return "wallet"

    if _clean_value(credentials, "oracle_tns_alias"):
        return "tns"

    has_basic_shape = bool(_clean_value(credentials, "db_host")) and bool(
        _clean_value(credentials, "db_name")
    )
    if has_basic_shape:
        return "basic"

    return "uri"


def _auth_segment(username: str, password: str) -> str:
    if not username:
        return ""
    encoded_user = quote(username, safe="")
    encoded_password = quote(password, safe="")
    return f"{encoded_user}:{encoded_password}@"


def resolve_connection_string(credentials: Dict[str, str]) -> Tuple[str | None, str | None]:
    """Resolve a connection string from provided credentials.

    Respects the explicit connection_type and only processes the fields
    relevant to that type.
    """
    db_type = _normalize_db_type(_clean_value(credentials, "db_type"))
    if not db_type:
        return None, "Missing required field: Database Type"

    raw_conn_type = _clean_value(credentials, "connection_type")
    # Default to URI if not explicitly provided (backward compatibility)
    if not raw_conn_type:
        connection_type = _infer_connection_type(credentials, "")
    else:
        connection_type = _normalize_connection_type(raw_conn_type)

    allowed_types = ALLOWED_CONNECTION_TYPES.get(db_type)
    if not allowed_types:
        return None, f"Unsupported database type: {db_type}"
    if connection_type not in allowed_types:
        allowed = ", ".join(sorted(allowed_types))
        return (
            None,
            f"Connection type '{connection_type}' is not supported for '{db_type}'. Use: {allowed}",
        )

    # 1. Handle URI
    if connection_type == "uri":
        connection_string = _clean_value(credentials, "db_connection_string")
        if not connection_string:
            return None, "Connection URI is required when connection type is URI"

        scheme = urlparse(connection_string).scheme.lower()
        parsed_db_type = URI_SCHEME_TO_DB_TYPE.get(scheme)
        if parsed_db_type and parsed_db_type != db_type:
            return (
                None,
                f"Connection URI scheme '{scheme}' does not match selected database type '{db_type}'",
            )
        return connection_string, None

    # 2. Handle TNS
    if connection_type == "tns":
        alias = _clean_value(credentials, "oracle_tns_alias")
        user = _clean_value(credentials, "oracle_tns_user")
        password = _clean_value(credentials, "oracle_tns_password")

        ok, error = _require(alias, "Oracle TNS Alias")
        if not ok:
            return None, error
        ok, error = _require(user, "Oracle Username")
        if not ok:
            return None, error
        ok, error = _require(password, "Oracle Password")
        if not ok:
            return None, error

        encoded_alias = quote(alias, safe="")
        return f"oracle://{_auth_segment(user, password)}localhost/?dsn={encoded_alias}", None

    # 2b. Handle Wallet (Oracle Autonomous Database / ADW — mTLS via wallet zip)
    #
    # The wallet is a binary zip, but every connector receives only a single
    # connection string (which also keys the pool cache). We therefore carry the
    # wallet base64-encoded inside the URI query; the Oracle connector decodes it
    # to a temp dir on connect() and cleans it up on close().
    if connection_type == "wallet":
        alias = _clean_value(credentials, "oracle_wallet_alias")
        user = _clean_value(credentials, "oracle_wallet_user")
        password = _clean_value(credentials, "oracle_wallet_db_password")
        wallet_b64 = _clean_value(credentials, "oracle_wallet")
        wallet_password = _clean_value(credentials, "oracle_wallet_password")

        for value, label in (
            (alias, "Service Name"),
            (user, "Oracle Username"),
            (password, "Oracle Password"),
            (wallet_b64, "Oracle Wallet"),
            (wallet_password, "Wallet Password"),
        ):
            ok, error = _require(value, label)
            if not ok:
                return None, error

        if len(wallet_b64) > MAX_WALLET_B64_LEN:
            return None, "Oracle Wallet is too large. Upload the ADW wallet zip (tens of KB)."

        # quote_via=quote (safe="") percent-encodes the base64 '+', '/', '='
        # so the blob survives parse_qs on the connector side intact.
        params = urlencode(
            {
                "dsn": alias,
                "wallet_b64": wallet_b64,
                "wallet_password": wallet_password,
            },
            quote_via=quote,
        )
        return f"oracle://{_auth_segment(user, password)}wallet/?{params}", None

    # 3. Handle Basic
    if connection_type != "basic":
        return None, f"Unsupported connection type: {connection_type}"

    host = _clean_value(credentials, "db_host")
    database_name = _clean_value(credentials, "db_name")
    username = _clean_value(credentials, "db_username")
    password = _clean_value(credentials, "db_password")
    raw_port = _clean_value(credentials, "db_port")

    ok, error = _require(host, "Host")
    if not ok:
        return None, error
    ok, error = _require(database_name, "Database Name")
    if not ok:
        return None, error
    ok, error = _require(username, "Username")
    if not ok:
        return None, error

    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            return None, "Port must be a valid number"
    else:
        port = DEFAULT_PORTS.get(db_type)

    if not port:
        return None, f"Unsupported database type: {db_type}"

    auth = _auth_segment(username, password)

    if db_type == "postgresql":
        return (
            f"postgresql://{auth}{host}:{port}/{database_name}",
            None,
        )
    if db_type == "mysql":
        return f"mysql://{auth}{host}:{port}/{database_name}", None
    if db_type == "mongodb":
        return f"mongodb://{auth}{host}:{port}/{database_name}", None
    if db_type == "oracle":
        return f"oracle://{auth}{host}:{port}/{database_name}", None
    if db_type == "sql server":
        trust = _clean_value(credentials, "db_trust_server_certificate").lower()
        trust_cert = "yes" if trust in ("true", "yes", "1") else "no"
        return (
            f"mssql://{auth}{host}:{port}/{database_name}?TrustServerCertificate={trust_cert}",
            None,
        )

    return None, f"Unsupported database type: {db_type}"
