"""
repair_adgroups_home.py
========================
Repairs the ad_groups table for the Home Insurance Google Ads account.

PROBLEM
-------
The ad_groups table contains 109,618 rows for account 8886502147, but only
791 of those ad_group_ids appear in keyword_instances. The other ~108,827
rows are spurious data (Auto Insurance / Display ad groups pulled via an
overly broad MCC query scope in 02_sync_campaigns_adgroups.py).

SOLUTION
--------
1. Delete all ad_group rows where account_id = '8886502147'
2. Rebuild the correct ad_groups from keyword_instances, which already
   holds the authoritative ad_group_id, ad_group_name, campaign_id, and
   status for every keyword-bearing ad group in the account.
3. Verify the result.

This script does NOT touch:
  - campaigns table (correct, leave alone)
  - campaign_performance_daily (correct, leave alone)
  - keyword_instances (source of truth, read-only here)
  - Any other account's data

BACKFILL NOTE
-------------
No API call is needed. keyword_instances was populated by
02_sync_keyword_structure.py which pulls directly from the account,
so it already contains the correct ad group metadata.
first_seen_date is set to the earliest date in keyword_performance_daily
for that ad group, giving you historical context back to Jan 1, 2025.

RUN
---
    conda activate paid-search
    python campaign_db_scripts/repair_adgroups_home.py

    # Preview without writing:
    python campaign_db_scripts/repair_adgroups_home.py --dry-run
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import date, datetime

DB_PATH         = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
HOME_ACCOUNT_ID = "8886502147"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing to database.")
    args = parser.parse_args()

    conn   = get_conn()
    cursor = conn.cursor()
    now    = datetime.utcnow().isoformat()

    print("=" * 65)
    print("repair_adgroups_home.py")
    print(f"Account  : {HOME_ACCOUNT_ID}")
    print(f"Dry run  : {args.dry_run}")
    print(f"Run time : {now}")
    print("=" * 65)

    # ── Step 1: Count what we're deleting ────────────────────────────
    current_count = cursor.execute(
        "SELECT COUNT(*) FROM ad_groups WHERE account_id = ?",
        (HOME_ACCOUNT_ID,)
    ).fetchone()[0]

    ki_ag_count = cursor.execute("""
        SELECT COUNT(DISTINCT ki.ad_group_id)
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
    """, (HOME_ACCOUNT_ID,)).fetchone()[0]

    print(f"\n[1] Current state:")
    print(f"  ad_groups rows for home account : {current_count:>8,}")
    print(f"  Distinct ad_group_ids in ki      : {ki_ag_count:>8,}")
    print(f"  Rows to delete                   : {current_count:>8,}")
    print(f"  Rows to insert                   : {ki_ag_count:>8,}")

    # ── Step 2: Build replacement rows from keyword_instances ─────────
    print(f"\n[2] Building replacement ad_groups from keyword_instances...")

    replacement_rows = cursor.execute("""
        SELECT
            ki.ad_group_id,
            ki.campaign_id,
            -- account_id stored as platform_account_id string (matches campaigns table)
            a.platform_account_id                           AS account_id,
            ki.ad_group_name,
            -- Use the most common status across keyword instances in this ag
            CASE
                WHEN SUM(CASE WHEN ki.status = 'ENABLED' THEN 1 ELSE 0 END) > 0
                THEN 'ENABLED'
                ELSE 'PAUSED'
            END                                             AS status,
            -- Earliest keyword date as a proxy for first_seen_date
            MIN(kpd.report_date)                            AS first_seen_date,
            -- No cpc_bid_micros, target_cpa/roas, parsed columns at this stage
            -- Those will be populated when 02_sync_campaigns_adgroups.py is fixed
            NULL  AS ad_group_type,
            NULL  AS cpc_bid_micros,
            NULL  AS target_cpa_micros,
            NULL  AS target_roas,
            0     AS bid_override_flag,
            NULL  AS parsed_theme,
            NULL  AS parsed_match_tier,
            NULL  AS parsed_intent_tier,
            0     AS naming_convention_flag
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        LEFT JOIN keyword_performance_daily kpd ON kpd.instance_id = ki.id
        WHERE a.platform_account_id = ?
        GROUP BY ki.ad_group_id, ki.campaign_id, a.platform_account_id, ki.ad_group_name
        ORDER BY ki.campaign_id, ki.ad_group_name
    """, (HOME_ACCOUNT_ID,)).fetchall()

    print(f"  Built {len(replacement_rows):,} replacement rows")

    # Preview sample
    print(f"\n  Sample replacement rows:")
    print(f"  {'ad_group_id':<20} {'campaign_id':<20} "
          f"{'status':<8} {'first_seen':<12} {'ad_group_name'}")
    print(f"  {'─'*20} {'─'*20} {'─'*8} {'─'*12} {'─'*45}")
    for r in list(replacement_rows)[:10]:
        print(f"  {str(r['ad_group_id']):<20} {str(r['campaign_id']):<20} "
              f"{str(r['status']):<8} {str(r['first_seen_date'] or '—'):<12} "
              f"{str(r['ad_group_name'])[:44]}")

    if args.dry_run:
        print(f"\n  DRY RUN — no changes written.")
        print(f"  Would delete  : {current_count:,} rows")
        print(f"  Would insert  : {len(replacement_rows):,} rows")
        conn.close()
        return

    # ── Step 3: Delete existing rows ──────────────────────────────────
    print(f"\n[3] Deleting {current_count:,} existing ad_group rows...")
    cursor.execute(
        "DELETE FROM ad_groups WHERE account_id = ?",
        (HOME_ACCOUNT_ID,)
    )
    deleted = cursor.rowcount
    print(f"  Deleted: {deleted:,} rows")

    # ── Step 4: Insert replacement rows ───────────────────────────────
    print(f"\n[4] Inserting {len(replacement_rows):,} replacement rows...")
    cursor.executemany("""
        INSERT OR REPLACE INTO ad_groups (
            ad_group_id,
            campaign_id,
            account_id,
            ad_group_name,
            status,
            ad_group_type,
            cpc_bid_micros,
            target_cpa_micros,
            target_roas,
            bid_override_flag,
            parsed_theme,
            parsed_match_tier,
            parsed_intent_tier,
            naming_convention_flag,
            first_seen_date,
            last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (
            r['ad_group_id'],
            r['campaign_id'],
            r['account_id'],
            r['ad_group_name'],
            r['status'],
            r['ad_group_type'],
            r['cpc_bid_micros'],
            r['target_cpa_micros'],
            r['target_roas'],
            r['bid_override_flag'],
            r['parsed_theme'],
            r['parsed_match_tier'],
            r['parsed_intent_tier'],
            r['naming_convention_flag'],
            r['first_seen_date'],
            now,
        )
        for r in replacement_rows
    ])
    inserted = cursor.rowcount
    conn.commit()
    print(f"  Inserted: {inserted:,} rows")

    # ── Step 5: Verify ────────────────────────────────────────────────
    print(f"\n[5] Verification:")
    final_count = cursor.execute(
        "SELECT COUNT(*) FROM ad_groups WHERE account_id = ?",
        (HOME_ACCOUNT_ID,)
    ).fetchone()[0]
    enabled_count = cursor.execute(
        "SELECT COUNT(*) FROM ad_groups "
        "WHERE account_id = ? AND status = 'ENABLED'",
        (HOME_ACCOUNT_ID,)
    ).fetchone()[0]
    paused_count = cursor.execute(
        "SELECT COUNT(*) FROM ad_groups "
        "WHERE account_id = ? AND status = 'PAUSED'",
        (HOME_ACCOUNT_ID,)
    ).fetchone()[0]

    print(f"  Final ad_groups rows  : {final_count:,}")
    print(f"  Enabled               : {enabled_count:,}")
    print(f"  Paused                : {paused_count:,}")

    # Campaign breakdown
    print(f"\n  Ad groups per campaign:")
    camp_rows = cursor.execute("""
        SELECT c.campaign_name, COUNT(*) AS ag_count,
               SUM(CASE WHEN ag.status='ENABLED' THEN 1 ELSE 0 END) AS enabled
        FROM ad_groups ag
        LEFT JOIN campaigns c ON ag.campaign_id = c.campaign_id
        WHERE ag.account_id = ?
        GROUP BY ag.campaign_id
        ORDER BY ag_count DESC
        LIMIT 15
    """, (HOME_ACCOUNT_ID,)).fetchall()
    for r in camp_rows:
        print(f"  {str(r['campaign_name'] or '—')[:55]:<55} "
              f"total={r['ag_count']:>4}  enabled={r['enabled']:>4}")

    conn.close()
    print(f"\nDone. ad_groups table for {HOME_ACCOUNT_ID} has been repaired.")
    print("Next: python campaign_db_scripts/04_parse_campaign_names.py")
    print("Then: python analysis/analysis_campaign_keyword_roas.py")

if __name__ == "__main__":
    main()
