# PortGuard UI: the Windward look, examined and mapped

Examined windward.ai on 2026-09-19 by loading it in Chrome and reading its computed styles
(exact colours, fonts, type scale below), plus their product screenshot. This file is the design
brief for `src/dashboard.py`.

## 1. Their design system (measured, not guessed)

| Token | Value | Notes |
|---|---|---|
| Canvas (dark) | `#001028` (`rgb(0,16,40)`) | The dominant dark surface; a deeper navy than usual |
| Panel / deep | `#021953` | Secondary dark, used behind cards |
| Accent orange | `#FF6B17`, CTA `#FF6F00` | The only accent. Active tab, CTA, alerts, key figures |
| Body text (light bg) | `#626E86` | Blue-grey, not black, for paragraphs |
| Heading text (light bg) | `#000000` | Near-black headings |
| Text on dark | `#FFFFFF` | White with generous line-height |
| Link / info blue | `#1CA5F2` | Sparing |
| Radii | `4px` (dominant), `6px` buttons, `12px` cards, `50%` icon circles | Restrained, never pill-shaped except the CTA |
| Type | "Akkurat LL" brand, Inter for UI, IBM Plex Mono for micro-labels | Free stand-ins: **Inter** + **IBM Plex Mono** |
| Scale | body 16/24, small 14/21, H2 32px **weight 300**, H1 48px weight 400, tracking −1.2px | Headings are **light weight, UPPERCASE**, tight tracking |
| Weights in use | 400 (most), 300 (headings), 600 (emphasis) | No bold walls of text |

The whole feel comes from three decisions: **one accent colour**, **light-weight uppercase
headings**, and **defence-intel chrome** (corner brackets, reticles, mono micro-labels).

## 2. Component inventory (what to steal)

1. **Alert strip** (top, above the nav): navy bar, bold uppercase orange text, an underlined
   `MONITOR LIVE ›` link, dismiss ×. Theirs currently reads *"TRACK VESSEL ACTIVITY IN THE
   STRAIT OF HORMUZ"* — we have that event live in our own database.
2. **Nav bar**: white, letterspaced wordmark with a ° mark, centred links, one orange CTA.
3. **Hero**: small orange uppercase eyebrow ("WINDWARD MARITIME AI™"), huge light uppercase
   headline, grey body copy, orange button, dark map imagery on the right.
4. **Corner-bracket cards**: floating panels with `⌐ ¬ ∟ ⌡` corner marks, a mono uppercase
   label plus icon (`DARK ACTIVITY ☀ EO`, `GPS JAMMING ((•)) RF`), containing one visual.
5. **Map annotation chips**: small dark/red rounded chips with an icon over the map
   (`📍 STRAIT OF HORMUZ`, `⚠ BEHAVIORAL RISK / GNSS MANIPULATION` = uppercase label above a
   bold value).
6. **Stat chip**: circular orange icon + orange figure + uppercase grey label
   (`↓ 32% DECREASE IN ACTIVITY COUNT`), inside a dark card with an expander chevron.
7. **Section tabs**: uppercase, letterspaced, active one orange with a 2px orange underline.
8. **Dark data table** (product shot): translucent panel, uppercase column headers, route as
   `LHFA → USLA` with a grey `Direct` / `1 TSP` subtitle, coloured status dot + short insight,
   and chips: green `Completed`, orange `+8d delay`, yellow `On time`.
9. **Route on map**: orange solid for the travelled leg, white dashed ahead, teardrop `POL` /
   `POD` pins, reticles around points of interest.
10. **Quote cards**: dark card, 1px orange border, 4px radius, grey logo.

## 3. Mapping to PortGuard

| Theirs | Ours | Source of the data |
|---|---|---|
| Alert strip "TRACK VESSEL ACTIVITY IN THE STRAIT OF HORMUZ · MONITOR LIVE" | **"⚠ HORMUZ-26 · TRANSITS DOWN 93% · 71 REPORTS · VIEW"** rotating through active events | `events` + `traffic_stats` |
| Eyebrow "WINDWARD MARITIME AI™" | **"PORTGUARD DECISION AI"** | static |
| H1 "MISSION-GRADE ALL-SOURCE OPERATIONAL INTELLIGENCE" | **"CONTRACT-GRADE DISRUPTION DECISIONS"** | static |
| Corner-bracket card `DARK ACTIVITY ☀ EO` | Same frame for each KPI: `ACTIVE EVENTS`, `AT RISK`, `EXPOSURE ⌁ USD`, `NEXT NOTICE ⏱` | KPI queries |
| Stat chip `↓ 32% DECREASE IN ACTIVITY COUNT` | `↓ 93% TRANSITS · STRAIT OF HORMUZ` | `traffic_stats.pct_change` |
| Map chip `📍 STRAIT OF HORMUZ` | Same, on the matched chokepoint | `signal_ports` |
| Map chip `⚠ BEHAVIORAL RISK / GNSS MANIPULATION` | `⚠ SECURITY THREAT / RED SEA TENSIONS` (uppercase label over bold value) | `signals.event_type` + title |
| Table `LHFA → USLA` + `Direct`/`1 TSP` | `CNSGH → NLRTM` + `Direct` / `1 TSP` | `ports.locode`, `transshipment_port_ids` |
| `Latest carrier ETA` vs `Maritime AI™ Predicted ETA` | `CONTRACT ETA` vs `PORTGUARD ETA` (ETA + expected delay) | `shipments.eta` + `exposure` |
| Status chips `+8d delay`, `On time` | `+10d`, `FM 46h`, `$30K`, `APPROVAL NEEDED` | exposure, deadline, decision |
| Status dot + insight | Risk dot + recommended action | `decision_logs` |
| Orange/dashed route, POL/POD pins | Same: solid to the ship's estimated position, dashed to arrival | `etd`, `eta`, port coordinates |
| Section tabs | Our tabs, restyled uppercase with the orange underline | — |
| "Request a Demo" CTA | **"RUN DECISIONS"** (the only orange button) | `run_nemotron.py` |

## 4. Build plan for `dashboard.py`

**Step 1: theme and chrome (~1 h).**
`.streamlit/config.toml`: `base="dark"`, `backgroundColor="#001028"`,
`secondaryBackgroundColor="#0A1F3C"`, `primaryColor="#FF6B17"`, `textColor="#E8EEF5"`,
`font="sans serif"`. Then one CSS block that: loads Inter + IBM Plex Mono; hides the Streamlit
menu, footer and header; sets headings to uppercase weight 300 with −1px tracking; styles tabs
(uppercase, letterspaced, orange underline when active); defines `.chip`, `.chip-warn`,
`.chip-risk`, `.chip-ok`, `.bracket-card` (corner brackets via four absolutely-positioned
pseudo-elements), `.eyebrow`, `.micro` (IBM Plex Mono, 11px, uppercase, letterspaced).

**Step 2: header + alert strip (~30 min).** An HTML block: alert strip built from the most
severe active event, then a brand row (`PORTGUARD°`, tabs, 🔔 with the pending count, "RUN
DECISIONS" button).

**Step 3: map restyle (~1.5 h).** Dark basemap; routes as two `PathLayer`s (solid orange
travelled, dashed white remaining, progress from ETD/ETA); `IconLayer` POL/POD pins; event
halos sized by report count; chokepoints dimmed to 20% opacity unless disrupted; annotation
chips as a `TextLayer` with background, in the chip style.

**Step 4: fleet table + details panel (~1.5 h).** Replace stacked expanders with one dense
dark table (LOCODE path, contract ETA, PortGuard ETA, delay chip, risk dot, recommendation,
exposure) and a right-hand details panel for the selected row: options, deadline, sources,
Approve/Override, drafts.

**Step 5: KPI bracket cards + stat chips (~45 min).** Replace `st.metric` with bracket cards,
and put a `↓ 93% TRANSITS` stat chip inside each decision card.

## 5. Copy and tone

Uppercase, declarative, operational. Their "mission-grade / all-source / explainable" becomes
our **"contract-grade / all-source / cited"**. Keep micro-labels in mono uppercase (`EVENT`,
`EXPOSURE`, `NOTICE DUE`), and figures large and light. Never more than one orange element per
row: orange means *act now*.

## 6. Limits

Streamlit can't do a fixed top bar, a real icon rail, or panels floating over the map without
fighting it; the plan above approximates with columns, CSS and pydeck layers. Corner brackets
and chips are pure CSS, so they're safe. If the UI becomes the selling point, the next step is a
small React front end over the same SQLite data (about a day), not more Streamlit CSS.
