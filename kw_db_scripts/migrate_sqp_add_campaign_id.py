"""
migrate_sqp_add_campaign_id.py
================================
Adds campaign_id to search_query_performance and backfills from
keyword_instances (same pattern as migrate_kpd_add_campaign_id.py).

RUN
───
    python kw_db_scripts/migrate_sqp_add_campaign_id.py --dry-run
    python kw_db_scripts/migrate_sqp_add_campaign_id.py
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn   = get_conn()
    cursor = conn.cursor()

    print("=" * 65)
    print("migrate_sqp_add_campaign_id.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Dry run  : {args.dry_run}")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 65)

    total = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance"
    ).fetchone()[0]

    cols = [r[1] for r in cursor.execute(
        "PRAGMA table_info(search_query_performance)"
    ).fetchall()]
    has_col = 'campaign_id' in cols
    has_idx = cursor.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name='idx_sqp_campaign_date'"
    ).fetchone()[0] > 0

    print(f"\n  sqp rows             : {total:,}")
    print(f"  campaign_id column   : {'already exists' if has_col else 'missing — will add'}")
    print(f"  campaign_date index  : {'already exists' if has_idx else 'missing — will add'}")

    if has_col:
        filled = cursor.execute(
            "SELECT COUNT(*) FROM search_query_performance "
            "WHERE campaign_id IS NOT NULL"
        ).fetchone()[0]
        print(f"  Rows with campaign_id: {filled:,} / {total:,}")

    if args.dry_run:
        print("\n  DRY RUN — no changes written.")
        conn.close()
        return

    # Step 1: Add column
    if not has_col:
        print(f"\n[1] Adding campaign_id column...", end=" ", flush=True)
        t = time.time()
        cursor.execute(
            "ALTER TABLE search_query_performance ADD COLUMN campaign_id TEXT"
        )
        conn.commit()
        print(f"done ({time.time()-t:.1f}s)")
    else:
        print(f"\n[1] campaign_id column already exists — skipping")

    # Step 2: Add index
    if not has_idx:
        print(f"\n[2] Creating index...", end=" ", flush=True)
        t = time.time()
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sqp_campaign_date
            ON search_query_performance(campaign_id, report_date, device)
        """)
        conn.commit()
        print(f"done ({time.time()-t:.1f}s)")
    else:
        print(f"\n[2] Index already exists — skipping")

    # Step 3: Backfill from keyword_instances
    needs_fill = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance WHERE campaign_id IS NULL"
    ).fetchone()[0]

    if needs_fill > 0:
        print(f"\n[3] Backfilling campaign_id for {needs_fill:,} rows...",
              end=" ", flush=True)
        t = time.time()
        cursor.execute("""
            UPDATE search_query_performance
            SET campaign_id = (
                SELECT ki.campaign_id
                FROM keyword_instances ki
                WHERE ki.id = search_query_performance.instance_id
            )
            WHERE campaign_id IS NULL
        """)
        conn.commit()
        print(f"done ({time.time()-t:.1f}s)")
    else:
        print(f"\n[3] All rows already have campaign_id — skipping")

    # Step 4: Verify
    print(f"\n[4] Verification:")
    filled = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance "
        "WHERE campaign_id IS NOT NULL"
    ).fetchone()[0]
    null_ct = total - filled
    print(f"  Rows with campaign_id : {filled:,} / {total:,}")
    if null_ct > 0:
        print(f"  ⚠ Still NULL          : {null_ct:,} (orphaned sqp rows)")

    # TIER TWO check
    t2 = cursor.execute("""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT instance_id) AS slots,
               COUNT(DISTINCT query_id) AS queries
        FROM search_query_performance
        WHERE campaign_id = '23496334752'
          AND device = 'ALL'
          AND report_date >= date('now', '-30 days')
    """).fetchone()
    print(f"\n  TIER TWO sqp (30d, device=ALL):")
    print(f"    rows={t2['rows']:,}  "
          f"kw_slots={t2['slots']:,}  "
          f"queries={t2['queries']:,}")

    conn.close()
    print(f"\nDone. {datetime.now().strftime('%H:%M:%S')}")
    print("\nNext:")
    print("  python kw_db_scripts/sync_search_queries_by_campaign.py "
          "--account home --campaign-id 23496334752 --refresh 90")


if __name__ == "__main__":
    main()
