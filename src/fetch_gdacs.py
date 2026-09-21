"""Fetch live natural-hazard alerts from GDACS and link each one to the ports near it.

Output: data/gdacs_signals.json (Xtract Signals with proximity-matched ports)
Feed notes: docs/DATA_SOURCES.md, section 4.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import feedparser

from common import (ALERT_SEVERITY, DATA_DIR, GDACS_EVENT_TYPES, as_sentence, canonical_url, clean_text, http_get,
                    iso, load_ports, make_signal_id, nearby_ports, setup_logging, utcnow, write_signals)
from schemas import PortLocation, XtractSignal

GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"
SIGNALS_JSON = DATA_DIR / "gdacs_signals.json"
PORTWATCH_JSON = DATA_DIR / "portwatch_signals.json"
ALERT_RANK = {"green": 0, "orange": 1, "red": 2}
# Default ("auto") minimum alert per type: GDACS emits hundreds of Green wildfire and drought
# notices that never matter to ports, while a Green cyclone or flood next to a port can.
AUTO_MIN_ALERT = {"TC": "green", "TS": "green", "EQ": "green", "FL": "green", "VO": "green", "WF": "orange", "DR": "orange"}
# How far from the event's reported point a port counts as exposed (km). Heuristics: a cyclone's
# wind field is wide, a wildfire's reach is local, a drought has no meaningful point.
RADIUS_KM = {"TC": 400, "TS": 400, "EQ": 150, "FL": 100, "VO": 100, "WF": 50, "DR": 0}
MAX_PORTS = 25

log = logging.getLogger("majeure.gdacs")


def _date(value: Optional[str]) -> Optional[datetime]:
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc) if value else None
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def portwatch_event_ids(path: Path = PORTWATCH_JSON) -> set[str]:
    """GDACS events PortWatch already curates (same event ids); PortWatch's version wins."""
    if not path.exists():
        return set()
    return {str(s["metadata"].get("event_id")) for s in json.loads(path.read_text(encoding="utf-8"))
            if s["metadata"].get("gdacs_event_type") in GDACS_EVENT_TYPES}


def build_signals(entries, ports, min_alert: str, days: int, now: datetime, skip_ids: set[str]) -> list[XtractSignal]:
    signals: dict[str, XtractSignal] = {}
    dropped = Counter()
    cutoff = now - timedelta(days=days)

    for e in entries:
        code = clean_text(e.get("gdacs_eventtype")).upper()
        alert = clean_text(e.get("gdacs_alertlevel")).lower()
        event_id = clean_text(e.get("gdacs_eventid"))
        link = (e.get("link") or "").strip()
        if code not in GDACS_EVENT_TYPES or not link.startswith("http") or not event_id:
            dropped["invalid"] += 1
            continue
        threshold = AUTO_MIN_ALERT.get(code, "orange") if min_alert == "auto" else min_alert
        if ALERT_RANK.get(alert, -1) < ALERT_RANK[threshold]:
            dropped[f"below_{threshold}"] += 1
            continue
        if event_id in skip_ids:
            dropped["already_in_portwatch"] += 1
            continue
        from_dt, to_dt = _date(e.get("gdacs_fromdate")), _date(e.get("gdacs_todate"))
        is_active = clean_text(e.get("gdacs_iscurrent")).lower() == "true"
        if not is_active and (to_dt or from_dt or now) < cutoff:
            dropped["stale"] += 1
            continue

        lat, lon = _float(e.get("geo_lat")), _float(e.get("geo_long"))
        country = clean_text(e.get("gdacs_country")) or None
        radius = RADIUS_KM.get(code, 0)
        near = nearby_ports(ports, lat, lon, radius, MAX_PORTS) if ports is not None and lat is not None else None
        if near is not None and len(near):
            first = near.iloc[0]
            location = PortLocation(name=first.port_name, kind="port", port_id=first.port_id, country=first.country,
                                    lat=first.lat, lon=first.lon, match_method="proximity")
        elif lat is not None and lon is not None:
            location = PortLocation(name=country or clean_text(e.get("title")), kind="region", country=country,
                                    lat=lat, lon=lon, match_method="structured")
        else:
            location = None
        nearby = [] if near is None else [
            {"port_id": r.port_id, "name": r.port_name, "distance_km": float(r.distance_km)} for r in near.itertuples()]

        description = clean_text(e.get("summary") or e.get("gdacs_description"))
        ports_text = ", ".join(f"{p['name']} ({p['distance_km']:.0f} km)" for p in nearby[:10])
        raw_text = " ".join(filter(None, [
            as_sentence(description or clean_text(e.get("title"))),
            f"Ports within {radius} km of the reported location: {ports_text}." if ports_text else "",
        ]))
        published = from_dt if from_dt and from_dt <= now else _date(e.get("published"))
        signal = XtractSignal(
            signal_id=make_signal_id(link), source="gdacs", title=clean_text(e.get("title")),
            source_url=canonical_url(link), published_at=published, port_location=location,
            related_port_ids=[p["port_id"] for p in nearby], event_type=GDACS_EVENT_TYPES[code],
            severity=ALERT_SEVERITY.get(alert.upper()), raw_text=raw_text or clean_text(e.get("title")),
            ingested_at=now,
            metadata={
                "event_id": int(event_id) if event_id.isdigit() else event_id,
                "episode_id": clean_text(e.get("gdacs_episodeid")) or None, "gdacs_event_type": code,
                "alert_level": alert.upper(), "country": country, "from_date": iso(from_dt), "to_date": iso(to_dt),
                "is_active": is_active, "event_lat": lat, "event_lon": lon, "radius_km": radius,
                "nearby_ports": nearby,
            },
        )
        signals[signal.signal_id] = signal  # later episodes of the same event replace earlier ones

    log.info("%d GDACS items -> %d signals (dropped: %s)", len(entries), len(signals), dict(dropped) or "none")
    return sorted(signals.values(), key=lambda s: s.published_at or now, reverse=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--min-alert", choices=["auto", *ALERT_RANK], default="auto",
                        help="lowest GDACS alert level to keep (default: auto = green for cyclones, quakes, floods, "
                             "tsunamis, volcanoes; orange for wildfires and droughts)")
    parser.add_argument("--days", type=int, default=14, help="keep non-current events that ended within N days")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        feed = feedparser.parse(http_get(GDACS_RSS, timeout=60).content)
    except Exception as exc:  # keep the previous output intact
        log.error("GDACS fetch failed: %s", exc)
        return 1
    if not feed.entries:
        log.error("GDACS feed had no entries (%s); output left unchanged", feed.get("bozo_exception"))
        return 1

    ports = load_ports()
    signals = build_signals(feed.entries, ports, args.min_alert, args.days, utcnow(), portwatch_event_ids())
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by type: %s; linked to ports: %d", len(signals), args.out,
             dict(Counter(s.metadata["gdacs_event_type"] for s in signals)),
             sum(1 for s in signals if s.related_port_ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
