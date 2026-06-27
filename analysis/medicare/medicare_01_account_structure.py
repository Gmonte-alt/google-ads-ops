"""
medicare_01_account_structure.py
==================================
Medicare Account Structure & Hygiene Analysis
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Produces sheets:
  01_Campaign_Inventory   — All Medicare campaigns with budget, bid strategy, status
  01_Budget_Allocation    — Budget by campaign, % of total, IS metrics
  01_Ad_Group_Inventory   — Ad groups per campaign, bid overrides flagged
  01_Consolidation        — Campaigns/ad groups with low conversion signal (< 30 conv / 90 days)
  01_Naming_Flags         — Campaigns deviating from naming convention

Usage:
  python medicare_01_account_structure.py --period last_90
  python medicare_01_account_structure.py --period aep2025
  python medicare_01_account_structure.py --period custom --start 2025-10-01 --end 2025-12-31

Returns:
  dict of {sheet_name: DataFrame} — consumed by medicare_00_build_workbook.py
  Also writes standalone .xlsx if run directly.
"""

import argparse
import sqlite3
import pandas as pd
from pathlib import Path
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

import sys
sys.path.insert(0, str(Path(__file__).parent))
from medicare_utils import (
    get_db_connection, resolve_period, PERIOD_LABELS, VALID_PERIODS,
    MEDICARE_ACCOUNT_ID, micros_to_dollars, safe_div,
    write_title_row, write_dataframe_to_sheet, autofit_columns,
    freeze_header_rows, XL
)

OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\analysis\output")


# ── Queries ───────────────────────────────────────────────────────────────────

def q_campaign_inventory(conn, start: str, end: str) -> pd.DataFrame:
    """All Medicare campaigns with structural metadata and performance summary."""
    rows = conn.execute("""
        SELECT
            c.campaign_id,
            c.campaign_name,
            c.status,
            c.campaign_type,
            c.bidding_strategy_type,
            CASE WHEN c.bidding_strategy_id IS NOT NULL
                 THEN 'Portfolio' ELSE 'Campaign-level' END          AS strategy_level,
            ROUND(c.daily_budget_micros / 1e6, 2)                   AS daily_budget,
            c.is_shared_budget,
            ROUND(c.target_cpa_micros / 1e6, 2)                     AS target_cpa,
            c.target_roas,
            c.first_seen_date,
            c.last_synced_at,
            -- Performance summary for the period
            COALESCE(SUM(p.impressions), 0)                          AS impressions,
            COALESCE(SUM(p.clicks), 0)                               AS clicks,
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2)         AS spend,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)                AS conversions,
            ROUND(COALESCE(SUM(p.conversion_value), 0), 2)           AS conv_value,
            COUNT(DISTINCT p.date)                                    AS active_days,
            -- Ad group count
            (SELECT COUNT(*) FROM ad_groups ag
             WHERE ag.campaign_id = c.campaign_id
               AND ag.status != 'REMOVED')                           AS active_ad_groups
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id
        ORDER BY spend DESC, c.campaign_name
    """, (start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"]  = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["ctr"]  = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["cvr"]  = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["roas"] = df.apply(lambda r: safe_div(r.conv_value, r.spend), axis=1)

    col_order = [
        "campaign_id", "campaign_name", "status", "campaign_type",
        "bidding_strategy_type", "strategy_level", "daily_budget",
        "is_shared_budget", "target_cpa", "target_roas",
        "active_ad_groups", "active_days",
        "impressions", "clicks", "spend", "conversions", "conv_value",
        "ctr", "cvr", "cpa", "roas", "first_seen_date",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_budget_allocation(conn, start: str, end: str) -> pd.DataFrame:
    """Budget allocation and impression share by campaign."""
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            c.bidding_strategy_type,
            ROUND(c.daily_budget_micros / 1e6, 2)                       AS daily_budget,
            c.is_shared_budget,
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2)             AS spend,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)                    AS conversions,
            ROUND(AVG(p.search_impression_share), 3)                     AS avg_is,
            ROUND(AVG(p.search_budget_lost_is), 3)                       AS avg_budget_lost_is,
            ROUND(AVG(p.search_rank_lost_is), 3)                         AS avg_rank_lost_is,
            COUNT(DISTINCT CASE WHEN p.cost_micros > 0 THEN p.date END)  AS days_with_spend,
            -- Days where spend >= 90% of daily budget (budget-capped proxy)
            COUNT(DISTINCT CASE
                WHEN p.budget_micros_that_day > 0
                 AND CAST(p.cost_micros AS REAL) / p.budget_micros_that_day >= 0.90
                THEN p.date END)                                          AS days_near_cap
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id
        ORDER BY spend DESC
    """, (start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    total_spend = df["spend"].sum()
    df["pct_of_total_spend"] = df["spend"].apply(
        lambda x: safe_div(x, total_spend, 0)
    )
    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["budget_cap_pct"] = df.apply(
        lambda r: safe_div(r.days_near_cap, r.days_with_spend, 0), axis=1
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type",
        "daily_budget", "is_shared_budget",
        "spend", "pct_of_total_spend", "conversions", "cpa",
        "avg_is", "avg_budget_lost_is", "avg_rank_lost_is",
        "days_with_spend", "days_near_cap", "budget_cap_pct",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_ad_group_inventory(conn, start: str, end: str) -> pd.DataFrame:
    """Ad groups with bid override flags and performance."""
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            ag.ad_group_name,
            ag.status,
            ag.ad_group_type,
            ROUND(ag.cpc_bid_micros / 1e6, 2)                       AS max_cpc,
            ag.bid_override_flag,
            ROUND(ag.target_cpa_micros / 1e6, 2)                    AS ag_target_cpa,
            ag.target_roas                                           AS ag_target_roas,
            -- Keyword count from keyword_instances
            (SELECT COUNT(*) FROM keyword_instances ki
             WHERE ki.campaign_name = c.campaign_name
               AND ki.ad_group_name = ag.ad_group_name
               AND ki.account_id = (
                   SELECT id FROM accounts
                   WHERE platform_account_id = ? AND platform = 'google'
               )
               AND ki.status != 'REMOVED'
            )                                                        AS active_keywords
        FROM ad_groups ag
        JOIN campaigns c ON ag.campaign_id = c.campaign_id
        WHERE ag.account_id = ?
          AND ag.status != 'REMOVED'
          AND c.status != 'REMOVED'
        ORDER BY c.campaign_name, ag.ad_group_name
    """, (MEDICARE_ACCOUNT_ID, MEDICARE_ACCOUNT_ID)).fetchall()

    return pd.DataFrame([dict(r) for r in rows])


def q_consolidation_signals(conn, start: str, end: str) -> pd.DataFrame:
    """
    Campaigns with low conversion signal in the period.
    Low signal = fewer than 30 conversions over the analysis window.
    These are candidates for consolidation to improve Smart Bidding efficiency.
    """
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            c.bidding_strategy_type,
            ROUND(c.daily_budget_micros / 1e6, 2)            AS daily_budget,
            (SELECT COUNT(*) FROM ad_groups ag
             WHERE ag.campaign_id = c.campaign_id
               AND ag.status != 'REMOVED')                   AS active_ad_groups,
            COALESCE(SUM(p.impressions), 0)                  AS impressions,
            COALESCE(SUM(p.clicks), 0)                       AS clicks,
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2) AS spend,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)        AS conversions,
            COUNT(DISTINCT p.date)                           AS active_days
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id
        HAVING conversions < 30
        ORDER BY conversions ASC, spend DESC
    """, (start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["signal_risk"] = df["conversions"].apply(
        lambda x: "Critical (<5)" if x < 5
        else "High (<15)" if x < 15
        else "Medium (<30)"
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type", "daily_budget",
        "active_ad_groups", "impressions", "clicks", "spend",
        "conversions", "cpa", "active_days", "signal_risk",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_naming_flags(conn) -> pd.DataFrame:
    """Campaigns with naming convention issues."""
    rows = conn.execute("""
        SELECT
            campaign_id,
            campaign_name,
            status,
            campaign_type,
            bidding_strategy_type,
            parsed_product_type,
            parsed_geo_state,
            parsed_match_tier,
            parsed_intent_tier,
            first_seen_date
        FROM campaigns
        WHERE account_id = ?
          AND naming_convention_flag = 1
          AND status != 'REMOVED'
        ORDER BY campaign_name
    """, (MEDICARE_ACCOUNT_ID,)).fetchall()

    return pd.DataFrame([dict(r) for r in rows])


# ── Excel writer ───────────────────────────────────────────────────────────────

def build_sheets(period: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    conn   = get_db_connection()
    label  = PERIOD_LABELS.get(period, f"{start} → {end}")

    sheets = {
        "01_Campaign_Inventory": q_campaign_inventory(conn, start, end),
        "01_Budget_Allocation":  q_budget_allocation(conn, start, end),
        "01_Ad_Group_Inventory": q_ad_group_inventory(conn, start, end),
        "01_Consolidation":      q_consolidation_signals(conn, start, end),
        "01_Naming_Flags":       q_naming_flags(conn),
    }
    conn.close()
    return sheets


def write_workbook(sheets: dict[str, pd.DataFrame], period: str,
                   start: str, end: str, output_path: Path) -> None:
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)   # remove default empty sheet

    sheet_configs = {
        "01_Campaign_Inventory": {
            "title":    "Medicare — Campaign Inventory",
            "subtitle": "All active campaigns with performance summary",
            "formats": {
                "daily_budget": XL.FMT_DOLLAR,
                "target_cpa":   XL.FMT_DOLLAR,
                "spend":        XL.FMT_DOLLAR,
                "conv_value":   XL.FMT_DOLLAR,
                "cpa":          XL.FMT_DOLLAR,
                "ctr":          XL.FMT_PCT_1,
                "cvr":          XL.FMT_PCT_1,
                "roas":         XL.FMT_FLOAT_2,
                "impressions":  XL.FMT_INT,
                "clicks":       XL.FMT_INT,
            },
            "widths": {"campaign_name": 55, "campaign_id": 18, "status": 12,
                       "campaign_type": 22, "bidding_strategy_type": 28},
        },
        "01_Budget_Allocation": {
            "title":    "Medicare — Budget Allocation",
            "subtitle": "Budget distribution, IS, and budget cap pressure by campaign",
            "formats": {
                "daily_budget":       XL.FMT_DOLLAR,
                "spend":              XL.FMT_DOLLAR,
                "cpa":                XL.FMT_DOLLAR,
                "pct_of_total_spend": XL.FMT_PCT_1,
                "avg_is":             XL.FMT_PCT_1,
                "avg_budget_lost_is": XL.FMT_PCT_1,
                "avg_rank_lost_is":   XL.FMT_PCT_1,
                "budget_cap_pct":     XL.FMT_PCT_1,
            },
            "widths": {"campaign_name": 55},
            "flag_col": "budget_cap_pct",
            "flag_rules": [
                (">=", 0.75, XL.FLAG_RED),
                (">=", 0.50, XL.FLAG_AMBER),
            ],
        },
        "01_Ad_Group_Inventory": {
            "title":    "Medicare — Ad Group Inventory",
            "subtitle": "All active ad groups with bid override flags",
            "formats": {
                "max_cpc":       XL.FMT_DOLLAR,
                "ag_target_cpa": XL.FMT_DOLLAR,
            },
            "widths": {"campaign_name": 55, "ad_group_name": 45},
            "flag_col": "bid_override_flag",
            "flag_rules": [("==", 1, XL.FLAG_RED)],
        },
        "01_Consolidation": {
            "title":    "Medicare — Consolidation Signals",
            "subtitle": "Campaigns with < 30 conversions in period — Smart Bidding signal risk",
            "formats": {
                "daily_budget": XL.FMT_DOLLAR,
                "spend":        XL.FMT_DOLLAR,
                "cpa":          XL.FMT_DOLLAR,
                "impressions":  XL.FMT_INT,
                "clicks":       XL.FMT_INT,
            },
            "widths": {"campaign_name": 55, "signal_risk": 18},
            "flag_col": "signal_risk",
            "flag_rules": [
                ("==", "Critical (<5)",  XL.FLAG_RED),    # note: string compare won't work
            ],
        },
        "01_Naming_Flags": {
            "title":    "Medicare — Naming Convention Flags",
            "subtitle": "Campaigns deviating from expected naming convention",
            "formats":  {},
            "widths": {"campaign_name": 65, "campaign_id": 18},
        },
    }

    for sheet_name, df in sheets.items():
        cfg = sheet_configs.get(sheet_name, {})
        ws  = wb.create_sheet(title=sheet_name)

        if df.empty:
            ws["A1"] = f"No data for period: {label}"
            continue

        n_cols  = len(df.columns)
        data_row = write_title_row(
            ws,
            title    = cfg.get("title", sheet_name),
            subtitle = cfg.get("subtitle", ""),
            period_label = label,
            n_cols   = n_cols,
        )

        write_dataframe_to_sheet(
            ws, df, data_row,
            col_widths     = cfg.get("widths"),
            number_formats = cfg.get("formats"),
            flag_col       = cfg.get("flag_col"),
            flag_rules     = cfg.get("flag_rules"),
        )

        autofit_columns(ws)
        freeze_header_rows(ws, freeze_row=data_row + 1)
        ws.sheet_view.zoomScale = 90

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"Saved: {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Medicare Account Structure & Hygiene analysis"
    )
    parser.add_argument("--period", choices=VALID_PERIODS, default="last_90",
                        help="Named analysis period (default: last_90)")
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date (required for --period custom)")
    parser.add_argument("--end",   metavar="YYYY-MM-DD",
                        help="End date (required for --period custom)")
    parser.add_argument("--out",   metavar="PATH",
                        help="Output .xlsx path (default: analysis/output/)")
    return parser.parse_args()


def main():
    args          = parse_args()
    start, end    = resolve_period(args.period, args.start, args.end)
    output_path   = Path(args.out) if args.out else (
        OUTPUT_DIR / f"medicare_01_account_structure_{args.period}.xlsx"
    )

    print(f"Period : {PERIOD_LABELS.get(args.period)} ({start} → {end})")
    print(f"Account: Medicare ({MEDICARE_ACCOUNT_ID})")

    sheets = build_sheets(args.period, start, end)
    for name, df in sheets.items():
        print(f"  {name:<35} {len(df):>6} rows")

    write_workbook(sheets, args.period, start, end, output_path)


if __name__ == "__main__":
    main()
