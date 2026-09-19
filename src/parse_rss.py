"""Parse maritime trade-press RSS feeds (gCaptain, Maritime Executive, Seatrade Maritime,
Hellenic Shipping News, Splash247) into Xtract Signals.

Output: data/rss_signals.json (title, citation link, publication date, clean excerpt per item)
Feed notes: docs/DATA_SOURCES.md, section 3.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser

from common import (DATA_DIR, PortGazetteer, canonical_url, classify_event, clean_text, http_get, make_signal_id,
                    setup_logging, url_key, utcnow, write_signals)
from schemas import XtractSignal

# source name (must be in schemas.Source) -> feed URL. Order = precedence for duplicate URLs.
FEEDS = {
    "gcaptain": "https://gcaptain.com/feed/",
    "maritime_executive": "https://maritime-executive.com/articles.rss",
    "seatrade_maritime": "https://www.seatrade-maritime.com/rss.xml",
    "hellenic_shipping_news": "https://www.hellenicshippingnews.com/feed/",
    "splash247": "https://splash247.com/feed/",
}
SIGNALS_JSON = DATA_DIR / "rss_signals.json"

_FOOTER_RE = re.compile(r"\s*The post .+? appeared first on .+?\.?$")
_ELLIPSIS_RE = re.compile(r"\s*(?:\[(?:…|\.\.\.)\]|…|\.\.\.)$")
# Wire-story byline + dateline, e.g. "By A, B and C Sep 19, 2026 (Bloomberg) –"
_BYLINE_RE = re.compile(r"^By\s[^()]{3,200}?\([A-Z][\w .&-]{1,40}\)\s*[–—-]+\s*")

log = logging.getLogger("portguard.rss")


def clean_snippet(value: Optional[str]) -> str:
    """Plain-text excerpt; a truncated excerpt keeps a single trailing '…' so readers know it is cut."""
    text = _BYLINE_RE.sub("", _FOOTER_RE.sub("", clean_text(value)).strip())
    truncated = bool(_ELLIPSIS_RE.search(text))
    text = _ELLIPSIS_RE.sub("", text).strip()
    return f"{text}…" if truncated and text else text


def entry_datetime(entry) -> Optional[datetime]:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")  # feedparser normalises to UTC
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    feed = feedparser.parse(http_get(url).content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"could not parse feed {url}: {feed.bozo_exception}")
    if feed.bozo:
        log.warning("%s parsed with warnings: %s", url, feed.bozo_exception)
    return feed


def build_signals(feed: feedparser.FeedParserDict, source: str, gazetteer: PortGazetteer, now: datetime,
                  seen: set[str]) -> list[XtractSignal]:
    """Signals for one feed; `seen` holds URL keys already emitted by earlier feeds."""
    signals: list[XtractSignal] = []
    dropped = Counter()
    feed_title = clean_text(feed.feed.get("title")) or None

    for entry in feed.entries:
        title = clean_text(entry.get("title"))
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            dropped["invalid"] += 1
            continue
        if url_key(link) in seen:
            dropped["duplicate_url"] += 1
            continue
        seen.add(url_key(link))

        snippet = clean_snippet(entry.get("summary") or entry.get("description"))
        categories = list(dict.fromkeys(t for t in (clean_text(tag.get("term")) for tag in entry.get("tags", [])) if t))
        signals.append(XtractSignal(
            signal_id=make_signal_id(link), source=source, title=title, source_url=canonical_url(link),
            published_at=entry_datetime(entry),
            port_location=gazetteer.match(title, snippet, chokepoint_hints=" | ".join(categories)),
            event_type=classify_event(" | ".join([title, snippet, *categories])),
            raw_text=snippet or title, ingested_at=now,
            metadata={"author": clean_text(entry.get("author")) or None, "categories": categories,
                      "guid": entry.get("id") or None, "feed_title": feed_title},
        ))

    log.info("%s: %d items -> %d signals (dropped: %s)", source, len(feed.entries), len(signals), dict(dropped) or "none")
    return signals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=list(FEEDS), metavar="FEED",
                        help=f"parse only these feeds (default: all of {', '.join(FEEDS)})")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    gazetteer, now, seen = PortGazetteer.from_csv(), utcnow(), set()
    signals: list[XtractSignal] = []
    failed: list[str] = []
    for source in args.only or FEEDS:
        try:
            feed = fetch_feed(FEEDS[source])
        except Exception as exc:  # one broken feed must not stop the others
            log.error("%s failed: %s", source, exc)
            failed.append(source)
            continue
        signals.extend(build_signals(feed, source, gazetteer, now, seen))

    if not signals and failed:  # nothing fetched at all: keep the previous output intact
        log.error("every feed failed; %s left unchanged", args.out)
        return 1
    signals.sort(key=lambda s: s.published_at or s.ingested_at, reverse=True)
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by source: %s; by type: %s; with port_location: %d",
             len(signals), args.out, dict(Counter(s.source for s in signals)),
             dict(Counter(s.event_type for s in signals).most_common()), sum(1 for s in signals if s.port_location))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
