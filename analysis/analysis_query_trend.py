"""
analysis_query_trend.py  [v2 — multi-account]
------------------------
Weekly ISO-week trend analysis of search query performance, segmented
by brand_category and vertical.

Pass --account to select which account to analyze. The script
automatically adapts its sheet set based on whether the account has
a Microsoft Advertising presence.

Accounts with Google + Microsoft (full 26-sheet workbook):
    home

Accounts with Google only (12-sheet workbook, all MS sheets dropped):
    medicare, auto, term_life, auto_quote

Usage:
    conda activate paid-search
    python analysis/analysis_query_trend.py --account home
    python analysis/analysis_query_trend.py --account medicare
    python analysis/analysis_query_trend.py --account auto

Output:
    analysis/output/query_trend_{account}.xlsx
"""

import sqlite3
import os
import argparse
import pandas as pd
from pathlib import Path
from datetime import date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule

# ---------------------------------------------------------------------------
# Account map
# ---------------------------------------------------------------------------

ACCOUNT_MAP = {
    "home": {
        "display_name":    "Home Insurance",
        "google_id":       "8886502147",
        "microsoft_id":    "F149SN63",
        "ms_start_date":   date(2026, 1, 13),   # Microsoft offline upload start
        "ytd_start_date":  date(2026, 1, 1),
    },
    "medicare": {
        "display_name":    "Medicare",
        "google_id":       "5310820901",
        "microsoft_id":    None,
        "ms_start_date":   None,
        "ytd_start_date":  date(2025, 4, 1),    # adjust to earliest Medicare data date
    },
    "auto": {
        "display_name":    "Auto Insurance (autoinsurance.org)",
        "google_id":       "3863660890",
        "microsoft_id":    "F149Y8EN",
        "ms_start_date":   None,                # update when MS offline uploads begin
        "ytd_start_date":  date(2026, 1, 1),
    },
    "term_life": {
        "display_name":    "Term Life",
        "google_id":       "1671155176",
        "microsoft_id":    "F149ZJ6F",
        "ms_start_date":   None,
        "ytd_start_date":  date(2026, 1, 1),
    },
    "auto_quote": {
        "display_name":    "Auto Insurance (auto.quote.com)",
        "google_id":       "1006729211",
        "microsoft_id":    None,
        "ms_start_date":   None,
        "ytd_start_date":  date(2026, 1, 1),
    },
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Weekly search query trend analysis for a single account."
    )
    parser.add_argument(
        "--account",
        required=True,
        choices=list(ACCOUNT_MAP.keys()),
        help="Account to analyze.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Runtime config (populated after arg parse)
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ANALYSIS_END_DATE = date.today() - timedelta(days=1)

BRAND_CATEGORY_ORDER = [
    "carrier_approved",
    "carrier_conditional",
    "carrier_restricted_requested",
    "carrier_restricted_partner",
    "aggregator_lead_buyer",
    "aggregator_competitor",
    None,
]

CATEGORY_LABELS = {
    "carrier_approved":              "Carrier Approved",
    "carrier_conditional":           "Carrier Conditional",
    "carrier_restricted_requested":  "Carrier Restricted (QW Request)",
    "carrier_restricted_partner":    "Carrier Restricted (Partner)",
    "aggregator_lead_buyer":         "Aggregator — Lead Buyer",
    "aggregator_competitor":         "Aggregator — Competitor",
    None:                            "Non-Brand / Generic",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Core fetches — Google only
# ---------------------------------------------------------------------------

def fetch_weekly_performance(conn, google_id: str) -> pd.DataFrame:
    """
    Daily search query performance for the Google account, aggregated
    to ISO week. One row per (date, vertical, brand_category, modifier_brand).
    """
    df = pd.read_sql_query("""
        SELECT
            sqp.report_date,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_bundle,
            sq.modifier_geo,
            sq.modifier_price,
            sq.modifier_coverage,
            sq.tag_method,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks,
            COALESCE(SUM(sqp.impressions), 0)         AS impressions,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue,
            COALESCE(SUM(sqp.secondary_conv_count),0) AS pixel_leads,
            COUNT(DISTINCT sq.id)                     AS query_count,
            COUNT(DISTINCT CASE WHEN sqp.clicks > 0
                           THEN sq.id END)            AS active_query_count
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform            = 'google'
          AND sqp.device            = 'ALL'
        GROUP BY
            sqp.report_date,
            sq.vertical, sq.brand_category, sq.modifier_brand,
            sq.modifier_bundle, sq.modifier_geo,
            sq.modifier_price, sq.modifier_coverage, sq.tag_method
    """, conn, params=(google_id,))

    df['report_date'] = pd.to_datetime(df['report_date'])
    df['iso_year']    = df['report_date'].dt.isocalendar().year
    df['iso_week']    = df['report_date'].dt.isocalendar().week
    df['week_label']  = (df['iso_year'].astype(str) + '-W' +
                         df['iso_week'].astype(str).str.zfill(2))
    return df


def fetch_top_queries(conn, google_id: str, n: int = 50) -> pd.DataFrame:
    """Top N queries by total offline revenue with weekly breakdown."""
    return pd.read_sql_query(f"""
        SELECT
            sq.query_text, sq.vertical, sq.brand_category,
            sq.modifier_brand, sq.tag_method, sq.tag_confidence,
            STRFTIME('%Y-W%W', sqp.report_date) AS week_raw,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
          AND sqp.query_id IN (
              SELECT sqp2.query_id
              FROM search_query_performance sqp2
              JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
              JOIN accounts a2           ON ki2.account_id   = a2.id
              WHERE a2.platform_account_id = ?
                AND a2.platform = 'google'
                AND sqp2.device = 'ALL'
              GROUP BY sqp2.query_id
              ORDER BY SUM(sqp2.primary_conv_value) DESC
              LIMIT {n}
          )
        GROUP BY sqp.query_id, sq.query_text, sq.vertical, sq.brand_category,
                 sq.modifier_brand, sq.tag_method, sq.tag_confidence, week_raw
        ORDER BY sq.query_text, week_raw
    """, conn, params=(google_id, google_id))


def fetch_query_word_count(conn, google_id: str) -> pd.DataFrame:
    """
    Weekly revenue-weighted average word count per brand_category.
    Uses actual query_text rather than modifier_brand approximation.
    """
    df = pd.read_sql_query("""
        SELECT
            STRFTIME('%Y-W%W', sqp.report_date) AS week_label,
            sq.brand_category,
            sq.query_text,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
        GROUP BY week_label, sq.id
    """, conn, params=(google_id,))

    df['word_count']    = df['query_text'].apply(
        lambda x: len(str(x).strip().split()) if pd.notna(x) else 0
    )
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        avg_word_count=('word_count', 'mean'),
        unique_queries=('query_text', 'nunique'),
        revenue=('revenue', 'sum'),
    ).round(2)

    wc_pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='avg_word_count', aggfunc='mean'
    ).reset_index()
    wc_pivot.columns.name = None

    uq_pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='unique_queries', aggfunc='sum', fill_value=0
    ).reset_index()
    uq_pivot.columns.name = None

    cat_rename = {
        'carrier_approved':             'Approved Carriers',
        'carrier_conditional':          'Conditional Carriers',
        'carrier_restricted_requested': 'Restricted-Requested',
        'carrier_restricted_partner':   'Restricted-Partner',
        'aggregator_lead_buyer':        'Lead Buyer Aggregators',
        'aggregator_competitor':        'Competitor Aggregators',
        'non_brand':                    'Non-Brand / Generic',
    }
    wc_pivot = wc_pivot.rename(columns=cat_rename)
    uq_pivot = uq_pivot.rename(columns=cat_rename)

    cat_cols    = [c for c in wc_pivot.columns if c != 'week_label']
    uq_cat_cols = [c for c in uq_pivot.columns if c != 'week_label']

    wc_pivot = wc_pivot.rename(columns={c: f'{c} — avg words'   for c in cat_cols})
    uq_pivot = uq_pivot.rename(columns={c: f'{c} — unique queries' for c in uq_cat_cols})

    result = wc_pivot.merge(uq_pivot, on='week_label', how='outer')
    result = result.rename(columns={'week_label': 'Week'})

    ordered = ['Week']
    for cat in cat_rename.values():
        for suffix in ['— avg words', '— unique queries']:
            col = f'{cat} {suffix}'
            if col in result.columns:
                ordered.append(col)

    result = result[[c for c in ordered if c in result.columns]]
    return result.sort_values('Week').round(2)


def fetch_keyword_query_efficiency(
    conn, google_id: str, revenue_start: str
) -> pd.DataFrame:
    """
    Keyword efficiency grouped by identity_id (normalized_text + match_type).
    Eliminates duplicate rows from the same keyword in multiple ad groups.
    """
    return pd.read_sql_query("""
        WITH query_perf AS (
            SELECT
                ki.identity_id,
                sq.query_text,
                sqp.match_type_served,
                SUM(sqp.clicks)             AS clicks,
                SUM(sqp.cost_micros) / 1e6  AS spend,
                SUM(sqp.primary_conv_count) AS conversions,
                SUM(sqp.primary_conv_value) AS revenue
            FROM search_query_performance sqp
            JOIN search_queries sq    ON sqp.query_id   = sq.id
            JOIN keyword_instances ki ON sqp.instance_id = ki.id
            JOIN accounts a           ON ki.account_id  = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'google'
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
            GROUP BY ki.identity_id, sq.id, sqp.match_type_served
        ),
        identity_agg AS (
            SELECT
                identity_id,
                SUM(spend)                                    AS total_spend,
                SUM(revenue)                                  AS total_revenue,
                SUM(conversions)                              AS total_conversions,
                SUM(clicks)                                   AS total_clicks,
                SUM(CASE WHEN conversions = 0
                         THEN spend ELSE 0 END)               AS wasted_spend,
                SUM(CASE WHEN conversions > 0
                         THEN spend ELSE 0 END)               AS productive_spend,
                COUNT(DISTINCT query_text)                    AS total_queries,
                COUNT(DISTINCT CASE WHEN conversions = 0
                               THEN query_text END)           AS zero_conv_queries,
                COUNT(DISTINCT CASE WHEN conversions > 0
                               THEN query_text END)           AS converting_queries,
                SUM(CASE WHEN match_type_served = 'EXACT'
                         THEN revenue ELSE 0 END)             AS exact_query_revenue,
                SUM(CASE WHEN match_type_served = 'BROAD'
                         THEN revenue ELSE 0 END)             AS broad_query_revenue,
                SUM(CASE WHEN match_type_served = 'PHRASE'
                         THEN revenue ELSE 0 END)             AS phrase_query_revenue
            FROM query_perf
            GROUP BY identity_id
        )
        SELECT
            ki_id.normalized_text   AS keyword_text,
            ki_id.match_type,
            ki_id.vertical,
            ki_id.brand_category,
            ki_id.modifier_brand,
            ki_id.modifier_geo,
            ki_id.modifier_bundle,
            ki_id.modifier_near_me,
            ki_id.modifier_home_purchase,
            ia.total_spend,
            ia.total_revenue,
            ia.total_conversions,
            ia.total_clicks,
            ia.wasted_spend,
            ia.productive_spend,
            ia.total_queries,
            ia.zero_conv_queries,
            ia.converting_queries,
            ia.exact_query_revenue,
            ia.broad_query_revenue,
            ia.phrase_query_revenue,
            COUNT(DISTINCT ki.id)   AS instance_count
        FROM identity_agg ia
        JOIN keyword_identities ki_id ON ki_id.id = ia.identity_id
        JOIN keyword_instances ki     ON ki.identity_id = ia.identity_id
            AND ki.account_id IN (
                SELECT id FROM accounts
                WHERE platform_account_id = ? AND platform = 'google'
            )
        WHERE ia.total_spend > 0
        GROUP BY ia.identity_id
        ORDER BY ia.wasted_spend DESC
    """, conn, params=(google_id, revenue_start, google_id))


def fetch_top_queries_ytd(
    conn, google_id: str, ytd_start: str, n: int = 50
) -> pd.DataFrame:
    """Top N queries by offline revenue YTD with monthly breakdown."""
    return pd.read_sql_query(f"""
        SELECT
            sq.query_text, sq.vertical, sq.brand_category,
            sq.modifier_brand, sq.tag_method, sq.tag_confidence,
            STRFTIME('%Y-W%W', sqp.report_date) AS week_raw,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND sqp.query_id IN (
              SELECT sqp2.query_id
              FROM search_query_performance sqp2
              JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
              JOIN accounts a2           ON ki2.account_id   = a2.id
              WHERE a2.platform_account_id = ?
                AND a2.platform = 'google'
                AND sqp2.device = 'ALL'
                AND sqp2.report_date >= ?
              GROUP BY sqp2.query_id
              ORDER BY SUM(sqp2.primary_conv_value) DESC
              LIMIT {n}
          )
        GROUP BY sqp.query_id, sq.query_text, sq.vertical, sq.brand_category,
                 sq.modifier_brand, sq.tag_method, sq.tag_confidence, week_raw
        ORDER BY sq.query_text, week_raw
    """, conn, params=(google_id, ytd_start, google_id, ytd_start))


def fetch_brand_performance(
    conn, google_id: str, ytd_start: str
) -> pd.DataFrame:
    """Brand-level aggregated performance YTD, Google only."""
    return pd.read_sql_query("""
        SELECT
            COALESCE(sq.modifier_brand, '(non-brand)') AS brand,
            sq.brand_category,
            sq.vertical,
            COUNT(DISTINCT sq.id)               AS unique_queries,
            SUM(sqp.clicks)                     AS clicks,
            SUM(sqp.cost_micros) / 1e6          AS spend,
            SUM(sqp.primary_conv_count)         AS conversions,
            SUM(sqp.primary_conv_value)         AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq    ON sqp.query_id   = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE sqp.device = 'ALL'
          AND a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.report_date >= ?
        GROUP BY sq.modifier_brand, sq.brand_category, sq.vertical
        ORDER BY SUM(sqp.primary_conv_value) DESC
    """, conn, params=(google_id, ytd_start))


def fetch_query_coverage_gaps(
    conn, google_id: str, ytd_start: str
) -> pd.DataFrame:
    """
    High-revenue search queries matched via BROAD or PHRASE with no exact
    match keyword covering the same query text on Google.
    Sorted by revenue — these are keyword expansion candidates.
    """
    return pd.read_sql_query("""
        WITH converting_queries AS (
            SELECT
                sq.id              AS query_id,
                sq.query_text,
                sq.normalized_text,
                sq.brand_category,
                sq.modifier_brand,
                sq.vertical,
                sq.modifier_geo,
                sq.modifier_near_me,
                MIN(sqp.match_type_served)          AS best_match_type,
                SUM(sqp.primary_conv_value)         AS revenue,
                SUM(sqp.cost_micros) / 1e6          AS spend,
                SUM(sqp.clicks)                     AS clicks,
                SUM(sqp.primary_conv_count)         AS conversions
            FROM search_query_performance sqp
            JOIN search_queries sq    ON sqp.query_id   = sq.id
            JOIN keyword_instances ki ON sqp.instance_id = ki.id
            JOIN accounts a           ON ki.account_id  = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'google'
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
              AND sqp.match_type_served != 'EXACT'
            GROUP BY sq.id
            HAVING revenue > 0
        ),
        exact_coverage AS (
            SELECT DISTINCT ki_id.normalized_text
            FROM keyword_identities ki_id
            JOIN keyword_instances ki ON ki.identity_id = ki_id.id
            JOIN accounts a           ON ki.account_id  = a.id
            WHERE ki_id.match_type = 'EXACT'
              AND a.platform_account_id = ?
              AND a.platform = 'google'
        )
        SELECT
            cq.query_text,
            cq.brand_category,
            cq.modifier_brand,
            cq.vertical,
            cq.modifier_geo,
            cq.modifier_near_me,
            cq.best_match_type  AS served_match_type,
            cq.revenue,
            cq.spend,
            cq.clicks,
            cq.conversions,
            CASE WHEN ec.normalized_text IS NOT NULL
                 THEN 'Yes' ELSE 'No' END AS exact_exists
        FROM converting_queries cq
        LEFT JOIN exact_coverage ec ON ec.normalized_text = cq.normalized_text
        WHERE ec.normalized_text IS NULL
        ORDER BY cq.revenue DESC
        LIMIT 200
    """, conn, params=(google_id, ytd_start, google_id))


def fetch_brand_top_keywords(
    conn, google_id: str, ytd_start: str
) -> pd.DataFrame:
    """Top 5 keywords per brand by offline revenue YTD, Google only."""
    kw_df = pd.read_sql_query("""
        SELECT
            ki.identity_id,
            ki_id.normalized_text   AS keyword_text,
            ki_id.match_type,
            ki_id.brand_category,
            ki_id.modifier_brand,
            ki_id.vertical,
            SUM(sqp.clicks)                 AS kw_clicks,
            SUM(sqp.cost_micros) / 1e6      AS kw_spend,
            SUM(sqp.primary_conv_count)     AS kw_conversions,
            SUM(sqp.primary_conv_value)     AS kw_revenue
        FROM search_query_performance sqp
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        JOIN accounts a               ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id
        HAVING SUM(sqp.primary_conv_value) > 0
        ORDER BY kw_revenue DESC
    """, conn, params=(google_id, ytd_start))

    if kw_df.empty:
        return pd.DataFrame()

    top_query_df = pd.read_sql_query("""
        SELECT
            ki.identity_id,
            sq.query_text,
            SUM(sqp.primary_conv_value) AS q_revenue
        FROM search_query_performance sqp
        JOIN search_queries sq        ON sqp.query_id   = sq.id
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN accounts a               ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id, sq.id
        ORDER BY ki.identity_id, q_revenue DESC
    """, conn, params=(google_id, ytd_start))

    top_query = (
        top_query_df.sort_values('q_revenue', ascending=False)
                    .drop_duplicates(subset='identity_id', keep='first')
                    [['identity_id', 'query_text', 'q_revenue']]
    )

    kw_df['brand_key']   = kw_df['modifier_brand'].fillna('(non-brand)')
    kw_df                = kw_df.sort_values('kw_revenue', ascending=False)
    kw_df['brand_rank']  = kw_df.groupby('brand_key').cumcount() + 1
    kw_df                = kw_df[kw_df['brand_rank'] <= 5].copy()

    result = kw_df.merge(top_query, on='identity_id', how='left')
    result = result.rename(columns={
        'brand_key':  'brand',
        'brand_rank': 'rank_within_brand',
        'query_text': 'top_search_query',
        'q_revenue':  'top_query_revenue',
    })
    return result.sort_values(['brand', 'rank_within_brand']).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------

def build_summary_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    summary = weekly_df.groupby('week_label', as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
        pixel_leads=('pixel_leads', 'sum'),
        query_count=('query_count', 'sum'),
        active_query_count=('active_query_count', 'sum'),
    ).sort_values('week_label')

    summary['CPL ($)'] = pd.to_numeric(
        summary['spend'] / summary['conversions'], errors='coerce').round(2)
    summary['ROAS']    = pd.to_numeric(
        summary['revenue'] / summary['spend'], errors='coerce').round(2)
    summary['CTR (%)'] = pd.to_numeric(
        summary['clicks'] / summary['query_count'] * 100, errors='coerce').round(2)

    summary.columns = [
        'Week', 'Offline Revenue ($)', 'Clicks', 'Spend ($)',
        'Offline Conversions', 'Pixel Leads', 'Query Impressions',
        'Active Queries (w/ Clicks)', 'CPL ($)', 'ROAS', 'CTR (%)',
    ]
    return summary.round(2)


def build_brand_category_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )
    agg['CPL']  = pd.to_numeric(agg['spend'] / agg['conversions'], errors='coerce').round(2)
    agg['ROAS'] = pd.to_numeric(agg['revenue'] / agg['spend'], errors='coerce').round(2)

    pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    pivot.columns.name = None

    cat_cols = [c for c in pivot.columns if c != 'week_label']
    pivot['Total Revenue ($)'] = pivot[cat_cols].sum(axis=1)

    rename = {
        'carrier_approved':             'Approved Carriers ($)',
        'carrier_conditional':          'Conditional Carriers ($)',
        'carrier_restricted_requested': 'Restricted-Requested ($)',
        'carrier_restricted_partner':   'Restricted-Partner ($)',
        'aggregator_lead_buyer':        'Lead Buyer Aggregators ($)',
        'aggregator_competitor':        'Competitor Aggregators ($)',
        'non_brand':                    'Non-Brand / Generic ($)',
        'week_label':                   'Week',
    }
    return pivot.rename(columns=rename).sort_values('Week').round(2)


def build_restricted_trend_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    restricted_cats = {
        'carrier_restricted_requested',
        'carrier_restricted_partner',
        'carrier_conditional',
    }
    df = weekly_df[weekly_df['brand_category'].isin(restricted_cats)].copy()

    if df.empty:
        return pd.DataFrame(columns=[
            'Week', 'Brand Category', 'Brand', 'Revenue ($)',
            'Clicks', 'Spend ($)', 'Conversions', 'CPL ($)',
        ])

    agg = df.groupby(
        ['week_label', 'brand_category', 'modifier_brand'], as_index=False
    ).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )
    agg['CPL ($)'] = pd.to_numeric(
        agg['spend'] / agg['conversions'], errors='coerce').round(2)

    return agg.rename(columns={
        'week_label':     'Week',
        'brand_category': 'Brand Category',
        'modifier_brand': 'Brand',
        'revenue':        'Revenue ($)',
        'clicks':         'Clicks',
        'spend':          'Spend ($)',
        'conversions':    'Offline Conversions',
    }).sort_values(['Week', 'Brand Category', 'Revenue ($)'],
                   ascending=[False, True, False]).reset_index(drop=True).round(2)


def build_top_queries_sheet(top_df: pd.DataFrame) -> pd.DataFrame:
    if top_df.empty:
        return pd.DataFrame()

    df = top_df.copy()

    def week_raw_to_date(w):
        if not isinstance(w, str) or '-W' not in w:
            return None
        try:
            year, wnum = w.split('-W')
            return pd.Timestamp.fromisocalendar(int(year), int(wnum), 1)
        except Exception:
            return None

    df['report_date'] = df['week_raw'].apply(week_raw_to_date)
    df['month']       = df['report_date'].dt.to_period('M').astype(str)

    monthly = df.groupby(
        ['query_text', 'vertical', 'brand_category',
         'modifier_brand', 'tag_method', 'month'], as_index=False
    ).agg(revenue=('revenue', 'sum'), clicks=('clicks', 'sum'),
          spend=('spend', 'sum'), conversions=('conversions', 'sum'))

    totals = monthly.groupby('query_text', as_index=False).agg(
        total_revenue=('revenue', 'sum'),
        total_clicks=('clicks', 'sum'),
        total_spend=('spend', 'sum'),
        total_conversions=('conversions', 'sum'),
    )
    totals['CPL ($)'] = pd.to_numeric(
        totals['total_spend'] / totals['total_conversions'], errors='coerce').round(2)
    totals['ROAS']    = pd.to_numeric(
        totals['total_revenue'] / totals['total_spend'], errors='coerce').round(2)

    rev_pivot = monthly.pivot_table(
        index='query_text', columns='month',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    rev_pivot.columns.name = None

    meta = df.groupby('query_text', as_index=False).agg(
        vertical=('vertical', 'first'),
        brand_category=('brand_category', 'first'),
        modifier_brand=('modifier_brand', 'first'),
    )

    result = totals.merge(meta, on='query_text').merge(rev_pivot, on='query_text')
    result = result.sort_values('total_revenue', ascending=False).reset_index(drop=True)

    meta_cols  = ['query_text', 'vertical', 'brand_category', 'modifier_brand',
                  'total_revenue', 'total_spend', 'total_conversions',
                  'total_clicks', 'CPL ($)', 'ROAS']
    month_cols = sorted([c for c in result.columns if c not in meta_cols])
    result = result[meta_cols + month_cols].rename(columns={
        'query_text':        'Query',
        'vertical':          'Vertical',
        'brand_category':    'Brand Category',
        'modifier_brand':    'Brand',
        'total_revenue':     'Total Revenue ($)',
        'total_spend':       'Total Spend ($)',
        'total_conversions': 'Total Conversions',
        'total_clicks':      'Total Clicks',
    })
    return result.round(2)


def build_vertical_trend_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = weekly_df.copy()
    df['vertical'] = df['vertical'].fillna('unclassified')

    agg = df.groupby(['week_label', 'vertical'], as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )
    pivot = agg.pivot_table(
        index='week_label', columns='vertical',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    pivot.columns.name = None
    vert_cols = [c for c in pivot.columns if c != 'week_label']
    pivot['Total ($)'] = pivot[vert_cols].sum(axis=1)
    pivot = pivot.rename(columns={'week_label': 'Week'})
    pivot = pivot.rename(columns={c: f'{c} ($)' for c in vert_cols})
    return pivot.sort_values('Week').round(2)


def build_keyword_efficiency_sheet(kw_df: pd.DataFrame) -> pd.DataFrame:
    if kw_df.empty:
        return pd.DataFrame()

    df = kw_df.copy()
    df['CPL ($)']             = pd.to_numeric(
        df['total_spend'] / df['total_conversions'], errors='coerce').round(2)
    df['ROAS']                = pd.to_numeric(
        df['total_revenue'] / df['total_spend'], errors='coerce').round(2)
    df['Waste Rate (%)']      = pd.to_numeric(
        df['wasted_spend'] / df['total_spend'] * 100, errors='coerce').round(1)
    df['Query Conv Rate (%)'] = pd.to_numeric(
        df['converting_queries'] / df['total_queries'] * 100, errors='coerce').round(1)
    df['Exact Query Rev (%)'] = pd.to_numeric(
        df['exact_query_revenue'] / df['total_revenue'] * 100, errors='coerce').round(1)

    def match_signal(row):
        mt    = str(row.get('match_type', '')).upper()
        e_rev = float(row.get('exact_query_revenue') or 0)
        t_rev = float(row.get('total_revenue') or 0)
        waste = float(row.get('Waste Rate (%)') or 0)
        if t_rev == 0:
            return 'No revenue — review'
        e_pct = e_rev / t_rev * 100 if t_rev > 0 else 0
        if mt == 'BROAD' and e_pct > 60:
            return 'Consider EXACT — most revenue from exact queries'
        if mt == 'BROAD' and waste > 70:
            return 'Consider PHRASE — high waste, cap with phrase'
        if mt == 'BROAD' and waste > 50:
            return 'Monitor — moderate waste rate'
        if mt == 'EXACT' and float(row.get('broad_query_revenue') or 0) / t_rev > 0.3:
            return 'Add BROAD variant — broad queries generating revenue'
        if mt == 'PHRASE' and e_pct > 80:
            return 'Consider EXACT — mostly exact matches'
        return 'Efficient — match type appropriate'

    df['Match Type Signal'] = df.apply(match_signal, axis=1)

    result = df.rename(columns={
        'keyword_text':         'Keyword',
        'match_type':           'Match Type',
        'vertical':             'Vertical',
        'brand_category':       'Brand Category',
        'modifier_brand':       'Brand',
        'modifier_geo':         'Geo Modifier',
        'modifier_bundle':      'Bundle',
        'modifier_near_me':     'Near Me',
        'modifier_home_purchase': 'Home Purchase Intent',
        'total_spend':          'Total Spend ($)',
        'total_revenue':        'Total Revenue ($)',
        'total_conversions':    'Total Offline Convs',
        'total_clicks':         'Total Clicks',
        'wasted_spend':         'Wasted Spend ($)',
        'productive_spend':     'Productive Spend ($)',
        'total_queries':        'Total Queries Matched',
        'zero_conv_queries':    'Zero-Conv Queries',
        'converting_queries':   'Converting Queries',
        'exact_query_revenue':  'Exact Query Revenue ($)',
        'broad_query_revenue':  'Broad Query Revenue ($)',
        'phrase_query_revenue': 'Phrase Query Revenue ($)',
        'instance_count':       'Ad Group Instances',
    })

    col_order = [
        'Keyword', 'Match Type', 'Vertical', 'Brand Category', 'Brand',
        'Geo Modifier', 'Bundle', 'Near Me', 'Home Purchase Intent',
        'Ad Group Instances',
        'Total Revenue ($)', 'Total Spend ($)', 'Total Offline Convs',
        'Total Clicks', 'CPL ($)', 'ROAS',
        'Wasted Spend ($)', 'Productive Spend ($)', 'Waste Rate (%)',
        'Exact Query Revenue ($)', 'Exact Query Rev (%)',
        'Broad Query Revenue ($)', 'Phrase Query Revenue ($)',
        'Total Queries Matched', 'Converting Queries', 'Zero-Conv Queries',
        'Query Conv Rate (%)', 'Match Type Signal',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Wasted Spend ($)', ascending=False).round(2)


def build_brand_sheet(brand_df: pd.DataFrame) -> pd.DataFrame:
    """Brand-level YTD performance summary for Google only."""
    df = brand_df.copy()
    df['CPL ($)']    = pd.to_numeric(
        df['spend'] / df['conversions'], errors='coerce').round(2)
    df['ROAS']       = pd.to_numeric(
        df['revenue'] / df['spend'], errors='coerce').round(2)
    df['Rev/Query']  = pd.to_numeric(
        df['revenue'] / df['unique_queries'], errors='coerce').round(2)

    result = df.rename(columns={
        'brand':          'Brand',
        'brand_category': 'Brand Category',
        'vertical':       'Vertical',
        'unique_queries': 'Unique Queries',
        'clicks':         'Clicks',
        'spend':          'Spend ($)',
        'conversions':    'Offline Conversions',
        'revenue':        'Revenue ($)',
    })
    col_order = [
        'Brand', 'Brand Category', 'Vertical',
        'Revenue ($)', 'Spend ($)', 'Offline Conversions',
        'Clicks', 'Unique Queries', 'CPL ($)', 'ROAS', 'Rev/Query',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Revenue ($)', ascending=False).round(2)


def build_top_queries_ytd_sheet(top_df: pd.DataFrame) -> pd.DataFrame:
    """Top 50 queries by offline revenue YTD with monthly breakdown."""
    return build_top_queries_sheet(top_df)  # same logic, reuse


def build_coverage_gaps_sheet(gaps_df: pd.DataFrame) -> pd.DataFrame:
    if gaps_df.empty:
        return pd.DataFrame()

    df = gaps_df.copy()
    df['CPL ($)'] = pd.to_numeric(
        df['spend'] / df['conversions'], errors='coerce').round(2)
    df['ROAS']    = pd.to_numeric(
        df['revenue'] / df['spend'], errors='coerce').round(2)

    def action(row):
        rev = float(row.get('revenue', 0) or 0)
        cpl = float(row.get('CPL ($)') or 0)
        if rev >= 500:
            return 'High priority — add as EXACT keyword'
        if rev >= 100 and cpl > 0 and cpl < 45:
            return 'Add as EXACT — strong CPL, no exact coverage'
        if rev >= 50:
            return 'Consider EXACT — converting, no exact coverage'
        return 'Monitor — low revenue, not urgent'

    df['Recommended Action'] = df.apply(action, axis=1)

    result = df.rename(columns={
        'query_text':       'Search Query',
        'brand_category':   'Brand Category',
        'modifier_brand':   'Brand',
        'vertical':         'Vertical',
        'modifier_geo':     'Geo Modifier',
        'modifier_near_me': 'Near Me',
        'served_match_type':'Served Match Type',
        'revenue':          'Revenue ($)',
        'spend':            'Spend ($)',
        'clicks':           'Clicks',
        'conversions':      'Offline Convs',
        'exact_exists':     'Exact Match Exists',
    })
    col_order = [
        'Search Query', 'Brand Category', 'Brand', 'Vertical',
        'Geo Modifier', 'Near Me',
        'Revenue ($)', 'Spend ($)', 'Offline Convs', 'Clicks',
        'CPL ($)', 'ROAS', 'Served Match Type',
        'Exact Match Exists', 'Recommended Action',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Revenue ($)', ascending=False).round(2)


def build_brand_keyword_sheet(bkw_df: pd.DataFrame) -> pd.DataFrame:
    if bkw_df.empty:
        return pd.DataFrame()

    df = bkw_df.copy()
    df['CPL ($)']         = pd.to_numeric(
        df['kw_spend'] / df['kw_conversions'], errors='coerce').round(2)
    df['ROAS']            = pd.to_numeric(
        df['kw_revenue'] / df['kw_spend'], errors='coerce').round(2)
    df['Top Query Rev %'] = pd.to_numeric(
        df['top_query_revenue'] / df['kw_revenue'] * 100, errors='coerce').round(1)

    result = df.rename(columns={
        'brand':             'Brand',
        'brand_category':    'Brand Category',
        'vertical':          'Vertical',
        'rank_within_brand': 'Rank',
        'keyword_text':      'Keyword',
        'match_type':        'Match Type',
        'kw_revenue':        'Keyword Revenue ($)',
        'kw_spend':          'Keyword Spend ($)',
        'kw_conversions':    'Keyword Offline Convs',
        'kw_clicks':         'Keyword Clicks',
        'top_search_query':  'Top Search Query',
        'top_query_revenue': 'Top Query Revenue ($)',
    })
    col_order = [
        'Brand', 'Brand Category', 'Vertical', 'Rank',
        'Keyword', 'Match Type',
        'Keyword Revenue ($)', 'Keyword Spend ($)',
        'Keyword Offline Convs', 'Keyword Clicks',
        'CPL ($)', 'ROAS',
        'Top Search Query', 'Top Query Revenue ($)', 'Top Query Rev %',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    brand_rev   = result.groupby('Brand')['Keyword Revenue ($)'].sum()
    result['_sort'] = result['Brand'].map(brand_rev)
    result = result.sort_values(
        ['_sort', 'Brand', 'Rank'], ascending=[False, True, True]
    ).drop(columns=['_sort']).reset_index(drop=True)
    return result.round(2)


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path):
    wb = load_workbook(path)

    header_fills = {
        'Summary':              '1F4E79',
        'By brand category':    '375623',
        'Restricted trend':     '7B0000',
        'Top queries':          '4A235A',
        'Vertical trend':       '1F4E79',
        'Query word count':     '375623',
        'Top queries YTD':      '4A235A',
        'Keyword efficiency':   '375623',
        'Brand performance':    '1F4E79',
        'Brand keywords':       '375623',
        'Coverage gaps':        '833C00',
    }
    white_font = Font(color='FFFFFF', bold=True, size=11)

    for sheet_name in wb.sheetnames:
        ws       = wb[sheet_name]
        fill_hex = header_fills.get(sheet_name, '1F4E79')
        hdr_fill = PatternFill('solid', fgColor=fill_hex)

        for cell in ws[1]:
            cell.font      = white_font
            cell.fill      = hdr_fill
            cell.alignment = Alignment(
                horizontal='center', vertical='center', wrap_text=True)
        ws.row_dimensions[1].height = 32

        for col in ws.columns:
            max_len    = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    val_len = len(str(cell.value)) if cell.value is not None else 0
                    max_len = max(max_len, val_len)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 4, 36)

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if not isinstance(cell.value, (int, float)):
                    continue
                hdr = str(ws.cell(1, cell.column).value or '').lower()
                if '$' in hdr or 'revenue' in hdr or 'spend' in hdr or \
                   'cpl' in hdr or 'roas' in hdr:
                    cell.number_format = '$#,##0.00'
                elif '%' in hdr or 'ctr' in hdr:
                    cell.number_format = '0.00'
                elif 'conv' in hdr or 'click' in hdr or 'impression' in hdr:
                    cell.number_format = '#,##0'

        if sheet_name in ('Summary', 'By brand category', 'Vertical trend'):
            max_row = ws.max_row
            for col in ws.columns:
                hdr = str(ws.cell(1, col[0].column).value or '').lower()
                if 'revenue' in hdr or '$' in hdr:
                    col_letter = get_column_letter(col[0].column)
                    ws.conditional_formatting.add(
                        f'{col_letter}2:{col_letter}{max_row}',
                        ColorScaleRule(
                            start_type='min', start_color='FFFFFF',
                            end_type='max',   end_color='63BE7B',
                        )
                    )
        ws.freeze_panes = 'B2'

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args     = parse_args()
    cfg      = ACCOUNT_MAP[args.account]
    google_id = cfg['google_id']
    ms_id     = cfg['microsoft_id']
    has_ms    = ms_id is not None and cfg['ms_start_date'] is not None

    ytd_start      = cfg['ytd_start_date'].isoformat()
    revenue_start  = (cfg['ms_start_date'] or cfg['ytd_start_date']).isoformat()
    output_file    = OUTPUT_DIR / f"query_trend_{args.account}.xlsx"

    print('=' * 65)
    print(f'Search Query Trend Analysis — {cfg["display_name"]}')
    print(f'Google account : {google_id}')
    print(f'Microsoft      : {ms_id or "none (Google-only)"}')
    print(f'YTD start      : {ytd_start}')
    print(f'Revenue start  : {revenue_start}')
    print(f'End date       : {ANALYSIS_END_DATE}')
    print('=' * 65)

    conn = get_conn()

    print('\nFetching weekly performance...')
    weekly_df = fetch_weekly_performance(conn, google_id)
    print(f'  {len(weekly_df):,} daily rows | '
          f'{weekly_df["week_label"].nunique()} ISO weeks | '
          f'{weekly_df["report_date"].min().date()} to '
          f'{weekly_df["report_date"].max().date()}')

    print('Fetching top 50 queries (all-time)...')
    top_df = fetch_top_queries(conn, google_id, n=50)
    print(f'  {len(top_df):,} query-week rows')

    print('Fetching query word count...')
    word_count_df = fetch_query_word_count(conn, google_id)
    print(f'  {len(word_count_df):,} week rows')

    print(f'Fetching top 50 queries YTD (from {ytd_start})...')
    top_ytd_df = fetch_top_queries_ytd(conn, google_id, ytd_start, n=50)
    print(f'  {len(top_ytd_df):,} query-week rows')

    print(f'Fetching keyword efficiency (from {revenue_start})...')
    kw_eff_df = fetch_keyword_query_efficiency(conn, google_id, revenue_start)
    print(f'  {len(kw_eff_df):,} keywords with spend')

    print(f'Fetching brand performance YTD (from {ytd_start})...')
    brand_df = fetch_brand_performance(conn, google_id, ytd_start)
    print(f'  {len(brand_df):,} brand rows')

    print(f'Fetching brand top keywords YTD (from {ytd_start})...')
    bkw_df = fetch_brand_top_keywords(conn, google_id, ytd_start)
    print(f'  {len(bkw_df):,} brand-keyword rows')

    print(f'Fetching coverage gaps (from {ytd_start})...')
    gaps_df = fetch_query_coverage_gaps(conn, google_id, ytd_start)
    print(f'  {len(gaps_df):,} gap rows')

    conn.close()

    # Build sheets
    print('\nBuilding sheets...')
    summary_df    = build_summary_sheet(weekly_df)
    category_df   = build_brand_category_sheet(weekly_df)
    restricted_df = build_restricted_trend_sheet(weekly_df)
    top_df_sheet  = build_top_queries_sheet(top_df)
    vertical_df   = build_vertical_trend_sheet(weekly_df)
    top_ytd_sheet = build_top_queries_ytd_sheet(top_ytd_df)
    kw_eff_sheet  = build_keyword_efficiency_sheet(kw_eff_df)
    brand_sheet   = build_brand_sheet(brand_df)
    bkw_sheet     = build_brand_keyword_sheet(bkw_df)
    gaps_sheet    = build_coverage_gaps_sheet(gaps_df)

    # Terminal preview
    print('\n--- Weekly summary (last 8 weeks) ---')
    print(summary_df.tail(8)[
        ['Week', 'Offline Revenue ($)', 'Clicks', 'Spend ($)', 'ROAS', 'CPL ($)']
    ].to_string(index=False))

    print('\n--- Brand performance (top 10 by revenue) ---')
    print(brand_sheet.head(10)[
        ['Brand', 'Brand Category', 'Vertical',
         'Revenue ($)', 'Spend ($)', 'CPL ($)', 'ROAS']
    ].to_string(index=False))

    print(f'\nWriting Excel output...')
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        summary_df.to_excel(writer,    sheet_name='Summary',            index=False)
        category_df.to_excel(writer,   sheet_name='By brand category',  index=False)
        restricted_df.to_excel(writer, sheet_name='Restricted trend',   index=False)
        top_df_sheet.to_excel(writer,  sheet_name='Top queries',        index=False)
        vertical_df.to_excel(writer,   sheet_name='Vertical trend',     index=False)
        word_count_df.to_excel(writer, sheet_name='Query word count',   index=False)
        top_ytd_sheet.to_excel(writer, sheet_name='Top queries YTD',    index=False)
        kw_eff_sheet.to_excel(writer,  sheet_name='Keyword efficiency',  index=False)
        brand_sheet.to_excel(writer,   sheet_name='Brand performance',  index=False)
        bkw_sheet.to_excel(writer,     sheet_name='Brand keywords',     index=False)
        gaps_sheet.to_excel(writer,    sheet_name='Coverage gaps',      index=False)

    print('Applying formatting...')
    format_workbook(output_file)

    size_kb = os.path.getsize(output_file) / 1024
    print(f'\nOutput saved : {output_file}')
    print(f'File size    : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
