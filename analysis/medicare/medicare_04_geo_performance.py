"""
medicare_04_geo_performance.py
================================
Medicare Geo & Demographic Performance Analysis
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Produces sheets:
  04_State_Performance     — CPA, CVR, spend, conversions, IS by state
  04_State_Trend           — Monthly state performance for top 10 states by spend
  04_High_Cost_Low_Return  — States with CPA > 2× account average and > $500 spend
  04_Expansion_Signals     — States with IS < 50% and CPA < account average
  04_Campaign_x_State      — Campaign × state cross-tab (top campaigns)

Usage:
  python medicare_04_geo_performance.py --period last_90
  python medicare_04_geo_performance.py --period aep2025
"""

import argparse
import pandas as pd
from pathlib import Path
from openpyxl import Workbook

import sys
sys.path.insert(0, str(Path(__file__).parent))
from medicare_utils import (
    get_db_connection, resolve_period, PERIOD_LABELS, VALID_PERIODS,
    MEDICARE_ACCOUNT_ID, safe_div,
    write_title_row, write_dataframe_to_sheet, autofit_columns,
    freeze_header_rows, XL
)

OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\analysis\output")


# ── Queries ───────────────────────────────────────────────────────────────────

def q_state_performance(conn, start: str, end: str) -> pd.DataFrame:
    """CPA, CVR, spend, conversions, IS by state across all Medicare campaigns."""
    rows = conn.execute("""
        SELECT
            COALESCE(g.geo_state, 'Unknown')                    AS state,
            g.geo_target_constant_id,
            ROUND(SUM(g.cost_micros) / 1e6, 2)                 AS spend,
            SUM(g.impressions)                                  AS impressions,
            SUM(g.clicks)                                       AS clicks,
            ROUND(SUM(g.conversions), 1)                        AS conversions,
            ROUND(SUM(g.conversion_value), 2)                   AS conv_value,
            -- IS from daily table (not available at geo grain)
            -- Joined at state level by averaging IS across campaigns active in that state
            ROUND(AVG(d.search_impression_share), 3)            AS avg_is,
            ROUND(AVG(d.search_budget_lost_is), 3)              AS avg_budget_lost_is,
            ROUND(AVG(d.search_rank_lost_is), 3)                AS avg_rank_lost_is,
            COUNT(DISTINCT g.campaign_id)                       AS active_campaigns,
            COUNT(DISTINCT g.date)                              AS active_days
        FROM campaign_performance_geo_daily g
        -- Join daily IS data on matching campaign + date
        LEFT JOIN campaign_performance_daily d
            ON  d.campaign_id = g.campaign_id
            AND d.date = g.date
            AND d.device = 'MOBILE'
        WHERE g.account_id = ?
          AND g.date BETWEEN ? AND ?
        GROUP BY g.geo_state, g.geo_target_constant_id
        ORDER BY spend DESC
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["cvr"] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["ctr"] = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["rpc"] = df.apply(lambda r: safe_div(r.conv_value, r.clicks), axis=1)

    # Flag vs account average CPA
    acct_cpa = safe_div(df["spend"].sum(), df["conversions"].sum())
    df["vs_avg_cpa"] = df["cpa"].apply(
        lambda x: safe_div(x - acct_cpa, acct_cpa) if x and acct_cpa else None
    )

    col_order = [
        "state", "spend", "impressions", "clicks", "conversions",
        "conv_value", "cpa", "vs_avg_cpa", "cvr", "ctr", "rpc",
        "avg_is", "avg_budget_lost_is", "avg_rank_lost_is",
        "active_campaigns", "active_days",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_state_trend(conn, start: str, end: str, top_n: int = 10) -> pd.DataFrame:
    """Monthly performance for top N states by total spend in the period."""
    # First get top states
    top_rows = conn.execute("""
        SELECT COALESCE(geo_state, 'Unknown') AS state,
               SUM(cost_micros) AS total_spend
        FROM campaign_performance_geo_daily
        WHERE account_id = ?
          AND date BETWEEN ? AND ?
        GROUP BY geo_state
        ORDER BY total_spend DESC
        LIMIT ?
    """, (MEDICARE_ACCOUNT_ID, start, end, top_n)).fetchall()

    top_states = [r["state"] for r in top_rows]
    if not top_states:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(top_states))
    rows = conn.execute(f"""
        SELECT
            COALESCE(g.geo_state, 'Unknown')            AS state,
            STRFTIME('%Y-%m', g.date)                   AS month,
            ROUND(SUM(g.cost_micros) / 1e6, 2)          AS spend,
            SUM(g.clicks)                               AS clicks,
            ROUND(SUM(g.conversions), 1)                AS conversions,
            ROUND(SUM(g.conversion_value), 2)           AS conv_value
        FROM campaign_performance_geo_daily g
        WHERE g.account_id = ?
          AND g.date BETWEEN ? AND ?
          AND COALESCE(g.geo_state, 'Unknown') IN ({placeholders})
        GROUP BY g.geo_state, month
        ORDER BY state, month
    """, [MEDICARE_ACCOUNT_ID, start, end] + top_states).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["cvr"] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    return df


def q_high_cost_low_return(conn, start: str, end: str,
                            min_spend: float = 500.0,
                            cpa_multiplier: float = 2.0) -> pd.DataFrame:
    """States with CPA > N× account average and spend > threshold."""
    state_df = q_state_performance(conn, start, end)
    if state_df.empty:
        return state_df

    acct_cpa = safe_div(state_df["spend"].sum(), state_df["conversions"].sum())
    if not acct_cpa:
        return pd.DataFrame()

    threshold = acct_cpa * cpa_multiplier
    filtered  = state_df[
        (state_df["spend"] >= min_spend) &
        (state_df["cpa"].notna()) &
        (state_df["cpa"] >= threshold)
    ].copy()

    filtered["account_avg_cpa"]  = round(acct_cpa, 2)
    filtered["cpa_vs_avg"]       = filtered["cpa"] / acct_cpa
    filtered["recommended_action"] = filtered.apply(
        lambda r: "Review targeting / bids" if r.cpa < threshold * 1.5
        else "Consider pausing or restructuring", axis=1
    )

    col_order = [
        "state", "spend", "conversions", "conv_value", "cpa",
        "account_avg_cpa", "cpa_vs_avg", "avg_is", "avg_budget_lost_is",
        "clicks", "impressions", "cvr", "recommended_action",
    ]
    return filtered[[c for c in col_order if c in filtered.columns]].sort_values(
        "cpa", ascending=False
    )


def q_expansion_signals(conn, start: str, end: str,
                         max_is: float = 0.50) -> pd.DataFrame:
    """
    States with IS < 50% and CPA below account average.
    These are under-served markets with efficient performance — expansion candidates.
    """
    state_df = q_state_performance(conn, start, end)
    if state_df.empty:
        return state_df

    acct_cpa = safe_div(state_df["spend"].sum(), state_df["conversions"].sum())
    if not acct_cpa:
        return pd.DataFrame()

    filtered = state_df[
        (state_df["avg_is"].notna()) &
        (state_df["avg_is"] < max_is) &
        (state_df["cpa"].notna()) &
        (state_df["cpa"] < acct_cpa) &
        (state_df["conversions"] >= 1)
    ].copy()

    filtered["account_avg_cpa"]     = round(acct_cpa, 2)
    filtered["is_opportunity_gap"]  = 1 - filtered["avg_is"]
    filtered["expansion_priority"]  = filtered.apply(
        lambda r: "High" if r.avg_is < 0.30 and r.cpa < acct_cpa * 0.80
        else "Medium", axis=1
    )

    col_order = [
        "state", "spend", "conversions", "conv_value", "cpa",
        "account_avg_cpa", "avg_is", "is_opportunity_gap",
        "avg_budget_lost_is", "avg_rank_lost_is",
        "clicks", "cvr", "expansion_priority",
    ]
    return filtered[[c for c in col_order if c in filtered.columns]].sort_values(
        "is_opportunity_gap", ascending=False
    )


def q_campaign_x_state(conn, start: str, end: str, top_campaigns: int = 10) -> pd.DataFrame:
    """Top campaigns × top states spend cross-tab."""
    top_c = conn.execute("""
        SELECT campaign_id, SUM(cost_micros) AS total
        FROM campaign_performance_geo_daily
        WHERE account_id = ? AND date BETWEEN ? AND ?
        GROUP BY campaign_id ORDER BY total DESC LIMIT ?
    """, (MEDICARE_ACCOUNT_ID, start, end, top_campaigns)).fetchall()

    top_c_ids = [r["campaign_id"] for r in top_c]
    if not top_c_ids:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(top_c_ids))
    rows = conn.execute(f"""
        SELECT
            c.campaign_name,
            COALESCE(g.geo_state, 'Unknown')            AS state,
            ROUND(SUM(g.cost_micros) / 1e6, 2)          AS spend,
            ROUND(SUM(g.conversions), 1)                 AS conversions
        FROM campaign_performance_geo_daily g
        JOIN campaigns c ON g.campaign_id = c.campaign_id
        WHERE g.account_id = ?
          AND g.date BETWEEN ? AND ?
          AND g.campaign_id IN ({placeholders})
        GROUP BY c.campaign_name, g.geo_state
        ORDER BY spend DESC
    """, [MEDICARE_ACCOUNT_ID, start, end] + top_c_ids).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    return df


# ── Excel writer ───────────────────────────────────────────────────────────────

def build_sheets(period: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    conn = get_db_connection()
    sheets = {
        "04_State_Performance":    q_state_performance(conn, start, end),
        "04_State_Trend":          q_state_trend(conn, start, end),
        "04_High_Cost_Low_Return": q_high_cost_low_return(conn, start, end),
        "04_Expansion_Signals":    q_expansion_signals(conn, start, end),
        "04_Campaign_x_State":     q_campaign_x_state(conn, start, end),
    }
    conn.close()
    return sheets


def write_workbook(sheets: dict[str, pd.DataFrame], period: str,
                   start: str, end: str, output_path: Path) -> None:
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)

    dollar_pct = {
        "spend": XL.FMT_DOLLAR, "conv_value": XL.FMT_DOLLAR,
        "cpa": XL.FMT_DOLLAR, "account_avg_cpa": XL.FMT_DOLLAR,
        "rpc": XL.FMT_DOLLAR,
        "avg_is": XL.FMT_PCT_1, "avg_budget_lost_is": XL.FMT_PCT_1,
        "avg_rank_lost_is": XL.FMT_PCT_1, "vs_avg_cpa": XL.FMT_PCT_1,
        "cvr": XL.FMT_PCT_1, "ctr": XL.FMT_PCT_1,
        "is_opportunity_gap": XL.FMT_PCT_1, "cpa_vs_avg": XL.FMT_FLOAT_2,
        "impressions": XL.FMT_INT, "clicks": XL.FMT_INT,
    }

    sheet_configs = {
        "04_State_Performance": {
            "title":    "Medicare — State Performance",
            "subtitle": "Spend, CPA, CVR, IS by state across all campaigns",
            "formats":  dollar_pct,
            "widths":   {"state": 25, "recommended_action": 38},
            "flag_col": "vs_avg_cpa",
            "flag_rules": [
                (">=", 0.50, XL.FLAG_RED),
                (">=", 0.20, XL.FLAG_AMBER),
                ("<=", -0.20, XL.FLAG_GREEN),
            ],
        },
        "04_State_Trend": {
            "title":    "Medicare — Monthly State Trend (Top 10 States)",
            "subtitle": "Month-by-month spend, conversions, CPA for highest-spend states",
            "formats":  {**dollar_pct, "month": "@"},
            "widths":   {"state": 25, "month": 12},
        },
        "04_High_Cost_Low_Return": {
            "title":    "Medicare — High Cost / Low Return States",
            "subtitle": f"States with CPA > 2× account average and spend > $500",
            "formats":  dollar_pct,
            "widths":   {"state": 25, "recommended_action": 40},
        },
        "04_Expansion_Signals": {
            "title":    "Medicare — Expansion Opportunity States",
            "subtitle": "States with IS < 50% and CPA below account average",
            "formats":  dollar_pct,
            "widths":   {"state": 25, "expansion_priority": 15},
            "flag_col": "expansion_priority",
        },
        "04_Campaign_x_State": {
            "title":    "Medicare — Campaign × State Spend",
            "subtitle": "Spend and conversions by campaign and state (top 10 campaigns)",
            "formats":  {"spend": XL.FMT_DOLLAR, "cpa": XL.FMT_DOLLAR},
            "widths":   {"campaign_name": 55, "state": 25},
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
        description="Medicare Geo & Demographic Performance"
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
        OUTPUT_DIR / f"medicare_04_geo_performance_{args.period}.xlsx"
    )

    print(f"Period : {PERIOD_LABELS.get(args.period)} ({start} → {end})")
    sheets = build_sheets(args.period, start, end)
    for name, df in sheets.items():
        print(f"  {name:<40} {len(df):>6} rows")

    write_workbook(sheets, args.period, start, end, output_path)


if __name__ == "__main__":
    main()
