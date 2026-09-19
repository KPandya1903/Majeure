"""Fetch recent maritime-disruption news from GDELT.

Two modes:
  raw (default)  GDELT 2.0 Global Knowledge Graph 15-minute files: current, no rate limit,
                 ~3 MB per file; headlines are filtered for shipping + disruption relevance.
  api            GDELT 2.0 Doc API search: one request, but rate-limited (1 request / 5 s)
                 and its index can lag days behind the raw files.

Output: data/gdelt_signals.json (Xtract Signals, duplicates removed)
Endpoint notes: docs/DATA_SOURCES.md, section 2.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import (DATA_DIR, PortGazetteer, canonical_url, classify_event, clean_text, http_get, is_maritime,
                    make_signal_id, setup_logging, url_key, utcnow, write_signals)
from schemas import EventType, XtractSignal

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERY = "(port OR maritime OR shipping) (strike OR delay OR disruption)"
RAW_LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
RAW_GKG_FILE = "http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"
SIGNALS_JSON = DATA_DIR / "gdelt_signals.json"
GDELT_BACKOFF_S = 6.0  # GDELT allows one request every 5 seconds
GDELT_RETRIES = 6  # waits 6+12+24+48+96 s at most: GDELT's throttle can take minutes to cool down
# GKG 2.1 columns used (tab-separated, no header)
GKG_DATE, GKG_COLLECTION, GKG_DOMAIN, GKG_URL, GKG_EXTRAS = 1, 2, 3, 4, 26

log = logging.getLogger("portguard.gdelt")


# --------------------------------------------------------------------------- shared


def clean_title(value: Optional[str]) -> str:
    """GDELT tokenises titles: 'border , tighten' / '[ Blog ]' -> 'border, tighten' / '[Blog]'."""
    title = re.sub(r"\s+([,.;:!?%)\]])", r"\1", clean_text(value))
    return re.sub(r"([(\[])\s+", r"\1", title)


_PUBLISHER_SUFFIX_RE = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,60}$")


def title_key(title: str) -> str:
    """Syndication key: lowercase alphanumerics, minus a trailing ' - Publisher' segment."""
    return re.sub(r"[^a-z0-9]+", " ", _PUBLISHER_SUFFIX_RE.sub("", title).lower()).strip()


def build_signals(articles: list[dict], gazetteer: PortGazetteer, now: datetime, relevance_filter: bool) -> list[XtractSignal]:
    """articles: dicts with url, title, published_at, metadata."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    signals: list[XtractSignal] = []
    dropped = Counter()

    for art in articles:
        url, title = art["url"], clean_title(art["title"])
        if not url.startswith(("http://", "https://")) or not title:
            dropped["invalid"] += 1
            continue
        event_type = classify_event(title)
        if relevance_filter and (not is_maritime(title) or event_type == EventType.OTHER):
            dropped["off_topic"] += 1
            continue
        key = url_key(url)
        if key in seen_urls:
            dropped["duplicate_url"] += 1
            continue
        seen_urls.add(key)
        if title_key(title) in seen_titles:  # same wire story syndicated on another domain
            dropped["duplicate_title"] += 1
            continue
        seen_titles.add(title_key(title))

        signals.append(XtractSignal(
            signal_id=make_signal_id(url), source="gdelt", title=title, source_url=canonical_url(url),
            published_at=art["published_at"], port_location=gazetteer.match(title), event_type=event_type,
            raw_text=title, ingested_at=now, metadata=art["metadata"],
        ))

    log.info("%d articles -> %d signals (dropped: %s)", len(articles), len(signals), dict(dropped) or "none")
    return signals


# --------------------------------------------------------------------------- raw GKG files


def _download(url: str) -> bytes:
    """GDELT's file server occasionally answers 200 with an empty body: retry those."""
    for _ in range(3):
        content = http_get(url, timeout=120).content
        if content:
            return content
    raise RuntimeError(f"empty response from {url}")


def latest_gkg_stamp() -> datetime:
    text = _download(RAW_LASTUPDATE).decode("utf-8", "replace")
    match = re.search(r"/(\d{14})\.gkg\.csv\.zip", text)
    if not match:
        raise RuntimeError(f"no GKG file listed in {RAW_LASTUPDATE}: {text[:200]!r}")
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def fetch_raw_articles(hours: float) -> list[dict]:
    """Maritime-looking headlines from the GKG files of the last `hours`, newest first."""
    csv.field_size_limit(2**31 - 1)
    latest = latest_gkg_stamp()
    n_files = max(1, int(hours * 4))
    articles, stats = [], Counter()
    for i in range(n_files):
        stamp = (latest - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
        try:
            data = _download(RAW_GKG_FILE.format(stamp=stamp))
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                rows = csv.reader(io.TextIOWrapper(zf.open(zf.namelist()[0]), encoding="utf-8", errors="replace"),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)
                for row in rows:
                    stats["rows"] += 1
                    if len(row) <= GKG_EXTRAS or row[GKG_COLLECTION] != "1":  # 1 = web article with a URL
                        continue
                    match = re.search(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", row[GKG_EXTRAS])
                    if not match or not is_maritime(clean_text(match.group(1))):
                        continue
                    articles.append({
                        "url": row[GKG_URL].strip(), "title": match.group(1),
                        "published_at": datetime.strptime(row[GKG_DATE], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc),
                        "metadata": {"domain": row[GKG_DOMAIN] or None, "via": "gkg", "gkg_file": stamp},
                    })
            stats["files"] += 1
            stats["mb"] += len(data) / 1e6
        except Exception as exc:  # a missing or corrupt 15-minute file is skipped, not fatal
            log.warning("skipping GKG file %s: %s", stamp, exc)
            stats["files_failed"] += 1
    if not stats["files"]:
        raise RuntimeError("no GKG file could be downloaded")
    log.info("GKG %s..%s: %d files (%.0f MB), %d articles scanned, %d maritime headlines",
             (latest - timedelta(minutes=15 * (n_files - 1))).strftime("%Y-%m-%d %H:%M"), latest.strftime("%H:%M UTC"),
             stats["files"], stats["mb"], stats["rows"], len(articles))
    return articles


# --------------------------------------------------------------------------- Doc API


def parse_seendate(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(value or "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_api_articles(query: str, max_records: int, timespan: str) -> list[dict]:
    resp = http_get(GDELT_URL, params={
        "query": query, "mode": "artlist", "maxrecords": max_records, "format": "json",
        "timespan": timespan, "sort": "datedesc",
    }, timeout=60, retries=GDELT_RETRIES, backoff=GDELT_BACKOFF_S)
    body = resp.text.strip()
    if not body.startswith("{"):  # errors (bad query, rate limit) come back as plain text
        raise RuntimeError(f"GDELT returned a non-JSON response: {body[:200]!r}")
    return [{
        "url": (a.get("url") or "").strip(), "title": a.get("title"), "published_at": parse_seendate(a.get("seendate")),
        "metadata": {"domain": a.get("domain") or None, "language": a.get("language") or None,
                     "source_country": a.get("sourcecountry") or None, "social_image": a.get("socialimage") or None,
                     "via": "doc_api"},
    } for a in json.loads(body, strict=False).get("articles", [])]  # key is absent when nothing matched


# --------------------------------------------------------------------------- main


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=["raw", "api"], default="raw", help="raw GKG files (default) or the Doc API")
    parser.add_argument("--hours", type=float, default=6, help="raw mode: look-back window in hours (default: 6, ~75 MB)")
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"api mode: GDELT query (default: {DEFAULT_QUERY!r})")
    parser.add_argument("--timespan", default="7d", help="api mode: look-back window, e.g. 24h, 3d (default: 7d)")
    parser.add_argument("--max-records", type=int, default=50, choices=range(1, 251), metavar="1-250", help="api mode")
    parser.add_argument("--all-languages", action="store_true", help="api mode: do not append sourcelang:english")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        if args.mode == "raw":
            articles = fetch_raw_articles(args.hours)
        else:
            query = args.query if args.all_languages else f"{args.query} sourcelang:english"
            log.info("query=%r timespan=%s maxrecords=%d", query, args.timespan, args.max_records)
            articles = fetch_api_articles(query, args.max_records, args.timespan)
    except Exception as exc:  # keep the previous output intact
        log.error("GDELT %s fetch failed: %s", args.mode, exc)
        if args.mode == "api":
            log.error("(a 429 means GDELT is throttling this IP: wait a few minutes, or use the default --mode raw)")
        return 1

    # Raw mode scans the whole news firehose, so it keeps only shipping + disruption headlines.
    signals = build_signals(articles, PortGazetteer.from_csv(), utcnow(), relevance_filter=args.mode == "raw")
    signals.sort(key=lambda s: s.published_at or s.ingested_at, reverse=True)
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by type: %s; with port_location: %d",
             len(signals), args.out, dict(Counter(s.event_type for s in signals).most_common()),
             sum(1 for s in signals if s.port_location))
    return 0


if __name__ == "__main__":
    sys.exit(main())
