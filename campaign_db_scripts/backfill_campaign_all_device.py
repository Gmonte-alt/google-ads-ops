"""
backfill_campaign_all_device.py
================================
One-time script to generate device='ALL' rollup rows in
campaign_performance_daily from the existing device-level rows
already in the database.

This avoids a full API re-pull. The ALL rows are derived entirely
from the MOBILE + DESKTOP + TABLET rows that 03_sync_campaign_performance.py
already inserted.

IMPRESSION SHARE:
  IS metrics are campaign-level blended figures -- Google returns the same
  IS value on each device row for a given campaign-date. The ALL rollup
  takes the first non-NULL IS value from the device rows. Volume metrics
  (impressions, clicks, cost, conversions) are summed.

After this runs, future syncs via 03_sync_campaign_performance.py will
generate the ALL row automatically on every new pull.

RUN:
    conda activate paid-search
    python campaign_db_scripts/backfill_campaign_all_device.py

    # Preview row counts without writing:
    python campaign_db_scripts/backfill_campaign_all_device.py --dry-run
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing.")
    args = parser.parse_args()

    conn   = get_conn()
    cursor = conn.cursor()
    now    = datetime.utcnow().isoformat()

    print("=" * 65)
    print("backfill_campaign_all_device.py")
    print(f"Database : {DB_PATH}")
    print(f"Dry run  : {args.dry_run}")
    print("=" * 65)

    # How many existing device rows?
    device_rows = cursor.execute("""
        SELECT COUNT(*) FROM campaign_performance_daily
        WHERE device != 'ALL'
    """).fetchone()[0]

    existing_all = cursor.execute("""
        SELECT COUNT(*) FROM campaign_performance_daily
        WHERE device = 'ALL'
    """).fetchone()[0]

    # How many unique (campaign_id, account_id, date) combos will become ALL rows?
    combos = cursor.execute("""
        SELECT COUNT(DISTINCT campaign_id || '|' || account_id || '|' || date)
        FROM campaign_performance_daily
        WHERE device != 'ALL'
    """).fetchone()[0]

    print(f"\n  Existing device rows    : {device_rows:>10,}")
    print(f"  Existing ALL rows       : {existing_all:>10,}")
    print(f"  ALL rows to generate    : {combos:>10,}")

    if args.dry_run:
        print("\n  DRY RUN — no changes written.")
        conn.close()
        return

    # Delete any existing ALL rows so we can rebuild cleanly
    if existing_all > 0:
        print(f"\n  Deleting {existing_all:,} existing ALL rows...")
        cursor.execute(
            "DELETE FROM campaign_performance_daily WHERE device = 'ALL'"
        )
        conn.commit()

    # Build ALL rows by aggregating device rows in SQL
    # IS fields: use MAX() which returns the non-NULL value
    # (all devices have the same IS for a given campaign-date, or NULL)
    print(f"  Generating {combos:,} ALL rollup rows...")

    cursor.execute("""
        INSERT INTO campaign_performance_daily (
            campaign_id,
            account_id,
            date,
            device,
            impressions,
            clicks,
            cost_micros,
            conversions,
            conversion_value,
            view_through_conversions,
            all_conversions,
            all_conversion_value,
            search_impression_share,
            search_budget_lost_is,
            search_rank_lost_is,
            search_exact_match_is,
            budget_micros_that_day,
            bidding_strategy_type_that_day,
            bidding_strategy_id_that_day,
            target_cpa_micros_that_day,
            target_roas_that_day,
            last_synced_at
        )
        SELECT
            campaign_id,
            account_id,
            date,
            'ALL'                                   AS device,
            SUM(impressions)                        AS impressions,
            SUM(clicks)                             AS clicks,
            SUM(cost_micros)                        AS cost_micros,
            SUM(conversions)                        AS conversions,
            SUM(conversion_value)                   AS conversion_value,
            SUM(view_through_conversions)           AS view_through_conversions,
            SUM(all_conversions)                    AS all_conversions,
            SUM(all_conversion_value)               AS all_conversion_value,
            -- IS is identical across devices -- take the non-NULL value
            MAX(search_impression_share)            AS search_impression_share,
            MAX(search_budget_lost_is)              AS search_budget_lost_is,
            MAX(search_rank_lost_is)                AS search_rank_lost_is,
            MAX(search_exact_match_is)              AS search_exact_match_is,
            -- Snapshot columns are campaign-level, same across devices
            MAX(budget_micros_that_day)             AS budget_micros_that_day,
            MAX(bidding_strategy_type_that_day)     AS bidding_strategy_type_that_day,
            MAX(bidding_strategy_id_that_day)       AS bidding_strategy_id_that_day,
            MAX(target_cpa_micros_that_day)         AS target_cpa_micros_that_day,
            MAX(target_roas_that_day)               AS target_roas_that_day,
            ?                                       AS last_synced_at
        FROM campaign_performance_daily
        WHERE device != 'ALL'
        GROUP BY campaign_id, account_id, date
    """, (now,))

    inserted = cursor.rowcount
    conn.commit()
    print(f"  Inserted: {inserted:,} ALL rows")

    # Verification
    print(f"\n  Verification:")
    rows = cursor.execute("""
        SELECT
            account_id,
            COUNT(DISTINCT date)        AS date_count,
            MIN(date)                   AS min_date,
            MAX(date)                   AS max_date,
            COUNT(*)                    AS all_rows,
            SUM(impressions)            AS impressions,
            SUM(cost_micros) / 1e6      AS spend,
            AVG(search_impression_share) AS avg_is
        FROM campaign_performance_daily
        WHERE device = 'ALL'
        GROUP BY account_id
        ORDER BY account_id
    """).fetchall()

    print(f"  {'account_id':<15} {'dates':>6} {'min_date':<12} {'max_date':<12} "
          f"{'rows':>8} {'spend':>12} {'avg_IS':>8}")
    print(f"  {'─'*15} {'─'*6} {'─'*12} {'─'*12} {'─'*8} {'─'*12} {'─'*8}")
    for r in rows:
        avg_is = f"{float(r['avg_is'])*100:.1f}%" if r['avg_is'] else "—"
        print(f"  {str(r['account_id']):<15} {r['date_count']:>6,} "
              f"{str(r['min_date']):<12} {str(r['max_date']):<12} "
              f"{r['all_rows']:>8,} ${float(r['spend']):>11,.2f} "
              f"{avg_is:>8}")

    conn.close()
    print(f"\nDone. campaign_performance_daily now has device='ALL' rollup rows.")
    print("The analysis script's WHERE device='ALL' filter will now work correctly.")
    print("\nNext steps:")
    print("  python campaign_db_scripts/04_parse_campaign_names.py")
    print("  python analysis/analysis_campaign_keyword_roas.py")


if __name__ == "__main__":
    main()
