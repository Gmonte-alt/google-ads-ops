"""
reconcile_spend_by_month.py
===========================
The instance-count comparisons were confounded by orphaned historical
instance_ids in kpd (kpd has 208K distinct instance_ids but only 60,728 are
current keyword_instances). The clean test that doesn't depend on instance
bookkeeping: does total SPEND match, kpd vs network, per month?

If spend matches per month, the network table is a complete decomposition of
kpd regardless of instance-id churn. If it diverges, there's a real gap.

Also checks the negative-gap months (network > kpd) which shouldn't happen if
the network table is a strict decomposition.
"""

import sqlite3
from pathlib import Path

DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("Spend reconciliation kpd vs network, by month (device='ALL'):\n")
print(f"  {'month':<9} {'kpd_spend':>16} {'net_spend':>16} {'delta':>14} {'pct':>7}")
print(f"  {'-'*9} {'-'*16} {'-'*16} {'-'*14} {'-'*7}")

rows = conn.execute("""
    SELECT mon,
           SUM(kpd_spend) kpd_spend,
           SUM(net_spend) net_spend
    FROM (
      SELECT substr(report_date,1,7) mon, cost_micros kpd_spend, 0 net_spend
        FROM keyword_performance_daily WHERE device='ALL'
      UNION ALL
      SELECT substr(report_date,1,7) mon, 0 kpd_spend, cost_micros net_spend
        FROM keyword_performance_network_daily WHERE device='ALL'
    )
    GROUP BY mon ORDER BY mon
""").fetchall()

tot_k = tot_n = 0
worst = []
for r in rows:
    k = (r["kpd_spend"] or 0) / 1e6
    n = (r["net_spend"] or 0) / 1e6
    tot_k += k; tot_n += n
    delta = k - n
    pct = (100 * delta / k) if k else 0
    flag = "" if abs(pct) < 0.5 else ("  <-- gap" if pct > 0 else "  <-- net>kpd")
    print(f"  {r['mon']:<9} {k:>16,.2f} {n:>16,.2f} {delta:>14,.2f} {pct:>6.1f}%{flag}")
    if abs(pct) >= 0.5:
        worst.append((r["mon"], pct))

print(f"  {'-'*9} {'-'*16} {'-'*16} {'-'*14}")
print(f"  {'TOTAL':<9} {tot_k:>16,.2f} {tot_n:>16,.2f} {tot_k-tot_n:>14,.2f} "
      f"{100*(tot_k-tot_n)/tot_k:>6.2f}%")

print("\nInterpretation:")
print("  If TOTAL delta is ~0 and every month is within ~0.5%, the network table")
print("  is a complete spend decomposition of kpd — the instance-count 'gap' was")
print("  just orphaned historical instance_ids, not missing data. GOOD.")
print("  If some months show real gaps (kpd >> net), those months' network data")
print("  didn't load — a real gap to fix.")
print("  Any month where net > kpd (negative delta) means the network table has")
print("  spend kpd doesn't — that would indicate double-write or a stale-row issue.")

conn.close()
