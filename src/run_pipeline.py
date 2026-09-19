"""Run the whole PortGuard pipeline in order: ingest -> traffic -> database -> (Xtract) -> events ->
decisions -> drafts -> alerts. Once, or every N minutes with --loop.

Each step runs as its own process (same scripts as the README); a failing ingestion source is
reported but does not stop the run. The database step is required.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from common import DATA_DIR, setup_logging

SRC = Path(__file__).resolve().parent
log = logging.getLogger("portguard.pipeline")


def steps(args: argparse.Namespace) -> list[tuple[str, list[str], bool]]:
    """(label, command, required)."""
    ingest = [] if args.skip_ingest else [
        ("PortWatch", ["fetch_portwatch.py"], True), ("GDACS", ["fetch_gdacs.py"], False),
        ("Trade-press RSS", ["parse_rss.py"], False), ("GDELT", ["fetch_gdelt.py"], False),
        ("Google News", ["fetch_google_news.py"], False), ("PortWatch traffic", ["fetch_traffic.py"], False),
    ]
    db = DATA_DIR / "portguard.db"
    need_shipments = args.shipments or not db.exists() or not _has_shipments(db)
    decide = ["run_nemotron.py", "--engine", args.engine] + (["--dry-run"] if args.dry_run else [])
    return ingest + [
        ("Database", ["init_db.py"], True),
        *([("Shipments", ["load_shipments.py"] + (["--file", args.shipments] if args.shipments else []), True)] if need_shipments else []),
        *([("Xtract (Nemotron)", ["xtract.py"], False)] if args.xtract else []),
        ("Events", ["cluster_events.py"], True),
        ("Decisions", decide, False),
        *([] if args.dry_run else [("Drafts", ["draft_actions.py"], False), ("Alerts", ["alerts.py"], False)]),
    ]


def _has_shipments(db: Path) -> bool:
    try:
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def run_once(args: argparse.Namespace) -> int:
    results = []
    for label, cmd, required in steps(args):
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(SRC / cmd[0]), *cmd[1:]], capture_output=True, text=True)
        last = next((l.split(": ", 1)[-1] for l in reversed((proc.stdout + proc.stderr).strip().splitlines()) if l.strip()), "")
        results.append((label, proc.returncode, time.monotonic() - started, last))
        log.info("%-20s %s %5.1fs  %s", label, "ok  " if proc.returncode == 0 else "FAIL", results[-1][2], last[:110])
        if proc.returncode != 0 and required:
            log.error("%s is required; stopping this run", label)
            return 1
    failed = [r[0] for r in results if r[1] != 0]
    log.info("run finished: %d steps, %s", len(results), f"failed: {', '.join(failed)}" if failed else "all ok")
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=["nemotron", "rules"], default="nemotron", help="decision engine (default: nemotron)")
    parser.add_argument("--xtract", action="store_true", help="also run the Nemotron Xtract step on news signals")
    parser.add_argument("--shipments", help="load this shipments JSON first (default: the demo sample, if the DB has none)")
    parser.add_argument("--skip-ingest", action="store_true", help="reuse the data already downloaded")
    parser.add_argument("--dry-run", action="store_true", help="write payloads only; no decisions, drafts or alerts")
    parser.add_argument("--loop", type=float, metavar="MINUTES", help="repeat every N minutes (Ctrl+C to stop)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    while True:
        code = run_once(args)
        if not args.loop:
            return code
        log.info("next run in %g minutes", args.loop)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    sys.exit(main())
