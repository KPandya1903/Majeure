"""Read a Bill of Lading / sales contract (PDF, TXT or MD) and extract a shipment with Nemotron.

Nemotron returns the parties, route, dates, cargo and contract terms (force majeure clause verbatim);
port names are resolved to PortWatch port ids and canal/strait names to chokepoint ids. The result
is validated as a `Shipment`, saved to data/shipments_extracted.json and, with --load, upserted
into the shipments table.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from pydantic import ValidationError

from common import CHOKEPOINTS, DATA_DIR, PORTS_CSV, PortGazetteer, iso, setup_logging, utcnow
from nemotron import add_client_args, client_from_args, extract_json
from schemas import Shipment

OUT_JSON = DATA_DIR / "shipments_extracted.json"
SYSTEM_PROMPT = """You extract structured data from shipping documents (Bills of Lading, sales contracts, booking notes).
Return ONLY a JSON object with this shape (use null when the document does not say):
{"bill_of_lading": {"bl_number": str, "carrier": str, "shipper": str, "consignee": str, "vessel_name": str,
   "voyage_number": str, "port_of_loading": {"name": str, "country": str}, "port_of_discharge": {"name": str, "country": str},
   "transshipment_ports": [{"name": str, "country": str}], "route_chokepoints": [names of canals/straits the route passes],
   "etd": "YYYY-MM-DD", "eta": "YYYY-MM-DD", "cargo_description": str, "container_count": int, "cargo_value_usd": number},
 "contract_terms": {"incoterm": one of EXW FCA CPT CIP DAP DPU DDP FAS FOB CFR CIF, "force_majeure_clause": the clause
   copied VERBATIM, "demurrage_usd_per_day": number, "detention_usd_per_day": number, "free_time_days": int,
   "late_delivery_penalty_usd_per_day": number, "deviation_permitted": bool, "notice_period_hours": int,
   "governing_law": str}}
Convert notice periods to hours (e.g. 3 days = 72). Copy numbers exactly; do not guess missing values."""

log = logging.getLogger("majeure.contract")


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8", errors="replace")


class PortResolver:
    def __init__(self):
        self.ports = pd.read_csv(PORTS_CSV, dtype={"port_id": str})
        self.gazetteer = PortGazetteer(self.ports)

    def port(self, ref: Optional[dict]) -> Optional[dict]:
        if not ref or not ref.get("name"):
            return None
        name, country = ref["name"].strip(), (ref.get("country") or "").strip().lower()
        cands = self.ports[self.ports["port_name"].str.lower() == name.lower()]
        if len(cands) > 1 and country:
            cands = cands[cands["country"].str.lower().str.contains(country, regex=False)] if not cands[cands["country"].str.lower().str.contains(country, regex=False)].empty else cands
        if cands.empty:
            loc = self.gazetteer.match(re.sub(r"^port of\s+", "", name, flags=re.I).title())
            if not loc or loc.kind != "port":
                return None
            cands = self.ports[self.ports["port_id"] == loc.port_id]
        r = cands.sort_values("vessel_count_total", ascending=False).iloc[0]
        return {"port_id": r.port_id, "name": r.port_name, "country": r.country, "lat": r.lat, "lon": r.lon}

    def chokepoints(self, names: list) -> list[str]:
        ids = []
        for n in names or []:
            c = next((c for c in CHOKEPOINTS if re.search(rf"\b(?:{c.pattern})\b", str(n), re.I)), None)
            if c and c.location_id not in ids:
                ids.append(c.location_id)
        return ids


def to_shipment(raw: dict, resolver: PortResolver, shipment_id: Optional[str]) -> Shipment:
    bl = dict(raw.get("bill_of_lading") or {})
    problems = []
    for key in ("port_of_loading", "port_of_discharge"):
        resolved = resolver.port(bl.get(key))
        if not resolved:
            problems.append(f"{key} {bl.get(key)!r} not found in PortWatch ports")
        bl[key] = resolved
    bl["transshipment_ports"] = [p for p in (resolver.port(t) for t in bl.get("transshipment_ports") or []) if p]
    bl["route_chokepoints"] = resolver.chokepoints(bl.get("route_chokepoints"))
    if problems:
        raise ValueError("; ".join(problems))
    bl = {k: v for k, v in bl.items() if v is not None}
    terms = {k: v for k, v in (raw.get("contract_terms") or {}).items() if v is not None}
    sid = shipment_id or f"SHP-{re.sub(r'[^A-Za-z0-9]+', '-', bl.get('bl_number') or 'UNKNOWN').strip('-')}"
    return Shipment.model_validate({"shipment_id": sid, "bill_of_lading": bl, "contract_terms": terms})


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", type=Path, required=True, help="PDF, TXT or MD contract / Bill of Lading")
    parser.add_argument("--shipment-id", help="id to give the shipment (default: SHP-<bl_number>)")
    parser.add_argument("--load", action="store_true", help="also upsert it into the shipments table")
    add_client_args(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    text = read_text(args.file)
    if len(text.strip()) < 50:
        log.error("could not read text from %s (scanned PDFs need OCR first)", args.file)
        return 1
    try:
        client = client_from_args(args)
        content, _ = client.chat(SYSTEM_PROMPT, text[:20000])
        shipment = to_shipment(extract_json(content), PortResolver(), args.shipment_id)
    except (requests.RequestException, RuntimeError) as exc:
        log.error("cannot reach Nemotron at %s: %s", args.base_url, exc)
        return 1
    except (ValueError, ValidationError) as exc:
        log.error("could not build a shipment from the model's reading: %s", str(exc).splitlines()[0])
        return 1

    existing = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else []
    existing = [s for s in existing if s.get("shipment_id") != shipment.shipment_id] + [shipment.model_dump(mode="json")]
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    bl, t = shipment.bill_of_lading, shipment.contract_terms
    log.info("%s: %s -> %s (%s), ETA %s, %s, FM notice %sh, deviation %s -> %s", shipment.shipment_id,
             bl.port_of_loading.name, bl.port_of_discharge.name, ", ".join(bl.route_chokepoints) or "no chokepoints",
             bl.eta.date(), t.incoterm, t.notice_period_hours, t.deviation_permitted, OUT_JSON.name)

    if args.load:
        from init_db import DB_PATH, connect
        from load_shipments import UPSERT, check_references, shipment_row
        conn = connect(DB_PATH)
        if problems := check_references(conn, shipment):
            log.error("not loaded: %s", "; ".join(problems))
            return 1
        with conn:
            conn.execute(UPSERT, shipment_row(shipment, utcnow()))
        log.info("loaded %s into the shipments table", shipment.shipment_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
