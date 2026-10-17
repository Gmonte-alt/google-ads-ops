#!/usr/bin/env python
"""
check_backfill_progress.py -- ad_assets/diagnostics/ -- read-only (mode=ro)
progress check for the running historical backfill. Safe to run repeatedly
from a second prompt while the backfill writes; shows the last COMMITTED
month (the in-flight month is invisible until its commit lands).

  python C:\\Projects\\google-ads-ops\\ad_assets\\diagnostics\\check_backfill_progress.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
TARGET_ROWS = 1_665_712  # dry-run total

conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
n, mx = conn.execute(
    "SELECT COUNT(*), MAX(report_date) FROM rsa_ad_performance_daily "
    "WHERE report_date < '2026-01-01'").fetchone()
ads = conn.execute("SELECT COUNT(*) FROM rsa_ads").fetchone()[0]
by_month = conn.execute(
    "SELECT substr(report_date,1,7), COUNT(*) FROM rsa_ad_performance_daily "
    "WHERE report_date < '2026-01-01' GROUP BY 1 ORDER BY 1 DESC LIMIT 3").fetchall()
conn.close()

print("historical perf rows: %s / %s  (%.0f%%)"
      % (f"{n:,}", f"{TARGET_ROWS:,}", 100.0 * n / TARGET_ROWS if TARGET_ROWS else 0))
print("last committed date:  %s" % mx)
print("rsa_ads total:        %s  (expect ~49,031 when done)" % f"{ads:,}")
if by_month:
    print("latest committed months: "
          + ", ".join("%s (%s rows)" % (m, f"{c:,}") for m, c in by_month))
