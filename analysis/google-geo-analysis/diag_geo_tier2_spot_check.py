"""
diag_geo_tier2_spot_check.py
=============================
Spot check for the Google geo restructure analysis.

Confirms that Colorado, Utah, and South Dakota appear in the
Tier Two campaign in campaign_performance_geo_daily_search, and shows
their relative spend weights before the full geo analysis runs.

Also shows a summary of all states across all 7 enabled Home
Insurance campaigns so you can see the full geo distribution.

RUN
───
    conda activate paid-search
    python analysis\google-geo-analysis\diag_geo_tier2_spot_check.py
    python analysis\google-geo-analysis\diag_geo_tier2_spot_check.py --days 90
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import date, timedelta

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE
    "23394808297",   # HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE
    "23394808300",   # HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE
    "23428389081",   # HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
    "23496334752",   # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
    "23530860942",   # HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE
    "23586637678",   # HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE
]

TARGET_STATES = ["Colorado", "Utah", "South Dakota"]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def main():
    p = argparse.ArgumentParser(
        description="Spot check geo data for the Home Insurance Tier Two restructure."
    )
    p.add_argument("--days", type=int, default=90,
                   help="Rolling window in days (default: 90)")
    p.add_argument("--end-date", default=None,
                   help="End date YYYY-MM-DD (default: yesterday)")
    args = p.parse_args()

    end   = date.fromisoformat(args.end_date) if args.end_date \
            else date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)

    conn = get_conn()
    ph   = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))

    print("=" * 72)
    print("diag_geo_tier2_spot_check.py")
    print(f"  Database : {DB_PATH}")
    print(f"  Window   : {start} → {end}  ({args.days}d)")
    print(f"  Account  : account_id = {ACCOUNT_ID}")
    print("=" * 72)

    # ── 1. Table health check ──────────────────────────────────────────────
    print("\n── 1. campaign_performance_geo_daily_search health check ────────────────────\n")

    total = conn.execute("""
        SELECT COUNT(*) AS rows, MIN(date) AS min_date, MAX(date) AS max_date,
               SUM(cost_micros)/1e6 AS spend,
               COUNT(DISTINCT geo_state) AS states
        FROM campaign_performance_geo_daily_search
        WHERE account_id = ?
        -- No device='ALL' rollup in geo table — sum across all devices
    """, (ACCOUNT_ID,)).fetchone()

    if total["rows"] == 0:
        print("  ✗ No rows found. Run the geo backfill first:")
        print("    python campaign_db_scripts/03_sync_campaign_performance.py "
              "--geo-start 2025-01-01 --geo-only")
        conn.close()
        return

    print(f"  Total rows (all devices) : {total['rows']:,}")
    print(f"  Date range               : {total['min_date']} → {total['max_date']}")
    print(f"  Total spend              : ${total['spend']:,.2f}")
    print(f"  Distinct geo_states      : {total['states']}")

    # ── 2. Tier Two state breakdown ────────────────────────────────────────
    print("\n── 2. Tier Two states — spend breakdown ──────────────────────────────\n")

    tier2_rows = conn.execute(f"""
        SELECT
            cpg.geo_state,
            COUNT(DISTINCT cpg.date)      AS days_active,
            SUM(cpg.impressions)          AS impressions,
            SUM(cpg.clicks)               AS clicks,
            SUM(cpg.cost_micros) / 1e6    AS spend,
            SUM(cpg.conversions)          AS conversions,
            SUM(cpg.conversion_value)     AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND c.campaign_name LIKE '%TIER TWO%'
          AND cpg.date BETWEEN ? AND ?
        GROUP BY cpg.geo_state
        ORDER BY spend DESC
    """, (ACCOUNT_ID, start.isoformat(), end.isoformat())).fetchall()

    if not tier2_rows:
        print("  ✗ No Tier Two rows found in this window.")
    else:
        tier2_total_spend = sum(r["spend"] for r in tier2_rows)
        print(f"  {'State':<20} {'Days':>5} {'Impressions':>12} {'Clicks':>8} "
              f"{'Spend':>12} {'% of T2':>9} {'Convs':>7} {'Revenue':>12}")
        print(f"  {'─'*20} {'─'*5} {'─'*12} {'─'*8} "
              f"{'─'*12} {'─'*9} {'─'*7} {'─'*12}")

        found_states = set()
        for r in tier2_rows:
            state    = str(r["geo_state"] or "Unknown")
            pct      = r["spend"] / tier2_total_spend * 100 if tier2_total_spend > 0 else 0
            marker   = " ← TARGET" if state in TARGET_STATES else ""
            found_states.add(state)
            print(f"  {state:<20} {r['days_active']:>5} {r['impressions']:>12,} "
                  f"{r['clicks']:>8,} ${r['spend']:>11,.2f} {pct:>8.1f}% "
                  f"{r['conversions']:>7,.1f} ${r['revenue']:>11,.2f}{marker}")

        print(f"\n  {'TIER TWO TOTAL':<20} {'':>5} {'':>12} {'':>8} "
              f"${tier2_total_spend:>11,.2f} {'100.0%':>9}")

        # Check which target states were found
        print()
        for state in TARGET_STATES:
            if state in found_states:
                print(f"  ✓ {state} present in Tier Two data")
            else:
                print(f"  ✗ {state} NOT FOUND in Tier Two data in this window")

    # ── 3. Colorado isolation vs Tier Two total ────────────────────────────
    print("\n── 3. Colorado isolation check ───────────────────────────────────────\n")

    colorado = conn.execute("""
        SELECT
            SUM(cpg.cost_micros) / 1e6  AS spend,
            SUM(cpg.clicks)             AS clicks,
            SUM(cpg.conversions)        AS conversions,
            SUM(cpg.conversion_value)   AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND c.campaign_name LIKE '%TIER TWO%'
          AND cpg.geo_state = 'Colorado'
          AND cpg.date BETWEEN ? AND ?
    """, (ACCOUNT_ID, start.isoformat(), end.isoformat())).fetchone()

    tier2_total = conn.execute("""
        SELECT
            SUM(cpg.cost_micros) / 1e6  AS spend,
            SUM(cpg.clicks)             AS clicks,
            SUM(cpg.conversions)        AS conversions,
            SUM(cpg.conversion_value)   AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND c.campaign_name LIKE '%TIER TWO%'
          AND cpg.date BETWEEN ? AND ?
    """, (ACCOUNT_ID, start.isoformat(), end.isoformat())).fetchone()

    if tier2_total["spend"] and tier2_total["spend"] > 0:
        co_pct = (colorado["spend"] or 0) / tier2_total["spend"] * 100
        print(f"  Colorado spend    : ${colorado['spend']:>10,.2f}  "
              f"({co_pct:.1f}% of Tier Two total)")
        print(f"  Tier Two total    : ${tier2_total['spend']:>10,.2f}")
        print(f"  Colorado clicks   : {colorado['clicks']:>10,}")
        print(f"  Colorado convs    : {colorado['conversions']:>10,.1f}")
        print(f"  Colorado revenue  : ${colorado['revenue']:>10,.2f}")
        if co_pct >= 50:
            print(f"\n  ✓ Colorado is {co_pct:.1f}% of Tier Two spend — "
                  "viable as a standalone campaign.")
        else:
            print(f"\n  ⚠  Colorado is only {co_pct:.1f}% of Tier Two spend "
                  "(below 50% threshold).")
            print("     Confirm the restructure intent before renaming the campaign.")
    else:
        print("  No Tier Two data found in this window.")

    # ── 4. All states across all 7 campaigns ──────────────────────────────
    print("\n── 4. All states across all 7 enabled campaigns ──────────────────────\n")

    all_states = conn.execute(f"""
        SELECT
            c.campaign_name,
            cpg.geo_state,
            SUM(cpg.cost_micros) / 1e6  AS spend,
            SUM(cpg.clicks)             AS clicks,
            SUM(cpg.conversions)        AS conversions
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND cpg.campaign_id IN ({ph})
          AND cpg.date BETWEEN ? AND ?
          AND cpg.geo_state IS NOT NULL
        GROUP BY c.campaign_id, cpg.geo_state
        ORDER BY c.campaign_name, spend DESC
    """, [ACCOUNT_ID] + ENABLED_CAMPAIGN_IDS +
         [start.isoformat(), end.isoformat()]).fetchall()

    current_camp = None
    for r in all_states:
        camp = str(r["campaign_name"] or "").replace(
            "HOME INSURANCE-", "").replace("-NB-SEARCH-ALL-USA-STE", "")
        if camp != current_camp:
            print(f"\n  {camp}")
            print(f"  {'─'*68}")
            current_camp = camp
        state  = str(r["geo_state"] or "Unknown")
        marker = " ← MOVING OUT" if state in ["Utah", "South Dakota"] else ""
        marker = " ← STAYING"   if state == "Colorado" and "TIER TWO" in str(r["campaign_name"]) else marker
        print(f"    {state:<25} ${r['spend']:>10,.2f}  "
              f"{r['clicks']:>6,} clicks  {r['conversions']:>6,.1f} convs{marker}")

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print()
    print("  If all three target states appear in Tier Two above with spend data,")
    print("  the geo backfill is complete and the full geo_restructure_home.py")
    print("  analysis script can be run in a new chat thread.")
    print()
    print("  Next step:")
    print("  python analysis/geo_restructure_home.py --days 90")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
