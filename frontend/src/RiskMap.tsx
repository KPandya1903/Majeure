import { useEffect, useState } from "react";
import Map, { Source, Layer, NavigationControl } from "react-map-gl/maplibre";
import { setWorkerUrl } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre's tile-parsing Web Worker is a prebuilt file inside the package, not something
// Vite's static `new Worker(new URL(...))` bundling can discover by scanning our own source —
// without pointing it at this explicitly, the map canvas renders but stays blank (no tiles),
// which only shows up in a production build (Vite's dev server serves node_modules directly,
// so this masks the problem locally).
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import type { MapData } from "./api";
import { api, ACTIONS } from "./api";

setWorkerUrl(maplibreWorkerUrl);

// Same no-labels Carto Voyager style used by the (legacy) Streamlit map — light-blue ocean,
// not greyscale, and no place-name labels (which otherwise render in the browser's language).
const MAP_STYLE = "https://basemaps.cartocdn.com/gl/voyager-nolabels-gl-style/style.json";

const SEVERITY_COLOR: Record<string, string> = { critical: "#D62828", high: "#B4661B", medium: "#B4661B", low: "#237A45" };

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

  useEffect(() => {
    api.map().then(setData).catch((e) => console.error("map fetch failed", e));
  }, []);

  if (!data) return <div className="pg-card pg-empty">Loading map…</div>;

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
      </Map>

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
        {data.routes.length > 0 &&
          ` ${data.routes.length} shipment${data.routes.length === 1 ? "" : "s"} tracked.`}
      </div>
    </div>
  );
}

export { ACTIONS };
