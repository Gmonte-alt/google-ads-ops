"""
medicare_02_bidding_efficiency.py
===================================
Medicare Bidding Strategy & Efficiency Analysis
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Produces sheets:
  02_Bid_Strategy_Summary  — Strategy type, target CPA/ROAS vs actual, delta
  02_Portfolio_vs_Campaign — Portfolio strategy campaigns vs campaign-level comparison
  02_Device_Performance    — CPA, CVR, spend, conversions by device per campaign
  02_Weekly_Target_vs_Actual — tCPA target vs actual CPA trended weekly
  02_Learning_Flags        — Campaigns with < 30 conversions in last 30 days

Usage:
  python medicare_02_bidding_efficiency.py --period last_90
  python medicare_02_bidding_efficiency.py --period aep2025
"""

import argparse
import pandas as pd
from pathlib import Path
from openpyxl import Workbook
from datetime import datetime, timedelta

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

def q_bid_strategy_summary(conn, start: str, end: str) -> pd.DataFrame:
    """Bid strategy per campaign with target vs actual CPA/ROAS."""
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            c.bidding_strategy_type                                  AS current_strategy,
            CASE WHEN c.bidding_strategy_id IS NOT NULL
                 THEN 'Portfolio' ELSE 'Campaign-level' END          AS strategy_level,
            ROUND(c.target_cpa_micros / 1e6, 2)                     AS target_cpa,
            c.target_roas                                            AS target_roas,
            -- Actual performance over period (device=ALL to avoid double-counting)
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2)         AS spend,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)                AS conversions,
            ROUND(COALESCE(SUM(p.conversion_value), 0), 2)           AS conv_value,
            COUNT(DISTINCT p.date)                                    AS active_days,
            -- Snapshot: what strategy was active on most recent day in period
            (SELECT p2.bidding_strategy_type_that_day
             FROM campaign_performance_daily p2
             WHERE p2.campaign_id = c.campaign_id
               AND p2.device = 'MOBILE'
               AND p2.date BETWEEN ? AND ?
               AND p2.bidding_strategy_type_that_day IS NOT NULL
             ORDER BY p2.date DESC LIMIT 1)                          AS last_strategy_snapshot,
            (SELECT ROUND(p2.target_cpa_micros_that_day / 1e6, 2)
             FROM campaign_performance_daily p2
             WHERE p2.campaign_id = c.campaign_id
               AND p2.device = 'MOBILE'
               AND p2.date BETWEEN ? AND ?
               AND p2.target_cpa_micros_that_day IS NOT NULL
             ORDER BY p2.date DESC LIMIT 1)                          AS last_target_cpa_snapshot
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id
        ORDER BY spend DESC
    """, (start, end, start, end, start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["actual_cpa"]  = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["actual_roas"] = df.apply(lambda r: safe_div(r.conv_value, r.spend), axis=1)
    df["cpa_delta"]   = df.apply(
        lambda r: (r.actual_cpa - r.target_cpa)
        if r.target_cpa and r.actual_cpa else None, axis=1
    )
    df["cpa_vs_target_pct"] = df.apply(
        lambda r: safe_div(r.actual_cpa - r.target_cpa, r.target_cpa)
        if r.target_cpa and r.actual_cpa else None, axis=1
    )

    col_order = [
        "campaign_name", "status", "current_strategy", "strategy_level",
        "target_cpa", "last_target_cpa_snapshot",
        "actual_cpa", "cpa_delta", "cpa_vs_target_pct",
        "target_roas", "actual_roas",
        "spend", "conversions", "conv_value", "active_days",
        "last_strategy_snapshot",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_portfolio_vs_campaign(conn, start: str, end: str) -> pd.DataFrame:
    """Compare performance of portfolio-strategy vs campaign-level-strategy campaigns."""
    rows = conn.execute("""
        SELECT
            CASE WHEN c.bidding_strategy_id IS NOT NULL
                 THEN 'Portfolio' ELSE 'Campaign-level' END          AS strategy_level,
            c.bidding_strategy_type,
            COUNT(DISTINCT c.campaign_id)                            AS campaign_count,
            ROUND(SUM(c.daily_budget_micros) / 1e6, 2)              AS total_daily_budget,
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2)         AS spend,
            ROUND(COALESCE(SUM(p.impressions), 0), 0)               AS impressions,
            ROUND(COALESCE(SUM(p.clicks), 0), 0)                    AS clicks,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)               AS conversions,
            ROUND(COALESCE(SUM(p.conversion_value), 0), 2)          AS conv_value,
            ROUND(AVG(p.search_impression_share), 3)                AS avg_is
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY strategy_level, c.bidding_strategy_type
        ORDER BY spend DESC
    """, (start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"]  = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["roas"] = df.apply(lambda r: safe_div(r.conv_value, r.spend), axis=1)
    df["ctr"]  = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["cvr"]  = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)

    col_order = [
        "strategy_level", "bidding_strategy_type", "campaign_count",
        "total_daily_budget", "spend", "impressions", "clicks", "conversions",
        "conv_value", "cpa", "roas", "ctr", "cvr", "avg_is",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_device_performance(conn, start: str, end: str) -> pd.DataFrame:
    """CPA, CVR, spend by device per campaign."""
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            p.device,
            ROUND(SUM(p.cost_micros) / 1e6, 2)         AS spend,
            SUM(p.impressions)                          AS impressions,
            SUM(p.clicks)                               AS clicks,
            ROUND(SUM(p.conversions), 1)                AS conversions,
            ROUND(SUM(p.conversion_value), 2)           AS conv_value
        FROM campaign_performance_daily p
        JOIN campaigns c ON p.campaign_id = c.campaign_id
        WHERE p.account_id = ?
          AND p.device != 'ALL'
          AND p.date BETWEEN ? AND ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id, p.device
        ORDER BY c.campaign_name, spend DESC
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["cvr"] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["ctr"] = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)

    col_order = [
        "campaign_name", "device", "spend", "impressions", "clicks",
        "conversions", "conv_value", "cpa", "cvr", "ctr",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_weekly_target_vs_actual(conn, start: str, end: str) -> pd.DataFrame:
    """Weekly tCPA target vs actual CPA trend for tCPA campaigns."""
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            -- ISO week start (Monday)
            DATE(p.date, 'weekday 0', '-6 days')                     AS week_start,
            ROUND(AVG(p.target_cpa_micros_that_day) / 1e6, 2)        AS avg_target_cpa,
            ROUND(SUM(p.cost_micros) / 1e6, 2)                       AS spend,
            ROUND(SUM(p.conversions), 1)                              AS conversions
        FROM campaign_performance_daily p
        JOIN campaigns c ON p.campaign_id = c.campaign_id
        WHERE p.account_id = ?
          AND p.device = 'MOBILE'
          AND p.date BETWEEN ? AND ?
          AND p.target_cpa_micros_that_day IS NOT NULL
          AND p.target_cpa_micros_that_day > 0
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id, week_start
        ORDER BY c.campaign_name, week_start
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["actual_cpa"] = df.apply(
        lambda r: safe_div(r.spend, r.conversions), axis=1
    )
    df["cpa_delta"] = df.apply(
        lambda r: (r.actual_cpa - r.avg_target_cpa)
        if r.avg_target_cpa and r.actual_cpa else None, axis=1
    )
    df["pct_vs_target"] = df.apply(
        lambda r: safe_div(r.actual_cpa - r.avg_target_cpa, r.avg_target_cpa)
        if r.avg_target_cpa and r.actual_cpa else None, axis=1
    )

    col_order = [
        "campaign_name", "week_start", "avg_target_cpa",
        "actual_cpa", "cpa_delta", "pct_vs_target",
        "spend", "conversions",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_learning_flags(conn) -> pd.DataFrame:
    """
    Campaigns with fewer than 30 conversions in the last 30 days.
    Smart Bidding requires ~30-50 conversions/month to exit learning mode.
    """
    from datetime import date, timedelta
    end_30   = (date.today() - timedelta(days=1)).isoformat()
    start_30 = (date.today() - timedelta(days=30)).isoformat()

    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            c.bidding_strategy_type,
            ROUND(c.target_cpa_micros / 1e6, 2)                     AS target_cpa,
            ROUND(COALESCE(SUM(p.cost_micros), 0) / 1e6, 2)         AS spend_30d,
            ROUND(COALESCE(SUM(p.conversions), 0), 1)                AS conversions_30d,
            COUNT(DISTINCT p.date)                                    AS active_days_30d
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
          AND c.bidding_strategy_type IN (
              'TARGET_CPA', 'TARGET_ROAS',
              'MAXIMIZE_CONVERSIONS', 'MAXIMIZE_CONVERSION_VALUE'
          )
        GROUP BY c.campaign_id
        HAVING conversions_30d < 30
        ORDER BY conversions_30d ASC
    """, (start_30, end_30, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["learning_risk"] = df["conversions_30d"].apply(
        lambda x: "Likely in Learning (<15 conv)" if x < 15
        else "At Risk (15–29 conv)"
    )
    df["actual_cpa_30d"] = df.apply(
        lambda r: safe_div(r.spend_30d, r.conversions_30d), axis=1
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type", "target_cpa",
        "conversions_30d", "spend_30d", "actual_cpa_30d",
        "active_days_30d", "learning_risk",
    ]
    return df[[c for c in col_order if c in df.columns]]


# ── Excel writer ───────────────────────────────────────────────────────────────

def build_sheets(period: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    conn = get_db_connection()
    sheets = {
        "02_Bid_Strategy_Summary":   q_bid_strategy_summary(conn, start, end),
        "02_Portfolio_vs_Campaign":  q_portfolio_vs_campaign(conn, start, end),
        "02_Device_Performance":     q_device_performance(conn, start, end),
        "02_Weekly_Target_vs_Actual": q_weekly_target_vs_actual(conn, start, end),
        "02_Learning_Flags":         q_learning_flags(conn),
    }
    conn.close()
    return sheets


def write_workbook(sheets: dict[str, pd.DataFrame], period: str,
                   start: str, end: str, output_path: Path) -> None:
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)

    sheet_configs = {
        "02_Bid_Strategy_Summary": {
            "title":    "Medicare — Bidding Strategy Summary",
            "subtitle": "Target CPA/ROAS vs actual performance by campaign",
            "formats": {
                "target_cpa":              XL.FMT_DOLLAR,
                "last_target_cpa_snapshot":XL.FMT_DOLLAR,
                "actual_cpa":              XL.FMT_DOLLAR,
                "cpa_delta":               XL.FMT_DOLLAR,
                "cpa_vs_target_pct":       XL.FMT_PCT_1,
                "spend":                   XL.FMT_DOLLAR,
                "conv_value":              XL.FMT_DOLLAR,
                "actual_roas":             XL.FMT_FLOAT_2,
                "target_roas":             XL.FMT_FLOAT_2,
            },
            "widths": {"campaign_name": 55},
            "flag_col": "cpa_vs_target_pct",
            "flag_rules": [
                (">=", 0.20, XL.FLAG_RED),
                (">=", 0.10, XL.FLAG_AMBER),
            ],
        },
        "02_Portfolio_vs_Campaign": {
            "title":    "Medicare — Portfolio vs Campaign-Level Bidding",
            "subtitle": "Aggregate performance comparison by strategy level",
            "formats": {
                "total_daily_budget": XL.FMT_DOLLAR,
                "spend":  XL.FMT_DOLLAR,
                "cpa":    XL.FMT_DOLLAR,
                "roas":   XL.FMT_FLOAT_2,
                "ctr":    XL.FMT_PCT_1,
                "cvr":    XL.FMT_PCT_1,
                "avg_is": XL.FMT_PCT_1,
                "conv_value": XL.FMT_DOLLAR,
                "impressions": XL.FMT_INT,
                "clicks": XL.FMT_INT,
            },
            "widths": {"bidding_strategy_type": 35},
        },
        "02_Device_Performance": {
            "title":    "Medicare — Device Performance",
            "subtitle": "CPA, CVR, spend segmented by device per campaign",
            "formats": {
                "spend":     XL.FMT_DOLLAR,
                "cpa":       XL.FMT_DOLLAR,
                "cvr":       XL.FMT_PCT_1,
                "ctr":       XL.FMT_PCT_1,
                "conv_value":XL.FMT_DOLLAR,
                "impressions": XL.FMT_INT,
                "clicks":    XL.FMT_INT,
            },
            "widths": {"campaign_name": 55},
        },
        "02_Weekly_Target_vs_Actual": {
            "title":    "Medicare — Weekly tCPA Target vs Actual",
            "subtitle": "Week-by-week CPA drift for tCPA campaigns",
            "formats": {
                "avg_target_cpa": XL.FMT_DOLLAR,
                "actual_cpa":     XL.FMT_DOLLAR,
                "cpa_delta":      XL.FMT_DOLLAR,
                "pct_vs_target":  XL.FMT_PCT_1,
                "spend":          XL.FMT_DOLLAR,
            },
            "widths": {"campaign_name": 55, "week_start": 14},
            "flag_col": "pct_vs_target",
            "flag_rules": [
                (">=", 0.20, XL.FLAG_RED),
                (">=", 0.10, XL.FLAG_AMBER),
                ("<=", -0.10, XL.FLAG_GREEN),
            ],
        },
        "02_Learning_Flags": {
            "title":    "Medicare — Smart Bidding Learning Risk",
            "subtitle": "Campaigns with < 30 conversions in last 30 days (Smart Bidding signal risk)",
            "formats": {
                "target_cpa":     XL.FMT_DOLLAR,
                "spend_30d":      XL.FMT_DOLLAR,
                "actual_cpa_30d": XL.FMT_DOLLAR,
            },
            "widths": {"campaign_name": 55, "learning_risk": 30},
        },
    }

    for sheet_name, df in sheets.items():
        cfg = sheet_configs.get(sheet_name, {})
        ws  = wb.create_sheet(title=sheet_name)

        if df.empty:
            ws["A1"] = f"No data for period: {label}"
            continue

        n_cols   = len(df.columns)
        data_row = write_title_row(
            ws,
            title        = cfg.get("title", sheet_name),
            subtitle     = cfg.get("subtitle", ""),
            period_label = label,
            n_cols       = n_cols,
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
        description="Medicare Bidding Strategy & Efficiency analysis"
    )
    parser.add_argument("--period", choices=VALID_PERIODS, default="last_90")
    parser.add_argument("--start", metavar="YYYY-MM-DD")
    parser.add_argument("--end",   metavar="YYYY-MM-DD")
    parser.add_argument("--out",   metavar="PATH")
    return parser.parse_args()


def main():
    args       = parse_args()
    start, end = resolve_period(args.period, args.start, args.end)
    output_path = Path(args.out) if args.out else (
        OUTPUT_DIR / f"medicare_02_bidding_efficiency_{args.period}.xlsx"
    )

    print(f"Period : {PERIOD_LABELS.get(args.period)} ({start} → {end})")
    sheets = build_sheets(args.period, start, end)
    for name, df in sheets.items():
        print(f"  {name:<40} {len(df):>6} rows")

    write_workbook(sheets, args.period, start, end, output_path)


if __name__ == "__main__":
    main()
