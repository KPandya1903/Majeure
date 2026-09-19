# PortGuard AI — Architecture

PortGuard AI watches for real-world maritime disruptions and decides, for each affected
shipment, what the disruption means under that shipment's Bill of Lading (B/L) contract terms.
For the product idea, see [MAIN_IDEA.md](MAIN_IDEA.md). For a file-by-file guide, see the
[README](../README.md).

## End-to-end flow

```
 TRACK A: STRUCTURED                           TRACK B: UNSTRUCTURED NEWS
 IMF PortWatch          GDACS live alerts      Trade-press RSS (5)     GDELT raw GKG     Google News
 (ports + curated       (cyclones, quakes,     gCaptain, MarEx,        (15-min files)    (RSS search)
  disruption events)     floods, …)            Seatrade, HSN, Splash
       │                     │                        │                      │                │
 fetch_portwatch.py    fetch_gdacs.py            parse_rss.py          fetch_gdelt.py   fetch_google_news.py
       │                 proximity:                   │               relevance filter  relevance filter
       │                 ports within radius          │                      │                │
       ├── ports CSV ────────┴── gazetteer: port / chokepoint tagging ───────┴────────────────┘
       ▼                                  ▼
 portwatch_ports.csv, *_signals.json  (one JSON array of Xtract Signals per source family)
                                  │
                                  ▼
                             init_db.py ──► data/portguard.db
                                             ports · signals · signal_ports
                                  ▲          shipments · decision_logs
 samples/shipments.sample.json ──► load_shipments.py
                                  │
                                  ▼
                           run_nemotron.py
      1. match signals ⇄ shipments (port of loading/discharge, transshipment, route chokepoint)
      2. build a validated NemotronPayload per (signal, shipment) pair (≤5 per shipment per run)
      3. POST to Nemotron (OpenAI-compatible /chat/completions)
      4. parse + validate NemotronDecision  ──►  decision_logs (with citations)
```

* **Track A (structured):** IMF PortWatch gives the reference list of ports and a curated
  database of disruption events, already tagged with affected port and chokepoint IDs. GDACS
  adds live hazard alerts; each one is linked to the PortWatch ports within a per-type radius
  (**proximity** matching).
* **Track B (unstructured):** five maritime trade-press feeds, GDELT's raw news firehose, and
  a Google News search. Firehose sources are filtered to headlines about shipping *and* a
  disruption. Every article is pre-tagged with a rule-based `event_type` and a
  `port_location` found by matching the text against PortWatch port names and 31 chokepoints
  and sea areas (the "gazetteer").
* Both tracks produce the same **Xtract Signal** shape
  ([SCHEMAS.md §2](SCHEMAS.md#2-xtract-signal-output)) and land in one `signals` table.
* **Decision stage:** `run_nemotron.py` pairs signals with shipments whose route touches the
  same port or chokepoint. It sends each pair to Nemotron with the contract terms and stores
  the answer (risk score, level, action, rationale, and citations) in `decision_logs`.

Every signal keeps its `source_url`, and every decision cites it. That is how a decision can
be traced back to its evidence.

## Stages and contracts

| Stage | Script | Reads | Writes | Contract |
|---|---|---|---|---|
| 1 | `fetch_portwatch.py` | PortWatch ArcGIS | `portwatch_ports.csv`, `portwatch_signals.json` | ports columns = `ports` table; signals = `XtractSignal` |
| 2 | `fetch_gdacs.py` | GDACS RSS, ports CSV, PortWatch signals (to skip duplicates) | `gdacs_signals.json` | `XtractSignal` |
| 3 | `parse_rss.py` | 5 trade-press feeds, ports CSV | `rss_signals.json` | `XtractSignal` |
| 4 | `fetch_gdelt.py` | GDELT GKG files (or Doc API), ports CSV | `gdelt_signals.json` | `XtractSignal` |
| 5 | `fetch_google_news.py` | Google News RSS, ports CSV | `google_news_signals.json` | `XtractSignal` |
| 6 | `init_db.py` | `docs/SCHEMAS.md` (DDL), CSV, all JSON | `portguard.db` | SQL schema in SCHEMAS.md §1 |
| 7 | `load_shipments.py` | shipments JSON | `shipments` table | `Shipment` (= `NemotronPayload.shipment`) |
| 8 | `run_nemotron.py` | `portguard.db`, Nemotron API | `decision_logs`, optional `data/payloads/*.json` | `NemotronPayload` in, `NemotronDecision` out (SCHEMAS.md §3–4) |

Stage 1 must run first: stages 2–5 use its ports CSV (as gazetteer and for proximity), and
stage 2 uses its signals to avoid duplicating GDACS events. Stages 2–5 are independent of
each other, and any of them can fail without blocking the rest.

## Design decisions

* **Idempotent everywhere.** `signal_id = sha1(canonical URL)[:16]`. Re-running any fetcher
  followed by `init_db.py` updates rows in place. The same article seen through two sources
  collapses into one row (the first source loaded wins: PortWatch, GDACS, trade-press
  RSS, GDELT, then Google News; structured data before publishers before aggregators). Shipments upsert by `shipment_id`. `run_nemotron.py` skips pairs that already have
  a decision.
* **JSON files are snapshots; the DB accumulates.** Feeds only hold their latest items and
  GDELT/Google News only cover a time window, so history builds up in SQLite across runs.
* **Human review is kept.** `signals.status` (`new`/`processed`/`dismissed`) and
  `decision_logs.human_decision` (`pending`/`approved`/`overridden`) are never overwritten by
  the pipeline. Dismissed signals are never sent to Nemotron.
* **The docs are the schema.** `init_db.py` executes the DDL from SCHEMAS.md. The Pydantic
  models in `src/schemas.py` mirror the JSON Schemas in SCHEMAS.md, and `MATCH_SQL` in
  `run_nemotron.py` is copied verbatim into SCHEMAS.md.
* **Free sources only; Nemotron behind a standard API.** No API keys are needed for
  ingestion. The HTTP helper backs off on `429`/5xx; GDELT defaults to its raw files, which
  have no rate limit (its Doc API allows one request per 5 s).
  Nemotron is reached through the OpenAI-compatible `/chat/completions` endpoint that NVIDIA
  NIM, vLLM, Ollama, and the hosted NVIDIA API all expose.
* **Fail safe.** A failed fetch exits with code 1 and leaves the previous output file intact
  (writes are atomic). A model reply that can't be parsed is logged and retried on the next
  run.
* **Timestamps** are ISO-8601 UTC strings (`2026-09-19T15:28:46Z`) everywhere.

## Database

Full DDL is in [SCHEMAS.md §1](SCHEMAS.md#1-sqlite-database-schema).

```
ports 1───* signal_ports *───1 signals 1───* decision_logs *───1 shipments
  1                                                                 │
  └──────────────── pol_port_id / pod_port_id ─────────────────────*┘
```
