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
# Keyword efficiency analysis -- query-to-keyword attribution
# ---------------------------------------------------------------------------

def fetch_keyword_query_efficiency(conn, platform: str,
                                    account_id: str) -> pd.DataFrame:
    """
    Keyword efficiency analysis grouped by keyword_identities.identity_id
    (normalized_text + match_type), not by keyword_instances.id.

    This eliminates duplicate rows caused by the same keyword appearing
    in multiple ad groups or campaigns -- e.g. "Farmers Home Insurance BROAD"
    in 5 ad groups becomes one row showing combined spend and waste.

    New metrics vs previous version:
    - exact_query_revenue: revenue from search queries where match_type_served = EXACT
    - exact_query_pct: % of keyword revenue from exact-match queries
    - best_query_match_type: the match type that generates most converting queries
      (used to flag if keyword's bid match type differs from what's actually working)

    Also pulls vertical, brand_category, modifier_brand from keyword_identities
    which are populated by 09_tag_keyword_identities.py.
    """
    return pd.read_sql_query("""
        WITH query_perf AS (
            -- One row per (identity, search_query) -- aggregated across
            -- all instances (ad groups) of the same keyword
            SELECT
                ki.identity_id,
                sq.query_text,
                sqp.match_type_served,
                SUM(sqp.clicks)             AS clicks,
                SUM(sqp.cost_micros) / 1e6  AS spend,
                SUM(sqp.primary_conv_count) AS conversions,
                SUM(sqp.primary_conv_value) AS revenue
            FROM search_query_performance sqp
            JOIN search_queries sq ON sqp.query_id = sq.id
            JOIN keyword_instances ki ON sqp.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = ?
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
            GROUP BY ki.identity_id, sq.id, sqp.match_type_served
        ),
        identity_agg AS (
            SELECT
                qp.identity_id,
                SUM(qp.spend)                                       AS total_spend,
                SUM(qp.revenue)                                     AS total_revenue,
                SUM(qp.conversions)                                 AS total_conversions,
                SUM(qp.clicks)                                      AS total_clicks,
                -- Wasted spend: queries that generated zero conversions
                SUM(CASE WHEN qp.conversions = 0
                         THEN qp.spend ELSE 0 END)                  AS wasted_spend,
                SUM(CASE WHEN qp.conversions > 0
                         THEN qp.spend ELSE 0 END)                  AS productive_spend,
                COUNT(DISTINCT qp.query_text)                       AS total_queries,
                COUNT(DISTINCT CASE WHEN qp.conversions = 0
                               THEN qp.query_text END)              AS zero_conv_queries,
                COUNT(DISTINCT CASE WHEN qp.conversions > 0
                               THEN qp.query_text END)              AS converting_queries,
                -- Revenue from exact-match served queries
                SUM(CASE WHEN qp.match_type_served = 'EXACT'
                         THEN qp.revenue ELSE 0 END)                AS exact_query_revenue,
                -- Revenue from broad-served queries (potential waste signal)
                SUM(CASE WHEN qp.match_type_served = 'BROAD'
                         THEN qp.revenue ELSE 0 END)                AS broad_query_revenue,
                SUM(CASE WHEN qp.match_type_served = 'PHRASE'
                         THEN qp.revenue ELSE 0 END)                AS phrase_query_revenue
            FROM query_perf qp
            GROUP BY qp.identity_id
        )
        SELECT
            ki_id.normalized_text         AS keyword_text,
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
            COUNT(DISTINCT ki.id)         AS instance_count
        FROM identity_agg ia
        JOIN keyword_identities ki_id ON ki_id.id = ia.identity_id
        -- Count how many ad group instances this identity has
        JOIN keyword_instances ki ON ki.identity_id = ia.identity_id
            AND ki.account_id IN (
                SELECT id FROM accounts
                WHERE platform_account_id = ? AND platform = ?
            )
        WHERE ia.total_spend > 0
        GROUP BY ia.identity_id
        ORDER BY ia.wasted_spend DESC
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE),
                       account_id, platform))


def fetch_top_queries_by_platform(conn, platform: str,
                                   account_id: str, n: int = 50) -> pd.DataFrame:
    """Top N queries by offline revenue for a specific platform, YTD."""
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
          AND a.platform = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND sqp.query_id IN (
              SELECT sqp2.query_id
              FROM search_query_performance sqp2
              JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
              JOIN accounts a2 ON ki2.account_id = a2.id
              WHERE a2.platform_account_id = ?
                AND a2.platform = ?
                AND sqp2.device = 'ALL'
                AND sqp2.report_date >= ?
              GROUP BY sqp2.query_id
              ORDER BY SUM(sqp2.primary_conv_value) DESC
              LIMIT {n}
          )
        GROUP BY sqp.query_id, sq.query_text, sq.vertical, sq.brand_category,
                 sq.modifier_brand, sq.tag_method, sq.tag_confidence, week_raw
        ORDER BY sq.query_text, week_raw
    """, conn, params=(account_id, platform, str(YTD_START_DATE),
                       account_id, platform, str(YTD_START_DATE)))


def fetch_ytd_summary(conn) -> pd.DataFrame:
    """
    Weekly summary for BOTH platforms combined, YTD from Jan 13.
    Used for the YTD overview sheet.
    """
    df = pd.read_sql_query("""
        SELECT
            sqp.report_date,
            a.platform,
            COALESCE(SUM(sqp.clicks), 0)              AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS revenue,
            COUNT(DISTINCT sqp.query_id)               AS active_queries
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND sqp.report_date >= ?
          AND a.platform_account_id IN (?, ?)
        GROUP BY sqp.report_date, a.platform
    """, conn, params=(str(REVENUE_START_DATE), GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID))

    df['report_date'] = pd.to_datetime(df['report_date'])
    df['iso_year']    = df['report_date'].dt.isocalendar().year
    df['iso_week']    = df['report_date'].dt.isocalendar().week
    df['week_label']  = (df['iso_year'].astype(str) + '-W' +
                         df['iso_week'].astype(str).str.zfill(2))
    return df


# ---------------------------------------------------------------------------
# New sheet builders (keyword efficiency + top queries per platform)
# ---------------------------------------------------------------------------

def build_ytd_summary_sheet(ytd_df: pd.DataFrame) -> pd.DataFrame:
    """
    Side-by-side weekly YTD view: Google and Microsoft each week.
    Fair comparison window: Jan 13 - present (offline upload start).
    Excludes W01-W02 where Microsoft had no offline revenue.
    """
    agg = ytd_df.groupby(['week_label', 'platform'], as_index=False).agg(
        revenue=('revenue', 'sum'),
        spend=('spend', 'sum'),
        conversions=('conversions', 'sum'),
        clicks=('clicks', 'sum'),
        active_queries=('active_queries', 'sum'),
    )

    g = agg[agg['platform'] == 'google'].copy()
    m = agg[agg['platform'] == 'microsoft'].copy()

    for df_p in [g, m]:
        df_p['CPL ($)']  = pd.to_numeric(
            df_p['spend'] / df_p['conversions'], errors='coerce').round(2)
        df_p['ROAS']     = pd.to_numeric(
            df_p['revenue'] / df_p['spend'], errors='coerce').round(2)

    g = g.rename(columns={
        'revenue': 'Google Revenue ($)', 'spend': 'Google Spend ($)',
        'conversions': 'Google Offline Convs', 'clicks': 'Google Clicks',
        'active_queries': 'Google Active Queries',
        'CPL ($)': 'Google CPL ($)', 'ROAS': 'Google ROAS',
    }).drop(columns=['platform'])

    m = m.rename(columns={
        'revenue': 'MS Revenue ($)', 'spend': 'MS Spend ($)',
        'conversions': 'MS Offline Convs', 'clicks': 'MS Clicks',
        'active_queries': 'MS Active Queries',
        'CPL ($)': 'MS CPL ($)', 'ROAS': 'MS ROAS',
    }).drop(columns=['platform'])

    result = g.merge(m, on='week_label', how='outer').fillna(0)
    result = result.rename(columns={'week_label': 'Week'})

    # Add combined and efficiency delta
    result['Combined Revenue ($)'] = result['Google Revenue ($)'] + result['MS Revenue ($)']
    result['Combined Spend ($)']   = result['Google Spend ($)']   + result['MS Spend ($)']
    result['CPL Delta (G-MS)']     = pd.to_numeric(
        result['Google CPL ($)'] - result['MS CPL ($)'], errors='coerce').round(2)
    result['ROAS Delta (MS-G)']    = pd.to_numeric(
        result['MS ROAS'] - result['Google ROAS'], errors='coerce').round(2)

    return result.sort_values('Week').round(2)


def build_top_queries_platform_sheet(
    top_df: pd.DataFrame, platform_label: str
) -> pd.DataFrame:
    """
    Top 50 queries by offline revenue for a single platform, YTD.
    Monthly breakdown same format as existing Top queries sheet.
    """
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
        total_revenue=('revenue', 'sum'), total_spend=('spend', 'sum'),
        total_conversions=('conversions', 'sum'), total_clicks=('clicks', 'sum'),
    )
    totals['CPL ($)'] = pd.to_numeric(
        totals['total_spend'] / totals['total_conversions'], errors='coerce').round(2)
    totals['ROAS'] = pd.to_numeric(
        totals['total_revenue'] / totals['total_spend'], errors='coerce').round(2)

    rev_pivot = monthly.pivot_table(
        index='query_text', columns='month',
        values='revenue', aggfunc='sum', fill_value=0
    ).reset_index()
    rev_pivot.columns.name = None

    meta = monthly.groupby('query_text', as_index=False).agg(
        vertical=('vertical', 'first'),
        brand_category=('brand_category', 'first'),
        modifier_brand=('modifier_brand', 'first'),
    )

    result = totals.merge(meta, on='query_text').merge(rev_pivot, on='query_text')
    result = result.sort_values('total_revenue', ascending=False).reset_index(drop=True)

    meta_cols = ['query_text', 'vertical', 'brand_category', 'modifier_brand',
                 'total_revenue', 'total_spend', 'total_conversions',
                 'total_clicks', 'CPL ($)', 'ROAS']
    month_cols = sorted([c for c in result.columns if c not in meta_cols])
    result = result[meta_cols + month_cols].rename(columns={
        'query_text': 'Query', 'vertical': 'Vertical',
        'brand_category': 'Brand Category', 'modifier_brand': 'Brand',
        'total_revenue': f'Total Revenue ($)',
        'total_spend': f'Total Spend ($)',
        'total_conversions': 'Total Offline Convs',
        'total_clicks': 'Total Clicks',
    })
    return result.round(2)


def build_keyword_efficiency_sheet(
    kw_df: pd.DataFrame, platform_label: str
) -> pd.DataFrame:
    """
    Keyword efficiency analysis grouped by identity (normalized_text + match_type).
    No duplicate rows -- each keyword+match_type appears once, spanning all ad groups.

    Key metrics:
    - Exact Query Revenue %: what % of this keyword's revenue comes from
      queries that matched exactly (high = precise, low = broad matching noise)
    - Match Type Signal: flag when a keyword's bid match type may not match
      how queries are actually converting
    - Waste Rate: % of spend on zero-conversion queries
    """
    df = kw_df.copy()

    df['CPL ($)']              = pd.to_numeric(
        df['total_spend'] / df['total_conversions'], errors='coerce').round(2)
    df['ROAS']                 = pd.to_numeric(
        df['total_revenue'] / df['total_spend'], errors='coerce').round(2)
    df['Waste Rate (%)']       = pd.to_numeric(
        df['wasted_spend'] / df['total_spend'] * 100, errors='coerce').round(1)
    df['Query Conv Rate (%)']  = pd.to_numeric(
        df['converting_queries'] / df['total_queries'] * 100,
        errors='coerce').round(1)

    # Exact query revenue %: how much revenue comes from exact-match queries
    df['Exact Query Rev (%)']  = pd.to_numeric(
        df['exact_query_revenue'] / df['total_revenue'] * 100,
        errors='coerce').round(1)

    # Match type signal: flag mismatches
    def match_signal(row):
        mt     = str(row.get('match_type', '')).upper()
        e_rev  = float(row.get('exact_query_revenue') or 0)
        b_rev  = float(row.get('broad_query_revenue') or 0)
        p_rev  = float(row.get('phrase_query_revenue') or 0)
        t_rev  = float(row.get('total_revenue') or 0)
        waste  = float(row.get('Waste Rate (%)') or 0)

        if t_rev == 0:
            return 'No revenue — review'

        e_pct = e_rev / t_rev * 100 if t_rev > 0 else 0
        b_pct = b_rev / t_rev * 100 if t_rev > 0 else 0

        if mt == 'BROAD' and e_pct > 60:
            return 'Consider EXACT — most revenue from exact queries'
        if mt == 'BROAD' and waste > 70:
            return 'Consider PHRASE — high waste, cap with phrase'
        if mt == 'BROAD' and waste > 50:
            return 'Monitor — moderate waste rate'
        if mt == 'EXACT' and b_pct > 30:
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
        'modifier_geo':          'Geo Modifier',
        'modifier_bundle':       'Bundle',
        'modifier_near_me':      'Near Me',
        'modifier_home_purchase':'Home Purchase Intent',
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
        'Geo Modifier', 'Bundle', 'Near Me', 'Home Purchase Intent', 'Ad Group Instances',
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


def build_consolidation_sheet(xp_df: pd.DataFrame,
                               g_kw_df: pd.DataFrame,
                               ms_kw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Strategic recommendation sheet: where to consolidate vs differentiate.

    Uses raw xp_df column names (lowercase snake_case from SQL fetch):
        google_revenue, google_spend, google_conversions, google_clicks
        ms_revenue, ms_spend, ms_conversions, ms_clicks
    Computes CPL/ROAS/recommendations internally, then renames for output.
    """
    df = xp_df.copy()

    def safe_float(v):
        try:
            return float(v) if v not in (None, '', 'inf', 'nan') else 0.0
        except Exception:
            return 0.0

    for col in ['google_revenue', 'google_spend', 'google_conversions',
                'google_clicks', 'ms_revenue', 'ms_spend',
                'ms_conversions', 'ms_clicks']:
        if col in df.columns:
            df[col] = df[col].apply(safe_float)

    df['google_cpl']  = pd.to_numeric(
        df['google_spend'] / df['google_conversions'], errors='coerce').round(2)
    df['google_roas'] = pd.to_numeric(
        df['google_revenue'] / df['google_spend'], errors='coerce').round(2)
    df['ms_cpl']      = pd.to_numeric(
        df['ms_spend'] / df['ms_conversions'], errors='coerce').round(2)
    df['ms_roas']     = pd.to_numeric(
        df['ms_revenue'] / df['ms_spend'], errors='coerce').round(2)
    df['combined_revenue'] = df['google_revenue'] + df['ms_revenue']

    def recommendation(row):
        g_cpl  = row['google_cpl']  if pd.notna(row['google_cpl'])  else 0
        m_cpl  = row['ms_cpl']      if pd.notna(row['ms_cpl'])      else 0
        g_roas = row['google_roas'] if pd.notna(row['google_roas']) else 0
        m_roas = row['ms_roas']     if pd.notna(row['ms_roas'])     else 0

        if g_cpl == 0 and m_cpl == 0:
            return 'No conversions on either — review keyword targeting'
        if g_cpl == 0:
            return 'Scale on Microsoft — Google not converting'
        if m_cpl == 0:
            return 'Scale on Google — Microsoft not converting'

        cpl_diff = (m_cpl - g_cpl) / g_cpl * 100

        if cpl_diff > 30:
            return f'Consolidate to Google (MS CPL {cpl_diff:.0f}% higher)'
        if cpl_diff < -30:
            return f'Scale on Microsoft (Google CPL {abs(cpl_diff):.0f}% higher)'
        if abs(cpl_diff) <= 15:
            if g_roas >= 1.2 and m_roas >= 1.2:
                return 'Maintain on both — strong ROAS on each platform'
            return 'Maintain on both — similar efficiency'
        if cpl_diff > 15:
            return f'Favor Google (MS CPL {cpl_diff:.0f}% higher)'
        return f'Favor Microsoft (Google CPL {abs(cpl_diff):.0f}% higher)'

    def ms_match_rec(row):
        m_cpl  = row['ms_cpl']      if pd.notna(row['ms_cpl'])  else 0
        m_roas = row['ms_roas']     if pd.notna(row['ms_roas']) else 0
        m_rev  = row['ms_revenue']
        query  = str(row.get('query_text', ''))
        words  = len(query.split())

        if m_rev == 0:
            return 'Add as EXACT — no current MS coverage'
        if words <= 2:
            return 'EXACT — short query, precise intent'
        if words >= 4 and m_cpl > 0 and m_cpl < 35:
            return 'PHRASE — long-tail, good CPL, capture variants'
        if m_roas >= 1.5:
            return 'EXACT — high ROAS, protect with exact match'
        return 'EXACT — brand query, exact preferred'

    df['Recommendation']    = df.apply(recommendation, axis=1)
    df['MS Match Type Rec'] = df.apply(ms_match_rec, axis=1)

    # Filter to queries with meaningful combined revenue
    df = df[df['combined_revenue'] >= 50].copy()

    # Rename for output
    result = df.rename(columns={
        'query_text':          'Query',
        'vertical':            'Vertical',
        'brand_category':      'Brand Category',
        'modifier_brand':      'Brand',
        'combined_revenue':    'Combined Revenue ($)',
        'google_revenue':      'Google Revenue ($)',
        'google_cpl':          'Google CPL ($)',
        'google_roas':         'Google ROAS',
        'ms_revenue':          'MS Revenue ($)',
        'ms_cpl':              'MS CPL ($)',
        'ms_roas':             'MS ROAS',
    })

    col_order = [
        'Query', 'Vertical', 'Brand Category', 'Brand',
        'Combined Revenue ($)',
        'Google Revenue ($)', 'Google CPL ($)', 'Google ROAS',
        'MS Revenue ($)', 'MS CPL ($)', 'MS ROAS',
        'Recommendation', 'MS Match Type Rec',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Combined Revenue ($)', ascending=False).round(2)


# ---------------------------------------------------------------------------
# Brand-level YTD cross-platform query aggregation
# ---------------------------------------------------------------------------

def fetch_brand_ytd_cross_platform(conn,
                                       start_date: str = None) -> pd.DataFrame:
    """
    Aggregates search query performance by brand with Google and Microsoft
    columns side by side.

    When start_date is provided (REVENUE_START_DATE = Jan 13):
      Both platforms use the same start date -- fair YTD comparison.
      Microsoft has no offline revenue before Jan 13 so this is correct.

    When start_date is None:
      Google uses full historical window (Mar 2025 - present).
      Microsoft still uses REVENUE_START_DATE (no earlier offline data).
      This gives a richer Google view for all-time brand analysis.
    """
    if start_date:
        # Fair YTD: both platforms from Jan 13
        params = (GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID, start_date, start_date)
        date_filter = """
          AND (
              (a.platform = 'google'    AND sqp.report_date >= ?)
              OR
              (a.platform = 'microsoft' AND sqp.report_date >= ?)
          )"""
    else:
        # All-time: Google full window, Microsoft from Jan 13
        params = (GOOGLE_ACCOUNT_ID, MS_ACCOUNT_ID, str(REVENUE_START_DATE))
        date_filter = """
          AND (
              (a.platform = 'google')
              OR
              (a.platform = 'microsoft' AND sqp.report_date >= ?)
          )"""

    return pd.read_sql_query(f"""
        SELECT
            COALESCE(sq.modifier_brand, '(non-brand)') AS brand,
            sq.brand_category,
            sq.vertical,
            a.platform,
            COUNT(DISTINCT sq.id)                AS unique_queries,
            SUM(sqp.clicks)                      AS clicks,
            SUM(sqp.cost_micros) / 1e6           AS spend,
            SUM(sqp.primary_conv_count)          AS conversions,
            SUM(sqp.primary_conv_value)          AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE sqp.device = 'ALL'
          AND a.platform_account_id IN (?, ?)
          {date_filter}
        GROUP BY sq.modifier_brand, sq.brand_category, sq.vertical, a.platform
        ORDER BY SUM(sqp.primary_conv_value) DESC
    """, conn, params=params)


def fetch_brand_top_keywords(conn, platform: str, account_id: str) -> pd.DataFrame:
    """
    For each brand, the top 5 keywords by revenue on this platform YTD,
    plus the top raw search query per keyword.

    Implemented in two simple queries + Python-side ranking to avoid
    SQLite limitations with window functions inside GROUP BY CTEs.
    """
    # Step 1: keyword performance by identity (deduped across ad groups)
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
          AND a.platform = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id
        HAVING SUM(sqp.primary_conv_value) > 0
        ORDER BY kw_revenue DESC
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE)))

    if kw_df.empty:
        return pd.DataFrame()

    # Step 2: top search query per keyword identity
    query_df = pd.read_sql_query("""
        SELECT
            ki.identity_id,
            sq.query_text,
            SUM(sqp.primary_conv_value) AS q_revenue
        FROM search_query_performance sqp
        JOIN search_queries sq        ON sqp.query_id   = sq.id
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN accounts a               ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id, sq.id
        ORDER BY ki.identity_id, q_revenue DESC
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE)))

    # Python-side: pick top query per identity_id
    top_query = (
        query_df.sort_values('q_revenue', ascending=False)
                .drop_duplicates(subset='identity_id', keep='first')
                [['identity_id', 'query_text', 'q_revenue']]
    )

    # Python-side: rank keywords within each brand, keep top 5
    kw_df['brand_key'] = kw_df['modifier_brand'].fillna('(non-brand)')
    kw_df = kw_df.sort_values('kw_revenue', ascending=False)
    kw_df['brand_rank'] = (
        kw_df.groupby('brand_key').cumcount() + 1
    )
    kw_df = kw_df[kw_df['brand_rank'] <= 5].copy()

    # Merge top query onto keywords
    result = kw_df.merge(top_query, on='identity_id', how='left')
    result = result.rename(columns={
        'brand_key':   'brand',
        'brand_rank':  'rank_within_brand',
        'query_text':  'top_search_query',
        'q_revenue':   'top_query_revenue',
    })

    return result.sort_values(
        ['brand', 'rank_within_brand']
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# New sheet builders
# ---------------------------------------------------------------------------

def build_brand_ytd_sheet(brand_df: pd.DataFrame) -> pd.DataFrame:
    """
    Brand-level YTD cross-platform comparison.
    One row per brand, with Google and Microsoft columns side by side.
    Sorted by combined revenue descending.
    """
    df = brand_df.copy()

    g = df[df['platform'] == 'google'].copy()
    m = df[df['platform'] == 'microsoft'].copy()

    def pivot_platform(p_df, prefix):
        p_df = p_df.copy()
        p_df[f'{prefix} CPL ($)'] = pd.to_numeric(
            p_df['spend'] / p_df['conversions'], errors='coerce').round(2)
        p_df[f'{prefix} ROAS'] = pd.to_numeric(
            p_df['revenue'] / p_df['spend'], errors='coerce').round(2)
        return p_df.rename(columns={
            'revenue':        f'{prefix} Revenue ($)',
            'spend':          f'{prefix} Spend ($)',
            'conversions':    f'{prefix} Offline Convs',
            'clicks':         f'{prefix} Clicks',
            'unique_queries': f'{prefix} Unique Queries',
        }).drop(columns=['platform'])

    g_pivot = pivot_platform(g, 'Google')
    m_pivot = pivot_platform(m, 'MS')

    # Merge on brand + brand_category + vertical
    key_cols = ['brand', 'brand_category', 'vertical']
    result = g_pivot.merge(m_pivot, on=key_cols, how='outer').fillna(0)

    # Combined revenue for sorting
    result['Combined Revenue ($)'] = (
        result.get('Google Revenue ($)', 0) +
        result.get('MS Revenue ($)', 0)
    )

    # CPL advantage
    def cpl_adv(row):
        g_c = float(row.get('Google CPL ($)') or 0)
        m_c = float(row.get('MS CPL ($)') or 0)
        if g_c == 0 and m_c == 0:
            return 'No conversions'
        if g_c == 0: return 'MS only'
        if m_c == 0: return 'Google only'
        diff = (m_c - g_c) / g_c * 100
        if diff > 15:  return f'Google better (+{diff:.0f}% MS premium)'
        if diff < -15: return f'MS better ({abs(diff):.0f}% Google premium)'
        return f'Similar ({diff:+.0f}%)'

    result['CPL Advantage'] = result.apply(cpl_adv, axis=1)

    col_order = [
        'brand', 'brand_category', 'vertical', 'Combined Revenue ($)',
        'Google Revenue ($)', 'Google Spend ($)', 'Google Offline Convs',
        'Google Clicks', 'Google Unique Queries',
        'Google CPL ($)', 'Google ROAS',
        'MS Revenue ($)', 'MS Spend ($)', 'MS Offline Convs',
        'MS Clicks', 'MS Unique Queries',
        'MS CPL ($)', 'MS ROAS', 'CPL Advantage',
    ]
    result = result.rename(columns={'brand': 'Brand', 'brand_category': 'Brand Category',
                                    'vertical': 'Vertical'})
    col_order = [c.replace('brand', 'Brand').replace('brand_category', 'Brand Category')
                   .replace('vertical', 'Vertical') for c in col_order]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Combined Revenue ($)', ascending=False).round(2)


def build_brand_keyword_sheet(bkw_df: pd.DataFrame,
                               platform_label: str) -> pd.DataFrame:
    """
    Per-brand top 5 keywords + their top search query.
    """
    if bkw_df.empty:
        return pd.DataFrame()

    df = bkw_df.copy()

    # Compute derived metrics using the raw column names from fetch
    df['CPL ($)'] = pd.to_numeric(
        df['kw_spend'] / df['kw_conversions'], errors='coerce').round(2)
    df['ROAS'] = pd.to_numeric(
        df['kw_revenue'] / df['kw_spend'], errors='coerce').round(2)
    df['Top Query Rev %'] = pd.to_numeric(
        df['top_query_revenue'] / df['kw_revenue'] * 100,
        errors='coerce').round(1)

    result = df.rename(columns={
        'brand':               'Brand',
        'brand_category':      'Brand Category',
        'vertical':            'Vertical',
        'rank_within_brand':   'Rank',
        'keyword_text':        'Keyword',
        'match_type':          'Match Type',
        'kw_revenue':          'Keyword Revenue ($)',
        'kw_spend':            'Keyword Spend ($)',
        'kw_conversions':      'Keyword Offline Convs',
        'kw_clicks':           'Keyword Clicks',
        'top_search_query':    'Top Search Query',
        'top_query_revenue':   'Top Query Revenue ($)',
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

    # Sort by brand total revenue descending, then rank within brand
    brand_rev = result.groupby('Brand')['Keyword Revenue ($)'].sum()
    result['_brand_rev'] = result['Brand'].map(brand_rev)
    result = result.sort_values(
        ['_brand_rev', 'Brand', 'Rank'], ascending=[False, True, True]
    ).drop(columns=['_brand_rev']).reset_index(drop=True)

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
        'YTD comparison':         '1F4E79',
        'Google top queries YTD': '375623',
        'MS top queries YTD':     '833C00',
        'Google kw efficiency':   '375623',
        'MS kw efficiency':       '833C00',
        'Consolidation recs':     '4A235A',
        'Brand YTD (queries)':    '1F4E79',
        'Google brand keywords':  '375623',
        'MS brand keywords':      '833C00',
        'Brand all-time (queries)': '1F4E79',
        'Investment overlap':        '1F4E79',
        'Google match type':        '375623',
        'MS match type analysis':   '833C00',
        'Google coverage gaps':     '375623',
        'MS coverage gaps':         '833C00',
        'MS geo capture':           '833C00',
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


# ===========================================================================
# PASS 1: Investment overlap analysis (Action Item 1)
# ===========================================================================

def fetch_investment_overlap(conn) -> pd.DataFrame:
    """
    For each brand_category, counts queries appearing on:
      - Google only
      - Microsoft only
      - Both platforms
    And sums revenue / spend for each segment.
    Window: REVENUE_START_DATE onward (Jan 13) for fair comparison.
    """
    return pd.read_sql_query("""
        WITH google_queries AS (
            SELECT DISTINCT sqp.query_id,
                   sq.brand_category,
                   sq.vertical,
                   SUM(sqp.primary_conv_value) AS g_revenue,
                   SUM(sqp.cost_micros) / 1e6  AS g_spend,
                   SUM(sqp.clicks)             AS g_clicks
            FROM search_query_performance sqp
            JOIN search_queries sq       ON sqp.query_id = sq.id
            JOIN keyword_instances ki    ON sqp.instance_id = ki.id
            JOIN accounts a              ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'google'
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
            GROUP BY sqp.query_id, sq.brand_category, sq.vertical
        ),
        ms_queries AS (
            SELECT DISTINCT sqp.query_id,
                   sq.brand_category,
                   sq.vertical,
                   SUM(sqp.primary_conv_value) AS m_revenue,
                   SUM(sqp.cost_micros) / 1e6  AS m_spend,
                   SUM(sqp.clicks)             AS m_clicks
            FROM search_query_performance sqp
            JOIN search_queries sq       ON sqp.query_id = sq.id
            JOIN keyword_instances ki    ON sqp.instance_id = ki.id
            JOIN accounts a              ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = 'microsoft'
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
            GROUP BY sqp.query_id, sq.brand_category, sq.vertical
        ),
        combined AS (
            SELECT
                COALESCE(g.query_id, m.query_id)       AS query_id,
                COALESCE(g.brand_category, m.brand_category) AS brand_category,
                COALESCE(g.vertical, m.vertical)        AS vertical,
                CASE
                    WHEN g.query_id IS NOT NULL AND m.query_id IS NOT NULL
                        THEN 'both'
                    WHEN g.query_id IS NOT NULL
                        THEN 'google_only'
                    ELSE 'ms_only'
                END                                     AS presence,
                COALESCE(g.g_revenue, 0)                AS g_revenue,
                COALESCE(g.g_spend,   0)                AS g_spend,
                COALESCE(g.g_clicks,  0)                AS g_clicks,
                COALESCE(m.m_revenue, 0)                AS m_revenue,
                COALESCE(m.m_spend,   0)                AS m_spend,
                COALESCE(m.m_clicks,  0)                AS m_clicks
            FROM google_queries g
            FULL OUTER JOIN ms_queries m ON g.query_id = m.query_id
        )
        SELECT
            COALESCE(brand_category, 'non_brand') AS brand_category,
            presence,
            COUNT(*)                              AS query_count,
            SUM(g_revenue + m_revenue)            AS total_revenue,
            SUM(g_revenue)                        AS google_revenue,
            SUM(m_revenue)                        AS ms_revenue,
            SUM(g_spend)                          AS google_spend,
            SUM(m_spend)                          AS ms_spend,
            SUM(g_clicks)                         AS google_clicks,
            SUM(m_clicks)                         AS ms_clicks
        FROM combined
        GROUP BY brand_category, presence
        ORDER BY brand_category, presence
    """, conn, params=(GOOGLE_ACCOUNT_ID, str(REVENUE_START_DATE),
                       MS_ACCOUNT_ID, str(REVENUE_START_DATE)))


def build_investment_overlap_sheet(overlap_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pivots the overlap data into a summary table with one row per
    brand_category showing Google-only, MS-only, both-platform query counts
    and revenue, plus overlap % and efficiency comparison.
    """
    if overlap_df.empty:
        return pd.DataFrame()

    df = overlap_df.copy()

    cat_labels = {
        'carrier_approved':             'Carrier — Approved',
        'carrier_conditional':          'Carrier — Conditional',
        'carrier_restricted_requested': 'Carrier — Restricted (QW Request)',
        'carrier_restricted_partner':   'Carrier — Restricted (Partner)',
        'aggregator_lead_buyer':        'Aggregator — Lead Buyer',
        'aggregator_competitor':        'Aggregator — Competitor',
        'aggregator_unverified':        'Aggregator — Unverified',
        'non_brand':                    'Non-Brand / Generic',
    }
    df['brand_category'] = df['brand_category'].map(cat_labels).fillna(
        df['brand_category'])

    # Pivot presence wide
    piv = df.pivot_table(
        index='brand_category',
        columns='presence',
        values=['query_count', 'total_revenue', 'google_revenue',
                'ms_revenue', 'google_spend', 'ms_spend'],
        aggfunc='sum',
        fill_value=0
    )
    piv.columns = [f'{col[0]}_{col[1]}' for col in piv.columns]
    piv = piv.reset_index()

    def col(metric, presence):
        return f'{metric}_{presence}'

    def safe(df, c):
        return df[c] if c in df.columns else 0

    # Query counts
    piv['Google Only Queries']  = safe(piv, col('query_count','google_only'))
    piv['MS Only Queries']      = safe(piv, col('query_count','ms_only'))
    piv['Both Platform Queries']= safe(piv, col('query_count','both'))
    piv['Total Queries']        = (piv['Google Only Queries'] +
                                   piv['MS Only Queries'] +
                                   piv['Both Platform Queries'])
    piv['Overlap % (of total)'] = pd.to_numeric(
        piv['Both Platform Queries'] / piv['Total Queries'] * 100,
        errors='coerce').round(1)

    # Revenue
    piv['Google Only Revenue ($)']   = safe(piv, col('google_revenue','google_only'))
    piv['MS Only Revenue ($)']        = safe(piv, col('ms_revenue','ms_only'))
    piv['Shared Revenue — Google ($)']= safe(piv, col('google_revenue','both'))
    piv['Shared Revenue — MS ($)']    = safe(piv, col('ms_revenue','both'))
    piv['Total Revenue ($)']          = (piv['Google Only Revenue ($)'] +
                                          piv['MS Only Revenue ($)'] +
                                          piv['Shared Revenue — Google ($)'] +
                                          piv['Shared Revenue — MS ($)'])

    # Platform efficiency on shared queries
    piv['Shared G CPL ($)'] = pd.to_numeric(
        safe(piv, col('google_spend','both')) /
        safe(piv, col('google_revenue','both')).replace(0, float('nan')),
        errors='coerce').round(2)
    piv['Shared MS CPL ($)'] = pd.to_numeric(
        safe(piv, col('ms_spend','both')) /
        safe(piv, col('ms_revenue','both')).replace(0, float('nan')),
        errors='coerce').round(2)

    def efficiency_signal(row):
        g = row.get('Shared G CPL ($)', 0) or 0
        m = row.get('Shared MS CPL ($)', 0) or 0
        if g == 0 and m == 0:
            return 'No shared conversions'
        if g == 0: return 'MS only converting'
        if m == 0: return 'Google only converting'
        diff = (m - g) / g * 100
        if diff > 20:   return f'Google more efficient (+{diff:.0f}% MS premium)'
        if diff < -20:  return f'MS more efficient ({abs(diff):.0f}% Google premium)'
        return f'Similar ({diff:+.0f}%)'

    piv['Efficiency on Shared Queries'] = piv.apply(efficiency_signal, axis=1)

    def overlap_signal(row):
        pct = row.get('Overlap % (of total)', 0) or 0
        go  = row.get('Google Only Queries', 0) or 0
        mo  = row.get('MS Only Queries', 0) or 0
        if pct >= 50:
            return 'High overlap — focus on efficiency, not expansion'
        if go > mo * 3:
            return 'Google-dominant — MS expansion opportunity'
        if mo > go * 3:
            return 'MS-dominant — Google expansion opportunity'
        return 'Balanced — review CPL to guide investment'

    piv['Investment Signal'] = piv.apply(overlap_signal, axis=1)

    col_order = [
        'brand_category',
        'Total Queries', 'Google Only Queries', 'MS Only Queries',
        'Both Platform Queries', 'Overlap % (of total)',
        'Total Revenue ($)',
        'Google Only Revenue ($)', 'MS Only Revenue ($)',
        'Shared Revenue — Google ($)', 'Shared Revenue — MS ($)',
        'Shared G CPL ($)', 'Shared MS CPL ($)',
        'Efficiency on Shared Queries', 'Investment Signal',
    ]
    piv = piv.rename(columns={'brand_category': 'Brand Category'})
    col_order[0] = 'Brand Category'
    result = piv[[c for c in col_order if c in piv.columns]]
    return result.sort_values('Total Revenue ($)', ascending=False).round(2)


# ===========================================================================
# PASS 2: Match type analysis (Action Item 2)
# ===========================================================================

def fetch_match_type_analysis(conn, platform: str,
                               account_id: str) -> pd.DataFrame:
    """
    Fetches all data needed for match type recommendation per keyword identity.

    For each keyword:
    - Performance metrics (revenue, spend, waste)
    - Query diversity (unique queries matched)
    - Conversion spread (HHI index + top query revenue %)
    - Exact coverage check (does exact match version exist on same platform?)
    - Geographic capture (% of queries with geo modifier)

    Returns one row per keyword identity.
    """
    # Step 1: keyword-level aggregates
    kw_df = pd.read_sql_query("""
        SELECT
            ki.identity_id,
            ki_id.normalized_text            AS keyword_text,
            ki_id.match_type,
            ki_id.brand_category,
            ki_id.modifier_brand,
            ki_id.vertical,
            ki_id.modifier_geo               AS kw_geo,
            COUNT(DISTINCT ki.id)            AS ad_group_instances,
            SUM(sqp.clicks)                  AS total_clicks,
            SUM(sqp.cost_micros) / 1e6       AS total_spend,
            SUM(sqp.primary_conv_count)      AS total_conversions,
            SUM(sqp.primary_conv_value)      AS total_revenue,
            -- Waste: spend on zero-conversion queries
            SUM(CASE WHEN sqp.primary_conv_value = 0
                     THEN sqp.cost_micros / 1e6 ELSE 0 END) AS wasted_spend,
            COUNT(DISTINCT sqp.query_id)     AS unique_queries,
            COUNT(DISTINCT CASE WHEN sqp.primary_conv_value > 0
                           THEN sqp.query_id END) AS converting_queries,
            -- Geo capture
            COUNT(DISTINCT CASE WHEN sq.modifier_geo IS NOT NULL
                           THEN sqp.query_id END) AS geo_queries,
            SUM(CASE WHEN sq.modifier_geo IS NOT NULL
                     THEN sqp.primary_conv_value ELSE 0 END) AS geo_revenue,
            -- Near me capture
            COUNT(DISTINCT CASE WHEN sq.modifier_near_me = 1
                           THEN sqp.query_id END) AS near_me_queries,
            SUM(CASE WHEN sq.modifier_near_me = 1
                     THEN sqp.primary_conv_value ELSE 0 END) AS near_me_revenue
        FROM search_query_performance sqp
        JOIN search_queries sq        ON sqp.query_id = sq.id
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        JOIN accounts a               ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id
        HAVING total_spend > 0
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE)))

    if kw_df.empty:
        return pd.DataFrame()

    # Step 2: per-query conversion revenue for HHI calculation
    query_conv_df = pd.read_sql_query("""
        SELECT
            ki.identity_id,
            sqp.query_id,
            sq.modifier_geo,
            sq.modifier_near_me,
            SUM(sqp.primary_conv_value) AS q_revenue,
            SUM(sqp.clicks)             AS q_clicks
        FROM search_query_performance sqp
        JOIN search_queries sq        ON sqp.query_id = sq.id
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN accounts a               ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = ?
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki.identity_id, sqp.query_id
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE)))

    # Step 3: Check exact coverage -- does an EXACT version of this keyword
    # exist on the same platform?
    exact_ids = set(pd.read_sql_query("""
        SELECT ki_id.id
        FROM keyword_identities ki_id
        JOIN keyword_instances ki ON ki.identity_id = ki_id.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE ki_id.match_type = 'EXACT'
          AND a.platform_account_id = ?
          AND a.platform = ?
    """, conn, params=(account_id, platform))['id'].tolist())

    # Build normalized_text -> has_exact_on_platform map
    exact_text_map = set(pd.read_sql_query("""
        SELECT DISTINCT ki_id.normalized_text
        FROM keyword_identities ki_id
        JOIN keyword_instances ki ON ki.identity_id = ki_id.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE ki_id.match_type = 'EXACT'
          AND a.platform_account_id = ?
          AND a.platform = ?
    """, conn, params=(account_id, platform))['normalized_text'].tolist())

    # Also check if exact version exists on the OTHER platform
    other_platform = 'microsoft' if platform == 'google' else 'google'
    other_account  = MS_ACCOUNT_ID if platform == 'google' else GOOGLE_ACCOUNT_ID
    exact_other_text = set(pd.read_sql_query("""
        SELECT DISTINCT ki_id.normalized_text
        FROM keyword_identities ki_id
        JOIN keyword_instances ki ON ki.identity_id = ki_id.id
        JOIN accounts a           ON ki.account_id  = a.id
        WHERE ki_id.match_type = 'EXACT'
          AND a.platform_account_id = ?
          AND a.platform = ?
    """, conn, params=(other_account, other_platform))['normalized_text'].tolist())

    # Step 4: Compute HHI and top query metrics per keyword
    if not query_conv_df.empty:
        conv_groups = query_conv_df[query_conv_df['q_revenue'] > 0].groupby(
            'identity_id')

        hhi_map         = {}
        top_q_rev_map   = {}
        top_q_text_map  = {}

        # Get query texts for top query lookup
        top_q_df = pd.read_sql_query("""
            SELECT ki.identity_id, sq.query_text,
                   SUM(sqp.primary_conv_value) AS q_revenue
            FROM search_query_performance sqp
            JOIN search_queries sq        ON sqp.query_id = sq.id
            JOIN keyword_instances ki     ON sqp.instance_id = ki.id
            JOIN accounts a               ON ki.account_id  = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = ?
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
              AND sqp.primary_conv_value > 0
            GROUP BY ki.identity_id, sq.id
            ORDER BY ki.identity_id, q_revenue DESC
        """, conn, params=(account_id, platform, str(REVENUE_START_DATE)))

        if not top_q_df.empty:
            top_q_by_kw = top_q_df.groupby('identity_id').first().reset_index()
            for _, r in top_q_by_kw.iterrows():
                top_q_text_map[r['identity_id']] = r['query_text']

        for iid, grp in conv_groups:
            total_rev = grp['q_revenue'].sum()
            if total_rev > 0:
                shares = grp['q_revenue'] / total_rev
                hhi_map[iid] = round((shares ** 2).sum(), 3)
                top_rev = grp['q_revenue'].max()
                top_q_rev_map[iid] = round(top_rev / total_rev * 100, 1)
    else:
        hhi_map = {}
        top_q_rev_map = {}
        top_q_text_map = {}

    # Step 5: Assemble final DataFrame
    kw_df['HHI'] = kw_df['identity_id'].map(hhi_map)
    kw_df['Top Query Rev %'] = kw_df['identity_id'].map(top_q_rev_map)
    kw_df['Top Search Query'] = kw_df['identity_id'].map(top_q_text_map)

    kw_df['Exact Exists (This Platform)'] = kw_df['keyword_text'].isin(
        exact_text_map).map({True: 'Yes', False: 'No'})
    kw_df['Exact Exists (Other Platform)'] = kw_df['keyword_text'].isin(
        exact_other_text).map({True: 'Yes', False: 'No'})
    kw_df['Is Exact Match'] = (kw_df['match_type'] == 'EXACT').map(
        {True: 'Yes', False: 'No'})

    kw_df['Waste Rate (%)'] = pd.to_numeric(
        kw_df['wasted_spend'] / kw_df['total_spend'] * 100,
        errors='coerce').round(1)
    kw_df['CPL ($)'] = pd.to_numeric(
        kw_df['total_spend'] / kw_df['total_conversions'],
        errors='coerce').round(2)
    kw_df['ROAS'] = pd.to_numeric(
        kw_df['total_revenue'] / kw_df['total_spend'],
        errors='coerce').round(2)
    kw_df['Geo Query %'] = pd.to_numeric(
        kw_df['geo_queries'] / kw_df['unique_queries'] * 100,
        errors='coerce').round(1)
    kw_df['Geo Revenue %'] = pd.to_numeric(
        kw_df['geo_revenue'] / kw_df['total_revenue'] * 100,
        errors='coerce').round(1)
    kw_df['Near Me Query %'] = pd.to_numeric(
        kw_df['near_me_queries'] / kw_df['unique_queries'] * 100,
        errors='coerce').round(1)

    # Step 6: Match type recommendation
    def recommend_match_type(row):
        hhi      = row.get('HHI') or 0
        top_pct  = row.get('Top Query Rev %') or 0
        waste    = row.get('Waste Rate (%)') or 0
        mt       = str(row.get('match_type', '')).upper()
        uniq     = row.get('unique_queries') or 1
        conv_q   = row.get('converting_queries') or 0
        is_exact = mt == 'EXACT'

        if is_exact:
            if waste > 50:
                return 'EXACT — review negatives (high waste for exact)'
            return 'EXACT — keep (performing)'

        # Concentration signals → EXACT
        if hhi >= 0.60 or top_pct >= 75:
            return 'Upgrade to EXACT — revenue concentrated in top query'

        # Moderate concentration → PHRASE
        if (hhi >= 0.25 or top_pct >= 50) and waste > 40:
            return 'Test PHRASE — moderate concentration, high waste'
        if hhi >= 0.25 or top_pct >= 50:
            return 'Consider PHRASE — moderate concentration'

        # Spread with high waste → PHRASE to filter junk
        if hhi < 0.10 and waste > 60:
            return 'Test PHRASE — high spread but excessive waste'

        # Low concentration, acceptable waste → keep
        if hhi < 0.10 and waste <= 40:
            return 'Keep BROAD — spread is generating value'

        return 'Monitor — review query mix'

    def recommend_rationale(row):
        hhi      = row.get('HHI') or 0
        top_pct  = row.get('Top Query Rev %') or 0
        waste    = row.get('Waste Rate (%)') or 0
        uniq     = row.get('unique_queries') or 1
        conv_q   = row.get('converting_queries') or 0
        parts = []
        if hhi:
            parts.append(f'HHI={hhi:.3f}')
        if top_pct:
            parts.append(f'top query={top_pct:.0f}% of revenue')
        parts.append(f'waste={waste:.0f}%')
        parts.append(f'{uniq} unique queries')
        parts.append(f'{conv_q} converting')
        return ' | '.join(parts)

    kw_df['Match Type Recommendation'] = kw_df.apply(recommend_match_type, axis=1)
    kw_df['Recommendation Rationale']  = kw_df.apply(recommend_rationale, axis=1)

    return kw_df


def fetch_query_coverage_gaps(conn, platform: str,
                               account_id: str) -> pd.DataFrame:
    """
    Identifies high-revenue search queries matched via BROAD or PHRASE
    on the given platform that have NO exact match keyword covering them
    on either platform. These are the immediate keyword expansion candidates.
    """
    other_platform = 'microsoft' if platform == 'google' else 'google'
    other_account  = MS_ACCOUNT_ID if platform == 'google' else GOOGLE_ACCOUNT_ID

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
                sq.modifier_home_purchase,
                -- Best match type this query was served by on this platform
                MIN(sqp.match_type_served) AS best_match_type,
                SUM(sqp.primary_conv_value) AS revenue,
                SUM(sqp.cost_micros) / 1e6  AS spend,
                SUM(sqp.clicks)             AS clicks,
                SUM(sqp.primary_conv_count) AS conversions
            FROM search_query_performance sqp
            JOIN search_queries sq       ON sqp.query_id = sq.id
            JOIN keyword_instances ki    ON sqp.instance_id = ki.id
            JOIN accounts a              ON ki.account_id = a.id
            WHERE a.platform_account_id = ?
              AND a.platform = ?
              AND sqp.device = 'ALL'
              AND sqp.report_date >= ?
              AND sqp.match_type_served != 'EXACT'
            GROUP BY sq.id
            HAVING revenue > 0
        ),
        exact_coverage_this AS (
            SELECT DISTINCT ki_id.normalized_text
            FROM keyword_identities ki_id
            JOIN keyword_instances ki ON ki.identity_id = ki_id.id
            JOIN accounts a           ON ki.account_id = a.id
            WHERE ki_id.match_type = 'EXACT'
              AND a.platform_account_id = ?
              AND a.platform = ?
        ),
        exact_coverage_other AS (
            SELECT DISTINCT ki_id.normalized_text
            FROM keyword_identities ki_id
            JOIN keyword_instances ki ON ki.identity_id = ki_id.id
            JOIN accounts a           ON ki.account_id = a.id
            WHERE ki_id.match_type = 'EXACT'
              AND a.platform_account_id = ?
              AND a.platform = ?
        )
        SELECT
            cq.query_text,
            cq.brand_category,
            cq.modifier_brand,
            cq.vertical,
            cq.modifier_geo,
            cq.modifier_near_me,
            cq.modifier_home_purchase,
            cq.best_match_type     AS served_match_type,
            cq.revenue,
            cq.spend,
            cq.clicks,
            cq.conversions,
            CASE WHEN et.normalized_text IS NOT NULL
                 THEN 'Yes' ELSE 'No' END AS exact_exists_this_platform,
            CASE WHEN eo.normalized_text IS NOT NULL
                 THEN 'Yes' ELSE 'No' END AS exact_exists_other_platform
        FROM converting_queries cq
        LEFT JOIN exact_coverage_this  et ON et.normalized_text = cq.normalized_text
        LEFT JOIN exact_coverage_other eo ON eo.normalized_text = cq.normalized_text
        WHERE et.normalized_text IS NULL  -- no exact match on this platform
        ORDER BY cq.revenue DESC
        LIMIT 200
    """, conn, params=(account_id, platform, str(REVENUE_START_DATE),
                       account_id, platform,
                       other_account, other_platform))


def fetch_geo_capture_summary(conn) -> pd.DataFrame:
    """
    Q3: What % of Microsoft BROAD/PHRASE/EXACT revenue comes from
    geo-modified queries? Broken down by match type and brand category.
    """
    return pd.read_sql_query("""
        SELECT
            ki_id.match_type,
            COALESCE(sq.brand_category, 'non_brand') AS brand_category,
            COUNT(DISTINCT sqp.query_id)              AS total_queries,
            COUNT(DISTINCT CASE WHEN sq.modifier_geo IS NOT NULL
                           THEN sqp.query_id END)     AS geo_queries,
            COUNT(DISTINCT CASE WHEN sq.modifier_near_me = 1
                           THEN sqp.query_id END)     AS near_me_queries,
            SUM(sqp.primary_conv_value)               AS total_revenue,
            SUM(CASE WHEN sq.modifier_geo IS NOT NULL
                     THEN sqp.primary_conv_value ELSE 0 END) AS geo_revenue,
            SUM(CASE WHEN sq.modifier_near_me = 1
                     THEN sqp.primary_conv_value ELSE 0 END) AS near_me_revenue,
            SUM(sqp.cost_micros) / 1e6                AS total_spend,
            SUM(CASE WHEN sq.modifier_geo IS NOT NULL
                     THEN sqp.cost_micros / 1e6 ELSE 0 END) AS geo_spend
        FROM search_query_performance sqp
        JOIN search_queries sq        ON sqp.query_id = sq.id
        JOIN keyword_instances ki     ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        JOIN accounts a               ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'microsoft'
          AND sqp.device = 'ALL'
          AND sqp.report_date >= ?
        GROUP BY ki_id.match_type, sq.brand_category
        ORDER BY ki_id.match_type, total_revenue DESC
    """, conn, params=(MS_ACCOUNT_ID, str(REVENUE_START_DATE)))


# ===========================================================================
# Sheet builders for Pass 2
# ===========================================================================

def build_match_type_analysis_sheet(mt_df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per keyword identity with HHI, top query %, waste, exact coverage,
    geo capture, and match type recommendation.
    """
    if mt_df.empty:
        return pd.DataFrame()

    col_order = [
        'keyword_text', 'match_type', 'brand_category', 'modifier_brand',
        'vertical', 'ad_group_instances',
        'total_revenue', 'total_spend', 'total_conversions', 'total_clicks',
        'CPL ($)', 'ROAS', 'Waste Rate (%)', 'wasted_spend',
        'unique_queries', 'converting_queries',
        'HHI', 'Top Query Rev %', 'Top Search Query',
        'Is Exact Match',
        'Exact Exists (This Platform)', 'Exact Exists (Other Platform)',
        'Geo Query %', 'Geo Revenue %', 'Near Me Query %',
        'Match Type Recommendation', 'Recommendation Rationale',
    ]

    result = mt_df.rename(columns={
        'keyword_text':       'Keyword',
        'match_type':         'Match Type',
        'brand_category':     'Brand Category',
        'modifier_brand':     'Brand',
        'vertical':           'Vertical',
        'ad_group_instances': 'Ad Group Instances',
        'total_revenue':      'Total Revenue ($)',
        'total_spend':        'Total Spend ($)',
        'total_conversions':  'Total Offline Convs',
        'total_clicks':       'Total Clicks',
        'wasted_spend':       'Wasted Spend ($)',
        'unique_queries':     'Unique Queries Matched',
        'converting_queries': 'Converting Queries',
    })

    col_order_renamed = [
        'Keyword', 'Match Type', 'Brand Category', 'Brand', 'Vertical',
        'Ad Group Instances',
        'Total Revenue ($)', 'Total Spend ($)', 'Total Offline Convs',
        'Total Clicks', 'CPL ($)', 'ROAS', 'Waste Rate (%)', 'Wasted Spend ($)',
        'Unique Queries Matched', 'Converting Queries',
        'HHI', 'Top Query Rev %', 'Top Search Query',
        'Is Exact Match',
        'Exact Exists (This Platform)', 'Exact Exists (Other Platform)',
        'Geo Query %', 'Geo Revenue %', 'Near Me Query %',
        'Match Type Recommendation', 'Recommendation Rationale',
    ]
    result = result[[c for c in col_order_renamed if c in result.columns]]
    return result.sort_values('Total Revenue ($)', ascending=False).round(2)


def build_query_coverage_gaps_sheet(gaps_df: pd.DataFrame,
                                    platform_label: str) -> pd.DataFrame:
    """
    Top 200 queries matched via BROAD/PHRASE that have no exact match
    keyword covering them. Sorted by revenue -- these are the candidates
    for EXACT keyword additions.
    """
    if gaps_df.empty:
        return pd.DataFrame()

    df = gaps_df.copy()
    df['CPL ($)'] = pd.to_numeric(
        df['spend'] / df['conversions'], errors='coerce').round(2)
    df['ROAS'] = pd.to_numeric(
        df['revenue'] / df['spend'], errors='coerce').round(2)

    # Add action recommendation
    def action(row):
        g_other = row.get('exact_exists_other_platform', 'No')
        rev = float(row.get('revenue', 0) or 0)
        cpl = float(row.get('CPL ($)') or 0)
        if g_other == 'Yes':
            return f'Add EXACT to {platform_label} — already EXACT on other platform'
        if rev >= 500:
            return 'High priority — add as EXACT keyword'
        if rev >= 100 and cpl > 0 and cpl < 45:
            return 'Add as EXACT — strong CPL, no exact coverage'
        if rev >= 50:
            return 'Consider EXACT — converting, no exact coverage'
        return 'Monitor — low revenue, not urgent'

    df['Recommended Action'] = df.apply(action, axis=1)

    result = df.rename(columns={
        'query_text':                 'Search Query',
        'brand_category':             'Brand Category',
        'modifier_brand':             'Brand',
        'vertical':                   'Vertical',
        'modifier_geo':               'Geo Modifier',
        'modifier_near_me':           'Near Me',
        'modifier_home_purchase':     'Home Purchase',
        'served_match_type':          'Served Match Type',
        'revenue':                    'Revenue ($)',
        'spend':                      'Spend ($)',
        'clicks':                     'Clicks',
        'conversions':                'Offline Convs',
        'exact_exists_this_platform': 'Exact on This Platform',
        'exact_exists_other_platform':'Exact on Other Platform',
    })

    col_order = [
        'Search Query', 'Brand Category', 'Brand', 'Vertical',
        'Geo Modifier', 'Near Me', 'Home Purchase',
        'Revenue ($)', 'Spend ($)', 'Offline Convs', 'Clicks',
        'CPL ($)', 'ROAS', 'Served Match Type',
        'Exact on This Platform', 'Exact on Other Platform',
        'Recommended Action',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values('Revenue ($)', ascending=False).round(2)


def build_geo_capture_sheet(geo_df: pd.DataFrame) -> pd.DataFrame:
    """
    Microsoft Q3: What % of revenue from each match type + brand category
    comes from geo-modified queries?
    """
    if geo_df.empty:
        return pd.DataFrame()

    df = geo_df.copy()
    cat_labels = {
        'carrier_approved':             'Carrier — Approved',
        'carrier_conditional':          'Carrier — Conditional',
        'carrier_restricted_requested': 'Carrier — Restricted (QW Request)',
        'carrier_restricted_partner':   'Carrier — Restricted (Partner)',
        'aggregator_lead_buyer':        'Aggregator — Lead Buyer',
        'aggregator_competitor':        'Aggregator — Competitor',
        'aggregator_unverified':        'Aggregator — Unverified',
        'non_brand':                    'Non-Brand / Generic',
    }
    df['brand_category'] = df['brand_category'].map(cat_labels).fillna(
        df['brand_category'])

    df['Geo Query %']    = pd.to_numeric(
        df['geo_queries'] / df['total_queries'] * 100,
        errors='coerce').round(1)
    df['Geo Revenue %']  = pd.to_numeric(
        df['geo_revenue'] / df['total_revenue'] * 100,
        errors='coerce').round(1)
    df['Geo Spend %']    = pd.to_numeric(
        df['geo_spend'] / df['total_spend'] * 100,
        errors='coerce').round(1)
    df['Near Me Query %'] = pd.to_numeric(
        df['near_me_queries'] / df['total_queries'] * 100,
        errors='coerce').round(1)
    df['Near Me Rev %']  = pd.to_numeric(
        df['near_me_revenue'] / df['total_revenue'] * 100,
        errors='coerce').round(1)

    result = df.rename(columns={
        'match_type':      'Match Type',
        'brand_category':  'Brand Category',
        'total_queries':   'Total Queries',
        'geo_queries':     'Geo Queries',
        'near_me_queries': 'Near Me Queries',
        'total_revenue':   'Total Revenue ($)',
        'geo_revenue':     'Geo Revenue ($)',
        'near_me_revenue': 'Near Me Revenue ($)',
        'total_spend':     'Total Spend ($)',
        'geo_spend':       'Geo Spend ($)',
    })

    col_order = [
        'Match Type', 'Brand Category',
        'Total Queries', 'Geo Queries', 'Geo Query %',
        'Near Me Queries', 'Near Me Query %',
        'Total Revenue ($)', 'Geo Revenue ($)', 'Geo Revenue %',
        'Near Me Revenue ($)', 'Near Me Rev %',
        'Total Spend ($)', 'Geo Spend ($)', 'Geo Spend %',
    ]
    result = result[[c for c in col_order if c in result.columns]]
    return result.sort_values(
        ['Match Type', 'Total Revenue ($)'], ascending=[True, False]
    ).round(2)


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

    print('Fetching YTD comparison data...')
    ytd_df = fetch_ytd_summary(conn)
    print(f'  {len(ytd_df):,} platform-day rows')

    print('Fetching Google top queries YTD...')
    g_top_df = fetch_top_queries_by_platform(
        conn, 'google', GOOGLE_ACCOUNT_ID, n=50)
    print(f'  {len(g_top_df):,} Google query-week rows')

    print('Fetching Microsoft top queries YTD...')
    ms_top_df = fetch_top_queries_by_platform(
        conn, 'microsoft', MS_ACCOUNT_ID, n=50)
    print(f'  {len(ms_top_df):,} Microsoft query-week rows')

    print('Fetching Google keyword efficiency...')
    g_kw_df = fetch_keyword_query_efficiency(
        conn, 'google', GOOGLE_ACCOUNT_ID)
    print(f'  {len(g_kw_df):,} Google keywords with spend')

    print('Fetching Microsoft keyword efficiency...')
    ms_kw_df = fetch_keyword_query_efficiency(
        conn, 'microsoft', MS_ACCOUNT_ID)
    print(f'  {len(ms_kw_df):,} Microsoft keywords with spend')

    print('Fetching brand YTD cross-platform query data (Jan 13+, both platforms)...')
    brand_ytd_df = fetch_brand_ytd_cross_platform(conn, start_date=str(REVENUE_START_DATE))
    print(f'  {len(brand_ytd_df):,} brand-platform rows (YTD fair window)')

    print('Fetching brand all-time cross-platform query data...')
    brand_alltime_df = fetch_brand_ytd_cross_platform(conn, start_date=None)
    print(f'  {len(brand_alltime_df):,} brand-platform rows (all-time)')

    print('Fetching Google brand top keywords...')
    g_bkw_df = fetch_brand_top_keywords(conn, 'google', GOOGLE_ACCOUNT_ID)
    print(f'  {len(g_bkw_df):,} Google brand-keyword rows')

    print('Fetching Microsoft brand top keywords...')
    ms_bkw_df = fetch_brand_top_keywords(conn, 'microsoft', MS_ACCOUNT_ID)
    print(f'  {len(ms_bkw_df):,} Microsoft brand-keyword rows')

    print('Fetching investment overlap data...')
    overlap_df = fetch_investment_overlap(conn)
    print(f'  {len(overlap_df):,} overlap rows')

    print('Fetching Google match type analysis...')
    g_mt_df = fetch_match_type_analysis(conn, 'google', GOOGLE_ACCOUNT_ID)
    print(f'  {len(g_mt_df):,} Google keyword rows')

    print('Fetching Microsoft match type analysis...')
    ms_mt_analysis_df = fetch_match_type_analysis(conn, 'microsoft', MS_ACCOUNT_ID)
    print(f'  {len(ms_mt_analysis_df):,} Microsoft keyword rows')

    print('Fetching Google coverage gaps...')
    g_gaps_df = fetch_query_coverage_gaps(conn, 'google', GOOGLE_ACCOUNT_ID)
    print(f'  {len(g_gaps_df):,} Google gap rows')

    print('Fetching Microsoft coverage gaps...')
    ms_gaps_df = fetch_query_coverage_gaps(conn, 'microsoft', MS_ACCOUNT_ID)
    print(f'  {len(ms_gaps_df):,} Microsoft gap rows')

    print('Fetching Microsoft geo capture summary...')
    geo_capture_df = fetch_geo_capture_summary(conn)
    print(f'  {len(geo_capture_df):,} geo capture rows')

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

    print('  YTD comparison...')
    ytd_comparison_df = build_ytd_summary_sheet(ytd_df)

    print('  Google top queries YTD...')
    g_top_result_df = build_top_queries_platform_sheet(g_top_df, 'Google')

    print('  Microsoft top queries YTD...')
    ms_top_result_df = build_top_queries_platform_sheet(ms_top_df, 'Microsoft')

    print('  Google keyword efficiency...')
    g_kw_eff_df = build_keyword_efficiency_sheet(g_kw_df, 'Google')

    print('  Microsoft keyword efficiency...')
    ms_kw_eff_df = build_keyword_efficiency_sheet(ms_kw_df, 'Microsoft')

    print('  Consolidation recommendations...')
    consolidation_df = build_consolidation_sheet(xp_df, g_kw_df, ms_kw_df)

    print('  Brand YTD (queries) -- fair window Jan 13+...')
    brand_ytd_result = build_brand_ytd_sheet(brand_ytd_df)

    print('  Brand all-time (queries) -- Google full, MS Jan 13+...')
    brand_alltime_result = build_brand_ytd_sheet(brand_alltime_df)

    print('  Google brand keywords...')
    g_bkw_result = build_brand_keyword_sheet(g_bkw_df, 'Google')

    print('  Microsoft brand keywords...')
    ms_bkw_result = build_brand_keyword_sheet(ms_bkw_df, 'Microsoft')

    print('  Investment overlap...')
    overlap_result = build_investment_overlap_sheet(overlap_df)

    print('  Google match type analysis...')
    g_mt_analysis = build_match_type_analysis_sheet(g_mt_df)

    print('  Microsoft match type analysis...')
    ms_mt_analysis = build_match_type_analysis_sheet(ms_mt_analysis_df)

    print('  Google coverage gaps...')
    g_gaps_result = build_query_coverage_gaps_sheet(g_gaps_df, 'Google')

    print('  Microsoft coverage gaps...')
    ms_gaps_result = build_query_coverage_gaps_sheet(ms_gaps_df, 'Microsoft')

    print('  Microsoft geo capture...')
    geo_capture_result = build_geo_capture_sheet(geo_capture_df)

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
        ytd_comparison_df.to_excel(writer,   sheet_name='YTD comparison',          index=False)
        g_top_result_df.to_excel(writer,     sheet_name='Google top queries YTD',  index=False)
        ms_top_result_df.to_excel(writer,    sheet_name='MS top queries YTD',      index=False)
        g_kw_eff_df.to_excel(writer,         sheet_name='Google kw efficiency',    index=False)
        ms_kw_eff_df.to_excel(writer,        sheet_name='MS kw efficiency',        index=False)
        consolidation_df.to_excel(writer,    sheet_name='Consolidation recs',      index=False)
        brand_ytd_result.to_excel(writer,    sheet_name='Brand YTD (queries)',     index=False)
        brand_alltime_result.to_excel(writer, sheet_name='Brand all-time (queries)', index=False)
        g_bkw_result.to_excel(writer,        sheet_name='Google brand keywords',   index=False)
        ms_bkw_result.to_excel(writer,       sheet_name='MS brand keywords',       index=False)
        overlap_result.to_excel(writer,       sheet_name='Investment overlap',       index=False)
        g_mt_analysis.to_excel(writer,        sheet_name='Google match type',        index=False)
        ms_mt_analysis.to_excel(writer,       sheet_name='MS match type analysis',   index=False)
        g_gaps_result.to_excel(writer,        sheet_name='Google coverage gaps',     index=False)
        ms_gaps_result.to_excel(writer,       sheet_name='MS coverage gaps',         index=False)
        geo_capture_result.to_excel(writer,   sheet_name='MS geo capture',           index=False)

    print('Applying formatting...')
    format_workbook(OUTPUT_FILE)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f'\nOutput saved : {OUTPUT_FILE}')
    print(f'File size    : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
