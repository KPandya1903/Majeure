"""Draft the correspondence a decision calls for: a force majeure notice (with its deadline) and a
customer update email, citing the evidence. Uses Nemotron when reachable, otherwise templates.

Writes `action_drafts` (one row per decision and kind). Humans review, edit and approve drafts in
the dashboard; nothing is ever sent automatically.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import requests

from common import iso, setup_logging, utcnow
from init_db import DB_PATH, connect
from nemotron import add_client_args, client_from_args, extract_json
from schemas import NemotronPayload

ACTION_LABELS = {"monitor": "monitoring", "notify_customer": "customer notified", "reroute": "reroute proposed",
                 "expedite": "expedite proposed", "hold_cargo": "hold cargo proposed",
                 "invoke_force_majeure": "force majeure notice"}
SYSTEM_PROMPT = """You draft concise, professional shipping correspondence for a logistics team.
You receive JSON with `kind` (force_majeure_notice or customer_email), the decision, the shipment, the event and the
cost analysis. Rules: use only facts in the input; never invent dates, figures or parties; for a force_majeure_notice
quote the force majeure clause verbatim, name the event, state the expected delay and that notice is given within the
notice period; for a customer_email explain the situation, the recommended action and the cost options in plain
language. Always cite the source URLs. Respond with ONLY a JSON object: {"subject": "...", "body": "..."}"""

log = logging.getLogger("majeure.drafts")


def needed_kinds(decision: sqlite3.Row) -> list[str]:
    kinds = []
    if decision["recommended_action"] == "invoke_force_majeure" or decision["force_majeure_applicable"] == 1:
        kinds.append("force_majeure_notice")
    if decision["recommended_action"] != "monitor" or decision["risk_level"] in ("medium", "high", "critical"):
        kinds.append("customer_email")
    return kinds


def _evidence(p: NemotronPayload) -> str:
    lines = [f"- {p.signal.title}: {p.signal.source_url}"]
    if p.analysis.corroboration:
        c = p.analysis.corroboration
        lines += [f"- {h.title} ({h.source}): {h.url}" for h in c.headlines[:3]]
        lines.append(f"({c.n_reports} reports from {c.n_publishers} publishers in total)")
    return "\n".join(lines)


def _traffic_line(p: NemotronPayload) -> str:
    t = p.analysis.traffic
    if not t or t.pct_change is None:
        return ""
    what = "transits" if t.metric == "transits" else "port calls"
    return (f"Measured {what} at {t.name} averaged {t.avg_7d:g}/day in the week to {t.latest_date}, "
            f"{t.pct_change:+.0f}% against the previous 90 days (IMF PortWatch).\n\n")


def template_draft(kind: str, d: sqlite3.Row, p: NemotronPayload) -> tuple[str, str]:
    bl, terms, sig, exp = p.shipment.bill_of_lading, p.shipment.contract_terms, p.signal, p.analysis.exposure
    route = f"{bl.port_of_loading.name} to {bl.port_of_discharge.name}"
    vessel = f"{bl.vessel_name or 'the vessel'}{' voyage ' + bl.voyage_number if bl.voyage_number else ''}"
    delay = next((o.expected_delay_days for o in exp.options if o.action == "monitor"), None) if exp else None
    summary = sig.raw_text.split(". ")[0].rstrip(".") + "."
    if kind == "force_majeure_notice":
        due = f" This notice is given within the {terms.notice_period_hours}-hour notice period." if terms.notice_period_hours else ""
        subject = f"Notice of force majeure: B/L {bl.bl_number}, {vessel}"
        body = (f"Dear {bl.consignee or 'Sir or Madam'},\n\n"
                f"We hereby give notice of force majeure in respect of the shipment under Bill of Lading {bl.bl_number} "
                f"({bl.cargo_description or 'cargo'}, {bl.container_count or 'n/a'} containers) carried on {vessel} "
                f"from {route}, currently due on {bl.eta:%d %B %Y}.\n\n"
                f"Event: {sig.title}. {summary}\n\n{_traffic_line(p)}"
                f"Our contract provides: \"{terms.force_majeure_clause}\"\n\n"
                f"This event is beyond our reasonable control"
                + (f" and is expected to delay delivery by about {delay:g} days" if delay else "")
                + f". We will take all reasonable steps to mitigate its effects and will keep you informed.{due}\n\n"
                f"Evidence:\n{_evidence(p)}\n\nKind regards,\n{bl.shipper or '[Sender]'}")
        return subject, body
    options = "\n".join(f"- {o.action.replace('_', ' ')}: ~${o.cost_usd:,.0f}{'' if o.available else ' (not available)'}. {o.note}"
                        for o in exp.options) if exp else "- (no cost analysis)"
    subject = f"Shipment update: B/L {bl.bl_number} ({route}): {ACTION_LABELS.get(d['recommended_action'], d['recommended_action'])}"
    body = (f"Dear {bl.consignee or 'customer'},\n\n"
            f"We are monitoring an event that may affect your shipment under B/L {bl.bl_number} on {vessel} "
            f"({route}, due {bl.eta:%d %B %Y}).\n\n"
            f"What happened: {sig.title}. {summary}\n\n{_traffic_line(p)}"
            f"Our assessment: {d['risk_level']} risk. Recommended action: {d['recommended_action'].replace('_', ' ')}. "
            f"{d['rationale']}\n\nOptions we compared (estimated cost):\n{options}\n\n"
            f"Sources:\n{_evidence(p)}\n\nWe will update you as the situation develops.\n\nKind regards,\n"
            f"{bl.shipper or '[Sender]'}")
    return subject, body


def model_draft(client, kind: str, d: sqlite3.Row, p: NemotronPayload) -> tuple[str, str]:
    exp = p.analysis.exposure
    request = {
        "kind": kind,
        "decision": {k: d[k] for k in ("recommended_action", "risk_level", "risk_score", "rationale", "estimated_cost_usd")},
        "shipment": p.shipment.model_dump(mode="json"),
        "event": {"title": p.signal.title, "summary": p.signal.raw_text, "source_url": p.signal.source_url},
        "analysis": {
            "options": [o.model_dump(mode="json") for o in exp.options] if exp else [],
            "force_majeure_notice_due": iso(exp.force_majeure_notice_due) if exp else None,
            "traffic": p.analysis.traffic.model_dump(mode="json") if p.analysis.traffic else None,
            "corroborating_headlines": [h.model_dump() for h in p.analysis.corroboration.headlines]
                                       if p.analysis.corroboration else [],
        },
    }
    content, _ = client.chat(SYSTEM_PROMPT, json.dumps(request, ensure_ascii=False), schema={
        "type": "object", "required": ["subject", "body"], "additionalProperties": False,
        "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}})
    obj = extract_json(content)
    if not obj.get("subject") or not obj.get("body"):
        raise ValueError("draft is missing subject or body")
    return str(obj["subject"]), str(obj["body"])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=["auto", "nemotron", "template"], default="auto",
                        help="auto (default): Nemotron if reachable, else templates")
    parser.add_argument("--redo", action="store_true", help="regenerate drafts that already exist (keeps approved ones)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    add_client_args(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    conn = connect(args.db)
    conn.row_factory = sqlite3.Row
    # Only the latest usable decision per (event or signal, shipment): superseded ones are never shown or sent.
    decisions = conn.execute("""SELECT d.* FROM decision_logs d
                                WHERE d.recommended_action IS NOT NULL AND d.human_decision <> 'overridden'
                                  AND d.decision_id = (SELECT MAX(x.decision_id) FROM decision_logs x
                                                       WHERE x.recommended_action IS NOT NULL
                                                         AND x.shipment_id = d.shipment_id
                                                         AND COALESCE(x.event_id, x.signal_id) = COALESCE(d.event_id, d.signal_id))
                                ORDER BY d.decision_id""").fetchall()
    existing = {(r["decision_id"], r["kind"]): r["status"] for r in conn.execute("SELECT decision_id, kind, status FROM action_drafts")}
    todo = [(d, k) for d in decisions for k in needed_kinds(d)
            if (d["decision_id"], k) not in existing or (args.redo and existing[(d["decision_id"], k)] == "draft")]
    log.info("%d drafts to write for %d decisions", len(todo), len({d["decision_id"] for d, _ in todo}))
    if not todo:
        return 0

    client = None
    if args.engine != "template":
        try:
            client = client_from_args(args)
        except (requests.RequestException, RuntimeError) as exc:
            if args.engine == "nemotron":
                log.error("cannot reach Nemotron at %s: %s", args.base_url, exc)
                return 1
            log.info("Nemotron not reachable (%s); using templates", type(exc).__name__)

    written = failed = 0
    for d, kind in todo:
        payload = NemotronPayload.model_validate_json(d["input_payload"])
        generated_by = "template"
        try:
            if client:
                subject, body = model_draft(client, kind, d, payload)
                generated_by = client.model
            else:
                subject, body = template_draft(kind, d, payload)
        except (requests.RequestException, ValueError) as exc:
            log.warning("model draft failed for decision %s (%s); using the template", d["decision_id"], str(exc).splitlines()[0])
            subject, body = template_draft(kind, d, payload)
            failed += 1
        exp = payload.analysis.exposure
        deadline = iso(exp.force_majeure_notice_due) if kind == "force_majeure_notice" and exp else None
        with conn:
            conn.execute("""INSERT OR REPLACE INTO action_drafts (decision_id, kind, subject, body, deadline_at, generated_by,
                            status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)""",
                         (d["decision_id"], kind, subject, body, deadline, generated_by, iso(utcnow())))
        written += 1
        log.info("decision %s %s: %s%s", d["decision_id"], kind, subject[:80], f" (due {deadline})" if deadline else "")
    log.info("wrote %d drafts (%d fell back to templates)", written, failed)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
