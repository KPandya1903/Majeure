"""PortGuard AI dashboard: risk map, decision board with approvals and drafts, events, traffic,
backtest and Q&A. Run with:  streamlit run src/dashboard.py
Reads and writes data/portguard.db; pipeline buttons run the same scripts as the command line.
"""
from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
DB = ROOT / "data" / "portguard.db"
BACKTEST_JSON = ROOT / "data" / "backtest.json"
sys.path.insert(0, str(SRC))
from common import CHOKEPOINTS  # noqa: E402

RISK_COLORS = {"critical": [183, 28, 28], "high": [229, 57, 53], "medium": [251, 140, 0], "low": [124, 179, 66]}
NO_RISK = [66, 133, 244]
RISK_BADGE = {"critical": "red", "high": "red", "medium": "orange", "low": "green"}
ACTIONS = {"monitor": "Monitor", "notify_customer": "Notify customer", "reroute": "Reroute", "expedite": "Expedite",
           "hold_cargo": "Hold cargo", "invoke_force_majeure": "Invoke force majeure"}

st.set_page_config(page_title="PortGuard AI", page_icon="⚓", layout="wide")


# --------------------------------------------------------------------------- data access


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=20)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql(sql, conn, params=params)


def write(sql: str, params: tuple) -> None:
    with connect() as conn:
        conn.execute(sql, params)
    st.cache_data.clear()


def md(text) -> str:
    """Escape $ so Streamlit markdown doesn't render "$10,000 ... $0" as LaTeX."""
    return str(text or "").replace("$", "\\$")


def hours_until(ts: str | None) -> float | None:
    if not ts:
        return None
    due = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (due - datetime.now(timezone.utc)).total_seconds() / 3600


def latest_decisions() -> pd.DataFrame:
    """Most recent usable decision per (event or signal, shipment)."""
    df = q("""SELECT d.*, s.title, s.source_url, s.event_type, s.location_name FROM decision_logs d
              JOIN signals s USING (signal_id) WHERE d.recommended_action IS NOT NULL ORDER BY d.created_at DESC""")
    if df.empty:
        return df
    df["pair"] = df["event_id"].fillna(df["signal_id"]) + "|" + df["shipment_id"]
    return df.drop_duplicates("pair")


def run_script(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(SRC / args[0]), *args[1:]], cwd=ROOT, capture_output=True, text=True,
                          timeout=900)
    st.cache_data.clear()
    lines = [l.split(": ", 1)[-1] for l in (proc.stdout + proc.stderr).strip().splitlines()]
    return proc.returncode, "\n".join(lines[-12:])


# --------------------------------------------------------------------------- guard + sidebar

if not DB.exists():
    st.title("⚓ PortGuard AI")
    st.error("No database yet. Run the ingestion steps in README §1 (or `python src/run_pipeline.py`), then reload.")
    st.stop()

with st.sidebar:
    st.header("⚓ PortGuard AI")
    st.caption("Maritime disruptions → shipment-specific, contract-aware decisions by NVIDIA Nemotron.")
    fresh = q("SELECT source, COUNT(*) AS signals, MAX(ingested_at) AS last_ingested FROM signals GROUP BY source ORDER BY signals DESC")
    st.subheader("Data")
    st.dataframe(fresh, hide_index=True, width="stretch")
    st.subheader("Run pipeline steps")
    steps = {
        "Re-cluster events": ["cluster_events.py"],
        "Decide with Nemotron": ["run_nemotron.py"],
        "Decide with rules (no GPU)": ["run_nemotron.py", "--engine", "rules"],
        "Draft notices & emails": ["draft_actions.py"],
        "Send alerts": ["alerts.py"],
    }
    for label, args in steps.items():
        if st.button(label, width="stretch"):
            with st.spinner(f"{label}…"):
                code, out = run_script(args)
            (st.success if code == 0 else st.error)(f"{label}: exit {code}")
            st.code(out or "(no output)", language=None)

# --------------------------------------------------------------------------- header + KPIs

st.title("⚓ PortGuard AI")
st.caption("Real-time maritime disruption intelligence, linked to your shipments and contracts. "
           "Sources: IMF PortWatch · GDACS · GDELT · Google News · maritime trade press.")

decisions = latest_decisions()
events = q("SELECT * FROM events ORDER BY is_structured DESC, n_signals DESC")
shipments = q("SELECT * FROM shipments")
drafts = q("SELECT * FROM action_drafts")
pending = decisions[decisions["human_decision"] == "pending"] if not decisions.empty else decisions
at_risk = decisions[decisions["risk_level"].isin(["high", "critical"])]["shipment_id"].nunique() if not decisions.empty else 0
exposure = decisions[decisions["human_decision"] != "overridden"]["estimated_cost_usd"].fillna(0).sum() if not decisions.empty else 0
open_deadlines = [h for h in (hours_until(t) for t in drafts[drafts["status"] == "draft"]["deadline_at"].dropna()) if h is not None] if not drafts.empty else []

k = st.columns(5)
k[0].metric("Active events", len(events), help="Clusters of reports about one real-world event")
k[1].metric("Shipments at high risk", f"{at_risk} / {len(shipments)}")
k[2].metric("Decisions awaiting approval", len(pending))
k[3].metric("Estimated exposure", f"${exposure:,.0f}", help="Sum of the recommended option's cost across open decisions")
k[4].metric("Next notice deadline", f"{min(open_deadlines):.0f} h" if open_deadlines else "—",
            help="Force majeure notices still in draft")

tab_map, tab_ship, tab_events, tab_traffic, tab_backtest, tab_ask, tab_signals = st.tabs(
    ["Risk map", "Shipments & decisions", "Events", "Traffic", "Backtest", "Ask PortGuard", "All signals"])

# --------------------------------------------------------------------------- map

CHOKE = {c.location_id: c for c in CHOKEPOINTS}


def location_coords() -> dict[str, tuple[float, float, str]]:
    coords = {c.location_id: (c.lat, c.lon, c.name) for c in CHOKEPOINTS}
    for r in q("SELECT port_id, port_name, lat, lon FROM ports").itertuples():
        coords[r.port_id] = (r.lat, r.lon, r.port_name)
    return coords


def route_path(row, coords) -> list[list[float]]:
    """POL -> intermediate stops (nearest-first) -> POD, as [lon, lat] points."""
    stops = [p for p in json.loads(row.transshipment_port_ids) + json.loads(row.route_chokepoints) if p in coords]
    here, path = coords[row.pol_port_id], [coords[row.pol_port_id]]
    while stops:
        nxt = min(stops, key=lambda p: (coords[p][0] - here[0]) ** 2 + (coords[p][1] - here[1]) ** 2)
        stops.remove(nxt)
        path.append(coords[nxt])
        here = coords[nxt]
    path.append(coords[row.pod_port_id])
    return [[lon, lat] for lat, lon, _ in path]


with tab_map:
    coords = location_coords()
    risk_by_ship = {}
    if not decisions.empty:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for sid, g in decisions.groupby("shipment_id"):
            top = g.loc[g["risk_level"].map(order).idxmax()]
            risk_by_ship[sid] = (top["risk_level"], top["recommended_action"], top["title"])
    routes = pd.DataFrame([{
        "shipment": r.shipment_id, "path": route_path(r, coords),
        "label": f"{r.shipment_id}: {coords[r.pol_port_id][2]} → {coords[r.pod_port_id][2]}",
        "risk": risk_by_ship.get(r.shipment_id, ("none",))[0],
        "action": ACTIONS.get(risk_by_ship.get(r.shipment_id, (None, "—"))[1], "—"),
        "color": RISK_COLORS.get(risk_by_ship.get(r.shipment_id, ("none",))[0], NO_RISK),
    } for r in shipments.itertuples() if r.pol_port_id in coords and r.pod_port_id in coords])

    ev_points = q("""SELECT e.*, s.location_lat AS lat, s.location_lon AS lon FROM events e
                     JOIN signals s ON s.signal_id = e.anchor_signal_id WHERE s.location_lat IS NOT NULL""")
    if not ev_points.empty:
        ev_points["color"] = ev_points["severity"].map(lambda s: RISK_COLORS.get(s, [251, 140, 0]) + [180])
        ev_points["radius"] = ev_points["n_signals"].map(lambda n: 60000 + 45000 * math.log2(1 + n))
        ev_points["label"] = ev_points.apply(lambda r: f"{r['title']} · {r['n_signals']} reports · {r['event_type']}", axis=1)

    traffic = q("SELECT * FROM traffic_stats WHERE metric = 'transits'")
    choke_df = pd.DataFrame([{"lon": c.lon, "lat": c.lat, "name": c.name, "location_id": c.location_id} for c in CHOKEPOINTS])
    choke_df = choke_df.merge(traffic[["location_id", "pct_change", "avg_7d"]], on="location_id", how="left")
    choke_df["label"] = choke_df.apply(lambda r: f"{r['name']}" + (f" · transits {r['pct_change']:+.0f}% ({r['avg_7d']:g}/day)"
                                                                   if pd.notna(r["pct_change"]) else ""), axis=1)
    choke_df["color"] = choke_df["pct_change"].map(lambda p: [229, 57, 53, 220] if pd.notna(p) and p <= -30
                                                   else [120, 120, 120, 160])

    layers = [
        pdk.Layer("ScatterplotLayer", choke_df, get_position=["lon", "lat"], get_fill_color="color", get_radius=45000,
                  pickable=True, radius_min_pixels=3),
        pdk.Layer("PathLayer", routes, get_path="path", get_color="color", width_min_pixels=3, pickable=True,
                  joint_rounded=True, cap_rounded=True, wrap_longitude=True),  # Busan -> Long Beach crosses 180°
    ]
    if not ev_points.empty:
        layers.append(pdk.Layer("ScatterplotLayer", ev_points, get_position=["lon", "lat"], get_fill_color="color",
                                get_radius="radius", pickable=True, stroked=True, get_line_color=[255, 255, 255],
                                line_width_min_pixels=1, radius_min_pixels=6))
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=22, longitude=45, zoom=1.4),
                             tooltip={"text": "{label}\n{risk} {action}"}, map_style=None), height=560)
    st.caption("Circles: clustered events (size = number of reports, colour = severity). Lines: your shipments, "
               "coloured by their highest open risk. Small dots: chokepoints (red = transits down 30%+ vs 90 days).")

# --------------------------------------------------------------------------- shipments & decisions

with tab_ship:
    if shipments.empty:
        st.info("No shipments yet: run `python src/load_shipments.py`, or add one from a contract below.")
    for r in shipments.itertuples():
        coords_ = location_coords()
        d_ship = decisions[decisions["shipment_id"] == r.shipment_id] if not decisions.empty else decisions
        eta_days = (datetime.fromisoformat(r.eta.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
        top = d_ship["risk_level"].map({"low": 0, "medium": 1, "high": 2, "critical": 3}).max() if not d_ship.empty else None
        badge = ["green", "orange", "red", "red"][int(top)] if top is not None and not pd.isna(top) else "blue"
        with st.container(border=True):
            st.markdown(f"#### {r.shipment_id} · {coords_[r.pol_port_id][2]} → {coords_[r.pod_port_id][2]}  "
                        f":{badge}-background[{'no open risk' if d_ship.empty else str(d_ship['risk_level'].iloc[0]) + ' risk'}]")
            st.caption(f"B/L {r.bl_number} · {r.vessel_name or ''} {r.voyage_number or ''} · {r.cargo_description or ''} · "
                       f"{r.container_count or '?'} containers · ${r.cargo_value_usd or 0:,.0f} · {r.incoterm or ''} · "
                       f"ETA {r.eta[:10]} ({eta_days} days)")
            if d_ship.empty:
                st.write("No matching events right now.")
                continue
            for d in d_ship.itertuples():
                payload = json.loads(d.input_payload)
                exp = (payload.get("analysis") or {}).get("exposure") or {}
                corr = (payload.get("analysis") or {}).get("corroboration") or {}
                traffic = (payload.get("analysis") or {}).get("traffic") or {}
                with st.expander(f"{ACTIONS.get(d.recommended_action, d.recommended_action)} · {d.risk_level} "
                                 f"({d.risk_score:.2f}) · {d.title[:70]}", expanded=d.risk_level in ("high", "critical")):
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        st.markdown(f"**Recommendation:** :{RISK_BADGE.get(d.risk_level, 'blue')}-background["
                                    f"{ACTIONS.get(d.recommended_action, d.recommended_action)}]  "
                                    f"est. **\\${(d.estimated_cost_usd or 0):,.0f}** · model `{d.model}`")
                        st.markdown(md(d.rationale))
                        facts = []
                        if corr:
                            facts.append(f"**Corroboration:** {corr['n_reports']} reports from {corr['n_publishers']} "
                                         f"publishers (confidence {corr['confidence']:.0%})")
                        if traffic and traffic.get("pct_change") is not None:
                            facts.append(f"**Measured traffic** at {traffic['name']}: {traffic['avg_7d']:g}/day, "
                                         f"{traffic['pct_change']:+.0f}% vs previous 90 days")
                        mc = payload.get("match_context", {})
                        facts.append(f"**Why matched:** {mc.get('match_reason', '').replace('_', ' ')} "
                                     f"`{mc.get('matched_port_id')}`" + (f" · {mc['distance_km']:.0f} km away" if mc.get("distance_km") else ""))
                        st.markdown("  \n".join(facts))
                        st.markdown("**Sources:** " + " · ".join(f"[{i + 1}]({u})" for i, u in enumerate(json.loads(d.citations))))
                    with c2:
                        if exp.get("options"):
                            opts = pd.DataFrame([{"option": ACTIONS.get(o["action"], o["action"]),
                                                  "cost (USD)": o["cost_usd"] if o["available"] else None,
                                                  "delay (days)": o["expected_delay_days"],
                                                  "available": "yes" if o["available"] else "no"} for o in exp["options"]])
                            st.dataframe(opts, hide_index=True, width="stretch",
                                         column_config={"cost (USD)": st.column_config.NumberColumn(format="$%,.0f")})
                        due = hours_until(exp.get("force_majeure_notice_due"))
                        if exp.get("force_majeure_covered") is False:
                            st.info("Force majeure clause does **not** cover this event.")
                        elif due is not None:
                            (st.warning if due > 0 else st.error)(
                                f"Force majeure notice due {'in %.0f h' % due if due > 0 else '%.0f h ago' % -due} "
                                f"({exp['force_majeure_notice_due'][:16].replace('T', ' ')} UTC)")
                    st.divider()
                    h1, h2, h3 = st.columns([2, 1, 1])
                    notes = h1.text_input("Reviewer notes", value=d.human_notes or "", key=f"notes-{d.decision_id}")
                    st.caption(f"Human decision: **{d.human_decision}**")
                    if h2.button("Approve", key=f"ok-{d.decision_id}", type="primary", width="stretch"):
                        write("UPDATE decision_logs SET human_decision='approved', human_notes=? WHERE decision_id=?", (notes, d.decision_id))
                        st.rerun()
                    if h3.button("Override", key=f"no-{d.decision_id}", width="stretch"):
                        write("UPDATE decision_logs SET human_decision='overridden', human_notes=? WHERE decision_id=?", (notes, d.decision_id))
                        st.rerun()
                    for dr in drafts[drafts["decision_id"] == d.decision_id].itertuples() if not drafts.empty else []:
                        with st.popover(f"✉️ {dr.kind.replace('_', ' ').title()} ({dr.status})", width="stretch"):
                            subj = st.text_input("Subject", dr.subject, key=f"s-{dr.draft_id}")
                            body = st.text_area("Body", dr.body, height=360, key=f"b-{dr.draft_id}")
                            st.caption(f"Generated by {dr.generated_by}" + (f" · due {dr.deadline_at}" if dr.deadline_at else ""))
                            b1, b2, b3, b4 = st.columns(4)
                            for col, label, status in [(b1, "Save", dr.status), (b2, "Approve", "approved"),
                                                       (b3, "Mark sent", "sent"), (b4, "Discard", "discarded")]:
                                if col.button(label, key=f"{label}-{dr.draft_id}"):
                                    write("UPDATE action_drafts SET subject=?, body=?, status=? WHERE draft_id=?",
                                          (subj, body, status, dr.draft_id))
                                    st.rerun()
                            st.download_button("Download .txt", f"Subject: {subj}\n\n{body}", file_name=f"{dr.kind}-{r.shipment_id}.txt",
                                               key=f"dl-{dr.draft_id}")
                    with st.expander("Exact payload sent to the model"):
                        st.json(payload, expanded=False)

    st.subheader("Add a shipment from a contract")
    st.caption("Upload a Bill of Lading / sales contract (PDF or text). Nemotron extracts the route and contract terms.")
    up = st.file_uploader("Contract file", type=["pdf", "txt", "md"])
    if up and st.button("Extract with Nemotron"):
        tmp = ROOT / "data" / f"upload_{up.name}"
        tmp.write_bytes(up.getvalue())
        with st.spinner("Reading the contract…"):
            code, out = run_script(["extract_contract.py", "--file", str(tmp), "--load"])
        (st.success if code == 0 else st.error)(f"extract_contract: exit {code}")
        st.code(out, language=None)

# --------------------------------------------------------------------------- events

with tab_events:
    if events.empty:
        st.info("No events yet: run `python src/cluster_events.py`.")
    else:
        show = events[["title", "event_type", "severity", "location_name", "n_signals", "n_publishers", "confidence",
                       "is_structured", "last_seen", "event_id"]].rename(columns={
            "n_signals": "reports", "n_publishers": "publishers", "is_structured": "PortWatch/GDACS"})
        st.dataframe(show.drop(columns="event_id"), hide_index=True, width="stretch",
                     column_config={"confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        pick = st.selectbox("Event details", events["event_id"], format_func=lambda e: events.set_index("event_id").loc[e, "title"][:90])
        members = q("""SELECT s.published_at, s.source, s.title, s.source_url FROM signals s JOIN event_signals es USING (signal_id)
                       WHERE es.event_id = ? ORDER BY s.published_at DESC""", (pick,))
        st.dataframe(members, hide_index=True, width="stretch", column_config={"source_url": st.column_config.LinkColumn("link")})

# --------------------------------------------------------------------------- traffic

with tab_traffic:
    stats = q("SELECT * FROM traffic_stats ORDER BY pct_change")
    if stats.empty:
        st.info("No traffic data yet: run `python src/fetch_traffic.py` then `python src/init_db.py`.")
    else:
        c1, c2 = st.columns([2, 3])
        c1.dataframe(stats[["name", "avg_7d", "baseline_avg", "pct_change"]], hide_index=True, width="stretch", height=420,
                     column_config={"avg_7d": st.column_config.NumberColumn("last 7 days", format="%.1f/day"),
                                    "baseline_avg": st.column_config.NumberColumn("prior 90 days", format="%.1f/day"),
                                    "pct_change": st.column_config.NumberColumn("change", format="%+.0f%%")})
        loc = c2.selectbox("Location", stats["location_id"], format_func=lambda l: stats.set_index("location_id").loc[l, "name"])
        series = q("SELECT date, value FROM traffic_daily WHERE location_id = ? ORDER BY date", (loc,))
        if not series.empty:
            series["7-day average"] = series["value"].rolling(7, min_periods=1).mean()
            c2.line_chart(series.set_index("date")[["value", "7-day average"]], height=320)
            c2.caption("Daily ship transits (chokepoints) or port calls (ports), IMF PortWatch. Data lags ~5 days.")

# --------------------------------------------------------------------------- backtest

with tab_backtest:
    if not BACKTEST_JSON.exists():
        st.info("Run `python src/backtest.py` to replay historical disruptions (Red Sea 2023, Baltimore 2024, "
                "US ports strike 2024, Panama drought 2023, Hormuz 2026).")
    else:
        report = json.loads(BACKTEST_JSON.read_text())
        st.caption(report.get("note", ""))
        rows = pd.DataFrame(report["scenarios"])
        st.dataframe(rows[["scenario", "event", "matched", "match_reason", "warning_days_before_eta", "traffic_change_pct",
                           "cheapest_action", "exposure_wait_usd", "exposure_best_usd", "recommended_action"]],
                     hide_index=True, width="stretch",
                     column_config={"traffic_change_pct": st.column_config.NumberColumn("measured traffic change", format="%+.0f%%"),
                                    "exposure_wait_usd": st.column_config.NumberColumn("cost if waiting", format="$%,.0f"),
                                    "exposure_best_usd": st.column_config.NumberColumn("cost of best option", format="$%,.0f")})
        pick = st.selectbox("Scenario", rows["scenario"])
        sc = next(s for s in report["scenarios"] if s["scenario"] == pick)
        st.markdown(md(sc.get("summary", "")))
        if sc.get("traffic_series"):
            ts = pd.DataFrame(sc["traffic_series"]).set_index("date")
            st.line_chart(ts, height=300)
            st.caption(f"{sc['traffic_location']}: daily {sc['traffic_metric']} around the event start "
                       f"({sc['event_start'][:10]}), 7-day average.")

# --------------------------------------------------------------------------- ask

with tab_ask:
    st.caption("Ask about your shipments, events and decisions. Nemotron answers only from PortGuard's data and cites sources.")
    st.session_state.setdefault("chat", [])
    for role, text in st.session_state["chat"]:
        st.chat_message(role).markdown(text)
    if question := st.chat_input("e.g. Which shipments go through Hormuz, and can we invoke force majeure?"):
        st.chat_message("user").markdown(question)
        with st.chat_message("assistant"), st.spinner("Asking Nemotron…"):
            try:
                from ask import answer
                reply = answer(question)
            except Exception as exc:  # the demo must not crash if the model is down
                reply = f"Nemotron is not reachable ({type(exc).__name__}). Check NEMOTRON_BASE_URL in .env."
            st.markdown(md(reply))
        st.session_state["chat"] += [("user", question), ("assistant", md(reply))]

# --------------------------------------------------------------------------- signals

with tab_signals:
    sig = q("SELECT published_at, source, event_type, severity, location_name, title, source_url FROM signals ORDER BY published_at DESC")
    f1, f2, f3 = st.columns([2, 2, 3])
    srcs = f1.multiselect("Source", sorted(sig["source"].unique()))
    types = f2.multiselect("Event type", sorted(sig["event_type"].unique()))
    text = f3.text_input("Search titles")
    mask = pd.Series(True, index=sig.index)
    if srcs:
        mask &= sig["source"].isin(srcs)
    if types:
        mask &= sig["event_type"].isin(types)
    if text:
        mask &= sig["title"].str.contains(text, case=False, na=False)
    view = sig[mask]
    st.dataframe(view, hide_index=True, width="stretch", height=520,
                 column_config={"source_url": st.column_config.LinkColumn("link")})
    st.caption(f"{len(view)} of {len(sig)} signals")
