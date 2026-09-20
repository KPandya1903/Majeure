# PortGuard AI — Schemas

The source of truth is the Pydantic models in [`src/schemas.py`](../src/schemas.py). The DDL
below is executed verbatim by `src/init_db.py`. If you change this file, change those two as
well.

**Conventions**

* Timestamps are ISO-8601 UTC strings ending in `Z` (`2026-09-19T15:28:46Z`).
* JSON-valued SQLite columns are `TEXT` holding compact JSON (query with `json_extract` /
  `json_each`).
* `signal_id = sha1(canonical_url(source_url))[:16]`. It is deterministic, so re-ingesting a
  signal updates it rather than duplicating it.
* `port_id` is the IMF PortWatch `portid` (`port1188`) for ports, or `chokepoint:<slug>` for
  chokepoints. The slug is the lowercased, hyphenated PortWatch chokepoint name
  (`chokepoint:strait-of-hormuz`, `chokepoint:bab-el-mandeb-strait`, `chokepoint:suez-canal`), or
  `chokepoint:red-sea` / `gulf-of-aden` / `black-sea` for the three sea areas. The full list is
  `CHOKEPOINTS` in `src/common.py`.

---

## 1. SQLite database schema

File: `data/portguard.db`. The spec asks for `signals`, `shipments` and `decision_logs`. The
supporting tables are:

| Table | Written by | Purpose |
|---|---|---|
| `ports` | `init_db.py` (from the PortWatch CSV) | Reference ports |
| `signal_ports` | `init_db.py`, `xtract.py` | Every port or chokepoint a signal touches. A sea area adds its straits (`SEA_AREA_LINKS`) |
| `events`, `event_signals` | `cluster_events.py` (rebuilt each run) | Reports clustered into real-world events, with corroboration |
| `traffic_daily`, `traffic_stats` | `init_db.py` (from `fetch_traffic.py` CSVs) | Measured transits and port calls |
| `action_drafts` | `draft_actions.py`, dashboard | Force majeure notices and customer emails |
| `xtract_results` | `xtract.py` | Nemotron's re-reading of news signals, re-applied after every load |
| `alerts_sent` | `alerts.py` | Alerts already sent, one per decision and channel |

Columns added after the first release (`decision_logs.event_id`, `estimated_cost_usd`,
`force_majeure_applicable`) are also added in place by `init_db.py` (`MIGRATIONS`), so
existing databases keep their data. Changed `CHECK` lists (new sources or match methods) only
take effect after `init_db.py --reset`.

```sql
PRAGMA foreign_keys = ON;

-- Reference data: IMF PortWatch ports database (data/portwatch_ports.csv).
CREATE TABLE IF NOT EXISTS ports (
    port_id                        TEXT PRIMARY KEY,           -- 'port1188'
    port_name                      TEXT NOT NULL,              -- 'Shanghai'
    full_name                      TEXT,                       -- 'Shanghai, China'
    country                        TEXT,
    iso3                           TEXT,
    continent                      TEXT,
    locode                         TEXT,                       -- UN/LOCODE 'CN SGH'
    lat                            REAL NOT NULL,
    lon                            REAL NOT NULL,
    vessel_count_total             INTEGER,
    vessel_count_container         REAL,
    vessel_count_dry_bulk          REAL,
    vessel_count_general_cargo     REAL,
    vessel_count_roro              REAL,
    vessel_count_tanker            REAL,
    industry_top1                  TEXT,
    industry_top2                  TEXT,
    industry_top3                  TEXT,
    share_country_maritime_import  REAL,
    share_country_maritime_export  REAL,
    active_disruption_count        INTEGER NOT NULL DEFAULT 0,
    active_disruption_ids          TEXT,                       -- ';'-joined PortWatch event ids
    portwatch_url                  TEXT,
    updated_at                     TEXT NOT NULL
);

-- One row per Xtract Signal, from every source (see docs/DATA_SOURCES.md).
CREATE TABLE IF NOT EXISTS pipeline_state (
    -- One row per ingest GROUP (the labels in run_pipeline.SCHEDULE_MIN), recording when the
    -- pipeline last ATTEMPTED that fetch. Distinct from a source's newest article: a quiet feed is
    -- polled on schedule and may still have published nothing for days. The sidebar shows both and
    -- must never conflate them.
    --
    -- This lives in the database rather than data/pipeline_state.json because that file is
    -- gitignored and Render's free tier has no persistent disk: the deployed API could never see it,
    -- so last_fetched_min was NULL for every source on the live site.
    source_group     TEXT PRIMARY KEY,
    last_fetched_at  TEXT NOT NULL,
    ok               INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    source           TEXT NOT NULL CHECK (source IN (
                         'portwatch', 'gdacs',
                         'nga_warnings', 'nhc', 'jtwc', 'usgs',
                         'joc', 'loadstar', 'gcaptain', 'maritime_executive', 'seatrade_maritime',
                         'splash247', 'container_news', 'world_cargo_news', 'marine_link',
                         'maritime_logistics', 'marine_insight', 'bunkerspot',
                         'hellenic_shipping_news', 'sea_news',
                         'freightwaves', 'supply_chain_dive', 'offshore_energy',
                         'gdelt', 'google_news')),
    title            TEXT NOT NULL,
    source_url       TEXT NOT NULL,
    published_at     TEXT,
    event_type       TEXT NOT NULL CHECK (event_type IN (
                         'security_threat', 'labor_strike', 'chokepoint_disruption',
                         'port_closure', 'severe_weather', 'natural_disaster',
                         'vessel_incident', 'port_congestion', 'regulatory_sanctions', 'other')),
    severity         TEXT CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    -- port_location, flattened (all NULL when port_location is null)
    location_name    TEXT,
    location_kind    TEXT CHECK (location_kind IN ('port', 'chokepoint', 'region')),
    location_port_id TEXT,
    location_country TEXT,
    location_lat     REAL,
    location_lon     REAL,
    match_method     TEXT CHECK (match_method IN ('structured', 'gazetteer', 'proximity', 'xtract')),
    raw_text         TEXT NOT NULL,
    metadata         TEXT NOT NULL DEFAULT '{}',              -- JSON object, source-specific
    status           TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'processed', 'dismissed')),
    ingested_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_published  ON signals (published_at);
CREATE INDEX IF NOT EXISTS idx_signals_event_type ON signals (event_type);
CREATE INDEX IF NOT EXISTS idx_signals_location   ON signals (location_port_id);

-- Every port or chokepoint a signal touches (primary port_location + related_port_ids).
-- No FK to ports: chokepoint ids are not ports.
CREATE TABLE IF NOT EXISTS signal_ports (
    signal_id   TEXT NOT NULL REFERENCES signals (signal_id) ON DELETE CASCADE,
    port_id     TEXT NOT NULL,
    is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (signal_id, port_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_ports_port ON signal_ports (port_id);

-- A shipment = one Bill of Lading plus its contract terms.
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id                    TEXT PRIMARY KEY,
    bl_number                      TEXT NOT NULL UNIQUE,
    carrier                        TEXT,
    shipper                        TEXT,
    consignee                      TEXT,
    vessel_name                    TEXT,
    imo                            TEXT,    -- IMO number of the real vessel, check-digit validated
    vessel_source                  TEXT,    -- 'public_registry' | 'demo': is this a real ship?
    carrier_source                 TEXT,    -- 'equasis' | 'name_prefix' | 'demo': how was the line attributed?
    voyage_number                  TEXT,
    pol_port_id                    TEXT NOT NULL REFERENCES ports (port_id),   -- port of loading
    pod_port_id                    TEXT NOT NULL REFERENCES ports (port_id),   -- port of discharge
    transshipment_port_ids         TEXT NOT NULL DEFAULT '[]',                 -- JSON array of port_id
    route_chokepoints              TEXT NOT NULL DEFAULT '[]',                 -- JSON array of 'chokepoint:<slug>'
    etd                            TEXT,
    eta                            TEXT NOT NULL,
    cargo_description              TEXT,
    container_count                INTEGER,
    cargo_value_usd                REAL,
    -- contract terms
    incoterm                       TEXT CHECK (incoterm IN ('EXW', 'FCA', 'CPT', 'CIP', 'DAP', 'DPU',
                                                            'DDP', 'FAS', 'FOB', 'CFR', 'CIF')),
    force_majeure_clause           TEXT,
    demurrage_usd_per_day          REAL,
    detention_usd_per_day          REAL,
    free_time_days                 INTEGER,
    late_delivery_penalty_usd_per_day REAL,
    deviation_permitted            INTEGER CHECK (deviation_permitted IN (0, 1)),
    notice_period_hours            INTEGER,
    governing_law                  TEXT,
    terms_source                   TEXT,    -- 'synthetic' | 'extracted': are the contract terms real?
    status                         TEXT NOT NULL DEFAULT 'in_transit'
                                       CHECK (status IN ('booked', 'in_transit', 'delivered', 'cancelled')),
    created_at                     TEXT NOT NULL,
    updated_at                     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shipments_pol ON shipments (pol_port_id);
CREATE INDEX IF NOT EXISTS idx_shipments_pod ON shipments (pod_port_id);

-- Audit trail: one row per Nemotron call for a (signal, shipment) pair.
CREATE TABLE IF NOT EXISTS decision_logs (
    decision_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         TEXT NOT NULL UNIQUE,                   -- = NemotronPayload.request_id
    signal_id          TEXT NOT NULL REFERENCES signals (signal_id),
    shipment_id        TEXT NOT NULL REFERENCES shipments (shipment_id),
    model              TEXT NOT NULL,                          -- Nemotron model identifier used
    risk_score         REAL CHECK (risk_score BETWEEN 0 AND 1),
    risk_level         TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    recommended_action TEXT CHECK (recommended_action IN (
                           'monitor', 'notify_customer', 'reroute', 'expedite',
                           'hold_cargo', 'invoke_force_majeure')),
    rationale          TEXT,
    citations          TEXT NOT NULL DEFAULT '[]',             -- JSON array of source URLs
    input_payload      TEXT NOT NULL,                          -- full NemotronPayload JSON
    output_payload     TEXT,                                   -- raw model response JSON
    human_decision     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (human_decision IN ('pending', 'approved', 'overridden')),
    human_notes        TEXT,
    latency_ms         INTEGER,
    created_at         TEXT NOT NULL,
    event_id           TEXT,                                   -- events.event_id the signal belonged to
    estimated_cost_usd REAL,                                   -- model's cost estimate for its recommendation
    force_majeure_applicable INTEGER CHECK (force_majeure_applicable IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_decisions_signal   ON decision_logs (signal_id);
CREATE INDEX IF NOT EXISTS idx_decisions_shipment ON decision_logs (shipment_id);

-- Clustered events: many reports of one real-world event (src/cluster_events.py, rebuilt each run).
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,                         -- anchor signal_id, or a hash for news-only events
    title            TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    severity         TEXT,
    location_id      TEXT NOT NULL,                            -- port_id or chokepoint:<slug>
    location_name    TEXT,
    location_kind    TEXT,
    anchor_signal_id TEXT NOT NULL REFERENCES signals (signal_id), -- representative signal sent to Nemotron
    is_structured    INTEGER NOT NULL CHECK (is_structured IN (0, 1)), -- anchored on PortWatch/GDACS
    n_signals        INTEGER NOT NULL,
    n_sources        INTEGER NOT NULL,
    n_publishers     INTEGER NOT NULL,
    confidence       REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    first_seen       TEXT,
    last_seen        TEXT,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_signals (
    event_id   TEXT NOT NULL REFERENCES events (event_id) ON DELETE CASCADE,
    signal_id  TEXT NOT NULL REFERENCES signals (signal_id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, signal_id)
);
CREATE INDEX IF NOT EXISTS idx_event_signals_signal ON event_signals (signal_id);

-- Daily traffic at chokepoints (transits) and ports (port calls), from PortWatch (src/fetch_traffic.py).
CREATE TABLE IF NOT EXISTS traffic_daily (
    location_id  TEXT NOT NULL,
    date         TEXT NOT NULL,                                -- YYYY-MM-DD
    metric       TEXT NOT NULL CHECK (metric IN ('transits', 'port_calls')),
    value        REAL NOT NULL,
    PRIMARY KEY (location_id, date)
);
CREATE TABLE IF NOT EXISTS traffic_stats (
    location_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    metric        TEXT NOT NULL CHECK (metric IN ('transits', 'port_calls')),
    latest_date   TEXT NOT NULL,
    avg_7d        REAL NOT NULL,                               -- mean of the latest 7 days
    baseline_avg  REAL NOT NULL,                               -- mean of the 90 days before those
    pct_change    REAL,                                        -- (avg_7d - baseline) / baseline * 100
    updated_at    TEXT NOT NULL
);

-- Correspondence drafted for a decision (src/draft_actions.py).
CREATE TABLE IF NOT EXISTS action_drafts (
    draft_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER NOT NULL REFERENCES decision_logs (decision_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('force_majeure_notice', 'customer_email')),
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    deadline_at   TEXT,                                        -- e.g. force majeure notice due
    generated_by  TEXT NOT NULL,                               -- 'template' or the model id
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'sent', 'discarded')),
    sent_at       TEXT,                                        -- when it was actually transmitted
    sent_to       TEXT,                                        -- the address it went to
    provider_message_id TEXT,                                  -- the mail provider's receipt
    created_at    TEXT NOT NULL,
    UNIQUE (decision_id, kind)
);

-- Nemotron "Xtract" refinements of news signals (src/xtract.py); re-applied by init_db.py after every load.
CREATE TABLE IF NOT EXISTS xtract_results (
    signal_id               TEXT PRIMARY KEY REFERENCES signals (signal_id) ON DELETE CASCADE,
    model                   TEXT NOT NULL,
    is_disruption           INTEGER NOT NULL CHECK (is_disruption IN (0, 1)),
    event_type              TEXT NOT NULL,
    severity                TEXT,
    location_text           TEXT,                              -- place as the model wrote it
    location_port_id        TEXT,                              -- resolved port_id / chokepoint id, if any
    expected_duration_days  REAL,
    confidence              REAL,
    raw_output              TEXT,
    created_at              TEXT NOT NULL
);

-- Alerts already sent for a decision (src/alerts.py), so each is sent once.
CREATE TABLE IF NOT EXISTS alerts_sent (
    decision_id  INTEGER NOT NULL REFERENCES decision_logs (decision_id) ON DELETE CASCADE,
    channel      TEXT NOT NULL,                                -- 'slack' or 'console'
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (decision_id, channel)
);
```

**Finding at-risk shipments.** This is the query `src/run_nemotron.py` runs (`MATCH_SQL`,
copied verbatim). It returns one row per (signal, shipment, matched port/chokepoint).
`run_nemotron.py` then keeps one row per (signal, shipment), choosing the strongest
`match_reason` in the order `port_of_discharge` > `port_of_loading` > `transshipment_port` >
`route_chokepoint`. Next it groups those rows into one item per **(event, shipment)** using
`event_signals` (`group_by_event`). The event's anchor signal represents it when the anchor
matched, otherwise its newest matching report represents it. The recency filter matters because `signals` also holds PortWatch history
back to 2018. `:since` is now minus `--since-days` (default 14).

```sql
SELECT s.signal_id, sh.shipment_id, sp.port_id AS matched_port_id,
       CASE WHEN sp.port_id = sh.pod_port_id THEN 'port_of_discharge'
            WHEN sp.port_id = sh.pol_port_id THEN 'port_of_loading'
            WHEN sp.port_id IN (SELECT value FROM json_each(sh.transshipment_port_ids)) THEN 'transshipment_port'
            ELSE 'route_chokepoint' END AS match_reason
FROM signals s
JOIN signal_ports sp ON sp.signal_id = s.signal_id
JOIN shipments sh    ON sp.port_id IN (sh.pol_port_id, sh.pod_port_id)
                     OR sp.port_id IN (SELECT value FROM json_each(sh.transshipment_port_ids))
                     OR sp.port_id IN (SELECT value FROM json_each(sh.route_chokepoints))
WHERE s.status <> 'dismissed'
  AND sh.status IN ('booked', 'in_transit')
  AND (:include_other = 1 OR s.event_type <> 'other')
  AND (json_extract(s.metadata, '$.is_active') = 1                -- PortWatch: ongoing or recent
       OR (s.source <> 'portwatch' AND s.published_at >= :since))  -- news: inside the look-back window
  AND (:redo = 1 OR NOT EXISTS (SELECT 1 FROM decision_logs d      -- skip pairs already decided
                                WHERE d.signal_id = s.signal_id AND d.shipment_id = sh.shipment_id
                                  AND d.recommended_action IS NOT NULL))
ORDER BY s.source = 'portwatch' DESC, s.published_at DESC, sh.shipment_id  -- authoritative events first
```

---

## 2. Xtract Signal Output

This is the shape of every object in `data/portwatch_signals.json`, `data/gdelt_signals.json`
and `data/rss_signals.json` (each file is a JSON **array** of these). The spec's required
fields are `title`, `source_url`, `port_location`, `event_type` and `raw_text`.
`port_location` must always be present but may be `null` when no location was found.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "XtractSignal",
  "type": "object",
  "required": ["signal_id", "source", "title", "source_url", "port_location",
               "event_type", "raw_text", "ingested_at"],
  "additionalProperties": false,
  "properties": {
    "signal_id":    { "type": "string", "pattern": "^[0-9a-f]{16}$" },
    "source":       { "enum": ["portwatch", "gdacs", "nga_warnings", "nhc", "jtwc", "usgs",
                               "joc", "loadstar", "gcaptain", "maritime_executive", "seatrade_maritime",
                               "splash247", "container_news", "world_cargo_news", "marine_link",
                               "maritime_logistics", "marine_insight", "bunkerspot",
                               "hellenic_shipping_news", "sea_news",
                               "freightwaves", "supply_chain_dive", "offshore_energy",
                               "gdelt", "google_news"] },
    "title":        { "type": "string", "minLength": 1 },
    "source_url":   { "type": "string", "format": "uri", "pattern": "^https?://",
                      "description": "Citation link to the original article or event page." },
    "published_at": { "type": ["string", "null"], "format": "date-time" },
    "port_location": {
      "anyOf": [{ "$ref": "#/$defs/PortLocation" }, { "type": "null" }]
    },
    "related_port_ids": {
      "type": "array", "items": { "type": "string" }, "default": [],
      "description": "Every affected port/chokepoint id, including port_location's."
    },
    "event_type": {
      "enum": ["security_threat", "labor_strike", "chokepoint_disruption", "port_closure",
               "severe_weather", "natural_disaster", "vessel_incident", "port_congestion",
               "regulatory_sanctions", "other"]
    },
    "severity":    { "enum": ["low", "medium", "high", "critical", null], "default": null },
    "raw_text":    { "type": "string", "minLength": 1,
                     "description": "Clean plain text the signal was derived from." },
    "ingested_at": { "type": "string", "format": "date-time" },
    "metadata":    { "type": "object", "default": {},
                     "description": "Source-specific extras (see table below)." }
  },
  "$defs": {
    "PortLocation": {
      "type": "object",
      "required": ["name", "kind", "match_method"],
      "additionalProperties": false,
      "properties": {
        "name":         { "type": "string" },
        "kind":         { "enum": ["port", "chokepoint", "region"] },
        "port_id":      { "type": ["string", "null"] },
        "country":      { "type": ["string", "null"] },
        "lat":          { "type": ["number", "null"], "minimum": -90,  "maximum": 90 },
        "lon":          { "type": ["number", "null"], "minimum": -180, "maximum": 180 },
        "match_method": { "enum": ["structured", "gazetteer", "proximity", "xtract"],
                          "description": "structured = given by the source; gazetteer = name found in text; proximity = nearest port to a GDACS event; xtract = place named by Nemotron (xtract.py)." }
      }
    }
  }
}
```

**`metadata` by source**

| Source | Keys |
|---|---|
| `portwatch` | `event_id`, `gdacs_event_type`, `alert_level`, `severity_text`, `from_date`, `to_date` (null = ongoing), `is_active`, `n_affected_ports`, `n_affected_chokepoints`, `gdacs_url` (GDACS types only) |
| `gdacs` | `event_id`, `episode_id`, `gdacs_event_type`, `alert_level`, `country`, `from_date`, `to_date`, `is_active` (GDACS `iscurrent`), `event_lat`, `event_lon`, `radius_km`, `nearby_ports` (list of `{port_id, name, distance_km}`, nearest first) |
| `gcaptain`, `maritime_executive`, `seatrade_maritime`, `hellenic_shipping_news`, `splash247` | `author`, `categories`, `guid`, `feed_title` |
| `gdelt` | `via` (`gkg` or `doc_api`), `domain`; raw mode adds `gkg_file`; API mode adds `language`, `source_country`, `social_image` |
| `google_news` | `publisher`, `publisher_url` |

**Example** (real output from `data/portwatch_signals.json`: an ongoing event that affects
chokepoints rather than ports):

```json
{
  "signal_id": "c1c9ad29f8d8f3fc",
  "source": "portwatch",
  "title": "RED SEA TENSIONS",
  "source_url": "https://portwatch.imf.org/pages/573013af3b6545deaeb50ed1cbaf9444",
  "published_at": "2023-12-16T00:00:00Z",
  "port_location": {
    "name": "Bab el-Mandeb Strait",
    "kind": "chokepoint",
    "port_id": "chokepoint:bab-el-mandeb-strait",
    "country": null,
    "lat": 12.7886,
    "lon": 43.3495,
    "match_method": "structured"
  },
  "related_port_ids": [
    "chokepoint:bab-el-mandeb-strait",
    "chokepoint:suez-canal",
    "chokepoint:cape-of-good-hope"
  ],
  "event_type": "security_threat",
  "severity": "high",
  "raw_text": "Trade disruptions in the Red Sea (near Bab el-Mandeb Strait) due to attacks on commercial ships. Affected chokepoints: Bab el-Mandeb Strait, Suez Canal, Cape of Good Hope.",
  "ingested_at": "2026-09-19T18:20:44Z",
  "metadata": {
    "event_id": 1000000,
    "gdacs_event_type": "OT",
    "alert_level": "RED",
    "severity_text": null,
    "from_date": "2023-12-16T00:00:00Z",
    "to_date": null,
    "is_active": true,
    "n_affected_ports": 0,
    "n_affected_chokepoints": 3
  }
}
```

A news signal has the same shape. Its `port_location` comes from the gazetteer
(`"match_method": "gazetteer"`), `severity` is `null` (left for the Xtract step), and `raw_text`
is the cleaned excerpt (RSS feeds) or the headline (GDELT, Google News). A GDACS signal's
`port_location` is the nearest port within the hazard's radius (`"match_method": "proximity"`),
and all ports in range are in `related_port_ids`.

**Mapping to the `signals` table:** `port_location.*` is flattened into the `location_*`
columns plus `match_method`. `metadata` is stored as JSON. `port_location.port_id` and each
`related_port_ids` entry become rows in `signal_ports` (the primary one gets `is_primary = 1`).

---

## 3. Nemotron Pipeline Input Payload

This is the input for one (signal, shipment) pair: the full Xtract Signal, plus the shipment's
Bill of Lading and contract terms, plus the reason the pair was matched and the actions the
model may recommend. The whole payload is stored in `decision_logs.input_payload`. Fields
with a `default` may be omitted when *constructing* a payload, but serialized payloads (what
Nemotron receives) always contain every field.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "NemotronPayload",
  "type": "object",
  "required": ["request_id", "created_at", "signal", "shipment", "match_context"],
  "additionalProperties": false,
  "properties": {
    "payload_version": { "const": "1.1", "default": "1.1" },
    "request_id":      { "type": "string", "format": "uuid" },
    "created_at":      { "type": "string", "format": "date-time" },
    "signal":          { "$ref": "xtract-signal (section 2)" },
    "shipment": {
      "type": "object",
      "required": ["shipment_id", "bill_of_lading", "contract_terms"],
      "additionalProperties": false,
      "properties": {
        "shipment_id":    { "type": "string" },
        "bill_of_lading": { "$ref": "#/$defs/BillOfLading" },
        "contract_terms": { "$ref": "#/$defs/ContractTerms" }
      }
    },
    "match_context": { "$ref": "#/$defs/MatchContext" },
    "analysis":      { "$ref": "#/$defs/Analysis", "description": "Rule-based context computed before the model is called." },
    "task":          { "$ref": "#/$defs/Task", "description": "allowed_actions = the six actions minus options analysis.exposure marks unavailable (impossible reroute, force majeure the clause excludes)." }
  },
  "$defs": {
    "PortRef": {
      "type": "object",
      "required": ["port_id", "name"],
      "properties": {
        "port_id": { "type": "string" },
        "name":    { "type": "string" },
        "country": { "type": ["string", "null"] },
        "lat":     { "type": ["number", "null"] },
        "lon":     { "type": ["number", "null"] }
      }
    },
    "BillOfLading": {
      "type": "object",
      "required": ["bl_number", "port_of_loading", "port_of_discharge", "eta"],
      "properties": {
        "bl_number":           { "type": "string" },
        "carrier":             { "type": ["string", "null"] },
        "shipper":             { "type": ["string", "null"] },
        "consignee":           { "type": ["string", "null"] },
        "vessel_name":         { "type": ["string", "null"] },
        "voyage_number":       { "type": ["string", "null"] },
        "port_of_loading":     { "$ref": "#/$defs/PortRef" },
        "port_of_discharge":   { "$ref": "#/$defs/PortRef" },
        "transshipment_ports": { "type": "array", "items": { "$ref": "#/$defs/PortRef" }, "default": [] },
        "route_chokepoints":   { "type": "array", "items": { "type": "string" }, "default": [] },
        "etd":                 { "type": ["string", "null"], "format": "date-time" },
        "eta":                 { "type": "string", "format": "date-time" },
        "cargo_description":   { "type": ["string", "null"] },
        "container_count":     { "type": ["integer", "null"], "minimum": 0 },
        "cargo_value_usd":     { "type": ["number", "null"], "minimum": 0 }
      }
    },
    "ContractTerms": {
      "type": "object",
      "properties": {
        "incoterm": { "enum": ["EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP",
                               "FAS", "FOB", "CFR", "CIF", null] },
        "force_majeure_clause":              { "type": ["string", "null"] },
        "demurrage_usd_per_day":             { "type": ["number", "null"], "minimum": 0 },
        "detention_usd_per_day":             { "type": ["number", "null"], "minimum": 0 },
        "free_time_days":                    { "type": ["integer", "null"], "minimum": 0 },
        "late_delivery_penalty_usd_per_day": { "type": ["number", "null"], "minimum": 0 },
        "deviation_permitted":               { "type": ["boolean", "null"] },
        "notice_period_hours":               { "type": ["integer", "null"], "minimum": 0 },
        "governing_law":                     { "type": ["string", "null"] }
      }
    },
    "MatchContext": {
      "type": "object",
      "required": ["match_reason"],
      "properties": {
        "match_reason":    { "enum": ["port_of_loading", "port_of_discharge", "transshipment_port",
                                      "route_chokepoint", "manual"] },
        "matched_port_id": { "type": ["string", "null"] },
        "distance_km":     { "type": ["number", "null"], "minimum": 0 },
        "days_until_eta":  { "type": ["number", "null"] }
      }
    },
    "Analysis": {
      "type": "object",
      "properties": {
        "exposure": {
          "type": ["object", "null"],
          "description": "src/exposure.py: cost of each option from the contract terms and stated assumptions.",
          "required": ["options"],
          "properties": {
            "currency": { "const": "USD" },
            "options": { "type": "array", "items": {
              "type": "object", "required": ["action", "available", "expected_delay_days", "cost_usd"],
              "properties": {
                "action": { "enum": ["monitor", "notify_customer", "reroute", "expedite", "hold_cargo", "invoke_force_majeure"] },
                "available": { "type": "boolean" }, "expected_delay_days": { "type": "number", "minimum": 0 },
                "cost_usd": { "type": "number", "minimum": 0 },
                "breakdown": { "type": "object", "additionalProperties": { "type": "number" } },
                "note": { "type": "string" } } } },
            "cheapest_action": { "type": ["string", "null"] },
            "force_majeure_covered": { "type": ["boolean", "null"], "description": "null = clause too general to tell" },
            "force_majeure_notice_due": { "type": ["string", "null"], "format": "date-time" },
            "assumptions": { "type": "array", "items": { "type": "string" } }
          }
        },
        "traffic": {
          "type": ["object", "null"],
          "description": "PortWatch daily data at the matched location: latest 7-day mean vs the 90 days before.",
          "required": ["location_id", "name", "metric", "latest_date", "avg_7d", "baseline_avg"],
          "properties": {
            "location_id": { "type": "string" }, "name": { "type": "string" },
            "metric": { "enum": ["transits", "port_calls"] }, "latest_date": { "type": "string" },
            "avg_7d": { "type": "number" }, "baseline_avg": { "type": "number" }, "pct_change": { "type": ["number", "null"] }
          }
        },
        "corroboration": {
          "type": ["object", "null"],
          "description": "src/cluster_events.py: the event this signal belongs to.",
          "required": ["event_id", "n_reports", "n_sources", "n_publishers", "confidence"],
          "properties": {
            "event_id": { "type": "string" }, "n_reports": { "type": "integer", "minimum": 1 },
            "n_sources": { "type": "integer", "minimum": 1 }, "n_publishers": { "type": "integer", "minimum": 1 },
            "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
            "first_seen": { "type": ["string", "null"], "format": "date-time" },
            "last_seen": { "type": ["string", "null"], "format": "date-time" },
            "headlines": { "type": "array", "items": { "type": "object", "required": ["title", "source", "url"],
              "properties": { "title": { "type": "string" }, "source": { "type": "string" }, "url": { "type": "string" } } } }
          }
        }
      }
    },
    "Task": {
      "type": "object",
      "required": ["objective", "allowed_actions"],
      "properties": {
        "objective":       { "const": "assess_disruption_risk" },
        "allowed_actions": {
          "type": "array", "minItems": 1, "uniqueItems": true,
          "items": { "enum": ["monitor", "notify_customer", "reroute", "expedite",
                              "hold_cargo", "invoke_force_majeure"] }
        },
        "instructions": { "type": ["string", "null"] }
      }
    }
  }
}
```

**Example:**

```json
{
  "payload_version": "1.1",
  "request_id": "0b6d7a3e-4c1f-4a55-9b1e-2f6f0d9c7a10",
  "created_at": "2026-09-19T16:05:00Z",
  "signal": { "...": "full XtractSignal object, e.g. the RED SEA TENSIONS example above" },
  "shipment": {
    "shipment_id": "SHP-0001",
    "bill_of_lading": {
      "bl_number": "DEMO-BL-0001",
      "carrier": "Demo Line",
      "shipper": "Demo Exporter Co.",
      "consignee": "Demo Importer BV",
      "vessel_name": "DEMO VESSEL",
      "voyage_number": "042W",
      "port_of_loading":   { "port_id": "port1188", "name": "Shanghai",  "country": "China",           "lat": 31.19, "lon": 121.64 },
      "port_of_discharge": { "port_id": "port1114", "name": "Rotterdam", "country": "The Netherlands", "lat": 51.92, "lon": 4.22 },
      "transshipment_ports": [
        { "port_id": "port1201", "name": "Singapore", "country": "Singapore", "lat": 1.27, "lon": 103.71 }
      ],
      "route_chokepoints": ["chokepoint:malacca-strait", "chokepoint:bab-el-mandeb-strait", "chokepoint:suez-canal"],
      "etd": "2026-09-10T00:00:00Z",
      "eta": "2026-10-14T00:00:00Z",
      "cargo_description": "Consumer electronics, 40' HC",
      "container_count": 12,
      "cargo_value_usd": 480000
    },
    "contract_terms": {
      "incoterm": "CIF",
      "force_majeure_clause": "Neither party is liable for delay caused by strikes, war, blockade, or closure of ports or canals beyond its reasonable control, provided notice is given within 48 hours.",
      "demurrage_usd_per_day": 150,
      "detention_usd_per_day": 100,
      "free_time_days": 5,
      "late_delivery_penalty_usd_per_day": 1000,
      "deviation_permitted": true,
      "notice_period_hours": 48,
      "governing_law": "English law"
    }
  },
  "match_context": {
    "match_reason": "route_chokepoint",
    "matched_port_id": "chokepoint:bab-el-mandeb-strait",
    "distance_km": 0.0,
    "days_until_eta": 30.2
  },
  "analysis": {
    "exposure": {
      "currency": "USD",
      "options": [
        { "action": "monitor", "available": true, "expected_delay_days": 10, "cost_usd": 10000,
          "breakdown": { "late_delivery_penalty": 10000, "demurrage_detention": 0 }, "note": "Wait it out: ~10 days of delay." },
        { "action": "reroute", "available": true, "expected_delay_days": 12, "cost_usd": 30000,
          "breakdown": { "late_delivery_penalty": 12000, "carrier_surcharge": 18000 }, "note": "Reroute via the Cape of Good Hope (+12 days)." },
        { "action": "invoke_force_majeure", "available": true, "expected_delay_days": 10, "cost_usd": 0,
          "breakdown": { "demurrage_detention": 0, "late_delivery_penalty": 0 },
          "note": "The clause covers this kind of event. Written notice is due by 2026-09-21 19:32 UTC." }
      ],
      "cheapest_action": "invoke_force_majeure",
      "force_majeure_covered": true,
      "force_majeure_notice_due": "2026-09-21T19:32:09Z",
      "assumptions": ["... see src/exposure.py ASSUMPTIONS ..."]
    },
    "traffic": { "location_id": "chokepoint:bab-el-mandeb-strait", "name": "Bab el-Mandeb Strait", "metric": "transits",
                 "latest_date": "2026-09-13", "avg_7d": 27.3, "baseline_avg": 31.7, "pct_change": -13.8 },
    "corroboration": { "event_id": "c1c9ad29f8d8f3fc", "n_reports": 21, "n_sources": 5, "n_publishers": 15, "confidence": 0.99,
                       "first_seen": "2023-12-16T00:00:00Z", "last_seen": "2026-09-19T18:11:00Z",
                       "headlines": [{ "title": "Houthis Seize Bab al-Mandeb, Closing World's Second Oil Chokepoint",
                                       "source": "ibtimes.com", "url": "https://jp.ibtimes.com/houthis-seize-bab-al-mandeb-104493" }] }
  },
  "task": {
    "objective": "assess_disruption_risk",
    "allowed_actions": ["monitor", "notify_customer", "reroute", "expedite", "hold_cargo", "invoke_force_majeure"],
    "instructions": "Assess delay and cost exposure for this shipment given the signal and contract terms. Cite signal.source_url."
  }
}
```

---

## 4. Nemotron Decision Output

`run_nemotron.py` asks Nemotron to reply with only this object (`NemotronDecision` in
`src/schemas.py`). It is stored flattened in `decision_logs`, together with the full input
payload (`input_payload`) and the raw API response (`output_payload`).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "NemotronDecision",
  "type": "object",
  "required": ["risk_score", "risk_level", "recommended_action", "rationale"],
  "properties": {
    "risk_score":         { "type": "number", "minimum": 0, "maximum": 1 },
    "risk_level":         { "enum": ["low", "medium", "high", "critical"] },
    "recommended_action": { "enum": ["monitor", "notify_customer", "reroute", "expedite",
                                     "hold_cargo", "invoke_force_majeure"],
                            "description": "Must also be in the payload's task.allowed_actions." },
    "rationale":          { "type": "string", "minLength": 1 },
    "citations":          { "type": "array", "items": { "type": "string" }, "default": [] },
    "estimated_cost_usd": { "type": ["number", "null"], "minimum": 0, "default": null,
                            "description": "Expected cost of the recommended action." },
    "force_majeure_applicable": { "type": ["boolean", "null"], "default": null }
  }
}
```

`run_nemotron.py --engine rules` produces the same object without a model (`model =
portguard-rules-v1`): the cheapest available option from `analysis.exposure` (upgraded from
`monitor` to `notify_customer` at high risk), with a risk score built from severity, measured
traffic, corroboration, and time to ETA. Use it as a baseline, or when no GPU is available.

**Lenient parsing.** Local reasoning models are messy, so the parser (`extract_json`,
`parse_decision`):
* drops `<think>…</think>` blocks and takes the first JSON object anywhere in the reply (prose
  and ``` fences are ignored);
* ignores extra keys, lowercases labels (`"High"` → `high`, `"Invoke Force Majeure"` →
  `invoke_force_majeure`), and reads a score between 1 and 100 as a percentage (`78` → `0.78`);
* always makes sure `citations` contains `signal.source_url`, adding it if the model left it
  out;
* rejects a `recommended_action` that isn't in `task.allowed_actions`.

**When a reply can't be used,** the row is still written to `decision_logs`, with
`recommended_action = NULL` and the raw reply in `output_payload`. The pair is retried on the
next run. (Event, shipment) pairs that already have a non-NULL decision are skipped unless
`--redo` is passed.
