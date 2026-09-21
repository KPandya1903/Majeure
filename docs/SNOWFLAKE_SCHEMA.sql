-- Majeure — Snowflake schema (replaces the SQLite DDL formerly in docs/SCHEMAS.md §1).
-- Executed by src/init_db.py via src/db.py. Differences from the SQLite version, and why:
--
-- * No `PRAGMA foreign_keys` — not a Snowflake concept.
-- * `REFERENCES` and `CHECK (...)` are kept for self-documentation (they parse) but Snowflake
--   does NOT enforce them — uniqueness/FK integrity is guaranteed by the app's upsert logic
--   (src/db.py's MERGE-based `upsert()`), not the database.
-- * No `ON DELETE CASCADE` — not supported (FKs aren't enforced, so nothing to cascade).
--   Anywhere the app relied on cascade deletes, it now deletes children explicitly first
--   (see src/db.py `delete_cascade()` and its call sites).
-- * No `CREATE INDEX` — Snowflake has no user-managed indexes (micro-partitioning + automatic
--   clustering instead). At this data volume, no clustering keys are needed either.
-- * `INTEGER PRIMARY KEY AUTOINCREMENT` -> `NUMBER AUTOINCREMENT START 1 INCREMENT 1`.
-- * JSON-valued columns (`metadata`, `transshipment_port_ids`, `route_chokepoints`, `citations`,
--   `input_payload`, `output_payload`) stay `TEXT` holding compact JSON, exactly as in SQLite —
--   every Python call site already does `json.loads`/`json.dumps` itself. Only the few queries
--   that need to look *inside* the JSON in SQL (MATCH_SQL) use `PARSE_JSON(...)`/`LATERAL
--   FLATTEN`/the `:field` path operator instead of SQLite's `json_extract`/`json_each`.

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
CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    source           TEXT NOT NULL CHECK (source IN (
                         'portwatch', 'gdacs', 'gcaptain', 'maritime_executive', 'seatrade_maritime',
                         'hellenic_shipping_news', 'splash247', 'gdelt', 'google_news')),
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

-- Every port or chokepoint a signal touches (primary port_location + related_port_ids).
-- No FK to ports: chokepoint ids are not ports.
CREATE TABLE IF NOT EXISTS signal_ports (
    signal_id   TEXT NOT NULL REFERENCES signals (signal_id),
    port_id     TEXT NOT NULL,
    is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (signal_id, port_id)
);

-- A shipment = one Bill of Lading plus its contract terms.
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id                    TEXT PRIMARY KEY,
    bl_number                      TEXT NOT NULL UNIQUE,
    carrier                        TEXT,
    shipper                        TEXT,
    consignee                      TEXT,
    vessel_name                    TEXT,
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
    status                         TEXT NOT NULL DEFAULT 'in_transit'
                                       CHECK (status IN ('booked', 'in_transit', 'delivered', 'cancelled')),
    created_at                     TEXT NOT NULL,
    updated_at                     TEXT NOT NULL
);

-- Audit trail: one row per Nemotron call for a (signal, shipment) pair.
CREATE TABLE IF NOT EXISTS decision_logs (
    decision_id        NUMBER AUTOINCREMENT START 1 INCREMENT 1 PRIMARY KEY,
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
    event_id   TEXT NOT NULL REFERENCES events (event_id),
    signal_id  TEXT NOT NULL REFERENCES signals (signal_id),
    PRIMARY KEY (event_id, signal_id)
);

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
    draft_id      NUMBER AUTOINCREMENT START 1 INCREMENT 1 PRIMARY KEY,
    decision_id   INTEGER NOT NULL REFERENCES decision_logs (decision_id),
    kind          TEXT NOT NULL CHECK (kind IN ('force_majeure_notice', 'customer_email')),
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    deadline_at   TEXT,                                        -- e.g. force majeure notice due
    generated_by  TEXT NOT NULL,                               -- 'template' or the model id
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'sent', 'discarded')),
    created_at    TEXT NOT NULL,
    UNIQUE (decision_id, kind)
);

-- Nemotron "Xtract" refinements of news signals (src/xtract.py); re-applied by init_db.py after every load.
CREATE TABLE IF NOT EXISTS xtract_results (
    signal_id               TEXT PRIMARY KEY REFERENCES signals (signal_id),
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
    decision_id  INTEGER NOT NULL REFERENCES decision_logs (decision_id),
    channel      TEXT NOT NULL,                                -- 'slack' or 'console'
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (decision_id, channel)
);
