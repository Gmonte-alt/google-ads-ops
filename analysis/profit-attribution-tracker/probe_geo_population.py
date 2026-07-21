#!/usr/bin/env python3
"""probe_geo_population.py -- which geo table actually has Google Home data?
Read-only. Run: python probe_geo_population.py --db "C:\\...\\paid_search.db"
"""
import argparse
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
args = ap.parse_args()

conn = sqlite3.connect(args.db)
conn.execute("PRAGMA query_only = ON")

for tbl in ("geo_performance_daily", "campaign_performance_geo_daily"):
    print("=" * 70)
    print(tbl)
    print("=" * 70)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{tbl}')")]
    if not cols:
        print("  (table not found)")
        continue
    print("  columns:", cols)
    total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  total rows: {total:,}")
    if total == 0:
        continue
    date_col = next((c for c in ("date", "report_date", "day")
                     if c in cols), None)
    if "account_id" in cols:
        print("  rows / date range by account_id:")
        for row in conn.execute(
                f"SELECT account_id, COUNT(*), MIN({date_col}), "
                f"MAX({date_col}) FROM {tbl} GROUP BY account_id"):
            print(f"    account {row[0]}: {row[1]:,} rows, "
                  f"{row[2]} .. {row[3]}")
    elif "campaign_id" in cols:
        print("  no account_id; top campaign_ids by rows:")
        for row in conn.execute(
                f"SELECT campaign_id, COUNT(*) FROM {tbl} "
                f"GROUP BY campaign_id ORDER BY 2 DESC LIMIT 8"):
            print(f"    campaign {row[0]}: {row[1]:,} rows")
        if date_col:
            lo, hi = conn.execute(f"SELECT MIN({date_col}), "
                                  f"MAX({date_col}) FROM {tbl}").fetchone()
            print(f"  date range: {lo} .. {hi}")
    for dim in ("device", "geo_state", "location_type"):
        if dim in cols:
            vals = [str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT {dim} FROM {tbl} LIMIT 8")]
            print(f"  {dim} sample: {vals}")
conn.close()
