"""
fix_kpd_delete_and_resync.py
=============================
Deletes keyword_performance_daily rows for a Google account and
confirms the delete before re-sync instructions are printed.

WHY THIS EXISTS
───────────────
03_sync_keyword_performance.py had a bug where it resolved instance_id
using only criterion_id, without ad_group_id. This caused performance
rows to be written to the wrong instance when the same criterion_id
appears across multiple ad groups (e.g. 102 EXACT instances sharing
criterion_id=19000971 across state-specific campaigns).

The fix to 03_sync_keyword_performance.py corrects the lookup going
forward, but existing rows written under the old logic must be deleted
and re-synced so the data is clean.

SAFE TO RUN
───────────
Only deletes rows for the specified account (default: home, account_id=1).
Microsoft keyword performance (account_id=2) is unaffected.
Google keyword structure (keyword_instances) is unaffected.
All other tables are unaffected.

USAGE
─────
    conda activate paid-search

    # Preview — shows what would be deleted without deleting:
    python kw_db_scripts/fix_kpd_delete_and_resync.py --dry-run

    # Delete rows for home account (default):
    python kw_db_scripts/fix_kpd_delete_and_resync.py

    # Delete rows for a different account:
    python kw_db_scripts/fix_kpd_delete_and_resync.py --account medicare

AFTER RUNNING
─────────────
Re-sync keyword performance using the fixed script:
    python kw_db_scripts/03_sync_keyword_performance.py --account home --backfill

Then verify:
    python analysis/diag_fire_insurance_kw_v2.py --end-date 2026-04-14 --days 30
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

ACCOUNT_MAP = {
    "home":       1,
    "medicare":   28,
    "auto":       23,
    "term_life":  29,
    "auto_quote": 26,
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def main():
    p = argparse.ArgumentParser(
        description="Delete keyword_performance_daily rows for a Google account "
                    "so they can be re-synced with the corrected instance_id lookup."
    )
    p.add_argument("--account", default="home",
                   choices=list(ACCOUNT_MAP.keys()),
                   help="Account vertical to clear (default: home).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show counts only — do not delete.")
    args = p.parse_args()

    account_id   = ACCOUNT_MAP[args.account]
    conn         = get_conn()

    print("=" * 65)
    print("fix_kpd_delete_and_resync.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Account  : {args.account}  (account_id = {account_id})")
    print(f"  Mode     : {'DRY RUN — no changes will be made' if args.dry_run else 'LIVE DELETE'}")
    print("=" * 65)

    # ── Pre-delete counts ──────────────────────────────────────────────────
    counts = conn.execute("""
        SELECT
            COUNT(*)                     AS total_rows,
            MIN(kpd.report_date)         AS min_date,
            MAX(kpd.report_date)         AS max_date,
            SUM(kpd.cost_micros) / 1e6   AS total_spend,
            SUM(kpd.primary_conv_count)  AS total_convs,
            SUM(kpd.primary_conv_value)  AS total_revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?
    """, (account_id,)).fetchone()

    total = counts["total_rows"] or 0
    print(f"\n  Rows to delete     : {total:,}")
    print(f"  Date range         : {counts['min_date']} → {counts['max_date']}")
    print(f"  Spend (sanity)     : ${counts['total_spend']:,.2f}" if counts['total_spend'] else "  Spend: $0")
    print(f"  Conversions        : {counts['total_convs']:,.1f}" if counts['total_convs'] else "  Conversions: 0")
    print(f"  Revenue            : ${counts['total_revenue']:,.2f}" if counts['total_revenue'] else "  Revenue: $0")

    # ── Per-platform breakdown ─────────────────────────────────────────────
    by_platform = conn.execute("""
        SELECT ki.platform, COUNT(*) AS n
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?
        GROUP BY ki.platform
    """, (account_id,)).fetchall()

    if by_platform:
        print(f"\n  Breakdown by platform:")
        for r in by_platform:
            print(f"    {str(r['platform'] or 'unknown'):<15} {r['n']:>10,} rows")

    if total == 0:
        print("\n  Nothing to delete — table is already empty for this account.")
        conn.close()
        return

    if args.dry_run:
        print(f"\n  DRY RUN — {total:,} rows would be deleted.")
        print("  Re-run without --dry-run to execute.")
        conn.close()
        return

    # ── Confirmation prompt ────────────────────────────────────────────────
    print(f"\n  ⚠  About to permanently delete {total:,} rows from keyword_performance_daily.")
    print("     These will be restored by running:")
    print(f"     python kw_db_scripts/03_sync_keyword_performance.py --account {args.account} --backfill")
    print()
    confirm = input("  Type 'yes' to confirm delete: ").strip().lower()

    if confirm != "yes":
        print("  Cancelled — no rows deleted.")
        conn.close()
        return

    # ── Delete ─────────────────────────────────────────────────────────────
    print(f"\n  Deleting...")
    deleted = conn.execute("""
        DELETE FROM keyword_performance_daily
        WHERE instance_id IN (
            SELECT id FROM keyword_instances WHERE account_id = ?
        )
    """, (account_id,)).rowcount
    conn.commit()

    print(f"  ✓ Deleted {deleted:,} rows.")

    # ── Post-delete verification ───────────────────────────────────────────
    remaining = conn.execute("""
        SELECT COUNT(*) AS n
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?
    """, (account_id,)).fetchone()["n"]

    if remaining == 0:
        print(f"  ✓ Verified: 0 rows remain for account_id={account_id}.")
    else:
        print(f"  ⚠  {remaining:,} rows still remain — delete may be incomplete.")

    conn.close()

    print()
    print("=" * 65)
    print("NEXT STEPS")
    print("=" * 65)
    print()
    print("  1. Confirm 03_sync_keyword_performance.py has been replaced")
    print("     with the fixed version (composite key lookup).")
    print()
    print("  2. Re-sync keyword performance:")
    print(f"     python kw_db_scripts/03_sync_keyword_performance.py --account {args.account} --backfill")
    print()
    print("  3. Verify fire insurance aggregation is correct:")
    print("     python analysis/diag_fire_insurance_kw_v2.py --end-date 2026-04-14 --days 30")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
