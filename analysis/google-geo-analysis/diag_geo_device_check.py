"""
Quick diagnostic — check what device values exist in
campaign_performance_geo_daily for Home Insurance.


    python analysis\google-geo-analysis\diag_geo_device_check.py
    
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("=" * 65)
print("campaign_performance_geo_daily — device value audit")
print(f"Database: {DB_PATH}")
print("=" * 65)

# Total rows regardless of device
total = conn.execute("""
    SELECT COUNT(*) AS n FROM campaign_performance_geo_daily
    WHERE account_id = 1
""").fetchone()
print(f"\nTotal rows (all devices, account_id=1): {total['n']:,}")

# Breakdown by device
print("\nBreakdown by device:")
rows = conn.execute("""
    SELECT device, COUNT(*) AS n, SUM(cost_micros)/1e6 AS spend
    FROM campaign_performance_geo_daily
    WHERE account_id = 1
    GROUP BY device
    ORDER BY n DESC
""").fetchall()

if rows:
    print(f"  {'device':<20} {'rows':>10} {'spend':>14}")
    print(f"  {'─'*20} {'─'*10} {'─'*14}")
    for r in rows:
        print(f"  {str(r['device'] or 'NULL'):<20} {r['n']:>10,} ${r['spend']:>13,.2f}")
else:
    print("  No rows found for account_id=1")

# Also check if the account_id join is the issue
print("\nRow counts by account_id (all accounts):")
acct_rows = conn.execute("""
    SELECT account_id, COUNT(*) AS n
    FROM campaign_performance_geo_daily
    GROUP BY account_id
    ORDER BY n DESC
    LIMIT 10
""").fetchall()
for r in acct_rows:
    print(f"  account_id={r['account_id']}  rows={r['n']:,}")

conn.close()
print("\nDone.")
