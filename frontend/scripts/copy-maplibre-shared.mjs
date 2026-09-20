// MapLibre's prebuilt tile-parsing worker (dist/maplibre-gl-worker.mjs, loaded via Vite's `?url`
// in src/RiskMap.tsx) does a relative import of a sibling `./maplibre-gl-shared.mjs` — a plain
// path baked into that prebuilt file, which Vite's `?url` asset copy has no reason to follow.
// Left alone, the worker 404s on that import at runtime and the map renders blank. Fix: copy the
// sibling into public/assets/ (same unhashed name Vite's dev/build server will serve it under,
// matching the worker's own relative path) on every install, so it can't drift out of sync with
// whatever maplibre-gl version is actually installed.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const src = join(root, "node_modules/maplibre-gl/dist/maplibre-gl-shared.mjs");
const destDir = join(root, "public/assets");
const dest = join(destDir, "maplibre-gl-shared.mjs");

mkdirSync(destDir, { recursive: true });
copyFileSync(src, dest);
console.log(`copied ${src} -> ${dest}`);
