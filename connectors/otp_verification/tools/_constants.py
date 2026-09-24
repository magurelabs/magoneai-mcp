"""Constants for OTP verification MCP server."""

# OTP defaults (callers can override via tool params)
DEFAULT_OTP_LENGTH = 6
DEFAULT_EXPIRY_MINUTES = 5
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RESENDS = 3
DEFAULT_BLOCK_HOURS = 2
DEFAULT_COOLDOWN_MINUTES = 15
DEFAULT_TABLE = "otp_verifications"

# Valid statuses
STATUS_PENDING = "pending"
STATUS_VERIFIED = "verified"
STATUS_EXPIRED = "expired"
STATUS_LOCKED = "locked"

# Valid identifier types
VALID_IDENTIFIER_TYPES = {"email", "phone", "user_id", "session"}

# Credential headers
CREDENTIAL_HEADERS = [
    "X-Db-Connection-String",
]
