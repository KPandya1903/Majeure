import { useEffect, useRef, useState } from "react";
import Map, { Source, Layer, NavigationControl, Marker } from "react-map-gl/maplibre";
import type { MapRef } from "react-map-gl/maplibre";
import { setWorkerUrl } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre's tile-parsing Web Worker is a prebuilt file inside the package, not something
// Vite's static `new Worker(new URL(...))` bundling can discover by scanning our own source —
// without pointing it at this explicitly, the map canvas renders but stays blank (no tiles),
// which only shows up in a production build (Vite's dev server serves node_modules directly,
// so this masks the problem locally).
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import type { ChokePoint, MapData, PortPoint, Route } from "./api";
import { api, ACTIONS, RISK_TONE, fmtDate } from "./api";
import { Chip } from "./components";

setWorkerUrl(maplibreWorkerUrl);

// Same no-labels Carto Voyager style used by the (legacy) Streamlit map — light-blue ocean,
// not greyscale, and no place-name labels (which otherwise render in the browser's language).
const MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-nolabels-gl-style/style.json";

// Four distinct dot colours, matching the chip ramp. Measured perceptual distance between adjacent
// steps is >30 deltaE, so they stay separable as small dots on a pale basemap.
// Categorical palette for carriers. Deliberately not the severity ramp: a vessel now encodes two
// independent things at once -- WHO operates it (the arrow's colour) and HOW exposed it is (the
// ring). Reusing red/amber/green for carriers would make a blue-ringed Maersk ship look "safe".
// Eleven hues, each dark enough to read against the pale land and sea of the basemap.
const CARRIER_PALETTE = [
  "#1F77B4", "#D9541E", "#2CA02C", "#9467BD", "#8C564B", "#17A2B8",
  "#C5283D", "#BC8B00", "#E377C2", "#4B6584", "#7F7F7F",
];

/** Stable colour per carrier: index by position in the sorted carrier list, so a carrier keeps its
 *  colour between refreshes instead of shuffling whenever the fleet changes size. */
function carrierColors(carriers: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  [...carriers].sort().forEach((c, i) => { out[c] = CARRIER_PALETTE[i % CARRIER_PALETTE.length]; });
  return out;
}

const SEVERITY_COLOR: Record<string, string> =
  { critical: "#D62828", high: "#E2711D", medium: "#C9A227", low: "#237A45" };

/** One vessel at its estimated position. The position itself comes from the backend (elapsed
 * ETD->ETA fraction along the real sea route — a scheduling estimate, not AIS), but a new
 * value only arrives when the map data is refetched, which would make the icon jump. This
 * tweens between the last two known positions the way a ride-hailing app smooths between GPS
 * pings: nothing is invented, the motion just interpolates values we already have. */
function ShipMarker({ route, active, onOpen, onClose, center, color, dimmed, highlighted }:
  { route: Route; active: boolean; onOpen: () => void; onClose: () => void;
    center: { lng: number; lat: number }; color: string; dimmed: boolean; highlighted: boolean }) {
  const size = active ? 28 : highlighted ? 25 : 20;
  const [pos, setPos] = useState<[number, number] | null>(route.position);
  const fromRef = useRef<[number, number] | null>(route.position);
  const rafRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    const target = route.position;
    const from = fromRef.current;
    if (!target) return;
    if (!from) { setPos(target); fromRef.current = target; return; }
    const dist = Math.hypot(target[0] - from[0], target[1] - from[1]);
    if (dist < 1e-6) return;                       // nothing moved; don't spin up a frame loop
    const start = performance.now();
    const DURATION = 1200;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      const ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      setPos([from[0] + (target[0] - from[0]) * ease, from[1] + (target[1] - from[1]) * ease]);
      if (t < 1) rafRef.current = requestAnimationFrame(step);
      else fromRef.current = target;
    };
    rafRef.current = requestAnimationFrame(step);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [route.position]);

  if (!pos) return null;
  // Arrow = carrier, ring = risk. A ship with no event on its route gets a ring in its own
  // carrier colour rather than a misleading green.
  const ring = route.risk_level ? SEVERITY_COLOR[route.risk_level] ?? color : color;
  // Flip the card towards the middle of the map so a vessel near an edge does not get its details
  // clipped by the map frame. Cheap approximation from the viewport centre, which is accurate
  // enough at these zoom levels and avoids projecting on every render.
  const flipX = pos[0] > center.lng;
  const flipY = pos[1] > center.lat;
  return (
    <Marker longitude={pos[0]} latitude={pos[1]} anchor="center" style={{ zIndex: active ? 10 : 1 }}>
      {/* The card is a DOM child of this hover container, so moving the pointer onto the card does
          not fire mouseleave here -- only leaving both does. The close delay in the parent covers
          the few pixels of gap between the icon and the card. */}
      <div style={{ position: "relative", lineHeight: 0, opacity: dimmed ? 0.28 : 1,
                    transition: "opacity 180ms ease-out" }}
           onMouseEnter={onOpen} onMouseLeave={onClose}>
        {/* No `title` attribute: the OS tooltip fires on the same hover as the card, drawing a
            second, uglier label in the corner of the screen that says less than the card does. */}
        <div
          aria-label={`${route.vessel_name ?? route.shipment_id}, ${route.carrier ?? "unknown carrier"}`}
          style={{ cursor: "pointer", transform: `rotate(${route.bearing}deg)`,
                   transition: "transform 400ms ease-out" }}
        >
          {/* Simple hull outline pointing "up" (north) at 0deg, rotated to the route bearing. */}
          <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="11" fill="#fff" stroke={ring} strokeWidth={dimmed ? 1.5 : 2.5} />
            <path d="M12 4 L16.5 17 L12 14.5 L7.5 17 Z" fill={color} />
          </svg>
        </div>
        {active && (
          <div className="pg-card pg-ship-pop"
               style={{ [flipX ? "right" : "left"]: 20, [flipY ? "top" : "bottom"]: 20 } as React.CSSProperties}>
            <div className="pg-eyebrow">{route.carrier}</div>
            <div style={{ fontWeight: 700, fontSize: 15, marginTop: 2 }}>
              {route.vessel_name ?? route.shipment_id}
            </div>
            <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 4 }}>
              {route.label.split(": ")[1]}
            </div>
            {route.cargo_description && (
              <div style={{ fontSize: 12.5, marginTop: 6 }}>{route.cargo_description}</div>
            )}
            <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
              <Chip text={`${Math.round(route.progress * 100)}% sailed`} tone="info" />
              {route.risk_level
                ? <Chip text={route.risk_level.toUpperCase()} tone={RISK_TONE[route.risk_level]} />
                : <Chip text="CLEAR" tone="ok" />}
            </div>
            <div style={{ fontSize: 11.5, color: "var(--pg-faint)", marginTop: 8, lineHeight: 1.45 }}>
              ETA {fmtDate(route.eta)}. Position estimated from voyage timing along the sea route —
              not live AIS.
            </div>
          </div>
        )}
      </div>
    </Marker>
  );
}

function toLineFeatures(data: MapData) {
  return {
    type: "FeatureCollection" as const,
    features: data.routes.map((r) => ({
      type: "Feature" as const,
      properties: { label: r.label },
      geometry: { type: "LineString" as const, coordinates: r.sailed },
    })),
  };
}
function toDotFeatures(points: [number, number][]) {
  return {
    type: "FeatureCollection" as const,
    features: points.map((p) => ({ type: "Feature" as const, properties: {}, geometry: { type: "Point" as const, coordinates: p } })),
  };
}
function toEventFeatures(data: MapData) {
  return {
    type: "FeatureCollection" as const,
    features: data.events.map((e) => ({
      type: "Feature" as const,
      properties: { color: SEVERITY_COLOR[e.severity] ?? "#B4661B", radius: Math.min(60, Math.max(10, e.radius / 6000)) },
      geometry: { type: "Point" as const, coordinates: [e.lon, e.lat] },
    })),
  };
}
/** GeoJSON points that KEEP their label. `properties: {}` was throwing away the name and locode
 *  the API already sends, which is why a port dot had nothing to show on hover. */
function toPointFeatures(points: { lon: number; lat: number }[], label?: (p: never) => string) {
  return {
    type: "FeatureCollection" as const,
    features: points.map((p) => ({
      type: "Feature" as const,
      properties: label ? { label: label(p as never) } : {},
      geometry: { type: "Point" as const, coordinates: [p.lon, p.lat] },
    })),
  };
}

/** "Rotterdam · NLRTM" — the locode is what a shipping person actually recognises. */
function portLabel(p: PortPoint): string {
  const code = (p.locode || "").trim();
  return code && code !== p.name ? `${p.name} · ${code}` : p.name;
}

/** "Suez Canal · transits −8% vs baseline" — the measured number, where we have one. */
function chokepointLabel(c: ChokePoint): string {
  return c.pct_change == null ? c.name
    : `${c.name} · transits ${c.pct_change > 0 ? "+" : ""}${Math.round(c.pct_change)}% vs baseline`;
}

export default function RiskMap() {
  const [data, setData] = useState<MapData | null>(null);
  const [hover, setHover] = useState<{ label: string; x: number; y: number } | null>(null);
  const [hoveredShip, setHoveredShip] = useState<string | null>(null);
  // Viewport centre, used only to decide which side of a vessel its card opens on. Tracked on
  // moveEnd rather than move: the flip is re-evaluated when the card next opens, so updating it on
  // every animation frame of a pan would re-render every marker for no visible benefit.
  const [center, setCenter] = useState({ lng: 45, lat: 22 });
  // Which carrier's fleet is picked out. null = show every vessel at full strength.
  const [carrier, setCarrier] = useState<string | null>(null);
  // Smallest zoom at which ONE copy of the world still fills the container. Below it MapLibre
  // tiles the globe horizontally and you see Africa twice, which reads as a rendering fault.
  // Turning `renderWorldCopies` off is not the fix: routing.unwrap_lons deliberately pushes
  // trans-Pacific routes (Busan -> Long Beach) past ±180 so they draw as one continuous line, and
  // those segments only exist in the adjacent copy. Constrain the zoom instead of removing them.
  const [minZoom, setMinZoom] = useState(1.2);
  const frameRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapRef | null>(null);
  const fitted = useRef(false);

  useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const fit = () => {
      // MapLibre draws the world 512 CSS px wide at zoom 0 and doubles per level, so this is the
      // zoom at which exactly one world spans the container. At it the globe fills the viewport
      // edge to edge and the neighbouring copies fall just outside it -- whole world visible, no
      // duplicate continents. The epsilon covers sub-pixel rounding that would otherwise leak a
      // one-pixel sliver of the next copy.
      const z = Math.max(0, Math.log2(Math.max(1, el.clientWidth) / 512) + 0.01);
      setMinZoom(z);
      const map = mapRef.current;
      if (!map) return;
      // Snap to the fitted zoom on first measure; afterwards only push back if a resize has made
      // the current zoom too low, so a user who has zoomed in is not yanked back out.
      if (!fitted.current) {
        fitted.current = true;
        map.jumpTo({ zoom: z, center: [20, 25] });
      } else if (map.getZoom() < z) {
        map.jumpTo({ zoom: z });
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Crossing the gap between the vessel icon and its card briefly leaves both elements, which
  // would otherwise close the card the moment you reached for it. A short grace period makes the
  // card reachable; entering it cancels the close outright.
  const openShip = (id: string) => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setHoveredShip(id);
  };
  const closeShip = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setHoveredShip(null), 140);
  };
  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);

  useEffect(() => {
    const load = () => api.map().then(setData).catch((e) => console.error("map fetch failed", e));
    load();
    // Positions are a function of elapsed ETD->ETA time, so refetching advances them. On a real
    // voyage that's millimetres per minute — the point is that it's live and self-correcting,
    // not that you can watch it crawl.
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, []);

  if (!data) return <div className="pg-card pg-empty">Loading map…</div>;

  const remainingDots = data.routes.flatMap((r) => r.remaining_dots);

  // Carrier -> vessel count, biggest fleet first, and a stable colour for each.
  // A plain record rather than a `Map`: react-map-gl's default export is named Map and shadows
  // the global here, so `new Map()` resolves to the React component.
  const counts: Record<string, number> = {};
  data.routes.forEach((r) => {
    const c = r.carrier ?? "Unknown";
    counts[c] = (counts[c] ?? 0) + 1;
  });
  const fleets = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const colors = carrierColors(Object.keys(counts));

  return (
    <div className="pg-card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ position: "relative" }} ref={frameRef}>
      <Map
        ref={mapRef}
        initialViewState={{ longitude: 20, latitude: 25, zoom: 1.2 }}
        minZoom={minZoom}
        style={{ width: "100%", height: 560 }}
        mapStyle={MAP_STYLE}
        // Trackpad two-finger vertical scroll otherwise zooms the map instead of scrolling the
        // page underneath it — disable that specifically. Pinch (two fingers apart/together,
        // touchZoomRotate) and the +/- NavigationControl buttons stay on as the only zoom paths.
        onMoveEnd={(e) => setCenter({ lng: e.viewState.longitude, lat: e.viewState.latitude })}
        scrollZoom={false}
        touchZoomRotate={true}
        onMouseMove={(e) => {
          const f = e.features?.[0];
          const label = f ? (f.properties?.label as string) ?? null : null;
          setHover(label ? { label, x: e.point.x, y: e.point.y } : null);
        }}
        onMouseLeave={() => setHover(null)}
        // Ports and chokepoints are hoverable too, not just the route lines. Order matters: the
        // first match wins, so the small precise dots are listed ahead of the big event halos,
        // which would otherwise swallow every port sitting inside one.
        interactiveLayerIds={["ports-dot", "chokepoints-dot", "events-dot", "routes-line"]}
      >
        <NavigationControl position="top-right" />

        {/* Painted bottom to top. The route lines go FIRST so the port and chokepoint dots
            sit above them: a line drawn over a dot also captures its hover, and routes pass
            straight through the chokepoints people most want to inspect. */}
        <Source id="routes" type="geojson" data={toLineFeatures(data)}>
          <Layer id="routes-line" type="line"
            paint={{ "line-color": "#0B5CD5", "line-width": 2.4, "line-opacity": 0.9 }}
            layout={{ "line-cap": "round", "line-join": "round" }} />
        </Source>
        <Source id="remaining" type="geojson" data={toDotFeatures(remainingDots)}>
          <Layer id="remaining-dot" type="circle" paint={{ "circle-radius": 1.5, "circle-color": "#0B5CD5", "circle-opacity": 0.55 }} />
        </Source>
        <Source id="events" type="geojson" data={toEventFeatures(data)}>
          <Layer id="events-halo" type="circle"
            paint={{ "circle-radius": ["get", "radius"], "circle-color": ["get", "color"], "circle-opacity": 0.18 }} />
          <Layer id="events-dot" type="circle"
            paint={{ "circle-radius": 6, "circle-color": ["get", "color"], "circle-stroke-width": 2, "circle-stroke-color": "#fff" }} />
        </Source>
        <Source id="chokepoints" type="geojson"
                data={toPointFeatures(data.chokepoints, (c) => chokepointLabel(c))}>
          <Layer id="chokepoints-dot" type="circle" paint={{ "circle-radius": 3.5, "circle-color": "#6B7581", "circle-opacity": 0.65 }} />
        </Source>
        <Source id="ports" type="geojson" data={toPointFeatures(data.ports, (p) => portLabel(p))}>
          <Layer id="ports-dot" type="circle"
            paint={{ "circle-radius": 4, "circle-color": "#2B333C", "circle-stroke-width": 2, "circle-stroke-color": "#fff" }} />
        </Source>

        {data.routes.map((r) => (
          <ShipMarker key={r.shipment_id} route={r} center={center}
            active={r.shipment_id === hoveredShip}
            color={colors[r.carrier ?? ""] ?? "#0B5CD5"}
            dimmed={carrier !== null && r.carrier !== carrier}
            highlighted={carrier !== null && r.carrier === carrier}
            onOpen={() => openShip(r.shipment_id)} onClose={closeShip} />
        ))}
      </Map>

      {hover && (
        <div style={{ position: "absolute", pointerEvents: "none", background: "#fff",
                      border: "1px solid var(--pg-line)", borderRadius: 6, padding: "5px 9px",
                      fontSize: 12, whiteSpace: "nowrap", boxShadow: "0 4px 12px rgba(0,0,0,0.14)",
                      left: hover.x + 14, top: hover.y - 10 }}>
          {hover.label}
        </div>
      )}

        {/* Carrier index, inside the map at the bottom-right. Clicking a line picks out its
            vessels and fades the rest; clicking it again clears the filter. */}
        <div className="pg-carrier-index">
          <div className="pg-carrier-index-head">
            <span>Carriers</span>
            {carrier && (
              <button className="pg-carrier-clear" onClick={() => setCarrier(null)}>clear</button>
            )}
          </div>
          <div className="pg-carrier-list">
            {fleets.map(([name, count]) => {
              const on = carrier === name;
              return (
                <button key={name} className={`pg-carrier-row${on ? " on" : ""}`}
                        aria-pressed={on}
                        onClick={() => setCarrier(on ? null : name)}>
                  <span className="pg-carrier-dot" style={{ background: colors[name] }} />
                  <span className="pg-carrier-name">{name}</span>
                  <span className="pg-carrier-count">{count}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="pg-legend" style={{ padding: "10px 14px" }}>
        <span><span className="dot" style={{ background: "var(--pg-blue)" }} />Sailed / remaining route</span>
        <span><span className="dot" style={{ background: "#D62828" }} />Critical</span>
        <span><span className="dot" style={{ background: "#E2711D" }} />High</span>
        <span><span className="dot" style={{ background: "#C9A227" }} />Medium</span>
        <span><span className="dot" style={{ background: "#237A45" }} />Low</span>
        <span><span className="dot" style={{ background: "#2B333C" }} />Port</span>
      </div>
      <div style={{ padding: "0 14px 14px", fontSize: 12.5, color: "var(--pg-muted)" }}>
        Halos: clustered events (size = reports, colour = severity). Blue line: real sea routes
        (via a maritime shipping-lane network) — solid where sailed, dotted where still ahead.
        Arrows: each vessel's estimated position from voyage timing (not live AIS) — hover one
        for details.
        {data.routes.length > 0 &&
          ` ${data.routes.length} shipment${data.routes.length === 1 ? "" : "s"} tracked.`}
      </div>
    </div>
  );
}

export { ACTIONS };
