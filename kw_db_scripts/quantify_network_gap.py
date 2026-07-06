"""
quantify_network_gap.py
=======================
The network table has ~3.3M fewer rows than kpd. This quantifies the gap and
identifies what the missing instances have in common — specifically whether
they're the old '-NB-SEARCH-ALL-USA-STE' campaign structure vs the new
'_STATE-GROUP_TierN' structure.
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. How many distinct instances are in kpd but have ZERO network rows?
gap = conn.execute("""
    SELECT COUNT(*) AS c FROM (
        SELECT DISTINCT k.instance_id
        FROM keyword_performance_daily k
        WHERE k.device='ALL'
          AND NOT EXISTS (
            SELECT 1 FROM keyword_performance_network_daily n
            WHERE n.instance_id = k.instance_id
          )
    )
""").fetchone()["c"]
total_inst = conn.execute("""
    SELECT COUNT(DISTINCT instance_id) c FROM keyword_performance_daily WHERE device='ALL'
""").fetchone()["c"]
print(f"Instances in kpd with ZERO network rows: {gap:,} of {total_inst:,} "
      f"({100*gap/total_inst:.1f}%)")

# 2. Of those missing instances, what campaign name pattern do they belong to?
print("\nMissing instances by campaign naming pattern:")
rows = conn.execute("""
    SELECT
      CASE
        WHEN ki.campaign_name LIKE '%STATE-GROUP%Tier%' THEN 'NEW tier-group'
        WHEN ki.campaign_name LIKE '%-NB-SEARCH-ALL-USA-STE' THEN 'OLD -NB- structure'
        ELSE 'other: '||substr(ki.campaign_name,1,40)
      END AS pattern,
      COUNT(*) AS n_instances
    FROM keyword_instances ki
    WHERE ki.account_id = 2
      AND EXISTS (SELECT 1 FROM keyword_performance_daily k
                  WHERE k.instance_id = ki.id AND k.device='ALL')
      AND NOT EXISTS (SELECT 1 FROM keyword_performance_network_daily n
                      WHERE n.instance_id = ki.id)
    GROUP BY pattern
    ORDER BY n_instances DESC
""").fetchall()
for r in rows:
    print(f"  {r['pattern']:<45} {r['n_instances']:>6,} instances")

# 3. And the instances that DID get network rows — same breakdown, for contrast
print("\nInstances WITH network rows by campaign pattern (contrast):")
rows = conn.execute("""
    SELECT
      CASE
        WHEN ki.campaign_name LIKE '%STATE-GROUP%Tier%' THEN 'NEW tier-group'
        WHEN ki.campaign_name LIKE '%-NB-SEARCH-ALL-USA-STE' THEN 'OLD -NB- structure'
        ELSE 'other'
      END AS pattern,
      COUNT(DISTINCT ki.id) AS n_instances
    FROM keyword_instances ki
    WHERE ki.account_id = 2
      AND EXISTS (SELECT 1 FROM keyword_performance_network_daily n
                  WHERE n.instance_id = ki.id)
    GROUP BY pattern
    ORDER BY n_instances DESC
""").fetchall()
for r in rows:
    print(f"  {r['pattern']:<45} {r['n_instances']:>6,} instances")

# 4. Row-count gap by report_date — is the gap uniform over time, or only early?
print("\nRow-count gap by month (kpd vs network, device='ALL'):")
rows = conn.execute("""
    SELECT substr(report_date,1,7) AS mon,
           SUM(CASE WHEN src='kpd' THEN 1 ELSE 0 END) AS kpd_rows,
           SUM(CASE WHEN src='net' THEN 1 ELSE 0 END) AS net_rows
    FROM (
      SELECT report_date, 'kpd' AS src FROM keyword_performance_daily WHERE device='ALL'
      UNION ALL
      SELECT report_date, 'net' AS src FROM keyword_performance_network_daily WHERE device='ALL'
    )
    GROUP BY mon ORDER BY mon
""").fetchall()
print(f"  {'month':<9} {'kpd':>10} {'net':>10} {'gap':>10}")
for r in rows:
    gap = r['kpd_rows'] - r['net_rows']
    print(f"  {r['mon']:<9} {r['kpd_rows']:>10,} {r['net_rows']:>10,} {gap:>10,}")

conn.close()
