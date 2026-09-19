# PortGuard AI

PortGuard AI watches free, real-world maritime data for disruptions: port strikes, chokepoint
crises, cyclones, earthquakes, attacks on shipping. It links each disruption to the shipments
whose route it touches, then asks **NVIDIA Nemotron** what the disruption means for that
shipment under its **Bill of Lading contract terms**. Nemotron returns a risk score, a
recommended action, and a rationale that cites its sources, and every answer is logged for
human review.

* **Why it exists, in detail:** [docs/MAIN_IDEA.md](docs/MAIN_IDEA.md)
* **How the parts fit:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* **Exact endpoints and cleaning rules for every source:** [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)
* **Database schema and every JSON contract:** [docs/SCHEMAS.md](docs/SCHEMAS.md)

> **For an LLM/agent picking up this repo:** read this README top to bottom first. It explains
> every file, the invariants you must not break (§5), and the files that have to change
> together (§5.6). Then read `docs/SCHEMAS.md` before touching any data shape.

---

## 1. Quick start

**Requirements:** Python 3.10+ (tested on 3.10, 3.11 and 3.13), internet access, and no API
keys for ingestion. A running Nemotron server is only needed for the final step.

```bash
git clone https://github.com/KPandya1903/Pixels.git portguard-ai && cd portguard-ai
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip install -r requirements.txt

# 1) Ingest (about 1 minute). Run fetch_portwatch.py first: the others use its port list.
.venv/bin/python src/fetch_portwatch.py            # IMF PortWatch ports + curated disruption events
.venv/bin/python src/fetch_gdacs.py                # live cyclone/quake/flood alerts, linked to nearby ports
.venv/bin/python src/parse_rss.py                  # 5 maritime trade-press RSS feeds
.venv/bin/python src/fetch_gdelt.py                # GDELT raw news files, last 6 h (~75 MB download)
.venv/bin/python src/fetch_google_news.py          # Google News search, last 3 days
.venv/bin/python src/init_db.py                    # build/refresh data/portguard.db

# 2) Add shipments (demo data; replace with real Bills of Lading later)
.venv/bin/python src/load_shipments.py

# 3) Ask Nemotron. See the dry-run first, then the real thing.
cp .env.example .env                               # then edit NEMOTRON_BASE_URL (and NEMOTRON_MODEL if needed)
.venv/bin/python src/run_nemotron.py --dry-run     # writes the exact payloads to data/payloads/
.venv/bin/python src/run_nemotron.py --limit 3     # 3 real Nemotron calls → decision_logs
.venv/bin/python src/run_nemotron.py               # up to 5 pending pairs per shipment
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`. Every script accepts
`--help` and `-v` (debug logging). Every script exits with **0 on success and 1 on failure**,
so they can be chained in cron or CI. Any single ingestion script can fail (the network, a
source being down) without blocking the rest.

**Typical results (2026-09-19):** 448 signals, from PortWatch (132 curated events), GDACS (30
alerts, 10 linked to ports), trade press (179 items), GDELT (48 stories from 17,306 scanned
articles), and Google News (59). The 4 demo shipments match 83 (signal, shipment) pairs, and
the default cap of 5 per shipment sends 10 of them to Nemotron in the first run:
- SHP-DEMO-001 via Bab el-Mandeb: the PortWatch RED SEA TENSIONS event plus same-day reports
  such as "Houthis Seize Bab al-Mandeb".
- SHP-DEMO-002 via Hormuz: HORMUZ-26 plus tanker-attack reports.

---

## 2. Connecting Nemotron

`run_nemotron.py` talks to any **OpenAI-compatible** `/chat/completions` endpoint, which is
how Nemotron is normally served. The settings live in `.env` (copy `.env.example`; `.env` is
git-ignored). Real environment variables and CLI flags override `.env`.

| Variable | Default | Notes |
|---|---|---|
| `NEMOTRON_BASE_URL` | `http://localhost:8000/v1` | NVIDIA NIM / vLLM default. Ollama: `http://localhost:11434/v1`. Hosted NVIDIA API: `https://integrate.api.nvidia.com/v1` |
| `NEMOTRON_MODEL` | *(empty → auto-detect)* | Auto-detect calls `GET /models` and picks the first id containing "nemotron", otherwise the first id listed |
| `NEMOTRON_API_KEY` | *(empty)* | Only for hosted endpoints. Sent as `Authorization: Bearer …` |
| `NEMOTRON_TEMPERATURE` | `0.2` | |
| `NEMOTRON_MAX_TOKENS` | `2048` | Raise it if a reasoning model's replies get cut off mid-thought |
| `NEMOTRON_TIMEOUT` | `300` | Seconds per request |

Useful flags: `--dry-run` (no model calls; payload JSON written to `data/payloads/`),
`--limit N`, `--max-per-shipment N` (default **5** per shipment per run, highest priority
first: PortWatch events, then newest news; later runs pick up the rest; `0` removes the cap),
`--since-days N` (news look-back, default 14), `--include-other` (also send
`event_type = other`), and `--redo` (re-assess pairs that already have a decision).

**Reading the results:**

```sql
-- sqlite3 data/portguard.db   (or open the file in any SQLite viewer)
SELECT d.shipment_id, d.recommended_action, d.risk_level, ROUND(d.risk_score, 2) AS score,
       s.title, s.source_url, d.rationale
FROM decision_logs d JOIN signals s USING (signal_id)
WHERE d.recommended_action IS NOT NULL
ORDER BY d.risk_score DESC;
```

Reasoning models may "think" before answering. The parser drops `<think>…</think>`, ignores
code fences and surrounding prose, and accepts `"High"` or `78` in place of `high` or `0.78`
(SCHEMAS.md §4). A reply that still can't be used is logged with
`recommended_action = NULL` and retried on the next run.

---

## 3. How it works (one screen)

1. **Ingest (two tracks).** *Structured:* IMF PortWatch ports and curated disruption events
   (already tagged with affected port and chokepoint IDs), and GDACS live hazard alerts
   (linked to the ports within a radius of the event). *Unstructured:* five maritime
   trade-press RSS feeds, GDELT's raw news files, and a Google News search. The latter two
   are filtered to headlines about shipping *and* a disruption. Every article is pre-tagged by
   rules with an `event_type` and a `port_location`.
2. **Normalise.** Every item from every source becomes the same **Xtract Signal** JSON
   object: title, citation URL, location, event type, severity, raw text, and metadata.
3. **Store.** `init_db.py` upserts ports and signals into SQLite. `signal_ports` links each
   signal to every port or chokepoint it affects.
4. **Match.** A signal matches a shipment when one of its ports or chokepoints is the
   shipment's port of loading, port of discharge, a transshipment port, or a chokepoint on
   its route. Only active or recent signals are used.
5. **Decide.** Each pair becomes a **NemotronPayload** (signal + B/L + contract terms + why
   they matched, including the distance for hazards + allowed actions). Nemotron replies with
   a **NemotronDecision** (risk score, level, action, rationale, and citations), which goes
   into `decision_logs` for human approval.

---

## 4. Every file and what it is for

```
portguard-ai/
├── README.md                    ← you are here: setup, file map, conventions
├── requirements.txt             ← 4 dependencies, version-bounded
├── .env.example                 ← Nemotron connection template (copy to .env)
├── .gitignore                   ← ignores .venv, .env, __pycache__, generated data/
├── docs/
│   ├── MAIN_IDEA.md             ← the product idea, the users, a worked example, the roadmap
│   ├── ARCHITECTURE.md          ← end-to-end flow diagram, stage contracts, design decisions
│   ├── DATA_SOURCES.md          ← verified endpoints, field notes, cleaning/filtering/tagging rules
│   └── SCHEMAS.md               ← SQL DDL (executed by init_db.py) + all JSON Schemas
├── src/
│   ├── schemas.py               ← Pydantic models: the single source of truth for data shapes
│   ├── common.py                ← shared helpers: HTTP, cleaning, URL keys, classifier, gazetteer, geo
│   ├── fetch_portwatch.py       ← IMF PortWatch → ports CSV + curated disruption signals
│   ├── fetch_gdacs.py           ← GDACS live hazard alerts → signals linked to nearby ports
│   ├── parse_rss.py             ← 5 maritime trade-press RSS feeds → signals
│   ├── fetch_gdelt.py           ← GDELT raw news files (or Doc API) → relevant signals
│   ├── fetch_google_news.py     ← Google News search → relevant signals
│   ├── init_db.py               ← SQLite schema + load CSV/JSON (idempotent)
│   ├── load_shipments.py        ← Bill of Lading JSON → shipments table
│   └── run_nemotron.py          ← match → payload → Nemotron → decision_logs
├── samples/
│   └── shipments.sample.json    ← 4 demo shipments on real routes (fictional companies)
└── data/                        ← generated at run time; git-ignored except .gitkeep
```

### 4.1 Root files

| File | Use |
|---|---|
| `requirements.txt` | `requests` (HTTP, including the Nemotron calls), `feedparser` (RSS), `pandas` (CSV + ports table; brings `numpy`, used for distances), `pydantic` v2 (validation). Nothing else, and no OpenAI SDK. |
| `.env.example` | Template for the `NEMOTRON_*` settings in §2. `run_nemotron.py` reads `.env` with a small built-in parser (no python-dotenv). Never commit `.env`. |
| `.gitignore` | Keeps secrets, virtualenvs, and generated `data/` out of git. Everything in `data/` can be rebuilt with the §1 commands. |

### 4.2 `docs/`

| File | Use | Read it when… |
|---|---|---|
| `MAIN_IDEA.md` | The problem, who it's for, how the decision works, a worked example with real data, what's real vs demo, the roadmap, and a glossary (B/L, Incoterms, force majeure, demurrage, GKG…). | …you need to understand *why* before changing *what*, or you're preparing a demo or pitch. |
| `ARCHITECTURE.md` | Flow diagram, stage-by-stage contracts (reads/writes), design decisions (idempotency, source precedence, fail-safe behaviour). | …you're adding a stage or changing how stages connect. |
| `DATA_SOURCES.md` | For each of the 5 source families: exact URLs and parameters, response quirks (epoch-ms dates, `"; "`-separated IDs, GKG column indexes, GDACS namespaces, Google redirect links, rate limits), every cleaning and filtering step, the proximity radii, the classifier rules, the relevance filter, and the gazetteer rules. | …a source changes or breaks, or you're tuning classification, filtering, or location tagging. |
| `SCHEMAS.md` | §1 SQL DDL (**executed verbatim by `init_db.py`**) plus the matching query (**copied verbatim from `run_nemotron.py`**). §2 Xtract Signal JSON Schema with a real example and per-source `metadata`. §3 Nemotron input payload. §4 Nemotron output and the lenient-parsing rules. | …you change any table, field, or JSON shape. |

### 4.3 `src/` (run each script as `python src/<name>.py`)

All scripts resolve paths from their own location, so they work from any working directory.
They import each other as plain modules (`from common import …`), which works because
`src/` is the script's directory. There is no package or `__init__.py`.

#### `schemas.py`: data contracts (no side effects)
* **Use:** Pydantic v2 models that every other file validates against. They mirror
  SCHEMAS.md.
* **Models:** `EventType` (10-value enum). `Source`, the 9 allowed `source` values: portwatch,
  gdacs, gcaptain, maritime_executive, seatrade_maritime, hellenic_shipping_news, splash247,
  gdelt, google_news. `PortLocation` (`match_method`: structured, gazetteer, or proximity),
  `XtractSignal` (ingestion output). `PortRef`, `BillOfLading`, `ContractTerms`, `Shipment`,
  `MatchContext`, `Task`, `NemotronPayload` (model input). `NemotronDecision` (model output;
  lenient: extra keys ignored, labels lowercased, 1–100 read as a percentage).
* **Key rules:** input models use `extra="forbid"`, so unknown keys are errors. All datetimes
  are normalised to UTC and serialised with a `Z` suffix. `XtractSignal.port_location` must be
  present but may be `null`. `signal_id` must be 16 hex characters.

#### `common.py`: shared helpers
* **Paths:** `ROOT`, `DATA_DIR`, `PORTS_CSV`. **`USER_AGENT`** must not contain
  "python-requests": GDACS's firewall returns 403 for it.
* **HTTP:** `http_get()` retries connection errors, timeouts, 429, and 5xx with exponential
  backoff, and honours `Retry-After`.
* **Cleaning:** `clean_text()` strips tags, unescapes entities, and collapses whitespace.
  `as_sentence()` adds a missing full stop. `canonical_url()` gives a citation-safe URL
  (lowercase host, no fragment, no `utm_*`/`fbclid`/`gclid`). `url_key()` is the dedupe key
  (also drops `www.` and the trailing `/`). `make_signal_id()` is `sha1(url_key)[:16]`.
* **Output:** `write_signals()` and `write_csv()` write atomically (temp file, then rename).
* **GDACS codes:** `GDACS_EVENT_TYPES` (TC/FL/DR → `severe_weather`; EQ/VO/WF/TS →
  `natural_disaster`) and `ALERT_SEVERITY` (RED → high, ORANGE → medium, GREEN → low), shared
  by PortWatch and GDACS.
* **Relevance and geo:** `is_maritime(text)` is the "is this headline about shipping?" filter
  for firehose sources. `load_ports()` reads the ports CSV. `nearby_ports(ports, lat, lon,
  radius_km)` returns the ports within a radius, nearest first, using a vectorised haversine
  distance.
* **`CHOKEPOINTS`:** the 28 PortWatch chokepoints (official names, coordinates, and
  `chokepoint1..28` IDs) plus Red Sea, Gulf of Aden, and Black Sea, each with a text regex.
  `Chokepoint.location_id` gives `chokepoint:<slug>`.
* **`classify_event(text)`:** an ordered keyword classifier; the first rule that matches
  wins. A bare "strike" is *not* labour: it needs labour context, because "ship strike" means
  an attack.
* **`PortGazetteer`:** finds the most likely port or chokepoint in free text (case-sensitive
  port names with aliases; offshore-terminal and ambiguous names excluded; the busiest port
  wins ties; title beats body; ports beat chokepoints; the first-mentioned chokepoint wins).

#### `fetch_portwatch.py`: IMF PortWatch (run first)
* **Writes:** `data/portwatch_ports.csv` (one row per port; columns equal the `ports`
  table, plus `active_disruption_count`/`_ids`) and `data/portwatch_signals.json` (one signal
  per curated event).
* **Logic:** pages the ArcGIS layers and resolves `affectedports`, which mixes `port123` and
  `chokepoint4` IDs. `port_location` is the busiest affected port, else the first chokepoint,
  else a `region`. `is_active` = ongoing, or ended within `--active-days` (default 30). The
  citation is `https://portwatch.imf.org/pages/{pageid}`.
* **Flags:** `--active-days`, `--ports-out`, `--signals-out`, `-v`.

#### `fetch_gdacs.py`: GDACS live hazard alerts
* **Writes:** `data/gdacs_signals.json`.
* **Logic:** reads `https://www.gdacs.org/xml/rss.xml`. It keeps alerts at or above the
  threshold (default `auto`: Green+ for cyclones, tsunamis, quakes, floods, and volcanoes;
  Orange+ for wildfires and droughts) that are current or ended within `--days`. Events
  PortWatch already curates are skipped. Each alert is linked to the PortWatch ports within a
  per-type radius (TC/TS 400 km, EQ 150, FL/VO 100, WF 50, DR none): `port_location` is the
  nearest port (`match_method = "proximity"`), and `metadata.nearby_ports` keeps the
  distances, which `run_nemotron.py` passes on as `match_context.distance_km`.
* **Flags:** `--min-alert auto|green|orange|red`, `--days`, `--out`, `-v`.

#### `parse_rss.py`: maritime trade press
* **Writes:** `data/rss_signals.json`.
* **Feeds (`FEEDS`, in precedence order):** gcaptain, maritime_executive, seatrade_maritime,
  hellenic_shipping_news, splash247. The feed name becomes each signal's `source`.
* **Logic:** each feed is fetched and parsed independently (a broken feed is skipped, and the
  exit code is 1). The excerpt has HTML, entities, the WordPress "appeared first on" footer,
  and wire bylines stripped; truncation is marked with `…`. Categories feed the classifier and
  chokepoint matching. All items are kept; general industry news gets `event_type = other`
  and is ignored at matching.
* **Flags:** `--only FEED …`, `--out`, `-v`.

#### `fetch_gdelt.py`: GDELT
* **Writes:** `data/gdelt_signals.json`.
* **`--mode raw` (default):** downloads the GKG 15-minute files for the last `--hours`
  (default 6, ~75 MB, ~10 s). It keeps web articles whose headline passes `is_maritime()` and
  isn't `other`. It is current, with no rate limit.
* **`--mode api`:** one Doc API search (`--query`, `--timespan`, `--max-records`,
  `--all-languages`). It is rate-limited (1 request per 5 s; it backs off up to ~3 minutes),
  and its index can lag days behind.
* **Both:** GDELT title spacing is cleaned, dedupe runs by canonical URL and then by
  normalised title (catching syndicated copies), and `raw_text` is the headline.

#### `fetch_google_news.py`: Google News search
* **Writes:** `data/google_news_signals.json`.
* **Logic:** one RSS search (`--query`, default maritime × disruption terms; `--days`,
  default 3). The trailing " - Publisher" is stripped from titles. It keeps only headlines
  that pass `is_maritime()` and aren't `other`, then dedupes by URL and title. `source_url`
  is Google's redirect link; the publisher is in `metadata`.
* **Flags:** `--query`, `--days`, `--out`, `-v`.

#### `init_db.py`: database
* **Logic:** runs the one ```` ```sql ```` block containing `CREATE TABLE` in
  `docs/SCHEMAS.md`. Upserts ports from the CSV. Validates every signal with `XtractSignal`
  and rejects invalid ones. Upserts signals from `SIGNAL_FILES` in precedence order:
  PortWatch → GDACS → trade press → GDELT → Google News. A signal already loaded from a
  *different* source is skipped. Rebuilds `signal_ports`. A re-run updates content but keeps
  `status` and the first `ingested_at`. Missing files are skipped with a warning.
* **Flags:** `--db`, `--reset` (**deletes the DB, including shipments and decision_logs**),
  `-v`.

#### `load_shipments.py`: shipments
* **Input:** a JSON array of `Shipment` objects, the same shape as `NemotronPayload.shipment`
  (SCHEMAS.md §3). Default: `samples/shipments.sample.json`.
* **Logic:** validates each shipment and checks every port ID against `ports`, with a
  helpful error and a lookup hint. It warns about unknown chokepoint IDs. Upserts by
  `shipment_id` and keeps `created_at`.
* **Flags:** `--file`, `--db`, `-v`.

#### `run_nemotron.py`: decision engine
* **Logic:** `find_pairs()` runs `MATCH_SQL` and keeps the strongest reason per pair
  (discharge > loading > transshipment > route chokepoint), with PortWatch events first, then
  the newest news. It is capped per shipment by `--max-per-shipment`. `build_payload()`
  rebuilds signal and shipment from the DB into a validated `NemotronPayload` (with
  `days_until_eta`, and `distance_km` for GDACS). `NemotronClient` calls the
  OpenAI-compatible API (with model auto-detect). `extract_json()` and `parse_decision()`
  apply the lenient parsing. `log_decision()` writes a `decision_logs` row with the input
  payload, the raw response, and latency. `SYSTEM_PROMPT` holds the model instructions.
* **Flags:** see §2.

### 4.4 `samples/shipments.sample.json`

Four demo shipments on real routes with real PortWatch port IDs. The companies, vessels, and
B/L numbers are fictional (`DEMO-…`). They were chosen so the demo shows contrast:

| Shipment | Route | Contract twist |
|---|---|---|
| SHP-DEMO-001 | Shanghai → (Singapore) → Rotterdam via Malacca, Bab el-Mandeb, Suez | CIF; force majeure **covers** war and canal closure; deviation allowed |
| SHP-DEMO-002 | Jebel Ali → Nhava Sheva via Strait of Hormuz | FOB; force majeure **excludes** war and security incidents; no deviation; $2,500/day penalty |
| SHP-DEMO-003 | Busan → Los Angeles-Long Beach | DAP; usually a control with no matches |
| SHP-DEMO-004 | Hamburg → Santos via Dover Strait | CFR; matches only when something touches the Channel, and tests whether the model can say "monitor" |

What each shipment matches depends on the day's news: on 2026-09-19, 001 and 002 had
plenty and 003 and 004 had none. ETAs are in September and October 2026. If you run the demo
much later, edit the dates, or `days_until_eta` will be negative.

### 4.5 `data/` (generated, git-ignored)

| File | Produced by | Contents |
|---|---|---|
| `portwatch_ports.csv` | `fetch_portwatch.py` | 2,065 ports; the gazetteer and proximity table for everything else |
| `portwatch_signals.json` | `fetch_portwatch.py` | Curated disruption events, 2018 → now |
| `gdacs_signals.json` | `fetch_gdacs.py` | Current hazard alerts with nearby ports |
| `rss_signals.json` | `parse_rss.py` | Latest items of the 5 trade-press feeds |
| `gdelt_signals.json` | `fetch_gdelt.py` | Relevant GDELT headlines from the last `--hours` |
| `google_news_signals.json` | `fetch_google_news.py` | Relevant Google News headlines from the last `--days` |
| `portguard.db` | `init_db.py` (+ `load_shipments.py`, `run_nemotron.py`) | SQLite: `ports`, `signals`, `signal_ports`, `shipments`, `decision_logs` |
| `payloads/*.json` | `run_nemotron.py --dry-run` | One `NemotronPayload` per pair: exactly what Nemotron would receive |

The JSON files are **snapshots** (overwritten on every run); the **DB accumulates** history.

---

## 5. Conventions and invariants (don't break these)

1. **IDs.** `signal_id = sha1(url_key(source_url))[:16]`, which is deterministic and
   collapses duplicates across sources that share a URL. Ports use the PortWatch `portid`
   (`port1188`). Chokepoints use `chokepoint:<slug of name>` (`chokepoint:strait-of-hormuz`),
   shared by PortWatch events, news tagging, and `shipments.route_chokepoints`.
2. **Timestamps.** ISO-8601 UTC with `Z`, via `common.iso()`.
3. **Idempotency.** Every stage can be re-run safely. Upserts, never blind inserts (except
   `decision_logs`, which is an append-only audit trail).
4. **Human fields are sacred.** The pipeline never overwrites `signals.status` or
   `decision_logs.human_decision`/`human_notes`. `status = 'dismissed'` removes a signal
   from matching.
5. **Fail safe.** Network failure → exit 1, and the previous output is left untouched. A bad
   record or a single broken feed or file is logged and skipped; it never crashes the run.
6. **Files that change together:**
   * A table or column: `docs/SCHEMAS.md` §1 (the DDL is executed from there), the loader
     (`init_db.py`, `load_shipments.py` `COLUMNS`, or `run_nemotron.py` `log_decision`), and
     the reader (`run_nemotron.py` `load_signal`/`load_shipment`).
   * A JSON field: `src/schemas.py` **and** the JSON Schema in `docs/SCHEMAS.md` §2–4.
   * **A new source:** the `Source` literal in `schemas.py`, the `signals.source` CHECK and
     the `source` enum in SCHEMAS.md, a `fetch_*.py` (or an entry in `parse_rss.FEEDS`),
     `SIGNAL_FILES` in `init_db.py` (for a new output file), and DATA_SOURCES.md. Then run
     `init_db.py --reset`, because CHECK constraints only apply to new tables.
   * The matching logic: `MATCH_SQL` in `run_nemotron.py` **and** its copy in
     `docs/SCHEMAS.md` §1.
   * An event type: the `EventType` enum, the `_EVENT_RULES` in `common.py`, the CHECK in
     SCHEMAS.md §1 (then `init_db.py --reset`), the enum in SCHEMAS.md §2, and the table in
     DATA_SOURCES.md.
   * Allowed actions: the `Action` literal in `schemas.py`, the CHECK on
     `decision_logs.recommended_action`, and SCHEMAS.md §3–4.

---

## 6. Common tasks

* **Use real shipments:** write a JSON array shaped like `samples/shipments.sample.json` and
  run `python src/load_shipments.py --file my_shipments.json`. Look up port IDs with
  `SELECT port_id, port_name, country, locode FROM ports WHERE port_name LIKE '%Antwerp%';`.
  Chokepoint IDs are the `location_id`s of `common.CHOKEPOINTS`.
* **Refresh the data:** re-run the ingestion commands in §1, then `run_nemotron.py`. Only new
  pairs are sent to the model.
* **Add an RSS feed:** add `name: url` to `FEEDS` in `parse_rss.py` and `name` to `Source`,
  then follow §5.6 "A new source".
* **Add another kind of source:** write `src/fetch_<source>.py` that emits `XtractSignal`s
  via `write_signals()`, using `classify_event()`, `PortGazetteer.from_csv().match()`, and,
  for firehose sources, `is_maritime()`. Then follow §5.6.
* **More or less GDACS:** `fetch_gdacs.py --min-alert green` (everything) or `orange`. Radii
  are in `RADIUS_KM`.
* **Fix a wrong location tag:** add the name to `AMBIGUOUS_PORT_NAMES` (false positive) or
  to `PORT_ALIASES` (missed alias) in `common.py`, then re-run ingestion and `init_db.py`.
* **Tune the model:** edit `SYSTEM_PROMPT` or `TASK_INSTRUCTIONS` in `run_nemotron.py`,
  then run `run_nemotron.py --redo --limit 3`.
* **Dismiss a noisy signal:** `UPDATE signals SET status = 'dismissed' WHERE signal_id = '…';`
* **Approve or override a decision:** `UPDATE decision_logs SET human_decision = 'approved',
  human_notes = '…' WHERE decision_id = …;`

**More useful queries:**

```sql
-- Active PortWatch/GDACS disruptions and what they touch
SELECT s.source, s.title, s.event_type, s.severity, GROUP_CONCAT(sp.port_id, ', ') AS affects
FROM signals s JOIN signal_ports sp USING (signal_id)
WHERE json_extract(s.metadata, '$.is_active') = 1 GROUP BY s.signal_id;

-- Signals per source and how many carry a location
SELECT source, COUNT(*) AS n, SUM(location_name IS NOT NULL) AS located FROM signals GROUP BY source ORDER BY n DESC;

-- Replies the parser rejected (to debug the prompt)
SELECT shipment_id, substr(output_payload, 1, 300) FROM decision_logs WHERE recommended_action IS NULL;
```

---

## 7. How this was verified

Run from a fresh clone on Python 3.10 (pandas 2.x) and 3.13 (pandas 3.x), against the live
sources on 2026-09-19:
* Every ingestion script ran. Every JSON record validates against `XtractSignal`, with no
  duplicate IDs, and `init_db.py --reset` loaded all 448 signals from 9 sources.
* Re-running `init_db.py` gave identical row counts and kept a `processed` status. A GDELT
  copy of a gCaptain URL (with `www.`, `utm_*`, and a `#fragment`) collapsed into the gCaptain
  row. An invalid signal was rejected. The shipment foreign keys rejected an unknown port.
* GDACS proximity was checked by hand: a Honduras flood linked to Puerto Barrios (73 km) and
  Puerto Cortés (95 km).
* The SCHEMAS.md examples validate, the docs' JSON Schemas list the same fields as the
  Pydantic models, and the SCHEMAS.md matching query is a verbatim copy of `MATCH_SQL`.
* `run_nemotron.py` was tested against a mock OpenAI-compatible server whose replies included
  `<think>` blocks, code fences, `"High"`, and `78`. All were parsed. An unusable reply was
  logged, retried on the next run, and gave exit code 1. Decided pairs were skipped on
  re-run. A stopped server gave a clear error and exit code 1. **It has not been run against a
  real Nemotron model.** The first real run is the one to watch (`--limit 3`).
* Failure paths: failed GDELT fetches (API and raw mode) and a failed GDACS fetch each gave
  exit code 1, and the previous output file was left untouched. A broken shipment reply
  stream was retried on the next run until it succeeded.

---

## 8. Known limitations

* **GDELT's Doc API can lag and throttle.** On 2026-09-19 its newest indexed article was from
  2026-09-13, and repeated use got this IP throttled for minutes. That is why `--mode raw`
  (current files, no rate limit) is the default.
* **News signals are mostly headline-only** (GDELT, Google News); the RSS feeds give short
  excerpts.
* **Rule-based tagging and filtering.** Expect some false positives. "**London**'s marine
  insurers…" is tagged as the port of London, and Google News lets through towns named
  "Port …" (a football report from *Port Neches*). These rarely match a shipment. The planned
  Xtract LLM step would refine tagging.
* **Google News links are redirects** (`news.google.com/rss/articles/…`), so the same story
  from a publisher's own feed isn't recognised as a duplicate.
* **GDACS radii are heuristics**, and PortWatch `region` events (no listed port) still never
  match a shipment.
* **Many reports of one event** each become a signal. SHP-DEMO-002 matched about 70 Hormuz
  stories; `--max-per-shipment` (default 5) caps the model calls per run.
* **Shipments are demo data** until real B/L data is loaded.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `GDELT raw fetch failed` / `skipping GKG file …` | GDELT's file server sometimes returns empty responses. Single files are skipped automatically. If all fail, re-run later. Everything else works without GDELT. |
| `HTTP 429 from api.gdeltproject.org` (only with `--mode api`) | The Doc API allows 1 request per 5 s and throttles repeated use for minutes. Wait, or use the default `--mode raw`. |
| `403 Client Error: Forbidden` from gdacs.org | GDACS's firewall rejects some User-Agents. Keep `USER_AGENT` in `common.py` free of "python-requests". |
| `<feed> failed: …` from `parse_rss.py` | That one feed is down or moved. The other feeds are still written (exit code 1). Check the URL in `FEEDS`. |
| `portwatch_ports.csv not found …` | Run `fetch_portwatch.py` before the other ingestion scripts. |
| `shipments table is empty` | Run `load_shipments.py`. |
| `cannot reach Nemotron at …` | Start the server or fix `NEMOTRON_BASE_URL`. For Ollama, include `/v1`. Test with `curl $NEMOTRON_BASE_URL/models`. |
| `unusable reply … no JSON object` | The model answered in prose or ran out of tokens while thinking. Raise `NEMOTRON_MAX_TOKENS`, lower the temperature, or tighten `SYSTEM_PROMPT`. Check the `output_payload` column. |
| `0 (signal, shipment) pairs to assess` | Every pair already has a decision (use `--redo`), no shipment route touches an active signal, or the news is older than `--since-days`. |
| `CHECK constraint failed: source …` | A new source was added without recreating the DB. Run `init_db.py --reset` (this wipes shipments and decisions; reload them afterwards). |

## 10. Data attribution

IMF PortWatch (portwatch.imf.org). GDACS (European Commission JRC / UN OCHA). The GDELT
Project. gCaptain, The Maritime Executive, Seatrade Maritime News, Hellenic Shipping News
Worldwide, and Splash247 (only titles, links, excerpts, and categories are stored, as
citations). Google News (titles and links only).
