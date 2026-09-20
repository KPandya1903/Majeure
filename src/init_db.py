"""Initialise the Snowflake schema and load the PortWatch CSV plus all signal JSON files.

The DDL is read from docs/SNOWFLAKE_SCHEMA.sql, so the docs are the schema. Loading is
idempotent: ports and signals are upserted (via db.upsert's MERGE), and re-running never
duplicates rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import ValidationError

import db
from common import DATA_DIR, PORTS_CSV, ROOT, SEA_AREA_LINKS, iso, setup_logging, utcnow
from schemas import XtractSignal

SNOWFLAKE_SCHEMA_SQL = ROOT / "docs" / "SNOWFLAKE_SCHEMA.sql"
# Load order = precedence when the same URL arrives from two sources (first one wins):
# structured data first, then publishers' own feeds, then news aggregators.
SIGNAL_FILES = [DATA_DIR / name for name in (
    "portwatch_signals.json", "gdacs_signals.json", "rss_signals.json", "gdelt_signals.json", "google_news_signals.json",
)]

SIGNAL_COLUMNS = [
    "signal_id", "source", "title", "source_url", "published_at", "event_type", "severity",
    "location_name", "location_kind", "location_port_id", "location_country", "location_lat", "location_lon",
    "match_method", "raw_text", "metadata", "ingested_at",
]

# All tables, in an order safe to DROP in for --reset (children before the parents they
# REFERENCE — not enforced by Snowflake, but keeps the intent readable) and safe to CREATE in
# reverse. Kept here (not introspected) since Snowflake's DDL execution is one statement at a
# time and dependency order isn't derivable from the DDL text alone.
ALL_TABLES = ["alerts_sent", "xtract_results", "action_drafts", "event_signals", "events",
              "decision_logs", "shipments", "signal_ports", "signals", "traffic_daily",
              "traffic_stats", "ports"]

log = logging.getLogger("portguard.db")


def load_ddl(path: Path = SNOWFLAKE_SCHEMA_SQL) -> str:
    return path.read_text(encoding="utf-8")


def _native(value):
    return None if isinstance(value, float) and math.isnan(value) else value


def load_ports(conn, csv_path: Path) -> int:
    if not csv_path.exists():
        log.warning("%s not found (run fetch_portwatch.py); skipping ports", csv_path)
        return 0
    df = pd.read_csv(csv_path, dtype={"port_id": str})
    if "updated_at" not in df.columns:
        df["updated_at"] = iso(utcnow())
    cols = [c for c in db.table_columns(conn, "ports") if c in df.columns]
    rows = [tuple(_native(v) for v in rec.values()) for rec in df[cols].to_dict("records")]
    with db.transaction(conn):
        n = db.upsert(conn, "ports", cols, ["port_id"], rows)
    log.info("ports: upserted %d rows from %s", n, csv_path.name)
    return n


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


def load_signals(conn, path: Path) -> Counter:
    stats = Counter()
    if not path.exists():
        log.warning("%s not found; skipping", path.name)
        return stats
    with db.transaction(conn):
        for i, obj in enumerate(json.loads(path.read_text(encoding="utf-8"))):
            try:
                sig = XtractSignal.model_validate(obj)
            except ValidationError as exc:
                stats["invalid"] += 1
                log.warning("%s[%d] failed validation: %s", path.name, i, exc.errors()[0])
                continue
            existing = db.fetchone(conn, "SELECT source FROM signals WHERE signal_id = ?", (sig.signal_id,))
            if existing and existing["source"] != sig.source:
                stats["cross_source_duplicate"] += 1  # same article already loaded from a higher-precedence source
                log.debug("%s already loaded from %s: %s", sig.signal_id, existing["source"], sig.source_url)
                continue
            row = signal_row(sig)
            db.upsert(conn, "signals", SIGNAL_COLUMNS, ["signal_id"], [tuple(row[c] for c in SIGNAL_COLUMNS)])
            stats["updated" if existing else "inserted"] += 1

            port_ids = dict.fromkeys(sig.related_port_ids)
            if sig.port_location and sig.port_location.port_id:
                port_ids = {sig.port_location.port_id: None, **port_ids}
            for area in [p for p in port_ids if p in SEA_AREA_LINKS]:  # a sea implies its straits
                port_ids.update(dict.fromkeys(SEA_AREA_LINKS[area]))
            primary = sig.port_location.port_id if sig.port_location else None
            db.execute(conn, "DELETE FROM signal_ports WHERE signal_id = ?", (sig.signal_id,))
            db.executemany(conn, "INSERT INTO signal_ports (signal_id, port_id, is_primary) VALUES (?, ?, ?)",
                          [(sig.signal_id, p, int(p == primary)) for p in port_ids])
    log.info("signals: %s -> %s", path.name, dict(stats) or "empty")
    return stats


# Columns added after the first release: added in place so existing databases keep their data.
# `ADD COLUMN IF NOT EXISTS` is a Snowflake extension — no need to introspect first.
MIGRATIONS = {
    "decision_logs": {"event_id": "TEXT", "estimated_cost_usd": "REAL", "force_majeure_applicable": "INTEGER"},
}
TRAFFIC_FILES = {"traffic_daily": DATA_DIR / "traffic_daily.csv", "traffic_stats": DATA_DIR / "traffic_stats.csv"}


def migrate(conn) -> None:
    with db.transaction(conn):
        for table, columns in MIGRATIONS.items():
            for column, sql_type in columns.items():
                db.execute(conn, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def load_traffic(conn) -> None:
    """Replace traffic tables with the CSVs written by fetch_traffic.py (if present)."""
    for table, path in TRAFFIC_FILES.items():
        if not path.exists():
            log.info("%s not found (run fetch_traffic.py for measured traffic); skipping", path.name)
            continue
        df = pd.read_csv(path, dtype={"location_id": str, "date": str, "latest_date": str})
        cols = [c for c in db.table_columns(conn, table) if c in df.columns]
        rows = [tuple(_native(v) for v in rec.values()) for rec in df[cols].to_dict("records")]
        with db.transaction(conn):
            db.execute(conn, f"DELETE FROM {table}")
            db.executemany(conn, f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        log.info("%s: loaded %d rows", table, len(rows))


def summarise(conn) -> None:
    for table in ("ports", "signals", "signal_ports", "events", "traffic_stats", "shipments", "decision_logs",
                  "action_drafts"):
        n = db.fetchone(conn, f"SELECT COUNT(*) AS n FROM {table}")["n"]
        log.info("  %-14s %6d rows", table, n)
    by_source = [(r["source"], r["n"]) for r in
                db.fetchall(conn, "SELECT source, COUNT(*) AS n FROM signals GROUP BY source ORDER BY 2 DESC")]
    by_type = [(r["event_type"], r["n"]) for r in
              db.fetchall(conn, "SELECT event_type, COUNT(*) AS n FROM signals GROUP BY event_type ORDER BY 2 DESC")]
    log.info("  signals by source: %s", dict(by_source))
    log.info("  signals by event_type: %s", dict(by_type))


def reset(conn) -> None:
    with db.transaction(conn):
        for table in ALL_TABLES:
            db.execute(conn, f"DROP TABLE IF EXISTS {table}")
            log.info("dropped %s", table)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="drop every table first (loses all data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    conn = db.connect()
    try:
        if args.reset:
            reset(conn)
        with db.transaction(conn):
            db.run_ddl(conn, load_ddl())
        migrate(conn)
        log.info("schema ready")
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
