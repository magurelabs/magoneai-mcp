"""Vendored helpers from the v1 repository-wide shared package.

The port created this package with only this docstring and vendored nothing,
so this connector failed at import on the first line that used it. Restored
from `shared/microsoft_auth.py` in the v1 catalogue.
"""

# The app-only credential set. `site_id` is not a secret - it is configuration
# - but it travels the same way, as a request header the framework folds into
# the handler's `credentials` dict.
SHAREPOINT_APP_ONLY_CREDENTIAL_HEADERS = [
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
    "X-Sharepoint-Site-Id",
]

__all__ = ["SHAREPOINT_APP_ONLY_CREDENTIAL_HEADERS"]
