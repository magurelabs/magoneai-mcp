"""Shared constants for Outlook calendar tools."""

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

MAX_RESULTS_CAP = 100

# Length applied when a caller gives a start and no end. A caller that knows
# when something starts but not how long it runs is usually booking a reminder
# or a personal block, and being forced to invent an end time is how "in 30
# minutes" gets mistaken for a 30-minute duration.
DEFAULT_EVENT_MINUTES = 30
