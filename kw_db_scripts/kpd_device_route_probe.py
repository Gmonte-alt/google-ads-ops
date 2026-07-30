#!/usr/bin/env python
"""
kpd_device_route_probe.py -- READ-ONLY. The refresh wrote 15,222 Pass-2 device
rows for July but the campaigns-join probe sees none. This checks: do the rows
exist via the INSTANCE route, and is campaign_id populated on them?

    python kpd_device_route_probe.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
args = ap.parse_args()
c = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)

print("=" * 68)
print("A) July 2026 kpd rows, INSTANCE route (acct 1)")
print("=" * 68)
for r in c.execute("""
    SELECT p.device, COUNT(*), ROUND(SUM(p.primary_conv_count),1)
    FROM keyword_performance_daily p
    JOIN keyword_instances ki ON ki.id = p.instance_id
    WHERE ki.account_id = 1 AND p.report_date >= '2026-07-01'
    GROUP BY p.device"""):
    print("  ", r)

print("\n" + "=" * 68)
print("B) July 2026 kpd rows, campaign_id population by device (whole table)")
print("=" * 68)
for r in c.execute("""
    SELECT device, COUNT(*),
           SUM(CASE WHEN campaign_id IS NULL OR campaign_id = '' THEN 1 ELSE 0 END) AS null_campaign
    FROM keyword_performance_daily
    WHERE report_date >= '2026-07-01'
    GROUP BY device"""):
    print("  ", r)

print("\n" + "=" * 68)
print("C) Same, via the workbook's campaigns join (acct 1)")
print("=" * 68)
for r in c.execute("""
    SELECT p.device, COUNT(*)
    FROM keyword_performance_daily p
    JOIN campaigns ca ON ca.campaign_id = p.campaign_id
    WHERE ca.account_id = 1 AND p.report_date >= '2026-07-01'
    GROUP BY p.device"""):
    print("  ", r)

c.close()
print("\nRead: if A shows MOBILE rows but C doesn't, the sync's device upsert")
print("isn't writing campaign_id (B shows the NULL count) -- fix is in 03's")
print("Pass-2 upsert; workbook workaround is joining via instances instead.")
