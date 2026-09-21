"""Compare Nemotron settings on the current (event, shipment) payloads, read-only.

Each reply is scored on objective checks: it parses; risk_level matches risk_score; the score is
pre-mitigation (active, well-corroborated, high-severity events score >= 0.5); the action fits the
contract (force majeure only when covered, no reroute when impossible); the cost is one of the
computed option costs; the event's source is cited. Nothing is written to the database.

    python src/eval_model.py --config ctx4k:nemotron-3-nano:4b:default:off \\
                             --config tuned:majeure-nemotron:default:on --pairs 3
Config format: name:model:reasoning:structured[:temperature]   (reasoning = default|none|low|medium|high)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from common import setup_logging, utcnow
from init_db import DB_PATH
from nemotron import NemotronClient, env, extract_json, load_dotenv
from run_nemotron import SYSTEM_PROMPT, build_payload, decision_schema, find_pairs, group_by_event, level_for
from schemas import NemotronDecision, NemotronPayload

log = logging.getLogger("majeure.eval")
CHECKS = ["parsed", "level_matches", "pre_mitigation", "action_fits", "cost_grounded", "cites_source"]


def score_reply(content: str, p: NemotronPayload) -> dict:
    out = {c: False for c in CHECKS}
    try:
        raw = extract_json(content)
        d = NemotronDecision.model_validate(raw)
    except (ValueError, ValidationError):
        return out
    exp, sig, corr = p.analysis.exposure, p.signal, p.analysis.corroboration
    out["parsed"] = d.recommended_action in p.task.allowed_actions
    out["level_matches"] = d.risk_level == level_for(d.risk_score)
    serious = (sig.severity in ("high", "critical") or (sig.metadata.get("is_active") and corr and corr.confidence >= 0.9))
    out["pre_mitigation"] = d.risk_score >= 0.5 if serious else True
    options = {o.action: o for o in exp.options} if exp else {}
    fits = True
    if exp and exp.force_majeure_covered is False and d.recommended_action == "invoke_force_majeure":
        fits = False
    if "reroute" in options and not options["reroute"].available and d.recommended_action == "reroute":
        fits = False
    out["action_fits"] = fits
    costs = [o.cost_usd for o in options.values()] + [0.0]
    out["cost_grounded"] = d.estimated_cost_usd is not None and any(abs(d.estimated_cost_usd - c) <= max(1.0, 0.01 * c) for c in costs)
    out["cites_source"] = sig.source_url in (raw.get("citations") or [])
    out["_decision"] = f"{d.recommended_action} {d.risk_level}/{d.risk_score:.2f} ${d.estimated_cost_usd or 0:,.0f}"
    return out


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", action="append", required=True, help="name:model:reasoning:structured[:temperature]")
    parser.add_argument("--pairs", type=int, default=3, help="how many top (event, shipment) pairs to use (default: 3)")
    parser.add_argument("--repeats", type=int, default=1, help="runs per pair, to measure stability (default: 1)")
    parser.add_argument("--base-url", default=env("NEMOTRON_BASE_URL", "http://localhost:11434/v1"))
    parser.add_argument("--max-tokens", type=int, default=int(env("NEMOTRON_MAX_TOKENS", "4096")))
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # read-only: evaluation never writes
    conn.row_factory = sqlite3.Row
    now = utcnow()
    items = group_by_event(conn, find_pairs(conn, now - timedelta(days=14), False, True), True)[: args.pairs]
    payloads = [build_payload(conn, i, now) for i in items]
    log.info("%d payloads: %s", len(payloads), "; ".join(f"{p.shipment.shipment_id} x {p.signal.title[:30]}" for p in payloads))

    rows = []
    for spec in args.config:
        name, *parts = spec.split(":")  # parse from the right: model names can contain ':' (nemotron-3-nano:4b)
        temperature = float(parts.pop()) if parts[-1].replace(".", "", 1).isdigit() else 0.2
        structured, reasoning, model = parts.pop() == "on", parts.pop(), ":".join(parts)
        client = NemotronClient(args.base_url, model, env("NEMOTRON_API_KEY"), temperature, args.max_tokens, 600,
                                reasoning, structured)
        results, latencies, scores = [], [], {}
        for p in payloads:
            for _ in range(args.repeats):
                t0 = time.monotonic()
                try:
                    content, _ = client.chat(SYSTEM_PROMPT, p.model_dump_json(), schema=decision_schema(p))
                except Exception as exc:  # count a failed call as a failed reply
                    content = ""
                    log.warning("%s: call failed: %s", name, exc)
                latencies.append(time.monotonic() - t0)
                r = score_reply(content, p)
                results.append(r)
                scores.setdefault(p.shipment.shipment_id, []).append(r.get("_decision", "unparsed"))
        row = {"config": name, **{c: sum(r[c] for r in results) / len(results) for c in CHECKS},
               "median_s": statistics.median(latencies)}
        row["total"] = sum(row[c] for c in CHECKS) / len(CHECKS)
        rows.append(row)
        log.info("%-22s %s", name, " | ".join(f"{k}: {'; '.join(v)}" for k, v in scores.items()))

    print(f"\n{'config':24}" + "".join(f"{c:>15}" for c in CHECKS) + f"{'TOTAL':>8}{'median s':>10}")
    for r in rows:
        print(f"{r['config']:24}" + "".join(f"{r[c]:>15.0%}" for c in CHECKS) + f"{r['total']:>8.0%}{r['median_s']:>10.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
