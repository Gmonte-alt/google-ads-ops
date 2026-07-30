"""
diagnose_roas_discrepancy.py
────────────────────────────────────────────────────────────────────
Standalone script to isolate why efficiency_frontier.py is reporting
30d ROAS ≈ 0.57 for the 7 enabled Home Insurance campaigns when the
actual figure from Google Ads UI is ≈ 1.15.

Runs 5 separate checks and prints results to terminal:

  1. Is there a device='ALL' rollup convention on campaign_performance_daily_search?
     If yes, my spend sum is double-counting.
  2. Compare 4 different ways to compute 30d spend & conv for one
     campaign (Tier One) to narrow down which layer is wrong.
  3. Check conversion_actions table — is primary_conv_value capturing
     the same set of actions the UI uses?
  4. Check all_conv_value vs primary_conv_value at the campaign level.
  5. Check keyword_instances for NULL campaign_id orphans.

Usage:
    python diagnose_roas_discrepancy.py
────────────────────────────────────────────────────────────────────
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

# Same 7 campaigns used in efficiency_frontier.py
ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # California
    "23394808297",   # Texas
    "23394808300",   # Florida
    "23428389081",   # Tier One
    "23496334752",   # Tier Two
    "23530860942",   # Tier Three
    "23586637678",   # Tier Four
]
TIER_ONE = "23428389081"   # pick one campaign to deep-dive

# Match the window used in the dry-run output you shared
WINDOW_START = date(2026, 3, 12)
WINDOW_END   = date(2026, 4, 10)

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.float_format", lambda x: f"{x:,.4f}")


def banner(msg: str) -> None:
    print("\n" + "═" * 78)
    print(msg)
    print("═" * 78)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════════════════════════
# CHECK 1: device rollup convention on campaign_performance_daily_search?
# ═══════════════════════════════════════════════════════════════════

def check_1_device_rollup(conn: sqlite3.Connection) -> None:
    banner("CHECK 1 — Device values in campaign_performance_daily_search")
    print("""
Hypothesis: if `campaign_performance_daily_search` stores both per-device rows
AND a rollup row (convention: device='ALL'), my SQL is summing both and
double-counting spend. Let's see what device values exist.
""")
    q = f"""
        SELECT device, COUNT(*) AS n_rows, SUM(cost_micros)/1e6 AS total_cost_usd
        FROM campaign_performance_daily_search
        WHERE account_id = {ACCOUNT_ID}
          AND date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'
          AND campaign_id IN ({','.join('?' * len(ENABLED_CAMPAIGN_IDS))})
        GROUP BY device
        ORDER BY total_cost_usd DESC
    """
    df = pd.read_sql(q, conn, params=ENABLED_CAMPAIGN_IDS)
    print(df.to_string(index=False))

    total = df["total_cost_usd"].sum()
    print(f"\n  Sum across ALL device values: ${total:,.0f}")
    if "ALL" in df["device"].values:
        all_val = df.loc[df["device"] == "ALL", "total_cost_usd"].iloc[0]
        per_device = df.loc[df["device"] != "ALL", "total_cost_usd"].sum()
        print(f"  ALL rollup row:               ${all_val:,.0f}")
        print(f"  Sum of per-device rows:       ${per_device:,.0f}")
        if abs(all_val - per_device) < all_val * 0.05:
            print("\n  ⚠  ROLLUP CONVENTION CONFIRMED: ALL ≈ sum of per-device.")
            print("     My SQL is double-counting. Need to filter device='ALL' OR exclude 'ALL'.")
        else:
            print("\n  Rollup and per-device differ significantly — not a pure rollup.")
    else:
        print("\n  No 'ALL' rollup row exists in campaign_performance_daily_search.")
        print("  This bug hypothesis is NOT the cause of the discrepancy.")


# ═══════════════════════════════════════════════════════════════════
# CHECK 2: four ways to compute Tier One 30d spend & conversions
# ═══════════════════════════════════════════════════════════════════

def check_2_four_way_reconciliation(conn: sqlite3.Connection) -> None:
    banner(f"CHECK 2 — Four ways to compute Tier One 30d totals ({WINDOW_START} to {WINDOW_END})")

    # Method A: The script's current logic — sum campaign_performance_daily_search
    q_a = f"""
        SELECT SUM(cost_micros)/1e6 AS cost_usd,
               SUM(conversions)     AS native_conv,
               SUM(conversion_value) AS native_value
        FROM campaign_performance_daily_search
        WHERE campaign_id = '{TIER_ONE}'
          AND account_id  = {ACCOUNT_ID}
          AND date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'
    """
    a = pd.read_sql(q_a, conn).iloc[0]

    # Method B: same but filtered device != 'ALL' (per-device only)
    q_b = f"""
        SELECT SUM(cost_micros)/1e6 AS cost_usd,
               SUM(conversions)     AS native_conv,
               SUM(conversion_value) AS native_value
        FROM campaign_performance_daily_search
        WHERE campaign_id = '{TIER_ONE}'
          AND account_id  = {ACCOUNT_ID}
          AND date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'
          AND device != 'ALL'
    """
    b = pd.read_sql(q_b, conn).iloc[0]

    # Method C: same but filtered device = 'ALL' (rollup only)
    q_c = f"""
        SELECT SUM(cost_micros)/1e6 AS cost_usd,
               SUM(conversions)     AS native_conv,
               SUM(conversion_value) AS native_value
        FROM campaign_performance_daily_search
        WHERE campaign_id = '{TIER_ONE}'
          AND account_id  = {ACCOUNT_ID}
          AND date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'
          AND device = 'ALL'
    """
    c = pd.read_sql(q_c, conn).iloc[0]

    # Method D: keyword_performance_daily with device='ALL' and geo_state='ALL'
    # (matches the primary_conv_value path used in efficiency_frontier.py)
    q_d = f"""
        SELECT SUM(kpd.cost_micros)/1e6     AS cost_usd,
               SUM(kpd.primary_conv_count)  AS primary_conv,
               SUM(kpd.primary_conv_value)  AS primary_value,
               SUM(kpd.all_conv_count)      AS all_conv,
               SUM(kpd.all_conv_value)      AS all_value
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.campaign_id = '{TIER_ONE}'
          AND ki.account_id  = {ACCOUNT_ID}
          AND kpd.report_date BETWEEN '{WINDOW_START}' AND '{WINDOW_END}'
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
    """
    d = pd.read_sql(q_d, conn).iloc[0]

    print(f"\nMethod A — campaign_performance_daily_search, NO device filter (what script currently does):")
    print(f"    cost_usd = ${a['cost_usd']:,.2f}    native_conv = {a['native_conv']:,.2f}    "
          f"native_value = ${a['native_value']:,.2f}")
    if a['cost_usd'] > 0:
        print(f"    native ROAS = {a['native_value']/a['cost_usd']:.4f}")

    print(f"\nMethod B — campaign_performance_daily_search, device != 'ALL':")
    print(f"    cost_usd = ${b['cost_usd']:,.2f}    native_conv = {b['native_conv']:,.2f}    "
          f"native_value = ${b['native_value']:,.2f}")
    if b['cost_usd'] > 0:
        print(f"    native ROAS = {b['native_value']/b['cost_usd']:.4f}")

    print(f"\nMethod C — campaign_performance_daily_search, device = 'ALL' (rollup only):")
    print(f"    cost_usd = ${c['cost_usd']:,.2f}    native_conv = {c['native_conv']:,.2f}    "
          f"native_value = ${c['native_value']:,.2f}")
    if c['cost_usd'] > 0:
        print(f"    native ROAS = {c['native_value']/c['cost_usd']:.4f}")

    print(f"\nMethod D — keyword_performance_daily, device='ALL' AND geo_state='ALL' (primary_conv path):")
    print(f"    cost_usd      = ${d['cost_usd']:,.2f}")
    print(f"    primary_conv  = {d['primary_conv']:,.2f}    primary_value = ${d['primary_value']:,.2f}")
    print(f"    all_conv      = {d['all_conv']:,.2f}    all_value     = ${d['all_value']:,.2f}")
    if d['cost_usd'] > 0:
        print(f"    primary ROAS = {d['primary_value']/d['cost_usd']:.4f}")
        print(f"    all     ROAS = {d['all_value']/d['cost_usd']:.4f}")

    print("\nInterpretation:")
    print("  → If B ≈ C and A ≈ 2×B, there's a rollup double-count.")
    print("  → If 'all ROAS' in method D is ≈ 1.15 but 'primary ROAS' ≈ 0.57,")
    print("    the issue is conversion_actions routing — UI uses a broader set")
    print("    of actions than primary_conv_value captures.")
    print("  → Compare method A cost vs UI cost for Tier One to see if spend matches.")


# ═══════════════════════════════════════════════════════════════════
# CHECK 3: conversion_actions table contents
# ═══════════════════════════════════════════════════════════════════

def check_3_conversion_actions(conn: sqlite3.Connection) -> None:
    banner("CHECK 3 — conversion_actions table for account_id=1")
    q = f"""
        SELECT id, conversion_name, include_mode, notes
        FROM conversion_actions
        WHERE account_id = {ACCOUNT_ID}
        ORDER BY id
    """
    df = pd.read_sql(q, conn)
    print(f"\n  Found {len(df)} conversion actions for Home Insurance (account_id=1):\n")
    if not df.empty:
        print(df.to_string(index=False))
        print(f"\n  include_mode values: {df['include_mode'].value_counts().to_dict()}")
    else:
        print("  (empty — conversion_actions not populated for this account)")


# ═══════════════════════════════════════════════════════════════════
# CHECK 4: all_conv_value vs primary_conv_value at campaign level
# ═══════════════════════════════════════════════════════════════════

def check_4_primary_vs_all(conn: sqlite3.Connection) -> None:
    banner("CHECK 4 — primary_conv_value vs all_conv_value for all 7 campaigns")
    print("""
If UI ROAS ≈ 1.15 but our primary ROAS ≈ 0.57, and all_conv_value ROAS
matches the UI, then the UI is using all-conversions (or a different
conversion action routing) than primary_conv_value.
""")
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    q = f"""
        SELECT ki.campaign_id, ki.campaign_name,
               SUM(kpd.cost_micros)/1e6    AS cost_usd,
               SUM(kpd.primary_conv_count) AS primary_conv,
               SUM(kpd.primary_conv_value) AS primary_value,
               SUM(kpd.all_conv_count)     AS all_conv,
               SUM(kpd.all_conv_value)     AS all_value
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.campaign_id IN ({placeholders})
          AND ki.account_id  = {ACCOUNT_ID}
          AND kpd.report_date BETWEEN ? AND ?
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        GROUP BY ki.campaign_id, ki.campaign_name
    """
    params = ENABLED_CAMPAIGN_IDS + [str(WINDOW_START), str(WINDOW_END)]
    df = pd.read_sql(q, conn, params=params)
    df["primary_roas"] = df["primary_value"] / df["cost_usd"]
    df["all_roas"]     = df["all_value"]     / df["cost_usd"]
    df["primary_name"] = df["campaign_name"].str.extract(r"HOME INSURANCE-([^-]+(?:\s[^-]+)*)-NB")
    print(df[["primary_name", "cost_usd", "primary_value", "all_value",
              "primary_roas", "all_roas"]].to_string(index=False))

    # Totals
    total_cost    = df["cost_usd"].sum()
    total_primary = df["primary_value"].sum()
    total_all     = df["all_value"].sum()
    print(f"\n  Totals across 7 campaigns:")
    print(f"    cost          = ${total_cost:,.2f}")
    print(f"    primary_value = ${total_primary:,.2f}   (blended primary ROAS = "
          f"{total_primary/total_cost:.4f})")
    print(f"    all_value     = ${total_all:,.2f}   (blended all ROAS     = "
          f"{total_all/total_cost:.4f})")


# ═══════════════════════════════════════════════════════════════════
# CHECK 5: keyword_instances with NULL campaign_id
# ═══════════════════════════════════════════════════════════════════

def check_5_orphan_keyword_instances(conn: sqlite3.Connection) -> None:
    banner("CHECK 5 — keyword_instances with NULL or mismatched campaign_id")
    print("""
If any keyword_instances rows have NULL campaign_id or a campaign_id
that doesn't exist in campaigns, their conversions are silently
dropped from the campaign-level rollup.
""")
    q = f"""
        SELECT COUNT(*) AS n_total,
               SUM(CASE WHEN campaign_id IS NULL THEN 1 ELSE 0 END) AS n_null,
               SUM(CASE WHEN campaign_id = '' THEN 1 ELSE 0 END)   AS n_empty
        FROM keyword_instances
        WHERE account_id = {ACCOUNT_ID}
    """
    r = pd.read_sql(q, conn).iloc[0]
    print(f"  Total keyword_instances for account 1: {r['n_total']:,}")
    print(f"  With NULL campaign_id:                 {r['n_null']:,}")
    print(f"  With empty string campaign_id:         {r['n_empty']:,}")

    # Also check: any instances linked to the 7 campaigns whose ad_group is paused?
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    q2 = f"""
        SELECT ki.campaign_id, ag.status AS ad_group_status, COUNT(*) AS n_instances
        FROM keyword_instances ki
        JOIN ad_groups ag ON ag.ad_group_id = ki.ad_group_id
        WHERE ki.campaign_id IN ({placeholders})
          AND ki.account_id  = {ACCOUNT_ID}
        GROUP BY ki.campaign_id, ag.status
        ORDER BY ki.campaign_id, ag.status
    """
    df2 = pd.read_sql(q2, conn, params=ENABLED_CAMPAIGN_IDS)
    print("\n  keyword_instances by campaign × ad_group status:")
    print(df2.to_string(index=False))


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    conn = connect()

    print(f"Diagnostic window: {WINDOW_START} to {WINDOW_END}")
    print(f"Script expects: 30d ROAS ≈ 1.15 per user")
    print(f"Script currently reports: 30d ROAS ≈ 0.57")

    check_1_device_rollup(conn)
    check_2_four_way_reconciliation(conn)
    check_3_conversion_actions(conn)
    check_4_primary_vs_all(conn)
    check_5_orphan_keyword_instances(conn)

    banner("NEXT STEPS")
    print("""
After running this script:

1. Open the Google Ads UI and pull for Tier One specifically:
   - Date range: 2026-03-12 to 2026-04-10
   - Campaign: HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
   - Columns: Cost, All conv., All conv. value, Conversions, Conv. value

2. Compare UI numbers to Method A / Method D above to isolate where
   the discrepancy lives.

3. Common outcomes:
   a) Method A cost matches UI → no rollup bug. Spend is fine.
   b) Method A cost is 2× UI → rollup bug. Filter device='ALL' only.
   c) all_value in Method D matches UI revenue → primary_conv routing
      is excluding the conversion actions the UI uses. Talk to the
      architect about updating conversion_actions.include_mode OR
      switch the script to use all_conv_value.
   d) Everything matches UI cost but revenue is half UI → some
      conversion actions aren't being synced into the DB at all.
""")
    conn.close()


if __name__ == "__main__":
    main()
