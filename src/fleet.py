"""Build a shipment book around REAL vessels operated by REAL carriers.

The demo book shipped with PortGuard was synthetic end to end: "Demo Ocean Line" sailing the "DEMO
VOYAGER". That made every downstream number unfalsifiable, because nobody could check the vessel
existed. This module replaces the vessel *identity* with public-registry fact while leaving the
commercial terms synthetic, and records which is which so the UI can say so.

What is real here:

* **Vessel name and IMO number** -- from Wikidata's container-ship set, every IMO re-validated
  against its own check digit by `valid_imo()` below. These ships exist and can be looked up in
  Equasis, GISIS or any AIS site.
* **Operating carrier** -- resolved from the vessel name prefix. Container-ship naming is
  effectively a carrier namespace ("MSC ...", "CMA CGM ...", "Ever ..."), so this is reliable for
  liner tonnage, and the registered owner of a chartered ship is often a leasing company rather than
  the line that operates it anyway. It is an attribution, not a registry lookup, and is labelled
  `name_prefix` in `carrier_source` so nothing downstream can mistake it for one.
* **Ports and routes** -- real UN/LOCODE ports from the IMF PortWatch gazetteer, with the real
  chokepoints those routes transit.

What is still synthetic, and flagged `terms_source="synthetic"` on every row: Bill of Lading
numbers, shipper and consignee names, cargo descriptions and values, and every contract term
(demurrage, detention, penalties, force majeure clause). Real contracts are commercially
confidential and are not published anywhere -- this is the one part a demo cannot source honestly,
so it is generated and declared rather than dressed up.

    python src/fleet.py --out samples/shipments.real.json     # regenerate the book
    python src/fleet.py --check                               # re-validate every IMO check digit
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple, Optional

from common import ROOT, iso, load_ports, setup_logging, utcnow

SAMPLE_OUT = ROOT / "samples" / "shipments.real.json"

log = logging.getLogger("portguard.fleet")


# --------------------------------------------------------------------------- vessel identity


def valid_imo(imo: str) -> bool:
    """IMO check digit: digits 1-6 weighted 7..2, summed, last digit must equal the total's units.

    Worth enforcing rather than trusting the source -- a transposed digit still looks like an IMO
    number, and an invalid one would send anyone verifying this fleet to a dead end."""
    digits = re.sub(r"\D", "", imo)
    if len(digits) != 7:
        return False
    return sum(int(digits[i]) * (7 - i) for i in range(6)) % 10 == int(digits[6])


# Vessel-name prefix -> the line that operates it, under the name that line trades as today.
# Ordered longest-first at use so "CMA CGM" wins before a hypothetical "CMA".
CARRIER_BY_PREFIX: dict[str, str] = {
    "cma cgm": "CMA CGM",
    "yang ming": "Yang Ming Marine Transport",
    "hyundai": "HMM",
    "maersk": "A.P. Moller-Maersk",
    "cosco": "COSCO Shipping Lines",
    "hapag": "Hapag-Lloyd",
    "msc": "Mediterranean Shipping Company",
    "oocl": "OOCL",
    "ever": "Evergreen Marine",
    "apl": "APL (CMA CGM)",
    "nyk": "NYK Line",
    "mol": "Ocean Network Express",
    "hmm": "HMM",
    "zim": "ZIM Integrated Shipping",
}


def carrier_for(vessel_name: str) -> Optional[str]:
    """Operating line inferred from the vessel name, or None when the name carries no line prefix."""
    lowered = vessel_name.lower()
    for prefix in sorted(CARRIER_BY_PREFIX, key=len, reverse=True):
        if lowered.startswith(prefix):
            return CARRIER_BY_PREFIX[prefix]
    return None


class Vessel(NamedTuple):
    name: str
    imo: str
    carrier: str


# Real container ships, IMO check digits verified. Sourced from Wikidata's container-ship set
# (wdt:P31/P279* wd:Q17210 with wdt:P458), then filtered to vessels whose name identifies an
# operating line. Regenerate with the SPARQL query in docs/DATA_SOURCES.md.
VESSELS: tuple[Vessel, ...] = tuple(Vessel(n, i, carrier_for(n) or "") for n, i in (
    ("Maersk Tukang", "9334686"), ("Maersk Salalah", "9352016"), ("Maersk Stockholm", "9352042"),
    ("MSC Valencia", "9301471"), ("MSC Tomoko", "9309461"), ("MSC Lorena", "9320403"),
    ("CMA CGM Nabucco", "9299630"), ("CMA CGM White Shark", "9317975"), ("CMA CGM Baudelaire", "9222285"),
    ("Cosco America", "9345427"), ("Cosco Africa", "9345439"), ("Cosco Ningbo", "9305582"),
    ("Ever Shine", "9300386"), ("Ever Sigma", "9300398"), ("Ever Smart", "9300403"),
    ("OOCL San Francisco", "9199268"), ("OOCL Chicago", "9199270"), ("OOCL Washington", "9417256"),
    ("Hyundai Bangkok", "9323510"), ("Hyundai Together", "9473731"), ("Hyundai Tenacity", "9475674"),
    ("NYK Meteor", "9337638"), ("NYK Diana", "9337688"), ("NYK Orpheus", "9313008"),
    ("MOL Charisma", "9321249"), ("MOL Courage", "9321263"), ("MOL Precision", "9236470"),
    ("ZIM Virginia", "9231808"), ("Zim Rio Grande", "9363376"), ("ZIM Atlantic", "9440801"),
    ("APL Minnesota", "9350018"), ("APL Raffles", "9631979"), ("APL Changi", "9631981"),
))


# --------------------------------------------------------------------------- routes


class Leg(NamedTuple):
    """A real trade lane: load port, discharge port, and the chokepoints the route transits."""
    pol: str            # port name as the PortWatch gazetteer spells it
    pod: str
    chokepoints: tuple[str, ...]
    transship: tuple[str, ...] = ()
    cargo: str = "Containerised general cargo"


# Chokepoint ids must exist in common.CHOKEPOINTS or they will never match a signal.
LANES: tuple[Leg, ...] = (
    Leg("Shanghai", "Rotterdam", ("chokepoint:malacca-strait", "chokepoint:bab-el-mandeb-strait",
                                  "chokepoint:suez-canal"), ("Singapore",), "Consumer electronics, 40' HC"),
    Leg("Ningbo", "Hamburg", ("chokepoint:malacca-strait", "chokepoint:bab-el-mandeb-strait",
                              "chokepoint:suez-canal"), (), "Household appliances"),
    Leg("Jebel Ali", "Mumbai-Jawaharlal Nehru (Nhava Sheva)", ("chokepoint:strait-of-hormuz",), (), "Polyethylene resin, 20' dry"),
    Leg("Busan", "Los Angeles-Long Beach", (), (), "Automotive parts, 40' dry"),
    Leg("Antwerp", "Santos", ("chokepoint:dover-strait",), (), "Agricultural machinery, flat racks"),
    Leg("Singapore", "Felixstowe", ("chokepoint:bab-el-mandeb-strait", "chokepoint:suez-canal"),
        (), "Textiles and footwear"),
    Leg("Yantian", "New York-New Jersey", ("chokepoint:panama-canal",), (), "Furniture, 40' HC"),
    Leg("Piraeus", "Valencia", (), (), "Marble and building stone"),
    Leg("Qingdao Port", "Los Angeles-Long Beach", (), (), "Solar modules, 40' HC"),
    Leg("Port Klang", "Barcelona", ("chokepoint:bab-el-mandeb-strait", "chokepoint:suez-canal"),
        (), "Rubber and latex products"),
)

# Synthetic commercial terms. Ranges chosen to be plausible for containerised liner cargo rather
# than to flatter the product: every one of these is invented and labelled as such on the row.
INCOTERMS = ("CIF", "FOB", "CFR", "DAP")
FM_CLAUSE = (
    "Neither party is liable for delay or failure to perform caused by war, blockade, hostilities, "
    "piracy, strikes, closure of ports or canals, or any other cause beyond its reasonable control, "
    "provided written notice is given within {hours} hours of the party becoming aware of the event."
)
GOVERNING_LAW = ("English law", "Singapore law", "New York law")

# Real SCAC codes, the four-letter carrier prefix that genuinely opens a Bill of Lading number.
# Using them keeps a synthetic BL number looking like a BL number instead of "A.P.-4686-001".
SCAC = {
    "A.P. Moller-Maersk": "MAEU", "Mediterranean Shipping Company": "MSCU", "CMA CGM": "CMDU",
    "COSCO Shipping Lines": "COSU", "Evergreen Marine": "EGLV", "OOCL": "OOLU", "HMM": "HDMU",
    "Ocean Network Express": "ONEY", "NYK Line": "NYKS", "ZIM Integrated Shipping": "ZIMU",
    "APL (CMA CGM)": "APLU", "Yang Ming Marine Transport": "YMLU", "Hapag-Lloyd": "HLCU",
}


# --------------------------------------------------------------------------- build


def _port_index(ports) -> dict[str, dict]:
    """Port name (lowercased) -> the gazetteer row, so lanes can be written in plain English."""
    if ports is None:
        return {}
    index = {}
    for row in ports.itertuples(index=False):
        index.setdefault(str(row.port_name).lower(), {
            "port_id": row.port_id, "name": row.port_name, "country": row.country,
            "lat": float(row.lat), "lon": float(row.lon),
        })
    return index


def build(seed: int = 20260920, count: int = 33) -> list[dict]:
    """One shipment per lane, each crewed by a real vessel. Deterministic for a given seed so the
    book does not reshuffle itself between pipeline runs and invalidate every stored decision."""
    rng = random.Random(seed)
    index = _port_index(load_ports())
    if not index:
        raise RuntimeError("ports gazetteer is empty: run fetch_portwatch.py first")

    now = utcnow()
    shipments, skipped = [], []
    # One shipment per VESSEL, cycling through the lanes -- rather than one per lane. Real carriers
    # compete on the same trade lanes, so several ships sharing one is accurate, and adding a
    # carrier no longer requires inventing a new route for it.
    for i, vessel in enumerate(VESSELS[:count]):
        lane = LANES[i % len(LANES)]
        pol, pod = index.get(lane.pol.lower()), index.get(lane.pod.lower())
        if not pol or not pod:
            skipped.append(f"{lane.pol}->{lane.pod}")
            continue
        # Spread departures around today so some shipments are at sea, some not yet sailed: the map
        # needs vessels at different points along their route for the fleet view to mean anything.
        etd = now + timedelta(days=rng.randint(-18, 6))
        eta = etd + timedelta(days=rng.randint(12, 38))
        notice_hours = rng.choice((24, 48, 72))
        shipments.append({
            "shipment_id": f"SHP-{vessel.imo}",
            "bill_of_lading": {
                "bl_number": f"{SCAC.get(vessel.carrier, 'GENL')}{vessel.imo[-4:]}{i + 1:03d}",
                "carrier": vessel.carrier,
                "shipper": f"{lane.cargo.split(',')[0]} exporter ({pol['name']})",
                "consignee": f"Importer of record ({pod['name']})",
                "vessel_name": vessel.name.upper(),
                "imo": vessel.imo,
                "voyage_number": f"{rng.randint(1, 340):03d}{rng.choice('WENS')}",
                "port_of_loading": pol,
                "port_of_discharge": pod,
                "transshipment_ports": [index[t.lower()] for t in lane.transship if t.lower() in index],
                "route_chokepoints": list(lane.chokepoints),
                "etd": iso(etd), "eta": iso(eta),
                "cargo_description": lane.cargo,
                "container_count": rng.randint(4, 40),
                "cargo_value_usd": float(rng.randrange(180_000, 1_900_000, 10_000)),
                "vessel_source": "public_registry",
                "carrier_source": "name_prefix",
            },
            "contract_terms": {
                "incoterm": rng.choice(INCOTERMS),
                "force_majeure_clause": FM_CLAUSE.format(hours=notice_hours),
                "demurrage_usd_per_day": float(rng.randrange(120, 340, 10)),
                "detention_usd_per_day": float(rng.randrange(80, 260, 10)),
                "free_time_days": rng.choice((3, 5, 7)),
                "late_delivery_penalty_usd_per_day": float(rng.randrange(500, 3_000, 100)),
                "deviation_permitted": rng.random() > 0.3,
                "notice_period_hours": notice_hours,
                "governing_law": rng.choice(GOVERNING_LAW),
                "terms_source": "synthetic",
            },
        })
    if skipped:
        log.warning("%d lanes skipped, port not in the gazetteer: %s", len(skipped), ", ".join(skipped))
    return shipments


def check() -> int:
    bad = [v for v in VESSELS if not valid_imo(v.imo)]
    unattributed = [v for v in VESSELS if not v.carrier]
    for v in bad:
        print(f"  INVALID IMO check digit: {v.name} ({v.imo})")
    for v in unattributed:
        print(f"  no carrier prefix matched: {v.name}")
    print(f"{len(VESSELS) - len(bad)}/{len(VESSELS)} IMO numbers valid; "
          f"{len(VESSELS) - len(unattributed)}/{len(VESSELS)} attributed to a carrier "
          f"({len({v.carrier for v in VESSELS if v.carrier})} distinct lines)")
    return 1 if bad or unattributed else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=SAMPLE_OUT)
    parser.add_argument("--count", type=int, default=len(VESSELS))
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--check", action="store_true", help="validate IMO check digits and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.check:
        return check()
    shipments = build(seed=args.seed, count=args.count)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(shipments, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = {s["bill_of_lading"]["carrier"] for s in shipments}
    log.info("wrote %d shipments on %d real vessels across %d carriers -> %s",
             len(shipments), len({s["bill_of_lading"]["imo"] for s in shipments}), len(lines), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
