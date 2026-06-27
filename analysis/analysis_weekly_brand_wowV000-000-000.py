"""
analysis_weekly_brand_wow.py
-----------------------------
Weekly WoW brand-level performance analysis for Home Insurance.
Compares the most recent complete ISO week against the prior ISO week.

Metrics computed per brand (modifier_brand from search_queries):
    Revenue ($)         -- offline revenue (primary_conv_value)
    Cost ($)            -- ad spend
    GP ($)              -- Revenue - Cost
    GP %                -- GP / Revenue
    Rev/Click           -- Revenue / Clicks
    ROAS                -- Revenue / Cost
    Impressions
    Clicks
    CTR %               -- Clicks / Impressions
    CPC ($)             -- Cost / Clicks
    CPL ($)             -- Cost / Leads (offline conversions)
    RPL ($)             -- Revenue / Leads
    Leads               -- offline conversions (primary_conv_count)
    Lead CVR %          -- Leads / Clicks

WoW columns: current week value, prior week value, absolute change, % change.
Color-coded: green = improvement, red = deterioration (directional per metric).

Output:
    analysis/output/weekly_brand_wow.xlsx

Usage:
    conda activate paid-search
    python analysis/analysis_weekly_brand_wow.py

    Override week (default = most recent complete week):
    python analysis/analysis_weekly_brand_wow.py --week 2026-W13

    Compare specific weeks:
    python analysis/analysis_weekly_brand_wow.py --week 2026-W14 --prior 2026-W13
"""

import sqlite3
import argparse
import os
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import (Font, PatternFill, Alignment,
                              GradientFill, Border, Side)
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "weekly_brand_wow.xlsx"

GOOGLE_ACCOUNT_ID = "8886502147"

# Metrics where a DECREASE is good (lower is better)
LOWER_IS_BETTER = {'Cost ($)', 'CPC ($)', 'CPL ($)'}

# Metrics where an INCREASE is good (higher is better)
HIGHER_IS_BETTER = {
    'Revenue ($)', 'GP ($)', 'GP %', 'Rev/Click ($)', 'ROAS',
    'Impressions', 'Clicks', 'CTR %', 'RPL ($)', 'Leads', 'Lead CVR %',
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_most_recent_complete_weeks(conn, n: int = 2) -> list[str]:
    """
    Returns the N most recent complete ISO weeks that have data.

    Uses Python isocalendar() for true ISO 8601 week numbers (Monday-based).
    strftime('%W') is NOT used because it is one week behind isocalendar()
    for dates in the first few days of a new ISO year.

    Strategy: fetch all distinct report_dates from the DB, convert each
    to its ISO year+week in Python, deduplicate, sort descending,
    skip the current partial week, return the top N.
    """
    rows = conn.execute("""
        SELECT DISTINCT report_date
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        JOIN accounts a ON ki.account_id = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
        ORDER BY report_date DESC
        LIMIT 200
    """, (GOOGLE_ACCOUNT_ID,)).fetchall()

    today        = date.today()
    current_iso  = today.isocalendar()
    current_year = current_iso.year
    current_week = current_iso.week

    seen   = set()
    weeks  = []
    for r in rows:
        d      = date.fromisoformat(r['report_date'])
        iso    = d.isocalendar()
        label  = f"{iso.year}-W{str(iso.week).zfill(2)}"
        if label in seen:
            continue
        seen.add(label)
        # Skip the current partial week
        if iso.year == current_year and iso.week == current_week:
            continue
        weeks.append(label)
        if len(weeks) == n:
            break

    return weeks


def fetch_brand_week_performance(conn, week_label: str) -> pd.DataFrame:
    """
    Aggregates search query performance for a single ISO week,
    grouped by modifier_brand (brand name) and brand_category.

    modifier_brand = NULL is grouped as '(non-brand)'.
    """
    # Parse week label to get date range
    year, wnum = week_label.split('-W')
    # Monday of the week
    week_start = date.fromisocalendar(int(year), int(wnum), 1)
    week_end   = date.fromisocalendar(int(year), int(wnum), 7)

    return pd.read_sql_query("""
        SELECT
            COALESCE(sq.modifier_brand, '(non-brand)') AS brand,
            sq.brand_category,
            sq.vertical,
            SUM(sqp.impressions)                        AS impressions,
            SUM(sqp.clicks)                             AS clicks,
            SUM(sqp.cost_micros) / 1e6                  AS cost,
            SUM(sqp.primary_conv_count)                 AS leads,
            SUM(sqp.primary_conv_value)                 AS revenue
        FROM search_query_performance sqp
        JOIN search_queries sq       ON sqp.query_id   = sq.id
        JOIN keyword_instances ki    ON sqp.instance_id = ki.id
        JOIN accounts a              ON ki.account_id   = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND sqp.device = 'ALL'
          AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sq.modifier_brand, sq.brand_category, sq.vertical
        ORDER BY revenue DESC
    """, conn, params=(GOOGLE_ACCOUNT_ID,
                       str(week_start), str(week_end)))


def fetch_keyword_week_performance(conn, week_label: str) -> pd.DataFrame:
    """
    Aggregates keyword_performance_daily for a single ISO week,
    grouped by keyword_text + match_type + brand_category.
    Uses keyword-level performance data rather than search query data.
    Gives a cleaner CPL/CPC view because it reflects actual bid-level spend.
    """
    year, wnum = week_label.split('-W')
    week_start = date.fromisocalendar(int(year), int(wnum), 1)
    week_end   = date.fromisocalendar(int(year), int(wnum), 7)

    return pd.read_sql_query("""
        SELECT
            ki_id.normalized_text                        AS keyword,
            ki_id.match_type,
            COALESCE(ki_id.modifier_brand, '(non-brand)') AS brand,
            ki_id.brand_category,
            ki_id.vertical,
            SUM(kpd.impressions)                          AS impressions,
            SUM(kpd.clicks)                               AS clicks,
            SUM(kpd.cost_micros) / 1e6                    AS cost,
            SUM(kpd.primary_conv_count)                   AS leads,
            SUM(kpd.primary_conv_value)                   AS revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki     ON kpd.instance_id  = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id   = ki_id.id
        JOIN accounts a               ON ki.account_id    = a.id
        WHERE a.platform_account_id = ?
          AND a.platform = 'google'
          AND kpd.device = 'ALL'
          AND kpd.report_date BETWEEN ? AND ?
        GROUP BY ki_id.normalized_text, ki_id.match_type,
                 ki_id.modifier_brand, ki_id.brand_category, ki_id.vertical
        ORDER BY revenue DESC
    """, conn, params=(GOOGLE_ACCOUNT_ID,
                       str(week_start), str(week_end)))


def build_keyword_wow_table(curr_df: pd.DataFrame,
                             prior_df: pd.DataFrame,
                             curr_week: str,
                             prior_week: str) -> pd.DataFrame:
    """
    WoW table at keyword + match type grain.
    Sorted by current week revenue descending.
    """
    curr  = compute_metrics(curr_df)
    prior = compute_metrics(prior_df)

    key = ['keyword', 'match_type', 'brand', 'brand_category', 'vertical']
    merged = curr.merge(prior, on=key, how='outer',
                        suffixes=('_cw', '_pw')).fillna(0)

    rows = []
    for _, r in merged.iterrows():
        row = {
            'Keyword':        r['keyword'],
            'Match Type':     r['match_type'],
            'Brand':          r['brand'],
            'Brand Category': r.get('brand_category', ''),
            'Vertical':       r.get('vertical', ''),
        }
        for raw_col, display_name in METRIC_COLS:
            cw  = r.get(f'{raw_col}_cw', 0) or 0
            pw  = r.get(f'{raw_col}_pw', 0) or 0
            delta     = cw - pw
            delta_pct = (delta / abs(pw) * 100) if pw != 0 else np.nan
            row[f'{display_name} ({curr_week})']  = round(cw, 2)
            row[f'{display_name} ({prior_week})'] = round(pw, 2)
            row[f'{display_name} Δ']              = round(delta, 2)
            row[f'{display_name} Δ%']             = round(delta_pct, 1)                 if not np.isnan(delta_pct) else None
        rows.append(row)

    result = pd.DataFrame(rows)
    cw_rev = f'Revenue ($) ({curr_week})'
    if cw_rev in result.columns:
        result = result.sort_values(cw_rev, ascending=False)
    return result.reset_index(drop=True)


def build_keyword_summary_row(curr_df: pd.DataFrame,
                               prior_df: pd.DataFrame,
                               curr_week: str,
                               prior_week: str) -> pd.DataFrame:
    """Total row for keyword-level sheet."""
    curr_tot  = compute_metrics(curr_df.sum(numeric_only=True).to_frame().T)
    prior_tot = compute_metrics(prior_df.sum(numeric_only=True).to_frame().T)
    for d in [curr_tot, prior_tot]:
        d['gp_pct']   = (d['gp'] / d['revenue'] * 100).where(d['revenue'] > 0)
        d['rev_click'] = (d['revenue'] / d['clicks']).where(d['clicks'] > 0)
        d['roas']     = (d['revenue'] / d['cost']).where(d['cost'] > 0)
        d['ctr']      = (d['clicks'] / d['impressions'] * 100).where(d['impressions'] > 0)
        d['cpc']      = (d['cost'] / d['clicks']).where(d['clicks'] > 0)
        d['cpl']      = (d['cost'] / d['leads']).where(d['leads'] > 0)
        d['rpl']      = (d['revenue'] / d['leads']).where(d['leads'] > 0)
        d['lead_cvr'] = (d['leads'] / d['clicks'] * 100).where(d['clicks'] > 0)

    row = {'Keyword': 'TOTAL', 'Match Type': '', 'Brand': '',
           'Brand Category': '', 'Vertical': ''}
    for raw_col, display_name in METRIC_COLS:
        cw  = float(curr_tot[raw_col].iloc[0]) if raw_col in curr_tot else 0
        pw  = float(prior_tot[raw_col].iloc[0]) if raw_col in prior_tot else 0
        cw  = 0 if np.isnan(cw) else cw
        pw  = 0 if np.isnan(pw) else pw
        delta = cw - pw
        delta_pct = (delta / abs(pw) * 100) if pw != 0 else np.nan
        row[f'{display_name} ({curr_week})']  = round(cw, 2)
        row[f'{display_name} ({prior_week})'] = round(pw, 2)
        row[f'{display_name} Δ']              = round(delta, 2)
        row[f'{display_name} Δ%']             = round(delta_pct, 1)             if not np.isnan(delta_pct) else None
    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Adds derived metric columns to a raw performance DataFrame."""
    d = df.copy()
    d['gp']       = d['revenue'] - d['cost']
    d['gp_pct']   = np.where(d['revenue'] > 0,
                              d['gp'] / d['revenue'] * 100, np.nan)
    d['rev_click'] = np.where(d['clicks'] > 0,
                               d['revenue'] / d['clicks'], np.nan)
    d['roas']     = np.where(d['cost'] > 0,
                              d['revenue'] / d['cost'], np.nan)
    d['ctr']      = np.where(d['impressions'] > 0,
                              d['clicks'] / d['impressions'] * 100, np.nan)
    d['cpc']      = np.where(d['clicks'] > 0,
                              d['cost'] / d['clicks'], np.nan)
    d['cpl']      = np.where(d['leads'] > 0,
                              d['cost'] / d['leads'], np.nan)
    d['rpl']      = np.where(d['leads'] > 0,
                              d['revenue'] / d['leads'], np.nan)
    d['lead_cvr'] = np.where(d['clicks'] > 0,
                              d['leads'] / d['clicks'] * 100, np.nan)
    return d


METRIC_COLS = [
    ('revenue',   'Revenue ($)'),
    ('cost',      'Cost ($)'),
    ('gp',        'GP ($)'),
    ('gp_pct',    'GP %'),
    ('rev_click', 'Rev/Click ($)'),
    ('roas',      'ROAS'),
    ('impressions','Impressions'),
    ('clicks',    'Clicks'),
    ('ctr',       'CTR %'),
    ('cpc',       'CPC ($)'),
    ('cpl',       'CPL ($)'),
    ('rpl',       'RPL ($)'),
    ('leads',     'Leads'),
    ('lead_cvr',  'Lead CVR %'),
]


# ---------------------------------------------------------------------------
# WoW builder
# ---------------------------------------------------------------------------

def build_wow_table(curr_df: pd.DataFrame,
                    prior_df: pd.DataFrame,
                    curr_week: str,
                    prior_week: str) -> pd.DataFrame:
    """
    Joins current and prior week data by brand, computes WoW deltas.
    Returns a wide DataFrame with columns:
        Brand | Brand Category | Vertical |
        [metric] CW | [metric] PW | [metric] Δ | [metric] Δ% |
        ...for each metric
    """
    curr = compute_metrics(curr_df)
    prior = compute_metrics(prior_df)

    # Merge on brand + brand_category + vertical
    key = ['brand', 'brand_category', 'vertical']
    merged = curr.merge(prior, on=key, how='outer',
                        suffixes=('_cw', '_pw')).fillna(0)

    rows = []
    for _, r in merged.iterrows():
        row = {
            'Brand':          r['brand'],
            'Brand Category': r.get('brand_category', ''),
            'Vertical':       r.get('vertical', ''),
        }
        for raw_col, display_name in METRIC_COLS:
            cw  = r.get(f'{raw_col}_cw', 0) or 0
            pw  = r.get(f'{raw_col}_pw', 0) or 0
            delta     = cw - pw
            delta_pct = (delta / abs(pw) * 100) if pw != 0 else np.nan

            row[f'{display_name} ({curr_week})'] = round(cw, 2)
            row[f'{display_name} ({prior_week})'] = round(pw, 2)
            row[f'{display_name} Δ']             = round(delta, 2)
            row[f'{display_name} Δ%']            = round(delta_pct, 1) \
                                                    if not np.isnan(delta_pct) \
                                                    else None
        rows.append(row)

    result = pd.DataFrame(rows)
    # Sort by current week revenue descending
    cw_rev_col = f'Revenue ($) ({curr_week})'
    if cw_rev_col in result.columns:
        result = result.sort_values(cw_rev_col, ascending=False)
    return result.reset_index(drop=True)


def build_summary_row(curr_df: pd.DataFrame,
                      prior_df: pd.DataFrame,
                      curr_week: str,
                      prior_week: str) -> pd.DataFrame:
    """Builds a single TOTAL row for the top of the sheet."""
    curr_tot  = compute_metrics(curr_df.sum(numeric_only=True).to_frame().T)
    prior_tot = compute_metrics(prior_df.sum(numeric_only=True).to_frame().T)

    # Re-derive ratio metrics from totals
    for d in [curr_tot, prior_tot]:
        d['gp_pct']   = (d['gp'] / d['revenue'] * 100).where(d['revenue'] > 0)
        d['rev_click'] = (d['revenue'] / d['clicks']).where(d['clicks'] > 0)
        d['roas']     = (d['revenue'] / d['cost']).where(d['cost'] > 0)
        d['ctr']      = (d['clicks'] / d['impressions'] * 100).where(d['impressions'] > 0)
        d['cpc']      = (d['cost'] / d['clicks']).where(d['clicks'] > 0)
        d['cpl']      = (d['cost'] / d['leads']).where(d['leads'] > 0)
        d['rpl']      = (d['revenue'] / d['leads']).where(d['leads'] > 0)
        d['lead_cvr'] = (d['leads'] / d['clicks'] * 100).where(d['clicks'] > 0)

    row = {'Brand': 'TOTAL', 'Brand Category': '', 'Vertical': ''}
    for raw_col, display_name in METRIC_COLS:
        cw  = float(curr_tot[raw_col].iloc[0]) if raw_col in curr_tot else 0
        pw  = float(prior_tot[raw_col].iloc[0]) if raw_col in prior_tot else 0
        cw  = 0 if np.isnan(cw) else cw
        pw  = 0 if np.isnan(pw) else pw
        delta = cw - pw
        delta_pct = (delta / abs(pw) * 100) if pw != 0 else np.nan

        row[f'{display_name} ({curr_week})'] = round(cw, 2)
        row[f'{display_name} ({prior_week})'] = round(pw, 2)
        row[f'{display_name} Δ']             = round(delta, 2)
        row[f'{display_name} Δ%']            = round(delta_pct, 1) \
                                                if not np.isnan(delta_pct) \
                                                else None

    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# Excel formatting
# ---------------------------------------------------------------------------

def format_wow_workbook(path: Path, curr_week: str, prior_week: str):
    """
    Applies formatting to the WoW Excel output:
    - Header row: dark blue background, white bold text
    - TOTAL row: medium blue background
    - Δ% columns: green text for improvement, red for deterioration
    - Metric group separators (light border between metric groups)
    - Frozen panes at B2 (brand column + header locked)
    - Auto column width
    """
    wb = load_workbook(path)

    GREEN = 'FF006400'   # dark green text
    RED   = 'FFCC0000'   # dark red text
    AMBER = 'FF996600'   # amber for small changes

    HDR_FILL   = PatternFill('solid', fgColor='1F4E79')
    TOTAL_FILL = PatternFill('solid', fgColor='2E75B6')
    ALT_FILL   = PatternFill('solid', fgColor='EBF3FB')
    WHITE_FONT = Font(color='FFFFFF', bold=True, size=10)
    BOLD_WHITE = Font(color='FFFFFF', bold=True, size=11)

    thin = Side(style='thin', color='CCCCCC')
    med  = Side(style='medium', color='4472C4')

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        max_row = ws.max_row
        max_col = ws.max_column

        # Header row
        for cell in ws[1]:
            cell.fill      = HDR_FILL
            cell.font      = WHITE_FONT
            cell.alignment = Alignment(horizontal='center',
                                       vertical='center', wrap_text=True)
        ws.row_dimensions[1].height = 48

        # TOTAL row (row 2)
        for cell in ws[2]:
            cell.fill = TOTAL_FILL
            cell.font = Font(color='FFFFFF', bold=True, size=10)
            cell.alignment = Alignment(horizontal='center', vertical='center')

        # Alternating row shading from row 3
        for row_idx in range(3, max_row + 1):
            if row_idx % 2 == 0:
                for cell in ws[row_idx]:
                    if not cell.fill or cell.fill.fgColor.rgb in ('00000000', 'FFFFFFFF'):
                        cell.fill = ALT_FILL

        # Color Δ% columns based on direction
        for col_idx in range(1, max_col + 1):
            hdr = str(ws.cell(1, col_idx).value or '')
            if not hdr.endswith('Δ%'):
                continue

            # Determine the metric name from header
            metric_name = hdr.replace(' Δ%', '').strip()
            is_lower_better = metric_name in LOWER_IS_BETTER

            for row_idx in range(3, max_row + 1):
                cell = ws.cell(row_idx, col_idx)
                val  = cell.value
                if val is None:
                    continue
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue

                # For lower-is-better: negative change = green
                # For higher-is-better: positive change = green
                if is_lower_better:
                    is_good = val < -2
                    is_bad  = val > 2
                else:
                    is_good = val > 2
                    is_bad  = val < -2

                if is_good:
                    cell.font = Font(color=GREEN, bold=True, size=10)
                elif is_bad:
                    cell.font = Font(color=RED, bold=True, size=10)
                else:
                    cell.font = Font(color='FF666666', size=10)

        # Number formatting
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if cell.value is None:
                    continue
                hdr = str(ws.cell(1, cell.column).value or '').lower()
                if not isinstance(cell.value, (int, float)):
                    continue
                if 'δ%' in hdr or 'ctr' in hdr or 'gp %' in hdr or 'cvr' in hdr:
                    cell.number_format = '+0.0%;-0.0%;0.0%' \
                        if 'δ%' in hdr else '0.00%'
                elif '$' in hdr or 'revenue' in hdr or 'cost' in hdr \
                        or 'gp' in hdr or 'cpl' in hdr or 'rpl' in hdr \
                        or 'cpc' in hdr or 'rev/click' in hdr:
                    cell.number_format = '$#,##0.00'
                elif 'roas' in hdr:
                    cell.number_format = '0.00'
                elif 'impression' in hdr or 'click' in hdr or 'lead' in hdr:
                    cell.number_format = '#,##0'
                elif 'δ' in hdr:
                    cell.number_format = '+#,##0.00;-#,##0.00;0.00'

        # Auto-fit columns
        for col in ws.columns:
            max_len    = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                try:
                    val_len = len(str(cell.value)) if cell.value else 0
                    max_len = max(max_len, val_len)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 3, 28)

        # Freeze: brand + category + vertical columns locked
        ws.freeze_panes = 'D2'

    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Weekly WoW brand performance analysis'
    )
    parser.add_argument('--week', help='Current week label e.g. 2026-W14')
    parser.add_argument('--prior', help='Prior week label e.g. 2026-W13')
    args = parser.parse_args()

    conn = get_conn()

    # Resolve weeks
    if args.week and args.prior:
        curr_week  = args.week
        prior_week = args.prior
    else:
        recent = get_most_recent_complete_weeks(conn, n=2)
        if len(recent) < 2:
            print("ERROR: Not enough complete weeks in database.")
            return
        curr_week  = recent[0]
        prior_week = recent[1]

    print('=' * 65)
    print('Weekly WoW Brand Analysis — Home Insurance Google')
    print(f'  Current week : {curr_week}')
    print(f'  Prior week   : {prior_week}')
    print('=' * 65)

    # Fetch data
    print('\nFetching current week performance...')
    curr_df  = fetch_brand_week_performance(conn, curr_week)
    print(f'  {len(curr_df):,} brand rows, '
          f'${curr_df["revenue"].sum():,.2f} revenue')

    print('Fetching prior week performance (search query level)...')
    prior_df = fetch_brand_week_performance(conn, prior_week)
    print(f'  {len(prior_df):,} brand rows, '
          f'${prior_df["revenue"].sum():,.2f} revenue')

    print('\nFetching current week performance (keyword level)...')
    curr_kw_df  = fetch_keyword_week_performance(conn, curr_week)
    print(f'  {len(curr_kw_df):,} keyword rows, '
          f'${curr_kw_df["revenue"].sum():,.2f} revenue')

    print('Fetching prior week performance (keyword level)...')
    prior_kw_df = fetch_keyword_week_performance(conn, prior_week)
    print(f'  {len(prior_kw_df):,} keyword rows, '
          f'${prior_kw_df["revenue"].sum():,.2f} revenue')

    conn.close()

    # Build tables
    print('\nBuilding WoW table...')
    summary_row = build_summary_row(curr_df, prior_df, curr_week, prior_week)
    detail_rows = build_wow_table(curr_df, prior_df, curr_week, prior_week)
    full_table  = pd.concat([summary_row, detail_rows], ignore_index=True)

    # Keyword-level WoW
    print('\nBuilding keyword-level WoW table...')
    kw_summary_row = build_keyword_summary_row(
        curr_kw_df, prior_kw_df, curr_week, prior_week)
    kw_detail_rows = build_keyword_wow_table(
        curr_kw_df, prior_kw_df, curr_week, prior_week)
    kw_full_table  = pd.concat([kw_summary_row, kw_detail_rows],
                                ignore_index=True)

    # Terminal preview -- TOTAL row only
    print('\n--- TOTAL WoW summary ---')
    tot = summary_row.iloc[0]
    metrics_preview = [
        ('Revenue ($)', 'Revenue'),
        ('Cost ($)',    'Cost'),
        ('GP ($)',      'GP'),
        ('GP %',        'GP%'),
        ('ROAS',        'ROAS'),
        ('CPC ($)',     'CPC'),
        ('CPL ($)',     'CPL'),
        ('Leads',       'Leads'),
    ]
    for metric, label in metrics_preview:
        cw_col  = f'{metric} ({curr_week})'
        pw_col  = f'{metric} ({prior_week})'
        dpct_col = f'{metric} Δ%'
        cw  = tot.get(cw_col, 0)
        pw  = tot.get(pw_col, 0)
        pct = tot.get(dpct_col, None)
        pct_str = f'{pct:+.1f}%' if pct is not None else 'N/A'
        print(f'  {label:<12} CW={cw:>10.2f}  PW={pw:>10.2f}  WoW={pct_str}')

    # Top 5 movers by GP change
    gp_col = f'GP ($) Δ'
    if gp_col in detail_rows.columns:
        movers = detail_rows.nsmallest(5, gp_col)
        print(f'\n--- Top 5 GP decliners (WoW) ---')
        for _, r in movers.iterrows():
            brand = r['Brand']
            gp_chg = r.get(gp_col, 0)
            gp_pct = r.get('GP % Δ%', None)
            rev_chg = r.get(f'Revenue ($) Δ', 0)
            pct_str = f'{gp_pct:+.1f}%' if gp_pct is not None else ''
            print(f'  {str(brand):<30}  GP Δ={gp_chg:>+10.2f}  '
                  f'Rev Δ={rev_chg:>+10.2f}  GP% WoW={pct_str}')

    # Write Excel
    print(f'\nWriting Excel output...')
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        full_table.to_excel(writer,    sheet_name='WoW by Brand (SQ)',     index=False)
        kw_full_table.to_excel(writer, sheet_name='WoW by Keyword',        index=False)

    print('Applying formatting...')
    format_wow_workbook(OUTPUT_FILE, curr_week, prior_week)
    # Note: formatting applies to all sheets via the loop in format_workbook

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f'\nOutput : {OUTPUT_FILE}')
    print(f'Size   : {size_kb:.1f} KB')
    print('\nDone.')


if __name__ == '__main__':
    main()
