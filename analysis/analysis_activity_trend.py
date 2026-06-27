"""
analysis_activity_trend.py  [v3 — extended sheets + dynamic date range]
--------------------------
Keyword activity trend analysis — what is the team actively running
and how is that changing month over month?

Pass --account to select which account to analyze.
Pass --months to override the default month window.

Defines "active" as a keyword that generated at least one impression
in a given month — not simply ENABLED status, which can be misleading
when bids are too low or budgets are throttled.

For accounts with Microsoft (home):
    Produces 8 sheets including Platform Comparison.
For Google-only accounts (medicare, auto, term_life, auto_quote):
    Produces 7 sheets — Platform Comparison is omitted.

Sheets:
    1. Summary              -- headline keyword counts, spend by lifecycle,
                               plus campaign-level totals from campaign_performance_daily
    2. Monthly active       -- active keyword counts by match type
    3. Campaign activity    -- per-campaign per-month spend/conv from campaign_performance_daily
    4. Structure snapshot   -- aggregate counts of campaigns/ad groups/keywords by month
    5. Keyword density      -- keyword distribution per campaign and ad group
    6. Keyword lifecycle    -- each keyword classified into one of eight states
    7. Spend migration      -- spend and conversions lost from paused keywords
    8. Platform comparison  -- Google vs Microsoft side-by-side (home only)

Default date range (no --months flag):
    For --account home: all months from the earliest month in campaign_performance_daily
        through the most recent complete month with data. Since keyword_performance_daily
        only starts Dec 2025, earlier months show zero keyword-level activity (intentional).
    For all other accounts: the 3 most recent complete months with keyword performance data.

Usage:
    conda activate paid-search
    python analysis/analysis_activity_trend.py --account home
    python analysis/analysis_activity_trend.py --account medicare
    python analysis/analysis_activity_trend.py --account medicare --months 2026-01 2026-02 2026-03

Output:
    analysis/output/activity_trend_{account}.xlsx
"""

import sqlite3
import os
import argparse
import calendar
import pandas as pd
from pathlib import Path
from datetime import date
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Account map
# ---------------------------------------------------------------------------

ACCOUNT_MAP = {
    "home": {
        "display_name":    "Home Insurance",
        "google_id":       "8886502147",
        "microsoft_id":    "F149SN63",
        "has_microsoft":   True,
    },
    "medicare": {
        "display_name":    "Medicare",
        "google_id":       "5310820901",
        "microsoft_id":    None,
        "has_microsoft":   False,
    },
    "auto": {
        "display_name":    "Auto Insurance (autoinsurance.org)",
        "google_id":       "3863660890",
        "microsoft_id":    "F149Y8EN",
        "has_microsoft":   False,   # flip to True when MS data is available
    },
    "term_life": {
        "display_name":    "Term Life",
        "google_id":       "1671155176",
        "microsoft_id":    "F149ZJ6F",
        "has_microsoft":   False,
    },
    "auto_quote": {
        "display_name":    "Auto Insurance (auto.quote.com)",
        "google_id":       "1006729211",
        "microsoft_id":    None,
        "has_microsoft":   False,
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Minimum impressions to be considered "active" in a month.
# 1 impression = the keyword entered at least one auction.
ACTIVE_IMPRESSION_THRESHOLD = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def month_bounds(month_str: str) -> tuple[str, str]:
    """
    Returns (start, end) ISO date strings for a 'YYYY-MM' month string.
    The end date is the last calendar day of the month.
    Partial months (e.g. current month) get the last available day.
    """
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day    = calendar.monthrange(year, month)[1]
    today       = date.today()
    end         = date(year, month, last_day)
    end         = min(end, today - pd.Timedelta(days=1))
    return f"{year:04d}-{month:02d}-01", end.isoformat()


def make_month_labels(months: list[str]) -> dict[str, str]:
    """Maps 'YYYY-MM' to a human-readable label like 'January 2026'."""
    labels = {}
    for m in months:
        year, month = int(m[:4]), int(m[5:7])
        labels[m] = f"{calendar.month_name[month]} {year}"
    return labels


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Keyword activity trend analysis for a single account.'
    )
    parser.add_argument(
        '--account', required=True, choices=list(ACCOUNT_MAP.keys()),
        help='Account to analyze.',
    )
    parser.add_argument(
        '--months', nargs='+', metavar='YYYY-MM',
        help=(
            'Month(s) to analyze, e.g. --months 2026-01 2026-02 2026-03. '
            'Default: the three most recent complete months with data.'
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -64000")   # 64MB page cache
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def detect_recent_months(conn, google_id: str, n: int = 3) -> list[str]:
    """
    Auto-detects the N most recent months that have keyword performance
    data for this account, scoped to avoid cross-contamination between
    accounts with different data windows.
    """
    rows = conn.execute("""
        SELECT DISTINCT STRFTIME('%Y-%m', kpd.report_date) AS month
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id  = ki.id
        JOIN accounts a           ON ki.account_id    = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        ORDER BY month DESC
        LIMIT ?
    """, (google_id, n + 1)).fetchall()   # +1 to handle partial current month

    today         = date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"
    months        = [r['month'] for r in rows if r['month'] != current_month]
    return sorted(months)[-n:]   # oldest to newest


def detect_full_campaign_month_range(conn, google_id: str) -> list[str]:
    """
    Dynamically detects all complete months available in campaign_performance_daily
    for this account, from the earliest month through the most recent complete month.

    Used as the default date range for accounts with campaign-level data that
    predates keyword_performance_daily (e.g. home: campaign data from Jan 2025,
    keyword data from Dec 2025). Months without keyword data will show zeros on
    keyword-level sheets — this is intentional.
    """
    today         = date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"

    rows = conn.execute("""
        SELECT DISTINCT STRFTIME('%Y-%m', cpd.date) AS month
        FROM campaign_performance_daily cpd
        JOIN campaigns c   ON cpd.campaign_id = c.campaign_id
        JOIN accounts a    ON c.account_id    = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND cpd.device = 'ALL'
        ORDER BY month ASC
    """, (google_id,)).fetchall()

    months = [r['month'] for r in rows if r['month'] != current_month]
    return months  # already oldest → newest


# ---------------------------------------------------------------------------
# Core data fetch
# ---------------------------------------------------------------------------

def detect_kpd_months(conn, google_id: str) -> list[str]:
    """
    Returns only months that actually have keyword_performance_daily data
    for this account. Used to avoid running the keyword fetch against months
    where the result would be all zeros (e.g. Jan–Nov 2025 for home).
    """
    today         = date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"
    rows = conn.execute("""
        SELECT DISTINCT STRFTIME('%Y-%m', kpd.report_date) AS month
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        JOIN accounts a           ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        ORDER BY month ASC
    """, (google_id,)).fetchall()
    return [r['month'] for r in rows if r['month'] != current_month]


def fetch_total_instance_count(conn, account_id: int) -> int:
    """
    Returns the total number of keyword instances for an account.
    Used as the denominator for 'Never active' in build_lifecycle —
    since we filter fetch_keyword_monthly_activity to active rows only,
    we need this count separately to know how many keywords had zero
    impressions across all months.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?",
        (account_id,)
    ).fetchone()
    return row[0]


def fetch_keyword_monthly_activity(
    conn,
    platform_account_id: str,
    platform: str,
    months: list[str],
    month_labels: dict[str, str],
) -> pd.DataFrame:
    """
    Returns one row per keyword instance per month showing whether the
    keyword was active (impressions >= threshold) and its performance.

    Uses keyword_performance_daily (not search_query_performance) so the
    metrics reflect keyword-level bid spend and conversions, not query-level.
    """
    rows   = []
    cursor = conn.cursor()

    acct = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (platform_account_id, platform)
    ).fetchone()
    if not acct:
        return pd.DataFrame()
    account_id = acct['id']

    for month in months:
        start, end = month_bounds(month)

        results = cursor.execute("""
            SELECT
                ki.id            AS instance_id,
                ki.criterion_id,
                ki.identity_id,
                ki.keyword_text,
                ki.match_type,
                ki.campaign_name,
                ki.status,
                kd.normalized_text,
                kd.vertical,
                kd.brand_category,
                kd.modifier_brand,
                COALESCE(SUM(kpd.impressions), 0)        AS impressions,
                COALESCE(SUM(kpd.clicks), 0)             AS clicks,
                COALESCE(SUM(kpd.cost_micros), 0) / 1e6  AS spend,
                COALESCE(SUM(kpd.primary_conv_count), 0) AS conversions,
                COALESCE(SUM(kpd.primary_conv_value), 0) AS revenue
            FROM keyword_instances ki
            JOIN accounts a           ON ki.account_id = a.id
            JOIN keyword_identities kd ON ki.identity_id = kd.id
            LEFT JOIN keyword_performance_daily kpd
                ON kpd.instance_id  = ki.id
                AND kpd.device      = 'ALL'
                AND kpd.geo_state   = 'ALL'
                AND kpd.report_date BETWEEN ? AND ?
            WHERE a.id = ?
            GROUP BY ki.id
            HAVING SUM(kpd.impressions) > 0
        """, (start, end, account_id)).fetchall()

        for r in results:
            rows.append({
                'platform':        platform,
                'month':           month,
                'month_label':     month_labels[month],
                'instance_id':     r['instance_id'],
                'criterion_id':    r['criterion_id'],
                'identity_id':     r['identity_id'],
                'keyword_text':    r['keyword_text'],
                'normalized_text': r['normalized_text'],
                'match_type':      r['match_type'],
                'campaign_name':   r['campaign_name'],
                'status':          r['status'],
                'vertical':        r['vertical'],
                'brand_category':  r['brand_category'],
                'modifier_brand':  r['modifier_brand'],
                'impressions':     r['impressions'],
                'clicks':          r['clicks'],
                'spend':           r['spend'],
                'conversions':     r['conversions'],
                'revenue':         r['revenue'],
                'is_active':       r['impressions'] >= ACTIVE_IMPRESSION_THRESHOLD,
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analysis 1: Monthly active keyword counts
# ---------------------------------------------------------------------------

def build_monthly_active(
    google_df: pd.DataFrame,
    ms_df: pd.DataFrame,
    months: list[str],
    kpd_months: list[str],
    month_labels: dict[str, str],
    has_ms: bool,
) -> pd.DataFrame:
    """
    Counts active keywords by month, platform, and match type.
    Uses the full months list so Jan–Nov 2025 show zero rows
    (no kpd data yet). Only months in kpd_months will have non-zero counts.
    Microsoft rows are included only when the account has Microsoft data.
    """
    rows      = []
    platforms = [('Google', google_df)]
    if has_ms and not ms_df.empty:
        platforms.append(('Microsoft', ms_df))

    for platform, df in platforms:
        for month in months:
            if month not in kpd_months or df.empty:
                # No keyword performance data for this month — emit zero rows
                # per match type only if we have any keyword instances at all
                for mt in ['EXACT', 'PHRASE', 'BROAD']:
                    rows.append({
                        'Platform':           platform,
                        'Month':              month_labels[month],
                        'Match type':         mt,
                        'Total keywords':     0,
                        'Active keywords':    0,
                        'Inactive keywords':  0,
                        'Active %':           0.0,
                        'Active spend ($)':   0.0,
                        'Active conversions': 0.0,
                    })
                continue

            mdf = df[df['month'] == month]
            for mt in ['EXACT', 'PHRASE', 'BROAD']:
                mt_mask   = mdf['match_type'] == mt
                mt_active = mdf[mt_mask]['is_active'].sum()
                mt_spend  = mdf[mt_mask & mdf['is_active']]['spend'].sum()
                mt_convs  = mdf[mt_mask & mdf['is_active']]['conversions'].sum()
                total     = mt_mask.sum()
                rows.append({
                    'Platform':           platform,
                    'Month':              month_labels[month],
                    'Match type':         mt,
                    'Total keywords':     int(total),
                    'Active keywords':    int(mt_active),
                    'Inactive keywords':  int(total - mt_active),
                    'Active %':           round(mt_active / total * 100, 1) if total > 0 else 0.0,
                    'Active spend ($)':   round(mt_spend, 2),
                    'Active conversions': round(mt_convs, 1),
                })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Campaign data fetch (campaign_performance_daily — data from Jan 2025)
# ---------------------------------------------------------------------------

def fetch_campaign_monthly_activity(
    conn,
    google_id: str,
    months: list[str],
    month_labels: dict[str, str],
) -> pd.DataFrame:
    """
    Returns one row per campaign per month from campaign_performance_daily.
    Uses device='ALL' rows only. Joined to campaigns table for name/status.
    Months with no data for a campaign are omitted (not zero-filled) since
    campaigns may not yet have been created for earlier months.
    """
    acct = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (google_id,)
    ).fetchone()
    if not acct:
        return pd.DataFrame()
    account_id = acct['id']

    rows = []
    for month in months:
        start, end = month_bounds(month)

        results = conn.execute("""
            SELECT
                c.campaign_id,
                c.campaign_name,
                c.status                                      AS campaign_status,
                COALESCE(SUM(cpd.impressions), 0)            AS impressions,
                COALESCE(SUM(cpd.clicks), 0)                 AS clicks,
                COALESCE(SUM(cpd.cost_micros), 0) / 1e6     AS spend,
                COALESCE(SUM(cpd.conversions), 0)            AS conversions,
                COALESCE(SUM(cpd.conversion_value), 0)       AS revenue
            FROM campaign_performance_daily cpd
            JOIN campaigns c ON cpd.campaign_id = c.campaign_id
            JOIN accounts a  ON c.account_id    = a.id
            WHERE a.id         = ?
              AND cpd.device   = 'ALL'
              AND cpd.date BETWEEN ? AND ?
            GROUP BY c.campaign_id, c.campaign_name, c.status
            HAVING SUM(cpd.impressions) > 0
                OR SUM(cpd.clicks) > 0
                OR SUM(cpd.cost_micros) > 0
        """, (account_id, start, end)).fetchall()

        for r in results:
            spend = r['spend']
            clicks = r['clicks']
            convs  = r['conversions']
            rev    = r['revenue']
            rows.append({
                'month':           month,
                'month_label':     month_labels[month],
                'campaign_id':     r['campaign_id'],
                'campaign_name':   r['campaign_name'],
                'campaign_status': r['campaign_status'],
                'impressions':     int(r['impressions']),
                'clicks':          int(clicks),
                'spend':           round(spend, 2),
                'conversions':     round(convs, 1),
                'revenue':         round(rev, 2),
                'roas':            round(rev / spend, 2)   if spend > 0 else None,
                'cpc':             round(spend / clicks, 2) if clicks > 0 else None,
                'cpl':             round(spend / convs, 2)  if convs > 0 else None,
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# New Sheet 1 of 3: Campaign activity
# ---------------------------------------------------------------------------

def build_campaign_activity(
    campaign_df: pd.DataFrame,
    months: list[str],
    month_labels: dict[str, str],
) -> pd.DataFrame:
    """
    One row per campaign per month, sorted by Month then Spend descending.
    Pulled from campaign_performance_daily via fetch_campaign_monthly_activity().
    """
    if campaign_df.empty:
        return pd.DataFrame()

    rows = []
    for _, r in campaign_df.iterrows():
        rows.append({
            'Month':            r['month_label'],
            'Campaign':         r['campaign_name'],
            'Status':           r['campaign_status'],
            'Impressions':      r['impressions'],
            'Clicks':           r['clicks'],
            'Spend ($)':        r['spend'],
            'Conversions':      r['conversions'],
            'Revenue ($)':      r['revenue'],
            'ROAS':             r['roas'],
            'CPC ($)':          r['cpc'],
            'CPL ($)':          r['cpl'],
        })

    out = pd.DataFrame(rows)
    # Sort by month order (preserve chronological), then spend desc within month
    month_order = {month_labels[m]: i for i, m in enumerate(months)}
    out['_month_ord'] = out['Month'].map(month_order)
    out = out.sort_values(['_month_ord', 'Spend ($)'], ascending=[True, False])
    out = out.drop(columns=['_month_ord']).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# New Sheet 2 of 3: Structure snapshot
# ---------------------------------------------------------------------------

def build_structure_snapshot(
    conn,
    google_id: str,
    ms_id: str | None,
    months: list[str],
    month_labels: dict[str, str],
    kpd_months: list[str],
) -> pd.DataFrame:
    """
    One row per platform per month showing impression-based active/inactive counts.
    Active = had >= 1 impression in that month (from campaign_performance_daily
    for campaigns, keyword_performance_daily for keywords).
    Months with no performance data show zeros.
    """
    # Resolve account IDs
    g_acct = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (google_id,)
    ).fetchone()
    if not g_acct:
        return pd.DataFrame()
    g_id = g_acct['id']

    ms_account_id = None
    if ms_id:
        ms_acct = conn.execute(
            "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'microsoft'",
            (ms_id,)
        ).fetchone()
        if ms_acct:
            ms_account_id = ms_acct['id']

    # Total keyword instances (static counts — structure, not time-series)
    g_ki_total = conn.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?", (g_id,)
    ).fetchone()[0]

    ms_ki_total = 0
    if ms_account_id:
        ms_ki_total = conn.execute(
            "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?", (ms_account_id,)
        ).fetchone()[0]

    rows = []

    for month in months:
        start, end = month_bounds(month)

        # --- Google campaign activity ---
        g_camp = conn.execute("""
            SELECT
                COUNT(DISTINCT cpd.campaign_id)                                AS total_campaigns,
                COUNT(DISTINCT CASE WHEN cpd.impressions > 0
                      THEN cpd.campaign_id END)                                AS active_campaigns,
                COUNT(DISTINCT CASE WHEN cpd.impressions = 0
                      THEN cpd.campaign_id END)                                AS inactive_campaigns
            FROM campaign_performance_daily cpd
            JOIN campaigns c ON cpd.campaign_id = c.campaign_id
            JOIN accounts a  ON c.account_id    = a.id
            WHERE a.id = ?
              AND cpd.device = 'ALL'
              AND cpd.date BETWEEN ? AND ?
        """, (g_id, start, end)).fetchone()

        # --- Google keyword activity ---
        if month in kpd_months:
            g_kw = conn.execute("""
                SELECT
                    COUNT(DISTINCT kpd.instance_id)                                AS active_kw,
                    (SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?) AS total_kw
                FROM keyword_performance_daily kpd
                JOIN keyword_instances ki ON kpd.instance_id = ki.id
                JOIN accounts a           ON ki.account_id   = a.id
                WHERE a.id = ?
                  AND kpd.device    = 'ALL'
                  AND kpd.geo_state = 'ALL'
                  AND kpd.report_date BETWEEN ? AND ?
                  AND kpd.impressions > 0
            """, (g_id, g_id, start, end)).fetchone()
            g_kw_active   = g_kw['active_kw']   if g_kw else 0
            g_kw_inactive = g_ki_total - g_kw_active

            # Active keyword identities — distinct (text, match_type) pairs
            # where at least one instance had impressions > 0 this month.
            g_id_row = conn.execute("""
                SELECT COUNT(DISTINCT ki.identity_id) AS active_identities
                FROM keyword_performance_daily kpd
                JOIN keyword_instances ki ON kpd.instance_id = ki.id
                WHERE ki.account_id   = ?
                  AND kpd.device      = 'ALL'
                  AND kpd.geo_state   = 'ALL'
                  AND kpd.report_date BETWEEN ? AND ?
                  AND kpd.impressions > 0
            """, (g_id, start, end)).fetchone()
            g_active_identities = g_id_row['active_identities'] if g_id_row else 0
        else:
            g_kw_active         = 0
            g_kw_inactive       = 0
            g_active_identities = 0

        # --- Google active ad groups ---
        g_ag = conn.execute("""
            SELECT COUNT(DISTINCT agpd.ad_group_id) AS active_ad_groups
            FROM ad_group_performance_daily agpd
            JOIN ad_groups ag ON ag.ad_group_id = agpd.ad_group_id
            WHERE ag.account_id = ?
              AND agpd.device = 'ALL'
              AND agpd.date BETWEEN ? AND ?
              AND agpd.impressions > 0
        """, (g_id, start, end)).fetchone()
        g_active_ag = g_ag['active_ad_groups'] if g_ag else 0

        # --- Microsoft keyword activity ---
        ms_kw_active   = 0
        ms_kw_inactive = 0
        if ms_account_id and month in kpd_months:
            ms_kw = conn.execute("""
                SELECT COUNT(DISTINCT kpd.instance_id) AS active_kw
                FROM keyword_performance_daily kpd
                JOIN keyword_instances ki ON kpd.instance_id = ki.id
                JOIN accounts a           ON ki.account_id   = a.id
                WHERE a.id = ?
                  AND kpd.device    = 'ALL'
                  AND kpd.geo_state = 'ALL'
                  AND kpd.report_date BETWEEN ? AND ?
                  AND kpd.impressions > 0
            """, (ms_account_id, start, end)).fetchone()
            ms_kw_active   = ms_kw['active_kw'] if ms_kw else 0
            ms_kw_inactive = ms_ki_total - ms_kw_active

        label = month_labels[month]
        g_total_camps    = g_camp['total_campaigns']    if g_camp else 0
        g_active_camps   = g_camp['active_campaigns']   if g_camp else 0
        g_inactive_camps = g_camp['inactive_campaigns'] if g_camp else 0

        rows.append({
            'Month':                              label,
            'Platform':                           'Google',
            'Active campaigns (≥1 impression)':   g_active_camps,
            'Inactive campaigns (0 impressions)': g_inactive_camps,
            'Total campaigns in CPD':             g_total_camps,
            'Active ad groups (≥1 impression)':   g_active_ag,
            'Active keywords (≥1 impression)':    g_kw_active,
            'Inactive keywords (0 impressions)':  g_kw_inactive,
            'Total keyword instances':            g_ki_total,
            'Active keyword identities':          g_active_identities,
        })

        if ms_account_id is not None:
            rows.append({
                'Month':                              label,
                'Platform':                           'Microsoft',
                'Active campaigns (≥1 impression)':   0,
                'Inactive campaigns (0 impressions)': 0,
                'Total campaigns in CPD':             0,
                'Active ad groups (≥1 impression)':   0,
                'Active keywords (≥1 impression)':    ms_kw_active,
                'Inactive keywords (0 impressions)':  ms_kw_inactive,
                'Total keyword instances':            ms_ki_total,
                'Total keyword identities':           0,
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# New Sheet 3 of 3: Keyword density
# ---------------------------------------------------------------------------

def build_keyword_density(
    conn,
    google_id: str,
) -> pd.DataFrame:
    """
    One row per campaign showing keyword distribution across ad groups.
    Sorted by total keyword slots descending.

    Uses three separate aggregation queries (keyword_instances, ad_groups,
    campaigns) merged in Python rather than a single three-way JOIN, to
    avoid the cross-product explosion on 474K × 74K × 118 rows in SQLite.
    """
    g_acct = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (google_id,)
    ).fetchone()
    if not g_acct:
        return pd.DataFrame()
    g_id = g_acct['id']

    # Query 1: keyword counts per campaign
    kw_rows = conn.execute("""
        SELECT
            ki.campaign_id,
            ki.campaign_name,
            COUNT(ki.id)                                               AS total_kw_slots,
            SUM(CASE WHEN ki.status = 'ENABLED' THEN 1 ELSE 0 END)    AS enabled_kw_slots,
            SUM(CASE WHEN ki.match_type = 'EXACT'  THEN 1 ELSE 0 END) AS exact_count,
            SUM(CASE WHEN ki.match_type = 'PHRASE' THEN 1 ELSE 0 END) AS phrase_count,
            SUM(CASE WHEN ki.match_type = 'BROAD'  THEN 1 ELSE 0 END) AS broad_count
        FROM keyword_instances ki
        WHERE ki.account_id = ?
          AND ki.campaign_id IS NOT NULL
        GROUP BY ki.campaign_id
    """, (g_id,)).fetchall()

    # Query 2: ad group counts per campaign
    ag_rows = conn.execute("""
        SELECT
            ag.campaign_id,
            COUNT(ag.ad_group_id)                                     AS total_ad_groups,
            SUM(CASE WHEN ag.status = 'ENABLED' THEN 1 ELSE 0 END)   AS enabled_ad_groups
        FROM ad_groups ag
        WHERE ag.account_id = ?
        GROUP BY ag.campaign_id
    """, (g_id,)).fetchall()

    # Query 3: campaign status lookup
    camp_rows = conn.execute("""
        SELECT campaign_id, campaign_name, status
        FROM campaigns
        WHERE account_id = ?
    """, (g_id,)).fetchall()

    # Merge in Python
    ag_by_campaign = {r['campaign_id']: r for r in ag_rows}
    camp_by_id     = {r['campaign_id']: r for r in camp_rows}

    rows = []
    for r in kw_rows:
        cid        = r['campaign_id']
        ag         = ag_by_campaign.get(cid)
        camp       = camp_by_id.get(cid)
        total_ag   = ag['total_ad_groups']   if ag else 0
        enabled_ag = ag['enabled_ad_groups'] if ag else 0
        total_kw   = r['total_kw_slots']     or 0
        enabled_kw = r['enabled_kw_slots']   or 0
        rows.append({
            'Campaign':                          r['campaign_name'],
            'Status':                            camp['status'] if camp else '',
            'Total ad groups':                   total_ag,
            'Enabled ad groups':                 enabled_ag,
            'Total keyword slots':               total_kw,
            'Enabled keyword slots':             enabled_kw,
            'EXACT count':                       r['exact_count']  or 0,
            'PHRASE count':                      r['phrase_count'] or 0,
            'BROAD count':                       r['broad_count']  or 0,
            'Avg keywords / ad group':           round(total_kw / total_ag, 1)
                                                 if total_ag > 0 else None,
            'Avg keywords / enabled ad group':   round(total_kw / enabled_ag, 1)
                                                 if enabled_ag > 0 else None,
        })

    return pd.DataFrame(rows).sort_values(
        'Total keyword slots', ascending=False
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Analysis 2: Keyword lifecycle classification
# ---------------------------------------------------------------------------

def build_lifecycle(
    google_df: pd.DataFrame,
    ms_df: pd.DataFrame,
    months: list[str],
    month_labels: dict[str, str],
    has_ms: bool,
    google_total_instances: int = 0,
    ms_total_instances: int = 0,
) -> pd.DataFrame:
    """
    Classifies each keyword into a lifecycle state based on which months
    it was active. Works for any number of months (2 or 3+).

    Since fetch_keyword_monthly_activity now returns only rows with
    impressions > 0 (HAVING clause), keywords that were never active in
    any month won't appear in the DataFrame at all. We account for them
    by injecting a synthetic 'Never active' summary row using the total
    instance counts fetched separately.

    For a 3-month window the states are:
        Always active     -- active all 3 months
        Paused in M3      -- active M1+M2, not M3
        Paused in M2      -- active M1 only
        New in M2         -- not M1, active M2+M3
        New in M3         -- not M1 or M2, active M3 only
        New then paused   -- not M1, active M2, not M3
        Intermittent      -- active M1 and M3, not M2
        Never active      -- zero impressions in all months (count injected)
    """
    rows      = []
    platforms = [('Google', google_df, google_total_instances)]
    if has_ms and not ms_df.empty:
        platforms.append(('Microsoft', ms_df, ms_total_instances))

    m = months   # convenience alias

    for platform, df, total_instances in platforms:
        if df.empty:
            # No active keywords at all — inject a single Never active summary row
            if total_instances > 0:
                row = {
                    'Platform':       platform,
                    'Lifecycle':      'Never active',
                    'Keyword':        f'({total_instances:,} keywords — none active in window)',
                    'Match type':     '',
                    'Campaign':       '',
                    'Current status': '',
                    'Total spend ($)':   0.0,
                    'Total conversions': 0.0,
                }
                for mo in m:
                    label = month_labels[mo][:3]
                    row[f'{label} active']      = 'No'
                    row[f'{label} impressions'] = 0
                    row[f'{label} spend ($)']   = 0.0
                    row[f'{label} conversions'] = 0.0
                rows.append(row)
            continue

        pivot = df.pivot_table(
            index=['criterion_id', 'keyword_text', 'match_type',
                   'campaign_name', 'status'],
            columns='month',
            values=['is_active', 'impressions', 'spend', 'conversions'],
            aggfunc='first',
        ).reset_index()

        # Flatten multi-level columns
        pivot.columns = [
            '_'.join(str(c) for c in col).strip('_')
            if isinstance(col, tuple) else col
            for col in pivot.columns
        ]
        # Fill NaN for months where a keyword had no data row (sparse pivot)
        numeric_cols = [c for c in pivot.columns if any(
            c.startswith(p) for p in ('impressions_', 'spend_', 'conversions_', 'is_active_')
        )]
        pivot[numeric_cols] = pivot[numeric_cols].fillna(0)

        def active(col):
            return bool(pivot_row.get(f'is_active_{col}', False))

        seen_instance_ids = set()

        for _, pivot_row in pivot.iterrows():
            flags = [bool(pivot_row.get(f'is_active_{mo}', False)) for mo in m]
            seen_instance_ids.add(pivot_row.get('criterion_id'))

            if len(m) == 3:
                a, b, c = flags
                if   a and b and c:         lifecycle = 'Always active'
                elif a and b and not c:     lifecycle = f'Paused in {month_labels[m[2]][:3]}'
                elif a and not b and not c: lifecycle = f'Paused in {month_labels[m[1]][:3]}'
                elif not a and b and c:     lifecycle = f'New in {month_labels[m[1]][:3]}'
                elif not a and not b and c: lifecycle = f'New in {month_labels[m[2]][:3]}'
                elif not a and b and not c: lifecycle = 'New then paused'
                elif a and not b and c:     lifecycle = 'Intermittent'
                else:                       lifecycle = 'Never active'
            elif len(m) == 2:
                a, b = flags
                if   a and b:      lifecycle = 'Always active'
                elif a and not b:  lifecycle = f'Paused in {month_labels[m[1]][:3]}'
                elif not a and b:  lifecycle = f'New in {month_labels[m[1]][:3]}'
                else:              lifecycle = 'Never active'
            else:
                lifecycle = 'Active' if flags[0] else 'Never active'

            row = {
                'Platform':       platform,
                'Lifecycle':      lifecycle,
                'Keyword':        pivot_row.get('keyword_text', ''),
                'Match type':     pivot_row.get('match_type', ''),
                'Campaign':       pivot_row.get('campaign_name', ''),
                'Current status': pivot_row.get('status', ''),
            }
            total_spend = 0
            total_convs = 0
            for i, mo in enumerate(m):
                label    = month_labels[mo][:3]
                imps     = pivot_row.get(f'impressions_{mo}', 0) or 0
                sp       = pivot_row.get(f'spend_{mo}', 0) or 0
                cv       = pivot_row.get(f'conversions_{mo}', 0) or 0
                row[f'{label} active']       = 'Yes' if flags[i] else 'No'
                row[f'{label} impressions']  = int(imps)
                row[f'{label} spend ($)']    = round(sp, 2)
                row[f'{label} conversions']  = round(cv, 1)
                total_spend += sp
                total_convs += cv
            row['Total spend ($)']   = round(total_spend, 2)
            row['Total conversions'] = round(total_convs, 1)
            rows.append(row)

        # Inject Never active count for keywords with zero impressions across all kpd months.
        # These were excluded by the HAVING clause in the fetch, so we derive the count
        # as: total instances - unique instances seen with any impression in any month.
        never_active_count = total_instances - len(seen_instance_ids)
        if never_active_count > 0:
            row = {
                'Platform':       platform,
                'Lifecycle':      'Never active',
                'Keyword':        f'({never_active_count:,} keywords — zero impressions in window)',
                'Match type':     '',
                'Campaign':       '',
                'Current status': '',
                'Total spend ($)':   0.0,
                'Total conversions': 0.0,
            }
            for mo in m:
                label = month_labels[mo][:3]
                row[f'{label} active']      = 'No'
                row[f'{label} impressions'] = 0
                row[f'{label} spend ($)']   = 0.0
                row[f'{label} conversions'] = 0.0
            rows.append(row)

    df_out = pd.DataFrame(rows)
    return df_out.sort_values(
        ['Platform', 'Lifecycle', 'Total spend ($)'],
        ascending=[True, True, False],
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Analysis 3: Spend migration
# ---------------------------------------------------------------------------

def build_spend_migration(
    lifecycle_df: pd.DataFrame, months: list[str], month_labels: dict[str, str]
) -> pd.DataFrame:
    """
    Focuses on keywords that were active in the first month but became
    inactive — the paused states. Shows what spend and conversions were
    cut and whether those cuts look like good optimization or over-cutting.
    """
    first_label = month_labels[months[0]][:3]
    paused_states = {
        s for s in lifecycle_df['Lifecycle'].unique()
        if 'Paused' in s or 'paused' in s
    }
    paused = lifecycle_df[lifecycle_df['Lifecycle'].isin(paused_states)].copy()

    if paused.empty:
        return pd.DataFrame()

    def assess(row):
        spend = row['Total spend ($)']
        convs = row['Total conversions']
        if spend == 0:
            return 'No spend — clean cut'
        if convs == 0:
            return 'Zero conversions — good cut'
        cpl = spend / convs
        if cpl > 200:
            return 'High CPL — good cut'
        if cpl > 50:
            return 'Moderate CPL — review'
        return 'Low CPL — risky cut'

    paused['CPL ($)'] = paused.apply(
        lambda r: round(r['Total spend ($)'] / r['Total conversions'], 2)
        if r['Total conversions'] > 0 else None, axis=1
    )
    paused['Assessment'] = paused.apply(assess, axis=1)

    # Select columns present in the dataframe
    base_cols = ['Platform', 'Keyword', 'Match type', 'Lifecycle',
                 'Total spend ($)', 'Total conversions', 'CPL ($)',
                 'Assessment', 'Campaign', 'Current status']
    month_cols = [c for c in paused.columns
                  if any(month_labels[m][:3] in c for m in months)]
    select_cols = [c for c in base_cols + month_cols if c in paused.columns]

    out = paused[select_cols].copy()
    return out.sort_values(
        ['Platform', 'Assessment', 'Total spend ($)'],
        ascending=[True, True, False],
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Analysis 4: Platform comparison (Google-only accounts skip this)
# ---------------------------------------------------------------------------

def build_platform_comparison(
    google_df: pd.DataFrame,
    ms_df: pd.DataFrame,
    months: list[str],
    month_labels: dict[str, str],
) -> pd.DataFrame:
    """
    Side-by-side Google vs Microsoft summary by month.
    Only produced when has_microsoft = True for the account.
    """
    rows = []
    for month in months:
        g = google_df[google_df['month'] == month]
        m = ms_df[ms_df['month'] == month] if not ms_df.empty else pd.DataFrame()

        def plat_stats(df):
            active = df[df['is_active']]
            return {
                'total':   len(df),
                'active':  int(df['is_active'].sum()),
                'spend':   active['spend'].sum(),
                'clicks':  active['clicks'].sum(),
                'convs':   active['conversions'].sum(),
                'revenue': active['revenue'].sum(),
                'imps':    active['impressions'].sum(),
            }

        gs = plat_stats(g)
        ms = plat_stats(m) if not m.empty else {k: 0 for k in
             ['total', 'active', 'spend', 'clicks', 'convs', 'revenue', 'imps']}

        rows.append({
            'Month':                     month_labels[month],
            'Google total keywords':     gs['total'],
            'Google active keywords':    gs['active'],
            'Google active %':           round(gs['active'] / gs['total'] * 100, 1)
                                         if gs['total'] > 0 else 0,
            'Google impressions':        int(gs['imps']),
            'Google clicks':             int(gs['clicks']),
            'Google spend ($)':          round(gs['spend'], 2),
            'Google CPC ($)':            round(gs['spend'] / gs['clicks'], 2)
                                         if gs['clicks'] > 0 else None,
            'Google offline convs':      round(gs['convs'], 1),
            'Google CPL ($)':            round(gs['spend'] / gs['convs'], 2)
                                         if gs['convs'] > 0 else None,
            'Google offline revenue ($)':round(gs['revenue'], 2),
            'Google ROAS':               round(gs['revenue'] / gs['spend'], 2)
                                         if gs['spend'] > 0 else None,
            'Microsoft total keywords':  ms['total'],
            'Microsoft active keywords': ms['active'],
            'Microsoft active %':        round(ms['active'] / ms['total'] * 100, 1)
                                         if ms['total'] > 0 else 0,
            'Microsoft impressions':     int(ms['imps']),
            'Microsoft clicks':          int(ms['clicks']),
            'Microsoft spend ($)':       round(ms['spend'], 2),
            'Microsoft CPC ($)':         round(ms['spend'] / ms['clicks'], 2)
                                         if ms['clicks'] > 0 else None,
            'Microsoft offline convs':   round(ms['convs'], 1),
            'Microsoft CPL ($)':         round(ms['spend'] / ms['convs'], 2)
                                         if ms['convs'] > 0 else None,
            'Microsoft offline revenue ($)': round(ms['revenue'], 2),
            'Microsoft ROAS':            round(ms['revenue'] / ms['spend'], 2)
                                         if ms['spend'] > 0 else None,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Summary sheet
# ---------------------------------------------------------------------------

def build_summary(
    lifecycle_df: pd.DataFrame,
    migration_df: pd.DataFrame,
    campaign_df: pd.DataFrame,
    months: list[str],
    month_labels: dict[str, str],
    has_ms: bool,
    display_name: str,
) -> pd.DataFrame:
    rows  = {'Metric': [], 'Value': []}

    def add(metric, value):
        rows['Metric'].append(metric)
        rows['Value'].append(value)

    def blank():
        add('', '')

    add('Account', display_name)
    add('Analysis window',
        f"{month_labels[months[0]]} — {month_labels[months[-1]]}")
    add('Active = impressions >= 1 in that month', '')
    blank()

    platforms = ['Google']
    if has_ms:
        platforms.append('Microsoft')

    for platform in platforms:
        add(f'--- {platform} ---', '')
        ldf = lifecycle_df[lifecycle_df['Platform'] == platform]

        for state in sorted(ldf['Lifecycle'].unique()):
            mask  = ldf['Lifecycle'] == state
            count = mask.sum()
            spend = ldf[mask]['Total spend ($)'].sum()
            convs = ldf[mask]['Total conversions'].sum()
            if count > 0:
                add(f'  {state} — keywords', f'{count:,}')
                add(f'  {state} — spend ($)', f'${spend:,.2f}')
                if convs > 0:
                    add(f'  {state} — conversions', f'{convs:,.1f}')
        blank()

    if not migration_df.empty:
        add('--- Spend migration (paused keywords) ---', '')
        for platform in platforms:
            mdf = migration_df[migration_df['Platform'] == platform]
            if not mdf.empty:
                risky     = (mdf['Assessment'] == 'Low CPL — risky cut').sum()
                good      = mdf['Assessment'].str.contains('good cut|clean cut').sum()
                tot_spend = mdf['Total spend ($)'].sum()
                tot_convs = mdf['Total conversions'].sum()
                add(f'  {platform} paused keywords',      f'{len(mdf):,}')
                add(f'  {platform} spend removed ($)',    f'${tot_spend:,.2f}')
                add(f'  {platform} conversions removed',  f'{tot_convs:,.1f}')
                add(f'  {platform} risky cuts (low CPL)', f'{risky:,}')
                add(f'  {platform} clean cuts',           f'{good:,}')
            blank()

    # Campaign-level totals from campaign_performance_daily
    if not campaign_df.empty:
        blank()
        add('--- Campaign performance totals (campaign_performance_daily) ---', '')
        add('  Window',
            f"{month_labels[months[0]]} — {month_labels[months[-1]]}")
        tot_imp   = int(campaign_df['impressions'].sum())
        tot_clk   = int(campaign_df['clicks'].sum())
        tot_spend = campaign_df['spend'].sum()
        tot_convs = campaign_df['conversions'].sum()
        tot_rev   = campaign_df['revenue'].sum()
        add('  Total impressions',   f'{tot_imp:,}')
        add('  Total clicks',        f'{tot_clk:,}')
        add('  Total spend ($)',     f'${tot_spend:,.2f}')
        add('  Total conversions',   f'{tot_convs:,.1f}')
        add('  Total revenue ($)',   f'${tot_rev:,.2f}')
        if tot_spend > 0:
            add('  Overall ROAS',    f'{tot_rev / tot_spend:.2f}x')
            add('  Overall CPC ($)', f'${tot_spend / tot_clk:.2f}' if tot_clk > 0 else 'N/A')
            add('  Overall CPL ($)', f'${tot_spend / tot_convs:.2f}' if tot_convs > 0 else 'N/A')
        blank()
        # Per-month breakdown
        add('  Monthly breakdown (campaign_performance_daily)', '')
        for month in months:
            mdf = campaign_df[campaign_df['month'] == month]
            if mdf.empty:
                continue
            m_spend = mdf['spend'].sum()
            m_convs = mdf['conversions'].sum()
            m_rev   = mdf['revenue'].sum()
            m_clk   = int(mdf['clicks'].sum())
            m_imp   = int(mdf['impressions'].sum())
            label   = month_labels[month]
            add(f'  {label} — impressions',  f'{m_imp:,}')
            add(f'  {label} — clicks',       f'{m_clk:,}')
            add(f'  {label} — spend ($)',    f'${m_spend:,.2f}')
            add(f'  {label} — conversions',  f'{m_convs:,.1f}')
            add(f'  {label} — revenue ($)',  f'${m_rev:,.2f}')
            if m_spend > 0:
                add(f'  {label} — ROAS',     f'{m_rev / m_spend:.2f}x')
            blank()

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_workbook(path: Path, has_ms: bool):
    wb = load_workbook(path)

    lifecycle_colors = {
        'Always active':  'E2EFDA',
        'Never active':   'F2F2F2',
        'New then paused':'FCE4D6',
        'Intermittent':   'FFF2CC',
    }
    # Dynamic paused/new states get colours by keyword
    for state in ['Paused', 'paused']:
        lifecycle_colors[state] = 'FFF2CC'

    header_fills = {
        'Summary':            '1F4E79',
        'Monthly active':     '375623',
        'Campaign activity':  '7B3F00',
        'Structure snapshot': '1F4E79',
        'Keyword density':    '4A235A',
        'Keyword lifecycle':  '4A235A',
        'Spend migration':    '7B3F00',
        'Platform comparison':'1F4E79',
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
            ws.column_dimensions[col_letter].width = min(max_len + 4, 42)

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                hdr = str(ws.cell(1, cell.column).value or '').lower()
                if isinstance(cell.value, float):
                    if any(k in hdr for k in ('spend', 'revenue', 'cpl', 'cpc')):
                        cell.number_format = '$#,##0.00'
                    elif 'roas' in hdr:
                        cell.number_format = '0.00'
                    elif '%' in hdr:
                        cell.number_format = '0.0'
                    elif any(k in hdr for k in ('conv', 'impression', 'click')):
                        cell.number_format = '#,##0.0'

        # Colour lifecycle rows by state
        if sheet_name == 'Keyword lifecycle':
            lifecycle_col = None
            for cell in ws[1]:
                if cell.value == 'Lifecycle':
                    lifecycle_col = cell.column
                    break
            if lifecycle_col:
                for row in ws.iter_rows(min_row=2):
                    state = row[lifecycle_col - 1].value or ''
                    # Match by prefix for dynamic month-named states
                    color = None
                    if state == 'Always active':
                        color = 'E2EFDA'
                    elif state == 'Never active':
                        color = 'F2F2F2'
                    elif 'Paused' in state or 'paused' in state:
                        color = 'FFF2CC'
                    elif 'New' in state:
                        color = 'DEEAF1'
                    elif 'Intermittent' in state:
                        color = 'FFF2CC'
                    if color:
                        fill = PatternFill('solid', fgColor=color)
                        for cell in row:
                            cell.fill = fill

        ws.freeze_panes = 'A2'

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args       = parse_args()
    cfg        = ACCOUNT_MAP[args.account]
    google_id  = cfg['google_id']
    ms_id      = cfg['microsoft_id']
    has_ms     = cfg['has_microsoft']
    output_file = OUTPUT_DIR / f"activity_trend_{args.account}.xlsx"

    conn = get_conn()

    # Resolve months
    if args.months:
        months = sorted(args.months)
    elif args.account == 'home':
        # Dynamic: all complete months in campaign_performance_daily (Jan 2025 onward)
        months = detect_full_campaign_month_range(conn, google_id)
        if not months:
            print("ERROR: No campaign performance data found for this account.")
            print("Run 02_sync_keyword_structure.py and 03_sync_keyword_performance.py first.")
            conn.close()
            return
    else:
        months = detect_recent_months(conn, google_id, n=3)
        if not months:
            print("ERROR: No keyword performance data found for this account.")
            print("Run 02_sync_keyword_structure.py and 03_sync_keyword_performance.py first.")
            conn.close()
            return

    month_labels = make_month_labels(months)

    print('=' * 60)
    print(f'Activity Trend Analysis — {cfg["display_name"]}')
    print(f'Account  : {google_id}')
    print(f'Months   : {", ".join(month_labels[m] for m in months)}')
    print(f'Microsoft: {"yes (" + ms_id + ")" if has_ms and ms_id else "none"}')
    print(f'Output   : {output_file.name}')
    print('=' * 60)

    print('\nFetching Google monthly activity...')
    kpd_months = detect_kpd_months(conn, google_id)
    if not kpd_months:
        print('  No keyword performance data found yet — keyword sheets will be empty.')
    google_df = fetch_keyword_monthly_activity(
        conn, google_id, 'google', kpd_months, month_labels
    ) if kpd_months else pd.DataFrame()
    print(f'  {len(google_df):,} keyword-month rows  '
          f'(across {len(kpd_months)} months with kpd data)')

    ms_df = pd.DataFrame()
    if has_ms and ms_id:
        print('Fetching Microsoft monthly activity...')
        ms_kpd_months = detect_kpd_months(conn, ms_id) if ms_id else []
        ms_df = fetch_keyword_monthly_activity(
            conn, ms_id, 'microsoft', ms_kpd_months, month_labels
        ) if ms_kpd_months else pd.DataFrame()
        print(f'  {len(ms_df):,} keyword-month rows')
    else:
        print('Microsoft: skipped (no account configured)')

    # Resolve account IDs for instance counts
    g_acct_row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (google_id,)
    ).fetchone()
    g_acct_id = g_acct_row['id'] if g_acct_row else None

    ms_acct_id = None
    if has_ms and ms_id:
        ms_acct_row = conn.execute(
            "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'microsoft'",
            (ms_id,)
        ).fetchone()
        ms_acct_id = ms_acct_row['id'] if ms_acct_row else None

    google_total_instances = fetch_total_instance_count(conn, g_acct_id) if g_acct_id else 0
    ms_total_instances     = fetch_total_instance_count(conn, ms_acct_id) if ms_acct_id else 0
    print(f'  Google total keyword instances : {google_total_instances:,}')
    if has_ms:
        print(f'  Microsoft total keyword instances: {ms_total_instances:,}')

    print('Fetching campaign monthly activity...')
    campaign_df = fetch_campaign_monthly_activity(
        conn, google_id, months, month_labels
    )
    print(f'  {len(campaign_df):,} campaign-month rows')

    conn_for_structure = get_conn()   # keep separate cursor for structure queries

    print('\nBuilding monthly active counts...')
    monthly_df = build_monthly_active(
        google_df, ms_df, months, kpd_months, month_labels, has_ms
    )

    print('Building campaign activity sheet...')
    campaign_activity_df = build_campaign_activity(
        campaign_df, months, month_labels
    )

    print('Building structure snapshot...')
    structure_df = build_structure_snapshot(
        conn_for_structure, google_id, ms_id if has_ms else None,
        months, month_labels, kpd_months
    )

    print('Building keyword density...')
    density_df = build_keyword_density(conn_for_structure, google_id)

    conn_for_structure.close()

    print('Building keyword lifecycle classification...')
    lifecycle_df = build_lifecycle(
        google_df, ms_df, kpd_months, month_labels, has_ms,
        google_total_instances, ms_total_instances
    )

    print('Building spend migration analysis...')
    migration_df = build_spend_migration(lifecycle_df, kpd_months, month_labels)

    print('Building summary...')
    summary_df = build_summary(
        lifecycle_df, migration_df, campaign_df, months, month_labels,
        has_ms, cfg['display_name']
    )

    # Terminal preview
    print('\n--- Monthly active counts ---')
    for platform in (['Google'] + (['Microsoft'] if has_ms else [])):
        pdf = monthly_df[monthly_df['Platform'] == platform]
        print(f'\n  {platform}:')
        for _, row in pdf.iterrows():
            print(f'    {row["Month"][:3]}  {row["Match type"]:<8}  '
                  f'active={row["Active keywords"]:>5,}/{row["Total keywords"]:>5,}  '
                  f'({row["Active %"]:>5.1f}%)  '
                  f'spend=${row["Active spend ($)"]:>10,.2f}')
    print(f'\n  (Keyword data covers {len(kpd_months)} months: '
          f'{month_labels[kpd_months[0]] if kpd_months else "none"}'
          f' → {month_labels[kpd_months[-1]] if kpd_months else "none"})')

    print('\n--- Campaign activity totals ---')
    if not campaign_df.empty:
        for month in months:
            mdf = campaign_df[campaign_df['month'] == month]
            if not mdf.empty:
                print(f'  {month_labels[month]}: '
                      f'spend=${mdf["spend"].sum():>10,.2f}  '
                      f'clicks={int(mdf["clicks"].sum()):>6,}  '
                      f'convs={mdf["conversions"].sum():>6.1f}')

    print('\n--- Lifecycle summary ---')
    for platform in (['Google'] + (['Microsoft'] if has_ms else [])):
        ldf = lifecycle_df[lifecycle_df['Platform'] == platform]
        print(f'\n  {platform}:')
        for state, count in ldf['Lifecycle'].value_counts().items():
            spend = ldf[ldf['Lifecycle'] == state]['Total spend ($)'].sum()
            print(f'    {state:<30} {count:>5,} keywords  ${spend:>10,.2f}')

    # Platform comparison (only if Microsoft is active)
    platform_df = pd.DataFrame()
    if has_ms and not ms_df.empty:
        print('\nBuilding platform comparison...')
        platform_df = build_platform_comparison(
            google_df, ms_df, months, month_labels
        )
        print('\n--- Platform comparison ---')
        for _, row in platform_df.iterrows():
            print(f'\n  {row["Month"]}')
            print(f'    Google    active={row["Google active keywords"]:>5,}  '
                  f'({row["Google active %"]:>5.1f}%)  '
                  f'spend=${row["Google spend ($)"]:>10,.2f}  '
                  f'convs={row["Google offline convs"]:>7,.1f}  '
                  f'ROAS={row["Google ROAS"] or 0:.2f}x')
            print(f'    Microsoft active={row["Microsoft active keywords"]:>5,}  '
                  f'({row["Microsoft active %"]:>5.1f}%)  '
                  f'spend=${row["Microsoft spend ($)"]:>10,.2f}  '
                  f'convs={row["Microsoft offline convs"]:>7,.1f}  '
                  f'ROAS={row["Microsoft ROAS"] or 0:.2f}x')

    print(f'\nWriting Excel output...')
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        summary_df.to_excel(writer,          sheet_name='Summary',            index=False)
        monthly_df.to_excel(writer,          sheet_name='Monthly active',     index=False)
        if not campaign_activity_df.empty:
            campaign_activity_df.to_excel(writer, sheet_name='Campaign activity', index=False)
        if not structure_df.empty:
            structure_df.to_excel(writer,    sheet_name='Structure snapshot', index=False)
        if not density_df.empty:
            density_df.to_excel(writer,      sheet_name='Keyword density',    index=False)
        lifecycle_df.to_excel(writer,        sheet_name='Keyword lifecycle',  index=False)
        migration_df.to_excel(writer,        sheet_name='Spend migration',    index=False)
        if not platform_df.empty:
            platform_df.to_excel(writer,     sheet_name='Platform comparison',index=False)

    conn.close()

    print('Applying formatting...')
    format_workbook(output_file, has_ms)

    size_kb = os.path.getsize(output_file) / 1024
    print(f'\nOutput saved : {output_file}')
    print(f'File size    : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
