#!/usr/bin/env python
"""
purge_termlife_perf.py -- deletes acct-29 campaign-grain performance rows so
the full-history re-pull (2024-10-01 floor) lands on clean, uniform provenance.
Structure tables (campaigns, ad_groups) untouched.

    python purge_termlife_perf.py            (dry run: counts only)
    python purge_termlife_perf.py --apply
"""
import argparse
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
A = 29
TABLES = ["campaign_performance_daily", "campaign_performance_geo_daily",
          "campaign_performance_hourly", "ad_group_performance_daily"]

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(DB)
print("[PLAN] acct-%d rows per table:" % A)
for t in TABLES:
    try:
        n = conn.execute("SELECT COUNT(*) FROM %s WHERE account_id=?" % t, (A,)).fetchone()[0]
        print("   %-36s %s" % (t, format(n, ",")))
    except sqlite3.Error as e:
        print("   %-36s (skip: %s)" % (t, e))
if not args.apply:
    print("[DRY RUN] nothing deleted.")
else:
    for t in TABLES:
        try:
            cur = conn.execute("DELETE FROM %s WHERE account_id=?" % t, (A,))
            print("[DELETE] %-36s %s rows" % (t, format(cur.rowcount, ",")))
        except sqlite3.Error as e:
            print("[SKIP]   %-36s %s" % (t, e))
    conn.commit()
    print("[APPLY] done. Re-pull with the trimmed .env per the plan.")
conn.close()
