"""Fetch first-party government warning feeds into Xtract Signals.

Four publishers, all primary sources rather than reporting about them, which is why they carry the
highest credibility tier in common.PUBLISHER_TIER:

* ``nhc``          NOAA National Hurricane Center advisories (Atlantic, East and Central Pacific).
* ``jtwc``         US Navy Joint Typhoon Warning Center (NW Pacific / North Indian Ocean).
* ``usgs``         USGS M4.5+ earthquakes, proximity-matched to ports the way GDACS alerts are.
* ``nga_warnings`` NGA NAVAREA broadcast warnings in force (navigational hazards, live-firing areas,
  piracy advisories). Note this feed carries warnings that are *in force*, not warnings that are
  *new*: the newest message NGA serves is from 2024, and in-force warnings legitimately persist for
  years. They are ingested for standing-hazard context and will not cluster into recent events
  (cluster_events.py only looks back 14 days). Do not read an old issue date here as a stale feed.

Output: data/official_signals.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional

import feedparser

from common import (DATA_DIR, PortGazetteer, as_sentence, canonical_url, classify_event, clean_text, http_get,
                    iso, load_ports, make_signal_id, nearby_ports, setup_logging, utcnow, write_signals)
from schemas import EventType, PortLocation, XtractSignal

SIGNALS_JSON = DATA_DIR / "official_signals.json"

NHC_FEEDS = {  # basin -> RSS of all current advisories for that basin
    "Atlantic": "https://www.nhc.noaa.gov/index-at.xml",
    "East Pacific": "https://www.nhc.noaa.gov/index-ep.xml",
    "Central Pacific": "https://www.nhc.noaa.gov/index-cp.xml",
}
JTWC_FEED = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"
USGS_FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
NGA_FEED = "https://msi.nga.mil/api/publications/broadcast-warn?status=active&output=json"
NGA_PERMALINK = "https://msi.nga.mil/NavWarnings?navArea={area}&msgYear={year}&msgNumber={number}"

# A quake only matters here if it could damage a port or close a berth; 300 km is the radius GDACS
# uses for the same question, kept identical so the two hazard sources stay comparable.
QUAKE_RADIUS_KM = 300.0
MAX_PORTS = 25
# NAVAREA warnings are mostly routine (drilling-rig position lists, buoy outages). Only warnings whose
# text names a port or chokepoint Majeure actually tracks are worth a signal; the rest are noise.
NGA_MIN_YEAR = 2020
# Much tighter than QUAKE_RADIUS_KM: a quake 300 km away can still shake a port, but a navigational
# warning concerns one specific piece of water. Beyond this the "nearest port" is a coincidence rather
# than a relationship -- at 300 km this feed produced 259 proximity matches, nearly all meaningless.
NGA_RADIUS_KM = 100.0
# In-force warnings accumulate for years, so the matched set is ~200 messages dating back to 2021.
# Ingesting all of them would swamp the signals table and bury live news in the all-signals view for
# no analytical gain, since none of them can cluster into a recent event. Keep the newest slice.
NGA_MAX_SIGNALS = 60

# "19-23.0N 092-03.1W" -> degrees + decimal minutes, the DTG coordinate format NAVAREA text uses.
_NGA_COORD_RE = re.compile(r"(\d{2})-(\d{2}(?:\.\d+)?)\s*([NS])\s+(\d{3})-(\d{2}(?:\.\d+)?)\s*([EW])")
# JTWC puts every storm's products in one RSS item; each storm has its own warning-text URL.
_JTWC_PRODUCT_RE = re.compile(
    r"""href=["'](https://www\.metoc\.navy\.mil/jtwc/products/[^"']+web\.txt)["']""", re.I)
# Capture only the text immediately inside <b>, stopping at the next tag rather than at </b>.
# JTWC's feed contains an UNCLOSED <b> ("<b>Issued at 20/0600Z<b>"), so a lazy `</b>` match runs on
# past it and swallows every link label until the next storm's heading -- producing titles like
# "Issued at 20/0600Z TCFA Text TCFA Graphic JMV 3.0 Data Google Earth Overlay IR Satellite
# Imagery Tropical Cyclone Formation Alert WTPN21". Those still passed the storm filter below,
# because the real heading was buried inside the garbage.
_JTWC_STORM_RE = re.compile(r"<b>\s*([^<]+)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

log = logging.getLogger("majeure.official")


# --------------------------------------------------------------------------- shared helpers


def _rss_datetime(entry) -> Optional[datetime]:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def _storm_severity(text: str) -> str:
    """Map a cyclone's own classification to Majeure severity. The publisher already graded it."""
    lowered = text.lower()
    if re.search(r"\b(?:super typhoon|category [45]|major hurricane)\b", lowered):
        return "critical"
    if re.search(r"\b(?:hurricane|typhoon|cyclone)\b", lowered):
        return "high"
    if "tropical storm" in lowered:
        return "medium"
    return "low"


def _proximity_location(ports, lat: Optional[float], lon: Optional[float], radius_km: float,
                        fallback_name: str) -> tuple[Optional[PortLocation], list[dict]]:
    """Nearest tracked port to a point, plus every port inside the radius (same shape as GDACS)."""
    if ports is None or lat is None or lon is None:
        return None, []
    near = nearby_ports(ports, lat, lon, radius_km, MAX_PORTS)
    if not len(near):
        return PortLocation(name=fallback_name, kind="region", lat=lat, lon=lon,
                            match_method="structured"), []
    first = near.iloc[0]
    location = PortLocation(name=first.port_name, kind="port", port_id=first.port_id, country=first.country,
                            lat=first.lat, lon=first.lon, match_method="proximity")
    nearby = [{"port_id": r.port_id, "name": r.port_name, "distance_km": float(r.distance_km)}
              for r in near.itertuples()]
    return location, nearby


# --------------------------------------------------------------------------- NOAA NHC


def nhc_signals(gazetteer: PortGazetteer, now: datetime) -> list[XtractSignal]:
    signals: list[XtractSignal] = []
    for basin, url in NHC_FEEDS.items():
        try:
            feed = feedparser.parse(http_get(url).content)
        except Exception as exc:  # one basin must not stop the others
            log.error("nhc %s failed: %s", basin, exc)
            continue
        for entry in feed.entries:
            title, link = clean_text(entry.get("title")), (entry.get("link") or "").strip()
            if not title or not link.startswith("http"):
                continue
            # The basin-wide feed always carries a "no active cyclones" placeholder; it is not an event.
            if re.search(r"there are no tropical cyclones|no tropical cyclone", title, re.I):
                continue
            summary = clean_text(entry.get("summary") or entry.get("description"))
            signals.append(XtractSignal(
                signal_id=make_signal_id(link), source="nhc", title=title, source_url=canonical_url(link),
                published_at=_rss_datetime(entry),
                port_location=gazetteer.match(title, summary),
                event_type=EventType.SEVERE_WEATHER, severity=_storm_severity(f"{title} {summary}"),
                raw_text=as_sentence(summary or title), ingested_at=now,
                metadata={"basin": basin, "publisher": "NOAA National Hurricane Center",
                          "guid": entry.get("id") or None},
            ))
    log.info("nhc: %d advisories", len(signals))
    return signals


# --------------------------------------------------------------------------- JTWC


def jtwc_signals(gazetteer: PortGazetteer, now: datetime) -> list[XtractSignal]:
    """One signal per active storm. JTWC bundles every storm into one RSS item, so the item is split
    on its per-storm product links -- using the item link alone would collide all storms into one id."""
    try:
        feed = feedparser.parse(http_get(JTWC_FEED).content)
    except Exception as exc:
        log.error("jtwc failed: %s", exc)
        return []
    signals: list[XtractSignal] = []
    for entry in feed.entries:
        description = entry.get("summary") or entry.get("description") or ""
        published = _rss_datetime(entry)
        # Storm headings and product links appear in document order, so zip pairs each storm with its own.
        storms = [clean_text(_TAG_RE.sub(" ", s)) for s in _JTWC_STORM_RE.findall(description)]
        storms = [s for s in storms if re.search(r"\b(?:typhoon|storm|depression|cyclone)\b", s, re.I)]
        links = _JTWC_PRODUCT_RE.findall(description)
        for storm, link in zip(storms, links):
            signals.append(XtractSignal(
                signal_id=make_signal_id(link), source="jtwc", title=storm,
                source_url=canonical_url(link), published_at=published,
                port_location=gazetteer.match(storm, clean_text(entry.get("title"))),
                event_type=EventType.SEVERE_WEATHER, severity=_storm_severity(storm),
                raw_text=as_sentence(f"{storm} -- {clean_text(entry.get('title'))}"), ingested_at=now,
                metadata={"publisher": "US Navy Joint Typhoon Warning Center", "product_url": link},
            ))
    log.info("jtwc: %d active storms", len(signals))
    return signals


# --------------------------------------------------------------------------- USGS


def _quake_severity(magnitude: float) -> str:
    if magnitude >= 7.0:
        return "critical"
    if magnitude >= 6.0:
        return "high"
    return "medium" if magnitude >= 5.0 else "low"


def usgs_signals(ports, now: datetime) -> list[XtractSignal]:
    """M4.5+ quakes that have at least one tracked port within QUAKE_RADIUS_KM. A quake in open ocean
    or deep inland cannot shut a berth, so it is dropped rather than logged as a maritime signal."""
    try:
        payload = http_get(USGS_FEED).json()
    except Exception as exc:
        log.error("usgs failed: %s", exc)
        return []
    signals, skipped = [], 0
    for feature in payload.get("features", []):
        props, geom = feature.get("properties") or {}, feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        link, magnitude = (props.get("url") or "").strip(), props.get("mag")
        if not link.startswith("http") or magnitude is None or len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        location, nearby = _proximity_location(ports, lat, lon, QUAKE_RADIUS_KM,
                                               clean_text(props.get("place")) or "Offshore")
        if not nearby:
            skipped += 1
            continue
        place = clean_text(props.get("place")) or f"{lat:.2f}, {lon:.2f}"
        title = f"M{magnitude:.1f} earthquake -- {place}"
        ports_text = ", ".join(f"{p['name']} ({p['distance_km']:.0f} km)" for p in nearby[:10])
        published = datetime.fromtimestamp(props["time"] / 1000, tz=timezone.utc) if props.get("time") else None
        signals.append(XtractSignal(
            signal_id=make_signal_id(link), source="usgs", title=title, source_url=canonical_url(link),
            published_at=published, port_location=location,
            event_type=EventType.NATURAL_DISASTER, severity=_quake_severity(float(magnitude)),
            raw_text=as_sentence(f"{title} at {coords[2] if len(coords) > 2 else 0:.0f} km depth. "
                                 f"Ports within {QUAKE_RADIUS_KM:.0f} km: {ports_text}"),
            ingested_at=now,
            metadata={"publisher": "US Geological Survey", "magnitude": float(magnitude),
                      "tsunami_flag": bool(props.get("tsunami")), "event_lat": lat, "event_lon": lon,
                      "nearby_ports": nearby, "radius_km": QUAKE_RADIUS_KM},
        ))
    log.info("usgs: %d quakes near tracked ports (%d with no port in range)", len(signals), skipped)
    return signals


# --------------------------------------------------------------------------- NGA NAVAREA


def _nga_coords(text: str) -> tuple[Optional[float], Optional[float]]:
    """First lat/lon in a NAVAREA message body, converted from degrees + decimal minutes."""
    if not (m := _NGA_COORD_RE.search(text)):
        return None, None
    lat = int(m.group(1)) + float(m.group(2)) / 60
    lon = int(m.group(4)) + float(m.group(5)) / 60
    return (-lat if m.group(3) == "S" else lat), (-lon if m.group(6) == "W" else lon)


def _nga_datetime(dtg: Optional[str]) -> Optional[datetime]:
    """NAVAREA date-time group, e.g. '081653Z MAY 2024' -> 2024-05-08T16:53Z."""
    if not dtg or not (m := re.match(r"(\d{2})(\d{2})(\d{2})Z\s+([A-Z]{3})\s+(\d{4})", dtg.strip(), re.I)):
        return None
    months = {mn: i for i, mn in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
    month = months.get(m.group(4).upper())
    if not month:
        return None
    try:
        return datetime(int(m.group(5)), month, int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def nga_signals(gazetteer: PortGazetteer, ports, now: datetime) -> list[XtractSignal]:
    try:
        payload = http_get(NGA_FEED).json()
    except Exception as exc:
        log.error("nga_warnings failed: %s", exc)
        return []
    signals, skipped = [], 0
    for item in payload.get("broadcast-warn", []):
        text = clean_text(item.get("text"))
        year, number, area = item.get("msgYear"), item.get("msgNumber"), item.get("navArea")
        if not text or not year or number is None or year < NGA_MIN_YEAR:
            continue
        # Only keep a warning that names somewhere Majeure tracks -- a rig-position list in an area
        # with no tracked port cannot affect any shipment, and there are hundreds of them.
        location = gazetteer.match(text)
        lat, lon = _nga_coords(text)
        if location is None:
            location, _ = _proximity_location(ports, lat, lon, NGA_RADIUS_KM, f"NAVAREA {area}")
            if location is None or location.kind != "port":
                skipped += 1
                continue
        link = NGA_PERMALINK.format(area=area, year=year, number=number)
        headline = re.sub(r"^\d+\.\s*", "", text.split(".")[0]).strip()[:160] or f"NAVAREA {area} warning {number}"
        signals.append(XtractSignal(
            signal_id=make_signal_id(link), source="nga_warnings",
            title=f"NAVAREA {area} {number}/{year}: {headline}", source_url=canonical_url(link),
            published_at=_nga_datetime(item.get("issueDate")), port_location=location,
            event_type=classify_event(text), severity=None,
            raw_text=as_sentence(text[:1200]), ingested_at=now,
            metadata={"publisher": "US National Geospatial-Intelligence Agency", "nav_area": area,
                      "msg_number": number, "msg_year": year, "subregion": item.get("subregion"),
                      "authority": clean_text(item.get("authority")) or None,
                      "in_force": True, "event_lat": lat, "event_lon": lon},
        ))
    signals.sort(key=lambda s: s.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    kept = signals[:NGA_MAX_SIGNALS]
    log.info("nga_warnings: %d in-force warnings near tracked places (%d elsewhere), keeping newest %d",
             len(signals), skipped, len(kept))
    return kept


# --------------------------------------------------------------------------- entry point


SOURCES = ("nhc", "jtwc", "usgs", "nga_warnings")


def collect(only: Optional[Iterable[str]] = None) -> list[XtractSignal]:
    wanted = set(only or SOURCES)
    gazetteer, ports, now = PortGazetteer.from_csv(), load_ports(), utcnow()
    signals: list[XtractSignal] = []
    if "nhc" in wanted:
        signals += nhc_signals(gazetteer, now)
    if "jtwc" in wanted:
        signals += jtwc_signals(gazetteer, now)
    if "usgs" in wanted:
        signals += usgs_signals(ports, now)
    if "nga_warnings" in wanted:
        signals += nga_signals(gazetteer, ports, now)
    return signals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=SOURCES, metavar="SOURCE",
                        help=f"fetch only these (default: all of {', '.join(SOURCES)})")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    signals = collect(args.only)
    if not signals:
        log.error("no official warnings fetched; %s left unchanged", args.out)
        return 1
    signals.sort(key=lambda s: s.published_at or s.ingested_at, reverse=True)
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by source: %s; with port_location: %d",
             len(signals), args.out, dict(Counter(s.source for s in signals)),
             sum(1 for s in signals if s.port_location))
    return 0


if __name__ == "__main__":
    sys.exit(main())
