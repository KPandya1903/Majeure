import { useEffect, useState } from "react";
import type { Decision, Shipment } from "./api";
import { ACTIONS, RISK_TONE, api, fmtCountdown, fmtDate, hoursUntil } from "./api";
import { Chip } from "./components";

const RISK_ORDER: Record<string, number> = { low: 0, medium: 1, high: 2, critical: 3 };

function topDecision(s: Shipment): Decision | undefined {
  return [...s.decisions].sort((a, b) => RISK_ORDER[b.risk_level] - RISK_ORDER[a.risk_level])[0];
}

function FleetRow({ s, selected, onSelect }: { s: Shipment; selected: boolean; onSelect: () => void }) {
  const top = topDecision(s);
  const contractEta = new Date(s.eta);
  let pgEta = contractEta;
  let delayDays = 0;
  if (top) {
    const opt = top.exposure?.options.find((o) => o.action === top.recommended_action);
    delayDays = opt ? Math.round(opt.expected_delay_days) : 0;
    pgEta = new Date(contractEta.getTime() + delayDays * 86_400_000);
  }
  const dueHours = top?.exposure?.force_majeure_notice_due ? hoursUntil(top.exposure.force_majeure_notice_due) : null;
  return (
    <tr className={selected ? "selected" : ""} onClick={onSelect}>
      <td>
        <div style={{ fontWeight: 600 }}>{s.shipment_id}</div>
        <div className="pg-mono">{s.bl_number}</div>
      </td>
      <td>
        <div>{s.pol_locode} → {s.pod_locode}</div>
        <div style={{ fontSize: 11.5, color: "var(--pg-faint)" }}>
          {s.transshipment.length ? `${s.transshipment.length} TSP · ${s.transshipment.join(", ")}` : "Direct"}
        </div>
      </td>
      <td className="pg-num">{fmtDate(contractEta.toISOString())}</td>
      <td className="pg-num">
        {fmtDate(pgEta.toISOString(), false)}{" "}
        {delayDays > 0 ? <Chip text={`+${delayDays}d`} tone="warn" /> : <Chip text="On time" tone="ok" />}
      </td>
      <td>
        {top ? (
          <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Chip text={top.risk_level.toUpperCase()} tone={RISK_TONE[top.risk_level]} />
            <span>{top.recommended_action_label} — ${(top.estimated_cost_usd ?? 0).toLocaleString()}</span>
            {top.recommended_action === "invoke_force_majeure" && dueHours !== null ? (
              <Chip text={`Notice ${fmtCountdown(dueHours)}`} tone={dueHours > 0 ? "warn" : "risk"} />
            ) : top.estimated_cost_usd ? (
              <Chip text={`$${top.estimated_cost_usd.toLocaleString()}`} tone={top.risk_level === "critical" ? "risk" : "warn"} />
            ) : null}
          </span>
        ) : (
          <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <Chip text="CLEAR" tone="ok" />
            <span style={{ color: "var(--pg-muted)" }}>No event touches this route</span>
          </span>
        )}
      </td>
    </tr>
  );
}

function DecisionPanel({ shipment, decision, onReviewed }: { shipment: Shipment; decision: Decision; onReviewed: () => void }) {
  const [note, setNote] = useState(decision.human_notes ?? "");
  const [busy, setBusy] = useState(false);
  const dueHours = decision.exposure?.force_majeure_notice_due ? hoursUntil(decision.exposure.force_majeure_notice_due) : null;
  const options = (decision.exposure?.options ?? []).filter((o) => o.available);
  const maxCost = Math.max(0, ...options.map((o) => o.cost_usd));

  async function review(human_decision: "approved" | "overridden") {
    setBusy(true);
    try {
      await api.reviewDecision(decision.decision_id, human_decision, note);
      onReviewed();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pg-card" style={{ marginTop: 18 }}>
      <div className="pg-eyebrow">{shipment.pol} → {shipment.pod} · {shipment.bl_number}</div>
      <div className="pg-h">{decision.recommended_action_label}</div>

      {decision.exposure?.force_majeure_covered === false ? (
        <div className="pg-card" style={{ marginTop: 10, color: "var(--pg-muted)", fontSize: "0.87rem" }}>
          Force majeure clause does not cover this event.
        </div>
      ) : dueHours !== null ? (
        <div className={`pg-banner${dueHours <= 0 ? " late" : ""}`} style={{ marginTop: 10 }}>
          {"⏱"} Written force majeure notice {dueHours > 0 ? "due in" : "was due"}{" "}
          <strong>{fmtCountdown(dueHours)}</strong>{dueHours <= 0 ? " ago" : ""} — by{" "}
          {decision.exposure!.force_majeure_notice_due!.slice(0, 16).replace("T", " ")} UTC
        </div>
      ) : null}

      <div className="pg-panel-grid" style={{ marginTop: 14 }}>
        <div className="pg-card">
          <div className="pg-eyebrow">What each option costs</div>
          {options.map((o) => {
            const isBest = o.action === decision.recommended_action;
            const isWorst = !isBest && maxCost > 0 && o.cost_usd === maxCost && o.cost_usd > 0;
            const width = maxCost ? Math.max(3, Math.round((100 * o.cost_usd) / maxCost)) : 3;
            const wrapClass = isBest ? "pg-best" : "pg-option";
            return (
              <div key={o.action} className={wrapClass}>
                <div className="pg-option-head">
                  <span style={{ fontWeight: isBest ? 600 : 400, color: isBest ? "var(--pg-blue)" : "var(--pg-text)" }}>
                    {ACTIONS[o.action] ?? o.action}{isBest ? " · recommended" : ""}
                  </span>
                  <span className="pg-num" style={{ fontWeight: 600, color: isBest ? "var(--pg-blue)" : isWorst ? "var(--pg-red)" : "var(--pg-text)" }}>
                    ${o.cost_usd.toLocaleString()}
                  </span>
                </div>
                <div className={`pg-bar${isBest ? " is-best" : isWorst ? " is-risk" : ""}`}>
                  <span style={{ width: `${width}%` }} />
                </div>
                <div className="pg-option-note">{o.note}</div>
              </div>
            );
          })}
          {decision.exposure?.assumptions?.length ? (
            <div style={{ fontSize: 11.5, color: "var(--pg-faint)", marginTop: 12 }}>
              {decision.exposure.assumptions[decision.exposure.assumptions.length - 1]}
            </div>
          ) : null}
        </div>

        <div>
          <div className="pg-card">
            <div className="pg-eyebrow">Why this fired</div>
            <div style={{ fontSize: 13.5, lineHeight: 1.55, marginTop: 6 }}>{decision.rationale}</div>
            <div className="pg-facts">
              <div>
                <div className="pg-fact-label">Matched on</div>
                <div className="pg-fact-value">{decision.match_context?.match_reason?.replace(/_/g, " ") ?? "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>{decision.match_context?.matched_port_id}</div>
              </div>
              <div>
                <div className="pg-fact-label">Corroboration</div>
                <div className="pg-fact-value">{decision.corroboration ? `${decision.corroboration.n_reports} reports` : "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>
                  {decision.corroboration ? `${decision.corroboration.n_publishers} publishers · ${Math.round(decision.corroboration.confidence * 100)}%` : ""}
                </div>
              </div>
              <div>
                <div className="pg-fact-label">Days to ETA</div>
                <div className="pg-fact-value">{decision.match_context?.days_until_eta ?? "—"}</div>
                <div style={{ fontSize: 12, color: "var(--pg-muted)" }}>ETA {shipment.eta.slice(0, 10)}</div>
              </div>
            </div>
          </div>

          {decision.traffic?.pct_change != null && (
            <div className="pg-card" style={{ marginTop: 14 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div className="pg-eyebrow">Measured traffic</div>
                  <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 2 }}>{decision.traffic.name} · IMF PortWatch</div>
                </div>
                <Chip text={`${decision.traffic.pct_change < 0 ? "↓" : "↑"} ${Math.abs(decision.traffic.pct_change).toFixed(1)}%`} tone="warn" />
              </div>
            </div>
          )}

          <div className="pg-card" style={{ marginTop: 14 }}>
            <div className="pg-eyebrow">Evidence</div>
            <div style={{ marginTop: 8, fontSize: 13.5, lineHeight: 1.8 }}>
              {(decision.corroboration?.headlines ?? []).slice(0, 4).map((h, i) => (
                <div key={i}><a href={h.url} target="_blank" rel="noreferrer">{h.source} — {h.title.slice(0, 70)}</a></div>
              ))}
              {!decision.corroboration?.headlines?.length &&
                decision.citations.slice(0, 4).map((u, i) => (
                  <div key={i}><a href={u} target="_blank" rel="noreferrer">[{i + 1}] {u.slice(0, 70)}</a></div>
                ))}
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 14 }}>
        <label htmlFor="note" style={{ fontSize: 13, color: "var(--pg-muted)" }}>Reviewer note</label>
        <input id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional — visible in the audit log"
          style={{ flexGrow: 1, border: "1px solid var(--pg-line)", borderRadius: 6, padding: "10px 12px", fontSize: 13.5 }} />
        <button className="pg-btn" disabled={busy} onClick={() => review("overridden")}>Override</button>
        <button className="pg-btn primary" disabled={busy}
          onClick={() => review("approved")}>
          {decision.recommended_action === "invoke_force_majeure" ? "Approve & send notice" : "Approve"}
        </button>
      </div>
      <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 6 }}>Human decision: <strong>{decision.human_decision}</strong></div>
    </div>
  );
}

export default function Shipments() {
  const [ships, setShips] = useState<Shipment[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  function reload() {
    api.shipments().then((data) => {
      setShips(data);
      if (!selected && data.length) setSelected(data[0].shipment_id);
    });
  }
  useEffect(reload, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!ships) return <div className="pg-card pg-empty">Loading shipments…</div>;
  if (!ships.length) return <div className="pg-card pg-empty">No shipments yet — run <code>python src/load_shipments.py</code>.</div>;

  const sorted = [...ships].sort((a, b) => RISK_ORDER[topDecision(b)?.risk_level ?? "low"] - RISK_ORDER[topDecision(a)?.risk_level ?? "low"]);
  const activeShip = ships.find((s) => s.shipment_id === selected);
  const activeDecisions = [...(activeShip?.decisions ?? [])].sort((a, b) => RISK_ORDER[b.risk_level] - RISK_ORDER[a.risk_level]);

  return (
    <div>
      <table className="pg-table">
        <thead>
          <tr>
            <th>Shipment · B/L</th><th>Path</th><th>Contract ETA</th><th>PortGuard ETA</th><th>Status &amp; recommendation</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => (
            <FleetRow key={s.shipment_id} s={s} selected={s.shipment_id === selected} onSelect={() => setSelected(s.shipment_id)} />
          ))}
        </tbody>
      </table>

      {activeShip && activeDecisions.length === 0 && (
        <div className="pg-card pg-empty" style={{ marginTop: 18 }}>No matching events for {activeShip.shipment_id} right now.</div>
      )}
      {activeShip && activeDecisions.map((d) => (
        <DecisionPanel key={d.decision_id} shipment={activeShip} decision={d} onReviewed={reload} />
      ))}
    </div>
  );
}
