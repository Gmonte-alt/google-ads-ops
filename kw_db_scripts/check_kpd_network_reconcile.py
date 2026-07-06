"""
check_kpd_network_reconcile.py
==============================
Atomic reconciliation between keyword_performance_daily (kpd) and
keyword_performance_network_daily, joined per (instance_id, report_date) for a
single date, so multi-file accumulation can't confound the comparison.

For the chosen date, every kpd device='ALL' row's cost is compared to the sum
of the network table's rows (across networks) for the same instance+date.
They should match to the penny for any instance whose data came from the same
file. Rows where net=0 but kpd>0 mean that instance/date isn't in the network
table yet (loaded into kpd by an earlier file the network table never saw) —
which a full reload fixes.

Usage:
    python check_kpd_network_reconcile.py                 # default date 2026-06-15
    python check_kpd_network_reconcile.py 2026-06-20      # a specific date
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-06-15"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT k.instance_id,
           ROUND(k.cost_micros, 2)          AS kpd,
           ROUND(COALESCE(n.net, 0), 2)      AS net
    FROM keyword_performance_daily k
    LEFT JOIN (
        SELECT instance_id, report_date, SUM(cost_micros) AS net
        FROM keyword_performance_network_daily
        WHERE device = 'ALL'
        GROUP BY instance_id, report_date
    ) n ON n.instance_id = k.instance_id AND n.report_date = k.report_date
    WHERE k.device = 'ALL'
      AND k.report_date = ?
      AND k.cost_micros > 0
    ORDER BY k.cost_micros DESC
    LIMIT 20
""", (DATE,)).fetchall()

print(f"Per-instance reconcile for {DATE} (top 20 by spend):\n")
print(f"  {'instance_id':<12} {'kpd_micros':>18} {'net_micros':>18}  status")
print(f"  {'-'*12} {'-'*18} {'-'*18}  ------")
n_ok = n_missing = n_diff = 0
for r in rows:
    if r["net"] == 0:
        status = "MISSING (net=0)"
        n_missing += 1
    elif abs(r["kpd"] - r["net"]) < 1:
        status = "OK"
        n_ok += 1
    else:
        status = "DIFF <-- investigate"
        n_diff += 1
    print(f"  {r['instance_id']:<12} {r['kpd']:>18,.2f} {r['net']:>18,.2f}  {status}")

# Aggregate over the whole date (not just top 20)
agg = conn.execute("""
    SELECT
      (SELECT ROUND(SUM(cost_micros),2) FROM keyword_performance_daily
         WHERE device='ALL' AND report_date=?) AS kpd_total,
      (SELECT ROUND(SUM(cost_micros),2) FROM keyword_performance_network_daily
         WHERE device='ALL' AND report_date=?) AS net_total
""", (DATE, DATE)).fetchone()

print(f"\n  Top-20 summary: {n_ok} OK, {n_missing} missing(net=0), {n_diff} real-diff")
print(f"\n  Whole-day {DATE}:")
print(f"    kpd  total: {agg['kpd_total']:>18,.2f}")
print(f"    net  total: {agg['net_total']:>18,.2f}")
kt, nt = agg["kpd_total"] or 0, agg["net_total"] or 0
print(f"    delta     : {kt - nt:>18,.2f}")

print("\nInterpretation:")
if n_diff > 0:
    print("  Real DIFF rows exist (both nonzero, unequal) -> LOGIC BUG. Stop; fix loader before reload.")
elif n_missing > 0 and n_ok > 0:
    print("  Mix of OK and MISSING -> logic is correct; MISSING rows are instances/dates the")
    print("  single loaded file didn't cover but earlier files put in kpd. A full reload of")
    print("  BOTH tables from the same file set reconciles them. Safe to proceed with reload.")
elif n_ok > 0 and n_missing == 0:
    print("  All OK -> tables reconcile exactly for this date. Safe to proceed with reload.")
else:
    print("  All MISSING -> the network table has no rows for this date (file didn't cover it).")
    print("  Try a date inside the loaded file's range, or proceed to full reload.")

conn.close()
