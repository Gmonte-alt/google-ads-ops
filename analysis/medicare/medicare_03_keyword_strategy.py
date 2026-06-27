"""
medicare_03_keyword_strategy.py
=================================
Medicare Keyword Strategy & Search Query Mining
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Data source: keyword tracking system tables (kw_db_scripts)
  - keyword_instances       (1,723 keywords confirmed for Medicare)
  - keyword_performance_daily
  - search_query_performance (2,197,010 rows confirmed)
  - search_queries           (136,277 unique queries confirmed)

Produces sheets:
  03_Match_Type_Mix       — Keyword count and spend/conv by match type per campaign
  03_Brand_vs_NonBrand    — Revenue/conversion split by brand category
  03_Waste_Candidates     — High-spend, low-conversion queries → negative candidates
  03_Promote_Candidates   — High-converting broad/phrase queries → promote to exact
  03_Top_Queries          — Top 50 queries by conversion value in period
  03_CPC_Inflation        — Top 25 keywords by avg CPC with CTR and CVR

Usage:
  python medicare_03_keyword_strategy.py --period last_90
  python medicare_03_keyword_strategy.py --period aep2025
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

# ── Medicare internal account ID lookup ───────────────────────────────────────

def get_medicare_internal_id(conn) -> int | None:
    """Get the accounts.id (integer PK) for the Medicare platform account."""
    row = conn.execute("""
        SELECT id FROM accounts
        WHERE platform_account_id = ? AND platform = 'google'
    """, (MEDICARE_ACCOUNT_ID,)).fetchone()
    return row["id"] if row else None


# ── Queries ───────────────────────────────────────────────────────────────────

def q_match_type_mix(conn, account_id: int, start: str, end: str) -> pd.DataFrame:
    """Keyword count and performance by match type per campaign."""
    rows = conn.execute("""
        SELECT
            ki.campaign_name,
            ki.match_type,
            COUNT(DISTINCT ki.id)                                AS keyword_count,
            ROUND(SUM(kpd.cost_micros) / 1e6, 2)               AS spend,
            SUM(kpd.clicks)                                     AS clicks,
            SUM(kpd.impressions)                                AS impressions,
            ROUND(SUM(kpd.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(kpd.primary_conv_value), 2)               AS conv_value
        FROM keyword_instances ki
        JOIN keyword_performance_daily kpd
            ON  kpd.instance_id = ki.id
            AND kpd.device = 'ALL'
            AND kpd.geo_state = 'ALL'
            AND kpd.report_date BETWEEN ? AND ?
        WHERE ki.account_id = ?
          AND ki.status != 'REMOVED'
        GROUP BY ki.campaign_name, ki.match_type
        ORDER BY ki.campaign_name, spend DESC
    """, (start, end, account_id)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"]  = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["ctr"]  = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["cvr"]  = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["cpc"]  = df.apply(lambda r: safe_div(r.spend, r.clicks), axis=1)

    col_order = [
        "campaign_name", "match_type", "keyword_count",
        "impressions", "clicks", "spend", "conversions", "conv_value",
        "cpa", "ctr", "cvr", "cpc",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_brand_vs_nonbrand(conn, account_id: int, start: str, end: str) -> pd.DataFrame:
    """Revenue and conversion split by brand category."""
    rows = conn.execute("""
        SELECT
            ki.campaign_name,
            COALESCE(sq.brand_category, 'unclassified')         AS brand_category,
            COALESCE(sq.modifier_brand, '(non-brand)')          AS modifier_brand,
            COUNT(DISTINCT sq.id)                               AS query_count,
            ROUND(SUM(sqp.primary_conv_value), 2)               AS conv_value,
            ROUND(SUM(sqp.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(sqp.cost_micros) / 1e6, 2)               AS spend,
            SUM(sqp.clicks)                                     AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY ki.campaign_name, sq.brand_category, sq.modifier_brand
        ORDER BY ki.campaign_name, conv_value DESC
    """, (account_id, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"]     = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["rev_per_click"] = df.apply(lambda r: safe_div(r.conv_value, r.clicks), axis=1)

    col_order = [
        "campaign_name", "brand_category", "modifier_brand",
        "query_count", "clicks", "spend", "conversions", "conv_value",
        "cpa", "rev_per_click",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_waste_candidates(conn, account_id: int, start: str, end: str,
                        min_spend: float = 50.0,
                        max_conversions: float = 0.0) -> pd.DataFrame:
    """
    High-spend, zero-conversion queries → negative keyword candidates.
    Default threshold: spend > $50, conversions = 0.
    """
    rows = conn.execute("""
        SELECT
            sq.query_text,
            sq.normalized_text,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.tag_method,
            ki.match_type                                       AS matched_match_type,
            ki.campaign_name,
            ki.ad_group_name,
            ROUND(SUM(sqp.cost_micros) / 1e6, 2)               AS spend,
            SUM(sqp.clicks)                                     AS clicks,
            SUM(sqp.impressions)                                AS impressions,
            ROUND(SUM(sqp.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(sqp.primary_conv_value), 2)               AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sq.id, ki.campaign_name, ki.ad_group_name
        HAVING spend >= ?
           AND conversions <= ?
        ORDER BY spend DESC
        LIMIT 200
    """, (account_id, start, end, min_spend, max_conversions)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpc"] = df.apply(lambda r: safe_div(r.spend, r.clicks), axis=1)
    df["negative_recommendation"] = df["matched_match_type"].apply(
        lambda m: f"Add as {m} negative" if m else "Review and add negative"
    )

    col_order = [
        "query_text", "campaign_name", "ad_group_name", "matched_match_type",
        "brand_category", "modifier_brand", "tag_method",
        "spend", "clicks", "impressions", "cpc", "conversions", "conv_value",
        "negative_recommendation",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_promote_candidates(conn, account_id: int, start: str, end: str,
                          min_conversions: float = 3.0) -> pd.DataFrame:
    """
    High-converting broad/phrase queries → candidates for exact/phrase promotion.
    Filters to queries matched by BROAD or PHRASE keywords with >= min_conversions.
    """
    rows = conn.execute("""
        SELECT
            sq.query_text,
            sq.normalized_text,
            ki.match_type                                       AS current_match_type,
            ki.campaign_name,
            ki.ad_group_name,
            sq.brand_category,
            sq.modifier_brand,
            ROUND(SUM(sqp.cost_micros) / 1e6, 2)               AS spend,
            SUM(sqp.clicks)                                     AS clicks,
            ROUND(SUM(sqp.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(sqp.primary_conv_value), 2)               AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ?
          AND ki.match_type IN ('BROAD', 'PHRASE')
          AND sqp.device = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sq.id, ki.campaign_name, ki.ad_group_name
        HAVING conversions >= ?
        ORDER BY conv_value DESC
        LIMIT 150
    """, (account_id, start, end, min_conversions)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["cvr"] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["promotion_action"] = df["current_match_type"].apply(
        lambda m: "Add as EXACT" if m == "BROAD" else "Consider EXACT"
    )

    col_order = [
        "query_text", "campaign_name", "ad_group_name", "current_match_type",
        "brand_category", "modifier_brand",
        "spend", "clicks", "conversions", "conv_value",
        "cpa", "cvr", "promotion_action",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_top_queries(conn, account_id: int, start: str, end: str) -> pd.DataFrame:
    """Top 50 queries by conversion value in period."""
    rows = conn.execute("""
        SELECT
            sq.query_text,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_geo,
            sq.vertical,
            sq.tag_method,
            COUNT(DISTINCT ki.campaign_name)                    AS campaigns,
            ROUND(SUM(sqp.primary_conv_value), 2)               AS conv_value,
            ROUND(SUM(sqp.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(sqp.cost_micros) / 1e6, 2)               AS spend,
            SUM(sqp.clicks)                                     AS clicks,
            SUM(sqp.impressions)                                AS impressions
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sq.id
        ORDER BY conv_value DESC
        LIMIT 50
    """, (account_id, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["cpa"] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df["ctr"] = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["cvr"] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)

    col_order = [
        "query_text", "brand_category", "modifier_brand", "modifier_geo",
        "vertical", "tag_method", "campaigns",
        "conv_value", "conversions", "spend", "clicks", "impressions",
        "cpa", "ctr", "cvr",
    ]
    return df[[c for c in col_order if c in df.columns]]


def q_cpc_inflation(conn, account_id: int, start: str, end: str) -> pd.DataFrame:
    """Top 25 keywords by average CPC with CTR and CVR."""
    rows = conn.execute("""
        SELECT
            ki.keyword_text,
            ki.match_type,
            ki.campaign_name,
            ki.ad_group_name,
            ROUND(SUM(kpd.cost_micros) / 1e6, 2)               AS spend,
            SUM(kpd.clicks)                                     AS clicks,
            SUM(kpd.impressions)                                AS impressions,
            ROUND(SUM(kpd.primary_conv_count), 1)               AS conversions,
            ROUND(SUM(kpd.primary_conv_value), 2)               AS conv_value,
            ki.quality_score
        FROM keyword_instances ki
        JOIN keyword_performance_daily kpd
            ON  kpd.instance_id = ki.id
            AND kpd.device = 'ALL'
            AND kpd.geo_state = 'ALL'
            AND kpd.report_date BETWEEN ? AND ?
        WHERE ki.account_id = ?
          AND ki.status != 'REMOVED'
          AND kpd.clicks > 0
        GROUP BY ki.id
        ORDER BY spend DESC
        LIMIT 25
    """, (start, end, account_id)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df["avg_cpc"] = df.apply(lambda r: safe_div(r.spend, r.clicks), axis=1)
    df["ctr"]     = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    df["cvr"]     = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df["cpa"]     = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)

    col_order = [
        "keyword_text", "match_type", "campaign_name", "ad_group_name",
        "quality_score", "spend", "clicks", "impressions",
        "avg_cpc", "ctr", "conversions", "conv_value", "cpa", "cvr",
    ]
    return df[[c for c in col_order if c in df.columns]]


# ── Excel writer ───────────────────────────────────────────────────────────────

def build_sheets(period: str, start: str, end: str) -> dict[str, pd.DataFrame]:
    conn       = get_db_connection()
    account_id = get_medicare_internal_id(conn)

    if account_id is None:
        conn.close()
        raise RuntimeError(
            f"Medicare account {MEDICARE_ACCOUNT_ID} not found in accounts table."
        )

    sheets = {
        "03_Match_Type_Mix":     q_match_type_mix(conn, account_id, start, end),
        "03_Brand_vs_NonBrand":  q_brand_vs_nonbrand(conn, account_id, start, end),
        "03_Waste_Candidates":   q_waste_candidates(conn, account_id, start, end),
        "03_Promote_Candidates": q_promote_candidates(conn, account_id, start, end),
        "03_Top_Queries":        q_top_queries(conn, account_id, start, end),
        "03_CPC_Inflation":      q_cpc_inflation(conn, account_id, start, end),
    }
    conn.close()
    return sheets


def write_workbook(sheets: dict[str, pd.DataFrame], period: str,
                   start: str, end: str, output_path: Path) -> None:
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)

    sheet_configs = {
        "03_Match_Type_Mix": {
            "title":    "Medicare — Match Type Mix",
            "subtitle": "Keyword count and performance by match type per campaign",
            "formats": {
                "spend":      XL.FMT_DOLLAR,
                "cpa":        XL.FMT_DOLLAR,
                "cpc":        XL.FMT_DOLLAR,
                "ctr":        XL.FMT_PCT_1,
                "cvr":        XL.FMT_PCT_1,
                "conv_value": XL.FMT_DOLLAR,
                "impressions":XL.FMT_INT,
                "clicks":     XL.FMT_INT,
            },
            "widths": {"campaign_name": 55},
        },
        "03_Brand_vs_NonBrand": {
            "title":    "Medicare — Brand vs Non-Brand Revenue",
            "subtitle": "Conversion value and spend split by brand category",
            "formats": {
                "spend":         XL.FMT_DOLLAR,
                "conv_value":    XL.FMT_DOLLAR,
                "cpa":           XL.FMT_DOLLAR,
                "rev_per_click": XL.FMT_DOLLAR,
                "clicks":        XL.FMT_INT,
            },
            "widths": {"campaign_name": 55, "brand_category": 25, "modifier_brand": 30},
        },
        "03_Waste_Candidates": {
            "title":    "Medicare — Search Query Waste (Negative Candidates)",
            "subtitle": f"Queries with spend ≥ $50 and 0 conversions — add as negatives",
            "formats": {
                "spend":      XL.FMT_DOLLAR,
                "cpc":        XL.FMT_DOLLAR,
                "conv_value": XL.FMT_DOLLAR,
                "clicks":     XL.FMT_INT,
                "impressions":XL.FMT_INT,
            },
            "widths": {
                "query_text": 50, "campaign_name": 45, "ad_group_name": 35,
                "negative_recommendation": 28,
            },
            "flag_col": "spend",
            "flag_rules": [(">=", 500, XL.FLAG_RED), (">=", 150, XL.FLAG_AMBER)],
        },
        "03_Promote_Candidates": {
            "title":    "Medicare — Promotion Candidates (Broad/Phrase → Exact)",
            "subtitle": "High-converting broad/phrase queries → add as exact keywords",
            "formats": {
                "spend":      XL.FMT_DOLLAR,
                "conv_value": XL.FMT_DOLLAR,
                "cpa":        XL.FMT_DOLLAR,
                "cvr":        XL.FMT_PCT_1,
                "clicks":     XL.FMT_INT,
            },
            "widths": {
                "query_text": 50, "campaign_name": 45, "ad_group_name": 35,
                "promotion_action": 22,
            },
        },
        "03_Top_Queries": {
            "title":    "Medicare — Top 50 Queries by Conversion Value",
            "subtitle": "Highest-value search queries in the period",
            "formats": {
                "conv_value": XL.FMT_DOLLAR,
                "spend":      XL.FMT_DOLLAR,
                "cpa":        XL.FMT_DOLLAR,
                "ctr":        XL.FMT_PCT_1,
                "cvr":        XL.FMT_PCT_1,
                "clicks":     XL.FMT_INT,
                "impressions":XL.FMT_INT,
            },
            "widths": {"query_text": 55, "brand_category": 25, "modifier_brand": 30},
        },
        "03_CPC_Inflation": {
            "title":    "Medicare — Top 25 Keywords by Spend",
            "subtitle": "CPC, CTR, and CVR for highest-spend keywords",
            "formats": {
                "spend":    XL.FMT_DOLLAR,
                "avg_cpc":  XL.FMT_DOLLAR,
                "cpa":      XL.FMT_DOLLAR,
                "conv_value":XL.FMT_DOLLAR,
                "ctr":      XL.FMT_PCT_1,
                "cvr":      XL.FMT_PCT_1,
                "clicks":   XL.FMT_INT,
                "impressions":XL.FMT_INT,
            },
            "widths": {
                "keyword_text": 45, "campaign_name": 45, "ad_group_name": 35,
            },
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
        description="Medicare Keyword Strategy & Search Query Mining"
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
        OUTPUT_DIR / f"medicare_03_keyword_strategy_{args.period}.xlsx"
    )

    print(f"Period : {PERIOD_LABELS.get(args.period)} ({start} → {end})")
    sheets = build_sheets(args.period, start, end)
    for name, df in sheets.items():
        print(f"  {name:<40} {len(df):>6} rows")

    write_workbook(sheets, args.period, start, end, output_path)


if __name__ == "__main__":
    main()
