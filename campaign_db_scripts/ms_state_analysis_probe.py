#!/usr/bin/env python
"""
ms_state_analysis_probe.py -- READ-ONLY. Verifies the four facts the
cross-platform state analysis needs on the Microsoft side.

    python ms_state_analysis_probe.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("1) any geo/location/state table at all?")
rows = c.execute("""SELECT name FROM sqlite_master WHERE type='table'
    AND (instr(lower(name),'geo') OR instr(lower(name),'location')
         OR instr(lower(name),'state') OR instr(lower(name),'region'))""").fetchall()
for r in rows:
    print("  ", r[0])

print("\n2) acct-2 campaign_performance_daily coverage by month (rows, cost):")
for r in c.execute("""SELECT substr(date,1,7), COUNT(*), ROUND(SUM(cost_micros)/1e6,0)
    FROM campaign_performance_daily WHERE account_id=2
    GROUP BY 1 ORDER BY 1"""):
    print("  ", r)

print("\n3) acct-2 device values (ALL rollup vs splits?):")
for r in c.execute("""SELECT device, COUNT(*) FROM campaign_performance_daily
    WHERE account_id=2 GROUP BY device"""):
    print("  ", r)

print("\n4) March-May acct-2 campaign names (state-parseable?):")
for r in c.execute("""SELECT DISTINCT c2.campaign_name
    FROM campaign_performance_daily p JOIN campaigns c2
      ON c2.campaign_id = p.campaign_id AND c2.account_id = p.account_id
    WHERE p.account_id=2 AND p.date BETWEEN '2026-03-01' AND '2026-05-31'
    ORDER BY 1 LIMIT 40"""):
    print("  ", r[0][:70])

print("\n5) network table shape:")
try:
    print("   columns:", [r[1] for r in c.execute("PRAGMA table_info(campaign_performance_network_daily)")])
    print("   acct-2 span:", c.execute("""SELECT MIN(date), MAX(date), COUNT(*)
        FROM campaign_performance_network_daily WHERE account_id=2""").fetchone())
except sqlite3.Error as e:
    print("   (table missing or differently named: %s)" % e)

c.close()
