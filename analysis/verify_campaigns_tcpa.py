"""
verify_campaigns_tcpa.py
========================
Verifies where tCPA lives for Microsoft Home Insurance campaigns.

Specifically:
  1. What columns exist on the `campaigns` table for bid strategy / tCPA
  2. What the current values look like for the 12 active Home campaigns
  3. Whether ad_groups table has its own bid strategy columns (Microsoft
     allows ad-group-level tCPA)
  4. How target_cpa_micros maps to dollars (divide by 1e6)

Run:
    python verify_campaigns_tcpa.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("=" * 80)
    print("1. campaigns table — CREATE statement")
    print("=" * 80)
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='campaigns'"
    ).fetchone()
    if row:
        print(row[0])
    print()

    print("=" * 80)
    print("2. ad_groups table — CREATE statement (check for ad-group-level tCPA)")
    print("=" * 80)
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ad_groups'"
    ).fetchone()
    if row:
        print(row[0])
    print()

    print("=" * 80)
    print("3. Microsoft Home active campaigns — current bid strategy + tCPA snapshot")
    print("=" * 80)
    # Look at the active campaigns for account 2
    rows = cur.execute(
        """
        SELECT
            campaign_id,
            campaign_name,
            bidding_strategy_type,
            target_cpa_micros,
            target_cpa_micros / 1.0e6 AS target_cpa_dollars,
            target_roas,
            daily_budget_micros / 1.0e6 AS daily_budget_dollars,
            status
        FROM campaigns
        WHERE account_id = 2
        ORDER BY campaign_name
        """
    ).fetchall()
    cols = [d[0] for d in cur.description]
    print(f"\n  {'campaign_id':<12} {'campaign_name':<48} {'strategy':<15} "
          f"{'tCPA $':>10} {'budget $':>10} {'status':<10}")
    print("  " + "-" * 110)
    for r in rows:
        name = (r[1] or "")[:48]
        strat = (r[2] or "None")[:15]
        tcpa = f"${r[4]:.2f}" if r[4] is not None else "n/a"
        bud = f"${r[6]:.2f}" if r[6] is not None else "n/a"
        status = (r[7] or "")[:10]
        print(f"  {r[0]:<12} {name:<48} {strat:<15} {tcpa:>10} {bud:>10} {status:<10}")
    print()

    print("=" * 80)
    print("4. tCPA distribution — how many campaigns at which value")
    print("=" * 80)
    rows = cur.execute(
        """
        SELECT
            bidding_strategy_type,
            COUNT(*) AS n,
            MIN(target_cpa_micros / 1.0e6) AS min_tcpa,
            AVG(target_cpa_micros / 1.0e6) AS avg_tcpa,
            MAX(target_cpa_micros / 1.0e6) AS max_tcpa
        FROM campaigns
        WHERE account_id = 2
        GROUP BY bidding_strategy_type
        ORDER BY n DESC
        """
    ).fetchall()
    print(f"\n  {'strategy':<20} {'count':>6} {'min tCPA':>10} {'avg tCPA':>10} {'max tCPA':>10}")
    print("  " + "-" * 60)
    for r in rows:
        strat = (r[0] or "None")[:20]
        mn = f"${r[2]:.2f}" if r[2] is not None else "n/a"
        av = f"${r[3]:.2f}" if r[3] is not None else "n/a"
        mx = f"${r[4]:.2f}" if r[4] is not None else "n/a"
        print(f"  {strat:<20} {r[1]:>6} {mn:>10} {av:>10} {mx:>10}")
    print()

    print("=" * 80)
    print("5. Ad-group-level bid strategy (if present)")
    print("=" * 80)
    # Try to find tCPA columns on ad_groups
    cur.execute("PRAGMA table_info(ad_groups)")
    ag_cols = [r[1] for r in cur.fetchall()]
    tcpa_cols = [c for c in ag_cols if "cpa" in c.lower() or "bid" in c.lower() or "strategy" in c.lower()]
    if tcpa_cols:
        print(f"  Found bid/cpa-related columns on ad_groups: {tcpa_cols}")
        col_list = ", ".join(tcpa_cols)
        rows = cur.execute(
            f"""
            SELECT COUNT(*) AS n_total,
                   SUM(CASE WHEN {tcpa_cols[0]} IS NOT NULL THEN 1 ELSE 0 END) AS n_populated
            FROM ad_groups WHERE account_id = 2
            """
        ).fetchone()
        print(f"  ad_groups for account 2: total={rows[0]}, with {tcpa_cols[0]} populated={rows[1]}")
    else:
        print("  No bid-strategy or CPA columns on ad_groups — bid strategy lives at campaign grain only.")
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
