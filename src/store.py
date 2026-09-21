"""One connection seam for both backends: SQLite locally, Postgres (Supabase) when deployed.

Render's free tier has no persistent disk, so `data/majeure.db` reverts to the committed
snapshot on every redeploy -- everything the cron pipeline ingests in between is thrown away, and
so is every human approve/override. That is the bug this module exists to fix.

The design goal is a *small diff*. Rather than rewriting fifteen scripts, `connect()` returns
something that behaves like a `sqlite3.Connection` in the ways this codebase actually uses one:

    conn.execute(sql, params)          conn.executemany(sql, rows)
    conn.executescript(ddl)            with conn:  # commit on success, rollback on error
    row["col"]   row[0]   dict(row)    a, b, c = row

...and the SQL each call site already writes is translated to Postgres on the way through. Nine
SQLite-specific constructs appear in this repo (`INSERT OR REPLACE/IGNORE`, `json_extract`,
`GROUP_CONCAT`, `?` placeholders); translating them in one tested place is far less risky than
nine hand-edited call sites that drift apart later.

Backend is chosen by `DB_BACKEND` (`sqlite` default, or `postgres`). Postgres needs `DATABASE_URL`
-- use Supabase's **Session pooler** string: the direct `db.<ref>.supabase.co` host is IPv6-only
and Render has no IPv6 egress, and the transaction pooler on :6543 breaks psycopg's prepared
statements. `prepare_threshold=None` below is belt-and-braces for anyone who points this at :6543
anyway.

    python src/store.py --check      # connect, report backend and row counts
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "majeure.db"

log = logging.getLogger("majeure.store")


def backend() -> str:
    """'sqlite' or 'postgres'. Read per-call so tests and the CLI can flip it with os.environ."""
    return (os.environ.get("DB_BACKEND") or "sqlite").strip().lower()


def is_postgres() -> bool:
    return backend() == "postgres"


# --------------------------------------------------------------------------- SQL translation

# `?` -> `%s`, but only outside string literals: a literal '?' in text (or a '%' in a LIKE
# pattern) must survive untouched, and psycopg treats a bare % as a placeholder introducer.
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_INSERT_OR_RE = re.compile(r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
_JSON_EXTRACT_RE = re.compile(r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.(\w+)'\s*\)", re.I)
_GROUP_CONCAT_RE = re.compile(r"\bGROUP_CONCAT\(\s*([\w.]+)\s*\)", re.I)
# SELECT value FROM json_each(col) -- SQLite's JSON table function, used to test membership of a
# JSON array column (transshipment_port_ids, route_chokepoints).
_JSON_EACH_RE = re.compile(r"SELECT\s+value\s+FROM\s+json_each\(\s*([\w.]+)\s*\)", re.I)

# Primary keys, for turning INSERT OR REPLACE into a real ON CONFLICT target. SQLite's OR REPLACE
# resolves against ANY uniqueness constraint; Postgres needs the conflict column named. Every use
# in this repo conflicts on the primary key, which is what these record.
PRIMARY_KEY = {
    "signals": ("signal_id",), "ports": ("port_id",), "shipments": ("shipment_id",),
    "events": ("event_id",), "event_signals": ("event_id", "signal_id"),
    "signal_ports": ("signal_id", "port_id"), "decision_logs": ("decision_id",),
    "action_drafts": ("decision_id", "kind"), "alerts_sent": ("decision_id", "channel"),
    "xtract_results": ("signal_id",), "traffic_stats": ("location_id",),
    "traffic_daily": ("location_id", "date"), "pipeline_state": ("source_group",),
}
# ON CONFLICT needs a column set that a real unique constraint covers, so these must match the
# schema exactly -- Postgres rejects anything else with "no unique or exclusion constraint matching
# the ON CONFLICT specification". check_primary_keys() below asks Postgres itself rather than
# trusting this dict, because getting one wrong (traffic_stats did) only fails at write time.


def _outside_literals(sql: str, fn) -> str:
    """Apply `fn` to the stretches of SQL that are not inside single-quoted string literals."""
    out, last = [], 0
    for m in _LITERAL_RE.finditer(sql):
        out.append(fn(sql[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(sql[last:]))
    return "".join(out)


def translate(sql: str) -> str:
    """Rewrite this repo's SQLite SQL for Postgres. Identity when the backend is SQLite."""
    if not is_postgres():
        return sql

    # json_extract(col, '$.key') -> col::jsonb->>'key'. Values are compared against 1/0 in the
    # SQLite SQL because SQLite has no boolean; JSON booleans come back as the text 'true'/'false'
    # from ->>, so the comparison is rewritten alongside the extraction rather than separately.
    def _json(m: re.Match) -> str:
        return f"({m.group(1)}::jsonb->>'{m.group(2)}')"
    sql = _JSON_EXTRACT_RE.sub(_json, sql)
    sql = re.sub(r"(\(\w[\w.]*::jsonb->>'\w+'\))\s*=\s*1\b", r"\1 = 'true'", sql)
    sql = re.sub(r"(\(\w[\w.]*::jsonb->>'\w+'\))\s*=\s*0\b", r"\1 = 'false'", sql)

    sql = _GROUP_CONCAT_RE.sub(r"string_agg(\1, ',')", sql)

    # json_each(col) -> jsonb_array_elements_text(col::jsonb). Both expand a JSON array into rows;
    # the _text variant is what makes the values plain strings comparable to a TEXT port_id rather
    # than quoted JSON scalars, which would match nothing and fail silently.
    sql = _JSON_EACH_RE.sub(r"SELECT jsonb_array_elements_text(\1::jsonb)", sql)

    # INSERT OR REPLACE/IGNORE -> ON CONFLICT. The conflict target comes from PRIMARY_KEY, and
    # REPLACE updates every non-key column so the statement keeps its upsert semantics.
    # The clause is appended to the END of the statement (after VALUES / SELECT), not spliced in
    # where "OR REPLACE" used to sit -- these are always single INSERT statements.
    if (m := _INSERT_OR_RE.search(sql)):
        mode, table, cols = m.group(1).upper(), m.group(2), m.group(3)
        names = [c.strip() for c in cols.split(",") if c.strip()]
        keys = PRIMARY_KEY.get(table.lower())
        if not keys:
            raise ValueError(f"INSERT OR {mode} INTO {table}: add its primary key to store.PRIMARY_KEY")
        updates = [c for c in names if c not in keys]
        if mode == "IGNORE" or not updates:
            action = "DO NOTHING"
        else:
            action = "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
        sql = sql[:m.start()] + f"INSERT INTO {table} ({cols})" + sql[m.end():]
        sql = f"{sql.rstrip().rstrip(';')} ON CONFLICT ({', '.join(keys)}) {action}"

    # `?` -> `%s` only outside string literals, so a literal '?' in text survives.
    #
    # `%` is different and must be escaped EVERYWHERE, including inside literals. psycopg scans the
    # whole query for placeholders whenever parameters are passed, so a LIKE pattern such as
    # '%Shanghai%' raises "only '%s', '%b', '%t' are allowed as placeholders, got '%S'" -- it cannot
    # tell the literal from a placeholder. Doing the escaping here would be wrong though: psycopg
    # skips that scan entirely when there are no parameters, and '%%' would then reach Postgres
    # verbatim. So the '%' doubling happens in PgConnection.execute(), which knows whether any
    # parameters are actually being sent.
    # Both sqlite3 paramstyles are in use here: `?` with tuples, and `:name` with dicts (the
    # UPSERT statements in init_db.py and load_shipments.py are built that way). psycopg spells the
    # named form `%(name)s`. The negative lookbehind keeps `::jsonb` casts -- including the ones
    # this function just produced -- from being mistaken for a named placeholder.
    def _placeholders(part: str) -> str:
        part = part.replace("?", "%s")
        return re.sub(r"(?<!:):([A-Za-z_]\w*)", r"%(\1)s", part)
    return _outside_literals(sql, _placeholders)


def escape_percent(sql: str) -> str:
    """Double every literal `%` that is not part of a `%s` or `%(name)s` placeholder."""
    return re.sub(r"%(?!s\b)(?!\()", "%%", sql)


def translate_ddl(ddl: str) -> str:
    """Rewrite the SQLite schema in docs/SCHEMAS.md for Postgres. Identity on SQLite.

    Only two constructs in the whole schema are not already portable -- the rest (TEXT, INTEGER,
    CHECK, REFERENCES ... ON DELETE CASCADE, UNIQUE, DEFAULT literals, CREATE INDEX IF NOT EXISTS)
    means the same thing in both engines.

    `GENERATED BY DEFAULT AS IDENTITY` rather than SERIAL is deliberate: BY DEFAULT still accepts an
    explicitly supplied id, which the data migration needs in order to preserve existing
    decision_id and draft_id values and keep action_drafts pointing at the right decisions. ALWAYS
    would reject them.
    """
    if not is_postgres():
        return ddl
    ddl = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                 "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", ddl, flags=re.I)
    # SQLite REAL is a 64-bit float; Postgres REAL is 32-bit and would silently round money.
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl, flags=re.I)


# --------------------------------------------------------------------------- row compatibility


class Row:
    """A `sqlite3.Row` work-alike: subscript by name OR position, iterate VALUES, `dict(row)`.

    Plain dicts are not enough. Call sites in this repo do all four of
    `r["title"]`, `dict(r)`, `dict(r, ports=...)` and `for a, b, c in rows` -- a dict iterates its
    KEYS, so tuple unpacking would silently yield column names instead of values.
    """

    __slots__ = ("_keys", "_values")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]):
        self._keys, self._values = list(keys), list(values)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._keys.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self._keys else default

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._keys, self._values))!r})"


class _Cursor:
    """psycopg cursor wearing sqlite3's clothes: iterable, with fetchone/fetchall/rowcount."""

    def __init__(self, cur):
        self._cur = cur

    def _rows(self) -> list[Row]:
        if self._cur.description is None:
            return []
        names = [d.name for d in self._cur.description]
        return [Row(names, v) for v in self._cur.fetchall()]

    def fetchall(self) -> list[Row]:
        return self._rows()

    def fetchone(self) -> Optional[Row]:
        if self._cur.description is None:
            return None
        names = [d.name for d in self._cur.description]
        value = self._cur.fetchone()
        return Row(names, value) if value is not None else None

    def __iter__(self) -> Iterator[Row]:
        return iter(self._rows())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):  # pragma: no cover - not used by this codebase on Postgres
        return None


class PgConnection:
    """`sqlite3.Connection`-shaped wrapper over psycopg, so call sites need not branch.

    `with conn:` commits on success and rolls back on exception, matching sqlite3 rather than
    psycopg (whose own context manager closes the connection).

    `close()` returns the connection to the pool rather than dropping it, which is what makes
    pooling transparent to every existing call site.
    """

    def __init__(self, conn, pool=None):
        self._conn = conn
        self._pool = pool
        self.row_factory = None  # accepted and ignored: rows are always Row

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        cur = self._conn.cursor()
        args = params if isinstance(params, dict) else (tuple(params) if params else None)
        sql = translate(sql)
        cur.execute(escape_percent(sql) if args else sql, args)
        return _Cursor(cur)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> _Cursor:
        rows = [tuple(r) for r in rows]
        cur = self._conn.cursor()
        if rows:
            cur.executemany(escape_percent(translate(sql)), rows)
        return _Cursor(cur)

    def executescript(self, script: str) -> None:
        """sqlite3 runs a whole `;`-separated block; psycopg is happy to as well, but only
        outside an explicit transaction for some DDL, so it is executed statement by statement."""
        cur = self._conn.cursor()
        for statement in split_statements(script):
            cur.execute(translate(statement))
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._pool is None:
            self._conn.close()
            return
        try:
            # A connection handed back mid-transaction would leak that state to the next caller,
            # which is how one endpoint's failed query becomes another's "current transaction is
            # aborted". Reset it before returning it to the pool.
            self._conn.rollback()
        except Exception:
            pass
        self._pool.putconn(self._conn)

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


def split_statements(script: str) -> list[str]:
    """Split a DDL block into statements on `;`, ignoring `;` inside literals and comments.

    Comments must be stripped as part of the same left-to-right scan that tracks string literals,
    not beforehand and not line-by-line. This schema is full of inline comments that contain
    apostrophes (``-- 'port1188'``, ``-- Render's free tier``) and one that contains a semicolon
    (``-- ';'-joined PortWatch event ids``). Treating those apostrophes as literal delimiters
    desyncs the tracker for the rest of the file: statements silently merge, and the run creates
    some tables while dropping every CREATE INDEX on the floor with no error at all.
    """
    statements, current = [], []
    in_literal, i, n = False, 0, len(script)
    while i < n:
        ch = script[i]
        if not in_literal and ch == "-" and script.startswith("--", i):
            i = script.find("\n", i)          # skip the comment, keep the newline
            if i == -1:
                break
            continue
        if ch == "'":
            # '' inside a literal is an escaped quote, not the end of one.
            if in_literal and script.startswith("''", i):
                current.append("''")
                i += 2
                continue
            in_literal = not in_literal
        if ch == ";" and not in_literal:
            statements.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if (tail := "".join(current).strip()):
        statements.append(tail)
    # PRAGMA is SQLite-only; Postgres enforces foreign keys unconditionally, so there is nothing
    # to translate it into and dropping it is correct rather than a workaround.
    return [s for s in statements if s and not (is_postgres() and s.lstrip().upper().startswith("PRAGMA"))]


# --------------------------------------------------------------------------- connecting


_POOL = None
_POOL_FAILED = False


def _require_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DB_BACKEND=postgres but DATABASE_URL is not set (see .env.example)")
    return url


def _get_pool():
    """A small shared connection pool, created on first use.

    Opening a Postgres connection to Supabase measured **169 ms** -- TCP, TLS and auth -- against
    25 ms for an actual query. Every request opening its own connection therefore spent roughly
    two thirds of its time on handshakes, which no amount of response caching would recover.

    A pool and not a single shared connection: FastAPI runs sync endpoints in a threadpool, so two
    requests genuinely do overlap, and interleaving their transactions on one connection corrupts
    both. `max_size` stays small because Supabase's pooler has its own client limit and this
    service runs one web worker.
    """
    global _POOL, _POOL_FAILED
    if _POOL is not None or _POOL_FAILED:
        return _POOL
    try:
        from psycopg_pool import ConnectionPool
        _POOL = ConnectionPool(
            _require_url(), min_size=1, max_size=4, timeout=20,
            # Checked when a connection is handed out, so a socket Supabase dropped while idle is
            # replaced instead of surfacing as a random failure on an unrelated request.
            check=ConnectionPool.check_connection,
            kwargs={"autocommit": False, "prepare_threshold": None},
            open=True, name="majeure",
        )
    except Exception as exc:  # pool unavailable: keep working, one connection per call
        _POOL_FAILED = True
        log.warning("connection pool unavailable (%s); falling back to per-request connections", exc)
    return _POOL


def connect(db_path: Path = DB_PATH):
    """A connection to whichever backend DB_BACKEND selects."""
    if not is_postgres():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    pool = _get_pool()
    if pool is None:  # pooling unavailable: fall back to a connection per call
        import psycopg
        return PgConnection(psycopg.connect(_require_url(), autocommit=False, prepare_threshold=None))
    return PgConnection(pool.getconn(), pool)


def database_ready(db_path: Path = DB_PATH) -> bool:
    """Is there a database to talk to?

    On SQLite that means the file exists. On Postgres the file is irrelevant -- and asking about it
    is actively harmful: once data/majeure.db stops being committed, every `if not
    db.exists(): abort` guard in the codebase would refuse to run against a perfectly healthy
    Supabase instance, and the API would answer 503 to everything.
    """
    return True if is_postgres() else db_path.exists()


@contextmanager
def session(db_path: Path = DB_PATH):
    """Open a connection, commit on success, and ALWAYS dispose of it afterwards.

    `with connect(...) as conn:` cannot do this. In sqlite3 `with conn:` is a *transaction*, not a
    close -- scripts rely on reopening one repeatedly on a single long-lived connection -- so
    PgConnection.__exit__ deliberately does not release. Pooled connections still have to be given
    back though, and short-lived users that do not are what exhausted the pool in four requests.
    Use this wherever a connection is opened for one unit of work; use connect() when a script
    holds one for its whole run.
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(conn, table: str) -> list[str]:
    """Replacement for `PRAGMA table_info(table)`, in declared order, on either backend."""
    if not is_postgres():
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return [r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = current_schema() ORDER BY ordinal_position",
        (table,))]


def check_primary_keys(conn) -> list[str]:
    """Names of tables whose PRIMARY_KEY entry no unique constraint in Postgres actually backs."""
    if not is_postgres():
        return []
    rows = conn.execute("""
        SELECT tc.table_name,
               string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS cols
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
        WHERE tc.table_schema = current_schema()
          AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        GROUP BY tc.table_name, tc.constraint_name""").fetchall()
    unique: dict[str, set[tuple[str, ...]]] = {}
    for name, cols in rows:
        unique.setdefault(name, set()).add(tuple(cols.split(",")))
    return [t for t, keys in PRIMARY_KEY.items()
            if t in unique and tuple(keys) not in unique[t]]


def table_exists(conn, table: str) -> bool:
    if not is_postgres():
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())
    return bool(conn.execute("SELECT to_regclass(?)", (table,)).fetchone()[0])


TABLES = ("signals", "ports", "shipments", "events", "event_signals", "signal_ports",
          "decision_logs", "action_drafts", "alerts_sent", "xtract_results",
          "traffic_stats", "traffic_daily", "pipeline_state")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="connect and report row counts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    print(f"backend: {backend()}")
    if not args.check:
        return 0
    try:
        conn = connect()
    except Exception as exc:
        print(f"could not connect: {type(exc).__name__}: {exc}")
        return 1
    for table in TABLES:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<16} {count:>7}")
        except Exception as exc:
            print(f"  {table:<16} {type(exc).__name__}: {str(exc).strip()[:60]}")
            conn.rollback()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
