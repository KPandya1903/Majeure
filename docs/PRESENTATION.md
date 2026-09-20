# PortGuard AI

## SteelHacks XIII — 12-slide presentation script

**Tracks:** Xtract (Signal-to-Insight Engine) · Beyond the Chatbot (Best Use of NVIDIA Nemotron)

**Live:** pixelsplease.vercel.app · **API:** portguard-api.onrender.com

Every figure in this deck is either externally cited or measured from the running
system. Nothing is estimated for effect.

---

## Slide 1 — Title

**Visual:** full dashboard screenshot — Risk map tab, carrier index open, 33 vessels on the map.

# PortGuard AI

### Maritime disruption → shipment-specific, contract-aware decisions

29 live sources · 993 signals · 66 clustered events · 33 vessels

---

## Slide 2 — Problem statement

**Visual:** the Traffic tab chart, or a three-row table of the chokepoint drops.

**A disruption is public within minutes. Its cost to *your* container takes days to work out.**

When Hormuz closes, three questions decide the money:

1. How long if we wait?
2. Can we legally reroute — does *this* Bill of Lading permit deviation?
3. Does *this* force majeure clause cover *this* event, and when does notice expire?

Today that is a person reading news, then a PDF contract, then a calculator. Per
container. **The notice window often expires before the answer arrives.**

---

## Slide 3 — Who actually loses the money

**Visual:** two-column contrast — "Carriers" against "Cargo owners".

**The counter-intuitive part: carriers profited. Cargo owners paid.**

| Measure | Figure | Period |
|---|---|---|
| Maersk EBIT | **$6.5B, +65% year on year** (rates rose with the crisis) | 2024 |
| Maersk guidance | **$0 EBIT if the Red Sea reopens**, **$3.0B if rerouting continues** | 2025 |
| **Demurrage and detention added to global freight costs** (Drewry) | **more than $10B** | **per year** |
| D&D *collected* by nine carriers (US FMC finding) | $12.9B | Apr 2020 – Mar 2023, ~3 years |
| Cape of Good Hope reroute | **+$200–400 per TEU** | per container |

> **Do not conflate these two.** Demurrage and detention are daily charges for holding a
> container or terminal slot too long, and **force majeure does not waive them** — it waives the
> late-delivery penalty. The $12.9B figure is money that *was* billed and paid over three years,
> most of it legitimately owed. It is **not** a pile of unfiled force majeure claims.
>
> **There is no published figure for force majeure that goes unclaimed**, and there structurally
> cannot be: a claim never filed leaves no record. Do not assert one. If asked how big the prize
> is, cite the >$10B/yr D&D figure as the *ceiling* of what better timing could touch, then give
> the number this system actually computes: **$183,600 avoided across 33 shipments**, which is
> our own model and can be shown working.

The loss sits with the **beneficial cargo owner** — the manufacturer, retailer or
importer. They pay demurrage, detention and late-delivery penalties on contracts
they did not get to reprice.

> **Say this out loud.** Everyone assumes the shipping lines were the victims of
> the Red Sea crisis. They were the beneficiaries. That reframing is the pitch.

---

## Slide 4 — How we identified the problem

**Visual:** the All signals tab — article cards from mixed sources.

**We started from the data, not from an idea.**

1. Pulled IMF PortWatch daily transit counts for 2,065 ports and every major chokepoint.
2. Measured the drops ourselves against a 90-day baseline. **These are not taken
   from a press release** — they are computed in the product:

| Chokepoint | vs 90-day baseline | Transits per day |
|---|---|---|
| Kerch Strait | **−100.0%** | 0.0 against 3.6 |
| Strait of Hormuz | **−42.8%** | 5.3 against 9.2 |
| Bosporus Strait | **−25.3%** | 52.3 against 70.0 |
| Bab el-Mandeb Strait | **−13.8%** | 27.3 against 31.7 |
| Suez Canal | **−8.0%** | 37.9 against 41.2 |

*As of 2026-09-13. Source: IMF PortWatch daily traffic.*

3. The disruption data was **free and public**. The contract exposure was the part
   nobody had connected to it.

---

## Slide 5 — Databases we searched

**Visual:** the sidebar source list — 29 sources with their fetch times.

**29 sources wired. We also documented what does not work.**

**Working, and live in the product:**

- IMF PortWatch — 2,065 ports, 4,897 daily traffic rows
- GDACS hazard alerts
- **NGA NAVAREA** broadcast warnings — JSON, 386 in force
- **NOAA National Hurricane Center** — three basins
- **US Navy Joint Typhoon Warning Center**
- **USGS** — M4.5+ earthquakes, proximity-matched to ports
- 17 maritime trade-press RSS feeds — JOC, The Loadstar, gCaptain, Maritime
  Executive, Seatrade, Splash247, Container News, World Cargo News, Marine Link,
  Maritime Logistics, Marine Insight, Bunkerspot, Hellenic Shipping News, SeaNews,
  FreightWaves, Supply Chain Dive, Offshore Energy
- **Trade policy** — US Federal Register (JSON API), White House presidential actions,
  USTR press releases, WTO news. This is what feeds `regulatory_sanctions`: a tariff change
  or export control can strand a container as surely as a closed strait, and it usually
  arrives with more warning.
- GDELT and Google News

**Rejected, with reasons:**

- **UKMTO and MARAD MSCI return 403 to any server.** The two most authoritative
  sources for Hormuz and the Red Sea, blocked behind a WAF.
- Lloyd's List and TradeWinds — paywalled, 500 and 404 without a subscription.
- **AIS is a paid product.** So we do not claim live vessel tracking anywhere.

> Judges respect knowing what you rejected and why. This slide is credibility.

---

## Slide 6 — Existing solutions and the gap

**Visual:** three columns — Track → Alert → **Decide**, with the last one highlighted.

**Visibility platforms stop one step short.**

| Platform | What it answers |
|---|---|
| project44, FourKites | *Where is my container?* |
| Windward, Everstream | *Is there a disruption?* |
| Portcast, Sinay | *What is the new ETA?* |

All three answer questions about **the world**. None answers a question about
**your contract**.

**The gap we address:** nobody prices *wait against reroute against force majeure*
using **your** Bill of Lading's demurrage rate, deviation clause and notice
deadline — and then drafts the notice.

Disruption data is commoditised. **The contract is where the money is, and it is
where nobody is looking.**

---

## Slide 7 — Real data against simulated

**Visual:** an expanded shipment row showing the provenance line.

**We label every row. Provenance is a database column, not a footnote.**

**Real and verifiable:**

- 993 signals across 29 sources, every one with a live source URL
- 2,065 ports and 4,897 daily traffic rows from IMF PortWatch
- **33 real container vessels across 11 real carriers** — Maersk, MSC, CMA CGM,
  COSCO, Evergreen, OOCL, HMM, NYK, ONE, ZIM, APL
- **Every IMO number validated against its own check digit.** The validator caught
  two bad numbers during the build. Look any vessel up in Equasis or GISIS.

**Simulated, and flagged in the schema as `terms_source = 'synthetic'`:**

- Contract terms, BL numbers, shipper and consignee names, cargo values
- *Why:* real Bills of Lading are commercially confidential and published nowhere.
  The Xtract rules explicitly permit synthetic documents you create yourself.

**Estimated, and stated in the interface:**

- Vessel positions — computed from elapsed ETD to ETA along a real sea route.
  **Not AIS. The map says so.**

---

## Slide 8 — Solution

**Visual:** pipeline diagram — Ingest → Cluster → Match → Price → Draft → Approve.

**Six stages, from a headline to a signed notice.**

1. **Ingest** — 29 sources, each on its own schedule
2. **Cluster** — reports become events; confidence weighted by *who* reported it,
   not only how many did
3. **Match** — events to shipments by port and chokepoint
4. **Price** — wait, reroute or force majeure, from that Bill of Lading's own terms
5. **Draft** — force majeure notice and customer email
6. **Approve** — **nothing sends without a human.** 30 drafts waiting, 0 sent
   automatically

**Credibility tiering** is what makes extra sources useful rather than noisy:

`confidence = tier_floor + 0.15 × (publishers − 1)`

Tier 1 sources — NOAA, NGA, IMF PortWatch — floor at 0.75. Tier 4 aggregators floor
at 0.35. **One NOAA advisory outranks three aggregators repeating each other.**

---

## Slide 9 — The calculation

**Visual:** the "What each option costs" panel — Monitor $20,000, Reroute $48,000,
Force majeure $0 recommended.

**Rule-based, auditable, and no model guessing at money.**

**Wait** = `delay_days × late_delivery_penalty` plus demurrage and detention past
free time. Delay comes from a table by event type and severity — a chokepoint
disruption is **2, 6 or 12 days** for low, medium and high severity.

**Reroute** = `extra_days × penalty` plus a `$1,500 per container` surcharge. Real
alternatives at real distances:

| Chokepoint | Alternative | Extra days |
|---|---|---|
| Suez Canal, Bab el-Mandeb | Cape of Good Hope | +12 |
| Panama Canal | Suez or Cape Horn | +10 |
| Malacca Strait | Lombok and Makassar | +3 |
| Dover Strait | North of Scotland | +2 |
| **Strait of Hormuz, Bosporus** | **None** | **no sea route avoids them** |

The model returns `None` rather than inventing an alternative that does not exist.

**Force majeure** = demurrage and detention only. **It excuses the late-delivery
penalty, not the port charges.** Available only when the clause names that event
type and notice falls inside the contractual window.

> Worked example on screen: Monitor $20,000 · Reroute $48,000 ·
> **Force majeure $0, recommended.** Notice due 2026-09-21 19:32 UTC.

---

## Slide 10 — Nemotron: Beyond the Chatbot

**Visual:** architecture diagram with the Nemotron stages highlighted.

**Nemotron is in the pipeline, not bolted onto a chat box.**

| Stage | What Nemotron does |
|---|---|
| `xtract.py` | Refines raw signals — event type, severity, location — from unstructured news |
| `run_nemotron.py` | **The decision engine.** Structured-JSON risk, action and rationale per event and shipment |
| `ask.py` | Grounded question answering, cited, over the live database |
| `eval_model.py` | Our own benchmark harness |

**Evidence it works, including a failure we found:**

- Measured `reasoning_effort=none` as **3 to 4 times faster and more stable** on
  nemotron-3-nano:4b. Found by benchmarking, not by guessing.
- **The failure:** the hosted model ran at **about 39 seconds** on analytical
  questions. The documented 20-second budget **never fired**, because `requests`
  applies its timeout as a per-read gap rather than a total deadline — a model
  trickling tokens resets that clock indefinitely.
- **The fix:** a deterministic router answers the 194 most common questions from
  SQL in **about 1 millisecond**, and Nemotron keeps the genuinely open-ended ones.
  Roughly **39,000× faster on the hot path**, with the model where it earns its keep.

---

## Slide 11 — Xtract fit

**Visual:** a decision panel showing numbered, clickable citations.

**Against the four criteria, directly.**

**"Support for different sources without rebuilding everything."**
Fetchers are pluggable. **We added 20 sources**, taking it from 9 to 29. A `STRICT` flag filters low-purity feeds — FreightWaves measured 23 of 56
items as maritime.

**"A model that can find useful signals in incoming documents."**
Nemotron Xtract over news text, alongside a deterministic classifier. 993 signals
resolve into 66 events.

**"Insights that link back to where they came from."**
Every signal carries its `source_url`. Every event carries report and publisher
counts. Every decision carries a citation list, rendered as clickable links.
*HORMUZ-26: 125 reports from 94 publishers, confidence 0.99. Click through to any
one of them.*

**"A clean way to actually see and use the output."**
A live dashboard on one screen, with no page scrolling.

**Rules:** GDELT, RSS feeds and press releases — yes. Synthetic documents we created
ourselves — yes. **No LANXESS data** — correct, none is used.

---

## Slide 12 — Demo

**Visual:** the live application. Do not use slides for this section.

**Ninety seconds, in this order:**

1. **Sidebar** — 29 sources, all fetched minutes ago. It is live right now.
2. **Risk map** — 33 real vessels. Click **COSCO Shipping Lines** in the carrier
   index; its three ships light up and the other thirty fade. Hover one for a real
   IMO number, a real carrier, and an honest "estimated, not AIS".
3. **Shipments** — click **MAERSK SALALAH**. The detail opens underneath it: HIGH
   risk, Red Sea, **notice due in 33h 21m**.
4. **The calculation** — Monitor $20,000, Reroute $48,000, **Force majeure $0**. The
   cheapest option, derived from that Bill of Lading's own clause.
5. **The draft** — a force majeure notice, already written, awaiting approval.
   **Not sent.**
6. **Ask PortGuard** — *"How many ships does Cosco Africa have in transit?"* Answers
   instantly, and corrects the premise: COSCO AFRICA is a ship, not a company.

**Close on:** *"Everything you have just seen runs on 29 live public sources. The
only synthetic part is the contract terms — and the database labels every one of
them."*

---

## Two things to fix before presenting

1. **The `$0` force-majeure figures look like a bug to a judge.** They are correct —
   force majeure waives the late-delivery penalty, and these Bills of Lading still
   have free time left, so there is no demurrage yet. Say that out loud or the
   audience will assume the number is broken.
2. **Check the cron is firing** so the sidebar reads green rather than amber on
   stage. An amber "the ingest pipeline may not be running" warning during a live
   demo undercuts the whole freshness argument.

---

## Sources

- Maersk 2024 results — FreightWaves,
  `freightwaves.com/news/first-look-maersk-saw-across-the-board-growth-in-2024`
- Maersk and Hapag-Lloyd guidance — The Maritime Executive,
  `maritime-executive.com/article/hapag-and-maersk-raise-outlook-for-2024-as-volatile-market-fuels-profits`
- Red Sea crisis and Maersk profit — Seatrade Maritime,
  `seatrade-maritime.com/containers/red-sea-crisis-drives-maersk-to-3rd-most-profitable-ever-year`
- Demurrage and detention benchmark — Global Maritime Hub,
  `globalmaritimehub.com/report-presentation/demurrage-and-detention-benchmark-report-2`
- The hidden cost of demurrage and detention — BTX Global,
  `blog.btxglobal.com/2026/the-hidden-cost-of-demurrage-and-detention-how-global-shippers-lose-millions-without-realizing-it`
- Reroute cost per TEU — Transporeon,
  `transporeon.com/en/community/blog/the-red-sea-crisis-and-its-impacts-on-supply-chain`
- Chokepoint traffic figures — IMF PortWatch, measured in PortGuard as of 2026-09-13
