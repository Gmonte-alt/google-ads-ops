"""
diagnose_pre_jan13.py
---------------------
Investigates why primary_conv_count is non-zero on Jan 10-12
when offline uploads started Jan 13. This should be zero if the
subtraction logic (All conversions - Conversions) is working correctly.

Usage:
    python analysis/diagnose_pre_jan13.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 70)
print("PRE-JAN-13 PRIMARY CONV COUNT INVESTIGATION")
print("=" * 70)

# ------------------------------------------------------------------
# Check 1: What keywords have primary_conv_count > 0 before Jan 13?
# ------------------------------------------------------------------
print("\n[1] Keywords with primary_conv_count > 0 before Jan 13:")
rows = cur.execute("""
    SELECT kpd.report_date,
           ki.keyword_text,
           ki.match_type,
           ki.campaign_name,
           kpd.primary_conv_count,
           kpd.primary_conv_value,
           kpd.secondary_conv_count,
           kpd.all_conv_count
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform       = 'microsoft'
      AND kpd.device       = 'ALL'
      AND kpd.geo_state    = 'ALL'
      AND kpd.report_date  < '2026-01-13'
      AND kpd.primary_conv_count > 0
    ORDER BY kpd.primary_conv_count DESC
    LIMIT 20
""").fetchall()

if rows:
    print(f"  Found {len(rows)} rows with primary_conv_count > 0 before Jan 13")
    print(f"  {'Date':<12} {'primary_cnt':>11} {'primary_val':>12} "
          f"{'secondary':>10} {'all_conv':>9}  Keyword")
    for r in rows:
        print(f"  {r['report_date']:<12} {r['primary_conv_count']:>11.1f} "
              f"${r['primary_conv_value']:>11.2f} "
              f"{r['secondary_conv_count']:>10.1f} "
              f"{r['all_conv_count']:>9.1f}  "
              f"'{r['keyword_text'][:40]}'")
else:
    print("  No rows found -- pre-Jan-13 primary_conv_count is correctly zero")

# ------------------------------------------------------------------
# Check 2: Verify the raw CSV math for those dates
# What was all_conversions vs conversions in the source data?
# all_conv_count stores raw All conversions
# secondary_conv_count stores Conversions (pixel)
# primary_conv_count should = all_conv_count - secondary_conv_count
# ------------------------------------------------------------------
print("\n[2] Verify subtraction math (primary should = all_conv - secondary):")
rows = cur.execute("""
    SELECT kpd.report_date,
           SUM(kpd.primary_conv_count)   AS primary_cnt,
           SUM(kpd.secondary_conv_count) AS secondary_cnt,
           SUM(kpd.all_conv_count)       AS all_conv_cnt,
           SUM(kpd.all_conv_count) - SUM(kpd.secondary_conv_count) AS computed_offline,
           SUM(kpd.primary_conv_count) - (SUM(kpd.all_conv_count) - SUM(kpd.secondary_conv_count)) AS discrepancy
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform       = 'microsoft'
      AND kpd.device       = 'ALL'
      AND kpd.geo_state    = 'ALL'
      AND kpd.report_date  BETWEEN '2026-01-10' AND '2026-01-15'
    GROUP BY kpd.report_date
    ORDER BY kpd.report_date
""").fetchall()

print(f"  {'Date':<12} {'primary':>8} {'secondary':>10} {'all_conv':>9} "
      f"{'computed':>9} {'discrepancy':>12}")
for r in rows:
    flag = " **" if abs(r['discrepancy']) > 0.01 else ""
    print(f"  {r['report_date']:<12} {r['primary_cnt']:>8.1f} "
          f"{r['secondary_cnt']:>10.1f} {r['all_conv_cnt']:>9.1f} "
          f"{r['computed_offline']:>9.1f} {r['discrepancy']:>12.1f}{flag}")

# ------------------------------------------------------------------
# Check 3: Is this a Microsoft data artifact?
# Some offline conversion uploads can be backdated -- the partner may
# upload a conversion that happened on Jan 10 but was uploaded Jan 15+.
# Check if all_conv_count > secondary_conv_count on those dates.
# ------------------------------------------------------------------
print("\n[3] Was all_conv_count > secondary_conv_count before Jan 13?")
print("    (If yes, Microsoft backdated some offline conversions to pre-Jan-13)")
rows = cur.execute("""
    SELECT kpd.report_date,
           SUM(kpd.all_conv_count)       AS all_conv,
           SUM(kpd.secondary_conv_count) AS secondary,
           SUM(kpd.all_conv_count) - SUM(kpd.secondary_conv_count) AS difference
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform       = 'microsoft'
      AND kpd.device       = 'ALL'
      AND kpd.geo_state    = 'ALL'
      AND kpd.report_date  < '2026-01-13'
      AND kpd.all_conv_count > 0
    GROUP BY kpd.report_date
    ORDER BY kpd.report_date DESC
    LIMIT 10
""").fetchall()

if rows:
    print(f"  {'Date':<12} {'all_conv':>9} {'secondary':>10} {'difference':>11}")
    for r in rows:
        flag = " <-- offline backdated" if r['difference'] > 0 else ""
        print(f"  {r['report_date']:<12} {r['all_conv']:>9.1f} "
              f"{r['secondary']:>10.1f} {r['difference']:>11.1f}{flag}")
else:
    print("  No pre-Jan-13 rows with any conversion activity found")

conn.close()
print("\n" + "=" * 70)
