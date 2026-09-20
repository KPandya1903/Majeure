import { useEffect, useState } from "react";
import type { SourceRow } from "./api";
import { api, fmtAgo } from "./api";

/** Left rail, ported from the Streamlit build's sidebar: what PortGuard is, how fresh the data
 * is, and which sources it came from. Freshness is the important bit — if the ingest pipeline
 * stops, every number on screen silently goes stale, and this is the only place that shows it. */
export default function Sidebar() {
  const [sources, setSources] = useState<SourceRow[] | null>(null);
  const [lastIngested, setLastIngested] = useState<string | null>(null);
  const [, forceTick] = useState(0);

  useEffect(() => {
    const load = () =>
      api.sources()
        .then((d) => { setSources(d.sources); setLastIngested(d.last_ingested); })
        .catch((e) => console.error("sources fetch failed", e));
    load();
    const refetch = setInterval(load, 60_000);
    // Re-render every 30s so the relative "N min ago" labels age even between refetches.
    const tick = setInterval(() => forceTick((n) => n + 1), 30_000);
    return () => { clearInterval(refetch); clearInterval(tick); };
  }, []);

  const ageMins = lastIngested ? (Date.now() - new Date(lastIngested).getTime()) / 60_000 : Infinity;
  const stale = ageMins > 20;   // pipeline ticks every 5 min; 20 means something's wrong

  return (
    <aside className="pg-sidebar">
      <div className="pg-brand">
        <span className="pg-brand-name">PortGuard</span>
        <span className="pg-brand-tag">AI</span>
      </div>
      <div className="pg-sub" style={{ marginTop: 6, lineHeight: 1.45 }}>
        Maritime disruptions → shipment-specific, contract-aware decisions.
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 14, fontSize: 12.5 }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", display: "inline-block",
                       background: stale ? "var(--pg-amber)" : "var(--pg-green)" }} />
        <span style={{ color: stale ? "var(--pg-amber)" : "var(--pg-muted)" }}>
          Data fetched {fmtAgo(lastIngested)}
        </span>
      </div>
      {stale && (
        <div style={{ fontSize: 11.5, color: "var(--pg-amber)", marginTop: 4, lineHeight: 1.4 }}>
          Expected every ~5 min — the ingest pipeline may not be running.
        </div>
      )}

      <div className="pg-eyebrow" style={{ marginTop: 20 }}>Sources</div>
      {!sources ? (
        <div className="pg-sub" style={{ marginTop: 8 }}>Loading…</div>
      ) : (
        <table className="pg-table" style={{ marginTop: 8, fontSize: "0.78rem" }}>
          <thead><tr><th>Source</th><th style={{ textAlign: "right" }}>Signals</th><th>Last</th></tr></thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.source} style={{ cursor: "default" }}>
                <td style={{ padding: "7px 10px" }}>{s.source.replace(/_/g, " ")}</td>
                <td className="pg-num" style={{ padding: "7px 10px", textAlign: "right" }}>{s.signals}</td>
                <td style={{ padding: "7px 10px", color: "var(--pg-faint)", whiteSpace: "nowrap" }}>
                  {fmtAgo(s.last_ingested)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </aside>
  );
}
