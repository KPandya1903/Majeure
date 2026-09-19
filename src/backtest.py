"""Backtest: replay historical disruptions and show what PortGuard would have done.

For each scenario, the event (from IMF PortWatch) and the traffic data are real; the shipment is a
synthetic example routed through the affected place. PortGuard is evaluated "as of" the day after the
event started, using only the traffic known then; the traffic of the following month is reported
separately as what actually happened. Output: data/backtest.json (shown in the dashboard) + a table.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from common import CHOKEPOINTS, DATA_DIR, PORTS_CSV, iso, nearby_ports, setup_logging, utcnow
from exposure import compute_exposure
from fetch_traffic import CHOKEPOINT_LAYER, PORT_LAYER, query_daily
from run_nemotron import (REASON_PRIORITY, RULES_MODEL, SYSTEM_PROMPT, decision_schema, feasible_actions, parse_decision,
                          rules_decision)
from schemas import Analysis, MatchContext, NemotronPayload, Shipment, Task, Traffic, XtractSignal

OUT_JSON = DATA_DIR / "backtest.json"
SIGNALS_JSON = DATA_DIR / "portwatch_signals.json"
FM_WAR = ("Neither party is liable for delay caused by strikes, war, blockade, hostilities, or closure of ports or "
          "canals beyond its reasonable control, provided written notice is given within 48 hours.")
FM_NARROW = ("Force majeure covers natural disasters and government-ordered port closures only. War risk and security "
             "incidents are excluded and remain at the buyer's risk.")
FM_GENERIC = "Standard BIMCO force majeure clause."

SCENARIOS = [
    {"scenario": "Red Sea crisis (Dec 2023)", "event": "RED SEA TENSIONS", "pol": "Shanghai", "pod": "Rotterdam",
     "via_ports": ["Singapore"], "chokepoints": ["chokepoint:malacca-strait", "chokepoint:bab-el-mandeb-strait", "chokepoint:suez-canal"],
     "eta_offset_days": 27, "containers": 12, "value": 480000, "fm": FM_WAR, "deviation": True, "penalty": 1000},
    {"scenario": "Baltimore bridge collapse (Mar 2024)", "event": "COLLAPSE FRANCIS SCOTT KEY BRIDGE", "pol": "Rotterdam",
     "pod": "Baltimore", "via_ports": [], "chokepoints": [], "eta_offset_days": 6, "containers": 20, "value": 900000,
     "fm": FM_WAR, "deviation": True, "penalty": 1200},
    {"scenario": "US East & Gulf Coast port strike (Oct 2024)", "event": "US PORTS STRIKE", "pol": "Shanghai",
     "pod": "New York-New Jersey", "via_ports": [], "chokepoints": ["chokepoint:panama-canal"], "eta_offset_days": 3,
     "containers": 30, "value": 1500000, "fm": FM_GENERIC, "deviation": True, "penalty": 2000},
    {"scenario": "Panama Canal drought restrictions (2023)", "event": "RESTRICTIONS PANAMA CANAL", "pol": "Shanghai",
     "pod": "Houston (US-TX)", "via_ports": [], "chokepoints": ["chokepoint:panama-canal"], "eta_offset_days": 20,
     "containers": 15, "value": 700000, "fm": FM_NARROW, "deviation": True, "penalty": 800},
    {"scenario": "Strait of Hormuz disruption (Mar 2026)", "event": "HORMUZ-26", "pol": "Jebel Ali",
     "pod": "Mumbai-Jawaharlal Nehru (Nhava Sheva)", "via_ports": [], "chokepoints": ["chokepoint:strait-of-hormuz"],
     "eta_offset_days": 5, "containers": 30, "value": 610000, "fm": FM_NARROW, "deviation": False, "penalty": 2500},
]

log = logging.getLogger("portguard.backtest")
_CHOKE = {c.location_id: c for c in CHOKEPOINTS}


def port_ref(ports: pd.DataFrame, name: str) -> dict:
    r = ports[ports["port_name"] == name].sort_values("vessel_count_total", ascending=False).iloc[0]
    return {"port_id": r.port_id, "name": r.port_name, "country": r.country, "lat": r.lat, "lon": r.lon}


def build_shipment(sc: dict, ports: pd.DataFrame, start: datetime) -> Shipment:
    eta = start + timedelta(days=sc["eta_offset_days"])
    return Shipment.model_validate({
        "shipment_id": "BT-" + sc["event"].replace(" ", "-")[:20], "bill_of_lading": {
            "bl_number": "BACKTEST-" + sc["event"][:12].replace(" ", "-"), "carrier": "Example Line",
            "port_of_loading": port_ref(ports, sc["pol"]), "port_of_discharge": port_ref(ports, sc["pod"]),
            "transshipment_ports": [port_ref(ports, p) for p in sc["via_ports"]], "route_chokepoints": sc["chokepoints"],
            "etd": eta - timedelta(days=30), "eta": eta, "container_count": sc["containers"], "cargo_value_usd": sc["value"]},
        "contract_terms": {"incoterm": "CIF", "force_majeure_clause": sc["fm"], "demurrage_usd_per_day": 150,
                           "detention_usd_per_day": 100, "free_time_days": 5, "late_delivery_penalty_usd_per_day": sc["penalty"],
                           "deviation_permitted": sc["deviation"], "notice_period_hours": 48}})


def match(signal: XtractSignal, shipment: Shipment) -> Optional[tuple[str, str]]:
    bl, ids = shipment.bill_of_lading, set(signal.related_port_ids)
    candidates = {"port_of_discharge": [bl.port_of_discharge.port_id], "port_of_loading": [bl.port_of_loading.port_id],
                  "transshipment_port": [p.port_id for p in bl.transshipment_ports], "route_chokepoint": bl.route_chokepoints}
    for reason in REASON_PRIORITY:
        hit = next((p for p in candidates[reason] if p in ids), None)
        if hit:
            return reason, hit
    return None


def traffic_window(location_id: str, start: date) -> pd.DataFrame:
    lo, hi = (start - timedelta(days=100)).isoformat(), (start + timedelta(days=60)).isoformat()
    if location_id in _CHOKE:
        df = query_daily(CHOKEPOINT_LAYER, f"portid = '{_CHOKE[location_id].portwatch_id}' AND date >= DATE '{lo}' AND date <= DATE '{hi}'", "n_total")
    else:
        df = query_daily(PORT_LAYER, f"portid = '{location_id}' AND date >= DATE '{lo}' AND date <= DATE '{hi}'", "portcalls")
    return df[["date", "value"]].dropna().sort_values("date").reset_index(drop=True)


def run_scenario(sc: dict, signals: dict, ports: pd.DataFrame, client=None) -> dict:
    signal = signals.get(sc["event"])
    if not signal:
        return {"scenario": sc["scenario"], "event": sc["event"], "matched": False, "summary": "Event not found in PortWatch data."}
    start = signal.published_at
    as_of = start + timedelta(days=1)
    shipment = build_shipment(sc, ports, start)
    hit = match(signal, shipment)
    if not hit:
        return {"scenario": sc["scenario"], "event": sc["event"], "matched": False, "summary": "No overlap with the route."}
    reason, location_id = hit
    daily = traffic_window(location_id, start.date())
    before = daily[daily["date"] < start.date().isoformat()]
    known = daily[daily["date"] <= as_of.date().isoformat()]
    after = daily[(daily["date"] >= (start.date() + timedelta(days=7)).isoformat())
                  & (daily["date"] < (start.date() + timedelta(days=37)).isoformat())]
    base = before.tail(30)["value"].mean() if not before.empty else None
    change = round((after["value"].mean() - base) / base * 100, 1) if base and not after.empty else None
    traffic_known = None
    if len(known) >= 10:
        recent, prior = known.tail(7)["value"].mean(), known.iloc[:-7].tail(90)["value"].mean()
        traffic_known = Traffic(location_id=location_id, name=_CHOKE[location_id].name if location_id in _CHOKE else location_id,
                                metric="transits" if location_id in _CHOKE else "port_calls", latest_date=known["date"].iloc[-1],
                                avg_7d=round(recent, 1), baseline_avg=round(prior, 1),
                                pct_change=round((recent - prior) / prior * 100, 1) if prior else None)
    mc = MatchContext(match_reason=reason, matched_port_id=location_id, distance_km=0.0,
                      days_until_eta=round((shipment.bill_of_lading.eta - as_of).total_seconds() / 86400, 1))
    alt = None
    if reason != "route_chokepoint":
        o = ports[ports["port_id"] == location_id].iloc[0]
        pool = ports[(ports["country"] == o.country) & ~ports["port_id"].isin(set(signal.related_port_ids))
                     & (ports["vessel_count_total"] >= 100)]
        near = nearby_ports(pool, o.lat, o.lon, 600, 1)
        if not near.empty:
            alt = {"port_id": near.iloc[0].port_id, "name": near.iloc[0].port_name, "distance_km": float(near.iloc[0].distance_km)}
    exposure = compute_exposure(signal, shipment, mc, traffic_known, aware_at=as_of, alternate_port=alt)
    payload = NemotronPayload(request_id=str(uuid.uuid4()), created_at=as_of, signal=signal, shipment=shipment,
                              match_context=mc, analysis=Analysis(exposure=exposure, traffic=traffic_known),
                              task=Task(allowed_actions=feasible_actions(exposure)))
    decision, model = rules_decision(payload), RULES_MODEL
    if client is not None:
        try:
            content = client.chat(SYSTEM_PROMPT, payload.model_dump_json(), schema=decision_schema(payload))[0]
            decision, model = parse_decision(content, payload), client.model
        except Exception as exc:  # keep the rules decision if the model fails
            log.warning("%s: Nemotron failed (%s); keeping the rules decision", sc["scenario"], str(exc).splitlines()[0])
    wait = next(o for o in exposure.options if o.action == "monitor")
    best = min((o for o in exposure.options if o.available), key=lambda o: o.cost_usd)
    series = daily[(daily["date"] >= (start.date() - timedelta(days=45)).isoformat())].copy()
    series["7-day average"] = series["value"].rolling(7, min_periods=1).mean().round(1)
    loc_name = _CHOKE[location_id].name if location_id in _CHOKE else ports.set_index("port_id").loc[location_id, "port_name"]
    summary = (f"{signal.title} started {start:%d %b %Y}. The example shipment ({shipment.bill_of_lading.port_of_loading.name} → "
               f"{shipment.bill_of_lading.port_of_discharge.name}) matches via {reason.replace('_', ' ')} ({loc_name}), "
               f"{mc.days_until_eta:g} days before its ETA. PortGuard would recommend **{decision.recommended_action.replace('_', ' ')}** "
               f"({decision.risk_level} risk): waiting would cost ~${wait.cost_usd:,.0f}, the best available option ~${best.cost_usd:,.0f}."
               + (f" Over the following month, measured {'transits' if location_id in _CHOKE else 'port calls'} at {loc_name} "
                  f"changed {change:+.0f}% vs the month before." if change is not None else ""))
    return {
        "scenario": sc["scenario"], "event": signal.title, "event_start": iso(start), "matched": True,
        "match_reason": reason, "location_id": location_id, "warning_days_before_eta": mc.days_until_eta,
        "traffic_location": loc_name, "traffic_metric": "transits" if location_id in _CHOKE else "port calls",
        "traffic_at_decision_pct": traffic_known.pct_change if traffic_known else None, "traffic_change_pct": change,
        "cheapest_action": exposure.cheapest_action, "force_majeure_covered": exposure.force_majeure_covered,
        "exposure_wait_usd": wait.cost_usd, "exposure_best_usd": best.cost_usd, "savings_usd": wait.cost_usd - best.cost_usd,
        "recommended_action": decision.recommended_action, "risk_level": decision.risk_level, "model": model,
        "rationale": decision.rationale, "summary": summary,
        "traffic_series": series.rename(columns={"value": "daily"})[["date", "daily", "7-day average"]].to_dict("records"),
    }


def main(argv: Optional[list[str]] = None) -> int:
    from nemotron import add_client_args, client_from_args
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=["rules", "nemotron"], default="rules", help="decision engine (default: rules)")
    add_client_args(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not SIGNALS_JSON.exists() or not PORTS_CSV.exists():
        log.error("run fetch_portwatch.py first (needs %s and %s)", SIGNALS_JSON.name, PORTS_CSV.name)
        return 1
    signals = {s["title"]: XtractSignal.model_validate(s) for s in json.loads(SIGNALS_JSON.read_text())}
    ports = pd.read_csv(PORTS_CSV, dtype={"port_id": str})
    client = client_from_args(args) if args.engine == "nemotron" else None
    results = []
    for sc in SCENARIOS:
        try:
            results.append(run_scenario(sc, signals, ports, client))
        except Exception as exc:  # one failing scenario (e.g. a network error) must not lose the others
            log.warning("%s failed: %s", sc["scenario"], exc)
            results.append({"scenario": sc["scenario"], "event": sc["event"], "matched": False, "summary": f"Failed: {exc}"})
    OUT_JSON.write_text(json.dumps({
        "generated_at": iso(utcnow()),
        "note": "Historical replay. Events and traffic are real (IMF PortWatch); each shipment is a synthetic example "
                "routed through the affected place. Decisions use only what was known the day after the event started.",
        "scenarios": results}, indent=2), encoding="utf-8")
    for r in results:
        if r["matched"]:
            log.info("%-44s warn %4.0fd  traffic next month %+5.0f%%  wait $%9s  best $%9s  -> %s",
                     r["scenario"], r["warning_days_before_eta"], r["traffic_change_pct"] or 0,
                     f"{r['exposure_wait_usd']:,.0f}", f"{r['exposure_best_usd']:,.0f}", r["recommended_action"])
        else:
            log.info("%-44s %s", r["scenario"], r["summary"])
    log.info("wrote %s", OUT_JSON)
    return 0 if all(r["matched"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
