"""
analysis_query_trend.py
------------------------
Weekly ISO-week trend analysis of search query performance, segmented
by brand_category and vertical. Designed to surface where the performance
drawdown originated post-March 2025.

Produces an Excel workbook with five sheets:

    1. Summary           -- weekly totals by platform, key metrics
    2. By brand category -- weekly revenue/clicks/CPL per brand_category
    3. Restricted trend  -- restricted_requested + restricted_partner
                           week by week (Liberty Mutual, USAA, Progressive)
    4. Top queries       -- top 50 queries by total revenue, weekly sparkline data
    5. Vertical trend    -- weekly performance split by vertical

All metrics use device='ALL' and the primary_conv_value / primary_conv_count
columns for revenue and conversions (offline upload values).

Revenue window note:
    Google: full 12-month backfill available (Mar 2025 - Mar 2026)
    Microsoft: offline revenue available from 2026-01-13 onward
    Cross-platform comparisons use 2026-01-13 as the start date.
    Single-platform Google trend uses the full 12-month window.

Usage:
    conda activate paid-search
    python analysis/analysis_query_trend.py

Output:
    analysis/output/query_trend_home_insurance.xlsx
"""

import sqlite3
import os
import pandas as pd
from pathlib import Path
from datetime import date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule, DataBarRule

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR  = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "query_trend_home_insurance.xlsx"

GOOGLE_ACCOUNT_ID    = "8886502147"
MS_ACCOUNT_ID        = "F149SN63"
REVENUE_START_DATE   = date(2026, 1, 13)   # Microsoft offline upload start
YTD_START_DATE       = date(2026, 1, 1)    # YTD window start
ANALYSIS_END_DATE    = date.today() - timedelta(days=1)

# Brand categories -- ordered for display
BRAND_CATEGORY_ORDER = [
    "carrier_approved",
    "carrier_conditional",
    "carrier_restricted_requested",
    "carrier_restricted_partner",
    "aggregator_lead_buyer",
    "aggregator_competitor",
    None,   # non-brand queries (generic, geo, price intent)
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
# Core query: weekly performance from search_query_performance
# ---------------------------------------------------------------------------

def fetch_weekly_performance(conn) -> pd.DataFrame:
    """
    Returns daily search query performance joined to query tags,
    then aggregated to ISO week. One row per (iso_week, brand_category,
    vertical, modifier_brand).

    Uses Google only for the full trend window since Microsoft only has
    data from Jan 2026.
    """
    df = pd.read_sql_query("""
        SELECT
            sqp.report_date,
            STRFTIME('%Y-W%W', sqp.report_date)     AS week_raw,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_bundle,
            sq.modifier_geo,
            sq.modifier_price,
            sq.modifier_coverage,
            sq.tag_method,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks,
            COALESCE(SUM(sqp.impressions), 0)        AS impressions,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue,
            COALESCE(SUM(sqp.secondary_conv_count),0) AS pixel_leads,
            COUNT(DISTINCT sq.id)                    AS query_count,
            COUNT(DISTINCT CASE WHEN sqp.clicks > 0
                           THEN sq.id END)          AS active_query_count
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND a.platform            = 'google'
          AND sqp.device            = 'ALL'
        GROUP BY
            sqp.report_date,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_bundle,
            sq.modifier_geo,
            sq.modifier_price,
            sq.modifier_coverage,
            sq.tag_method
    """, conn, params=(GOOGLE_ACCOUNT_ID,))

    # Convert to proper ISO week (YYYY-Www format)
    df['report_date'] = pd.to_datetime(df['report_date'])
    df['iso_year']    = df['report_date'].dt.isocalendar().year
    df['iso_week']    = df['report_date'].dt.isocalendar().week
    df['week_label']  = df['iso_year'].astype(str) + '-W' + \
                        df['iso_week'].astype(str).str.zfill(2)

    return df


def fetch_top_queries(conn, n: int = 50) -> pd.DataFrame:
    """
    Returns top N queries by total offline revenue with weekly breakdown.
    """
    return pd.read_sql_query(f"""
        SELECT
            sq.query_text,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.tag_method,
            sq.tag_confidence,
            STRFTIME('%Y-W%W', sqp.report_date) AS week_raw,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6  AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0) AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND a.platform            = 'google'
          AND sqp.device            = 'ALL'
          AND sqp.query_id IN (
              SELECT sqp2.query_id
              FROM search_query_performance sqp2
              JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
              JOIN accounts a2 ON ki2.account_id = a2.id
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
    """, conn, params=(GOOGLE_ACCOUNT_ID, GOOGLE_ACCOUNT_ID))


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------

def build_summary_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Weekly totals across all queries -- the macro view.
    Columns: week, total_revenue, total_clicks, total_spend,
             total_conversions, CPL, ROAS, active_queries
    """
    summary = weekly_df.groupby('week_label', as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
        pixel_leads=('pixel_leads', 'sum'),
        query_count=('query_count', 'sum'),
        active_query_count=('active_query_count', 'sum'),
    ).sort_values('week_label')

    summary['CPL ($)']  = pd.to_numeric(
        summary['spend'] / summary['conversions'], errors='coerce').round(2)
    summary['ROAS']     = pd.to_numeric(
        summary['revenue'] / summary['spend'], errors='coerce').round(2)
    summary['CTR (%)']  = pd.to_numeric(
        summary['clicks'] / summary['query_count'] * 100, errors='coerce').round(2)

    summary.columns = [
        'Week', 'Offline Revenue ($)', 'Clicks', 'Spend ($)',
        'Offline Conversions', 'Pixel Leads', 'Query Impressions',
        'Active Queries (w/ Clicks)', 'CPL ($)', 'ROAS', 'CTR (%)'
    ]
    return summary.round(2)


def build_brand_category_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Weekly revenue and clicks per brand_category.
    Pivoted wide: one column per category, rows = weeks.
    """
    # Fill null brand_category for non-brand queries
    df = weekly_df.copy()
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    # Aggregate by week + category
    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )
    agg['CPL'] = pd.to_numeric(
        agg['spend'] / agg['conversions'], errors='coerce'
    ).round(2)
    agg['ROAS'] = pd.to_numeric(
        agg['revenue'] / agg['spend'], errors='coerce'
    ).round(2)

    # Pivot revenue wide
    rev_pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    rev_pivot.columns.name = None

    # Add total
    cat_cols = [c for c in rev_pivot.columns if c != 'week_label']
    rev_pivot['Total Revenue ($)'] = rev_pivot[cat_cols].sum(axis=1)

    # Rename columns with friendly labels
    rename = {
        'carrier_approved':             'Approved Carriers ($)',
        'carrier_conditional':          'Conditional Carriers ($)',
        'carrier_restricted_requested': 'Restricted-Requested ($)',
        'carrier_restricted_partner':   'Restricted-Partner ($)',
        'aggregator_lead_buyer':        'Lead Buyer Aggregators ($)',
        'aggregator_competitor':        'Competitor Aggregators ($)',
        'non_brand':                    'Non-Brand / Generic ($)',
    }
    rev_pivot = rev_pivot.rename(columns=rename)
    rev_pivot = rev_pivot.rename(columns={'week_label': 'Week'})

    return rev_pivot.sort_values('Week').round(2)


def build_restricted_trend_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Detailed weekly view of restricted brand query performance.
    Shows restricted_requested (USAA, Liberty Mutual) and
    restricted_partner (Progressive, Allstate) side by side with
    the specific brand names, so the revenue impact of each pause
    is visible week by week.
    """
    restricted_cats = {
        'carrier_restricted_requested',
        'carrier_restricted_partner',
        'carrier_conditional',
    }
    df = weekly_df[weekly_df['brand_category'].isin(restricted_cats)].copy()

    if df.empty:
        return pd.DataFrame(columns=[
            'Week', 'Brand Category', 'Brand', 'Revenue ($)',
            'Clicks', 'Spend ($)', 'Conversions', 'CPL ($)'
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
        agg['spend'] / agg['conversions'], errors='coerce'
    ).round(2)

    agg = agg.rename(columns={
        'week_label':      'Week',
        'brand_category':  'Brand Category',
        'modifier_brand':  'Brand',
        'revenue':         'Revenue ($)',
        'clicks':          'Clicks',
        'spend':           'Spend ($)',
        'conversions':     'Offline Conversions',
    })

    # Sort: most recent week first, then by brand category and brand
    agg = agg.sort_values(
        ['Week', 'Brand Category', 'Revenue ($)'],
        ascending=[False, True, False]
    ).reset_index(drop=True)

    return agg.round(2)


def build_top_queries_sheet(top_df: pd.DataFrame) -> pd.DataFrame:
    """
    Top 50 queries by total revenue with monthly aggregation.
    Shows total across all time plus monthly breakdown.
    """
    if top_df.empty:
        return pd.DataFrame()

    top_df = top_df.copy()
    # Convert week_raw (format: '2025-W08' from STRFTIME('%Y-W%W'))
    # to the Monday of that ISO week using pandas Period
    def week_raw_to_date(w):
        if not isinstance(w, str) or '-W' not in w:
            return None
        try:
            year, wnum = w.split('-W')
            return pd.Timestamp.fromisocalendar(int(year), int(wnum), 1)
        except Exception:
            return None

    top_df['report_date'] = top_df['week_raw'].apply(week_raw_to_date)
    top_df['month'] = top_df['report_date'].dt.to_period('M').astype(str)

    # Monthly aggregation per query
    monthly = top_df.groupby(
        ['query_text', 'vertical', 'brand_category',
         'modifier_brand', 'tag_method', 'month'], as_index=False
    ).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )

    # Total per query
    totals = monthly.groupby('query_text', as_index=False).agg(
        total_revenue=('revenue', 'sum'),
        total_clicks=('clicks', 'sum'),
        total_spend=('spend', 'sum'),
        total_conversions=('conversions', 'sum'),
    )
    totals['CPL ($)'] = pd.to_numeric(
        totals['total_spend'] / totals['total_conversions'], errors='coerce').round(2)
    totals['ROAS'] = pd.to_numeric(
        totals['total_revenue'] / totals['total_spend'], errors='coerce').round(2)

    # Pivot monthly revenue wide
    rev_pivot = monthly.pivot_table(
        index='query_text', columns='month',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    rev_pivot.columns.name = None

    # Merge totals with monthly pivot
    # Also bring in metadata from first occurrence
    meta = top_df.groupby('query_text', as_index=False).agg(
        vertical=('vertical', 'first'),
        brand_category=('brand_category', 'first'),
        modifier_brand=('modifier_brand', 'first'),
    )

    result = totals.merge(meta, on='query_text').merge(rev_pivot, on='query_text')
    result = result.sort_values('total_revenue', ascending=False).reset_index(drop=True)

    # Move metadata columns to front
    meta_cols = ['query_text', 'vertical', 'brand_category', 'modifier_brand',
                 'total_revenue', 'total_spend', 'total_conversions',
                 'total_clicks', 'CPL ($)', 'ROAS']
    month_cols = [c for c in result.columns if c not in meta_cols]
    result = result[meta_cols + sorted(month_cols)]

    result = result.rename(columns={
        'query_text':         'Query',
        'vertical':           'Vertical',
        'brand_category':     'Brand Category',
        'modifier_brand':     'Brand',
        'total_revenue':      'Total Revenue ($)',
        'total_spend':        'Total Spend ($)',
        'total_conversions':  'Total Conversions',
        'total_clicks':       'Total Clicks',
    })
    return result.round(2)


def build_vertical_trend_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Weekly revenue and clicks broken out by vertical.
    """
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
    # Rename verticals with dollar sign for clarity
    pivot = pivot.rename(columns={c: f'{c} ($)' for c in vert_cols})

    return pivot.sort_values('Week').round(2)


# ---------------------------------------------------------------------------
# Word count by brand category sheet
# ---------------------------------------------------------------------------

def build_word_count_sheet(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Weekly average word count of search queries per brand_category.

    Word count is computed from the raw query_text stored in search_queries.
    A higher average word count indicates users are searching with more
    specific, long-tail queries -- generally a signal of higher purchase
    intent. Tracking this over time shows whether the query mix is becoming
    more or less specific for each brand category.

    This requires a second pass against the DB to get query_text with
    its brand_category. We use the pre-aggregated weekly_df to get the
    week dimension, then join word counts by brand_category at the
    query level.
    """
    import re

    df = weekly_df.copy()
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    # weekly_df doesn't carry query_text -- use modifier_brand as a proxy
    # for word count computation. We compute word count from modifier_brand
    # where available, and flag non-brand rows separately.
    # Since weekly_df is pre-aggregated at daily grain, word count needs
    # to be fetched separately. We approximate using the brand_category
    # grouping and a separate DB query in main() -- for now compute from
    # available columns.

    # Compute word count from modifier_brand (best available in weekly_df)
    # Non-brand rows have no modifier_brand -- word count not meaningful there.
    # We return a note in that column.
    df['word_count'] = df['modifier_brand'].apply(
        lambda x: len(str(x).split()) if pd.notna(x) and x else None
    )

    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        avg_word_count=('word_count', 'mean'),
        revenue=('revenue', 'sum'),
    )
    agg['avg_word_count'] = agg['avg_word_count'].round(2)

    pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='avg_word_count', aggfunc='mean', fill_value=None
    ).reset_index()
    pivot.columns.name = None

    rename = {
        'carrier_approved':             'Approved Carriers',
        'carrier_conditional':          'Conditional Carriers',
        'carrier_restricted_requested': 'Restricted-Requested',
        'carrier_restricted_partner':   'Restricted-Partner',
        'aggregator_lead_buyer':        'Lead Buyer Aggregators',
        'aggregator_competitor':        'Competitor Aggregators',
        'non_brand':                    'Non-Brand / Generic',
        'week_label':                   'Week',
    }
    pivot = pivot.rename(columns=rename)
    pivot = pivot.rename(columns={'week_label': 'Week'} if 'week_label' in pivot.columns else {})

    return pivot.sort_values('Week').round(2)


def build_query_word_count_sheet(conn) -> pd.DataFrame:
    """
    Fetches query_text with brand_category and computes true word count
    per query from the database, then aggregates to weekly average by
    brand_category.

    This is the accurate version -- uses actual search query text rather
    than the brand modifier approximation.
    """
    df = pd.read_sql_query("""
        SELECT
            STRFTIME('%Y-W%W', sqp.report_date)      AS week_label,
            sq.brand_category,
            sq.query_text,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = '8886502147'
          AND a.platform            = 'google'
          AND sqp.device            = 'ALL'
        GROUP BY week_label, sq.id
    """, conn)

    # Compute word count from actual query text
    df['word_count'] = df['query_text'].apply(
        lambda x: len(str(x).strip().split()) if pd.notna(x) else 0
    )
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    # Weighted average word count by revenue (revenue-weighted gives more
    # weight to high-value queries, which is more analytically meaningful)
    def weighted_avg(group):
        total_rev = group['revenue'].sum()
        if total_rev > 0:
            return (group['word_count'] * group['revenue']).sum() / total_rev
        return group['word_count'].mean()

    # Also compute simple average and query count
    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        avg_word_count=('word_count', 'mean'),
        unique_queries=('query_text', 'nunique'),
        revenue=('revenue', 'sum'),
    )
    agg['avg_word_count'] = agg['avg_word_count'].round(2)

    # Pivot avg word count wide by brand_category
    wc_pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='avg_word_count', aggfunc='mean'
    ).reset_index()
    wc_pivot.columns.name = None

    # Pivot unique query count wide
    uq_pivot = agg.pivot_table(
        index='week_label', columns='brand_category',
        values='unique_queries', aggfunc='sum', fill_value=0
    ).reset_index()
    uq_pivot.columns.name = None

    # Rename columns for word count sheet
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

    # Combine: word count pivot + total unique query count
    cat_cols = [c for c in wc_pivot.columns if c != 'week_label']
    uq_cat_cols = [c for c in uq_pivot.columns if c != 'week_label']

    # Add prefix to distinguish
    wc_pivot  = wc_pivot.rename(
        columns={c: f'{c} — avg words' for c in cat_cols}
    )
    uq_pivot  = uq_pivot.rename(
        columns={c: f'{c} — unique queries' for c in uq_cat_cols}
    )

    result = wc_pivot.merge(uq_pivot, on='week_label', how='outer')
    result = result.rename(columns={'week_label': 'Week'})

    # Interleave columns: avg_words then unique_queries per category
    ordered_cols = ['Week']
    for cat in cat_rename.values():
        wc_col = f'{cat} — avg words'
        uq_col = f'{cat} — unique queries'
        if wc_col in result.columns:
            ordered_cols.append(wc_col)
        if uq_col in result.columns:
            ordered_cols.append(uq_col)

    result = result[[c for c in ordered_cols if c in result.columns]]
    return result.sort_values('Week').round(2)



# ---------------------------------------------------------------------------
# Microsoft fetch functions
# ---------------------------------------------------------------------------

def fetch_ms_weekly_performance(conn) -> pd.DataFrame:
    """Weekly Microsoft search query performance Jan-Mar 2026."""
    df = pd.read_sql_query("""
        SELECT
            sqp.report_date,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_bundle,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks,
            COALESCE(SUM(sqp.impressions), 0)         AS impressions,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue,
            COALESCE(SUM(sqp.secondary_conv_count),0) AS pixel_leads,
            COUNT(DISTINCT sq.id)                     AS query_count
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND a.platform            = 'microsoft'
          AND sqp.device            = 'ALL'
        GROUP BY
            sqp.report_date, sq.vertical, sq.brand_category,
            sq.modifier_brand, sq.modifier_bundle
    """, conn, params=(MS_ACCOUNT_ID,))

    df['report_date'] = pd.to_datetime(df['report_date'])
    df['iso_year']    = df['report_date'].dt.isocalendar().year
    df['iso_week']    = df['report_date'].dt.isocalendar().week
    df['week_label']  = (df['iso_year'].astype(str) + '-W' +
                         df['iso_week'].astype(str).str.zfill(2))
    return df


def fetch_ms_match_type_performance(conn) -> pd.DataFrame:
    """
    Microsoft performance by match type served, weekly.
    Uses match_type_served from search_query_performance which
    reflects the delivered match type at auction time.
    """
    return pd.read_sql_query("""
        SELECT
            sqp.report_date,
            sqp.match_type_served,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks,
            COALESCE(SUM(sqp.impressions), 0)         AS impressions,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND a.platform            = 'microsoft'
          AND sqp.device            = 'ALL'
        GROUP BY sqp.report_date, sqp.match_type_served
    """, conn, params=(MS_ACCOUNT_ID,))


def fetch_cross_platform_queries(conn) -> pd.DataFrame:
    """
    Queries appearing on BOTH Google and Microsoft YTD (Jan 13 - present).
    Returns one row per query with Google and Microsoft metrics side by side.
    Revenue start date = 2026-01-13 (Microsoft offline upload start).
    """
    return pd.read_sql_query("""
        WITH google_perf AS (
            SELECT
                sqp.query_id,
                SUM(sqp.clicks)             AS g_clicks,
                SUM(sqp.cost_micros) / 1e6  AS g_spend,
                SUM(sqp.primary_conv_count) AS g_conversions,
                SUM(sqp.primary_conv_value) AS g_revenue
            FROM search_query_performance sqp
            JOIN keyword_instances ki ON sqp.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'google'
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
            GROUP BY sqp.query_id
        ),
        ms_perf AS (
            SELECT
                sqp.query_id,
                SUM(sqp.clicks)             AS m_clicks,
                SUM(sqp.cost_micros) / 1e6  AS m_spend,
                SUM(sqp.primary_conv_count) AS m_conversions,
                SUM(sqp.primary_conv_value) AS m_revenue
            FROM search_query_performance sqp
            JOIN keyword_instances ki ON sqp.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'microsoft'
              AND sqp.device = 'ALL'
            GROUP BY sqp.query_id
        )
        SELECT
            sq.query_text,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.tag_method,
            COALESCE(g.g_clicks, 0)       AS google_clicks,
            COALESCE(g.g_spend, 0)        AS google_spend,
            COALESCE(g.g_conversions, 0)  AS google_conversions,
            COALESCE(g.g_revenue, 0)      AS google_revenue,
            COALESCE(m.m_clicks, 0)       AS ms_clicks,
            COALESCE(m.m_spend, 0)        AS ms_spend,
            COALESCE(m.m_conversions, 0)  AS ms_conversions,
            COALESCE(m.m_revenue, 0)      AS ms_revenue
        FROM search_queries sq
        JOIN google_perf g ON g.query_id = sq.id
        JOIN ms_perf m     ON m.query_id = sq.id
        ORDER BY (g.g_revenue + m.m_revenue) DESC
    """, conn, params=(GOOGLE_ACCOUNT_ID, str(REVENUE_START_DATE), MS_ACCOUNT_ID))


def fetch_platform_category_summary(conn) -> pd.DataFrame:
    """
    YTD (Jan 13 - present) revenue, spend, conversions by brand_category
    for both platforms. The strategic POV sheet.
    """
    return pd.read_sql_query("""
        SELECT
            a.platform,
            sq.brand_category,
            SUM(sqp.clicks)             AS clicks,
            SUM(sqp.cost_micros) / 1e6  AS spend,
            SUM(sqp.primary_conv_count) AS conversions,
            SUM(sqp.primary_conv_value) AS revenue,
            COUNT(DISTINCT sq.id)       AS unique_queries
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
        GROUP BY a.platform, sq.brand_category
        ORDER BY a.platform, SUM(sqp.primary_conv_value) DESC
    """, conn, params=(str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID))


# ---------------------------------------------------------------------------
# New sheet builders
# ---------------------------------------------------------------------------

def build_ms_brand_category_sheet(ms_weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Microsoft weekly revenue by brand_category, Jan-Mar 2026.
    Mirrors the Google 'By brand category' sheet structure exactly
    so the two can be compared directly.
    """
    df = ms_weekly_df.copy()
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    agg = df.groupby(['week_label', 'brand_category'], as_index=False).agg(
        revenue=('revenue', 'sum'),
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
    )

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
    pivot = pivot.rename(columns=rename)
    if 'week_label' in pivot.columns:
        pivot = pivot.rename(columns={'week_label': 'Week'})

    return pivot.sort_values('Week').round(2)


def build_cross_platform_sheet(xp_df: pd.DataFrame) -> pd.DataFrame:
    """
    Side-by-side Google vs Microsoft performance for shared queries YTD.
    Sorted by combined revenue descending.
    Includes efficiency comparison: CPL and ROAS per platform, and
    a 'Platform edge' column indicating which platform is more efficient.
    """
    df = xp_df.copy()

    df['google_cpl']  = pd.to_numeric(
        df['google_spend'] / df['google_conversions'], errors='coerce').round(2)
    df['google_roas'] = pd.to_numeric(
        df['google_revenue'] / df['google_spend'], errors='coerce').round(2)
    df['ms_cpl']      = pd.to_numeric(
        df['ms_spend'] / df['ms_conversions'], errors='coerce').round(2)
    df['ms_roas']     = pd.to_numeric(
        df['ms_revenue'] / df['ms_spend'], errors='coerce').round(2)
    df['combined_revenue'] = df['google_revenue'] + df['ms_revenue']

    # Platform edge: which platform has lower CPL (better efficiency)
    def platform_edge(row):
        g_cpl = row['google_cpl']
        m_cpl = row['ms_cpl']
        if pd.isna(g_cpl) and pd.isna(m_cpl):
            return 'Neither (no conversions)'
        if pd.isna(g_cpl):
            return 'Microsoft'
        if pd.isna(m_cpl):
            return 'Google'
        if g_cpl < m_cpl * 0.9:
            return 'Google'
        if m_cpl < g_cpl * 0.9:
            return 'Microsoft'
        return 'Similar (<10% diff)'

    df['Platform Edge (CPL)'] = df.apply(platform_edge, axis=1)

    result = df[[
        'query_text', 'vertical', 'brand_category', 'modifier_brand',
        'combined_revenue',
        'google_revenue', 'google_spend', 'google_conversions',
        'google_clicks', 'google_cpl', 'google_roas',
        'ms_revenue', 'ms_spend', 'ms_conversions',
        'ms_clicks', 'ms_cpl', 'ms_roas',
        'Platform Edge (CPL)',
    ]].copy()

    result = result.rename(columns={
        'query_text':          'Query',
        'vertical':            'Vertical',
        'brand_category':      'Brand Category',
        'modifier_brand':      'Brand',
        'combined_revenue':    'Combined Revenue ($)',
        'google_revenue':      'Google Revenue ($)',
        'google_spend':        'Google Spend ($)',
        'google_conversions':  'Google Offline Convs',
        'google_clicks':       'Google Clicks',
        'google_cpl':          'Google CPL ($)',
        'google_roas':         'Google ROAS',
        'ms_revenue':          'MS Revenue ($)',
        'ms_spend':            'MS Spend ($)',
        'ms_conversions':      'MS Offline Convs',
        'ms_clicks':           'MS Clicks',
        'ms_cpl':              'MS CPL ($)',
        'ms_roas':             'MS ROAS',
    })

    return result.sort_values('Combined Revenue ($)', ascending=False).round(2)


def build_match_type_efficiency_sheet(mt_df: pd.DataFrame) -> pd.DataFrame:
    """
    Microsoft match type efficiency by week.
    Shows CPL and ROAS per match type to confirm whether the
    EXACT ramp is delivering better unit economics than BROAD.
    """
    df = mt_df.copy()
    df['report_date'] = pd.to_datetime(df['report_date'])
    df['iso_year']    = df['report_date'].dt.isocalendar().year
    df['iso_week']    = df['report_date'].dt.isocalendar().week
    df['week_label']  = (df['iso_year'].astype(str) + '-W' +
                         df['iso_week'].astype(str).str.zfill(2))

    agg = df.groupby(['week_label', 'match_type_served'], as_index=False).agg(
        clicks=('clicks', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
        revenue=('revenue', 'sum'),
        impressions=('impressions', 'sum'),
    )
    agg['CPL ($)']  = pd.to_numeric(
        agg['spend'] / agg['conversions'], errors='coerce').round(2)
    agg['ROAS']     = pd.to_numeric(
        agg['revenue'] / agg['spend'], errors='coerce').round(2)
    agg['CTR (%)']  = pd.to_numeric(
        agg['clicks'] / agg['impressions'] * 100, errors='coerce').round(2)

    # Pivot so each match type gets its own CPL, ROAS, Revenue columns
    metrics = ['revenue', 'spend', 'clicks', 'conversions', 'CPL ($)', 'ROAS']
    frames = []
    for metric in metrics:
        p = agg.pivot_table(
            index='week_label', columns='match_type_served',
            values=metric, aggfunc='sum' if metric not in ('CPL ($)', 'ROAS') else 'mean',
            fill_value=None
        ).reset_index()
        p.columns.name = None
        mt_cols = [c for c in p.columns if c != 'week_label']
        suffix_map = {
            'revenue':      'Revenue ($)',
            'spend':        'Spend ($)',
            'clicks':       'Clicks',
            'conversions':  'Offline Convs',
            'CPL ($)':      'CPL ($)',
            'ROAS':         'ROAS',
        }
        p = p.rename(columns={c: f'{c} — {suffix_map[metric]}' for c in mt_cols})
        frames.append(p)

    result = frames[0]
    for f in frames[1:]:
        result = result.merge(f, on='week_label', how='outer')

    # Order columns: BROAD then EXACT then PHRASE per metric block
    result = result.rename(columns={'week_label': 'Week'})

    # Interleave: for each match type, group all metrics together
    ordered = ['Week']
    for mt in ['BROAD', 'EXACT', 'PHRASE']:
        for m in ['Revenue ($)', 'Spend ($)', 'Clicks', 'Offline Convs',
                  'CPL ($)', 'ROAS']:
            col = f'{mt} — {m}'
            if col in result.columns:
                ordered.append(col)

    result = result[[c for c in ordered if c in result.columns]]
    return result.sort_values('Week').round(2)


def build_platform_summary_sheet(cat_df: pd.DataFrame) -> pd.DataFrame:
    """
    Strategic POV sheet: YTD performance by brand_category for each platform.
    Side-by-side Google vs Microsoft with CPL and ROAS per category.
    This is the meeting-ready summary answering:
    'Where should we invest more on Microsoft vs Google?'
    """
    df = cat_df.copy()
    df['brand_category'] = df['brand_category'].fillna('non_brand')

    df['CPL ($)']  = pd.to_numeric(
        df['spend'] / df['conversions'], errors='coerce').round(2)
    df['ROAS']     = pd.to_numeric(
        df['revenue'] / df['spend'], errors='coerce').round(2)
    df['Rev/Query'] = pd.to_numeric(
        df['revenue'] / df['unique_queries'], errors='coerce').round(2)

    cat_label = {
        'carrier_approved':             'Carrier — Approved',
        'carrier_conditional':          'Carrier — Conditional',
        'carrier_restricted_requested': 'Carrier — Restricted (QW Request)',
        'carrier_restricted_partner':   'Carrier — Restricted (Partner)',
        'aggregator_lead_buyer':        'Aggregator — Lead Buyer',
        'aggregator_competitor':        'Aggregator — Competitor',
        'non_brand':                    'Non-Brand / Generic',
    }
    df['Brand Category'] = df['brand_category'].map(cat_label).fillna(
        df['brand_category'].str.title()
    )

    # Build two sub-tables (Google and Microsoft) then merge
    google_df = df[df['platform'] == 'google'].copy()
    ms_df     = df[df['platform'] == 'microsoft'].copy()

    merge_cols = ['Brand Category', 'revenue', 'spend', 'conversions',
                  'clicks', 'unique_queries', 'CPL ($)', 'ROAS', 'Rev/Query']

    google_df = google_df[merge_cols].rename(columns={
        'revenue':        'Google Revenue ($)',
        'spend':          'Google Spend ($)',
        'conversions':    'Google Offline Convs',
        'clicks':         'Google Clicks',
        'unique_queries': 'Google Unique Queries',
        'CPL ($)':        'Google CPL ($)',
        'ROAS':           'Google ROAS',
        'Rev/Query':      'Google Rev/Query ($)',
    })

    ms_df = ms_df[merge_cols].rename(columns={
        'revenue':        'MS Revenue ($)',
        'spend':          'MS Spend ($)',
        'conversions':    'MS Offline Convs',
        'clicks':         'MS Clicks',
        'unique_queries': 'MS Unique Queries',
        'CPL ($)':        'MS CPL ($)',
        'ROAS':           'MS ROAS',
        'Rev/Query':      'MS Rev/Query ($)',
    })

    result = google_df.merge(ms_df, on='Brand Category', how='outer').fillna(0)

    # Add CPL advantage column
    def cpl_advantage(row):
        g = row.get('Google CPL ($)', 0)
        m = row.get('MS CPL ($)', 0)
        if g == 0 and m == 0:
            return 'No conversions'
        if g == 0:
            return 'MS only'
        if m == 0:
            return 'Google only'
        diff_pct = (m - g) / g * 100
        if diff_pct > 10:
            return f'Google better (+{diff_pct:.0f}% MS CPL premium)'
        if diff_pct < -10:
            return f'MS better ({abs(diff_pct):.0f}% Google CPL premium)'
        return f'Similar ({diff_pct:+.0f}%)'

    result['CPL Advantage'] = result.apply(cpl_advantage, axis=1)

    # Sort by combined revenue
    result['_sort'] = (
        result.get('Google Revenue ($)', 0) +
        result.get('MS Revenue ($)', 0)
    )
    result = result.sort_values('_sort', ascending=False).drop(
        columns=['_sort']
    ).reset_index(drop=True)

    return result.round(2)

# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path):
    wb = load_workbook(path)

    header_fills = {
        'Summary':                '1F4E79',
        'By brand category':      '375623',
        'Restricted trend':       '7B0000',
        'Top queries':            '4A235A',
        'Vertical trend':         '1F4E79',
        'Query word count':       '375623',
        'MS brand category':      '375623',
        'Cross-platform queries': '1F4E79',
        'MS match type':          '833C00',
        'Platform summary':       '1F4E79',
    }
    white_font  = Font(color='FFFFFF', bold=True, size=11)

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        fill_hex = header_fills.get(sheet_name, '1F4E79')
        hdr_fill = PatternFill('solid', fgColor=fill_hex)

        for cell in ws[1]:
            cell.font      = white_font
            cell.fill      = hdr_fill
            cell.alignment = Alignment(
                horizontal='center', vertical='center', wrap_text=True
            )
        ws.row_dimensions[1].height = 32

        # Auto-fit columns
        for col in ws.columns:
            max_len    = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    val_len  = len(str(cell.value)) if cell.value is not None else 0
                    max_len  = max(max_len, val_len)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 4, 36)

        # Number formatting
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if not isinstance(cell.value, (int, float)):
                    continue
                hdr = str(ws.cell(1, cell.column).value or '').lower()
                if '$' in hdr or 'revenue' in hdr or 'spend' in hdr or \
                   'cpl' in hdr or 'roas' in hdr:
                    cell.number_format = '$#,##0.00'
                elif '%' in hdr or 'ctr' in hdr:
                    cell.number_format = '0.00%'
                elif 'conversion' in hdr or 'click' in hdr or \
                     'impression' in hdr or 'lead' in hdr:
                    cell.number_format = '#,##0'

        # Color scale on revenue columns for trend sheets
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
                            end_type='max',   end_color='63BE7B'
                        )
                    )

        ws.freeze_panes = 'B2'

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print('=' * 65)
    print('Search Query Trend Analysis — Home Insurance')
    print(f'Google account : {GOOGLE_ACCOUNT_ID}')
    print(f'End date       : {ANALYSIS_END_DATE}')
    print('=' * 65)

    conn = get_conn()

    print('\nFetching weekly performance data...')
    weekly_df = fetch_weekly_performance(conn)
    print(f'  {len(weekly_df):,} daily rows fetched')
    print(f'  Date range: {weekly_df["report_date"].min().date()} to '
          f'{weekly_df["report_date"].max().date()}')
    print(f'  ISO weeks: {weekly_df["week_label"].nunique()}')

    print('\nFetching top 50 queries...')
    top_df = fetch_top_queries(conn, n=50)
    print(f'  {len(top_df):,} query-week rows')

    print('Fetching query word count data...')
    word_count_df = build_query_word_count_sheet(conn)
    print(f'  {len(word_count_df):,} week rows')

    print('Fetching Microsoft weekly performance...')
    ms_weekly_df = fetch_ms_weekly_performance(conn)
    print(f'  {len(ms_weekly_df):,} Microsoft daily rows')

    print('Fetching Microsoft match type performance...')
    ms_mt_df = fetch_ms_match_type_performance(conn)
    print(f'  {len(ms_mt_df):,} match type rows')

    print('Fetching cross-platform shared queries...')
    xp_df = fetch_cross_platform_queries(conn)
    print(f'  {len(xp_df):,} shared queries (both platforms)')

    print('Fetching platform category summary...')
    platform_cat_df = fetch_platform_category_summary(conn)
    print(f'  {len(platform_cat_df):,} category-platform rows')

    conn.close()

    # Build sheets
    print('\nBuilding sheets...')

    print('  Summary...')
    summary_df = build_summary_sheet(weekly_df)

    print('  By brand category...')
    category_df = build_brand_category_sheet(weekly_df)

    print('  Restricted trend...')
    restricted_df = build_restricted_trend_sheet(weekly_df)

    print('  Top queries...')
    top_queries_df = build_top_queries_sheet(top_df)

    print('  Vertical trend...')
    vertical_df = build_vertical_trend_sheet(weekly_df)

    print('  Query word count...')
    # word_count_df already built during fetch phase

    print('  MS brand category...')
    ms_cat_df = build_ms_brand_category_sheet(ms_weekly_df)

    print('  Cross-platform queries...')
    xp_result_df = build_cross_platform_sheet(xp_df)

    print('  MS match type efficiency...')
    mt_eff_df = build_match_type_efficiency_sheet(ms_mt_df)

    print('  Platform summary...')
    platform_summary_df = build_platform_summary_sheet(platform_cat_df)

    # Terminal preview
    print('\n--- Weekly summary (last 8 weeks) ---')
    print(summary_df.tail(8)[
        ['Week', 'Offline Revenue ($)', 'Clicks', 'Spend ($)', 'ROAS', 'CPL ($)']
    ].to_string(index=False))

    print('\n--- Brand category totals (last 4 weeks) ---')
    print(category_df.tail(4).to_string(index=False))

    print('\n--- Platform summary (YTD) ---')
    preview_cols = ['Brand Category', 'Google Revenue ($)', 'Google CPL ($)',
                    'Google ROAS', 'MS Revenue ($)', 'MS CPL ($)', 'MS ROAS',
                    'CPL Advantage']
    available = [c for c in preview_cols if c in platform_summary_df.columns]
    print(platform_summary_df[available].to_string(index=False))

    # Write Excel
    print(f'\nWriting Excel output...')
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        summary_df.to_excel(writer,          sheet_name='Summary',                index=False)
        category_df.to_excel(writer,         sheet_name='By brand category',      index=False)
        restricted_df.to_excel(writer,       sheet_name='Restricted trend',       index=False)
        top_queries_df.to_excel(writer,      sheet_name='Top queries',            index=False)
        vertical_df.to_excel(writer,         sheet_name='Vertical trend',         index=False)
        word_count_df.to_excel(writer,       sheet_name='Query word count',       index=False)
        ms_cat_df.to_excel(writer,           sheet_name='MS brand category',      index=False)
        xp_result_df.to_excel(writer,        sheet_name='Cross-platform queries', index=False)
        mt_eff_df.to_excel(writer,           sheet_name='MS match type',          index=False)
        platform_summary_df.to_excel(writer, sheet_name='Platform summary',       index=False)

    print('Applying formatting...')
    format_workbook(OUTPUT_FILE)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f'\nOutput saved : {OUTPUT_FILE}')
    print(f'File size    : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
