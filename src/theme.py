"""Design tokens and CSS for the dashboard (docs/UI_REDESIGN.md).

White cards on a light grey canvas, charcoal text, one blue accent; amber only where a deadline
is running; red/amber/green only on status, always paired with a word (never colour alone).
Spacing follows a 4/8px rhythm; transitions are 160ms and respect prefers-reduced-motion.
"""
from __future__ import annotations

# Semantic tokens, also used by the map layers (as RGB) so Python and CSS stay in step.
CANVAS, CARD, LINE, LINE_SOFT = "#F4F5F7", "#FFFFFF", "#E1E5EA", "#ECEEF1"
TEXT, MUTED, FAINT = "#2B333C", "#6B7581", "#949CA6"
BLUE, BLUE_SOFT, BLUE_DARK = "#0B5CD5", "#E8F1FF", "#08409A"
AMBER, AMBER_SOFT = "#B4661B", "#FFF3E2"
RED, RED_SOFT = "#D62828", "#FDECEC"
GREEN, GREEN_SOFT = "#237A45", "#E9F7EF"

RGB = {"blue": [11, 92, 213], "red": [214, 40, 40], "amber": [180, 102, 27],
       "green": [35, 122, 69], "grey": [107, 117, 129], "white": [255, 255, 255]}
RISK_HEX = {"critical": RED, "high": AMBER, "medium": AMBER, "low": GREEN}
RISK_TONE = {"critical": "risk", "high": "warn", "medium": "warn", "low": "ok"}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Plus+Jakarta+Sans:wght@500;600;700&family=Public+Sans:wght@400;500;600&display=swap');

:root {{
  --pg-canvas: {CANVAS}; --pg-card: {CARD}; --pg-line: {LINE}; --pg-line-soft: {LINE_SOFT};
  --pg-text: {TEXT}; --pg-muted: {MUTED}; --pg-faint: {FAINT};
  --pg-blue: {BLUE}; --pg-blue-soft: {BLUE_SOFT}; --pg-amber: {AMBER}; --pg-amber-soft: {AMBER_SOFT};
  --pg-red: {RED}; --pg-red-soft: {RED_SOFT}; --pg-green: {GREEN}; --pg-green-soft: {GREEN_SOFT};
  --pg-radius: 10px; --pg-s2: 8px; --pg-s3: 12px; --pg-s4: 16px; --pg-s5: 24px;
}}

/* Streamlit chrome: keep the toolbar (it holds Rerun/Settings) but drop menu, footer and padding. */
#MainMenu, footer {{ visibility: hidden; }}
/* top padding clears Streamlit's fixed header (~60px) — without it, the first row of content
   (now the alert strip / brand header) renders invisibly underneath the toolbar. */
.block-container {{ padding: 4.5rem 1.6rem 2rem; max-width: 1560px; }}
html, body, [class*="st-"], .stMarkdown, button, input, textarea {{ font-family: 'Public Sans', system-ui, sans-serif; }}
h1, h2, h3, h4 {{ font-family: 'Plus Jakarta Sans', system-ui, sans-serif; letter-spacing: -0.4px; color: var(--pg-text); }}
/* Streamlit's chevrons/toolbar glyphs are icon-font ligatures ("keyboard_double_arrow_..."); the
   broad font-family reset above would otherwise render that text literally instead of a glyph. */
[data-testid="stIconMaterial"] {{ font-family: 'Material Symbols Rounded' !important; }}
section[data-testid="stSidebar"] {{ background: var(--pg-card); border-right: 1px solid var(--pg-line); }}
section[data-testid="stSidebar"] .block-container {{ padding-top: 1rem; }}

/* Buttons: one primary blue, everything else quiet. 44px tall for comfortable targets. */
.stButton > button {{
  border-radius: 8px; border: 1px solid var(--pg-line); background: var(--pg-card); color: var(--pg-text);
  font-weight: 500; font-size: 0.86rem; padding: 0.55rem 0.95rem; min-height: 44px;
  transition: background 160ms ease-out, border-color 160ms ease-out, box-shadow 160ms ease-out;
}}
.stButton > button:hover {{ background: #F7F9FC; border-color: #C9D2DC; color: var(--pg-text); }}
.stButton > button:focus-visible {{ outline: 3px solid rgba(11,92,213,0.35); outline-offset: 2px; }}
.stButton > button[kind="primary"] {{ background: var(--pg-blue); border-color: var(--pg-blue); color: #FFFFFF; }}
.stButton > button[kind="primary"]:hover {{ background: var(--pg-blue-dark, {BLUE_DARK}); border-color: {BLUE_DARK}; color: #FFFFFF; }}

/* Tabs: quiet labels, blue underline on the active one. */
.stTabs [data-baseweb="tab-list"] {{ gap: 1.4rem; border-bottom: 1px solid var(--pg-line); }}
.stTabs [data-baseweb="tab"] {{ height: 44px; padding: 0 2px; color: var(--pg-muted); font-size: 0.92rem; font-weight: 500; }}
.stTabs [aria-selected="true"] {{ color: var(--pg-text); font-weight: 600; }}
.stTabs [data-baseweb="tab-highlight"] {{ background: var(--pg-blue); height: 2px; }}

/* Cards, KPIs, panels */
.pg-card {{ background: var(--pg-card); border: 1px solid var(--pg-line); border-radius: var(--pg-radius);
  padding: var(--pg-s4) 18px; box-shadow: 0 1px 2px rgba(43,51,60,0.05); }}
.pg-kpis {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--pg-s4); margin: 2px 0 6px; }}
.pg-kpi-label {{ font-size: 0.8rem; color: var(--pg-muted); font-weight: 500; }}
.pg-kpi-value {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.85rem; font-weight: 700;
  letter-spacing: -0.8px; margin-top: 4px; font-variant-numeric: tabular-nums; }}
.pg-kpi-note {{ font-size: 0.8rem; color: var(--pg-muted); margin-top: 2px; }}
.pg-kpi-due {{ background: #FFFDF8; border-color: #F0CFA0; }}
.pg-kpi-due .pg-kpi-label, .pg-kpi-due .pg-kpi-value {{ color: var(--pg-amber); }}

/* Alert strip + brand row */
.pg-strip {{ display: flex; align-items: center; gap: 10px; background: #F1F3F5; border: 1px solid var(--pg-line);
  border-radius: 8px; padding: 9px 14px; font-size: 0.85rem; color: #4A545F; margin-bottom: 10px; }}
.pg-strip strong {{ color: var(--pg-text); font-weight: 700; }}
.pg-brand {{ display: flex; align-items: baseline; gap: 8px; }}
.pg-brand-name {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.3rem; font-weight: 700; letter-spacing: -0.5px; }}
.pg-brand-tag {{ font-size: 0.68rem; font-weight: 700; color: var(--pg-blue); background: var(--pg-blue-soft);
  border-radius: 5px; padding: 2px 6px; letter-spacing: 0.5px; }}
.pg-sub {{ font-size: 0.85rem; color: var(--pg-muted); }}

/* Status chips: colour + word, never colour alone */
.pg-chip {{ display: inline-flex; align-items: center; gap: 5px; border-radius: 6px; font-size: 0.72rem;
  font-weight: 600; padding: 3px 8px; white-space: nowrap; font-variant-numeric: tabular-nums; }}
.pg-chip.risk {{ background: var(--pg-red-soft); color: var(--pg-red); }}
.pg-chip.warn {{ background: var(--pg-amber-soft); color: var(--pg-amber); }}
.pg-chip.ok {{ background: var(--pg-green-soft); color: var(--pg-green); }}
.pg-chip.info {{ background: var(--pg-blue-soft); color: var(--pg-blue); }}
.pg-chip.mute {{ background: #EDEFF2; color: var(--pg-muted); }}

/* Dense fleet table */
.pg-table {{ width: 100%; border-collapse: collapse; background: var(--pg-card); border: 1px solid var(--pg-line);
  border-radius: var(--pg-radius); overflow: hidden; font-size: 0.86rem; }}
.pg-table th {{ text-align: left; font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.7px;
  color: var(--pg-faint); background: #F7F8FA; padding: 10px 14px; border-bottom: 1px solid var(--pg-line); }}
.pg-table td {{ padding: 12px 14px; border-bottom: 1px solid var(--pg-line-soft); vertical-align: middle; color: var(--pg-text); }}
.pg-table tr:last-child td {{ border-bottom: 0; }}
.pg-table tr.selected td {{ background: #F7F9FD; }}
.pg-table tr.selected td:first-child {{ box-shadow: inset 3px 0 0 var(--pg-blue); }}
.pg-mono {{ font-family: 'IBM Plex Mono', monospace; font-size: 0.74rem; color: var(--pg-faint); }}
.pg-num {{ font-variant-numeric: tabular-nums; }}

/* Decision panel */
.pg-eyebrow {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase; color: var(--pg-muted); }}
.pg-h {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 1.15rem; font-weight: 700; margin: 2px 0 0; }}
.pg-option {{ margin-top: var(--pg-s3); }}
.pg-option-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 0.88rem; }}
.pg-bar {{ height: 7px; background: #E7EAEE; border-radius: 4px; margin-top: 6px; overflow: hidden; }}
.pg-bar > span {{ display: block; height: 7px; border-radius: 4px; background: var(--pg-faint);
  transition: width 220ms ease-out; }}
.pg-bar.is-risk > span {{ background: var(--pg-red); }}
.pg-bar.is-best > span {{ background: var(--pg-blue); }}
.pg-option-note {{ font-size: 0.78rem; color: var(--pg-muted); margin-top: 4px; }}
.pg-best {{ background: #F5F8FE; border: 1px solid #BBD4F7; border-radius: 8px; padding: 12px 13px; margin-top: var(--pg-s3); }}
.pg-banner {{ display: flex; align-items: center; gap: 10px; background: var(--pg-amber-soft); border: 1px solid #F0CFA0;
  border-radius: 8px; padding: 11px 14px; font-size: 0.87rem; color: #7A4A12; }}
.pg-banner.late {{ background: var(--pg-red-soft); border-color: #F3C2C2; color: #8E1B1B; }}
.pg-facts {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--pg-s3); margin-top: var(--pg-s3); }}
.pg-fact-label {{ font-size: 0.72rem; color: var(--pg-faint); }}
.pg-fact-value {{ font-size: 0.86rem; font-weight: 600; margin-top: 2px; }}
.pg-legend {{ display: flex; gap: var(--pg-s4); align-items: center; font-size: 0.75rem; color: var(--pg-muted); margin-top: 6px; }}
.pg-legend span.dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; }}
.pg-empty {{ text-align: center; color: var(--pg-muted); font-size: 0.9rem; padding: 26px 12px; }}

a {{ color: var(--pg-blue); }}
a:hover {{ color: {BLUE_DARK}; }}
[data-testid="stMetricValue"] {{ font-family: 'Plus Jakarta Sans', sans-serif; font-variant-numeric: tabular-nums; }}
div[data-testid="stDataFrame"] {{ border: 1px solid var(--pg-line); border-radius: var(--pg-radius); overflow: hidden; }}

@media (prefers-reduced-motion: reduce) {{
  * {{ transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }}
}}
</style>
"""


def chip(text: str, tone: str = "mute") -> str:
    """A status chip; the word carries the meaning, the colour only reinforces it."""
    return f'<span class="pg-chip {tone}">{text}</span>'
