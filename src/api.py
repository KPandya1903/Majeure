"""PortGuard Demo API — lightweight, read-only FastAPI for DigitalOcean App Platform.

Serves seeded demo data from data/demo_portguard.db (built by src/seed_demo_db.py).
Does not call Nemotron, Gemini, or any live ingest pipeline.

Run (from repository root):
    uvicorn api:app --app-dir src --host 0.0.0.0 --port ${PORT:-8080}
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# When launched as `uvicorn api:app --app-dir src`, this file's directory is already on sys.path.
# When imported from the repo root without --app-dir, ensure sibling modules resolve.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import ROOT  # noqa: E402
from seed_demo_db import DEMO_DB, SEEDED_AT, seed  # noqa: E402

SERVICE_NAME = "portguard-demo-api"
APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-demo")
ACTIONS = {
    "monitor": "Monitor",
    "notify_customer": "Notify customer",
    "reroute": "Reroute",
    "expedite": "Expedite",
    "hold_cargo": "Hold cargo",
    "invoke_force_majeure": "Invoke force majeure",
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _environment() -> str:
    return (os.environ.get("PORTGUARD_ENV")
            or os.environ.get("ENVIRONMENT")
            or "development").strip().lower()


def _demo_mode() -> bool:
    raw = (os.environ.get("DEMO_MODE") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _db_path() -> Path:
    override = os.environ.get("PORTGUARD_DEMO_DB") or os.environ.get("DEMO_DB_PATH")
    return Path(override) if override else DEMO_DB


def _cors_origins() -> list[str]:
    """Production requires explicit CORS_ORIGINS; development also allows localhost Vite ports."""
    configured = [o.strip() for o in (os.environ.get("CORS_ORIGINS") or "").split(",") if o.strip()]
    if "*" in configured:
        # Never honour a wildcard — strip it so production cannot accidentally open the API.
        configured = [o for o in configured if o != "*"]
    env = _environment()
    if env in ("development", "dev", "local"):
        for origin in (
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ):
            if origin not in configured:
                configured.append(origin)
    return configured


def ensure_demo_db() -> Path:
    """Create the demo DB if missing. Idempotent when the file already exists."""
    path = _db_path()
    # Force SQLite for this service even if a Render-style DB_BACKEND=postgres is in the ambient env.
    os.environ["DB_BACKEND"] = "sqlite"
    if not path.exists():
        seed(path)
    return path


def _connect() -> sqlite3.Connection:
    path = ensure_demo_db()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "status": status}},
    )


app = FastAPI(
    title="PortGuard Demo API",
    version=APP_VERSION,
    description="Read-only demo API for DigitalOcean App Platform (seeded SQLite, no model keys).",
)


@app.on_event("startup")
def _startup() -> None:
    ensure_demo_db()


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    code = "not_found" if exc.status_code == 404 else "http_error"
    if exc.status_code == 503:
        code = "service_unavailable"
    return _error(exc.status_code, code, detail)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return _error(500, "internal_error", f"{type(exc).__name__}: {exc}")


def _data_freshness() -> Optional[str]:
    """Truthful freshness: demo seed timestamp when present, else newest event update."""
    try:
        rows = _rows(
            "SELECT last_fetched_at FROM pipeline_state WHERE source_group = 'demo_seed'"
        )
        if rows:
            return rows[0]["last_fetched_at"]
        rows = _rows("SELECT MAX(updated_at) AS ts FROM events")
        return rows[0]["ts"] if rows and rows[0]["ts"] else SEEDED_AT
    except Exception:
        return SEEDED_AT if _db_path().exists() else None


def _port_lookup() -> dict[str, tuple[str, str]]:
    """port_id -> (name, locode-or-name)."""
    out: dict[str, tuple[str, str]] = {}
    for r in _rows("SELECT port_id, port_name, locode FROM ports"):
        locode = (r["locode"] or "").replace(" ", "") or r["port_name"]
        out[r["port_id"]] = (r["port_name"], locode)
    return out


def _decision_detail(d: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(d["input_payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    analysis = payload.get("analysis") or {}
    try:
        citations = json.loads(d["citations"] or "[]")
    except json.JSONDecodeError:
        citations = []
    return {
        "decision_id": d["decision_id"],
        "event_id": d.get("event_id"),
        "signal_id": d["signal_id"],
        "shipment_id": d["shipment_id"],
        "risk_level": d["risk_level"],
        "risk_score": d["risk_score"],
        "recommended_action": d["recommended_action"],
        "recommended_action_label": ACTIONS.get(d["recommended_action"], d["recommended_action"]),
        "estimated_cost_usd": d["estimated_cost_usd"],
        "force_majeure_applicable": (
            None if d.get("force_majeure_applicable") is None
            else bool(d["force_majeure_applicable"])
        ),
        "rationale": d["rationale"],
        "citations": citations,
        "human_decision": d["human_decision"],
        "human_notes": d.get("human_notes"),
        "title": d.get("title"),
        "model": d["model"],
        "created_at": d["created_at"],
        "exposure": analysis.get("exposure"),
        "traffic": analysis.get("traffic"),
        "corroboration": analysis.get("corroboration"),
        "match_context": payload.get("match_context"),
    }


# --------------------------------------------------------------------------- endpoints


@app.get("/health")
def health() -> dict[str, Any]:
    path = _db_path()
    db_ok = False
    tables: dict[str, int] = {}
    detail = "ok"
    try:
        if path.exists():
            with _connect() as conn:
                for table in ("events", "shipments", "decision_logs", "traffic_stats"):
                    tables[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            db_ok = True
        else:
            detail = "demo database missing"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "ok": db_ok,
        "service": SERVICE_NAME,
        "version": APP_VERSION,
        "demo_mode": _demo_mode(),
        "environment": _environment(),
        "db_path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "db_exists": path.exists(),
        "table_counts": tables,
        "detail": detail,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    freshness = _data_freshness()
    counts = {}
    try:
        for table in ("events", "shipments", "decision_logs", "traffic_stats", "signals"):
            rows = _rows(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = rows[0]["n"] if rows else 0
    except Exception:
        counts = {}
    return {
        "service": SERVICE_NAME,
        "version": APP_VERSION,
        "environment": _environment(),
        "demo_mode": _demo_mode(),
        "read_only": True,
        "data_freshness": freshness,
        "data_note": (
            "Seeded demo SQLite snapshot for DigitalOcean App Platform. "
            "Not live PortWatch/GDELT ingestion; not Nemotron inference."
        ),
        "counts": counts,
        "endpoints": [
            "GET /health",
            "GET /api/meta",
            "GET /api/events",
            "GET /api/shipments",
            "GET /api/decisions",
            "GET /api/traffic",
        ],
    }


@app.get("/api/events")
def events() -> list[dict[str, Any]]:
    try:
        return _rows(
            """SELECT event_id, title, event_type, severity, location_id, location_name,
                      location_kind, anchor_signal_id, is_structured, n_signals, n_sources,
                      n_publishers, confidence, first_seen, last_seen, updated_at
               FROM events
               ORDER BY is_structured DESC, n_signals DESC"""
        )
    except sqlite3.Error:
        return []


@app.get("/api/shipments")
def shipments() -> list[dict[str, Any]]:
    try:
        ports = _port_lookup()
        ships = _rows("SELECT * FROM shipments ORDER BY shipment_id")
        decisions = _rows(
            """SELECT d.*, s.title, s.source_url
               FROM decision_logs d
               JOIN signals s USING (signal_id)
               WHERE d.recommended_action IS NOT NULL
               ORDER BY d.created_at DESC"""
        )
    except sqlite3.Error:
        return []

    by_ship: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for d in decisions:
        pair = (d.get("event_id") or d["signal_id"], d["shipment_id"])
        if pair in seen:
            continue
        seen.add(pair)
        by_ship.setdefault(d["shipment_id"], []).append(d)

    out: list[dict[str, Any]] = []
    for r in ships:
        pol = ports.get(r["pol_port_id"])
        pod = ports.get(r["pod_port_id"])
        if not pol or not pod:
            # Graceful skip if a FK port is somehow missing from the demo set.
            continue
        try:
            transshipment_ids = json.loads(r["transshipment_port_ids"] or "[]")
        except json.JSONDecodeError:
            transshipment_ids = []
        ship_decisions = sorted(
            by_ship.get(r["shipment_id"], []),
            key=lambda d: RISK_ORDER.get(d["risk_level"] or "low", 0),
            reverse=True,
        )
        out.append({
            "shipment_id": r["shipment_id"],
            "bl_number": r["bl_number"],
            "pol": pol[0],
            "pol_locode": pol[1],
            "pod": pod[0],
            "pod_locode": pod[1],
            "transshipment": [ports[p][0] for p in transshipment_ids if p in ports],
            "eta": r["eta"],
            "etd": r["etd"],
            "vessel_name": r["vessel_name"],
            "voyage_number": r["voyage_number"],
            "cargo_description": r["cargo_description"],
            "container_count": r["container_count"],
            "cargo_value_usd": r["cargo_value_usd"],
            "incoterm": r["incoterm"],
            "carrier": r["carrier"],
            "imo": r.get("imo"),
            "vessel_source": r.get("vessel_source"),
            "carrier_source": r.get("carrier_source"),
            "terms_source": r.get("terms_source"),
            "status": r["status"],
            "decisions": [_decision_detail(d) for d in ship_decisions],
        })
    return out


@app.get("/api/decisions")
def decisions() -> list[dict[str, Any]]:
    try:
        rows = _rows(
            """SELECT d.*, s.title, s.source_url
               FROM decision_logs d
               JOIN signals s USING (signal_id)
               WHERE d.recommended_action IS NOT NULL
               ORDER BY d.created_at DESC"""
        )
    except sqlite3.Error:
        return []
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for d in rows:
        pair = (d.get("event_id") or d["signal_id"], d["shipment_id"])
        if pair in seen:
            continue
        seen.add(pair)
        out.append(_decision_detail(d))
    return out


@app.get("/api/traffic")
def traffic() -> list[dict[str, Any]]:
    try:
        return _rows(
            """SELECT location_id, name, metric, latest_date, avg_7d, baseline_avg,
                      pct_change, updated_at
               FROM traffic_stats
               ORDER BY pct_change"""
        )
    except sqlite3.Error:
        return []
