"""
check_pk_collision.py
=====================
LAST-STANDING HYPOTHESIS: resolution succeeds (100%), but multiple CSV rows
resolve to the SAME instance_id for the same (report_date, network), colliding
on the network table's PRIMARY KEY (instance_id, report_date, device, network,
geo_state). INSERT OR REPLACE keeps only the last, silently dropping the rest —
which would collapse spend exactly as observed.

This replicates the resolution, then checks: after mapping each perf_net row to
its instance_id, are there DUPLICATE (instance_id, report_date, network) keys
with different original (criterion_id, ad_group_id)? If yes, those collide and
only one survives — and we can measure how much spend is lost to collisions.

Usage:
    python check_pk_collision.py "C:\\...\\microsoft-home-kw-20260331.csv"
"""

import sys
import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CSV = sys.argv[1] if len(sys.argv) > 1 else \
      r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-kw\microsoft-home-kw-20260331.csv"
MS_ACCOUNT_ID = 2


def strip_brackets(s):
    return s.astype(str).str.replace("[", "", regex=False).str.replace("]", "", regex=False).str.strip()


raw = open(CSV, encoding="utf-8-sig").read().splitlines()
hdr = next(i for i, l in enumerate(raw) if "Keyword ID" in l and "Ad group ID" in l)
df = pd.read_csv(CSV, skiprows=hdr, dtype=str)
df = df[df.iloc[:, 0] != "Total"]
df = df[df["Keyword ID"].notna()]
df["criterion_id"] = strip_brackets(df["Keyword ID"])
df["ad_group_id"]  = strip_brackets(df["Ad group ID"])
df["report_date"]  = df["Date"]
df["network"]      = df["Network"]
df["cost_micros"]  = (pd.to_numeric(
    df["Spend"].astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False),
    errors="coerce").fillna(0) * 1e6)
df = df[df["network"] != "-"]

perf_net = df.groupby(["criterion_id", "ad_group_id", "report_date", "network"],
                      as_index=False).agg(cost_micros=("cost_micros", "sum"))

conn = sqlite3.connect(DB)
cur = conn.cursor()
cache = {}
def resolve(ag, cr):
    k = (ag, cr)
    if k not in cache:
        r = cur.execute("SELECT id FROM keyword_instances WHERE account_id=? AND ad_group_id=? AND criterion_id=?",
                        (MS_ACCOUNT_ID, ag, cr)).fetchone()
        cache[k] = r[0] if r else None
    return cache[k]

perf_net["instance_id"] = perf_net.apply(lambda r: resolve(r["ad_group_id"], r["criterion_id"]), axis=1)
perf_net = perf_net[perf_net["instance_id"].notna()]

# The network table PK is (instance_id, report_date, device='ALL', network, geo_state='ALL').
# So the effective collision key is (instance_id, report_date, network).
pk = ["instance_id", "report_date", "network"]
grp = perf_net.groupby(pk)
collisions = grp.size()
colliding_keys = collisions[collisions > 1]

print(f"CSV: {Path(CSV).name}")
print(f"perf_net rows (resolved): {len(perf_net):,}")
print(f"Distinct PK keys (instance_id, date, network): {len(collisions):,}")
print(f"PK keys with COLLISIONS (>1 row): {len(colliding_keys):,}")
print()

if len(colliding_keys) == 0:
    print("NO collisions. This hypothesis is ALSO wrong — the PK is unique per row.")
else:
    # How much spend is lost? For each colliding key, all but the max-cost row's
    # spend is dropped by INSERT OR REPLACE (last wins; we approximate loss as
    # total minus the single surviving row).
    def loss(g):
        return g["cost_micros"].sum() - g["cost_micros"].max()
    lost = grp.apply(lambda g: g["cost_micros"].sum() - g["cost_micros"].max())
    total_lost = lost.sum() / 1e6
    total = perf_net["cost_micros"].sum() / 1e6
    print(f"Spend LOST to PK collisions (all-but-one row per key): ${total_lost:,.2f}"
          f"  ({100*total_lost/total:.1f}% of ${total:,.2f})")
    print()
    # Show a few worst colliding keys with their multiple source rows
    print("Worst colliding keys (multiple criterion/ad_group -> same instance/date/network):")
    worst = colliding_keys.sort_values(ascending=False).head(5)
    for key in worst.index:
        iid, dt, net = key
        rows = perf_net[(perf_net["instance_id"] == iid) &
                        (perf_net["report_date"] == dt) &
                        (perf_net["network"] == net)]
        print(f"\n  instance_id={iid} date={dt} network={net}  ({len(rows)} colliding rows):")
        for _, r in rows.iterrows():
            print(f"    criterion_id={r['criterion_id']:<16} ad_group_id={r['ad_group_id']:<16} "
                  f"spend=${r['cost_micros']/1e6:,.2f}")

conn.close()
