"""
fix_kpd_tier_two_campaign_id.py
=================================
Corrects campaign_id on keyword_performance_daily rows for the
TIER TWO campaign using the Google Ads keyword export CSV as the
authoritative source of criterion_id → campaign_id mapping.

PROBLEM
───────
migrate_kpd_add_campaign_id.py backfilled campaign_id from
keyword_instances.campaign_id. For 161 of the 178 TIER TWO keywords,
keyword_instances.campaign_id is stale (shows Florida CPA or Maryland CPA)
because 02_sync_keyword_structure.py attributed those keywords to other
campaigns. The kpd rows for those keywords therefore got the wrong campaign_id.

FIX
───
Uses the CSV export (which has Keyword ID + Campaign ID columns) as the
ground truth. Updates kpd.campaign_id = '23496334752' for every kpd row
whose instance_id maps to a criterion_id in the CSV.

SAFE
────
Only touches rows for criterion_ids in the CSV. All other campaigns
are untouched. Safe to re-run.

RUN
───
    python kw_db_scripts/fix_kpd_tier_two_campaign_id.py

    # Preview without writing:
    python kw_db_scripts/fix_kpd_tier_two_campaign_id.py --dry-run
"""

import csv
import sqlite3
import argparse
from io import StringIO
from pathlib import Path
from datetime import datetime

DB_PATH  = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CSV_PATH = Path(r"C:\Projects\google-ads-ops\analysis\input\Search_keyword_report.csv")

TIER_TWO_CAMPAIGN_ID = "23496334752"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    return conn


def load_criterion_ids(path: Path) -> list[str]:
    with open(path, encoding='utf-8-sig') as f:
        lines = f.readlines()
    data_lines = []
    header_found = False
    for line in lines:
        if not header_found:
            if 'Keyword' in line and 'Keyword ID' in line:
                header_found = True
                data_lines.append(line)
        else:
            data_lines.append(line)
    reader = csv.DictReader(StringIO(''.join(data_lines)))
    ids = [r.get('Keyword ID','').strip() for r in reader
           if r.get('Keyword ID','').strip()
           and r.get('Keyword','').strip() not in ('', 'Total')]
    return ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 65)
    print("fix_kpd_tier_two_campaign_id.py")
    print(f"  Database : {DB_PATH}")
    print(f"  CSV      : {CSV_PATH}")
    print(f"  Dry run  : {args.dry_run}")
    print(f"  Started  : {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 65)

    criterion_ids = load_criterion_ids(CSV_PATH)
    print(f"\n  Criterion IDs from CSV : {len(criterion_ids)}")

    conn   = get_conn()
    cursor = conn.cursor()

    # Find the instance_ids for these criterion_ids
    placeholders = ','.join('?' * len(criterion_ids))
    instance_ids = cursor.execute(f"""
        SELECT ki.id, ki.criterion_id, ki.campaign_id AS current_campaign
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = '8886502147'
          AND ki.criterion_id IN ({placeholders})
    """, criterion_ids).fetchall()

    matched   = len(instance_ids)
    correct   = sum(1 for r in instance_ids
                    if r['current_campaign'] == TIER_TWO_CAMPAIGN_ID)
    stale     = matched - correct

    print(f"  Matched to ki          : {matched}")
    print(f"  Already correct        : {correct}")
    print(f"  Stale (need updating)  : {stale}")

    iids = [r['id'] for r in instance_ids]
    iid_placeholders = ','.join('?' * len(iids))

    # Count kpd rows that will be updated
    kpd_count = cursor.execute(f"""
        SELECT COUNT(*) FROM keyword_performance_daily
        WHERE instance_id IN ({iid_placeholders})
          AND campaign_id != ?
    """, iids + [TIER_TWO_CAMPAIGN_ID]).fetchone()[0]

    already_correct = cursor.execute(f"""
        SELECT COUNT(*) FROM keyword_performance_daily
        WHERE instance_id IN ({iid_placeholders})
          AND campaign_id = ?
    """, iids + [TIER_TWO_CAMPAIGN_ID]).fetchone()[0]

    print(f"\n  kpd rows to update     : {kpd_count:,}")
    print(f"  kpd rows already right : {already_correct:,}")

    # Verify spend that will become visible after update
    spend_preview = cursor.execute(f"""
        SELECT
            COUNT(DISTINCT instance_id) AS slots,
            SUM(cost_micros)/1e6        AS spend,
            MIN(report_date)            AS min_date,
            MAX(report_date)            AS max_date
        FROM keyword_performance_daily
        WHERE instance_id IN ({iid_placeholders})
          AND device = 'ALL'
    """, iids).fetchone()

    print(f"\n  Preview (all kpd for these keywords):")
    print(f"    Keyword slots : {spend_preview['slots']:,}")
    print(f"    Total spend   : ${spend_preview['spend']:,.2f}")
    print(f"    Date range    : {spend_preview['min_date']} → "
          f"{spend_preview['max_date']}")

    if args.dry_run:
        print(f"\n  DRY RUN — no changes written.")
        conn.close()
        return

    # Execute the update
    print(f"\n  Updating {kpd_count:,} kpd rows...", end=" ", flush=True)
    import time
    t = time.time()
    cursor.execute(f"""
        UPDATE keyword_performance_daily
        SET campaign_id = ?
        WHERE instance_id IN ({iid_placeholders})
          AND campaign_id != ?
    """, [TIER_TWO_CAMPAIGN_ID] + iids + [TIER_TWO_CAMPAIGN_ID])
    updated = cursor.rowcount
    conn.commit()
    print(f"done ({time.time()-t:.1f}s)")
    print(f"  Updated: {updated:,} rows")

    # Verify
    print(f"\n  Verification — TIER TWO kpd by window:")
    for days, label in [(30,'30d'), (60,'60d'), (90,'90d')]:
        row = cursor.execute(f"""
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT instance_id) AS slots,
                   SUM(cost_micros)/1e6 AS spend
            FROM keyword_performance_daily
            WHERE campaign_id = ?
              AND device      = 'ALL'
              AND report_date >= date('now', '-{days} days')
        """, (TIER_TWO_CAMPAIGN_ID,)).fetchone()
        print(f"    {label}: {row['slots']:,} slots  "
              f"{row['rows']:,} rows  ${row['spend']:,.2f} spend")

    conn.close()
    print(f"\nDone. {datetime.now().strftime('%H:%M:%S')}")
    print("\nNext step:")
    print("  python analysis/analysis_tier_two_keywords.py")


if __name__ == "__main__":
    main()
