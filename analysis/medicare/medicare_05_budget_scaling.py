"""
medicare_05_budget_scaling.py
===============================
Medicare Budget & Scaling Opportunities Analysis
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Produces sheets:
  05_Budget_Utilization    — Daily spend vs budget cap, % utilized, days capped
  05_Constrained_Campaigns — Campaigns capped > 50% of days with CPA below target
  05_Marginal_CPA          — Spend buckets with conversion rate (diminishing returns proxy)
  05_Reallocation_Matrix   — Over-funded vs under-funded campaigns side-by-side
  05_Daily_Spend_Trend     — Daily spend trend by campaign for the period

Usage:
  python medicare_05_budget_scaling.py --period last_90
  python medicare_05_budget_scaling.py --period aep2025
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

# Budget cap threshold: day is "near cap" when spend >= this % of budget
NEAR_CAP_THRESHOLD = 0.90


# ── Queries ───────────────────────────────────────────────────────────────────

def q_budget_utilization(conn, start: str, end: str) -> pd.DataFrame:
    """
    Daily budget utilization by campaign.
    budget_micros_that_day is the snapshot of the daily budget on each date,
    so this correctly reflects historical budget changes.
    """
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.status,
            c.bidding_strategy_type,
            c.is_shared_budget,
            -- Current budget
            ROUND(c.daily_budget_micros / 1e6, 2)                    AS current_daily_budget,
            -- Period totals
            COUNT(DISTINCT p.date)                                    AS total_days,
            COUNT(DISTINCT CASE WHEN p.cost_micros > 0 THEN p.date END) AS days_with_spend,
            -- Days near budget cap (spend >= 90% of that day's budget)
            COUNT(DISTINCT CASE
                WHEN p.budget_micros_that_day > 0
                 AND CAST(p.cost_micros AS REAL) / p.budget_micros_that_day >= ?
                THEN p.date END)                                      AS days_near_cap,
            ROUND(SUM(p.cost_micros) / 1e6, 2)                       AS total_spend,
            ROUND(SUM(p.conversions), 1)                              AS total_conversions,
            ROUND(SUM(p.conversion_value), 2)                        AS total_conv_value,
            -- Average daily spend vs average daily budget
            ROUND(AVG(p.cost_micros) / 1e6, 2)                       AS avg_daily_spend,
            ROUND(AVG(p.budget_micros_that_day) / 1e6, 2)            AS avg_daily_budget,
            -- IS metrics
            ROUND(AVG(p.search_impression_share), 3)                  AS avg_is,
            ROUND(AVG(p.search_budget_lost_is), 3)                    AS avg_budget_lost_is
        FROM campaigns c
        LEFT JOIN campaign_performance_daily p
            ON  p.campaign_id = c.campaign_id
            AND p.device = 'MOBILE'
            AND p.date BETWEEN ? AND ?
        WHERE c.account_id = ?
          AND c.status != 'REMOVED'
        GROUP BY c.campaign_id
        ORDER BY days_near_cap DESC, total_spend DESC
    """, (NEAR_CAP_THRESHOLD, start, end, MEDICARE_ACCOUNT_ID)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cap_rate"] = df.apply(
        lambda r: safe_div(r.days_near_cap, r.days_with_spend, 0), axis=1
    )
    df["avg_utilization"] = df.apply(
        lambda r: safe_div(r.avg_daily_spend, r.avg_daily_budget, 0), axis=1
    )
    df["cpa"] = df.apply(
        lambda r: safe_div(r.total_spend, r.total_conversions), axis=1
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type", "is_shared_budget",
        "current_daily_budget", "avg_daily_budget", "avg_daily_spend",
        "avg_utilization", "total_days", "days_with_spend", "days_near_cap",
        "cap_rate", "total_spend", "total_conversions", "total_conv_value",
        "cpa", "avg_is", "avg_budget_lost_is",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_constrained_campaigns(conn, start: str, end: str,
                             min_cap_rate: float = 0.50) -> pd.DataFrame:
    """
    Campaigns capped > 50% of active days with good CPA.
    These are the strongest scaling opportunities — they're efficient but
    limited by budget, not by demand or performance.
    """
    util_df = q_budget_utilization(conn, start, end)
    if util_df.empty:
        return util_df

    # Account average CPA as benchmark
    acct_cpa = safe_div(util_df["total_spend"].sum(),
                        util_df["total_conversions"].sum())

    constrained = util_df[
        util_df["cap_rate"] >= min_cap_rate
    ].copy()

    if constrained.empty:
        return constrained

    constrained["account_avg_cpa"] = round(acct_cpa, 2) if acct_cpa else None
    constrained["cpa_vs_avg"]      = constrained["cpa"].apply(
        lambda x: safe_div(x, acct_cpa) if x and acct_cpa else None
    )
    constrained["scaling_priority"] = constrained.apply(
        lambda r: "High — efficient + budget-capped"
        if r.cpa and acct_cpa and r.cpa < acct_cpa
        else "Review — capped but above avg CPA", axis=1
    )
    constrained["suggested_budget_increase"] = constrained.apply(
        lambda r: f"+{int((1 / (1 - r.cap_rate) - 1) * 100)}% (est.)"
        if r.cap_rate < 1.0 else "Review — fully capped", axis=1
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type",
        "current_daily_budget", "avg_daily_budget", "cap_rate",
        "days_near_cap", "days_with_spend",
        "total_spend", "total_conversions", "cpa",
        "account_avg_cpa", "cpa_vs_avg",
        "avg_is", "avg_budget_lost_is",
        "scaling_priority", "suggested_budget_increase",
    ]
    return constrained[[c for c in col_order if c in constrained.columns]].sort_values(
        "cap_rate", ascending=False
    )


def q_marginal_cpa(conn, start: str, end: str) -> pd.DataFrame:
    """
    Approximation of diminishing returns by bucketing campaigns into spend tiers
    and computing conversion rate per tier.
    Higher spend tiers with lower CVR indicate diminishing returns.
    """
    rows = conn.execute("""
        SELECT
            c.campaign_name,
            c.bidding_strategy_type,
            ROUND(p.cost_micros / 1e6, 2)       AS daily_spend,
            p.clicks,
            p.conversions,
            p.date
        FROM campaign_performance_daily p
        JOIN campaigns c ON p.campaign_id = c.campaign_id
        WHERE p.account_id = ?
          AND p.device = 'MOBILE'
          AND p.date BETWEEN ? AND ?
          AND p.cost_micros > 0
          AND c.status != 'REMOVED'
        ORDER BY c.campaign_name, p.date
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    # Bucket daily spend into quintiles per campaign
    def bucket_spend(grp):
        try:
            grp["spend_bucket"] = pd.qcut(
                grp["daily_spend"], q=5,
                labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"],
                duplicates="drop"
            )
        except Exception:
            grp["spend_bucket"] = "Single bucket"
        return grp

    df = df.groupby("campaign_name", group_keys=False).apply(bucket_spend)

    summary = df.groupby(["campaign_name", "spend_bucket"]).agg(
        days          = ("date", "count"),
        avg_spend     = ("daily_spend", "mean"),
        total_clicks  = ("clicks", "sum"),
        total_conv    = ("conversions", "sum"),
    ).reset_index()

    summary["avg_spend"]  = summary["avg_spend"].round(2)
    summary["cvr"]        = summary.apply(
        lambda r: safe_div(r.total_conv, r.total_clicks), axis=1
    )
    summary["avg_cpa_bucket"] = summary.apply(
        lambda r: safe_div(r.avg_spend, safe_div(r.total_conv, r.days)),
        axis=1
    )

    col_order = [
        "campaign_name", "spend_bucket", "days", "avg_spend",
        "total_clicks", "total_conv", "cvr", "avg_cpa_bucket",
    ]
    return summary[[c for c in col_order if c in summary.columns]]


def q_reallocation_matrix(conn, start: str, end: str) -> pd.DataFrame:
    """
    Reallocation opportunities: over-funded vs under-funded campaigns.
    - Over-funded: low cap rate AND above-average CPA (spending freely, inefficiently)
    - Under-funded: high cap rate AND below-average CPA (spending constrained, efficiently)
    """
    util_df = q_budget_utilization(conn, start, end)
    if util_df.empty:
        return util_df

    acct_cpa = safe_div(
        util_df["total_spend"].sum(),
        util_df["total_conversions"].sum()
    )
    if not acct_cpa:
        return pd.DataFrame()

    def classify(row):
        if row["total_spend"] < 100:
            return "Insufficient data"
        cpa     = row["cpa"]
        cap     = row["cap_rate"]
        if cpa is None:
            return "No conversions"
        if cap >= 0.50 and cpa < acct_cpa:
            return "SCALE UP — efficient + constrained"
        if cap < 0.20 and cpa > acct_cpa * 1.20:
            return "SCALE DOWN — inefficient + unconstrained"
        if cap >= 0.50 and cpa >= acct_cpa:
            return "REVIEW — constrained but inefficient"
        if cap < 0.20 and cpa <= acct_cpa:
            return "MONITOR — efficient but low utilization"
        return "STABLE"

    util_df["account_avg_cpa"]  = round(acct_cpa, 2)
    util_df["allocation_action"] = util_df.apply(classify, axis=1)
    util_df["budget_delta_rec"]  = util_df.apply(
        lambda r: "+25–50%" if "SCALE UP" in str(r.allocation_action)
        else "-25%" if "SCALE DOWN" in str(r.allocation_action)
        else "Hold", axis=1
    )

    col_order = [
        "campaign_name", "status", "bidding_strategy_type",
        "current_daily_budget", "cap_rate", "avg_utilization",
        "total_spend", "total_conversions", "cpa",
        "account_avg_cpa", "avg_is", "avg_budget_lost_is",
        "allocation_action", "budget_delta_rec",
    ]
    return util_df[[c for c in col_order if c in util_df.columns]].sort_values(
        "allocation_action"
    )


def q_daily_spend_trend(conn, start: str, end: str,
                         top_n: int = 10) -> pd.DataFrame:
    """Daily spend trend for top N campaigns by total spend."""
    top_c = conn.execute("""
        SELECT campaign_id FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'MOBILE' AND date BETWEEN ? AND ?
        GROUP BY campaign_id
        ORDER BY SUM(cost_micros) DESC LIMIT ?
    """, (MEDICARE_ACCOUNT_ID, start, end, top_n)).fetchall()

    if not top_c:
        return pd.DataFrame()

    top_ids      = [r["campaign_id"] for r in top_c]
    placeholders = ",".join("?" * len(top_ids))

    rows = conn.execute(f"""
        SELECT
            c.campaign_name,
            p.date,
            ROUND(p.cost_micros / 1e6, 2)                    AS spend,
            ROUND(p.budget_micros_that_day / 1e6, 2)         AS budget,
            p.conversions,
            p.impressions
        FROM campaign_performance_daily p
        JOIN campaigns c ON p.campaign_id = c.campaign_id
        WHERE p.account_id = ?
          AND p.device = 'MOBILE'
          AND p.date BETWEEN ? AND ?
          AND p.campaign_id IN ({placeholders})
        ORDER BY c.campaign_name, p.date
    """, [MEDICARE_ACCOUNT_ID, start, end] + top_ids).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["utilization"] = df.apply(
        lambda r: safe_div(r.spend, r.budget, 0), axis=1
    )
    return df


# ── Excel writer ───────────────────────────────────────────────────────────────

def build_sheets(period: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    conn = get_db_connection()
    sheets = {
        "05_Budget_Utilization":    q_budget_utilization(conn, start, end),
        "05_Constrained_Campaigns": q_constrained_campaigns(conn, start, end),
        "05_Marginal_CPA":          q_marginal_cpa(conn, start, end),
        "05_Reallocation_Matrix":   q_reallocation_matrix(conn, start, end),
        "05_Daily_Spend_Trend":     q_daily_spend_trend(conn, start, end),
    }
    conn.close()
    return sheets


def write_workbook(sheets: dict[str, pd.DataFrame], period: str,
                   start: str, end: str, output_path: Path) -> None:
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)

    common_formats = {
        "current_daily_budget": XL.FMT_DOLLAR,
        "avg_daily_budget":     XL.FMT_DOLLAR,
        "avg_daily_spend":      XL.FMT_DOLLAR,
        "total_spend":          XL.FMT_DOLLAR,
        "total_conv_value":     XL.FMT_DOLLAR,
        "cpa":                  XL.FMT_DOLLAR,
        "account_avg_cpa":      XL.FMT_DOLLAR,
        "spend":                XL.FMT_DOLLAR,
        "budget":               XL.FMT_DOLLAR,
        "avg_spend":            XL.FMT_DOLLAR,
        "avg_cpa_bucket":       XL.FMT_DOLLAR,
        "cap_rate":             XL.FMT_PCT_1,
        "avg_utilization":      XL.FMT_PCT_1,
        "utilization":          XL.FMT_PCT_1,
        "cpa_vs_avg":           XL.FMT_FLOAT_2,
        "avg_is":               XL.FMT_PCT_1,
        "avg_budget_lost_is":   XL.FMT_PCT_1,
        "cvr":                  XL.FMT_PCT_1,
    }

    sheet_configs = {
        "05_Budget_Utilization": {
            "title":    "Medicare — Budget Utilization",
            "subtitle": f"Daily spend vs budget cap (≥{int(NEAR_CAP_THRESHOLD*100)}% = near cap)",
            "formats":  common_formats,
            "widths":   {"campaign_name": 55, "bidding_strategy_type": 30},
            "flag_col": "cap_rate",
            "flag_rules": [
                (">=", 0.75, XL.FLAG_RED),
                (">=", 0.50, XL.FLAG_AMBER),
                ("<=", 0.10, XL.FLAG_GREEN),
            ],
        },
        "05_Constrained_Campaigns": {
            "title":    "Medicare — Budget-Constrained Campaigns",
            "subtitle": "Campaigns capped > 50% of days — scaling opportunities",
            "formats":  common_formats,
            "widths":   {
                "campaign_name": 55, "scaling_priority": 40,
                "suggested_budget_increase": 28,
            },
        },
        "05_Marginal_CPA": {
            "title":    "Medicare — Marginal CPA by Spend Tier",
            "subtitle": "Daily spend bucketed into quintiles — diminishing returns proxy",
            "formats":  {
                "avg_spend":      XL.FMT_DOLLAR,
                "avg_cpa_bucket": XL.FMT_DOLLAR,
                "cvr":            XL.FMT_PCT_1,
            },
            "widths": {"campaign_name": 55, "spend_bucket": 18},
        },
        "05_Reallocation_Matrix": {
            "title":    "Medicare — Budget Reallocation Matrix",
            "subtitle": "Scale up efficient/constrained campaigns; scale down inefficient/unconstrained",
            "formats":  common_formats,
            "widths":   {
                "campaign_name": 55, "allocation_action": 40,
                "budget_delta_rec": 16,
            },
            "flag_col": "allocation_action",
        },
        "05_Daily_Spend_Trend": {
            "title":    "Medicare — Daily Spend Trend (Top 10 Campaigns)",
            "subtitle": "Daily spend vs budget for highest-spend campaigns",
            "formats":  {
                "spend":       XL.FMT_DOLLAR,
                "budget":      XL.FMT_DOLLAR,
                "utilization": XL.FMT_PCT_1,
            },
            "widths": {"campaign_name": 55, "date": 14},
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
        description="Medicare Budget & Scaling Opportunities"
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
        OUTPUT_DIR / f"medicare_05_budget_scaling_{args.period}.xlsx"
    )

    print(f"Period : {PERIOD_LABELS.get(args.period)} ({start} → {end})")
    sheets = build_sheets(args.period, start, end)
    for name, df in sheets.items():
        print(f"  {name:<40} {len(df):>6} rows")

    write_workbook(sheets, args.period, start, end, output_path)


if __name__ == "__main__":
    main()
