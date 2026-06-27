"""
diag_home_adgroups.py
----------------------
Quick diagnostic to understand the 109,618 ad group rows
for the Home Insurance Google Ads account (8886502147).

Run:
    python campaign_db_scripts/diag_home_adgroups.py
"""

import sqlite3
from pathlib import Path

DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
HOME_ACCOUNT_ID  = "8886502147"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def main():
    conn   = get_conn()
    cursor = conn.cursor()

    print("=" * 65)
    print("Home Insurance Ad Group Diagnostic")
    print(f"Account: {HOME_ACCOUNT_ID}")
    print("=" * 65)

    # ── 1. Status breakdown ──────────────────────────────────────────
    print("\n[1] Ad group status breakdown:")
    rows = cursor.execute("""
        SELECT status, COUNT(*) AS cnt
        FROM ad_groups
        WHERE account_id = ?
        GROUP BY status
        ORDER BY cnt DESC
    """, (HOME_ACCOUNT_ID,)).fetchall()
    total = sum(r['cnt'] for r in rows)
    for r in rows:
        pct = r['cnt'] / total * 100
        print(f"  {r['status']:<12} {r['cnt']:>8,}  ({pct:.1f}%)")
    print(f"  {'TOTAL':<12} {total:>8,}")

    # ── 2. Campaign breakdown ────────────────────────────────────────
    print("\n[2] Ad groups per campaign (top 20 by count):")
    rows = cursor.execute("""
        SELECT
            ag.campaign_id,
            c.campaign_name,
            COUNT(*)                                              AS ag_count,
            SUM(CASE WHEN ag.status='ENABLED' THEN 1 ELSE 0 END) AS enabled,
            SUM(CASE WHEN ag.status='PAUSED'  THEN 1 ELSE 0 END) AS paused
        FROM ad_groups ag
        LEFT JOIN campaigns c ON ag.campaign_id = c.campaign_id
        WHERE ag.account_id = ?
        GROUP BY ag.campaign_id
        ORDER BY ag_count DESC
        LIMIT 20
    """, (HOME_ACCOUNT_ID,)).fetchall()
    print(f"  {'Campaign':<55} {'total':>7} {'enabled':>8} {'paused':>8}")
    print(f"  {'─'*55} {'─'*7} {'─'*8} {'─'*8}")
    for r in rows:
        camp = str(r['campaign_name'] or r['campaign_id'])[:54]
        print(f"  {camp:<55} {r['ag_count']:>7,} {r['enabled']:>8,} {r['paused']:>8,}")

    # ── 3. Sample ad group names ─────────────────────────────────────
    print("\n[3] Sample of 20 enabled ad group names:")
    rows = cursor.execute("""
        SELECT ad_group_id, ad_group_name, campaign_id, cpc_bid_micros, bid_override_flag
        FROM ad_groups
        WHERE account_id = ? AND status = 'ENABLED'
        ORDER BY ad_group_id
        LIMIT 20
    """, (HOME_ACCOUNT_ID,)).fetchall()
    for r in rows:
        bid = f"${r['cpc_bid_micros']/1e6:.2f}" if r['cpc_bid_micros'] else "smart"
        ovr = " ⚠bid_override" if r['bid_override_flag'] else ""
        print(f"  [{r['ad_group_id']}] '{r['ad_group_name']}'  "
              f"bid={bid}{ovr}")

    # ── 4. Cross-check vs keyword_instances ─────────────────────────
    print("\n[4] Ad groups in keyword_instances vs ad_groups table:")
    ki_ags = cursor.execute("""
        SELECT COUNT(DISTINCT ad_group_id) AS cnt
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
    """, (HOME_ACCOUNT_ID,)).fetchone()['cnt']

    db_ags = cursor.execute(
        "SELECT COUNT(*) FROM ad_groups WHERE account_id = ?",
        (HOME_ACCOUNT_ID,)
    ).fetchone()[0]

    ki_enabled = cursor.execute("""
        SELECT COUNT(DISTINCT ki.ad_group_id) AS cnt
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND ki.status IN ('ENABLED','PAUSED')
    """, (HOME_ACCOUNT_ID,)).fetchone()['cnt']

    print(f"  Distinct ad_group_ids in keyword_instances : {ki_ags:>8,}")
    print(f"  Rows in ad_groups table                    : {db_ags:>8,}")
    print(f"  Active keyword_instance ad groups          : {ki_enabled:>8,}")
    print()
    if db_ags > ki_ags * 10:
        print("  ⚠ ad_groups table is much larger than keyword_instances suggests.")
        print("    This likely means the sync pulled REMOVED ad groups despite the filter,")
        print("    OR historical ad groups from campaigns that are now paused/removed.")
        print("    For analysis purposes, filter WHERE status = 'ENABLED' AND")
        print("    JOIN to keyword_instances to get only ad groups with active keywords.")
    else:
        print("  ✓ Row counts are consistent.")

    # ── 5. Campaign performance daily device check ───────────────────
    print("\n[5] campaign_performance_daily — device values for home account:")
    rows = cursor.execute("""
        SELECT device, COUNT(*) AS cnt, MIN(date) AS min_d, MAX(date) AS max_d
        FROM campaign_performance_daily
        WHERE account_id = ?
        GROUP BY device
        ORDER BY cnt DESC
    """, (HOME_ACCOUNT_ID,)).fetchall()

    if not rows:
        print("  No rows found for account_id = '8886502147'")
        print("  Checking if campaign_performance_daily uses integer account_id instead:")
        # Try integer lookup
        acct_int = cursor.execute(
            "SELECT id FROM accounts WHERE platform_account_id = ?",
            (HOME_ACCOUNT_ID,)
        ).fetchone()
        if acct_int:
            rows2 = cursor.execute("""
                SELECT device, COUNT(*) AS cnt, MIN(date) AS min_d, MAX(date) AS max_d
                FROM campaign_performance_daily
                WHERE account_id = ?
                GROUP BY device
                ORDER BY cnt DESC
            """, (str(acct_int['id']),)).fetchall()
            if rows2:
                print(f"  Found rows using integer account_id={acct_int['id']}:")
                for r in rows2:
                    print(f"  device='{r['device']}'  rows={r['cnt']:,}  "
                          f"{r['min_d']} → {r['max_d']}")
                print("\n  ⚠ campaign_performance_daily uses INTEGER account_id")
                print("    but the diagnostic was querying with STRING '8886502147'.")
                print("    The 03_sync_campaign_performance.py script stores a different")
                print("    account_id format than the other campaign scripts.")
            else:
                print("  No rows found with integer account_id either.")
                print("  campaign_performance_daily may store account_id differently.")
                # Check what account_id values actually exist
                sample = cursor.execute(
                    "SELECT DISTINCT account_id FROM campaign_performance_daily LIMIT 5"
                ).fetchall()
                print(f"  Sample account_id values in CPD: {[r[0] for r in sample]}")
    else:
        for r in rows:
            print(f"  device='{r['device']}'  rows={r['cnt']:,}  "
                  f"{r['min_d']} → {r['max_d']}")

    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
