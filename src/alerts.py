"""Alert on high-risk decisions that are waiting for human approval.

Posts to a Slack incoming webhook when SLACK_WEBHOOK_URL is set (in .env), otherwise prints to the
console. Each decision is alerted once per channel (table alerts_sent).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from common import USER_AGENT, iso, setup_logging, utcnow
from init_db import DB_PATH, connect
from nemotron import env, load_dotenv

ACTIONS = {"monitor": "Monitor", "notify_customer": "Notify customer", "reroute": "Reroute", "expedite": "Expedite",
           "hold_cargo": "Hold cargo", "invoke_force_majeure": "Invoke force majeure"}
RISK_ORDER = ["low", "medium", "high", "critical"]
log = logging.getLogger("majeure.alerts")


def message(d: sqlite3.Row) -> str:
    payload = json.loads(d["input_payload"])
    bl = payload["shipment"]["bill_of_lading"]
    exp = (payload.get("analysis") or {}).get("exposure") or {}
    corr = (payload.get("analysis") or {}).get("corroboration") or {}
    wait = next((o["cost_usd"] for o in exp.get("options", []) if o["action"] == "monitor"), None)
    lines = [
        f"*Majeure: {d['risk_level'].upper()} risk* for {d['shipment_id']} "
        f"({bl['port_of_loading']['name']} → {bl['port_of_discharge']['name']}, ETA {bl['eta'][:10]})",
        f"Event: {payload['signal']['title']}" + (f" ({corr['n_reports']} reports, {corr['n_publishers']} publishers)" if corr else ""),
        f"Recommendation: *{ACTIONS.get(d['recommended_action'], d['recommended_action'])}*"
        + (f" (~${d['estimated_cost_usd']:,.0f}" if d["estimated_cost_usd"] is not None else "")
        + (f"; waiting ~${wait:,.0f})" if wait is not None else ")" if d["estimated_cost_usd"] is not None else ""),
    ]
    if exp.get("force_majeure_notice_due"):
        due = datetime.fromisoformat(exp["force_majeure_notice_due"].replace("Z", "+00:00"))
        hours = (due - datetime.now(timezone.utc)).total_seconds() / 3600
        lines.append(f"Force majeure notice due in {hours:.0f} h" if hours > 0 else f"Force majeure notice deadline passed {-hours:.0f} h ago")
    lines.append(f"Source: {payload['signal']['source_url']}")
    lines.append("Review and approve in the Majeure dashboard.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--min-risk", choices=RISK_ORDER, default="high", help="lowest risk level to alert on (default: high)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    webhook = env("SLACK_WEBHOOK_URL")
    channel = "slack" if webhook else "console"
    conn = connect(args.db)
    conn.row_factory = sqlite3.Row
    levels = RISK_ORDER[RISK_ORDER.index(args.min_risk):]
    rows = conn.execute(f"""SELECT * FROM decision_logs d WHERE d.recommended_action IS NOT NULL
                            AND d.human_decision = 'pending' AND d.risk_level IN ({', '.join('?' * len(levels))})
                            AND NOT EXISTS (SELECT 1 FROM alerts_sent a WHERE a.decision_id = d.decision_id AND a.channel = ?)
                            ORDER BY d.risk_score DESC""", (*levels, channel)).fetchall()
    log.info("%d new alerts (%s)", len(rows), channel)
    failed = 0
    for d in rows:
        text = message(d)
        if webhook:
            try:
                requests.post(webhook, json={"text": text}, headers={"User-Agent": USER_AGENT}, timeout=20).raise_for_status()
            except requests.RequestException as exc:
                log.error("Slack post failed for decision %s: %s", d["decision_id"], exc)
                failed += 1
                continue
        else:
            print(text.replace("*", "") + "\n")
        with conn:
            conn.execute("INSERT OR IGNORE INTO alerts_sent (decision_id, channel, sent_at) VALUES (?, ?, ?)",
                         (d["decision_id"], channel, iso(utcnow())))
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
