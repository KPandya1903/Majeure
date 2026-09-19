"""Match signals to shipments, ask Nemotron for a decision on each pair, and log it in decision_logs.

Nemotron is called through an OpenAI-compatible /chat/completions endpoint (NVIDIA NIM, vLLM,
Ollama or the hosted NVIDIA API). Configure it with NEMOTRON_* variables in .env (see .env.example).
Use --dry-run to write the payloads to data/payloads/ without calling a model.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from pydantic import ValidationError

from common import DATA_DIR, ROOT, USER_AGENT, iso, setup_logging, utcnow
from init_db import DB_PATH, connect
from schemas import (BillOfLading, ContractTerms, MatchContext, NemotronDecision, NemotronPayload, PortLocation,
                     PortRef, Shipment, Task, XtractSignal)

PAYLOAD_DIR = DATA_DIR / "payloads"
REASON_PRIORITY = ["port_of_discharge", "port_of_loading", "transshipment_port", "route_chokepoint"]

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
contract terms), `match_context` (why the signal was paired with this shipment) and `task`.

Assess how likely and how severely this signal will delay or add cost to THIS shipment. Consider:
- whether the signal is ongoing or recent (signal.metadata.is_active, signal.published_at) and its severity;
- where the matched port or chokepoint sits on the route, and match_context.days_until_eta;
- the contract terms: force_majeure_clause (does it cover this kind of event?), deviation_permitted,
  notice_period_hours, demurrage/detention and late-delivery penalties, and the incoterm (who bears the risk).

Rules:
- Use only facts in the payload. Do not invent events, dates, or figures.
- recommended_action must be one of task.allowed_actions.
- citations must include signal.source_url.

Respond with ONLY a JSON object, no markdown, exactly in this shape:
{"risk_score": <number between 0 and 1>, "risk_level": "low" | "medium" | "high" | "critical",
 "recommended_action": "<one of task.allowed_actions>", "rationale": "<2-4 sentences>",
 "citations": ["<url>", ...]}"""

TASK_INSTRUCTIONS = ("Assess delay and cost exposure for this shipment given the signal and contract terms. "
                     "Cite signal.source_url.")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)

log = logging.getLogger("portguard.nemotron")


# --------------------------------------------------------------------------- config


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name) or default  # empty values count as unset


# --------------------------------------------------------------------------- matching + payloads


def find_pairs(conn: sqlite3.Connection, since: datetime, include_other: bool, redo: bool) -> list[tuple[str, str, str, str]]:
    """(signal_id, shipment_id, match_reason, matched_port_id), one per pair, strongest reason first."""
    best: dict[tuple[str, str], tuple[str, str]] = {}
    for signal_id, shipment_id, port_id, reason in conn.execute(
            MATCH_SQL, {"since": iso(since), "include_other": int(include_other), "redo": int(redo)}):
        current = best.get((signal_id, shipment_id))
        if current is None or REASON_PRIORITY.index(reason) < REASON_PRIORITY.index(current[0]):
            best[(signal_id, shipment_id)] = (reason, port_id)
    return [(sig, ship, reason, port) for (sig, ship), (reason, port) in best.items()]


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


def build_payload(conn: sqlite3.Connection, signal_id: str, shipment_id: str, reason: str, port_id: str,
                  now: datetime) -> NemotronPayload:
    shipment, signal = load_shipment(conn, shipment_id), load_signal(conn, signal_id)
    days_until_eta = round((shipment.bill_of_lading.eta - now).total_seconds() / 86400, 1)
    # Proximity-linked signals (GDACS) record how far each port is from the event; ID matches are 0 km.
    distances = {p["port_id"]: p["distance_km"] for p in signal.metadata.get("nearby_ports", [])}
    return NemotronPayload(
        request_id=str(uuid.uuid4()), created_at=now, signal=signal, shipment=shipment,
        match_context=MatchContext(match_reason=reason, matched_port_id=port_id,
                                   distance_km=distances.get(port_id, 0.0), days_until_eta=days_until_eta),
        task=Task(instructions=TASK_INSTRUCTIONS),
    )


# --------------------------------------------------------------------------- Nemotron


class NemotronClient:
    """Minimal OpenAI-compatible chat-completions client (NIM, vLLM, Ollama, NVIDIA API)."""

    def __init__(self, base_url: str, model: Optional[str], api_key: Optional[str],
                 temperature: float, max_tokens: int, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.model = model or self._detect_model()

    def _detect_model(self) -> str:
        resp = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=30)
        resp.raise_for_status()
        ids = [m["id"] for m in resp.json().get("data", [])]
        if not ids:
            raise RuntimeError(f"{self.base_url}/models lists no models; set NEMOTRON_MODEL")
        chosen = ([i for i in ids if "nemotron" in i.lower()] or ids)[0]
        log.info("NEMOTRON_MODEL not set; using %r (server lists: %s)", chosen, ", ".join(ids))
        return chosen

    def complete(self, payload: NemotronPayload) -> tuple[str, dict]:
        """Return (message content, full response JSON)."""
        resp = requests.post(f"{self.base_url}/chat/completions", headers=self.headers, timeout=self.timeout, json={
            "model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": payload.model_dump_json()}],
        })
        resp.raise_for_status()
        body = resp.json()
        return body["choices"][0]["message"].get("content") or "", body


def extract_json(text: str) -> dict:
    """First JSON object in a model reply, ignoring <think> blocks, prose and ``` fences."""
    text = _THINK_RE.sub("", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


def parse_decision(content: str, payload: NemotronPayload) -> NemotronDecision:
    decision = NemotronDecision.model_validate(extract_json(content))
    if decision.recommended_action not in payload.task.allowed_actions:
        raise ValueError(f"recommended_action {decision.recommended_action!r} is not in task.allowed_actions")
    if payload.signal.source_url not in decision.citations:  # the evidence link is mandatory
        decision.citations.insert(0, payload.signal.source_url)
    return decision


def log_decision(conn: sqlite3.Connection, payload: NemotronPayload, model: str, decision: Optional[NemotronDecision],
                 response: dict, latency_ms: int) -> None:
    with conn:
        conn.execute(
            """INSERT INTO decision_logs (request_id, signal_id, shipment_id, model, risk_score, risk_level,
                   recommended_action, rationale, citations, input_payload, output_payload, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload.request_id, payload.signal.signal_id, payload.shipment.shipment_id, model,
             decision and decision.risk_score, decision and decision.risk_level,
             decision and decision.recommended_action, decision and decision.rationale,
             json.dumps(decision.citations if decision else []), payload.model_dump_json(),
             json.dumps(response, ensure_ascii=False), latency_ms, iso(utcnow())),
        )


# --------------------------------------------------------------------------- main


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="write payloads to data/payloads/ without calling Nemotron")
    parser.add_argument("--limit", type=int, help="process at most N (signal, shipment) pairs")
    parser.add_argument("--max-per-shipment", type=int, default=5,
                        help="at most N signals per shipment per run, highest priority first; the rest are picked "
                             "up by later runs (default: 5; 0 = no cap)")
    parser.add_argument("--since-days", type=int, default=14, help="news look-back window in days (default: 14)")
    parser.add_argument("--include-other", action="store_true", help="also send signals with event_type 'other'")
    parser.add_argument("--redo", action="store_true", help="re-run pairs that already have a decision")
    parser.add_argument("--base-url", default=env("NEMOTRON_BASE_URL", "http://localhost:8000/v1"))
    parser.add_argument("--model", default=env("NEMOTRON_MODEL"), help="default: auto-detect from GET /models")
    parser.add_argument("--temperature", type=float, default=float(env("NEMOTRON_TEMPERATURE", "0.2")))
    parser.add_argument("--max-tokens", type=int, default=int(env("NEMOTRON_MAX_TOKENS", "2048")))
    parser.add_argument("--timeout", type=float, default=float(env("NEMOTRON_TIMEOUT", "300")))
    parser.add_argument("--db", type=Path, default=DB_PATH)
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

    now = utcnow()
    pairs = find_pairs(conn, now - timedelta(days=args.since_days), args.include_other, args.redo)
    if args.max_per_shipment:
        per_shipment = Counter()
        pairs = [p for p in pairs if (per_shipment.update([p[1]]) or per_shipment[p[1]]) <= args.max_per_shipment]
    if args.limit is not None:
        pairs = pairs[: args.limit]
    log.info("%d (signal, shipment) pairs to assess", len(pairs))
    if not pairs:
        return 0

    client = None
    if not args.dry_run:
        try:
            client = NemotronClient(args.base_url, args.model, env("NEMOTRON_API_KEY"),
                                    args.temperature, args.max_tokens, args.timeout)
        except (requests.RequestException, RuntimeError) as exc:
            log.error("cannot reach Nemotron at %s: %s (is the server running? check NEMOTRON_BASE_URL in .env)",
                      args.base_url, exc)
            return 1
        log.info("Nemotron: %s @ %s", client.model, args.base_url)

    stats, results = Counter(), []
    for signal_id, shipment_id, reason, port_id in pairs:
        payload = build_payload(conn, signal_id, shipment_id, reason, port_id, now)
        label = f"{shipment_id} x {payload.signal.title[:60]!r} ({reason}: {port_id})"
        if args.dry_run:
            PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
            (PAYLOAD_DIR / f"{shipment_id}__{signal_id}.json").write_text(payload.model_dump_json(indent=2), encoding="utf-8")
            log.info("payload: %s", label)
            stats["written"] += 1
            continue

        started = time.monotonic()
        try:
            content, response = client.complete(payload)
        except requests.ConnectionError as exc:
            log.error("lost connection to Nemotron: %s", exc)
            return 1
        except requests.RequestException as exc:
            log.error("Nemotron request failed for %s: %s", label, exc)
            stats["failed"] += 1
            continue
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            decision = parse_decision(content, payload)
        except (ValueError, ValidationError) as exc:
            log.warning("unusable reply for %s (logged; retried next run): %s", label, str(exc).splitlines()[0])
            decision = None
        log_decision(conn, payload, client.model, decision, response, latency_ms)
        if decision:
            stats["decided"] += 1
            results.append((shipment_id, decision, payload.signal.title))
            log.info("%s -> %s (%s, %.2f) in %.1fs", label, decision.recommended_action, decision.risk_level,
                     decision.risk_score, latency_ms / 1000)
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
