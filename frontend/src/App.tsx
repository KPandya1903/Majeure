import { useEffect, useState } from "react";
import "./theme.css";
import { api, RISK_TONE } from "./api";
import { AlertStrip, Chip, KpiRow, Tabs } from "./components";
import { BarList, LineChart, severityColor } from "./Charts";
import ChatWidget from "./ChatWidget";
import RiskMap from "./RiskMap";
import Shipments from "./Shipments";
import Backtest from "./Backtest";
import Signals from "./Signals";
import Sidebar from "./Sidebar";
import type { EventRow, Kpis, TrafficStat } from "./api";

const TABS = ["Risk map", "Shipments & decisions", "Events", "Traffic", "Backtest", "All signals"];

function EventsTab() {
  const [events, setEvents] = useState<EventRow[] | null>(null);
  useEffect(() => { api.events().then(setEvents); }, []);
  if (!events) return <div className="pg-card pg-empty">Loading…</div>;
  if (!events.length) return <div className="pg-card pg-empty">No events yet.</div>;

  const count = (key: (e: EventRow) => string | null) => {
    const m = new Map<string, number>();
    events.forEach((e) => { const k = key(e); if (k) m.set(k, (m.get(k) ?? 0) + 1); });
    return [...m.entries()].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
  };
  const bySeverity = count((e) => e.severity);
  const byType = count((e) => e.event_type).slice(0, 6);
  const mostReported = [...events].sort((a, b) => b.n_signals - a.n_signals).slice(0, 6)
    .map((e) => ({ label: e.title.slice(0, 38), value: e.n_signals, tone: severityColor(e.severity ?? "") }));

  return (
    <div>
      <div className="pg-panel-grid" style={{ marginBottom: 16 }}>
        <div className="pg-card">
          <div className="pg-eyebrow">By severity</div>
          <BarList items={bySeverity} tone={severityColor} />
          <div className="pg-eyebrow" style={{ marginTop: 18 }}>By type</div>
          <BarList items={byType} />
        </div>
        <div className="pg-card">
          <div className="pg-eyebrow">Most corroborated</div>
          <div style={{ fontSize: 12, color: "var(--pg-muted)", marginTop: 2 }}>
            Reports clustered into each event — more reports, more confidence it's real.
          </div>
          <BarList items={mostReported} />
        </div>
      </div>
      <table className="pg-table">
        <thead><tr><th>Title</th><th>Type</th><th>Severity</th><th>Location</th><th>Reports</th><th>Publishers</th></tr></thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.event_id}>
              <td>{e.title.slice(0, 90)}</td><td>{e.event_type.replace(/_/g, " ")}</td>
              <td>{e.severity ? <Chip text={e.severity.toUpperCase()} tone={RISK_TONE[e.severity] ?? "mute"} /> : "—"}</td>
              <td>{e.location_name}</td><td className="pg-num">{e.n_signals}</td><td className="pg-num">{e.n_publishers}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TrafficTab() {
  const [stats, setStats] = useState<TrafficStat[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [series, setSeries] = useState<{ date: string; value: number }[] | null>(null);

  useEffect(() => {
    api.traffic().then((s) => {
      setStats(s);
      if (s.length && !picked) setPicked(s[0].location_id);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!picked) return;
    setSeries(null);
    api.trafficDaily(picked).then(setSeries);
  }, [picked]);

  if (!stats) return <div className="pg-card pg-empty">Loading…</div>;
  if (!stats.length) return <div className="pg-card pg-empty">No traffic data yet — run <code>python src/fetch_traffic.py</code>.</div>;

  const current = stats.find((s) => s.location_id === picked);
  // Biggest movements first — a chokepoint down 40% is the story, one flat at 0% isn't.
  const ranked = [...stats].sort((a, b) => Math.abs(b.pct_change ?? 0) - Math.abs(a.pct_change ?? 0));

  return (
    <div>
      {current && (
        <div className="pg-card" style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
            <div>
              <div className="pg-eyebrow">Daily {current.metric.replace(/_/g, " ")}</div>
              <div className="pg-h">{current.name}</div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Chip text={`${current.avg_7d.toFixed(1)}/day last 7d`} tone="info" />
              {current.pct_change != null && (
                <Chip text={`${current.pct_change > 0 ? "+" : ""}${current.pct_change.toFixed(0)}% vs 90-day`}
                      tone={current.pct_change <= -30 ? "risk" : current.pct_change <= -10 ? "warn" : "ok"} />
              )}
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            {series
              ? <LineChart series={series.map((d) => ({ x: d.date, y: d.value }))}
                           baseline={current.baseline_avg} label={`${current.name} daily ${current.metric}`} />
              : <div className="pg-empty">Loading series…</div>}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--pg-faint)" }}>
            IMF PortWatch. Data lags ~5 days. Dashed line is the prior 90-day average.
          </div>
        </div>
      )}
      <table className="pg-table">
        <thead><tr><th>Location</th><th>Last 7 days</th><th>Prior 90 days</th><th>Change</th></tr></thead>
        <tbody>
          {ranked.map((s) => (
            <tr key={s.location_id} className={s.location_id === picked ? "selected" : ""}
                onClick={() => setPicked(s.location_id)}>
              <td>{s.name}</td><td className="pg-num">{s.avg_7d.toFixed(1)}/day</td>
              <td className="pg-num">{s.baseline_avg.toFixed(1)}/day</td>
              <td className="pg-num">
                {s.pct_change != null
                  ? <Chip text={`${s.pct_change > 0 ? "+" : ""}${s.pct_change.toFixed(0)}%`}
                          tone={s.pct_change <= -30 ? "risk" : s.pct_change <= -10 ? "warn" : "mute"} />
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState(TABS[0]);
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [topEvent, setTopEvent] = useState<{ title: string; n_signals: number; n_publishers: number } | null>(null);

  useEffect(() => {
    api.kpis().then(setKpis).catch((e) => console.error("kpis fetch failed", e));
    api.events().then((events) => {
      const sev: Record<string, number> = { low: 0, medium: 1, high: 2, critical: 3 };
      const sorted = [...events].filter((e) => e.severity).sort(
        (a, b) => (sev[b.severity!] ?? 0) - (sev[a.severity!] ?? 0) || b.n_signals - a.n_signals);
      if (sorted.length) setTopEvent(sorted[0]);
    });
  }, []);

  return (
    <div className="pg-layout">
      <Sidebar />
      <div className="pg-shell">
        {/* Fixed chrome: alert, KPIs and tabs stay put while the panel below them scrolls. */}
        <div className="pg-shell-head">
          {topEvent && !dismissed && (
            <AlertStrip
              text={`<strong>${topEvent.title}</strong> · ${topEvent.n_signals} reports from ${topEvent.n_publishers} publishers`}
              onDismiss={() => setDismissed(true)}
            />
          )}
          {/* Brand/tagline/freshness live in the sidebar now — repeating them here was noise. */}
          {kpis && <KpiRow kpis={kpis} />}
          <Tabs tabs={TABS} active={tab} onChange={setTab} />
        </div>
        {/* `key={tab}` resets scroll position when the tab changes: without it, switching from the
            bottom of a long signals list to a short tab leaves you staring at empty space. */}
        <div className="pg-tabpanel" key={tab}>
          {tab === "Risk map" && <RiskMap />}
          {tab === "Shipments & decisions" && <Shipments />}
          {tab === "Events" && <EventsTab />}
          {tab === "Traffic" && <TrafficTab />}
          {tab === "Backtest" && <Backtest />}
          {tab === "All signals" && <Signals />}
        </div>
      </div>
      <ChatWidget />
    </div>
  );
}
