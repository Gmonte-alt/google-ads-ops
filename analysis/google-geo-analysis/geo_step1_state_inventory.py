"""
geo_step1_state_inventory.py
=============================
Step 1 of the Home Insurance geo restructure analysis.

Purpose:
  Build an inventory of every (campaign, geo_state) combination that appears
  in campaign_performance_geo_daily for account_id = 1. This validates the
  state-to-campaign assignments before the full analysis script is built.

Output (terminal only — no Excel at this stage):
  - Per-campaign state list with first/last date, days active, spend
  - Currently-active flag (state had activity in last 30 days ending at --end-date)
  - Cross-tier duplicate detection and resolution via most-recent-activity rule

Device convention:
  campaign_performance_geo_daily has NO device='ALL' rollup — device values
  are MOBILE, DESKTOP, TABLET, CONNECTED_TV, OTHER. Sum across all device rows.

Usage:
  python analysis\google-geo-analysis\geo_step1_state_inventory.py
  python analysis\google-geo-analysis\geo_step1_state_inventory.py --end-date 2026-04-20
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

# ─── Config (matches full-script conventions) ────────────────────────────────
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

TIER_CAMPAIGN_KEYWORDS = ("TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_inventory(conn, end_date):
    """
    Per (campaign, geo_state): first date, last date, days active,
    total spend, clicks, conversions. Summed across all devices.
    """
    sql = """
        SELECT
            c.campaign_id,
            c.campaign_name,
            cpg.geo_state,
            MIN(cpg.date)                          AS first_date,
            MAX(cpg.date)                          AS last_date,
            COUNT(DISTINCT cpg.date)               AS days_active,
            SUM(cpg.impressions)                   AS impressions,
            SUM(cpg.clicks)                        AS clicks,
            SUM(cpg.cost_micros) / 1e6             AS spend,
            SUM(cpg.conversions)                   AS conversions
        FROM campaign_performance_geo_daily cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND cpg.geo_state IS NOT NULL
        GROUP BY c.campaign_id, c.campaign_name, cpg.geo_state
        ORDER BY c.campaign_name, spend DESC
    """
    return conn.execute(sql, (ACCOUNT_ID,)).fetchall()


def resolve_cross_tier(rows, end_date):
    """
    For states that appear in >1 tier campaign, keep the row with the most
    recent last_date; mark the others as cross_tier_dropped.

    Returns a list of dicts with all original fields plus:
      - currently_active (bool): last_date within 30 days of end_date
      - cross_tier_dropped (bool): True for the non-winning tier row(s)
      - cross_tier_note (str): explanation if part of a cross-tier set
    """
    # Group by state, but only consider tier campaigns for cross-tier logic.
    # Single-state campaigns (CA/TX/FL) are excluded from the dedup because
    # they're scoped campaigns, not tiered.
    state_to_tier_rows = defaultdict(list)
    out = []

    active_cutoff = end_date - timedelta(days=30)

    for r in rows:
        d = dict(r)
        d["last_date_obj"] = date.fromisoformat(r["last_date"])
        d["first_date_obj"] = date.fromisoformat(r["first_date"])
        d["currently_active"] = d["last_date_obj"] >= active_cutoff
        d["cross_tier_dropped"] = False
        d["cross_tier_note"] = ""

        is_tier_campaign = any(kw in r["campaign_name"] for kw in TIER_CAMPAIGN_KEYWORDS)
        if is_tier_campaign:
            state_to_tier_rows[r["geo_state"]].append(d)
        out.append(d)

    # Now find states with >1 tier row and resolve
    for state, tier_rows in state_to_tier_rows.items():
        if len(tier_rows) <= 1:
            continue
        # Winner = most recent last_date; ties broken by higher spend
        winner = max(tier_rows, key=lambda x: (x["last_date_obj"], x["spend"]))
        for tr in tier_rows:
            if tr is winner:
                tr["cross_tier_note"] = (
                    f"KEPT (most recent activity {tr['last_date']}; "
                    f"also appears in {len(tier_rows) - 1} other tier(s))"
                )
            else:
                tr["cross_tier_dropped"] = True
                tr["cross_tier_note"] = (
                    f"DROPPED (stale; last activity {tr['last_date']}, "
                    f"winner was {winner['campaign_name'].split('-')[1]} "
                    f"with last activity {winner['last_date']})"
                )

    return out


def short_name(campaign_name):
    """Strip the boilerplate from campaign names for terminal display."""
    return (campaign_name
            .replace("HOME INSURANCE-", "")
            .replace("-NB-SEARCH-ALL-USA-STE", ""))


def print_inventory(inventory, end_date):
    # Group by campaign for display, preserving original order
    by_campaign = defaultdict(list)
    campaign_order = []
    for row in inventory:
        cname = row["campaign_name"]
        if cname not in by_campaign:
            campaign_order.append(cname)
        by_campaign[cname].append(row)

    print("=" * 88)
    print("STEP 1 — STATE INVENTORY PER CAMPAIGN")
    print(f"  Database : {DB_PATH}")
    print(f"  Account  : account_id = {ACCOUNT_ID}")
    print(f"  End date : {end_date}  (currently-active cutoff: {end_date - timedelta(days=30)})")
    print("=" * 88)

    # Health summary across all campaigns shown
    all_spend  = sum(r["spend"] for r in inventory)
    all_states = len(set(r["geo_state"] for r in inventory))
    n_pairs    = len(inventory)
    n_dropped  = sum(1 for r in inventory if r["cross_tier_dropped"])
    print(f"\n  Unique (campaign × state) pairs : {n_pairs:,}"
          f"   (of which {n_dropped} will be dropped as cross-tier stale)")
    print(f"  Distinct states                 : {all_states}")
    print(f"  Total spend (all time)          : ${all_spend:,.2f}")
    print()

    for cname in campaign_order:
        rows = by_campaign[cname]
        active_count = sum(1 for r in rows if r["currently_active"] and not r["cross_tier_dropped"])
        dropped_count = sum(1 for r in rows if r["cross_tier_dropped"])

        header = f"\n  {short_name(cname)}"
        meta = f"  states: {len(rows)}, active: {active_count}"
        if dropped_count:
            meta += f", dropped (cross-tier): {dropped_count}"
        print(header)
        print(f"  {'─' * 84}")
        print(meta)
        print()
        print(f"    {'State':<20} {'First':<11} {'Last':<11} {'Days':>5} "
              f"{'Spend':>12} {'Active':>7}  Note")
        print(f"    {'─'*20} {'─'*11} {'─'*11} {'─'*5} "
              f"{'─'*12} {'─'*7}  {'─'*40}")

        for r in rows:
            active_mark = "YES" if r["currently_active"] else "no"
            if r["cross_tier_dropped"]:
                active_mark = "DROP"
            state = str(r["geo_state"] or "Unknown")
            print(f"    {state:<20} {r['first_date']:<11} {r['last_date']:<11} "
                  f"{r['days_active']:>5} ${r['spend']:>11,.2f} "
                  f"{active_mark:>7}  {r['cross_tier_note']}")

    # Cross-tier summary block
    print()
    print("─" * 88)
    print("CROSS-TIER DUPLICATE SUMMARY")
    print("─" * 88)

    states_by_tier_count = defaultdict(list)
    for r in inventory:
        if any(kw in r["campaign_name"] for kw in TIER_CAMPAIGN_KEYWORDS):
            states_by_tier_count[r["geo_state"]].append(r)

    duplicates = {s: rs for s, rs in states_by_tier_count.items() if len(rs) > 1}
    if not duplicates:
        print("  No cross-tier duplicates detected.")
    else:
        for state, rs in duplicates.items():
            print(f"\n  {state}:")
            for r in sorted(rs, key=lambda x: x["last_date_obj"], reverse=True):
                status = "KEPT " if not r["cross_tier_dropped"] else "DROP "
                print(f"    [{status}] {short_name(r['campaign_name']):<15} "
                      f"last={r['last_date']}  spend=${r['spend']:>10,.2f}  "
                      f"days={r['days_active']}")

    # Target state confirmation
    print()
    print("─" * 88)
    print("TARGET STATE CONFIRMATION (restructure inputs)")
    print("─" * 88)
    target_states = ["Colorado", "Utah", "South Dakota"]
    for state in target_states:
        matches = [r for r in inventory
                   if r["geo_state"] == state
                   and "TIER TWO" in r["campaign_name"]
                   and not r["cross_tier_dropped"]]
        if matches:
            m = matches[0]
            print(f"  ✓ {state:<15} in Tier Two  "
                  f"last={m['last_date']}  "
                  f"spend=${m['spend']:,.2f}  "
                  f"active={'yes' if m['currently_active'] else 'NO'}")
        else:
            print(f"  ✗ {state:<15} NOT FOUND in Tier Two")

    print()
    print("=" * 88)
    print("Next step: review the inventory above, confirm the cross-tier attribution,")
    print("then the full geo_restructure_home.py script will be built on top of this.")
    print("=" * 88)


def main():
    p = argparse.ArgumentParser(description="Step 1: geo state inventory per campaign.")
    p.add_argument("--end-date", default=None,
                   help="End date YYYY-MM-DD (default: yesterday). Controls "
                        "the currently-active cutoff (last 30 days pre end).")
    args = p.parse_args()

    end_date = (date.fromisoformat(args.end_date) if args.end_date
                else date.today() - timedelta(days=1))

    conn = get_conn()
    rows = fetch_inventory(conn, end_date)
    conn.close()

    if not rows:
        print(f"No rows found in campaign_performance_geo_daily for account_id={ACCOUNT_ID}.")
        return

    inventory = resolve_cross_tier(rows, end_date)
    print_inventory(inventory, end_date)


if __name__ == "__main__":
    main()
