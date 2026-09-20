import { useEffect, useMemo, useState } from "react";
import type { SignalRow } from "./api";
import { api } from "./api";

export default function Signals() {
  const [rows, setRows] = useState<SignalRow[] | null>(null);
  const [source, setSource] = useState("");
  const [eventType, setEventType] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => { api.signals({ limit: 1000 }).then(setRows); }, []);

  const { sources, eventTypes } = useMemo(() => {
    const s = new Set<string>(), t = new Set<string>();
    (rows ?? []).forEach((r) => { s.add(r.source); t.add(r.event_type); });
    return { sources: [...s].sort(), eventTypes: [...t].sort() };
  }, [rows]);

  const filtered = useMemo(() => {
    if (!rows) return [];
    return rows.filter((r) =>
      (!source || r.source === source) &&
      (!eventType || r.event_type === eventType) &&
      (!q || r.title.toLowerCase().includes(q.toLowerCase())));
  }, [rows, source, eventType, q]);

  if (!rows) return <div className="pg-card pg-empty">Loading…</div>;

  return (
    <div>
      <div style={{ display: "flex", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
        <select value={source} onChange={(e) => setSource(e.target.value)}
          style={{ border: "1px solid var(--pg-line)", borderRadius: 6, padding: "8px 10px", minHeight: 40 }}>
          <option value="">All sources</option>
          {sources.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={eventType} onChange={(e) => setEventType(e.target.value)}
          style={{ border: "1px solid var(--pg-line)", borderRadius: 6, padding: "8px 10px", minHeight: 40 }}>
          <option value="">All event types</option>
          {eventTypes.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search titles…"
          style={{ flexGrow: 1, minWidth: 200, border: "1px solid var(--pg-line)", borderRadius: 6, padding: "8px 12px", minHeight: 40 }} />
      </div>

      {filtered.length === 0 ? (
        <div className="pg-card pg-empty">No signals match these filters.</div>
      ) : (
        <table className="pg-table">
          <thead>
            <tr><th>Published</th><th>Source</th><th>Type</th><th>Severity</th><th>Location</th><th>Title</th></tr>
          </thead>
          <tbody>
            {filtered.slice(0, 300).map((r, i) => (
              <tr key={i}>
                <td className="pg-mono">{r.published_at?.slice(0, 16).replace("T", " ") ?? "—"}</td>
                <td>{r.source}</td>
                <td>{r.event_type}</td>
                <td>{r.severity ?? "—"}</td>
                <td>{r.location_name ?? "—"}</td>
                <td><a href={r.source_url} target="_blank" rel="noreferrer">{r.title.slice(0, 100)}</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 8 }}>
        {filtered.length} of {rows.length} signals{filtered.length > 300 ? " (showing first 300)" : ""}
      </div>
    </div>
  );
}
