"""Parse maritime trade-press RSS feeds into Xtract Signals.

Seventeen feeds: the five original trade-press titles plus container/logistics trade press (JOC,
The Loadstar, Container News, World Cargo News, Marine Link, Maritime Logistics Professional,
Marine Insight, Bunkerspot, SeaNews) and three general logistics titles whose maritime coverage
is real but a minority of their output -- those are marked STRICT and filtered with is_maritime()
so the general freight/trucking stories never reach the event clusterer.

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

from common import (DATA_DIR, PortGazetteer, canonical_url, classify_event, clean_text, http_get, is_maritime,
                    make_signal_id, setup_logging, url_key, utcnow, write_signals)
from schemas import XtractSignal

# source name (must be in schemas.Source) -> feed URL. Order = precedence for duplicate URLs, so
# the highest-credibility titles come first and win the de-duplication when a wire story is
# syndicated across several feeds. Keep in step with SOURCE_GROUP in backend/app/main.py.
FEEDS = {
    # Tier-1 maritime trade press (see PUBLISHER_TIER in common.py)
    "joc": "https://www.joc.com/rss.xml",
    "loadstar": "https://theloadstar.com/feed/",
    "gcaptain": "https://gcaptain.com/feed/",
    "maritime_executive": "https://maritime-executive.com/articles.rss",
    "seatrade_maritime": "https://www.seatrade-maritime.com/rss.xml",
    "splash247": "https://splash247.com/feed/",
    # Tier-2 maritime/container trade press
    "container_news": "https://container-news.com/feed/",
    "world_cargo_news": "https://www.worldcargonews.com/feed/",
    "marine_link": "https://www.marinelink.com/news/rss",
    "maritime_logistics": "https://www.maritimeprofessional.com/rss/news",
    "marine_insight": "https://www.marineinsight.com/feed/",
    "bunkerspot": "https://www.bunkerspot.com/rss",
    "hellenic_shipping_news": "https://www.hellenicshippingnews.com/feed/",
    "sea_news": "https://www.seanews.com.tr/rss/",
    # General logistics: maritime coverage is a minority of output, so STRICT-filtered below.
    "freightwaves": "https://www.freightwaves.com/news/feed",
    "supply_chain_dive": "https://www.supplychaindive.com/feeds/news/",
    "offshore_energy": "https://www.offshore-energy.biz/feed/",
}
# Feeds whose output is mostly NOT maritime: drop items that fail is_maritime(). Measured purity
# when these were added: freightwaves 23/56, supply_chain_dive 4/10, offshore_energy 3/10 -- without
# this filter they would bury the maritime signal in trucking and upstream-energy stories.
STRICT = {"freightwaves", "supply_chain_dive", "offshore_energy"}
SIGNALS_JSON = DATA_DIR / "rss_signals.json"

_FOOTER_RE = re.compile(r"\s*The post .+? appeared first on .+?\.?$")
_ELLIPSIS_RE = re.compile(r"\s*(?:\[(?:…|\.\.\.)\]|…|\.\.\.)$")
# Wire-story byline + dateline, e.g. "By A, B and C Sep 19, 2026 (Bloomberg) –"
_BYLINE_RE = re.compile(r"^By\s[^()]{3,200}?\([A-Z][\w .&-]{1,40}\)\s*[–—-]+\s*")

log = logging.getLogger("majeure.rss")


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
        # A general-logistics feed carries trucking, rail and warehousing stories that have nothing
        # to do with a vessel or a port; keeping them would dilute every downstream event cluster.
        if source in STRICT and not is_maritime(f"{title} {snippet}"):
            dropped["not_maritime"] += 1
            continue
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
