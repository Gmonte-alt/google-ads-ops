"""
check_reload_coverage.py
========================
The loader is proven correct (100% resolve, no collisions, full spend on a
single file). So the 95% gap must be COVERAGE: the network table didn't receive
all the files/dates the reload was supposed to load.

This compares, per date, how many rows and how much spend each table has —
looking for whole date ranges where the network table has FEW rows relative to
kpd (meaning that date's file never wrote to the network table) vs dates where
they're comparable (loaded after the network table existed / loader was fixed).

If the gap is concentrated in a contiguous early date range, the network table
was created/fixed partway through the reload and earlier files never populated
it — a re-run of those files fixes it, no loader change needed.
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("Per-date row-count ratio (network / kpd), sampled weekly:\n")
print(f"  {'date':<12} {'kpd_rows':>9} {'net_rows':>9} {'row%':>6} "
      f"{'kpd_spend':>13} {'net_spend':>13} {'spend%':>7}")
rows = conn.execute("""
    WITH k AS (
      SELECT report_date, COUNT(*) kr, SUM(cost_micros) ks
      FROM keyword_performance_daily WHERE device='ALL' GROUP BY report_date
    ),
    n AS (
      SELECT report_date, COUNT(*) nr, SUM(cost_micros) ns
      FROM keyword_performance_network_daily WHERE device='ALL' GROUP BY report_date
    )
    SELECT k.report_date d, k.kr, COALESCE(n.nr,0) nr,
           k.ks, COALESCE(n.ns,0) ns
    FROM k LEFT JOIN n ON n.report_date=k.report_date
    ORDER BY k.report_date
""").fetchall()

# sample ~every 7th date to keep output readable
for i, r in enumerate(rows):
    if i % 7 != 0:
        continue
    krow = r["kr"] or 0; nrow = r["nr"] or 0
    ks = (r["ks"] or 0)/1e6; ns = (r["ns"] or 0)/1e6
    rowpct = 100*nrow/krow if krow else 0
    sppct = 100*ns/ks if ks else 0
    flag = "  <-- gap" if sppct < 50 else ""
    print(f"  {r['d']:<12} {krow:>9,} {nrow:>9,} {rowpct:>5.0f}% "
          f"${ks:>11,.0f} ${ns:>11,.0f} {sppct:>6.0f}%{flag}")

# Find the boundary: latest date where network spend < 50% of kpd
boundary = None
for r in rows:
    ks = (r["ks"] or 0)/1e6; ns = (r["ns"] or 0)/1e6
    if ks > 0 and 100*ns/ks < 50:
        boundary = r["report_date"]
print(f"\nLatest date where network spend < 50% of kpd: {boundary}")
print("If the gap is a contiguous EARLY range ending at some date, the network")
print("table simply wasn't populated for those files — re-run just those files.")

conn.close()
