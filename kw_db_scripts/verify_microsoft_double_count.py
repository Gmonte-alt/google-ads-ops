"""
verify_microsoft_double_count.py
--------------------------------
Tests whether device='ALL' rows are a true rollup of per-device rows
in Microsoft keyword_performance_daily. If they are, the brand lookup
should use device='ALL' (same as Google), NOT no-filter.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("=" * 78)
    print("Microsoft device='ALL' rollup verification")
    print("=" * 78)

    for account_name in ("Home Insurance", "auto"):
        print(f"\n── {account_name} (microsoft) ──")

        # Sum of device='ALL' rows only
        all_only = cursor.execute("""
            SELECT
                COALESCE(SUM(kpd.cost_micros)/1e6, 0)  AS spend,
                COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue,
                COUNT(*) AS rows
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            JOIN accounts a           ON a.id = ki.account_id
            WHERE a.platform = 'microsoft'
              AND a.account_name = ?
              AND kpd.device = 'ALL'
              AND kpd.report_date >= '2026-01-01'
        """, (account_name,)).fetchone()

        # Sum of per-device rows only (excluding ALL)
        per_device = cursor.execute("""
            SELECT
                COALESCE(SUM(kpd.cost_micros)/1e6, 0)  AS spend,
                COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue,
                COUNT(*) AS rows
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON ki.id = kpd.instance_id
            JOIN accounts a           ON a.id = ki.account_id
            WHERE a.platform = 'microsoft'
              AND a.account_name = ?
              AND kpd.device != 'ALL'
              AND kpd.report_date >= '2026-01-01'
        """, (account_name,)).fetchone()

        print(f"  device='ALL' only       : "
              f"spend=${all_only['spend']:>12,.2f}  "
              f"rev=${all_only['revenue']:>12,.2f}  "
              f"rows={all_only['rows']:>10,}")
        print(f"  per-device only (!=ALL) : "
              f"spend=${per_device['spend']:>12,.2f}  "
              f"rev=${per_device['revenue']:>12,.2f}  "
              f"rows={per_device['rows']:>10,}")

        # Check ratio
        if all_only['spend'] > 0 and per_device['spend'] > 0:
            ratio = per_device['spend'] / all_only['spend']
            print(f"  per-device / ALL ratio  : {ratio:.4f}  "
                  f"(should be 1.0 if ALL is a true rollup)")
            if abs(ratio - 1.0) < 0.01:
                print(f"  → ALL is a TRUE ROLLUP. Use device='ALL' filter.")
            elif abs(ratio - 1.0) < 0.05:
                print(f"  → ALL is approximately a rollup, small drift.")
            else:
                print(f"  → ALL is NOT a clean rollup. Investigate.")

    conn.close()


if __name__ == "__main__":
    main()