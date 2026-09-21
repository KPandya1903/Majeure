"""Xtract: let Nemotron re-read news signals and correct their event type, location and severity.

The rule-based tags from ingestion are a recall-oriented first pass. This step sends recent news
signals to Nemotron in batches and stores its reading in `xtract_results`; apply_xtract() then
overwrites the signal's event_type / severity / location (match_method = "xtract"). init_db.py
calls apply_xtract() after every load, so the refinements survive re-ingestion.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

import requests

import store
from common import CHOKEPOINTS, PortGazetteer, iso, setup_logging, utcnow
from schemas import EventType

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "majeure.db"
STRUCTURED_SOURCES = ("portwatch", "gdacs")
EVENT_TYPES = [e.value for e in EventType]
SEVERITIES = ("low", "medium", "high", "critical")
BATCH_SIZE = 10

SYSTEM_PROMPT = f"""You are an information-extraction model for maritime supply-chain risk.
You receive a JSON array of news items (id, title, text). For EACH item return one object:
{{"id": "<same id>", "is_disruption": true|false,
  "event_type": one of {EVENT_TYPES},
  "location": "<the single most affected port, strait, canal or sea, as a proper name, or null>",
  "severity": "low"|"medium"|"high"|"critical"|null,
  "expected_duration_days": <number or null>,
  "confidence": <0-1>}}
is_disruption is true only if the item reports something that can delay or stop ships or cargo
(attacks, closures, strikes, storms, accidents, congestion, sanctions). Industry news, finance,
newbuild orders and opinion pieces are false. Use only the given text.
Respond with ONLY a JSON object {{"items": [one object per input item, in the same order]}}, no markdown."""
ITEM_SCHEMA = {"type": "object", "required": ["id", "is_disruption", "event_type", "location", "severity",
                                              "expected_duration_days", "confidence"],
               "properties": {"id": {"type": "string"}, "is_disruption": {"type": "boolean"},
                              "event_type": {"enum": [e.value for e in EventType]},
                              "location": {"type": ["string", "null"]},
                              "severity": {"enum": ["low", "medium", "high", "critical", None]},
                              "expected_duration_days": {"type": ["number", "null"]},
                              "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}
RESPONSE_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}}}

log = logging.getLogger("majeure.xtract")
_CHOKEPOINTS_BY_ID = {c.location_id: c for c in CHOKEPOINTS}


def candidates(conn: sqlite3.Connection, since: str, limit: int, redo: bool) -> list[sqlite3.Row]:
    return conn.execute(f"""
        SELECT s.signal_id, s.title, s.raw_text, s.source FROM signals s
        WHERE s.source NOT IN {STRUCTURED_SOURCES} AND s.status <> 'dismissed' AND s.published_at >= ?
          AND (? = 1 OR s.signal_id NOT IN (SELECT signal_id FROM xtract_results))
        ORDER BY s.published_at DESC LIMIT ?""", (since, int(redo), limit)).fetchall()


def location_details(conn: sqlite3.Connection, location_id: str) -> Optional[dict]:
    if location_id in _CHOKEPOINTS_BY_ID:
        c = _CHOKEPOINTS_BY_ID[location_id]
        return {"name": c.name, "kind": "chokepoint", "country": None, "lat": c.lat, "lon": c.lon}
    row = conn.execute("SELECT port_name, country, lat, lon FROM ports WHERE port_id = ?", (location_id,)).fetchone()
    return {"name": row[0], "kind": "port", "country": row[1], "lat": row[2], "lon": row[3]} if row else None


def apply_xtract(conn: sqlite3.Connection) -> int:
    """Overwrite signals with their stored Xtract readings. Returns the number of signals updated."""
    rows = conn.execute("SELECT signal_id, is_disruption, event_type, severity, location_port_id FROM xtract_results").fetchall()
    with conn:
        for signal_id, is_disruption, event_type, severity, location_id in rows:
            conn.execute("UPDATE signals SET event_type = ?, severity = COALESCE(?, severity) WHERE signal_id = ?",
                         (event_type if is_disruption else "other", severity, signal_id))
            loc = location_details(conn, location_id) if location_id else None
            if loc:
                conn.execute("""UPDATE signals SET location_name = ?, location_kind = ?, location_port_id = ?,
                                location_country = ?, location_lat = ?, location_lon = ?, match_method = 'xtract'
                                WHERE signal_id = ?""",
                             (loc["name"], loc["kind"], location_id, loc["country"], loc["lat"], loc["lon"], signal_id))
                conn.execute("DELETE FROM signal_ports WHERE signal_id = ? AND is_primary = 1", (signal_id,))
                conn.execute("INSERT OR REPLACE INTO signal_ports (signal_id, port_id, is_primary) VALUES (?, ?, 1)",
                             (signal_id, location_id))
    if rows:
        log.info("applied %d Xtract refinements", len(rows))
    return len(rows)


def run_batch(client, conn: sqlite3.Connection, gazetteer: PortGazetteer, batch: list[sqlite3.Row]) -> int:
    from nemotron import extract_json_list  # local import keeps init_db.py free of the requests stack
    items = [{"id": r["signal_id"], "title": r["title"], "text": (r["raw_text"] or "")[:600]} for r in batch]
    content, _ = client.chat(SYSTEM_PROMPT, json.dumps(items, ensure_ascii=False), schema=RESPONSE_SCHEMA)
    by_id = {str(o.get("id")): o for o in extract_json_list(content) if isinstance(o, dict)}
    saved = 0
    for row in batch:
        o = by_id.get(row["signal_id"])
        if not o:
            continue
        event_type = o.get("event_type") if o.get("event_type") in EVENT_TYPES else "other"
        severity = o.get("severity") if o.get("severity") in SEVERITIES else None
        place = (o.get("location") or "").strip() or None
        loc = gazetteer.match(place) if place else None
        def num(v):
            return float(v) if isinstance(v, (int, float)) else None
        conn.execute("""INSERT OR REPLACE INTO xtract_results (signal_id, model, is_disruption, event_type, severity,
                        location_text, location_port_id, expected_duration_days, confidence, raw_output, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (row["signal_id"], client.model, int(bool(o.get("is_disruption"))), event_type, severity, place,
                      loc.port_id if loc else None, num(o.get("expected_duration_days")), num(o.get("confidence")),
                      json.dumps(o, ensure_ascii=False), iso(utcnow())))
        saved += 1
    conn.commit()
    return saved


def main(argv: Optional[list[str]] = None) -> int:
    from nemotron import add_client_args, client_from_args
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=60, help="max news signals to read, newest first (default: 60)")
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--redo", action="store_true", help="re-read signals that already have a result")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    add_client_args(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not store.database_ready(args.db):
        log.error("%s not found: run init_db.py first", args.db)
        return 1
    conn = store.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = candidates(conn, iso(utcnow() - timedelta(days=args.since_days)), args.limit, args.redo)
    log.info("%d news signals to read", len(rows))
    if not rows:
        return 0
    try:
        client = client_from_args(args)
    except (requests.RequestException, RuntimeError) as exc:
        log.error("cannot reach Nemotron at %s: %s", args.base_url, exc)
        return 1

    gazetteer, saved, failed = PortGazetteer.from_csv(), 0, 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i: i + BATCH_SIZE]
        try:
            saved += run_batch(client, conn, gazetteer, batch)
        except (requests.RequestException, ValueError) as exc:
            failed += len(batch)
            log.warning("batch %d failed: %s", i // BATCH_SIZE + 1, str(exc).splitlines()[0])
    apply_xtract(conn)
    stats = conn.execute("""SELECT SUM(is_disruption), SUM(location_port_id IS NOT NULL), COUNT(*) FROM xtract_results""").fetchone()
    log.info("read %d signals (%d failed); all results: %d disruptions, %d with a resolved location, %d total",
             saved, failed, stats[0] or 0, stats[1] or 0, stats[2])
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
