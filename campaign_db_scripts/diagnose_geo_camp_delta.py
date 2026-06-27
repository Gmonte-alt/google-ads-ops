"""
diagnose_geo_camp_delta.py
==========================
Refresh-check shows geo $5,079.66 higher than campaign. Most likely
explanation: geo extends through 2026-05-12, campaign through 2026-05-10.
This script confirms by comparing spend on a common date window.

If the gap closes when both tables are filtered to the same date range,
the delta is just date-asymmetry and will resolve on the next refresh.

If the gap persists, there's a real reconciliation issue worth investigating
before refreshing.

Run:
    python diagnose_geo_camp_delta.py
"""

import sys
from pathlib import Path
sys.path.insert(0, r"C:\Projects\google-ads-ops\campaign_db_scripts")
from refresh_helper import get_connection, resolve_account_id


ACCOUNT_KEY = "home"


def main():
    conn = get_connection()
    account_id = resolve_account_id(conn, ACCOUNT_KEY)

    print("=" * 78)
    print("Geo-vs-campaign delta diagnostic")
    print("=" * 78)
    print()

    # What MAX(date) does each table have?
    maxes = {}
    for table, date_col in [
        ("campaign_performance_daily", "date"),
        ("campaign_performance_network_daily", "date"),
        ("geo_performance_daily", "date"),
    ]:
        row = conn.execute(
            f"SELECT MAX({date_col}) FROM {table} WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        maxes[table] = row[0]
        print(f"  {table:<40} max date: {row[0]}")
    print()

    # Common end date = min of all three (so we compare apples to apples)
    common_end = min(maxes.values())
    print(f"  Common end date for comparison: {common_end}")
    print()

    # Full-history totals (what refresh_check reports)
    print("Full-history totals (no date filter, what refresh_check shows):")
    camp_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()
    net_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM campaign_performance_network_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()
    geo_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM geo_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()
    camp_total = (camp_row[0] or 0) + (net_row[0] or 0)
    geo_total = geo_row[0] or 0
    print(f"  Campaign (search + network): ${camp_total:>14,.2f}")
    print(f"  Geo (PHYSICAL+INTEREST):     ${geo_total:>14,.2f}")
    print(f"  Delta:                       ${geo_total - camp_total:>+14,.2f}  "
          f"({(geo_total - camp_total) / camp_total * 100:.3f}%)")
    print()

    # Same-window totals (clip both to common_end)
    print(f"Same-window totals (date <= {common_end}):")
    camp_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL' AND date <= ?
    """, (account_id, common_end)).fetchone()
    net_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM campaign_performance_network_daily
        WHERE account_id = ? AND device = 'ALL' AND date <= ?
    """, (account_id, common_end)).fetchone()
    geo_row = conn.execute("""
        SELECT COALESCE(SUM(cost_micros)/1e6, 0)
        FROM geo_performance_daily
        WHERE account_id = ? AND device = 'ALL' AND date <= ?
    """, (account_id, common_end)).fetchone()
    camp_total = (camp_row[0] or 0) + (net_row[0] or 0)
    geo_total = geo_row[0] or 0
    delta = geo_total - camp_total
    pct = (delta / camp_total * 100) if camp_total > 0 else 0
    print(f"  Campaign (search + network): ${camp_total:>14,.2f}")
    print(f"  Geo (PHYSICAL+INTEREST):     ${geo_total:>14,.2f}")
    print(f"  Delta:                       ${delta:>+14,.2f}  ({pct:.3f}%)")
    print()

    # Just the asymmetric tail (geo-only days)
    if maxes["geo_performance_daily"] > common_end:
        print(f"Geo-only tail (dates {common_end} < date <= "
              f"{maxes['geo_performance_daily']}):")
        geo_tail = conn.execute("""
            SELECT COALESCE(SUM(cost_micros)/1e6, 0)
            FROM geo_performance_daily
            WHERE account_id = ? AND device = 'ALL' AND date > ?
        """, (account_id, common_end)).fetchone()
        print(f"  Geo spend on geo-only-dates: ${geo_tail[0] or 0:>14,.2f}")
        print()

    print("Interpretation:")
    print("  - If 'Same-window delta' is much smaller than 'Full-history delta',")
    print("    the original gap is mostly date-asymmetry. Refresh will close it.")
    print("  - If 'Same-window delta' is similar to 'Full-history delta',")
    print("    there's structural divergence to investigate before refreshing.")

    conn.close()


if __name__ == "__main__":
    main()
