"""
sync_home_active_campaigns.py
==============================
Syncs keyword performance and search query performance for the 7 currently
enabled Home Insurance campaigns, from 2025-01-01 to yesterday.

Calls sync_keyword_perf_by_campaign.py and sync_search_queries_by_campaign.py
for each campaign in sequence as subprocesses, streaming output live.

CAMPAIGNS SYNCED
────────────────
  HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE   23385188622
  HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE        23394808297
  HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE      23394808300
  HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE     23428389081
  HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE     23496334752
  HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE   23530860942
  HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE    23586637678

USAGE
─────
    conda activate paid-search

    # Sync kpd + sqp for all 7 campaigns:
    python kw_db_scripts/sync_home_active_campaigns.py

    # Sync kpd only (skip sqp):
    python kw_db_scripts/sync_home_active_campaigns.py --skip-sqp

    # Sync sqp only (skip kpd):
    python kw_db_scripts/sync_home_active_campaigns.py --skip-kpd

    # Override date range:
    python kw_db_scripts/sync_home_active_campaigns.py --start 2025-06-01
"""

import sys
import time
import sqlite3
import argparse
import subprocess
from pathlib import Path
from datetime import date, timedelta

DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
SCRIPTS_DIR = Path(r"C:\Projects\google-ads-ops\kw_db_scripts")
PYTHON      = sys.executable

CAMPAIGNS = [
    ("23385188622", "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"),
    ("23394808297", "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE"),
    ("23394808300", "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE"),
    ("23428389081", "HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE"),
    ("23496334752", "HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE"),
    ("23530860942", "HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE"),
    ("23586637678", "HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE"),
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Sync kpd + sqp for all 7 enabled Home Insurance campaigns."
    )
    p.add_argument("--start", default="2025-01-01", metavar="YYYY-MM-DD",
                   help="Start date (default: 2025-01-01).")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="End date (default: yesterday).")
    p.add_argument("--skip-kpd", action="store_true",
                   help="Skip keyword performance sync.")
    p.add_argument("--skip-sqp", action="store_true",
                   help="Skip search query performance sync.")
    return p.parse_args()


def run_script(script_name: str, extra_args: list[str]) -> tuple[bool, float]:
    """Runs a kw_db_scripts script as a subprocess, streaming output live."""
    script_path = SCRIPTS_DIR / script_name
    cmd = [PYTHON, str(script_path)] + extra_args
    t0 = time.time()
    try:
        result = subprocess.run(cmd, check=True, text=True, capture_output=False)
        return True, time.time() - t0
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ FAILED with exit code {e.returncode}")
        return False, time.time() - t0
    except FileNotFoundError:
        print(f"  ⚠ Script not found: {script_path}")
        return False, time.time() - t0


def print_diagnostic(start: str, end: str):
    """Post-sync verification: spend, revenue, rows per campaign."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    campaign_ids = [cid for cid, _ in CAMPAIGNS]
    placeholders = ",".join("?" * len(campaign_ids))

    print(f"\n{'='*70}")
    print("DIAGNOSTIC — keyword_performance_daily (device='ALL')")
    print(f"{'='*70}")
    rows = conn.execute(f"""
        SELECT
            kpd.campaign_id,
            c.campaign_name,
            COUNT(DISTINCT kpd.instance_id)  AS kw_slots,
            COUNT(*)                          AS rows,
            MIN(kpd.report_date)              AS min_date,
            MAX(kpd.report_date)              AS max_date,
            SUM(kpd.cost_micros) / 1e6        AS spend,
            SUM(kpd.primary_conv_value)       AS revenue
        FROM keyword_performance_daily kpd
        LEFT JOIN campaigns c ON c.campaign_id = kpd.campaign_id
        WHERE kpd.campaign_id IN ({placeholders})
          AND kpd.device     = 'ALL'
        GROUP BY kpd.campaign_id
        ORDER BY spend DESC
    """, campaign_ids).fetchall()

    if not rows:
        print("  (no kpd data yet)")
    else:
        print(f"  {'Campaign':<50} {'Slots':>6} {'Rows':>7} "
              f"{'Spend':>12} {'Revenue':>10} {'Date range'}")
        print(f"  {'─'*50} {'─'*6} {'─'*7} "
              f"{'─'*12} {'─'*10} {'─'*24}")
        for r in rows:
            name = (r['campaign_name'] or r['campaign_id'])[:49]
            print(
                f"  {name:<50} "
                f"{r['kw_slots']:>6,} "
                f"{r['rows']:>7,} "
                f"${r['spend']:>11,.2f} "
                f"${r['revenue']:>9,.2f} "
                f"{r['min_date']} → {r['max_date']}"
            )

    print(f"\n{'='*70}")
    print("DIAGNOSTIC — search_query_performance (device='ALL')")
    print(f"{'='*70}")
    rows = conn.execute(f"""
        SELECT
            sqp.campaign_id,
            c.campaign_name,
            COUNT(DISTINCT sqp.query_id)     AS unique_queries,
            COUNT(DISTINCT sqp.instance_id)  AS kw_slots,
            COUNT(*)                          AS rows,
            MIN(sqp.report_date)              AS min_date,
            MAX(sqp.report_date)              AS max_date,
            SUM(sqp.cost_micros) / 1e6        AS spend,
            SUM(sqp.primary_conv_value)       AS revenue
        FROM search_query_performance sqp
        LEFT JOIN campaigns c ON c.campaign_id = sqp.campaign_id
        WHERE sqp.campaign_id IN ({placeholders})
          AND sqp.device      = 'ALL'
        GROUP BY sqp.campaign_id
        ORDER BY spend DESC
    """, campaign_ids).fetchall()

    if not rows:
        print("  (no sqp data yet)")
    else:
        print(f"  {'Campaign':<45} {'Queries':>8} {'Slots':>6} "
              f"{'Spend':>12} {'Revenue':>10}")
        print(f"  {'─'*45} {'─'*8} {'─'*6} {'─'*12} {'─'*10}")
        for r in rows:
            name = (r['campaign_name'] or r['campaign_id'])[:44]
            print(
                f"  {name:<45} "
                f"{r['unique_queries']:>8,} "
                f"{r['kw_slots']:>6,} "
                f"${r['spend']:>11,.2f} "
                f"${r['revenue']:>9,.2f}"
            )

    conn.close()


def main():
    args = parse_args()

    end_date = (date.fromisoformat(args.end)
                if args.end
                else date.today() - timedelta(days=1))
    start_date = date.fromisoformat(args.start)

    print("=" * 70)
    print("sync_home_active_campaigns.py")
    print(f"  Start    : {start_date}")
    print(f"  End      : {end_date}")
    print(f"  Skip kpd : {args.skip_kpd}")
    print(f"  Skip sqp : {args.skip_sqp}")
    print(f"  Campaigns: {len(CAMPAIGNS)}")
    print("=" * 70)

    total_t  = time.time()
    failures = []
    results  = []

    for i, (campaign_id, campaign_name) in enumerate(CAMPAIGNS, 1):
        print(f"\n{'─'*70}")
        print(f"[{i}/{len(CAMPAIGNS)}] {campaign_name}")
        print(f"  Campaign ID: {campaign_id}")
        print(f"{'─'*70}")

        common = [
            "--account",     "home",
            "--campaign-id", campaign_id,
            "--start",       start_date.isoformat(),
            "--end",         end_date.isoformat(),
        ]

        kpd_ok = sqp_ok = True
        kpd_t  = sqp_t  = 0.0

        if not args.skip_kpd:
            print(f"\n  → sync_keyword_perf_by_campaign.py")
            kpd_ok, kpd_t = run_script(
                "sync_keyword_perf_by_campaign.py", common
            )

        if not args.skip_sqp:
            print(f"\n  → sync_search_queries_by_campaign.py")
            sqp_ok, sqp_t = run_script(
                "sync_search_queries_by_campaign.py", common
            )

        ok = kpd_ok and sqp_ok
        results.append({
            "campaign_id":   campaign_id,
            "campaign_name": campaign_name,
            "kpd_ok":        kpd_ok,
            "sqp_ok":        sqp_ok,
            "elapsed":       kpd_t + sqp_t,
        })
        if not ok:
            failures.append(campaign_name)

    total_elapsed = time.time() - total_t

    print(f"\n{'='*70}")
    print("ALL CAMPAIGNS COMPLETE")
    print(f"{'='*70}")
    print(f"  Total time : {total_elapsed/60:.1f} min")
    print(f"  Failures   : {len(failures)}")
    if failures:
        for f in failures:
            print(f"    ⚠ {f}")

    print(f"\n  {'Campaign':<50} {'kpd':>5} {'sqp':>5} {'Time':>6}")
    print(f"  {'─'*50} {'─'*5} {'─'*5} {'─'*6}")
    for r in results:
        kpd_str = "✓" if r["kpd_ok"] else "✗"
        sqp_str = "✓" if r["sqp_ok"] else "✗"
        print(
            f"  {r['campaign_name'][:49]:<50} "
            f"{kpd_str:>5} {sqp_str:>5} "
            f"{r['elapsed']:>5.0f}s"
        )

    # Final diagnostic
    print_diagnostic(start_date.isoformat(), end_date.isoformat())

    print(f"\nNext step:")
    print(f"  python check_sync.py")


if __name__ == "__main__":
    main()
