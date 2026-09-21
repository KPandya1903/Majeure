"""Load Bill of Lading shipments into the shipments table (upsert by shipment_id).

Input: a JSON array of Shipment objects, the same shape as NemotronPayload.shipment
(docs/SCHEMAS.md, section 3). Default: samples/shipments.sample.json (demo data).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

import store
from common import CHOKEPOINTS, ROOT, iso, setup_logging, utcnow
from init_db import DB_PATH, connect
from schemas import Shipment

SAMPLE_FILE = ROOT / "samples" / "shipments.sample.json"
KNOWN_CHOKEPOINTS = {c.location_id for c in CHOKEPOINTS}

COLUMNS = [
    "shipment_id", "bl_number", "carrier", "shipper", "consignee", "vessel_name", "voyage_number",
    "pol_port_id", "pod_port_id", "transshipment_port_ids", "route_chokepoints", "etd", "eta",
    "cargo_description", "container_count", "cargo_value_usd", "incoterm", "force_majeure_clause",
    "demurrage_usd_per_day", "detention_usd_per_day", "free_time_days", "late_delivery_penalty_usd_per_day",
    "deviation_permitted", "notice_period_hours", "governing_law", "created_at", "updated_at",
    "imo", "vessel_source", "carrier_source", "terms_source",
]
UPSERT = (
    f"INSERT INTO shipments ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)}) "
    f"ON CONFLICT (shipment_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c not in ("shipment_id", "created_at"))
)

log = logging.getLogger("majeure.shipments")


def shipment_row(s: Shipment, now: datetime) -> dict:
    bl, terms = s.bill_of_lading, s.contract_terms
    return {
        "shipment_id": s.shipment_id, "bl_number": bl.bl_number, "carrier": bl.carrier, "shipper": bl.shipper,
        "consignee": bl.consignee, "vessel_name": bl.vessel_name, "voyage_number": bl.voyage_number,
        "pol_port_id": bl.port_of_loading.port_id, "pod_port_id": bl.port_of_discharge.port_id,
        "transshipment_port_ids": json.dumps([p.port_id for p in bl.transshipment_ports]),
        "route_chokepoints": json.dumps(bl.route_chokepoints),
        "etd": iso(bl.etd), "eta": iso(bl.eta), "cargo_description": bl.cargo_description,
        "container_count": bl.container_count, "cargo_value_usd": bl.cargo_value_usd,
        "incoterm": terms.incoterm, "force_majeure_clause": terms.force_majeure_clause,
        "demurrage_usd_per_day": terms.demurrage_usd_per_day, "detention_usd_per_day": terms.detention_usd_per_day,
        "free_time_days": terms.free_time_days,
        "late_delivery_penalty_usd_per_day": terms.late_delivery_penalty_usd_per_day,
        "deviation_permitted": None if terms.deviation_permitted is None else int(terms.deviation_permitted),
        "notice_period_hours": terms.notice_period_hours, "governing_law": terms.governing_law,
        "created_at": iso(now), "updated_at": iso(now),
        # Default to the honest answer for a book loaded without provenance: it is demo data.
        "imo": bl.imo, "vessel_source": bl.vessel_source or "demo",
        "carrier_source": bl.carrier_source or "demo", "terms_source": terms.terms_source or "synthetic",
    }


def check_references(conn: sqlite3.Connection, s: Shipment) -> list[str]:
    """Return problems that would break the FK constraints; warn about unknown chokepoints."""
    bl = s.bill_of_lading
    port_ids = [bl.port_of_loading.port_id, bl.port_of_discharge.port_id, *(p.port_id for p in bl.transshipment_ports)]
    known = {r[0] for r in conn.execute(
        f"SELECT port_id FROM ports WHERE port_id IN ({', '.join('?' * len(port_ids))})", port_ids)}
    unknown_choke = [c for c in bl.route_chokepoints if c not in KNOWN_CHOKEPOINTS]
    if unknown_choke:
        log.warning("%s: unknown chokepoint ids %s (they will never match a signal)", s.shipment_id, unknown_choke)
    return [f"unknown port_id {p!r}" for p in port_ids if p not in known]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", type=Path, default=SAMPLE_FILE, help="JSON array of shipments (default: demo sample)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not store.database_ready(args.db):
        log.error("%s not found: run init_db.py first", args.db)
        return 1
    conn = connect(args.db)
    if not conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]:
        log.error("ports table is empty: run fetch_portwatch.py and init_db.py first")
        return 1

    now, loaded, failed = utcnow(), 0, 0
    for i, obj in enumerate(json.loads(args.file.read_text(encoding="utf-8"))):
        try:
            shipment = Shipment.model_validate(obj)
        except ValidationError as exc:
            failed += 1
            log.error("%s[%d] is not a valid shipment: %s", args.file.name, i, exc.errors()[0])
            continue
        if problems := check_references(conn, shipment):
            failed += 1
            log.error("%s skipped: %s (look ids up with: SELECT port_id, port_name, country FROM ports "
                      "WHERE port_name LIKE '%%name%%')", shipment.shipment_id, "; ".join(problems))
            continue
        try:
            with conn:
                conn.execute(UPSERT, shipment_row(shipment, now))
            loaded += 1
        except sqlite3.IntegrityError as exc:  # e.g. bl_number already used by another shipment_id
            failed += 1
            log.error("%s skipped: %s", shipment.shipment_id, exc)

    total = conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    conn.close()
    log.info("loaded %d shipments from %s (%d failed); shipments table now has %d rows", loaded, args.file, failed, total)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
