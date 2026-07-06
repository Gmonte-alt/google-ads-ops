"""
diagnose_resolution.py
======================
Replicates EXACTLY what _write_perf_net_rows does — builds perf_net_df from a
real CSV and, for each row, runs the same instance-resolution query against the
REAL keyword_instances table. Reports the resolve rate SPLIT BY SPEND TIER.

Hypothesis under test: high-spend (criterion, ad_group, date) rows fail the
(account_id, ad_group_id, criterion_id) lookup during reload, so the network
writer skips them (writes nothing), while kpd retains stale rows from original
loads. If true, we'll see: low-spend rows resolve, high-spend rows don't.

For failures, it shows what keyword_instances DOES have for that ad_group_id
and that criterion_id separately — revealing whether it's an ad_group_id
mismatch, a criterion_id mismatch, or the instance is simply absent.

Usage (point at any archived keyword CSV — an OLDER one is most informative):
    python diagnose_resolution.py "C:\\...\\microsoft-home-kw-20260323.csv"
"""

import sys
import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
CSV = sys.argv[1] if len(sys.argv) > 1 else \
      r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-kw\microsoft-home-kw-20260323.csv"
MS_ACCOUNT_ID = 2


def strip_brackets(s):
    return s.astype(str).str.replace("[", "", regex=False).str.replace("]", "", regex=False).str.strip()


# --- Read + minimal clean (matching the loader's key derivation) ---
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

# drop the dash/subtotal network row
df = df[df["network"] != "-"]

# Build perf_net_df exactly like the loader
perf_net = df.groupby(["criterion_id", "ad_group_id", "report_date", "network"],
                      as_index=False).agg(cost_micros=("cost_micros", "sum"))
print(f"CSV: {Path(CSV).name}")
print(f"perf_net_df rows: {len(perf_net):,}   total spend: ${perf_net['cost_micros'].sum()/1e6:,.2f}\n")

# --- Resolve each row against the REAL keyword_instances ---
conn = sqlite3.connect(DB)
cur = conn.cursor()

resolve_cache = {}
def resolves(ad_group_id, criterion_id):
    key = (ad_group_id, criterion_id)
    if key not in resolve_cache:
        r = cur.execute(
            "SELECT id FROM keyword_instances WHERE account_id=? AND ad_group_id=? AND criterion_id=?",
            (MS_ACCOUNT_ID, ad_group_id, criterion_id)).fetchone()
        resolve_cache[key] = r[0] if r else None
    return resolve_cache[key]

perf_net["resolved"] = perf_net.apply(
    lambda r: resolves(r["ad_group_id"], r["criterion_id"]) is not None, axis=1)

# --- Report: resolve rate by spend tier ---
def tier(c):
    d = c / 1e6
    if d == 0:      return "0  (zero spend)"
    if d < 1:       return "1  <$1"
    if d < 10:      return "2  $1-10"
    if d < 100:     return "3  $10-100"
    return "4  >=$100"

perf_net["tier"] = perf_net["cost_micros"].apply(tier)
print("Resolve rate by spend tier:")
print(f"  {'tier':<18} {'rows':>8} {'resolved':>9} {'resolve%':>9} {'spend':>14} {'spend_resolved':>15}")
for t in sorted(perf_net["tier"].unique()):
    sub = perf_net[perf_net["tier"] == t]
    res = sub[sub["resolved"]]
    sp = sub["cost_micros"].sum() / 1e6
    spr = res["cost_micros"].sum() / 1e6
    print(f"  {t:<18} {len(sub):>8,} {sub['resolved'].sum():>9,} "
          f"{100*sub['resolved'].mean():>8.1f}% ${sp:>12,.2f} ${spr:>13,.2f}")

tot = perf_net["cost_micros"].sum() / 1e6
totr = perf_net[perf_net["resolved"]]["cost_micros"].sum() / 1e6
print(f"\n  TOTAL spend in file:      ${tot:,.2f}")
print(f"  TOTAL spend that resolves:${totr:,.2f}  ({100*totr/tot:.1f}%)")
print(f"  => spend the network writer WOULD DROP: ${tot-totr:,.2f}")

# --- For failures, show what keyword_instances has ---
fails = perf_net[~perf_net["resolved"]].nlargest(8, "cost_micros")
print(f"\nTop unresolved rows — what does keyword_instances have?")
for _, r in fails.iterrows():
    ag = r["ad_group_id"]; cr = r["criterion_id"]
    by_ag = cur.execute("SELECT COUNT(*) FROM keyword_instances WHERE account_id=? AND ad_group_id=?",
                        (MS_ACCOUNT_ID, ag)).fetchone()[0]
    by_cr = cur.execute("SELECT id, ad_group_id FROM keyword_instances WHERE account_id=? AND criterion_id=?",
                        (MS_ACCOUNT_ID, cr)).fetchall()
    print(f"\n  spend=${r['cost_micros']/1e6:,.2f}  ad_group_id={ag}  criterion_id={cr}")
    print(f"    keyword_instances rows with that ad_group_id: {by_ag}")
    print(f"    keyword_instances rows with that criterion_id: {len(by_cr)}"
          + (f" (ad_group_ids: {[b[1] for b in by_cr][:3]})" if by_cr else ""))

conn.close()
