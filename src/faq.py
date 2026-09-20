"""Instant answers for the questions people actually ask PortGuard.

Ask PortGuard routes every question to a reasoning model, which costs 8-40 seconds on the hosted
NVIDIA endpoint. Most questions in a demo or a daily standup are not novel -- they are the same
thirty or so questions about risk, exposure, force majeure and cost, asked in different words. This
module answers those directly from SQLite in single-digit milliseconds and leaves the model for the
genuinely open-ended ones.

The questions are hardcoded; **the answers are not**. Every answer is computed from the live
database at request time, so a cached question can never serve a stale number -- which is the whole
reason this is a router over SQL rather than a dictionary of canned text.

    python src/faq.py "what is the highest risk event"     # answer + which entry matched
    python src/faq.py --list                               # the whole question bank
    python src/faq.py --selftest                           # every question must match its own entry
"""
from __future__ import annotations

import store

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "portguard.db"

# Words that carry no topic signal. Kept deliberately small: dropping too much makes distinct
# questions collide (e.g. "cost to reroute" vs "cost to wait" differ only in their last word).
_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "can", "could", "should",
    "would", "will", "of", "to", "for", "in", "on", "at", "by", "with", "and", "or", "if", "we",
    "our", "us", "i", "me", "my", "you", "your", "it", "its", "this", "that", "these", "those",
    "what", "whats", "which", "who", "how", "when", "where", "why", "any", "some", "there", "be",
    "have", "has", "had", "been", "get", "got", "show", "tell", "give", "list", "please", "about",
    # Generic request verbs. They read as topical but appear across every entry, so on their own they
    # were enough to match: "summarise the news in gaza" scored 0.40 against "summarise the shipments"
    # on the single shared word, and "explain quantum entanglement" against "explain portguard".
    "summarise", "summarize", "explain", "describe", "summary",
}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """Crude but sufficient: fold plurals and past tense so 'shipments', 'shipment' and
    'drafted', 'draft' each land on one token. Not a real stemmer and does not need to be."""
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-1] if word.endswith("eed") else word[:-2]
    return word


def normalise(text: str, keep_stopwords: bool = False) -> list[str]:
    """Question -> content tokens. Lowercase, punctuation dropped, plurals and -ed folded.

    `keep_stopwords` powers the fallback index: questions like "what are you" or "what should we do"
    are *entirely* stopwords, so the content-token index cannot see them at all. Matching those on
    their full token set is the difference between answering them instantly and shipping them to a
    40-second model call.
    """
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if keep_stopwords or w not in _STOP]


class Entry(NamedTuple):
    """One answerable topic: the phrasings that should reach it, and how to answer it."""
    key: str
    questions: tuple[str, ...]
    answer: Callable[[sqlite3.Connection], str]
    must: frozenset[str] = frozenset()  # tokens a question MUST contain to match this entry


class Match(NamedTuple):
    entry: Entry
    score: float
    question: str


# --------------------------------------------------------------------------- formatting helpers


def _money(value: Optional[float]) -> str:
    """Thousands-separated USD. Never locale-dependent -- see the fmtMoney note in frontend/src/api.ts."""
    if value is None:
        return "n/a"
    return "$" + f"{round(float(value)):,}"


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.0f}%"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _cite(urls: Iterable[str], start: int = 1) -> str:
    """Numbered markdown links. The model used to emit the literal placeholder '[n]'; these are real."""
    links = [f"[{i}]({u})" for i, u in enumerate(dict.fromkeys(u for u in urls if u), start=start)]
    return (" Sources: " + " ".join(links)) if links else ""


def _event_urls(conn: sqlite3.Connection, event_id: str, limit: int = 4) -> list[str]:
    return [r["source_url"] for r in _rows(conn, """
        SELECT s.source_url FROM signals s JOIN event_signals es USING (signal_id)
        WHERE es.event_id = ? ORDER BY s.published_at DESC LIMIT ?""", (event_id, limit))]


def _no_data(subject: str) -> str:
    return f"There is no {subject} in the database right now."


# --------------------------------------------------------------------------- answerers


def a_top_risk(conn: sqlite3.Connection) -> str:
    row = _one(conn, """SELECT d.*, e.title, e.location_name, e.confidence, e.n_publishers
                        FROM decision_logs d LEFT JOIN events e USING (event_id)
                        WHERE d.risk_score IS NOT NULL ORDER BY d.risk_score DESC LIMIT 1""")
    if not row:
        return _no_data("scored risk")
    cites = _cite(json.loads(row["citations"] or "[]"))
    where = f" at {row['location_name']}" if row["location_name"] else ""
    corroboration = ""
    if row["n_publishers"]:
        corroboration = (f" Corroborated by {row['n_publishers']} publisher(s), "
                         f"confidence {row['confidence']:.2f}.")
    return (f"The highest-scoring risk is **{row['title'] or row['event_id']}**{where} — "
            f"risk **{row['risk_level']}** ({row['risk_score']:.2f}), affecting **{row['shipment_id']}**. "
            f"Recommended action: **{(row['recommended_action'] or 'monitor').replace('_', ' ')}**, "
            f"estimated cost {_money(row['estimated_cost_usd'])}.{corroboration}{cites}")


def a_risk_counts(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT risk_level, COUNT(*) n FROM decision_logs
                          WHERE risk_level IS NOT NULL GROUP BY risk_level""")
    if not rows:
        return _no_data("risk assessment")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    parts = sorted(((r["risk_level"], r["n"]) for r in rows), key=lambda x: order.get(x[0], 9))
    total = sum(n for _, n in parts)
    return (f"There are **{total} scored shipment-event pairs**: "
            + ", ".join(f"**{n}** {lvl}" for lvl, n in parts) + ".")


def a_shipments_overview(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, "SELECT * FROM shipments ORDER BY eta")
    if not rows:
        return _no_data("shipment")
    total = sum(r["cargo_value_usd"] or 0 for r in rows)
    lines = [f"- **{r['shipment_id']}** — {r['carrier']}, {r['vessel_name']}, "
             f"{r['container_count']} containers, {_money(r['cargo_value_usd'])}, ETA {r['eta'][:10]}"
             for r in rows]
    return (f"**{len(rows)} shipments** under management, {_money(total)} of cargo value in total.\n"
            + "\n".join(lines))


def a_shipments_at_risk(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, score, risk_level, title FROM (
                              SELECT d.shipment_id, d.risk_score AS score, d.risk_level, e.title,
                                     ROW_NUMBER() OVER (PARTITION BY d.shipment_id
                                                        ORDER BY d.risk_score DESC) AS rn
                              FROM decision_logs d LEFT JOIN events e USING (event_id)
                              WHERE d.risk_level IN ('high','critical')) ranked
                          WHERE rn = 1 ORDER BY score DESC""")
    if not rows:
        return "No shipment is currently at high or critical risk."
    lines = [f"- **{r['shipment_id']}** — {r['risk_level']} ({r['score']:.2f}) from *{r['title']}*" for r in rows]
    return f"**{len(rows)} shipments** are at high or critical risk:\n" + "\n".join(lines)


def a_value_at_risk(conn: sqlite3.Connection) -> str:
    row = _one(conn, """SELECT COUNT(DISTINCT s.shipment_id) n, SUM(s.cargo_value_usd) v FROM shipments s
                        WHERE s.shipment_id IN (SELECT shipment_id FROM decision_logs
                                                WHERE risk_level IN ('high','critical'))""")
    total = _one(conn, "SELECT SUM(cargo_value_usd) v FROM shipments")
    if not row or not row["n"]:
        return "No cargo value is currently exposed to a high or critical risk event."
    share = 100 * (row["v"] or 0) / (total["v"] or 1)
    return (f"**{_money(row['v'])}** of cargo value sits on **{row['n']} at-risk shipments** — "
            f"{share:.0f}% of the {_money(total['v'])} total book.")


def a_force_majeure(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, recommended_action, estimated_cost_usd, title, citations
                          FROM (
                              SELECT d.shipment_id, d.recommended_action, d.estimated_cost_usd,
                                     e.title, d.citations,
                                     ROW_NUMBER() OVER (PARTITION BY d.shipment_id
                                                        ORDER BY d.risk_score DESC) AS rn
                              FROM decision_logs d LEFT JOIN events e USING (event_id)
                              WHERE d.force_majeure_applicable = 1) ranked
                          WHERE rn = 1""")
    if not rows:
        return ("No shipment currently qualifies for force majeure. PortGuard marks a clause as "
                "applicable only when the event type is named in the Bill of Lading's clause text.")
    lines = [f"- **{r['shipment_id']}** — triggered by *{r['title']}*, "
             f"recommended action **{(r['recommended_action'] or '').replace('_', ' ')}**" for r in rows]
    urls = [u for r in rows for u in json.loads(r["citations"] or "[]")[:2]]
    return (f"**{len(rows)} shipments** have force majeure available:\n" + "\n".join(lines) + _cite(urls))


def a_fm_deadline(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, notice_period_hours, force_majeure_clause FROM shipments
                          WHERE notice_period_hours IS NOT NULL ORDER BY notice_period_hours""")
    if not rows:
        return _no_data("notice period on file")
    lines = [f"- **{r['shipment_id']}** — written notice within **{r['notice_period_hours']} hours**"
             for r in rows]
    return ("Notice periods are contractual and start when the event becomes known:\n" + "\n".join(lines)
            + "\nMiss the window and the clause is typically unenforceable even if the event qualifies.")


def a_fm_meaning(conn: sqlite3.Connection) -> str:
    row = _one(conn, "SELECT force_majeure_clause FROM shipments WHERE force_majeure_clause IS NOT NULL LIMIT 1")
    clause = f"\n\nExample clause on file:\n> {row['force_majeure_clause']}" if row else ""
    return ("Force majeure excuses a party from contractual performance when a named, uncontrollable "
            "event prevents it. PortGuard treats it as available only when three things hold: the "
            "event type is named in the clause, the disruption is on the shipment's route, and the "
            "notice window has not expired." + clause)


def a_options_cost(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, input_payload FROM decision_logs
                          WHERE input_payload IS NOT NULL ORDER BY risk_score DESC LIMIT 1""")
    if not rows:
        return _no_data("priced option")
    exposure = (json.loads(rows[0]["input_payload"]).get("analysis") or {}).get("exposure") or {}
    options = exposure.get("options") or []
    if not options:
        return _no_data("priced option")
    lines = []
    for option in options:
        unavailable = "" if option.get("available", True) else " *(not available under this contract)*"
        note = f" — {option['note']}" if option.get("note") else ""
        lines.append(f"- **{option['action'].replace('_', ' ')}** — "
                     f"{_money(option.get('cost_usd'))}{unavailable}{note}")
    cheapest = exposure.get("cheapest_action")
    return (f"Priced options for **{rows[0]['shipment_id']}** (the highest-risk shipment):\n"
            + "\n".join(lines)
            + (f"\n\nCheapest available: **{cheapest.replace('_', ' ')}**." if cheapest else ""))


def a_total_exposure(conn: sqlite3.Connection) -> str:
    row = _one(conn, """SELECT SUM(estimated_cost_usd) v, COUNT(*) n FROM decision_logs
                        WHERE estimated_cost_usd IS NOT NULL""")
    if not row or not row["n"]:
        return _no_data("cost estimate")
    return (f"Across **{row['n']} decisions**, the recommended actions carry an estimated "
            f"**{_money(row['v'])}** in total cost. These are modelled from each Bill of Lading's own "
            f"terms (demurrage, detention, late-delivery penalty), not market quotes.")


def a_cost_avoided(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT input_payload, estimated_cost_usd FROM decision_logs
                          WHERE input_payload IS NOT NULL AND estimated_cost_usd IS NOT NULL""")
    saved = 0.0
    for r in rows:
        options = ((json.loads(r["input_payload"]).get("analysis") or {}).get("exposure") or {}).get("options") or []
        costs = [o.get("cost_usd") for o in options if o.get("available", True) and o.get("cost_usd") is not None]
        if costs:
            saved += max(costs) - (r["estimated_cost_usd"] or 0)
    if not rows:
        return _no_data("decision to compare")
    return (f"Taking the recommended action instead of the worst available one avoids an estimated "
            f"**{_money(saved)}** across {len(rows)} decisions. This is a modelled difference between "
            f"priced options, not realised savings.")


def a_demurrage(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, demurrage_usd_per_day, detention_usd_per_day, free_time_days,
                          late_delivery_penalty_usd_per_day FROM shipments""")
    if not rows:
        return _no_data("contract term")
    lines = [f"- **{r['shipment_id']}** — demurrage {_money(r['demurrage_usd_per_day'])}/day, "
             f"detention {_money(r['detention_usd_per_day'])}/day, {r['free_time_days']} free days, "
             f"late penalty {_money(r['late_delivery_penalty_usd_per_day'])}/day" for r in rows]
    return "Per-day contract costs on file:\n" + "\n".join(lines)


def a_recommendations(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT recommended_action, COUNT(*) n FROM decision_logs
                          WHERE recommended_action IS NOT NULL GROUP BY recommended_action ORDER BY n DESC""")
    if not rows:
        return _no_data("recommendation")
    return ("Current recommendations across all shipment-event pairs: "
            + ", ".join(f"**{r['n']}x** {r['recommended_action'].replace('_', ' ')}" for r in rows) + ".")


def a_pending_approval(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT d.shipment_id, d.recommended_action, d.risk_level, e.title
                          FROM decision_logs d LEFT JOIN events e USING (event_id)
                          WHERE d.human_decision IS NULL AND d.recommended_action IS NOT NULL
                          ORDER BY d.risk_score DESC LIMIT 10""")
    if not rows:
        return "Every decision has been reviewed — nothing is waiting for approval."
    lines = [f"- **{r['shipment_id']}** — {(r['recommended_action'] or '').replace('_', ' ')} "
             f"({r['risk_level']}) for *{r['title']}*" for r in rows]
    return f"**{len(rows)} decisions** are awaiting human approval:\n" + "\n".join(lines)


def a_drafts(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, "SELECT kind, COUNT(*) n FROM action_drafts GROUP BY kind ORDER BY n DESC")
    if not rows:
        return _no_data("drafted document")
    return ("Drafted and ready for review: "
            + ", ".join(f"**{r['n']}** {r['kind'].replace('_', ' ')}" for r in rows)
            + ". Every draft needs human approval before it is sent.")


def a_events_recent(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT title, location_name, severity, n_publishers, confidence, event_id
                          FROM events ORDER BY is_structured DESC, n_signals DESC LIMIT 8""")
    if not rows:
        return _no_data("event")
    lines = []
    for r in rows:
        severity = f", {r['severity']}" if r["severity"] else ""
        lines.append(f"- **{r['title'][:95]}** — {r['location_name']}{severity}, "
                     f"{r['n_publishers']} publisher(s), confidence {r['confidence']:.2f}")
    return f"The {len(rows)} most significant active events:\n" + "\n".join(lines)


def a_events_by_type(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type ORDER BY n DESC")
    if not rows:
        return _no_data("event")
    return ("Events by type: "
            + ", ".join(f"**{r['n']}** {r['event_type'].replace('_', ' ')}" for r in rows) + ".")


def a_traffic(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT name, metric, avg_7d, baseline_avg, pct_change, latest_date
                          FROM traffic_stats WHERE pct_change IS NOT NULL
                          ORDER BY pct_change LIMIT 8""")
    if not rows:
        return _no_data("traffic measurement")
    lines = [f"- **{r['name']}** — {_pct(r['pct_change'])} ({r['avg_7d']:.1f}/day vs "
             f"{r['baseline_avg']:.1f} baseline), as of {r['latest_date']}" for r in rows]
    return ("Largest measured traffic changes vs the 90-day baseline (IMF PortWatch daily data):\n"
            + "\n".join(lines))


def a_chokepoint_status(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT name, avg_7d, baseline_avg, pct_change FROM traffic_stats
                          WHERE location_id LIKE 'chokepoint%' AND pct_change IS NOT NULL
                          ORDER BY pct_change""")
    if not rows:
        return _no_data("chokepoint traffic measurement")
    lines = [f"- **{r['name']}** — {_pct(r['pct_change'])} ({r['avg_7d']:.1f} transits/day)" for r in rows]
    return "Chokepoint transit volumes against their 90-day baseline:\n" + "\n".join(lines)


def a_sources(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, "SELECT source, COUNT(*) n FROM signals GROUP BY source ORDER BY n DESC")
    if not rows:
        return _no_data("ingested signal")
    total = sum(r["n"] for r in rows)
    top = ", ".join(f"{r['source'].replace('_', ' ')} ({r['n']})" for r in rows[:8])
    return (f"PortGuard is ingesting **{len(rows)} sources**, **{total} signals** in total. "
            f"Largest: {top}. They are tiered by credibility — official bodies (IMF PortWatch, GDACS, "
            f"NGA, NOAA, JTWC, USGS) outrank trade press, which outranks news aggregators.")


def a_freshness(conn: sqlite3.Connection) -> str:
    row = _one(conn, "SELECT MAX(ingested_at) t FROM signals")
    if not row or not row["t"]:
        return _no_data("ingested signal")
    return (f"The newest signal in the database was ingested at **{row['t']}**. Note that a source "
            f"being quiet is not the same as the pipeline being down — the sidebar shows both clocks "
            f"separately (when each source was last polled, and the age of its newest article).")


def a_confidence(conn: sqlite3.Connection) -> str:
    row = _one(conn, """SELECT COUNT(*) n, AVG(confidence) c,
                        SUM(CASE WHEN n_publishers = 1 THEN 1 ELSE 0 END) solo FROM events""")
    if not row or not row["n"]:
        return _no_data("event")
    mean = round(float(row["c"] or 0), 2)
    return (f"Across **{row['n']} events**, mean confidence is **{mean}**. "
            f"**{row['solo']}** rest on a single publisher. Confidence starts from the most "
            f"authoritative source in the cluster and rises with each independent publisher that "
            f"corroborates it, so one official advisory can outrank several aggregators.")


def a_what_is_portguard(conn: sqlite3.Connection) -> str:
    return ("PortGuard AI monitors maritime disruption and turns it into shipment-specific, "
            "contract-aware decisions. It ingests official hazard data, government warnings and "
            "maritime trade press; clusters reports into events; matches each event to your shipments "
            "by port and chokepoint; prices the options (wait, reroute, expedite, hold, force majeure) "
            "from each Bill of Lading's own terms; and drafts the notice or customer email for a human "
            "to approve. Nothing is sent automatically.")


def a_how_costs(conn: sqlite3.Connection) -> str:
    return ("Costs are rule-based, not model-guessed. For each option PortGuard computes delay days "
            "and multiplies them by the contract's own demurrage, detention and late-delivery penalty "
            "rates, adding rerouting distance cost where a deviation is permitted. Every figure is an "
            "estimate from stated assumptions, and the assumptions travel with the number.")


def a_limitations(conn: sqlite3.Connection) -> str:
    row = _one(conn, """SELECT COUNT(CASE WHEN vessel_source = 'public_registry' THEN 1 END) real_ships,
                               COUNT(*) n FROM shipments""")
    fleet = ""
    if row and row["n"]:
        fleet = (f" **{row['real_ships']} of {row['n']}** vessels in the book are real ships with "
                 f"verifiable IMO numbers; their carriers are attributed from the vessel name rather "
                 f"than looked up in a registry.")
    return ("Known limits, stated plainly: ship positions are **estimated** from the schedule along a "
            "real sea route, not live AIS — no ingested source carries vessel positions." + fleet +
            " Every contract term and cargo value is **synthetic**, because real Bills of Lading are "
            "commercially confidential and published nowhere. Cost figures are modelled from those "
            "synthetic terms, not market quotes. Events resting on a single publisher are labelled "
            "as such, and confidence reflects who reported them, not just how many did.")


def a_vessel_positions(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT vessel_name, imo, carrier, etd, eta, vessel_source
                          FROM shipments ORDER BY eta""")
    if not rows:
        return _no_data("vessel")
    lines = []
    for r in rows:
        imo = f" · IMO {r['imo']}" if r["imo"] else ""
        lines.append(f"- **{r['vessel_name']}**{imo} ({r['carrier']}) — "
                     f"ETD {(r['etd'] or '')[:10]}, ETA {r['eta'][:10]}")
    return ("These are real vessels with verifiable IMO numbers, but their positions on the map are "
            "**estimated** — computed from elapsed time between ETD and ETA along the real sea route. "
            "PortGuard ingests no AIS feed, so this is a scheduling estimate, not live tracking.\n"
            + "\n".join(lines))


def a_carriers(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT carrier, COUNT(*) n, SUM(cargo_value_usd) v FROM shipments
                          GROUP BY carrier ORDER BY n DESC, v DESC""")
    if not rows:
        return _no_data("carrier")
    lines = [f"- **{r['carrier']}** — {r['n']} shipment(s), {_money(r['v'])}" for r in rows]
    return f"**{len(rows)} carriers** in the book:\n" + "\n".join(lines)


def a_routes(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT s.shipment_id, s.route_chokepoints, po.port_name AS pol, pd.port_name AS pod
                          FROM shipments s LEFT JOIN ports po ON po.port_id = s.pol_port_id
                          LEFT JOIN ports pd ON pd.port_id = s.pod_port_id""")
    if not rows:
        return _no_data("route")
    lines = []
    for r in rows:
        chokes = [c.replace("chokepoint:", "").replace("-", " ") for c in json.loads(r["route_chokepoints"] or "[]")]
        lines.append(f"- **{r['shipment_id']}** — {r['pol']} → {r['pod']}"
                     + (f", via {', '.join(chokes)}" if chokes else ", no tracked chokepoint"))
    return "Routes and the chokepoints they transit:\n" + "\n".join(lines)


def a_incoterms(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, "SELECT incoterm, COUNT(*) n FROM shipments WHERE incoterm IS NOT NULL GROUP BY incoterm")
    if not rows:
        return _no_data("incoterm")
    return ("Incoterms in the book: " + ", ".join(f"**{r['incoterm']}** ({r['n']})" for r in rows)
            + ". The incoterm decides who carries the risk once cargo is loaded, so it changes who "
              "absorbs a delay cost even when the disruption is identical.")


def a_deviation(conn: sqlite3.Connection) -> str:
    rows = _rows(conn, """SELECT shipment_id, deviation_permitted, governing_law FROM shipments""")
    if not rows:
        return _no_data("shipment")
    yes = [r["shipment_id"] for r in rows if r["deviation_permitted"]]
    no = [r["shipment_id"] for r in rows if not r["deviation_permitted"]]
    return (f"Rerouting is contractually permitted for **{len(yes)}** shipments"
            + (f" ({', '.join(yes)})" if yes else "")
            + (f", and not permitted for **{len(no)}** ({', '.join(no)}) — for those, a reroute is a "
               f"breach unless the customer agrees first." if no else "."))


# --------------------------------------------------------------------------- the question bank

# Each entry lists the phrasings that should route to it. `must` tokens guard entries that would
# otherwise steal questions from a neighbour (cost/reroute/force-majeure overlap heavily).
BANK: tuple[Entry, ...] = (
    Entry("top_risk", (
        "what is the highest risk event right now", "what is the biggest risk",
        "what is the most urgent thing", "what should i look at first",
        "which event is most severe", "what is the top risk", "worst risk currently",
        "which disruption matters most", "what needs my attention",
    ), a_top_risk),
    Entry("risk_counts", (
        "how many risks are there", "how many high risk shipments", "risk breakdown",
        "how many critical risks", "summarise risk levels", "how many events are critical",
    ), a_risk_counts),
    Entry("shipments_overview", (
        "how many shipments do we have", "list our shipments", "what shipments are we tracking",
        "show me the fleet", "what is in the book", "how many containers",
        "what is our total cargo value", "summarise the shipments",
    ), a_shipments_overview),
    Entry("shipments_at_risk", (
        "which shipments are affected", "which shipments are at risk",
        "which shipments are in danger", "what shipments are disrupted",
        "which cargo is exposed", "which shipments need action",
        "are any shipments affected by the red sea", "which shipments are impacted",
    ), a_shipments_at_risk),
    Entry("value_at_risk", (
        "how much cargo value is at risk", "what is our exposure in dollars",
        "how much money is exposed", "value at risk", "what is the financial exposure",
    ), a_value_at_risk, frozenset({"value", "money", "dollar", "financial", "exposure"})),
    Entry("force_majeure", (
        "can we invoke force majeure", "which shipments qualify for force majeure",
        "is force majeure available", "do we have a force majeure case",
        "should we declare force majeure", "force majeure status",
    ), a_force_majeure, frozenset({"majeure"})),
    Entry("fm_deadline", (
        "what is the force majeure notice deadline", "how long do we have to give notice",
        "when must we notify", "what is the notice period", "notice window for force majeure",
    ), a_fm_deadline, frozenset({"notice", "deadline", "notify"})),
    Entry("fm_meaning", (
        "what is force majeure", "what does force majeure mean", "explain force majeure",
        "how does force majeure work", "when does force majeure apply",
    ), a_fm_meaning, frozenset({"majeure"})),
    Entry("options_cost", (
        "what does it cost to reroute", "what are our options", "what is the cost of waiting",
        "compare the options", "reroute or wait", "how much would rerouting cost",
        "what are the priced options", "what is the cheapest option", "should we reroute",
    ), a_options_cost),
    Entry("total_exposure", (
        "what is our total cost exposure", "how much will this cost us in total",
        "total estimated cost", "what is the overall cost",
    ), a_total_exposure, frozenset({"total", "overall"})),
    Entry("cost_avoided", (
        "how much have we saved", "what is the cost avoided", "what did portguard save us",
        "how much money did we avoid losing", "what is the roi", "savings so far",
    ), a_cost_avoided, frozenset({"saved", "save", "avoided", "avoid", "roi", "saving"})),
    Entry("demurrage", (
        "what is the demurrage rate", "what are the per day costs", "detention charges",
        "how much is demurrage", "what are the daily penalties", "late delivery penalty",
        "what is the free time", "contract rates",
    ), a_demurrage),
    Entry("recommendations", (
        "what do you recommend", "what actions are recommended", "what should we do",
        "summarise the recommendations", "what is portguard recommending",
    ), a_recommendations),
    Entry("pending_approval", (
        "what is waiting for approval", "what needs approving", "what is pending",
        "what decisions need review", "is anything awaiting sign off", "what is unapproved",
    ), a_pending_approval, frozenset({"approval", "approving", "pending", "review", "sign", "unapproved"})),
    Entry("drafts", (
        "what drafts are ready", "have you drafted anything", "show me the draft notices",
        "what documents are prepared", "is the customer email ready",
        "what have you prepared for me", "is the paperwork ready",
    ), a_drafts, frozenset({"draft", "document", "email", "notice", "letter", "prepared",
                            "paperwork"})),
    Entry("events_recent", (
        "what events are happening", "what is going on right now", "recent disruptions",
        "what happened today", "show me the events", "what is the latest",
        "any new disruptions", "what is active",
    ), a_events_recent),
    Entry("events_by_type", (
        "what types of events are there", "breakdown of events by type",
        "how many weather events", "how many strikes", "what kind of disruptions",
    ), a_events_by_type),
    Entry("traffic", (
        "what is happening to traffic", "which ports are quieter", "traffic changes",
        "where has volume dropped", "what do the transit numbers show",
        "is traffic down anywhere", "show me traffic trends",
    ), a_traffic),
    Entry("chokepoint_status", (
        "what is the status of the suez canal", "how is hormuz doing",
        "is the panama canal affected", "what about the red sea",
        "chokepoint status", "how are the straits", "is malacca disrupted",
        "what is happening at bab el mandeb", "is the bosporus open",
        "how is the taiwan strait", "what is the dover strait like",
    ), a_chokepoint_status),
    Entry("sources", (
        "what sources do you use", "where does this data come from",
        "how many sources are there", "what feeds do you ingest",
        "is this data reliable", "who publishes this", "what feeds do you read",
    ), a_sources, frozenset({"source", "data", "feed", "publishes", "reliable", "come"})),
    Entry("freshness", (
        "how fresh is this data", "when was this last updated", "how old is the data",
        "when did you last check", "is this data current",
    ), a_freshness, frozenset({"fresh", "updated", "old", "check", "current", "last"})),
    Entry("confidence", (
        "how confident are you", "how well corroborated is this",
        "how many publishers confirm this", "what is the confidence score",
        "how do you score confidence", "is this verified",
    ), a_confidence, frozenset({"confident", "confidence", "corroborated", "publisher", "verified"})),
    Entry("what_is_portguard", (
        "what is portguard", "what does this app do", "explain portguard",
        "what are you", "how does this work", "what is this dashboard",
        "what does portguard do", "what problem does this solve",
    ), a_what_is_portguard, frozenset({"portguard", "app", "dashboard", "work", "solve"})),
    Entry("how_costs", (
        "how do you calculate cost", "where do the numbers come from",
        "how are the estimates produced", "how is cost computed", "are these real quotes",
    ), a_how_costs, frozenset({"calculate", "computed", "estimate", "quote", "number"})),
    Entry("limitations", (
        "what are the limitations", "what can you not do", "what is fake in this demo",
        "what data is synthetic", "what are the caveats", "how accurate is this",
        "is this real data",
    ), a_limitations),
    Entry("vessel_positions", (
        "where are the ships", "where is the vessel now", "show me vessel positions",
        "are these positions real", "is this live ais", "how do you know where ships are",
        "what vessels are we tracking",
    ), a_vessel_positions, frozenset({"ship", "vessel", "position", "ais", "where"})),
    Entry("carriers", (
        "which carriers do we use", "how many carriers", "who are the carriers",
        "break down by carrier", "which shipping line",
    ), a_carriers, frozenset({"carrier", "line"})),
    Entry("routes", (
        "what routes do we run", "which chokepoints do we transit",
        "show me the routes", "what ports do we call at", "where do the ships sail",
    ), a_routes, frozenset({"route", "chokepoint", "port", "sail", "transit"})),
    Entry("incoterms", (
        "what incoterms do we have", "who bears the risk", "what does cif mean for us",
        "incoterm breakdown",
    ), a_incoterms, frozenset({"incoterm", "cif", "fob", "bear"})),
    Entry("deviation", (
        "can we reroute legally", "is deviation permitted", "are we allowed to change route",
        "does the contract allow rerouting", "what does the contract say about deviation",
        "do we have permission to divert", "can we divert the vessel",
    ), a_deviation, frozenset({"deviation", "permitted", "permission", "allowed", "legally",
                               "contract", "divert"})),
)

# Every phrasing in the bank, pre-tokenised once at import. This is what makes a lookup microseconds.
# Entries declare `must` in plain English; questions arrive stemmed. Stem the guards once at import
# so "how much have we saved" ('sav') can still satisfy a guard written as "saved".
BANK = tuple(e._replace(must=frozenset(_stem(w) for w in e.must)) for e in BANK)

_INDEX: list[tuple[Entry, str, frozenset[str]]] = [
    (entry, question, frozenset(normalise(question)))
    for entry in BANK for question in entry.questions
]
# Same bank, tokenised without stopword removal. Only consulted when the content-token pass finds
# nothing, so it never overrides a confident topical match with a coincidence of filler words.
_INDEX_FULL: list[tuple[Entry, str, frozenset[str]]] = [
    (entry, question, frozenset(normalise(question, keep_stopwords=True)))
    for entry in BANK for question in entry.questions
]
QUESTION_COUNT = sum(len(e.questions) for e in BANK)

# Below this, the question is not one of ours and belongs to the model. Tuned so that paraphrases
# match ("which boxes are in trouble" -> shipments_at_risk) but genuinely new questions do not.
THRESHOLD = 0.34
# Minimum share of the asker's own content words the matched phrasing has to account for.
COVERAGE = 0.4
# The stopword-inclusive fallback scores much higher by default (every question shares "what", "is",
# "the"), so it needs a far stricter bar to mean anything.
FULL_THRESHOLD = 0.62


def _best(asked: frozenset[str], index: list[tuple[Entry, str, frozenset[str]]],
          check_must: bool) -> Optional[Match]:
    best: Optional[Match] = None
    for entry, phrasing, tokens in index:
        if check_must and entry.must and not (asked & entry.must):
            continue
        overlap = len(asked & tokens)
        if not overlap:
            continue
        # The match must also explain a decent share of what was ASKED, not just resemble a stored
        # phrasing. Dice alone is too forgiving when the stored phrasing is short: "write a
        # two-sentence executive summary of our Red Sea posture for the board" scored 0.40 against
        # "what about the red sea" on {red, sea} and was answered with a transit-volume table. That
        # is a request to compose something, and it belongs to the model.
        if overlap / len(asked) < COVERAGE:
            continue
        # Symmetric overlap (Dice): rewards covering the stored phrasing without letting a long
        # rambling question score highly just because it happens to contain a couple of the words.
        score = 2 * overlap / (len(asked) + len(tokens))
        if best is None or score > best.score:
            best = Match(entry, score, phrasing)
    return best


def match(question: str) -> Optional[Match]:
    """Best bank entry for a question, or None when the model should answer it instead."""
    content = frozenset(normalise(question))
    hit = _best(content, _INDEX, check_must=True)
    if hit and hit.score >= THRESHOLD:
        return hit
    # Nothing topical matched. Retry on full tokens ONLY for questions that are essentially all
    # filler ("what are you", "what should we do") -- the case the content index structurally cannot
    # see. Allowing it for questions that do carry content words lets stopwords alone carry a match:
    # "what is the weather in paris tomorrow" scored 0.67 against "what is in the book" on
    # {what, is, in, the} and was confidently answered with the shipment list.
    if len(content) > 1:
        return None
    full = _best(frozenset(normalise(question, keep_stopwords=True)), _INDEX_FULL, check_must=False)
    return full if full and full.score >= FULL_THRESHOLD else None


def answer(question: str, db: Path = DB_PATH) -> Optional[tuple[str, str]]:
    """(answer_markdown, matched_entry_key), or None if no bank entry is confident enough."""
    hit = match(question)
    if not hit:
        return None
    with store.connect(db) as conn:
        return hit.entry.answer(conn), hit.entry.key


def selftest() -> int:
    """Every stored phrasing must route to its own entry. Catches entries stealing each other's
    questions when a new one is added -- the failure mode that silently degrades the whole router."""
    wrong = []
    for entry in BANK:
        for question in entry.questions:
            hit = match(question)
            if hit is None or hit.entry.key != entry.key:
                wrong.append((question, entry.key, hit.entry.key if hit else "NO MATCH"))
    for question, expected, got in wrong:
        print(f"  {question!r}\n    expected {expected}, got {got}")
    print(f"{QUESTION_COUNT - len(wrong)}/{QUESTION_COUNT} questions route correctly "
          f"({len(BANK)} entries)")
    return 1 if wrong else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("question", nargs="*", help="question to answer")
    parser.add_argument("--list", action="store_true", help="print the whole question bank")
    parser.add_argument("--selftest", action="store_true", help="check every question routes to its own entry")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.list:
        for entry in BANK:
            print(f"\n{entry.key}")
            for question in entry.questions:
                print(f"  - {question}")
        print(f"\n{QUESTION_COUNT} questions across {len(BANK)} entries")
        return 0
    if not args.question:
        parser.error("give a question, --list or --selftest")
    hit = match(" ".join(args.question))
    if not hit:
        print("no bank entry matched; this question would go to the model")
        return 1
    with store.connect(args.db) as conn:
        print(hit.entry.answer(conn))
    print(f"\n[matched {hit.entry.key} via {hit.question!r}, score {hit.score:.2f}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
