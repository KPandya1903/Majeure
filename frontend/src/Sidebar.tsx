import { useEffect, useState } from "react";
import type { SourceRow } from "./api";
import { api, fmtAgo } from "./api";

/** Left rail, ported from the Streamlit build's sidebar: what PortGuard is, how fresh the data
 * is, and which sources it came from. Freshness is the important bit — if the ingest pipeline
 * stops, every number on screen silently goes stale, and this is the only place that shows it. */
export default function Sidebar() {
  const [sources, setSources] = useState<SourceRow[] | null>(null);
  const [lastIngested, setLastIngested] = useState<string | null>(null);
  const [checkedMin, setCheckedMin] = useState<number | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [, forceTick] = useState(0);

  useEffect(() => {
    const load = () =>
      api.sources()
        .then((d) => {
          setSources(d.sources); setLastIngested(d.last_ingested);
          setCheckedMin(d.last_checked_min); setUnreachable(false);
        })
        .catch((e) => { console.error("sources fetch failed", e); setUnreachable(true); });
    load();
    const refetch = setInterval(load, 60_000);
    // Re-render every 30s so the relative "N min ago" labels age even between refetches.
    const tick = setInterval(() => forceTick((n) => n + 1), 30_000);
    return () => { clearInterval(refetch); clearInterval(tick); };
  }, []);

  // Staleness is about whether the pipeline is RUNNING (last fetch attempt), not whether the
  // sources happened to publish anything. A quiet trade feed at 3am is normal; a pipeline that
  // hasn't checked in 20 minutes when it ticks every 5 is not.
  const stale = checkedMin == null ? false : checkedMin > 20;

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
                       background: unreachable ? "var(--pg-red)" : stale ? "var(--pg-amber)" : "var(--pg-green)" }} />
        <span style={{ color: unreachable ? "var(--pg-red)" : stale ? "var(--pg-amber)" : "var(--pg-muted)" }}>
          {unreachable ? "API unreachable"
            : checkedMin == null ? `Newest data ${fmtAgo(lastIngested)}`
            : checkedMin < 1 ? "Checked just now"
            : `Checked ${Math.round(checkedMin)} min ago`}
        </span>
      </div>
      {/* Distinguish "the API didn't answer" from "the API answered, but the data is old" —
          they have completely different causes and conflating them sends you debugging the
          wrong one (a cold-started/redeploying backend looks identical to a dead pipeline). */}
      {unreachable ? (
        <div style={{ fontSize: 11.5, color: "var(--pg-red)", marginTop: 4, lineHeight: 1.4 }}>
          Can't reach the backend — it may be redeploying or cold-starting. Retrying every 60s.
        </div>
      ) : stale ? (
        <div style={{ fontSize: 11.5, color: "var(--pg-amber)", marginTop: 4, lineHeight: 1.4 }}>
          Expected every ~5 min — the ingest pipeline may not be running.
        </div>
      ) : null}

      <div className="pg-eyebrow" style={{ marginTop: 20 }}>Sources</div>
      <div style={{ fontSize: 11, color: "var(--pg-faint)", marginTop: 3, lineHeight: 1.4 }}>
        "Newest" = age of the latest article seen, not when we last checked. Quiet feeds show
        old times even though every source is polled on schedule.
      </div>
      {!sources ? (
        <div className="pg-sub" style={{ marginTop: 8 }}>{unreachable ? "Unavailable" : "Loading…"}</div>
      ) : (
        <table className="pg-table" style={{ marginTop: 8, fontSize: "0.78rem" }}>
          <thead><tr><th>Source</th><th style={{ textAlign: "right" }}>Signals</th><th>Newest</th></tr></thead>
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
