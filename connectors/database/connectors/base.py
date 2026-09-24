"""Abstract base class for database connectors.

All database connectors (PostgreSQL, MySQL, MongoDB) implement this
interface so tools can work with any database engine.
"""

from abc import ABC, abstractmethod
from typing import Any


class DatabaseConnector(ABC):
    """Abstract interface for database connectors."""

    @abstractmethod
    async def connect(self, connection_string: str) -> None:
        """Establish connection to the database."""

    @abstractmethod
    async def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a read query and return rows as dicts."""

    @abstractmethod
    async def insert(
        self, table: str, data: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Insert one or more records. Returns inserted_count and inserted_ids."""

    @abstractmethod
    async def update(
        self, table: str, filters: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        """Update records matching filters. Returns updated_count."""

    @abstractmethod
    async def search(
        self,
        table: str,
        filters: dict[str, Any],
        limit: int = 100,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search records by filters with pagination."""

    @abstractmethod
    async def get_tables(self) -> list[str]:
        """List all tables or collections."""

    @abstractmethod
    async def get_schema(self, table: str) -> list[dict[str, Any]]:
        """Get column/field definitions for a table."""

    @abstractmethod
    async def create_table(self, table: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
        """Create a new table/collection with the given column definitions."""

    @abstractmethod
    async def drop_table(self, table: str) -> dict[str, Any]:
        """Drop a table/collection."""

    @abstractmethod
    async def close(self) -> None:
        """Close the database connection."""

    async def __aenter__(self) -> "DatabaseConnector":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


FILTER_SUFFIXES = [
    "__contains",
    "__starts_with",
    "__ends_with",
    "__is_null",
    "__gte",
    "__lte",
    "__gt",
    "__lt",
    "__ne",
    "__eq",
    "__in",
]


def parse_filter_key(key: str) -> tuple[str, str]:
    """Split 'column__operator' into (column, __operator)."""
    for suffix in FILTER_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)], suffix
    return key, "__eq"
