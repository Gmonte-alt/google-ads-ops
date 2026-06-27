"""
verify_phantom_in_live_export.py
--------------------------------
Compares the 'acceptance insurance' BROAD keyword in MA Auto-Mobile-Tier1-Group1
against the live Google Ads Editor export, the database, and looks at
recent performance to see if it ever served traffic.
"""

import sqlite3
import csv
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
LIVE_EXPORT = Path(
    r"C:\Projects\google-ads-ops\analysis\input"
    r"\Auto_Insurance__30_Campaigns_4991_Ad_groups_2026-04-29.csv"
)

TARGET_CAMPAIGN = "AUTO INSURANCE-MASSACHUSETTS-SEARCH-MOBILE-USA-STE"
TARGET_AG       = "Auto-Mobile-Tier1-Group1"
TARGET_KW       = "acceptance insurance"


def check_db():
    print("=" * 78)
    print("[1] keyword_instances rows for the specific phantom case")
    print("=" * 78)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT
            ki.id              AS instance_id,
            ki.criterion_id,
            ki.match_type,
            ki.status          AS kw_status,
            ki.last_seen_date,
            ki.cpc_bid_micros,
            ki.quality_score
        FROM keyword_instances ki
        JOIN keyword_identities kid ON kid.id = ki.identity_id
        WHERE ki.account_id = 23
          AND ki.campaign_name = ?
          AND ki.ad_group_name = ?
          AND LOWER(kid.normalized_text) = LOWER(?)
    """, (TARGET_CAMPAIGN, TARGET_AG, TARGET_KW)).fetchall()

    if not rows:
        print(f"  No keyword_instances rows found.")
    for r in rows:
        print(f"  instance_id={r['instance_id']} criterion_id={r['criterion_id']} "
              f"match_type={r['match_type']} status={r['kw_status']} "
              f"last_seen={r['last_seen_date']}")

    # Performance check — did it ever serve?
    print(f"\n[2] Has this instance ever had impressions/clicks/spend?")
    for r in rows:
        perf = cur.execute("""
            SELECT
                MIN(report_date) AS first_d,
                MAX(report_date) AS last_d,
                SUM(impressions) AS imps,
                SUM(clicks)      AS clk,
                SUM(cost_micros)/1e6 AS spend
            FROM keyword_performance_daily
            WHERE instance_id = ?
              AND device = 'ALL' AND geo_state = 'ALL'
        """, (r["instance_id"],)).fetchone()
        if perf and perf["imps"]:
            print(f"  instance {r['instance_id']} ({r['match_type']}): "
                  f"first={perf['first_d']} last={perf['last_d']} "
                  f"imps={perf['imps']} clk={perf['clk']} spend=${perf['spend'] or 0:,.2f}")
        else:
            print(f"  instance {r['instance_id']} ({r['match_type']}): "
                  f"NEVER had any performance data")

    conn.close()


def check_live_export():
    print(f"\n[3] Searching live Editor export for the keyword...")
    if not LIVE_EXPORT.exists():
        print(f"  Live export not found at {LIVE_EXPORT}")
        print(f"  (skipping — adjust LIVE_EXPORT path if needed)")
        return

    with open(LIVE_EXPORT, "r", encoding="utf-16") as f:
        reader = csv.reader(f, delimiter="\t")
        rows = list(reader)

    headers = rows[0]
    camp_idx = headers.index("Campaign")
    ag_idx   = headers.index("Ad Group")
    kw_idx   = headers.index("Keyword")
    crit_idx = headers.index("Criterion Type")

    found = []
    for r in rows[1:]:
        if len(r) <= max(camp_idx, ag_idx, kw_idx, crit_idx):
            continue
        if (r[camp_idx] == TARGET_CAMPAIGN
            and r[ag_idx] == TARGET_AG
            and r[kw_idx].lower() == TARGET_KW.lower()):
            found.append(r)

    if not found:
        print(f"  Keyword NOT FOUND in live Editor export.")
        print(f"  → Either it doesn't exist in Google Ads, OR it wasn't")
        print(f"    included in the export (paused-keyword filter, etc.)")
    else:
        print(f"  Found {len(found)} matching rows in live export:")
        for r in found:
            status = r[headers.index("Status")] if "Status" in headers else "?"
            print(f"    keyword='{r[kw_idx]}' match_type='{r[crit_idx]}' "
                  f"status='{status}'")


if __name__ == "__main__":
    check_db()
    check_live_export()