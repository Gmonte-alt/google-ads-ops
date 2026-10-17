#!/usr/bin/env python
"""
cpd_kpd_gap_localizer.py -- READ-ONLY. Localizes the campaign-vs-keyword
spend gap: which campaigns, which days, and whether any campaign has spend
with NO keyword rows at all (keywordless spend, e.g. DSA).

    python cpd_kpd_gap_localizer.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
START, END = "2026-07-29", "2026-08-27"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("gap by campaign (cpd spend - kpd spend), %s..%s:" % (START, END))
q = """
WITH cs AS (
  SELECT campaign_id, SUM(cost_micros)/1e6 AS spend
  FROM campaign_performance_daily_search
  WHERE account_id=1 AND device='ALL' AND date BETWEEN ? AND ?
  GROUP BY campaign_id),
ks AS (
  SELECT p.campaign_id, SUM(p.cost_micros)/1e6 AS spend
  FROM keyword_performance_daily p
  JOIN keyword_instances k ON k.id = p.instance_id
  WHERE k.account_id=1 AND p.device='ALL' AND p.report_date BETWEEN ? AND ?
  GROUP BY p.campaign_id)
SELECT substr(ca.campaign_name,1,44), ROUND(cs.spend,0), ROUND(COALESCE(ks.spend,0),0),
       ROUND(cs.spend - COALESCE(ks.spend,0),2) AS gap,
       CASE WHEN ks.campaign_id IS NULL THEN 'NO KPD ROWS AT ALL' ELSE '' END
FROM cs LEFT JOIN ks ON ks.campaign_id = cs.campaign_id
JOIN campaigns ca ON ca.campaign_id = cs.campaign_id AND ca.account_id = 1
ORDER BY gap DESC LIMIT 15
"""
for r in c.execute(q, (START, END, START, END)):
    print("  ", r)

print("\ntop campaign's gap by day (largest-gap campaign):")
row = c.execute("""
WITH cs AS (
  SELECT campaign_id, SUM(cost_micros)/1e6 s FROM campaign_performance_daily_search
  WHERE account_id=1 AND device='ALL' AND date BETWEEN ? AND ? GROUP BY 1),
ks AS (
  SELECT p.campaign_id, SUM(p.cost_micros)/1e6 s FROM keyword_performance_daily p
  JOIN keyword_instances k ON k.id=p.instance_id
  WHERE k.account_id=1 AND p.device='ALL' AND p.report_date BETWEEN ? AND ? GROUP BY 1)
SELECT cs.campaign_id FROM cs LEFT JOIN ks USING (campaign_id)
ORDER BY cs.s - COALESCE(ks.s,0) DESC LIMIT 1""", (START, END, START, END)).fetchone()
if row:
    cid = row[0]
    for r in c.execute("""
        SELECT d.date, ROUND(d.s,2), ROUND(COALESCE(k2.s,0),2), ROUND(d.s-COALESCE(k2.s,0),2)
        FROM (SELECT date, SUM(cost_micros)/1e6 s FROM campaign_performance_daily_search
              WHERE campaign_id=? AND device='ALL' AND date BETWEEN ? AND ? GROUP BY 1) d
        LEFT JOIN (SELECT report_date, SUM(cost_micros)/1e6 s FROM keyword_performance_daily
              WHERE campaign_id=? AND device='ALL' AND report_date BETWEEN ? AND ? GROUP BY 1) k2
          ON k2.report_date = d.date
        WHERE d.s - COALESCE(k2.s,0) > 1 ORDER BY d.date""",
        (cid, START, END, cid, START, END)):
        print("  ", r)
c.close()
print("\nread: gap concentrated on recent days = adds since last kw02 (self-heals");
print("on next 02+03); 'NO KPD ROWS AT ALL' = keywordless spend (DSA-class,")
print("permanent+benign, document it); gap spread thin everywhere = timing skew.")
