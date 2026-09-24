"""Database connector factory.

Parses the connection URI scheme to pick the right connector.
Supported: postgresql://, mysql://, mongodb://, mongodb+srv://

Uses a module-level pool to cache connections across MCP tool calls.
"""

from .base import DatabaseConnector
from .pool import get_or_create_connection


async def _create_postgresql(connection_string: str) -> DatabaseConnector:
    from .postgresql import PostgreSQLConnector

    connector: DatabaseConnector = PostgreSQLConnector()
    await connector.connect(connection_string)
    return connector


async def _create_mysql(connection_string: str) -> DatabaseConnector:
    from .mysql import MySQLConnector

    connector: DatabaseConnector = MySQLConnector()
    await connector.connect(connection_string)
    return connector


async def _create_mongodb(connection_string: str) -> DatabaseConnector:
    from .mongodb import MongoDBConnector

    connector: DatabaseConnector = MongoDBConnector()
    await connector.connect(connection_string)
    return connector


async def _create_oracle(connection_string: str) -> DatabaseConnector:
    from .oracle import OracleConnector

    connector: DatabaseConnector = OracleConnector()
    await connector.connect(connection_string)
    return connector


async def _create_mssql(connection_string: str) -> DatabaseConnector:
    from .mssql import MSSQLConnector

    connector: DatabaseConnector = MSSQLConnector()
    await connector.connect(connection_string)
    return connector


async def create_connector(connection_string: str) -> DatabaseConnector:
    """Create or retrieve a cached database connector from a connection URI.

    Args:
        connection_string: Database URI (e.g. postgresql://user:pass@host:5432/db)

    Returns:
        Connected DatabaseConnector instance (may be cached).

    Raises:
        ValueError: If the URI scheme is unsupported.
    """
    scheme = connection_string.split("://")[0].lower() if "://" in connection_string else ""

    if scheme in ("postgresql", "postgres"):
        factory = _create_postgresql
    elif scheme == "mysql":
        factory = _create_mysql
    elif scheme in ("mongodb", "mongodb+srv"):
        factory = _create_mongodb
    elif scheme in ("oracle", "oracle+thin", "oracle+thick"):
        factory = _create_oracle
    elif scheme == "mssql":
        factory = _create_mssql
    else:
        supported = "postgresql://, mysql://, mongodb://, oracle://, mssql://"
        raise ValueError(f"Unsupported database URI scheme: '{scheme}'. Supported: {supported}")

    return await get_or_create_connection(connection_string, factory)


__all__ = ["DatabaseConnector", "create_connector"]
