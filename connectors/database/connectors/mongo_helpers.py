"""MongoDB helper functions.

URI handling, filter conversion, and document serialization.
"""

import json
import logging
import re
from typing import Any
from urllib.parse import quote_plus, urlparse

from .base import parse_filter_key

logger = logging.getLogger(__name__)

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None


def encode_credentials(connection_string: str) -> str:
    """URL-encode username and password in MongoDB connection string."""
    parsed = urlparse(connection_string)
    if not parsed.username:
        return connection_string

    encoded_user = quote_plus(parsed.username)
    encoded_pass = quote_plus(parsed.password) if parsed.password else ""

    if parsed.scheme in ("mongodb+srv", "mongodb"):
        host_part = parsed.hostname or ""
        if parsed.port:
            host_part = f"{host_part}:{parsed.port}"
        path = parsed.path or ""
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{encoded_user}:{encoded_pass}@{host_part}{path}{query}"

    return connection_string


def log_masked_uri(connection_string: str) -> None:
    """Log a masked version of the URI for debugging auth issues."""
    parsed = urlparse(connection_string)
    user = parsed.username or "<none>"
    host = parsed.hostname or "<none>"
    db = parsed.path.lstrip("/").split("?")[0] or "<none>"
    pass_len = len(parsed.password) if parsed.password else 0
    logger.info(
        "mongodb_connect user=%s host=%s db=%s pass_length=%d scheme=%s",
        user,
        host,
        db,
        pass_len,
        parsed.scheme,
    )


def extract_db_name(connection_string: str) -> str:
    """Extract database name from MongoDB connection string."""
    parsed = urlparse(connection_string)
    db_name = parsed.path.lstrip("/").split("?")[0]
    return db_name or "test"


def to_mongo_filter(filters: dict[str, Any]) -> dict[str, Any]:
    """Convert generic filters to MongoDB query format."""
    mongo_ops = {
        "__ne": "$ne",
        "__gt": "$gt",
        "__lt": "$lt",
        "__gte": "$gte",
        "__lte": "$lte",
        "__in": "$in",
    }
    result: dict[str, Any] = {}

    for key, value in filters.items():
        field, op_suffix = parse_filter_key(key)

        if op_suffix == "__contains":
            result[field] = {"$regex": re.escape(str(value)), "$options": "i"}
        elif op_suffix == "__starts_with":
            result[field] = {"$regex": f"^{re.escape(str(value))}", "$options": "i"}
        elif op_suffix == "__ends_with":
            result[field] = {"$regex": f"{re.escape(str(value))}$", "$options": "i"}
        elif op_suffix == "__is_null":
            result[field] = None if value else {"$ne": None}
        elif op_suffix in mongo_ops:
            if op_suffix == "__in" and not isinstance(value, list):
                value = [value]
            result[field] = {mongo_ops[op_suffix]: value}
        else:
            result[field] = value

    return result


def serialize_doc(doc: dict) -> dict[str, Any]:
    """Convert MongoDB document to JSON-serializable dict."""
    result: dict[str, Any] = {}
    for key, value in doc.items():
        if ObjectId is not None and isinstance(value, ObjectId):
            result[key] = str(value)
        elif isinstance(value, bytes):
            result[key] = value.hex()
        else:
            try:
                json.dumps(value, default=str)
                result[key] = value
            except (TypeError, ValueError):
                result[key] = str(value)
    return result
