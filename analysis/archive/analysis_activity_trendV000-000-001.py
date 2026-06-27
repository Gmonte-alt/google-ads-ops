"""
analysis_activity_trend.py  [v2 — multi-account]
--------------------------
Keyword activity trend analysis — what is the team actively running
and how is that changing month over month?

Pass --account to select which account to analyze.
Pass --months to override the default three-month window.

Defines "active" as a keyword that generated at least one impression
in a given month — not simply ENABLED status, which can be misleading
when bids are too low or budgets are throttled.

For accounts with Microsoft (home):
    Produces 5 sheets including Platform Comparison.
For Google-only accounts (medicare, auto, term_life, auto_quote):
    Produces 4 sheets — Platform Comparison is omitted.

Sheets:
    1. Summary              -- headline keyword counts and spend by lifecycle
    2. Monthly active       -- active keyword counts by match type
    3. Keyword lifecycle    -- each keyword classified into one of eight states
    4. Spend migration      -- spend and conversions lost from paused keywords
    5. Platform comparison  -- Google vs Microsoft side-by-side (home only)

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


# ---------------------------------------------------------------------------
# Core data fetch
# ---------------------------------------------------------------------------

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
    month_labels: dict[str, str],
    has_ms: bool,
) -> pd.DataFrame:
    """
    Counts active keywords by month, platform, and match type.
    Microsoft rows are included only when the account has Microsoft data.
    """
    rows      = []
    platforms = [('Google', google_df)]
    if has_ms and not ms_df.empty:
        platforms.append(('Microsoft', ms_df))

    for platform, df in platforms:
        for month in months:
            mdf = df[df['month'] == month]
            for mt in ['EXACT', 'PHRASE', 'BROAD']:
                mt_mask   = mdf['match_type'] == mt
                mt_active = mdf[mt_mask]['is_active'].sum()
                mt_spend  = mdf[mt_mask & mdf['is_active']]['spend'].sum()
                mt_convs  = mdf[mt_mask & mdf['is_active']]['conversions'].sum()
                total     = mt_mask.sum()
                if total > 0:
                    rows.append({
                        'Platform':          platform,
                        'Month':             month_labels[month],
                        'Match type':        mt,
                        'Total keywords':    int(total),
                        'Active keywords':   int(mt_active),
                        'Inactive keywords': int(total - mt_active),
                        'Active %':          round(mt_active / total * 100, 1),
                        'Active spend ($)':  round(mt_spend, 2),
                        'Active conversions': round(mt_convs, 1),
                    })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Analysis 2: Keyword lifecycle classification
# ---------------------------------------------------------------------------

def build_lifecycle(
    google_df: pd.DataFrame,
    ms_df: pd.DataFrame,
    months: list[str],
    month_labels: dict[str, str],
    has_ms: bool,
) -> pd.DataFrame:
    """
    Classifies each keyword into a lifecycle state based on which months
    it was active. Works for any number of months (2 or 3).

    For a 3-month window the states are:
        Always active     -- active all 3 months
        Paused in M3      -- active M1+M2, not M3
        Paused in M2      -- active M1 only
        New in M2         -- not M1, active M2+M3
        New in M3         -- not M1 or M2, active M3 only
        New then paused   -- not M1, active M2, not M3
        Intermittent      -- active M1 and M3, not M2
        Never active      -- zero impressions in all months
    """
    rows      = []
    platforms = [('Google', google_df)]
    if has_ms and not ms_df.empty:
        platforms.append(('Microsoft', ms_df))

    m = months   # convenience alias

    for platform, df in platforms:
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

        def active(col):
            return bool(pivot_row.get(f'is_active_{col}', False))

        for _, pivot_row in pivot.iterrows():
            flags = [bool(pivot_row.get(f'is_active_{mo}', False)) for mo in m]

            if len(m) == 3:
                a, b, c = flags
                if   a and b and c:      lifecycle = 'Always active'
                elif a and b and not c:  lifecycle = f'Paused in {month_labels[m[2]][:3]}'
                elif a and not b and not c: lifecycle = f'Paused in {month_labels[m[1]][:3]}'
                elif not a and b and c:  lifecycle = f'New in {month_labels[m[1]][:3]}'
                elif not a and not b and c: lifecycle = f'New in {month_labels[m[2]][:3]}'
                elif not a and b and not c: lifecycle = 'New then paused'
                elif a and not b and c:  lifecycle = 'Intermittent'
                else:                    lifecycle = 'Never active'
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
    google_df = fetch_keyword_monthly_activity(
        conn, google_id, 'google', months, month_labels
    )
    print(f'  {len(google_df):,} keyword-month rows')

    ms_df = pd.DataFrame()
    if has_ms and ms_id:
        print('Fetching Microsoft monthly activity...')
        ms_df = fetch_keyword_monthly_activity(
            conn, ms_id, 'microsoft', months, month_labels
        )
        print(f'  {len(ms_df):,} keyword-month rows')
    else:
        print('Microsoft: skipped (no account configured)')

    conn.close()

    print('\nBuilding monthly active counts...')
    monthly_df = build_monthly_active(
        google_df, ms_df, months, month_labels, has_ms
    )

    print('Building keyword lifecycle classification...')
    lifecycle_df = build_lifecycle(
        google_df, ms_df, months, month_labels, has_ms
    )

    print('Building spend migration analysis...')
    migration_df = build_spend_migration(lifecycle_df, months, month_labels)

    print('Building summary...')
    summary_df = build_summary(
        lifecycle_df, migration_df, months, month_labels,
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
        summary_df.to_excel(writer,    sheet_name='Summary',           index=False)
        monthly_df.to_excel(writer,    sheet_name='Monthly active',    index=False)
        lifecycle_df.to_excel(writer,  sheet_name='Keyword lifecycle',  index=False)
        migration_df.to_excel(writer,  sheet_name='Spend migration',   index=False)
        if not platform_df.empty:
            platform_df.to_excel(writer, sheet_name='Platform comparison', index=False)

    print('Applying formatting...')
    format_workbook(output_file, has_ms)

    size_kb = os.path.getsize(output_file) / 1024
    print(f'\nOutput saved : {output_file}')
    print(f'File size    : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
