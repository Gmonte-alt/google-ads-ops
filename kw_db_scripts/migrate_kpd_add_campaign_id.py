"""
migrate_kpd_add_campaign_id.py
================================
Adds campaign_id to keyword_performance_daily and backfills it
from keyword_instances.

WHAT IT DOES
────────────
1. ALTER TABLE — adds campaign_id TEXT column to kpd (instant)
2. CREATE INDEX — on (campaign_id, report_date, device) for fast
   campaign-filtered queries
3. BACKFILL — single UPDATE JOIN sets campaign_id from ki.campaign_id
   for all 1.5M existing rows (~30–60 seconds)
4. VERIFY — prints row counts and a sample to confirm correctness

SAFE TO RE-RUN
──────────────
If the column already exists the ALTER TABLE is skipped gracefully.
The UPDATE is idempotent.

RUN
───
    conda activate paid-search
    python kw_db_scripts/migrate_kpd_add_campaign_id.py

    # Preview counts without writing:
    python kw_db_scripts/migrate_kpd_add_campaign_id.py --dry-run
"""

import sqlite3
import argparse
import time
from pathlib import Path
from datetime import datetime

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    return conn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows and check structure without writing.")
    args = parser.parse_args()

    conn   = get_conn()
    cursor = conn.cursor()

    print("=" * 65)
    print("migrate_kpd_add_campaign_id.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Dry run  : {args.dry_run}")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 65)

    # ── Check current state ───────────────────────────────────────
    total_kpd = cursor.execute(
        "SELECT COUNT(*) FROM keyword_performance_daily"
    ).fetchone()[0]

    cols = [r[1] for r in cursor.execute(
        "PRAGMA table_info(keyword_performance_daily)"
    ).fetchall()]

    already_has_col = 'campaign_id' in cols
    already_has_idx = cursor.execute("""
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='index' AND name='idx_kpd_campaign_date'
    """).fetchone()[0] > 0

    print(f"\n  kpd rows             : {total_kpd:,}")
    print(f"  campaign_id column   : {'already exists' if already_has_col else 'missing — will add'}")
    print(f"  campaign_date index  : {'already exists' if already_has_idx else 'missing — will add'}")

    if already_has_col:
        filled = cursor.execute(
            "SELECT COUNT(*) FROM keyword_performance_daily "
            "WHERE campaign_id IS NOT NULL"
        ).fetchone()[0]
        print(f"  Rows with campaign_id: {filled:,} / {total_kpd:,}")

    if args.dry_run:
        print("\n  DRY RUN — no changes written.")
        conn.close()
        return

    # ── Step 1: Add column ────────────────────────────────────────
    if not already_has_col:
        print(f"\n[1] Adding campaign_id column...", end=" ", flush=True)
        t = time.time()
        cursor.execute(
            "ALTER TABLE keyword_performance_daily ADD COLUMN campaign_id TEXT"
        )
        conn.commit()
        print(f"done ({time.time()-t:.1f}s)")
    else:
        print(f"\n[1] campaign_id column already exists — skipping ALTER TABLE")

    # ── Step 2: Add index ─────────────────────────────────────────
    if not already_has_idx:
        print(f"\n[2] Creating index idx_kpd_campaign_date...", end=" ", flush=True)
        t = time.time()
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_kpd_campaign_date
            ON keyword_performance_daily(campaign_id, report_date, device)
        """)
        conn.commit()
        print(f"done ({time.time()-t:.1f}s)")
    else:
        print(f"\n[2] Index already exists — skipping")

    # ── Step 3: Backfill ──────────────────────────────────────────
    needs_fill = cursor.execute(
        "SELECT COUNT(*) FROM keyword_performance_daily "
        "WHERE campaign_id IS NULL"
    ).fetchone()[0]

    if needs_fill > 0:
        print(f"\n[3] Backfilling campaign_id for {needs_fill:,} rows...")
        print(f"    (reading campaign_id from keyword_instances via instance_id)")
        t = time.time()
        cursor.execute("""
            UPDATE keyword_performance_daily
            SET campaign_id = (
                SELECT ki.campaign_id
                FROM keyword_instances ki
                WHERE ki.id = keyword_performance_daily.instance_id
            )
            WHERE campaign_id IS NULL
        """)
        updated = cursor.rowcount
        conn.commit()
        elapsed = time.time() - t
        print(f"    Updated {updated:,} rows in {elapsed:.1f}s")
    else:
        print(f"\n[3] All rows already have campaign_id — skipping backfill")

    # ── Step 4: Verify ────────────────────────────────────────────
    print(f"\n[4] Verification:")

    filled = cursor.execute(
        "SELECT COUNT(*) FROM keyword_performance_daily "
        "WHERE campaign_id IS NOT NULL"
    ).fetchone()[0]
    null_ct = total_kpd - filled
    print(f"  Rows with campaign_id    : {filled:,} / {total_kpd:,}")
    if null_ct > 0:
        print(f"  ⚠ Rows still NULL        : {null_ct:,}")
        print(f"    (keywords not in keyword_instances — orphaned kpd rows)")

    # Row counts by campaign for home account
    print(f"\n  Home account kpd by campaign (top 10 by rows):")
    rows = cursor.execute("""
        SELECT
            kpd.campaign_id,
            ki.campaign_name,
            COUNT(*)                    AS rows,
            COUNT(DISTINCT kpd.instance_id) AS kw_slots,
            MIN(kpd.report_date)        AS min_date,
            MAX(kpd.report_date)        AS max_date,
            SUM(kpd.cost_micros)/1e6    AS spend
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        JOIN accounts a           ON ki.account_id = a.id
        WHERE a.platform_account_id = '8886502147'
          AND kpd.device = 'ALL'
          AND kpd.campaign_id IS NOT NULL
        GROUP BY kpd.campaign_id
        ORDER BY spend DESC
        LIMIT 10
    """).fetchall()

    print(f"  {'Campaign':<50} {'rows':>7} {'slots':>6} "
          f"{'min':>12} {'max':>12} {'spend':>12}")
    print(f"  {'─'*50} {'─'*7} {'─'*6} {'─'*12} {'─'*12} {'─'*12}")
    for r in rows:
        print(f"  {str(r['campaign_name'] or r['campaign_id'])[:49]:<50} "
              f"{r['rows']:>7,} {r['kw_slots']:>6,} "
              f"{str(r['min_date']):>12} {str(r['max_date']):>12} "
              f"${r['spend']:>11,.2f}")

    # Spot-check TIER TWO
    t2 = cursor.execute("""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT instance_id) AS slots,
               SUM(cost_micros)/1e6 AS spend
        FROM keyword_performance_daily
        WHERE campaign_id = '23496334752'
          AND device = 'ALL'
          AND report_date >= date('now', '-30 days')
    """).fetchone()
    print(f"\n  TIER TWO (23496334752) last 30d:")
    print(f"    rows={t2['rows']:,}  "
          f"slots={t2['slots']:,}  "
          f"spend=${t2['spend']:,.2f}")

    conn.close()
    print(f"\nDone. {datetime.now().strftime('%H:%M:%S')}")
    print("\nNext steps:")
    print("  1. Replace kw_db_scripts/03_sync_keyword_performance.py "
          "with the updated version")
    print("  2. python analysis/analysis_tier_two_keywords.py")

if __name__ == "__main__":
    main()
