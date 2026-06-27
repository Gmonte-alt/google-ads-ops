"""
spot_check_2025_impressions.py
==============================
Quick spot check after the 2025 campaign performance re-backfill:
prints monthly Audience impressions for 2025 to confirm the comma-bug
fix took effect.

Run:
    python spot_check_2025_impressions.py

Pre-fix expectation: very low or zero impressions per month (the bug
was zeroing out high-volume Audience rows).
Post-fix expectation: hundreds of thousands to millions of impressions
per month, with a smooth progression over the year.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2  # Microsoft Home Insurance


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)

    print("=" * 70)
    print("2025 Audience impressions by month (device='ALL', network='Audience')")
    print("=" * 70)
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', date) AS month,
               SUM(impressions)         AS imp,
               SUM(clicks)              AS clicks,
               SUM(cost_micros) / 1e6   AS spend
        FROM campaign_performance_network_daily
        WHERE account_id = ?
          AND network    = 'Audience'
          AND device     = 'ALL'
          AND date BETWEEN '2025-01-01' AND '2025-12-31'
        GROUP BY month
        ORDER BY month
        """,
        (ACCOUNT_ID,),
    ).fetchall()

    if not rows:
        print("  No rows found.")
    else:
        print(f"  {'Month':<10} {'Impressions':>15} {'Clicks':>12} {'Spend':>14}")
        print(f"  {'-'*10} {'-'*15} {'-'*12} {'-'*14}")
        total_imp = 0
        total_clk = 0
        total_spend = 0
        for month, imp, clk, spend in rows:
            print(f"  {month:<10} {imp or 0:>15,} {clk or 0:>12,} ${spend or 0:>12,.2f}")
            total_imp += imp or 0
            total_clk += clk or 0
            total_spend += spend or 0
        print(f"  {'-'*10} {'-'*15} {'-'*12} {'-'*14}")
        print(f"  {'TOTAL':<10} {total_imp:>15,} {total_clk:>12,} ${total_spend:>12,.2f}")
        print()
        if total_imp > 0:
            ctr = total_clk / total_imp * 100
            print(f"  CTR (2025 full year, Audience): {ctr:.3f}%")
            print(f"  (Expected ~0.20-0.30% for Audience network)")

    print()
    print("=" * 70)
    print("2025 Search impressions by month (device='ALL', search bucket)")
    print("=" * 70)
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', date) AS month,
               SUM(impressions)         AS imp,
               SUM(clicks)              AS clicks,
               SUM(cost_micros) / 1e6   AS spend
        FROM campaign_performance_daily
        WHERE account_id = ?
          AND device     = 'ALL'
          AND date BETWEEN '2025-01-01' AND '2025-12-31'
        GROUP BY month
        ORDER BY month
        """,
        (ACCOUNT_ID,),
    ).fetchall()

    if not rows:
        print("  No rows found.")
    else:
        print(f"  {'Month':<10} {'Impressions':>15} {'Clicks':>12} {'Spend':>14}")
        print(f"  {'-'*10} {'-'*15} {'-'*12} {'-'*14}")
        total_imp = 0
        total_clk = 0
        total_spend = 0
        for month, imp, clk, spend in rows:
            print(f"  {month:<10} {imp or 0:>15,} {clk or 0:>12,} ${spend or 0:>12,.2f}")
            total_imp += imp or 0
            total_clk += clk or 0
            total_spend += spend or 0
        print(f"  {'-'*10} {'-'*15} {'-'*12} {'-'*14}")
        print(f"  {'TOTAL':<10} {total_imp:>15,} {total_clk:>12,} ${total_spend:>12,.2f}")
        print()
        if total_imp > 0:
            ctr = total_clk / total_imp * 100
            print(f"  CTR (2025 full year, Search): {ctr:.3f}%")
            print(f"  (Expected ~2-4% for Search)")

    conn.close()


if __name__ == "__main__":
    main()
