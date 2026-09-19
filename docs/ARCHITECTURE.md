# PortGuard AI — Architecture

PortGuard AI watches for real-world maritime disruptions and decides, for each affected
shipment, what the disruption means under that shipment's Bill of Lading (B/L) contract terms.
For the product idea, see [MAIN_IDEA.md](MAIN_IDEA.md). For a file-by-file guide, see the
[README](../README.md).

## End-to-end flow

```
 SENSE ─────────────────────────────────────────────────────────────────────────────────────────
 IMF PortWatch          GDACS live alerts      Trade-press RSS (5)     GDELT raw GKG     Google News
 ports + curated events cyclones/quakes/floods gCaptain, MarEx, …      15-min files      RSS search
 + daily traffic        (proximity → ports)                            (relevance filter)(relevance filter)
       │                     │                        │                      │                │
 fetch_portwatch.py    fetch_gdacs.py            parse_rss.py          fetch_gdelt.py   fetch_google_news.py
 fetch_traffic.py            │                        │                      │                │
       └────────── ports CSV = gazetteer + proximity table ─────────────────┴────────────────┘
                                          ▼
 STORE ────────────────────────────── init_db.py ──► data/portguard.db
                                          │            (re-applies Xtract refinements after each load)
 UNDERSTAND ─── xtract.py (optional, Nemotron re-reads news: type, place, severity)
                                          │
 CLUSTER ────── cluster_events.py: 120 reports → ~25 events, corroboration + confidence
                                          │
 SHIPMENTS ──── load_shipments.py (JSON) · extract_contract.py (PDF/TXT → Nemotron → Shipment)
                                          │
 DECIDE ─────── run_nemotron.py, per (event, shipment):
                  match route ⇄ event → payload.analysis = exposure.py (priced options, FM coverage,
                  notice deadline) + traffic + corroboration → Nemotron (or --engine rules) → decision_logs
                                          │
 ACT ────────── draft_actions.py (FM notice + customer email) · alerts.py (Slack/console)
                                          │
 REVIEW ─────── dashboard.py (Streamlit): map, decisions, approve/override, drafts, traffic, backtest,
                ask.py Q&A · backtest.py (historical replay)

 run_pipeline.py runs SENSE → ACT in order, once or every N minutes.
```

* **Two tracks of evidence.** *Structured:* PortWatch curated events (already tagged with
  affected ports and chokepoints) and GDACS hazards (linked to ports within a per-type radius).
  *Unstructured:* trade press, GDELT, and Google News, pre-tagged by rules and optionally
  re-read by Nemotron (Xtract).
* **Events, not headlines.** News attaches to the structured event at the same place
  (HORMUZ-26 absorbs about 70 Hormuz reports), so one decision covers one real-world event,
  with its corroboration counted.
* **Numbers before the model.** `exposure.py` prices waiting, rerouting, and invoking force
  majeure from the contract terms. `fetch_traffic.py` adds measured traffic. Nemotron then
  reasons with those figures instead of guessing them, and `--engine rules` makes the same
  decision without a model.
* **Humans close the loop.** Drafts, alerts, and approve/override happen in the dashboard;
  nothing is sent automatically.

## Stages and contracts

| Stage | Script | Reads | Writes | Needs Nemotron |
|---|---|---|---|---|
| Ingest | `fetch_portwatch.py` | PortWatch ArcGIS | `portwatch_ports.csv`, `portwatch_signals.json` | no |
| Ingest | `fetch_gdacs.py` | GDACS RSS, ports CSV, PortWatch signals | `gdacs_signals.json` | no |
| Ingest | `parse_rss.py` | 5 trade-press feeds, ports CSV | `rss_signals.json` | no |
| Ingest | `fetch_gdelt.py` | GDELT GKG files (or Doc API), ports CSV | `gdelt_signals.json` | no |
| Ingest | `fetch_google_news.py` | Google News RSS, ports CSV | `google_news_signals.json` | no |
| Ingest | `fetch_traffic.py` | PortWatch daily layers, shipment ports | `traffic_daily.csv`, `traffic_stats.csv` | no |
| Store | `init_db.py` | SCHEMAS.md DDL, all CSV/JSON | `portguard.db` | no |
| Shipments | `load_shipments.py` / `extract_contract.py` | shipments JSON / contract PDF | `shipments` | only for contracts |
| Understand | `xtract.py` | news signals | `xtract_results`, refined `signals` | yes |
| Cluster | `cluster_events.py` | signals, `signal_ports` | `events`, `event_signals` | no |
| Decide | `run_nemotron.py` | everything above | `decision_logs`, `data/payloads/` | yes, or `--engine rules` |
| Act | `draft_actions.py` | decisions | `action_drafts` | optional (templates) |
| Act | `alerts.py` | decisions | Slack/console, `alerts_sent` | no |
| Review | `dashboard.py`, `ask.py` | the DB | approvals, draft edits | Q&A only |
| Validate | `backtest.py` | PortWatch history | `data/backtest.json` | optional |

`fetch_portwatch.py` must run first: every other ingester uses its ports CSV, and GDACS uses
its signals to avoid duplicates. Every other ingester can fail without blocking the rest.

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
* **Decisions are per (event, shipment).** One real-world event produces one decision per
  affected shipment, however many headlines report it. Already-decided pairs are skipped;
  `--max-per-shipment` (default 5) caps model calls per run.
* **Planning estimates, stated assumptions.** Delay tables, reroute days, surcharges, and
  haulage rates live at the top of `exposure.py` and travel with every payload
  (`analysis.exposure.assumptions`).
* **Timestamps** are ISO-8601 UTC strings (`2026-09-19T15:28:46Z`) everywhere.

## Database

Full DDL is in [SCHEMAS.md §1](SCHEMAS.md#1-sqlite-database-schema).

```
ports 1───* signal_ports *───1 signals 1───* decision_logs *───1 shipments
  1                               │  1          │ 1                   │
  │                 event_signals *  xtract_results  action_drafts *  │
  │                       *                          alerts_sent *    │
  │                    events                                         │
  └──────────────── pol_port_id / pod_port_id ─────────────────────*──┘
traffic_daily / traffic_stats keyed by location_id (port_id or chokepoint:<slug>)
```
