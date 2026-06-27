"""
medicare_06_dayparting.py
==========================
Medicare Dayparting Analysis — Hourly Spend & Conversion Patterns
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Data source: campaign_performance_hourly
  - Both active campaigns combined (OVER-TIER1 + UNDER-TIER1) as account-level view
  - Split into OEP (Jan 7 – Mar 31 2026) and SEP (Apr 1 – present)
  - Hours in Mountain Time (account timezone)

Key business context baked in:
  - Hours 0–7 MT   = off-hours (pre-buyer-active) — flagged RED
  - Hours 8–21 MT  = active buyer window (ET to PT buyers) — normal
  - Hours 22–23 MT = marginal late hours — flagged AMBER
  - Conversions = billable calls (Google Ads fires on connected/qualifying call)

Produces sheets:
  06_Hourly_Spend_Curve     OEP vs SEP avg hourly spend, clicks, conversions by hour
  06_Hourly_CPA             CPA by hour — efficient vs wasteful hours
  06_Budget_Pacing          Cumulative spend by hour — shows if budget exhausts early
  06_Off_Hours_Waste        Total spend in hours 0–7 MT with zero/near-zero conversions
  06_Peak_vs_OffPeak        Summary: active window vs off-hours spend and billable call split

Usage:
  python medicare_06_dayparting.py
  python medicare_06_dayparting.py --out custom_path.xlsx
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from datetime import date, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent))
from medicare_utils import (
    get_db_connection, MEDICARE_ACCOUNT_ID,
    write_title_row, write_dataframe_to_sheet,
    autofit_columns, freeze_header_rows, XL, safe_div, micros_to_dollars
)

OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\analysis\output")

# ── Period definitions ────────────────────────────────────────────────────────
OEP_START = "2026-01-07"
OEP_END   = "2026-03-31"
SEP_START = "2026-04-01"
SEP_END   = (date.today() - timedelta(days=1)).isoformat()

# Mountain Time buyer window
OFF_HOURS_EARLY  = list(range(0, 8))    # 0–7 MT: pre-market, flag red
ACTIVE_HOURS     = list(range(8, 22))   # 8–21 MT: buyer window
OFF_HOURS_LATE   = [22, 23]             # 22–23 MT: marginal, flag amber

HOUR_LABELS = {h: f"{h:02d}:00 MT" for h in range(24)}


# ── Queries ───────────────────────────────────────────────────────────────────

def q_hourly_raw(conn, start: str, end: str) -> pd.DataFrame:
    """Pull hourly data for both active campaigns, combined."""
    rows = conn.execute("""
        SELECT
            h.hour,
            h."date",
            SUM(h.cost_micros) / 1e6        AS spend,
            SUM(h.clicks)                    AS clicks,
            SUM(h.impressions)               AS impressions,
            SUM(h.conversions)               AS conversions,
            SUM(h.conversion_value)          AS conv_value,
            COUNT(DISTINCT h."date")         AS days_in_avg
        FROM campaign_performance_hourly h
        WHERE h.account_id = ?
          AND h."date" BETWEEN ? AND ?
          AND h.device = 'MOBILE'
        GROUP BY h.hour, h."date"
        ORDER BY h."date", h.hour
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def build_hourly_summary(df_raw: pd.DataFrame, period_label: str) -> pd.DataFrame:
    """
    Aggregate raw hourly data to avg per hour across all days in period.
    Also computes totals and flags off-hours.
    """
    if df_raw.empty:
        return pd.DataFrame()

    n_days = df_raw['date'].nunique()

    agg = df_raw.groupby('hour').agg(
        total_spend       = ('spend',       'sum'),
        total_clicks      = ('clicks',      'sum'),
        total_impressions = ('impressions',  'sum'),
        total_conversions = ('conversions',  'sum'),
        total_conv_value  = ('conv_value',   'sum'),
        days_active       = ('date',         'nunique'),
    ).reset_index()

    agg['period']        = period_label
    agg['n_days']        = n_days
    agg['avg_spend']     = agg['total_spend']       / n_days
    agg['avg_clicks']    = agg['total_clicks']       / n_days
    agg['avg_conv']      = agg['total_conversions']  / n_days
    agg['avg_conv_val']  = agg['total_conv_value']   / n_days
    agg['cpa']           = agg.apply(lambda r: safe_div(r.total_spend, r.total_conversions), axis=1)
    agg['cvr']           = agg.apply(lambda r: safe_div(r.total_conversions, r.total_clicks), axis=1)
    agg['cpc']           = agg.apply(lambda r: safe_div(r.total_spend, r.total_clicks), axis=1)

    agg['hour_label']    = agg['hour'].map(HOUR_LABELS)
    agg['buyer_window']  = agg['hour'].apply(
        lambda h: 'Off-Hours (0–7 MT)'   if h in OFF_HOURS_EARLY
        else      'Marginal (22–23 MT)'  if h in OFF_HOURS_LATE
        else      'Active (8–21 MT)'
    )

    # Pct of daily total for context
    total_spend_day = agg['avg_spend'].sum()
    total_conv_day  = agg['avg_conv'].sum()
    agg['pct_daily_spend'] = agg['avg_spend'].apply(
        lambda x: safe_div(x, total_spend_day, 0)
    )
    agg['pct_daily_conv']  = agg['avg_conv'].apply(
        lambda x: safe_div(x, total_conv_day, 0)
    )

    col_order = [
        'period', 'hour', 'hour_label', 'buyer_window',
        'avg_spend', 'pct_daily_spend', 'avg_clicks', 'avg_conv',
        'pct_daily_conv', 'avg_conv_val', 'cpa', 'cvr', 'cpc',
        'total_spend', 'total_conversions', 'n_days',
    ]
    return agg[[c for c in col_order if c in agg.columns]]


def build_cpa_by_hour(oep_df: pd.DataFrame, sep_df: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side CPA, spend, and conversions by hour for OEP vs SEP."""
    if oep_df.empty and sep_df.empty:
        return pd.DataFrame()

    rows = []
    for hour in range(24):
        r = {'hour': hour, 'hour_label': HOUR_LABELS[hour],
             'buyer_window': (
                 'Off-Hours (0–7 MT)' if hour in OFF_HOURS_EARLY
                 else 'Marginal (22–23 MT)' if hour in OFF_HOURS_LATE
                 else 'Active (8–21 MT)'
             )}

        for label, df in [('oep', oep_df), ('sep', sep_df)]:
            match = df[df['hour'] == hour] if not df.empty else pd.DataFrame()
            if not match.empty:
                r[f'{label}_avg_spend'] = match['avg_spend'].iloc[0]
                r[f'{label}_avg_conv']  = match['avg_conv'].iloc[0]
                r[f'{label}_cpa']       = match['cpa'].iloc[0]
                r[f'{label}_pct_spend'] = match['pct_daily_spend'].iloc[0]
                r[f'{label}_pct_conv']  = match['pct_daily_conv'].iloc[0]
            else:
                r[f'{label}_avg_spend'] = None
                r[f'{label}_avg_conv']  = None
                r[f'{label}_cpa']       = None
                r[f'{label}_pct_spend'] = None
                r[f'{label}_pct_conv']  = None

        rows.append(r)

    return pd.DataFrame(rows)


def build_budget_pacing(oep_df: pd.DataFrame, sep_df: pd.DataFrame) -> pd.DataFrame:
    """
    Cumulative spend by hour showing budget depletion curve.
    If budget exhausts by hour 14 (2pm MT), hours 15–23 get no spend.
    """
    rows = []
    for hour in range(24):
        r = {'hour': hour, 'hour_label': HOUR_LABELS[hour]}
        for label, df in [('oep', oep_df), ('sep', sep_df)]:
            if df.empty:
                r[f'{label}_cumulative_spend']    = None
                r[f'{label}_cumulative_conv']     = None
                r[f'{label}_pct_budget_used']     = None
                continue
            cum_spend = df[df['hour'] <= hour]['avg_spend'].sum()
            cum_conv  = df[df['hour'] <= hour]['avg_conv'].sum()
            total_day = df['avg_spend'].sum()
            r[f'{label}_cumulative_spend'] = round(cum_spend, 2)
            r[f'{label}_cumulative_conv']  = round(cum_conv, 2)
            r[f'{label}_pct_budget_used']  = safe_div(cum_spend, total_day, 0)
        rows.append(r)
    return pd.DataFrame(rows)


def build_off_hours_waste(conn, oep_start, oep_end, sep_start, sep_end) -> pd.DataFrame:
    """
    Total spend, clicks, conversions in off-hours (0–7 MT) by campaign and period.
    Shows exactly what is being spent before buyers are active.
    """
    rows = []
    for label, start, end in [('OEP', oep_start, oep_end), ('SEP', sep_start, sep_end)]:
        result = conn.execute("""
            SELECT
                c.campaign_name,
                h.hour,
                SUM(h.cost_micros) / 1e6   AS spend,
                SUM(h.clicks)              AS clicks,
                SUM(h.conversions)         AS conversions,
                COUNT(DISTINCT h."date")   AS days
            FROM campaign_performance_hourly h
            JOIN campaigns c ON h.campaign_id = c.campaign_id
            WHERE h.account_id = ?
              AND h."date" BETWEEN ? AND ?
              AND h.device = 'MOBILE'
              AND h.hour BETWEEN 0 AND 7
            GROUP BY c.campaign_name, h.hour
            ORDER BY spend DESC
        """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

        for r in result:
            d = dict(r)
            d['period'] = label
            d['hour_label'] = HOUR_LABELS[d['hour']]
            d['cpa'] = safe_div(d['spend'], d['conversions'])
            rows.append(d)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    col_order = ['period', 'campaign_name', 'hour', 'hour_label',
                 'spend', 'clicks', 'conversions', 'cpa', 'days']
    return df[[c for c in col_order if c in df.columns]]


def build_peak_vs_offpeak(oep_df: pd.DataFrame, sep_df: pd.DataFrame) -> pd.DataFrame:
    """
    Summary: active window (8–21) vs off-hours total for each period.
    Shows the proportion of budget going to hours where calls can't be taken.
    """
    rows = []
    for label, df in [('OEP (Jan 7 – Mar 31, 2026)', oep_df),
                      ('SEP (Apr 1 – present)', sep_df)]:
        if df.empty:
            continue
        for window_label, hours in [
            ('Off-Hours Early (0–7 MT)',  OFF_HOURS_EARLY),
            ('Active Window (8–21 MT)',   ACTIVE_HOURS),
            ('Marginal Late (22–23 MT)',  OFF_HOURS_LATE),
        ]:
            subset = df[df['hour'].isin(hours)]
            spend = subset['avg_spend'].sum()
            conv  = subset['avg_conv'].sum()
            total_spend = df['avg_spend'].sum()
            total_conv  = df['avg_conv'].sum()
            rows.append({
                'period':          label,
                'window':          window_label,
                'avg_daily_spend': round(spend, 2),
                'pct_of_daily_spend': safe_div(spend, total_spend, 0),
                'avg_daily_conv':  round(conv, 2),
                'pct_of_daily_conv': safe_div(conv, total_conv, 0),
                'implied_cpa':     safe_div(spend, conv),
            })
    return pd.DataFrame(rows)


# ── Build all sheets ──────────────────────────────────────────────────────────

def build_sheets() -> dict[str, pd.DataFrame]:
    conn = get_db_connection()

    oep_raw = q_hourly_raw(conn, OEP_START, OEP_END)
    sep_raw = q_hourly_raw(conn, SEP_START, SEP_END)

    oep_summary = build_hourly_summary(oep_raw, 'OEP')
    sep_summary = build_hourly_summary(sep_raw, 'SEP')

    # Combined for the spend curve sheet
    combined = pd.concat([oep_summary, sep_summary], ignore_index=True) if not (
        oep_summary.empty and sep_summary.empty) else pd.DataFrame()

    sheets = {
        '06_Hourly_Spend_Curve': combined,
        '06_Hourly_CPA':         build_cpa_by_hour(oep_summary, sep_summary),
        '06_Budget_Pacing':      build_budget_pacing(oep_summary, sep_summary),
        '06_Off_Hours_Waste':    build_off_hours_waste(conn, OEP_START, OEP_END, SEP_START, SEP_END),
        '06_Peak_vs_OffPeak':    build_peak_vs_offpeak(oep_summary, sep_summary),
    }
    conn.close()
    return sheets


# ── Excel writer ──────────────────────────────────────────────────────────────

def write_workbook(sheets: dict[str, pd.DataFrame], output_path: Path) -> None:
    wb  = Workbook()
    wb.remove(wb.active)

    period_label = (
        f"OEP: {OEP_START} → {OEP_END}  |  SEP: {SEP_START} → {SEP_END}"
    )

    fmt_dollar  = XL.FMT_DOLLAR
    fmt_pct     = XL.FMT_PCT_1
    fmt_int     = XL.FMT_INT
    fmt_float   = XL.FMT_FLOAT_2

    sheet_configs = {
        '06_Hourly_Spend_Curve': {
            'title':    'Medicare — Hourly Spend Curve (Account Level)',
            'subtitle': 'Average hourly spend, conversions, and CPA — OEP vs SEP combined',
            'formats': {
                'avg_spend':      fmt_dollar,
                'avg_conv_val':   fmt_dollar,
                'cpa':            fmt_dollar,
                'cpc':            fmt_dollar,
                'pct_daily_spend':fmt_pct,
                'pct_daily_conv': fmt_pct,
                'cvr':            fmt_pct,
                'total_spend':    fmt_dollar,
            },
            'widths': {'hour_label': 16, 'buyer_window': 25, 'period': 8},
        },
        '06_Hourly_CPA': {
            'title':    'Medicare — CPA by Hour (OEP vs SEP)',
            'subtitle': 'Average CPA, spend, and conversions per hour — Mountain Time',
            'formats': {
                'oep_avg_spend':  fmt_dollar,
                'sep_avg_spend':  fmt_dollar,
                'oep_cpa':        fmt_dollar,
                'sep_cpa':        fmt_dollar,
                'oep_pct_spend':  fmt_pct,
                'sep_pct_spend':  fmt_pct,
                'oep_pct_conv':   fmt_pct,
                'sep_pct_conv':   fmt_pct,
            },
            'widths': {'hour_label': 16, 'buyer_window': 25},
            'flag_col': 'buyer_window',
        },
        '06_Budget_Pacing': {
            'title':    'Medicare — Cumulative Budget Pacing by Hour',
            'subtitle': 'How much of daily budget is consumed by each hour — MT',
            'formats': {
                'oep_cumulative_spend': fmt_dollar,
                'sep_cumulative_spend': fmt_dollar,
                'oep_pct_budget_used':  fmt_pct,
                'sep_pct_budget_used':  fmt_pct,
            },
            'widths': {'hour_label': 16},
        },
        '06_Off_Hours_Waste': {
            'title':    'Medicare — Off-Hours Spend (Hours 0–7 MT)',
            'subtitle': 'Spend before buyer active window — pre-8am Mountain Time',
            'formats': {
                'spend':  fmt_dollar,
                'cpa':    fmt_dollar,
                'clicks': fmt_int,
            },
            'widths': {'campaign_name': 50, 'hour_label': 16},
        },
        '06_Peak_vs_OffPeak': {
            'title':    'Medicare — Active Window vs Off-Hours Summary',
            'subtitle': 'Spend and conversion split: buyer active (8–21 MT) vs off-hours',
            'formats': {
                'avg_daily_spend':    fmt_dollar,
                'implied_cpa':        fmt_dollar,
                'pct_of_daily_spend': fmt_pct,
                'pct_of_daily_conv':  fmt_pct,
            },
            'widths': {'period': 35, 'window': 28},
        },
    }

    for sheet_name, df in sheets.items():
        cfg = sheet_configs.get(sheet_name, {})
        ws  = wb.create_sheet(title=sheet_name)

        if df is None or df.empty:
            ws['A1'] = f'No data available — run backfill first: 03_sync_campaign_performance.py --hourly-days 90 --hourly-only'
            continue

        n_cols   = len(df.columns)
        data_row = write_title_row(
            ws,
            title        = cfg.get('title', sheet_name),
            subtitle     = cfg.get('subtitle', ''),
            period_label = period_label,
            n_cols       = n_cols,
        )

        write_dataframe_to_sheet(
            ws, df, data_row,
            col_widths     = cfg.get('widths'),
            number_formats = cfg.get('formats'),
        )

        # Apply color coding for buyer_window column in CPA sheet
        if sheet_name == '06_Hourly_CPA' and 'buyer_window' in df.columns:
            bw_col_idx = list(df.columns).index('buyer_window') + 1
            for i, val in enumerate(df['buyer_window']):
                r = data_row + 1 + i
                cell = ws.cell(row=r, column=bw_col_idx)
                if 'Off-Hours' in str(val):
                    cell.fill = XL.fill(XL.FLAG_RED)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                                     bold=True, color=XL.TEXT_WHITE)
                elif 'Marginal' in str(val):
                    cell.fill = XL.fill(XL.FLAG_AMBER)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                                     bold=True, color=XL.TEXT_WHITE)

        autofit_columns(ws)
        freeze_header_rows(ws, freeze_row=data_row + 1)
        ws.sheet_view.zoomScale = 90

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f'Saved: {output_path}')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Medicare Dayparting Analysis — hourly spend OEP vs SEP'
    )
    parser.add_argument('--out', metavar='PATH', help='Output .xlsx path')
    args = parser.parse_args()

    output_path = Path(args.out) if args.out else (
        OUTPUT_DIR / 'medicare_06_dayparting.xlsx'
    )

    print(f'OEP window : {OEP_START} → {OEP_END}')
    print(f'SEP window : {SEP_START} → {SEP_END}')
    print(f'Account    : Medicare ({MEDICARE_ACCOUNT_ID}) — MOBILE only')
    print(f'Time zone  : Mountain Time (account local)')
    print()

    sheets = build_sheets()
    for name, df in sheets.items():
        print(f'  {name:<35} {len(df):>6} rows')

    write_workbook(sheets, output_path)


if __name__ == '__main__':
    main()
