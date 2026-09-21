# Majeure — The Main Idea

> **In one sentence:** Majeure turns free, real-time maritime disruption data into
> shipment-specific, contract-aware decisions. For every disruption that touches a shipment's
> route, NVIDIA Nemotron reads the Bill of Lading terms and recommends what to do, citing the
> source.

---

## 1. The problem

Global trade runs through a small number of ports and chokepoints. When one of them is
disrupted, thousands of shipments are affected at once. The disruption might be a dockworker
strike, missile attacks near Bab el-Mandeb, a drought that restricts the Panama Canal, a
typhoon over Shanghai, or a bridge collapse that closes Baltimore.

The people responsible for those shipments (freight forwarders, importers and exporters,
logistics control towers, cargo insurers, trade-finance desks) face three gaps:

1. **The detection gap.** Disruption news is scattered across wire services, trade press,
   and official datasets. Nobody can read all of it for hundreds of live shipments.
2. **The relevance gap.** Most disruption news doesn't matter to any *particular* shipment.
   The question is not "is something happening in the Red Sea?" but "is **my** container on
   voyage 042W going through it, and when?"
3. **The contract gap.** Even when a shipment is affected, the right response depends on
   its contract. Does the force majeure clause cover war risk, or only natural disasters? Is
   the carrier allowed to deviate around the Cape of Good Hope? Who bears the risk under the
   Incoterm (CIF vs FOB)? How much does each day of delay cost in demurrage or late-delivery
   penalties? This information is buried in B/L and sales-contract terms that operations
   staff rarely have time to re-read under pressure.

Today these gaps are closed by hand, late, and inconsistently. Missing a notice deadline in a
force majeure clause, or rerouting when the contract forbids deviation, costs real money.

## 2. The solution

Majeure closes all three gaps in one automated loop:

| Gap | What Majeure does | Where in the code |
|---|---|---|
| Detection | Continuously ingests **free, open sources** (IMF PortWatch, GDACS, five maritime trade-press feeds, GDELT, Google News) into one normalised signal format. | `fetch_*.py`, `parse_rss.py` → `init_db.py` |
| Relevance | **Matches** each signal to shipments whose port of loading, port of discharge, transshipment port, or route chokepoint it touches, and drops stale signals. | `run_nemotron.py` (`MATCH_SQL`, `find_pairs`) |
| Contract | Prices the options (wait, reroute, invoke force majeure) from the contract terms, checks whether the force majeure clause covers the event, starts the notice-deadline clock, then sends it all to **Nemotron**, which returns a risk score, a recommended action, a cost estimate, and a cited rationale. | `exposure.py`, `run_nemotron.py` |
| Action | Drafts the force majeure notice and the customer email, and alerts the team before the deadline. | `draft_actions.py`, `alerts.py`, `dashboard.py` |

Every decision is written to an **audit log** (`decision_logs`) along with the exact input
the model saw, its raw output, and the evidence URLs. A human approves or overrides it. The
AI recommends; people decide.

## 3. How it works, layer by layer

```
 SENSE ─────────► UNDERSTAND ─────────► MATCH ─────────► DECIDE ─────────► AUDIT
 9 open feeds     Xtract Signal         route ⇄ signal   Nemotron +        decision_logs,
                  (one shape for all)   (ports and       contract terms    citations,
                                         chokepoints)                      human approval
```

### 3.1 Sense: two tracks of evidence

| Track | Source | Strength | Weakness |
|---|---|---|---|
| **Structured** | **IMF PortWatch**: the IMF and University of Oxford's port database (2,065 ports) and curated disruption events (GDACS cyclones, earthquakes, and floods, plus events such as the 2024 US East/Gulf Coast port strike, the Red Sea crisis, and the 2026 Hormuz disruption) | Authoritative. Already tagged with the exact affected port and chokepoint IDs, with severity and start/end dates. | Curated, so only notable events, and not instant. |
| **Structured** | **GDACS** live hazard alerts (EC JRC / UN OCHA) | Real-time, with coordinates. Majeure links each hazard to the ports within a per-type radius (a cyclone within 400 km, an earthquake within 150 km). | Natural hazards only; radius-based exposure is approximate. |
| **Unstructured** | **Maritime trade press**: gCaptain, Maritime Executive, Seatrade Maritime, Hellenic Shipping News, Splash247 | All maritime, written by specialists; excerpts give context. | More than half is general industry news (filtered out at matching). |
| **Unstructured** | **GDELT** raw news firehose (every 15 minutes, worldwide) and **Google News** search | Fast and broad. Catches attacks, strikes, and closures within minutes to hours. | Noisy and duplicated, so Majeure keeps only headlines about shipping *and* a disruption. Headline-only. |

Using both tracks gives **coverage and confirmation**: news raises the early alarm, and
PortWatch and GDACS provide the authoritative anchor. They reinforce each other when they
agree. On 2026-09-19, for example, the PortWatch HORMUZ-26 event was joined by dozens of
same-day tanker-attack reports from Google News, GDELT, and the trade press.

### 3.2 Understand: the Xtract Signal, then events

Every item from every source is converted into one **Xtract Signal**, which is the project's
name for a normalised, citable disruption record: title, `source_url` (the citation),
`port_location` (a port, chokepoint, or region, with IDs and coordinates), `event_type` (one
of 10 categories), `severity`, `raw_text`, and source-specific `metadata`.

* PortWatch events are mapped from their structured fields. GDACS hazards are linked by
  distance to the ports around them.
* News is pre-tagged by rules: a keyword classifier plus a **gazetteer** of the 2,065
  PortWatch ports and 31 chokepoints and sea areas. The optional **Xtract step**
  (`xtract.py`) then has Nemotron re-read the news in batches, correcting event type,
  location, and severity, and flagging items that aren't disruptions at all.
* Finally **`cluster_events.py`** groups reports into real-world **events**. On 2026-09-19,
  121 matchable signals became 24 events: HORMUZ-26 was confirmed by 71 reports from 50
  publishers, and the Red Sea crisis by 21 from 15. Each event gets a confidence score, and
  the model sees how well-confirmed it is.

### 3.3 Match: only what touches *your* route

A shipment's route is described by its B/L: port of loading, port of discharge,
transshipment ports, and the chokepoints it passes (for example Shanghai → Singapore →
Rotterdam via Malacca, Bab el-Mandeb, and Suez). Because signals and shipments use the same
IDs (PortWatch `portid`s and `chokepoint:<slug>`), matching is an exact join, not guesswork.
Stale signals are filtered out: a PortWatch event must be ongoing or recent, and news must
fall inside the look-back window. Each pair keeps its strongest reason (a disruption at the
discharge port outranks one at a chokepoint).

### 3.4 Decide: numbers first, then Nemotron reads the contract

Before any model is called, `exposure.py` prices the options from the contract terms:
*wait it out* (expected delay × late penalty, plus demurrage and detention for port-side
problems), *reroute* (extra sailing days plus carrier surcharges, or diversion to the nearest
alternate port plus haulage), and *invoke force majeure* (penalty excused, but only if the
clause covers this kind of event, and only with notice inside the notice period). Measured
traffic from PortWatch raises the expected delay when a chokepoint is visibly choked.

For each (event, shipment) pair, Majeure then builds a validated **Nemotron payload**:

* the full signal (what, where, when, severity, and source);
* the Bill of Lading (route, ETD/ETA, cargo, value);
* the **contract terms**: Incoterm, force majeure clause text, deviation permission, notice
  period, demurrage, detention, free time, late-delivery penalty, governing law;
* the **match context**: why the pair was selected, which port or chokepoint, and days until
  ETA;
* the **analysis**: the priced options and assumptions, force majeure coverage and the notice
  deadline, measured traffic at the matched location, and the event's corroboration with
  sample headlines;
* the task: the allowed actions `monitor`, `notify_customer`, `reroute`, `expedite`,
  `hold_cargo`, `invoke_force_majeure`.

Nemotron is told to use only the facts in the payload, pick one allowed action, and cite the
source. It returns a risk score (0–1), a risk level, the action, the expected cost of that
action, whether force majeure applies, a 2–4 sentence rationale, and citations. The same
contract produces a model-free baseline (`--engine rules`: cheapest available option), so
the demo works without a GPU and the model's added value can be compared. The parser accepts the messy output typical of reasoning models (thinking
blocks, code fences, percentages) and rejects actions that weren't offered.

**Why Nemotron:** NVIDIA's Nemotron models are open and can be self-hosted (NIM, vLLM,
Ollama). Commercially sensitive contract terms and cargo values therefore never have to leave
the operator's infrastructure. They're reached through the standard OpenAI-compatible API, so
the same code also works with NVIDIA's hosted endpoint.

### 3.5 Act and audit: drafts, alerts, approval

* `draft_actions.py` writes the **force majeure notice**, quoting the clause verbatim, naming
  the event, the expected delay, and the measured traffic, listing the evidence links, and
  showing its deadline. It also writes the **customer update email** with the cost
  comparison. Nemotron writes them when reachable; templates otherwise.
* `alerts.py` posts high-risk decisions that await approval to Slack (or the console), with
  the hours left before the notice deadline.
* It runs **continuously**: every 5 minutes Majeure checks its sources (each at the rate it
  actually changes), and new events reach the decision board within minutes of being
  reported.
* The **dashboard** shows everything: a risk map, a decision card per shipment (options
  table, deadline countdown, sources), Approve / Override with notes, editable drafts, traffic
  charts, the backtest, and an **Ask Majeure** chat that answers from the database.
* `decision_logs` keeps the model, score, action, cost, rationale, citations, the **complete
  input payload**, the **raw model output**, latency, and the human verdict. Any
  recommendation can be traced back to the exact evidence and contract text the model saw.

## 4. Worked example (real data, 2026-09-19)

**Event:** *RED SEA TENSIONS* (IMF PortWatch), "trade disruptions in the Red Sea (near
Bab el-Mandeb Strait) due to attacks on commercial ships". Alert level RED, ongoing since
2023-12-16. It affects Bab el-Mandeb, Suez, and the Cape of Good Hope, and is corroborated
by 21 current reports from 15 publishers, including *"Houthis Seize Bab al-Mandeb, Closing
World's Second Oil Chokepoint"*.

**Shipment (demo):** SHP-DEMO-001, 12 × 40' HC of consumer electronics ($480,000), Shanghai
→ Singapore → Rotterdam via Malacca, Bab el-Mandeb, and Suez, ETA 2026-10-20. CIF; force
majeure **covers war, blockade, hostilities, and canal closure** with 48 h notice; deviation
permitted; $1,000/day late penalty.

**What Majeure computes before asking the model:**

| Option | Cost | Why |
|---|---|---|
| Wait it out | ~$10,000 | ~10 days × $1,000 late penalty |
| Reroute via the Cape of Good Hope | ~$30,000 | +12 days × $1,000 + $1,500 × 12 containers surcharge |
| **Invoke force majeure** | **~$0** | Clause covers war and hostilities; penalty excused. **Notice due in 48 h** |

Nemotron receives this with the traffic at Bab el-Mandeb (27 transits/day in the latest week)
and the corroborating headlines. The draft notice is ready for approval, and a Slack alert
counts down the 48 hours.

**Contrast, same day:** SHP-DEMO-002 (Jebel Ali → Nhava Sheva through Hormuz) meets
HORMUZ-26, where transits are down more than 90% against pre-crisis levels. Its FOB contract
**excludes war and security incidents** from force majeure, **forbids deviation**, and no sea
route avoids Hormuz anyway. Force majeure and rerouting are both unavailable; waiting costs
about $30,000 at $2,500/day, so the right move is to notify the customer now. Same kind of
disruption, opposite advice, because the contracts differ. That contrast is the core of the
demo.

**Backtest (real history):** replaying five past disruptions with the traffic known on day
one, Majeure would have flagged the Red Sea crisis 26 days before the example shipment's
arrival (Bab el-Mandeb transits then fell 41% in the following month). It would have caught
the Baltimore bridge collapse (port calls −76%) and the Hormuz closure (−96%), with the
cheapest contract-compliant option priced each time.

## 5. What is real and what is demo

| Part | Status |
|---|---|
| PortWatch, GDACS, trade-press RSS, GDELT, Google News ingestion, PortWatch daily traffic | **Real, live data** from public endpoints; no keys |
| Normalisation, dedupe, relevance filtering, classification, location tagging, proximity linking, event clustering | **Real**, rule-based, tested on live data |
| Exposure pricing, force majeure coverage check, notice deadlines | **Real logic**; the rates and delay tables are **stated planning assumptions** (`exposure.py`), not quotes |
| Database, matching, payloads, rules engine, drafts, alerts, dashboard, backtest | **Real**; tested end to end |
| Always-on operation | **Real**: `run_pipeline.py --loop 5` checks every 5 minutes and fetches each source when it can have changed; the dashboard refreshes itself when new data lands |
| Nemotron calls (decisions, Xtract, drafts, contract reading, Q&A) | **Real client**, tested against a mock OpenAI-compatible server; **not yet run on a real Nemotron model** |
| Shipments / Bills of Lading | **Demo**: 4 fictional shipments on real routes, plus a fictional sample contract (`samples/`) |

## 6. Design principles

1. **Evidence first.** No decision without a citation (`source_url` is mandatory in the
   output).
2. **Human in the loop.** The model recommends; people approve or override. The pipeline
   never overwrites human fields.
3. **Open and free inputs.** Public data, and an open model that can run on your own
   hardware.
4. **Contracts are first-class data.** The decision depends on clause text, not just on
   geography.
5. **Deterministic plumbing, probabilistic judgement.** Ingestion, matching, and validation
   are rules and SQL (repeatable, idempotent, testable). Only the final judgement uses the
   LLM, and its output is schema-checked.
6. **Auditability.** Every model call is stored with its full input and raw output.

## 7. Demo script (90 seconds)

1. **Map:** "Here are our shipments. Two routes are red: the Red Sea and Hormuz events are
   confirmed by dozens of publishers, and Hormuz traffic is down more than 90%."
2. **SHP-DEMO-001:** "Nemotron read this contract: force majeure covers war, and deviation is
   allowed. Waiting costs $10k, rerouting $30k, force majeure $0, and the notice is due in
   48 hours." Open the drafted notice → **Approve**.
3. **SHP-DEMO-002:** "Same kind of event, opposite advice: this contract excludes war and
   forbids deviation, so no route or force majeure is available; tell the customer now."
4. **Backtest:** "Would it have worked? Red Sea 2023: flagged 26 days ahead, and traffic
   then fell 41%."
5. **Ask Majeure:** "Which shipments go through Hormuz, and what does it cost us?"
6. **Upload** the sample contract: Nemotron turns it into a tracked shipment, which the Red
   Sea event immediately matches.

## 8. Roadmap

* **Richer delay estimates:** learn the expected delay per event type from PortWatch history
  (how long traffic stayed depressed after each past event) instead of a fixed table.
* **Proximity for every source:** extend GDACS-style distance linking to PortWatch `region`
  events and to news that names only a city or country.
* **Real B/L feeds:** EDI (IFTMIN/IFTSTA) and carrier APIs alongside PDF upload; live vessel
  positions (AIS) to know which chokepoint a ship will reach, and when.
* **Learning loop:** use `human_decision` overrides to evaluate prompts and Nemotron model
  sizes against the rules baseline.
* **Multi-tenant product:** authentication, per-customer shipments, email delivery of
  approved drafts.

## 9. Glossary

| Term | Meaning |
|---|---|
| **Bill of Lading (B/L)** | The document a carrier issues for cargo; it records shipper, consignee, vessel, voyage, and the ports of loading and discharge. |
| **POL / POD** | Port of loading / port of discharge. |
| **Transshipment port** | An intermediate port where cargo moves between vessels (e.g. Singapore). |
| **Chokepoint** | A narrow passage most trade must pass through (Suez Canal, Strait of Hormuz, Bab el-Mandeb, Malacca Strait, …). PortWatch tracks 28. |
| **Incoterm** | Standard trade terms (ICC Incoterms 2020) defining who bears cost and risk where, e.g. **FOB** (risk passes to the buyer at loading) or **CIF** (the seller pays freight and insurance to the destination). |
| **Force majeure** | A clause excusing a party from liability for events beyond its control. Its scope (war? strikes? weather?) and notice period decide whether it can be invoked. |
| **Deviation** | A vessel departing from the agreed route, for instance around the Cape of Good Hope instead of through Suez. Contracts may permit or forbid it. |
| **Demurrage / detention** | Daily charges when containers stay too long in the port (demurrage) or outside it (detention) beyond the **free time**. |
| **GDACS** | Global Disaster Alert and Coordination System (EC JRC and UN OCHA); the source of PortWatch's natural-hazard events and alert levels. |
| **GDELT** | Global Database of Events, Language, and Tone: a free, worldwide news index, published as 15-minute files. |
| **GKG** | GDELT's Global Knowledge Graph: one row per article with URL, headline, themes, and locations. |
| **Xtract Signal** | This project's normalised disruption record (see `docs/SCHEMAS.md` §2). |
| **Nemotron** | NVIDIA's family of open LLMs, used here as the decision model. |
