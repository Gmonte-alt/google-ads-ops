"""
refresh_orchestrator_google.py
==============================
Daily orchestrator for the Google Ads data refresh.
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Implements the SOP rolling-window pattern:
  1. Find MAX(date) per performance table for the home account
  2. Delete trailing N days (default 14) from that MAX
  3. Compute end_date = yesterday
  4. Compute start_date = MAX(date) - (N-1) days
     (so the deletion window is repopulated AND any gaps to yesterday close)
  5. Run the existing sync scripts with --start / --end set to that range

DESIGN PRINCIPLES
  - Anchored on the latest date already in the database, NOT today's calendar
    date. Google's offline conversion uploads settle for ~3 days, so re-pulling
    14 days lets late conversions and revenue corrections flow through.
  - Each refresh is delete-then-load within the window. Data outside the window
    is bit-for-bit untouched.
  - If MAX(date) per table differs (e.g. ad_group_performance_daily lags
    campaign_performance_daily by a day), the orchestrator uses the MIN of
    those MAXes as the anchor, so all tables converge to the same end-of-window.
  - Skip-zero-activity is delegated to sync_all_campaigns_home.py, which
    already filters to ENABLED + recently-spending campaigns.

DEFAULT BEHAVIOR (no flags)
  Refreshes the home account at all three campaign grains plus keyword and
  search-query performance, using the rolling 14-day window.

USAGE
    conda activate paid-search

    # Daily refresh (recommended Mon-Fri morning):
    python campaign_db_scripts/refresh_orchestrator_google.py

    # Preview the deletion + sync window without writing:
    python campaign_db_scripts/refresh_orchestrator_google.py --dry-run

    # Custom window (e.g., Monday morning catching up Fri-Sun):
    python campaign_db_scripts/refresh_orchestrator_google.py --window 17

    # Skip the structure refresh (campaigns/ad_groups -- usually safe to skip
    # daily; only needed when you've added/renamed campaigns):
    python campaign_db_scripts/refresh_orchestrator_google.py --skip-structure

    # Account other than home (single-account refresh):
    python campaign_db_scripts/refresh_orchestrator_google.py \\
        --account medicare --window 14

    # Skip search query sync (kpd only -- faster):
    python campaign_db_scripts/refresh_orchestrator_google.py --skip-sqp

    # Override the start date explicitly (skips MAX(date) detection):
    python campaign_db_scripts/refresh_orchestrator_google.py \\
        --start 2026-04-01 --end 2026-04-14

EXIT CODES
    0  All sync scripts completed successfully
    1  At least one sync script failed (orchestrator continues past failures
       so partial progress is preserved; see end-of-run summary)
    2  Pre-flight failure (missing args, no data in DB to anchor on, etc.)

NOTES
    - This orchestrator does NOT call 04_parse_campaign_names.py or the
      tagging scripts. Those run weekly (per the SOP) and are not on the
      daily critical path.
    - Structure sync (02_sync_campaigns_adgroups.py) is account-wide -- there's
      no --account flag on it currently. Skipping it via --skip-structure is
      the right move for daily runs unless campaigns have changed.
"""

import os
import sys
import sqlite3
import argparse
import subprocess
from pathlib import Path
from datetime import date, timedelta, datetime


DB_PATH         = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CAMP_SCRIPTS    = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts")
KW_SCRIPTS      = Path(r"C:\Projects\google-ads-ops\kw_db_scripts")
PYTHON          = sys.executable

# Account label -> (platform_account_id, accounts.id)
ACCOUNT_LOOKUP = {
    "home":       "8886502147",
    "auto":       "3863660890",
    "auto_quote": "1006729211",
    "medicare":   "5310820901",
    "term_life":  "1671155176",
}

# Campaign-grain performance tables -- account-scoped, deleted by date range
CAMP_PERF_TABLES = [
    "campaign_performance_daily",
    "campaign_performance_hourly",
    "campaign_performance_geo_daily",
    "ad_group_performance_daily",
]

# Keyword-grain performance tables -- joined to keyword_instances for account scope
KW_PERF_TABLES = [
    ("keyword_performance_daily", "report_date"),
    ("search_query_performance",  "report_date"),
]

DEFAULT_WINDOW = 14


# DB helpers -----------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_account_id(conn, account_label: str) -> int:
    pid = ACCOUNT_LOOKUP.get(account_label)
    if not pid:
        raise SystemExit(f"Unknown account label: {account_label}")
    row = conn.execute("""
        SELECT id FROM accounts
        WHERE platform_account_id = ? AND platform = 'google'
    """, (pid,)).fetchone()
    if not row:
        raise SystemExit(f"No accounts row for {account_label} ({pid})")
    return row["id"]


def get_max_dates(conn, account_id: int) -> dict:
    """Returns {table_name: max_date_string_or_None} for all six perf tables."""
    out = {}
    for tbl in CAMP_PERF_TABLES:
        row = conn.execute(
            f"SELECT MAX(date) AS d FROM {tbl} WHERE account_id = ?",
            (account_id,)
        ).fetchone()
        out[tbl] = row["d"]
    for tbl, date_col in KW_PERF_TABLES:
        row = conn.execute(f"""
            SELECT MAX({date_col}) AS d
            FROM {tbl} t
            JOIN keyword_instances ki ON ki.id = t.instance_id
            WHERE ki.account_id = ?
        """, (account_id,)).fetchone()
        out[tbl] = row["d"]
    return out


def compute_window(max_dates: dict, window_days: int):
    """
    Returns (start_date, end_date, anchor_date).
    Anchor = MIN of the per-table MAX(date) values, so all tables converge
    on the same end-of-window. End = yesterday.
    Start = anchor - (window_days - 1).
    """
    valid = [d for d in max_dates.values() if d]
    if not valid:
        return None, None, None
    anchor = min(valid)
    anchor_d = date.fromisoformat(anchor)
    start = anchor_d - timedelta(days=window_days - 1)
    end = date.today() - timedelta(days=1)
    if end < start:
        end = start
    return start.isoformat(), end.isoformat(), anchor


def preview_delete(conn, account_id: int, start: str, end: str):
    """Print what the delete would touch -- row counts + spend per table."""
    print()
    print(f"  Window to delete: {start} -> {end}")
    print(f"  {'Table':<40} {'rows':>10} {'spend':>14}")
    print(f"  {'-'*40} {'-'*10} {'-'*14}")

    for tbl in CAMP_PERF_TABLES:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(cost_micros),0)/1e6 AS spend
            FROM {tbl}
            WHERE account_id = ? AND date BETWEEN ? AND ?
        """, (account_id, start, end)).fetchone()
        print(f"  {tbl:<40} {row['n']:>10,} ${row['spend']:>13,.2f}")

    for tbl, date_col in KW_PERF_TABLES:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(t.cost_micros),0)/1e6 AS spend
            FROM {tbl} t
            JOIN keyword_instances ki ON ki.id = t.instance_id
            WHERE ki.account_id = ? AND t.{date_col} BETWEEN ? AND ?
        """, (account_id, start, end)).fetchone()
        print(f"  {tbl:<40} {row['n']:>10,} ${row['spend']:>13,.2f}")


def delete_window(conn, account_id: int, start: str, end: str) -> dict:
    """Delete rows in [start, end] for the given account across all six
    perf tables. Single transaction. Returns per-table delete counts."""
    counts = {}
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for tbl in CAMP_PERF_TABLES:
            cur.execute(f"""
                DELETE FROM {tbl}
                WHERE account_id = ? AND date BETWEEN ? AND ?
            """, (account_id, start, end))
            counts[tbl] = cur.rowcount
        for tbl, date_col in KW_PERF_TABLES:
            cur.execute(f"""
                DELETE FROM {tbl}
                WHERE instance_id IN (SELECT id FROM keyword_instances WHERE account_id = ?)
                  AND {date_col} BETWEEN ? AND ?
            """, (account_id, start, end))
            counts[tbl] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


# Subprocess runner ----------------------------------------------------------

def run_script(label: str, cmd: list, dry_run: bool) -> bool:
    print()
    print(f"  >>> {label}")
    print(f"      {' '.join(str(c) for c in cmd)}")
    if dry_run:
        print(f"      [DRY RUN -- not executed]")
        return True
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)
        return True
    except subprocess.CalledProcessError as e:
        print(f"      FAILED with exit code {e.returncode}")
        return False
    except FileNotFoundError as e:
        print(f"      Script not found: {e}")
        return False


# Main -----------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Daily Google Ads data refresh orchestrator.")
    p.add_argument("--account", default="home",
                   choices=list(ACCOUNT_LOOKUP.keys()),
                   help="Account to refresh (default: home).")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                   help=f"Days to delete-and-reload from MAX(date) anchor "
                        f"(default {DEFAULT_WINDOW}).")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help="Override start date. Skips MAX(date) detection.")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="Override end date (default: yesterday).")
    p.add_argument("--skip-structure", action="store_true",
                   help="Skip campaigns/ad_groups structure sync. "
                        "Recommended for daily runs unless campaigns changed.")
    p.add_argument("--skip-camp-perf", action="store_true",
                   help="Skip campaign performance grains (daily/hourly/geo + adgroup).")
    p.add_argument("--skip-kw", action="store_true",
                   help="Skip keyword + search query sync.")
    p.add_argument("--skip-sqp", action="store_true",
                   help="Skip search query sync only.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show window + commands without writing.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt before deleting.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 70)
    print("refresh_orchestrator_google.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Account  : {args.account}")
    print(f"  Window   : {args.window} days")
    print(f"  Dry run  : {args.dry_run}")
    print(f"  Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    conn = get_conn()
    account_id = get_account_id(conn, args.account)

    # Resolve window
    if args.start:
        start = args.start
        end = args.end or (date.today() - timedelta(days=1)).isoformat()
        anchor = "(explicit override)"
        print(f"\n  Using explicit date range: {start} -> {end}")
    else:
        max_dates = get_max_dates(conn, account_id)
        print(f"\n  MAX(date) per table (account_id = {account_id}):")
        for tbl, d in max_dates.items():
            print(f"    {tbl:<40} {d or '(empty)'}")
        start, end, anchor = compute_window(max_dates, args.window)
        if start is None:
            print(f"\n  No data found for {args.account}. Run an initial backfill instead:")
            print(f"    python kw_db_scripts/03_sync_keyword_performance.py "
                  f"--account {args.account} --backfill")
            return 2
        print(f"\n  Anchor (MIN of MAX dates) : {anchor}")
        print(f"  Computed start            : {start}")
        print(f"  Computed end (yesterday)  : {end}")

    # Preview
    preview_delete(conn, account_id, start, end)

    # Confirm before deleting
    if not args.dry_run and not args.yes:
        print()
        ans = input(f"  Proceed with delete + re-sync? (y/N): ").strip().lower()
        if ans != "y":
            print(f"  Cancelled.")
            return 0

    # Delete
    if not args.dry_run:
        print(f"\n  Deleting window...")
        counts = delete_window(conn, account_id, start, end)
        for tbl, n in counts.items():
            print(f"    {tbl:<40} -{n:,} rows")
    else:
        print(f"\n  [DRY RUN -- no rows deleted]")

    conn.close()

    # Run the loaders
    failures = []

    # 1. Structure (account-wide; can't scope to one account currently)
    if not args.skip_structure:
        ok = run_script(
            "campaigns + ad_groups (all accounts)",
            [PYTHON, str(CAMP_SCRIPTS / "02_sync_campaigns_adgroups.py")],
            args.dry_run
        )
        if not ok:
            failures.append("02_sync_campaigns_adgroups.py")

    # 2. Campaign performance (three grains)
    if not args.skip_camp_perf:
        # daily
        ok = run_script(
            "campaign_performance_daily",
            [PYTHON, str(CAMP_SCRIPTS / "03_sync_campaign_performance.py"),
             "--daily-only", "--daily-start", start, "--daily-end", end],
            args.dry_run
        )
        if not ok:
            failures.append("03_sync_campaign_performance.py --daily-only")

        # hourly
        ok = run_script(
            "campaign_performance_hourly",
            [PYTHON, str(CAMP_SCRIPTS / "03_sync_campaign_performance.py"),
             "--hourly-only", "--hourly-start", start, "--hourly-end", end],
            args.dry_run
        )
        if not ok:
            failures.append("03_sync_campaign_performance.py --hourly-only")

        # geo
        ok = run_script(
            "campaign_performance_geo_daily",
            [PYTHON, str(CAMP_SCRIPTS / "03_sync_campaign_performance.py"),
             "--geo-only", "--geo-start", start, "--geo-end", end],
            args.dry_run
        )
        if not ok:
            failures.append("03_sync_campaign_performance.py --geo-only")

        # ad_group_performance_daily
        ok = run_script(
            "ad_group_performance_daily",
            [PYTHON, str(CAMP_SCRIPTS / "05_add_adgroup_performance.py"),
             "--account", args.account, "--start", start, "--end", end],
            args.dry_run
        )
        if not ok:
            failures.append("05_add_adgroup_performance.py")

    # 3. Keyword + search query
    if not args.skip_kw:
        if args.account == "home":
            # Use the per-campaign wrapper for home -- skips zero-activity campaigns
            extra = ["--start", start, "--end", end]
            if args.skip_sqp:
                extra.append("--skip-sqp")
            ok = run_script(
                "sync_all_campaigns_home (kpd + sqp, per-campaign)",
                [PYTHON, str(KW_SCRIPTS / "sync_all_campaigns_home.py")] + extra,
                args.dry_run
            )
            if not ok:
                failures.append("sync_all_campaigns_home.py")
        else:
            # Other accounts: account-wide direct calls
            ok = run_script(
                f"keyword_performance_daily ({args.account})",
                [PYTHON, str(KW_SCRIPTS / "03_sync_keyword_performance.py"),
                 "--account", args.account, "--refresh", str(args.window)],
                args.dry_run
            )
            if not ok:
                failures.append(f"03_sync_keyword_performance.py --account {args.account}")

            if not args.skip_sqp:
                ok = run_script(
                    f"search_query_performance ({args.account})",
                    [PYTHON, str(KW_SCRIPTS / "05_sync_search_queries.py"),
                     "--account", args.account, "--start", start, "--end", end],
                    args.dry_run
                )
                if not ok:
                    failures.append(f"05_sync_search_queries.py --account {args.account}")

    # Summary
    print()
    print("=" * 70)
    print("REFRESH COMPLETE" if not failures else "REFRESH COMPLETE WITH FAILURES")
    print("=" * 70)
    print(f"  Window  : {start} -> {end}")
    print(f"  Account : {args.account}")
    if failures:
        print(f"  Failures ({len(failures)}):")
        for f in failures:
            print(f"    - {f}")
        print()
        print(f"  Re-run individual failed scripts after fixing the issue.")
        print(f"  Then run 00_precheck_google_load.py to verify recovery.")
        return 1

    print(f"\n  Verify with:")
    print(f"    python campaign_db_scripts/00_precheck_google_load.py --skip-api-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
