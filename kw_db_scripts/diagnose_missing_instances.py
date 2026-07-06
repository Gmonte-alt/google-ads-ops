"""
diagnose_missing_instances.py
=============================
Tests why high-spend instances are MISSING from the network table on 2026-06-15.

Theory: the single loaded file (microsoft-home-kw-20260630.csv, 12 current
campaigns / 39 ad groups) doesn't contain those keywords — they're historical
instances from the pre-restructure state campaigns, present in kpd from earlier
loads but not in the current file. If so, a full reload populates them and the
MISSING is benign.

For a sample of MISSING instances, this prints:
  - the keyword text, ad group, campaign (from keyword_instances)
  - whether the network table has ANY row for that instance (any date)
  - whether kpd has it on other dates too
so we can see if these are historical/retired-structure keywords.
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DATE = "2026-06-15"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# The MISSING high-spenders from the reconcile output
missing_ids = [420659, 420619, 412510, 53428, 420661]
# A couple of the OK ones for contrast
ok_ids = [499570, 532314, 511024]

def describe(instance_id, label):
    ki = conn.execute("""
        SELECT ki.id, ki.keyword_text, ki.match_type, ki.status,
               ki.ad_group_name, ki.campaign_name
        FROM keyword_instances ki WHERE ki.id = ?
    """, (instance_id,)).fetchone()
    if ki is None:
        print(f"  [{label}] instance {instance_id}: NOT FOUND in keyword_instances")
        return
    # any network rows for this instance, any date?
    net_any = conn.execute("""
        SELECT COUNT(*) c, MIN(report_date) mn, MAX(report_date) mx
        FROM keyword_performance_network_daily WHERE instance_id = ?
    """, (instance_id,)).fetchone()
    # kpd rows for this instance across all dates
    kpd_any = conn.execute("""
        SELECT COUNT(*) c, MIN(report_date) mn, MAX(report_date) mx
        FROM keyword_performance_daily WHERE instance_id = ? AND device='ALL'
    """, (instance_id,)).fetchone()
    print(f"  [{label}] instance {instance_id}")
    print(f"      keyword : {ki['keyword_text']!r}  ({ki['match_type']}, {ki['status']})")
    print(f"      adgroup : {ki['ad_group_name']}")
    print(f"      campaign: {ki['campaign_name']}")
    print(f"      network table rows: {net_any['c']}  (dates {net_any['mn']}..{net_any['mx']})")
    print(f"      kpd ALL rows      : {kpd_any['c']}  (dates {kpd_any['mn']}..{kpd_any['mx']})")
    print()

print(f"=== MISSING high-spenders on {DATE} ===\n")
for iid in missing_ids:
    describe(iid, "MISSING")

print(f"=== OK instances on {DATE} (for contrast) ===\n")
for iid in ok_ids:
    describe(iid, "OK")

# Bottom line: do the MISSING instances have ANY network rows at all?
missing_with_net = conn.execute("""
    SELECT COUNT(DISTINCT instance_id) c
    FROM keyword_performance_network_daily
    WHERE instance_id IN (420659,420619,412510,53428,420661)
""").fetchone()["c"]
print(f"Of the 5 MISSING instances, how many have ANY network row: {missing_with_net}")
print("  0 -> they're simply not in the loaded file (historical keywords). Benign; reload fixes.")
print("  >0 -> they ARE in the network table on other dates but missing on 06-15. Investigate.")

conn.close()
