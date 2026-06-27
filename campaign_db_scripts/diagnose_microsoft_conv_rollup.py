"""
Check whether the conversion-count discrepancy seen in
HOME INSURANCE-TEXAS-SEARCH-MOBILE-USA-STE is a systemic loader bug or
just one weird campaign.

For each enabled Microsoft Home Insurance campaign, prints the
device='ALL' conversion count AND the sum of per-device conversion
counts for May 4-10, 2026. If `ALL` consistently equals the device sum
across all campaigns, then the loader is populating ALL by summing the
device rows (instead of fetching the un-segmented "All devices" total
from the Microsoft Ads API). Cross-device attribution means the
un-segmented total is usually LOWER than the device sum.

After running this, take a sample of 3-5 campaigns and compare the
device='ALL' figure here against the Microsoft Ads UI campaign view
(same dates: May 4-10). The ratio of (ALL row count) / (UI count)
should be close to 1.0 if loaded correctly. A ratio of ~2.0 across
multiple campaigns confirms the loader bug.

Usage:
    python diagnose_microsoft_conv_rollup.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2
START = "2026-05-04"
END = "2026-05-10"


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("=" * 90)
    print(f"Conversion rollup check | {START} to {END} | account {ACCOUNT_ID}")
    print("=" * 90)
    print()

    # Pull all enabled campaigns
    campaigns = cur.execute(
        "SELECT campaign_id, campaign_name FROM campaigns "
        "WHERE account_id = ? AND status = 'ENABLED' "
        "ORDER BY campaign_name",
        (ACCOUNT_ID,),
    ).fetchall()

    print("Search table (campaign_performance_daily)")
    print(f"  {'campaign':<55} {'ALL_conv':>10} {'dev_sum':>10} {'ratio':>8}")
    print(f"  {'-'*55} {'-'*10} {'-'*10} {'-'*8}")

    n_match = 0
    n_off = 0
    for campaign_id, campaign_name in campaigns:
        # device='ALL' row's conversion count
        row = cur.execute(
            """
            SELECT SUM(conversions)
            FROM campaign_performance_daily
            WHERE campaign_id = ?
              AND date BETWEEN ? AND ?
              AND device = 'ALL'
            """,
            (campaign_id, START, END),
        ).fetchone()
        all_conv = row[0] or 0

        # Sum of per-device rows
        row = cur.execute(
            """
            SELECT SUM(conversions)
            FROM campaign_performance_daily
            WHERE campaign_id = ?
              AND date BETWEEN ? AND ?
              AND device != 'ALL'
            """,
            (campaign_id, START, END),
        ).fetchone()
        dev_sum = row[0] or 0

        if all_conv == 0 and dev_sum == 0:
            continue  # no data this week, skip

        short = campaign_name.replace("HOME INSURANCE-", "")[:55]
        ratio = (all_conv / dev_sum) if dev_sum else float("inf")
        marker = " ← match" if abs(ratio - 1.0) < 0.01 else " ← MISMATCH"
        print(f"  {short:<55} {all_conv:>10.2f} {dev_sum:>10.2f} {ratio:>8.3f}{marker}")
        if abs(ratio - 1.0) < 0.01:
            n_match += 1
        else:
            n_off += 1

    print()
    print(f"  Summary: {n_match} campaigns where ALL == device-sum, "
          f"{n_off} where they differ")
    print()
    print("Audience table (campaign_performance_network_daily, Audience only)")
    print(f"  {'campaign':<55} {'ALL_conv':>10} {'dev_sum':>10} {'ratio':>8}")
    print(f"  {'-'*55} {'-'*10} {'-'*10} {'-'*8}")

    for campaign_id, campaign_name in campaigns:
        row = cur.execute(
            """
            SELECT SUM(conversions)
            FROM campaign_performance_network_daily
            WHERE campaign_id = ?
              AND date BETWEEN ? AND ?
              AND device = 'ALL'
              AND network = 'Audience'
            """,
            (campaign_id, START, END),
        ).fetchone()
        all_conv = row[0] or 0
        row = cur.execute(
            """
            SELECT SUM(conversions)
            FROM campaign_performance_network_daily
            WHERE campaign_id = ?
              AND date BETWEEN ? AND ?
              AND device != 'ALL'
              AND network = 'Audience'
            """,
            (campaign_id, START, END),
        ).fetchone()
        dev_sum = row[0] or 0

        if all_conv == 0 and dev_sum == 0:
            continue
        short = campaign_name.replace("HOME INSURANCE-", "")[:55]
        ratio = (all_conv / dev_sum) if dev_sum else float("inf")
        marker = " ← match" if abs(ratio - 1.0) < 0.01 else " ← MISMATCH"
        print(f"  {short:<55} {all_conv:>10.2f} {dev_sum:>10.2f} {ratio:>8.3f}{marker}")

    print()
    print("Interpretation:")
    print("  - If ALL == device-sum across all campaigns: loader is SUMMING device")
    print("    rows into the ALL row. That's wrong for conversions (cross-device")
    print("    attribution makes the un-segmented total lower than the sum).")
    print("    Fix in the loader, not in the visualization script.")
    print("  - If ALL differs from device-sum: loader is fetching the un-segmented")
    print("    row separately. In that case, ALL is right and the Texas mismatch")
    print("    has a different cause.")
    print()
    print("Next step: compare device='ALL' figures here against the Microsoft Ads")
    print("UI campaign view for May 4-10 on 3-5 specific campaigns. If the DB")
    print("figure is ~2x the UI figure consistently, that confirms the loader bug.")
    conn.close()


if __name__ == "__main__":
    main()
