"""Fetch measured daily traffic from IMF PortWatch: ship transits through all 28 chokepoints and
port calls at the ports your shipments use.

Outputs: data/traffic_daily.csv (daily series) and data/traffic_stats.csv (latest 7-day average
vs the 90 days before it). init_db.py loads both.
Layer notes: docs/DATA_SOURCES.md, section 1.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

import store
from common import CHOKEPOINTS_BY_PORTWATCH_ID, DATA_DIR, ROOT, http_get, iso, setup_logging, utcnow, write_csv

ARCGIS_ROOT = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
CHOKEPOINT_LAYER = f"{ARCGIS_ROOT}/Daily_Chokepoints_Data/FeatureServer/0/query"
PORT_LAYER = f"{ARCGIS_ROOT}/Daily_Ports_Data/FeatureServer/0/query"
DAILY_CSV = DATA_DIR / "traffic_daily.csv"
STATS_CSV = DATA_DIR / "traffic_stats.csv"
DB_PATH = DATA_DIR / "portguard.db"
SAMPLE_SHIPMENTS = ROOT / "samples" / "shipments.sample.json"
RECENT_DAYS, BASELINE_DAYS = 7, 90

log = logging.getLogger("portguard.traffic")


def query_daily(layer: str, where: str, value_field: str) -> pd.DataFrame:
    """All (portid, date, value) rows matching `where`, paging through the ArcGIS layer."""
    rows: list[dict] = []
    while True:
        body = http_get(layer, timeout=60, params={
            "where": where, "outFields": f"portid,portname,date,{value_field}", "orderByFields": "ObjectId ASC",
            "resultOffset": len(rows), "resultRecordCount": 1000, "returnGeometry": "false", "f": "json",
        }).json()
        if "error" in body:
            raise RuntimeError(f"ArcGIS error: {body['error']}")
        features = body.get("features", [])
        rows.extend(f["attributes"] for f in features)
        if not features or not body.get("exceededTransferLimit"):
            break
    df = pd.DataFrame(rows, columns=["portid", "portname", "date", value_field])
    return df.rename(columns={value_field: "value"})


def shipment_port_ids(db: Path) -> set[str]:
    """Ports used by shipments: from the DB if it has shipments, else from the demo sample file."""
    if store.database_ready(db):
        with store.session(db) as conn:
            try:
                rows = conn.execute("SELECT pol_port_id, pod_port_id, transshipment_port_ids FROM shipments").fetchall()
            except sqlite3.OperationalError:
                rows = []
        if rows:
            return {p for pol, pod, ts in rows for p in (pol, pod, *json.loads(ts or "[]"))}
    ids: set[str] = set()
    for s in json.loads(SAMPLE_SHIPMENTS.read_text(encoding="utf-8")):
        bl = s["bill_of_lading"]
        ids |= {bl["port_of_loading"]["port_id"], bl["port_of_discharge"]["port_id"],
                *(p["port_id"] for p in bl.get("transshipment_ports", []))}
    return ids


def compute_stats(daily: pd.DataFrame, now_iso: str) -> pd.DataFrame:
    """Latest 7-day mean vs the 90 days before it, per location."""
    out = []
    for (location_id, name, metric), g in daily.groupby(["location_id", "name", "metric"]):
        g = g.sort_values("date")
        latest = date.fromisoformat(g["date"].iloc[-1])
        recent = g[g["date"] > (latest - timedelta(days=RECENT_DAYS)).isoformat()]["value"]
        base = g[(g["date"] <= (latest - timedelta(days=RECENT_DAYS)).isoformat())
                 & (g["date"] > (latest - timedelta(days=RECENT_DAYS + BASELINE_DAYS)).isoformat())]["value"]
        if recent.empty or base.empty:
            continue
        baseline = float(base.mean())
        out.append({
            "location_id": location_id, "name": name, "metric": metric, "latest_date": latest.isoformat(),
            "avg_7d": round(float(recent.mean()), 1), "baseline_avg": round(baseline, 1),
            "pct_change": round((recent.mean() - baseline) / baseline * 100, 1) if baseline else None,
            "updated_at": now_iso,
        })
    return pd.DataFrame(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=RECENT_DAYS + BASELINE_DAYS + 14,
                        help="days of history to fetch (default: 111, enough for the 90-day baseline plus data lag)")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="read shipment ports from this database")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    since = (utcnow().date() - timedelta(days=args.days)).isoformat()
    ports = sorted(shipment_port_ids(args.db))
    try:
        chokepoints = query_daily(CHOKEPOINT_LAYER, f"date >= DATE '{since}'", "n_total")
        port_calls = (query_daily(PORT_LAYER, f"date >= DATE '{since}' AND portid IN ({', '.join(repr(p) for p in ports)})",
                                  "portcalls") if ports else pd.DataFrame(columns=["portid", "portname", "date", "value"]))
    except Exception as exc:  # keep the previous outputs intact
        log.error("PortWatch traffic fetch failed: %s", exc)
        return 1

    chokepoints = chokepoints[chokepoints["portid"].isin(CHOKEPOINTS_BY_PORTWATCH_ID)]
    chokepoints = chokepoints.assign(location_id=chokepoints["portid"].map(lambda p: CHOKEPOINTS_BY_PORTWATCH_ID[p].location_id),
                                     metric="transits")
    port_calls = port_calls.assign(location_id=port_calls["portid"], metric="port_calls")
    daily = pd.concat([chokepoints, port_calls], ignore_index=True).rename(columns={"portname": "name"})
    daily = daily.dropna(subset=["value"]).drop_duplicates(["location_id", "date"]).sort_values(["location_id", "date"])

    stats = compute_stats(daily, iso(utcnow()))
    write_csv(DAILY_CSV, daily[["location_id", "name", "date", "metric", "value"]])
    write_csv(STATS_CSV, stats)
    log.info("wrote %d daily rows for %d chokepoints + %d ports -> %s", len(daily),
             chokepoints["location_id"].nunique(), port_calls["location_id"].nunique(), DAILY_CSV.name)
    if not stats.empty:
        top = stats.dropna(subset=["pct_change"]).sort_values("pct_change").head(4)
        log.info("biggest drops vs 90-day baseline: %s",
                 "; ".join(f"{r.name} {r.pct_change:+.0f}% ({r.avg_7d:g}/day)" for r in top.itertuples()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
