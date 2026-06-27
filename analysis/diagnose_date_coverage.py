"""
diagnose_date_coverage.py
--------------------------
Validates data completeness for both platforms across the 90-day
analysis window used in analysis_coverage_gap.py.

Usage:
    python analysis/diagnose_date_coverage.py
"""

import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
END_DATE   = date(2026, 3, 23)
START_DATE = END_DATE - timedelta(days=90)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 65)
print("DATA COVERAGE DIAGNOSTIC")
print(f"Analysis window: {START_DATE} to {END_DATE} (90 days)")
print("=" * 65)

# ------------------------------------------------------------------
# Check 1: How many distinct dates exist per platform in the window
# ------------------------------------------------------------------
print("\n[1] Distinct dates with performance data in the 90-day window:")
rows = cur.execute("""
    SELECT a.platform,
           a.platform_account_id,
           COUNT(DISTINCT kpd.report_date) AS date_count,
           MIN(kpd.report_date)            AS earliest,
           MAX(kpd.report_date)            AS latest,
           SUM(kpd.clicks)                 AS total_clicks,
           SUM(kpd.impressions)            AS total_impressions,
           SUM(kpd.cost_micros) / 1e6      AS total_spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE kpd.device    = 'ALL'
      AND kpd.geo_state = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY a.id
    ORDER BY a.platform
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchall()

expected_days = 90
for r in rows:
    missing = expected_days - r['date_count']
    status = "COMPLETE" if missing == 0 else f"MISSING {missing} days"
    print(f"\n  {r['platform']:<12} {r['platform_account_id']}")
    print(f"  Dates with data : {r['date_count']} of {expected_days} expected  [{status}]")
    print(f"  Date range      : {r['earliest']} to {r['latest']}")
    print(f"  Total clicks    : {r['total_clicks']:,}")
    print(f"  Total impressions: {r['total_impressions']:,}")
    print(f"  Total spend     : ${r['total_spend']:,.2f}")

# ------------------------------------------------------------------
# Check 2: Identify specific missing dates for Google
# ------------------------------------------------------------------
print("\n[2] Missing dates in the window for Google (device=ALL):")
existing_dates = set(r[0] for r in cur.execute("""
    SELECT DISTINCT kpd.report_date
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchall())

all_dates = set(
    (START_DATE + timedelta(days=i)).isoformat()
    for i in range(91)
)
missing_dates = sorted(all_dates - existing_dates)
if missing_dates:
    print(f"  {len(missing_dates)} missing dates:")
    for d in missing_dates:
        print(f"    {d}")
else:
    print("  No missing dates -- Google data is complete for the window.")

# ------------------------------------------------------------------
# Check 3: Same check for Microsoft
# ------------------------------------------------------------------
print("\n[3] Missing dates in the window for Microsoft (device=ALL):")
existing_ms = set(r[0] for r in cur.execute("""
    SELECT DISTINCT kpd.report_date
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'microsoft'
      AND a.platform_account_id = 'F149SN63'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchall())

missing_ms = sorted(all_dates - existing_ms)
if missing_ms:
    print(f"  {len(missing_ms)} missing dates:")
    for d in missing_ms:
        print(f"    {d}")
else:
    print("  No missing dates -- Microsoft data is complete for the window.")

# ------------------------------------------------------------------
# Check 4: Weekly spend totals to spot any zero-traffic weeks
# ------------------------------------------------------------------
print("\n[4] Weekly spend by platform in the 90-day window:")
rows = cur.execute("""
    SELECT a.platform,
           STRFTIME('%Y-W%W', kpd.report_date) AS week,
           SUM(kpd.cost_micros) / 1e6          AS spend,
           SUM(kpd.clicks)                      AS clicks
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE kpd.device    = 'ALL'
      AND kpd.geo_state = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY a.platform, week
    ORDER BY a.platform, week
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchall()

current_platform = None
for r in rows:
    if r['platform'] != current_platform:
        current_platform = r['platform']
        print(f"\n  {current_platform}:")
    flag = "  <-- zero spend" if r['spend'] == 0 else ""
    print(f"    {r['week']}  spend=${r['spend']:>10,.2f}  clicks={r['clicks']:>6,}{flag}")

conn.close()
print("\n" + "=" * 65)
