"""
sync_all_campaigns_home.py
===========================
Wrapper that syncs keyword performance and search query performance
for ALL campaigns in the Home Insurance Google Ads account.

Discovers campaigns from ad_group_performance_daily (no API call needed).
Includes:
  - ENABLED campaigns always
  - PAUSED campaigns only if they accrued spend in the last --refresh days

For each qualifying campaign, runs in sequence:
  1. sync_keyword_perf_by_campaign.py   (kpd)
  2. sync_search_queries_by_campaign.py (sqp)

RUN
───
    conda activate paid-search

    # Standard 90-day refresh (recommended weekly):
    python kw_db_scripts/sync_all_campaigns_home.py --refresh 90

    # Custom date range:
    python kw_db_scripts/sync_all_campaigns_home.py --start 2026-01-01 --end 2026-04-08

    # Dry run — show which campaigns would sync without making API calls:
    python kw_db_scripts/sync_all_campaigns_home.py --refresh 90 --dry-run

OUTPUT
──────
Prints a per-campaign summary table on completion showing rows written,
spend, and revenue for each campaign synced.
"""

import os
import sys
import time
import sqlite3
import argparse
import subprocess
from pathlib import Path
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────
DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
SCRIPTS_DIR      = Path(r"C:\Projects\google-ads-ops\kw_db_scripts")
PYTHON           = sys.executable   # uses the active conda env

GOOGLE_ACCOUNT_ID = "8886502147"
ACCOUNT_ARG       = "home"
TODAY             = date.today()


# ── DB ────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def discover_campaigns(conn, start_date: str, end_date: str) -> list[dict]:
    """
    Returns campaigns to sync, sourced from ad_group_performance_daily.

    Inclusion rules:
      - ENABLED campaigns: always included regardless of spend
      - PAUSED campaigns:  included only if cost_micros > 0 in the window
        (they had real spend and may have keyword/query data worth syncing)

    Status comes from the campaigns table if populated, otherwise
    inferred from campaign_name patterns (fallback).
    """
    rows = conn.execute("""
        SELECT
            agpd.campaign_id,
            agpd.campaign_name,
            COALESCE(c.status, 'UNKNOWN') AS status,
            SUM(agpd.cost_micros) / 1e6   AS spend,
            MIN(agpd.date)                AS min_date,
            MAX(agpd.date)                AS max_date
        FROM ad_group_performance_daily agpd
        LEFT JOIN campaigns c ON agpd.campaign_id = c.campaign_id
        WHERE agpd.account_id = ?
          AND agpd.device     = 'ALL'
          AND agpd.date BETWEEN ? AND ?
        GROUP BY agpd.campaign_id, agpd.campaign_name
        HAVING spend > 0
           OR  COALESCE(c.status, 'UNKNOWN') = 'ENABLED'
        ORDER BY spend DESC
    """, (GOOGLE_ACCOUNT_ID, start_date, end_date)).fetchall()

    campaigns = []
    for r in rows:
        status = r['status']

        # If campaigns table isn't populated, infer PAUSED from name
        if status == 'UNKNOWN':
            name = r['campaign_name'] or ''
            # Tier campaigns are generally ENABLED; state mobile campaigns PAUSED
            if 'MOBILE' in name.upper():
                status = 'PAUSED'
            else:
                status = 'ENABLED'

        # Include rule:
        # - ENABLED: always
        # - PAUSED: only if spend > 0 in the window (already filtered by HAVING)
        # - UNKNOWN / other: only if spend > 0
        include = (status == 'ENABLED') or (r['spend'] > 0)
        if not include:
            continue

        campaigns.append({
            'campaign_id':   r['campaign_id'],
            'campaign_name': r['campaign_name'],
            'status':        status,
            'spend':         r['spend'],
            'min_date':      r['min_date'],
            'max_date':      r['max_date'],
        })

    return campaigns


def verify_results(conn, campaign_id: str,
                   start_date: str, end_date: str) -> dict:
    """Post-sync verification counts from kpd and sqp."""
    kpd = conn.execute("""
        SELECT COUNT(DISTINCT instance_id) AS slots,
               COUNT(*)                   AS rows,
               SUM(cost_micros) / 1e6     AS spend,
               SUM(primary_conv_value)    AS revenue
        FROM keyword_performance_daily
        WHERE campaign_id = ?
          AND device      = 'ALL'
          AND report_date BETWEEN ? AND ?
    """, (campaign_id, start_date, end_date)).fetchone()

    sqp = conn.execute("""
        SELECT COUNT(DISTINCT query_id)   AS queries,
               COUNT(DISTINCT instance_id) AS slots,
               COUNT(*)                   AS rows,
               SUM(cost_micros) / 1e6     AS spend
        FROM search_query_performance
        WHERE campaign_id = ?
          AND device      = 'ALL'
          AND report_date BETWEEN ? AND ?
    """, (campaign_id, start_date, end_date)).fetchone()

    return {
        'kpd_slots':   kpd['slots']   or 0,
        'kpd_rows':    kpd['rows']    or 0,
        'kpd_spend':   kpd['spend']   or 0.0,
        'kpd_revenue': kpd['revenue'] or 0.0,
        'sqp_queries': sqp['queries'] or 0,
        'sqp_rows':    sqp['rows']    or 0,
        'sqp_spend':   sqp['spend']   or 0.0,
    }


def run_script(script_name: str, extra_args: list[str],
               campaign_name: str, dry_run: bool) -> bool:
    """
    Runs a kw_db_scripts script as a subprocess.
    Returns True on success, False on failure.
    Streams stdout live so you can see API progress.
    """
    script_path = SCRIPTS_DIR / script_name
    cmd = [PYTHON, str(script_path)] + extra_args

    if dry_run:
        print(f"    [DRY RUN] would run: {' '.join(str(a) for a in cmd)}")
        return True

    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=False,   # stream live to terminal
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"    ⚠ FAILED with exit code {e.returncode}")
        return False
    except FileNotFoundError:
        print(f"    ⚠ Script not found: {script_path}")
        return False


# ── Args ──────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Sync kpd + sqp for all Home Insurance campaigns."
    )
    p.add_argument(
        "--refresh", type=int, default=90, metavar="N",
        help="Days to pull for each campaign (default: 90)."
    )
    p.add_argument(
        "--start", default=None, metavar="YYYY-MM-DD",
        help="Override start date (ignores --refresh)."
    )
    p.add_argument(
        "--end", default=None, metavar="YYYY-MM-DD",
        help="Override end date (default: yesterday)."
    )
    p.add_argument(
        "--skip-sqp", action="store_true",
        help="Skip search query sync (only sync keyword performance)."
    )
    p.add_argument(
        "--skip-kpd", action="store_true",
        help="Skip keyword performance sync (only sync search queries)."
    )
    p.add_argument(
        "--campaign-id", default=None, metavar="ID",
        help="Only sync a single campaign ID (for targeted re-runs)."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show which campaigns would sync without making API calls."
    )
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────
def main():
    args = parse_args()

    end_date   = (date.fromisoformat(args.end)
                  if args.end else TODAY - timedelta(days=1))
    start_date = (date.fromisoformat(args.start)
                  if args.start else end_date - timedelta(days=args.refresh - 1))

    print("=" * 70)
    print("sync_all_campaigns_home.py")
    print(f"  Account  : Home Insurance ({GOOGLE_ACCOUNT_ID})")
    print(f"  Range    : {start_date} → {end_date} "
          f"({(end_date - start_date).days + 1} days)")
    print(f"  Dry run  : {args.dry_run}")
    print(f"  Skip kpd : {args.skip_kpd}")
    print(f"  Skip sqp : {args.skip_sqp}")
    print("=" * 70)

    conn = get_conn()

    # Discover campaigns
    print("\nDiscovering campaigns from agpd...")
    campaigns = discover_campaigns(conn, start_date.isoformat(),
                                   end_date.isoformat())

    # Filter to single campaign if requested
    if args.campaign_id:
        campaigns = [c for c in campaigns
                     if c['campaign_id'] == args.campaign_id]
        if not campaigns:
            print(f"⚠ Campaign ID {args.campaign_id} not found in agpd "
                  f"for this date range.")
            conn.close()
            return

    enabled = [c for c in campaigns if c['status'] == 'ENABLED']
    paused  = [c for c in campaigns if c['status'] != 'ENABLED']

    print(f"\n  {len(campaigns)} campaigns to sync:")
    print(f"  {len(enabled)} ENABLED  |  {len(paused)} PAUSED with spend")
    print()
    print(f"  {'#':<4} {'Status':<8} {'Spend':>10} {'Campaign'}")
    print(f"  {'─'*4} {'─'*8} {'─'*10} {'─'*50}")
    for i, c in enumerate(campaigns, 1):
        print(f"  {i:<4} {c['status']:<8} ${c['spend']:>9,.2f}  "
              f"{c['campaign_name']}")

    if args.dry_run:
        print("\n[DRY RUN] No API calls made.")
        conn.close()
        return

    # Sync loop
    results  = []
    total_t  = time.time()
    n        = len(campaigns)
    failures = []

    for i, camp in enumerate(campaigns, 1):
        cid   = camp['campaign_id']
        cname = camp['campaign_name']
        t0    = time.time()

        print(f"\n{'─'*70}")
        print(f"[{i}/{n}] {cname}  ({cid})  [{camp['status']}]"
              f"  ${camp['spend']:,.2f} spend")
        print(f"{'─'*70}")

        common_args = [
            "--account",     ACCOUNT_ARG,
            "--campaign-id", cid,
            "--start",       start_date.isoformat(),
            "--end",         end_date.isoformat(),
        ]

        kpd_ok = sqp_ok = True

        # kpd sync
        if not args.skip_kpd:
            print(f"\n  → sync_keyword_perf_by_campaign.py")
            kpd_ok = run_script(
                "sync_keyword_perf_by_campaign.py",
                common_args, cname, args.dry_run
            )

        # sqp sync
        if not args.skip_sqp:
            print(f"\n  → sync_search_queries_by_campaign.py")
            sqp_ok = run_script(
                "sync_search_queries_by_campaign.py",
                common_args, cname, args.dry_run
            )

        elapsed = time.time() - t0
        if not kpd_ok or not sqp_ok:
            failures.append(cname)

        # Post-sync verification
        v = verify_results(conn, cid,
                           start_date.isoformat(), end_date.isoformat())
        results.append({**camp, **v, 'elapsed': elapsed,
                        'ok': kpd_ok and sqp_ok})

        print(f"\n  ✓ Done in {elapsed:.0f}s  |  "
              f"kpd: {v['kpd_slots']} slots, ${v['kpd_spend']:,.2f} spend  |  "
              f"sqp: {v['sqp_queries']} queries")

    conn.close()

    # ── Final summary ─────────────────────────────────────────────
    total_elapsed = time.time() - total_t
    print(f"\n{'='*70}")
    print("SYNC COMPLETE")
    print(f"{'='*70}")
    print(f"  Total time     : {total_elapsed/60:.1f} min")
    print(f"  Campaigns synced: {len(results)}")
    print(f"  Failures       : {len(failures)}")
    if failures:
        for f in failures:
            print(f"    ⚠ {f}")

    print(f"\n  {'Campaign':<50} {'Status':<8} {'kpd $':>9} "
          f"{'kpd rev':>9} {'sqp q':>7} {'sqp $':>9} {'t':>5}")
    print(f"  {'─'*50} {'─'*8} {'─'*9} {'─'*9} {'─'*7} {'─'*9} {'─'*5}")
    for r in results:
        ok_str = "✓" if r['ok'] else "⚠ FAIL"
        print(f"  {r['campaign_name'][:49]:<50} "
              f"{r['status'][:7]:<8} "
              f"${r['kpd_spend']:>8,.2f} "
              f"${r['kpd_revenue']:>8,.2f} "
              f"{r['sqp_queries']:>7,} "
              f"${r['sqp_spend']:>8,.2f} "
              f"{r['elapsed']:>4.0f}s "
              f"{ok_str}")

    total_kpd_spend = sum(r['kpd_spend'] for r in results)
    total_sqp_spend = sum(r['sqp_spend'] for r in results)
    total_rev       = sum(r['kpd_revenue'] for r in results)
    total_queries   = sum(r['sqp_queries'] for r in results)
    print(f"\n  {'TOTAL':<50} {'':8} "
          f"${total_kpd_spend:>8,.2f} "
          f"${total_rev:>8,.2f} "
          f"{total_queries:>7,} "
          f"${total_sqp_spend:>8,.2f}")

    print(f"\nNext step:")
    print(f"  python analysis/analysis_tier_two_keywords.py "
          f"--end-date {end_date.isoformat()}")


if __name__ == "__main__":
    main()
