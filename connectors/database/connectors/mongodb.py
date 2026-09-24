"""MongoDB connector using motor (async driver).

Security: Blocks dangerous MongoDB operators ($where, $accumulator,
$function, $expr) that could execute arbitrary JavaScript.
"""

import json
from typing import Any

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

from .base import DatabaseConnector
from .mongo_helpers import (
    encode_credentials,
    extract_db_name,
    log_masked_uri,
    serialize_doc,
    to_mongo_filter,
)

QUERY_TIMEOUT_MS = 30000

DANGEROUS_MONGO_OPERATORS = {"$where", "$accumulator", "$function", "$expr"}


def _reject_dangerous_operators(obj: Any) -> None:
    """Recursively check filter dict for dangerous MongoDB operators."""
    if isinstance(obj, dict):
        for key in obj:
            if key in DANGEROUS_MONGO_OPERATORS:
                raise ValueError(f"Prohibited MongoDB operator: {key}")
            _reject_dangerous_operators(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            _reject_dangerous_operators(item)


class MongoDBConnector(DatabaseConnector):
    """MongoDB database connector."""

    def __init__(self) -> None:
        self._client: AsyncIOMotorClient | None = None
        self._db: Any = None

    async def connect(self, connection_string: str) -> None:
        if AsyncIOMotorClient is None:
            raise ImportError(
                "motor and pymongo are required for MongoDB connections. "
                "Install them with: pip install motor pymongo"
            )
        safe_uri = encode_credentials(connection_string)
        log_masked_uri(safe_uri)
        self._client = AsyncIOMotorClient(safe_uri, serverSelectionTimeoutMS=10000)
        db_name = extract_db_name(safe_uri)
        self._db = self._client[db_name]
        await self._client.admin.command("ping")

    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """For MongoDB, 'sql' is a JSON string: {"collection": "...", "filter": {...}}."""
        try:
            query_doc = json.loads(sql) if sql.strip().startswith("{") else {"collection": sql}
        except json.JSONDecodeError:
            query_doc = {"collection": sql}

        collection = query_doc.get("collection", "")
        mongo_filter = query_doc.get("filter", {})
        _reject_dangerous_operators(mongo_filter)
        limit = query_doc.get("limit", 100)
        skip = query_doc.get("skip", 0)

        cursor = (
            self._db[collection]
            .find(mongo_filter, max_time_ms=QUERY_TIMEOUT_MS)
            .skip(skip)
            .limit(max(1, limit))
        )
        return [serialize_doc(doc) async for doc in cursor]

    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        records = data if isinstance(data, list) else [data]
        if not records:
            return {"inserted_count": 0, "inserted_ids": []}

        result = await self._db[table].insert_many([dict(r) for r in records])
        return {
            "inserted_count": len(result.inserted_ids),
            "inserted_ids": [str(oid) for oid in result.inserted_ids],
        }

    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not filters:
            return {"updated_count": 0, "error": "Filters required for update"}

        mongo_filter = to_mongo_filter(filters)
        result = await self._db[table].update_many(mongo_filter, {"$set": data})
        return {"updated_count": result.modified_count}

    async def search(
        self,
        table: str,
        filters: dict[str, Any],
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, limit)
        mongo_filter = to_mongo_filter(filters) if filters else {}
        _reject_dangerous_operators(mongo_filter)

        cursor = (
            self._db[table]
            .find(mongo_filter, max_time_ms=QUERY_TIMEOUT_MS)
            .skip(offset)
            .limit(limit)
        )
        if order_by:
            direction = -1 if order_by.startswith("-") else 1
            field = order_by.lstrip("-")
            cursor = cursor.sort(field, direction)

        return [serialize_doc(doc) async for doc in cursor]

    async def get_tables(self) -> list[str]:
        return await self._db.list_collection_names()

    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        """Sample documents to infer schema (MongoDB is schema-less)."""
        sample = await self._db[table].find_one()
        if not sample:
            return []
        return [
            {
                "name": key,
                "type": type(value).__name__,
                "nullable": True,
                "default": None,
                "primary_key": key == "_id",
            }
            for key, value in sample.items()
        ]

    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        await self._db.create_collection(table)
        return {"created": True, "table": table}

    async def drop_table(self, table: str) -> dict[str, Any]:
        await self._db.drop_collection(table)
        return {"dropped": True, "table": table}

    async def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
