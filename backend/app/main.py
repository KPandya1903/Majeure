"""PortGuard AI API — FastAPI backend serving the React frontend.

Reads/writes the same data/portguard.db SQLite database the pipeline and (legacy) Streamlit
dashboard use. Swap to Snowflake later by pointing DB_BACKEND=snowflake and using src/db.py's
connection instead of _connect() below — the query shapes returned to the frontend don't change.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
DB_PATH = ROOT / "data" / "portguard.db"
sys.path.insert(0, str(SRC))
from common import CHOKEPOINTS  # noqa: E402
from routing import dot_count, dotted_points, route_path, route_progress, split_path, unwrap_lons  # noqa: E402

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
ACTIONS = {"monitor": "Monitor", "notify_customer": "Notify customer", "reroute": "Reroute",
           "expedite": "Expedite", "hold_cargo": "Hold cargo", "invoke_force_majeure": "Invoke force majeure"}

app = FastAPI(title="PortGuard AI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(503, "No database yet — run the pipeline first (see README §1).")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def hours_until(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    due = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (due - datetime.now(timezone.utc)).total_seconds() / 3600


# --------------------------------------------------------------------------- reference data


def location_coords() -> dict[str, tuple[float, float, str, str]]:
    coords = {c.location_id: (c.lat, c.lon, c.name, "") for c in CHOKEPOINTS}
    for r in _rows("SELECT port_id, port_name, lat, lon, locode FROM ports"):
        locode = (r["locode"] or "").replace(" ", "")
        coords[r["port_id"]] = (r["lat"], r["lon"], r["port_name"], locode)
    return coords


# --------------------------------------------------------------------------- KPIs


@app.get("/api/kpis")
def kpis():
    events = _rows("SELECT * FROM events")
    shipments = _rows("SELECT * FROM shipments")
    decisions = _rows("""SELECT d.*, s.title FROM decision_logs d JOIN signals s USING (signal_id)
                         WHERE d.recommended_action IS NOT NULL""")
    seen = set()
    latest = []
    for d in sorted(decisions, key=lambda d: d["created_at"], reverse=True):
        pair = (d.get("event_id") or d["signal_id"], d["shipment_id"])
        if pair not in seen:
            seen.add(pair)
            latest.append(d)
    at_risk_ships = {d["shipment_id"] for d in latest if d["risk_level"] in ("high", "critical")}
    n_critical = len({d["shipment_id"] for d in latest if d["risk_level"] == "critical"})
    n_high = len({d["shipment_id"] for d in latest if d["risk_level"] == "high"})
    pending = [d for d in latest if d["human_decision"] == "pending"]
    exposure = sum(d.get("estimated_cost_usd") or 0 for d in latest if d["human_decision"] != "overridden")
    drafts = _rows("SELECT deadline_at FROM action_drafts WHERE status = 'draft' AND deadline_at IS NOT NULL")
    deadlines = [h for h in (hours_until(d["deadline_at"]) for d in drafts) if h is not None]
    return {
        "active_events": len(events),
        "structured_events": sum(1 for e in events if e["is_structured"]),
        "shipments_at_risk": len(at_risk_ships), "shipments_total": len(shipments),
        "n_critical": n_critical, "n_high": n_high,
        "pending_decisions": len(pending),
        "open_exposure_usd": exposure,
        "next_deadline_hours": min(deadlines) if deadlines else None,
    }


# --------------------------------------------------------------------------- risk map


@app.get("/api/map")
def map_data():
    coords = location_coords()
    shipments = _rows("SELECT * FROM shipments")
    decisions = _rows("""SELECT d.*, s.title FROM decision_logs d JOIN signals s USING (signal_id)
                         WHERE d.recommended_action IS NOT NULL""")
    seen = set()
    latest = []
    for d in sorted(decisions, key=lambda d: d["created_at"], reverse=True):
        pair = (d.get("event_id") or d["signal_id"], d["shipment_id"])
        if pair not in seen:
            seen.add(pair)
            latest.append(d)
    risk_by_ship: dict[str, dict] = {}
    for d in latest:
        cur = risk_by_ship.get(d["shipment_id"])
        if not cur or RISK_ORDER[d["risk_level"]] > RISK_ORDER[cur["risk_level"]]:
            risk_by_ship[d["shipment_id"]] = d

    routes = []
    for r in shipments:
        if r["pol_port_id"] not in coords or r["pod_port_id"] not in coords:
            continue
        path = unwrap_lons(route_path(r["pol_port_id"], r["pod_port_id"], json.loads(r["transshipment_port_ids"]),
                                      json.loads(r["route_chokepoints"]), coords))
        progress = route_progress(r["etd"], r["eta"])
        sailed, remaining = split_path(path, progress)
        top = risk_by_ship.get(r["shipment_id"])
        routes.append({
            "shipment_id": r["shipment_id"],
            "sailed": sailed,
            "remaining_dots": dotted_points(remaining, n=dot_count(remaining)),
            "progress": progress,
            "risk_level": top["risk_level"] if top else None,
            "recommended_action": top["recommended_action"] if top else None,
            "label": f"{r['shipment_id']}: {coords[r['pol_port_id']][2]} → {coords[r['pod_port_id']][2]}",
        })

    ev_points = _rows("""SELECT e.*, s.location_lat AS lat, s.location_lon AS lon FROM events e
                         JOIN signals s ON s.signal_id = e.anchor_signal_id WHERE s.location_lat IS NOT NULL""")
    traffic = {r["location_id"]: r for r in _rows("SELECT * FROM traffic_stats WHERE metric = 'transits'")}
    for e in ev_points:
        e["radius"] = 55000 + 42000 * math.log2(1 + e["n_signals"])
        tr = traffic.get(e["location_id"])
        e["pct_change"] = tr["pct_change"] if tr else None

    ports_used = set()
    for r in shipments:
        ports_used.update(p for p in [r["pol_port_id"], r["pod_port_id"], *json.loads(r["transshipment_port_ids"])]
                          if p in coords)
    port_points = [{"lon": coords[p][1], "lat": coords[p][0], "name": coords[p][2], "locode": coords[p][3]}
                  for p in ports_used]
    chokepoints = [{"lon": c.lon, "lat": c.lat, "name": c.name, "location_id": c.location_id,
                    "pct_change": traffic.get(c.location_id, {}).get("pct_change")} for c in CHOKEPOINTS]

    return {"routes": routes, "events": ev_points, "ports": port_points, "chokepoints": chokepoints}


# --------------------------------------------------------------------------- shipments & decisions


@app.get("/api/shipments")
def shipments():
    coords = location_coords()
    ships = _rows("SELECT * FROM shipments")
    decisions = _rows("""SELECT d.*, s.title, s.source_url FROM decision_logs d JOIN signals s USING (signal_id)
                         WHERE d.recommended_action IS NOT NULL""")
    by_ship: dict[str, list[dict]] = {}
    seen = set()
    for d in sorted(decisions, key=lambda d: d["created_at"], reverse=True):
        pair = (d.get("event_id") or d["signal_id"], d["shipment_id"])
        if pair in seen:
            continue
        seen.add(pair)
        by_ship.setdefault(d["shipment_id"], []).append(d)

    out = []
    for r in ships:
        if r["pol_port_id"] not in coords or r["pod_port_id"] not in coords:
            continue
        ship_decisions = sorted(by_ship.get(r["shipment_id"], []), key=lambda d: RISK_ORDER.get(d["risk_level"], 0), reverse=True)
        out.append({
            "shipment_id": r["shipment_id"], "bl_number": r["bl_number"],
            "pol": coords[r["pol_port_id"]][2], "pol_locode": coords[r["pol_port_id"]][3] or coords[r["pol_port_id"]][2],
            "pod": coords[r["pod_port_id"]][2], "pod_locode": coords[r["pod_port_id"]][3] or coords[r["pod_port_id"]][2],
            "transshipment": [coords[p][2] for p in json.loads(r["transshipment_port_ids"]) if p in coords],
            "eta": r["eta"], "etd": r["etd"], "vessel_name": r["vessel_name"], "voyage_number": r["voyage_number"],
            "cargo_description": r["cargo_description"], "container_count": r["container_count"],
            "cargo_value_usd": r["cargo_value_usd"], "incoterm": r["incoterm"],
            "decisions": [_decision_detail(d) for d in ship_decisions],
        })
    return out


def _decision_detail(d: dict) -> dict:
    payload = json.loads(d["input_payload"])
    analysis = payload.get("analysis") or {}
    return {
        "decision_id": d["decision_id"], "risk_level": d["risk_level"], "risk_score": d["risk_score"],
        "recommended_action": d["recommended_action"],
        "recommended_action_label": ACTIONS.get(d["recommended_action"], d["recommended_action"]),
        "estimated_cost_usd": d["estimated_cost_usd"], "rationale": d["rationale"],
        "citations": json.loads(d["citations"]), "human_decision": d["human_decision"],
        "human_notes": d["human_notes"], "title": d["title"], "model": d["model"],
        "exposure": analysis.get("exposure"), "traffic": analysis.get("traffic"),
        "corroboration": analysis.get("corroboration"), "match_context": payload.get("match_context"),
    }


class DecisionUpdate(BaseModel):
    human_decision: str
    human_notes: Optional[str] = None


@app.post("/api/decisions/{decision_id}/review")
def review_decision(decision_id: int, body: DecisionUpdate):
    if body.human_decision not in ("approved", "overridden"):
        raise HTTPException(400, "human_decision must be 'approved' or 'overridden'")
    with _connect() as conn:
        conn.execute("UPDATE decision_logs SET human_decision=?, human_notes=? WHERE decision_id=?",
                    (body.human_decision, body.human_notes, decision_id))
    return {"ok": True}


# --------------------------------------------------------------------------- drafts


@app.get("/api/decisions/{decision_id}/drafts")
def decision_drafts(decision_id: int):
    return _rows("SELECT * FROM action_drafts WHERE decision_id = ?", (decision_id,))


class DraftUpdate(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None


@app.patch("/api/drafts/{draft_id}")
def update_draft(draft_id: int, body: DraftUpdate):
    existing = _row("SELECT * FROM action_drafts WHERE draft_id = ?", (draft_id,))
    if not existing:
        raise HTTPException(404, "draft not found")
    subj = body.subject if body.subject is not None else existing["subject"]
    bod = body.body if body.body is not None else existing["body"]
    status = body.status if body.status is not None else existing["status"]
    with _connect() as conn:
        conn.execute("UPDATE action_drafts SET subject=?, body=?, status=? WHERE draft_id=?",
                    (subj, bod, status, draft_id))
    return {"ok": True}


# --------------------------------------------------------------------------- events / traffic / backtest / signals


@app.get("/api/events")
def events():
    return _rows("SELECT * FROM events ORDER BY is_structured DESC, n_signals DESC")


@app.get("/api/events/{event_id}/signals")
def event_signals(event_id: str):
    return _rows("""SELECT s.published_at, s.source, s.title, s.source_url FROM signals s
                    JOIN event_signals es USING (signal_id) WHERE es.event_id = ? ORDER BY s.published_at DESC""",
                (event_id,))


@app.get("/api/traffic")
def traffic():
    return _rows("SELECT * FROM traffic_stats ORDER BY pct_change")


@app.get("/api/traffic/{location_id}/daily")
def traffic_daily(location_id: str):
    return _rows("SELECT date, value FROM traffic_daily WHERE location_id = ? ORDER BY date", (location_id,))


@app.get("/api/backtest")
def backtest():
    path = ROOT / "data" / "backtest.json"
    if not path.exists():
        return {"scenarios": [], "note": "Run python src/backtest.py to generate this."}
    return json.loads(path.read_text())


@app.get("/api/signals")
def signals(source: Optional[str] = None, event_type: Optional[str] = None, q: Optional[str] = None, limit: int = 500):
    sql = "SELECT published_at, source, event_type, severity, location_name, title, source_url FROM signals WHERE 1=1"
    params: list = []
    if source:
        sql += " AND source = ?"; params.append(source)
    if event_type:
        sql += " AND event_type = ?"; params.append(event_type)
    if q:
        sql += " AND title LIKE ?"; params.append(f"%{q}%")
    sql += " ORDER BY published_at DESC LIMIT ?"; params.append(limit)
    return _rows(sql, tuple(params))


class ChatRequest(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: ChatRequest):
    try:
        sys.path.insert(0, str(SRC))
        from ask import answer
        return {"answer": answer(body.question)}
    except Exception as exc:
        return {"answer": f"Nemotron is not reachable ({type(exc).__name__}). Check NEMOTRON_BASE_URL in .env."}


@app.get("/api/health")
def health():
    return {"ok": True, "db_exists": DB_PATH.exists(), "pipeline_running": _pipeline_running()}


# --------------------------------------------------------------------------- pipeline trigger
#
# Render's free tier has no persistent disk and no scheduler of its own, so there's no
# `run_pipeline.py --loop 5` running here the way there is locally. Instead, an external free
# cron service (e.g. cron-job.org) hits this endpoint every 5-10 minutes: each hit both runs
# one pipeline tick AND, as a side effect, counts as traffic that keeps this free instance from
# idling out (Render spins a free service down after ~15 min of no requests, which would
# otherwise reset the filesystem back to whatever's in the last deploy on the next request).
# Note this does NOT survive an actual code redeploy — that always resets to the git-committed
# snapshot in data/portguard.db, disk or no disk, since the deploy checks out fresh from git.

PIPELINE_TOKEN = os.environ.get("PIPELINE_TRIGGER_TOKEN")
_pipeline_proc: Optional[subprocess.Popen] = None


def _pipeline_running() -> bool:
    return _pipeline_proc is not None and _pipeline_proc.poll() is None


@app.post("/api/pipeline/run")
def trigger_pipeline(token: Optional[str] = None):
    """Fire-and-forget one pipeline tick. Non-blocking (Popen, not run) so it doesn't stall
    the single request worker (WEB_CONCURRENCY=1 on Render's free plan) for other users while
    it's fetching/deciding; a module-level guard skips starting a second run if one is still
    going, since two processes writing the same SQLite file at once can deadlock/corrupt it."""
    if PIPELINE_TOKEN and token != PIPELINE_TOKEN:
        raise HTTPException(403, "invalid or missing token")
    global _pipeline_proc
    if _pipeline_running():
        return {"ok": True, "status": "already_running"}
    engine = os.environ.get("PIPELINE_ENGINE", "rules")  # "rules" needs no reachable LLM
    _pipeline_proc = subprocess.Popen([sys.executable, str(SRC / "run_pipeline.py"), "--engine", engine], cwd=ROOT)
    return {"ok": True, "status": "started", "engine": engine}
