import { useEffect, useState } from "react";
import "./theme.css";
import { api } from "./api";
import { AlertStrip, Header, KpiRow, Tabs } from "./components";
import RiskMap from "./RiskMap";
import Shipments from "./Shipments";
import Backtest from "./Backtest";
import Signals from "./Signals";
import type { EventRow, Kpis, TrafficStat } from "./api";

const TABS = ["Risk map", "Shipments & decisions", "Events", "Traffic", "Backtest", "Ask PortGuard", "All signals"];

function EventsTab() {
  const [events, setEvents] = useState<EventRow[] | null>(null);
  useEffect(() => { api.events().then(setEvents); }, []);
  if (!events) return <div className="pg-card pg-empty">Loading…</div>;
  if (!events.length) return <div className="pg-card pg-empty">No events yet.</div>;
  return (
    <table className="pg-table">
      <thead><tr><th>Title</th><th>Type</th><th>Severity</th><th>Location</th><th>Reports</th><th>Publishers</th></tr></thead>
      <tbody>
        {events.map((e) => (
          <tr key={e.event_id}>
            <td>{e.title.slice(0, 90)}</td><td>{e.event_type}</td><td>{e.severity ?? "—"}</td>
            <td>{e.location_name}</td><td className="pg-num">{e.n_signals}</td><td className="pg-num">{e.n_publishers}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TrafficTab() {
  const [stats, setStats] = useState<TrafficStat[] | null>(null);
  useEffect(() => { api.traffic().then(setStats); }, []);
  if (!stats) return <div className="pg-card pg-empty">Loading…</div>;
  if (!stats.length) return <div className="pg-card pg-empty">No traffic data yet — run <code>python src/fetch_traffic.py</code>.</div>;
  return (
    <table className="pg-table">
      <thead><tr><th>Location</th><th>Last 7 days</th><th>Prior 90 days</th><th>Change</th></tr></thead>
      <tbody>
        {stats.map((s) => (
          <tr key={s.location_id}>
            <td>{s.name}</td><td className="pg-num">{s.avg_7d.toFixed(1)}/day</td>
            <td className="pg-num">{s.baseline_avg.toFixed(1)}/day</td>
            <td className="pg-num">{s.pct_change != null ? `${s.pct_change > 0 ? "+" : ""}${s.pct_change.toFixed(0)}%` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AskTab() {
  const [q, setQ] = useState("");
  const [chat, setChat] = useState<{ role: "user" | "assistant"; text: string }[]>([]);
  const [busy, setBusy] = useState(false);

  async function ask() {
    if (!q.trim()) return;
    const question = q;
    setChat((c) => [...c, { role: "user", text: question }]);
    setQ("");
    setBusy(true);
    try {
      const { answer } = await api.ask(question);
      setChat((c) => [...c, { role: "assistant", text: answer }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="pg-sub" style={{ padding: "4px 0 12px" }}>
        Ask about your shipments, events and decisions. Nemotron answers only from PortGuard's data and cites sources.
      </div>
      {chat.length === 0 && (
        <div className="pg-card pg-empty">No messages yet — try: "Which shipments go through Hormuz, and can we invoke force majeure?"</div>
      )}
      {chat.map((m, i) => (
        <div key={i} className="pg-card" style={{ marginTop: 10, background: m.role === "user" ? "#F5F8FE" : undefined }}>
          <strong>{m.role === "user" ? "You" : "Nemotron"}:</strong> {m.text}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()}
          placeholder="Ask a question…" disabled={busy}
          style={{ flexGrow: 1, border: "1px solid var(--pg-line)", borderRadius: 6, padding: "10px 12px" }} />
        <button className="pg-btn primary" disabled={busy} onClick={ask}>{busy ? "Asking…" : "Ask"}</button>
      </div>
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
    <div className="pg-shell">
      {topEvent && !dismissed && (
        <AlertStrip
          text={`<strong>${topEvent.title}</strong> · ${topEvent.n_signals} reports from ${topEvent.n_publishers} publishers`}
          onDismiss={() => setDismissed(true)}
        />
      )}
      <Header updatedAgo={null} />
      {kpis && <KpiRow kpis={kpis} />}
      <Tabs tabs={TABS} active={tab} onChange={setTab} />
      <div style={{ marginTop: 16 }}>
        {tab === "Risk map" && <RiskMap />}
        {tab === "Shipments & decisions" && <Shipments />}
        {tab === "Events" && <EventsTab />}
        {tab === "Traffic" && <TrafficTab />}
        {tab === "Backtest" && <Backtest />}
        {tab === "Ask PortGuard" && <AskTab />}
        {tab === "All signals" && <Signals />}
      </div>
    </div>
  );
}
