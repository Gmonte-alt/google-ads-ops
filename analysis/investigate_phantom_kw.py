"""
investigate_phantom_kw.py
-------------------------
Investigates the specific 'acceptance insurance' BROAD keyword in
Massachusetts/Auto-Mobile-Tier1-Group1 that Google Ads Editor flagged
as a phantom Add.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=" * 78)
    print("Phantom keyword investigation")
    print("=" * 78)

    # 1. Find every keyword_instances row for 'acceptance insurance' in
    #    the Massachusetts campaign
    print("\n[1] keyword_instances rows for 'acceptance insurance' in MA campaign:")
    rows = cur.execute("""
        SELECT
            ki.id              AS instance_id,
            ki.criterion_id,
            ki.campaign_id,
            ki.campaign_name,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.keyword_text,
            ki.match_type,
            ki.status          AS kw_status,
            ki.first_seen_date,
            ki.last_seen_date,
            kid.normalized_text
        FROM keyword_instances ki
        JOIN keyword_identities kid ON kid.id = ki.identity_id
        WHERE ki.account_id = 23
          AND LOWER(kid.normalized_text) LIKE '%acceptance insurance%'
          AND ki.campaign_name LIKE '%MASSACHUSETTS%'
        ORDER BY ki.ad_group_name, ki.match_type
    """).fetchall()

    if not rows:
        print("  (no matches — different campaign name or different brand?)")
    for r in rows:
        print(f"\n  instance_id    : {r['instance_id']}")
        print(f"  criterion_id   : {r['criterion_id']}")
        print(f"  campaign       : '{r['campaign_name']}' (id={r['campaign_id']})")
        print(f"  ad_group       : '{r['ad_group_name']}' (id={r['ad_group_id']})")
        print(f"  keyword_text   : '{r['keyword_text']}'")
        print(f"  match_type     : {r['match_type']}")
        print(f"  kw_status      : {r['kw_status']}")
        print(f"  first_seen     : {r['first_seen_date']}")
        print(f"  last_seen      : {r['last_seen_date']}")

    # 2. Status distribution of all 'acceptance insurance' instances in account
    print("\n[2] Status distribution for all 'acceptance insurance' "
          "instances in auto MCC:")
    status_rows = cur.execute("""
        SELECT ki.status, ki.match_type, COUNT(*) AS n
        FROM keyword_instances ki
        JOIN keyword_identities kid ON kid.id = ki.identity_id
        WHERE ki.account_id = 23
          AND LOWER(kid.normalized_text) LIKE '%acceptance insurance%'
        GROUP BY ki.status, ki.match_type
        ORDER BY ki.status, ki.match_type
    """).fetchall()
    for r in status_rows:
        print(f"  {r['status']:<10} {r['match_type']:<8} {r['n']:>5}")

    # 3. Overall status distribution across the whole upload set
    print("\n[3] Status distribution across ALL matched 'auto MCC' instances:")
    overall_rows = cur.execute("""
        SELECT ki.status, COUNT(*) AS n
        FROM keyword_instances ki
        WHERE ki.account_id = 23
        GROUP BY ki.status
        ORDER BY ki.status
    """).fetchall()
    for r in overall_rows:
        print(f"  {r['status']:<10} {r['n']:>8}")

    # 4. last_seen_date distribution — keywords not seen recently are
    #    likely deleted upstream
    print("\n[4] last_seen_date for the MA acceptance-insurance instance(s):")
    if rows:
        for r in rows:
            print(f"  '{r['ad_group_name']}' ({r['match_type']}): "
                  f"last_seen={r['last_seen_date']}")

    # 5. What's today's most recent sync date for the auto account?
    print("\n[5] Most recent last_seen_date in the auto MCC overall:")
    most_recent = cur.execute("""
        SELECT MAX(last_seen_date) AS recent FROM keyword_instances WHERE account_id = 23
    """).fetchone()
    print(f"  Most recent: {most_recent['recent']}")

    conn.close()


if __name__ == "__main__":
    main()