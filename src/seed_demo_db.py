"""Build a tiny, deterministic, demo-only SQLite database for the DigitalOcean App Platform API.

Does NOT fetch live sources, call Nemotron, or touch the production Postgres/Render database.
Idempotent: re-running replaces data/demo_portguard.db with the same fixed contents.

Usage:
    python src/seed_demo_db.py
    python src/seed_demo_db.py --db data/demo_portguard.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from common import CHOKEPOINTS, DATA_DIR, ROOT, iso, setup_logging, utcnow
from init_db import load_ddl
from load_shipments import SAMPLE_FILE, shipment_row
from schemas import Shipment

log = logging.getLogger("portguard.seed_demo")

DEMO_DB = DATA_DIR / "demo_portguard.db"
SEEDED_AT = "2026-09-20T12:00:00Z"

# Ports referenced by samples/shipments.sample.json (PortWatch-style ids + coords from the sample).
DEMO_PORTS = [
    ("port1188", "Shanghai", "Shanghai, China", "China", "CHN", "CN SGH", 31.19, 121.64),
    ("port1114", "Rotterdam", "Rotterdam, The Netherlands", "The Netherlands", "NLD", "NL RTM", 51.92, 4.22),
    ("port1201", "Singapore", "Singapore", "Singapore", "SGP", "SG SIN", 1.27, 103.71),
    ("port744", "Jebel Ali", "Jebel Ali, United Arab Emirates", "United Arab Emirates", "ARE", "AE JEA", 25.0, 55.08),
    ("port776", "Mumbai-Jawaharlal Nehru (Nhava Sheva)", "Nhava Sheva, India", "India", "IND", "IN NSA", 18.92, 72.94),
    ("port1065", "Busan", "Busan, Korea", "Korea", "KOR", "KR PUS", 35.09, 128.94),
    ("port664", "Los Angeles-Long Beach", "Los Angeles-Long Beach, United States", "United States", "USA", "US LGB", 33.76, -118.24),
    ("port446", "Hamburg", "Hamburg, Germany", "Germany", "DEU", "DE HAM", 53.52, 9.96),
    ("port1160", "Santos", "Santos, Brazil", "Brazil", "BRA", "BR SSZ", -23.92, -46.34),
]

# Fixed 16-hex signal ids (demo-only; not derived from live URLs).
SIG_RED_SEA = "d0e1000000000001"
SIG_HORMUZ = "d0e1000000000002"
SIG_ROTTERDAM = "d0e1000000000003"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_ports(conn: sqlite3.Connection) -> None:
    now = SEEDED_AT
    for port_id, name, full, country, iso3, locode, lat, lon in DEMO_PORTS:
        conn.execute(
            """INSERT INTO ports (
                port_id, port_name, full_name, country, iso3, continent, locode, lat, lon,
                active_disruption_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, ?)""",
            (port_id, name, full, country, iso3, locode, lat, lon, now),
        )


def _insert_signals(conn: sqlite3.Connection) -> None:
    bab = next(c for c in CHOKEPOINTS if c.location_id == "chokepoint:bab-el-mandeb-strait")
    hormuz = next(c for c in CHOKEPOINTS if c.location_id == "chokepoint:strait-of-hormuz")
    rotterdam = next(p for p in DEMO_PORTS if p[0] == "port1114")

    signals = [
        {
            "signal_id": SIG_RED_SEA,
            "source": "portwatch",
            "title": "RED SEA TENSIONS (demo)",
            "source_url": "https://portwatch.imf.org/pages/demo-red-sea",
            "published_at": "2023-12-16T00:00:00Z",
            "event_type": "security_threat",
            "severity": "high",
            "location_name": bab.name,
            "location_kind": "chokepoint",
            "location_port_id": bab.location_id,
            "location_country": None,
            "location_lat": bab.lat,
            "location_lon": bab.lon,
            "match_method": "structured",
            "raw_text": (
                "Demo snapshot: trade disruptions in the Red Sea near Bab el-Mandeb Strait "
                "due to attacks on commercial ships. Affected chokepoints: Bab el-Mandeb, "
                "Suez Canal, Cape of Good Hope."
            ),
            "metadata": json.dumps({
                "event_id": 1000000, "is_active": True, "demo": True,
                "from_date": "2023-12-16T00:00:00Z", "to_date": None,
                "n_affected_chokepoints": 3,
            }),
            "status": "processed",
            "ingested_at": SEEDED_AT,
            "ports": [
                (bab.location_id, 1),
                ("chokepoint:suez-canal", 0),
                ("chokepoint:cape-of-good-hope", 0),
            ],
        },
        {
            "signal_id": SIG_HORMUZ,
            "source": "portwatch",
            "title": "STRAIT OF HORMUZ DISRUPTION (demo)",
            "source_url": "https://portwatch.imf.org/pages/demo-hormuz",
            "published_at": "2026-06-15T00:00:00Z",
            "event_type": "chokepoint_disruption",
            "severity": "critical",
            "location_name": hormuz.name,
            "location_kind": "chokepoint",
            "location_port_id": hormuz.location_id,
            "location_country": None,
            "location_lat": hormuz.lat,
            "location_lon": hormuz.lon,
            "match_method": "structured",
            "raw_text": (
                "Demo snapshot: elevated risk and reduced transit volumes through the "
                "Strait of Hormuz affecting Gulf export routes."
            ),
            "metadata": json.dumps({
                "event_id": 1000001, "is_active": True, "demo": True,
                "from_date": "2026-06-15T00:00:00Z", "to_date": None,
            }),
            "status": "processed",
            "ingested_at": SEEDED_AT,
            "ports": [(hormuz.location_id, 1)],
        },
        {
            "signal_id": SIG_ROTTERDAM,
            "source": "gcaptain",
            "title": "Rotterdam port access delayed by bridge works (demo)",
            "source_url": "https://example.com/demo/rotterdam-bridge",
            "published_at": "2026-09-10T08:00:00Z",
            "event_type": "port_congestion",
            "severity": "medium",
            "location_name": rotterdam[1],
            "location_kind": "port",
            "location_port_id": rotterdam[0],
            "location_country": rotterdam[3],
            "location_lat": rotterdam[6],
            "location_lon": rotterdam[7],
            "match_method": "gazetteer",
            "raw_text": (
                "Demo snapshot: temporary bridge works near Rotterdam reduce berth access "
                "and add waiting time for inbound container vessels."
            ),
            "metadata": json.dumps({"demo": True, "author": "demo", "feed_title": "Demo Feed"}),
            "status": "processed",
            "ingested_at": SEEDED_AT,
            "ports": [(rotterdam[0], 1)],
        },
    ]

    cols = [
        "signal_id", "source", "title", "source_url", "published_at", "event_type", "severity",
        "location_name", "location_kind", "location_port_id", "location_country",
        "location_lat", "location_lon", "match_method", "raw_text", "metadata", "status", "ingested_at",
    ]
    for s in signals:
        conn.execute(
            f"INSERT INTO signals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(s[c] for c in cols),
        )
        for port_id, is_primary in s["ports"]:
            conn.execute(
                "INSERT INTO signal_ports (signal_id, port_id, is_primary) VALUES (?, ?, ?)",
                (s["signal_id"], port_id, is_primary),
            )


def _insert_events(conn: sqlite3.Connection) -> None:
    events = [
        (SIG_RED_SEA, "RED SEA TENSIONS (demo)", "security_threat", "high",
         "chokepoint:bab-el-mandeb-strait", "Bab el-Mandeb Strait", "chokepoint",
         SIG_RED_SEA, 1, 3, 1, 2, 0.97, "2023-12-16T00:00:00Z", SEEDED_AT),
        (SIG_HORMUZ, "STRAIT OF HORMUZ DISRUPTION (demo)", "chokepoint_disruption", "critical",
         "chokepoint:strait-of-hormuz", "Strait of Hormuz", "chokepoint",
         SIG_HORMUZ, 1, 2, 1, 1, 0.95, "2026-06-15T00:00:00Z", SEEDED_AT),
        (SIG_ROTTERDAM, "Rotterdam port access delayed by bridge works (demo)", "port_congestion", "medium",
         "port1114", "Rotterdam", "port",
         SIG_ROTTERDAM, 0, 1, 1, 1, 0.50, "2026-09-10T08:00:00Z", SEEDED_AT),
    ]
    for e in events:
        (event_id, title, etype, sev, loc_id, loc_name, loc_kind, anchor,
         structured, n_sig, n_src, n_pub, conf, first, last) = e
        conn.execute(
            """INSERT INTO events (
                event_id, title, event_type, severity, location_id, location_name, location_kind,
                anchor_signal_id, is_structured, n_signals, n_sources, n_publishers, confidence,
                first_seen, last_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, title, etype, sev, loc_id, loc_name, loc_kind, anchor,
             structured, n_sig, n_src, n_pub, conf, first, last, SEEDED_AT),
        )
        conn.execute(
            "INSERT INTO event_signals (event_id, signal_id) VALUES (?, ?)",
            (event_id, anchor),
        )


def _insert_shipments(conn: sqlite3.Connection) -> int:
    now = utcnow()
    raw = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    n = 0
    for obj in raw:
        s = Shipment.model_validate(obj)
        row = shipment_row(s, now)
        # Freeze timestamps for reproducibility across builds.
        row["created_at"] = SEEDED_AT
        row["updated_at"] = SEEDED_AT
        cols = list(row.keys())
        conn.execute(
            f"INSERT INTO shipments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(row[c] for c in cols),
        )
        n += 1
    return n


def _decision_payload(signal_id: str, shipment_id: str, match_reason: str, matched_port_id: str,
                      options: list[dict], fm_covered: bool | None, notice_due: str | None) -> str:
    return json.dumps({
        "payload_version": "1.1",
        "request_id": f"demo-{shipment_id}-{signal_id}",
        "created_at": SEEDED_AT,
        "signal": {"signal_id": signal_id},
        "shipment": {"shipment_id": shipment_id},
        "match_context": {
            "match_reason": match_reason,
            "matched_port_id": matched_port_id,
            "distance_km": 0.0,
            "days_until_eta": 14.0,
        },
        "analysis": {
            "exposure": {
                "currency": "USD",
                "options": options,
                "cheapest_action": next((o["action"] for o in options if o.get("available")), None),
                "force_majeure_covered": fm_covered,
                "force_majeure_notice_due": notice_due,
                "assumptions": ["Demo seed: planning estimates only, not carrier quotes."],
            },
            "traffic": None,
            "corroboration": None,
        },
        "task": {
            "objective": "assess_disruption_risk",
            "allowed_actions": [o["action"] for o in options if o.get("available")],
            "instructions": "Demo seed decision.",
        },
    })


def _insert_decisions(conn: sqlite3.Connection) -> None:
    red_opts = [
        {"action": "monitor", "available": True, "expected_delay_days": 10, "cost_usd": 10000,
         "breakdown": {"late_delivery_penalty": 10000}, "note": "Wait it out: ~10 days of delay."},
        {"action": "reroute", "available": True, "expected_delay_days": 12, "cost_usd": 30000,
         "breakdown": {"late_delivery_penalty": 12000, "carrier_surcharge": 18000},
         "note": "Reroute via the Cape of Good Hope (+12 days)."},
        {"action": "invoke_force_majeure", "available": True, "expected_delay_days": 10, "cost_usd": 0,
         "breakdown": {}, "note": "Clause covers war/canal closure. Notice due within 48 h."},
        {"action": "notify_customer", "available": True, "expected_delay_days": 10, "cost_usd": 10000,
         "breakdown": {}, "note": "Inform the consignee."},
    ]
    hormuz_opts = [
        {"action": "monitor", "available": True, "expected_delay_days": 12, "cost_usd": 30000,
         "breakdown": {"late_delivery_penalty": 30000}, "note": "Wait: high daily penalty."},
        {"action": "reroute", "available": False, "expected_delay_days": 0, "cost_usd": 0,
         "breakdown": {}, "note": "Deviation not permitted by contract."},
        {"action": "invoke_force_majeure", "available": False, "expected_delay_days": 0, "cost_usd": 0,
         "breakdown": {}, "note": "Clause excludes war and security incidents."},
        {"action": "notify_customer", "available": True, "expected_delay_days": 12, "cost_usd": 30000,
         "breakdown": {}, "note": "Force majeure unavailable; notify the buyer."},
    ]
    rot_opts = [
        {"action": "monitor", "available": True, "expected_delay_days": 3, "cost_usd": 3000,
         "breakdown": {"late_delivery_penalty": 3000}, "note": "Short congestion delay."},
        {"action": "notify_customer", "available": True, "expected_delay_days": 3, "cost_usd": 3000,
         "breakdown": {}, "note": "Advise the consignee of possible ETA slip."},
        {"action": "reroute", "available": True, "expected_delay_days": 2, "cost_usd": 4500,
         "breakdown": {}, "note": "Divert to an alternate NW Europe berth if available."},
    ]

    rows = [
        # SHP-DEMO-001 × Red Sea → invoke force majeure (~$0 vs waiting)
        (f"demo-req-{SIG_RED_SEA}-001", SIG_RED_SEA, "SHP-DEMO-001", "portguard-rules-v1-demo",
         0.82, "high", "invoke_force_majeure",
         "Demo: Red Sea security threat touches the Bab el-Mandeb / Suez route. "
         "CIF clause covers war and canal closure; invoking force majeure is the cheapest option.",
         json.dumps(["https://portwatch.imf.org/pages/demo-red-sea"]),
         _decision_payload(SIG_RED_SEA, "SHP-DEMO-001", "route_chokepoint",
                           "chokepoint:bab-el-mandeb-strait", red_opts, True, "2026-09-22T12:00:00Z"),
         json.dumps({"risk_score": 0.82, "risk_level": "high",
                     "recommended_action": "invoke_force_majeure", "demo": True}),
         0.0, 1, SIG_RED_SEA),
        # SHP-DEMO-002 × Hormuz → notify (FM excluded, no reroute)
        (f"demo-req-{SIG_HORMUZ}-002", SIG_HORMUZ, "SHP-DEMO-002", "portguard-rules-v1-demo",
         0.91, "critical", "notify_customer",
         "Demo: Hormuz disruption matches this FOB shipment. Force majeure excludes war/security "
         "and deviation is forbidden, so exposure stays with the buyer — notify the customer.",
         json.dumps(["https://portwatch.imf.org/pages/demo-hormuz"]),
         _decision_payload(SIG_HORMUZ, "SHP-DEMO-002", "route_chokepoint",
                           "chokepoint:strait-of-hormuz", hormuz_opts, False, None),
         json.dumps({"risk_score": 0.91, "risk_level": "critical",
                     "recommended_action": "notify_customer", "demo": True}),
         30000.0, 0, SIG_HORMUZ),
        # SHP-DEMO-001 × Rotterdam congestion
        (f"demo-req-{SIG_ROTTERDAM}-001", SIG_ROTTERDAM, "SHP-DEMO-001", "portguard-rules-v1-demo",
         0.55, "medium", "notify_customer",
         "Demo: bridge works at the port of discharge add a short delay. Notify the consignee "
         "and keep monitoring berth availability.",
         json.dumps(["https://example.com/demo/rotterdam-bridge"]),
         _decision_payload(SIG_ROTTERDAM, "SHP-DEMO-001", "port_of_discharge",
                           "port1114", rot_opts, None, None),
         json.dumps({"risk_score": 0.55, "risk_level": "medium",
                     "recommended_action": "notify_customer", "demo": True}),
         3000.0, None, SIG_ROTTERDAM),
    ]

    for (req, sig, ship, model, score, level, action, rationale, cites, inp, out,
         cost, fm, event_id) in rows:
        conn.execute(
            """INSERT INTO decision_logs (
                request_id, signal_id, shipment_id, model, risk_score, risk_level,
                recommended_action, rationale, citations, input_payload, output_payload,
                human_decision, latency_ms, created_at, event_id, estimated_cost_usd,
                force_majeure_applicable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (req, sig, ship, model, score, level, action, rationale, cites, inp, out,
             SEEDED_AT, event_id, cost, fm),
        )


def _insert_traffic(conn: sqlite3.Connection) -> None:
    stats = [
        ("chokepoint:bab-el-mandeb-strait", "Bab el-Mandeb Strait", "transits",
         "2026-09-13", 27.3, 31.7, -13.8),
        ("chokepoint:suez-canal", "Suez Canal", "transits",
         "2026-09-13", 18.1, 42.0, -56.9),
        ("chokepoint:strait-of-hormuz", "Strait of Hormuz", "transits",
         "2026-09-13", 8.2, 55.0, -85.1),
        ("chokepoint:malacca-strait", "Malacca Strait", "transits",
         "2026-09-13", 110.0, 108.5, 1.4),
        ("port1114", "Rotterdam", "port_calls",
         "2026-09-13", 42.0, 48.0, -12.5),
    ]
    for loc, name, metric, latest, avg7, base, pct in stats:
        conn.execute(
            """INSERT INTO traffic_stats (
                location_id, name, metric, latest_date, avg_7d, baseline_avg, pct_change, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (loc, name, metric, latest, avg7, base, pct, SEEDED_AT),
        )
        # A few daily points so /api/traffic consumers have a non-empty related series if needed.
        for i, val in enumerate((avg7 * 0.95, avg7, avg7 * 1.02)):
            day = f"2026-09-{11 + i:02d}"
            conn.execute(
                "INSERT INTO traffic_daily (location_id, date, metric, value) VALUES (?, ?, ?, ?)",
                (loc, day, metric, val),
            )


def seed(db_path: Path = DEMO_DB) -> Path:
    """Create (or replace) the demo database. Returns the path written."""
    log.info("Seeding demo database at %s", db_path)
    conn = _connect(db_path)
    try:
        conn.executescript(load_ddl())
        _insert_ports(conn)
        _insert_signals(conn)
        _insert_events(conn)
        n_ships = _insert_shipments(conn)
        _insert_decisions(conn)
        _insert_traffic(conn)
        conn.execute(
            "INSERT INTO pipeline_state (source_group, last_fetched_at, ok) VALUES (?, ?, 1)",
            ("demo_seed", SEEDED_AT),
        )
        conn.commit()
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("ports", "signals", "events", "shipments", "decision_logs", "traffic_stats")
        }
        log.info("Seeded %s (shipments=%d): %s", db_path, n_ships, counts)
    finally:
        conn.close()
    return db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEMO_DB)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if not SAMPLE_FILE.exists():
        log.error("Missing %s", SAMPLE_FILE)
        return 1
    if not (ROOT / "docs" / "SCHEMAS.md").exists():
        log.error("Missing docs/SCHEMAS.md (DDL source)")
        return 1
    seed(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
