"""Copy data/majeure.db into Postgres (Supabase), then verify both sides agree.

One-shot, idempotent and re-runnable: the schema is created from docs/SCHEMAS.md the same way
init_db.py creates it, rows are upserted on their primary key, and every table's row count is
compared afterwards. Nothing is deleted from SQLite -- it stays the local-dev backend.

Order matters because Postgres enforces foreign keys unconditionally (SQLite only does with
`PRAGMA foreign_keys = ON`), so parents are copied before children.

    python src/migrate_to_postgres.py --dry-run     # report what would move
    python src/migrate_to_postgres.py               # create schema, copy, verify
    python src/migrate_to_postgres.py --verify-only # just diff the row counts
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import store
from common import setup_logging

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "majeure.db"

# Parents before children: every table here is referenced only by tables later in the list.
ORDER = (
    "ports", "pipeline_state", "signals", "signal_ports", "shipments",
    "events", "event_signals", "decision_logs", "action_drafts", "alerts_sent",
    "xtract_results", "traffic_daily", "traffic_stats",
)
# Postgres reports an identity column's next value from its own sequence, which stays at 1 after
# rows are inserted with explicit ids. Left alone, the next natural insert collides with an
# existing key; these are the tables whose sequences must be fast-forwarded afterwards.
IDENTITY = {"decision_logs": "decision_id", "action_drafts": "draft_id"}
BATCH = 500

log = logging.getLogger("majeure.migrate")


def sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def copy_table(src: sqlite3.Connection, dst, table: str, dry_run: bool) -> int:
    columns = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    if not columns:
        log.warning("%s: not present in SQLite, skipping", table)
        return 0
    target = set(store.table_columns(dst, table))
    shared = [c for c in columns if c in target]
    if missing := [c for c in columns if c not in target]:
        log.warning("%s: columns absent from Postgres, not copied: %s", table, ", ".join(missing))
    rows = src.execute(f"SELECT {', '.join(shared)} FROM {table}").fetchall()
    if dry_run or not rows:
        log.info("%-16s %6d rows%s", table, len(rows), " (dry run)" if dry_run else "")
        return len(rows)

    keys = store.PRIMARY_KEY[table]
    updates = [c for c in shared if c not in keys]
    action = (f"DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in updates)}"
              if updates else "DO NOTHING")
    sql = (f"INSERT INTO {table} ({', '.join(shared)}) "
           f"VALUES ({', '.join('?' * len(shared))}) "
           f"ON CONFLICT ({', '.join(keys)}) {action}")
    written = 0
    for start in range(0, len(rows), BATCH):
        chunk = [tuple(r) for r in rows[start:start + BATCH]]
        dst.executemany(sql, chunk)
        dst.commit()
        written += len(chunk)
    log.info("%-16s %6d rows copied", table, written)
    return written


def fix_sequences(dst) -> None:
    """Fast-forward identity sequences past the ids we inserted explicitly."""
    for table, column in IDENTITY.items():
        row = dst.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()
        top = row[0] if row else 0
        if top:
            dst.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), {int(top)}, true)")
            dst.commit()
            log.info("%-16s sequence set to %d", table, top)


def verify(src: sqlite3.Connection, dst) -> int:
    """Compare row counts table by table. Returns the number of mismatches."""
    mismatches = 0
    print(f"  {'table':<16} {'sqlite':>8} {'postgres':>10}")
    for table in ORDER:
        try:
            a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            a = 0
        b = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        flag = "" if a == b else "   <-- MISMATCH"
        mismatches += a != b
        print(f"  {table:<16} {a:>8} {b:>10}{flag}")
    return mismatches


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report row counts, copy nothing")
    parser.add_argument("--verify-only", action="store_true", help="only diff row counts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not DB_PATH.exists():
        log.error("%s not found", DB_PATH)
        return 1
    os.environ["DB_BACKEND"] = "postgres"  # this script is only ever about the Postgres side
    if not os.environ.get("DATABASE_URL"):
        log.error("DATABASE_URL is not set (see .env.example)")
        return 1

    src, dst = sqlite_conn(), store.connect()
    try:
        if not args.verify_only:
            if not args.dry_run:
                from init_db import load_ddl
                for statement in store.split_statements(store.translate_ddl(load_ddl())):
                    dst.execute(statement)
                dst.commit()
                log.info("schema ready in Postgres")
            total = sum(copy_table(src, dst, t, args.dry_run) for t in ORDER)
            log.info("%d rows %s", total, "would be copied" if args.dry_run else "copied")
            if not args.dry_run:
                fix_sequences(dst)
        if args.dry_run:
            return 0
        print()
        mismatches = verify(src, dst)
        print()
        if mismatches:
            log.error("%d table(s) disagree between the two backends", mismatches)
            return 1
        log.info("every table matches; Postgres is ready to serve")
        return 0
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    sys.exit(main())
