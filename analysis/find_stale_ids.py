"""
find_stale_ids.py
-----------------
Finds the campaign_id / ad_group_id values for the entities that
Google Ads Editor identified as no longer existing in the live account.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

STALE_CAMPAIGNS = [
    "AUTO INSURANCE-MINNESOTA-SEARCH-MOBILE-USA-STE",
    "AUTO INSURANCE-NORTH CAROLINA-SEARCH-TEST-USA-STE",
]

STALE_AD_GROUPS = [
    ("AUTO INSURANCE-MINNESOTA-SEARCH-MOBILE-USA-STE",       "Auto-Mobile-Tier1-Master-Low"),
    ("AUTO INSURANCE-NORTH CAROLINA-SEARCH-TEST-USA-STE",    "Auto-NonGeneric-Acceptance-Core-na"),
    ("AUTO INSURANCE-NORTH CAROLINA-SEARCH-TEST-USA-STE",    "Auto-NonGeneric-Freeway Insurance-Core-na"),
    ("AUTO INSURANCE-OHIO-SEARCH-MOBILE-USA-STE",            "Auto-NonGeneric-Young America Insurance-Core-na"),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=" * 70)
    print("Stale entity ID lookup")
    print("=" * 70)

    print("\nStale campaigns (account_id=23, auto MCC):")
    for name in STALE_CAMPAIGNS:
        rows = cur.execute("""
            SELECT campaign_id, campaign_name, status
            FROM campaigns
            WHERE account_id = 23 AND campaign_name = ?
        """, (name,)).fetchall()
        for r in rows:
            print(f"  campaign_id={r['campaign_id']:<15} status={r['status']:<10} '{r['campaign_name']}'")

    print("\nStale ad groups:")
    for camp, ag in STALE_AD_GROUPS:
        rows = cur.execute("""
            SELECT ag.ad_group_id, ag.ad_group_name, ag.status,
                   c.campaign_name
            FROM ad_groups ag
            JOIN campaigns c ON c.account_id = ag.account_id
                            AND c.campaign_id = ag.campaign_id
            WHERE ag.account_id = 23
              AND c.campaign_name = ?
              AND ag.ad_group_name = ?
        """, (camp, ag)).fetchall()
        for r in rows:
            print(f"  ad_group_id={r['ad_group_id']:<15} status={r['status']:<10} "
                  f"'{r['campaign_name']}' | '{r['ad_group_name']}'")

    conn.close()


if __name__ == "__main__":
    main()