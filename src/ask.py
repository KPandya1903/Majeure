"""Ask Majeure: answer questions about shipments, events and decisions with Nemotron, using only
Majeure's own data (sent as a compact JSON context) and citing sources.

    python src/ask.py "Which shipments go through Hormuz, and can we invoke force majeure?"
"""
from __future__ import annotations

import store

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from nemotron import NemotronClient, client_from_env

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "majeure.db"
SYSTEM_PROMPT = """You are Majeure's analyst assistant for a logistics team.
Answer the user's question using ONLY the JSON context (their shipments with contract terms, current disruption events,
Majeure's decisions with cost estimates, and measured traffic). If the context does not contain the answer, say so.
Be concise: short paragraphs or bullet points, dollar figures where relevant, shipment ids in bold.
Cite sources as numbered markdown links using URLs from the context, numbering them from 1 in the order you
use them: write [1](https://example.com/article), then [2](...), and so on. Never write a literal "[n]" -- n is a
placeholder for the number, not text to copy. Never invent facts or URLs."""


def build_context(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    names = dict(conn.execute("SELECT port_id, port_name FROM ports").fetchall())
    shipments = [{
        "shipment_id": r["shipment_id"], "bl_number": r["bl_number"],
        # Vessel identity. Without these the model cannot answer "where is MAERSK TUKANG" or
        # "how many ships does COSCO have" at all -- it was being asked about a fleet whose
        # names were never in its context, and answered, correctly but uselessly, that it had
        # no information about them.
        "vessel_name": r["vessel_name"], "imo": r["imo"], "carrier": r["carrier"],
        "status": r["status"],
        "route": " -> ".join(names.get(p, p) for p in [r["pol_port_id"], *json.loads(r["transshipment_port_ids"]), r["pod_port_id"]]),
        "route_chokepoints": json.loads(r["route_chokepoints"]), "etd": r["etd"], "eta": r["eta"],
        "cargo": r["cargo_description"], "containers": r["container_count"], "cargo_value_usd": r["cargo_value_usd"],
        "incoterm": r["incoterm"], "force_majeure_clause": r["force_majeure_clause"],
        "deviation_permitted": r["deviation_permitted"], "notice_period_hours": r["notice_period_hours"],
        "late_delivery_penalty_usd_per_day": r["late_delivery_penalty_usd_per_day"],
    } for r in conn.execute("SELECT * FROM shipments")]
    decisions = []
    for r in conn.execute("""SELECT d.*, s.title, s.source_url FROM decision_logs d JOIN signals s USING (signal_id)
                             WHERE d.recommended_action IS NOT NULL ORDER BY d.created_at DESC LIMIT 40"""):
        exp = (json.loads(r["input_payload"]).get("analysis") or {}).get("exposure") or {}
        decisions.append({
            "shipment_id": r["shipment_id"], "event": r["title"], "risk_level": r["risk_level"],
            "risk_score": r["risk_score"], "recommended_action": r["recommended_action"],
            "estimated_cost_usd": r["estimated_cost_usd"], "force_majeure_applicable": r["force_majeure_applicable"],
            "force_majeure_notice_due": exp.get("force_majeure_notice_due"),
            "options": [{k: o[k] for k in ("action", "available", "cost_usd", "note")} for o in exp.get("options", [])],
            "rationale": r["rationale"], "human_decision": r["human_decision"],
            "sources": json.loads(r["citations"])[:6],
        })
    events = [{
        "title": r["title"], "event_type": r["event_type"], "severity": r["severity"], "location": r["location_name"],
        "reports": r["n_signals"], "publishers": r["n_publishers"], "confidence": r["confidence"],
        "last_seen": r["last_seen"], "source_url": r["source_url"],
    } for r in conn.execute("""SELECT e.*, s.source_url FROM events e JOIN signals s ON s.signal_id = e.anchor_signal_id
                               ORDER BY e.is_structured DESC, e.n_signals DESC LIMIT 20""")]
    traffic = [dict(r) for r in conn.execute(
        "SELECT name, metric, latest_date, avg_7d, baseline_avg, pct_change FROM traffic_stats WHERE ABS(pct_change) >= 10")]
    return {"shipments": shipments, "decisions": decisions, "events": events, "traffic_changes": traffic}


def answer(question: str, client: Optional[NemotronClient] = None, db: Path = DB_PATH) -> str:
    with store.session(db) as conn:
        context = build_context(conn)
    client = client or client_from_env()
    content, _ = client.chat(SYSTEM_PROMPT, json.dumps({"question": question, "context": context}, ensure_ascii=False),
                             max_tokens=1200)
    import re
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S | re.I).strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python src/ask.py "your question"')
    print(answer(" ".join(sys.argv[1:])))
