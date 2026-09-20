# PortGuard AI — Data Sources

This file covers every free, unauthenticated source, how each is queried, and how the raw
response becomes clean data. Everything here was checked against the live endpoints on
2026-09-19.

| # | Source | Type | Script | Output | Typical yield (2026-09-19) |
|---|---|---|---|---|---|
| 1 | IMF PortWatch | ArcGIS REST (FeatureServer) | `src/fetch_portwatch.py` | `data/portwatch_ports.csv`, `data/portwatch_signals.json` | 2,065 ports, 132 curated events |
| 2 | GDELT 2.0 (raw GKG files, or Doc API) | zipped TSV / JSON REST | `src/fetch_gdelt.py` | `data/gdelt_signals.json` | ~48 stories from 6 h of files |
| 3 | Maritime trade press (gCaptain, Maritime Executive, Seatrade Maritime, Hellenic Shipping News, Splash247) | RSS 2.0 | `src/parse_rss.py` | `data/rss_signals.json` | ~180 items |
| 4 | GDACS live hazard alerts | RSS 2.0 + GDACS/GeoRSS tags | `src/fetch_gdacs.py` | `data/gdacs_signals.json` | ~30 alerts, ~10 linked to ports |
| 5 | Google News search | RSS 2.0 | `src/fetch_google_news.py` | `data/google_news_signals.json` | ~60 stories from 3 days |

All requests send `User-Agent: PortGuardAI/0.1 (maritime disruption research)`. Keep
"python-requests" out of it: GDACS's firewall returns 403 for a User-Agent containing it.

---

## 1. IMF PortWatch

PortWatch (IMF with the University of Oxford) publishes port reference data and a curated
port-disruption database as ArcGIS Online hosted feature layers.

### Endpoints

| Purpose | URL |
|---|---|
| Dataset catalogue (human UI) | `https://portwatch.imf.org/search?collection=dataset` |
| ArcGIS REST services root (IMF org) | `https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services?f=json` |
| **Ports database** (used) | `https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/PortWatch_ports_database/FeatureServer/0/query` |
| **Disruptions database** (used) | `https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/portwatch_disruptions_database/FeatureServer/0/query` |
| **Daily port calls** (used by `fetch_traffic.py`, 5.8 M rows: always filter by `portid`) | `.../Daily_Ports_Data/FeatureServer/0/query` |
| Chokepoints DB (28 rows; IDs, names, and coordinates are mirrored in `CHOKEPOINTS`, `src/common.py`) | `.../PortWatch_chokepoints_database/FeatureServer/0/query` |
| **Daily chokepoint transits** (used by `fetch_traffic.py`, ~79 k rows) | `.../Daily_Chokepoints_Data/FeatureServer/0/query` |
| Layer metadata (fields, `maxRecordCount`) | `<layer>/FeatureServer/0?f=json` |
| Human citation page for a port or event | `https://portwatch.imf.org/pages/{pageid}` (`pageid` is an ArcGIS Hub Page item ID) |

### Query parameters (ArcGIS `query` operation)

| Param | Value | Notes |
|---|---|---|
| `where` | `1=1` | All rows |
| `outFields` | `*` | |
| `returnGeometry` | `false` | Coordinates are already attribute fields (`lat`/`lon`, `lat`/`long`) |
| `orderByFields` | `ObjectId ASC` | Stable ordering is required for offset paging |
| `resultOffset` / `resultRecordCount` | `0, 1000, 2000…` / `1000` | Page size ≤ layer `maxRecordCount` (ports: 1000, disruptions: 2000) |
| `f` | `json` | |

```bash
curl "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/PortWatch_ports_database/FeatureServer/0/query?where=1%3D1&outFields=*&returnGeometry=false&orderByFields=ObjectId&resultOffset=0&resultRecordCount=1000&f=json"
```

**Pagination:** keep requesting with `resultOffset += len(features)` while the response has
`"exceededTransferLimit": true`. ArcGIS reports errors as HTTP 200 with an `{"error": {...}}`
body, so the script checks for that key.

### Ports layer — `PortWatch_ports_database` (2,065 rows)

| Field | Type | Example | Notes |
|---|---|---|---|
| `portid` | string | `port1188` | Primary key; the disruptions layer refers to ports by this ID |
| `portname` / `fullname` | string | `Shanghai` / `Shanghai, China` | |
| `country`, `ISO3`, `continent` | string | `China`, `CHN`, `Asia` | |
| `LOCODE` | string | `CN SGH` | UN/LOCODE, space-separated |
| `lat`, `lon` | double | `31.19`, `121.64` | |
| `vessel_count_total`, `vessel_count_{container,dry_bulk,general_cargo,RoRo,tanker}` | number | `41093` | Port-call volume; used to rank ambiguous matches |
| `industry_top1..3` | string | | Main industries served |
| `share_country_maritime_import/export` | double | | Port's share of national seaborne trade |
| `pageid` | string | `bde0534e…` | Hub Page ID for the citation URL |

### Disruptions layer — `portwatch_disruptions_database` (132 rows, 2018 → present)

| Field | Type | Notes |
|---|---|---|
| `eventid` | int | GDACS event ID for natural hazards |
| `eventtype` | string | GDACS code: `TC` tropical cyclone, `EQ` earthquake, `FL` flood, `DR` drought, `VO` volcano, `WF` wildfire, `OT` other (strikes, Red Sea, Panama Canal, bridge collapse, …) |
| `eventname`, `htmldescription`, `severitytext` | string | Description of the event |
| `alertlevel` | string | `RED` / `ORANGE` (GDACS scale; `GREEN` is possible) |
| `fromdate`, `todate` | epoch **ms** | **`todate = null` means the event is ongoing** (for example, *RED SEA TENSIONS*, *HORMUZ-26*) |
| `affectedports` | string | **`"; "`-separated ID list that mixes port IDs and chokepoint IDs**, e.g. `port2180; port2173; …` (up to 37) or `chokepoint4; chokepoint1; chokepoint7` (RED SEA TENSIONS). Chokepoint IDs `chokepoint1`–`chokepoint28` refer to the chokepoints layer |
| `n_affectedports` | int | Counts ports and chokepoints together |
| `lat`, `long` | float | Event location (note `long`, not `lon`) |
| `pageid` | string | Hub Page ID for the citation URL |

### Cleaning logic (`fetch_portwatch.py`)

1. Page through both layers.
2. **Ports:** rename columns to snake_case (matching the `ports` table), trim strings,
   de-duplicate on `port_id`, drop rows with no coordinates, and sort by `vessel_count_total`
   descending. Add `portwatch_url`, `active_disruption_count`, and `active_disruption_ids`.
3. **Disruptions:** convert epoch-ms to UTC ISO-8601, split `affectedports`, and resolve each ID
   to a port (ports layer) or a chokepoint (`chokepoint:<slug>`). IDs that resolve to neither
   are logged as a warning. An event is **active** when `todate` is null or within the last
   `--active-days` (default 30). Ongoing events' `… to: - .` becomes `… to: ongoing.`
4. Map each event to an Xtract Signal:
   * `port_location`: the affected port with the most traffic. If the event lists no ports, the
     first affected chokepoint. If it lists neither, a `region` at the event's coordinates.
   * `related_port_ids`: every affected port and chokepoint.
   * `event_type`: GDACS code mapped directly (`TC/FL/DR → severe_weather`,
     `EQ/VO/WF → natural_disaster`). `OT` events go through the keyword classifier.
   * `severity`: `RED → high`, `ORANGE → medium`, `GREEN → low`.
   * `source_url`: `https://portwatch.imf.org/pages/{pageid}`. The GDACS report URL
     `https://www.gdacs.org/report.aspx?eventid={eventid}&eventtype={eventtype}` goes in
     `metadata.gdacs_url` for GDACS event types.

### Daily traffic layers (`fetch_traffic.py`)

| Layer | Key fields | Used |
|---|---|---|
| `Daily_Chokepoints_Data` | `date` (DateOnly), `portid` (`chokepoint1..28`), `portname`, `n_total` (+ `n_container`, `n_tanker`, …), `capacity` | `n_total` = ships transiting per day |
| `Daily_Ports_Data` | `date`, `portid`, `portname`, `portcalls` (+ by vessel type), `import`, `export` | `portcalls` = ship calls per day |

Date filters use ArcGIS SQL: `where=date >= DATE '2026-06-01' AND portid IN ('port1188', …)`.
The data **lags about 5–7 days** (on 2026-09-19 the latest day was 2026-09-13 for chokepoints
and 2026-09-11 for ports). `fetch_traffic.py` fetches the last 111 days for all 28 chokepoints
and for the ports your shipments use. It stores the series (`traffic_daily`) and, per
location, the latest 7-day mean against the 90 days before it (`traffic_stats`).

Checked against history, the data clearly shows each crisis:

| Chokepoint | Before (60 days) | After (60 days) | Change |
|---|---|---|---|
| Suez Canal, Red Sea crisis (from 2023-12-16) | 76.4 transits/day | 40.6 | −47% |
| Bab el-Mandeb, same period | 78.0 | 33.7 | −57% |
| Cape of Good Hope, same period (diversions) | 49.8 | 85.8 | +72% |
| Panama Canal, drought (2023-06/07 vs 2023-12/2024-01) | 31.1 | 20.6 | −34% |
| Strait of Hormuz, HORMUZ-26 (2026-01/02 vs 2026-08/09) | 67.9 | 4.6 | −93% |

`backtest.py` replays these events. It uses only the traffic known the day after each event
started, and reports the following month's change separately.

---

## 2. GDELT 2.0

GDELT monitors worldwide online news and refreshes every 15 minutes. `fetch_gdelt.py` can read
it in two ways.

| | `--mode raw` (**default**) | `--mode api` |
|---|---|---|
| What | Global Knowledge Graph (GKG) 15-minute files | Doc API full-text search |
| Freshness | Current (the latest file is ≤15 min old) | **Its index can lag days.** On 2026-09-19 its newest article was from 2026-09-13 |
| Rate limit | None (static files) | 1 request per 5 s; heavy use gets the IP throttled for minutes |
| Cost | ~3 MB per file, 24 files (~75 MB) for the default 6 h | One small request |
| Filtering | We scan every headline ourselves | GDELT does the keyword search |

### 2a. Raw GKG files (default)

| Purpose | URL |
|---|---|
| Latest file list (updated every 15 min) | `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` |
| One GKG file | `http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.gkg.csv.zip` (timestamps every 15 min: `…0000`, `…1500`, `…3000`, `…4500`) |

`lastupdate.txt` has three lines (`export`, `mentions`, `gkg`), each `<size> <md5> <url>`. The
script reads the GKG timestamp and walks back `--hours × 4` files.

**GKG 2.1 columns used** (tab-separated, no header, UTF-8; very large fields, so
`csv.field_size_limit` is raised):

| Index | Field | Use |
|---|---|---|
| 1 | `V2.1DATE` (`YYYYMMDDHHMMSS`) | `published_at` |
| 2 | `V2SOURCECOLLECTIONIDENTIFIER` | Only `1` (web article with a URL) is kept |
| 3 | `V2SOURCECOMMONNAME` | `metadata.domain` |
| 4 | `V2DOCUMENTIDENTIFIER` | Article URL, which becomes `source_url` |
| 26 | `V2EXTRASXML` | Contains `<PAGE_TITLE>…</PAGE_TITLE>`, the headline |

GKG also has `V1THEMES` (e.g. `MARITIME`, `MARITIME_INCIDENT`) and `V2LOCATIONS`, but they're
**too noisy to filter on**: one 15-minute file tagged `MARITIME_INCIDENT` on a story about a
dancer sleeping in a studio. Headline keywords work far better.

**Quirks:** the file server sometimes answers `200` with an empty body, so `_download()`
retries up to 3 times. A missing or corrupt file is skipped with a warning; the run only fails
if *no* file downloads.

**Relevance filter (raw mode only):** a headline is kept only if `is_maritime(title)` is true
(ports, shipping, vessels, tankers, cargo, canals, straits, dockworkers, chokepoint names…)
**and** `classify_event(title)` is not `other`. In testing, 6 h of files held 17,306
articles, of which 143 had maritime headlines and 48 were kept after the disruption filter and
de-duplication.

### 2b. Doc API (`--mode api`)

```
https://api.gdeltproject.org/api/v2/doc/doc?query=(port OR maritime OR shipping) (strike OR delay OR disruption)&mode=artlist&maxrecords=50&format=json
```

| Param | Default in script | Notes |
|---|---|---|
| `query` | `(port OR maritime OR shipping) (strike OR delay OR disruption) sourcelang:english` | `sourcelang:english` is appended because the classifier and gazetteer are English-only (`--all-languages` turns it off). `OR` groups must be in parentheses, and adjacent groups are ANDed |
| `mode` | `artlist` | |
| `maxrecords` | `50` (`--max-records`, max 250) | |
| `format` | `json` | |
| `timespan` | `7d` (`--timespan`) | |
| `sort` | `datedesc` | |

The response is `{"articles": [{"url", "title", "seendate": "20260919T121500Z", "domain",
"language", "sourcecountry", "socialimage"}]}`. The `articles` key is missing when nothing
matched. **Rate limit: one request every 5 s.** Faster calls get HTTP 429 with a plain-text
body; retries run up to 6 attempts with backoff from 6 s to 96 s (about 3 minutes in total).
Other errors (e.g. a query term that's too short) come back as HTTP 200 with plain text, so the
body is checked before JSON parsing. Parsing uses `json.loads(strict=False)`, since titles
can contain control characters.

### Cleaning (both modes)

1. Unescape entities and remove GDELT's tokenisation spaces (`border , tighten` →
   `border, tighten`, `[ Blog ]` → `[Blog]`). Hyphens are left alone, because `el - Mandeb`
   can't be told apart from a ` - Publisher` separator.
2. **URL de-duplication** by canonical URL (lowercase host; no `www.`, fragment, trailing
   `/`, `utm_*`, `fbclid`, or `gclid`).
3. **Syndication de-duplication** by normalised title, ignoring a trailing ` - Publisher` /
   ` | Publisher`. One wire story often appears on dozens of domains.
4. `event_type` comes from the title; `port_location` from the gazetteer. `raw_text` is the
   headline (neither mode provides article bodies). `metadata.via` is `gkg` or `doc_api`.

---

## 3. Maritime trade-press RSS feeds

Seventeen feeds, all standard RSS 2.0. `parse_rss.py` reads them in the order below; the order
sets precedence when two feeds carry the same URL, so the highest-credibility titles win the
de-duplication when a wire story is syndicated. The feed name is the signal's `source`, and each
one's credibility tier lives in `PUBLISHER_TIER` (`src/common.py`).

| `source` | Feed URL | Items | Notes |
|---|---|---|---|
| `gcaptain` | `https://gcaptain.com/feed/` | 12 | Excerpts with wire bylines; categories such as `strait of hormuz` |
| `maritime_executive` | `https://maritime-executive.com/articles.rss` | ~87 | **No `pubDate`**, only `updated`, which the script falls back to |
| `seatrade_maritime` | `https://www.seatrade-maritime.com/rss.xml` | 50 | One-sentence standfirst as the summary |
| `hellenic_shipping_news` | `https://www.hellenicshippingnews.com/feed/` | 20 | Categories include Greek labels (harmless) |
| `splash247` | `https://splash247.com/feed/` | 10 | Categories carry region and company tags |
| `joc` | `https://www.joc.com/rss.xml` | 40 | Journal of Commerce; container/rates analysis, tier 2 |
| `loadstar` | `https://theloadstar.com/feed/` | 10 | The Loadstar; container-market reporting, tier 2 |
| `container_news` | `https://container-news.com/feed/` | 10 | High maritime purity (10/10 measured) |
| `world_cargo_news` | `https://www.worldcargonews.com/feed/` | 10 | Ports and terminal equipment focus |
| `marine_link` | `https://www.marinelink.com/news/rss` | 10 | Custom feed; `<!-- Take=… -->` header before the XML |
| `maritime_logistics` | `https://www.maritimeprofessional.com/rss/news` | 15 | Carries commodity stories too |
| `marine_insight` | `https://www.marineinsight.com/feed/` | 15 | High maritime purity (15/15 measured) |
| `bunkerspot` | `https://www.bunkerspot.com/rss` | 10 | Bunker fuel and emissions regulation |
| `sea_news` | `https://www.seanews.com.tr/rss/` | 100 | Largest feed by item count; Turkish publisher, English text |
| `freightwaves` | `https://www.freightwaves.com/news/feed` | 56 | **STRICT** — 23/56 maritime; rest is trucking/rail |
| `supply_chain_dive` | `https://www.supplychaindive.com/feeds/news/` | 10 | **STRICT** — 4/10 maritime |
| `offshore_energy` | `https://www.offshore-energy.biz/feed/` | 10 | **STRICT** — 3/10 maritime; mostly upstream energy |

Feeds in `STRICT` (`parse_rss.py`) have their items passed through `is_maritime()` before becoming
signals. Their maritime coverage is real but a minority of their output, and without the filter the
trucking and upstream-energy stories would dilute every downstream event cluster.

An earlier version of this file recorded FreightWaves as rejected because "its newest item is from
2022". That is no longer true — the feed's newest item is current, though it still carries a long
tail going back to 2021, which is another reason it is STRICT-filtered rather than trusted wholesale.

Checked and **not** usable from a server: UKMTO (`/rss/incidents`), MARAD MSCI advisories and Port
Technology all return 403 behind a WAF; Lloyd's List and TradeWinds return 500/404 without a
subscription; ReCAAP and EMSA publish no RSS at the documented paths. UKMTO and MARAD are the most
authoritative sources for the Hormuz and Bab el-Mandeb events PortGuard cares most about, so they
are worth revisiting with a headless fetch or an email-to-feed bridge.

**Per-item fields used:** `title`, `link` (the citation), `published`/`updated`,
`author`, `tags` (categories), `id` (guid), and `summary` (the excerpt). No feed carries full
articles, and PortGuard stores excerpts only, as citations.

### Cleaning (`parse_rss.py`)

1. Fetch with `requests` (User-Agent, timeout, retries), then `feedparser.parse(bytes)`. A
   malformed feed (`bozo`) only fails if no entries were parsed.
2. **Each feed fails independently.** One broken feed is logged and skipped, and the rest are
   written. The exit code is 1 if any feed failed, and if *every* feed failed the previous
   output is left untouched.
3. `raw_text`: strip tags, unescape entities, remove the WordPress "The post … appeared first
   on …" footer and any leading wire byline (`By A, B and C Sep 19, 2026 (Bloomberg) –`), and
   collapse whitespace. A truncated excerpt keeps a single trailing `…`.
4. `published_at` comes from `published_parsed`, else `updated_parsed` (UTC).
5. `event_type` is classified from title, excerpt, and categories; `port_location` comes from
   title and excerpt, and chokepoints are also matched in categories.
6. **All items are kept**, including general industry news (`event_type = "other"`, more
   than half of them). Matching ignores `other` by default, so they cost nothing downstream.
7. The same URL appearing in two feeds is kept once (the first feed wins).

`--only gcaptain splash247` limits a run to some feeds. To add a feed, add it to `FEEDS` in
`parse_rss.py` **and** to `Source` in `schemas.py`, and to the `signals.source` CHECK in
SCHEMAS.md §1.

---

## 4. GDACS live hazard alerts

GDACS (Global Disaster Alert and Coordination System; EC JRC and UN OCHA) publishes real-time
alerts for cyclones, earthquakes, floods, volcanoes, droughts, wildfires, and tsunamis.
PortWatch's disruption database is *curated from* GDACS, but only for notable events; the live
feed is complete and current.

| Purpose | URL |
|---|---|
| All current alerts (RSS, ~1 MB, ~370 items) | `https://www.gdacs.org/xml/rss.xml` |
| Human report page (the `link` of each item) | `https://www.gdacs.org/report.aspx?eventtype={TYPE}&eventid={ID}` |

**Fields used** (feedparser exposes the `gdacs:` and `geo:` namespaces as `gdacs_*` / `geo_*`):
`gdacs_eventtype` (TC, EQ, FL, VO, DR, WF, TS), `gdacs_alertlevel` (Green / Orange / Red),
`gdacs_eventid`, `gdacs_episodeid`, `gdacs_iscurrent` (`"true"`/`"false"`),
`gdacs_fromdate`/`gdacs_todate` (RFC 822; `fromdate` can be in the future for forecast
floods), `gdacs_country`, `geo_lat`/`geo_long`, `title`, `summary`, `link`.

### Filtering

* **Alert threshold, `--min-alert auto` (default):** Green and above for cyclones (TC),
  tsunamis (TS), earthquakes (EQ), floods (FL), and volcanoes (VO); Orange and above for
  wildfires (WF) and droughts (DR). On 2026-09-19 the feed held 377 items, 335 of them Green
  wildfire notices, and `auto` keeps 30. `--min-alert green|orange|red` applies one level to
  all types.
* **Freshness:** events with `iscurrent = true`, or that ended within `--days` (default 14).
* **No double counting:** events whose id already appears in `portwatch_signals.json`
  (PortWatch's curated copy) are skipped. Run `fetch_portwatch.py` first.

### Proximity linking (`match_method = "proximity"`)

GDACS gives a point, not a list of ports, so the script finds PortWatch ports within a
per-type radius with a vectorised haversine distance (`common.nearby_ports`):

| Type | Radius | Why |
|---|---|---|
| TC tropical cyclone, TS tsunami | 400 km | Wide wind and surge field |
| EQ earthquake | 150 km | Shaking damage to quays and cranes |
| FL flood, VO volcano | 100 km | Local to regional |
| WF wildfire | 50 km | Local |
| DR drought | 0 (none) | No meaningful point; only affects river and canal draft |

These radii are heuristics. `port_location` is the **nearest** port (up to 25 are listed in
`related_port_ids`, nearest first). `metadata.nearby_ports` keeps `{port_id, name,
distance_km}`, and `run_nemotron.py` passes that distance to Nemotron as
`match_context.distance_km`. Without a port in range, `port_location` is a `region` at the
event point (not matchable). `event_type` comes from the GDACS code (as for PortWatch) and
`severity` from the alert level (Red → high, Orange → medium, Green → low).

---

## 5. Google News search (RSS)

| | |
|---|---|
| URL | `https://news.google.com/rss/search?q={query}+when:{days}d&hl=en-US&gl=US&ceid=US:en` |
| Default query | `(port OR shipping OR maritime OR vessel OR tanker OR "container ship") (strike OR disruption OR closure OR congestion OR attack OR blockade OR delay)` |
| Window | `--days 3` (Google's `when:3d` operator) |
| Results | Up to 100 items, newest articles from the last few hours |

**Item shape:** `title` is `"Headline - Publisher"` (the script strips the suffix), `link`
is a **Google News redirect URL** (`news.google.com/rss/articles/CBMi…`), which opens the
article in a browser, `source` gives the publisher name and homepage (`metadata.publisher`,
`metadata.publisher_url`), and `description` just repeats the headline. `raw_text` is
therefore the headline.

**Relevance filter:** keyword search over *all* news is noisy, so a result is kept only if
`is_maritime(title)` **and** its `event_type` isn't `other` (typically 36 of 100 are dropped).
Some noise survives. Towns named "Port …" are the usual case (a football report from *Port
Neches*, a park closure in *Port Elgin*). Such items rarely get a `port_location`, so they
almost never match a shipment. Results are de-duplicated by URL and normalised title.

**Caveats:** the redirect URLs mean the same article found via a publisher's own feed gets a
different `signal_id` (cross-source de-duplication only works on identical URLs). Google News
RSS is an unofficial convenience feed; one request per run is polite.

---

## 6. Official government warnings (`fetch_official.py`)

Four first-party publishers. These are the bodies that *declare* the fact rather than report on it,
which is why they sit in tier 1 of `PUBLISHER_TIER` and why a single one of them can outrank several
news aggregators agreeing with each other.

| `source` | Endpoint | Shape | Notes |
|---|---|---|---|
| `nhc` | `https://www.nhc.noaa.gov/index-{at,ep,cp}.xml` | RSS | NOAA hurricane advisories, three basins. The "no active cyclones" placeholder item is dropped |
| `jtwc` | `https://www.metoc.navy.mil/jtwc/rss/jtwc.rss` | RSS | US Navy, NW Pacific / N Indian Ocean |
| `usgs` | `.../4.5_day.geojson` | GeoJSON | M4.5+ quakes; kept only when a tracked port is within 300 km |
| `nga_warnings` | `https://msi.nga.mil/api/publications/broadcast-warn?status=active&output=json` | JSON | NAVAREA broadcast warnings in force |

Three things about these that cost time to work out:

**JTWC bundles every active storm into one RSS item.** Using the item link would collide all storms
onto one `signal_id`, so the description is split on each storm's own warning-text product URL
(`…/products/wp2426web.txt`). feedparser normalises `href='…'` to double quotes — matching only
single quotes silently yields zero storms while the feed looks fine.

**NGA serves warnings that are *in force*, not warnings that are *new*.** The newest message the API
returns is from 2024 and the set reaches back to 2021; in-force navigational warnings legitimately
persist for years. They are ingested for standing-hazard context and cannot cluster into recent
events (`cluster_events.py` looks back 14 days). An old issue date here is not a stale feed. The
matched set is ~200 messages, capped at the newest `NGA_MAX_SIGNALS` so live news is not buried.

**NGA warnings carry no lat/lon field.** Coordinates are embedded in the message text in
degrees-and-decimal-minutes DTG format (`19-23.0N 092-03.1W`) and are parsed out with a regex, then
proximity-matched at a much tighter radius than GDACS uses (100 km, not 300). A navigational warning
concerns one specific piece of water; at 300 km the "nearest port" is a coincidence, and the feed
produced 259 essentially meaningless proximity matches before the radius was tightened.

---

## 7. Vessel identity (`fleet.py`)

The shipment book is built on **real vessels**. Names and IMO numbers come from Wikidata's
container-ship set, and every IMO is re-validated against its own check digit by `valid_imo()`
before it is used — a transposed digit still looks like an IMO number, and an invalid one sends
anyone verifying the fleet to a dead end. Two of the twenty vessels originally listed were caught
this way.

```sparql
SELECT ?name ?imo WHERE {
  ?ship wdt:P31/wdt:P279* wd:Q17210 ; wdt:P458 ?imo ; rdfs:label ?name .
  FILTER(LANG(?name) = "en")
}
```

Note the label must be bound with `rdfs:label` and filtered directly: putting the name filter inside
`SERVICE wikibase:label` matches nothing, because the label is bound after the filter runs.

The operating carrier is inferred from the vessel-name prefix ("MSC …", "CMA CGM …", "Ever …"),
which is reliable for liner tonnage and is recorded as `carrier_source = "name_prefix"` — an
attribution, not a registry lookup. The registered owner of a chartered ship is often a leasing
company rather than the line operating it, so Equasis or GISIS would answer a subtly different
question; both need credentials and neither publishes an API.

**Everything commercial is synthetic** and flagged `terms_source = "synthetic"`: BL numbers, shipper
and consignee, cargo values, and every contract term. Real Bills of Lading are commercially
confidential and are published nowhere. The dashboard states this next to the money rather than in a
footer.

---

## Shared enrichment (`src/common.py`)

### Event-type classifier

A case-insensitive keyword classifier. **The first rule that matches wins**, so rule order
settles ambiguity. For example, "missile strike" is `security_threat`, not `labor_strike`.

| Order | `event_type` | Sample triggers |
|---|---|---|
| 1 | `security_threat` | piracy, hijack, missile, drone, Houthi, attack, seized, warship, naval, war (not "trade war"), conflict, invasion, "ship strike", "strike on vessel", "vessel hit/struck/targeted" |
| 2 | `labor_strike` | strikes **with labour context** ("port/dock/union/rail/workers strike", "on strike", "strike by/over/ballot"), walkout, dockworkers, longshore(men), ILA, ILWU, work stoppage, lockout, industrial action |
| 3 | `chokepoint_disruption` | Any chokepoint name (see gazetteer): Suez, Panama Canal, Hormuz, Bab el-Mandeb, Red Sea, Malacca, Bosphorus, … |
| 4 | `port_closure` | closure, closed, closes, shut (down), halted, reopened, "suspended operations/sailings/transits" |
| 5 | `severe_weather` | typhoon, hurricane, cyclone, storm, flood, fog, drought, monsoon, gale, low water |
| 6 | `natural_disaster` | earthquake, tsunami, volcano, eruption, wildfire, landslide |
| 7 | `vessel_incident` | collision, grounding/aground, capsized, sank, fire aboard, explosion, oil spill, containers overboard, bridge collapse, blackout |
| 8 | `port_congestion` | congestion, backlog, bottleneck, queue, waiting time, dwell time, delay(s/ed) |
| 9 | `regulatory_sanctions` | sanction, tariff, embargo, customs, export/import ban, port fees, trade war |
| — | `other` | No rule matched |

A bare "strike" is deliberately **not** a labour trigger. In maritime news it usually means an
attack ("1 killed in ship strike on the Strait of Hormuz").

### Maritime relevance filter (`is_maritime`)

Firehose sources (GDELT raw, Google News) keep a headline only if it mentions shipping:
ports, harbours, shipping, shipments, maritime, vessels, ships, tankers, container ships,
containers, freight, cargo, canals, straits, docks/dockworkers, longshore…, seafarers,
mariners, shipowners, supply chains, chokepoints, sea lanes, or a chokepoint name. `navy`,
`naval`, `crew`, `terminal`, and `carrier` are deliberately left out, because they mostly hit
defence, aviation, and sports stories.

### Port gazetteer (`port_location` tagging)

* **Ports:** built from `data/portwatch_ports.csv` (1,821 matchable ports). Names are matched
  as **case-sensitive whole words**, since they are proper nouns. `Singapore-flagged` is not
  treated as a port mention. Alias rules:
  * a trailing " Port" is dropped (`Qingdao Port` → `Qingdao`);
  * parenthesised names contribute both parts (`Guangzhou (Nansha)` → `Guangzhou`, `Nansha`),
    except for codes such as `Houston (US-TX)` → `Houston`;
  * the part before a tight hyphen becomes an alias (`Sakai-Semboku` → `Sakai`,
    `Tangier-Mediterranean` → `Tangier`), but not the part after it, which is often generic;
  * `<Country> - Offshore Oil Terminal N` entries (~180) are excluded; otherwise every
    country name would match one;
  * names shorter than 4 characters, generic words (`Channel`, `Port`, …), and ambiguous
    English words or surnames (`Mobile`, `Hull`, `Anchorage`, `Split`, `Male`, `Moss`,
    `Nelson`, …; see `AMBIGUOUS_PORT_NAMES`) are excluded;
  * when a name belongs to several ports (e.g. *Cartagena*), the busiest wins
    (`vessel_count_total`).
* **Chokepoints:** the 28 PortWatch chokepoints plus Red Sea, Gulf of Aden, and Black Sea,
  matched case-insensitively (`port_id = "chokepoint:<slug>"`). gCaptain categories
  (`strait of hormuz`) are also searched for chokepoints.
* **Precedence:** title before body. Within one text, ports beat chokepoints. Among several
  ports, the busiest wins. Among several chokepoints, the first mentioned wins.
* This is a **recall-oriented pre-tag** (`match_method = "gazetteer"`), not ground truth. A
  known false-positive pattern is a city named for a non-port reason, e.g. "**London**'s marine
  insurers widen Black Sea risk zone" is tagged as the port of London. The Xtract LLM step is
  expected to confirm or correct it.

---

## Attribution & usage notes

* **IMF PortWatch:** cite "IMF PortWatch (portwatch.imf.org)". Disruption events derive from
  GDACS.
* **GDACS:** cite "GDACS, European Commission JRC and UN OCHA".
* **GDELT:** free and open. Cite "The GDELT Project". In API mode, stay within 1 request per
  5 s.
* **Trade press (gCaptain, Maritime Executive, Seatrade Maritime, Hellenic Shipping News,
  Splash247):** articles are copyrighted. PortGuard stores only titles, links, excerpts, and
  categories, as citations, and never republishes full text.
* **Google News:** an unofficial RSS endpoint. Titles and links are stored as citations; one
  request per run.
* **Refresh cadence (what `run_pipeline.py --loop 5` does, `SCHEDULE_MIN`):** GDACS and trade
  press every 5 min; Google News every 10 min; GDELT every 15 min, downloading only GKG files
  newer than the last run (`--incremental`, state in `data/gdelt_state.json`); PortWatch
  curated events every 60 min; PortWatch traffic every 6 h. Faster polling gains nothing:
  GDELT publishes every 15 minutes, and PortWatch traffic is daily and days behind. It also
  risks throttling (GDELT API, Google News).
