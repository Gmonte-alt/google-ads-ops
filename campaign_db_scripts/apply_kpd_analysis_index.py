#!/usr/bin/env python
"""
apply_kpd_analysis_index.py -- creates the analysis-pattern index on
keyword_performance_daily requested by keyword_health_scenario.py v2 and
serving every device='ALL' + date-range analytical query.

ADDITIVE ONLY: CREATE INDEX touches no data; an interrupted build leaves no
partial index (SQLite builds transactionally). Expect a few minutes' build
on ~21M rows and roughly 0.5-1 GB of DB growth. Do NOT run during a sync
(index build takes the write lock).

    python apply_kpd_analysis_index.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    python apply_kpd_analysis_index.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import sqlite3
import time

IDX = "idx_kpd_dev_date"
DDL = ("CREATE INDEX IF NOT EXISTS idx_kpd_dev_date "
       "ON keyword_performance_daily(device, report_date, instance_id)")

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
existing = [r[1] for r in conn.execute(
    "PRAGMA index_list(keyword_performance_daily)")]
print("[PLAN] index %s: %s" % (IDX, "exists (skip)" if IDX in existing else "WILL CREATE"))
print("[INFO] existing kpd indexes:", existing)

if not args.apply:
    print("[DRY RUN] no changes. Expect a few minutes and ~0.5-1 GB growth on --apply.")
else:
    if IDX not in existing:
        t0 = time.time()
        conn.execute(DDL)
        print("[BUILD] index created in %.1f min" % ((time.time() - t0) / 60))
        t0 = time.time()
        conn.execute("ANALYZE keyword_performance_daily")
        conn.commit()
        print("[ANALYZE] stats refreshed in %.1f min" % ((time.time() - t0) / 60))
    # prove the plan changed
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT instance_id, SUM(clicks) "
        "FROM keyword_performance_daily "
        "WHERE device='ALL' AND report_date >= '2026-06-01' "
        "GROUP BY instance_id").fetchall()
    print("[VERIFY] plan for the analysis pattern:")
    for r in plan:
        print("  ", r)
    print("  (want: SEARCH ... USING INDEX idx_kpd_dev_date)")
conn.close()
