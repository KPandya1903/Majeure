# PortGuard AI — The Main Idea

> **In one sentence:** PortGuard AI turns free, real-time maritime disruption data into
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

PortGuard AI closes all three gaps in one automated loop:

| Gap | What PortGuard does | Where in the code |
|---|---|---|
| Detection | Continuously ingests **free, open sources** (IMF PortWatch, GDACS, five maritime trade-press feeds, GDELT, Google News) into one normalised signal format. | `fetch_*.py`, `parse_rss.py` → `init_db.py` |
| Relevance | **Matches** each signal to shipments whose port of loading, port of discharge, transshipment port, or route chokepoint it touches, and drops stale signals. | `run_nemotron.py` (`MATCH_SQL`, `find_pairs`) |
| Contract | Sends signal and contract terms to **Nemotron**, which weighs the disruption against the clauses and returns a risk score, a recommended action, and a cited rationale. | `run_nemotron.py` (`NemotronPayload` → `NemotronDecision`) |

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
| **Structured** | **GDACS** live hazard alerts (EC JRC / UN OCHA) | Real-time, with coordinates. PortGuard links each hazard to the ports within a per-type radius (a cyclone within 400 km, an earthquake within 150 km). | Natural hazards only; radius-based exposure is approximate. |
| **Unstructured** | **Maritime trade press**: gCaptain, Maritime Executive, Seatrade Maritime, Hellenic Shipping News, Splash247 | All maritime, written by specialists; excerpts give context. | More than half is general industry news (filtered out at matching). |
| **Unstructured** | **GDELT** raw news firehose (every 15 minutes, worldwide) and **Google News** search | Fast and broad. Catches attacks, strikes, and closures within minutes to hours. | Noisy and duplicated, so PortGuard keeps only headlines about shipping *and* a disruption. Headline-only. |

Using both tracks gives **coverage and confirmation**: news raises the early alarm, and
PortWatch and GDACS provide the authoritative anchor. They reinforce each other when they
agree. On 2026-09-19, for example, the PortWatch HORMUZ-26 event was joined by dozens of
same-day tanker-attack reports from Google News, GDELT, and the trade press.

### 3.2 Understand: the Xtract Signal

Every item from every source is converted into one **Xtract Signal**, which is the project's
name for a normalised, citable disruption record:

* `title`, `source_url` (the citation), `published_at`
* `port_location`: where it happens (a port, a chokepoint, or a region), with IDs and
  coordinates
* `event_type`: one of 10 categories (security threat, labour strike, chokepoint disruption,
  port closure, severe weather, natural disaster, vessel incident, congestion, regulatory,
  other)
* `severity`, `raw_text`, and source-specific `metadata`

PortWatch events are mapped from their structured fields. News is pre-tagged with rules: a
keyword classifier, plus a **gazetteer** that recognises the 2,065 PortWatch port names and
31 chokepoints and sea areas in text. GDACS hazards are linked to ports by distance. The rules deliberately favour recall; the
planned "Xtract" LLM extraction step would confirm or correct them.

### 3.3 Match: only what touches *your* route

A shipment's route is described by its B/L: port of loading, port of discharge,
transshipment ports, and the chokepoints it passes (for example Shanghai → Singapore →
Rotterdam via Malacca, Bab el-Mandeb, and Suez). Because signals and shipments use the same
IDs (PortWatch `portid`s and `chokepoint:<slug>`), matching is an exact join, not guesswork.
Stale signals are filtered out: a PortWatch event must be ongoing or recent, and news must
fall inside the look-back window. Each pair keeps its strongest reason (a disruption at the
discharge port outranks one at a chokepoint).

### 3.4 Decide: Nemotron reads the contract

For each (signal, shipment) pair, PortGuard builds a validated **Nemotron payload**:

* the full signal (what, where, when, severity, and source);
* the Bill of Lading (route, ETD/ETA, cargo, value);
* the **contract terms**: Incoterm, force majeure clause text, deviation permission, notice
  period, demurrage, detention, free time, late-delivery penalty, governing law;
* the **match context**: why the pair was selected, which port or chokepoint, and days until
  ETA;
* the task: the allowed actions `monitor`, `notify_customer`, `reroute`, `expedite`,
  `hold_cargo`, `invoke_force_majeure`.

Nemotron is told to use only the facts in the payload, pick one allowed action, and cite the
source. It returns a risk score (0–1), a risk level, the action, a 2–4 sentence rationale,
and citations. The parser accepts the messy output typical of reasoning models (thinking
blocks, code fences, percentages) and rejects actions that weren't offered.

**Why Nemotron:** NVIDIA's Nemotron models are open and can be self-hosted (NIM, vLLM,
Ollama). Commercially sensitive contract terms and cargo values therefore never have to leave
the operator's infrastructure. They're reached through the standard OpenAI-compatible API, so
the same code also works with NVIDIA's hosted endpoint.

### 3.5 Audit: evidence and human control

`decision_logs` stores for each decision: the model, score, level, action, rationale,
citations, the **complete input payload**, the **raw model output**, latency, and a
`human_decision` field (`pending` → `approved` / `overridden`) with notes. Any recommendation
can be traced back to the exact evidence and contract text the model saw. Human verdicts can
later be used to evaluate or tune the model.

## 4. Worked example (real data, 2026-09-19)

**Signal (PortWatch, structured):** *RED SEA TENSIONS*, "Trade disruptions in the Red Sea
(near Bab el-Mandeb Strait) due to attacks on commercial ships." Alert level RED
(→ severity `high`), started 2023-12-16, `to_date = null` (ongoing), and affects the
chokepoints Bab el-Mandeb Strait, Suez Canal, and Cape of Good Hope. Citation:
`https://portwatch.imf.org/pages/573013af3b6545deaeb50ed1cbaf9444`.

**Shipment (demo):** SHP-DEMO-001, 12 × 40' HC of consumer electronics worth $480,000.
Shanghai → Singapore → Rotterdam via `chokepoint:malacca-strait`,
`chokepoint:bab-el-mandeb-strait`, `chokepoint:suez-canal`; ETA 2026-10-20. Terms: CIF, force
majeure **covers war, blockade, hostilities, and canal closure** with 48 h written notice,
deviation **permitted**, $1,000/day late penalty.

**Match:** `route_chokepoint` via `chokepoint:bab-el-mandeb-strait`, about 30 days before ETA.

**What the model is asked to weigh:** an ongoing, high-severity security event on the route.
Force majeure applies to this kind of event but requires notice within 48 hours. Deviation
(for example via the Cape of Good Hope, itself listed as affected) is contractually allowed.
There is a month of lead time, and a daily penalty if the cargo is late. Candidate actions
include `reroute`, `notify_customer`, or `invoke_force_majeure`.

**Contrast:** SHP-DEMO-002 (Jebel Ali → Nhava Sheva through Hormuz) meets HORMUZ-26 plus
recent tanker-attack reports. Its FOB contract **excludes war and security incidents** from
force majeure, **forbids deviation**, and carries a $2,500/day penalty. The same kind of
disruption should therefore lead to different advice (for instance, don't count on force
majeure; notify the customer or hold cargo). Showing this contrast is the core of the demo.
What Nemotron actually recommends depends on the model and prompt, and is recorded in
`decision_logs`.

## 5. What is real and what is demo

| Part | Status |
|---|---|
| PortWatch, GDACS, trade-press RSS, GDELT, and Google News ingestion | **Real, live data** from public endpoints; no keys |
| Signal normalisation, dedupe, relevance filtering, event classification, location tagging, proximity linking | **Real**, rule-based, tested on live data |
| Database, matching, payload building, Nemotron client, parsing, audit log | **Real**; tested end to end against a mock Nemotron server |
| Shipments / Bills of Lading | **Demo**: 4 fictional shipments on real routes (`samples/shipments.sample.json`) |
| "Xtract" LLM extraction of signals | **Not built yet**; rule-based pre-tagging stands in for it |
| UI / notifications | **Not built**; results are in SQLite (`decision_logs`) |

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

## 7. Roadmap

* **Xtract LLM extraction:** use Nemotron to confirm or correct `event_type`,
  `port_location`, and `severity` for news signals, and to read full article text.
* **Proximity for every source:** GDACS hazards are already linked to ports by distance.
  Extend this to PortWatch `region` events and to news whose location is only a city or
  country.
* **Impact quantification:** add PortWatch *Daily_Chokepoints_Data* and *Daily_Ports_Data*
  (transit and port-call counts) to measure how much traffic actually dropped.
* **Real B/L ingestion:** parse B/L PDFs and EDI messages into the `Shipment` schema.
* **Event clustering:** collapse the many reports of one event (on 2026-09-19 SHP-DEMO-002
  matched ~70 Hormuz stories) into a single signal before calling the model. Today
  `--max-per-shipment` (default 5) caps the calls per run instead.
* **Delivery:** a dashboard over `decision_logs`, and alerts (email or Slack) when a
  high-risk decision needs approval before a force majeure notice deadline.
* **Learning loop:** use `human_decision` overrides to evaluate prompts and models.

## 8. Glossary

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
