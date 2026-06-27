"""
refresh_clear_window.py
=======================
Microsoft data refresh — DELETE-only stage.

Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Clears the trailing N-day window from Microsoft's performance tables
so the loaders can re-load that window with fresh data. Does NOT run
the loaders. Does NOT touch structure tables.

WHEN TO USE THIS SCRIPT

  - You're doing a manual refresh and want to inspect / control the
    delete step independently of the loaders.
  - You've pulled fresh CSVs and want to confirm what will be cleared
    before committing.
  - The orchestrator failed mid-run and you need to retry just the
    delete portion.

Most of the time you'll use refresh_orchestrator.py instead, which
calls this functionality and then runs the loaders.

WHAT IT TOUCHES

  Performance tables (DELETE WHERE date >= cutoff, account_id = 2):
    - campaign_performance_daily
    - campaign_performance_network_daily
    - keyword_performance_daily

  Structure tables NEVER touched:
    - campaigns, ad_groups, keyword_instances
    - These UPSERT in place via the loaders. Their first_seen_date
      semantics are preserved across refreshes.

WHAT THE WINDOW MEANS

  The cutoff for each table is computed as:
      cutoff = MAX(date in that table for MS account) - (window - 1) days

  This anchors on the data itself, not on date.today(). If the DB
  has data through 2026-04-20, a 7-day window deletes 2026-04-14
  through 2026-04-20 inclusive. The next loader run repopulates
  whichever of those dates the new CSV provides.

  Different tables can have different MAX(date) (campaign vs keyword
  data lag differently). Each table's cutoff is computed independently.

USAGE

    conda activate paid-search

    # Inspect what would be deleted, no changes
    python campaign_db_scripts/refresh_clear_window.py --dry-run

    # Default: clear trailing 7 days
    python campaign_db_scripts/refresh_clear_window.py

    # Custom window
    python campaign_db_scripts/refresh_clear_window.py --window 14

    # Specific tables only (advanced)
    python campaign_db_scripts/refresh_clear_window.py \\
        --tables campaign_performance_daily,campaign_performance_network_daily

SAFETY

  - Read-only inspection runs first regardless of mode.
  - Confirmation prompt before any DELETE in interactive mode.
  - Uses a single transaction across all tables; rolled back on error.
  - The Microsoft account is hardcoded (account_id = 2). This script
    will NOT touch Google data or any other platform.
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Allow running from either campaign_db_scripts/ or repo root
sys.path.insert(0, str(Path(__file__).parent))
from refresh_helper import (
    DEFAULT_WINDOW_DAYS, MS_ACCOUNT_ID, PERFORMANCE_TABLES,
    compute_cutoff, delete_window, fmt_window_report,
    get_connection, get_max_date, inspect_all, inspect_window,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear the trailing N-day window from Microsoft performance tables."
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"Trailing days to clear (default: {DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Inspect and report without deleting.",
    )
    parser.add_argument(
        "--tables",
        help="Comma-separated subset of performance tables. Default: all three.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt. Use with care.",
    )
    args = parser.parse_args()

    if args.window < 1:
        log.error("--window must be >= 1")
        return 2

    # Resolve table set
    if args.tables:
        requested = {t.strip() for t in args.tables.split(",") if t.strip()}
        unknown = requested - set(PERFORMANCE_TABLES.keys())
        if unknown:
            log.error(f"Unknown tables: {sorted(unknown)}")
            log.error(f"Valid tables: {sorted(PERFORMANCE_TABLES.keys())}")
            return 2
        tables = {t: PERFORMANCE_TABLES[t] for t in requested}
    else:
        tables = dict(PERFORMANCE_TABLES)

    log.info(f"Microsoft account_id : {MS_ACCOUNT_ID}")
    log.info(f"Window               : trailing {args.window} day(s)")
    log.info(f"Mode                 : {'DRY RUN' if args.dry_run else 'LIVE'}")
    log.info(f"Tables               : {', '.join(tables.keys())}")

    conn = get_connection()

    # Inspection pass — always run, dry or live
    results = {}
    for table, date_col in tables.items():
        max_d = get_max_date(conn, table, date_col)
        if max_d is None:
            results[table] = {
                "max_date_in_db": None, "cutoff": None,
                "row_count": 0, "spend": 0.0,
                "date_col": date_col,
            }
            continue
        cutoff = compute_cutoff(max_d, args.window)
        info = inspect_window(conn, table, date_col, cutoff)
        info["max_date_in_db"] = max_d
        info["cutoff"]   = cutoff
        info["date_col"] = date_col
        results[table] = info

    print(fmt_window_report(results, args.window))

    total_rows = sum(r["row_count"] for r in results.values())
    total_spend = sum(r["spend"] for r in results.values())
    log.info(f"  Total rows that {'would be' if args.dry_run else 'will be'} deleted: {total_rows:,}")
    log.info(f"  Total spend in window: ${total_spend:,.2f}")

    if args.dry_run:
        log.info("")
        log.info("DRY RUN — nothing deleted.")
        conn.close()
        return 0

    if total_rows == 0:
        log.info("")
        log.info("No rows in window — nothing to delete.")
        conn.close()
        return 0

    # Confirmation
    if not args.yes:
        log.info("")
        prompt = (
            f"  Proceed with deletion of {total_rows:,} rows "
            f"across {len(results)} table(s)? [y/N] "
        )
        try:
            resp = input(prompt).strip().lower()
        except EOFError:
            resp = ""
        if resp != "y":
            log.info("  Cancelled.")
            conn.close()
            return 1

    # Single transaction across all tables
    log.info("")
    log.info("── Deleting ─────────────────────────────────────────────────")
    try:
        for table, info in results.items():
            if info["row_count"] == 0:
                log.info(f"  {table:<38} 0 rows (skipped, nothing to delete)")
                continue
            n = delete_window(
                conn, table, info["date_col"], info["cutoff"], dry_run=False,
            )
            log.info(f"  {table:<38} deleted {n:,} rows  (>= {info['cutoff']})")
        conn.commit()
        log.info("")
        log.info("Committed.")
    except sqlite3.Error as e:
        conn.rollback()
        log.error(f"Database error: {e}")
        log.error("Transaction rolled back. Database is unchanged.")
        conn.close()
        return 1

    conn.close()
    log.info("")
    log.info("Next step:")
    log.info("  Run the loaders to repopulate the window with fresh CSVs:")
    log.info("    python campaign_db_scripts/08_load_microsoft_campaigns.py ...")
    log.info("    python campaign_db_scripts/09_load_microsoft_campaign_performance.py --file ...")
    log.info("    python kw_db_scripts/04_load_microsoft_csv.py --file ...")
    log.info("")
    log.info("Or use refresh_orchestrator.py to delete + load in one command.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
