"""
keyword_opportunity_sizing.py
==============================
Sizes the GP/day impact of two paid-search initiatives for the
Home Insurance account (account_id = 1 Google, account_id = 2 Microsoft):

  CASE 1 — Bundle reintroduction
            "home and auto", "car and home", "bundle", and variants.
            Identification: regex on keyword text with a deny-list
            (modifier_bundle tag is unreliable).
            Sizing intent: historical active-period GP/day as a sanity
            check against currently-live performance.

  CASE 2 — USAA reintroduction
            USAA is a competitor brand currently negated across the
            account. This sizes what historical performance looked
            like when USAA-branded keywords ran.
            Identification: keyword_identities.modifier_brand = 'usaa'
            UNION a text-based \\busaa\\b match for sanity.

DATA STRATEGY
─────────────
Fully DB-driven from keyword_performance_daily (the SQ tables are
~6 weeks stale per ops note). Window: 2025-01-01 → latest report_date.

GP DEFINITION
─────────────
  Google    (acct 1): GP = primary_conv_value − spend
  Microsoft (acct 2): GP = all_conv_value     − spend
  Computed at the row level so platform routing is correct in the
  per-day timeseries, not just at aggregate.

OUTPUT — one workbook, sheets:
  Metadata
  Bundle KW Matches              (audit: every regex hit, lifetime spend)
  Bundle Daily Timeseries        (date × platform: spend/rev/GP/ROAS)
  Bundle Active Period Summary   (avg GP/day during active weeks)
  Bundle Keyword Detail          (per-keyword lifetime stats + ROAS bucket)
  USAA KW Matches
  USAA Daily Timeseries
  USAA Active Period Summary
  USAA Keyword Detail
  Sizing Summary                 (point estimates, both cases)

RUN
───
  conda activate paid-search
  python analysis/keyword_opportunity_sizing.py [--end-date YYYY-MM-DD]
"""

import re
import sqlite3
import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta, datetime

# ── Configuration ──────────────────────────────────────────────────
DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR  = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_FILE = OUTPUT_DIR / "keyword_opportunity_sizing.xlsx"

# Account scope: Home Insurance only (Google + Microsoft mirror)
ACCOUNT_IDS = [1, 2]

# Window start — kept as a top-level constant for easy adjustment
WINDOW_START = "2025-01-01"

# ROAS bucketing thresholds — match home_insurance_keyword_analysisV3.py
CRITICAL_ROAS = 0.50
WATCH_ROAS    = 0.70
MIN_SPEND     = 5.00

# Active-period definition: a keyword instance is considered "active" on a
# date if it had any impressions or spend that day. We aggregate at the
# initiative-level (bundle / USAA), so a day is an "active day" for the
# initiative if at least 1 in-scope instance had activity.
MIN_ACTIVE_DAILY_SPEND = 1.0   # daily spend floor to count a day as active


# ── Bundle keyword regex ──────────────────────────────────────────
# The team's modifier_bundle tag is known to false-positive. This regex
# is deliberately tighter than the tagger and targeted at the linguistic
# patterns we know carry bundle intent in this vertical.
BUNDLE_PATTERN = re.compile(
    r'\b('
    # Explicit bundle word
    r'bundle\w*'
    # home + auto (and synonyms) in either order, with and/&/+/with/plus connectors
    r'|home\s*(?:and|&|\+|with|plus|\/|,)\s*(?:auto|car|vehicle)'
    r'|(?:auto|car|vehicle)\s*(?:and|&|\+|with|plus|\/|,)\s*home'
    r'|homeowners?\s*(?:and|&|\+|with|plus|\/|,)\s*(?:auto|car|vehicle)'
    r'|(?:auto|car|vehicle)\s*(?:and|&|\+|with|plus|\/|,)\s*homeowners?'
    # Multi-policy / multi-line
    r'|multi[\s\-]?polic\w*'
    r'|multi[\s\-]?line\w*'
    # Combine + (home/auto/car/policies)
    r'|combin\w*\s+(?:home|auto|car|polic\w*|insurance)'
    r'|combin\w*\s+(?:my\s+)?(?:home|auto|car|polic\w*)'
    # Discount-led bundle phrasing
    r'|(?:home|auto|car)\s+bundle\s+discount'
    r'|bundle\s+discount'
    r')\b',
    re.IGNORECASE
)

# Deny-list: phrases that match BUNDLE_PATTERN but are NOT bundle intent.
# Seeded with known false-positive patterns; you should review the
# "Bundle KW Matches" sheet and append confirmed FPs here.
#
# Format: regex patterns. A keyword whose text matches ANY of these is
# excluded from the bundle set even if it matches BUNDLE_PATTERN.
BUNDLE_DENY_LIST = [
    re.compile(r'\bbundle\s+of\s+joy\b', re.IGNORECASE),
    re.compile(r'\bsoftware\s+bundle\b', re.IGNORECASE),
    re.compile(r'\bcable\s+bundle\b', re.IGNORECASE),
    re.compile(r'\binternet\s+bundle\b', re.IGNORECASE),
    re.compile(r'\btv\s+bundle\b', re.IGNORECASE),
    re.compile(r'\bphone\s+bundle\b', re.IGNORECASE),
    # Add confirmed FPs here after first run, e.g.:
    # re.compile(r'\bbundle\s+monster\b', re.IGNORECASE),
]


# ── USAA keyword identification ───────────────────────────────────
# Primary: modifier_brand = 'usaa' on keyword_identities.
# Sanity:  also surface keyword_text matches of \busaa\b that DON'T have
#          modifier_brand = 'usaa', to catch any tagging gaps.
USAA_TEXT_PATTERN = re.compile(r'\busaa\b', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def chunked_query(conn, sql_template: str, ids: list,
                  extra_params: list = None,
                  chunk_size: int = 900) -> pd.DataFrame:
    """Mirrors the chunked_query helper in V3 — handles SQLite's variable cap."""
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
                                        params=list(chunk) + extra_params))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def get_data_window_end(conn) -> str:
    """Latest date present in keyword_performance_daily for our accounts."""
    sql = f"""
        SELECT MAX(kpd.report_date) AS max_date
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        WHERE ki.account_id IN ({','.join('?' * len(ACCOUNT_IDS))})
    """
    row = conn.execute(sql, ACCOUNT_IDS).fetchone()
    return row['max_date'] if row and row['max_date'] else date.today().isoformat()


def roas_bucket(spend: float, roas: float) -> str:
    if spend < MIN_SPEND:               return 'Insufficient spend'
    if pd.isna(roas):                   return 'No conversions'
    if roas < CRITICAL_ROAS:            return f'Critical (ROAS < {CRITICAL_ROAS})'
    if roas < WATCH_ROAS:               return f'Watch (ROAS {CRITICAL_ROAS}-{WATCH_ROAS})'
    return f'Healthy (ROAS >= {WATCH_ROAS})'


# ── Keyword identification ────────────────────────────────────────
def find_bundle_keywords(conn) -> pd.DataFrame:
    """
    Returns one row per (account_id, identity_id, instance_id) for bundle
    keywords. A keyword qualifies if its normalized_text matches
    BUNDLE_PATTERN AND does not match any pattern in BUNDLE_DENY_LIST.

    Includes the modifier_bundle tag value for transparency, but does NOT
    rely on it for filtering (per ops note: it's unreliable).
    """
    sql = f"""
        SELECT
            ki.id                      AS instance_id,
            ki.account_id,
            ki.platform,
            ki.identity_id,
            ki.criterion_id,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.campaign_id,
            ki.campaign_name,
            ki.status                  AS instance_status,
            ki.first_seen_date,
            ki.last_seen_date,
            ki_id.normalized_text      AS keyword_text,
            ki_id.match_type,
            ki_id.modifier_bundle      AS bundle_tag,
            ki_id.modifier_brand       AS brand_tag
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id IN ({','.join('?' * len(ACCOUNT_IDS))})
    """
    df = pd.read_sql_query(sql, conn, params=ACCOUNT_IDS)
    print(f"  Loaded {len(df):,} keyword instances for accounts {ACCOUNT_IDS}")

    if df.empty:
        return df

    # Apply regex match
    df['regex_match']  = df['keyword_text'].fillna('').apply(
        lambda t: bool(BUNDLE_PATTERN.search(t)))

    # Apply deny-list
    def is_denied(text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in BUNDLE_DENY_LIST)
    df['deny_listed']  = df['keyword_text'].fillna('').apply(is_denied)

    # Final qualifier
    df['qualifies']    = df['regex_match'] & ~df['deny_listed']

    matched = df[df['qualifies']].copy()
    print(f"  Bundle regex hits: {df['regex_match'].sum():,}")
    print(f"  Deny-listed:       {df['deny_listed'].sum():,}")
    print(f"  Qualifying bundle instances: {len(matched):,}")
    print(f"    Tagged modifier_bundle=1: {(matched['bundle_tag'] == 1).sum():,} "
          f"({(matched['bundle_tag'] == 1).mean() * 100:.0f}% overlap with tag)")

    return df  # return ALL with flags so the audit sheet shows non-matches too


def find_usaa_keywords(conn) -> pd.DataFrame:
    """
    Returns one row per (account_id, instance_id) for USAA keywords.
    Identified primarily by modifier_brand = 'usaa', with a text-based
    sanity flag for any \\busaa\\b matches that aren't tagged.
    """
    sql = f"""
        SELECT
            ki.id                      AS instance_id,
            ki.account_id,
            ki.platform,
            ki.identity_id,
            ki.criterion_id,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.campaign_id,
            ki.campaign_name,
            ki.status                  AS instance_status,
            ki.first_seen_date,
            ki.last_seen_date,
            ki_id.normalized_text      AS keyword_text,
            ki_id.match_type,
            LOWER(COALESCE(ki_id.modifier_brand, '')) AS brand_tag,
            ki_id.brand_category       AS brand_category,
            ki_id.tag_method           AS tag_method
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id IN ({','.join('?' * len(ACCOUNT_IDS))})
    """
    df = pd.read_sql_query(sql, conn, params=ACCOUNT_IDS)

    if df.empty:
        return df

    df['brand_tag_match'] = df['brand_tag'].str.strip().eq('usaa')
    df['text_match']      = df['keyword_text'].fillna('').apply(
        lambda t: bool(USAA_TEXT_PATTERN.search(t)))
    df['qualifies']       = df['brand_tag_match'] | df['text_match']

    matched = df[df['qualifies']].copy()
    print(f"  USAA brand-tag matches:          {df['brand_tag_match'].sum():,}")
    print(f"  USAA text-only matches:          {(df['text_match'] & ~df['brand_tag_match']).sum():,}")
    print(f"  Tagged but no text \\busaa\\b:    "
          f"{(df['brand_tag_match'] & ~df['text_match']).sum():,}")
    print(f"  Qualifying USAA instances:       {len(matched):,}")

    return df  # full set with flags


# ── Performance fetch ─────────────────────────────────────────────
def fetch_daily_performance(conn, instance_ids: list,
                            start_date: str, end_date: str) -> pd.DataFrame:
    """
    Pulls daily performance for the supplied instance_ids.
    Returns one row per (instance_id, report_date) with both
    primary_conv_value and all_conv_value so platform-aware GP can be
    computed downstream.
    """
    if not instance_ids:
        return pd.DataFrame()

    sql = """
        SELECT
            kpd.instance_id,
            kpd.report_date,
            COALESCE(SUM(kpd.impressions),         0)  AS impressions,
            COALESCE(SUM(kpd.clicks),              0)  AS clicks,
            COALESCE(SUM(kpd.cost_micros),         0) / 1e6   AS spend,
            COALESCE(SUM(kpd.primary_conv_count),  0)  AS primary_conv_count,
            COALESCE(SUM(kpd.primary_conv_value),  0)  AS primary_conv_value,
            COALESCE(SUM(kpd.all_conv_count),      0)  AS all_conv_count,
            COALESCE(SUM(kpd.all_conv_value),      0)  AS all_conv_value
        FROM keyword_performance_daily kpd
        WHERE kpd.instance_id IN ({placeholders})
          AND kpd.device      = 'ALL'
          AND kpd.geo_state   = 'ALL'
          AND kpd.report_date BETWEEN ? AND ?
        GROUP BY kpd.instance_id, kpd.report_date
    """
    df = chunked_query(conn, sql, instance_ids,
                       extra_params=[start_date, end_date])

    return df


def attach_platform_revenue(perf_df: pd.DataFrame,
                            kw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Joins instance-level platform/account info onto daily performance and
    computes platform-aware revenue + GP.

      Google    (account_id=1): revenue = primary_conv_value
      Microsoft (account_id=2): revenue = all_conv_value
      GP = revenue - spend
    """
    if perf_df.empty:
        return perf_df

    keep = ['instance_id', 'account_id', 'platform', 'campaign_id',
            'campaign_name', 'ad_group_id', 'ad_group_name',
            'keyword_text', 'match_type']
    keep = [c for c in keep if c in kw_df.columns]
    out  = perf_df.merge(kw_df[keep].drop_duplicates('instance_id'),
                         on='instance_id', how='left')

    # Platform-aware revenue
    out['revenue'] = np.where(
        out['account_id'] == 2,
        out['all_conv_value'],
        out['primary_conv_value']
    )
    out['conv_count'] = np.where(
        out['account_id'] == 2,
        out['all_conv_count'],
        out['primary_conv_count']
    )
    out['gp']   = out['revenue'] - out['spend']
    out['roas'] = np.where(out['spend'] > 0, out['revenue'] / out['spend'], np.nan)

    return out


# ── Sheet builders ────────────────────────────────────────────────
def build_kw_matches_sheet(kw_df: pd.DataFrame, perf_df: pd.DataFrame,
                           initiative: str) -> pd.DataFrame:
    """
    Audit sheet: every keyword instance considered, with match flags and
    its lifetime in-window performance. Use this to spot deny-list
    candidates (regex matches that shouldn't have qualified) and missed
    keywords (text matches without the brand/bundle tag).
    """
    if kw_df.empty:
        return pd.DataFrame([{'Note': f'No {initiative} keyword candidates found'}])

    # Lifetime in-window performance per instance
    if not perf_df.empty:
        agg = perf_df.groupby('instance_id', as_index=False).agg(
            spend       =('spend',       'sum'),
            clicks      =('clicks',      'sum'),
            impressions =('impressions', 'sum'),
            revenue     =('revenue',     'sum'),
            gp          =('gp',          'sum'),
            conv_count  =('conv_count',  'sum'),
            active_days =('report_date', 'nunique'),
        )
    else:
        agg = pd.DataFrame(columns=['instance_id', 'spend', 'clicks',
                                    'impressions', 'revenue', 'gp',
                                    'conv_count', 'active_days'])

    out = kw_df.merge(agg, on='instance_id', how='left')
    for col in ['spend', 'clicks', 'impressions', 'revenue', 'gp',
                'conv_count', 'active_days']:
        if col in out.columns:
            out[col] = out[col].fillna(0)

    out['roas'] = np.where(out['spend'] > 0, out['revenue'] / out['spend'], np.nan)
    out['roas_bucket'] = out.apply(
        lambda r: roas_bucket(r['spend'], r['roas']), axis=1)

    # Order: qualifying first, then by spend descending
    out = out.sort_values(['qualifies', 'spend'],
                          ascending=[False, False]).reset_index(drop=True)

    # Display columns
    display_cols = [
        'qualifies', 'account_id', 'platform', 'campaign_name',
        'ad_group_name', 'keyword_text', 'match_type', 'instance_status',
        'first_seen_date', 'last_seen_date',
    ]
    if initiative == 'bundle':
        display_cols += ['regex_match', 'deny_listed', 'bundle_tag']
    else:
        display_cols += ['brand_tag_match', 'text_match', 'brand_tag',
                         'brand_category', 'tag_method']
    display_cols += ['active_days', 'impressions', 'clicks',
                     'spend', 'revenue', 'gp', 'conv_count',
                     'roas', 'roas_bucket']

    out = out[[c for c in display_cols if c in out.columns]]

    # Round numerics for display
    for col in ['spend', 'revenue', 'gp', 'roas']:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(2)

    return out


def build_daily_timeseries(perf_df: pd.DataFrame) -> pd.DataFrame:
    """
    Daily performance aggregated by platform.
    Columns: report_date, platform, active_instances, impressions, clicks,
             spend, revenue, gp, roas
    """
    if perf_df.empty:
        return pd.DataFrame([{'Note': 'No performance data in window'}])

    grp = perf_df.groupby(['report_date', 'platform'], as_index=False).agg(
        active_instances=('instance_id', 'nunique'),
        impressions     =('impressions', 'sum'),
        clicks          =('clicks',      'sum'),
        spend           =('spend',       'sum'),
        revenue         =('revenue',     'sum'),
        gp              =('gp',          'sum'),
        conv_count      =('conv_count',  'sum'),
    )
    grp['roas'] = np.where(grp['spend'] > 0,
                            grp['revenue'] / grp['spend'], np.nan)
    grp = grp.sort_values(['report_date', 'platform']).reset_index(drop=True)

    for col in ['spend', 'revenue', 'gp', 'roas']:
        grp[col] = pd.to_numeric(grp[col], errors='coerce').round(2)

    return grp


def build_active_period_summary(perf_df: pd.DataFrame,
                                 window_start: str,
                                 window_end: str) -> pd.DataFrame:
    """
    Summary by platform of:
      - Total active days (days with spend >= MIN_ACTIVE_DAILY_SPEND)
      - Total window days
      - Activity rate (active / total)
      - Total spend, revenue, GP, conv_count
      - Avg daily GP during ACTIVE days only (the sizing metric)
      - Avg daily GP across ALL days (for contrast)
      - Lifetime ROAS
    """
    if perf_df.empty:
        return pd.DataFrame([{'Note': 'No performance data in window'}])

    # Per-platform-per-day aggregation
    daily = perf_df.groupby(['report_date', 'platform'], as_index=False).agg(
        spend  =('spend',   'sum'),
        revenue=('revenue', 'sum'),
        gp     =('gp',      'sum'),
        conv_count=('conv_count', 'sum'),
    )

    window_days = (datetime.fromisoformat(window_end)
                   - datetime.fromisoformat(window_start)).days + 1

    rows = []
    for platform, sub in daily.groupby('platform'):
        active = sub[sub['spend'] >= MIN_ACTIVE_DAILY_SPEND]
        active_days = len(active)
        total_spend   = sub['spend'].sum()
        total_revenue = sub['revenue'].sum()
        total_gp      = sub['gp'].sum()
        total_conv    = sub['conv_count'].sum()

        rows.append({
            'platform':                 platform,
            'window_start':             window_start,
            'window_end':               window_end,
            'window_days':              window_days,
            'active_days':              active_days,
            'activity_rate_pct':        round(active_days / window_days * 100, 1)
                                        if window_days else 0,
            'total_spend':              round(total_spend,   2),
            'total_revenue':            round(total_revenue, 2),
            'total_gp':                 round(total_gp,      2),
            'total_conversions':        round(total_conv,    2),
            'lifetime_roas':            round(total_revenue / total_spend, 3)
                                        if total_spend else 0,
            'avg_daily_gp_active':      round(active['gp'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_spend_active':   round(active['spend'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_revenue_active': round(active['revenue'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_gp_all':         round(total_gp / window_days, 2)
                                        if window_days else 0,
        })

    # Combined row (both platforms summed per day)
    combined = daily.groupby('report_date', as_index=False).agg(
        spend=('spend','sum'), revenue=('revenue','sum'),
        gp=('gp','sum'), conv_count=('conv_count','sum'))
    active_combined = combined[combined['spend'] >= MIN_ACTIVE_DAILY_SPEND]
    rows.append({
        'platform':                 'TOTAL (all platforms)',
        'window_start':             window_start,
        'window_end':               window_end,
        'window_days':              window_days,
        'active_days':              len(active_combined),
        'activity_rate_pct':        round(len(active_combined) / window_days * 100, 1)
                                    if window_days else 0,
        'total_spend':              round(combined['spend'].sum(),    2),
        'total_revenue':            round(combined['revenue'].sum(),  2),
        'total_gp':                 round(combined['gp'].sum(),       2),
        'total_conversions':        round(combined['conv_count'].sum(), 2),
        'lifetime_roas':            round(combined['revenue'].sum()
                                          / combined['spend'].sum(), 3)
                                    if combined['spend'].sum() else 0,
        'avg_daily_gp_active':      round(active_combined['gp'].mean(), 2)
                                    if len(active_combined) else 0,
        'avg_daily_spend_active':   round(active_combined['spend'].mean(), 2)
                                    if len(active_combined) else 0,
        'avg_daily_revenue_active': round(active_combined['revenue'].mean(), 2)
                                    if len(active_combined) else 0,
        'avg_daily_gp_all':         round(combined['gp'].sum() / window_days, 2)
                                    if window_days else 0,
    })

    return pd.DataFrame(rows)


def build_keyword_detail(kw_df: pd.DataFrame, perf_df: pd.DataFrame,
                         qualifying_only: bool = True) -> pd.DataFrame:
    """Per-keyword lifetime stats with ROAS bucket. One row per instance."""
    if qualifying_only:
        kw_df = kw_df[kw_df['qualifies']].copy()

    if kw_df.empty or perf_df.empty:
        return pd.DataFrame([{'Note': 'No qualifying keywords with performance'}])

    agg = perf_df.groupby('instance_id', as_index=False).agg(
        impressions =('impressions', 'sum'),
        clicks      =('clicks',      'sum'),
        spend       =('spend',       'sum'),
        revenue     =('revenue',     'sum'),
        gp          =('gp',          'sum'),
        conv_count  =('conv_count',  'sum'),
        active_days =('report_date', 'nunique'),
        first_active=('report_date', 'min'),
        last_active =('report_date', 'max'),
    )
    out = kw_df.merge(agg, on='instance_id', how='left')
    for col in ['impressions', 'clicks', 'spend', 'revenue', 'gp',
                'conv_count', 'active_days']:
        if col in out.columns:
            out[col] = out[col].fillna(0)

    out['roas'] = np.where(out['spend'] > 0, out['revenue'] / out['spend'], np.nan)
    out['cpc']  = np.where(out['clicks'] > 0, out['spend'] / out['clicks'], np.nan)
    out['conv_rate_pct'] = np.where(
        out['clicks'] > 0,
        out['conv_count'] / out['clicks'] * 100, np.nan)
    out['roas_bucket'] = out.apply(
        lambda r: roas_bucket(r['spend'], r['roas']), axis=1)

    cols = [
        'account_id', 'platform', 'campaign_name', 'ad_group_name',
        'keyword_text', 'match_type', 'instance_status',
        'first_seen_date', 'last_seen_date',
        'first_active', 'last_active', 'active_days',
        'impressions', 'clicks', 'cpc',
        'spend', 'revenue', 'gp',
        'conv_count', 'conv_rate_pct',
        'roas', 'roas_bucket',
    ]
    out = out[[c for c in cols if c in out.columns]]
    out = out.sort_values('spend', ascending=False).reset_index(drop=True)

    for col in ['cpc', 'spend', 'revenue', 'gp', 'roas', 'conv_rate_pct']:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(2)

    return out


def build_metadata_sheet(window_start: str, window_end: str,
                         bundle_kw_count: int, usaa_kw_count: int) -> pd.DataFrame:
    rows = [
        {'Parameter': 'Run timestamp',
         'Value': datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {'Parameter': 'Window start',          'Value': window_start},
        {'Parameter': 'Window end',            'Value': window_end},
        {'Parameter': 'Account scope',
         'Value': f'Home Insurance (account_ids = {ACCOUNT_IDS})'},
        {'Parameter': '', 'Value': ''},
        {'Parameter': 'GP — Google (acct 1)',
         'Value': 'primary_conv_value − spend'},
        {'Parameter': 'GP — Microsoft (acct 2)',
         'Value': 'all_conv_value − spend'},
        {'Parameter': '', 'Value': ''},
        {'Parameter': 'CASE 1: Bundle ID method',
         'Value': 'Regex on keyword_text + deny-list (modifier_bundle ignored)'},
        {'Parameter': 'Bundle qualifying instances', 'Value': bundle_kw_count},
        {'Parameter': '', 'Value': ''},
        {'Parameter': 'CASE 2: USAA ID method',
         'Value': 'modifier_brand = "usaa" UNION text \\busaa\\b'},
        {'Parameter': 'USAA qualifying instances',  'Value': usaa_kw_count},
        {'Parameter': '', 'Value': ''},
        {'Parameter': 'ROAS thresholds',
         'Value': f'Critical < {CRITICAL_ROAS} | Watch < {WATCH_ROAS} | '
                  f'Healthy ≥ {WATCH_ROAS} | Min spend = ${MIN_SPEND}'},
        {'Parameter': 'Active day definition',
         'Value': f'Daily spend ≥ ${MIN_ACTIVE_DAILY_SPEND}'},
    ]
    return pd.DataFrame(rows)


def build_sizing_summary(bundle_summary: pd.DataFrame,
                         usaa_summary:  pd.DataFrame) -> pd.DataFrame:
    """
    One-page sizing answer pulling the 'TOTAL (all platforms)' row from
    each initiative's active-period summary.
    """
    rows = []

    def extract(df: pd.DataFrame, label: str):
        if df.empty or 'platform' not in df.columns:
            rows.append({'Initiative': label, 'Note': 'No data'})
            return
        total = df[df['platform'] == 'TOTAL (all platforms)']
        if total.empty:
            rows.append({'Initiative': label, 'Note': 'No total row'})
            return
        r = total.iloc[0]
        rows.append({
            'Initiative':                 label,
            'Window':                     f"{r['window_start']} → {r['window_end']}",
            'Window days':                int(r['window_days']),
            'Active days':                int(r['active_days']),
            'Activity rate (%)':          r['activity_rate_pct'],
            'Total spend ($)':            r['total_spend'],
            'Total revenue ($)':          r['total_revenue'],
            'Total GP ($)':               r['total_gp'],
            'Lifetime ROAS':              r['lifetime_roas'],
            'Avg daily GP — active ($)':  r['avg_daily_gp_active'],
            'Avg daily GP — full window ($)': r['avg_daily_gp_all'],
            'Avg daily spend — active ($)':   r['avg_daily_spend_active'],
        })

    extract(bundle_summary, 'CASE 1: Bundle (historical sanity check)')
    extract(usaa_summary,   'CASE 2: USAA (historical baseline)')

    notes = pd.DataFrame([
        {'Initiative': '',
         'Window': 'NOTES',
         'Window days': '',
         'Active days': '',
         'Activity rate (%)': '',
         'Total spend ($)': '',
         'Total revenue ($)': '',
         'Total GP ($)': '',
         'Lifetime ROAS': '',
         'Avg daily GP — active ($)': '',
         'Avg daily GP — full window ($)': '',
         'Avg daily spend — active ($)':
            'Bundle: pair this with current live perf since reintroduction. '
            'USAA: this is the baseline if reactivated at historical run-rate.'},
    ])
    return pd.concat([pd.DataFrame(rows), notes], ignore_index=True)


# ── Workbook formatting ───────────────────────────────────────────
def format_workbook(writer):
    """Light formatting: freeze top row, autosize columns to a sane cap."""
    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        ws.freeze_panes = 'A2'
        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None),
                         default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)


# ── Main ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--end-date', help='YYYY-MM-DD; defaults to latest in DB')
    parser.add_argument('--start-date', default=WINDOW_START,
                        help=f'YYYY-MM-DD; defaults to {WINDOW_START}')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    end_date   = args.end_date or get_data_window_end(conn)
    start_date = args.start_date

    print(f"\n=== Keyword Opportunity Sizing ===")
    print(f"Window: {start_date} → {end_date}")
    print(f"Accounts: {ACCOUNT_IDS} (Home Insurance Google + Microsoft)")

    # ── CASE 1: Bundle ────────────────────────────────────────────
    print(f"\n[Case 1] Identifying bundle keywords...")
    bundle_kw = find_bundle_keywords(conn)

    bundle_qualifying_iids = (bundle_kw[bundle_kw['qualifies']]['instance_id']
                              .dropna().astype(int).unique().tolist())
    print(f"\n[Case 1] Fetching daily performance for "
          f"{len(bundle_qualifying_iids):,} instances...")
    bundle_perf_raw = fetch_daily_performance(
        conn, bundle_qualifying_iids, start_date, end_date)
    bundle_perf     = attach_platform_revenue(bundle_perf_raw, bundle_kw)
    print(f"  {len(bundle_perf):,} daily performance rows")

    bundle_matches_sheet = build_kw_matches_sheet(bundle_kw, bundle_perf, 'bundle')
    bundle_timeseries    = build_daily_timeseries(bundle_perf)
    bundle_summary       = build_active_period_summary(
        bundle_perf, start_date, end_date)
    bundle_detail        = build_keyword_detail(bundle_kw, bundle_perf,
                                                qualifying_only=True)

    # ── CASE 2: USAA ──────────────────────────────────────────────
    print(f"\n[Case 2] Identifying USAA keywords...")
    usaa_kw = find_usaa_keywords(conn)

    usaa_qualifying_iids = (usaa_kw[usaa_kw['qualifies']]['instance_id']
                            .dropna().astype(int).unique().tolist())
    print(f"\n[Case 2] Fetching daily performance for "
          f"{len(usaa_qualifying_iids):,} instances...")
    usaa_perf_raw = fetch_daily_performance(
        conn, usaa_qualifying_iids, start_date, end_date)
    usaa_perf     = attach_platform_revenue(usaa_perf_raw, usaa_kw)
    print(f"  {len(usaa_perf):,} daily performance rows")

    usaa_matches_sheet = build_kw_matches_sheet(usaa_kw, usaa_perf, 'usaa')
    usaa_timeseries    = build_daily_timeseries(usaa_perf)
    usaa_summary       = build_active_period_summary(
        usaa_perf, start_date, end_date)
    usaa_detail        = build_keyword_detail(usaa_kw, usaa_perf,
                                              qualifying_only=True)

    # ── Sizing summary + metadata ─────────────────────────────────
    sizing_summary = build_sizing_summary(bundle_summary, usaa_summary)
    metadata = build_metadata_sheet(
        start_date, end_date,
        bundle_kw_count=len(bundle_qualifying_iids),
        usaa_kw_count=len(usaa_qualifying_iids))

    conn.close()

    # ── Write workbook ────────────────────────────────────────────
    sheets = {
        'Metadata':                       metadata,
        'Sizing Summary':                 sizing_summary,
        'Bundle KW Matches':              bundle_matches_sheet,
        'Bundle Daily Timeseries':        bundle_timeseries,
        'Bundle Active Period Summary':   bundle_summary,
        'Bundle Keyword Detail':          bundle_detail,
        'USAA KW Matches':                usaa_matches_sheet,
        'USAA Daily Timeseries':          usaa_timeseries,
        'USAA Active Period Summary':     usaa_summary,
        'USAA Keyword Detail':            usaa_detail,
    }

    print(f"\nWriting: {OUTPUT_FILE}")
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {OUTPUT_FILE.stat().st_size / 1024:.1f} KB")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
