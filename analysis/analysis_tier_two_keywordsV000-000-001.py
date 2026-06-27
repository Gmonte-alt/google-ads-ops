"""
analysis_tier_two_keywords.py
==============================
Keyword performance analysis for:
  Campaign : HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
  Account  : 8886502147 (Google Ads)
  Windows  : 30 / 60 / 90 days

DATA STRATEGY
─────────────
The Google Ads CSV export is used as the authoritative keyword list for
TIER TWO. It defines exactly which 178 (keyword_text, match_type, ad_group)
combinations belong to this campaign.

Those triples are matched against keyword_instances on:
    normalized_text + match_type + ad_group_name

This finds the correct criterion_ids regardless of what campaign_name
keyword_instances currently shows for each keyword. keyword_performance_daily
is then pulled for those instance_ids scoped to the analysis window.

No dependency on: agpd, campaigns table, ad_groups table, campaign_name
in keyword_instances.

CSV PATH
────────
Update CSV_PATH below if you move the export file.
The CSV is the standard Google Ads keyword report export (Search keywords).

RUN
───
  conda activate paid-search
  python analysis/analysis_tier_two_keywords.py
"""

import re
import csv
import sqlite3
import pandas as pd
import numpy as np
from io import StringIO
from pathlib import Path
from datetime import date, timedelta

# ── Configuration ─────────────────────────────────────────────────
DB_PATH         = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR      = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_FILE     = OUTPUT_DIR / "tier_two_keyword_analysis.xlsx"
CSV_PATH        = Path(r"C:\Projects\google-ads-ops\analysis\input\Search_keyword_report.csv")

GOOGLE_ACCOUNT_ID  = "8886502147"
TIER_TWO_CAMPAIGN  = "HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE"
END_DATE           = date.today() - timedelta(days=1)

# Optional: pin the analysis window to match the CSV export period exactly.
# Uncomment and set both dates to lock the 30/60/90d windows to a fixed
# end date regardless of when the script runs. The 30d window will be
# END_DATE - 29 days through END_DATE.
# Example — to match a CSV exported Mar 10 – Apr 8:
#   END_DATE = date(2026, 4, 8)

# ROAS bucket thresholds — applied to the keyword's OWN absolute ROAS
# Critical : keyword ROAS < 0.50  (less than 50 cents returned per $1 spent)
# Watch     : keyword ROAS 0.50 – 0.70
# Healthy   : keyword ROAS > 0.70
CRITICAL_ROAS = 0.50
WATCH_ROAS    = 0.70
MIN_SPEND     = 5.00

# Match type mapping: CSV → keyword_identities format
MT_MAP = {
    'broad match':  'BROAD',
    'exact match':  'EXACT',
    'phrase match': 'PHRASE',
}


# ── Helpers ───────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def normalize(text: str) -> str:
    """Strip brackets/quotes and lowercase -- matches keyword_identities.normalized_text."""
    t = re.sub(r'[\[\]""\u201c\u201d]', '', str(text)).strip().lower()
    return re.sub(r'\s+', ' ', t)


# ── Load CSV keyword list ─────────────────────────────────────────
def load_csv_keywords(path: Path) -> pd.DataFrame:
    """
    Reads the Google Ads keyword report CSV.
    Requires Keyword ID, Campaign ID, Ad group ID columns in the export.
    Keyword ID maps directly to keyword_instances.criterion_id -- exact join,
    no name-matching, no cross-campaign ambiguity.
    """
    with open(path, encoding='utf-8-sig') as f:
        lines = f.readlines()

    data_lines = []
    header_found = False
    for line in lines:
        if not header_found:
            if 'Keyword' in line and 'Match type' in line:
                header_found = True
                data_lines.append(line)
        else:
            data_lines.append(line)

    reader = csv.DictReader(StringIO(''.join(data_lines)))
    rows   = [r for r in reader
              if r.get('Keyword','').strip()
              and r.get('Keyword','').strip() != 'Total']

    def cn(v):
        try:
            return float(str(v or '0').replace(',','').replace('$','')
                         .replace('%','').strip() or 0)
        except:
            return 0.0

    records = []
    for r in rows:
        kw_id = r.get('Keyword ID','').strip()
        if not kw_id:
            continue
        records.append({
            'criterion_id':  kw_id,
            'campaign_id':   r.get('Campaign ID','').strip(),
            'ad_group_id':   r.get('Ad group ID','').strip(),
            'ad_group_name': r.get('Ad group','').strip(),
            'keyword_text':  r.get('Keyword','').strip(),
            'match_type':    MT_MAP.get(r.get('Match type','').lower(), ''),
            'kw_status':     r.get('Keyword status','').strip(),
            'csv_conv_value':cn(r.get('Conv. value', 0)),
            'csv_spend':     cn(r.get('Cost', 0)),
            'csv_clicks':    cn(r.get('Clicks', 0)),
            'csv_impr':      cn(r.get('Impr.', 0)),
        })

    df = pd.DataFrame(records)
    print(f"  CSV keywords loaded: {len(df)} rows, "
          f"{df['ad_group_name'].nunique()} ad groups, "
          f"campaign_ids={df['campaign_id'].unique().tolist()}")
    return df



def fetch_keywords(conn, csv_df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    Joins CSV keyword IDs directly to keyword_instances.criterion_id.
    One criterion_id = one unique keyword slot in one specific campaign.
    No name matching, no cross-campaign contamination.
    kpd is then pulled for those exact instance_ids within the date window.
    """
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    # Step 1: look up instance_id for each criterion_id
    criterion_ids = csv_df['criterion_id'].tolist()
    placeholders  = ','.join('?' * len(criterion_ids))

    ki_df = pd.read_sql_query(f"""
        SELECT
            ki.id            AS instance_id,
            ki.criterion_id,
            ki.ad_group_name,
            ki_id.normalized_text  AS keyword,
            ki_id.match_type,
            ki_id.modifier_brand   AS brand
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        JOIN accounts a               ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND ki.criterion_id IN ({placeholders})
    """, conn, params=[GOOGLE_ACCOUNT_ID] + criterion_ids)

    # Merge CSV → ki on criterion_id (exact, unique key)
    merged = csv_df.merge(ki_df, on='criterion_id', how='left',
                          suffixes=('_csv', '_ki'))

    matched   = merged[merged['instance_id'].notna()]
    unmatched = merged[merged['instance_id'].isna()]
    print(f"  Matched criterion_ids : {len(matched)} / {len(csv_df)}")
    if len(unmatched) > 0:
        print(f"  Unmatched             : {len(unmatched)} "
              f"(not in keyword_instances, will show CSV zeros)")

    # Step 2: pull kpd for matched instance_ids only
    if matched.empty:
        kpd_df = pd.DataFrame(columns=['instance_id','impressions','clicks',
                                        'spend','conversions','conv_value'])
    else:
        ids          = matched['instance_id'].dropna().astype(int).tolist()
        placeholders2 = ','.join('?' * len(ids))
        kpd_df = pd.read_sql_query(f"""
            SELECT
                instance_id,
                COALESCE(SUM(impressions),        0)  AS impressions,
                COALESCE(SUM(clicks),             0)  AS clicks,
                COALESCE(SUM(cost_micros), 0) / 1e6   AS spend,
                COALESCE(SUM(primary_conv_count), 0)  AS conversions,
                COALESCE(SUM(primary_conv_value), 0)  AS conv_value
            FROM keyword_performance_daily
            WHERE instance_id IN ({placeholders2})
              AND campaign_id = '23496334752'
              AND device      = 'ALL'
              AND report_date BETWEEN ? AND ?
            GROUP BY instance_id
        """, conn, params=ids + [start_date, end_date])

    # Step 3: join kpd back to merged frame
    result = merged.merge(kpd_df, on='instance_id', how='left')
    for col in ['impressions','clicks','spend','conversions','conv_value']:
        result[col] = result[col].fillna(0)

    # CSV is the authoritative source for ad_group_name and match_type --
    # it is scoped to TIER TWO and reflects the correct ad group assignment.
    # ki.ad_group_name is now updated by 02 v2 per-campaign and may reflect
    # a different campaign's ad group name for shared criterion_ids.
    # ki values are used only for keyword text and brand (identity metadata).
    result['ad_group_name'] = result['ad_group_name_csv']
    result['match_type']    = result['match_type_csv']
    result['keyword']       = result['keyword'].fillna(result['keyword_text'])

    keep = ['ad_group_name','keyword','match_type','brand','kw_status',
            'criterion_id','instance_id',
            'impressions','clicks','spend','conversions','conv_value',
            'csv_conv_value','csv_spend']
    result = result[[c for c in keep if c in result.columns]]
    result = result.sort_values(['ad_group_name','spend'],
                                ascending=[True, False]).reset_index(drop=True)

    print(f"  {len(result)} keyword rows, "
          f"{result['ad_group_name'].nunique()} ad groups")
    return result



def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    d['ctr_pct']        = np.where(d['impressions'] > 0,
                                    d['clicks'] / d['impressions'] * 100, np.nan)
    d['cpc']            = np.where(d['clicks'] > 0,
                                    d['spend'] / d['clicks'], np.nan)
    d['roas']           = np.where(d['spend'] > 0,
                                    d['conv_value'] / d['spend'], np.nan)
    d['cpl']            = np.where(d['conversions'] > 0,
                                    d['spend'] / d['conversions'], np.nan)
    d['conv_rate_pct']  = np.where(d['clicks'] > 0,
                                    d['conversions'] / d['clicks'] * 100, np.nan)
    d['value_per_click']= np.where(d['clicks'] > 0,
                                    d['conv_value'] / d['clicks'], np.nan)

    # Ad group totals
    ag = d.groupby('ad_group_name').agg(
        ag_spend     =('spend',     'sum'),
        ag_conv_value=('conv_value','sum'),
        ag_impressions=('impressions','sum'),
        ag_clicks    =('clicks',    'sum'),
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


# ── Build sheet ───────────────────────────────────────────────────
# US state names for geo query detection (full names only to avoid false positives
# from two-letter abbreviations appearing in common words like "in", "or", "me")
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


def fetch_query_summary(conn, instance_ids: list,
                         csv_df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    Returns per-keyword summary stats for the Keyword Analysis columns.
    Computes from search_query_performance scoped to TIER TWO campaign_id.

    Per-keyword metrics:
      zero_conv_queries, converting_queries, zero_conv_spend, converting_rev
      hhi_revenue     -- query revenue concentration (0=spread, 1=one query)
      hhi_impressions -- query impression concentration
      top_search_query, top_query_rev_pct
      is_exact_match  -- Yes if keyword match_type = EXACT
      exact_exists    -- Yes if an EXACT version of this keyword text exists
                         in keyword_instances for the home account
      geo_query_pct   -- % of queries containing a geo modifier
      near_me_pct     -- % of queries containing "near me"
      top_waste_queries -- top 4 queries with spend > MIN_SPEND and zero convs
    """
    TIER_TWO_CAMPAIGN_ID = "23496334752"
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    placeholders = ','.join('?' * len(instance_ids))

    # Pull all query-level rows for these keywords in the window
    qp = pd.read_sql_query(f"""
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
    """, conn, params=instance_ids + [TIER_TWO_CAMPAIGN_ID,
                                       start_date, end_date])

    if qp.empty:
        return pd.DataFrame()

    # Get exact match existence for keywords in this account
    exact_kws = pd.read_sql_query("""
        SELECT DISTINCT ki_id.normalized_text AS keyword_text
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        JOIN accounts a               ON ki.account_id  = a.id
        WHERE a.platform_account_id = ?
          AND ki_id.match_type = 'EXACT'
    """, conn, params=(GOOGLE_ACCOUNT_ID,))
    exact_set = set(exact_kws['keyword_text'].str.lower().str.strip())

    # Map instance_id → csv fields (keyword text, match_type, ad_group)
    id_map = (csv_df[['criterion_id', 'ad_group_name', 'match_type', 'keyword_text']]
              .drop_duplicates(subset='criterion_id')
              .set_index('criterion_id')
              .to_dict('index'))

    # Map instance_id → criterion_id from ki
    ki_map = pd.read_sql_query(f"""
        SELECT ki.id AS instance_id, ki.criterion_id
        FROM keyword_instances ki
        WHERE ki.id IN ({placeholders})
    """, conn, params=instance_ids)
    inst_to_crit = dict(zip(ki_map['instance_id'], ki_map['criterion_id']))

    results = []
    for iid, grp in qp.groupby('instance_id'):
        crit_id  = str(inst_to_crit.get(iid, ''))
        csv_row  = id_map.get(crit_id, {})
        kw_text  = csv_row.get('keyword_text', '')
        mt       = csv_row.get('match_type', '')

        total_rev  = grp['conv_value'].sum()
        total_impr = grp['impressions'].sum()
        total_sp   = grp['spend'].sum()
        n_queries  = len(grp)

        # Zero conv / converting
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

        # Is exact match
        is_exact = 'Yes' if mt == 'EXACT' else 'No'

        # Normalize keyword_text (strip brackets/quotes) before exact lookup
        kw_norm  = normalize(kw_text)
        exact_exists = 'Yes' if kw_norm in exact_set else 'No'

        # Geo query % -- % of queries (by count) containing US state names
        # Uses query count rather than impressions for stability
        geo_mask  = grp['query_text'].str.contains(GEO_PATTERN, case=False,
                                                    regex=True, na=False)
        geo_count = int(geo_mask.sum())
        geo_pct   = round(geo_count / n_queries * 100, 1) if n_queries > 0 else 0.0

        # Near me query %
        near_mask  = grp['query_text'].str.contains(r'near me', case=False,
                                                     regex=True, na=False)
        near_count = int(near_mask.sum())
        near_pct   = round(near_count / n_queries * 100, 1) if n_queries > 0 else 0.0

        # Top 4 waste queries (spend > MIN_SPEND, zero conversions), by spend desc
        waste_qs    = (grp[(grp['conversions'] == 0) & (grp['spend'] >= MIN_SPEND)]
                       .sort_values('spend', ascending=False)
                       .head(4)['query_text']
                       .tolist())
        top_waste   = ' | '.join(waste_qs) if waste_qs else ''

        results.append({
            'instance_id':      iid,
            'zero_conv_queries':len(zero_conv_qs),
            'converting_queries':len(conv_qs),
            'zero_conv_spend':  round(zero_conv_sp, 2),
            'converting_rev':   round(converting_rv, 2),
            'hhi_revenue':      round(hhi_rev, 4) if not np.isnan(hhi_rev) else np.nan,
            'hhi_impressions':  round(hhi_impr, 4) if not np.isnan(hhi_impr) else np.nan,
            'top_search_query': top_query,
            'top_query_rev_pct':round(top_rev_pct, 1) if not np.isnan(top_rev_pct) else np.nan,
            'is_exact_match':   is_exact,
            'exact_exists':     exact_exists,
            'geo_query_pct':    round(geo_pct, 1),
            'near_me_pct':      round(near_pct, 1),
            'top_waste_queries':top_waste,
        })

    return pd.DataFrame(results)



def fetch_search_query_detail(conn, instance_ids: list,
                               csv_df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    Returns one row per search query x keyword for the Search Query sheet.
    Filtered to campaign_id = TIER_TWO_CAMPAIGN_ID in sqp.

    ad_group_name and match_type are overridden from csv_df after the DB join
    because ki.ad_group_name is now the last campaign that updated each
    criterion_id after 02 v2 per-campaign sync. CSV is authoritative for
    TIER TWO ad group and match type labels.
    """
    TIER_TWO_CAMPAIGN_ID = "23496334752"
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    placeholders = ','.join('?' * len(instance_ids))
    df = pd.read_sql_query(f"""
        SELECT
            ki.criterion_id,
            ki_id.normalized_text        AS keyword,
            sq.query_text,
            sqp.match_type_served,
            sqp.instance_id,
            SUM(sqp.impressions)         AS impressions,
            SUM(sqp.clicks)              AS clicks,
            SUM(sqp.cost_micros) / 1e6   AS spend,
            SUM(sqp.primary_conv_count)  AS conversions,
            SUM(sqp.primary_conv_value)  AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries sq       ON sqp.query_id   = sq.id
        JOIN keyword_instances ki    ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE sqp.instance_id IN ({placeholders})
          AND sqp.campaign_id = ?
          AND sqp.device      = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sqp.instance_id, sqp.query_id
    """, conn, params=instance_ids + [TIER_TWO_CAMPAIGN_ID,
                                       start_date, end_date])

    if df.empty:
        return df

    # Override ad_group_name and match_type from CSV (authoritative for TIER TWO)
    csv_lookup = csv_df[['criterion_id', 'ad_group_name', 'match_type']].copy()
    df = df.merge(csv_lookup, on='criterion_id', how='left')

    # Sort by CSV ad_group_name -> keyword -> spend desc
    df = df.sort_values(['ad_group_name', 'keyword', 'spend'],
                        ascending=[True, True, False]).reset_index(drop=True)
    return df


def build_search_query_sheet(df: pd.DataFrame,
                              kw_df: pd.DataFrame,
                              days: int) -> pd.DataFrame:
    """
    One row per search query × keyword.
    Includes derived metrics and a Waste Flag.
    Grouped by ad group then keyword, sorted by spend desc within each keyword.
    """
    if df.empty:
        return pd.DataFrame([{'Note': f'No search query data for {days}d window'}])

    import numpy as np

    # Merge keyword ROAS in for comparison
    kw_roas = kw_df[['instance_id','roas']].rename(columns={'roas':'kw_roas'})
    df = df.merge(kw_roas, on='instance_id', how='left')

    def sf(v):
        try: return float(v or 0)
        except: return 0.0

    df['query_ctr']        = np.where(df['impressions'] > 0,
                                       df['clicks'] / df['impressions'] * 100, np.nan)
    df['query_cpc']        = np.where(df['clicks'] > 0,
                                       df['spend'] / df['clicks'], np.nan)
    df['query_roas']       = np.where(df['spend'] > 0,
                                       df['conv_value'] / df['spend'], np.nan)
    df['query_conv_rate']  = np.where(df['clicks'] > 0,
                                       df['conversions'] / df['clicks'] * 100, np.nan)
    df['roas_vs_kw_pct']   = np.where(
        (~pd.isna(df['query_roas'])) & (~pd.isna(df['kw_roas'])) & (df['kw_roas'] > 0),
        df['query_roas'] / df['kw_roas'] * 100, np.nan)

    # Per-query HHI contribution: (query_share)^2
    # Shows how much each query contributes to the keyword's concentration score
    kw_rev_totals  = df.groupby('instance_id')['conv_value'].transform('sum')
    kw_impr_totals = df.groupby('instance_id')['impressions'].transform('sum')
    df['query_rev_share']  = np.where(kw_rev_totals  > 0,
                                       df['conv_value']  / kw_rev_totals,  np.nan)
    df['query_impr_share'] = np.where(kw_impr_totals > 0,
                                       df['impressions'] / kw_impr_totals, np.nan)
    df['hhi_rev_contribution']  = df['query_rev_share']  ** 2
    df['hhi_impr_contribution'] = df['query_impr_share'] ** 2

    # Per-query HHI contribution at AD GROUP level
    # Same query_text may match multiple keywords within an ad group,
    # so first aggregate revenue and impressions by (ad_group_name, query_text)
    # then compute each query's share of its ad group total.
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
        'query_text':               'Search Query',
        'match_type_served':        'Match Type Served',
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

    rnd = {'CTR %':2,'Spend ($)':2,'CPC ($)':2,'Conversions':1,
           'Revenue ($)':2,'Query ROAS':2,'Conv Rate %':2,'Query ROAS % of KW':1,
           'Rev Share of KW':4,'Impr Share of KW':4,
           'HHI Rev Contribution (KW)':6,'HHI Impr Contribution (KW)':6,
           'Rev Share of AG':4,'Impr Share of AG':4,
           'HHI Rev Contribution (AG)':6,'HHI Impr Contribution (AG)':6}
    for col, dec in rnd.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    # Sort: ad group → keyword → spend desc
    df_out = df_out.sort_values(
        ['Ad Group','Keyword','Spend ($)'], ascending=[True,True,False]
    ).reset_index(drop=True)

    return df_out


def build_sheet(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([{'Note': f'No data for {days}d window'}])

    col_map = {
        'ad_group_name':    'Ad Group',
        'keyword':          'Keyword',
        'match_type':       'Match Type',
        'brand':            'Brand',
        'kw_status':        'KW Status',
        'roas_bucket':      'ROAS Bucket',
        'impressions':      'Impressions',
        'clicks':           'Clicks',
        'ctr_pct':          'CTR %',
        'spend':            'Spend ($)',
        'cpc':              'CPC ($)',
        'conversions':      'Offline Convs',
        'conv_value':       'Revenue ($)',
        'roas':             'ROAS',
        'cpl':              'CPL ($)',
        'conv_rate_pct':    'Conv Rate %',
        'value_per_click':  'Value / Click',
        'pct_ag_spend':     '% of AG Spend',
        'pct_ag_revenue':   '% of AG Revenue',
        'roas_vs_ag_pct':   'KW ROAS % of AG',
        'zero_conv_queries':'Zero-Conv Queries',
        'converting_queries':'Converting Queries',
        'zero_conv_spend':  'Zero-Conv Spend ($)',
        'converting_rev':   'Converting Rev ($)',
        'hhi_revenue':      'HHI (Revenue)',
        'hhi_impressions':  'HHI (Impressions)',
        'top_search_query': 'Top Search Query',
        'top_query_rev_pct':'Top Query Rev %',
        'is_exact_match':   'Is Exact Match',
        'exact_exists':     'Exact Exists',
        'geo_query_pct':    'Geo Query %',
        'near_me_pct':      'Near Me Query %',
        'top_waste_queries':'Top Waste Queries',
    }

    df_out = df[[c for c in col_map if c in df.columns]].rename(columns=col_map)

    num_rounds = {
        'CTR %':2,'Spend ($)':2,'CPC ($)':2,'Offline Convs':1,
        'Revenue ($)':2,'ROAS':2,'CPL ($)':2,'Conv Rate %':2,
        'Value / Click':2,'% of AG Spend':1,'% of AG Revenue':1,
        'KW ROAS % of AG':1,'Zero-Conv Spend ($)':2,'Converting Rev ($)':2,
        'HHI (Revenue)':4,'HHI (Impressions)':4,
        'Top Query Rev %':1,'Geo Query %':1,'Near Me Query %':1,
    }
    for col, dec in num_rounds.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    result_rows = []
    for ag_name, group in df.groupby('ad_group_name', sort=True):
        # Ad group subtotal row
        ag_sp   = group['spend'].sum()
        ag_rev  = group['conv_value'].sum()
        ag_conv = group['conversions'].sum()
        ag_cl   = group['clicks'].sum()
        ag_im   = group['impressions'].sum()

        sub = {
            'Ad Group':         f'── {ag_name} (subtotal)',
            'Keyword':          '',
            'Match Type':       '',
            'Brand':            '',
            'KW Status':        '',
            'ROAS Bucket':      '',
            'Impressions':      int(ag_im),
            'Clicks':           int(ag_cl),
            'CTR %':            round(ag_cl/ag_im*100,2) if ag_im>0 else None,
            'Spend ($)':        round(ag_sp, 2),
            'CPC ($)':          round(ag_sp/ag_cl,2) if ag_cl>0 else None,
            'Offline Convs':    round(ag_conv, 1),
            'Revenue ($)':      round(ag_rev, 2),
            'ROAS':             round(ag_rev/ag_sp,2) if ag_sp>0 else None,
            'CPL ($)':          round(ag_sp/ag_conv,2) if ag_conv>0 else None,
            'Conv Rate %':      round(ag_conv/ag_cl*100,2) if ag_cl>0 else None,
            'Value / Click':    round(ag_rev/ag_cl,2) if ag_cl>0 else None,
            '% of AG Spend':    100.0,
            '% of AG Revenue':  100.0,
            'KW ROAS % of AG':  100.0,
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

    SUB_FILL    = PatternFill("solid", fgColor="1F4E79")
    SUB_FONT    = Font(bold=True, color="FFFFFF", size=10)
    HDR_FILL    = PatternFill("solid", fgColor="7030A0")
    HDR_FONT    = Font(bold=True, color="FFFFFF", size=10)
    CRIT_FILL   = PatternFill("solid", fgColor="FFCCCC")
    WATCH_FILL  = PatternFill("solid", fgColor="FFF2CC")
    HLTHY_FILL  = PatternFill("solid", fgColor="E2EFDA")
    THIN        = Border(
        bottom=Side(style='thin', color='D9D9D9'),
        right= Side(style='thin', color='D9D9D9'),
    )

    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        if ws.max_row < 2:
            continue

        headers = [ws.cell(1, c).value
                   for c in range(1, ws.max_column + 1)]

        for cell in ws[1]:
            cell.fill      = HDR_FILL
            cell.font      = HDR_FONT
            cell.alignment = Alignment(horizontal='center', wrap_text=True)

        ag_idx     = headers.index('Ad Group')     + 1 if 'Ad Group'     in headers else None
        bucket_idx = headers.index('ROAS Bucket')  + 1 if 'ROAS Bucket'  in headers else None

        for row in ws.iter_rows(min_row=2):
            is_sub = ag_idx and str(row[ag_idx-1].value or '').startswith('──')
            if is_sub:
                for cell in row:
                    cell.fill = SUB_FILL
                    cell.font = SUB_FONT
            elif bucket_idx:
                bval = str(row[bucket_idx-1].value or '')
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
            'Ad Group':38,'Keyword':36,'Match Type':10,'Brand':12,
            'KW Status':10,'ROAS Bucket':24,
        }
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 13)

        ws.freeze_panes = 'A2'


# ── Main ──────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="TIER TWO keyword analysis."
    )
    parser.add_argument(
        "--end-date", default=None, metavar="YYYY-MM-DD",
        help="Analysis end date (default: yesterday). Set this to match "
             "your CSV export end date so all windows are consistent. "
             "Example: --end-date 2026-04-08"
    )
    args = parser.parse_args()

    global END_DATE
    if args.end_date:
        END_DATE = date.fromisoformat(args.end_date)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("Tier Two Keyword Analysis")
    print(f"  Campaign : {TIER_TWO_CAMPAIGN}")
    print(f"  Account  : {GOOGLE_ACCOUNT_ID}")
    print(f"  End date : {END_DATE}")
    print(f"  Windows  : 30 / 60 / 90 days")
    print("=" * 65)

    # Locate CSV -- check analysis/input/ first, then uploads fallback
    csv_path = CSV_PATH
    if not csv_path.exists():
        fallback = Path(r"C:\Projects\google-ads-ops\Search_keyword_report.csv")
        if fallback.exists():
            csv_path = fallback
        else:
            print(f"\n⚠ CSV not found at {csv_path}")
            print(f"  Copy the Google Ads keyword export to:")
            print(f"  {CSV_PATH}")
            return

    print(f"\nCSV: {csv_path}")
    csv_df = load_csv_keywords(csv_path)
    if csv_df.empty:
        print("⚠ No keywords loaded from CSV.")
        return

    conn   = get_conn()
    sheets = {}

    for days in [30, 60, 90]:
        print(f"\n[{days}d] Fetching keyword performance...")
        raw          = fetch_keywords(conn, csv_df, days)
        with_metrics = add_derived_metrics(raw)

        # Query summary columns for keyword sheet
        instance_ids = with_metrics['instance_id'].dropna().astype(int).tolist()
        q_summary    = fetch_query_summary(conn, instance_ids, csv_df, days)
        if not q_summary.empty:
            with_metrics = with_metrics.merge(q_summary, on='instance_id', how='left')
            for col in ['zero_conv_queries','converting_queries',
                        'zero_conv_spend','converting_rev']:
                with_metrics[col] = with_metrics[col].fillna(0)

        sheet = build_sheet(with_metrics, days)

        kw_rows = sheet[~sheet['Ad Group'].str.startswith('──', na=False)]
        tot_sp  = kw_rows['Spend ($)'].sum() if 'Spend ($)' in kw_rows else 0
        tot_rev = kw_rows['Revenue ($)'].sum() if 'Revenue ($)' in kw_rows else 0
        print(f"  Spend: ${tot_sp:,.2f}  |  Revenue: ${tot_rev:,.2f}")
        if 'ROAS Bucket' in kw_rows.columns:
            for bkt, cnt in kw_rows['ROAS Bucket'].value_counts().items():
                print(f"    {bkt}: {cnt}")

        sheets[f'Keyword Analysis ({days}d)'] = sheet

        # Search query sheet — 30d only to keep file size manageable
        if days == 30:
            print(f"  Fetching search query detail...")
            sq_raw = fetch_search_query_detail(conn, instance_ids, csv_df, days)
            if not sq_raw.empty:
                print(f"    {len(sq_raw):,} query rows, "
                      f"{sq_raw['query_text'].nunique():,} unique queries")
            sheets['Search Query Analysis (30d)'] = build_search_query_sheet(
                sq_raw, with_metrics, days
            )

    conn.close()

    print(f"\nWriting: {OUTPUT_FILE}")
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {OUTPUT_FILE.stat().st_size/1024:.1f} KB")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
