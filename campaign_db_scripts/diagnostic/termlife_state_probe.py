#!/usr/bin/env python
"""
termlife_state_probe.py -- READ-ONLY. Full state inventory for account_id=29
(Term Life, customer 1671155176) ahead of the sync revival.

    python termlife_state_probe.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
A = 29

print("1) per-table coverage for acct %d:" % A)
checks = [
    ("campaigns",                        "SELECT COUNT(*), NULL, NULL FROM campaigns WHERE account_id=%d" % A),
    ("campaign_performance_daily",       "SELECT COUNT(*), MIN(date), MAX(date) FROM campaign_performance_daily WHERE account_id=%d" % A),
    ("campaign_performance_geo_daily",   "SELECT COUNT(*), MIN(date), MAX(date) FROM campaign_performance_geo_daily WHERE account_id=%d" % A),
    ("ad_group_performance_daily",       "SELECT COUNT(*), MIN(date), MAX(date) FROM ad_group_performance_daily WHERE account_id=%d" % A),
    ("ad_groups",                        "SELECT COUNT(*), NULL, NULL FROM ad_groups WHERE account_id=%d" % A),
    ("keyword_instances",                "SELECT COUNT(*), MIN(first_seen_date), MAX(last_seen_date) FROM keyword_instances WHERE account_id=%d" % A),
    ("keyword_performance_daily (via ki)","SELECT COUNT(*), MIN(p.report_date), MAX(p.report_date) FROM keyword_performance_daily p JOIN keyword_instances k ON k.id=p.instance_id WHERE k.account_id=%d" % A),
    ("keyword_quality_daily (via ki)",   "SELECT COUNT(*), MIN(q.report_date), MAX(q.report_date) FROM keyword_quality_daily q JOIN keyword_instances k ON k.id=q.instance_id WHERE k.account_id=%d" % A),
    ("search_query_performance (via camp)","SELECT COUNT(*), MIN(p.report_date), MAX(p.report_date) FROM search_query_performance p JOIN campaigns ca ON ca.campaign_id=p.campaign_id AND ca.account_id=%d" % A),
]
for name, q in checks:
    try:
        print("   %-38s %s" % (name, c.execute(q).fetchone()))
    except sqlite3.Error as e:
        print("   %-38s ERROR: %s" % (name, e))

print("\n2) conversion_actions map for acct %d (THE routing config):" % A)
try:
    rows = c.execute("SELECT id, conversion_name, include_mode, notes FROM conversion_actions WHERE account_id=%d ORDER BY id" % A).fetchall()
    if not rows:
        print("   (EMPTY -- routing config must be built before kw syncs run)")
    for r in rows:
        print("   ", r[0], "|", r[1], "|", r[2], "|", (r[3] or "")[:60])
except sqlite3.Error as e:
    print("   ERROR:", e)

print("\n3) campaign_type + status census (PMax / channel check):")
for r in c.execute("SELECT campaign_type, status, COUNT(*) FROM campaigns WHERE account_id=%d GROUP BY 1,2" % A):
    print("   ", r)

print("\n4) keyword match_type census (enum-corruption check):")
for r in c.execute("SELECT match_type, COUNT(*) FROM keyword_instances WHERE account_id=%d GROUP BY 1" % A):
    print("   ", r)

print("\n5) naming-convention parse rate (STE convention carry-over):")
print("   ", c.execute("""SELECT COUNT(*),
        SUM(CASE WHEN parsed_product_type IS NOT NULL AND parsed_product_type != '' THEN 1 ELSE 0 END),
        SUM(CASE WHEN naming_convention_flag IS NOT NULL AND naming_convention_flag != '' THEN 1 ELSE 0 END)
        FROM campaigns WHERE account_id=%d""" % A).fetchone(),
      "(campaigns, parsed_product ok, flagged)")

print("\n6) recent activity check -- was it spending when the sync froze?")
for r in c.execute("""SELECT substr(date,1,7), COUNT(*), ROUND(SUM(cost_micros)/1e6,0)
        FROM campaign_performance_daily WHERE account_id=%d AND device='ALL'
        AND date >= '2026-02-01' GROUP BY 1 ORDER BY 1""" % A):
    print("   ", r)
c.close()
