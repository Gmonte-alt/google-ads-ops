"""
check_kpd_march_composition.py
==============================
kpd 2026-03 = $1,826,098 but the March file's total spend is only $49,746 (37x
smaller). Either kpd accumulated March from many overlapping loads, OR kpd's
March has many more keyword-date rows than one file provides.

This checks: how many DISTINCT dates does kpd have in March, and what's the
per-date spend — to see if kpd's March is one clean month or an accumulation.
Also: does the single March file cover all 31 days, or just some?
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("kpd 2026-03 by date (device='ALL'):")
rows = conn.execute("""
    SELECT report_date, COUNT(*) rows, ROUND(SUM(cost_micros)/1e6,2) spend
    FROM keyword_performance_daily
    WHERE report_date BETWEEN '2026-03-01' AND '2026-03-31' AND device='ALL'
    GROUP BY report_date ORDER BY report_date
""").fetchall()
tot = 0
for r in rows:
    tot += r["spend"]
    print(f"  {r['report_date']}  rows={r['rows']:>7,}  spend=${r['spend']:>12,.2f}")
print(f"  ---- {len(rows)} dates, total ${tot:,.2f}")

print("\nnetwork 2026-03 by date:")
rows = conn.execute("""
    SELECT report_date, COUNT(*) rows, ROUND(SUM(cost_micros)/1e6,2) spend
    FROM keyword_performance_network_daily
    WHERE report_date BETWEEN '2026-03-01' AND '2026-03-31'
    GROUP BY report_date ORDER BY report_date
""").fetchall()
tot = 0
for r in rows:
    tot += r["spend"]
    print(f"  {r['report_date']}  rows={r['rows']:>7,}  spend=${r['spend']:>12,.2f}")
print(f"  ---- {len(rows)} dates, total ${tot:,.2f}")

# The key question: per-date, does kpd spend ENORMOUSLY exceed network for the
# SAME date? If kpd 2026-03-15 is $60k but network 2026-03-15 is $1.7k, and the
# March FILE says 03-15 was ~$1.7k, then kpd is inflated (accumulated), not network missing.
print("\nPer-date head-to-head (kpd vs network), mid-March:")
for d in ('2026-03-10','2026-03-15','2026-03-20','2026-03-25'):
    k = conn.execute("SELECT ROUND(SUM(cost_micros)/1e6,2) FROM keyword_performance_daily WHERE report_date=? AND device='ALL'", (d,)).fetchone()[0] or 0
    n = conn.execute("SELECT ROUND(SUM(cost_micros)/1e6,2) FROM keyword_performance_network_daily WHERE report_date=?", (d,)).fetchone()[0] or 0
    print(f"  {d}: kpd=${k:>12,.2f}   network=${n:>10,.2f}   ratio={k/n if n else 0:.1f}x")

conn.close()
