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
                      Revenue is structurally zero before
                      MICROSOFT_REVENUE_VALID_FROM (2026-01-13) due to a
                      data plumbing gap. Pre-cutoff Microsoft rows are
                      flagged and excluded from sizing aggregates.
  Computed at the row level so platform routing is correct in the
  per-day timeseries, not just at aggregate.

OUTPUT — one workbook, sheets:
  Metadata
  Sizing Summary                 (point estimates incl. current live)
  Bundle KW Matches              (qualifying keywords only — full audit
                                  goes to a sibling CSV)
  Bundle Daily Timeseries        (date × platform: spend/rev/GP/ROAS,
                                  with revenue_valid flag)
  Bundle Active Period Summary   (avg GP/day during active weeks; MS
                                  split into pre/post revenue cutoff)
  Bundle Keyword Detail          (per-keyword lifetime stats + ROAS bucket)
  USAA KW Matches                (qualifying-only)
  USAA Daily Timeseries
  USAA Active Period Summary
  USAA Keyword Detail

SIBLING FILES
  *_bundle_kw_audit_full.csv     all 530k+ candidates with flags
  *_usaa_kw_audit_full.csv

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

# Microsoft revenue tracking didn't go live until this date. Before this,
# spend was real but revenue was structurally absent (data pipeline gap).
# Pre-cutoff rows for account_id=2 are flagged revenue_valid=False and
# excluded from GP/ROAS sizing aggregates so the artifact doesn't pollute
# the headline numbers.
MICROSOFT_REVENUE_VALID_FROM = "2026-01-13"

# "Current live" detection: any initiative whose last active date is within
# this many days of the data window end is treated as live, and a current
# run-rate row is emitted in Sizing Summary.
CURRENT_LIVE_LOOKBACK_DAYS = 30
CURRENT_LIVE_FRESHNESS_DAYS = 14   # last activity must be within N days

# Microsoft USAA extrapolation (Case 2 only):
# When MS pre-cutoff has conversions but no revenue (the data plumbing gap),
# we estimate revenue using:
#     est_revenue = MS_conversions × (Google_USAA_RPL_recent_qtr × HAIRCUT)
#
# The haircut accounts for Microsoft's all_conversions blending zip-submits
# (no revenue) with converted leads (full revenue). RPL is computed from
# Google's most recent quarter of USAA activity (most predictive baseline).
MS_USAA_RPL_HAIRCUT = 0.80   # 20% reduction
MS_USAA_RPL_QUARTER_DAYS = 90   # how far back to look for "recent quarter"

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

# SQL LIKE patterns — a SUPERSET of BUNDLE_PATTERN, used for SQL-side
# pre-filtering to drop the candidate set from ~534k → ~1k before any
# Python regex work. The Python regex above tightens this down further
# (LIKE doesn't enforce word boundaries). Keep this list in sync any time
# BUNDLE_PATTERN changes — easier to add a missing LIKE pattern (regex
# will reject FPs) than to debug missing keywords.
BUNDLE_LIKE_PATTERNS = [
    '%bundle%',
    '%home and auto%',  '%home & auto%',  '%home + auto%',
    '%auto and home%',  '%auto & home%',  '%auto + home%',
    '%home and car%',   '%car and home%',
    '%home and vehicle%', '%vehicle and home%',
    '%homeowners and auto%', '%auto and homeowners%',
    '%homeowners and car%',  '%car and homeowners%',
    '%home with auto%', '%auto with home%',
    '%home plus auto%', '%auto plus home%',
    '%multi polic%',  '%multi-polic%',
    '%multiline%',    '%multi line%',  '%multi-line%',
    '%combine home%', '%combine auto%', '%combine car%',
    '%combine my home%', '%combine my auto%', '%combine my car%',
    '%combine polic%', '%combine insurance%',
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
def find_bundle_keywords(conn, full_audit: bool = False) -> pd.DataFrame:
    """
    Returns one row per (account_id, identity_id, instance_id) for bundle
    keywords.

    PERFORMANCE: SQL filters with LIKE patterns (a SUPERSET of the regex)
    to drop the candidate set from ~534k → ~1k before Python touches it.
    Python regex + deny-list then prune that smaller set to the final
    qualifying set.

    full_audit=True returns ALL keyword_instances (regardless of LIKE
    match) — useful for confirming the LIKE patterns aren't excluding
    legitimate bundle keywords. Slow on real data.
    """
    base_select = """
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
    """

    acct_clause = f"ki.account_id IN ({','.join('?' * len(ACCOUNT_IDS))})"

    if full_audit:
        sql = base_select + f" WHERE {acct_clause}"
        df = pd.read_sql_query(sql, conn, params=ACCOUNT_IDS)
        print(f"  [full_audit] Loaded {len(df):,} candidate instances")
    else:
        # Build LIKE clause: a SUPERSET of BUNDLE_PATTERN. Python regex
        # below tightens it down to exact intent.
        like_clauses = " OR ".join([
            f"LOWER(ki_id.normalized_text) LIKE ?" for _ in BUNDLE_LIKE_PATTERNS
        ])
        sql = base_select + f" WHERE {acct_clause} AND ({like_clauses})"
        params = list(ACCOUNT_IDS) + BUNDLE_LIKE_PATTERNS
        df = pd.read_sql_query(sql, conn, params=params)
        print(f"  SQL pre-filter narrowed to {len(df):,} candidate instances "
              f"(from ~534k via LIKE patterns)")

    if df.empty:
        df['regex_match']  = pd.Series([], dtype=bool)
        df['deny_listed']  = pd.Series([], dtype=bool)
        df['qualifies']    = pd.Series([], dtype=bool)
        return df

    # Tighten with regex (LIKE is permissive — regex enforces word boundaries)
    df['regex_match']  = df['keyword_text'].fillna('').apply(
        lambda t: bool(BUNDLE_PATTERN.search(t)))

    def is_denied(text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in BUNDLE_DENY_LIST)
    df['deny_listed']  = df['keyword_text'].fillna('').apply(is_denied)

    df['qualifies']    = df['regex_match'] & ~df['deny_listed']

    matched = df[df['qualifies']].copy()
    print(f"  Bundle regex hits within SQL set: {df['regex_match'].sum():,}")
    print(f"  Deny-listed:                      {df['deny_listed'].sum():,}")
    print(f"  Qualifying bundle instances:      {len(matched):,}")
    if len(matched) > 0:
        print(f"    Tagged modifier_bundle=1: {(matched['bundle_tag'] == 1).sum():,} "
              f"({(matched['bundle_tag'] == 1).mean() * 100:.0f}% overlap with tag)")

    return df


def find_usaa_keywords(conn, full_audit: bool = False) -> pd.DataFrame:
    """
    Returns one row per (account_id, instance_id) for USAA keywords.
    Identified primarily by modifier_brand = 'usaa', with a text-based
    sanity flag for any \\busaa\\b matches that aren't tagged.

    PERFORMANCE: SQL pre-filters to instances where either modifier_brand
    is 'usaa' OR keyword_text contains 'usaa'. Drops candidate set to
    ~10k from ~534k.
    """
    base_select = """
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
    """
    acct_clause = f"ki.account_id IN ({','.join('?' * len(ACCOUNT_IDS))})"

    if full_audit:
        sql = base_select + f" WHERE {acct_clause}"
        df = pd.read_sql_query(sql, conn, params=ACCOUNT_IDS)
        print(f"  [full_audit] Loaded {len(df):,} candidate instances")
    else:
        sql = (base_select +
               f" WHERE {acct_clause} "
               f" AND (LOWER(COALESCE(ki_id.modifier_brand,'')) = 'usaa'"
               f"      OR LOWER(ki_id.normalized_text) LIKE '%usaa%')")
        df = pd.read_sql_query(sql, conn, params=ACCOUNT_IDS)
        print(f"  SQL pre-filter narrowed to {len(df):,} candidate instances "
              f"(from ~534k via brand-tag or text match)")

    if df.empty:
        df['brand_tag_match'] = pd.Series([], dtype=bool)
        df['text_match']      = pd.Series([], dtype=bool)
        df['qualifies']       = pd.Series([], dtype=bool)
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
                                BUT: rows where report_date <
                                MICROSOFT_REVENUE_VALID_FROM are flagged
                                revenue_valid=False — Microsoft revenue
                                tracking only went live on that date.
      GP = revenue - spend (where revenue_valid)
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

    # Microsoft revenue cutoff flag — True for Google always, True for
    # Microsoft only on/after the valid-from date.
    out['revenue_valid'] = (
        (out['account_id'] != 2) |
        (out['report_date'] >= MICROSOFT_REVENUE_VALID_FROM)
    )

    out['gp']   = out['revenue'] - out['spend']
    out['roas'] = np.where(out['spend'] > 0, out['revenue'] / out['spend'], np.nan)

    return out


# ── Sheet builders ────────────────────────────────────────────────
def build_kw_matches_sheet(kw_df: pd.DataFrame, perf_df: pd.DataFrame,
                           initiative: str,
                           audit_csv_path: Path = None) -> pd.DataFrame:
    """
    Audit sheet: QUALIFYING keyword instances only, with match flags and
    lifetime in-window performance. Use this to spot deny-list candidates
    (regex matches that shouldn't have qualified).

    The full candidate set (all 530k+ rows with flags) is written to
    `audit_csv_path` if provided — that's where you go to confirm there
    aren't bundle/USAA keywords being missed by the regex/tag.
    """
    # Lifetime in-window performance per instance — computed on the FULL
    # candidate set so the audit CSV has spend/GP for every row, even
    # non-qualifying ones (so you can see whether a missed keyword had
    # meaningful spend).
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

    full = kw_df.merge(agg, on='instance_id', how='left')
    for col in ['spend', 'clicks', 'impressions', 'revenue', 'gp',
                'conv_count', 'active_days']:
        if col in full.columns:
            full[col] = full[col].fillna(0)

    full['roas'] = np.where(full['spend'] > 0,
                            full['revenue'] / full['spend'], np.nan)
    full['roas_bucket'] = full.apply(
        lambda r: roas_bucket(r['spend'], r['roas']), axis=1)

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

    full_display = full[[c for c in display_cols if c in full.columns]].copy()
    for col in ['spend', 'revenue', 'gp', 'roas']:
        if col in full_display.columns:
            full_display[col] = pd.to_numeric(full_display[col],
                                              errors='coerce').round(2)

    # Write full audit CSV (all candidates, qualifying or not)
    if audit_csv_path is not None:
        try:
            full_display.to_csv(audit_csv_path, index=False)
            print(f"  Full audit CSV: {audit_csv_path.name} "
                  f"({len(full_display):,} rows)")
        except Exception as e:
            print(f"  WARNING: failed to write audit CSV: {e}")

    # Workbook sheet: qualifying only, sorted by spend desc
    sheet = full_display[full_display['qualifies']].copy()
    sheet = sheet.sort_values('spend', ascending=False).reset_index(drop=True)

    if sheet.empty:
        return pd.DataFrame([{'Note': f'No qualifying {initiative} keywords'}])

    return sheet


def build_daily_timeseries(perf_df: pd.DataFrame) -> pd.DataFrame:
    """
    Daily performance aggregated by platform.
    Columns: report_date, platform, revenue_valid, active_instances,
             impressions, clicks, spend, revenue, gp, roas

    `revenue_valid` is False for Microsoft rows before
    MICROSOFT_REVENUE_VALID_FROM. We split on this flag so a Microsoft
    pre-cutoff row and post-cutoff row appear on different lines (pre-
    cutoff revenue=0 and gp=-spend, but flagged so it's clearly an
    artifact, not a true loss).
    """
    if perf_df.empty:
        return pd.DataFrame([{'Note': 'No performance data in window'}])

    grp = perf_df.groupby(
        ['report_date', 'platform', 'revenue_valid'], as_index=False).agg(
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
    Summary by platform-segment. Microsoft is split into two segments:
      - "microsoft (pre-revenue, cost-only)"  : rows before
        MICROSOFT_REVENUE_VALID_FROM. Spend was real but revenue was not
        being tracked. Reported for transparency, EXCLUDED from sizing
        TOTAL because GP is structurally meaningless here.
      - "microsoft (revenue valid)"            : rows on/after the cutoff.
        These are the figures that feed sizing.

    Per segment we report:
      - Window days, active days (spend >= MIN_ACTIVE_DAILY_SPEND), activity rate
      - Total spend / revenue / GP / conversions
      - Lifetime ROAS
      - Avg daily GP — active days only (the sizing metric)
      - Avg daily GP — full window (for contrast)

    The TOTAL row sums Google + Microsoft-revenue-valid only.
    """
    if perf_df.empty:
        return pd.DataFrame([{'Note': 'No performance data in window'}])

    df = perf_df.copy()
    # Build segment label: google stays as 'google', microsoft splits
    df['segment'] = np.where(
        df['platform'] == 'microsoft',
        np.where(df['revenue_valid'],
                 'microsoft (revenue valid)',
                 'microsoft (pre-revenue, cost-only)'),
        df['platform']
    )

    # Daily aggregation per segment
    daily = df.groupby(['report_date', 'segment'], as_index=False).agg(
        spend     =('spend',      'sum'),
        revenue   =('revenue',    'sum'),
        gp        =('gp',         'sum'),
        conv_count=('conv_count', 'sum'),
    )

    rows = []

    def summarize(segment_df: pd.DataFrame, label: str,
                  segment_window_start: str = None):
        """Emit one summary row for a segment."""
        active = segment_df[segment_df['spend'] >= MIN_ACTIVE_DAILY_SPEND]
        active_days = len(active)
        seg_window_start = segment_window_start or window_start
        seg_window_days = (datetime.fromisoformat(window_end)
                           - datetime.fromisoformat(seg_window_start)).days + 1
        rows.append({
            'segment':                  label,
            'window_start':             seg_window_start,
            'window_end':               window_end,
            'window_days':              seg_window_days,
            'active_days':              active_days,
            'activity_rate_pct':        round(active_days / seg_window_days * 100, 1)
                                        if seg_window_days else 0,
            'total_spend':              round(segment_df['spend'].sum(),   2),
            'total_revenue':            round(segment_df['revenue'].sum(), 2),
            'total_gp':                 round(segment_df['gp'].sum(),      2),
            'total_conversions':        round(segment_df['conv_count'].sum(), 2),
            'lifetime_roas':            round(segment_df['revenue'].sum()
                                              / segment_df['spend'].sum(), 3)
                                        if segment_df['spend'].sum() else 0,
            'avg_daily_gp_active':      round(active['gp'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_spend_active':   round(active['spend'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_revenue_active': round(active['revenue'].mean(), 2)
                                        if active_days else 0,
            'avg_daily_gp_all':         round(segment_df['gp'].sum() / seg_window_days, 2)
                                        if seg_window_days else 0,
        })

    # One row per segment
    for segment in sorted(daily['segment'].unique()):
        sub = daily[daily['segment'] == segment]
        # For microsoft (revenue valid) only, the meaningful window starts
        # at the cutoff date — averaging over the whole 488-day window
        # would understate the daily run-rate.
        seg_start = (MICROSOFT_REVENUE_VALID_FROM
                     if segment == 'microsoft (revenue valid)' else None)
        summarize(sub, segment, segment_window_start=seg_start)

    # TOTAL row: google + microsoft (revenue valid). Exclude pre-cutoff MS.
    total = daily[daily['segment'] != 'microsoft (pre-revenue, cost-only)']
    total_combined = total.groupby('report_date', as_index=False).agg(
        spend=('spend','sum'), revenue=('revenue','sum'),
        gp=('gp','sum'), conv_count=('conv_count','sum'))
    summarize(total_combined,
              'TOTAL (Google + MS-valid; excludes MS pre-revenue)')

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
        {'Parameter': 'Microsoft revenue cutoff',
         'Value': f'Revenue valid from {MICROSOFT_REVENUE_VALID_FROM} '
                  f'(prior rows excluded from sizing — flagged in timeseries)'},
        {'Parameter': 'Current-live detection',
         'Value': f'Last active day within {CURRENT_LIVE_FRESHNESS_DAYS}d of '
                  f'window end → trailing {CURRENT_LIVE_LOOKBACK_DAYS}d run-rate'},
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


def compute_current_live_row(perf_df: pd.DataFrame, label: str,
                              window_end: str) -> dict | None:
    """
    Detect whether an initiative is currently live and return a one-row
    dict summarizing its run-rate over the last CURRENT_LIVE_LOOKBACK_DAYS.

    "Currently live" = the most recent active day (spend >= MIN_ACTIVE_DAILY_SPEND)
    is within CURRENT_LIVE_FRESHNESS_DAYS of window_end. Microsoft pre-cutoff
    rows are excluded since their GP is artifactual.

    Returns None if the initiative isn't currently live.
    """
    if perf_df.empty:
        return None

    # Exclude Microsoft pre-cutoff rows from the live-rate calculation
    valid = perf_df[perf_df['revenue_valid']].copy()
    if valid.empty:
        return None

    valid['report_date'] = pd.to_datetime(valid['report_date'])
    end_dt = pd.to_datetime(window_end)
    daily = valid.groupby('report_date', as_index=False).agg(
        spend=('spend','sum'), revenue=('revenue','sum'),
        gp=('gp','sum'), conv_count=('conv_count','sum'))
    active = daily[daily['spend'] >= MIN_ACTIVE_DAILY_SPEND]
    if active.empty:
        return None

    last_active = active['report_date'].max()
    days_since = (end_dt - last_active).days
    if days_since > CURRENT_LIVE_FRESHNESS_DAYS:
        return None  # not currently live

    # Live run-rate over the last N days (use lookback window from end date)
    cutoff = end_dt - pd.Timedelta(days=CURRENT_LIVE_LOOKBACK_DAYS)
    recent = daily[daily['report_date'] > cutoff]
    recent_active = recent[recent['spend'] >= MIN_ACTIVE_DAILY_SPEND]
    if recent_active.empty:
        return None

    spend   = recent['spend'].sum()
    rev     = recent['revenue'].sum()
    gp      = recent['gp'].sum()
    n_active = len(recent_active)
    n_calendar = CURRENT_LIVE_LOOKBACK_DAYS

    return {
        'Initiative':                     label,
        'Window':                         f"Last {CURRENT_LIVE_LOOKBACK_DAYS} days "
                                          f"(last active {last_active.date()})",
        'Window days':                    n_calendar,
        'Active days':                    n_active,
        'Activity rate (%)':              round(n_active / n_calendar * 100, 1),
        'Total spend ($)':                round(spend, 2),
        'Total revenue ($)':              round(rev, 2),
        'Total GP ($)':                   round(gp, 2),
        'Lifetime ROAS':                  round(rev / spend, 3) if spend else 0,
        'Avg daily GP — active ($)':      round(gp / n_active, 2) if n_active else 0,
        'Avg daily GP — full window ($)': round(gp / n_calendar, 2),
        'Avg daily spend — active ($)':   round(spend / n_active, 2)
                                          if n_active else 0,
        'Implied GP/month ($)':           round(gp / n_active * 30, 2)
                                          if n_active else 0,
    }


def compute_ms_usaa_extrapolation(perf_df: pd.DataFrame,
                                   window_end: str) -> dict | None:
    """
    Extrapolates Microsoft USAA pre-cutoff GP using Google USAA's
    most-recent-quarter RPL with a 20% haircut.

    Logic:
      1. Find Google's most recent quarter of sustained USAA activity.
         "Sustained" = days with spend >= $100. We look at the last 90
         days where Google had USAA activity.
      2. Compute Google_USAA_RPL = Google_revenue / Google_conversions
         over that quarter.
      3. Apply haircut: Discounted_RPL = Google_RPL × MS_USAA_RPL_HAIRCUT
      4. Sum Microsoft pre-cutoff conversions and spend.
      5. Estimated MS revenue = MS_conversions × Discounted_RPL
         Estimated MS GP      = est_revenue − MS_spend

    Returns None if there's no Google data to baseline against, or no
    Microsoft pre-cutoff activity to extrapolate.

    Output dict keys mirror the Sizing Summary row schema, with extra
    transparency fields (formula inputs).
    """
    if perf_df.empty:
        return None

    google = perf_df[perf_df['platform'] == 'google'].copy()
    ms_pre = perf_df[
        (perf_df['platform'] == 'microsoft') &
        (~perf_df['revenue_valid'])
    ].copy()

    if google.empty or ms_pre.empty:
        return None

    # Step 1: Google's most recent quarter of sustained USAA activity.
    google['report_date'] = pd.to_datetime(google['report_date'])
    google_daily = google.groupby('report_date', as_index=False).agg(
        spend=('spend','sum'), revenue=('revenue','sum'),
        conv_count=('conv_count','sum'))
    google_active = google_daily[google_daily['spend'] >= 100]
    if google_active.empty:
        return None

    google_last_active = google_active['report_date'].max()
    quarter_start = google_last_active - pd.Timedelta(
        days=MS_USAA_RPL_QUARTER_DAYS)
    quarter = google_active[google_active['report_date'] > quarter_start]

    quarter_spend = quarter['spend'].sum()
    quarter_rev   = quarter['revenue'].sum()
    quarter_conv  = quarter['conv_count'].sum()

    if quarter_conv <= 0:
        return None

    google_rpl_recent = quarter_rev / quarter_conv

    # Step 2-3: Discounted RPL
    discounted_rpl = google_rpl_recent * MS_USAA_RPL_HAIRCUT

    # Step 4-5: MS pre-cutoff aggregates + extrapolation
    ms_pre['report_date'] = pd.to_datetime(ms_pre['report_date'])
    ms_active = ms_pre[ms_pre['spend'] >= MIN_ACTIVE_DAILY_SPEND]
    ms_spend       = ms_pre['spend'].sum()
    ms_conversions = ms_pre['conv_count'].sum()
    ms_active_days = ms_active['report_date'].nunique()

    if ms_active_days == 0 or ms_conversions <= 0:
        return None

    estimated_revenue = ms_conversions * discounted_rpl
    estimated_gp      = estimated_revenue - ms_spend
    estimated_roas    = estimated_revenue / ms_spend if ms_spend else 0

    ms_window_start = ms_pre['report_date'].min().date().isoformat()
    ms_window_end   = ms_pre['report_date'].max().date().isoformat()

    return {
        'Initiative':
            '  └ [ESTIMATED] USAA Microsoft pre-revenue '
            '(extrapolated from Google RPL × 0.80)',
        'Window':                         f"{ms_window_start} → {ms_window_end}",
        'Window days':                    (pd.to_datetime(ms_window_end)
                                            - pd.to_datetime(ms_window_start)).days + 1,
        'Active days':                    ms_active_days,
        'Activity rate (%)':              round(
            ms_active_days /
            ((pd.to_datetime(ms_window_end) - pd.to_datetime(ms_window_start)).days + 1)
            * 100, 1),
        'Total spend ($)':                round(ms_spend, 2),
        'Total revenue ($)':              round(estimated_revenue, 2),
        'Total GP ($)':                   round(estimated_gp, 2),
        'Lifetime ROAS':                  round(estimated_roas, 3),
        'Avg daily GP — active ($)':      round(estimated_gp / ms_active_days, 2),
        'Avg daily GP — full window ($)': '',
        'Avg daily spend — active ($)':   round(ms_spend / ms_active_days, 2),
        'Implied GP/month ($)':           round(estimated_gp / ms_active_days * 30, 2),
        # Transparency fields
        'Extrap inputs': (
            f"Google RPL (last {MS_USAA_RPL_QUARTER_DAYS}d sustained, "
            f"{quarter['report_date'].nunique()} days, "
            f"ending {google_last_active.date()}): "
            f"${google_rpl_recent:.2f} | "
            f"haircut: {MS_USAA_RPL_HAIRCUT} | "
            f"discounted RPL: ${discounted_rpl:.2f} | "
            f"MS conv: {ms_conversions:,.0f}"
        ),
    }


def build_sizing_summary(bundle_summary: pd.DataFrame,
                         usaa_summary:  pd.DataFrame,
                         bundle_perf:   pd.DataFrame,
                         usaa_perf:     pd.DataFrame,
                         window_end:    str) -> pd.DataFrame:
    """
    One-page sizing answer combining:
      - Historical baseline (TOTAL row from active-period summary)
      - Per-platform-segment breakdown
      - Current live run-rate row (when applicable, e.g. bundle reintro)
    """
    rows = []

    def extract_segment(df: pd.DataFrame, segment_name: str, label: str):
        """Pull a specific segment row from active period summary."""
        if df.empty or 'segment' not in df.columns:
            return
        sub = df[df['segment'] == segment_name]
        if sub.empty:
            return
        r = sub.iloc[0]
        rows.append({
            'Initiative':                     label,
            'Window':                         f"{r['window_start']} → {r['window_end']}",
            'Window days':                    int(r['window_days']),
            'Active days':                    int(r['active_days']),
            'Activity rate (%)':              r['activity_rate_pct'],
            'Total spend ($)':                r['total_spend'],
            'Total revenue ($)':              r['total_revenue'],
            'Total GP ($)':                   r['total_gp'],
            'Lifetime ROAS':                  r['lifetime_roas'],
            'Avg daily GP — active ($)':      r['avg_daily_gp_active'],
            'Avg daily GP — full window ($)': r['avg_daily_gp_all'],
            'Avg daily spend — active ($)':   r['avg_daily_spend_active'],
            'Implied GP/month ($)':
                round(r['avg_daily_gp_active'] * 30, 2)
                if pd.notna(r['avg_daily_gp_active']) else 0,
        })

    # ── BUNDLE ────────────────────────────────────────────────────
    # Current live run-rate (the most useful number for a reintroduction)
    bundle_live = compute_current_live_row(
        bundle_perf, 'CASE 1: Bundle — CURRENT LIVE (post-reintro)', window_end)
    if bundle_live:
        rows.append(bundle_live)

    # Historical baseline + per-platform
    extract_segment(bundle_summary,
                    'TOTAL (Google + MS-valid; excludes MS pre-revenue)',
                    'CASE 1: Bundle — historical sanity check (TOTAL)')
    extract_segment(bundle_summary, 'google',
                    '  └ Bundle Google only')
    extract_segment(bundle_summary, 'microsoft (revenue valid)',
                    '  └ Bundle Microsoft (revenue valid)')

    # ── USAA ──────────────────────────────────────────────────────
    usaa_live = compute_current_live_row(
        usaa_perf, 'CASE 2: USAA — CURRENT LIVE', window_end)
    if usaa_live:
        rows.append(usaa_live)

    extract_segment(usaa_summary,
                    'TOTAL (Google + MS-valid; excludes MS pre-revenue)',
                    'CASE 2: USAA — historical baseline (TOTAL)')
    extract_segment(usaa_summary, 'google',
                    '  └ USAA Google only')
    extract_segment(usaa_summary, 'microsoft (revenue valid)',
                    '  └ USAA Microsoft (revenue valid)')

    # Microsoft USAA extrapolation for the pre-cutoff period (no measured
    # revenue, but cost was real and conversions were tracked)
    ms_extrap = compute_ms_usaa_extrapolation(usaa_perf, window_end)
    if ms_extrap:
        rows.append(ms_extrap)

    if not rows:
        return pd.DataFrame([{'Initiative': 'No data'}])

    out = pd.DataFrame(rows)

    # Append notes row
    notes_text = (
        f"Microsoft revenue tracking valid from {MICROSOFT_REVENUE_VALID_FROM}; "
        f"earlier MS rows excluded from measured sizing. CURRENT LIVE row "
        f"appears only if last active day is within {CURRENT_LIVE_FRESHNESS_DAYS} "
        f"days of window end. [ESTIMATED] rows use Google RPL (most recent "
        f"{MS_USAA_RPL_QUARTER_DAYS}d sustained) × {MS_USAA_RPL_HAIRCUT} haircut "
        f"× MS conversions — see 'Extrap inputs' column for actual values used."
    )
    notes = {col: '' for col in out.columns}
    notes['Initiative'] = 'NOTES'
    notes['Window'] = notes_text
    out = pd.concat([out, pd.DataFrame([notes])], ignore_index=True)
    return out


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
    import time
    t0 = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument('--end-date', help='YYYY-MM-DD; defaults to latest in DB')
    parser.add_argument('--start-date', default=WINDOW_START,
                        help=f'YYYY-MM-DD; defaults to {WINDOW_START}')
    parser.add_argument('--initiative', choices=['bundle', 'usaa', 'all'],
                        default='all',
                        help='Which initiative to size. Default: all')
    parser.add_argument('--full-audit', action='store_true',
                        help='Load ALL keyword instances (no SQL pre-filter) '
                             'and write full audit CSVs. Slow on real data — '
                             'use only when hunting for missed keywords.')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    end_date   = args.end_date or get_data_window_end(conn)
    start_date = args.start_date
    run_bundle = args.initiative in ('bundle', 'all')
    run_usaa   = args.initiative in ('usaa',   'all')

    print(f"\n=== Keyword Opportunity Sizing ===")
    print(f"Window: {start_date} → {end_date}")
    print(f"Accounts: {ACCOUNT_IDS} (Home Insurance Google + Microsoft)")
    print(f"Initiatives: {args.initiative}  |  full_audit={args.full_audit}")

    bundle_perf = pd.DataFrame()
    bundle_kw   = pd.DataFrame()
    bundle_matches_sheet = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    bundle_timeseries    = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    bundle_summary       = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    bundle_detail        = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    bundle_qualifying_iids: list = []

    usaa_perf = pd.DataFrame()
    usaa_kw   = pd.DataFrame()
    usaa_matches_sheet = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    usaa_timeseries    = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    usaa_summary       = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    usaa_detail        = pd.DataFrame([{'Note': 'Skipped (--initiative)'}])
    usaa_qualifying_iids: list = []

    # ── CASE 1: Bundle ────────────────────────────────────────────
    if run_bundle:
        t = time.time()
        print(f"\n[Case 1] Identifying bundle keywords...")
        bundle_kw = find_bundle_keywords(conn, full_audit=args.full_audit)

        bundle_qualifying_iids = (bundle_kw[bundle_kw['qualifies']]['instance_id']
                                  .dropna().astype(int).unique().tolist())
        print(f"\n[Case 1] Fetching daily performance for "
              f"{len(bundle_qualifying_iids):,} instances...")
        bundle_perf_raw = fetch_daily_performance(
            conn, bundle_qualifying_iids, start_date, end_date)
        bundle_perf     = attach_platform_revenue(bundle_perf_raw, bundle_kw)
        print(f"  {len(bundle_perf):,} daily performance rows  "
              f"[Case 1 took {time.time()-t:.1f}s]")

        bundle_audit_csv = (OUTPUT_DIR /
                            "keyword_opportunity_sizing_bundle_kw_audit_full.csv"
                            ) if args.full_audit else None
        bundle_matches_sheet = build_kw_matches_sheet(
            bundle_kw, bundle_perf, 'bundle', audit_csv_path=bundle_audit_csv)
        bundle_timeseries    = build_daily_timeseries(bundle_perf)
        bundle_summary       = build_active_period_summary(
            bundle_perf, start_date, end_date)
        bundle_detail        = build_keyword_detail(bundle_kw, bundle_perf,
                                                    qualifying_only=True)

    # ── CASE 2: USAA ──────────────────────────────────────────────
    if run_usaa:
        t = time.time()
        print(f"\n[Case 2] Identifying USAA keywords...")
        usaa_kw = find_usaa_keywords(conn, full_audit=args.full_audit)

        usaa_qualifying_iids = (usaa_kw[usaa_kw['qualifies']]['instance_id']
                                .dropna().astype(int).unique().tolist())
        print(f"\n[Case 2] Fetching daily performance for "
              f"{len(usaa_qualifying_iids):,} instances...")
        usaa_perf_raw = fetch_daily_performance(
            conn, usaa_qualifying_iids, start_date, end_date)
        usaa_perf     = attach_platform_revenue(usaa_perf_raw, usaa_kw)
        print(f"  {len(usaa_perf):,} daily performance rows  "
              f"[Case 2 took {time.time()-t:.1f}s]")

        usaa_audit_csv = (OUTPUT_DIR /
                          "keyword_opportunity_sizing_usaa_kw_audit_full.csv"
                          ) if args.full_audit else None
        usaa_matches_sheet = build_kw_matches_sheet(
            usaa_kw, usaa_perf, 'usaa', audit_csv_path=usaa_audit_csv)
        usaa_timeseries    = build_daily_timeseries(usaa_perf)
        usaa_summary       = build_active_period_summary(
            usaa_perf, start_date, end_date)
        usaa_detail        = build_keyword_detail(usaa_kw, usaa_perf,
                                                  qualifying_only=True)

    # ── Sizing summary + metadata ─────────────────────────────────
    sizing_summary = build_sizing_summary(
        bundle_summary, usaa_summary,
        bundle_perf, usaa_perf,
        window_end=end_date)
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
    print(f"Total wall time: {time.time()-t0:.1f}s")
    print(f"\nTip: --initiative bundle|usaa  for single-case runs; "
          f"--full-audit  to dump non-qualifying candidates to CSV "
          f"(slow on real data)")


if __name__ == '__main__':
    main()
