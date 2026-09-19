"""Match events to shipments, ask Nemotron for a decision on each pair, and log it in decision_logs.

One decision per (event, shipment): the many reports of one event (see cluster_events.py) become a
single payload whose `analysis` carries the priced options (exposure.py), measured traffic
(fetch_traffic.py) and corroboration. Nemotron is called through an OpenAI-compatible
/chat/completions endpoint configured in .env; `--engine rules` makes the same decision without a
model (cheapest available option), and `--dry-run` only writes the payloads to data/payloads/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from pydantic import ValidationError

from cluster_events import corroboration_for
from common import DATA_DIR, iso, nearby_ports, setup_logging, utcnow
from exposure import compute_exposure
from init_db import DB_PATH, connect
from nemotron import add_client_args, client_from_args, extract_json
from schemas import (Analysis, BillOfLading, ContractTerms, MatchContext, NemotronDecision, NemotronPayload,
                     PortLocation, PortRef, Shipment, Task, Traffic, XtractSignal)

PAYLOAD_DIR = DATA_DIR / "payloads"
REASON_PRIORITY = ["port_of_discharge", "port_of_loading", "transshipment_port", "route_chokepoint"]
RULES_MODEL = "portguard-rules-v1"

# One row per (signal, shipment, matched port/chokepoint). Same query as docs/SCHEMAS.md section 1.
MATCH_SQL = """
SELECT s.signal_id, sh.shipment_id, sp.port_id AS matched_port_id,
       CASE WHEN sp.port_id = sh.pod_port_id THEN 'port_of_discharge'
            WHEN sp.port_id = sh.pol_port_id THEN 'port_of_loading'
            WHEN sp.port_id IN (SELECT value FROM json_each(sh.transshipment_port_ids)) THEN 'transshipment_port'
            ELSE 'route_chokepoint' END AS match_reason
FROM signals s
JOIN signal_ports sp ON sp.signal_id = s.signal_id
JOIN shipments sh    ON sp.port_id IN (sh.pol_port_id, sh.pod_port_id)
                     OR sp.port_id IN (SELECT value FROM json_each(sh.transshipment_port_ids))
                     OR sp.port_id IN (SELECT value FROM json_each(sh.route_chokepoints))
WHERE s.status <> 'dismissed'
  AND sh.status IN ('booked', 'in_transit')
  AND (:include_other = 1 OR s.event_type <> 'other')
  AND (json_extract(s.metadata, '$.is_active') = 1                -- PortWatch: ongoing or recent
       OR (s.source <> 'portwatch' AND s.published_at >= :since))  -- news: inside the look-back window
  AND (:redo = 1 OR NOT EXISTS (SELECT 1 FROM decision_logs d      -- skip pairs already decided
                                WHERE d.signal_id = s.signal_id AND d.shipment_id = sh.shipment_id
                                  AND d.recommended_action IS NOT NULL))
ORDER BY s.source = 'portwatch' DESC, s.published_at DESC, sh.shipment_id  -- authoritative events first
"""

SYSTEM_PROMPT = """You are PortGuard AI, a maritime supply-chain risk analyst.
You receive one JSON payload with: `signal` (a disruption report and its source_url), `shipment` (Bill of Lading and
contract terms), `match_context` (why they were paired), `analysis` and `task`.
`analysis` may contain:
- exposure: rule-based cost estimates (USD) for each option (monitor = wait it out, reroute, invoke_force_majeure),
  whether the force majeure clause covers this event, the notice deadline, and the assumptions behind the numbers;
- traffic: measured ship transits / port calls at the matched location (7-day average vs the previous 90 days);
- corroboration: how many independent publishers report the same event, with sample headlines and URLs.

Assess how likely and how severely this event will delay or add cost to THIS shipment. Consider whether it is ongoing,
its severity and corroboration, measured traffic, where it sits on the route and days_until_eta, and the contract:
force majeure scope and notice deadline, deviation_permitted, penalties, and the incoterm (who bears the risk).
Use the exposure figures as a starting point; you may disagree with them if the payload gives a reason.

Rules:
- risk_score measures the exposure BEFORE your recommended mitigation: how likely and how costly this event is for the
  shipment if nothing is done (0 = no impact, 1 = severe). risk_level must match it:
  low < 0.35 <= medium < 0.65 <= high < 0.85 <= critical.
- Use only facts in the payload. Do not invent events, dates, or figures.
- recommended_action must be one of task.allowed_actions.
- citations must include signal.source_url; add corroborating URLs that support your reasoning.

Respond with ONLY a JSON object, no markdown, exactly in this shape:
{"risk_score": <number between 0 and 1>, "risk_level": "low" | "medium" | "high" | "critical",
 "recommended_action": "<one of task.allowed_actions>", "rationale": "<2-4 sentences, mention the cost trade-off>",
 "citations": ["<url>", ...], "estimated_cost_usd": <expected cost of your recommendation, number>,
 "force_majeure_applicable": true | false | null}"""

TASK_INSTRUCTIONS = ("Assess delay and cost exposure for this shipment given the event, the analysis and the contract "
                     "terms. Cite signal.source_url.")

log = logging.getLogger("portguard.nemotron")


# --------------------------------------------------------------------------- matching


def find_pairs(conn: sqlite3.Connection, since: datetime, include_other: bool, redo: bool) -> list[tuple[str, str, str, str]]:
    """(signal_id, shipment_id, match_reason, matched_port_id), one per signal/shipment pair, strongest reason."""
    best: dict[tuple[str, str], tuple[str, str]] = {}
    for signal_id, shipment_id, port_id, reason in conn.execute(
            MATCH_SQL, {"since": iso(since), "include_other": int(include_other), "redo": int(redo)}):
        current = best.get((signal_id, shipment_id))
        if current is None or REASON_PRIORITY.index(reason) < REASON_PRIORITY.index(current[0]):
            best[(signal_id, shipment_id)] = (reason, port_id)
    return [(sig, ship, reason, port) for (sig, ship), (reason, port) in best.items()]


def group_by_event(conn: sqlite3.Connection, pairs: list[tuple], redo: bool) -> list[dict]:
    """Collapse signal-level pairs to one item per (event, shipment), highest priority first.

    The event's anchor signal represents it when it matched; otherwise its newest matched report.
    Signals that belong to no event (e.g. clustering not run) stay as their own item.
    """
    event_of = dict(conn.execute("SELECT signal_id, event_id FROM event_signals").fetchall())
    events = {r["event_id"]: r for r in conn.execute("SELECT * FROM events")}
    decided = set() if redo else {(r[0], r[1]) for r in conn.execute(  # plain tuples: sqlite3.Row != tuple
        "SELECT event_id, shipment_id FROM decision_logs WHERE event_id IS NOT NULL AND recommended_action IS NOT NULL")}
    groups: dict[tuple, dict] = {}
    for sig, ship, reason, port in pairs:
        event_id = event_of.get(sig)
        if event_id and (event_id, ship) in decided:
            continue
        key = (event_id or f"signal:{sig}", ship)
        g = groups.setdefault(key, {"event_id": event_id, "shipment_id": ship, "pairs": []})
        g["pairs"].append((sig, reason, port))
    items = []
    for g in groups.values():
        ev = events.get(g["event_id"])
        anchor = ev["anchor_signal_id"] if ev else None
        rep = next((p for p in g["pairs"] if p[0] == anchor), g["pairs"][0])  # pairs arrive newest-first
        reason, port = min(((r, p) for _, r, p in g["pairs"]), key=lambda rp: REASON_PRIORITY.index(rp[0]))
        items.append({"event_id": g["event_id"], "shipment_id": g["shipment_id"], "signal_id": rep[0],
                      "match_reason": reason, "matched_port_id": port, "n_matched_signals": len(g["pairs"]),
                      "priority": (ev["is_structured"] if ev else 0, ev["confidence"] if ev else 0,
                                   ev["last_seen"] or "" if ev else "")})
    return sorted(items, key=lambda i: i["priority"], reverse=True)


# --------------------------------------------------------------------------- payloads


def load_signal(conn: sqlite3.Connection, signal_id: str) -> XtractSignal:
    row = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    location = None
    if row["location_name"] is not None:
        location = PortLocation(name=row["location_name"], kind=row["location_kind"], port_id=row["location_port_id"],
                                country=row["location_country"], lat=row["location_lat"], lon=row["location_lon"],
                                match_method=row["match_method"])
    related = [r[0] for r in conn.execute(
        "SELECT port_id FROM signal_ports WHERE signal_id = ? ORDER BY is_primary DESC, rowid", (signal_id,))]
    return XtractSignal(
        signal_id=row["signal_id"], source=row["source"], title=row["title"], source_url=row["source_url"],
        published_at=row["published_at"], port_location=location, related_port_ids=related,
        event_type=row["event_type"], severity=row["severity"], raw_text=row["raw_text"],
        ingested_at=row["ingested_at"], metadata=json.loads(row["metadata"]),
    )


def _port_ref(conn: sqlite3.Connection, port_id: str) -> PortRef:
    row = conn.execute("SELECT port_id, port_name, country, lat, lon FROM ports WHERE port_id = ?", (port_id,)).fetchone()
    return PortRef(port_id=row["port_id"], name=row["port_name"], country=row["country"], lat=row["lat"], lon=row["lon"])


def load_shipment(conn: sqlite3.Connection, shipment_id: str) -> Shipment:
    row = conn.execute("SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone()
    bill = BillOfLading(
        bl_number=row["bl_number"], carrier=row["carrier"], shipper=row["shipper"], consignee=row["consignee"],
        vessel_name=row["vessel_name"], voyage_number=row["voyage_number"],
        port_of_loading=_port_ref(conn, row["pol_port_id"]), port_of_discharge=_port_ref(conn, row["pod_port_id"]),
        transshipment_ports=[_port_ref(conn, p) for p in json.loads(row["transshipment_port_ids"])],
        route_chokepoints=json.loads(row["route_chokepoints"]), etd=row["etd"], eta=row["eta"],
        cargo_description=row["cargo_description"], container_count=row["container_count"],
        cargo_value_usd=row["cargo_value_usd"],
    )
    deviation = row["deviation_permitted"]
    terms = ContractTerms(
        incoterm=row["incoterm"], force_majeure_clause=row["force_majeure_clause"],
        demurrage_usd_per_day=row["demurrage_usd_per_day"], detention_usd_per_day=row["detention_usd_per_day"],
        free_time_days=row["free_time_days"], late_delivery_penalty_usd_per_day=row["late_delivery_penalty_usd_per_day"],
        deviation_permitted=None if deviation is None else bool(deviation),
        notice_period_hours=row["notice_period_hours"], governing_law=row["governing_law"],
    )
    return Shipment(shipment_id=row["shipment_id"], bill_of_lading=bill, contract_terms=terms)


def load_traffic(conn: sqlite3.Connection, location_id: str) -> Optional[Traffic]:
    row = conn.execute("SELECT * FROM traffic_stats WHERE location_id = ?", (location_id,)).fetchone()
    if not row:
        return None
    return Traffic(location_id=row["location_id"], name=row["name"], metric=row["metric"], latest_date=row["latest_date"],
                   avg_7d=row["avg_7d"], baseline_avg=row["baseline_avg"], pct_change=row["pct_change"])


_PORTS_CACHE: dict[int, pd.DataFrame] = {}


def alternate_port(conn: sqlite3.Connection, port_id: str, avoid: set[str], radius_km: float = 600) -> Optional[dict]:
    """Nearest reasonably busy port in the same country, not affected by the same signal."""
    ports = _PORTS_CACHE.setdefault(id(conn), pd.read_sql("SELECT port_id, port_name, country, lat, lon, vessel_count_total FROM ports", conn))
    origin = ports[ports["port_id"] == port_id]
    if origin.empty:
        return None
    o = origin.iloc[0]
    pool = ports[(ports["country"] == o.country) & ~ports["port_id"].isin(avoid | {port_id})
                 & (ports["vessel_count_total"] >= max(100, 0.05 * (o.vessel_count_total or 0)))]
    near = nearby_ports(pool, o.lat, o.lon, radius_km, limit=1)
    if near.empty:
        return None
    r = near.iloc[0]
    return {"port_id": r.port_id, "name": r.port_name, "distance_km": float(r.distance_km)}


def build_payload(conn: sqlite3.Connection, item: dict, now: datetime) -> NemotronPayload:
    shipment, signal = load_shipment(conn, item["shipment_id"]), load_signal(conn, item["signal_id"])
    reason, port_id = item["match_reason"], item["matched_port_id"]
    days_until_eta = round((shipment.bill_of_lading.eta - now).total_seconds() / 86400, 1)
    # Proximity-linked signals (GDACS) record how far each port is from the event; ID matches are 0 km.
    distances = {p["port_id"]: p["distance_km"] for p in signal.metadata.get("nearby_ports", [])}
    match = MatchContext(match_reason=reason, matched_port_id=port_id, distance_km=distances.get(port_id, 0.0),
                         days_until_eta=days_until_eta)
    traffic = load_traffic(conn, port_id)
    alt = alternate_port(conn, port_id, set(signal.related_port_ids)) if reason != "route_chokepoint" else None
    exposure = compute_exposure(signal, shipment, match, traffic, aware_at=signal.ingested_at, alternate_port=alt)
    corroboration = corroboration_for(conn, item["event_id"]) if item.get("event_id") else None
    return NemotronPayload(
        request_id=str(uuid.uuid4()), created_at=now, signal=signal, shipment=shipment, match_context=match,
        analysis=Analysis(exposure=exposure, traffic=traffic, corroboration=corroboration),
        task=Task(allowed_actions=feasible_actions(exposure), instructions=TASK_INSTRUCTIONS),
    )


def feasible_actions(exposure) -> list[str]:
    """All actions except options the analysis rules out: an impossible reroute, or force majeure the clause
    excludes. With structured output the model cannot pick them (small models otherwise sometimes do)."""
    unavailable = {o.action for o in exposure.options if not o.available} if exposure else set()
    return [a for a in Task().allowed_actions if a not in unavailable]


# --------------------------------------------------------------------------- decisions


def level_for(score: float) -> str:
    """The risk bands shared by the prompt, the parser and the rules engine."""
    return "critical" if score >= 0.85 else "high" if score >= 0.65 else "medium" if score >= 0.35 else "low"


def decision_schema(payload: NemotronPayload) -> dict:
    """JSON Schema for response_format: the NemotronDecision shape, with only this payload's allowed actions."""
    return {"type": "object", "additionalProperties": False,
            "required": ["risk_score", "risk_level", "recommended_action", "rationale", "citations",
                         "estimated_cost_usd", "force_majeure_applicable"],
            "properties": {
                "risk_score": {"type": "number", "minimum": 0, "maximum": 1},
                "risk_level": {"enum": ["low", "medium", "high", "critical"]},
                "recommended_action": {"enum": list(payload.task.allowed_actions)},
                "rationale": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
                "estimated_cost_usd": {"type": "number", "minimum": 0},
                "force_majeure_applicable": {"type": "boolean"}}}


def parse_decision(content: str, payload: NemotronPayload) -> NemotronDecision:
    decision = NemotronDecision.model_validate(extract_json(content))
    if decision.risk_level != level_for(decision.risk_score):  # small models mix up the two; the score is finer
        log.debug("risk_level %s does not match score %.2f; using %s", decision.risk_level, decision.risk_score,
                  level_for(decision.risk_score))
        decision.risk_level = level_for(decision.risk_score)
    if decision.recommended_action not in payload.task.allowed_actions:
        raise ValueError(f"recommended_action {decision.recommended_action!r} is not in task.allowed_actions")
    if payload.signal.source_url not in decision.citations:  # the evidence link is mandatory
        decision.citations.insert(0, payload.signal.source_url)
    return decision


def rules_decision(payload: NemotronPayload) -> NemotronDecision:
    """Model-free baseline: cheapest available option, risk from severity, traffic, corroboration and timing."""
    exp, signal, analysis = payload.analysis.exposure, payload.signal, payload.analysis
    base = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 0.9}.get(signal.severity or "medium", 0.5)
    if analysis.traffic and analysis.traffic.pct_change is not None and analysis.traffic.pct_change <= -50:
        base += 0.1
    if analysis.corroboration:
        base = base * (0.7 + 0.3 * analysis.corroboration.confidence)
    eta = payload.match_context.days_until_eta
    if eta is not None and eta > 45:
        base *= 0.8  # far from arrival: more time for the situation to change
    score = round(min(0.99, max(0.01, base)), 2)
    level = level_for(score)
    options = {o.action: o for o in exp.options}
    action = exp.cheapest_action or "monitor"
    if action == "monitor" and level in ("high", "critical"):
        action = "notify_customer"  # nothing cheaper than waiting, but the customer must know
    chosen = options.get(action) or options["monitor"]
    citations = [signal.source_url] + [h.url for h in (analysis.corroboration.headlines[:2] if analysis.corroboration else [])]
    rationale = (f"{signal.title} ({signal.event_type}, {level} risk). Cheapest available option: "
                 f"{exp.cheapest_action} at about ${chosen.cost_usd:,.0f}. {chosen.note}")
    return NemotronDecision(risk_score=score, risk_level=level, recommended_action=action, rationale=rationale,
                            citations=citations, estimated_cost_usd=chosen.cost_usd,
                            force_majeure_applicable=exp.force_majeure_covered)


def log_decision(conn: sqlite3.Connection, payload: NemotronPayload, model: str, decision: Optional[NemotronDecision],
                 response: dict, latency_ms: int, event_id: Optional[str]) -> None:
    fm = decision.force_majeure_applicable if decision else None
    with conn:
        conn.execute(
            """INSERT INTO decision_logs (request_id, signal_id, shipment_id, model, risk_score, risk_level,
                   recommended_action, rationale, citations, input_payload, output_payload, latency_ms, created_at,
                   event_id, estimated_cost_usd, force_majeure_applicable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload.request_id, payload.signal.signal_id, payload.shipment.shipment_id, model,
             decision and decision.risk_score, decision and decision.risk_level,
             decision and decision.recommended_action, decision and decision.rationale,
             json.dumps(decision.citations if decision else []), payload.model_dump_json(),
             json.dumps(response, ensure_ascii=False), latency_ms, iso(utcnow()), event_id,
             decision and decision.estimated_cost_usd, None if fm is None else int(fm)),
        )


# --------------------------------------------------------------------------- main


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=["nemotron", "rules"], default="nemotron",
                        help="nemotron (default) or rules: cheapest-option baseline, no model needed")
    parser.add_argument("--dry-run", action="store_true", help="write payloads to data/payloads/ without deciding")
    parser.add_argument("--limit", type=int, help="process at most N (event, shipment) pairs")
    parser.add_argument("--max-per-shipment", type=int, default=5,
                        help="at most N events per shipment per run, highest priority first; the rest are picked "
                             "up by later runs (default: 5; 0 = no cap)")
    parser.add_argument("--by-signal", action="store_true", help="one decision per matching signal instead of per event")
    parser.add_argument("--since-days", type=int, default=14, help="news look-back window in days (default: 14)")
    parser.add_argument("--include-other", action="store_true", help="also send signals with event_type 'other'")
    parser.add_argument("--redo", action="store_true", help="re-run pairs that already have a decision")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    add_client_args(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not args.db.exists():
        log.error("%s not found: run the ingestion scripts and init_db.py first (see README)", args.db)
        return 1
    conn = connect(args.db)
    conn.row_factory = sqlite3.Row
    if not conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]:
        log.error("shipments table is empty: run load_shipments.py first")
        return 1
    if not args.by_signal and not conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
        log.warning("events table is empty (run cluster_events.py); deciding per signal")

    now = utcnow()
    pairs = find_pairs(conn, now - timedelta(days=args.since_days), args.include_other, args.redo)
    items = ([{"event_id": None, "shipment_id": ship, "signal_id": sig, "match_reason": r, "matched_port_id": p,
               "n_matched_signals": 1} for sig, ship, r, p in pairs] if args.by_signal
             else group_by_event(conn, pairs, args.redo))
    if args.max_per_shipment:
        per_shipment = Counter()
        items = [i for i in items if (per_shipment.update([i["shipment_id"]]) or per_shipment[i["shipment_id"]]) <= args.max_per_shipment]
    if args.limit is not None:
        items = items[: args.limit]
    log.info("%d matching signals -> %d (event, shipment) pairs to assess", len(pairs), len(items))
    if not items:
        return 0

    client = None
    if args.engine == "nemotron" and not args.dry_run:
        try:
            client = client_from_args(args)
        except (requests.RequestException, RuntimeError) as exc:
            log.error("cannot reach Nemotron at %s: %s (is the server running? check NEMOTRON_BASE_URL in .env, "
                      "or use --engine rules)", args.base_url, exc)
            return 1
        log.info("Nemotron: %s @ %s", client.model, args.base_url)

    stats, results = Counter(), []
    for item in items:
        payload = build_payload(conn, item, now)
        corr = payload.analysis.corroboration
        label = (f"{item['shipment_id']} x {payload.signal.title[:55]!r} ({item['match_reason']}: {item['matched_port_id']}"
                 + (f"; {corr.n_reports} reports" if corr else "") + ")")
        if args.dry_run:
            PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{item['shipment_id']}__{item['event_id'] or item['signal_id']}.json"
            (PAYLOAD_DIR / name).write_text(payload.model_dump_json(indent=2), encoding="utf-8")
            log.info("payload: %s", label)
            stats["written"] += 1
            continue

        started, response = time.monotonic(), {}
        if client is None:
            decision, model = rules_decision(payload), RULES_MODEL
        else:
            model = client.model
            try:
                content, response = client.chat(SYSTEM_PROMPT, payload.model_dump_json(), schema=decision_schema(payload))
            except requests.ConnectionError as exc:
                log.error("lost connection to Nemotron: %s", exc)
                return 1
            except requests.RequestException as exc:
                log.error("Nemotron request failed for %s: %s", label, exc)
                stats["failed"] += 1
                continue
            try:
                decision = parse_decision(content, payload)
            except (ValueError, ValidationError) as exc:
                log.warning("unusable reply for %s (logged; retried next run): %s", label, str(exc).splitlines()[0])
                decision = None
        latency_ms = int((time.monotonic() - started) * 1000)
        log_decision(conn, payload, model, decision, response, latency_ms, item.get("event_id"))
        if decision:
            stats["decided"] += 1
            results.append((item["shipment_id"], decision, payload.signal.title))
            log.info("%s -> %s (%s, %.2f, ~$%s)", label, decision.recommended_action, decision.risk_level,
                     decision.risk_score, f"{decision.estimated_cost_usd:,.0f}" if decision.estimated_cost_usd is not None else "?")
        else:
            stats["unparseable"] += 1

    conn.close()
    if args.dry_run:
        log.info("wrote %d payloads to %s", stats["written"], PAYLOAD_DIR)
        return 0
    log.info("done: %s", dict(stats))
    for shipment_id, d, title in sorted(results, key=lambda r: -r[1].risk_score):
        log.info("  %-13s %-20s %-8s %.2f  %s", shipment_id, d.recommended_action, d.risk_level, d.risk_score, title[:60])
    return 1 if stats["failed"] or stats["unparseable"] else 0


if __name__ == "__main__":
    sys.exit(main())
