"""
audit_ghost_ad_groups.py
------------------------
Cross-checks every (campaign_name, ad_group_name) pair in keyword_instances
against the live ad_groups table. Surfaces any pairs where the keyword's
recorded ad group name doesn't actually exist in ad_groups — those will
cause Google Ads Editor to attempt creating new ad groups on import.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# The accounts we're exporting label files for
TARGET_ACCOUNT_IDS = ["3863660890", "1006729211", "F149Y8EN", "F149SN63"]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("=" * 70)
    print("Ghost ad group / campaign audit")
    print("=" * 70)

    placeholders = ",".join("?" * len(TARGET_ACCOUNT_IDS))
    rows = cursor.execute(f"""
        SELECT
            a.account_name,
            a.platform,
            a.platform_account_id,
            ki.campaign_id,
            ki.campaign_name AS ki_campaign_name,
            c.campaign_name  AS c_campaign_name,
            ki.ad_group_id,
            ki.ad_group_name AS ki_ad_group_name,
            ag.ad_group_name AS ag_ad_group_name,
            COUNT(*)         AS n_instances
        FROM keyword_instances ki
        JOIN accounts a ON a.id = ki.account_id
        LEFT JOIN campaigns c
               ON c.account_id  = ki.account_id
              AND c.campaign_id = ki.campaign_id
        LEFT JOIN ad_groups ag
               ON ag.account_id  = ki.account_id
              AND ag.ad_group_id = ki.ad_group_id
        WHERE a.platform_account_id IN ({placeholders})
          AND ki.status = 'ENABLED'
          AND (
              ki.campaign_name != c.campaign_name
              OR ki.ad_group_name != ag.ad_group_name
              OR c.campaign_name IS NULL
              OR ag.ad_group_name IS NULL
          )
        GROUP BY ki.campaign_id, ki.ad_group_id,
                 ki.campaign_name, c.campaign_name,
                 ki.ad_group_name, ag.ad_group_name
        ORDER BY a.account_name, ki.campaign_name, ki.ad_group_name
    """, TARGET_ACCOUNT_IDS).fetchall()

    if not rows:
        print("\n  No mismatches found. All keyword_instances names match the\n"
              "  current campaigns and ad_groups tables.\n")
        return

    print(f"\n  Found {len(rows)} mismatched (campaign, ad_group) groups:\n")
    for r in rows:
        print(f"  Account     : {r['account_name']} ({r['platform_account_id']})")
        print(f"  Instances   : {r['n_instances']}")
        print(f"  KI campaign : '{r['ki_campaign_name']}'")
        print(f"  Live camp   : '{r['c_campaign_name']}'"
              f"  {'← MISMATCH' if r['ki_campaign_name'] != r['c_campaign_name'] else ''}")
        print(f"  KI ad_group : '{r['ki_ad_group_name']}'")
        print(f"  Live ad_grp : '{r['ag_ad_group_name']}'"
              f"  {'← MISMATCH' if r['ki_ad_group_name'] != r['ag_ad_group_name'] else ''}")
        print()

    conn.close()


if __name__ == "__main__":
    main()