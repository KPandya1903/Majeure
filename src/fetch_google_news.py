"""Search Google News (RSS) for maritime-disruption headlines.

Output: data/google_news_signals.json (Xtract Signals; headline-only, relevance-filtered)
Feed notes: docs/DATA_SOURCES.md, section 5.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from common import (DATA_DIR, PortGazetteer, canonical_url, classify_event, clean_text, is_maritime, make_signal_id,
                    setup_logging, url_key, utcnow, write_signals)
from parse_rss import entry_datetime, fetch_feed
from schemas import EventType, XtractSignal

SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
DEFAULT_QUERY = ('(port OR shipping OR maritime OR vessel OR tanker OR "container ship") '
                 '(strike OR disruption OR closure OR congestion OR attack OR blockade OR delay)')
SIGNALS_JSON = DATA_DIR / "google_news_signals.json"

log = logging.getLogger("portguard.google_news")


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def build_signals(entries, gazetteer: PortGazetteer, now: datetime) -> list[XtractSignal]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    signals: list[XtractSignal] = []
    dropped = Counter()

    for e in entries:
        link = (e.get("link") or "").strip()
        publisher = clean_text((e.get("source") or {}).get("title")) or None
        title = clean_text(e.get("title"))
        if publisher and title.endswith(f" - {publisher}"):  # Google appends " - Publisher"
            title = title[: -len(f" - {publisher}")].strip()
        if not title or not link.startswith("http"):
            dropped["invalid"] += 1
            continue
        # Keyword search over all news: keep only headlines that are about shipping AND a disruption.
        event_type = classify_event(title)
        if not is_maritime(title) or event_type == EventType.OTHER:
            dropped["off_topic"] += 1
            continue
        if url_key(link) in seen_urls or title_key(title) in seen_titles:
            dropped["duplicate"] += 1
            continue
        seen_urls.add(url_key(link))
        seen_titles.add(title_key(title))
        signals.append(XtractSignal(
            signal_id=make_signal_id(link), source="google_news", title=title, source_url=canonical_url(link),
            published_at=entry_datetime(e), port_location=gazetteer.match(title), event_type=event_type,
            raw_text=title, ingested_at=now,
            metadata={"publisher": publisher, "publisher_url": (e.get("source") or {}).get("href") or None},
        ))

    log.info("%d results -> %d signals (dropped: %s)", len(entries), len(signals), dict(dropped) or "none")
    return signals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Google News search query (default: maritime x disruption terms)")
    parser.add_argument("--days", type=int, default=3, help="look-back window in days (default: 3)")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    url = SEARCH_URL.format(query=quote_plus(f"{args.query} when:{args.days}d"))
    try:
        feed = fetch_feed(url)
    except Exception as exc:  # keep the previous output intact
        log.error("Google News fetch failed: %s", exc)
        return 1

    signals = build_signals(feed.entries, PortGazetteer.from_csv(), utcnow())
    signals.sort(key=lambda s: s.published_at or s.ingested_at, reverse=True)
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by type: %s; with port_location: %d", len(signals), args.out,
             dict(Counter(s.event_type for s in signals).most_common()), sum(1 for s in signals if s.port_location))
    return 0


if __name__ == "__main__":
    sys.exit(main())
