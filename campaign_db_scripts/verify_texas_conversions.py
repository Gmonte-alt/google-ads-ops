"""
verify_texas_conversions.py
===========================
Compares Microsoft Ads UI numbers against the database for the TEXAS
campaign over May 4-10, 2026, to verify whether the device='ALL' rollup
is correctly counting All conversions or whether cross-device
de-duplication is causing inflation.

Place this script anywhere convenient (e.g., the repo root) and run:

    conda activate paid-search
    python verify_texas_conversions.py

No flags. Read-only. Database path is hardcoded for the Microsoft
Home Insurance account.

WHAT IT PRINTS
  1. Campaign resolution sanity check (campaign_id, name, account_id)
  2. Raw rows in campaign_performance_daily and ..._network_daily
     for the Texas campaign, May 4-10, all device values
  3. Comparison table: UI value vs DB device='ALL' vs DB per-device-sum
     for spend, conversions, and conversion_value across both networks
  4. Diagnosis: which DB value matches the UI, which doesn't, and by how much
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2  # Microsoft Home Insurance
CAMPAIGN_NAME = "HOME INSURANCE-TEXAS-SEARCH-MOBILE-USA-STE"
START_DATE = "2026-05-04"
END_DATE = "2026-05-10"

# UI values from your message
UI_VALUES = {
    "search": {
        "spend": 870.48 + 0.00,         # Microsoft sites + AOL search
        "conv_pixel": 27 + 0,           # UI 'Conv.' column
        "all_conv": 58 + 0,             # UI 'All conv.' column
        "all_revenue": 2261.38 + 0.00,  # UI 'All conv. revenue' column
    },
    "audience": {
        "spend": 191.05,
        "conv_pixel": 3,
        "all_conv": 10,
        "all_revenue": 496.87,
    },
}


def banner(text, char="=", width=78):
    print(char * width)
    print(text)
    print(char * width)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── Step 1: Resolve campaign and confirm we're looking at the right one ──
    banner(f"STEP 1: CAMPAIGN RESOLUTION")
    print(f"Looking up campaign by name: {CAMPAIGN_NAME}")
    print(f"Account ID filter: {ACCOUNT_ID}")
    print()

    camp = conn.execute(
        """
        SELECT campaign_id, campaign_name, status, target_cpa_micros / 1.0e6 AS tcpa
        FROM campaigns
        WHERE account_id = ? AND campaign_name = ?
        """,
        (ACCOUNT_ID, CAMPAIGN_NAME),
    ).fetchone()

    if camp is None:
        print(f"ERROR: Campaign not found.")
        print(f"Listing all enabled campaigns to help find the right name:")
        for r in conn.execute(
            "SELECT campaign_id, campaign_name FROM campaigns "
            "WHERE account_id = ? AND status = 'ENABLED' ORDER BY campaign_name",
            (ACCOUNT_ID,),
        ).fetchall():
            print(f"  {r['campaign_id']:<20} {r['campaign_name']}")
        return 1

    print(f"  campaign_id  : {camp['campaign_id']}")
    print(f"  campaign_name: {camp['campaign_name']}")
    print(f"  status       : {camp['status']}")
    print(f"  target_cpa   : ${camp['tcpa']:.2f}" if camp['tcpa'] else "  target_cpa   : (not set)")
    print()

    campaign_id = camp["campaign_id"]

    # ── Step 2: Show all raw rows in both performance tables ──
    banner(f"STEP 2: RAW ROWS IN campaign_performance_daily (SEARCH)")
    print(f"Filter: campaign_id={campaign_id}, date BETWEEN {START_DATE} AND {END_DATE}")
    print()

    cpd_cols = [r["name"] for r in conn.execute("PRAGMA table_info(campaign_performance_daily)").fetchall()]
    # Show only the columns we care about, plus device
    cols_of_interest = [c for c in ["date", "device", "geo_state", "network",
                                     "cost_micros", "conversions", "conversion_value",
                                     "primary_conv_count", "primary_conv_value",
                                     "all_conv_count", "all_conv_value",
                                     "impressions", "clicks"] if c in cpd_cols]
    print(f"  Available columns of interest in campaign_performance_daily: {cols_of_interest}")
    print()

    rows = conn.execute(
        f"""
        SELECT {", ".join(cols_of_interest)}
        FROM campaign_performance_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
        ORDER BY date, device
        """,
        (campaign_id, START_DATE, END_DATE),
    ).fetchall()

    if not rows:
        print(f"  (no rows)")
    else:
        print(f"  Showing {len(rows)} rows...")
        # Print header
        print("  " + " | ".join(f"{c:>16}" for c in cols_of_interest))
        for r in rows[:50]:  # cap at 50 for readability
            print("  " + " | ".join(
                f"{(v if v is not None else ''):>16}" for v in (r[c] for c in cols_of_interest)
            ))
        if len(rows) > 50:
            print(f"  ... ({len(rows) - 50} more rows omitted)")
    print()

    banner(f"STEP 2b: RAW ROWS IN campaign_performance_network_daily (NON-SEARCH)")
    cpnd_cols = [r["name"] for r in conn.execute("PRAGMA table_info(campaign_performance_network_daily)").fetchall()]
    cols_of_interest_n = [c for c in ["date", "device", "geo_state", "network",
                                       "cost_micros", "conversions", "conversion_value",
                                       "primary_conv_count", "primary_conv_value",
                                       "all_conv_count", "all_conv_value",
                                       "impressions", "clicks"] if c in cpnd_cols]
    print(f"  Available columns: {cols_of_interest_n}")
    print()

    rows_n = conn.execute(
        f"""
        SELECT {", ".join(cols_of_interest_n)}
        FROM campaign_performance_network_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
        ORDER BY date, device, network
        """,
        (campaign_id, START_DATE, END_DATE),
    ).fetchall()

    if not rows_n:
        print(f"  (no rows)")
    else:
        print(f"  Showing {len(rows_n)} rows...")
        print("  " + " | ".join(f"{c:>16}" for c in cols_of_interest_n))
        for r in rows_n[:50]:
            print("  " + " | ".join(
                f"{(v if v is not None else ''):>16}" for v in (r[c] for c in cols_of_interest_n)
            ))
        if len(rows_n) > 50:
            print(f"  ... ({len(rows_n) - 50} more rows omitted)")
    print()

    # ── Step 3: Aggregation comparison ──
    banner(f"STEP 3: AGGREGATED COMPARISON — DB vs UI")

    # Detect the right conversion column names
    if "conversions" in cpd_cols and "conversion_value" in cpd_cols:
        conv_col_search = "conversions"
        rev_col_search = "conversion_value"
    elif "primary_conv_count" in cpd_cols:
        conv_col_search = "primary_conv_count"
        rev_col_search = "primary_conv_value"
    else:
        conv_col_search = cpd_cols[0]  # fallback
        rev_col_search = cpd_cols[0]
        print(f"  WARNING: couldn't detect conversion column name in campaign_performance_daily")

    if "conversions" in cpnd_cols and "conversion_value" in cpnd_cols:
        conv_col_net = "conversions"
        rev_col_net = "conversion_value"
    elif "primary_conv_count" in cpnd_cols:
        conv_col_net = "primary_conv_count"
        rev_col_net = "primary_conv_value"
    else:
        conv_col_net = cpnd_cols[0]
        rev_col_net = cpnd_cols[0]
        print(f"  WARNING: couldn't detect conversion column name in campaign_performance_network_daily")

    print(f"  Using columns: search={conv_col_search}/{rev_col_search}, "
          f"network={conv_col_net}/{rev_col_net}")
    print()

    def fetch_agg(table, conv_col, rev_col, device_filter=None, network_filter=None):
        sql = f"""
        SELECT
            SUM(cost_micros) / 1.0e6 AS spend,
            SUM({conv_col})           AS conv,
            SUM({rev_col})            AS rev,
            COUNT(*)                  AS row_count
        FROM {table}
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
        """
        params = [campaign_id, START_DATE, END_DATE]
        if device_filter == "ALL":
            sql += " AND device = 'ALL'"
        elif device_filter == "PER_DEVICE":
            sql += " AND device IN ('DESKTOP','MOBILE','TABLET')"
        if network_filter:
            sql += f" AND network = ?"
            params.append(network_filter)
        return conn.execute(sql, params).fetchone()

    # SEARCH bucket (campaign_performance_daily)
    print("─" * 78)
    print("SEARCH NETWORK (Microsoft sites + AOL search, in campaign_performance_daily)")
    print("─" * 78)
    print(f"  UI (combined search networks):")
    print(f"    spend       = ${UI_VALUES['search']['spend']:>10,.2f}")
    print(f"    Conv.       = {UI_VALUES['search']['conv_pixel']:>10}")
    print(f"    All conv.   = {UI_VALUES['search']['all_conv']:>10}")
    print(f"    All conv.$ = ${UI_VALUES['search']['all_revenue']:>10,.2f}")
    print()
    db_all = fetch_agg("campaign_performance_daily", conv_col_search, rev_col_search, "ALL")
    db_per = fetch_agg("campaign_performance_daily", conv_col_search, rev_col_search, "PER_DEVICE")
    print(f"  DB device='ALL' rollup ({db_all['row_count']} rows):")
    print(f"    spend       = ${(db_all['spend'] or 0):>10,.2f}  delta vs UI: ${(db_all['spend'] or 0) - UI_VALUES['search']['spend']:>+8,.2f}")
    print(f"    {conv_col_search:11} = {(db_all['conv'] or 0):>10}")
    print(f"    {rev_col_search:11} = ${(db_all['rev'] or 0):>10,.2f}")
    print()
    print(f"  DB per-device sum ({db_per['row_count']} rows):")
    print(f"    spend       = ${(db_per['spend'] or 0):>10,.2f}")
    print(f"    {conv_col_search:11} = {(db_per['conv'] or 0):>10}")
    print(f"    {rev_col_search:11} = ${(db_per['rev'] or 0):>10,.2f}")
    print()

    # AUDIENCE bucket (campaign_performance_network_daily)
    print("─" * 78)
    print("AUDIENCE NETWORK (in campaign_performance_network_daily)")
    print("─" * 78)
    print(f"  UI:")
    print(f"    spend       = ${UI_VALUES['audience']['spend']:>10,.2f}")
    print(f"    Conv.       = {UI_VALUES['audience']['conv_pixel']:>10}")
    print(f"    All conv.   = {UI_VALUES['audience']['all_conv']:>10}")
    print(f"    All conv.$ = ${UI_VALUES['audience']['all_revenue']:>10,.2f}")
    print()
    db_all_n = fetch_agg("campaign_performance_network_daily", conv_col_net, rev_col_net, "ALL", "Audience")
    db_per_n = fetch_agg("campaign_performance_network_daily", conv_col_net, rev_col_net, "PER_DEVICE", "Audience")
    print(f"  DB device='ALL' rollup, network='Audience' ({db_all_n['row_count']} rows):")
    print(f"    spend       = ${(db_all_n['spend'] or 0):>10,.2f}  delta vs UI: ${(db_all_n['spend'] or 0) - UI_VALUES['audience']['spend']:>+8,.2f}")
    print(f"    {conv_col_net:11} = {(db_all_n['conv'] or 0):>10}")
    print(f"    {rev_col_net:11} = ${(db_all_n['rev'] or 0):>10,.2f}")
    print()
    print(f"  DB per-device sum, network='Audience' ({db_per_n['row_count']} rows):")
    print(f"    spend       = ${(db_per_n['spend'] or 0):>10,.2f}")
    print(f"    {conv_col_net:11} = {(db_per_n['conv'] or 0):>10}")
    print(f"    {rev_col_net:11} = ${(db_per_n['rev'] or 0):>10,.2f}")
    print()

    # ── Step 4: Diagnosis ──
    banner(f"STEP 4: DIAGNOSIS")

    ui_total_all_conv = UI_VALUES['search']['all_conv'] + UI_VALUES['audience']['all_conv']
    ui_total_spend = UI_VALUES['search']['spend'] + UI_VALUES['audience']['spend']
    ui_total_rev = UI_VALUES['search']['all_revenue'] + UI_VALUES['audience']['all_revenue']

    db_all_total_conv = (db_all['conv'] or 0) + (db_all_n['conv'] or 0)
    db_all_total_spend = (db_all['spend'] or 0) + (db_all_n['spend'] or 0)
    db_all_total_rev = (db_all['rev'] or 0) + (db_all_n['rev'] or 0)

    print(f"  Combined (search + audience) totals:")
    print()
    print(f"  {'Metric':<25} {'UI':>12} {'DB device=ALL':>15} {'Delta':>12} {'% Match':>10}")
    print(f"  {'-'*25} {'-'*12} {'-'*15} {'-'*12} {'-'*10}")
    print(f"  {'Spend':<25} ${ui_total_spend:>10,.2f}  ${db_all_total_spend:>12,.2f}  ${db_all_total_spend - ui_total_spend:>+10,.2f}  {(db_all_total_spend / ui_total_spend * 100) if ui_total_spend else 0:>9.1f}%")
    print(f"  {'All conv. count':<25} {ui_total_all_conv:>12}  {db_all_total_conv:>15}  {db_all_total_conv - ui_total_all_conv:>+12}  {(db_all_total_conv / ui_total_all_conv * 100) if ui_total_all_conv else 0:>9.1f}%")
    print(f"  {'All conv. revenue':<25} ${ui_total_rev:>10,.2f}  ${db_all_total_rev:>12,.2f}  ${db_all_total_rev - ui_total_rev:>+10,.2f}  {(db_all_total_rev / ui_total_rev * 100) if ui_total_rev else 0:>9.1f}%")
    print()

    # Verdict
    spend_match = abs(db_all_total_spend - ui_total_spend) < 1.00
    conv_match = abs(db_all_total_conv - ui_total_all_conv) <= 1
    rev_match = abs(db_all_total_rev - ui_total_rev) < 1.00

    print(f"  Verdict:")
    print(f"    Spend matches UI within $1   : {'YES' if spend_match else 'NO'}")
    print(f"    All conv. matches UI within 1: {'YES' if conv_match else 'NO'}")
    print(f"    All revenue matches UI within $1: {'YES' if rev_match else 'NO'}")
    print()

    if spend_match and conv_match and rev_match:
        print("  → DB device='ALL' correctly mirrors UI 'All conv.' values.")
        print("  → No loader bug. The visualization script just needs to use device='ALL'")
        print("    on the campaign tables AND use the correct conversion column.")
        print()
        print("  → IMPORTANT: The DB stores 'All conversions' (offline-inclusive), NOT")
        print("    'Conv.' (pixel-only). Your tCPA bid strategy optimizes toward 'Conv.',")
        print("    not 'All conv.', so any 'CPA vs tCPA' analysis using the DB's conv column")
        print("    is comparing to the wrong denominator. Microsoft pixel 'Conv.' for Texas")
        print(f"    over this window = {UI_VALUES['search']['conv_pixel'] + UI_VALUES['audience']['conv_pixel']}, but DB shows {db_all_total_conv}.")
    elif conv_match is False and db_all_total_conv > ui_total_all_conv * 1.5:
        print("  → DB conv count is materially HIGHER than UI 'All conv.'")
        print("  → Possible cross-device de-duplication: per-device rows in CSV are being")
        print("    summed into ALL but UI's All conv. de-duplicates across devices.")
        print("  → Action: investigate Microsoft Ads CSV export — may need to export")
        print("    twice (once with device segment, once without) and load the un-segmented")
        print("    row as the 'ALL' rollup instead of synthesizing it from device sums.")
    else:
        print("  → Mixed results. See per-metric verdict above.")
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
