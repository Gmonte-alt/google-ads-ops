#!/usr/bin/env python
"""
termlife_acceptance.py -- READ-ONLY. Final integration scorecard for acct 29:
every table's coverage, the chunk-1 device check (03's Ctrl-C), and routed
totals. One run closes the greenfield.

    python termlife_acceptance.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
A = 29

print("=" * 66)
print("TERM LIFE (acct 29) INTEGRATION ACCEPTANCE")
print("=" * 66)

print("\n1) coverage spans:")
for name, q in [
    ("campaign_performance_daily", "SELECT MIN(date), MAX(date), COUNT(*) FROM campaign_performance_daily WHERE account_id=%d" % A),
    ("campaign_performance_geo_daily", "SELECT MIN(date), MAX(date), COUNT(*) FROM campaign_performance_geo_daily WHERE account_id=%d" % A),
    ("ad_group_performance_daily", "SELECT MIN(date), MAX(date), COUNT(*) FROM ad_group_performance_daily WHERE account_id=%d" % A),
    ("keyword_instances", "SELECT MIN(first_seen_date), MAX(last_seen_date), COUNT(*) FROM keyword_instances WHERE account_id=%d" % A),
    ("keyword_performance_daily", "SELECT MIN(p.report_date), MAX(p.report_date), COUNT(*) FROM keyword_performance_daily p JOIN keyword_instances k ON k.id=p.instance_id WHERE k.account_id=%d" % A),
    ("keyword_quality_daily", "SELECT MIN(q.report_date), MAX(q.report_date), COUNT(*) FROM keyword_quality_daily q JOIN keyword_instances k ON k.id=q.instance_id WHERE k.account_id=%d" % A),
    ("search_query_performance", "SELECT MIN(p.report_date), MAX(p.report_date), COUNT(*) FROM search_query_performance p JOIN campaigns ca ON ca.campaign_id=p.campaign_id AND ca.account_id=%d" % A),
]:
    print("   %-32s %s" % (name, c.execute(q).fetchone()))

print("\n2) chunk-1 device check (03 Ctrl-C on Oct-2024 Pass 2):")
rows = c.execute("""SELECT substr(p.report_date,1,7), p.device, COUNT(*)
    FROM keyword_performance_daily p JOIN keyword_instances k ON k.id=p.instance_id
    WHERE k.account_id=%d AND p.report_date < '2024-12-01'
    GROUP BY 1,2 ORDER BY 1,2""" % A).fetchall()
for r in rows:
    print("   ", r)
oct_devs = {r[1] for r in rows if r[0] == "2024-10"}
if {"DESKTOP", "MOBILE"} <= oct_devs:
    print("   -> PASS: Oct-2024 has device splits; the interrupted chunk healed on re-run.")
else:
    print("   -> HEAL NEEDED: Oct-2024 lacks device rows. Run:")
    print("      python kw_db_scripts\\03_sync_keyword_performance.py --account term_life --backfill --start-date 2024-10-01 --end-date 2024-10-31")

print("\n3) routed truth, hourly era (2025-08 onward), kpd vs cpd-native:")
r = c.execute("""SELECT ROUND(SUM(p.cost_micros)/1e6,0), ROUND(SUM(p.primary_conv_count),0),
    ROUND(SUM(p.primary_conv_value),0)
    FROM keyword_performance_daily p JOIN keyword_instances k ON k.id=p.instance_id
    WHERE k.account_id=%d AND p.device='ALL' AND p.report_date >= '2025-08-01'""" % A).fetchone()
n = c.execute("""SELECT ROUND(SUM(conversions),0) FROM campaign_performance_daily
    WHERE account_id=%d AND device='ALL' AND date >= '2025-08-01'""" % A).fetchone()[0]
print("   kpd: cost %s | routed leads %s | routed value %s || cpd native conv %s"
      % (r[0], r[1], r[2], n))

print("\n4) sqp query universe:", c.execute(
    "SELECT COUNT(DISTINCT p.query_id) FROM search_query_performance p "
    "JOIN campaigns ca ON ca.campaign_id=p.campaign_id AND ca.account_id=%d" % A).fetchone()[0])
c.close()
print("\nAll sections green = Term Life fully instrumented.")
