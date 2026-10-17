#!/usr/bin/env python
"""
geo_perf_daily_inspect.py -- READ-ONLY. Inspects geo_performance_daily,
the table the cross-platform probe just surfaced.

    python geo_perf_daily_inspect.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("columns:")
cols = [r[1] for r in c.execute("PRAGMA table_info(geo_performance_daily)")]
print("  ", cols)

print("\naccount spread + span:")
for r in c.execute("""SELECT account_id, COUNT(*), MIN(date), MAX(date)
                      FROM geo_performance_daily GROUP BY account_id"""):
    print("  ", r)

if "device" in cols:
    print("\nacct-2 device values:")
    for r in c.execute("""SELECT device, COUNT(*) FROM geo_performance_daily
                          WHERE account_id=2 GROUP BY device"""):
        print("  ", r)

if "network" in cols:
    print("\nacct-2 network values:")
    for r in c.execute("""SELECT network, COUNT(*) FROM geo_performance_daily
                          WHERE account_id=2 GROUP BY network"""):
        print("  ", r)

state_col = next((x for x in cols if "state" in x.lower()), None)
if state_col:
    print("\nacct-2 sample states (%s):" % state_col)
    for r in c.execute("""SELECT %s, COUNT(*) FROM geo_performance_daily
                          WHERE account_id=2 GROUP BY 1 ORDER BY 2 DESC LIMIT 10""" % state_col):
        print("  ", r)
    print("\nacct-2 monthly coverage 2026 (rows, cost, conv, all_conv_value if present):")
    money = "SUM(cost_micros)/1e6" if "cost_micros" in cols else "SUM(cost)"
    acv = ", ROUND(SUM(all_conversion_value),0)" if "all_conversion_value" in cols else ""
    for r in c.execute("""SELECT substr(date,1,7), COUNT(*), ROUND(%s,0),
                          ROUND(SUM(conversions),1)%s
                          FROM geo_performance_daily
                          WHERE account_id=2 AND date >= '2026-01-01'
                          GROUP BY 1 ORDER BY 1""" % (money, acv)):
        print("  ", r)

c.close()
