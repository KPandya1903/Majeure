# Handoff — continue the UI restyle in a new session

Paste the block below into a new chat. State as of this handoff:

* Pushed: GitHub `KPandya1903/Pixels`, branch `main`, commit `0515000` (pipeline, dashboard,
  Nemotron, docs all working).
* Uncommitted: `.streamlit/config.toml`, `src/theme.py`, `docs/UI_REDESIGN.md`, this file.
* Running in the background: `run_pipeline.py --loop 5` (log: `data/pipeline.log`) and the
  dashboard on http://localhost:8599.
* Local model: Ollama `portguard-nemotron` (16k context); `.env` already configured.
* Approved mockup: https://claude.ai/artifact/EyDNXmqH3nLukco1E8r1Hq

---

I'm working on PortGuard AI at /Users/enzo/Desktop/Pixels/portguard-ai
(GitHub: https://github.com/KPandya1903/Pixels, branch main, last commit 0515000).

READ FIRST: README.md, then docs/UI_REDESIGN.md (the design brief).
The README explains every file, the invariants, and which files must change together.

WHAT IT IS
Maritime disruption monitoring. It ingests IMF PortWatch, GDACS, 5 maritime RSS feeds,
GDELT raw files and Google News; clusters reports into events; matches events to shipments
by port/chokepoint; prices the options (wait / reroute / invoke force majeure) from each
Bill of Lading's contract terms; asks NVIDIA Nemotron for a cited recommendation; drafts the
force majeure notice and customer email; and shows it all on a Streamlit dashboard.

CURRENT STATE
- A pipeline loop runs every 5 minutes in the background (nohup run_pipeline.py --loop 5,
  logging to data/pipeline.log). The dashboard runs on http://localhost:8599.
  Stop with: pkill -f "run_pipeline.py --loop"; pkill -f "streamlit run src/dashboard.py"
- Nemotron runs locally via Ollama as model "portguard-nemotron" (16k context, built from
  ollama/Modelfile). .env is already set (thinking off + JSON schema on; measured best).
- Use the venv: .venv/bin/python and .venv/bin/streamlit

MY TASK FOR YOU: finish the UI restyle of src/dashboard.py
Design direction (already decided, see docs/UI_REDESIGN.md): the look of portcast.io crossed
with flexport.com. White cards on a light grey canvas #F4F5F7, charcoal text #2B333C, ONE blue
accent #0B5CD5, amber #B4661B only where a deadline is counting down, red/amber/green only on
status chips, light-blue map. No navy anywhere. Approved mockup of the three screens:
https://claude.ai/artifact/EyDNXmqH3nLukco1E8r1Hq

Already done (uncommitted): .streamlit/config.toml (light theme) and src/theme.py
(design tokens + full component CSS: cards, KPI tiles, chips, dense table, decision panel,
focus rings, 160ms transitions, reduced-motion). src/dashboard.py does NOT use them yet.

Please:
1. Import theme.py in src/dashboard.py, inject theme.CSS once, and restyle:
   - an alert strip built from the most severe active event, plus a brand header row
   - KPI row as HTML cards (not st.metric), including the force majeure countdown card
   - the risk map: light basemap (Carto Voyager or similar light-blue ocean, NOT greyscale),
     blue sailed route with a dotted remainder, red halos sized by report count, grey port dots
   - the fleet table as one dense HTML table: LOCODE path, contract ETA, PortGuard ETA,
     delay chip, status chip, recommendation
   - the decision panel: priced options as bars with the recommended one in blue, deadline
     banner, corroboration facts, evidence links, Approve/Override, draft popovers
2. Follow the ui-ux-pro-max skill's checklists: 4/8px spacing, 4.5:1 contrast, visible focus
   rings, 44px touch targets, status conveyed by word + colour (never colour alone), tabular
   figures for money, empty states.
3. Verify before telling me it works: run the dashboard headless and check with
   streamlit.testing.v1.AppTest that there are no exceptions and the tabs/buttons still work;
   then screenshot it in Chrome (Playwright driving channel="chrome" works on this Mac; no
   browser download needed) and look at the screenshots.
4. Don't commit or push unless I ask.

Keep the data pipeline untouched — this is a presentation-layer change only.
