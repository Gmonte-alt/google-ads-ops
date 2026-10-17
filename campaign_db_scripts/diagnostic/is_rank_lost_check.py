#!/usr/bin/env python
"""
is_rank_lost_check.py -- READ-ONLY. Confirms whether Lost IS (rank) data is
real or floor-censored, at campaign grain.

    python is_rank_lost_check.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("1) does campaign_performance_daily carry IS columns natively?")
cpd_cols = [r[1] for r in c.execute("PRAGMA table_info(campaign_performance_daily)")]
is_cols = [x for x in cpd_cols if "impression_share" in x or "lost_is" in x]
print("   IS-ish columns on cpd:", is_cols if is_cols else "NONE -- ad-group table is the only IS source")

print("\n2) campaign-grain monthly (impression-weighted from ad_group_performance_daily),")
print("   Jan-Aug 2026, acct 1  [IS weighting = proxy, documented]:")
q = """
SELECT substr(a.date,1,7) AS ym, substr(a.campaign_name,1,38) AS camp,
       ROUND(SUM(a.search_impression_share * a.impressions) /
             NULLIF(SUM(CASE WHEN a.search_impression_share IS NOT NULL
                             THEN a.impressions END),0), 4) AS iw_is,
       MIN(a.search_impression_share) AS min_is,
       MAX(a.search_impression_share) AS max_is,
       SUM(CASE WHEN a.search_rank_lost_is IS NOT NULL
                 AND a.search_rank_lost_is > 0 THEN 1 ELSE 0 END) AS rank_rows_nonzero,
       SUM(CASE WHEN a.search_budget_lost_is IS NOT NULL
                 AND a.search_budget_lost_is > 0 THEN 1 ELSE 0 END) AS budget_rows_nonzero,
       COUNT(*) AS rows
FROM ad_group_performance_daily a
WHERE a.account_id = 1 AND a.device = 'ALL'
  AND a.date BETWEEN '2026-01-01' AND '2026-08-31'
GROUP BY 1, 2 ORDER BY 1, 2
"""
for r in c.execute(q):
    print("  ", r)

print("\n3) THE CONTRAST -- same pull for 2025 Q1 (pre-collapse, higher-share era):")
for r in c.execute(q.replace("2026-01-01", "2025-01-01").replace("2026-08-31", "2025-03-31")):
    print("  ", r)

c.close()
print("\nread: 2026 floors (~0.10) + zero rank rows AND 2025 showing real values")
print("= censoring driven by share falling under Google's floor (confirmed).")
print("2025 ALSO floors/zeros = data never populated at any health; different story.")
