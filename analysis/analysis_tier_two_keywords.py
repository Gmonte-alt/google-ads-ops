"""
analysis_tier_two_keywords.py
==============================
Keyword performance analysis for:
  Campaign : HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
  Account  : Home Insurance (account_id = 1)
  Windows  : 30 / 60 / 90 days

DATA STRATEGY
─────────────
Previously this script used a Google Ads CSV export as the authoritative
keyword list for TIER TWO. The database is now fully populated, so the CSV
dependency has been removed entirely.

keyword_instances scoped to account_id=1 and campaign_id='23496334752'
is the authoritative keyword source. It defines exactly which
(keyword_text, match_type, ad_group) combinations belong to this campaign.

campaign_name, campaign_status, ad_group_name, and ad_group_status all
come from the DB (campaigns / ad_groups tables), not from a CSV.

RUN
───
  conda activate paid-search
  python analysis/analysis_tier_two_keywords.py [--end-date YYYY-MM-DD]
"""

import re
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta

# ── Configuration ─────────────────────────────────────────────────
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_FILE = OUTPUT_DIR / "tier_two_keyword_analysis.xlsx"

ACCOUNT_ID           = 1                 # Home Insurance internal account_id
TIER_TWO_CAMPAIGN_ID = "23496334752"     # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
END_DATE             = date.today() - timedelta(days=1)

SQLITE_MAX_VARS = 999

# ROAS bucket thresholds
CRITICAL_ROAS = 0.50
WATCH_ROAS    = 0.70
MIN_SPEND     = 5.00

# US state geo-detection pattern (full names only)
GEO_PATTERN = (
    r'\b(?:alabama|alaska|arizona|arkansas|california|colorado|'
    r'connecticut|delaware|florida|georgia|hawaii|idaho|illinois|'
    r'indiana|iowa|kansas|kentucky|louisiana|maine|maryland|'
    r'massachusetts|michigan|minnesota|mississippi|missouri|montana|'
    r'nebraska|nevada|new hampshire|new jersey|new mexico|new york|'
    r'north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|'
    r'rhode island|south carolina|south dakota|tennessee|texas|utah|'
    r'vermont|virginia|washington|west virginia|wisconsin|wyoming)\b'
)


# ── Helpers ───────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def normalize(text: str) -> str:
    """Strip brackets/quotes and lowercase — matches keyword_identities.normalized_text."""
    t = re.sub(r'[\[\]""\u201c\u201d]', '', str(text)).strip().lower()
    return re.sub(r'\s+', ' ', t)


def chunked_query(conn, sql_template: str, ids: list,
                  extra_params: list = None,
                  chunk_size: int = 900) -> pd.DataFrame:
    """
    Run a SELECT with a large IN (?) clause by splitting ids into chunks
    of at most chunk_size (< SQLite 999-variable limit) and concatenating.

    sql_template must contain exactly one {placeholders} token.
    extra_params are appended after the id placeholders in each chunk.
    """
    if extra_params is None:
        extra_params = []
    frames = []
    for i in range(0, max(len(ids), 1), chunk_size):
        chunk = ids[i:i + chunk_size]
        if not chunk:
            break
        ph  = ','.join('?' * len(chunk))
        sql = sql_template.replace('{placeholders}', ph)
        frames.append(pd.read_sql_query(sql, conn,
                                        params=chunk + extra_params))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── Load keyword list from DB ──────────────────────────────────────
def load_db_keywords(conn) -> pd.DataFrame:
    """
    Returns all keyword_instances rows for TIER TWO (account_id=1,
    campaign_id=TIER_TWO_CAMPAIGN_ID), joined to keyword_identities,
    campaigns, and ad_groups for labels.

    This replaces load_csv_keywords() entirely. The DB is authoritative.
    """
    df = pd.read_sql_query("""
        SELECT
            ki.id              AS instance_id,
            ki.criterion_id,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.campaign_id,
            COALESCE(c.campaign_name, ki.campaign_name) AS campaign_name,
            ki.status          AS keyword_status,
            COALESCE(c.status,  'UNKNOWN') AS campaign_status,
            COALESCE(ag.status, 'UNKNOWN') AS ad_group_status,
            ki_id.normalized_text AS keyword,
            ki_id.match_type,
            ki_id.modifier_brand  AS brand
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        LEFT JOIN campaigns  c        ON ki.campaign_id  = c.campaign_id
        LEFT JOIN ad_groups  ag       ON ki.ad_group_id  = ag.ad_group_id
        WHERE ki.account_id  = ?
          AND ki.campaign_id = ?
        ORDER BY ki.ad_group_name, ki_id.normalized_text
    """, conn, params=[ACCOUNT_ID, TIER_TWO_CAMPAIGN_ID])

    print(f"  DB keywords loaded: {len(df)} rows, "
          f"{df['ad_group_name'].nunique()} ad groups, "
          f"campaign_ids={df['campaign_id'].unique().tolist()}")
    return df


# ── Fetch performance ─────────────────────────────────────────────
def fetch_keywords(conn, ki_df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    Pulls keyword_performance_daily for the instance_ids in ki_df,
    scoped to TIER_TWO_CAMPAIGN_ID and the date window.

    ki_df is the authoritative keyword list from load_db_keywords().
    """
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if ki_df.empty:
        return pd.DataFrame()

    ids = ki_df['instance_id'].dropna().astype(int).tolist()

    kpd_sql = """
        SELECT
            instance_id,
            COALESCE(SUM(impressions),        0)  AS impressions,
            COALESCE(SUM(clicks),             0)  AS clicks,
            COALESCE(SUM(cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(primary_conv_value), 0)  AS conv_value
        FROM keyword_performance_daily
        WHERE instance_id IN ({placeholders})
          AND campaign_id = ?
          AND device      = 'ALL'
          AND report_date BETWEEN ? AND ?
        GROUP BY instance_id
    """
    kpd_df = chunked_query(conn, kpd_sql, ids,
                           extra_params=[TIER_TWO_CAMPAIGN_ID,
                                         start_date, end_date])

    # Left-join so keywords with zero impressions in the window still appear
    result = ki_df.merge(kpd_df, on='instance_id', how='left')
    for col in ['impressions', 'clicks', 'spend', 'conversions', 'conv_value']:
        result[col] = result[col].fillna(0)

    # kw_status mirrors keyword_status from ki (no CSV to override from)
    result['kw_status'] = result['keyword_status']

    keep = ['ad_group_name', 'keyword', 'match_type', 'brand', 'kw_status',
            'campaign_name', 'campaign_status', 'ad_group_status', 'keyword_status',
            'criterion_id', 'instance_id',
            'impressions', 'clicks', 'spend', 'conversions', 'conv_value']
    result = result[[c for c in keep if c in result.columns]]
    result = result.sort_values(['ad_group_name', 'spend'],
                                ascending=[True, False]).reset_index(drop=True)

    print(f"  {len(result)} keyword rows, "
          f"{result['ad_group_name'].nunique()} ad groups")
    return result


# ── Derived metrics ───────────────────────────────────────────────
def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    d['ctr_pct']         = np.where(d['impressions'] > 0,
                                     d['clicks'] / d['impressions'] * 100, np.nan)
    d['cpc']             = np.where(d['clicks'] > 0,
                                     d['spend'] / d['clicks'], np.nan)
    d['roas']            = np.where(d['spend'] > 0,
                                     d['conv_value'] / d['spend'], np.nan)
    d['cpl']             = np.where(d['conversions'] > 0,
                                     d['spend'] / d['conversions'], np.nan)
    d['conv_rate_pct']   = np.where(d['clicks'] > 0,
                                     d['conversions'] / d['clicks'] * 100, np.nan)
    d['value_per_click'] = np.where(d['clicks'] > 0,
                                     d['conv_value'] / d['clicks'], np.nan)

    # Ad group totals
    ag = d.groupby('ad_group_name').agg(
        ag_spend      =('spend',      'sum'),
        ag_conv_value =('conv_value', 'sum'),
        ag_impressions=('impressions','sum'),
        ag_clicks     =('clicks',     'sum'),
    ).reset_index()
    ag['ag_roas'] = np.where(ag['ag_spend'] > 0,
                              ag['ag_conv_value'] / ag['ag_spend'], np.nan)
    d = d.merge(ag, on='ad_group_name', how='left')

    d['pct_ag_spend']   = np.where(d['ag_spend'] > 0,
                                    d['spend'] / d['ag_spend'] * 100, np.nan)
    d['pct_ag_revenue'] = np.where(d['ag_conv_value'] > 0,
                                    d['conv_value'] / d['ag_conv_value'] * 100, np.nan)
    d['roas_vs_ag_pct'] = np.where(
        (~pd.isna(d['roas'])) & (~pd.isna(d['ag_roas'])) & (d['ag_roas'] > 0),
        d['roas'] / d['ag_roas'] * 100, np.nan)

    def bucket(row):
        if row['spend'] < MIN_SPEND:
            return 'Insufficient spend'
        if pd.isna(row['roas']):
            return 'No conversions'
        if row['roas'] < CRITICAL_ROAS:
            return f'Critical (ROAS < {CRITICAL_ROAS})'
        if row['roas'] < WATCH_ROAS:
            return f'Watch (ROAS {CRITICAL_ROAS}–{WATCH_ROAS})'
        return f'Healthy (ROAS ≥ {WATCH_ROAS})'

    d['roas_bucket'] = d.apply(bucket, axis=1)
    return d


# ── Query summary (per keyword) ───────────────────────────────────
def fetch_query_summary(conn, instance_ids: list, days: int) -> pd.DataFrame:
    """
    Returns per-keyword summary stats for the Keyword Analysis columns,
    computed from search_query_performance scoped to TIER_TWO_CAMPAIGN_ID.

    Per-keyword metrics:
      zero_conv_queries, converting_queries, zero_conv_spend, converting_rev
      hhi_revenue     — query revenue concentration (0=spread, 1=one query)
      hhi_impressions — query impression concentration
      top_search_query, top_query_rev_pct
      is_exact_match  — Yes if keyword match_type = EXACT
      exact_exists    — Yes if an EXACT version of this keyword text exists
                        in keyword_instances for account_id=1
      geo_query_pct   — % of queries containing a US state name
      near_me_pct     — % of queries containing "near me"
      top_waste_queries — top 4 queries with spend > MIN_SPEND and zero convs
    """
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    # Pull all query-level rows for these keywords in the window
    qp_sql = """
        SELECT
            sqp.instance_id,
            sq.query_text,
            sqp.match_type_served,
            COALESCE(SUM(sqp.impressions),        0)  AS impressions,
            COALESCE(SUM(sqp.clicks),             0)  AS clicks,
            COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(sqp.primary_conv_value), 0)  AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        WHERE sqp.instance_id IN ({placeholders})
          AND sqp.campaign_id = ?
          AND sqp.device      = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sqp.instance_id, sqp.query_id
    """
    qp = chunked_query(conn, qp_sql, instance_ids,
                       extra_params=[TIER_TWO_CAMPAIGN_ID,
                                     start_date, end_date])

    if qp.empty:
        return pd.DataFrame()

    # Get exact match existence for keywords in this account
    exact_kws = pd.read_sql_query("""
        SELECT DISTINCT ki_id.normalized_text AS keyword_text
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id   = ?
          AND ki_id.match_type = 'EXACT'
    """, conn, params=(ACCOUNT_ID,))
    exact_set = set(exact_kws['keyword_text'].str.lower().str.strip())

    # Map instance_id → (keyword_text, match_type) from ki + ki_id
    ki_meta = chunked_query(conn, """
        SELECT
            ki.id AS instance_id,
            ki_id.normalized_text AS keyword_text,
            ki_id.match_type
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.id IN ({placeholders})
    """, instance_ids)
    id_map = (ki_meta.drop_duplicates(subset='instance_id', keep='first')
              .set_index('instance_id')
              .to_dict('index'))

    results = []
    for iid, grp in qp.groupby('instance_id'):
        row_meta  = id_map.get(iid, {})
        kw_text   = row_meta.get('keyword_text', '')
        mt        = row_meta.get('match_type', '')

        total_rev  = grp['conv_value'].sum()
        total_impr = grp['impressions'].sum()
        total_sp   = grp['spend'].sum()
        n_queries  = len(grp)

        # Zero conv / converting split
        zero_conv_qs  = grp[(grp['conversions'] == 0) & (grp['spend'] > 0)]
        conv_qs       = grp[grp['conversions'] > 0]
        zero_conv_sp  = zero_conv_qs['spend'].sum()
        converting_rv = conv_qs['conv_value'].sum()

        # HHI revenue
        if total_rev > 0:
            rev_shares = grp['conv_value'] / total_rev
            hhi_rev    = float((rev_shares ** 2).sum())
        else:
            hhi_rev = np.nan

        # HHI impressions
        if total_impr > 0:
            impr_shares = grp['impressions'] / total_impr
            hhi_impr    = float((impr_shares ** 2).sum())
        else:
            hhi_impr = np.nan

        # Top search query by revenue (fallback to spend)
        if total_rev > 0:
            top_row = grp.loc[grp['conv_value'].idxmax()]
        elif total_sp > 0:
            top_row = grp.loc[grp['spend'].idxmax()]
        else:
            top_row = grp.iloc[0]
        top_query   = top_row['query_text']
        top_rev_pct = (top_row['conv_value'] / total_rev * 100
                       if total_rev > 0 else np.nan)

        is_exact     = 'Yes' if mt == 'EXACT' else 'No'
        kw_norm      = normalize(kw_text)
        exact_exists = 'Yes' if kw_norm in exact_set else 'No'

        # Geo query %
        geo_mask  = grp['query_text'].str.contains(GEO_PATTERN, case=False,
                                                    regex=True, na=False)
        geo_pct   = round(geo_mask.sum() / n_queries * 100, 1) if n_queries > 0 else 0.0

        # Near me query %
        near_mask = grp['query_text'].str.contains(r'near me', case=False,
                                                    regex=True, na=False)
        near_pct  = round(near_mask.sum() / n_queries * 100, 1) if n_queries > 0 else 0.0

        # Top 4 waste queries
        waste_qs  = (grp[(grp['conversions'] == 0) & (grp['spend'] >= MIN_SPEND)]
                     .sort_values('spend', ascending=False)
                     .head(4)['query_text'].tolist())
        top_waste = ' | '.join(waste_qs) if waste_qs else ''

        results.append({
            'instance_id':       iid,
            'zero_conv_queries': len(zero_conv_qs),
            'converting_queries':len(conv_qs),
            'zero_conv_spend':   round(zero_conv_sp, 2),
            'converting_rev':    round(converting_rv, 2),
            'hhi_revenue':       round(hhi_rev,  4) if not np.isnan(hhi_rev)  else np.nan,
            'hhi_impressions':   round(hhi_impr, 4) if not np.isnan(hhi_impr) else np.nan,
            'top_search_query':  top_query,
            'top_query_rev_pct': round(top_rev_pct, 1) if not np.isnan(top_rev_pct) else np.nan,
            'is_exact_match':    is_exact,
            'exact_exists':      exact_exists,
            'geo_query_pct':     round(geo_pct,  1),
            'near_me_pct':       round(near_pct, 1),
            'top_waste_queries': top_waste,
        })

    return pd.DataFrame(results)


# ── Search query detail (one row per keyword × query) ─────────────
def fetch_search_query_detail(conn, instance_ids: list, days: int) -> pd.DataFrame:
    """
    Returns one row per search query × keyword for the Search Query sheet.
    Filtered to campaign_id = TIER_TWO_CAMPAIGN_ID in sqp.
    """
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    sq_sql = """
        SELECT
            ki.criterion_id,
            ki_id.normalized_text            AS keyword,
            ki.ad_group_name,
            ki_id.match_type,
            ki.status                        AS keyword_status,
            COALESCE(c.status,  'UNKNOWN')   AS campaign_status,
            COALESCE(ag.status, 'UNKNOWN')   AS ad_group_status,
            COALESCE(c.campaign_name, ki.campaign_name) AS campaign_name,
            sq.query_text,
            COALESCE(sq.vertical,       '')  AS vertical,
            COALESCE(sq.modifier_brand, '')  AS brand,
            COALESCE(sq.brand_category, '')  AS brand_category,
            CASE WHEN sq.modifier_geo      IS NOT NULL
                  AND sq.modifier_geo      != '' THEN 'Yes' ELSE 'No' END AS geo_flag,
            CASE WHEN sq.modifier_near_me  IS NOT NULL
                  AND sq.modifier_near_me  != '' THEN 'Yes' ELSE 'No' END AS near_me_flag,
            CASE WHEN sq.modifier_price    IS NOT NULL
                  AND sq.modifier_price    != '' THEN 'Yes' ELSE 'No' END AS price_flag,
            sqp.match_type_served,
            sqp.instance_id,
            SUM(sqp.impressions)             AS impressions,
            SUM(sqp.clicks)                  AS clicks,
            SUM(sqp.cost_micros) / 1e6       AS spend,
            SUM(sqp.primary_conv_count)      AS conversions,
            SUM(sqp.primary_conv_value)      AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries     sq  ON sqp.query_id   = sq.id
        JOIN keyword_instances  ki  ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        LEFT JOIN campaigns     c   ON ki.campaign_id  = c.campaign_id
        LEFT JOIN ad_groups     ag  ON ki.ad_group_id  = ag.ad_group_id
        WHERE sqp.instance_id IN ({placeholders})
          AND sqp.campaign_id = ?
          AND sqp.device      = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sqp.instance_id, sqp.query_id
    """
    df = chunked_query(conn, sq_sql, instance_ids,
                       extra_params=[TIER_TWO_CAMPAIGN_ID,
                                     start_date, end_date])

    if df.empty:
        return df

    df = df.sort_values(['ad_group_name', 'keyword', 'spend'],
                        ascending=[True, True, False]).reset_index(drop=True)
    return df


# ── Build sheets ──────────────────────────────────────────────────
def build_search_query_sheet(df: pd.DataFrame,
                              kw_df: pd.DataFrame,
                              days: int) -> pd.DataFrame:
    """
    One row per search query × keyword.
    Includes derived metrics and a Waste Flag.
    """
    if df.empty:
        return pd.DataFrame([{'Note': f'No search query data for {days}d window'}])

    kw_roas = kw_df[['instance_id', 'roas']].rename(columns={'roas': 'kw_roas'})
    df = df.merge(kw_roas, on='instance_id', how='left')

    df['query_ctr']       = np.where(df['impressions'] > 0,
                                      df['clicks'] / df['impressions'] * 100, np.nan)
    df['query_cpc']       = np.where(df['clicks'] > 0,
                                      df['spend'] / df['clicks'], np.nan)
    df['query_roas']      = np.where(df['spend'] > 0,
                                      df['conv_value'] / df['spend'], np.nan)
    df['query_conv_rate'] = np.where(df['clicks'] > 0,
                                      df['conversions'] / df['clicks'] * 100, np.nan)
    df['roas_vs_kw_pct']  = np.where(
        (~pd.isna(df['query_roas'])) & (~pd.isna(df['kw_roas'])) & (df['kw_roas'] > 0),
        df['query_roas'] / df['kw_roas'] * 100, np.nan)

    # Per-query HHI contribution at keyword level
    kw_rev_totals  = df.groupby('instance_id')['conv_value'].transform('sum')
    kw_impr_totals = df.groupby('instance_id')['impressions'].transform('sum')
    df['query_rev_share']  = np.where(kw_rev_totals  > 0,
                                       df['conv_value']  / kw_rev_totals,  np.nan)
    df['query_impr_share'] = np.where(kw_impr_totals > 0,
                                       df['impressions'] / kw_impr_totals, np.nan)
    df['hhi_rev_contribution']  = df['query_rev_share']  ** 2
    df['hhi_impr_contribution'] = df['query_impr_share'] ** 2

    # Per-query HHI contribution at ad group level
    ag_query_agg = (
        df.groupby(['ad_group_name', 'query_text'], as_index=False)
          .agg(ag_query_rev=('conv_value', 'sum'),
               ag_query_impr=('impressions', 'sum'))
    )
    ag_totals = (
        ag_query_agg.groupby('ad_group_name', as_index=False)
                    .agg(ag_total_rev=('ag_query_rev', 'sum'),
                         ag_total_impr=('ag_query_impr', 'sum'))
    )
    ag_query_agg = ag_query_agg.merge(ag_totals, on='ad_group_name', how='left')
    ag_query_agg['ag_query_rev_share']  = np.where(
        ag_query_agg['ag_total_rev']  > 0,
        ag_query_agg['ag_query_rev']  / ag_query_agg['ag_total_rev'],  np.nan)
    ag_query_agg['ag_query_impr_share'] = np.where(
        ag_query_agg['ag_total_impr'] > 0,
        ag_query_agg['ag_query_impr'] / ag_query_agg['ag_total_impr'], np.nan)
    ag_query_agg['hhi_ag_rev_contribution']  = ag_query_agg['ag_query_rev_share']  ** 2
    ag_query_agg['hhi_ag_impr_contribution'] = ag_query_agg['ag_query_impr_share'] ** 2

    df = df.merge(
        ag_query_agg[['ad_group_name', 'query_text',
                       'ag_query_rev_share', 'ag_query_impr_share',
                       'hhi_ag_rev_contribution', 'hhi_ag_impr_contribution']],
        on=['ad_group_name', 'query_text'], how='left'
    )

    def waste_flag(row):
        if row['spend'] < MIN_SPEND:
            return ''
        if pd.isna(row['query_roas']) or row['query_roas'] == 0:
            return 'Waste — zero conversions'
        if row['query_roas'] < CRITICAL_ROAS:
            return f'Waste — ROAS < {CRITICAL_ROAS}'
        return ''

    df['waste_flag'] = df.apply(waste_flag, axis=1)

    col_map = {
        'ad_group_name':            'Ad Group',
        'keyword':                  'Keyword',
        'match_type':               'Match Type',
        'campaign_name':            'Campaign',
        'campaign_status':          'Campaign Status',
        'ad_group_status':          'AG Status',
        'keyword_status':           'KW Status',
        'query_text':               'Search Query',
        'match_type_served':        'Match Type Served',
        'vertical':                 'Vertical',
        'brand':                    'Brand',
        'brand_category':           'Brand Category',
        'geo_flag':                 'Geo Flag',
        'near_me_flag':             'Near Me Flag',
        'price_flag':               'Price Flag',
        'waste_flag':               'Waste Flag',
        'impressions':              'Impressions',
        'clicks':                   'Clicks',
        'query_ctr':                'CTR %',
        'spend':                    'Spend ($)',
        'query_cpc':                'CPC ($)',
        'conversions':              'Conversions',
        'conv_value':               'Revenue ($)',
        'query_roas':               'Query ROAS',
        'query_conv_rate':          'Conv Rate %',
        'roas_vs_kw_pct':           'Query ROAS % of KW',
        'query_rev_share':          'Rev Share of KW',
        'query_impr_share':         'Impr Share of KW',
        'hhi_rev_contribution':     'HHI Rev Contribution (KW)',
        'hhi_impr_contribution':    'HHI Impr Contribution (KW)',
        'ag_query_rev_share':       'Rev Share of AG',
        'ag_query_impr_share':      'Impr Share of AG',
        'hhi_ag_rev_contribution':  'HHI Rev Contribution (AG)',
        'hhi_ag_impr_contribution': 'HHI Impr Contribution (AG)',
    }

    df_out = df[[c for c in col_map if c in df.columns]].rename(columns=col_map)

    rnd = {
        'CTR %': 2, 'Spend ($)': 2, 'CPC ($)': 2, 'Conversions': 1,
        'Revenue ($)': 2, 'Query ROAS': 2, 'Conv Rate %': 2,
        'Query ROAS % of KW': 1,
        'Rev Share of KW': 4, 'Impr Share of KW': 4,
        'HHI Rev Contribution (KW)': 6, 'HHI Impr Contribution (KW)': 6,
        'Rev Share of AG': 4, 'Impr Share of AG': 4,
        'HHI Rev Contribution (AG)': 6, 'HHI Impr Contribution (AG)': 6,
    }
    for col, dec in rnd.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    df_out = df_out.sort_values(
        ['Ad Group', 'Keyword', 'Spend ($)'], ascending=[True, True, False]
    ).reset_index(drop=True)

    return df_out


def build_sheet(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([{'Note': f'No data for {days}d window'}])

    col_map = {
        'ad_group_name':     'Ad Group',
        'keyword':           'Keyword',
        'match_type':        'Match Type',
        'brand':             'Brand',
        'campaign_name':     'Campaign',
        'campaign_status':   'Campaign Status',
        'ad_group_status':   'AG Status',
        'keyword_status':    'KW Status',
        'roas_bucket':       'ROAS Bucket',
        'impressions':       'Impressions',
        'clicks':            'Clicks',
        'ctr_pct':           'CTR %',
        'spend':             'Spend ($)',
        'cpc':               'CPC ($)',
        'conversions':       'Offline Convs',
        'conv_value':        'Revenue ($)',
        'roas':              'ROAS',
        'cpl':               'CPL ($)',
        'conv_rate_pct':     'Conv Rate %',
        'value_per_click':   'Value / Click',
        'pct_ag_spend':      '% of AG Spend',
        'pct_ag_revenue':    '% of AG Revenue',
        'roas_vs_ag_pct':    'KW ROAS % of AG',
        'zero_conv_queries': 'Zero-Conv Queries',
        'converting_queries':'Converting Queries',
        'zero_conv_spend':   'Zero-Conv Spend ($)',
        'converting_rev':    'Converting Rev ($)',
        'hhi_revenue':       'HHI (Revenue)',
        'hhi_impressions':   'HHI (Impressions)',
        'top_search_query':  'Top Search Query',
        'top_query_rev_pct': 'Top Query Rev %',
        'is_exact_match':    'Is Exact Match',
        'exact_exists':      'Exact Exists',
        'geo_query_pct':     'Geo Query %',
        'near_me_pct':       'Near Me Query %',
        'top_waste_queries': 'Top Waste Queries',
    }

    df_out = df[[c for c in col_map if c in df.columns]].rename(columns=col_map)

    num_rounds = {
        'CTR %': 2, 'Spend ($)': 2, 'CPC ($)': 2, 'Offline Convs': 1,
        'Revenue ($)': 2, 'ROAS': 2, 'CPL ($)': 2, 'Conv Rate %': 2,
        'Value / Click': 2, '% of AG Spend': 1, '% of AG Revenue': 1,
        'KW ROAS % of AG': 1, 'Zero-Conv Spend ($)': 2, 'Converting Rev ($)': 2,
        'HHI (Revenue)': 4, 'HHI (Impressions)': 4,
        'Top Query Rev %': 1, 'Geo Query %': 1, 'Near Me Query %': 1,
    }
    for col, dec in num_rounds.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    result_rows = []
    for ag_name, group in df.groupby('ad_group_name', sort=True):
        ag_sp   = group['spend'].sum()
        ag_rev  = group['conv_value'].sum()
        ag_conv = group['conversions'].sum()
        ag_cl   = group['clicks'].sum()
        ag_im   = group['impressions'].sum()

        sub = {
            'Ad Group':          f'── {ag_name} (subtotal)',
            'Keyword':           '',
            'Match Type':        '',
            'Brand':             '',
            'KW Status':         '',
            'ROAS Bucket':       '',
            'Impressions':       int(ag_im),
            'Clicks':            int(ag_cl),
            'CTR %':             round(ag_cl / ag_im * 100, 2) if ag_im > 0 else None,
            'Spend ($)':         round(ag_sp, 2),
            'CPC ($)':           round(ag_sp / ag_cl, 2)   if ag_cl > 0 else None,
            'Offline Convs':     round(ag_conv, 1),
            'Revenue ($)':       round(ag_rev, 2),
            'ROAS':              round(ag_rev / ag_sp, 2)  if ag_sp > 0 else None,
            'CPL ($)':           round(ag_sp / ag_conv, 2) if ag_conv > 0 else None,
            'Conv Rate %':       round(ag_conv / ag_cl * 100, 2) if ag_cl > 0 else None,
            'Value / Click':     round(ag_rev / ag_cl, 2)  if ag_cl > 0 else None,
            '% of AG Spend':     100.0,
            '% of AG Revenue':   100.0,
            'KW ROAS % of AG':   100.0,
        }
        result_rows.append(sub)

        ag_kws = df_out[df_out['Ad Group'] == ag_name].sort_values(
            'Spend ($)', ascending=False)
        for _, row in ag_kws.iterrows():
            result_rows.append(row.to_dict())

    return pd.DataFrame(result_rows)


# ── Formatting ────────────────────────────────────────────────────
def format_workbook(writer):
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    SUB_FILL  = PatternFill("solid", fgColor="1F4E79")
    SUB_FONT  = Font(bold=True, color="FFFFFF", size=10)
    HDR_FILL  = PatternFill("solid", fgColor="7030A0")
    HDR_FONT  = Font(bold=True, color="FFFFFF", size=10)
    CRIT_FILL = PatternFill("solid", fgColor="FFCCCC")
    WATCH_FILL= PatternFill("solid", fgColor="FFF2CC")
    HLTHY_FILL= PatternFill("solid", fgColor="E2EFDA")
    THIN      = Border(
        bottom=Side(style='thin', color='D9D9D9'),
        right =Side(style='thin', color='D9D9D9'),
    )

    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        if ws.max_row < 2:
            continue

        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

        for cell in ws[1]:
            cell.fill      = HDR_FILL
            cell.font      = HDR_FONT
            cell.alignment = Alignment(horizontal='center', wrap_text=True)

        ag_idx     = headers.index('Ad Group')    + 1 if 'Ad Group'    in headers else None
        bucket_idx = headers.index('ROAS Bucket') + 1 if 'ROAS Bucket' in headers else None

        for row in ws.iter_rows(min_row=2):
            is_sub = ag_idx and str(row[ag_idx - 1].value or '').startswith('──')
            if is_sub:
                for cell in row:
                    cell.fill = SUB_FILL
                    cell.font = SUB_FONT
            elif bucket_idx:
                bval = str(row[bucket_idx - 1].value or '')
                if 'Critical' in bval:
                    for cell in row: cell.fill = CRIT_FILL
                elif 'Watch' in bval:
                    for cell in row: cell.fill = WATCH_FILL
                elif 'Healthy' in bval:
                    for cell in row: cell.fill = HLTHY_FILL
            for cell in row:
                cell.border    = THIN
                cell.alignment = Alignment(horizontal='left')

        widths = {
            'Ad Group': 38, 'Keyword': 36, 'Match Type': 10, 'Brand': 12,
            'KW Status': 10, 'ROAS Bucket': 24,
            'Search Query': 48, 'Top Search Query': 36, 'Top Waste Queries': 60,
        }
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 13)

        ws.freeze_panes = 'A2'


# ── Main ──────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="TIER TWO keyword analysis (DB-only, no CSV required)."
    )
    parser.add_argument(
        "--end-date", default=None, metavar="YYYY-MM-DD",
        help="Analysis end date (default: yesterday). "
             "Example: --end-date 2026-04-08"
    )
    args = parser.parse_args()

    global END_DATE
    if args.end_date:
        END_DATE = date.fromisoformat(args.end_date)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("Tier Two Keyword Analysis  [DB-only]")
    print(f"  Campaign ID : {TIER_TWO_CAMPAIGN_ID}")
    print(f"  Account ID  : {ACCOUNT_ID}")
    print(f"  End date    : {END_DATE}")
    print(f"  Windows     : 30 / 60 / 90 days")
    print("=" * 65)

    conn   = get_conn()
    ki_df  = load_db_keywords(conn)

    if ki_df.empty:
        print("⚠  No keywords found in DB for TIER TWO. Exiting.")
        conn.close()
        return

    sheets = {}

    for days in [30, 60, 90]:
        print(f"\n[{days}d] Fetching keyword performance...")
        raw          = fetch_keywords(conn, ki_df, days)
        with_metrics = add_derived_metrics(raw)

        instance_ids = with_metrics['instance_id'].dropna().astype(int).tolist()
        q_summary    = fetch_query_summary(conn, instance_ids, days)
        if not q_summary.empty:
            with_metrics = with_metrics.merge(q_summary, on='instance_id', how='left')
            for col in ['zero_conv_queries', 'converting_queries',
                        'zero_conv_spend', 'converting_rev']:
                with_metrics[col] = with_metrics[col].fillna(0)

        sheet = build_sheet(with_metrics, days)

        kw_rows = sheet[~sheet['Ad Group'].str.startswith('──', na=False)]
        tot_sp  = kw_rows['Spend ($)'].sum()  if 'Spend ($)'  in kw_rows else 0
        tot_rev = kw_rows['Revenue ($)'].sum() if 'Revenue ($)' in kw_rows else 0
        print(f"  Spend: ${tot_sp:,.2f}  |  Revenue: ${tot_rev:,.2f}")
        if 'ROAS Bucket' in kw_rows.columns:
            for bkt, cnt in kw_rows['ROAS Bucket'].value_counts().items():
                print(f"    {bkt}: {cnt}")

        sheets[f'Keyword Analysis ({days}d)'] = sheet

        print(f"  Fetching search query detail...")
        sq_raw = fetch_search_query_detail(conn, instance_ids, days)
        if not sq_raw.empty:
            print(f"    {len(sq_raw):,} query rows, "
                  f"{sq_raw['query_text'].nunique():,} unique queries")
        sheets[f'Search Query Analysis ({days}d)'] = build_search_query_sheet(
            sq_raw, with_metrics, days
        )

    conn.close()

    print(f"\nWriting: {OUTPUT_FILE}")
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {OUTPUT_FILE.stat().st_size / 1024:.1f} KB")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
