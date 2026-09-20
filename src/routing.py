"""Shipment route geometry — shared by the Streamlit dashboard and the FastAPI backend/React
map, so both render the exact same real sea routes (not straight lines that cut across land).

`coords` throughout is `{port_id_or_chokepoint_id: (lat, lon, name, locode)}`, as built by
`dashboard.py`'s / the API's `location_coords()`.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from functools import lru_cache

import searoute as sr


@lru_cache(maxsize=2048)
def sea_leg(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    """Real maritime route between two (lon, lat) points, via `searoute`'s offline shipping-lane
    network (hugs real sea lanes) — not a straight cartesian line, which on a real basemap can
    visibly cut across land. Cached process-wide (routes are static; ETD/ETA progress is applied
    on top by `split_path`, not baked into this)."""
    try:
        route = sr.searoute(list(a), list(b), units="naut", append_orig_dest=True)
        pts = route["geometry"]["coordinates"]
        if len(pts) >= 2:
            return tuple((float(lon), float(lat)) for lon, lat in pts)
    except Exception:
        pass
    return (a, b)  # unreachable in the sea-lane graph (shouldn't happen for real ports)


def route_path(pol_port_id: str, pod_port_id: str, transshipment_port_ids: list[str],
                route_chokepoints: list[str], coords: dict) -> list[list[float]]:
    """POL -> intermediate stops (nearest-first) -> POD, as a real sea route, as [lon, lat] points."""
    stops = [p for p in transshipment_port_ids + route_chokepoints if p in coords]
    here, waypoints = pol_port_id, [pol_port_id]
    while stops:
        nxt = min(stops, key=lambda p: (coords[p][0] - coords[here][0]) ** 2 + (coords[p][1] - coords[here][1]) ** 2)
        stops.remove(nxt)
        waypoints.append(nxt)
        here = nxt
    waypoints.append(pod_port_id)

    path: list[list[float]] = []
    for a, b in zip(waypoints, waypoints[1:]):
        lat1, lon1, _, _ = coords[a]
        lat2, lon2, _, _ = coords[b]
        leg = [list(p) for p in sea_leg((lon1, lat1), (lon2, lat2))]
        path.extend(leg if not path else leg[1:])  # drop duplicate join point
    return path


def unwrap_lons(path: list[list[float]]) -> list[list[float]]:
    """Keep longitude continuous across the antimeridian (e.g. Busan -> Long Beach)."""
    out = [list(path[0])]
    for lon, lat in path[1:]:
        prev = out[-1][0]
        while lon - prev > 180:
            lon -= 360
        while lon - prev < -180:
            lon += 360
        out.append([lon, lat])
    return out


def route_progress(etd: str | None, eta: str | None) -> float:
    """Fraction of the voyage elapsed, from ETD/ETA, clamped to [0, 1]."""
    try:
        etd_dt = datetime.fromisoformat(etd.replace("Z", "+00:00"))
        eta_dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
        if eta_dt <= etd_dt:
            return 0.0
        now = datetime.now(timezone.utc)
        return min(1.0, max(0.0, (now - etd_dt).total_seconds() / (eta_dt - etd_dt).total_seconds()))
    except Exception:
        return 0.0


def split_path(path: list[list[float]], frac: float) -> tuple[list[list[float]], list[list[float]]]:
    """(sailed, remaining) split of `path` at fraction `frac` of its total length."""
    if len(path) < 2:
        return list(path), list(path)
    seg_lens = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg_lens)
    if total == 0 or frac <= 0:
        return [path[0]], list(path)
    if frac >= 1:
        return list(path), [path[-1]]
    target, acc = frac * total, 0.0
    for i, seg in enumerate(seg_lens):
        if acc + seg >= target:
            t = (target - acc) / seg if seg else 0.0
            a, b = path[i], path[i + 1]
            pt = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            return path[:i + 1] + [pt], [pt] + path[i + 1:]
        acc += seg
    return list(path), [path[-1]]


def dotted_points(path: list[list[float]], n: int = 10) -> list[list[float]]:
    """`n` evenly spaced points along `path`, used to draw a route as a dashed/dotted trail
    (deck.gl/MapLibre have no native dash-along-a-geodesic primitive that survives reprojection
    as cleanly as a plain point string)."""
    if len(path) < 2:
        return []
    seg_lens = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg_lens)
    if total == 0:
        return []
    pts, acc, seg_i = [], 0.0, 0
    for k in range(n + 1):
        target = total * k / n
        while seg_i < len(seg_lens) - 1 and acc + seg_lens[seg_i] < target:
            acc += seg_lens[seg_i]
            seg_i += 1
        seg = seg_lens[seg_i]
        t = (target - acc) / seg if seg else 0.0
        a, b = path[seg_i], path[seg_i + 1]
        pts.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    return pts


def dot_count(path: list[list[float]], per_unit: float = 1.1, lo: int = 5, hi: int = 40) -> int:
    """Dash density scaled to a leg's length, so short hops don't get one giant dash."""
    if len(path) < 2:
        return lo
    length = sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))
    return max(lo, min(hi, round(length * per_unit)))
