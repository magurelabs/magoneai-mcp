"""CSV parsing, type inference and value conversion, carried across from v1.

Recovered from `/app/src/mcp/servers/csv_to_sql/tools.py` in the v1 image.
The inference and conversion rules are v1's, unchanged, so a CSV previews and
imports exactly as it did. What is new is `quote_ident` and
`validate_schema_name`: v1 interpolated the `schema` argument and caller-supplied
column names into SQL inside double quotes without escaping them, and ran the
result through the simple-query protocol, which accepts several statements.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

# A single CSV field may be as large as the request body allows; the csv
# module's default of 128 KiB refused a long text cell with "field larger than
# field limit".
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]
TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
]


def generate_import_id() -> str:
    """`imp_{timestamp}_{random}`, as v1 generated it."""
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")  # noqa: DTZ003 - v1 format
    return f"imp_{timestamp}_{uuid.uuid4().hex[:8]}"


class ColumnType(str, Enum):
    """PostgreSQL column types."""

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    BIGINT = "BIGINT"
    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    JSON = "JSON"


@dataclass
class ColumnInfo:
    """Information about a CSV column."""

    name: str
    original_name: str
    inferred_type: ColumnType
    nullable: bool
    sample_values: list[str]


def quote_ident(name: str) -> str:
    """A PostgreSQL identifier, quoted so no value can end it early."""
    return '"' + name.replace('"', '""') + '"'


def validate_schema_name(schema: str) -> str:
    """A schema name is an identifier, never a fragment of SQL."""
    value = (schema or "public").strip()
    if not _SCHEMA_NAME.match(value):
        raise ValueError(
            f"schema {schema!r} is not a valid identifier (letters, digits, underscore)"
        )
    return value


def sanitize_column_name(name: str) -> str:
    """Lowercase, underscores for anything else, letter or underscore first, 63 chars."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")
    if sanitized and sanitized[0].isdigit():
        sanitized = f"col_{sanitized}"
    if not sanitized:
        sanitized = "column"
    return sanitized[:63]


def make_column_names_unique(names: list[str]) -> list[str]:
    """Append `_N` to duplicates."""
    seen: dict[str, int] = {}
    result = []
    for name in names:
        if name in seen:
            seen[name] += 1
            unique_name = f"{name}_{seen[name]}"
            while unique_name in seen:
                seen[name] += 1
                unique_name = f"{name}_{seen[name]}"
            result.append(unique_name)
            seen[unique_name] = 0
        else:
            seen[name] = 0
            result.append(name)
    return result


def sanitize_table_name(name: str) -> str:
    """Drop a file extension, then sanitize like a column."""
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return sanitize_column_name(name)


def infer_column_type(values: list[str]) -> tuple[ColumnType, bool]:
    """(type, nullable) from sample values."""
    non_empty_values = [v.strip() for v in values if v.strip()]
    nullable = len(non_empty_values) < len(values)

    if not non_empty_values:
        return ColumnType.TEXT, True

    bool_values = {"true", "false", "yes", "no", "1", "0", "t", "f", "y", "n"}
    if all(v.lower() in bool_values for v in non_empty_values):
        return ColumnType.BOOLEAN, nullable

    try:
        parsed = [int(v.replace(",", "")) for v in non_empty_values]
        if max(abs(p) for p in parsed) > 2147483647:
            return ColumnType.BIGINT, nullable
        return ColumnType.INTEGER, nullable
    except ValueError:
        pass

    try:
        for v in non_empty_values:
            Decimal(v.replace(",", ""))
        return ColumnType.NUMERIC, nullable
    except InvalidOperation:
        pass

    for fmt in DATE_FORMATS:
        try:
            for v in non_empty_values:
                datetime.strptime(v, fmt)  # noqa: DTZ007
            return ColumnType.DATE, nullable
        except ValueError:
            continue

    for fmt in TIMESTAMP_FORMATS:
        try:
            for v in non_empty_values:
                datetime.strptime(v.replace("Z", ""), fmt.replace("Z", ""))  # noqa: DTZ007
            return ColumnType.TIMESTAMP, nullable
        except ValueError:
            continue

    return ColumnType.TEXT, nullable


def convert_value(value: str, col_type: ColumnType) -> Any:
    """A CSV string as the Python value asyncpg binds for `col_type`."""
    value = value.strip()
    if not value:
        return None
    if col_type == ColumnType.BOOLEAN:
        return value.lower() in ("true", "yes", "1", "t", "y")
    if col_type in (ColumnType.INTEGER, ColumnType.BIGINT):
        return int(value.replace(",", ""))
    if col_type == ColumnType.NUMERIC:
        return Decimal(value.replace(",", ""))
    if col_type == ColumnType.DATE:
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {value}")
    if col_type == ColumnType.TIMESTAMP:
        for fmt in TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(value.replace("Z", ""), fmt.replace("Z", ""))  # noqa: DTZ007
            except ValueError:
                continue
        raise ValueError(f"Cannot parse timestamp: {value}")
    return value


def read_rows(csv_content: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_content)))


def preview(
    csv_content: str, has_header: bool = True, preview_rows: int = 5, sample_rows: int = 100
) -> dict[str, Any]:
    """Column definitions and sample rows, in v1's response shape."""
    rows = read_rows(csv_content)
    if not rows:
        raise ValueError("CSV file is empty")

    if has_header:
        header = rows[0]
        data_rows = rows[1:]
    else:
        header = [f"column_{i + 1}" for i in range(len(rows[0]))]
        data_rows = rows

    sample_data = data_rows[:sample_rows]
    unique_names = make_column_names_unique([sanitize_column_name(c) for c in header])

    columns = []
    for i, col_name in enumerate(header):
        col_values = [row[i] if i < len(row) else "" for row in sample_data]
        col_type, nullable = infer_column_type(col_values)
        columns.append(
            ColumnInfo(
                name=unique_names[i],
                original_name=col_name,
                inferred_type=col_type,
                nullable=nullable,
                sample_values=col_values[:5],
            )
        )

    preview_data = []
    for row in data_rows[:preview_rows]:
        preview_data.append(
            {col.name: (row[i] if i < len(row) else None) for i, col in enumerate(columns)}
        )

    return {
        "columns": [
            {
                "name": col.name,
                "original_name": col.original_name,
                "inferred_type": col.inferred_type.value,
                "nullable": col.nullable,
                "sample_values": col.sample_values,
            }
            for col in columns
        ],
        "row_count": len(data_rows),
        "preview_rows": preview_data,
        "has_header": has_header,
    }


def columns_from_arguments(raw: list[dict[str, Any]] | None) -> list[ColumnInfo]:
    """Column definitions a caller sent back, checked.

    Each name is put through `sanitize_column_name`, which leaves a name that
    `preview_csv` produced unchanged and turns anything else into a plain
    identifier. v1 used the caller's name as given, inside double quotes it
    did not escape.
    """
    if not raw:
        raise ValueError("columns is required: pass the column definitions from preview_csv")
    columns = []
    valid = ", ".join(t.value for t in ColumnType)
    for index, col in enumerate(raw):
        if not isinstance(col, dict) or not col.get("name"):
            raise ValueError(f"column {index} has no name")
        try:
            col_type = ColumnType(str(col.get("inferred_type", "")).upper())
        except ValueError:
            raise ValueError(
                f"column {col['name']!r} has inferred_type {col.get('inferred_type')!r}; "
                f"use one of {valid}"
            ) from None
        columns.append(
            ColumnInfo(
                name=sanitize_column_name(str(col["name"])),
                original_name=str(col.get("original_name") or col["name"]),
                inferred_type=col_type,
                nullable=bool(col.get("nullable", True)),
                sample_values=[],
            )
        )
    return columns
