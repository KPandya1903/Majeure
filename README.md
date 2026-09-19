# PortGuard AI

PortGuard AI watches free, real-world maritime data for disruptions: port strikes, chokepoint
crises, cyclones, earthquakes, attacks on shipping. It links each disruption to the shipments
whose route it touches and prices the options from each shipment's **Bill of Lading contract
terms** (wait, reroute, invoke force majeure). Then **NVIDIA Nemotron** makes a cited,
contract-aware recommendation. PortGuard drafts the force majeure notice and the customer
email, starts the notice-deadline clock, and puts everything on a dashboard for human
approval.

* **Why it exists, the demo script, and the roadmap:** [docs/MAIN_IDEA.md](docs/MAIN_IDEA.md)
* **How the parts fit:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* **Endpoints and cleaning rules for every source:** [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
* **Database schema and every JSON contract:** [docs/SCHEMAS.md](docs/SCHEMAS.md)

> **For an LLM/agent picking up this repo:** read this README top to bottom first. It explains
> every file (§4), the invariants you must not break (§5), and the files that have to change
> together (§5.6). Then read `docs/SCHEMAS.md` before touching any data shape.

---

## 1. Quick start

**Requirements:** Python 3.10+ (tested on 3.10, 3.11 and 3.13), internet access, and no API
keys for data. Nemotron is optional: without it, use `--engine rules`.

```bash
git clone https://github.com/KPandya1903/Pixels.git portguard-ai && cd portguard-ai
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                               # set NEMOTRON_BASE_URL (see §2)

# Everything in one command: ingest → traffic → DB → demo shipments → events → decisions → drafts → alerts
.venv/bin/python src/run_pipeline.py               # needs Nemotron running
.venv/bin/python src/run_pipeline.py --engine rules   # ...or without a model (cheapest-option baseline)

# The dashboard (opens http://localhost:8501)
.venv/bin/streamlit run src/dashboard.py

# Optional extras
.venv/bin/python src/backtest.py                   # replay 5 historical disruptions (shown in the dashboard)
.venv/bin/python src/run_pipeline.py --xtract      # also let Nemotron re-read the news (Xtract)
.venv/bin/python src/extract_contract.py --file samples/contract.sample.txt --load   # contract → shipment
.venv/bin/python src/ask.py "Which shipments go through Hormuz, and what does it cost us?"
.venv/bin/python src/run_pipeline.py --loop 30     # keep running every 30 minutes
```

On Windows, use `.venv\Scripts\python` / `.venv\Scripts\streamlit`. Every script accepts
`--help` and `-v`, and exits **0 on success and 1 on failure**. A failing data source is
reported but never blocks the rest.

<details><summary>Running the steps one by one (what run_pipeline.py does)</summary>

```bash
.venv/bin/python src/fetch_portwatch.py     # run first: the others use its port list
.venv/bin/python src/fetch_gdacs.py
.venv/bin/python src/parse_rss.py
.venv/bin/python src/fetch_gdelt.py
.venv/bin/python src/fetch_google_news.py
.venv/bin/python src/fetch_traffic.py
.venv/bin/python src/init_db.py
.venv/bin/python src/load_shipments.py      # demo shipments (first run only)
.venv/bin/python src/xtract.py              # optional, Nemotron
.venv/bin/python src/cluster_events.py
.venv/bin/python src/run_nemotron.py --dry-run   # inspect the payloads in data/payloads/
.venv/bin/python src/run_nemotron.py        # or --engine rules
.venv/bin/python src/draft_actions.py
.venv/bin/python src/alerts.py
```
</details>

**Typical results (2026-09-19):** 450 signals from 9 sources. Those become 24 events: for
example HORMUZ-26 confirmed by 71 reports, and the Red Sea crisis by about 20. The 4 demo
shipments turn 90+ matching signals into **3 decisions**:
- SHP-DEMO-001 × Red Sea: invoke force majeure, ~$0 vs $10k waiting, notice due in 48 h.
- SHP-DEMO-001 × a bridge closure affecting Rotterdam port access.
- SHP-DEMO-002 × Hormuz: force majeure excluded and no alternative route, ~$30k exposure,
  notify the customer.

---

## 2. Connecting Nemotron

Every Nemotron call goes through one small OpenAI-compatible client (`src/nemotron.py`,
`/chat/completions`). NVIDIA NIM, vLLM, Ollama, and the hosted NVIDIA API all work. Settings
live in `.env` (copy `.env.example`; `.env` is git-ignored). Environment variables and CLI
flags override it.

| Variable | Default | Notes |
|---|---|---|
| `NEMOTRON_BASE_URL` | `http://localhost:8000/v1` | NIM / vLLM default. Ollama: `http://localhost:11434/v1`. Hosted: `https://integrate.api.nvidia.com/v1` |
| `NEMOTRON_MODEL` | *(empty → auto-detect)* | `GET /models`: the first id containing "nemotron", else the first id |
| `NEMOTRON_API_KEY` | *(empty)* | Hosted endpoints only (`Authorization: Bearer …`) |
| `NEMOTRON_TEMPERATURE` / `NEMOTRON_MAX_TOKENS` / `NEMOTRON_TIMEOUT` | `0.2` / `2048` / `300` | Raise max tokens if a reasoning model's reply gets cut off |
| `NEMOTRON_REASONING` | `default` | Thinking effort, sent as `reasoning_effort`: `none` turns thinking off (fastest), or `low` / `medium` / `high` |
| `NEMOTRON_STRUCTURED` | `on` | Sends a JSON Schema as `response_format`, so replies are forced into PortGuard's shape |
| `NEMOTRON_SEED` | *(empty)* | Fixed sampling seed, for reproducible runs |
| `SLACK_WEBHOOK_URL` | *(empty → console)* | Incoming-webhook URL for `alerts.py` |

**Where Nemotron is used:** decisions (`run_nemotron.py`), news re-reading (`xtract.py`,
optional), drafts (`draft_actions.py`, which falls back to templates), contract extraction
(`extract_contract.py`), and Q&A (`ask.py`, the dashboard chat). Reasoning models' `<think>`
blocks, code fences, `"High"` and `78` (for 0.78) are all handled (SCHEMAS.md §4). A reply
that can't be used is logged with `recommended_action = NULL` and retried on the next run.

**Useful `run_nemotron.py` flags:**
- `--engine nemotron|rules`
- `--dry-run` (payloads to `data/payloads/`)
- `--limit N`
- `--max-per-shipment N` (default 5 events per shipment per run; `0` = no cap)
- `--by-signal` (legacy: one decision per headline)
- `--since-days N` (news window, default 14)
- `--include-other`
- `--redo`

### 2.1 Tuning the model

* **Context window (Ollama).** Ollama loads models with a **4,096-token** window by default.
  A decision prompt is about 2,800 tokens, and a thinking model adds up to about 1,500 more,
  so the default can truncate it. [`ollama/Modelfile`](ollama/Modelfile) creates a tuned
  variant with a 16k window (the same weights, so nothing new to download):
  ```bash
  ollama create portguard-nemotron -f ollama/Modelfile   # edit FROM to tune a bigger Nemotron
  # .env: NEMOTRON_MODEL=portguard-nemotron
  ```
  Check with `ollama ps`: the CONTEXT column should read 16384.
* **Knobs sent with each request:** `NEMOTRON_REASONING`, `NEMOTRON_STRUCTURED`,
  `NEMOTRON_SEED`, temperature, and max tokens (table above, or the `--reasoning`,
  `--structured`, `--seed` flags on every script that calls the model). If a server rejects
  one, the client drops just that one and retries, so the same `.env` works on Ollama, vLLM,
  NIM, and the hosted API.
* **Measure, don't guess.** `src/eval_model.py` replays today's real decision payloads
  against any number of configurations. It is read-only and scores each reply on objective
  checks: does it parse, does the level match the score, is the score pre-mitigation, does
  the action fit the contract, is the cost grounded in the computed options, is the source
  cited. It also records latency:
  ```bash
  .venv/bin/python src/eval_model.py --pairs 3 --repeats 2 \
      --config "tuned:portguard-nemotron:default:on" --config "fast:portguard-nemotron:none:on"
  ```
  Config format: `name:model:reasoning:structured[:temperature]`.

### 2.2 If local isn't enough (last resort)

* **NVIDIA hosted API (uses your credits):** set `NEMOTRON_BASE_URL=https://integrate.api.nvidia.com/v1`,
  `NEMOTRON_API_KEY=nvapi-…`, and `NEMOTRON_MODEL=<the model id shown on build.nvidia.com>`.
  Set the model explicitly: auto-detect would pick the first "nemotron" in a long catalogue.
  Knobs the hosted API rejects are dropped automatically.
* **Google Colab (free GPU):** in a GPU notebook, install Ollama, `ollama serve`, and pull a
  larger Nemotron. Expose port 11434 through a tunnel (for example `cloudflared tunnel --url
  http://localhost:11434`), then set `NEMOTRON_BASE_URL=<tunnel URL>/v1` locally. Colab
  sessions time out, so this is for demos, not always-on runs. *Not tested in this repo.*

---

## 3. How it works (one screen)

1. **Sense.** *Structured:* IMF PortWatch curated disruption events (tagged with affected
   ports and chokepoints), PortWatch **daily traffic** (transits at 28 chokepoints, port calls
   at your ports), and GDACS live hazards (linked to ports within a radius). *Unstructured:*
   five maritime trade-press feeds, GDELT's raw news files, and a Google News search, the
   last two filtered to shipping *and* disruption headlines.
2. **Understand.** Everything becomes one **Xtract Signal** shape (title, citation URL,
   location, event type, severity, text), rule-tagged and optionally re-read by Nemotron.
3. **Cluster.** Reports are grouped into real-world **events** with corroboration (how many
   independent publishers) and a confidence score.
4. **Match.** An event matches a shipment when it touches its port of loading, port of
   discharge, a transshipment port, or a chokepoint on its route.
5. **Price.** `exposure.py` computes the cost of waiting, rerouting (around the chokepoint,
   or via the nearest alternate port), and invoking force majeure (if the clause covers it,
   with the notice deadline).
6. **Decide.** Nemotron gets the event, the shipment, the contract, and that analysis, and
   returns a risk score, an action, a cost estimate, a force majeure verdict, a rationale, and
   citations. `--engine rules` is the model-free baseline.
7. **Act.** Drafted notice and email, Slack/console alerts, and a dashboard for approval.
   The backtest replays past disruptions to show it would have worked.

---

## 4. Every file and what it is for

```
portguard-ai/
├── README.md                    ← you are here: setup, file map, conventions
├── requirements.txt             ← requests, feedparser, pandas, pydantic, streamlit, pypdf
├── .env.example                 ← Nemotron + Slack settings template (copy to .env)
├── .gitignore                   ← ignores .venv, .env, __pycache__, generated data/
├── docs/
│   ├── MAIN_IDEA.md             ← the idea, users, worked example, demo script, roadmap, glossary
│   ├── ARCHITECTURE.md          ← flow diagram, stage table, design decisions
│   ├── DATA_SOURCES.md          ← endpoints, field notes, cleaning/filtering/tagging rules, traffic layers
│   └── SCHEMAS.md               ← SQL DDL (executed by init_db.py) + every JSON Schema
├── src/
│   ├── schemas.py               ← Pydantic models: the source of truth for data shapes
│   ├── common.py                ← HTTP, cleaning, URL keys, classifier, gazetteer, geo helpers
│   ├── nemotron.py              ← shared Nemotron client (.env, chat, knobs + fallback, lenient JSON parsing)
│   ├── eval_model.py            ← compare model settings on real payloads (read-only scores)
│   │   ── ingest ──
│   ├── fetch_portwatch.py       ← IMF PortWatch → ports CSV + curated disruption signals
│   ├── fetch_gdacs.py           ← GDACS hazards → signals linked to nearby ports
│   ├── parse_rss.py             ← 5 trade-press RSS feeds → signals
│   ├── fetch_gdelt.py           ← GDELT raw news files (or Doc API) → relevant signals
│   ├── fetch_google_news.py     ← Google News search → relevant signals
│   ├── fetch_traffic.py         ← PortWatch daily transits / port calls → traffic CSVs
│   │   ── store, understand, cluster ──
│   ├── init_db.py               ← SQLite schema + load everything (idempotent, migrations)
│   ├── xtract.py                ← Nemotron re-reads news: type, place, severity (optional)
│   ├── cluster_events.py        ← reports → events with corroboration + confidence
│   │   ── shipments ──
│   ├── load_shipments.py        ← Bill of Lading JSON → shipments table
│   ├── extract_contract.py      ← contract PDF/TXT → Nemotron → shipment
│   │   ── decide + act ──
│   ├── exposure.py              ← cost of wait / reroute / force majeure; FM coverage; deadline
│   ├── run_nemotron.py          ← match → payload (with analysis) → Nemotron or rules → decision_logs
│   ├── draft_actions.py         ← force majeure notice + customer email per decision
│   ├── alerts.py                ← Slack/console alerts for high-risk pending decisions
│   │   ── review + validate ──
│   ├── dashboard.py             ← Streamlit dashboard (map, decisions, approvals, drafts, traffic, backtest, chat)
│   ├── ask.py                   ← Q&A over PortGuard's data with Nemotron
│   ├── backtest.py              ← replay 5 historical disruptions with real traffic
│   └── run_pipeline.py          ← run all steps in order, once or on a loop
├── ollama/
│   └── Modelfile                ← tuned Nemotron for Ollama (16k context, temperature 0.2)
├── samples/
│   ├── shipments.sample.json    ← 4 demo shipments on real routes (fictional companies)
│   └── contract.sample.txt      ← fictional sales contract to demo extract_contract.py
└── data/                        ← generated at run time; git-ignored except .gitkeep
```

### 4.1 Root files

| File | Use |
|---|---|
| `requirements.txt` | `requests` (HTTP, including Nemotron), `feedparser` (RSS), `pandas` (tables; brings `numpy` for distances), `pydantic` v2 (validation), `streamlit` (dashboard; brings `pydeck` for the map), `pypdf` (contract PDFs). No OpenAI SDK. |
| `.env.example` | `NEMOTRON_*` and `SLACK_WEBHOOK_URL` template. Read by `nemotron.load_dotenv()`, a small built-in parser. Never commit `.env`. |
| `.gitignore` | Keeps secrets, virtualenvs, and generated `data/` out of git. |
| `ollama/Modelfile` | Builds `portguard-nemotron` on Ollama: `FROM nemotron-3-nano:4b` with a 16k context (Ollama's 4,096 default truncates decision prompts), temperature 0.2, and top_p 0.95. Change `FROM` for a bigger Nemotron. |

### 4.2 `docs/`

| File | Read it when… |
|---|---|
| `MAIN_IDEA.md` | …you need the *why*: the problem, the solution layer by layer, a worked example with real numbers, what's real vs demo, the 90-second demo script, the roadmap, and a glossary (B/L, Incoterms, force majeure, demurrage, GKG…). |
| `ARCHITECTURE.md` | …you're changing how stages connect: the flow diagram, the stage table (reads, writes, whether Nemotron is needed), and design decisions. |
| `DATA_SOURCES.md` | …a source breaks or you're tuning ingestion: URLs, parameters, quirks, cleaning steps, the traffic layers (with the historical check), proximity radii, the classifier, the relevance filter, and the gazetteer. |
| `SCHEMAS.md` | …you change any table, field, or JSON shape: §1 DDL (executed verbatim) and `MATCH_SQL` (copied verbatim), §2 Xtract Signal, §3 Nemotron payload (v1.1 with `analysis`), §4 decision output and the rules engine. |

### 4.3 `src/` (run each script as `python src/<name>.py`)

Scripts resolve paths from their own location, so they work from any working directory, and
import each other as plain modules (`src/` is on the path). There's no package.

**Foundation**
* **`schemas.py`:** every data shape. `XtractSignal` and `PortLocation` (`match_method`:
  structured, gazetteer, proximity, or xtract); `Shipment` (`BillOfLading` +
  `ContractTerms`); `NemotronPayload` v1.1 with `Analysis` (`Exposure` with
  `ExposureOption`s, `Traffic`, `Corroboration` with `Headline`s); `NemotronDecision`
  (lenient). `Source` lists the 9 source names. Input models forbid unknown keys, and all
  datetimes are UTC with a `Z` suffix.
* **`common.py`:**
  - paths and `USER_AGENT` (must not contain "python-requests": GDACS returns 403);
  - `http_get()` with retry and backoff; cleaning (`clean_text`, `as_sentence`); URL keys
    (`canonical_url`, `url_key`, `make_signal_id`); atomic writers;
  - `GDACS_EVENT_TYPES` / `ALERT_SEVERITY`, `is_maritime()` (the relevance filter),
    `load_ports()`, `nearby_ports()` (vectorised haversine);
  - `CHOKEPOINTS` (28 PortWatch plus 3 seas) and `SEA_AREA_LINKS` (Red Sea → Bab el-Mandeb
    and Suez…);
  - `classify_event()` (ordered rules) and `PortGazetteer` (text → port or chokepoint).
* **`nemotron.py`:** `load_dotenv`, `env`, and `NemotronClient.chat(system, user, schema=…)`
  (with model auto-detect, and the `reasoning` / `structured` / `seed` knobs, each dropped
  individually if a server rejects it). Also `add_client_args` (`--base-url --model
  --temperature --max-tokens --timeout --reasoning --structured --seed`),
  `client_from_args` / `client_from_env`, `extract_json`, and `extract_json_list`. Every
  Nemotron call goes through here. `run_nemotron.decision_schema()`,
  `xtract.RESPONSE_SCHEMA`, and the draft schema define the shapes it enforces.

**Ingest** (each writes a JSON array of Xtract Signals, except `fetch_traffic.py`)
* **`fetch_portwatch.py`:** the ports database and curated disruption events.
  `affectedports` mixes `port123` and `chokepoint4` IDs, and both are resolved. `is_active`
  means ongoing, or ended within `--active-days` (30). Citation: the PortWatch event page.
* **`fetch_gdacs.py`:** live hazards. The default `--min-alert auto` threshold keeps Green+
  for cyclones, tsunamis, quakes, floods, and volcanoes, and Orange+ for wildfires and
  droughts. Events PortWatch already curates are skipped. Ports are linked within a radius
  (TC/TS 400 km, EQ 150, FL/VO 100, WF 50); `metadata.nearby_ports` holds the distances.
* **`parse_rss.py`:** `FEEDS` = gcaptain, maritime_executive, seatrade_maritime,
  hellenic_shipping_news, splash247. Feeds fail independently. Bylines and footers are
  stripped, and every item is kept (`other` is ignored at matching). `--only FEED …`.
* **`fetch_gdelt.py`:** `--mode raw` (default) reads the last `--hours` (6) of GKG 15-minute
  files (~75 MB, ~10 s, current, no rate limit) and keeps shipping-and-disruption headlines.
  `--mode api` uses the Doc API (rate-limited; its index can lag days). Dedupes by URL and by
  syndicated title.
* **`fetch_google_news.py`:** one RSS search (`--query`, `--days` 3), relevance-filtered;
  links are Google redirects.
* **`fetch_traffic.py`:** the last 111 days of transits (28 chokepoints) and port calls (the
  ports your shipments use, from the DB or the sample file) → `traffic_daily.csv` and
  `traffic_stats.csv` (latest 7 days vs the prior 90).

**Store, understand, cluster**
* **`init_db.py`:** runs the SCHEMAS.md DDL, adds missing columns (`MIGRATIONS`), upserts
  ports and signals (precedence PortWatch → GDACS → trade press → GDELT → Google News),
  expands sea areas into their straits in `signal_ports`, **re-applies Xtract results**, and
  loads traffic. It keeps `status` and the first `ingested_at`. `--reset` deletes everything,
  including shipments and decisions.
* **`xtract.py`:** sends recent news in batches of 10 to Nemotron. It stores
  `xtract_results` (disruption or not, type, place, severity, duration, confidence) and
  applies them to `signals` (`match_method = "xtract"`; non-disruptions become `other`).
  `--limit` 60, `--redo`.
* **`cluster_events.py`:** news joins the structured (PortWatch/GDACS) event at the same
  place when the type families are compatible (conflict, labour, hazard, operations; a
  closure fits any). The rest is grouped by place, family, and a 3-day window. Rebuilds
  `events` / `event_signals`. Confidence is 0.95+ when anchored on a structured event,
  otherwise 0.35 + 0.15 per extra publisher. `corroboration_for()` feeds the payload.

**Shipments**
* **`load_shipments.py`:** a JSON array of `Shipment`s (default: `samples/shipments.sample.json`).
  Port IDs are checked; upserts by `shipment_id`.
* **`extract_contract.py`:** PDF/TXT/MD → Nemotron → port names resolved to PortWatch IDs,
  and canal/strait names to chokepoint IDs → validated `Shipment` →
  `data/shipments_extracted.json` (`--load` also upserts it). The dashboard has an upload box
  for this.

**Decide + act**
* **`exposure.py`** (pure functions): `compute_exposure()` prices `monitor` (delay × late
  penalty, plus demurrage and detention after free time for port-side matches), `reroute`
  (`REROUTES` around chokepoints, or divert to an alternate port plus haulage; unavailable
  when the contract forbids deviation), and `invoke_force_majeure` (penalty excused; only if
  `force_majeure_covers()` allows it; notice due = first seen + notice period). All rates and
  delay tables sit at the top of the file, and `ASSUMPTIONS` travel with every payload.
* **`run_nemotron.py`:**
  - `MATCH_SQL` gives signal-level pairs, which `group_by_event()` turns into
    (event, shipment) items, anchor first.
  - `build_payload()` adds exposure, traffic, corroboration, `distance_km`, and an
    alternate port.
  - Then Nemotron (`SYSTEM_PROMPT`) or `rules_decision()` decides, and the result is
    logged with `event_id`, `estimated_cost_usd`, and `force_majeure_applicable`.
  - Already-decided pairs are skipped.
* **`draft_actions.py`:** for each usable decision, a force majeure notice (if the action or
  the verdict calls for it, with `deadline_at`) and a customer email (medium+ risk or any
  action other than monitor). `--engine auto|nemotron|template`. Drafts are never sent
  automatically.
* **`alerts.py`:** pending decisions at `--min-risk` (high) or above → Slack webhook or
  console, once per decision and channel.

**Review + validate**
* **`dashboard.py`:**
  - KPIs: events, shipments at high risk, pending approvals, estimated exposure, and the
    next notice deadline.
  - Tabs: **Risk map** (pydeck: events sized by reports, routes coloured by risk, chokepoints
    red when traffic is down 30%+), **Shipments & decisions** (options table, deadline
    countdown, sources, Approve/Override with notes, editable drafts, the raw payload, and
    contract upload), **Events**, **Traffic** (charts), **Backtest**, **Ask PortGuard**, and
    **All signals**.
  - Sidebar buttons re-run pipeline steps.
* **`ask.py`:** builds a compact JSON context (shipments, decisions with options, events,
  traffic changes) and has Nemotron answer with citations.
* **`backtest.py`:** 5 scenarios (Red Sea 2023, Baltimore 2024, US ports strike 2024, Panama
  2023, Hormuz 2026). Each uses the real event, a synthetic shipment through it, and the
  traffic known on day one. It records the decision plus the following month's measured
  traffic change → `data/backtest.json`. `--engine rules|nemotron`.
* **`eval_model.py`:** read-only comparison of model settings on the current top (event,
  shipment) payloads: six objective checks, a TOTAL, and median latency per configuration
  (§2.1).
* **`run_pipeline.py`:** runs all steps as subprocesses in order. Flags: `--engine`,
  `--xtract`, `--shipments FILE`, `--skip-ingest`, `--dry-run`, `--loop MINUTES`. PortWatch
  and the database are required; the other steps may fail.

### 4.4 `samples/`

* **`shipments.sample.json`:** four demo shipments on real routes with real PortWatch port
  IDs; the companies are fictional (`DEMO-…`).

  | Shipment | Route | Contract twist |
  |---|---|---|
  | SHP-DEMO-001 | Shanghai → (Singapore) → Rotterdam via Malacca, Bab el-Mandeb, Suez | CIF; force majeure **covers** war and canal closure; deviation allowed |
  | SHP-DEMO-002 | Jebel Ali → Nhava Sheva via Hormuz | FOB; force majeure **excludes** war and security; no deviation; $2,500/day penalty |
  | SHP-DEMO-003 | Busan → Los Angeles-Long Beach | DAP; usually a control with no matches |
  | SHP-DEMO-004 | Hamburg → Santos via Dover Strait | CFR; matches only when something touches the Channel |

  ETAs are in September and October 2026; edit them if you run the demo much later.
* **`contract.sample.txt`:** a fictional CIF sales contract (Ningbo → Colombo → Antwerp via
  Suez, force majeure covering war/piracy/canal closure, 72 h notice, $1,500/day penalty).
  Feed it to `extract_contract.py` or the dashboard's upload box.

### 4.5 `data/` (generated, git-ignored)

| File | Produced by |
|---|---|
| `portwatch_ports.csv`, `portwatch_signals.json` | `fetch_portwatch.py` |
| `gdacs_signals.json`, `rss_signals.json`, `gdelt_signals.json`, `google_news_signals.json` | the other ingesters |
| `traffic_daily.csv`, `traffic_stats.csv` | `fetch_traffic.py` |
| `portguard.db` | `init_db.py` + every later step (tables: ports, signals, signal_ports, events, event_signals, traffic_daily, traffic_stats, shipments, decision_logs, action_drafts, xtract_results, alerts_sent) |
| `payloads/*.json` | `run_nemotron.py --dry-run` |
| `backtest.json` | `backtest.py` |
| `shipments_extracted.json`, `upload_*` | `extract_contract.py` / dashboard upload |

JSON/CSV files are snapshots; the database accumulates history.

---

## 5. Conventions and invariants (don't break these)

1. **IDs.** `signal_id = sha1(url_key(source_url))[:16]`. Ports use the PortWatch `portid`.
   Chokepoints use `chokepoint:<slug of name>`, shared by signals, shipments, traffic, and
   events. Event IDs are the anchor `signal_id` (structured events) or a hash of place, type,
   and first day.
2. **Timestamps.** ISO-8601 UTC with `Z` (`common.iso()`).
3. **Idempotency.** Every step can be re-run. Decisions are per (event, shipment) and are
   skipped once decided; `decision_logs` is append-only.
4. **Human fields are sacred.** Nothing overwrites `signals.status`,
   `decision_logs.human_decision`/`human_notes`, or an approved or sent draft.
   `status = 'dismissed'` removes a signal from clustering and matching.
5. **Fail safe.** A failed fetch leaves the previous output untouched (exit 1). A bad record,
   feed, or file is skipped. The dashboard and drafts keep working without Nemotron (rules
   engine, templates).
6. **Files that change together:**
   * **A table or column:** SCHEMAS.md §1 (DDL executed from there; add new columns of
     existing tables to `MIGRATIONS` in `init_db.py`), plus its writer and reader.
   * **A JSON field:** `schemas.py` **and** the JSON Schema in SCHEMAS.md §2–4. A test
     compares them.
   * **A new source:** `Source` in `schemas.py`, the `signals.source` CHECK and enum in
     SCHEMAS.md, a `fetch_*.py` (or a `parse_rss.FEEDS` entry), `SIGNAL_FILES` in
     `init_db.py`, `STRUCTURED` in `cluster_events.py` if it's structured, `run_pipeline.py`,
     and DATA_SOURCES.md. Then `init_db.py --reset`.
   * **Matching:** `MATCH_SQL` **and** its copy in SCHEMAS.md §1. The cluster filter in
     `cluster_events.load_candidates()` must use the same recency rule.
   * **An event type:** `EventType`, `_EVENT_RULES` (common.py), `FAMILIES`
     (cluster_events.py), `DELAY_DAYS` and `FM_TERMS` (exposure.py), the CHECK and enums in
     SCHEMAS.md, and DATA_SOURCES.md.
   * **Allowed actions:** `Action` in `schemas.py`, the `decision_logs` CHECK, SCHEMAS.md
     §3–4, and `ACTIONS` in `dashboard.py` / `alerts.py`.
   * **Cost assumptions:** the constants and `ASSUMPTIONS` text in `exposure.py` (keep them
     in sync).

---

## 6. Common tasks

* **Use real shipments:** a JSON array shaped like `samples/shipments.sample.json` →
  `python src/load_shipments.py --file my.json`, or upload contracts in the dashboard. Find
  port IDs with `SELECT port_id, port_name, country, locode FROM ports WHERE port_name LIKE '%Antwerp%';`.
* **Demo without a GPU:** `run_pipeline.py --engine rules`, then the dashboard. Drafts use
  templates automatically.
* **Tune the cost model:** edit the tables at the top of `exposure.py`, then
  `run_nemotron.py --redo`.
* **Tune the model:** `SYSTEM_PROMPT` in `run_nemotron.py` (decisions), `xtract.py`,
  `draft_actions.py`, `ask.py`, and `extract_contract.py`.
* **Add an RSS feed:** a `FEEDS` entry plus `Source`, then follow §5.6 "A new source".
* **Fix a wrong location tag:** `AMBIGUOUS_PORT_NAMES` / `PORT_ALIASES` in `common.py`, or
  run `xtract.py`.
* **More or less GDACS:** `fetch_gdacs.py --min-alert green|orange|red`; radii are in
  `RADIUS_KM`.
* **Dismiss a noisy signal:** `UPDATE signals SET status = 'dismissed' WHERE signal_id = '…';`
  (or override its decision in the dashboard).

**Useful queries** (`sqlite3 data/portguard.db` or any SQLite viewer):

```sql
-- Open decisions, most expensive first
SELECT d.shipment_id, d.recommended_action, d.risk_level, ROUND(d.estimated_cost_usd) AS usd,
       s.title, s.source_url, d.human_decision
FROM decision_logs d JOIN signals s USING (signal_id)
WHERE d.recommended_action IS NOT NULL
ORDER BY d.estimated_cost_usd DESC;

-- Events and their corroboration
SELECT title, event_type, location_name, n_signals, n_publishers, confidence FROM events ORDER BY n_signals DESC;

-- Biggest measured traffic drops
SELECT name, metric, avg_7d, baseline_avg, pct_change FROM traffic_stats ORDER BY pct_change LIMIT 10;

-- Replies the parser rejected (to debug a prompt)
SELECT shipment_id, substr(output_payload, 1, 300) FROM decision_logs WHERE recommended_action IS NULL;
```

---

## 7. How this was verified

Tested on 2026-09-19 against the live sources:
* **Fresh clone** on Python 3.10 (pandas 2) and 3.13 (pandas 3): every script exits 0;
  every JSON record validates; re-loading is idempotent and keeps human fields; cross-source
  duplicates collapse; foreign keys hold; the SCHEMAS.md JSON Schemas equal the Pydantic
  models; `MATCH_SQL` equals its docs copy; the example payloads validate.
* **End to end with a mock OpenAI-compatible Nemotron** that answers every prompt type with
  `<think>` blocks, code fences, `"High"`, and `78`. `run_pipeline.py --xtract` succeeded in
  every step. Xtract refinements survived re-ingestion. The sample contract became a shipment
  and matched the Red Sea event. `ask.py` answered from the DB. Unusable replies were logged
  and retried. A second decision run produced **no duplicate decisions**.
* **Dashboard:** rendered with Streamlit's `AppTest` (no exceptions; filters, selectors,
  Approve and Override exercised and persisted) and in Chrome, via Playwright screenshots of
  the map, decisions, traffic, and backtest tabs.
* **Failure paths:** failed GDACS / GDELT fetches exit 1 and keep the previous output;
  Nemotron down gives a clear error (rules and templates still work).
* **Not tested:** a **real Nemotron model**. The first real run is the one to watch
  (`run_nemotron.py --limit 3`).

---

## 8. Known limitations

* **Cost figures are planning estimates** from stated assumptions (delay tables, surcharges,
  haulage), not quotes. The backtest shows where they're off: the 2024 US port strike lasted
  3 days, while the table assumes about 9.
* **Force majeure coverage is a keyword check** (`FM_TERMS`, including exclusions). Vague
  clauses return "unknown", and Nemotron and a human should confirm.
* **Rule-based tagging** has false positives ("London's marine insurers…" → port of London;
  towns named "Port …" in Google News). Xtract fixes many of them.
* **GDELT's Doc API lags and throttles**, which is why raw files are the default. News is
  mostly headline-only.
* **Traffic data lags 5–7 days**, and its 90-day baseline can already sit inside a
  long-running crisis (Hormuz shows −43% against a depressed baseline, but −93% against
  pre-crisis levels).
* **GDACS radii are heuristics;** PortWatch `region` events without ports never match.
* **Shipments are demo data** until real B/L data is loaded.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach Nemotron at …` | Start the server or fix `NEMOTRON_BASE_URL` (Ollama needs `/v1`). Test with `curl $NEMOTRON_BASE_URL/models`. Meanwhile use `--engine rules`. |
| `unusable reply … no JSON object` | The model answered in prose or ran out of tokens while thinking. Raise `NEMOTRON_MAX_TOKENS`, lower the temperature, or tighten the prompt. Check `output_payload`. |
| `0 (event, shipment) pairs to assess` | Already decided (`--redo`), no route touches an active event, or the news is older than `--since-days`. |
| Dashboard says "No database yet" | Run `python src/run_pipeline.py --engine rules` first. |
| Dashboard map has no background | The basemap comes from CARTO over the internet; the data layers still render. |
| `403 Forbidden` from gdacs.org | Keep `USER_AGENT` free of "python-requests". |
| `HTTP 429 from api.gdeltproject.org` | Only with `--mode api`: wait, or use the default raw mode. |
| `GDELT raw fetch failed` / `skipping GKG file` | GDELT's file server blinked. Single files are skipped; re-run later. |
| `portwatch_ports.csv not found` | Run `fetch_portwatch.py` first (`run_pipeline.py` does). |
| `CHECK constraint failed` after an upgrade | New sources or match methods need `init_db.py --reset`. This wipes shipments and decisions, so reload them. |

## 10. Data attribution

IMF PortWatch (portwatch.imf.org: disruption events, ports, daily traffic). GDACS (European
Commission JRC / UN OCHA). The GDELT Project. gCaptain, The Maritime Executive, Seatrade
Maritime News, Hellenic Shipping News Worldwide, and Splash247 (titles, links, excerpts, and
categories only, as citations). Google News (titles and links only). Map tiles © CARTO ©
OpenStreetMap contributors.
