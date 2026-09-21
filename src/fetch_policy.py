"""Fetch trade-policy actions -- tariffs, sanctions, trade remedies -- into Xtract Signals.

Majeure tracked physical disruption and nothing else, even though `regulatory_sanctions` has
been an event type since the first schema. A tariff schedule change or an export control can
strand a container as effectively as a closed strait, and it arrives with more warning, which is
exactly the kind of lead time this product exists to use.

Four first-party publishers, no scraping:

* ``federal_register`` The legal record of US trade action, as a JSON API with structured fields
  (agency, document type, abstract, publication date). A proclamation imposing tariffs IS a
  Federal Register document -- this is the primary source, not reporting about one.
* ``white_house``      Presidential actions feed: proclamations and executive orders.
* ``ustr``             Office of the US Trade Representative press releases.
* ``wto``              WTO news, for disputes and measures outside the US.

On scraping, deliberately not: every one of these publishes a feed or an API, and an HTML scrape
of a government site would be slower, more fragile and worse-behaved than the interface they
already provide. The model's job here is not retrieval, it is *extraction* -- deciding whether a
document affects shipping and how severely -- which is what xtract.py already does with Nemotron.

A caveat that matters and is not solved here: trade policy usually names a COUNTRY, not a port
("articles of Chinese origin"). Shipment matching in this codebase is by port and chokepoint, so
these signals corroborate events and show in the feed but will not by themselves attach to a
shipment unless the text names a place the gazetteer knows. Country-level matching is the real
next step.

Output: data/policy_signals.json
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import feedparser

from common import (DATA_DIR, PortGazetteer, as_sentence, canonical_url, clean_text, http_get,
                    make_signal_id, setup_logging, utcnow, write_signals)
from schemas import EventType, XtractSignal

SIGNALS_JSON = DATA_DIR / "policy_signals.json"

FEDERAL_REGISTER = "https://www.federalregister.gov/api/v1/documents.json"
# Targeted, because a bare "tariff" term search returns 10,000 documents, most of them FERC energy
# filings that merely contain the word. These select the agencies that actually set trade measures,
# plus presidential documents, which is where proclamations imposing duties appear.
FR_QUERIES = (
    {"conditions[agencies][]": "trade-representative-office-of-united-states"},
    {"conditions[agencies][]": "industry-and-security-bureau"},
    {"conditions[type][]": "PRESDOCU", "conditions[term]": "tariff OR duties OR trade"},
    {"conditions[agencies][]": "foreign-assets-control-office", "conditions[term]": "sanctions"},
)
FR_FIELDS = ("title", "abstract", "html_url", "publication_date", "type", "agencies", "document_number")

RSS_FEEDS = {
    "white_house": "https://www.whitehouse.gov/presidential-actions/feed/",
    "ustr": "https://ustr.gov/rss.xml",
    "wto": "https://www.wto.org/library/rss/latest_news_e.xml",
}
LOOKBACK_DAYS = 45
PER_QUERY = 20

# Does this document plausibly touch goods movement? Federal Register and WTO publish a great deal
# that has nothing to do with trade in physical things, and an unfiltered feed would bury the
# maritime signal exactly the way the general-logistics RSS feeds did.
_TRADE_RE = re.compile(
    r"\b(?:tariff|duty|duties|customs|import|export|quota|embargo|sanction|trade remedy|"
    r"antidumping|anti-dumping|countervailing|section 30[12]|section 232|section 301|"
    r"harmonized tariff|de minimis|origin|port of entry|shipment|cargo|vessel|maritime)\b", re.I)

log = logging.getLogger("majeure.policy")


def is_trade_relevant(text: str) -> bool:
    return bool(_TRADE_RE.search(text or ""))


def _severity(text: str) -> Optional[str]:
    """Grade by how abruptly the measure bites. A rate change with immediate effect strands cargo
    that is already on the water; a comment period does not."""
    lowered = (text or "").lower()
    if re.search(r"\b(?:embargo|prohibit|ban(?:ned|s)?|suspend(?:ed|s)?|effective immediately)\b", lowered):
        return "high"
    if re.search(r"\b(?:impos|increase|rais|new tariff|additional duties|sanction)\w*", lowered):
        return "medium"
    if re.search(r"\b(?:comment|propos|request for information|notice of inquiry)\w*", lowered):
        return "low"
    return "low"


def federal_register_signals(gazetteer: PortGazetteer, now: datetime) -> list[XtractSignal]:
    since = (now - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    signals, seen = [], set()
    for query in FR_QUERIES:
        params = {"per_page": str(PER_QUERY), "order": "newest",
                  "conditions[publication_date][gte]": since, **query}
        url = FEDERAL_REGISTER + "?" + "&".join(
            f"{k}={requests_quote(v)}" for k, v in
            [*params.items(), *[("fields[]", f) for f in FR_FIELDS]])
        try:
            payload = http_get(url).json()
        except Exception as exc:  # one query failing must not lose the others
            log.warning("federal_register query failed (%s): %s", query, exc)
            continue
        for doc in payload.get("results", []):
            link = (doc.get("html_url") or "").strip()
            title = clean_text(doc.get("title"))
            if not link.startswith("http") or not title or link in seen:
                continue
            abstract = clean_text(doc.get("abstract"))
            blob = f"{title} {abstract}"
            if not is_trade_relevant(blob):
                continue
            seen.add(link)
            agencies = [clean_text(a.get("name")) for a in (doc.get("agencies") or []) if a.get("name")]
            published = None
            if doc.get("publication_date"):
                published = datetime.fromisoformat(doc["publication_date"]).replace(tzinfo=timezone.utc)
            signals.append(XtractSignal(
                signal_id=make_signal_id(link), source="federal_register", title=title,
                source_url=canonical_url(link), published_at=published,
                port_location=gazetteer.match(title, abstract),
                event_type=EventType.REGULATORY_SANCTIONS, severity=_severity(blob),
                raw_text=as_sentence(abstract or title), ingested_at=now,
                metadata={"publisher": "US Federal Register", "agencies": agencies,
                          "document_type": doc.get("type"), "document_number": doc.get("document_number")},
            ))
    log.info("federal_register: %d trade-relevant documents", len(signals))
    return signals


def requests_quote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


def rss_signals(source: str, url: str, gazetteer: PortGazetteer, now: datetime) -> list[XtractSignal]:
    try:
        feed = feedparser.parse(http_get(url).content)
    except Exception as exc:
        log.error("%s failed: %s", source, exc)
        return []
    signals = []
    for entry in feed.entries:
        title, link = clean_text(entry.get("title")), (entry.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"))
        blob = f"{title} {summary}"
        if not is_trade_relevant(blob):
            continue
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        signals.append(XtractSignal(
            signal_id=make_signal_id(link), source=source, title=title,
            source_url=canonical_url(link),
            published_at=datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None,
            port_location=gazetteer.match(title, summary),
            event_type=EventType.REGULATORY_SANCTIONS, severity=_severity(blob),
            raw_text=as_sentence(summary or title), ingested_at=now,
            metadata={"publisher": clean_text(feed.feed.get("title")) or source},
        ))
    log.info("%s: %d trade-relevant items", source, len(signals))
    return signals


SOURCES = ("federal_register", "white_house", "ustr", "wto")


def collect(only: Optional[Iterable[str]] = None) -> list[XtractSignal]:
    wanted = set(only or SOURCES)
    gazetteer, now = PortGazetteer.from_csv(), utcnow()
    signals: list[XtractSignal] = []
    if "federal_register" in wanted:
        signals += federal_register_signals(gazetteer, now)
    for source, url in RSS_FEEDS.items():
        if source in wanted:
            signals += rss_signals(source, url, gazetteer, now)
    return signals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=SOURCES, metavar="SOURCE")
    parser.add_argument("--out", type=Path, default=SIGNALS_JSON)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    signals = collect(args.only)
    if not signals:
        log.error("no policy signals fetched; %s left unchanged", args.out)
        return 1
    signals.sort(key=lambda s: s.published_at or s.ingested_at, reverse=True)
    write_signals(args.out, signals)
    log.info("wrote %d signals -> %s; by source: %s; with port_location: %d",
             len(signals), args.out, dict(Counter(s.source for s in signals)),
             sum(1 for s in signals if s.port_location))
    return 0


if __name__ == "__main__":
    sys.exit(main())
