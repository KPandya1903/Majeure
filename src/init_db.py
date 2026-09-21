"""Initialise data/majeure.db and load the PortWatch CSV plus all signal JSON files.

The DDL is read from the ```sql block in docs/SCHEMAS.md, so the docs are the schema. Loading is
idempotent: ports and signals are upserted, and re-running never duplicates rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import ValidationError

import store
from common import DATA_DIR, PORTS_CSV, ROOT, SEA_AREA_LINKS, iso, setup_logging, utcnow
from schemas import XtractSignal

DB_PATH = DATA_DIR / "majeure.db"
SCHEMAS_MD = ROOT / "docs" / "SCHEMAS.md"
# Load order = precedence when the same URL arrives from two sources (first one wins):
# structured data first, then publishers' own feeds, then news aggregators.
SIGNAL_FILES = [DATA_DIR / name for name in (
    "portwatch_signals.json", "gdacs_signals.json", "official_signals.json",
    "policy_signals.json", "rss_signals.json", "gdelt_signals.json", "google_news_signals.json",
)]

SIGNAL_COLUMNS = [
    "signal_id", "source", "title", "source_url", "published_at", "event_type", "severity",
    "location_name", "location_kind", "location_port_id", "location_country", "location_lat", "location_lon",
    "match_method", "raw_text", "metadata", "ingested_at",
]
# Refreshed on re-ingest; `status` and the first `ingested_at` are kept.
_UPDATABLE = [c for c in SIGNAL_COLUMNS if c not in ("signal_id", "source", "ingested_at")]
UPSERT_SIGNAL = (
    f"INSERT INTO signals ({', '.join(SIGNAL_COLUMNS)}) VALUES ({', '.join(':' + c for c in SIGNAL_COLUMNS)}) "
    f"ON CONFLICT (signal_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in _UPDATABLE)}"
)

log = logging.getLogger("majeure.db")


def load_ddl(path: Path = SCHEMAS_MD) -> str:
    blocks = re.findall(r"```sql\n(.*?)```", path.read_text(encoding="utf-8"), re.S)
    ddl = [b for b in blocks if "CREATE TABLE" in b]
    if len(ddl) != 1:
        raise RuntimeError(f"expected exactly one ```sql block with CREATE TABLE in {path}, found {len(ddl)}")
    return ddl[0]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return store.connect(db_path)  # store.connect sets PRAGMA foreign_keys for SQLite


def table_columns(conn, table: str) -> list[str]:
    return store.table_columns(conn, table)


def migrate_source_check(conn: sqlite3.Connection, ddl: str) -> bool:
    """Rebuild `signals` when SCHEMAS.md has widened its source CHECK since the DB was created.

    `CREATE TABLE IF NOT EXISTS` silently keeps the old constraint, so adding a source to the docs
    would otherwise fail every insert from that source with a CHECK violation and nothing else --
    which reads as "the new fetcher is broken" rather than "the table is out of date". SQLite cannot
    ALTER a CHECK, so the table is copied into a new one; signals are reloaded from the JSON files on
    every run, but decision_logs references them, hence the FK-off copy rather than a DROP.
    """
    if store.is_postgres():
        # Postgres CAN alter a CHECK in place, which is why this does not need SQLite's
        # rebuild-the-table dance -- but it still has to happen. Skipping it entirely meant adding
        # a source to the docs left the deployed constraint stale, and every insert from that
        # source failed while the run still reported "ok".
        wanted = re.search(r"CREATE TABLE IF NOT EXISTS signals \((.*?)\n\);", ddl, re.S)
        if not wanted:
            return False
        clause = re.search(r"source\s+TEXT NOT NULL CHECK \(source IN \((.*?)\)\)", wanted.group(1), re.S)
        if not clause:
            return False
        sources = sorted(set(re.findall(r"'([a-z_0-9]+)'", clause.group(1))))
        row = conn.execute("""
            SELECT con.conname, pg_get_constraintdef(con.oid)
            FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
            WHERE rel.relname = 'signals' AND con.contype = 'c'
              AND pg_get_constraintdef(con.oid) LIKE '%source%'""").fetchone()
        if row and set(re.findall(r"'([a-z_0-9]+)'", row[1])) == set(sources):
            return False
        values = ", ".join(f"'{s}'" for s in sources)
        with conn:
            if row:
                conn.execute(f'ALTER TABLE signals DROP CONSTRAINT "{row[0]}"')
            conn.execute(f"ALTER TABLE signals ADD CONSTRAINT signals_source_check "
                         f"CHECK (source IN ({values}))")
        log.info("signals.source CHECK updated in Postgres (%d sources)", len(sources))
        return True
    current = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'signals'").fetchone()
    if not current:
        return False  # fresh database: the CREATE below makes it correctly first time
    wanted = re.search(r"CREATE TABLE IF NOT EXISTS signals \((.*?)\n\);", ddl, re.S)
    if not wanted:
        return False
    sources_now = set(re.findall(r"'([a-z_0-9]+)'", (re.search(r"source\s+TEXT NOT NULL CHECK \(source IN \((.*?)\)\)",
                                                               current[0], re.S) or [None, ""])[1]))
    sources_want = set(re.findall(r"'([a-z_0-9]+)'", (re.search(r"source\s+TEXT NOT NULL CHECK \(source IN \((.*?)\)\)",
                                                                wanted.group(1), re.S) or [None, ""])[1]))
    if not sources_want or sources_now == sources_want:
        return False
    log.info("signals.source CHECK is out of date (adding %s); rebuilding the table",
             ", ".join(sorted(sources_want - sources_now)) or "none")
    columns = ", ".join(table_columns(conn, "signals"))
    body = wanted.group(1).replace("CREATE TABLE IF NOT EXISTS signals", "")
    conn.execute("PRAGMA foreign_keys = OFF")
    # Renaming the LIVE table first would make SQLite helpfully rewrite every other table's REFERENCES
    # clause to point at the temporary name, leaving signal_ports and decision_logs referencing a table
    # that the next statement drops. Build the replacement under a temporary name instead, and keep
    # legacy_alter_table on for the final rename so the same rewrite does not happen in reverse.
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        with conn:
            conn.execute(f"CREATE TABLE signals_new ({body}\n)")
            conn.execute(f"INSERT INTO signals_new ({columns}) SELECT {columns} FROM signals")
            conn.execute("DROP TABLE signals")
            conn.execute("ALTER TABLE signals_new RENAME TO signals")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _native(value):
    return None if isinstance(value, float) and math.isnan(value) else value


def load_ports(conn: sqlite3.Connection, csv_path: Path) -> int:
    if not csv_path.exists():
        log.warning("%s not found (run fetch_portwatch.py); skipping ports", csv_path)
        return 0
    df = pd.read_csv(csv_path, dtype={"port_id": str})
    if "updated_at" not in df.columns:
        df["updated_at"] = iso(utcnow())
    cols = [c for c in table_columns(conn, "ports") if c in df.columns]
    rows = [tuple(_native(v) for v in rec.values()) for rec in df[cols].to_dict("records")]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "port_id")
    with conn:
        conn.executemany(
            f"INSERT INTO ports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT (port_id) DO UPDATE SET {updates}", rows)
    log.info("ports: upserted %d rows from %s", len(rows), csv_path.name)
    return len(rows)


def signal_row(sig: XtractSignal) -> dict:
    loc = sig.port_location
    return {
        "signal_id": sig.signal_id, "source": sig.source, "title": sig.title, "source_url": sig.source_url,
        "published_at": iso(sig.published_at), "event_type": sig.event_type, "severity": sig.severity,
        "location_name": loc and loc.name, "location_kind": loc and loc.kind, "location_port_id": loc and loc.port_id,
        "location_country": loc and loc.country, "location_lat": loc and loc.lat, "location_lon": loc and loc.lon,
        "match_method": loc and loc.match_method, "raw_text": sig.raw_text,
        "metadata": json.dumps(sig.metadata, ensure_ascii=False, default=str), "ingested_at": iso(sig.ingested_at),
    }


def load_signals(conn: sqlite3.Connection, path: Path) -> Counter:
    stats = Counter()
    if not path.exists():
        log.warning("%s not found; skipping", path.name)
        return stats
    with conn:
        for i, obj in enumerate(json.loads(path.read_text(encoding="utf-8"))):
            try:
                sig = XtractSignal.model_validate(obj)
            except ValidationError as exc:
                stats["invalid"] += 1
                log.warning("%s[%d] failed validation: %s", path.name, i, exc.errors()[0])
                continue
            existing = conn.execute("SELECT source FROM signals WHERE signal_id = ?", (sig.signal_id,)).fetchone()
            if existing and existing[0] != sig.source:
                stats["cross_source_duplicate"] += 1  # same article already loaded from a higher-precedence source
                log.debug("%s already loaded from %s: %s", sig.signal_id, existing[0], sig.source_url)
                continue
            conn.execute(UPSERT_SIGNAL, signal_row(sig))
            stats["updated" if existing else "inserted"] += 1

            port_ids = dict.fromkeys(sig.related_port_ids)
            if sig.port_location and sig.port_location.port_id:
                port_ids = {sig.port_location.port_id: None, **port_ids}
            for area in [p for p in port_ids if p in SEA_AREA_LINKS]:  # a sea implies its straits
                port_ids.update(dict.fromkeys(SEA_AREA_LINKS[area]))
            primary = sig.port_location.port_id if sig.port_location else None
            conn.execute("DELETE FROM signal_ports WHERE signal_id = ?", (sig.signal_id,))
            conn.executemany("INSERT INTO signal_ports (signal_id, port_id, is_primary) VALUES (?, ?, ?)",
                             [(sig.signal_id, p, int(p == primary)) for p in port_ids])
    log.info("signals: %s -> %s", path.name, dict(stats) or "empty")
    return stats


# Columns added after the first release: added in place so existing databases keep their data.
MIGRATIONS = {
    "decision_logs": {"event_id": "TEXT", "estimated_cost_usd": "REAL", "force_majeure_applicable": "INTEGER"},
    # Provenance for the shipment book: which parts are public-registry fact and which are generated.
    "shipments": {"imo": "TEXT", "vessel_source": "TEXT", "carrier_source": "TEXT", "terms_source": "TEXT"},
    # A force majeure notice is only worth anything if you can show who it went to and when.
    "action_drafts": {"sent_at": "TEXT", "sent_to": "TEXT", "provider_message_id": "TEXT"},
}
TRAFFIC_FILES = {"traffic_daily": DATA_DIR / "traffic_daily.csv", "traffic_stats": DATA_DIR / "traffic_stats.csv"}


def migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = set(table_columns(conn, table))
        for column, sql_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                log.info("migrated: added %s.%s", table, column)


def load_traffic(conn: sqlite3.Connection) -> None:
    """Replace traffic tables with the CSVs written by fetch_traffic.py (if present)."""
    for table, path in TRAFFIC_FILES.items():
        if not path.exists():
            log.info("%s not found (run fetch_traffic.py for measured traffic); skipping", path.name)
            continue
        df = pd.read_csv(path, dtype={"location_id": str, "date": str, "latest_date": str})
        cols = [c for c in table_columns(conn, table) if c in df.columns]
        rows = [tuple(_native(v) for v in rec.values()) for rec in df[cols].to_dict("records")]
        with conn:
            conn.execute(f"DELETE FROM {table}")
            conn.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        log.info("%s: loaded %d rows", table, len(rows))


def summarise(conn: sqlite3.Connection) -> None:
    for table in ("ports", "signals", "signal_ports", "events", "traffic_stats", "shipments", "decision_logs",
                  "action_drafts"):
        log.info("  %-14s %6d rows", table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    by_source = conn.execute("SELECT source, COUNT(*) FROM signals GROUP BY source ORDER BY 2 DESC").fetchall()
    by_type = conn.execute("SELECT event_type, COUNT(*) FROM signals GROUP BY event_type ORDER BY 2 DESC").fetchall()
    log.info("  signals by source: %s", dict(by_source))
    log.info("  signals by event_type: %s", dict(by_type))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--reset", action="store_true", help="delete the database first (drops shipments and decision_logs too)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.reset and args.db.exists():
        args.db.unlink()
        log.info("deleted %s", args.db)

    conn = connect(args.db)
    try:
        ddl = store.translate_ddl(load_ddl())
        migrate_source_check(conn, ddl)  # must run before the CREATEs: they are IF NOT EXISTS no-ops
        conn.executescript(ddl)
        migrate(conn)
        log.info("schema ready: %s", args.db)
        load_ports(conn, PORTS_CSV)
        totals = sum((load_signals(conn, path) for path in SIGNAL_FILES), Counter())
        from xtract import apply_xtract  # re-apply Nemotron refinements that the upsert just overwrote
        apply_xtract(conn)
        load_traffic(conn)
        summarise(conn)
    finally:
        conn.close()
    return 1 if totals["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
