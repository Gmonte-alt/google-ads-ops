"""
Diagnostic: introspect the actual grain of campaign_performance_daily and
campaign_performance_network_daily for Microsoft Home Insurance (account 2).

The schema-expert thread assumed both tables store per-device rows PLUS a
device='ALL' rollup row. The refresh_check reconciliation suggests that
may not be true for this DB — refresh_check sums without a device filter
and matches the keyword-level total to within 0.002%, which only works if
there is NOT a duplicate 'ALL' rollup row.

This script prints, for one campaign over one recent week, exactly what
device and network values exist and what they sum to. Run it before
trusting any campaign-level aggregation.

Usage:
    python diagnose_microsoft_campaign_grain.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 2
# Pick a campaign that appears in the broken chart, and the most recent
# complete week (May 4-10 per refresh_check freshness).
CAMPAIGN_NAME = "HOME INSURANCE-TEXAS-SEARCH-MOBILE-USA-STE"
START = "2026-05-04"
END = "2026-05-10"


def banner(t):
    print("=" * 72)
    print(t)
    print("=" * 72)


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    banner(f"Diagnostic for {CAMPAIGN_NAME} | {START} to {END}")

    # ── 1. Resolve campaign_id, confirm it exists for this account ──
    row = cur.execute(
        "SELECT campaign_id, status FROM campaigns "
        "WHERE account_id = ? AND campaign_name = ?",
        (ACCOUNT_ID, CAMPAIGN_NAME),
    ).fetchone()
    if not row:
        print(f"!! Campaign not found for account_id={ACCOUNT_ID}. "
              "Edit CAMPAIGN_NAME to one that exists.")
        # Show available campaigns to help
        print("\nFirst 20 enabled campaigns for account 2:")
        for r in cur.execute(
            "SELECT campaign_name FROM campaigns "
            "WHERE account_id = ? AND status = 'ENABLED' "
            "ORDER BY campaign_name LIMIT 20",
            (ACCOUNT_ID,),
        ):
            print(f"  - {r[0]}")
        return
    campaign_id, status = row
    print(f"campaign_id = {campaign_id}  status = {status}")
    print()

    # ── 2. campaign_performance_daily: what device values exist? ──
    print("── campaign_performance_daily (search) ──────────────────")
    rows = cur.execute(
        """
        SELECT device,
               COUNT(*)                AS n_rows,
               SUM(cost_micros)/1e6    AS spend,
               SUM(conversions)        AS conv,
               SUM(conversion_value)   AS conv_value,
               SUM(all_conversions)    AS all_conv,
               SUM(all_conversion_value) AS all_conv_value
        FROM campaign_performance_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
        GROUP BY device
        ORDER BY device
        """,
        (campaign_id, START, END),
    ).fetchall()
    print(f"  {'device':<10} {'rows':>6} {'spend':>12} {'conv':>10} "
          f"{'conv_val':>12} {'all_conv':>10} {'all_conv_val':>14}")
    total_spend = 0.0
    total_conv = 0.0
    total_conv_val = 0.0
    total_all_conv = 0.0
    total_all_conv_val = 0.0
    for device, n, spend, conv, conv_value, all_conv, all_conv_value in rows:
        print(f"  {device:<10} {n:>6} {spend:>12,.2f} {conv:>10.2f} "
              f"{conv_value:>12,.2f} {all_conv:>10.2f} {all_conv_value:>14,.2f}")
        total_spend += spend or 0
        total_conv += conv or 0
        total_conv_val += conv_value or 0
        total_all_conv += all_conv or 0
        total_all_conv_val += all_conv_value or 0
    print(f"  {'SUM-ALL':<10} {'':>6} {total_spend:>12,.2f} {total_conv:>10.2f} "
          f"{total_conv_val:>12,.2f} {total_all_conv:>10.2f} {total_all_conv_val:>14,.2f}")
    print()

    # ── 3. campaign_performance_network_daily: device × network ──
    print("── campaign_performance_network_daily (audience+other) ──")
    rows = cur.execute(
        """
        SELECT network, device,
               COUNT(*)                AS n_rows,
               SUM(cost_micros)/1e6    AS spend,
               SUM(conversions)        AS conv,
               SUM(conversion_value)   AS conv_value,
               SUM(all_conversions)    AS all_conv,
               SUM(all_conversion_value) AS all_conv_value
        FROM campaign_performance_network_daily
        WHERE campaign_id = ?
          AND date BETWEEN ? AND ?
        GROUP BY network, device
        ORDER BY network, device
        """,
        (campaign_id, START, END),
    ).fetchall()
    print(f"  {'network':<28} {'device':<10} {'rows':>6} {'spend':>12} "
          f"{'conv':>10} {'conv_val':>12} {'all_conv':>10} {'all_conv_val':>14}")
    a_spend = a_conv = a_conv_val = a_all_conv = a_all_conv_val = 0.0
    for network, device, n, spend, conv, conv_value, all_conv, all_conv_value in rows:
        print(f"  {(network or 'NULL'):<28} {device:<10} {n:>6} "
              f"{spend:>12,.2f} {conv:>10.2f} {conv_value:>12,.2f} "
              f"{all_conv:>10.2f} {all_conv_value:>14,.2f}")
        if (network or "").lower() == "audience":
            a_spend += spend or 0
            a_conv += conv or 0
            a_conv_val += conv_value or 0
            a_all_conv += all_conv or 0
            a_all_conv_val += all_conv_value or 0
    print(f"  {'AUDIENCE-ONLY-SUM':<28} {'':<10} {'':>6} {a_spend:>12,.2f} "
          f"{a_conv:>10.2f} {a_conv_val:>12,.2f} {a_all_conv:>10.2f} "
          f"{a_all_conv_val:>14,.2f}")
    print()

    # ── 4. Reconcile: campaign (search + audience) vs keyword for this campaign ──
    print("── Reconciliation: campaign sum vs keyword sum ──────────")
    kw = cur.execute(
        """
        SELECT
            SUM(kpd.cost_micros)/1e6    AS spend,
            SUM(kpd.primary_conv_count) AS conv,
            SUM(kpd.primary_conv_value) AS conv_value,
            SUM(kpd.all_conv_count)     AS all_conv,
            SUM(kpd.all_conv_value)     AS all_conv_value
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.campaign_id = ?
          AND kpd.report_date BETWEEN ? AND ?
        """,
        (campaign_id, START, END),
    ).fetchone()
    kw_spend, kw_conv, kw_conv_val, kw_all_conv, kw_all_conv_val = kw
    print(f"  keyword grain (sum over all device/geo rows):")
    print(f"    spend          = {kw_spend:>12,.2f}")
    print(f"    primary_conv   = {kw_conv:>12.2f}")
    print(f"    primary_conv_v = {kw_conv_val:>12,.2f}")
    print(f"    all_conv       = {kw_all_conv:>12.2f}")
    print(f"    all_conv_value = {kw_all_conv_val:>12,.2f}")
    print()
    print(f"  campaign-table search + audience (per the SUMs above):")
    print(f"    spend          = {total_spend + a_spend:>12,.2f}  "
          f"(search {total_spend:,.2f} + audience {a_spend:,.2f})")
    print(f"    conversions    = {total_conv + a_conv:>12.2f}")
    print(f"    conv_value     = {total_conv_val + a_conv_val:>12,.2f}")
    print(f"    all_conv       = {total_all_conv + a_all_conv:>12.2f}")
    print(f"    all_conv_value = {total_all_conv_val + a_all_conv_val:>12,.2f}")
    print()

    # ── 5. WHAT THE BROKEN CHART SHOWS — what spend, conv would produce
    #     CPA=$17 and RPL=$39 and 63 conversions for May 4-10? ──
    print("── What the chart claims for week of May 4 ──────────────")
    print("  conv=63  CPA=$17  =>  spend ≈ $1,071")
    print("  conv=63  RPL=$39  =>  conv_value ≈ $2,457")
    print("  profit  ≈ $1,386 (chart shows $1,396)")
    print()
    print("  Compare against the actual sums above to identify the bug.")
    print()

    conn.close()


if __name__ == "__main__":
    main()
