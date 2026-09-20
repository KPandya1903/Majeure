"""Cluster signals into real-world events and score how well each is corroborated.

News reports attach to a structured PortWatch/GDACS event at the same port or chokepoint when their
event types are compatible (e.g. Hormuz tanker-attack stories join HORMUZ-26). Remaining news is
grouped by location, type family and a 3-day time window. Rebuilds `events` / `event_signals`.
run_nemotron.py then makes one decision per (event, shipment) instead of one per headline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import store
from common import TIER_CONFIDENCE, best_tier, iso, setup_logging, utcnow
from schemas import Corroboration, Headline

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "portguard.db"
STRUCTURED = {"portwatch", "gdacs"}
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
GAP = timedelta(days=3)
# Event types that can describe the same situation share a family; a closure can result from anything.
FAMILIES = {
    "security_threat": {"conflict"}, "chokepoint_disruption": {"conflict"}, "regulatory_sanctions": {"conflict"},
    "labor_strike": {"labor"}, "severe_weather": {"hazard"}, "natural_disaster": {"hazard"},
    "vessel_incident": {"ops"}, "port_congestion": {"ops"}, "port_closure": {"conflict", "labor", "hazard", "ops"},
}

log = logging.getLogger("portguard.events")


def publisher(row: sqlite3.Row) -> str:
    meta = json.loads(row["metadata"] or "{}")
    return meta.get("publisher") or meta.get("domain") or row["source"]


def load_candidates(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Signals that could match a shipment: located, not 'other', active or recent (same filter as MATCH_SQL)."""
    rows = conn.execute("""
        SELECT s.*, (SELECT GROUP_CONCAT(port_id) FROM signal_ports sp WHERE sp.signal_id = s.signal_id) AS ports
        FROM signals s
        WHERE s.status <> 'dismissed' AND s.event_type <> 'other'
          AND (json_extract(s.metadata, '$.is_active') = 1 OR (s.source <> 'portwatch' AND s.published_at >= ?))
        ORDER BY s.published_at""", (since,)).fetchall()
    return [dict(r, ports=set((r["ports"] or "").split(",")) - {""}, publisher=publisher(r)) for r in rows if r["ports"]]


def cluster(signals: list[dict]) -> list[dict]:
    """Return events: {"anchor": signal, "members": [signals]}."""
    anchors = [s for s in signals if s["source"] in STRUCTURED]
    events = [{"anchor": a, "members": [a], "ports": set(a["ports"])} for a in anchors]
    loose: dict[tuple, list[list[dict]]] = {}
    for s in signals:
        if s["source"] in STRUCTURED:
            continue
        fam = FAMILIES.get(s["event_type"], set())
        hits = [e for e in events if e["anchor"]["source"] in STRUCTURED and e["ports"] & s["ports"]
                and FAMILIES.get(e["anchor"]["event_type"], set()) & fam]
        if hits:  # prefer an active anchor, then the most recent one
            best = max(hits, key=lambda e: (json.loads(e["anchor"]["metadata"]).get("is_active", False),
                                            e["anchor"]["published_at"] or ""))
            best["members"].append(s)
            continue
        key = (s["location_port_id"] or sorted(s["ports"])[0], min(fam) if fam else "other")
        chains = loose.setdefault(key, [])
        last = chains[-1][-1] if chains else None
        if last and s["published_at"] and last["published_at"] and \
                datetime.fromisoformat(s["published_at"].replace("Z", "+00:00")) - \
                datetime.fromisoformat(last["published_at"].replace("Z", "+00:00")) <= GAP:
            chains[-1].append(s)
        else:
            chains.append([s])
    for chains in loose.values():
        for members in chains:
            rep = max(members, key=lambda m: (m["published_at"] or "", len(m["raw_text"] or "")))
            events.append({"anchor": rep, "members": members, "ports": set().union(*(m["ports"] for m in members))})
    return events


def event_row(e: dict, now: str) -> dict:
    a, members = e["anchor"], e["members"]
    structured = a["source"] in STRUCTURED
    n_pub = len({m["publisher"] for m in members})
    if structured:
        confidence = min(0.99, 0.95 + 0.01 * (n_pub - 1))
        event_id = a["signal_id"]
        event_type = a["event_type"]
    else:
        # Confidence starts from WHO is reporting, not just how many are. A lone JOC or Loadstar
        # story is worth more than three aggregators recycling the same wire copy, and before
        # tiering every unconfirmed report scored an identical 0.35 regardless of publisher.
        tier = best_tier(m["source"] for m in members)
        confidence = min(0.9, TIER_CONFIDENCE[tier] + 0.15 * (n_pub - 1))
        first_day = min(m["published_at"] or now for m in members)[:10]
        key = f"{a['location_port_id'] or sorted(a['ports'])[0]}|{a['event_type']}|{first_day}"
        event_id = hashlib.sha1(key.encode()).hexdigest()[:16]
        event_type = Counter(m["event_type"] for m in members).most_common(1)[0][0]
    severities = [m["severity"] for m in members if m["severity"]]
    dates = [m["published_at"] for m in members if m["published_at"]]
    return {
        "event_id": event_id, "title": a["title"], "event_type": event_type,
        "severity": max(severities, key=SEVERITY_ORDER.get) if severities else None,
        "location_id": a["location_port_id"] or sorted(a["ports"])[0], "location_name": a["location_name"],
        "location_kind": a["location_kind"], "anchor_signal_id": a["signal_id"], "is_structured": int(structured),
        "n_signals": len(members), "n_sources": len({m["source"] for m in members}), "n_publishers": n_pub,
        "confidence": round(confidence, 2), "first_seen": min(dates) if dates else None,
        "last_seen": max(dates) if dates else None, "updated_at": now,
    }


def rebuild(conn: sqlite3.Connection, since_days: int = 14) -> list[dict]:
    now = utcnow()
    events = cluster(load_candidates(conn, iso(now - timedelta(days=since_days))))
    rows = [event_row(e, iso(now)) for e in events]
    with conn:
        conn.execute("DELETE FROM event_signals")
        conn.execute("DELETE FROM events")
        for e, r in zip(events, rows):
            conn.execute(f"INSERT OR REPLACE INTO events ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
            conn.executemany("INSERT OR IGNORE INTO event_signals (event_id, signal_id) VALUES (?, ?)",
                             [(r["event_id"], m["signal_id"]) for m in e["members"]])
    return rows


def corroboration_for(conn: sqlite3.Connection, event_id: str, max_headlines: int = 5) -> Optional[Corroboration]:
    """Corroboration block for a Nemotron payload: counts plus a few headlines from distinct publishers."""
    ev = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if not ev:
        return None
    rows = conn.execute("""SELECT s.title, s.source, s.source_url, s.metadata, s.signal_id FROM signals s
                           JOIN event_signals es USING (signal_id) WHERE es.event_id = ? AND s.signal_id <> ?
                           ORDER BY s.published_at DESC""", (event_id, ev["anchor_signal_id"])).fetchall()
    headlines, seen = [], set()
    for title, source, url, meta, _ in rows:
        pub = json.loads(meta or "{}").get("publisher") or json.loads(meta or "{}").get("domain") or source
        if pub in seen:
            continue
        seen.add(pub)
        headlines.append(Headline(title=title, source=pub, url=url))
        if len(headlines) >= max_headlines:
            break
    return Corroboration(event_id=event_id, n_reports=ev["n_signals"], n_sources=ev["n_sources"],
                         n_publishers=ev["n_publishers"], confidence=ev["confidence"], first_seen=ev["first_seen"],
                         last_seen=ev["last_seen"], headlines=headlines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since-days", type=int, default=14, help="news look-back window (default: 14, as run_nemotron.py)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if not args.db.exists():
        log.error("%s not found: run init_db.py first", args.db)
        return 1
    conn = store.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = rebuild(conn, args.since_days)
    n_signals = sum(r["n_signals"] for r in rows)
    log.info("%d signals -> %d events (%d anchored on PortWatch/GDACS)", n_signals, len(rows),
             sum(r["is_structured"] for r in rows))
    for r in sorted(rows, key=lambda r: -r["n_signals"])[:6]:
        log.info("  %3d reports / %2d publishers  conf %.2f  %-28s %s", r["n_signals"], r["n_publishers"],
                 r["confidence"], (r["location_name"] or r["location_id"])[:28], r["title"][:60])
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
