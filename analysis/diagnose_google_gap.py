"""
diagnose_google_gap.py
-----------------------
Pinpoints exactly why Google data appears missing from Dec 2025 - Feb 2026
in the coverage gap analysis window.

Usage:
    python analysis/diagnose_google_gap.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 65)
print("GOOGLE DATA GAP DIAGNOSTIC")
print("=" * 65)

# ------------------------------------------------------------------
# Check 1: Full date range of ALL Google performance rows
# regardless of device filter
# ------------------------------------------------------------------
print("\n[1] Full Google kpd date range (no device filter):")
row = cur.execute("""
    SELECT COUNT(*)             AS total_rows,
           MIN(kpd.report_date) AS earliest,
           MAX(kpd.report_date) AS latest,
           COUNT(DISTINCT kpd.report_date) AS distinct_dates
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
""").fetchone()
print(f"  Total rows     : {row['total_rows']:,}")
print(f"  Date range     : {row['earliest']} to {row['latest']}")
print(f"  Distinct dates : {row['distinct_dates']:,}")

# ------------------------------------------------------------------
# Check 2: Device values present in the database for Google
# ------------------------------------------------------------------
print("\n[2] Google kpd rows by device value:")
rows = cur.execute("""
    SELECT kpd.device,
           COUNT(*)                         AS rows,
           COUNT(DISTINCT kpd.report_date)  AS distinct_dates,
           MIN(kpd.report_date)             AS earliest,
           MAX(kpd.report_date)             AS latest,
           SUM(kpd.clicks)                  AS clicks,
           SUM(kpd.cost_micros) / 1e6       AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
    GROUP BY kpd.device
    ORDER BY rows DESC
""").fetchall()
for r in rows:
    print(f"  device='{r['device']}'  rows={r['rows']:>8,}  "
          f"dates={r['distinct_dates']:>4}  "
          f"{r['earliest']} to {r['latest']}  "
          f"clicks={r['clicks']:>7,}  spend=${r['spend']:>10,.2f}")

# ------------------------------------------------------------------
# Check 3: Monthly row counts and spend for Google (no device filter)
# to confirm Dec/Jan/Feb data actually exists
# ------------------------------------------------------------------
print("\n[3] Google kpd by month (all devices):")
rows = cur.execute("""
    SELECT SUBSTR(kpd.report_date, 1, 7) AS month,
           kpd.device,
           COUNT(*)                       AS rows,
           SUM(kpd.clicks)                AS clicks,
           SUM(kpd.cost_micros) / 1e6     AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.report_date BETWEEN '2025-12-01' AND '2026-03-31'
    GROUP BY month, kpd.device
    ORDER BY month, kpd.device
""").fetchall()
if rows:
    for r in rows:
        print(f"  {r['month']}  device='{r['device']}'  "
              f"rows={r['rows']:>7,}  clicks={r['clicks']:>6,}  "
              f"spend=${r['spend']:>10,.2f}")
else:
    print("  NO ROWS found for Dec 2025 - Mar 2026")

# ------------------------------------------------------------------
# Check 4: Spot-check -- does January 2026 data exist for Google?
# ------------------------------------------------------------------
print("\n[4] Google data for January 2026 (sample 5 rows, any device):")
rows = cur.execute("""
    SELECT ki.keyword_text, kpd.report_date, kpd.device,
           kpd.clicks, kpd.impressions,
           kpd.cost_micros / 1e6 AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.report_date BETWEEN '2026-01-01' AND '2026-01-31'
      AND kpd.clicks > 0
    LIMIT 5
""").fetchall()
if rows:
    for r in rows:
        print(f"  '{r['keyword_text']}'  {r['report_date']}  "
              f"device={r['device']}  clicks={r['clicks']}  "
              f"spend=${r['spend']:.2f}")
else:
    print("  NO Google rows found for January 2026 with clicks > 0")
    print("  This confirms the backfill did not capture Jan/Feb data.")

# ------------------------------------------------------------------
# Check 5: What is the actual earliest Google date in the database?
# ------------------------------------------------------------------
print("\n[5] Earliest and latest Google dates by device:")
rows = cur.execute("""
    SELECT kpd.device,
           MIN(kpd.report_date) AS earliest,
           MAX(kpd.report_date) AS latest
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform = 'google'
      AND a.platform_account_id = '8886502147'
    GROUP BY kpd.device
""").fetchall()
for r in rows:
    print(f"  device='{r['device']}'  {r['earliest']} to {r['latest']}")

conn.close()
print("\n" + "=" * 65)
print("If January 2026 shows no rows in Check 4, the Google backfill")
print("only captured recent data. Re-run 03_sync_keyword_performance.py")
print("with --backfill to pull the full 12-month history.")
print("=" * 65)
