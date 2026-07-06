"""
diagnose_reload_gap.py
======================
After the full reload, 17 historical instances still have 0 network rows.
This finds out WHY — is the network table just missing them everywhere, and
what distinguishes them from the instances that DID populate.

Checks, for the MISSING instances:
  1. Do they have ANY network rows on ANY date now (post-reload)?
  2. What is the max report_date in the network table overall vs in kpd?
     (Tests whether the network reload covered the same date span as kpd.)
  3. For one MISSING instance, what dates does kpd have vs the network table?
  4. Compare the network table's per-date coverage to kpd's — are whole
     historical date ranges absent from the network table?
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

missing = [420659, 420619, 412510, 53428, 420661, 420414, 420290]

print("=== 1. Do MISSING instances have ANY network rows now (post-reload)? ===")
for iid in missing:
    n = conn.execute("SELECT COUNT(*) c, MIN(report_date) mn, MAX(report_date) mx "
                     "FROM keyword_performance_network_daily WHERE instance_id=?",
                     (iid,)).fetchone()
    k = conn.execute("SELECT COUNT(*) c, MIN(report_date) mn, MAX(report_date) mx "
                     "FROM keyword_performance_daily WHERE instance_id=? AND device='ALL'",
                     (iid,)).fetchone()
    print(f"  inst {iid}: net_rows={n['c']:>4} ({n['mn']}..{n['mx']})   "
          f"kpd_rows={k['c']:>4} ({k['mn']}..{k['mx']})")

print("\n=== 2. Overall date span: network table vs kpd ===")
for tbl in ["keyword_performance_daily", "keyword_performance_network_daily"]:
    r = conn.execute(f"SELECT COUNT(*) c, MIN(report_date) mn, MAX(report_date) mx "
                     f"FROM {tbl} WHERE device='ALL'").fetchone()
    print(f"  {tbl:<38} rows={r['c']:>12,}  span {r['mn']}..{r['mx']}")

print("\n=== 3. Per-date coverage for one MISSING instance (420659) ===")
kdates = [r['report_date'] for r in conn.execute(
    "SELECT DISTINCT report_date FROM keyword_performance_daily "
    "WHERE instance_id=420659 AND device='ALL' ORDER BY report_date").fetchall()]
ndates = [r['report_date'] for r in conn.execute(
    "SELECT DISTINCT report_date FROM keyword_performance_network_daily "
    "WHERE instance_id=420659 ORDER BY report_date").fetchall()]
print(f"  kpd has {len(kdates)} dates: {kdates[:3]} .. {kdates[-3:] if len(kdates)>3 else ''}")
print(f"  net has {len(ndates)} dates: {ndates[:3] if ndates else '[]'}")

print("\n=== 4. What's the ad_group_id / campaign for a MISSING instance? ===")
r = conn.execute("""SELECT id, keyword_text, ad_group_id, ad_group_name, campaign_name, status
                    FROM keyword_instances WHERE id=420659""").fetchone()
print(f"  inst 420659: kw={r['keyword_text']!r}")
print(f"    ad_group_id={r['ad_group_id']}  ad_group_name={r['ad_group_name']}")
print(f"    campaign={r['campaign_name']}  status={r['status']}")

# Is that ad_group_id present in the CURRENT ad_groups table with a status?
ag = conn.execute("SELECT ad_group_id, ad_group_name, status FROM ad_groups WHERE ad_group_id=?",
                  (r['ad_group_id'],)).fetchone()
print(f"  ad_groups table: {dict(ag) if ag else 'NOT FOUND'}")

print("\n=== 5. A known-OK instance for contrast (499570) ===")
r = conn.execute("""SELECT id, keyword_text, ad_group_id, ad_group_name, campaign_name, status
                    FROM keyword_instances WHERE id=499570""").fetchone()
print(f"  inst 499570: kw={r['keyword_text']!r} ad_group_id={r['ad_group_id']}")
print(f"    campaign={r['campaign_name']}")
nr = conn.execute("SELECT COUNT(*) c FROM keyword_performance_network_daily WHERE instance_id=499570").fetchone()
print(f"    network rows: {nr['c']}")

conn.close()
