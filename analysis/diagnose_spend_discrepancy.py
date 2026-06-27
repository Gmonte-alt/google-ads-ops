"""
diagnose_spend_discrepancy.py
------------------------------
Reconciles the $277K expected spend vs $85.9K showing in the analysis.
Checks whether device='ALL' rows correctly capture total spend or
whether Pass 1 and Pass 2 rows are out of sync.

Usage:
    python analysis/diagnose_spend_discrepancy.py
"""

import sqlite3
from pathlib import Path
from datetime import date

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
START_DATE = date(2026, 1, 1)
END_DATE   = date(2026, 3, 24)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 65)
print("SPEND RECONCILIATION DIAGNOSTIC")
print(f"Window: {START_DATE} to {END_DATE}")
print("=" * 65)

# ------------------------------------------------------------------
# Check 1: Spend by device for Google in window
# ------------------------------------------------------------------
print("\n[1] Google spend by device in window:")
rows = cur.execute("""
    SELECT kpd.device,
           SUM(kpd.clicks)            AS clicks,
           SUM(kpd.impressions)       AS impressions,
           SUM(kpd.cost_micros) / 1e6 AS spend,
           SUM(kpd.primary_conv_count) AS conversions,
           SUM(kpd.primary_conv_value) AS revenue,
           COUNT(DISTINCT kpd.report_date) AS dates
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY kpd.device
    ORDER BY spend DESC
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchall()

total_all_device = 0
total_sum_devices = 0
for r in rows:
    print(f"  device='{r['device']}'  spend=${r['spend']:>12,.2f}  "
          f"clicks={r['clicks']:>7,}  convs={r['conversions']:>8,.1f}  "
          f"revenue=${r['revenue']:>12,.2f}  dates={r['dates']}")
    if r['device'] == 'ALL':
        total_all_device = r['spend']
    else:
        total_sum_devices += r['spend']

print(f"\n  device='ALL' total          : ${total_all_device:>12,.2f}")
print(f"  Sum of MOBILE+DESKTOP+TABLET: ${total_sum_devices:>12,.2f}")
ratio = total_all_device / total_sum_devices if total_sum_devices > 0 else 0
print(f"  Ratio (ALL / sum of devices): {ratio:.4f}")
print()
if abs(ratio - 1.0) > 0.01:
    print("  *** MISMATCH: device='ALL' does not equal sum of device rows ***")
    print("  This means Pass 1 (ALL) and Pass 2 (device) captured different")
    print("  traffic. The analysis uses device='ALL' which is understating spend.")
else:
    print("  Pass 1 and Pass 2 totals reconcile correctly.")

# ------------------------------------------------------------------
# Check 2: Sample reconciliation for a single high-spend keyword
# ------------------------------------------------------------------
print("\n[2] Reconciliation for top keyword by device='ALL' spend:")
top_kw = cur.execute("""
    SELECT ki.keyword_text, ki.match_type,
           SUM(kpd.cost_micros) / 1e6 AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY ki.id
    ORDER BY spend DESC
    LIMIT 1
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchone()

if top_kw:
    print(f"  Keyword: '{top_kw['keyword_text']}' [{top_kw['match_type']}]")
    rows = cur.execute("""
        SELECT kpd.device,
               SUM(kpd.clicks)            AS clicks,
               SUM(kpd.cost_micros) / 1e6 AS spend,
               SUM(kpd.primary_conv_count) AS convs
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform            = 'google'
          AND a.platform_account_id = '8886502147'
          AND ki.keyword_text       = ?
          AND ki.match_type         = ?
          AND kpd.geo_state         = 'ALL'
          AND kpd.report_date BETWEEN ? AND ?
        GROUP BY kpd.device
        ORDER BY kpd.device
    """, (top_kw['keyword_text'], top_kw['match_type'],
          START_DATE.isoformat(), END_DATE.isoformat())).fetchall()
    for r in rows:
        print(f"    device='{r['device']}'  spend=${r['spend']:>10,.2f}  "
              f"clicks={r['clicks']:>5,}  convs={r['convs']:>6,.1f}")

# ------------------------------------------------------------------
# Check 3: What does the full 12-month Google spend look like?
# Cross-reference against the $277K figure
# ------------------------------------------------------------------
print("\n[3] Google spend by month (device='ALL' only) -- full backfill:")
rows = cur.execute("""
    SELECT SUBSTR(kpd.report_date, 1, 7) AS month,
           SUM(kpd.cost_micros) / 1e6    AS spend,
           SUM(kpd.clicks)               AS clicks,
           SUM(kpd.primary_conv_count)   AS conversions,
           SUM(kpd.primary_conv_value)   AS revenue
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
    GROUP BY month
    ORDER BY month
""").fetchall()

ytd_spend = 0
for r in rows:
    ytd = " <-- YTD" if r['month'] >= '2026-01' else ""
    print(f"  {r['month']}  spend=${r['spend']:>10,.2f}  "
          f"clicks={r['clicks']:>6,}  convs={r['conversions']:>7,.1f}  "
          f"revenue=${r['revenue']:>10,.2f}{ytd}")
    if r['month'] >= '2026-01':
        ytd_spend += r['spend']

print(f"\n  YTD spend (Jan-Mar, device='ALL'): ${ytd_spend:,.2f}")
print(f"  Expected per your records        : $277,000")
print(f"  Gap                              : ${277000 - ytd_spend:,.2f}")

# ------------------------------------------------------------------
# Check 4: Full spend including all devices (no ALL filter)
# ------------------------------------------------------------------
print("\n[4] Google YTD spend summing ALL device rows (no dedup):")
row = cur.execute("""
    SELECT SUM(kpd.cost_micros) / 1e6 AS spend,
           SUM(kpd.clicks)             AS clicks
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.report_date BETWEEN ? AND ?
""", (START_DATE.isoformat(), END_DATE.isoformat())).fetchone()
print(f"  Total all rows (incl device+ALL): ${row['spend']:,.2f}  clicks={row['clicks']:,}")
print(f"  NOTE: This double-counts -- device rows + ALL rows sum together")

conn.close()
print("\n" + "=" * 65)
