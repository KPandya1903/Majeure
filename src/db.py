"""Snowflake connection and query layer — the one seam every script goes through instead of
calling `sqlite3.connect()` directly (see docs/SNOWFLAKE_SCHEMA.sql for why the DDL differs).

Three things every call site needs that raw `snowflake.connector` doesn't give you for free:

1. **Lowercase column names.** Snowflake upper-cases unquoted identifiers, so a table created as
   `signal_id TEXT` is actually named `SIGNAL_ID` in the catalog, and every SELECT of it comes
   back as `SIGNAL_ID` too. The whole codebase does `row["signal_id"]` / `r.signal_id`
   (pandas itertuples), so every row dict and DataFrame this module returns has its keys
   lowercased before the caller ever sees them.
2. **`?` placeholders everywhere.** `snowflake.connector`'s paramstyle is a module-level
   global (like `sqlite3.paramstyle`), so it's set once here to `"qmark"` — the same `?` style
   sqlite3 uses — so most call sites don't change at all.
3. **Upserts.** Snowflake has no `INSERT ... ON CONFLICT DO UPDATE`. `upsert()` below loads rows
   into a session-scoped temp table, then `MERGE`s them into the target — the standard Snowflake
   upsert pattern, and the only one that's still one call site regardless of row count.

Env vars (see .env.example): SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA (default PUBLIC), SNOWFLAKE_WAREHOUSE (default COMPUTE_WH),
SNOWFLAKE_ROLE (optional).
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
import snowflake.connector

snowflake.connector.paramstyle = "qmark"

_TEMP_TABLE_RE = re.compile(r"[^A-Za-z0-9_]")


class ConfigError(RuntimeError):
    """A required SNOWFLAKE_* env var is missing."""


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise ConfigError(f"{name} is not set (see .env.example)")
    return val


def connect() -> snowflake.connector.SnowflakeConnection:
    """One connection per call, autocommit off so `with transaction(conn):` blocks are atomic
    the same way `with conn:` was for sqlite3."""
    return snowflake.connector.connect(
        account=_env("SNOWFLAKE_ACCOUNT", required=True),
        user=_env("SNOWFLAKE_USER", required=True),
        password=_env("SNOWFLAKE_PASSWORD", required=True),
        database=_env("SNOWFLAKE_DATABASE", required=True),
        schema=_env("SNOWFLAKE_SCHEMA", "PUBLIC"),
        warehouse=_env("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=_env("SNOWFLAKE_ROLE"),
        autocommit=False,
        client_session_keep_alive=True,
    )


@contextmanager
def transaction(conn: snowflake.connector.SnowflakeConnection) -> Iterator[None]:
    """Commit on success, rollback on exception — the Snowflake equivalent of sqlite3's
    `with conn:`. `conn.execute(...)` doesn't exist on a Snowflake connection (only on a
    cursor), so callers use `cur = conn.cursor(); cur.execute(...)` inside this block."""
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _lower(d: dict) -> dict:
    return {k.lower(): v for k, v in d.items()}


def execute(conn: snowflake.connector.SnowflakeConnection, sql: str, params: Sequence[Any] = ()) -> None:
    """Run one statement, no results expected (INSERT/UPDATE/DELETE/DDL)."""
    conn.cursor().execute(sql, params)


def executemany(conn: snowflake.connector.SnowflakeConnection, sql: str, rows: Sequence[Sequence[Any]]) -> None:
    if rows:
        conn.cursor().executemany(sql, rows)


def fetchall(conn: snowflake.connector.SnowflakeConnection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """Rows as lowercase-keyed dicts (a `sqlite3.Row`-like replacement)."""
    cur = conn.cursor(snowflake.connector.DictCursor)
    cur.execute(sql, params)
    return [_lower(r) for r in cur.fetchall()]


def fetchone(conn: snowflake.connector.SnowflakeConnection, sql: str, params: Sequence[Any] = ()) -> dict | None:
    rows = fetchall(conn, sql, params)
    return rows[0] if rows else None


def read_df(conn: snowflake.connector.SnowflakeConnection, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
    """`pandas.read_sql` equivalent, columns lowercased to match every existing DataFrame call
    site (`df["shipment_id"]`, `row.pol_port_id`, ...)."""
    cur = conn.cursor()
    cur.execute(sql, params)
    df = cur.fetch_pandas_all()
    df.columns = [c.lower() for c in df.columns]
    return df


def run_ddl(conn: snowflake.connector.SnowflakeConnection, sql_text: str) -> None:
    """Execute a `;`-separated block of DDL statements (Snowflake's cursor runs one statement
    per `execute()` call, unlike sqlite3's `executescript`). Comment-only lines are stripped
    per statement so a trailing `-- comment` before the `;` doesn't confuse anything."""
    cur = conn.cursor()
    for raw in sql_text.split(";"):
        stmt = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            cur.execute(stmt)


def table_columns(conn: snowflake.connector.SnowflakeConnection, table: str) -> list[str]:
    """Replacement for `PRAGMA table_info(table)`, in ordinal (declared) order."""
    rows = fetchall(conn, """SELECT column_name FROM information_schema.columns
                             WHERE table_name = ? AND table_schema = CURRENT_SCHEMA()
                             ORDER BY ordinal_position""", (table.upper(),))
    return [r["column_name"] for r in rows]


def upsert(conn: snowflake.connector.SnowflakeConnection, table: str, columns: Sequence[str],
           key_columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """MERGE `rows` (tuples in `columns` order) into `table`, matching on `key_columns`:
    update the rest of the columns on a match, insert on no match. This is Snowflake's
    replacement for every `INSERT ... ON CONFLICT (...) DO UPDATE SET ... excluded.col` in the
    old SQLite code — one implementation instead of bespoke SQL per call site.
    """
    rows = list(rows)
    if not rows:
        return 0
    tmp = f"tmp_upsert_{_TEMP_TABLE_RE.sub('_', table)}"
    cur = conn.cursor()
    cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {tmp} LIKE {table}")
    cur.executemany(f"INSERT INTO {tmp} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows)
    updates = [c for c in columns if c not in key_columns]
    on_clause = " AND ".join(f"t.{c} = s.{c}" for c in key_columns)
    set_clause = ", ".join(f"t.{c} = s.{c}" for c in updates) or f"t.{key_columns[0]} = s.{key_columns[0]}"
    cur.execute(f"""
        MERGE INTO {table} t USING {tmp} s ON {on_clause}
        WHEN MATCHED THEN UPDATE SET {set_clause}
        WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({', '.join(f's.{c}' for c in columns)})
    """)
    cur.execute(f"DROP TABLE IF EXISTS {tmp}")
    return len(rows)
