#!/usr/bin/env python
"""
mobile_conv_merge_probe.py -- READ-ONLY. Localizes the workbook's zero mobile
conversions: SQL-empty (device rows missing in window) vs pandas-merge failure
(campaign_id dtype mismatch). Replicates campaign_performance_data_google.py's
exact queries and merge.

    python mobile_conv_merge_probe.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --start 2026-04-27 --end 2026-07-26
"""
import argparse
import sqlite3
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--start", default="2026-04-27")
ap.add_argument("--end", default="2026-07-26")
args = ap.parse_args()

conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)

cpd = pd.read_sql("""
    SELECT cpd.campaign_id, cpd.date, cpd.device,
           SUM(cpd.cost_micros)/1e6 AS cost_usd
    FROM campaign_performance_daily_search cpd
    JOIN campaigns c ON c.campaign_id = cpd.campaign_id
    WHERE c.account_id = 1 AND cpd.device IN ('ALL','MOBILE')
      AND cpd.date BETWEEN ? AND ?
    GROUP BY cpd.campaign_id, cpd.date, cpd.device
""", conn, params=(args.start, args.end))

kpd = pd.read_sql("""
    SELECT kpd.campaign_id, kpd.report_date AS date, kpd.device,
           SUM(kpd.primary_conv_count) AS primary_conversions
    FROM keyword_performance_daily kpd
    JOIN campaigns c ON c.campaign_id = kpd.campaign_id
    WHERE c.account_id = 1 AND kpd.device IN ('ALL','MOBILE')
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY kpd.campaign_id, kpd.report_date, kpd.device
""", conn, params=(args.start, args.end))
conn.close()

print("=" * 68)
print("1) WINDOW COVERAGE (%s .. %s)" % (args.start, args.end))
print("=" * 68)
print("cpd rows by device:\n", cpd.groupby("device").agg(rows=("date","count"), cost=("cost_usd","sum")))
print("\nkpd rows by device:\n", kpd.groupby("device").agg(rows=("date","count"), conv=("primary_conversions","sum")))
if kpd[kpd.device == "MOBILE"].empty:
    print("\nVERDICT -> kpd has NO MOBILE rows in this window: sync coverage gap,")
    print("           not a merge bug. Check whether daily 03 runs the device pass.")
else:
    print("\nkpd MOBILE rows exist in window with %.1f conversions -- checking merge..."
          % kpd[kpd.device == "MOBILE"]["primary_conversions"].sum())

print("\n" + "=" * 68)
print("2) DTYPES + KEY OVERLAP")
print("=" * 68)
print("cpd.campaign_id dtype:", cpd["campaign_id"].dtype,
      "| sample:", repr(cpd["campaign_id"].iloc[0]) if len(cpd) else "n/a")
print("kpd.campaign_id dtype:", kpd["campaign_id"].dtype,
      "| sample:", repr(kpd["campaign_id"].iloc[0]) if len(kpd) else "n/a")
print("cpd.date dtype:", cpd["date"].dtype, "| kpd.date dtype:", kpd["date"].dtype)

base = cpd[cpd.device == "ALL"].drop(columns=["device"])
mob = kpd[kpd.device == "MOBILE"].drop(columns=["device"]).rename(
    columns={"primary_conversions": "m_primary_conversions"})
merged = base.merge(mob, on=["campaign_id", "date"], how="left")
matched = merged["m_primary_conversions"].notna().sum()
print("\nmerge (cpd ALL <- kpd MOBILE): %d of %d base rows matched; "
      "mobile conv after merge = %.1f"
      % (matched, len(merged), merged["m_primary_conversions"].fillna(0).sum()))

if len(mob) and matched == 0:
    print("\nVERDICT -> MOBILE rows exist but ZERO merge matches: dtype/key mismatch.")
    print("FIX for the workbook: normalize keys before every merge, e.g.")
    print("    df['campaign_id'] = df['campaign_id'].astype(str)")
    print("    (apply to BOTH frames, cpd and kpd, right after read_sql)")
elif len(mob) and matched > 0:
    print("\nVERDICT -> merge works here. If the workbook still zeros, the bug is")
    print("           in a later step (rename map or section formulas) -- diff this")
    print("           probe's merge against the script's slice_dev output.")
