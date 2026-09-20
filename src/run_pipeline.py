"""Run the whole PortGuard pipeline in order: ingest -> database -> (Xtract) -> events -> decisions ->
drafts -> alerts. Once, or every N minutes with --loop.

In a loop, each source is fetched only when it can have changed (SCHEDULE_MIN below): checking every
2 minutes does not re-download GDELT or hammer Google News every 2 minutes. The downstream steps run
whenever at least one source was fetched. Each step runs as its own process (same scripts as the
README); a failing source is reported but does not stop the run. The database step is required.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import store
from common import DATA_DIR, setup_logging

SRC = Path(__file__).resolve().parent
STATE_JSON = DATA_DIR / "pipeline_state.json"
# Minimum minutes between fetches of each source in --loop mode: how often it can actually change.
SCHEDULE_MIN = {
    "PortWatch": 60,           # curated events: added irregularly
    "GDACS": 5,                # live hazard alerts
    "Trade-press RSS": 5,      # new articles a few times a day per feed
    "Official warnings": 10,   # NOAA/JTWC advisories reissue ~6-hourly; NGA warnings change rarely
    "GDELT": 15,               # one new GKG file every 15 minutes (fetched incrementally)
    "Google News": 10,         # unofficial feed: stay polite
    "PortWatch traffic": 360,  # daily data, published ~5-7 days late
}
log = logging.getLogger("portguard.pipeline")


def ingest_steps(loop: bool) -> list[tuple[str, list[str], bool]]:
    """(label, command, required)."""
    return [
        ("PortWatch", ["fetch_portwatch.py"], True), ("GDACS", ["fetch_gdacs.py"], False),
        ("Trade-press RSS", ["parse_rss.py"], False),
        ("Official warnings", ["fetch_official.py"], False),
        ("GDELT", ["fetch_gdelt.py"] + (["--incremental"] if loop else []), False),
        ("Google News", ["fetch_google_news.py"], False), ("PortWatch traffic", ["fetch_traffic.py"], False),
    ]


def downstream_steps(args: argparse.Namespace) -> list[tuple[str, list[str], bool]]:
    db = DATA_DIR / "portguard.db"
    need_shipments = args.shipments or not store.database_ready(db) or not _has_shipments(db)
    decide = ["run_nemotron.py", "--engine", args.engine] + (["--dry-run"] if args.dry_run else [])
    return [
        ("Database", ["init_db.py"], True),
        *([("Shipments", ["load_shipments.py"] + (["--file", args.shipments] if args.shipments else []), True)] if need_shipments else []),
        *([("Xtract (Nemotron)", ["xtract.py"], False)] if args.xtract else []),
        ("Events", ["cluster_events.py"], True),
        ("Decisions", decide, False),
        *([] if args.dry_run else [("Drafts", ["draft_actions.py"], False), ("Alerts", ["alerts.py"], False)]),
    ]


def _has_shipments(db: Path) -> bool:
    try:
        with store.session(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] > 0
    except Exception:  # missing file, missing table, unreachable Postgres -- all mean "no"
        return False


def _load_state() -> dict:
    try:
        return json.loads(STATE_JSON.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, failed: Optional[list[str]] = None) -> None:
    """Persist fetch times to BOTH the JSON file and the database.

    The JSON file is what the scheduler reads next tick. The database copy is what the API serves to
    the sidebar, and it exists because the JSON file is gitignored and Render's free tier has no
    persistent disk -- so on the deployed instance the file never existed and every source's
    "Fetched" column rendered as a dash regardless of whether the pipeline had run.
    """
    STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATE_JSON.write_text(json.dumps(state))
    db = DATA_DIR / "portguard.db"
    if not store.is_postgres() and not db.exists():
        return  # first ever run: init_db.py creates the table a few steps later
    bad = set(failed or ())
    try:
        with store.session(db) as conn:
            conn.executemany(
                "INSERT INTO pipeline_state (source_group, last_fetched_at, ok) VALUES (?, ?, ?) "
                "ON CONFLICT (source_group) DO UPDATE SET last_fetched_at = excluded.last_fetched_at, "
                "ok = excluded.ok",
                [(group, datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  0 if group in bad else 1) for group, ts in state.items()])
    except Exception as exc:  # never fail a run over bookkeeping, on either backend
        log.warning("could not record pipeline state in the database: %s", exc)


def _run(label: str, cmd: list[str]) -> int:
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(SRC / cmd[0]), *cmd[1:]], capture_output=True, text=True)
    last = next((l.split(": ", 1)[-1] for l in reversed((proc.stdout + proc.stderr).strip().splitlines()) if l.strip()), "")
    log.info("%-20s %s %5.1fs  %s", label, "ok  " if proc.returncode == 0 else "FAIL", time.monotonic() - started, last[:110])
    return proc.returncode


def run_once(args: argparse.Namespace, scheduled: bool) -> int:
    """One pass. scheduled=True (loop mode): fetch only sources that are due; skip downstream if none were."""
    state, now = _load_state(), time.time()
    failed, fetched = [], 0
    for label, cmd, required in ([] if args.skip_ingest else ingest_steps(scheduled)):
        if scheduled and now - state.get(label, 0) < SCHEDULE_MIN[label] * 60:
            continue
        code = _run(label, cmd)
        state[label] = now  # attempts count too, so a failing source is retried at its interval, not every tick
        fetched += 1
        if code:
            failed.append(label)
            if required and not (DATA_DIR / "portwatch_ports.csv").exists():
                log.error("%s is required on the first run; stopping this run", label)
                _save_state(state, failed)
                return 1
    _save_state(state, failed)
    if scheduled and not fetched and not args.skip_ingest:
        log.info("nothing due this tick (next: %s)", ", ".join(
            f"{k} in {max(0, SCHEDULE_MIN[k] * 60 - (now - state.get(k, 0))) / 60:.0f}m" for k in SCHEDULE_MIN))
        return 0
    for label, cmd, required in downstream_steps(args):
        if _run(label, cmd):
            failed.append(label)
            if required:
                log.error("%s is required; stopping this run", label)
                return 1
    log.info("run finished: %s", f"failed: {', '.join(failed)}" if failed else "all ok")
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=["nemotron", "rules"], default="nemotron", help="decision engine (default: nemotron)")
    parser.add_argument("--xtract", action="store_true", help="also run the Nemotron Xtract step on news signals")
    parser.add_argument("--shipments", help="load this shipments JSON first (default: the demo sample, if the DB has none)")
    parser.add_argument("--skip-ingest", action="store_true", help="reuse the data already downloaded")
    parser.add_argument("--dry-run", action="store_true", help="write payloads only; no decisions, drafts or alerts")
    parser.add_argument("--loop", type=float, metavar="MINUTES",
                        help="check every N minutes (e.g. 2); each source is fetched only when due (SCHEDULE_MIN)")
    parser.add_argument("--max-ticks", type=int, help=argparse.SUPPRESS)  # for tests
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if not args.loop:
        return run_once(args, scheduled=False)
    log.info("looping every %g min; source intervals (min): %s", args.loop, SCHEDULE_MIN)
    ticks = 0
    while True:
        started = time.monotonic()
        log.info("tick at %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        try:
            run_once(args, scheduled=True)
        except Exception as exc:  # a loop must survive anything a single run throws
            log.error("run crashed: %s", exc)
        ticks += 1
        if args.max_ticks and ticks >= args.max_ticks:
            return 0
        time.sleep(max(0.0, args.loop * 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
