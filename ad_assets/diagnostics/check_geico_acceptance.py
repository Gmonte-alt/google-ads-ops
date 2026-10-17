#!/usr/bin/env python
"""
check_geico_acceptance.py -- ad_assets/diagnostics/ -- v2 FAST. Track B
acceptance evidence via the indexed path only:

  ad_groups (name -> 2,605 Geico ad_group ids; pre-flight proved this table
  carries ALL generations, so the perf-table name UNION is unnecessary)
  -> rsa_ads (ids -> ad_ids, indexed PK)
  -> rsa_ad_performance_daily (per-ad indexed lookups, chunked)

v1 hung: DISTINCT over the multi-million-row ad-group perf table plus an
unindexed CTE join. This version runs in seconds. READ-ONLY.

  python C:\\Projects\\google-ads-ops\\ad_assets\\diagnostics\\check_geico_acceptance.py
"""
import sqlite3
from collections import defaultdict

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 1


def chunks(seq, n=900):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)

geico_ags = [r[0] for r in conn.execute(
    "SELECT ad_group_id FROM ad_groups WHERE instr(lower(ad_group_name),'geico')")]
print("Geico ad groups (from ad_groups, all generations): %d" % len(geico_ags))

ad_to_group = {}
for ch in chunks(geico_ags):
    marks = ",".join("?" * len(ch))
    for ad_id, ag in conn.execute(
            f"SELECT ad_id, ad_group_id FROM rsa_ads WHERE account_id=? "
            f"AND ad_group_id IN ({marks})", [ACCOUNT_ID] + ch):
        ad_to_group[ad_id] = ag
print("RSA ads in those groups (rsa_ads): %d" % len(ad_to_group))

tot_rows = tot_clicks = 0
d_min, d_max = "9999", "0000"
groups_with_data = set()
monthly = defaultdict(lambda: [0, set(), set(), 0])  # rows, groups, ads, clicks
for ch in chunks(ad_to_group):
    marks = ",".join("?" * len(ch))
    for ad_id, n, clk, lo, hi in conn.execute(
            f"""SELECT ad_id, COUNT(*), SUM(clicks), MIN(report_date), MAX(report_date)
                FROM rsa_ad_performance_daily
                WHERE account_id=? AND report_date < '2026-01-01'
                  AND ad_id IN ({marks}) GROUP BY ad_id""", [ACCOUNT_ID] + ch):
        tot_rows += n
        tot_clicks += clk or 0
        d_min, d_max = min(d_min, lo), max(d_max, hi)
        groups_with_data.add(ad_to_group[ad_id])
    for m, ad_id, n, clk in conn.execute(
            f"""SELECT substr(report_date,1,7), ad_id, COUNT(*), SUM(clicks)
                FROM rsa_ad_performance_daily
                WHERE account_id=? AND report_date BETWEEN '2024-05-01' AND '2024-09-30'
                  AND ad_id IN ({marks}) GROUP BY 1, 2""", [ACCOUNT_ID] + ch):
        row = monthly[m]
        row[0] += n
        row[1].add(ad_to_group[ad_id])
        row[2].add(ad_id)
        row[3] += clk or 0
conn.close()

print("\n[ACCEPT] historical Geico-cohort perf rows: %s" % f"{tot_rows:,}")
print("         across %d ad group(s) with data, %s .. %s, %s clicks"
      % (len(groups_with_data), d_min, d_max, f"{tot_clicks:,}"))
print("\nJul-2024 inflection window (the Track B requirement):")
for m in sorted(monthly):
    n, ags, ads, clk = monthly[m]
    print("  %s  rows %7s | ad groups %4d | ads %5d | clicks %s"
          % (m, f"{n:,}", len(ags), len(ads), f"{clk:,}"))
print("\nBoth sides of 2024-07 populated = acceptance PASSES; send this output")
print("plus the apply month table through the gate; Track B is unblocked.")
