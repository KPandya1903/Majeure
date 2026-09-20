// Typed client for the FastAPI backend (backend/app/main.py). Shapes here must match that
// file's response bodies — there's no shared schema generation yet, so keep them in sync by hand.
const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8700";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

async function send<T>(method: "POST" | "PATCH", path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
  return res.json();
}

export interface Kpis {
  active_events: number;
  structured_events: number;
  shipments_at_risk: number;
  shipments_total: number;
  n_critical: number;
  n_high: number;
  pending_decisions: number;
  open_exposure_usd: number;
  next_deadline_hours: number | null;
}

export interface Route {
  shipment_id: string;
  sailed: [number, number][];
  remaining_dots: [number, number][];
  progress: number;
  /** Estimated position along the real sea route from elapsed ETD->ETA time — not AIS/GPS. */
  position: [number, number] | null;
  /** Heading in degrees (0 = north) of the last sailed leg, for rotating the ship icon. */
  bearing: number;
  vessel_name: string | null;
  carrier: string | null;
  cargo_description: string | null;
  eta: string;
  risk_level: string | null;
  recommended_action: string | null;
  label: string;
}
export interface EventPoint {
  event_id: string; title: string; event_type: string; severity: string;
  location_name: string; location_id: string; n_signals: number; n_publishers: number;
  lat: number; lon: number; radius: number; pct_change: number | null;
}
export interface PortPoint { lon: number; lat: number; name: string; locode: string }
export interface ChokePoint { lon: number; lat: number; name: string; location_id: string; pct_change: number | null }
export interface MapData { routes: Route[]; events: EventPoint[]; ports: PortPoint[]; chokepoints: ChokePoint[] }

export interface ExposureOption {
  action: string; available: boolean; expected_delay_days: number; cost_usd: number; note: string;
}
export interface Decision {
  decision_id: number;
  risk_level: "low" | "medium" | "high" | "critical";
  risk_score: number;
  recommended_action: string;
  recommended_action_label: string;
  estimated_cost_usd: number | null;
  rationale: string;
  citations: string[];
  human_decision: "pending" | "approved" | "overridden";
  human_notes: string | null;
  title: string;
  model: string;
  exposure: { options: ExposureOption[]; force_majeure_covered: boolean | null; force_majeure_notice_due: string | null; assumptions: string[] } | null;
  traffic: { name: string; avg_7d: number; baseline_avg: number; pct_change: number | null } | null;
  corroboration: { n_reports: number; n_publishers: number; confidence: number; headlines: { title: string; source: string; url: string }[] } | null;
  match_context: { match_reason: string; matched_port_id: string; days_until_eta: number } | null;
}
export interface Shipment {
  shipment_id: string; bl_number: string; pol: string; pol_locode: string; pod: string; pod_locode: string;
  transshipment: string[]; eta: string; etd: string | null; vessel_name: string | null; voyage_number: string | null;
  cargo_description: string | null; container_count: number | null; cargo_value_usd: number | null;
  incoterm: string | null; decisions: Decision[];
}

export interface EventRow {
  event_id: string; title: string; event_type: string; severity: string | null; location_name: string;
  n_signals: number; n_publishers: number; confidence: number; is_structured: number; last_seen: string;
}
export interface TrafficStat {
  location_id: string; name: string; metric: string; avg_7d: number; baseline_avg: number; pct_change: number | null;
}
export interface Draft {
  draft_id: number; decision_id: number; kind: string; subject: string; body: string;
  deadline_at: string | null; generated_by: string; status: string;
}
export interface BacktestScenario {
  scenario: string; event: string; matched: boolean; match_reason: string | null;
  warning_days_before_eta: number | null; traffic_change_pct: number | null;
  cheapest_action: string; exposure_wait_usd: number; exposure_best_usd: number;
  recommended_action: string; summary?: string;
  traffic_series?: { date: string; daily: number; "7-day average": number }[];
  traffic_location?: string; traffic_metric?: string; event_start?: string;
}
export interface SignalRow {
  published_at: string | null; source: string; event_type: string; severity: string | null;
  location_name: string | null; title: string; source_url: string;
}

export const api = {
  kpis: () => get<Kpis>("/api/kpis"),
  map: () => get<MapData>("/api/map"),
  shipments: () => get<Shipment[]>("/api/shipments"),
  events: () => get<EventRow[]>("/api/events"),
  eventSignals: (id: string) => get<Record<string, unknown>[]>(`/api/events/${id}/signals`),
  traffic: () => get<TrafficStat[]>("/api/traffic"),
  trafficDaily: (locationId: string) => get<{ date: string; value: number }[]>(`/api/traffic/${locationId}/daily`),
  backtest: () => get<{ scenarios: BacktestScenario[]; note?: string }>("/api/backtest"),
  signals: (params: { source?: string; event_type?: string; q?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)]))
    ).toString();
    return get<SignalRow[]>(`/api/signals${qs ? `?${qs}` : ""}`);
  },
  reviewDecision: (id: number, human_decision: "approved" | "overridden", human_notes?: string) =>
    send<{ ok: true }>("POST", `/api/decisions/${id}/review`, { human_decision, human_notes }),
  drafts: (decisionId: number) => get<Draft[]>(`/api/decisions/${decisionId}/drafts`),
  updateDraft: (id: number, body: Partial<Pick<Draft, "subject" | "body" | "status">>) =>
    send<{ ok: true }>("PATCH", `/api/drafts/${id}`, body),
  ask: (question: string) => send<{ answer: string }>("POST", "/api/ask", { question }),
};

export const ACTIONS: Record<string, string> = {
  monitor: "Monitor", notify_customer: "Notify customer", reroute: "Reroute",
  expedite: "Expedite", hold_cargo: "Hold cargo", invoke_force_majeure: "Invoke force majeure",
};

export const RISK_TONE: Record<string, string> = { critical: "risk", high: "warn", medium: "warn", low: "ok" };

export function fmtDate(iso: string, withYear = true): string {
  const d = new Date(iso);
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return withYear ? `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`
                  : `${d.getUTCDate()} ${months[d.getUTCMonth()]}`;
}

export function fmtMoney(n: number): string {
  // toLocaleString() with no locale arg uses the BROWSER's locale — e.g. French formats
  // 32500 as "32 500" (space-separated) instead of "32,500". Pinning "en-US" here keeps
  // dollar figures consistent regardless of the viewer's browser/OS locale (same class of
  // bug as the earlier Streamlit strftime("%b") issue that produced French month names).
  return Math.round(n).toLocaleString("en-US");
}

export function hoursUntil(iso: string | null): number | null {
  if (!iso) return null;
  return (new Date(iso).getTime() - Date.now()) / 3_600_000;
}

export function fmtCountdown(hrs: number | null): string {
  if (hrs === null) return "—";
  if (hrs < 0) return "Overdue";
  let h = Math.floor(hrs);
  let m = Math.round((hrs - h) * 60);
  if (m === 60) { h += 1; m = 0; }
  return `${h}h ${String(m).padStart(2, "0")}m`;
}
