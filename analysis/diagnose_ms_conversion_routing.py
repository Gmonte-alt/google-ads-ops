"""
diagnose_ms_conversion_routing.py
----------------------------------
Verifies the Microsoft conversion routing is correct:
  - primary_conv_count  = offline only (zero before Jan 13, positive after)
  - primary_conv_value  = offline revenue (zero before Jan 13, positive after)
  - secondary_conv_count = pixel lead submit (positive throughout January)
  - all_conv_count      = raw All conversions total (pixel + offline)

Usage:
    python analysis/diagnose_ms_conversion_routing.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 70)
print("MICROSOFT CONVERSION ROUTING VERIFICATION")
print("=" * 70)

# ------------------------------------------------------------------
# Check 1: Daily totals around the Jan 13 offline upload start date
# ------------------------------------------------------------------
print("\n[1] Daily conversion totals Jan 10-20 (device='ALL' only):")
print(f"  {'Date':<12} {'primary_cnt':>11} {'primary_val':>12} "
      f"{'secondary_cnt':>13} {'all_conv_cnt':>12}")
print(f"  {'-'*12} {'-'*11} {'-'*12} {'-'*13} {'-'*12}")

rows = cur.execute("""
    SELECT kpd.report_date,
           SUM(kpd.primary_conv_count)   AS primary_cnt,
           SUM(kpd.primary_conv_value)   AS primary_val,
           SUM(kpd.secondary_conv_count) AS secondary_cnt,
           SUM(kpd.all_conv_count)       AS all_conv_cnt
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'microsoft'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN '2026-01-10' AND '2026-01-20'
    GROUP BY kpd.report_date
    ORDER BY kpd.report_date
""").fetchall()

for r in rows:
    flag = " <-- offline uploads start" if r["report_date"] == "2026-01-13" else ""
    print(f"  {r['report_date']:<12} "
          f"{r['primary_cnt']:>11,.1f} "
          f"${r['primary_val']:>11,.2f} "
          f"{r['secondary_cnt']:>13,.1f} "
          f"{r['all_conv_cnt']:>12,.1f}{flag}")

# ------------------------------------------------------------------
# Check 2: Monthly totals -- the headline numbers
# ------------------------------------------------------------------
print("\n[2] Monthly totals (device='ALL', all dates):")
print(f"  {'Month':<10} {'primary_cnt':>11} {'primary_val ($)':>15} "
      f"{'secondary_cnt':>13} {'all_conv_cnt':>12} {'spend ($)':>12}")
print(f"  {'-'*10} {'-'*11} {'-'*15} {'-'*13} {'-'*12} {'-'*12}")

rows = cur.execute("""
    SELECT SUBSTR(kpd.report_date, 1, 7) AS month,
           SUM(kpd.primary_conv_count)   AS primary_cnt,
           SUM(kpd.primary_conv_value)   AS primary_val,
           SUM(kpd.secondary_conv_count) AS secondary_cnt,
           SUM(kpd.all_conv_count)       AS all_conv_cnt,
           SUM(kpd.cost_micros) / 1e6    AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform   = 'microsoft'
      AND kpd.device   = 'ALL'
      AND kpd.geo_state = 'ALL'
    GROUP BY month
    ORDER BY month
""").fetchall()

ms_total_primary = 0
ms_total_revenue = 0
ms_total_spend   = 0
for r in rows:
    print(f"  {r['month']:<10} "
          f"{r['primary_cnt']:>11,.1f} "
          f"${r['primary_val']:>14,.2f} "
          f"{r['secondary_cnt']:>13,.1f} "
          f"{r['all_conv_cnt']:>12,.1f} "
          f"${r['spend']:>11,.2f}")
    ms_total_primary += r['primary_cnt']
    ms_total_revenue += r['primary_val']
    ms_total_spend   += r['spend']

print(f"\n  YTD totals:")
print(f"  Offline conversions (primary_conv_count) : {ms_total_primary:>10,.1f}")
print(f"  Offline revenue (primary_conv_value)     : ${ms_total_revenue:>10,.2f}")
print(f"  Total spend                              : ${ms_total_spend:>10,.2f}")
if ms_total_primary > 0:
    ms_cpl  = ms_total_spend / ms_total_primary
    ms_roas = ms_total_revenue / ms_total_spend if ms_total_spend > 0 else 0
    print(f"  CPL (spend / offline convs)              : ${ms_cpl:>10,.2f}")
    print(f"  ROAS (revenue / spend)                   : {ms_roas:>10.2f}x")

# ------------------------------------------------------------------
# Check 3: Google comparison for the same window (Jan 13 - Mar 29)
# Fair window where both platforms have offline data
# ------------------------------------------------------------------
print("\n[3] Google comparison (Jan 13 - Mar 29, fair revenue window):")
row = cur.execute("""
    SELECT SUM(kpd.primary_conv_count)  AS primary_cnt,
           SUM(kpd.primary_conv_value)  AS primary_val,
           SUM(kpd.cost_micros) / 1e6   AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'google'
      AND a.platform_account_id = '8886502147'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN '2026-01-13' AND '2026-03-29'
""").fetchone()

g_spend   = row['spend']
g_convs   = row['primary_cnt']
g_revenue = row['primary_val']
g_cpl     = g_spend / g_convs if g_convs > 0 else 0
g_roas    = g_revenue / g_spend if g_spend > 0 else 0

print(f"  Google offline conversions : {g_convs:>10,.1f}")
print(f"  Google offline revenue     : ${g_revenue:>10,.2f}")
print(f"  Google spend               : ${g_spend:>10,.2f}")
print(f"  Google CPL                 : ${g_cpl:>10,.2f}")
print(f"  Google ROAS                : {g_roas:>10.2f}x")

# Microsoft same window
row = cur.execute("""
    SELECT SUM(kpd.primary_conv_count)  AS primary_cnt,
           SUM(kpd.primary_conv_value)  AS primary_val,
           SUM(kpd.cost_micros) / 1e6   AS spend
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform            = 'microsoft'
      AND kpd.device            = 'ALL'
      AND kpd.geo_state         = 'ALL'
      AND kpd.report_date BETWEEN '2026-01-13' AND '2026-03-29'
""").fetchone()

m_spend   = row['spend']
m_convs   = row['primary_cnt']
m_revenue = row['primary_val']
m_cpl     = m_spend / m_convs if m_convs > 0 else 0
m_roas    = m_revenue / m_spend if m_spend > 0 else 0

print(f"\n  Microsoft offline conversions : {m_convs:>10,.1f}")
print(f"  Microsoft offline revenue     : ${m_revenue:>10,.2f}")
print(f"  Microsoft spend               : ${m_spend:>10,.2f}")
print(f"  Microsoft CPL                 : ${m_cpl:>10,.2f}")
print(f"  Microsoft ROAS                : {m_roas:>10.2f}x")

print(f"\n  --- Cross-platform comparison (Jan 13 - Mar 29) ---")
print(f"  Google ROAS vs Microsoft ROAS : {g_roas:.2f}x vs {m_roas:.2f}x")
print(f"  Google CPL vs Microsoft CPL   : ${g_cpl:.2f} vs ${m_cpl:.2f}")
spend_ratio = g_spend / m_spend if m_spend > 0 else 0
rev_ratio   = g_revenue / m_revenue if m_revenue > 0 else 0
print(f"  Google/Microsoft spend ratio  : {spend_ratio:.1f}x")
print(f"  Google/Microsoft revenue ratio: {rev_ratio:.1f}x")

conn.close()
print("\n" + "=" * 70)
