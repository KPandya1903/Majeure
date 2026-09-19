"""Fetch IMF PortWatch port reference data and port-disruption events.

Outputs:
  data/portwatch_ports.csv     port reference table (+ active disruption counts per port)
  data/portwatch_signals.json  disruption events as Xtract Signals

Endpoints and field notes: docs/DATA_SOURCES.md, section 1.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from common import (ALERT_SEVERITY, CHOKEPOINTS_BY_PORTWATCH_ID, as_sentence, DATA_DIR, GDACS_EVENT_TYPES, PORTS_CSV,
                    classify_event, clean_text, http_get, iso, make_signal_id, setup_logging, utcnow, write_csv,
                    write_signals)
from schemas import PortLocation, XtractSignal

ARCGIS_ROOT = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PORTS_LAYER = f"{ARCGIS_ROOT}/PortWatch_ports_database/FeatureServer/0"
DISRUPTIONS_LAYER = f"{ARCGIS_ROOT}/portwatch_disruptions_database/FeatureServer/0"
PAGE_URL = "https://portwatch.imf.org/pages/{pageid}"
GDACS_URL = "https://www.gdacs.org/report.aspx?eventid={eventid}&eventtype={eventtype}"
SIGNALS_JSON = DATA_DIR / "portwatch_signals.json"
PAGE_SIZE = 1000  # <= maxRecordCount of both layers

# PortWatch field -> `ports` table column (docs/SCHEMAS.md)
PORT_COLUMNS = {
    "portid": "port_id", "portname": "port_name", "fullname": "full_name", "country": "country",
    "ISO3": "iso3", "continent": "continent", "LOCODE": "locode", "lat": "lat", "lon": "lon",
    "vessel_count_total": "vessel_count_total", "vessel_count_container": "vessel_count_container",
    "vessel_count_dry_bulk": "vessel_count_dry_bulk", "vessel_count_general_cargo": "vessel_count_general_cargo",
    "vessel_count_RoRo": "vessel_count_roro", "vessel_count_tanker": "vessel_count_tanker",
    "industry_top1": "industry_top1", "industry_top2": "industry_top2", "industry_top3": "industry_top3",
    "share_country_maritime_import": "share_country_maritime_import",
    "share_country_maritime_export": "share_country_maritime_export",
    "pageid": "pageid",
}
TEXT_COLUMNS = ["port_id", "port_name", "full_name", "country", "iso3", "continent", "locode",
                "industry_top1", "industry_top2", "industry_top3", "pageid"]

log = logging.getLogger("portguard.portwatch")


def query_layer(layer_url: str) -> list[dict]:
    """Return the attributes of every feature in an ArcGIS layer, paging with resultOffset."""
    rows: list[dict] = []
    while True:
        body = http_get(f"{layer_url}/query", params={
            "where": "1=1", "outFields": "*", "returnGeometry": "false", "orderByFields": "ObjectId ASC",
            "resultOffset": len(rows), "resultRecordCount": PAGE_SIZE, "f": "json",
        }).json()
        if "error" in body:  # ArcGIS reports errors with HTTP 200
            raise RuntimeError(f"ArcGIS error from {layer_url}: {body['error']}")
        features = body.get("features", [])
        rows.extend(f["attributes"] for f in features)
        if not features or not body.get("exceededTransferLimit"):
            break
    log.info("%s: %d rows", layer_url.split("/services/")[1].split("/")[0], len(rows))
    return rows


def clean_ports(raw: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    missing = set(PORT_COLUMNS) - set(df.columns)
    if missing:
        raise RuntimeError(f"PortWatch ports layer is missing expected fields: {sorted(missing)}")
    df = df[list(PORT_COLUMNS)].rename(columns=PORT_COLUMNS)
    for col in TEXT_COLUMNS:
        df[col] = df[col].map(lambda v: clean_text(v) or None)
    before = len(df)
    df = df.dropna(subset=["port_id", "port_name", "lat", "lon"]).drop_duplicates("port_id")
    if len(df) < before:
        log.info("dropped %d port rows with missing id/name/coordinates or duplicate ids", before - len(df))
    df["portwatch_url"] = df.pop("pageid").map(lambda p: PAGE_URL.format(pageid=p) if p else None)
    return df.sort_values("vessel_count_total", ascending=False, na_position="last").reset_index(drop=True)


def epoch_ms(value) -> Optional[datetime]:
    if value is None or pd.isna(value):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def split_port_ids(value) -> list[str]:
    ids = (p.strip() for p in str(value or "").split(";"))
    return list(dict.fromkeys(p for p in ids if p))  # de-duplicate, keep order


_ONGOING_RE = re.compile(r"\s+to:\s*-\s*\.?\s*$")


def build_signals(events: list[dict], ports: pd.DataFrame, active_days: int, now: datetime
                  ) -> tuple[list[XtractSignal], dict[str, list[str]]]:
    """Map disruption events to Xtract Signals; also return {port_id: [active event ids]}."""
    port_index = ports.set_index("port_id")
    cutoff = now - timedelta(days=active_days)
    signals: dict[str, XtractSignal] = {}
    active_by_port: dict[str, list[str]] = {}

    for ev in events:
        name = clean_text(ev.get("eventname"))
        event_id = ev.get("eventid")
        if not name or event_id is None:
            log.debug("skipping event without name/id: %s", ev.get("ObjectId"))
            continue
        code = clean_text(ev.get("eventtype")).upper()
        from_dt, to_dt = epoch_ms(ev.get("fromdate")), epoch_ms(ev.get("todate"))
        is_active = to_dt is None or to_dt >= cutoff

        # `affectedports` mixes port ids (port1188) and chokepoint ids (chokepoint4).
        affected_ids = split_port_ids(ev.get("affectedports"))
        known = [p for p in affected_ids if p in port_index.index]
        chokepoints = [CHOKEPOINTS_BY_PORTWATCH_ID[p] for p in affected_ids if p in CHOKEPOINTS_BY_PORTWATCH_ID]
        unresolved = [p for p in affected_ids if p not in port_index.index and p not in CHOKEPOINTS_BY_PORTWATCH_ID]
        if unresolved:
            log.warning("event %s (%s): unresolved affected ids %s", event_id, name, unresolved)

        if known:
            primary = max(known, key=lambda p: port_index.at[p, "vessel_count_total"] or 0)
            port = port_index.loc[primary]
            location = PortLocation(name=port.port_name, kind="port", port_id=primary, country=port.country,
                                    lat=port.lat, lon=port.lon, match_method="structured")
        elif chokepoints:
            location = chokepoints[0].location("structured")
        elif ev.get("lat") is not None and ev.get("long") is not None:
            country = clean_text(ev.get("country")) or None
            location = PortLocation(name=country or name, kind="region", country=country,
                                    lat=round(ev["lat"], 4), lon=round(ev["long"], 4), match_method="structured")
        else:
            location = None

        # Ongoing events read "... from: 01 Mar 2026 to: - ."
        description = _ONGOING_RE.sub(" to: ongoing.", clean_text(ev.get("htmldescription")))
        severity_text = clean_text(ev.get("severitytext"))
        ports_text = ", ".join(f"{port_index.at[p, 'port_name']} ({port_index.at[p, 'country']})" for p in known)
        chokepoints_text = ", ".join(c.name for c in chokepoints)
        raw_text = " ".join(filter(None, [
            as_sentence(description or name), as_sentence(severity_text),
            f"Affected ports: {ports_text}." if ports_text else "",
            f"Affected chokepoints: {chokepoints_text}." if chokepoints_text else "",
        ]))

        pageid = clean_text(ev.get("pageid"))
        source_url = (PAGE_URL.format(pageid=pageid) if pageid
                      else f"{DISRUPTIONS_LAYER}/query?where=eventid%3D{event_id}&outFields=*&f=json")
        metadata = {
            "event_id": event_id, "gdacs_event_type": code or None, "alert_level": ev.get("alertlevel"),
            "severity_text": severity_text or None, "from_date": iso(from_dt), "to_date": iso(to_dt),
            "is_active": is_active, "n_affected_ports": len(known), "n_affected_chokepoints": len(chokepoints),
        }
        if code in GDACS_EVENT_TYPES:
            metadata["gdacs_url"] = GDACS_URL.format(eventid=event_id, eventtype=code)

        signal = XtractSignal(
            signal_id=make_signal_id(source_url), source="portwatch", title=name, source_url=source_url,
            published_at=from_dt, port_location=location,
            related_port_ids=known + [c.location_id for c in chokepoints],
            event_type=GDACS_EVENT_TYPES.get(code) or classify_event(f"{name} {description}"),
            severity=ALERT_SEVERITY.get(clean_text(ev.get("alertlevel")).upper()),
            raw_text=raw_text, ingested_at=now, metadata=metadata,
        )
        if signal.signal_id in signals:
            log.debug("duplicate event row %s (%s) skipped", event_id, name)
            continue
        signals[signal.signal_id] = signal
        if is_active:
            for p in known:
                active_by_port.setdefault(p, []).append(str(event_id))

    ordered = sorted(signals.values(), key=lambda s: s.published_at or now, reverse=True)
    return ordered, active_by_port


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--active-days", type=int, default=30,
                        help="event counts as active if ongoing or ended within this many days (default: 30)")
    parser.add_argument("--ports-out", type=Path, default=PORTS_CSV)
    parser.add_argument("--signals-out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    now = utcnow()
    try:
        ports = clean_ports(query_layer(PORTS_LAYER))
        signals, active_by_port = build_signals(query_layer(DISRUPTIONS_LAYER), ports, args.active_days, now)
    except Exception as exc:  # network/API failure: keep the previous outputs intact
        log.error("PortWatch fetch failed: %s", exc)
        return 1

    ports["active_disruption_count"] = ports["port_id"].map(lambda p: len(active_by_port.get(p, [])))
    ports["active_disruption_ids"] = ports["port_id"].map(lambda p: ";".join(active_by_port.get(p, [])) or None)
    ports["updated_at"] = iso(now)
    write_csv(args.ports_out, ports)
    write_signals(args.signals_out, signals)

    active = [s for s in signals if s.metadata["is_active"]]
    log.info("wrote %d ports -> %s (%d with an active disruption)",
             len(ports), args.ports_out, int((ports["active_disruption_count"] > 0).sum()))
    log.info("wrote %d disruption signals -> %s; by type: %s",
             len(signals), args.signals_out, dict(Counter(s.event_type for s in signals).most_common()))
    log.info("%d active (ongoing or ended < %dd ago): %s", len(active), args.active_days,
             "; ".join(f"{s.title} @ {', '.join(s.related_port_ids) or s.port_location.name}" for s in active) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
