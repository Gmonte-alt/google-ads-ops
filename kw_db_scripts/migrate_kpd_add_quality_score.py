#!/usr/bin/env python
"""
migrate_kpd_add_quality_score.py -- adds four historical-QS columns to
keyword_performance_daily. ALTER TABLE ADD COLUMN is metadata-only in SQLite:
instant even on a 20M-row table, existing rows read NULL (= unrated).

Dry-run by default.

    python migrate_kpd_add_quality_score.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import sqlite3

COLS = [
    ("historical_quality_score",  "INTEGER"),  # 1-10; NULL = unrated
    ("hist_creative_quality",     "TEXT"),     # ad relevance bucket (BELOW_AVERAGE/AVERAGE/ABOVE_AVERAGE)
    ("hist_landing_page_quality", "TEXT"),
    ("hist_predicted_ctr",        "TEXT"),
]

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
existing = [r[1] for r in conn.execute("PRAGMA table_info(keyword_performance_daily)")]
todo = [(n, t) for n, t in COLS if n not in existing]
print("[PLAN]")
for n, t in COLS:
    print("  %-28s %s" % (n, "exists (skip)" if n in existing else "WILL ADD (%s)" % t))
if not args.apply:
    print("[DRY RUN] no changes made.")
elif not todo:
    print("[APPLY] nothing to do.")
else:
    for n, t in todo:
        conn.execute("ALTER TABLE keyword_performance_daily ADD COLUMN %s %s" % (n, t))
    conn.commit()
    print("[APPLY] added %d column(s). New schema:" % len(todo))
    print("  ", [r[1] for r in conn.execute("PRAGMA table_info(keyword_performance_daily)")])
conn.close()
