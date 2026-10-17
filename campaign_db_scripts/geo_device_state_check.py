#!/usr/bin/env python
"""
geo_device_state_check.py -- READ-ONLY. Answers the two auto-detect questions
for ggl_rpl_cvr_tracker.py: device rollup pattern, and unattributed-state share.

    python geo_device_state_check.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("geo_search device values (acct 1, all time):")
for r in c.execute("""
    SELECT device, COUNT(*), SUM(impressions)
    FROM campaign_performance_geo_daily_search
    WHERE account_id = 1 GROUP BY device"""):
    print("  ", r)

print("\ngeo_state NULL/blank share since 2026-06-01 (unattrib impr, total impr):")
print("  ", c.execute("""
    SELECT SUM(CASE WHEN geo_state IS NULL OR geo_state = '' THEN impressions ELSE 0 END),
           SUM(impressions)
    FROM campaign_performance_geo_daily_search
    WHERE account_id = 1 AND date >= '2026-06-01'""").fetchone())

print("\ndate coverage:")
print("  ", c.execute("""
    SELECT MIN(date), MAX(date), COUNT(DISTINCT date)
    FROM campaign_performance_geo_daily_search
    WHERE account_id = 1""").fetchone())

c.close()
