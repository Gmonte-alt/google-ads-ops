"""
analysis_campaign_keyword_roas.py
==================================
Campaign → Ad Group → Keyword ROAS Drag Analysis
Home Insurance — Google Ads Account 8886502147

PURPOSE
-------
Identifies keywords that are dragging down ad group ROAS over 30 / 60 / 90
day windows. For each under-performing keyword, surfaces the search queries
driving waste, zero-conversion query counts, converting query ROAS, and
each keyword's share of ad group impressions.

ROAS BUCKETS (vs ad group ROAS)
  Bucket 1 (Critical) : keyword ROAS ≤ 50% of ad group ROAS
  Bucket 2 (Watch)    : keyword ROAS between 50% and 70% of ad group ROAS
  Healthy             : keyword ROAS ≥ 70% of ad group ROAS

IMPRESSION SHARE NOTE
  Keyword-level impression share is NOT available in the Google Ads API
  (metrics.search_impression_share requires campaign grain). This script
  derives a proxy: keyword impressions / total campaign impressions for the
  same period. This reflects the keyword's share of its campaign's traffic,
  not Google's IS estimate.

DATA COVERAGE NOTE
  The script always prints the min and max dates available per table so you
  know exactly what period the data covers and whether a backfill is needed.

OUTPUT WORKSHEETS (one workbook per run)
  1. Data Coverage          -- min/max dates per table, row counts
  2. Campaign Summary       -- campaign-level ROAS for 30/60/90d
  3. Ad Group Summary       -- ad group-level ROAS for 30/60/90d
  4. Keyword ROAS (30d)     -- keyword drag analysis, 30 days
  5. Keyword ROAS (60d)     -- keyword drag analysis, 60 days
  6. Keyword ROAS (90d)     -- keyword drag analysis, 90 days
  7. Query Waste (30d)      -- search query waste detail for dragging keywords
  8. Query Waste (60d)
  9. Query Waste (90d)

USAGE
  conda activate paid-search
  python analysis/analysis_campaign_keyword_roas.py

  # Specific end date (default = yesterday)
  python analysis/analysis_campaign_keyword_roas.py --end-date 2026-04-05
"""

import sqlite3
import argparse
import os
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR  = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "campaign_keyword_roas_analysis.xlsx"

GOOGLE_ACCOUNT_ID = "8886502147"

# ROAS bucket thresholds (relative to the ad group's ROAS for the same window)
BUCKET1_THRESHOLD = 0.50   # ≤ 50%  of ag ROAS → Critical
BUCKET2_THRESHOLD = 0.70   # ≤ 70%  of ag ROAS → Watch

# Minimum spend to include a keyword in the drag analysis
MIN_KW_SPEND = 5.00

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")   # read-only, safe
    return conn


# ---------------------------------------------------------------------------
# Data coverage check
# ---------------------------------------------------------------------------

def fetch_data_coverage(conn) -> pd.DataFrame:
    """
    Returns min/max dates and row counts for all relevant tables.
    Use this to determine whether a backfill is needed before running
    the analysis.
    """
    cursor = conn.cursor()
    today  = date.today()

    tables = [
        ("keyword_performance_daily",
         "SELECT MIN(report_date), MAX(report_date), COUNT(*) "
         "FROM keyword_performance_daily kpd "
         "JOIN keyword_instances ki ON kpd.instance_id = ki.id "
         "JOIN accounts a ON ki.account_id = a.id "
         "WHERE a.platform_account_id = ? AND kpd.device = 'ALL'"),

        ("search_query_performance",
         "SELECT MIN(report_date), MAX(report_date), COUNT(*) "
         "FROM search_query_performance sqp "
         "JOIN keyword_instances ki ON sqp.instance_id = ki.id "
         "JOIN accounts a ON ki.account_id = a.id "
         "WHERE a.platform_account_id = ? AND a.platform = 'google' "
         "AND sqp.device = 'ALL'"),

        ("campaign_performance_daily",
         "SELECT MIN(date), MAX(date), COUNT(*) "
         "FROM campaign_performance_daily cpd "
         "JOIN campaigns c ON cpd.campaign_id = c.campaign_id "
         "WHERE cpd.account_id = ? AND cpd.device IN ('MOBILE','DESKTOP','TABLET')"),

        ("campaigns",
         "SELECT MIN(first_seen_date), MAX(last_synced_at), COUNT(*) "
         "FROM campaigns WHERE account_id = ?"),

        ("ad_groups",
         "SELECT MIN(first_seen_date), MAX(last_synced_at), COUNT(*) "
         "FROM ad_groups WHERE account_id = ?"),
    ]

    # Get internal account_id for campaign tables (uses integer FK)
    acct_row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (GOOGLE_ACCOUNT_ID,)
    ).fetchone()
    internal_id = str(acct_row['id']) if acct_row else GOOGLE_ACCOUNT_ID

    rows = []
    for table_name, sql in tables:
        # campaign tables use internal integer id; kw/sq tables use string id
        param = internal_id if table_name in ('campaign_performance_daily',
                                               'campaigns', 'ad_groups') \
                else GOOGLE_ACCOUNT_ID
        try:
            r = cursor.execute(sql, (param,)).fetchone()
            min_date = r[0] or 'N/A'
            max_date = r[1] or 'N/A'
            row_count = r[2]
        except Exception as e:
            min_date = max_date = f'ERROR: {e}'
            row_count = 0

        # Days gap between max_date and today
        try:
            gap = (today - date.fromisoformat(max_date)).days
            freshness = f'{gap}d ago'
        except Exception:
            freshness = 'N/A'

        rows.append({
            'Table':              table_name,
            'Min Date':           min_date,
            'Max Date':           max_date,
            'Days Since Last Sync': freshness,
            'Row Count':          row_count,
            'Backfill Needed?':   'YES — run --refresh 20' if gap > 3
                                   else 'No' if isinstance(gap, int) else 'Check',
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Campaign summary
# ---------------------------------------------------------------------------

def fetch_campaign_summary(conn, end_date: date, days: int) -> pd.DataFrame:
    """
    Campaign summary. Attempts campaign_performance_daily first (has IS data).
    Falls back to keyword_performance_daily aggregated by campaign_name when
    campaign_performance_daily is empty (before campaign sync scripts are run).
    """
    start_date = end_date - timedelta(days=days - 1)

    # Check if campaign_performance_daily has data
    cpd_count = conn.execute(
        "SELECT COUNT(*) FROM campaign_performance_daily"
    ).fetchone()[0]

    if cpd_count > 0:
        acct_row = conn.execute(
            "SELECT id FROM accounts WHERE platform_account_id = ? AND platform='google'",
            (GOOGLE_ACCOUNT_ID,)
        ).fetchone()
        internal_id = acct_row['id'] if acct_row else None

        return pd.read_sql_query("""
            SELECT
                ki_camp.campaign_id,
                ki_camp.campaign_name,
                c.status,
                c.bidding_strategy_type,
                c.parsed_product_type,
                c.parsed_geo_state,
                c.parsed_match_tier,
                c.parsed_intent_tier,
                c.daily_budget_micros / 1e6                    AS daily_budget,
                SUM(cpd.impressions)                           AS impressions,
                SUM(cpd.clicks)                                AS clicks,
                SUM(cpd.cost_micros) / 1e6                     AS spend,
                SUM(cpd.conversion_value)                      AS revenue,
                SUM(cpd.conversions)                           AS conversions,
                AVG(cpd.search_impression_share)               AS avg_impression_share,
                AVG(cpd.search_budget_lost_is)                 AS avg_budget_lost_is,
                AVG(cpd.search_rank_lost_is)                   AS avg_rank_lost_is
            FROM campaign_performance_daily cpd
            LEFT JOIN campaigns c ON cpd.campaign_id = c.campaign_id
            -- Get campaign_name from keyword_instances since campaigns table may be empty
            LEFT JOIN (
                SELECT DISTINCT campaign_id, campaign_name
                FROM keyword_instances
            ) ki_camp ON cpd.campaign_id = ki_camp.campaign_id
            WHERE cpd.account_id = ?
              AND cpd.device IN ('MOBILE', 'DESKTOP', 'TABLET')
              AND cpd.date BETWEEN ? AND ?
            GROUP BY cpd.campaign_id
            HAVING spend > 0
            ORDER BY revenue DESC
        """, conn, params=(str(internal_id), str(start_date), str(end_date)))
    else:
        # Fallback: aggregate keyword_performance_daily by campaign
        # No IS data available, but all revenue/spend/clicks data is here
        print(f"  NOTE: campaign_performance_daily is empty. "
              f"Run campaign_db_scripts/03_sync_campaign_performance.py for IS data.")
        return pd.read_sql_query("""
            SELECT
                ki.campaign_id,
                ki.campaign_name,
                NULL                AS status,
                NULL                AS bidding_strategy_type,
                c.parsed_product_type,
                c.parsed_geo_state,
                c.parsed_match_tier,
                c.parsed_intent_tier,
                c.daily_budget_micros / 1e6  AS daily_budget,
                SUM(kpd.impressions)         AS impressions,
                SUM(kpd.clicks)              AS clicks,
                SUM(kpd.cost_micros) / 1e6   AS spend,
                SUM(kpd.primary_conv_value)  AS revenue,
                SUM(kpd.primary_conv_count)  AS conversions,
                NULL  AS avg_impression_share,
                NULL  AS avg_budget_lost_is,
                NULL  AS avg_rank_lost_is
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki ON kpd.instance_id = ki.id
            JOIN accounts a           ON ki.account_id   = a.id
            LEFT JOIN campaigns c     ON ki.campaign_id  = c.campaign_id
            WHERE a.platform_account_id = ?
              AND kpd.device = 'ALL'
              AND kpd.report_date BETWEEN ? AND ?
            GROUP BY ki.campaign_id
            HAVING spend > 0
            ORDER BY revenue DESC
        """, conn, params=(GOOGLE_ACCOUNT_ID, str(start_date), str(end_date)))


# ---------------------------------------------------------------------------
# Ad group summary
# ---------------------------------------------------------------------------

def fetch_adgroup_summary(conn, end_date: date, days: int) -> pd.DataFrame:
    """
    Ad group summary using keyword_instances text columns for campaign/ag names.
    This works even when the campaigns and ad_groups tables are empty (i.e.
    before campaign_db_scripts/02_sync_campaigns_adgroups.py has been run).
    When those tables are populated, the LEFT JOINs pull in parsed dimensions.
    """
    start_date = end_date - timedelta(days=days - 1)

    return pd.read_sql_query("""
        SELECT
            ki.campaign_id,
            ki.campaign_name,
            ki.ad_group_id,
            ki.ad_group_name,
            -- Parsed dimensions from campaign table if available (NULL if not synced)
            c.parsed_product_type,
            c.parsed_geo_state,
            c.parsed_intent_tier,
            ag.status                                          AS ag_status,
            ag.parsed_theme,
            ag.parsed_match_tier,
            SUM(kpd.impressions)                               AS ag_impressions,
            SUM(kpd.clicks)                                    AS ag_clicks,
            SUM(kpd.cost_micros) / 1e6                         AS ag_spend,
            SUM(kpd.primary_conv_value)                        AS ag_revenue,
            SUM(kpd.primary_conv_count)                        AS ag_conversions
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki     ON kpd.instance_id  = ki.id
        JOIN accounts a               ON ki.account_id    = a.id
        -- LEFT JOINs: work even when campaign/ad_group tables are empty
        LEFT JOIN campaigns c         ON ki.campaign_id   = c.campaign_id
        LEFT JOIN ad_groups ag        ON ki.ad_group_id   = ag.ad_group_id
        WHERE a.platform_account_id = ?
          AND kpd.device = 'ALL'
          AND kpd.report_date BETWEEN ? AND ?
        GROUP BY ki.ad_group_id, ki.campaign_id
        HAVING ag_spend > 0
        ORDER BY ag_revenue DESC
    """, conn, params=(GOOGLE_ACCOUNT_ID, str(start_date), str(end_date)))


# ---------------------------------------------------------------------------
# Keyword ROAS drag analysis
# ---------------------------------------------------------------------------

def fetch_keyword_roas(conn, end_date: date, days: int) -> pd.DataFrame:
    """
    Returns one row per keyword × ad group with performance metrics and
    ROAS bucket classification.

    JOIN STRATEGY — ad_group_id as the bridge, not campaign_id:
    ──────────────────────────────────────────────────────────────
    In this account, ad groups are shared across campaigns. A keyword
    (criterion_id) is owned by one campaign in keyword_instances, but
    the same ad group runs in multiple campaigns simultaneously (e.g.
    TIER TWO, NORTH CAROLINA, FLORIDA CPA all run the same ad groups).

    Joining kpd → ki on campaign_id would only return the 2 keywords
    that keyword_instances attributes to TIER TWO directly.

    Joining kpd → ki on ad_group_id returns ALL keywords that ran in
    an ad group, then agpd anchors those ad groups to their campaigns.
    This produces the full 178-keyword view matching the Google Ads UI.

    The trade-off: a keyword that runs in the same ad group across
    multiple campaigns will appear once per campaign it belongs to in
    agpd. Revenue and spend are correctly attributed per ad group per
    campaign via the agpd grain.
    """
    start_date = end_date - timedelta(days=days - 1)

    # Get the set of ad_group_ids that belong to this account with spend
    # Use agpd as the campaign anchor -- it has correct campaign attribution
    if conn.execute(
        "SELECT COUNT(*) FROM ad_group_performance_daily WHERE account_id = ?",
        (GOOGLE_ACCOUNT_ID,)
    ).fetchone()[0] > 0:
        # Primary path: use agpd to resolve ad_group → campaign mapping
        kw_df = pd.read_sql_query("""
            SELECT
                agpd.campaign_id,
                agpd.campaign_name,
                c.parsed_product_type,
                c.parsed_geo_state,
                c.parsed_intent_tier         AS campaign_intent,
                ki.ad_group_id,
                COALESCE(ag.ad_group_name,
                         ki.ad_group_name)   AS ad_group_name,
                ag.parsed_match_tier,
                ki_id.normalized_text        AS keyword,
                ki_id.match_type,
                ki_id.modifier_brand         AS brand,
                ki_id.brand_category,
                ki_id.vertical,
                ki.criterion_id,
                ki.id                        AS instance_id,
                SUM(kpd.impressions)         AS kw_impressions,
                SUM(kpd.clicks)              AS kw_clicks,
                SUM(kpd.cost_micros) / 1e6   AS kw_spend,
                SUM(kpd.primary_conv_value)  AS kw_revenue,
                SUM(kpd.primary_conv_count)  AS kw_conversions
            FROM keyword_performance_daily kpd
            -- Join kpd → ki on ad_group_id, not campaign_id
            -- This surfaces all keywords in an ad group regardless of
            -- which campaign owns the criterion_id in keyword_instances
            JOIN keyword_instances ki
                ON  kpd.instance_id   = ki.id
                AND ki.ad_group_id    IN (
                    SELECT DISTINCT ad_group_id
                    FROM ad_group_performance_daily
                    WHERE account_id = ?
                      AND date BETWEEN ? AND ?
                      AND device = 'ALL'
                )
            JOIN keyword_identities ki_id ON ki.identity_id    = ki_id.id
            JOIN accounts a               ON ki.account_id     = a.id
            -- agpd provides the campaign assignment for this ad group
            JOIN (
                SELECT DISTINCT ad_group_id, campaign_id, campaign_name
                FROM ad_group_performance_daily
                WHERE account_id = ?
                  AND date BETWEEN ? AND ?
                  AND device = 'ALL'
            ) agpd ON ki.ad_group_id = agpd.ad_group_id
            LEFT JOIN campaigns c  ON agpd.campaign_id  = c.campaign_id
            LEFT JOIN ad_groups ag ON ki.ad_group_id    = ag.ad_group_id
            WHERE a.platform_account_id = ?
              AND kpd.device = 'ALL'
              AND kpd.report_date BETWEEN ? AND ?
            GROUP BY ki.id, agpd.campaign_id
            HAVING kw_spend > 0
        """, conn, params=(
            GOOGLE_ACCOUNT_ID, str(start_date), str(end_date),
            GOOGLE_ACCOUNT_ID, str(start_date), str(end_date),
            GOOGLE_ACCOUNT_ID, str(start_date), str(end_date),
        ))
    else:
        # Fallback: original campaign_id join (used before agpd is populated)
        kw_df = pd.read_sql_query("""
            SELECT
                ki.campaign_id,
                COALESCE(c.campaign_name, ki.campaign_name)   AS campaign_name,
                c.parsed_product_type,
                c.parsed_geo_state,
                c.parsed_intent_tier        AS campaign_intent,
                ki.ad_group_id,
                COALESCE(ag.ad_group_name, ki.ad_group_name)  AS ad_group_name,
                ag.parsed_match_tier,
                ki_id.normalized_text       AS keyword,
                ki_id.match_type,
                ki_id.modifier_brand        AS brand,
                ki_id.brand_category,
                ki_id.vertical,
                ki.criterion_id,
                ki.id                       AS instance_id,
                SUM(kpd.impressions)        AS kw_impressions,
                SUM(kpd.clicks)             AS kw_clicks,
                SUM(kpd.cost_micros) / 1e6  AS kw_spend,
                SUM(kpd.primary_conv_value) AS kw_revenue,
                SUM(kpd.primary_conv_count) AS kw_conversions
            FROM keyword_performance_daily kpd
            JOIN keyword_instances ki     ON kpd.instance_id  = ki.id
            JOIN keyword_identities ki_id ON ki.identity_id   = ki_id.id
            JOIN accounts a               ON ki.account_id    = a.id
            LEFT JOIN ad_groups ag        ON ki.ad_group_id   = ag.ad_group_id
            LEFT JOIN campaigns c         ON ki.campaign_id   = c.campaign_id
            WHERE a.platform_account_id = ?
              AND kpd.device = 'ALL'
              AND kpd.report_date BETWEEN ? AND ?
            GROUP BY ki.id
            HAVING kw_spend > 0
        """, conn, params=(GOOGLE_ACCOUNT_ID, str(start_date), str(end_date)))

    if kw_df.empty:
        return pd.DataFrame()

    # Ad group totals for ROAS comparison
    ag_totals = kw_df.groupby(['ad_group_id','campaign_id']).agg(
        ag_revenue=('kw_revenue', 'sum'),
        ag_spend=('kw_spend', 'sum'),
        ag_impressions=('kw_impressions', 'sum'),
    ).reset_index()
    ag_totals['ag_roas'] = np.where(
        ag_totals['ag_spend'] > 0,
        ag_totals['ag_revenue'] / ag_totals['ag_spend'], np.nan)

    # Campaign impression totals
    camp_impr = kw_df.groupby('campaign_id')['kw_impressions'].sum().reset_index()
    camp_impr.columns = ['campaign_id', 'camp_impressions']

    kw_df = kw_df.merge(ag_totals[['ad_group_id','campaign_id','ag_roas',
                                    'ag_spend','ag_revenue','ag_impressions']],
                         on=['ad_group_id','campaign_id'], how='left')
    kw_df = kw_df.merge(camp_impr, on='campaign_id', how='left')

    # Derived metrics
    kw_df['kw_roas'] = np.where(kw_df['kw_spend'] > 0,
                                  kw_df['kw_revenue'] / kw_df['kw_spend'], np.nan)
    kw_df['kw_cpl']  = np.where(kw_df['kw_conversions'] > 0,
                                  kw_df['kw_spend'] / kw_df['kw_conversions'], np.nan)
    kw_df['kw_ctr']  = np.where(kw_df['kw_impressions'] > 0,
                                  kw_df['kw_clicks'] / kw_df['kw_impressions'] * 100, np.nan)
    kw_df['roas_vs_ag_pct'] = np.where(
        kw_df['ag_roas'] > 0,
        kw_df['kw_roas'] / kw_df['ag_roas'] * 100, np.nan)

    kw_df['kw_impr_share_of_campaign'] = np.where(
        kw_df['camp_impressions'] > 0,
        kw_df['kw_impressions'] / kw_df['camp_impressions'] * 100, np.nan)

    kw_df['kw_impr_share_of_ag'] = np.where(
        kw_df['ag_impressions'] > 0,
        kw_df['kw_impressions'] / kw_df['ag_impressions'] * 100, np.nan)

    def assign_bucket(row):
        pct   = row.get('roas_vs_ag_pct')
        spend = row.get('kw_spend', 0)
        if spend < MIN_KW_SPEND:
            return 'Insufficient spend'
        if pct is None or np.isnan(pct):
            return 'No conversions'
        if pct <= BUCKET1_THRESHOLD * 100:
            return 'Critical (≤50% of AG ROAS)'
        if pct <= BUCKET2_THRESHOLD * 100:
            return 'Watch (51–70% of AG ROAS)'
        return 'Healthy (>70% of AG ROAS)'

    kw_df['roas_bucket'] = kw_df.apply(assign_bucket, axis=1)

    return kw_df.sort_values(['campaign_name','ad_group_name','kw_spend'],
                              ascending=[True,True,False]).reset_index(drop=True)

def fetch_query_waste(conn, end_date: date, days: int,
                      kw_df: pd.DataFrame) -> pd.DataFrame:
    """
    For keywords in the Critical or Watch buckets, returns all matched
    search queries with:
    - zero-conversion query count and spend
    - converting query count, revenue, and ROAS
    - total unique queries matched
    """
    start_date = end_date - timedelta(days=days - 1)

    # Only pull queries for underperforming keywords
    drag_instances = kw_df[kw_df['roas_bucket'].isin(
        ['Critical (≤50% of AG ROAS)', 'Watch (51–70% of AG ROAS)',
         'No conversions']
    )]['instance_id'].tolist()

    if not drag_instances:
        return pd.DataFrame()

    # SQLite IN clause limit -- chunk if needed
    CHUNK = 500
    all_rows = []
    for i in range(0, len(drag_instances), CHUNK):
        chunk = drag_instances[i:i+CHUNK]
        placeholders = ','.join('?' * len(chunk))
        rows = pd.read_sql_query(f"""
            SELECT
                sqp.instance_id,
                sq.query_text,
                sq.modifier_brand           AS query_brand,
                sq.vertical                 AS query_vertical,
                sq.modifier_geo             AS query_geo,
                sq.modifier_near_me         AS near_me,
                sq.modifier_home_purchase   AS home_purchase,
                sqp.match_type_served,
                SUM(sqp.clicks)             AS clicks,
                SUM(sqp.cost_micros) / 1e6  AS spend,
                SUM(sqp.primary_conv_count) AS conversions,
                SUM(sqp.primary_conv_value) AS revenue
            FROM search_query_performance sqp
            JOIN search_queries sq ON sqp.query_id = sq.id
            WHERE sqp.instance_id IN ({placeholders})
              AND sqp.device = 'ALL'
              AND sqp.report_date BETWEEN ? AND ?
            GROUP BY sqp.instance_id, sq.id
            ORDER BY spend DESC
        """, conn, params=chunk + [str(start_date), str(end_date)])
        all_rows.append(rows)

    if not all_rows:
        return pd.DataFrame()

    q_df = pd.concat(all_rows, ignore_index=True)

    # Join keyword context from kw_df
    kw_context = kw_df[['instance_id','campaign_name','ad_group_name',
                         'keyword','match_type','brand','brand_category',
                         'roas_bucket','kw_roas','ag_roas']].copy()
    q_df = q_df.merge(kw_context, on='instance_id', how='left')

    # Per-keyword query summary
    q_df['is_zero_conv'] = (q_df['conversions'] == 0).astype(int)
    q_df['roas']         = np.where(q_df['spend'] > 0,
                                     q_df['revenue'] / q_df['spend'], np.nan)
    q_df['cpl']          = np.where(q_df['conversions'] > 0,
                                     q_df['spend'] / q_df['conversions'], np.nan)

    return q_df.sort_values(['campaign_name','ad_group_name','keyword','spend'],
                             ascending=[True,True,True,False]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------

def build_coverage_sheet(df: pd.DataFrame) -> pd.DataFrame:
    return df


def build_campaign_summary_sheet(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d['ROAS']    = pd.to_numeric(d['revenue'] / d['spend'],
                                   errors='coerce').round(2)
    d['CPL ($)'] = pd.to_numeric(d['spend'] / d['conversions'],
                                   errors='coerce').round(2)
    d['CTR %']   = pd.to_numeric(d['clicks'] / d['impressions'] * 100,
                                   errors='coerce').round(2)
    d['CPC ($)'] = pd.to_numeric(d['spend'] / d['clicks'],
                                   errors='coerce').round(2)
    d['Avg IS %'] = (d['avg_impression_share'] * 100).round(1)
    d['Budget Lost IS (Budget) %'] = (d['avg_budget_lost_is'] * 100).round(1)
    d['Budget Lost IS (Rank) %']   = (d['avg_rank_lost_is'] * 100).round(1)
    d['spend'] = d['spend'].round(2)
    d['revenue'] = d['revenue'].round(2)

    col_order = [
        'campaign_name','status','parsed_product_type','parsed_geo_state',
        'parsed_match_tier','parsed_intent_tier',
        'daily_budget',
        'impressions','clicks','CTR %','CPC ($)','spend',
        'conversions','revenue','ROAS','CPL ($)',
        'Avg IS %','Budget Lost IS (Budget) %','Budget Lost IS (Rank) %',
    ]
    d = d.rename(columns={
        'campaign_name':       'Campaign',
        'status':              'Status',
        'parsed_product_type': 'Product',
        'parsed_geo_state':    'Geo',
        'parsed_match_tier':   'Match Tier',
        'parsed_intent_tier':  'Intent Tier',
        'daily_budget':        'Daily Budget ($)',
        'impressions':         'Impressions',
        'clicks':              'Clicks',
        'spend':               'Spend ($)',
        'conversions':         'Offline Convs',
        'revenue':             'Revenue ($)',
    })
    col_order_renamed = [
        'Campaign','Status','Product','Geo','Match Tier','Intent Tier',
        'Daily Budget ($)',
        'Impressions','Clicks','CTR %','CPC ($)','Spend ($)',
        'Offline Convs','Revenue ($)','ROAS','CPL ($)',
        'Avg IS %','Budget Lost IS (Budget) %','Budget Lost IS (Rank) %',
    ]
    return d[[c for c in col_order_renamed if c in d.columns]].round(2)


def build_adgroup_summary_sheet(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d['ROAS']    = pd.to_numeric(d['ag_revenue'] / d['ag_spend'],
                                   errors='coerce').round(2)
    d['CPL ($)'] = pd.to_numeric(d['ag_spend'] / d['ag_conversions'],
                                   errors='coerce').round(2)
    d['CTR %']   = pd.to_numeric(d['ag_clicks'] / d['ag_impressions'] * 100,
                                   errors='coerce').round(2)

    d = d.rename(columns={
        'campaign_name':       'Campaign',
        'parsed_product_type': 'Product',
        'parsed_geo_state':    'Geo',
        'parsed_intent_tier':  'Campaign Intent',
        'ad_group_name':       'Ad Group',
        'ag_status':           'Status',
        'parsed_theme':        'AG Theme',
        'parsed_match_tier':   'AG Match Tier',
        'ag_impressions':      'Impressions',
        'ag_clicks':           'Clicks',
        'ag_spend':            'Spend ($)',
        'ag_revenue':          'Revenue ($)',
        'ag_conversions':      'Offline Convs',
        'ag_roas':             'ROAS',
        'ag_cpl':              'CPL ($)',
    })
    col_order = [
        'Campaign','Product','Geo','Campaign Intent',
        'Ad Group','Status','AG Theme','AG Match Tier',
        'Impressions','Clicks','CTR %','Spend ($)',
        'Offline Convs','Revenue ($)','ROAS','CPL ($)',
    ]
    return d[[c for c in col_order if c in d.columns]].sort_values(
        'Revenue ($)', ascending=False).round(2)


def build_keyword_roas_sheet(kw_df: pd.DataFrame, days: int) -> pd.DataFrame:
    if kw_df.empty:
        return pd.DataFrame()
    d = kw_df.copy()
    d = d.rename(columns={
        'campaign_name':               'Campaign',
        'parsed_product_type':         'Product',
        'parsed_geo_state':            'Geo',
        'campaign_intent':             'Campaign Intent',
        'ad_group_name':               'Ad Group',
        'parsed_match_tier':           'AG Match Tier',
        'keyword':                     'Keyword',
        'match_type':                  'Match Type',
        'brand':                       'Brand',
        'brand_category':              'Brand Category',
        'vertical':                    'Vertical',
        'kw_impressions':              'KW Impressions',
        'kw_clicks':                   'KW Clicks',
        'kw_spend':                    'KW Spend ($)',
        'kw_revenue':                  'KW Revenue ($)',
        'kw_conversions':              'KW Offline Convs',
        'kw_roas':                     'KW ROAS',
        'kw_cpl':                      'KW CPL ($)',
        'kw_ctr':                      'KW CTR %',
        'ag_roas':                     'AG ROAS',
        'ag_spend':                    'AG Spend ($)',
        'ag_revenue':                  'AG Revenue ($)',
        'roas_vs_ag_pct':              'KW ROAS % of AG ROAS',
        'kw_impr_share_of_ag':         'Impr Share of AG %',
        'kw_impr_share_of_campaign':   'Impr Share of Campaign %',
        'roas_bucket':                 'ROAS Bucket',
    })

    col_order = [
        'ROAS Bucket',
        'Campaign','Product','Geo','Campaign Intent',
        'Ad Group','AG Match Tier',
        'Keyword','Match Type','Brand','Brand Category','Vertical',
        'KW Revenue ($)','KW Spend ($)','KW Offline Convs',
        'KW ROAS','AG ROAS','KW ROAS % of AG ROAS',
        'KW CPL ($)','KW CTR %','KW Impressions','KW Clicks',
        'Impr Share of AG %','Impr Share of Campaign %',
        'AG Spend ($)','AG Revenue ($)',
    ]
    result = d[[c for c in col_order if c in d.columns]]
    # Sort: Critical first, then Watch, then rest; within each by spend desc
    bucket_order = {
        'Critical (≤50% of AG ROAS)': 0,
        'Watch (51–70% of AG ROAS)':  1,
        'No conversions':             2,
        'Healthy (>70% of AG ROAS)':  3,
        'Insufficient spend':         4,
    }
    result = result.copy()
    result['_sort'] = result['ROAS Bucket'].map(bucket_order).fillna(9)
    result = result.sort_values(['_sort','KW Spend ($)'],
                                 ascending=[True, False]
                                ).drop(columns=['_sort'])
    return result.round(2).reset_index(drop=True)


def build_query_waste_sheet(q_df: pd.DataFrame, days: int) -> pd.DataFrame:
    if q_df.empty:
        return pd.DataFrame()
    d = q_df.copy()
    d = d.rename(columns={
        'campaign_name':    'Campaign',
        'ad_group_name':    'Ad Group',
        'keyword':          'Keyword',
        'match_type':       'Match Type',
        'brand':            'KW Brand',
        'brand_category':   'Brand Category',
        'roas_bucket':      'KW ROAS Bucket',
        'kw_roas':          'KW ROAS',
        'ag_roas':          'AG ROAS',
        'query_text':       'Search Query',
        'query_brand':      'Query Brand',
        'query_vertical':   'Query Vertical',
        'query_geo':        'Query Geo',
        'near_me':          'Near Me',
        'home_purchase':    'Home Purchase',
        'match_type_served':'Match Type Served',
        'clicks':           'Query Clicks',
        'spend':            'Query Spend ($)',
        'conversions':      'Query Offline Convs',
        'revenue':          'Query Revenue ($)',
        'roas':             'Query ROAS',
        'cpl':              'Query CPL ($)',
        'is_zero_conv':     'Zero Conv (1=yes)',
    })

    col_order = [
        'KW ROAS Bucket',
        'Campaign','Ad Group',
        'Keyword','Match Type','KW Brand','Brand Category',
        'KW ROAS','AG ROAS',
        'Search Query','Query Brand','Query Vertical','Query Geo',
        'Near Me','Home Purchase','Match Type Served',
        'Query Revenue ($)','Query Spend ($)','Query Offline Convs',
        'Query ROAS','Query CPL ($)','Query Clicks',
        'Zero Conv (1=yes)',
    ]
    return d[[c for c in col_order if c in d.columns]].round(2).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path):
    wb = load_workbook(path)

    # Color scheme
    HDR_FILLS = {
        'Data Coverage':          'E2EFDA',   # green-tinted
        'Campaign Summary':       '1F4E79',
        'Ad Group Summary':       '2E75B6',
        'Keyword ROAS (30d)':     '7030A0',
        'Keyword ROAS (60d)':     '7030A0',
        'Keyword ROAS (90d)':     '7030A0',
        'Query Waste (30d)':      'C00000',
        'Query Waste (60d)':      'C00000',
        'Query Waste (90d)':      'C00000',
    }

    # ROAS bucket fill colors for data rows
    BUCKET_FILLS = {
        'Critical (≤50% of AG ROAS)': PatternFill('solid', fgColor='FFD7D7'),
        'Watch (51–70% of AG ROAS)':  PatternFill('solid', fgColor='FFF2CC'),
        'No conversions':             PatternFill('solid', fgColor='F2F2F2'),
        'Healthy (>70% of AG ROAS)':  PatternFill('solid', fgColor='E2EFDA'),
    }

    thin = Side(style='thin', color='CCCCCC')

    for ws in wb.worksheets:
        hdr_color = HDR_FILLS.get(ws.title, '1F4E79')
        hdr_fill  = PatternFill('solid', fgColor=hdr_color)
        white_bold = Font(color='FFFFFF', bold=True, size=10)
        if hdr_color in ('E2EFDA',):
            white_bold = Font(color='000000', bold=True, size=10)

        max_row = ws.max_row
        max_col = ws.max_column

        # Header row
        for cell in ws[1]:
            cell.fill      = hdr_fill
            cell.font      = white_bold
            cell.alignment = Alignment(
                horizontal='center', vertical='center', wrap_text=True)
        ws.row_dimensions[1].height = 42

        # Find ROAS bucket column index if present
        bucket_col = None
        for col_idx in range(1, max_col + 1):
            if str(ws.cell(1, col_idx).value or '').strip() == 'ROAS Bucket' or \
               str(ws.cell(1, col_idx).value or '').strip() == 'KW ROAS Bucket':
                bucket_col = col_idx
                break

        # Data rows -- alternating shading + bucket coloring
        for row_idx in range(2, max_row + 1):
            bucket_val = str(ws.cell(row_idx, bucket_col).value or '') \
                if bucket_col else ''

            bucket_fill = BUCKET_FILLS.get(bucket_val)

            for col_idx in range(1, max_col + 1):
                cell = ws.cell(row_idx, col_idx)
                if bucket_fill:
                    cell.fill = bucket_fill
                elif row_idx % 2 == 0:
                    cell.fill = PatternFill('solid', fgColor='F7FBFF')
                cell.alignment = Alignment(vertical='center')
                cell.border    = Border(bottom=Side(style='thin',
                                                     color='E0E0E0'))

        # Column widths
        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max(
                (len(str(c.value)) for c in col if c.value),
                default=10
            )
            ws.column_dimensions[col_letter].width = min(max_len + 3, 40)

        # Freeze panes
        ws.freeze_panes = 'A2'

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Campaign → Keyword ROAS drag analysis'
    )
    parser.add_argument(
        '--end-date', default=None,
        help='Analysis end date YYYY-MM-DD (default: yesterday)'
    )
    args = parser.parse_args()

    end_date = (date.fromisoformat(args.end_date)
                if args.end_date
                else date.today() - timedelta(days=1))

    print('=' * 65)
    print('Campaign Keyword ROAS Drag Analysis — Home Insurance Google')
    print(f'  Account  : {GOOGLE_ACCOUNT_ID}')
    print(f'  End date : {end_date}')
    print(f'  Windows  : 30 / 60 / 90 days')
    print('=' * 65)

    conn = get_conn()

    # Data coverage -- always run first
    print('\n[1] Checking data coverage...')
    coverage_df = fetch_data_coverage(conn)
    for _, r in coverage_df.iterrows():
        flag = '⚠ ' if r['Backfill Needed?'].startswith('YES') else '  '
        print(f"  {flag}{r['Table']:<35} "
              f"{r['Min Date']} → {r['Max Date']}  "
              f"({r['Days Since Last Sync']})  "
              f"rows={r['Row Count']:,}")

    print('\n[2] Fetching campaign summaries...')
    camp_30  = fetch_campaign_summary(conn, end_date, 30)
    camp_60  = fetch_campaign_summary(conn, end_date, 60)
    camp_90  = fetch_campaign_summary(conn, end_date, 90)
    print(f'  30d: {len(camp_30)} campaigns | '
          f'60d: {len(camp_60)} | 90d: {len(camp_90)}')

    print('\n[3] Fetching ad group summaries...')
    ag_30  = fetch_adgroup_summary(conn, end_date, 30)
    ag_60  = fetch_adgroup_summary(conn, end_date, 60)
    ag_90  = fetch_adgroup_summary(conn, end_date, 90)
    print(f'  30d: {len(ag_30)} ad groups | '
          f'60d: {len(ag_60)} | 90d: {len(ag_90)}')

    print('\n[4] Fetching keyword ROAS data...')
    kw_30 = fetch_keyword_roas(conn, end_date, 30)
    kw_60 = fetch_keyword_roas(conn, end_date, 60)
    kw_90 = fetch_keyword_roas(conn, end_date, 90)
    for days, kw in [(30, kw_30), (60, kw_60), (90, kw_90)]:
        if not kw.empty:
            crit = (kw['roas_bucket'] == 'Critical (≤50% of AG ROAS)').sum()
            watch = (kw['roas_bucket'] == 'Watch (51–70% of AG ROAS)').sum()
            no_conv = (kw['roas_bucket'] == 'No conversions').sum()
            print(f'  {days}d: {len(kw)} keywords  |  '
                  f'Critical={crit}  Watch={watch}  NoConv={no_conv}')

    print('\n[5] Fetching search query waste detail...')
    q_30 = fetch_query_waste(conn, end_date, 30, kw_30)
    q_60 = fetch_query_waste(conn, end_date, 60, kw_60)
    q_90 = fetch_query_waste(conn, end_date, 90, kw_90)
    print(f'  30d: {len(q_30)} query rows | '
          f'60d: {len(q_60)} | 90d: {len(q_90)}')

    conn.close()

    print('\n[6] Building sheets...')
    sheets = {
        'Data Coverage':     build_coverage_sheet(coverage_df),
        'Campaign Summary':  build_campaign_summary_sheet(
                                pd.concat([
                                    camp_30.assign(Window='30d'),
                                    camp_60.assign(Window='60d'),
                                    camp_90.assign(Window='90d'),
                                ], ignore_index=True), 90),
        'Ad Group Summary':  build_adgroup_summary_sheet(
                                pd.concat([
                                    ag_30.assign(Window='30d'),
                                    ag_60.assign(Window='60d'),
                                    ag_90.assign(Window='90d'),
                                ], ignore_index=True), 90),
        'Keyword ROAS (30d)': build_keyword_roas_sheet(kw_30, 30),
        'Keyword ROAS (60d)': build_keyword_roas_sheet(kw_60, 60),
        'Keyword ROAS (90d)': build_keyword_roas_sheet(kw_90, 90),
        'Query Waste (30d)':  build_query_waste_sheet(q_30, 30),
        'Query Waste (60d)':  build_query_waste_sheet(q_60, 60),
        'Query Waste (90d)':  build_query_waste_sheet(q_90, 90),
    }

    print('\n[7] Writing Excel output...')
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
            else:
                pd.DataFrame([{'Note': f'No data for {sheet_name}'}]).to_excel(
                    writer, sheet_name=sheet_name, index=False)

    print('[8] Applying formatting...')
    format_workbook(OUTPUT_FILE)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f'\nOutput : {OUTPUT_FILE}')
    print(f'Size   : {size_kb:.1f} KB')
    print('\nDone.')

if __name__ == '__main__':
    main()
