import { useEffect, useRef, useState } from "react";
import Map, { Source, Layer, NavigationControl, Marker } from "react-map-gl/maplibre";
import { setWorkerUrl } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre's tile-parsing Web Worker is a prebuilt file inside the package, not something
// Vite's static `new Worker(new URL(...))` bundling can discover by scanning our own source —
// without pointing it at this explicitly, the map canvas renders but stays blank (no tiles),
// which only shows up in a production build (Vite's dev server serves node_modules directly,
// so this masks the problem locally).
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import type { MapData, Route } from "./api";
import { api, ACTIONS, RISK_TONE, fmtDate } from "./api";
import { Chip } from "./components";

setWorkerUrl(maplibreWorkerUrl);

// Same no-labels Carto Voyager style used by the (legacy) Streamlit map — light-blue ocean,
// not greyscale, and no place-name labels (which otherwise render in the browser's language).
const MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-nolabels-gl-style/style.json";

const SEVERITY_COLOR: Record<string, string> = { critical: "#D62828", high: "#B4661B", medium: "#B4661B", low: "#237A45" };

/** One vessel at its estimated position. The position itself comes from the backend (elapsed
 * ETD->ETA fraction along the real sea route — a scheduling estimate, not AIS), but a new
 * value only arrives when the map data is refetched, which would make the icon jump. This
 * tweens between the last two known positions the way a ride-hailing app smooths between GPS
 * pings: nothing is invented, the motion just interpolates values we already have. */
function ShipMarker({ route, onSelect, selected }:
  { route: Route; onSelect: () => void; selected: boolean }) {
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
  const tone = route.risk_level ? SEVERITY_COLOR[route.risk_level] ?? "#0B5CD5" : "#0B5CD5";
  return (
    <Marker longitude={pos[0]} latitude={pos[1]} anchor="center" onClick={onSelect}>
      <div
        title={`${route.vessel_name ?? route.shipment_id} · ${route.carrier ?? ""}`}
        style={{ cursor: "pointer", transform: `rotate(${route.bearing}deg)`, transition: "transform 400ms ease-out" }}
      >
        {/* Simple hull outline pointing "up" (north) at 0deg, rotated to the route bearing. */}
        <svg width={selected ? 26 : 20} height={selected ? 26 : 20} viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="11" fill="#fff" stroke={tone} strokeWidth="2" />
          <path d="M12 4 L16.5 17 L12 14.5 L7.5 17 Z" fill={tone} />
        </svg>
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
function toPointFeatures(points: { lon: number; lat: number }[]) {
  return {
    type: "FeatureCollection" as const,
    features: points.map((p) => ({ type: "Feature" as const, properties: {}, geometry: { type: "Point" as const, coordinates: [p.lon, p.lat] } })),
  };
}

export default function RiskMap() {
  const [data, setData] = useState<MapData | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [selectedShip, setSelectedShip] = useState<string | null>(null);

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

  const ship = data.routes.find((r) => r.shipment_id === selectedShip) ?? null;

  const remainingDots = data.routes.flatMap((r) => r.remaining_dots);

  return (
    <div className="pg-card" style={{ padding: 0, overflow: "hidden" }}>
      <Map
        initialViewState={{ longitude: 45, latitude: 22, zoom: 1.6 }}
        style={{ width: "100%", height: 560 }}
        mapStyle={MAP_STYLE}
        // Trackpad two-finger vertical scroll otherwise zooms the map instead of scrolling the
        // page underneath it — disable that specifically. Pinch (two fingers apart/together,
        // touchZoomRotate) and the +/- NavigationControl buttons stay on as the only zoom paths.
        scrollZoom={false}
        touchZoomRotate={true}
        onMouseMove={(e) => {
          const f = e.features?.[0];
          setHover(f ? (f.properties?.label as string) ?? null : null);
        }}
        interactiveLayerIds={["routes-line"]}
      >
        <NavigationControl position="top-right" />

        <Source id="chokepoints" type="geojson" data={toPointFeatures(data.chokepoints)}>
          <Layer id="chokepoints-dot" type="circle" paint={{ "circle-radius": 3.5, "circle-color": "#6B7581", "circle-opacity": 0.65 }} />
        </Source>

        <Source id="events" type="geojson" data={toEventFeatures(data)}>
          <Layer id="events-halo" type="circle"
            paint={{ "circle-radius": ["get", "radius"], "circle-color": ["get", "color"], "circle-opacity": 0.18 }} />
          <Layer id="events-dot" type="circle"
            paint={{ "circle-radius": 6, "circle-color": ["get", "color"], "circle-stroke-width": 2, "circle-stroke-color": "#fff" }} />
        </Source>

        <Source id="ports" type="geojson" data={toPointFeatures(data.ports)}>
          <Layer id="ports-dot" type="circle"
            paint={{ "circle-radius": 4, "circle-color": "#2B333C", "circle-stroke-width": 2, "circle-stroke-color": "#fff" }} />
        </Source>

        <Source id="routes" type="geojson" data={toLineFeatures(data)}>
          <Layer id="routes-line" type="line"
            paint={{ "line-color": "#0B5CD5", "line-width": 2.4, "line-opacity": 0.9 }}
            layout={{ "line-cap": "round", "line-join": "round" }} />
        </Source>

        <Source id="remaining" type="geojson" data={toDotFeatures(remainingDots)}>
          <Layer id="remaining-dot" type="circle" paint={{ "circle-radius": 1.5, "circle-color": "#0B5CD5", "circle-opacity": 0.55 }} />
        </Source>

        {data.routes.map((r) => (
          <ShipMarker key={r.shipment_id} route={r} selected={r.shipment_id === selectedShip}
            onSelect={() => setSelectedShip(r.shipment_id === selectedShip ? null : r.shipment_id)} />
        ))}
      </Map>

      {ship && (
        <div className="pg-card" style={{ position: "absolute", top: 12, right: 12, width: 260, padding: "12px 14px",
                                          boxShadow: "0 6px 20px rgba(43,51,60,0.16)" }}>
          <div className="pg-eyebrow">{ship.carrier}</div>
          <div style={{ fontWeight: 700, fontSize: 15, marginTop: 2 }}>{ship.vessel_name ?? ship.shipment_id}</div>
          <div style={{ fontSize: 12.5, color: "var(--pg-muted)", marginTop: 4 }}>{ship.label.split(": ")[1]}</div>
          {ship.cargo_description && (
            <div style={{ fontSize: 12.5, marginTop: 6 }}>{ship.cargo_description}</div>
          )}
          <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            <Chip text={`${Math.round(ship.progress * 100)}% sailed`} tone="info" />
            {ship.risk_level
              ? <Chip text={ship.risk_level.toUpperCase()} tone={RISK_TONE[ship.risk_level]} />
              : <Chip text="CLEAR" tone="ok" />}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--pg-faint)", marginTop: 8, lineHeight: 1.45 }}>
            ETA {fmtDate(ship.eta)}. Position estimated from voyage timing along the sea route —
            not live AIS.
          </div>
        </div>
      )}

      {hover && (
        <div style={{ position: "absolute", pointerEvents: "none", background: "#fff", border: "1px solid var(--pg-line)",
                     borderRadius: 6, padding: "4px 8px", fontSize: 12, top: 8, left: 8, boxShadow: "0 4px 12px rgba(0,0,0,0.1)" }}>
          {hover}
        </div>
      )}

      <div className="pg-legend" style={{ padding: "10px 14px" }}>
        <span><span className="dot" style={{ background: "var(--pg-blue)" }} />Sailed / remaining route</span>
        <span><span className="dot" style={{ background: "#D62828" }} />Critical</span>
        <span><span className="dot" style={{ background: "#B4661B" }} />High / medium</span>
        <span><span className="dot" style={{ background: "#237A45" }} />Low</span>
        <span><span className="dot" style={{ background: "#2B333C" }} />Port</span>
      </div>
      <div style={{ padding: "0 14px 14px", fontSize: 12.5, color: "var(--pg-muted)" }}>
        Halos: clustered events (size = reports, colour = severity). Blue line: real sea routes
        (via a maritime shipping-lane network) — solid where sailed, dotted where still ahead.
        Arrows: each vessel's estimated position from voyage timing (not live AIS) — click one
        for details.
        {data.routes.length > 0 &&
          ` ${data.routes.length} shipment${data.routes.length === 1 ? "" : "s"} tracked.`}
      </div>
    </div>
  );
}

export { ACTIONS };
