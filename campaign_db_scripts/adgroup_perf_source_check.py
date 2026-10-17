#!/usr/bin/env python
"""
adgroup_perf_source_check.py -- READ-ONLY. Answers the Geico-cohort source
questions on ad_group_performance_daily: columns, coverage, device pattern,
conversion-column semantics, name-cohort integrity, era consistency.

    python adgroup_perf_source_check.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("1) COLUMNS:")
cols = [r[1] for r in c.execute("PRAGMA table_info(ad_group_performance_daily)")]
print("  ", cols)

has = lambda x: x in cols
acct = "account_id = 1" if has("account_id") else "1=1  /* no account_id col! scope via campaigns join */"

print("\n2) COVERAGE (acct 1, by quarter):")
for r in c.execute(f"""
    SELECT substr(date,1,7), COUNT(*), ROUND(SUM(cost_micros)/1e6,0)
    FROM ad_group_performance_daily WHERE {acct}
      AND substr(date,6,2) IN ('01','04','07','10')
    GROUP BY substr(date,1,7) ORDER BY 1"""):
    print("  ", r)
print("  full range:", c.execute(
    f"SELECT MIN(date), MAX(date) FROM ad_group_performance_daily WHERE {acct}").fetchone())

print("\n3) DEVICE VALUES:")
if has("device"):
    for r in c.execute(f"SELECT device, COUNT(*) FROM ad_group_performance_daily WHERE {acct} GROUP BY device"):
        print("  ", r)
else:
    print("   (no device column -- rows are already daily rollups)")

print("\n4) CONVERSION COLUMNS -- June 2026 totals, native vs primary (if present):")
sel = ["ROUND(SUM(conversions),1)", "ROUND(SUM(conversion_value),0)"]
if has("primary_conv_count"):
    sel += ["ROUND(SUM(primary_conv_count),1)", "ROUND(SUM(primary_conv_value),0)"]
dev = " AND device='ALL'" if has("device") else ""
print("  ", c.execute(f"""
    SELECT {', '.join(sel)} FROM ad_group_performance_daily
    WHERE {acct}{dev} AND date BETWEEN '2026-06-01' AND '2026-06-30'""").fetchone(),
    " (native conv, native val%s)" % (", primary conv, primary val" if has("primary_conv_count") else ""))
print("   kpd routed primary same month (truth anchor):")
print("  ", c.execute("""
    SELECT ROUND(SUM(p.primary_conv_count),1), ROUND(SUM(p.primary_conv_value),0)
    FROM keyword_performance_daily p JOIN keyword_instances ki ON ki.id=p.instance_id
    WHERE ki.account_id=1 AND p.device='ALL'
      AND p.report_date BETWEEN '2026-06-01' AND '2026-06-30'""").fetchone())

print("\n5) GEICO NAME COHORT -- distinct matching ad group names + id count:")
for r in c.execute(f"""
    SELECT ad_group_name, COUNT(DISTINCT ad_group_id), MIN(date), MAX(date)
    FROM ad_group_performance_daily
    WHERE {acct} AND instr(lower(ad_group_name),'geico')
    GROUP BY ad_group_name ORDER BY 3"""):
    print("  ", r)

c.close()
print("\nSend this full output back through the DB architect.")
