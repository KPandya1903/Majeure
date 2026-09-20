/** Small inline-SVG charts. Deliberately not a charting library: these are a line, some bars
 * and a baseline — a dependency for that would cost more (bundle, theming fights) than it saves,
 * and these inherit the design tokens directly. */

const BLUE = "#0B5CD5", GREY = "#C4C9CF", RED = "#D62828", GREEN = "#237A45";
// Severity ramp, matching RiskMap.SEVERITY_COLOR and the chip tones in theme.css. Keep the three
// in step: a bar, a map dot and a chip for the same event should never disagree on colour.
const ORANGE = "#E2711D", GOLD = "#C9A227";

export function LineChart({ series, baseline, height = 150, label }:
  { series: { x: string; y: number }[]; baseline?: number; height?: number; label?: string }) {
  if (series.length < 2) return <div className="pg-empty">Not enough data to chart.</div>;
  const W = 640, H = height, PAD = 8;
  const ys = series.map((d) => d.y);
  const lo = Math.min(...ys, baseline ?? Infinity);
  const hi = Math.max(...ys, baseline ?? -Infinity);
  const span = hi - lo || 1;
  const x = (i: number) => PAD + (i / (series.length - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - ((v - lo) / span) * (H - PAD * 2);
  const path = series.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)} ${y(d.y).toFixed(1)}`).join(" ");
  const area = `${path} L${x(series.length - 1).toFixed(1)} ${H - PAD} L${x(0).toFixed(1)} ${H - PAD} Z`;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height }} role="img"
           aria-label={label ?? "time series"}>
        <path d={area} fill={BLUE} opacity={0.08} />
        {baseline !== undefined && (
          <>
            <line x1={PAD} x2={W - PAD} y1={y(baseline)} y2={y(baseline)} stroke={GREY}
                  strokeWidth={1} strokeDasharray="4 4" />
            <text x={W - PAD} y={y(baseline) - 4} textAnchor="end" fontSize={10} fill="#949CA6">
              90-day avg {baseline.toFixed(1)}
            </text>
          </>
        )}
        <path d={path} fill="none" stroke={BLUE} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--pg-faint)" }}>
        <span>{series[0].x}</span><span>{series[series.length - 1].x}</span>
      </div>
    </div>
  );
}

/** Horizontal bars for a categorical breakdown (severity mix, events per source, ...). */
export function BarList({ items, tone }:
  { items: { label: string; value: number; tone?: string }[]; tone?: (label: string) => string }) {
  const max = Math.max(1, ...items.map((i) => i.value));
  const color = (i: { label: string; tone?: string }) =>
    i.tone ?? (tone ? tone(i.label) : BLUE);
  return (
    <div>
      {items.map((i) => (
        <div key={i.label} style={{ marginTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
            <span style={{ textTransform: "capitalize" }}>{i.label.replace(/_/g, " ")}</span>
            <span className="pg-num" style={{ color: "var(--pg-muted)" }}>{i.value}</span>
          </div>
          <div style={{ height: 7, background: "#E7EAEE", borderRadius: 4, overflow: "hidden" }}>
            <span style={{ display: "block", height: 7, borderRadius: 4,
                           width: `${Math.max(2, (100 * i.value) / max)}%`, background: color(i) }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export const severityColor = (s: string) =>
  ({ critical: RED, high: ORANGE, medium: GOLD, low: GREEN } as Record<string, string>)[s] ?? GREY;
