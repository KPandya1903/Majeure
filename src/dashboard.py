"""PortGuard AI dashboard: risk map, decision board with approvals and drafts, events, traffic,
backtest and Q&A. Run with:  streamlit run src/dashboard.py
Reads and writes data/portguard.db; pipeline buttons run the same scripts as the command line.
"""
from __future__ import annotations

import html
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
import theme  # noqa: E402

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
ACTIONS = {"monitor": "Monitor", "notify_customer": "Notify customer", "reroute": "Reroute", "expedite": "Expedite",
           "hold_cargo": "Hold cargo", "invoke_force_majeure": "Invoke force majeure"}

st.set_page_config(page_title="PortGuard AI", page_icon="⚓", layout="wide")
st.markdown(theme.CSS, unsafe_allow_html=True)


def hex_to_rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


SEVERITY_RGB = {lvl: hex_to_rgb(hexcode) for lvl, hexcode in theme.RISK_HEX.items()}


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


def esc(text) -> str:
    """Escape untrusted text (titles, notes) before it goes into an HTML block."""
    return html.escape(str(text or ""), quote=True)


def trunc(text: str, n: int) -> str:
    """Truncate at the last word boundary before `n` chars, so a headline never ends mid-word."""
    text = str(text or "")
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return (cut or text[:n]) + "…"


def hours_until(ts: str | None) -> float | None:
    if not ts:
        return None
    due = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (due - datetime.now(timezone.utc)).total_seconds() / 3600


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(dt: datetime, with_year: bool = True) -> str:
    """English `D Mon[ YYYY]`, independent of the machine's locale (strftime's %b/%a are
    locale-dependent and can silently render e.g. French month names)."""
    out = f"{dt.day} {_MONTHS[dt.month - 1]}"
    return f"{out} {dt.year}" if with_year else out


def format_countdown(hrs: float | None) -> str:
    if hrs is None:
        return "—"
    if hrs < 0:
        return "Overdue"
    h, m = int(hrs), int(round((hrs - int(hrs)) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h {m:02d}m"


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


# --------------------------------------------------------------------------- small HTML builders


def kpi_card(label: str, value: str, note: str = "", *, due: bool = False, note_tone: str = "") -> str:
    cls = "pg-card" + (" pg-kpi-due" if due else "")
    note_style = f' style="color:{theme.RED};font-weight:600;"' if note_tone == "risk" else ""
    return (f'<div class="{cls}"><div class="pg-kpi-label">{esc(label)}</div>'
            f'<div class="pg-kpi-value pg-num">{value}</div>'
            f'<div class="pg-kpi-note"{note_style}>{esc(note)}</div></div>')


def chip(text: str, tone: str = "mute") -> str:
    return theme.chip(esc(text), tone)


# --------------------------------------------------------------------------- guard + sidebar

if not DB.exists():
    st.title("⚓ PortGuard AI")
    st.error("No database yet. Run the ingestion steps in README §1 (or `python src/run_pipeline.py`), then reload.")
    st.stop()


def data_stamp() -> tuple[str, str]:
    """(fingerprint of the data, last fetch time); uncached so the watcher sees new pipeline runs."""
    with connect() as conn:
        row = conn.execute("""SELECT (SELECT MAX(ingested_at) FROM signals), (SELECT COUNT(*) FROM signals),
                                     (SELECT COUNT(*) FROM decision_logs), (SELECT COUNT(*) FROM action_drafts),
                                     (SELECT MAX(updated_at) FROM events)""").fetchone()
    return "|".join(map(str, row)), row[0] or ""


@st.fragment(run_every="60s")
def freshness_watcher() -> None:
    """Shows data age; when a pipeline run changed the data, reruns the whole app once."""
    stamp, last = data_stamp()
    if last:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 60
        st.caption(f"🟢 Data fetched {age:.0f} min ago" if age < 15 else f"🟠 Data fetched {age:.0f} min ago "
                   "(is `run_pipeline.py --loop 5` running?)")
    previous = st.session_state.get("data_stamp")
    st.session_state["data_stamp"] = stamp
    if previous is not None and previous != stamp:
        st.cache_data.clear()
        st.rerun(scope="app")


with st.sidebar:
    st.markdown('<div class="pg-brand"><span class="pg-brand-name">PortGuard</span>'
                '<span class="pg-brand-tag">AI</span></div>', unsafe_allow_html=True)
    st.caption("Maritime disruptions → shipment-specific, contract-aware decisions by NVIDIA Nemotron.")
    freshness_watcher()
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

# --------------------------------------------------------------------------- data for header + KPIs

decisions = latest_decisions()
events = q("SELECT * FROM events ORDER BY is_structured DESC, n_signals DESC")
shipments = q("SELECT * FROM shipments")
drafts = q("SELECT * FROM action_drafts")
pending = decisions[decisions["human_decision"] == "pending"] if not decisions.empty else decisions
at_risk_ships = decisions[decisions["risk_level"].isin(["high", "critical"])]["shipment_id"].unique() if not decisions.empty else []
n_critical = decisions[decisions["risk_level"] == "critical"]["shipment_id"].nunique() if not decisions.empty else 0
n_high = decisions[decisions["risk_level"] == "high"]["shipment_id"].nunique() if not decisions.empty else 0
exposure = decisions[decisions["human_decision"] != "overridden"]["estimated_cost_usd"].fillna(0).sum() if not decisions.empty else 0
open_deadlines = [h for h in (hours_until(t) for t in drafts[drafts["status"] == "draft"]["deadline_at"].dropna()) if h is not None] if not drafts.empty else []

# --------------------------------------------------------------------------- alert strip + brand header

_severity_events = events[events["severity"].isin(RISK_ORDER)] if not events.empty else events
if not _severity_events.empty and not st.session_state.get("alert_dismissed"):
    top_ev = _severity_events.assign(_r=_severity_events["severity"].map(RISK_ORDER)).sort_values(
        ["_r", "n_signals"], ascending=False).iloc[0]
    traffic_note = ""
    tr = q("SELECT pct_change, avg_7d FROM traffic_stats WHERE location_id = ? AND metric = 'transits'",
           (top_ev["location_id"],))
    if not tr.empty and pd.notna(tr.iloc[0]["pct_change"]):
        traffic_note = f" · transits down {abs(tr.iloc[0]['pct_change']):.0f}% against pre-crisis levels"
    strip_col, x_col = st.columns([40, 1])
    with strip_col:
        st.markdown(
            f'<div class="pg-strip">⚠ <strong>{esc(top_ev["title"])}</strong>{esc(traffic_note)} · '
            f'{top_ev["n_signals"]} reports from {top_ev["n_publishers"]} publishers</div>',
            unsafe_allow_html=True)
    with x_col:
        if st.button("", icon=":material/close:", key="dismiss_alert", help="Dismiss for this session"):
            st.session_state["alert_dismissed"] = True
            st.rerun()

_, last_fetch = data_stamp()
fresh_note = ""
if last_fetch:
    age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))).total_seconds() / 60
    dot = theme.GREEN if age_min < 15 else theme.AMBER
    fresh_note = (f'<span style="display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:{theme.MUTED};">'
                  f'<span style="width:7px;height:7px;border-radius:50%;background:{dot};display:inline-block;"></span>'
                  f'Updated {age_min:.0f} min ago</span>')

st.markdown(
    f'<div style="display:flex;align-items:center;gap:14px;padding:6px 0 10px;">'
    f'<div class="pg-brand"><span class="pg-brand-name">PortGuard</span><span class="pg-brand-tag">AI</span></div>'
    f'<span class="pg-sub">Contract-grade disruption decisions — IMF PortWatch · GDACS · GDELT · '
    f'Google News · maritime trade press.</span><span style="flex-grow:1;"></span>{fresh_note}</div>',
    unsafe_allow_html=True)

kpis = [
    kpi_card("Active events", str(len(events)),
             f"{int(events['is_structured'].sum())} anchored on PortWatch / GDACS" if not events.empty else "No events yet"),
    kpi_card("Shipments at risk", f"{len(at_risk_ships)} / {len(shipments)}",
             f"{n_critical} critical · {n_high} high" if len(at_risk_ships) else "No shipments at elevated risk",
             note_tone="risk" if len(at_risk_ships) else ""),
    kpi_card("Decisions awaiting approval", str(len(pending)), "reviewed in Shipments & decisions"),
    kpi_card("Open exposure", f"${exposure:,.0f}", "if every recommendation is taken"),
    kpi_card("Force majeure notice due", format_countdown(min(open_deadlines)) if open_deadlines else "—",
             "earliest of any drafted notice" if open_deadlines else "No notices pending",
             due=bool(open_deadlines)),
]
st.markdown(f'<div class="pg-kpis" style="grid-template-columns:repeat(5, minmax(0,1fr));">{"".join(kpis)}</div>',
            unsafe_allow_html=True)

tab_map, tab_ship, tab_events, tab_traffic, tab_backtest, tab_ask, tab_signals = st.tabs(
    ["Risk map", "Shipments & decisions", "Events", "Traffic", "Backtest", "Ask PortGuard", "All signals"])

# --------------------------------------------------------------------------- map

CHOKE = {c.location_id: c for c in CHOKEPOINTS}


def location_coords() -> dict[str, tuple[float, float, str, str]]:
    """port_id/chokepoint_id -> (lat, lon, name, locode-without-space)."""
    coords = {c.location_id: (c.lat, c.lon, c.name, "") for c in CHOKEPOINTS}
    for r in q("SELECT port_id, port_name, lat, lon, locode FROM ports").itertuples():
        locode = r.locode if isinstance(r.locode, str) else ""
        coords[r.port_id] = (r.lat, r.lon, r.port_name, locode.replace(" ", ""))
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
    return [[lon, lat] for lat, lon, _, _ in path]


def unwrap_lons(path: list[list[float]]) -> list[list[float]]:
    """Keep longitude continuous across the antimeridian (e.g. Busan -> Long Beach)."""
    out = [list(path[0])]
    for lon, lat in path[1:]:
        prev = out[-1][0]
        while lon - prev > 180:
            lon -= 360
        while lon - prev < -180:
            lon += 360
        out.append([lon, lat])
    return out


def route_progress(row) -> float:
    """Fraction of the voyage elapsed, from ETD/ETA, clamped to [0, 1]."""
    try:
        etd = datetime.fromisoformat(row.etd.replace("Z", "+00:00"))
        eta = datetime.fromisoformat(row.eta.replace("Z", "+00:00"))
        if eta <= etd:
            return 0.0
        now = datetime.now(timezone.utc)
        return min(1.0, max(0.0, (now - etd).total_seconds() / (eta - etd).total_seconds()))
    except Exception:
        return 0.0


def split_path(path: list[list[float]], frac: float) -> tuple[list[list[float]], list[list[float]]]:
    """(sailed, remaining) split of `path` at fraction `frac` of its total length."""
    if len(path) < 2:
        return list(path), list(path)
    seg_lens = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg_lens)
    if total == 0 or frac <= 0:
        return [path[0]], list(path)
    if frac >= 1:
        return list(path), [path[-1]]
    target, acc = frac * total, 0.0
    for i, seg in enumerate(seg_lens):
        if acc + seg >= target:
            t = (target - acc) / seg if seg else 0.0
            a, b = path[i], path[i + 1]
            pt = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            return path[:i + 1] + [pt], [pt] + path[i + 1:]
        acc += seg
    return list(path), [path[-1]]


def dotted_points(path: list[list[float]], n: int = 10) -> list[list[float]]:
    """`n` evenly spaced points along `path`, used to draw a route as a dashed/dotted trail.

    Routes here are straight segments between POL/TSP/POD/chokepoint coordinates, not real
    sailing lanes, so on a real basemap they can visibly cut across land. Dashing them (rather
    than drawing a solid line) reads as a schematic connector, not an asserted precise course.
    """
    if len(path) < 2:
        return []
    seg_lens = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg_lens)
    if total == 0:
        return []
    pts, acc, seg_i = [], 0.0, 0
    for k in range(n + 1):
        target = total * k / n
        while seg_i < len(seg_lens) - 1 and acc + seg_lens[seg_i] < target:
            acc += seg_lens[seg_i]
            seg_i += 1
        seg = seg_lens[seg_i]
        t = (target - acc) / seg if seg else 0.0
        a, b = path[seg_i], path[seg_i + 1]
        pts.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    return pts


def dot_count(path: list[list[float]], per_unit: float = 1.1, lo: int = 5, hi: int = 40) -> int:
    """Dash density scaled to a leg's length, so short hops don't get one giant dash."""
    if len(path) < 2:
        return lo
    length = sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))
    return max(lo, min(hi, round(length * per_unit)))


with tab_map:
    coords = location_coords()
    risk_by_ship: dict[str, tuple[str, str, str]] = {}
    if not decisions.empty:
        for sid, g in decisions.groupby("shipment_id"):
            top = g.loc[g["risk_level"].map(RISK_ORDER).idxmax()]
            risk_by_ship[sid] = (top["risk_level"], top["recommended_action"], top["title"])

    sailed_dot_rows, remaining_dot_rows, port_ids_used = [], [], set()
    for r in shipments.itertuples():
        if r.pol_port_id not in coords or r.pod_port_id not in coords:
            continue
        path = unwrap_lons(route_path(r, coords))
        sailed, remaining = split_path(path, route_progress(r))
        top = risk_by_ship.get(r.shipment_id)
        label = f"{r.shipment_id}: {coords[r.pol_port_id][2]} → {coords[r.pod_port_id][2]}"
        if top:
            label += f" · {top[0]} risk · {ACTIONS.get(top[1], '—')}"
        for pt in dotted_points(sailed, n=dot_count(sailed)):
            sailed_dot_rows.append({"position": pt, "label": label})
        for pt in dotted_points(remaining, n=dot_count(remaining)):
            remaining_dot_rows.append({"position": pt, "label": label})
        port_ids_used.update(p for p in [r.pol_port_id, r.pod_port_id, *json.loads(r.transshipment_port_ids)]
                             if p in coords)
    sailed_dots = pd.DataFrame(sailed_dot_rows)
    remaining_dots = pd.DataFrame(remaining_dot_rows)
    port_points = pd.DataFrame([{"lon": coords[p][1], "lat": coords[p][0], "name": coords[p][2],
                                 "locode": coords[p][3]} for p in port_ids_used])

    ev_points = q("""SELECT e.*, s.location_lat AS lat, s.location_lon AS lon FROM events e
                     JOIN signals s ON s.signal_id = e.anchor_signal_id WHERE s.location_lat IS NOT NULL""")
    traffic = q("SELECT * FROM traffic_stats WHERE metric = 'transits'")
    if not ev_points.empty:
        ev_points = ev_points.merge(traffic[["location_id", "pct_change", "avg_7d"]], on="location_id", how="left")
        ev_points["rgb"] = ev_points["severity"].map(SEVERITY_RGB).fillna(pd.Series([hex_to_rgb(theme.AMBER)] * len(ev_points)))
        ev_points["halo_color"] = ev_points["rgb"].map(lambda c: c + [46])
        ev_points["dot_color"] = ev_points["rgb"].map(lambda c: c + [235])
        ev_points["radius"] = ev_points["n_signals"].map(lambda n: 55000 + 42000 * math.log2(1 + n))
        ev_points["label"] = ev_points.apply(
            lambda r: f"{r['title']} · {str(r['severity']).upper()} · {r['n_signals']} reports"
                      + (f" · transits {r['pct_change']:+.0f}%" if pd.notna(r["pct_change"]) else ""), axis=1)

    choke_df = pd.DataFrame([{"lon": c.lon, "lat": c.lat, "name": c.name, "location_id": c.location_id} for c in CHOKEPOINTS])
    choke_df = choke_df.merge(traffic[["location_id", "pct_change", "avg_7d"]], on="location_id", how="left")
    choke_df["label"] = choke_df.apply(
        lambda r: r["name"] + (f" · transits {r['pct_change']:+.0f}% vs 90-day avg ({r['avg_7d']:g}/day)"
                               if pd.notna(r["pct_change"]) else " · no recent traffic data"), axis=1)

    layers = [
        pdk.Layer("ScatterplotLayer", choke_df, get_position=["lon", "lat"],
                  get_fill_color=hex_to_rgb(theme.MUTED) + [160], get_radius=32000, pickable=True,
                  radius_min_pixels=3),
    ]
    if not port_points.empty:
        layers.append(pdk.Layer("ScatterplotLayer", port_points, get_position=["lon", "lat"],
                                get_fill_color=hex_to_rgb(theme.TEXT) + [210], get_radius=1, radius_min_pixels=4,
                                stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=2, pickable=True))
        layers.append(pdk.Layer("TextLayer", port_points, get_position=["lon", "lat"], get_text="locode",
                                get_size=11, get_color=hex_to_rgb(theme.MUTED), get_pixel_offset=[10, 0]))
    if not ev_points.empty:
        layers.append(pdk.Layer("ScatterplotLayer", ev_points, get_position=["lon", "lat"],
                                get_fill_color="halo_color", get_radius="radius", pickable=False,
                                radius_min_pixels=8))
        layers.append(pdk.Layer("ScatterplotLayer", ev_points, get_position=["lon", "lat"],
                                get_fill_color="dot_color", get_radius=9000, radius_min_pixels=6, pickable=True,
                                stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=2))
    # Routes are straight segments between real port/chokepoint coordinates, not sailing lanes,
    # so on a real basemap a solid line can visibly cut across land. Dashed dot-trails read as a
    # schematic connector rather than an asserted precise course; sailed = denser + solid blue,
    # remaining = sparser + lighter, so progress is still legible.
    if not sailed_dots.empty:
        layers.append(pdk.Layer("ScatterplotLayer", sailed_dots, get_position="position",
                                get_fill_color=theme.RGB["blue"] + [225], get_radius=1, radius_min_pixels=1.8,
                                pickable=True))
    if not remaining_dots.empty:
        layers.append(pdk.Layer("ScatterplotLayer", remaining_dots, get_position="position",
                                get_fill_color=theme.RGB["blue"] + [110], get_radius=1, radius_min_pixels=1.3,
                                pickable=True))

    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=22, longitude=45, zoom=1.4),
                             tooltip={"text": "{label}\n{name}"}, map_provider="carto",
                             map_style="https://basemaps.cartocdn.com/gl/voyager-nolabels-gl-style/style.json"),
                    height=560)
    legend = [("Sailed", None, "dot-dense"), ("Remaining", None, "dot-sparse"), ("Critical", theme.RED, "dot"),
              ("High / medium", theme.AMBER, "dot"), ("Low", theme.GREEN, "dot"), ("Port", theme.TEXT, "dot")]

    def legend_item(label, color, kind):
        if kind == "dot-dense":
            mark = (f'<span style="width:20px;height:3px;background:repeating-linear-gradient(90deg,{theme.BLUE} 0 3px,'
                    f'transparent 3px 6px);display:inline-block;"></span>')
        elif kind == "dot-sparse":
            mark = (f'<span style="width:20px;height:3px;background:repeating-linear-gradient(90deg,{theme.BLUE}88 0 2px,'
                    f'transparent 2px 8px);display:inline-block;"></span>')
        else:
            mark = f'<span style="width:10px;height:10px;border-radius:50%;background:{color};display:inline-block;"></span>'
        return f'<span style="display:flex;align-items:center;gap:6px;">{mark}{esc(label)}</span>'

    st.markdown('<div class="pg-legend">' + "".join(legend_item(*i) for i in legend) + '</div>',
                unsafe_allow_html=True)
    st.caption("Halos: clustered events (size = number of reports, colour = severity). Blue dashes: your shipments' "
               "routes — illustrative straight-line connectors, denser where sailed, sparser where still ahead. "
               "Grey dots: ports and chokepoints.")

# --------------------------------------------------------------------------- shipments & decisions


def fleet_table_html(rows: list[dict]) -> str:
    head = ('<tr><th>Shipment · B/L</th><th>Path</th><th>Contract ETA</th><th>PortGuard ETA</th>'
            '<th>Status &amp; recommendation</th></tr>')
    body = []
    for r in rows:
        body.append(
            f'<tr><td><div style="font-weight:600;">{esc(r["shipment_id"])}</div>'
            f'<div class="pg-mono">{esc(r["bl_number"])}</div></td>'
            f'<td><div>{esc(r["path"])}</div><div style="font-size:11.5px;color:{theme.FAINT};">{esc(r["stops"])}</div></td>'
            f'<td class="pg-num">{esc(r["contract_eta"])}</td>'
            f'<td class="pg-num">{esc(r["pg_eta"])} {r["delay_chip"]}</td>'
            f'<td>{r["status_chip"]}<span style="margin:0 9px;">{esc(r["recommendation"])}</span>{r["extra_chip"]}</td></tr>')
    return f'<table class="pg-table">{head}{"".join(body)}</table>'


with tab_ship:
    if shipments.empty:
        st.markdown('<div class="pg-card pg-empty">No shipments yet — run <code>python src/load_shipments.py</code>, '
                    'or add one from a contract below.</div>', unsafe_allow_html=True)
    else:
        coords_ = location_coords()
        rows = []
        for r in shipments.itertuples():
            if r.pol_port_id not in coords_ or r.pod_port_id not in coords_:
                continue
            d_ship = decisions[decisions["shipment_id"] == r.shipment_id] if not decisions.empty else decisions
            stops = json.loads(r.transshipment_port_ids)
            stop_label = "Direct" if not stops else f"{len(stops)} TSP · " + ", ".join(coords_[s][2] for s in stops if s in coords_)
            pol_lc = coords_[r.pol_port_id][3] or coords_[r.pol_port_id][2]
            pod_lc = coords_[r.pod_port_id][3] or coords_[r.pod_port_id][2]
            contract_eta = datetime.fromisoformat(r.eta.replace("Z", "+00:00"))
            if d_ship.empty:
                rows.append({"shipment_id": r.shipment_id, "bl_number": r.bl_number, "path": f"{pol_lc} → {pod_lc}",
                            "stops": stop_label, "contract_eta": fmt_date(contract_eta),
                            "pg_eta": fmt_date(contract_eta, with_year=False), "delay_chip": chip("On time", "ok"),
                            "status_chip": chip("CLEAR", "ok"), "recommendation": "No event touches this route",
                            "extra_chip": "", "_risk": -1, "shipment_row": r})
                continue
            top = d_ship.loc[d_ship["risk_level"].map(RISK_ORDER).idxmax()]
            payload = json.loads(top["input_payload"])
            opts = ((payload.get("analysis") or {}).get("exposure") or {}).get("options") or []
            match = next((o for o in opts if o["action"] == top["recommended_action"]), None)
            delay_days = round(match["expected_delay_days"]) if match and match.get("expected_delay_days") else 0
            pg_eta = contract_eta + pd.Timedelta(days=delay_days)
            delay_chip = chip("On time", "ok") if delay_days <= 0 else chip(f"+{delay_days}d", "warn")
            extra = ""
            due = hours_until((payload.get("analysis", {}).get("exposure") or {}).get("force_majeure_notice_due"))
            if top["recommended_action"] == "invoke_force_majeure" and due is not None:
                extra = chip(f"Notice {format_countdown(due)}", "warn" if due > 0 else "risk")
            elif top["estimated_cost_usd"]:
                extra = chip(f"${top['estimated_cost_usd']:,.0f}", "risk" if top["risk_level"] == "critical" else "warn")
            rows.append({"shipment_id": r.shipment_id, "bl_number": r.bl_number, "path": f"{pol_lc} → {pod_lc}",
                        "stops": stop_label, "contract_eta": fmt_date(contract_eta),
                        "pg_eta": fmt_date(pg_eta, with_year=False), "delay_chip": delay_chip,
                        "status_chip": chip(top["risk_level"].upper(), theme.RISK_TONE.get(top["risk_level"], "mute")),
                        "recommendation": f"{ACTIONS.get(top['recommended_action'], top['recommended_action'])} "
                                          f"— ${(top['estimated_cost_usd'] or 0):,.0f}",
                        "extra_chip": extra, "_risk": RISK_ORDER.get(top["risk_level"], 0), "shipment_row": r})
        rows.sort(key=lambda x: -x["_risk"])
        st.markdown(fleet_table_html(rows), unsafe_allow_html=True)

    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

    if not decisions.empty:
        for r in sorted((r for r in shipments.itertuples() if r.pol_port_id in location_coords()
                         and r.pod_port_id in location_coords()),
                        key=lambda r: -(decisions[decisions["shipment_id"] == r.shipment_id]["risk_level"]
                                       .map(RISK_ORDER).max() if (decisions["shipment_id"] == r.shipment_id).any() else -1)):
            coords_ = location_coords()
            d_ship = decisions[decisions["shipment_id"] == r.shipment_id]
            if d_ship.empty:
                continue
            for d in d_ship.itertuples():
                payload = json.loads(d.input_payload)
                exp = (payload.get("analysis") or {}).get("exposure") or {}
                corr = (payload.get("analysis") or {}).get("corroboration") or {}
                traffic_d = (payload.get("analysis") or {}).get("traffic") or {}
                mc = payload.get("match_context", {})
                with st.expander(f"{r.shipment_id} · {coords_[r.pol_port_id][2]} → {coords_[r.pod_port_id][2]} — "
                                 f"{ACTIONS.get(d.recommended_action, d.recommended_action)} · {d.risk_level} "
                                 f"({d.risk_score:.2f}) · {d.title[:60]}",
                                 expanded=d.risk_level in ("high", "critical")):
                    eyebrow = f"{coords_[r.pol_port_id][2]} → {coords_[r.pod_port_id][2]} · {r.bl_number}"
                    st.markdown(
                        f'<div class="pg-eyebrow">{esc(eyebrow)}</div>'
                        f'<div class="pg-h">{esc(ACTIONS.get(d.recommended_action, d.recommended_action))}</div>',
                        unsafe_allow_html=True)

                    due = hours_until(exp.get("force_majeure_notice_due"))
                    if exp.get("force_majeure_covered") is False:
                        st.markdown('<div class="pg-card" style="margin-top:10px;color:' + theme.MUTED +
                                   ';font-size:0.87rem;">Force majeure clause does not cover this event.</div>',
                                   unsafe_allow_html=True)
                    elif due is not None:
                        banner_cls = "pg-banner" if due > 0 else "pg-banner late"
                        when = (exp.get("force_majeure_notice_due") or "")[:16].replace("T", " ")
                        wording = f"Written force majeure notice due in <strong>{format_countdown(due)}</strong> — by {when} UTC" \
                            if due > 0 else f"Written force majeure notice was due <strong>{format_countdown(due)}</strong> ago — by {when} UTC"
                        st.markdown(f'<div class="{banner_cls}" style="margin-top:10px;">⏱ {wording}</div>',
                                   unsafe_allow_html=True)

                    c1, c2 = st.columns([1.05, 1])
                    with c1:
                        st.markdown('<div class="pg-card" style="margin-top:14px;">'
                                   '<div class="pg-eyebrow">What each option costs</div>', unsafe_allow_html=True)
                        available = [o for o in (exp.get("options") or []) if o["available"]]
                        max_cost = max((o["cost_usd"] or 0) for o in available) if available else 0
                        for o in available:
                            is_best = o["action"] == d.recommended_action
                            is_worst = (not is_best and max_cost > 0 and o["cost_usd"] == max_cost
                                       and o["cost_usd"] > 0)
                            bar_cls = "pg-bar is-best" if is_best else ("pg-bar is-risk" if is_worst else "pg-bar")
                            width = max(3, round(100 * (o["cost_usd"] or 0) / max_cost)) if max_cost else 3
                            name = ACTIONS.get(o["action"], o["action"]) + (" · recommended" if is_best else "")
                            wrap_open, wrap_close = (f'<div class="pg-best">', '</div>') if is_best else ('<div class="pg-option">', '</div>')
                            st.markdown(
                                f'{wrap_open}<div class="pg-option-head"><span style="font-weight:{"600" if is_best else "400"};'
                                f'color:{theme.BLUE if is_best else theme.TEXT};">{esc(name)}</span>'
                                f'<span class="pg-num" style="font-weight:600;color:{theme.BLUE if is_best else (theme.RED if is_worst else theme.TEXT)};">'
                                f'${o["cost_usd"]:,.0f}</span></div>'
                                f'<div class="{bar_cls}"><span style="width:{width}%;"></span></div>'
                                f'<div class="pg-option-note">{esc(o.get("note", ""))}</div>{wrap_close}',
                                unsafe_allow_html=True)
                        assumptions = ((payload.get("analysis") or {}).get("exposure") or {}).get("assumptions") or []
                        if assumptions:
                            st.markdown(f'<div style="font-size:11.5px;color:{theme.FAINT};margin-top:12px;">'
                                       f'{esc(assumptions[-1])}</div>', unsafe_allow_html=True)
                        st.markdown('</div>', unsafe_allow_html=True)

                    with c2:
                        st.markdown('<div class="pg-card">'
                                   f'<div class="pg-eyebrow">Why this fired</div>'
                                   f'<div style="font-size:13.5px;line-height:1.55;margin-top:6px;color:{theme.TEXT};">'
                                   f'{esc(d.rationale)}</div>', unsafe_allow_html=True)
                        facts = [
                            ("Matched on", (mc.get("match_reason", "") or "").replace("_", " ").title() or "—",
                             mc.get("matched_port_id", "")),
                            ("Corroboration", f'{corr.get("n_reports", "—")} reports' if corr else "—",
                             f'{corr.get("n_publishers", "—")} publishers · {corr.get("confidence", 0):.0%}' if corr else ""),
                            ("Days to ETA", f'{mc.get("days_until_eta", "—")}' if mc.get("days_until_eta") is not None else "—",
                             f'ETA {r.eta[:10]}'),
                        ]
                        st.markdown('<div class="pg-facts">' + "".join(
                            f'<div><div class="pg-fact-label">{esc(l)}</div><div class="pg-fact-value">{esc(v)}</div>'
                            f'<div style="font-size:12px;color:{theme.MUTED};">{esc(s)}</div></div>' for l, v, s in facts)
                            + '</div></div>', unsafe_allow_html=True)

                        if traffic_d and traffic_d.get("pct_change") is not None:
                            pct = traffic_d["pct_change"]
                            arrow = "↓" if pct < 0 else "↑"
                            pct_chip = chip(f"{arrow} {abs(pct):.1f}%", "warn")
                            st.markdown(
                                '<div class="pg-card" style="margin-top:14px;">'
                                '<div style="display:flex;align-items:center;justify-content:space-between;">'
                                f'<div><div class="pg-eyebrow">Measured traffic</div>'
                                f'<div style="font-size:12.5px;color:{theme.MUTED};margin-top:2px;">{esc(traffic_d.get("name",""))} · IMF PortWatch</div></div>'
                                f'{pct_chip}</div>'
                                f'<div style="display:flex;justify-content:space-between;font-size:11.5px;color:{theme.FAINT};margin-top:8px;">'
                                f'<span>{traffic_d.get("avg_7d", "—")}/day, last 7 days</span>'
                                f'<span>{traffic_d.get("baseline_avg", "—")}/day, prior 90 days</span></div></div>',
                                unsafe_allow_html=True)

                        citations = json.loads(d.citations)
                        headlines = corr.get("headlines", []) if corr else []
                        ev_lines = []
                        for h in headlines[:4]:
                            ev_lines.append(f'<div><a href="{esc(h["url"])}" target="_blank">{esc(h.get("source", ""))} — '
                                            f'{esc(trunc(h["title"], 70))}</a></div>')
                        if not ev_lines:
                            ev_lines = [f'<div><a href="{esc(u)}" target="_blank">[{i + 1}] {esc(u[:70])}</a></div>'
                                       for i, u in enumerate(citations[:4])]
                        n_more = max(0, (corr.get("n_reports", len(citations)) if corr else len(citations)) - len(ev_lines))
                        st.markdown(
                            '<div class="pg-card" style="margin-top:14px;"><div class="pg-eyebrow">Evidence</div>'
                            f'<div style="margin-top:8px;font-size:13.5px;line-height:1.8;">{"".join(ev_lines)}'
                            + (f'<div style="color:{theme.FAINT};">+ {n_more} more reports in this event</div>' if n_more else "")
                            + '</div></div>', unsafe_allow_html=True)

                    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
                    h1, h2, h3 = st.columns([2, 1, 1])
                    notes = h1.text_input("Reviewer note — visible in the audit log", value=d.human_notes or "",
                                          key=f"notes-{d.decision_id}")
                    st.caption(f"Human decision: **{d.human_decision}**")
                    if h2.button("Approve & send notice" if d.recommended_action == "invoke_force_majeure" else "Approve",
                                key=f"ok-{d.decision_id}", type="primary", width="stretch"):
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
        st.markdown('<div class="pg-card pg-empty">No events yet — run <code>python src/cluster_events.py</code>.</div>',
                    unsafe_allow_html=True)
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
        st.markdown('<div class="pg-card pg-empty">No traffic data yet — run <code>python src/fetch_traffic.py</code> '
                    'then <code>python src/init_db.py</code>.</div>', unsafe_allow_html=True)
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
            c2.line_chart(series.set_index("date")[["value", "7-day average"]], height=320,
                         color=[theme.RGB["grey"], theme.RGB["blue"]])
            c2.caption("Daily ship transits (chokepoints) or port calls (ports), IMF PortWatch. Data lags ~5 days.")

# --------------------------------------------------------------------------- backtest

with tab_backtest:
    if not BACKTEST_JSON.exists():
        st.markdown('<div class="pg-card pg-empty">Run <code>python src/backtest.py</code> to replay historical '
                    'disruptions (Red Sea 2023, Baltimore 2024, US ports strike 2024, Panama drought 2023, Hormuz 2026).</div>',
                    unsafe_allow_html=True)
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
            st.line_chart(ts, height=300, color=[theme.RGB["grey"], theme.RGB["blue"]] if ts.shape[1] > 1 else [theme.RGB["blue"]])
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
