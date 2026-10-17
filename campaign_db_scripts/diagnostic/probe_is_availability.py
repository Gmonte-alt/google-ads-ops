#!/usr/bin/env python
"""
probe_is_availability.py -- READ-ONLY. Answers the Term Life analysis thread's
gaps: which impression-share columns cpd actually carries, whether they hold
real values per account (Home=1 vs Life=29), whether the campaign dimension
retains retired campaigns, and what the change-history table is called.

    python probe_is_availability.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

cols = [r[1] for r in c.execute("PRAGMA table_info(campaign_performance_daily)")]
is_cols = [x for x in cols if any(t in x.lower() for t in
           ("impression_share", "lost_is", "top_is", "_is_", "share"))]
print("1) cpd columns matching IS family:", is_cols if is_cols else "(none)")

print("\n2) per-account population of each IS column (acct, non-null, nonzero, min date, max date):")
for col in is_cols:
    print("   --", col)
    for r in c.execute(
            "SELECT account_id, COUNT(%s), SUM(CASE WHEN %s > 0 THEN 1 ELSE 0 END), "
            "MIN(CASE WHEN %s IS NOT NULL THEN date END), "
            "MAX(CASE WHEN %s IS NOT NULL THEN date END) "
            "FROM campaign_performance_daily WHERE account_id IN (1,29) "
            "AND device='ALL' GROUP BY 1" % (col, col, col, col)):
        print("      ", r)

print("\n3) value shape check (censoring): acct 1 vs 29 share distribution, Sep 2026:")
for col in is_cols[:3]:
    for r in c.execute(
            "SELECT account_id, ROUND(MIN(%s),4), ROUND(AVG(%s),4), ROUND(MAX(%s),4) "
            "FROM campaign_performance_daily WHERE account_id IN (1,29) AND device='ALL' "
            "AND date BETWEEN '2026-09-01' AND '2026-09-23' AND %s IS NOT NULL "
            "GROUP BY 1" % (col, col, col, col)):
        print("   %-38s %s" % (col, r))

print("\n4) campaigns registry: retired campaigns retained? (status census, accts 1 + 29):")
for r in c.execute("SELECT account_id, status, COUNT(*) FROM campaigns "
                   "WHERE account_id IN (1,29) GROUP BY 1,2 ORDER BY 1,2"):
    print("   ", r)

print("\n5) change-history tables present:")
for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                   "(name LIKE '%change%' OR name LIKE '%event%')"):
    n = r[0]
    try:
        acct = c.execute("SELECT COUNT(*) FROM %s WHERE account_id=29" % n).fetchone()[0]
        print("   %-36s acct-29 rows: %s" % (n, format(acct, ",")))
    except sqlite3.Error:
        print("   %-36s (no account_id column or not row-queryable)" % n)
c.close()
