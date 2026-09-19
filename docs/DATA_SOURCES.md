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
| Daily port calls / trade (not used yet) | `.../Daily_Ports_Data/FeatureServer/0/query` |
| Chokepoints DB (28 rows; IDs, names, and coordinates are mirrored in `CHOKEPOINTS`, `src/common.py`) | `.../PortWatch_chokepoints_database/FeatureServer/0/query` |
| Daily chokepoint transits (not used yet) | `.../Daily_Chokepoints_Data/FeatureServer/0/query` |
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

All five are standard RSS 2.0 (four are WordPress). `parse_rss.py` reads them in the order
below; the order sets precedence when two feeds carry the same URL. The feed name is the
signal's `source`.

| `source` | Feed URL | Items | Notes |
|---|---|---|---|
| `gcaptain` | `https://gcaptain.com/feed/` | 12 | Excerpts with wire bylines; categories such as `strait of hormuz` |
| `maritime_executive` | `https://maritime-executive.com/articles.rss` | ~87 | **No `pubDate`**, only `updated`, which the script falls back to |
| `seatrade_maritime` | `https://www.seatrade-maritime.com/rss.xml` | 50 | One-sentence standfirst as the summary |
| `hellenic_shipping_news` | `https://www.hellenicshippingnews.com/feed/` | 20 | Categories include Greek labels (harmless) |
| `splash247` | `https://splash247.com/feed/` | 10 | Categories carry region and company tags |

FreightWaves' feed (`/news/feed`) was checked and rejected: its newest item is from 2022.

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
* **Suggested refresh cadence:** PortWatch every 6–24 h; GDACS every 15–30 min; trade press
  and Google News every 15–30 min; GDELT raw every 15–60 min (use `--hours` equal to the
  interval plus a margin, since the DB keeps history).
