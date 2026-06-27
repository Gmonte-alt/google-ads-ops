"""
medicare_07_geo_trend.py
=========================
Medicare Geographic Performance Trend Analysis
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Data source: campaign_performance_geo_daily (all campaigns combined)

Five enrollment periods analyzed side by side:
  q1_2025      Jan 1  – Mar 31, 2025   (OEP 2025)
  off_peak     Apr 1  – Oct 14, 2025   (off-peak)
  aep_2025     Oct 15 – Dec 7,  2025   (AEP 2025)
  oep_2026     Jan 1  – Mar 31, 2026   (OEP 2026)
  sep_2026     Apr 1  – present        (SEP 2026)

Revenue shown two ways per period:
  Platform revenue  = conversion_value from Google Ads (what algorithm optimizes)
  Estimated revenue = conversions × period RPL from internal data (actual collected)

RPL values from Medicare_Search_Rolling_25_Months.csv (period averages):
  q1_2025:   $77.55  (Q1 2025 avg: Jan $87, Feb $76, Mar $70)
  off_peak:  $69.30  (Apr–Oct 2025 avg)
  aep_2025:  $79.52  (Oct 15–Dec 7 weighted avg)
  oep_2026:  $81.65  (Jan–Mar 2026 avg)
  sep_2026:  $85.00  (current Apr 2026 rate)

Produces sheets:
  07_State_All_Periods      All 5 periods per state: spend, conv, CPA, platform rev, est rev
  07_State_Monthly_Trend    Monthly state data formatted for Excel line charting
  07_Enrollment_Comp        AEP 2025 vs OEP 2026 head-to-head with delta columns
  07_State_Rank_Shift       State spend rank in each period — shows priority shifts
  07_Consistent_Performers  States with CPA below account avg in 3+ periods
  07_Drawdown_Diagnosis     States with declining spend: CPA trend + platform vs actual rev gap

Usage:
  python medicare_07_geo_trend.py
  python medicare_07_geo_trend.py --out custom_path.xlsx
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font

import sys
sys.path.insert(0, str(Path(__file__).parent))
from medicare_utils import (
    get_db_connection, MEDICARE_ACCOUNT_ID,
    write_title_row, write_dataframe_to_sheet,
    autofit_columns, freeze_header_rows, XL, safe_div
)

OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\analysis\output")

# ── Period definitions ────────────────────────────────────────────────────────
PERIODS = {
    'q1_2025':   ('2025-01-01', '2025-03-31', 'Q1 2025 (OEP)',      77.55),
    'off_peak':  ('2025-04-01', '2025-10-14', 'Off-Peak 2025',      69.30),
    'aep_2025':  ('2025-10-15', '2025-12-07', 'AEP 2025',           79.52),
    'oep_2026':  ('2026-01-01', '2026-03-31', 'OEP 2026',           81.65),
    'sep_2026':  ('2026-04-01', (date.today() - timedelta(days=1)).isoformat(),
                  'SEP 2026', 85.00),
}

PERIOD_KEYS   = list(PERIODS.keys())
PERIOD_LABELS = {k: v[2] for k, v in PERIODS.items()}
PERIOD_RPLS   = {k: v[3] for k, v in PERIODS.items()}


# ── Core geo query ────────────────────────────────────────────────────────────

def q_geo_period(conn, start: str, end: str) -> pd.DataFrame:
    """Pull state-level performance for a single period, all campaigns combined."""
    rows = conn.execute("""
        SELECT
            COALESCE(g.geo_state, 'Unknown')    AS state,
            g.geo_target_constant_id,
            SUM(g.cost_micros) / 1e6            AS spend,
            SUM(g.impressions)                  AS impressions,
            SUM(g.clicks)                       AS clicks,
            SUM(g.conversions)                  AS conversions,
            SUM(g.conversion_value)             AS platform_revenue,
            COUNT(DISTINCT g."date")            AS active_days,
            COUNT(DISTINCT g.campaign_id)       AS active_campaigns
        FROM campaign_performance_geo_daily g
        WHERE g.account_id = ?
          AND g."date" BETWEEN ? AND ?
          AND g.device = 'MOBILE'
        GROUP BY g.geo_state, g.geo_target_constant_id
        ORDER BY spend DESC
    """, (MEDICARE_ACCOUNT_ID, start, end)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df['cpa'] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)
    df['cvr'] = df.apply(lambda r: safe_div(r.conversions, r.clicks), axis=1)
    df['ctr'] = df.apply(lambda r: safe_div(r.clicks, r.impressions), axis=1)
    return df


def q_geo_monthly(conn) -> pd.DataFrame:
    """Monthly state performance across entire available date range."""
    rows = conn.execute("""
        SELECT
            COALESCE(g.geo_state, 'Unknown')    AS state,
            STRFTIME('%Y-%m', g."date")         AS month,
            SUM(g.cost_micros) / 1e6            AS spend,
            SUM(g.clicks)                       AS clicks,
            SUM(g.conversions)                  AS conversions,
            SUM(g.conversion_value)             AS platform_revenue
        FROM campaign_performance_geo_daily g
        WHERE g.account_id = ?
          AND g.device = 'MOBILE'
        GROUP BY g.geo_state, month
        ORDER BY state, month
    """, (MEDICARE_ACCOUNT_ID,)).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    df['cpa'] = df.apply(lambda r: safe_div(r.spend, r.conversions), axis=1)

    # Assign period and RPL to each month
    def get_period_rpl(month_str):
        if month_str <= '2025-03': return 'q1_2025',  PERIOD_RPLS['q1_2025']
        if month_str <= '2025-10': return 'off_peak', PERIOD_RPLS['off_peak']
        if month_str <= '2025-12': return 'aep_2025', PERIOD_RPLS['aep_2025']
        if month_str <= '2026-03': return 'oep_2026', PERIOD_RPLS['oep_2026']
        return 'sep_2026', PERIOD_RPLS['sep_2026']

    df[['period_key', 'rpl']] = df['month'].apply(
        lambda m: pd.Series(get_period_rpl(m))
    )
    df['period_label']   = df['period_key'].map(PERIOD_LABELS)
    df['est_revenue']    = df['conversions'] * df['rpl']
    df['est_gp']         = df['est_revenue'] - df['spend']
    df['est_gp_pct']     = df.apply(
        lambda r: safe_div(r.est_gp, r.est_revenue, 0), axis=1
    )
    df['platform_vs_est_delta'] = df['platform_revenue'] - df['est_revenue']

    return df


# ── Sheet builders ────────────────────────────────────────────────────────────

def build_state_all_periods(conn) -> pd.DataFrame:
    """
    Wide table: one row per state, columns for each period's
    spend, conversions, CPA, platform revenue, estimated revenue, est GP.
    """
    period_dfs = {}
    for key, (start, end, label, rpl) in PERIODS.items():
        df = q_geo_period(conn, start, end)
        if not df.empty:
            df['est_revenue'] = df['conversions'] * rpl
            df['est_gp']      = df['est_revenue'] - df['spend']
            df['platform_vs_est'] = df['platform_revenue'] - df['est_revenue']
            period_dfs[key] = df.set_index('state')

    if not period_dfs:
        return pd.DataFrame()

    # Collect all states
    all_states = set()
    for df in period_dfs.values():
        all_states.update(df.index.tolist())

    rows = []
    for state in sorted(all_states):
        # Total spend across all periods for sorting
        total_spend = sum(
            period_dfs[k].loc[state, 'spend']
            if state in period_dfs.get(k, pd.DataFrame()).index else 0
            for k in PERIOD_KEYS
        )
        row = {'state': state, 'total_spend_all_periods': total_spend}

        for key in PERIOD_KEYS:
            label = PERIOD_LABELS[key]
            prefix = key
            df = period_dfs.get(key, pd.DataFrame())
            if not df.empty and state in df.index:
                r = df.loc[state]
                row[f'{prefix}_spend']        = r.get('spend')
                row[f'{prefix}_conversions']  = r.get('conversions')
                row[f'{prefix}_cpa']          = r.get('cpa')
                row[f'{prefix}_platform_rev'] = r.get('platform_revenue')
                row[f'{prefix}_est_rev']      = r.get('est_revenue')
                row[f'{prefix}_est_gp']       = r.get('est_gp')
                row[f'{prefix}_plat_vs_est']  = r.get('platform_vs_est')
            else:
                for suffix in ['spend','conversions','cpa','platform_rev',
                                'est_rev','est_gp','plat_vs_est']:
                    row[f'{prefix}_{suffix}'] = None

        rows.append(row)

    df_wide = pd.DataFrame(rows).sort_values('total_spend_all_periods', ascending=False)
    return df_wide


def build_enrollment_comp(conn) -> pd.DataFrame:
    """AEP 2025 vs OEP 2026 head-to-head with delta columns."""
    aep_start, aep_end, _, aep_rpl = PERIODS['aep_2025']
    oep_start, oep_end, _, oep_rpl = PERIODS['oep_2026']

    aep = q_geo_period(conn, aep_start, aep_end)
    oep = q_geo_period(conn, oep_start, oep_end)

    if aep.empty and oep.empty:
        return pd.DataFrame()

    aep['est_revenue'] = aep['conversions'] * aep_rpl
    oep['est_revenue'] = oep['conversions'] * oep_rpl

    merged = pd.merge(
        aep[['state','spend','conversions','cpa','platform_revenue','est_revenue']].add_prefix('aep_').rename(columns={'aep_state':'state'}),
        oep[['state','spend','conversions','cpa','platform_revenue','est_revenue']].add_prefix('oep_').rename(columns={'oep_state':'state'}),
        on='state', how='outer'
    )

    merged['spend_delta']     = merged['oep_spend']       - merged['aep_spend']
    merged['conv_delta']      = merged['oep_conversions']  - merged['aep_conversions']
    merged['cpa_delta']       = merged['oep_cpa']          - merged['aep_cpa']
    merged['est_rev_delta']   = merged['oep_est_revenue']  - merged['aep_est_revenue']
    merged['spend_delta_pct'] = merged.apply(
        lambda r: safe_div(r.spend_delta, r.aep_spend), axis=1
    )

    merged['trend'] = merged['spend_delta'].apply(
        lambda x: 'Growing' if x and x > 0
        else 'Declining' if x and x < 0
        else 'Stable'
    )

    return merged.sort_values('aep_spend', ascending=False, na_position='last')


def build_state_rank_shift(conn) -> pd.DataFrame:
    """State spend rank in each period — shows which states rose/fell."""
    rows = []
    for key, (start, end, label, rpl) in PERIODS.items():
        df = q_geo_period(conn, start, end)
        if df.empty:
            continue
        df = df[df['spend'] > 0].copy()
        df['rank']  = df['spend'].rank(ascending=False, method='min').astype(int)
        df['period'] = label
        df['period_key'] = key
        rows.append(df[['state','period','period_key','rank','spend','conversions','cpa']])

    if not rows:
        return pd.DataFrame()

    long_df = pd.concat(rows, ignore_index=True)

    # Pivot to wide: state × period ranks
    pivot = long_df.pivot_table(
        index='state',
        columns='period_key',
        values='rank',
        aggfunc='first'
    ).reset_index()

    # Reorder columns to period sequence
    col_order = ['state'] + [k for k in PERIOD_KEYS if k in pivot.columns]
    pivot = pivot[[c for c in col_order if c in pivot.columns]]
    pivot.columns = ['state'] + [PERIOD_LABELS[k] for k in PERIOD_KEYS if k in pivot.columns]

    # Add total spend for sorting
    total_spend = long_df.groupby('state')['spend'].sum().reset_index()
    total_spend.columns = ['state', 'total_spend']
    pivot = pivot.merge(total_spend, on='state').sort_values('total_spend', ascending=False)

    return pivot


def build_consistent_performers(conn) -> pd.DataFrame:
    """States with below-average CPA in 3+ periods — reliable markets."""
    period_data = {}
    acct_cpas   = {}

    for key, (start, end, label, rpl) in PERIODS.items():
        df = q_geo_period(conn, start, end)
        if df.empty:
            continue
        df['est_revenue'] = df['conversions'] * rpl
        acct_spend = df['spend'].sum()
        acct_conv  = df['conversions'].sum()
        acct_cpas[key] = safe_div(acct_spend, acct_conv)
        period_data[key] = df

    if not period_data:
        return pd.DataFrame()

    all_states = set()
    for df in period_data.values():
        all_states.update(df['state'].tolist())

    rows = []
    for state in sorted(all_states):
        below_avg_count = 0
        row = {'state': state}
        total_spend = total_conv = total_est_rev = 0

        for key in PERIOD_KEYS:
            df = period_data.get(key)
            acct_cpa = acct_cpas.get(key)
            if df is None:
                row[f'{key}_cpa'] = None
                row[f'{key}_vs_avg'] = None
                continue
            match = df[df['state'] == state]
            if match.empty:
                row[f'{key}_cpa'] = None
                row[f'{key}_vs_avg'] = None
                continue
            cpa = match['cpa'].iloc[0]
            est_rev = match['est_revenue'].iloc[0] if 'est_revenue' in match.columns else None
            row[f'{key}_cpa']     = cpa
            row[f'{key}_vs_avg']  = safe_div((cpa - acct_cpa), acct_cpa) if cpa and acct_cpa else None
            if cpa and acct_cpa and cpa < acct_cpa:
                below_avg_count += 1
            total_spend += match['spend'].iloc[0] or 0
            total_conv  += match['conversions'].iloc[0] or 0
            if est_rev: total_est_rev += est_rev

        row['periods_below_avg'] = below_avg_count
        row['total_spend']       = total_spend
        row['total_conversions'] = total_conv
        row['total_est_revenue'] = total_est_rev
        row['blended_cpa']       = safe_div(total_spend, total_conv)
        rows.append(row)

    df_out = pd.DataFrame(rows)
    df_out = df_out[df_out['periods_below_avg'] >= 3].sort_values(
        'total_spend', ascending=False
    )
    return df_out


def build_drawdown_diagnosis(conn) -> pd.DataFrame:
    """
    States with declining spend across periods.
    For each declining state surfaces:
      - Spend by period with delta
      - CPA trend (rising = algo pulling back; falling = budget constrained)
      - Platform revenue vs estimated actual revenue gap
      - Whether state had dedicated campaigns in earlier periods

    Drawdown is defined as: spend in most recent period < 70% of peak period spend.
    """
    period_data = {}
    for key, (start, end, label, rpl) in PERIODS.items():
        df = q_geo_period(conn, start, end)
        if not df.empty:
            df['est_revenue'] = df['conversions'] * rpl
            df['est_gp']      = df['est_revenue'] - df['spend']
            df['platform_vs_est'] = df['platform_revenue'] - df['est_revenue']
            period_data[key]  = df

    if len(period_data) < 2:
        return pd.DataFrame()

    # Get most recent and earliest available period
    available = [k for k in PERIOD_KEYS if k in period_data]
    if len(available) < 2:
        return pd.DataFrame()

    most_recent_key = available[-1]
    most_recent     = period_data[most_recent_key].set_index('state')

    all_states = set()
    for df in period_data.values():
        all_states.update(df['state'].tolist())

    rows = []
    for state in sorted(all_states):
        # Find peak spend period
        peak_spend = 0
        peak_period = None
        for key in available:
            df = period_data[key]
            match = df[df['state'] == state]
            if not match.empty:
                s = match['spend'].iloc[0] or 0
                if s > peak_spend:
                    peak_spend  = s
                    peak_period = key

        if peak_period is None:
            continue

        # Current spend
        recent_match = most_recent.loc[state] if state in most_recent.index else None
        recent_spend = (recent_match['spend'] if recent_match is not None else 0) or 0

        # Only include if declining (recent < 70% of peak)
        if peak_spend == 0:
            continue
        if recent_spend >= peak_spend * 0.70 and most_recent_key != peak_period:
            continue
        if most_recent_key == peak_period:
            continue  # currently at peak, not declining

        # Build row
        row = {
            'state':              state,
            'peak_period':        PERIOD_LABELS.get(peak_period, peak_period),
            'peak_spend':         peak_spend,
            'recent_period':      PERIOD_LABELS.get(most_recent_key, most_recent_key),
            'recent_spend':       recent_spend,
            'spend_drawdown_pct': safe_div(recent_spend - peak_spend, peak_spend, 0),
        }

        # CPA trend: is CPA rising (algo pulling back) or falling (budget limited)?
        cpas = {}
        for key in available:
            df = period_data[key]
            match = df[df['state'] == state]
            if not match.empty:
                cpas[key] = match['cpa'].iloc[0]

        if len(cpas) >= 2:
            first_cpa  = cpas.get(available[0])
            recent_cpa = cpas.get(most_recent_key)
            row['first_period_cpa']  = first_cpa
            row['recent_cpa']        = recent_cpa
            if first_cpa and recent_cpa:
                row['cpa_trend_pct'] = safe_div(recent_cpa - first_cpa, first_cpa)
                row['cpa_trend_diagnosis'] = (
                    'CPA rising — algo pulling back (tROAS insufficient)'
                    if recent_cpa > first_cpa * 1.10
                    else 'CPA falling — budget/volume constrained'
                    if recent_cpa < first_cpa * 0.90
                    else 'CPA stable — other factor driving drawdown'
                )
            else:
                row['cpa_trend_pct']       = None
                row['cpa_trend_diagnosis'] = 'Insufficient data'
        else:
            row['first_period_cpa']    = None
            row['recent_cpa']          = None
            row['cpa_trend_pct']       = None
            row['cpa_trend_diagnosis'] = 'Single period'

        # Platform vs estimated revenue gap in most recent period
        if recent_match is not None:
            row['recent_platform_rev'] = recent_match.get('platform_revenue')
            row['recent_est_rev']      = recent_match.get('est_revenue')
            row['plat_vs_est_gap']     = recent_match.get('platform_vs_est')
        else:
            row['recent_platform_rev'] = None
            row['recent_est_rev']      = None
            row['plat_vs_est_gap']     = None

        # Per-period spend for context
        for key in available:
            df = period_data[key]
            match = df[df['state'] == state]
            row[f'{key}_spend'] = match['spend'].iloc[0] if not match.empty else None
            row[f'{key}_cpa']   = match['cpa'].iloc[0]   if not match.empty else None

        rows.append(row)

    df_out = pd.DataFrame(rows)
    if df_out.empty:
        return df_out

    df_out = df_out.sort_values('peak_spend', ascending=False)
    return df_out


# ── Build all sheets ──────────────────────────────────────────────────────────

def build_sheets() -> dict[str, pd.DataFrame]:
    conn = get_db_connection()

    sheets = {
        '07_State_All_Periods':     build_state_all_periods(conn),
        '07_State_Monthly_Trend':   q_geo_monthly(conn),
        '07_Enrollment_Comp':       build_enrollment_comp(conn),
        '07_State_Rank_Shift':      build_state_rank_shift(conn),
        '07_Consistent_Performers': build_consistent_performers(conn),
        '07_Drawdown_Diagnosis':    build_drawdown_diagnosis(conn),
    }
    conn.close()
    return sheets


# ── Excel writer ──────────────────────────────────────────────────────────────

def write_workbook(sheets: dict[str, pd.DataFrame], output_path: Path) -> None:
    wb  = Workbook()
    wb.remove(wb.active)

    period_label = '  |  '.join(
        f"{v[2]}: {v[0]} → {v[1]}" for v in PERIODS.values()
    )

    dollar_cols = {
        'spend', 'cpa', 'est_revenue', 'platform_revenue', 'est_gp',
        'platform_vs_est', 'plat_vs_est_gap', 'recent_spend', 'peak_spend',
        'recent_platform_rev', 'recent_est_rev', 'total_spend', 'total_est_revenue',
        'blended_cpa', 'first_period_cpa', 'recent_cpa', 'avg_daily_spend',
        'implied_cpa', 'est_rev_delta', 'spend_delta',
        'aep_spend', 'oep_spend', 'aep_cpa', 'oep_cpa',
        'aep_platform_revenue', 'oep_platform_revenue',
        'aep_est_revenue', 'oep_est_revenue',
    }
    for key in PERIOD_KEYS:
        for suffix in ['spend','cpa','platform_rev','est_rev','est_gp','plat_vs_est']:
            dollar_cols.add(f'{key}_{suffix}')

    pct_cols = {
        'cvr', 'ctr', 'pct_daily_spend', 'pct_daily_conv',
        'cpa_trend_pct', 'spend_delta_pct', 'spend_drawdown_pct',
        'est_gp_pct', 'platform_vs_est_delta',
    }
    for key in PERIOD_KEYS:
        pct_cols.add(f'{key}_vs_avg')

    sheet_configs = {
        '07_State_All_Periods': {
            'title':    'Medicare — State Performance: All Five Periods',
            'subtitle': 'Spend, conversions, CPA, platform revenue vs estimated actual revenue by state',
            'widths':   {'state': 25},
        },
        '07_State_Monthly_Trend': {
            'title':    'Medicare — Monthly State Trend (Chart-Ready)',
            'subtitle': 'One row per state × month — select a state and insert line chart in Excel',
            'widths':   {'state': 25, 'month': 10, 'period_label': 20},
        },
        '07_Enrollment_Comp': {
            'title':    'Medicare — AEP 2025 vs OEP 2026 Comparison',
            'subtitle': 'Head-to-head by state with spend delta and trend classification',
            'widths':   {'state': 25, 'trend': 12},
        },
        '07_State_Rank_Shift': {
            'title':    'Medicare — State Spend Rank by Period',
            'subtitle': 'Rank 1 = highest spend. Track which states rose/fell in priority.',
            'widths':   {'state': 25},
        },
        '07_Consistent_Performers': {
            'title':    'Medicare — Consistent Performers (Below-Avg CPA in 3+ Periods)',
            'subtitle': 'Reliable markets — strong candidates for budget priority',
            'widths':   {'state': 25},
        },
        '07_Drawdown_Diagnosis': {
            'title':    'Medicare — Geo Drawdown Diagnosis',
            'subtitle': (
                'States with spend < 70% of peak period. '
                'CPA trend diagnoses whether algo is pulling back or supply is constrained.'
            ),
            'widths':   {
                'state': 25, 'cpa_trend_diagnosis': 45,
                'peak_period': 20, 'recent_period': 20,
            },
        },
    }

    for sheet_name, df in sheets.items():
        cfg = sheet_configs.get(sheet_name, {})
        ws  = wb.create_sheet(title=sheet_name)

        if df is None or df.empty:
            ws['A1'] = (
                'No data — run geo backfill first: '
                'python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-only'
            )
            continue

        # Build number format map from column names
        number_formats = {}
        for col in df.columns:
            col_lower = col.lower()
            if any(dc in col_lower for dc in ['spend','cpa','revenue','gp','est_rev',
                                               'plat_vs','implied','blended','delta_amt']):
                number_formats[col] = XL.FMT_DOLLAR
            elif any(pc in col_lower for pc in ['pct','cvr','ctr','vs_avg','drawdown',
                                                 'trend_pct','delta_pct','gp_pct']):
                number_formats[col] = XL.FMT_PCT_1
            elif col_lower in ['conversions','clicks','impressions','total_conversions',
                                'active_days','active_campaigns','days','periods_below_avg']:
                number_formats[col] = XL.FMT_INT

        n_cols   = len(df.columns)
        data_row = write_title_row(
            ws,
            title        = cfg.get('title', sheet_name),
            subtitle     = cfg.get('subtitle', ''),
            period_label = period_label[:120],   # truncate for cell
            n_cols       = n_cols,
        )

        write_dataframe_to_sheet(
            ws, df, data_row,
            col_widths     = cfg.get('widths'),
            number_formats = number_formats,
            flag_col       = cfg.get('flag_col'),
            flag_rules     = cfg.get('flag_rules'),
        )

        # Color-code trend column in enrollment comp
        if sheet_name == '07_Enrollment_Comp' and 'trend' in df.columns:
            trend_col_idx = list(df.columns).index('trend') + 1
            for i, val in enumerate(df['trend']):
                r = data_row + 1 + i
                cell = ws.cell(row=r, column=trend_col_idx)
                if val == 'Growing':
                    cell.fill = XL.fill(XL.FLAG_GREEN)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                                     bold=True, color=XL.TEXT_WHITE)
                elif val == 'Declining':
                    cell.fill = XL.fill(XL.FLAG_RED)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                                     bold=True, color=XL.TEXT_WHITE)

        # Color-code diagnosis column in drawdown sheet
        if sheet_name == '07_Drawdown_Diagnosis' and 'cpa_trend_diagnosis' in df.columns:
            diag_col_idx = list(df.columns).index('cpa_trend_diagnosis') + 1
            for i, val in enumerate(df['cpa_trend_diagnosis']):
                r = data_row + 1 + i
                cell = ws.cell(row=r, column=diag_col_idx)
                val_str = str(val)
                if 'pulling back' in val_str:
                    cell.fill = XL.fill(XL.FLAG_RED)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                                     bold=True, color=XL.TEXT_WHITE)
                elif 'constrained' in val_str:
                    cell.fill = XL.fill(XL.FLAG_AMBER)
                    cell.font = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA, bold=True)

        autofit_columns(ws)
        freeze_header_rows(ws, freeze_row=data_row + 1)
        ws.sheet_view.zoomScale = 90

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f'Saved: {output_path}')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Medicare Geographic Trend Analysis — all five periods'
    )
    parser.add_argument('--out', metavar='PATH', help='Output .xlsx path')
    args = parser.parse_args()

    output_path = Path(args.out) if args.out else (
        OUTPUT_DIR / 'medicare_07_geo_trend.xlsx'
    )

    print('Periods:')
    for key, (start, end, label, rpl) in PERIODS.items():
        print(f'  {label:<22} {start} → {end}  RPL=${rpl:.2f}')
    print(f'\nAccount: Medicare ({MEDICARE_ACCOUNT_ID}) — MOBILE only, all campaigns combined')
    print()

    sheets = build_sheets()
    for name, df in sheets.items():
        print(f'  {name:<35} {len(df):>6} rows')

    write_workbook(sheets, output_path)


if __name__ == '__main__':
    main()
