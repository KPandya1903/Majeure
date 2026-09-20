import type { Kpis } from "./api";
import { fmtCountdown, fmtMoney } from "./api";

export function Chip({ text, tone = "mute" }: { text: string; tone?: string }) {
  return <span className={`pg-chip ${tone}`}>{text}</span>;
}

export function KpiCard({ label, value, note, due, noteTone }:
  { label: string; value: string; note: string; due?: boolean; noteTone?: "risk" }) {
  return (
    <div className={`pg-card${due ? " pg-kpi-due" : ""}`}>
      <div className="pg-kpi-label">{label}</div>
      <div className="pg-kpi-value pg-num">{value}</div>
      <div className="pg-kpi-note" style={noteTone === "risk" ? { color: "var(--pg-red)", fontWeight: 600 } : undefined}>
        {note}
      </div>
    </div>
  );
}

export function KpiRow({ kpis }: { kpis: Kpis }) {
  const hasDeadline = kpis.next_deadline_hours !== null;
  return (
    <div className="pg-kpis">
      <KpiCard label="Active events" value={String(kpis.active_events)}
        note={`${kpis.structured_events} anchored on PortWatch / GDACS`} />
      <KpiCard label="Shipments at risk" value={`${kpis.shipments_at_risk} / ${kpis.shipments_total}`}
        note={kpis.shipments_at_risk ? `${kpis.n_critical} critical · ${kpis.n_high} high` : "No shipments at elevated risk"}
        noteTone={kpis.shipments_at_risk ? "risk" : undefined} />
      <KpiCard label="Decisions awaiting approval" value={String(kpis.pending_decisions)}
        note="reviewed in Shipments & decisions" />
      <KpiCard label="Open exposure" value={`$${fmtMoney(kpis.open_exposure_usd)}`}
        note="if every recommendation is taken" />
      <KpiCard label="Cost avoided" value={`$${fmtMoney(kpis.avoided_cost_usd)}`}
        note="vs. the costliest option on the table" />
      <KpiCard label="Force majeure notice due" value={hasDeadline ? fmtCountdown(kpis.next_deadline_hours) : "—"}
        note={hasDeadline ? "earliest of any drafted notice" : "No notices pending"} due={hasDeadline} />
    </div>
  );
}

export function AlertStrip({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return (
    <div className="pg-strip">
      <span>⚠</span>
      <span dangerouslySetInnerHTML={{ __html: text }} />
      <span style={{ flexGrow: 1 }} />
      <button className="pg-btn" style={{ minHeight: 32, padding: "2px 10px" }} onClick={onDismiss} aria-label="Dismiss">
        ✕
      </button>
    </div>
  );
}

export function Header({ updatedAgo }: { updatedAgo: string | null }) {
  return (
    <div className="pg-header-row">
      <div className="pg-brand">
        <span className="pg-brand-name">PortGuard</span>
        <span className="pg-brand-tag">AI</span>
      </div>
      <span className="pg-sub">
        Contract-grade disruption decisions — IMF PortWatch · GDACS · GDELT · Google News · maritime trade press.
      </span>
      <span style={{ flexGrow: 1 }} />
      {updatedAgo && (
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12.5, color: "var(--pg-muted)" }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--pg-green)", display: "inline-block" }} />
          {updatedAgo}
        </span>
      )}
    </div>
  );
}

export function Tabs({ tabs, active, onChange }: { tabs: string[]; active: string; onChange: (t: string) => void }) {
  return (
    <div className="pg-tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t} role="tab" aria-selected={t === active} className={`pg-tab${t === active ? " active" : ""}`}
          onClick={() => onChange(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}
