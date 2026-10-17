"""
termlife_payout_probe.py
========================
Tests the hypothesis that Average_Cost = the per-lead amount SelectQuote pays
Quote.com (a pre-negotiated price, possibly banded by Lead_Tier), by checking:

  A. Population    - where avg_cost / lead_tier are filled, by channel
  B. Distribution  - is avg_cost a small set of repeated price points (banded)
                     or a continuous scatter (score-like / dynamic)?
  C. Banding       - does avg_cost step cleanly by Lead_Tier? by segment?
  D. Policy KPI    - reproduce SelectQuote's #1 metric (CRI Prem / Cost,
                     target 4) by month x segment, assuming
                     Cost = SUM(average_cost). THIS is the table to hold
                     against the Looker dashboard (Strategy=Search).
  E. Non-billable  - do leads SQ dispositioned as Duplicate (and Referral
                     streams) carry a price? Payout columns usually zero these.

Read-only; prints to console. Window = current batch (v_life_disposition_current).
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

SEG = {"CA": "Low", "CB": "Medium", "CC": "High", "QA": "Organic"}


def seg(code):
    return SEG.get(code[:2], code[:2])


def main():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    batch = con.execute(
        "SELECT MAX(batch_date) FROM life_disposition_snapshot").fetchone()[0]
    print(f"Payout probe — current batch {batch}\n")

    # A ------------------------------------------------------------------
    print("== A. POPULATION (avg_cost / lead_tier filled, by channel) ==")
    for ch, n, c_ok, t_ok in con.execute("""
            SELECT substr(source_code,3,1), COUNT(*),
                   SUM(average_cost IS NOT NULL), SUM(lead_tier IS NOT NULL)
            FROM v_life_disposition_current GROUP BY 1 ORDER BY 1"""):
        print(f"  channel {ch}: {n:5d} rows | avg_cost filled {c_ok:5d} "
              f"| lead_tier filled {t_ok:5d}")

    # B ------------------------------------------------------------------
    print("\n== B. AVG_COST VALUE DISTRIBUTION (banding check) ==")
    n_distinct, mn, mx, avg = con.execute("""
        SELECT COUNT(DISTINCT average_cost), MIN(average_cost),
               MAX(average_cost), ROUND(AVG(average_cost),2)
        FROM v_life_disposition_current WHERE average_cost IS NOT NULL""").fetchone()
    print(f"  distinct values: {n_distinct} | min {mn} | max {mx} | mean {avg}")
    print("  top 12 most common values:")
    for v, c in con.execute("""
            SELECT average_cost, COUNT(*) FROM v_life_disposition_current
            WHERE average_cost IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""):
        print(f"      {v:>8} x {c}")
    print("  (a handful of repeated values = banded price list;"
          " hundreds of distinct values = dynamic/score-like)")

    # C ------------------------------------------------------------------
    print("\n== C. AVG_COST BY LEAD_TIER (M-channel web leads) ==")
    print(f"  {'tier':>5} {'rows':>6} {'avg':>8} {'min':>8} {'max':>8}")
    for t, n, a, lo, hi in con.execute("""
            SELECT lead_tier, COUNT(*), ROUND(AVG(average_cost),2),
                   MIN(average_cost), MAX(average_cost)
            FROM v_life_disposition_current
            WHERE substr(source_code,3,1)='M' AND average_cost IS NOT NULL
            GROUP BY 1 ORDER BY CAST(lead_tier AS INT)"""):
        print(f"  {str(t):>5} {n:>6} {a:>8} {str(lo):>8} {str(hi):>8}")

    print("\n     ...and by segment x channel:")
    print(f"  {'segment':>8} {'ch':>3} {'rows':>6} {'avg':>8} {'min':>8} {'max':>8}")
    for s, ch, n, a, lo, hi in con.execute("""
            SELECT substr(source_code,1,2), substr(source_code,3,1), COUNT(*),
                   ROUND(AVG(average_cost),2), MIN(average_cost), MAX(average_cost)
            FROM v_life_disposition_current
            WHERE average_cost IS NOT NULL
            GROUP BY 1,2 ORDER BY 1,2"""):
        print(f"  {seg(s + 'xx'):>8} {ch:>3} {n:>6} {a:>8} {str(lo):>8} {str(hi):>8}")

    # D ------------------------------------------------------------------
    print("\n== D. POLICY KPI REPRODUCTION (CRI Prem / SUM(avg_cost); target 4) ==")
    print("   >> Hold this table against the SQLife Looker dashboard,"
          " Strategy=Search <<")
    print(f"  {'month':>8} {'segment':>8} {'leads':>6} {'payout$':>10} "
          f"{'cri_prem$':>11} {'policy_kpi':>10}")
    for m, s, n, pay, prem, kpi in con.execute("""
            SELECT strftime('%Y-%m', day) m, substr(source_code,1,2) s,
                   COUNT(*), ROUND(SUM(average_cost),0),
                   ROUND(SUM(capped_cri_dollars),0),
                   ROUND(SUM(capped_cri_dollars)/NULLIF(SUM(average_cost),0),2)
            FROM v_life_disposition_current
            GROUP BY 1,2 ORDER BY 1,2"""):
        print(f"  {m:>8} {seg(s + 'xx'):>8} {n:>6} {pay or 0:>10,.0f} "
              f"{prem or 0:>11,.0f} {kpi if kpi is not None else '':>10}")
    print("  --- monthly totals (all segments) ---")
    for m, n, pay, prem, kpi in con.execute("""
            SELECT strftime('%Y-%m', day), COUNT(*), ROUND(SUM(average_cost),0),
                   ROUND(SUM(capped_cri_dollars),0),
                   ROUND(SUM(capped_cri_dollars)/NULLIF(SUM(average_cost),0),2)
            FROM v_life_disposition_current GROUP BY 1 ORDER BY 1"""):
        print(f"  {m:>8} {'ALL':>8} {n:>6} {pay or 0:>10,.0f} "
              f"{prem or 0:>11,.0f} {kpi if kpi is not None else '':>10}")
    tot = con.execute("""
        SELECT COUNT(*), ROUND(SUM(average_cost),0),
               ROUND(SUM(capped_cri_dollars),0),
               ROUND(SUM(capped_cri_dollars)/NULLIF(SUM(average_cost),0),2)
        FROM v_life_disposition_current""").fetchone()
    print(f"  {'WINDOW':>8} {'ALL':>8} {tot[0]:>6} {tot[1] or 0:>10,.0f} "
          f"{tot[2] or 0:>11,.0f} {tot[3] if tot[3] is not None else '':>10}")

    # E ------------------------------------------------------------------
    print("\n== E. NON-BILLABLE CHECK ==")
    for label, where in [
            ("status = Duplicate", "status LIKE '%Duplicate%'"),
            ("status = Wrong Number", "status LIKE '%Wrong Number%'"),
            ("Referral streams (R)", "substr(source_code,3,1)='R'"),
            ("Transfer streams (T)", "substr(source_code,3,1)='T'")]:
        n, c_ok, a, s = con.execute(f"""
            SELECT COUNT(*), SUM(average_cost IS NOT NULL),
                   ROUND(AVG(average_cost),2), ROUND(SUM(average_cost),0)
            FROM v_life_disposition_current WHERE {where}""").fetchone()
        print(f"  {label:24s}: {n:5d} rows | avg_cost filled {c_ok or 0:4d} "
              f"| avg {a if a is not None else '-':>7} "
              f"| sum ${s or 0:,.0f}")
    print("  (if SQ doesn't pay for duplicates/returns, expect zeros or"
          " nulls there)")

    print("\nLOOKER CROSS-CHECK: compare section D's monthly 'payout$' to the"
          "\ndashboard's Cost, and 'policy_kpi' to its Policy KPI column."
          "\nMatches confirm avg_cost = per-lead payment; then"
          "\nSUM(average_cost) = Quote.com revenue at lead grain.")
    con.close()


if __name__ == "__main__":
    main()
