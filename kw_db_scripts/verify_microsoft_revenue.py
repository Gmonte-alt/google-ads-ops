"""
verify_microsoft_revenue.py
---------------------------
Verifies Microsoft keyword performance data is loaded correctly,
without the device='ALL' filter that fails on Microsoft keyword data.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("=" * 78)
    print("Microsoft keyword performance — revenue verification")
    print("=" * 78)

    # 1. Distinct device values per Microsoft account (sanity check)
    print("\n[1] Distinct device values in keyword_performance_daily:")
    rows = cursor.execute("""
        SELECT
            a.account_name,
            kpd.device,
            COUNT(*) AS n
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        JOIN accounts a           ON a.id = ki.account_id
        WHERE a.platform = 'microsoft'
        GROUP BY a.id, kpd.device
        ORDER BY a.account_name, kpd.device
    """).fetchall()
    for r in rows:
        print(f"  {r['account_name']:<25} {r['device']:<10} {r['n']:>12,} rows")

    # 2. Account totals from 2026-01-01 forward (no device filter)
    print("\n[2] Microsoft account totals (2026-01-01 onward, all devices):")
    rows = cursor.execute("""
        SELECT
            a.account_name,
            a.platform_account_id,
            SUM(kpd.cost_micros) / 1e6  AS spend,
            SUM(kpd.primary_conv_value) AS revenue,
            COUNT(*)                    AS rows
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        JOIN accounts a           ON a.id = ki.account_id
        WHERE a.platform = 'microsoft'
          AND kpd.report_date >= '2026-01-01'
        GROUP BY a.id
        ORDER BY a.account_name
    """).fetchall()
    for r in rows:
        print(f"  {r['account_name']:<25} {r['platform_account_id']:<12} "
              f"spend=${r['spend']:>12,.2f}  "
              f"rev=${r['revenue']:>12,.2f}  "
              f"rows={r['rows']:>10,}")

    # 3. Same totals for Google for comparison (using device='ALL')
    print("\n[3] Google account totals (2026-01-01 onward, device='ALL'):")
    rows = cursor.execute("""
        SELECT
            a.account_name,
            a.platform_account_id,
            SUM(kpd.cost_micros) / 1e6  AS spend,
            SUM(kpd.primary_conv_value) AS revenue,
            COUNT(*)                    AS rows
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        JOIN accounts a           ON a.id = ki.account_id
        WHERE a.platform = 'google'
          AND kpd.device = 'ALL'
          AND kpd.geo_state = 'ALL'
          AND kpd.report_date >= '2026-01-01'
        GROUP BY a.id
        ORDER BY a.account_name
    """).fetchall()
    for r in rows:
        print(f"  {r['account_name']:<25} {r['platform_account_id']:<12} "
              f"spend=${r['spend']:>12,.2f}  "
              f"rev=${r['revenue']:>12,.2f}  "
              f"rows={r['rows']:>10,}")

    conn.close()
    print("\n" + "=" * 78)
    print("If [2] shows non-zero spend AND revenue for both Microsoft accounts,")
    print("we're cleared to run the multi-platform brand lookup.")
    print("=" * 78)


if __name__ == "__main__":
    main()