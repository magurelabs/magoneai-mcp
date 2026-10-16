"""Where training lives: in the caller's own database.

v1 kept every caller's training in ONE ChromaDB directory on the server's disk,
with one collection per context name, so two tenants who both created a
context called `sales` trained and queried the same collection, and
`list_contexts` read a contexts table in whichever database the last
`connect_database` call had pointed the whole server at.

Here the training store is two tables in the database the caller connects to:

- `_vanna_contexts`, the same table v1 kept there, under the same name and
  shape, so contexts created by v1 still list.
- `_vanna_training`, one row per trained item (`ddl`, `documentation` or a
  question/`sql` pair), labelled with its context (`''` is the default
  context).

Isolation follows from that rather than being added to it. A caller reaches a
training set only by holding credentials that open the database it lives in,
two callers sharing training means two callers sharing a database, and the
server holds no state between calls, so any replica answers any call. Nothing
about the connection is written anywhere; what is written is what the caller
explicitly trained, in their own database.

Retrieval is lexical: items are ranked by how many of the question's words
they share, up to `N_RESULTS` per kind, the count v1's vector store returned.
v1 ranked by embedding similarity from a model the server downloaded at first
use; the trade is recorded in the pull request that made it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

CONTEXTS_TABLE = "_vanna_contexts"
TRAINING_TABLE = "_vanna_training"
BOOKKEEPING_TABLES = (CONTEXTS_TABLE, TRAINING_TABLE, "_user_data_tables")

DEFAULT_CONTEXT = ""
N_RESULTS = 10
MAX_ITEMS_LOADED = 5000

KINDS = ("ddl", "documentation", "sql")

_CONTEXT_NAME = re.compile(r"^[A-Za-z0-9_]{1,255}$")
_WORD = re.compile(r"[a-z0-9]+")

CONTEXTS_DDL = f"""
CREATE TABLE IF NOT EXISTS "{CONTEXTS_TABLE}" (
    name VARCHAR(255) PRIMARY KEY,
    description TEXT,
    tables_json JSONB NOT NULL DEFAULT '[]',
    trained_items INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
)
"""

TRAINING_DDL = f"""
CREATE TABLE IF NOT EXISTS "{TRAINING_TABLE}" (
    id BIGSERIAL PRIMARY KEY,
    context VARCHAR(255) NOT NULL DEFAULT '',
    kind VARCHAR(16) NOT NULL,
    question TEXT,
    content TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (context, kind, content_hash)
)
"""


class ContextNameError(ValueError):
    """A context name that is not alphanumeric with underscores."""


def context_key(context: str | None) -> str:
    """The stored label for a context: its name, or `''` for the default."""
    if context is None or context == "":
        return DEFAULT_CONTEXT
    if not _CONTEXT_NAME.match(context):
        raise ContextNameError("Context name must be alphanumeric with underscores only")
    return context


async def table_exists(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f'"{name}"'))


async def ensure_tables(conn: Any) -> None:
    await conn.execute(CONTEXTS_DDL)
    await conn.execute(TRAINING_DDL)


def _hash(question: str | None, content: str) -> str:
    return hashlib.sha256(f"{question or ''}\x00{content}".encode()).hexdigest()


async def add_item(
    conn: Any, context: str, kind: str, content: str, question: str | None = None
) -> None:
    """Store one trained item. Training the same item twice stores it once."""
    await conn.execute(
        f"""
        INSERT INTO "{TRAINING_TABLE}" (context, kind, question, content, content_hash)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (context, kind, content_hash) DO NOTHING
        """,  # noqa: S608 - constant table name
        context,
        kind,
        question,
        content,
        _hash(question, content),
    )


async def load_items(conn: Any, context: str) -> list[dict[str, Any]]:
    if not await table_exists(conn, TRAINING_TABLE):
        return []
    rows = await conn.fetch(
        f"""
        SELECT id, kind, question, content FROM "{TRAINING_TABLE}"
        WHERE context = $1 ORDER BY id DESC LIMIT $2
        """,  # noqa: S608 - constant table name
        context,
        MAX_ITEMS_LOADED,
    )
    return [dict(r) for r in rows]


async def count_items(conn: Any, context: str) -> int:
    if not await table_exists(conn, TRAINING_TABLE):
        return 0
    return int(
        await conn.fetchval(
            f'SELECT COUNT(*) FROM "{TRAINING_TABLE}" WHERE context = $1',  # noqa: S608
            context,
        )
    )


async def clear_items(conn: Any, context: str) -> int:
    if not await table_exists(conn, TRAINING_TABLE):
        return 0
    status = await conn.execute(
        f'DELETE FROM "{TRAINING_TABLE}" WHERE context = $1',  # noqa: S608
        context,
    )
    return int(status.split()[-1]) if status.startswith("DELETE") else 0


def as_training_data(item: dict[str, Any]) -> dict[str, Any]:
    """An item in the column names vanna's `get_training_data` used."""
    return {
        "id": str(item["id"]),
        "training_data_type": item["kind"],
        "question": item.get("question"),
        "content": item["content"],
    }


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower().replace("_", " "))) | set(
        re.findall(r"[a-z0-9_]+", (text or "").lower())
    )


def rank(question: str, items: list[dict[str, Any]], n: int = N_RESULTS) -> list[dict[str, Any]]:
    """The `n` items sharing the most words with `question`, newest first on ties."""
    if len(items) <= n:
        return list(items)
    asked = _words(question)
    scored = []
    for position, item in enumerate(items):
        text = f"{item.get('question') or ''} {item['content']}"
        words = _words(text)
        overlap = len(asked & words)
        score = overlap / math.sqrt(len(words) or 1)
        scored.append((-score, position, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in scored[:n]]


def context_row(row: Any) -> dict[str, Any]:
    tables = row["tables_json"]
    if isinstance(tables, str):
        tables = json.loads(tables) if tables else []
    return {
        "name": row["name"],
        "description": row["description"],
        "tables": tables or [],
        "trained_items": row["trained_items"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
