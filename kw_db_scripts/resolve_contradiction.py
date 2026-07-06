"""
resolve_contradiction.py
========================
Row counts say the network table has ~58% of kpd for 2025-07-15, but spend
says it has ~2%. Both can't be right. This resolves it by measuring kpd and
the network table the SAME way, side by side, for one date — and checking
whether kpd has multiple instance_ids per (keyword-date) that inflate its
spend (the orphaned-instance hypothesis).
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DATE = "2025-07-15"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

def one(q, p=()):
    return conn.execute(q, p).fetchone()[0]

print(f"For {DATE}, device='ALL':\n")

kpd_rows  = one("SELECT COUNT(*) FROM keyword_performance_daily WHERE report_date=? AND device='ALL'", (DATE,))
net_rows  = one("SELECT COUNT(*) FROM keyword_performance_network_daily WHERE report_date=? AND device='ALL'", (DATE,))
kpd_spend = one("SELECT COALESCE(SUM(cost_micros),0) FROM keyword_performance_daily WHERE report_date=? AND device='ALL'", (DATE,))/1e6
net_spend = one("SELECT COALESCE(SUM(cost_micros),0) FROM keyword_performance_network_daily WHERE report_date=? AND device='ALL'", (DATE,))/1e6

print(f"  kpd:     {kpd_rows:>8,} rows   ${kpd_spend:>14,.2f}")
print(f"  network: {net_rows:>8,} rows   ${net_spend:>14,.2f}")
print(f"  row ratio: {net_rows/kpd_rows*100:.1f}%   spend ratio: {net_spend/kpd_spend*100:.1f}%")

# Is kpd's spend inflated by orphaned instances (instance_ids not in keyword_instances)?
kpd_orphan_spend = one("""
    SELECT COALESCE(SUM(cost_micros),0) FROM keyword_performance_daily k
    WHERE report_date=? AND device='ALL'
      AND NOT EXISTS (SELECT 1 FROM keyword_instances ki WHERE ki.id=k.instance_id)
""", (DATE,))/1e6
kpd_valid_spend = one("""
    SELECT COALESCE(SUM(cost_micros),0) FROM keyword_performance_daily k
    WHERE report_date=? AND device='ALL'
      AND EXISTS (SELECT 1 FROM keyword_instances ki WHERE ki.id=k.instance_id)
""", (DATE,))/1e6
print(f"\n  kpd spend on VALID instances (in keyword_instances): ${kpd_valid_spend:,.2f}")
print(f"  kpd spend on ORPHAN instances (not in keyword_instances): ${kpd_orphan_spend:,.2f}")

# Does a single instance_id have MULTIPLE rows for this date in kpd? (would inflate)
dup = conn.execute("""
    SELECT instance_id, COUNT(*) c, SUM(cost_micros)/1e6 spend
    FROM keyword_performance_daily WHERE report_date=? AND device='ALL'
    GROUP BY instance_id HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 5
""", (DATE,)).fetchall()
print(f"\n  kpd instance_ids with >1 row for this date (should be NONE if PK is clean):")
if dup:
    for r in dup:
        print(f"    instance {r['instance_id']}: {r['c']} rows, ${r['spend']:,.2f}")
else:
    print("    none — kpd has one row per instance per date (PK clean)")

# Check kpd's actual primary key / does it include something that allows dup?
print("\n  kpd schema (PK columns):")
for r in conn.execute("PRAGMA table_info(keyword_performance_daily)"):
    if r["pk"] > 0:
        print(f"    pk[{r['pk']}] = {r['name']}")

# The big one: total distinct instances on this date, valid vs orphan
n_valid = one("""SELECT COUNT(DISTINCT k.instance_id) FROM keyword_performance_daily k
    WHERE report_date=? AND device='ALL' AND EXISTS (SELECT 1 FROM keyword_instances ki WHERE ki.id=k.instance_id)""",(DATE,))
n_orphan = one("""SELECT COUNT(DISTINCT k.instance_id) FROM keyword_performance_daily k
    WHERE report_date=? AND device='ALL' AND NOT EXISTS (SELECT 1 FROM keyword_instances ki WHERE ki.id=k.instance_id)""",(DATE,))
print(f"\n  distinct instances on {DATE}: valid={n_valid:,}  orphan={n_orphan:,}")

print("\nVerdict:")
print(f"  If orphan spend is large, kpd is inflated by pre-restructure duplicate")
print(f"  instances and the network table (valid instances only) is actually correct.")
print(f"  Compare network spend (${net_spend:,.2f}) to kpd VALID spend (${kpd_valid_spend:,.2f}) —")
print(f"  THOSE two should match if the network loader is fine.")

conn.close()
