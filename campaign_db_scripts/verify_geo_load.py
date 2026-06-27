"""
verify_geo_load.py — Post-load verification for geo_performance_daily.

Runs four checks against a loaded geo window and prints results. Exits 0 if all
checks pass, non-zero if any check fails. Tolerances:
    - Integer columns (impressions, clicks, cost_micros): ABS(diff) > 1 fails.
    - Float columns (conversions, conversion_value, all_conversions, all_conv_value):
      ABS(diff) > 0.01 fails.

Conversion-column diffs on dates that pre-date the campaign-table pixel-as-primary
backfill (May 2026) are flagged but NOT failed — those are expected legacy diffs.

Usage:
    python verify_geo_load.py --account home --start-date 2026-05-06 --end-date 2026-05-12
    python verify_geo_load.py --account auto --start-date 2026-04-01 --end-date 2026-06-30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Make refresh_helper importable regardless of where this script is invoked from.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from refresh_helper import ACCOUNT_REGISTRY, get_connection, resolve_account_id


# Tolerances
INT_TOLERANCE   = 1
FLOAT_TOLERANCE = 0.01

# Date before which campaign-table conversion columns may not reflect the
# pixel-as-primary convention (legacy diffs expected, not a failure).
PIXEL_CONVENTION_CUTOFF = "2026-05-01"

SEARCH_NETWORKS = ("Microsoft sites and select traffic", "AOL search")
NON_SEARCH_NETWORKS = ("Audience", "Syndicated search partners")


def _print_header(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def _summarize_diffs(diffs: pd.DataFrame, label: str, is_conv_only: bool = False) -> bool:
    """Print diff summary; return True if there are blocking failures."""
    if len(diffs) == 0:
        print(f"  PASS: {label} reconciled cleanly (0 diff rows).")
        return False

    print(f"  {len(diffs)} diff rows in {label}.")
    print(f"  Sample (first 10):")
    print(diffs.head(10).to_string(index=False))
    print()

    if is_conv_only:
        # Conversion diffs on pre-cutoff dates are legacy, not a failure.
        legacy = diffs[diffs["date"] < PIXEL_CONVENTION_CUTOFF]
        live   = diffs[diffs["date"] >= PIXEL_CONVENTION_CUTOFF]
        if len(legacy) > 0:
            print(f"  INFO: {len(legacy)} diff rows on pre-{PIXEL_CONVENTION_CUTOFF} dates "
                  f"(legacy pixel-convention drift, not a failure).")
        if len(live) > 0:
            print(f"  FAIL: {len(live)} diff rows on post-{PIXEL_CONVENTION_CUTOFF} dates.")
            return True
        return False

    return True  # Non-conversion diffs are always failures.


def check_1_load_summary(conn, account_id: int, start_date: str, end_date: str) -> bool:
    """Sanity-check: row counts and totals by device and location_type for the loaded window."""
    _print_header("Check 1: Load summary by device x location_type")
    df = pd.read_sql_query(
        """
        SELECT device, location_type,
               COUNT(*)                                 AS n_rows,
               SUM(impressions)                         AS impressions,
               SUM(clicks)                              AS clicks,
               ROUND(SUM(cost_micros)/1000000.0, 2)     AS spend_dollars,
               ROUND(SUM(conversions), 2)               AS conversions,
               ROUND(SUM(conversion_value), 2)          AS conversion_value,
               ROUND(SUM(all_conversions), 2)           AS all_conversions
        FROM geo_performance_daily
        WHERE account_id = ?
          AND date BETWEEN ? AND ?
        GROUP BY device, location_type
        ORDER BY location_type, device
        """,
        conn,
        params=(account_id, start_date, end_date),
    )
    if df.empty:
        print(f"  FAIL: No rows found for account_id={account_id} "
              f"between {start_date} and {end_date}.")
        return True
    print(df.to_string(index=False))

    # For each location_type, device='ALL' totals should equal sum of per-device totals.
    fail = False
    for loc_type in df["location_type"].unique():
        sub = df[df["location_type"] == loc_type]
        all_row = sub[sub["device"] == "ALL"]
        per_device = sub[sub["device"].isin(["DESKTOP", "MOBILE", "TABLET"])]
        if all_row.empty or per_device.empty:
            print(f"  FAIL: location_type={loc_type!r} — missing device='ALL' or per-device rows.")
            fail = True
            continue

        all_impr   = int(all_row["impressions"].iloc[0])
        per_impr   = int(per_device["impressions"].sum())
        all_clicks = int(all_row["clicks"].iloc[0])
        per_clicks = int(per_device["clicks"].sum())
        all_spend  = float(all_row["spend_dollars"].iloc[0])
        per_spend  = float(per_device["spend_dollars"].sum())

        print()
        print(f"  location_type={loc_type!r} — device='ALL' vs per-device sum:")
        print(f"    impressions: ALL={all_impr:,}  sum(per-device)={per_impr:,}  diff={all_impr - per_impr}")
        print(f"    clicks:      ALL={all_clicks:,}  sum(per-device)={per_clicks:,}  diff={all_clicks - per_clicks}")
        print(f"    spend:       ALL=${all_spend:,.2f}  sum(per-device)=${per_spend:,.2f}  diff=${all_spend - per_spend:.2f}")

        if abs(all_impr - per_impr) > INT_TOLERANCE \
           or abs(all_clicks - per_clicks) > INT_TOLERANCE \
           or abs(all_spend - per_spend) > FLOAT_TOLERANCE:
            print(f"  FAIL: device='ALL' totals don't match per-device sum for location_type={loc_type!r}.")
            fail = True

    if not fail:
        print()
        print("  PASS: device='ALL' totals match per-device sums for every location_type.")
    return fail


def check_2_internal_all_consistency(conn, account_id: int, start_date: str, end_date: str) -> bool:
    """For each (campaign, date, network, geo_state, location_type), device='ALL'
    row must equal the sum of its per-device rows. Catches groupby bugs."""
    _print_header("Check 2: Internal device='ALL' row consistency (per tuple)")
    df = pd.read_sql_query(
        """
        WITH all_rows AS (
            SELECT campaign_id, date, network, geo_state, location_type,
                   impressions, clicks, cost_micros, conversions,
                   conversion_value, all_conversions, all_conversion_value
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND date BETWEEN ? AND ?
        ),
        per_device_sums AS (
            SELECT campaign_id, date, network, geo_state, location_type,
                   SUM(impressions)          AS pd_impressions,
                   SUM(clicks)               AS pd_clicks,
                   SUM(cost_micros)          AS pd_cost_micros,
                   SUM(conversions)          AS pd_conversions,
                   SUM(conversion_value)     AS pd_conversion_value,
                   SUM(all_conversions)      AS pd_all_conversions,
                   SUM(all_conversion_value) AS pd_all_conversion_value
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device IN ('DESKTOP', 'MOBILE', 'TABLET')
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id, date, network, geo_state, location_type
        )
        SELECT a.campaign_id, a.date, a.network, a.geo_state, a.location_type,
               a.impressions          AS all_impressions,
               p.pd_impressions       AS pd_impressions,
               (a.impressions - p.pd_impressions) AS impressions_diff,
               (a.cost_micros  - p.pd_cost_micros) AS cost_micros_diff,
               (a.conversions  - p.pd_conversions) AS conversions_diff
        FROM all_rows a
        JOIN per_device_sums p
            ON a.campaign_id   = p.campaign_id
           AND a.date          = p.date
           AND a.network       = p.network
           AND a.geo_state     = p.geo_state
           AND a.location_type = p.location_type
        WHERE ABS(a.impressions          - p.pd_impressions)          > ?
           OR ABS(a.clicks               - p.pd_clicks)               > ?
           OR ABS(a.cost_micros          - p.pd_cost_micros)          > ?
           OR ABS(a.conversions          - p.pd_conversions)          > ?
           OR ABS(a.conversion_value     - p.pd_conversion_value)     > ?
           OR ABS(a.all_conversions      - p.pd_all_conversions)      > ?
           OR ABS(a.all_conversion_value - p.pd_all_conversion_value) > ?
        """,
        conn,
        params=(
            account_id, start_date, end_date,
            account_id, start_date, end_date,
            INT_TOLERANCE, INT_TOLERANCE, INT_TOLERANCE,
            FLOAT_TOLERANCE, FLOAT_TOLERANCE, FLOAT_TOLERANCE, FLOAT_TOLERANCE,
        ),
    )
    return _summarize_diffs(df, "internal device='ALL' consistency")


def check_3_search_reconciliation(conn, account_id: int, start_date: str, end_date: str) -> bool:
    """Geo SUM across states AND location_types (device='ALL', search networks)
    must reconcile against campaign_performance_daily (device='ALL') for the same
    window. campaign_performance_daily is not segmented by location_type, so the
    geo SUM here implicitly aggregates PHYSICAL + INTEREST rows together."""
    _print_header("Check 3: Reconciliation vs campaign_performance_daily (search networks)")

    network_placeholders = ",".join("?" * len(SEARCH_NETWORKS))
    df = pd.read_sql_query(
        f"""
        WITH geo_rollup AS (
            SELECT campaign_id, date,
                   SUM(impressions)        AS geo_impressions,
                   SUM(clicks)             AS geo_clicks,
                   SUM(cost_micros)        AS geo_cost_micros,
                   SUM(conversions)        AS geo_conversions,
                   SUM(conversion_value)   AS geo_conversion_value,
                   SUM(all_conversions)    AS geo_all_conversions
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id, date
        ),
        camp AS (
            SELECT campaign_id, date,
                   impressions          AS camp_impressions,
                   clicks               AS camp_clicks,
                   cost_micros          AS camp_cost_micros,
                   conversions          AS camp_conversions,
                   conversion_value     AS camp_conversion_value,
                   all_conversions      AS camp_all_conversions
            FROM campaign_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND date BETWEEN ? AND ?
        )
        SELECT COALESCE(g.campaign_id, c.campaign_id) AS campaign_id,
               COALESCE(g.date, c.date)               AS date,
               g.geo_impressions, c.camp_impressions,
               (COALESCE(g.geo_impressions,0) - COALESCE(c.camp_impressions,0)) AS impressions_diff,
               (COALESCE(g.geo_cost_micros,0) - COALESCE(c.camp_cost_micros,0)) AS cost_micros_diff,
               (COALESCE(g.geo_conversions,0) - COALESCE(c.camp_conversions,0)) AS conversions_diff
        FROM geo_rollup g
        FULL OUTER JOIN camp c
            ON g.campaign_id = c.campaign_id AND g.date = c.date
        WHERE ABS(COALESCE(g.geo_impressions,0)        - COALESCE(c.camp_impressions,0))        > ?
           OR ABS(COALESCE(g.geo_clicks,0)             - COALESCE(c.camp_clicks,0))             > ?
           OR ABS(COALESCE(g.geo_cost_micros,0)        - COALESCE(c.camp_cost_micros,0))        > ?
           OR ABS(COALESCE(g.geo_conversion_value,0)   - COALESCE(c.camp_conversion_value,0))   > ?
        """,
        conn,
        params=(
            account_id, *SEARCH_NETWORKS, start_date, end_date,
            account_id, start_date, end_date,
            INT_TOLERANCE, INT_TOLERANCE, INT_TOLERANCE, FLOAT_TOLERANCE,
        ),
    )
    structural_fail = _summarize_diffs(df, "search-network reconciliation")

    # Separately check conversion-column drift with legacy-date forgiveness.
    df_conv = pd.read_sql_query(
        f"""
        WITH geo_rollup AS (
            SELECT campaign_id, date,
                   SUM(conversions) AS geo_conversions
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id, date
        ),
        camp AS (
            SELECT campaign_id, date, conversions AS camp_conversions
            FROM campaign_performance_daily
            WHERE account_id = ? AND device = 'ALL'
              AND date BETWEEN ? AND ?
        )
        SELECT COALESCE(g.campaign_id, c.campaign_id) AS campaign_id,
               COALESCE(g.date, c.date)               AS date,
               g.geo_conversions, c.camp_conversions,
               (COALESCE(g.geo_conversions,0) - COALESCE(c.camp_conversions,0)) AS diff
        FROM geo_rollup g
        FULL OUTER JOIN camp c
            ON g.campaign_id = c.campaign_id AND g.date = c.date
        WHERE ABS(COALESCE(g.geo_conversions,0) - COALESCE(c.camp_conversions,0)) > ?
        """,
        conn,
        params=(
            account_id, *SEARCH_NETWORKS, start_date, end_date,
            account_id, start_date, end_date,
            FLOAT_TOLERANCE,
        ),
    )
    print()
    print("  Conversion-column reconciliation (separate; pre-cutoff dates forgiven):")
    conv_fail = _summarize_diffs(df_conv, "search-network conversion reconciliation",
                                 is_conv_only=True)

    return structural_fail or conv_fail


def check_4_non_search_reconciliation(conn, account_id: int, start_date: str, end_date: str) -> bool:
    """Geo SUM across states AND location_types (device='ALL') must reconcile
    against campaign_performance_network_daily, per network (Audience + Syndicated).
    The campaign_performance_network_daily table is not segmented by location_type,
    so the geo SUM here implicitly aggregates PHYSICAL + INTEREST rows together."""
    _print_header("Check 4: Reconciliation vs campaign_performance_network_daily (non-search networks)")

    network_placeholders = ",".join("?" * len(NON_SEARCH_NETWORKS))
    df = pd.read_sql_query(
        f"""
        WITH geo_rollup AS (
            SELECT campaign_id, date, network,
                   SUM(impressions)        AS geo_impressions,
                   SUM(clicks)             AS geo_clicks,
                   SUM(cost_micros)        AS geo_cost_micros,
                   SUM(conversions)        AS geo_conversions,
                   SUM(conversion_value)   AS geo_conversion_value
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id, date, network
        ),
        camp AS (
            SELECT campaign_id, date, network,
                   impressions          AS camp_impressions,
                   clicks               AS camp_clicks,
                   cost_micros          AS camp_cost_micros,
                   conversions          AS camp_conversions,
                   conversion_value     AS camp_conversion_value
            FROM campaign_performance_network_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
        )
        SELECT COALESCE(g.campaign_id, c.campaign_id) AS campaign_id,
               COALESCE(g.date, c.date)               AS date,
               COALESCE(g.network, c.network)         AS network,
               g.geo_impressions, c.camp_impressions,
               (COALESCE(g.geo_impressions,0) - COALESCE(c.camp_impressions,0)) AS impressions_diff,
               (COALESCE(g.geo_cost_micros,0) - COALESCE(c.camp_cost_micros,0)) AS cost_micros_diff
        FROM geo_rollup g
        FULL OUTER JOIN camp c
            ON g.campaign_id = c.campaign_id AND g.date = c.date AND g.network = c.network
        WHERE ABS(COALESCE(g.geo_impressions,0)      - COALESCE(c.camp_impressions,0))      > ?
           OR ABS(COALESCE(g.geo_clicks,0)           - COALESCE(c.camp_clicks,0))           > ?
           OR ABS(COALESCE(g.geo_cost_micros,0)      - COALESCE(c.camp_cost_micros,0))      > ?
           OR ABS(COALESCE(g.geo_conversion_value,0) - COALESCE(c.camp_conversion_value,0)) > ?
        """,
        conn,
        params=(
            account_id, *NON_SEARCH_NETWORKS, start_date, end_date,
            account_id, *NON_SEARCH_NETWORKS, start_date, end_date,
            INT_TOLERANCE, INT_TOLERANCE, INT_TOLERANCE, FLOAT_TOLERANCE,
        ),
    )
    structural_fail = _summarize_diffs(df, "non-search-network reconciliation")

    # Separately check conversion-column drift.
    df_conv = pd.read_sql_query(
        f"""
        WITH geo_rollup AS (
            SELECT campaign_id, date, network,
                   SUM(conversions) AS geo_conversions
            FROM geo_performance_daily
            WHERE account_id = ?
              AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id, date, network
        ),
        camp AS (
            SELECT campaign_id, date, network, conversions AS camp_conversions
            FROM campaign_performance_network_daily
            WHERE account_id = ? AND device = 'ALL'
              AND network IN ({network_placeholders})
              AND date BETWEEN ? AND ?
        )
        SELECT COALESCE(g.campaign_id, c.campaign_id) AS campaign_id,
               COALESCE(g.date, c.date)               AS date,
               COALESCE(g.network, c.network)         AS network,
               g.geo_conversions, c.camp_conversions,
               (COALESCE(g.geo_conversions,0) - COALESCE(c.camp_conversions,0)) AS diff
        FROM geo_rollup g
        FULL OUTER JOIN camp c
            ON g.campaign_id = c.campaign_id AND g.date = c.date AND g.network = c.network
        WHERE ABS(COALESCE(g.geo_conversions,0) - COALESCE(c.camp_conversions,0)) > ?
        """,
        conn,
        params=(
            account_id, *NON_SEARCH_NETWORKS, start_date, end_date,
            account_id, *NON_SEARCH_NETWORKS, start_date, end_date,
            FLOAT_TOLERANCE,
        ),
    )
    print()
    print("  Conversion-column reconciliation (separate; pre-cutoff dates forgiven):")
    conv_fail = _summarize_diffs(df_conv, "non-search-network conversion reconciliation",
                                 is_conv_only=True)

    return structural_fail or conv_fail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=list(ACCOUNT_REGISTRY.keys()))
    parser.add_argument("--start-date", required=True, help="ISO YYYY-MM-DD")
    parser.add_argument("--end-date",   required=True, help="ISO YYYY-MM-DD")
    args = parser.parse_args()

    print(f"Verifying geo load for account={args.account} "
          f"window=[{args.start_date} .. {args.end_date}]")

    failures = []
    with get_connection() as conn:
        account_id = resolve_account_id(conn, args.account)

        if check_1_load_summary(conn, account_id, args.start_date, args.end_date):
            failures.append("Check 1: load summary")
        if check_2_internal_all_consistency(conn, account_id, args.start_date, args.end_date):
            failures.append("Check 2: internal ALL consistency")
        if check_3_search_reconciliation(conn, account_id, args.start_date, args.end_date):
            failures.append("Check 3: search-network reconciliation")
        if check_4_non_search_reconciliation(conn, account_id, args.start_date, args.end_date):
            failures.append("Check 4: non-search-network reconciliation")

    _print_header("VERIFICATION SUMMARY")
    if not failures:
        print("  ALL CHECKS PASSED.")
        return 0
    print(f"  {len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"    - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
