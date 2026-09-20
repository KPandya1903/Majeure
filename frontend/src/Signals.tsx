import { useEffect, useMemo, useState } from "react";
import type { SignalRow } from "./api";
import { RISK_TONE, api, fmtAgo } from "./api";
import { Chip } from "./components";

const SOURCE_LABEL: Record<string, string> = {
  portwatch: "IMF PortWatch", gdacs: "GDACS", gdelt: "GDELT", google_news: "Google News",
  gcaptain: "gCaptain", maritime_executive: "Maritime Executive", seatrade_maritime: "Seatrade Maritime",
  hellenic_shipping_news: "Hellenic Shipping News", splash247: "Splash 247",
};

/** One report as a readable card. The stored text is a cleaned summary (75-350 chars depending
 * on the feed), not a full article body — so the card shows that in full once expanded and
 * sends you to the publisher for the rest, rather than pretending to hold the whole piece. */
function ArticleCard({ s }: { s: SignalRow }) {
  const [open, setOpen] = useState(false);
  // Aggregators (GDELT, Google News) store the headline as the body, so the card would print
  // the same sentence twice. Only show a body that actually adds something to the title.
  const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const raw = s.raw_text?.trim() || "";
  const body = raw && !norm(raw).startsWith(norm(s.title).slice(0, 60)) ? raw : "";
  // ~4 lines at this width; only worth a toggle if there's meaningfully more behind it.
  const clampable = body.length > 210;

  return (
    <article className="pg-article">
      <div className="pg-article-meta">
        <span className="pg-article-source">{SOURCE_LABEL[s.source] ?? s.source.replace(/_/g, " ")}</span>
        <span>·</span>
        <span>{fmtAgo(s.published_at)}</span>
        {s.location_name && <><span>·</span><span>{s.location_name}</span></>}
        <span style={{ flexGrow: 1 }} />
        {s.severity && <Chip text={s.severity.toUpperCase()} tone={RISK_TONE[s.severity] ?? "mute"} />}
        <Chip text={s.event_type.replace(/_/g, " ")} tone="mute" />
      </div>

      <h3 className="pg-article-title">{s.title}</h3>

      {body && (
        <p className={`pg-article-body${clampable && !open ? " clamped" : ""}`}
           onClick={() => clampable && setOpen((o) => !o)}
           style={{ cursor: clampable ? "pointer" : "default" }}>
          {body}
        </p>
      )}

      <div className="pg-article-foot">
        {clampable && (
          <button className="pg-link-btn" onClick={() => setOpen((o) => !o)}>
            {open ? "Show less" : "Read more"}
          </button>
        )}
        <a href={s.source_url} target="_blank" rel="noreferrer" className="pg-link-btn">
          Read full article ↗
        </a>
      </div>
    </article>
  );
}

export default function Signals() {
  const [rows, setRows] = useState<SignalRow[] | null>(null);
  const [source, setSource] = useState("");
  const [eventType, setEventType] = useState("");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(30);

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
      (!q || r.title.toLowerCase().includes(q.toLowerCase()) ||
             (r.raw_text ?? "").toLowerCase().includes(q.toLowerCase())));
  }, [rows, source, eventType, q]);

  useEffect(() => { setLimit(30); }, [source, eventType, q]);  // new filter, start from the top

  if (!rows) return <div className="pg-card pg-empty">Loading…</div>;

  return (
    <div>
      <div style={{ display: "flex", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
        <select value={source} onChange={(e) => setSource(e.target.value)} className="pg-select">
          <option value="">All sources</option>
          {sources.map((s) => <option key={s} value={s}>{SOURCE_LABEL[s] ?? s}</option>)}
        </select>
        <select value={eventType} onChange={(e) => setEventType(e.target.value)} className="pg-select">
          <option value="">All event types</option>
          {eventTypes.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search headlines and text…"
               className="pg-select" style={{ flexGrow: 1, minWidth: 220 }} />
      </div>

      {filtered.length === 0 ? (
        <div className="pg-card pg-empty">No reports match these filters.</div>
      ) : (
        <>
          <div className="pg-article-grid">
            {filtered.slice(0, limit).map((s, i) => <ArticleCard key={`${s.source_url}-${i}`} s={s} />)}
          </div>
          <div style={{ textAlign: "center", marginTop: 16 }}>
            <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginBottom: 8 }}>
              Showing {Math.min(limit, filtered.length)} of {filtered.length}
              {filtered.length !== rows.length ? ` (filtered from ${rows.length})` : ""}
            </div>
            {limit < filtered.length && (
              <button className="pg-btn" onClick={() => setLimit((l) => l + 30)}>Load 30 more</button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
