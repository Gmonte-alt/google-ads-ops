"""
Campaign Performance Data Tables — Microsoft Home Insurance
═══════════════════════════════════════════════════════════════════════
Produces a data-tables workbook with per-network breakouts (total /
search / audience) across 13 metrics:

  - Impressions, Clicks, CTR  (volume + engagement)
  - Cost  (spend)
  - Primary Conversions, Conversion Rate (CvR), Revenue  (outcome)
  - CPC, CPL, RPL, ROAS  (derived efficiency)
  - GP, Profit Margin  (profitability)
  - Audience share of cost  (network mix, single-column metric)

Window: 12 complete Mon-Sun weeks ending the most recent Sunday on or
before MAX(date), plus all days available in the current partial week.

SHEETS:
  - Index: navigation map + run context.
  - Daily_<code>: 42 columns (date, weekday, then per-network breakouts
    of 13 metrics, plus audience-share-of-cost as a single column).
    Total columns and derived metrics (CTR, CvR, CPC, CPL, RPL, ROAS,
    GP, margin, audience-share) are Excel formulas referencing the
    raw stored columns. Search columns tinted blue, audience columns
    tinted amber, current-week rows amber.
  - DOW_<code>: one consolidated sheet per campaign containing all 40
    grids in three sections:
      A. Volume:    Impressions, Clicks, CTR, Conversions, Revenue,
                    Cost   (6 metrics × 3 networks = 18 grids)
      B. Efficiency: CPC, CPL, CvR, ROAS  (4 × 3 = 12 grids) +
                     Audience share of cost (1 grid)  = 13 grids
      C. Revenue:   RPL, GP, Margin  (3 × 3 = 9 grids)
    Each grid: rows = ISO weeks, columns = weekdays + Total. Each grid
    has three bottom rows: avg/weighted, week-over-week %, and totals.
    A navigation table at row 3 lists section anchor rows.
  - DOW_Account: same 40-grid layout for the account-level rollup.

Each grid carries:
  - 'Total' column after Sunday — weekly sum (count metrics) or weighted
    weekly ratio (CPC/CPL/CvR/RPL/ROAS/margin/CTR/audshare). The Total
    column lets you read week-totals at a glance instead of mentally
    summing Mon-Sun.
  - 'avg / weighted' row — weighted across all weeks for each weekday,
    with the Total column showing the all-window weighted aggregate.
  - 'WoW %' row — most recent complete week vs prior complete week,
    per weekday and for the weekly Total.

DATA SOURCES & RECONCILIATION:
  - Spend, impressions, clicks, conversions, revenue (search) come from
    campaign_performance_daily.
  - Same metrics (audience) come from campaign_performance_network_daily.
  - Both tables use the pixel-as-primary convention: .conversions is the
    pixel-only count and .conversion_value is offline-inclusive revenue.
  - All queries filter device='ALL' (the synthesized campaign-level
    rollup row). Without this filter, sums double-count due to per-device
    rows being present alongside the ALL rollup.
  - Architect-confirmed reconciliation: camp_spend (search + network)
    matches keyword_spend within 0.001% across the account historically.

CAMPAIGN INCLUSION:
  - ENABLED campaigns: included unconditionally.
  - PAUSED campaigns: included only if they have >=1 impression in the
    last 14 days.

USAGE:
  python campaign_performance_data_microsoft.py
  python campaign_performance_data_microsoft.py --end-date 2026-05-10
  python campaign_performance_data_microsoft.py --weeks 16

OUTPUT: campaign_performance_data_microsoft_<YYYY-MM-DD>.xlsx
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── CONFIG ─────────────────────────────────────────────────────────
ACCOUNT_ID = 2
ACCOUNT_LABEL = "microsoft_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
DEFAULT_WEEKS = 12

HEADER_FILL = "305496"
HEADER_FONT_COLOR = "FFFFFF"
ACCOUNT_FILL = "E7E6E6"      # light grey background for account-level rows
CURRENT_WEEK_FILL = "FFF2CC"  # amber tint for partial/current week
# Network color tints (subtle, matching Network Split convention)
SEARCH_TINT = "EAF2F9"        # light blue
AUDIENCE_TINT = "FFF8E8"      # light amber
TOTAL_TINT = None             # no tint, white
THIN_BORDER = Side(border_style="thin", color="BFBFBF")


def banner(text: str) -> None:
    print("═" * 70)
    print(text)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# DATE RESOLUTION
# ═══════════════════════════════════════════════════════════════════

def resolve_window(conn, cli_end_date: str | None, n_weeks: int):
    """Resolve the analysis window.

    Returns (window_start_mon, last_complete_sun, max_data_date, source_note).

    - window_start_mon: Monday of the earliest complete week to display
    - last_complete_sun: Sunday of the most recent COMPLETE week
    - max_data_date: MAX(date) in campaign_performance_daily; the current
      partial week runs from (last_complete_sun + 1d) through max_data_date

    If cli_end_date is provided, treat it as the desired "last_complete_sun"
    (or the most recent Sunday on or before it if it's mid-week), and ignore
    any further data in the DB.

    No backfill buffer is applied — impressions/clicks/cost finalize within
    hours. The workbook surfaces the partial week as a visually distinct
    "current" region.
    """
    cur = conn.execute(
        "SELECT MAX(date) FROM campaign_performance_daily "
        "WHERE account_id = ? AND device = 'ALL'",
        (ACCOUNT_ID,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"No data in campaign_performance_daily for account {ACCOUNT_ID}")
    max_data_date = date.fromisoformat(row[0])

    if cli_end_date:
        anchor = date.fromisoformat(cli_end_date)
        source_note = f"CLI override (--end-date {cli_end_date})"
        # Anchor's most recent Sunday on or before
        days_past_sun = (anchor.weekday() + 1) % 7
        last_complete_sun = anchor - timedelta(days=days_past_sun)
        # In CLI-override mode, cap max_data_date at the anchor too so we
        # don't show data past what the user asked for.
        max_data_date = min(max_data_date, anchor)
    else:
        # Most recent Sunday strictly before max_data_date (or equal if
        # max_data_date itself is Sunday and we want to include it as a
        # complete week)
        if max_data_date.weekday() == 6:  # Sunday
            last_complete_sun = max_data_date
        else:
            days_past_sun = (max_data_date.weekday() + 1) % 7
            last_complete_sun = max_data_date - timedelta(days=days_past_sun)
        source_note = f"auto-detect: MAX(date)={max_data_date}"

    # Window start: Monday of the week n_weeks before last_complete_sun's week
    # last_complete_sun's Monday:
    week_monday = last_complete_sun - timedelta(days=6)
    window_start_mon = week_monday - timedelta(weeks=n_weeks - 1)

    return window_start_mon, last_complete_sun, max_data_date, source_note


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def fetch_campaigns(conn) -> pd.DataFrame:
    """Enabled + Paused campaigns. Sorted by name for stable ordering."""
    sql = """
    SELECT
        campaign_id,
        campaign_name,
        status,
        target_cpa_micros / 1.0e6 AS current_tcpa
    FROM campaigns
    WHERE account_id = ?
      AND status IN ('ENABLED', 'PAUSED')
    ORDER BY status, campaign_name
    """
    return pd.read_sql(sql, conn, params=(ACCOUNT_ID,))


def fetch_daily_metrics(conn, start_date: date, end_date: date) -> pd.DataFrame:
    """Per (campaign_id, date) raw metrics, by network.

    Returns 5 stored metrics × 2 networks = 10 numeric columns:
        imp_search, imp_audience
        clicks_search, clicks_audience
        cost_search, cost_audience
        conv_search, conv_audience          (pixel-only count, May 2026 convention)
        rev_search, rev_audience            (offline-inclusive revenue)

    The Daily sheet writer derives totals (search + audience) and the
    five derived metrics (CPC, CPL, RPL, ROAS, GP) as Excel formulas.

    Schema notes (architect-confirmed):
      - campaign_performance_daily and campaign_performance_network_daily
        both have account_id directly, so we filter on it without joining
        the campaigns table.
      - Both tables contain per-device rows (DESKTOP/MOBILE/TABLET) AND a
        synthesized device='ALL' rollup row per (campaign, date). To get
        the campaign-level total we MUST filter `device = 'ALL'` —
        otherwise aggregations double-count (ALL + per-device).
      - The architect's canonical reconciliation script
        (refresh_check.py) follows this convention for every query.
      - Under the pixel-as-primary convention (May 2026), .conversions is
        the pixel-only count and .conversion_value is offline-inclusive
        revenue (~$300K+ recently for this account).
      - The two tables ARE genuinely network-scoped: cpd is search-only
        (AOL + Microsoft Sites), cpnd is audience-only. Their sums
        reconcile to keyword_performance_daily within ~0.001%.
    """
    sql_search = """
    SELECT
        campaign_id AS campaign_id,
        date AS date,
        SUM(impressions) AS imp_search,
        SUM(clicks) AS clicks_search,
        SUM(cost_micros) / 1.0e6 AS cost_search,
        SUM(conversions) AS conv_search,
        SUM(conversion_value) AS rev_search
    FROM campaign_performance_daily
    WHERE account_id = ?
      AND device = 'ALL'
      AND date BETWEEN ? AND ?
    GROUP BY campaign_id, date
    """
    df_search = pd.read_sql(sql_search, conn, params=(
        ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()
    ))

    sql_audience = """
    SELECT
        campaign_id AS campaign_id,
        date AS date,
        SUM(impressions) AS imp_audience,
        SUM(clicks) AS clicks_audience,
        SUM(cost_micros) / 1.0e6 AS cost_audience,
        SUM(conversions) AS conv_audience,
        SUM(conversion_value) AS rev_audience
    FROM campaign_performance_network_daily
    WHERE account_id = ?
      AND device = 'ALL'
      AND date BETWEEN ? AND ?
    GROUP BY campaign_id, date
    """
    df_audience = pd.read_sql(sql_audience, conn, params=(
        ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()
    ))

    df = df_search.merge(df_audience, on=["campaign_id", "date"], how="outer").fillna(0.0)
    df["imp_search"] = df["imp_search"].astype(int)
    df["imp_audience"] = df["imp_audience"].astype(int)
    df["clicks_search"] = df["clicks_search"].astype(int)
    df["clicks_audience"] = df["clicks_audience"].astype(int)
    return df[[
        "campaign_id", "date",
        "imp_search", "imp_audience",
        "clicks_search", "clicks_audience",
        "cost_search", "cost_audience",
        "conv_search", "conv_audience",
        "rev_search", "rev_audience",
    ]]


# ═══════════════════════════════════════════════════════════════════
# SHORT-CODE GENERATION
# ═══════════════════════════════════════════════════════════════════

def short_code(campaign_name: str) -> str:
    """Extract the state abbreviation or short identifier from a campaign name.

    'HOME INSURANCE-CALIFORNIA-SEARCH-MOBILE-USA-STE' -> 'CA'
    'HOME INSURANCE-NORTH CAROLINA-SEARCH-MOBILE-USA-STE' -> 'NC'
    Falls back to first 8 chars sanitized if state name not recognized.
    """
    STATE_ABBR = {
        "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
        "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
        "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
        "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
        "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
        "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
        "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
        "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
        "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
        "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
        "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
        "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
        "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
        "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    }
    name_up = campaign_name.upper()
    for state, abbr in STATE_ABBR.items():
        if state in name_up:
            return abbr
    # Fallback: cleaned first 8 chars of the campaign name
    cleaned = "".join(c for c in name_up if c.isalnum())[:8]
    return cleaned or "CAMP"


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — DAILY
# ═══════════════════════════════════════════════════════════════════

def write_daily_sheet(ws, daily_df: pd.DataFrame,
                      start_date: date, end_date: date,
                      max_data_date: date, last_complete_sun: date,
                      label: str, is_account: bool):
    """Write a Daily_<code> sheet with per-network breakouts across 13 metrics.

    daily_df expected columns:
        date, imp_search, imp_audience, clicks_search, clicks_audience,
        cost_search, cost_audience, conv_search, conv_audience,
        rev_search, rev_audience

    Layout (42 columns: A..AP):
        A   date
        B   weekday
        C/D/E    imp_total/search/audience       (C = D+E)
        F/G/H    clicks_total/search/audience    (F = G+H)
        I/J/K    cost_total/search/audience      (I = J+K)
        L/M/N    conv_total/search/audience      (L = M+N)
        O/P/Q    rev_total/search/audience       (O = P+Q)
        R/S/T    cpc_total/search/audience       (= cost/clicks per network)
        U/V/W    cpl_total/search/audience       (= cost/conv per network)
        X/Y/Z    rpl_total/search/audience       (= rev/conv per network)
        AA/AB/AC roas_total/search/audience      (= rev/cost per network)
        AD/AE/AF gp_total/search/audience        (= rev - cost per network)
        AG/AH/AI ctr_total/search/audience       (= clicks/imp per network)
        AJ/AK/AL cvr_total/search/audience       (= conv/clicks per network)
        AM/AN/AO margin_total/search/audience    (= gp/rev per network)
        AP       audience_share_cost             (= cost_audience / cost_total)

    All total columns and derived metrics are Excel formulas. Search
    columns get a blue tint; audience columns amber. Current-week rows
    amber (overrides tints). The four new metric groups (CTR/CvR/margin/
    audience-share) are appended at the end rather than inserted between
    existing groups so that downstream DOW formulas that reference fixed
    column letters keep working unchanged.
    """
    title_font = Font(bold=True, size=12, color="305496", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    current_fill = PatternFill("solid", fgColor=CURRENT_WEEK_FILL)
    search_fill = PatternFill("solid", fgColor=SEARCH_TINT)
    audience_fill = PatternFill("solid", fgColor=AUDIENCE_TINT)

    ws.cell(row=1, column=1, value=f"Daily metrics: {label}").font = title_font
    ws.cell(row=2, column=1,
            value=(f"Window: {start_date} (Mon) to {max_data_date}. "
                   f"Last complete week ends {last_complete_sun} (Sun). "
                   f"Rows after that date are CURRENT (partial) week — "
                   f"highlighted amber. Search columns tinted blue; "
                   f"audience columns tinted amber. Total = search + "
                   f"audience. Derived metrics (CPC, CPL, RPL, ROAS, "
                   f"GP, CTR, CvR, margin, audience-share-of-cost) "
                   f"are Excel formulas, not stored.")).font = note_font

    # Column layout (42 columns: A..AP)
    headers = [
        "date", "weekday",
        "imp_total", "imp_search", "imp_audience",
        "clicks_total", "clicks_search", "clicks_audience",
        "cost_total", "cost_search", "cost_audience",
        "conv_total", "conv_search", "conv_audience",
        "rev_total", "rev_search", "rev_audience",
        "cpc_total", "cpc_search", "cpc_audience",
        "cpl_total", "cpl_search", "cpl_audience",
        "rpl_total", "rpl_search", "rpl_audience",
        "roas_total", "roas_search", "roas_audience",
        "gp_total", "gp_search", "gp_audience",
        "ctr_total", "ctr_search", "ctr_audience",
        "cvr_total", "cvr_search", "cvr_audience",
        "margin_total", "margin_search", "margin_audience",
        "audience_share_cost",
    ]
    # Which columns get search vs audience tints. Total columns get no tint.
    # Each metric is a triplet of cols (total, search, audience):
    #   imp:    C(3), D(4), E(5)
    #   clicks: F(6), G(7), H(8)
    #   cost:   I(9), J(10), K(11)
    #   conv:   L(12), M(13), N(14)
    #   rev:    O(15), P(16), Q(17)
    #   cpc:    R(18), S(19), T(20)
    #   cpl:    U(21), V(22), W(23)
    #   rpl:    X(24), Y(25), Z(26)
    #   roas:   AA(27), AB(28), AC(29)
    #   gp:     AD(30), AE(31), AF(32)
    #   ctr:    AG(33), AH(34), AI(35)
    #   cvr:    AJ(36), AK(37), AL(38)
    #   margin: AM(39), AN(40), AO(41)
    #   audience_share_cost: AP(42) — single column, no triplet
    metric_search_cols = [4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40]
    metric_audience_cols = [5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35, 38, 41]
    # AP (audience_share_cost) gets the audience tint since it's an
    # audience-flavored measurement, not a total/search/audience triplet.
    audience_share_col = 42

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Lookup map: date -> tuple of 10 raw values
    date_map = {}
    for _, r in daily_df.iterrows():
        if isinstance(r["date"], str):
            d = date.fromisoformat(r["date"])
        else:
            d = r["date"]
        date_map[d] = (
            int(r["imp_search"]),
            int(r["imp_audience"]),
            int(r["clicks_search"]),
            int(r["clicks_audience"]),
            float(r["cost_search"]),
            float(r["cost_audience"]),
            float(r["conv_search"]),
            float(r["conv_audience"]),
            float(r["rev_search"]),
            float(r["rev_audience"]),
        )

    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    int_fmt = "#,##0"
    money_fmt = '"$"#,##0.00'
    conv_fmt = "#,##0.0"
    roas_fmt = "0.0000"
    pct_fmt = "0.00%"

    row_idx = 5
    cur_date = start_date
    while cur_date <= end_date:
        (imp_s, imp_a, clk_s, clk_a, cost_s, cost_a,
         conv_s, conv_a, rev_s, rev_a) = date_map.get(
            cur_date, (0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        )
        is_current_week = cur_date > last_complete_sun
        r = row_idx

        # A/B: date, weekday
        ws.cell(row=r, column=1, value=cur_date).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=2, value=WEEKDAY_NAMES[cur_date.weekday()])

        # Stored raw values + total formulas for impressions, clicks, cost, conv, rev
        # C(total=D+E), D(search), E(audience)
        ws.cell(row=r, column=3, value=f"=D{r}+E{r}").number_format = int_fmt
        ws.cell(row=r, column=4, value=imp_s).number_format = int_fmt
        ws.cell(row=r, column=5, value=imp_a).number_format = int_fmt
        # F/G/H clicks
        ws.cell(row=r, column=6, value=f"=G{r}+H{r}").number_format = int_fmt
        ws.cell(row=r, column=7, value=clk_s).number_format = int_fmt
        ws.cell(row=r, column=8, value=clk_a).number_format = int_fmt
        # I/J/K cost
        ws.cell(row=r, column=9, value=f"=J{r}+K{r}").number_format = money_fmt
        ws.cell(row=r, column=10, value=round(cost_s, 2)).number_format = money_fmt
        ws.cell(row=r, column=11, value=round(cost_a, 2)).number_format = money_fmt
        # L/M/N conv
        ws.cell(row=r, column=12, value=f"=M{r}+N{r}").number_format = conv_fmt
        ws.cell(row=r, column=13, value=round(conv_s, 2)).number_format = conv_fmt
        ws.cell(row=r, column=14, value=round(conv_a, 2)).number_format = conv_fmt
        # O/P/Q rev
        ws.cell(row=r, column=15, value=f"=P{r}+Q{r}").number_format = money_fmt
        ws.cell(row=r, column=16, value=round(rev_s, 2)).number_format = money_fmt
        ws.cell(row=r, column=17, value=round(rev_a, 2)).number_format = money_fmt
        # R/S/T cpc = cost / clicks
        ws.cell(row=r, column=18, value=f"=IF(F{r}=0,0,I{r}/F{r})").number_format = money_fmt
        ws.cell(row=r, column=19, value=f"=IF(G{r}=0,0,J{r}/G{r})").number_format = money_fmt
        ws.cell(row=r, column=20, value=f"=IF(H{r}=0,0,K{r}/H{r})").number_format = money_fmt
        # U/V/W cpl = cost / conv
        ws.cell(row=r, column=21, value=f"=IF(L{r}=0,0,I{r}/L{r})").number_format = money_fmt
        ws.cell(row=r, column=22, value=f"=IF(M{r}=0,0,J{r}/M{r})").number_format = money_fmt
        ws.cell(row=r, column=23, value=f"=IF(N{r}=0,0,K{r}/N{r})").number_format = money_fmt
        # X/Y/Z rpl = rev / conv
        ws.cell(row=r, column=24, value=f"=IF(L{r}=0,0,O{r}/L{r})").number_format = money_fmt
        ws.cell(row=r, column=25, value=f"=IF(M{r}=0,0,P{r}/M{r})").number_format = money_fmt
        ws.cell(row=r, column=26, value=f"=IF(N{r}=0,0,Q{r}/N{r})").number_format = money_fmt
        # AA/AB/AC roas = rev / cost
        ws.cell(row=r, column=27, value=f"=IF(I{r}=0,0,O{r}/I{r})").number_format = roas_fmt
        ws.cell(row=r, column=28, value=f"=IF(J{r}=0,0,P{r}/J{r})").number_format = roas_fmt
        ws.cell(row=r, column=29, value=f"=IF(K{r}=0,0,Q{r}/K{r})").number_format = roas_fmt
        # AD/AE/AF gp = rev - cost
        ws.cell(row=r, column=30, value=f"=O{r}-I{r}").number_format = money_fmt
        ws.cell(row=r, column=31, value=f"=P{r}-J{r}").number_format = money_fmt
        ws.cell(row=r, column=32, value=f"=Q{r}-K{r}").number_format = money_fmt
        # AG/AH/AI ctr = clicks / impressions (percentage, 2 decimals)
        ws.cell(row=r, column=33, value=f"=IF(C{r}=0,0,F{r}/C{r})").number_format = pct_fmt
        ws.cell(row=r, column=34, value=f"=IF(D{r}=0,0,G{r}/D{r})").number_format = pct_fmt
        ws.cell(row=r, column=35, value=f"=IF(E{r}=0,0,H{r}/E{r})").number_format = pct_fmt
        # AJ/AK/AL cvr = conv / clicks
        ws.cell(row=r, column=36, value=f"=IF(F{r}=0,0,L{r}/F{r})").number_format = pct_fmt
        ws.cell(row=r, column=37, value=f"=IF(G{r}=0,0,M{r}/G{r})").number_format = pct_fmt
        ws.cell(row=r, column=38, value=f"=IF(H{r}=0,0,N{r}/H{r})").number_format = pct_fmt
        # AM/AN/AO margin = gp / rev
        ws.cell(row=r, column=39, value=f"=IF(O{r}=0,0,AD{r}/O{r})").number_format = pct_fmt
        ws.cell(row=r, column=40, value=f"=IF(P{r}=0,0,AE{r}/P{r})").number_format = pct_fmt
        ws.cell(row=r, column=41, value=f"=IF(Q{r}=0,0,AF{r}/Q{r})").number_format = pct_fmt
        # AP audience_share_cost = cost_audience / cost_total
        ws.cell(row=r, column=42, value=f"=IF(I{r}=0,0,K{r}/I{r})").number_format = pct_fmt

        # Apply styling
        for col_idx in range(1, 43):
            cell = ws.cell(row=r, column=col_idx)
            cell.font = body_font
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)
            if is_current_week:
                cell.fill = current_fill
            elif col_idx in metric_search_cols:
                cell.fill = search_fill
            elif col_idx in metric_audience_cols:
                cell.fill = audience_fill
            elif col_idx == audience_share_col:
                cell.fill = audience_fill

        row_idx += 1
        cur_date += timedelta(days=1)

    last_row = row_idx - 1

    # Column widths — narrow but readable
    ws.column_dimensions["A"].width = 12  # date
    ws.column_dimensions["B"].width = 8   # weekday
    for c in range(3, 43):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.freeze_panes = ws.cell(row=5, column=3)  # freeze date + weekday

    return last_row


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — DAY-OF-WEEK GRID
# ═══════════════════════════════════════════════════════════════════

def write_dow_grid_sheet(ws, daily_sheet_name: str,
                          start_date: date, end_date: date,
                          max_data_date: date, last_complete_sun: date,
                          daily_first_row: int, daily_last_row: int,
                          label: str, metric_group: str | None = None):
    """Write a DOW_<code>_<group> sheet using SUMIFS formulas.

    metric_group selects which grids to include:
      - 'volume':     impressions, clicks, CTR, conversions, revenue,
                      cost                               (6 × 3 = 18 grids)
      - 'efficiency': CPC, CPL, CvR, ROAS                (4 × 3 = 12 grids)
                      + audience_share_cost              (1 grid)
                      = 13 grids
      - 'revenue':    RPL, GP, margin                    (3 × 3 = 9 grids)
      - None: combined view — all 40 grids on a single sheet, with a
              navigation table at the top.

    Note: the script's main callers always use metric_group=None to get
    the combined view per campaign and at the account level. The
    sub-group plans are kept so partial views can be produced if needed.

    Each grid:
      - Rows = ISO weeks (Mon-Sun), cols = Mon..Sun, then Total.
      - Cells use SUMIFS referencing the Daily sheet date column.
        Reads naturally as 'sum metric where date = this cell's date'.
      - Total column = weekly aggregate. Sum for count metrics; weighted
        ratio (SUMIFS numerator / SUMIFS denominator) for ratio metrics.
      - 'avg / weighted' row = per-weekday aggregate across all weeks,
        with the Total column showing the all-window aggregate.
      - 'WoW %' row = most recent complete week vs prior complete week,
        per weekday and for the weekly Total. Partial / current week is
        skipped — only complete weeks compared.

    The SUMIFS approach reads like a human-built workbook. Formulas
    over the ~90-row daily range remain performant in modern Excel.
    """
    title_font = Font(bold=True, size=12, color="305496", name="Arial")
    section_font = Font(bold=True, size=11, color="305496", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    current_fill = PatternFill("solid", fgColor=CURRENT_WEEK_FILL)
    totals_fill = PatternFill("solid", fgColor=ACCOUNT_FILL)
    totals_font = Font(bold=True, name="Arial")
    search_fill = PatternFill("solid", fgColor=SEARCH_TINT)
    audience_fill = PatternFill("solid", fgColor=AUDIENCE_TINT)

    # Header
    group_label = {
        "volume":     "(volume: impressions, clicks, CTR, conversions, revenue, cost)",
        "efficiency": "(efficiency: CPC, CPL, CvR, ROAS, audience-share-cost)",
        "revenue":    "(revenue: RPL, GP, margin)",
        None:         "(all metrics, 40 grids)",
    }[metric_group]
    ws.cell(row=1, column=1,
            value=f"Day-of-week grid: {label} {group_label}").font = title_font

    note = (
        f"Grids reference '{daily_sheet_name}' via SUMIFS. Rows = ISO "
        f"weeks (Mon-Sun); columns = weekdays + Total. Scan a column to "
        f"compare a single weekday across weeks; read the Total column "
        f"for weekly aggregates. Amber rows = CURRENT (partial) week. "
        f"Search grids tinted blue, audience grids tinted amber. "
        f"'avg / weighted' row: count metrics use AVERAGE; ratio metrics "
        f"use SUMIFS(numerator)/SUMIFS(denominator) for that weekday "
        f"across all weeks. 'WoW %' row: most recent complete week vs "
        f"prior complete week (partial weeks skipped)."
    )
    ws.cell(row=2, column=1, value=note).font = note_font

    # Build list of weeks
    week_mondays = []
    cur_mon = start_date
    while cur_mon <= end_date:
        week_mondays.append(cur_mon)
        cur_mon += timedelta(days=7)
    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # SUMIFS range references on the Daily sheet
    # The Daily sheet has data rows from daily_first_row to daily_last_row.
    # Column A on Daily = date; Column B = weekday name.
    date_range = (f"'{daily_sheet_name}'!$A${daily_first_row}:"
                  f"$A${daily_last_row}")
    weekday_range = (f"'{daily_sheet_name}'!$B${daily_first_row}:"
                     f"$B${daily_last_row}")

    def col_range(col_letter: str) -> str:
        return (f"'{daily_sheet_name}'!${col_letter}${daily_first_row}:"
                f"${col_letter}${daily_last_row}")

    # Daily-sheet column letters per (metric, network):
    # imp:  total=C, search=D, audience=E
    # clk:  total=F, search=G, audience=H
    # cost: total=I, search=J, audience=K
    # conv: total=L, search=M, audience=N
    # rev:  total=O, search=P, audience=Q
    # cpc:  total=R, search=S, audience=T   (cost/clicks)
    # cpl:  total=U, search=V, audience=W   (cost/conv)
    # rpl:  total=X, search=Y, audience=Z   (rev/conv)
    # roas: total=AA, search=AB, audience=AC (rev/cost)
    # gp:   total=AD, search=AE, audience=AF (rev - cost)

    # For SUMIFS-based grids:
    # - Sum-able metrics (impressions, clicks, cost, conv, rev, gp): use SUMIFS on the
    #   raw column. GP is sum-able because rev and cost are linear, so SUM(gp) = SUM(rev) - SUM(cost).
    # - Ratio metrics (cpc, cpl, rpl, roas): must be computed weighted at the cell
    #   level for accurate aggregation — SUMIFS(numerator) / SUMIFS(denominator).

    def write_grid(start_row: int, metric_label: str, network: str,
                   grid_kind: str,           # 'sum', 'cpc', 'cpl', 'rpl', 'roas',
                                             # 'ctr', 'cvr', 'margin', 'audshare'
                   network_cost_col: str,    # for ratio kinds: cost col letter (cpc/cpl/audshare)
                   network_clicks_col: str,
                   network_conv_col: str,
                   network_rev_col: str,
                   sum_col: str | None,      # for 'sum' kind: the column to SUMIFS
                   network_imp_col: str,     # for 'ctr': impressions column letter
                   network_gp_col: str,      # for 'margin': gp column letter
                   audshare_total_col: str,  # for 'audshare': cost_total col (denominator)
                   number_format: str):
        """Write one grid block. Returns the last row used."""
        section_cell = ws.cell(row=start_row, column=1, value=metric_label)
        section_cell.font = section_font

        net_fill = None
        if network == "search":
            net_fill = search_fill
        elif network == "audience":
            net_fill = audience_fill

        # Header row — includes Total column after Sunday
        header_row = start_row + 1
        headers_row = ["week_start"] + WEEKDAY_NAMES + ["Total"]
        for col_idx, h in enumerate(headers_row, 1):
            cell = ws.cell(row=header_row, column=col_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Data rows: one per week
        for wk_idx, wk_mon in enumerate(week_mondays):
            r = header_row + 1 + wk_idx
            ws.cell(row=r, column=1, value=wk_mon).number_format = "yyyy-mm-dd"
            is_current_week = wk_mon > last_complete_sun - timedelta(days=6)

            for wd in range(7):
                col_idx = 2 + wd
                # The date for this cell = wk_mon + wd days
                # Build a SUMIFS that matches that exact date on the Daily sheet.
                # Reference: $A<row> on this DOW sheet is the wk_mon date;
                # +<wd> shifts to the target weekday.
                this_row = r
                date_ref = f"$A{this_row}+{wd}" if wd > 0 else f"$A{this_row}"

                if grid_kind == "sum":
                    formula = (f"=SUMIFS({col_range(sum_col)},"
                               f"{date_range},{date_ref})")
                elif grid_kind == "cpc":
                    # weighted: cost / clicks for this date (single-day, so it's
                    # just cost/clicks of one row — but we use SUMIFS for
                    # consistency and zero-guard via IFERROR)
                    num = f"SUMIFS({col_range(network_cost_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_clicks_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "cpl":
                    num = f"SUMIFS({col_range(network_cost_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_conv_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "rpl":
                    num = f"SUMIFS({col_range(network_rev_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_conv_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "roas":
                    num = f"SUMIFS({col_range(network_rev_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_cost_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "ctr":
                    # clicks / impressions
                    num = f"SUMIFS({col_range(network_clicks_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_imp_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "cvr":
                    # conversions / clicks
                    num = f"SUMIFS({col_range(network_conv_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_clicks_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "margin":
                    # gp / revenue
                    num = f"SUMIFS({col_range(network_gp_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(network_rev_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                elif grid_kind == "audshare":
                    # cost_audience / cost_total — network-mix metric, not
                    # split by network itself
                    num = f"SUMIFS({col_range(network_cost_col)},{date_range},{date_ref})"
                    den = f"SUMIFS({col_range(audshare_total_col)},{date_range},{date_ref})"
                    formula = f"=IFERROR({num}/{den},\"\")"
                else:
                    formula = ""

                cell = ws.cell(row=r, column=col_idx, value=formula)
                cell.number_format = number_format
                cell.font = body_font
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
                if is_current_week:
                    cell.fill = current_fill
                elif net_fill is not None:
                    cell.fill = net_fill

            # Total column (col 9) — week-level aggregate.
            # Date range for this week: $A<r> (Mon) to $A<r>+6 (Sun).
            # Uses SUMIFS with two date conditions to capture the full week.
            total_col_idx = 9
            week_start_ref = f"$A{r}"
            week_end_ref = f"$A{r}+6"
            if grid_kind == "sum":
                # Sum the metric over the whole week
                t_formula = (f"=SUMIFS({col_range(sum_col)},"
                             f"{date_range},\">=\"&{week_start_ref},"
                             f"{date_range},\"<=\"&{week_end_ref})")
            elif grid_kind == "cpc":
                num = (f"SUMIFS({col_range(network_cost_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_clicks_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "cpl":
                num = (f"SUMIFS({col_range(network_cost_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_conv_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "rpl":
                num = (f"SUMIFS({col_range(network_rev_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_conv_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "roas":
                num = (f"SUMIFS({col_range(network_rev_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_cost_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "ctr":
                num = (f"SUMIFS({col_range(network_clicks_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_imp_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "cvr":
                num = (f"SUMIFS({col_range(network_conv_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_clicks_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "margin":
                num = (f"SUMIFS({col_range(network_gp_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(network_rev_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "audshare":
                num = (f"SUMIFS({col_range(network_cost_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                den = (f"SUMIFS({col_range(audshare_total_col)},"
                       f"{date_range},\">=\"&{week_start_ref},"
                       f"{date_range},\"<=\"&{week_end_ref})")
                t_formula = f"=IFERROR({num}/{den},\"\")"
            else:
                t_formula = ""
            t_cell = ws.cell(row=r, column=total_col_idx, value=t_formula)
            t_cell.number_format = number_format
            t_cell.font = totals_font  # bold to read as a summary column
            t_cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                   left=THIN_BORDER, right=THIN_BORDER)
            if is_current_week:
                t_cell.fill = current_fill
            else:
                t_cell.fill = totals_fill  # grey to set apart from data cells

        # Totals row at the bottom — weighted across all weeks for this weekday.
        # Use weekday-name SUMIFS so it spans the whole column cleanly.
        totals_row = header_row + 1 + len(week_mondays)
        ws.cell(row=totals_row, column=1, value="avg / weighted").font = totals_font
        ws.cell(row=totals_row, column=1).fill = totals_fill

        for wd in range(7):
            wd_name = WEEKDAY_NAMES[wd]
            if grid_kind == "sum":
                # Simple average for count/sum metrics — read 'avg per week for this weekday'
                # Use AVERAGEIFS on the Daily column where weekday matches
                formula = (f"=IFERROR(AVERAGEIFS({col_range(sum_col)},"
                           f"{weekday_range},\"{wd_name}\"),\"\")")
            elif grid_kind == "cpc":
                num = f"SUMIFS({col_range(network_cost_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_clicks_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "cpl":
                num = f"SUMIFS({col_range(network_cost_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_conv_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "rpl":
                num = f"SUMIFS({col_range(network_rev_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_conv_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "roas":
                num = f"SUMIFS({col_range(network_rev_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_cost_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "ctr":
                num = f"SUMIFS({col_range(network_clicks_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_imp_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "cvr":
                num = f"SUMIFS({col_range(network_conv_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_clicks_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "margin":
                num = f"SUMIFS({col_range(network_gp_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(network_rev_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            elif grid_kind == "audshare":
                num = f"SUMIFS({col_range(network_cost_col)},{weekday_range},\"{wd_name}\")"
                den = f"SUMIFS({col_range(audshare_total_col)},{weekday_range},\"{wd_name}\")"
                formula = f"=IFERROR({num}/{den},\"\")"
            else:
                formula = ""
            cell = ws.cell(row=totals_row, column=2 + wd, value=formula)
            cell.number_format = number_format
            cell.font = totals_font
            cell.fill = totals_fill
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)

        # Total column on the avg/weighted row — all-window aggregate.
        # For sum metrics: average across all weeks (matches the weekday
        #   avg semantics, but rolled up to the whole-week scale).
        # For ratio metrics: SUMIFS(numerator) / SUMIFS(denominator) over
        #   the full window (no weekday filter).
        total_avg_col = 9
        full_date_lo = f"\"<={max_data_date.isoformat()}\""
        full_date_hi = f"\">={start_date.isoformat()}\""
        if grid_kind == "sum":
            # All-window: per-week average = total / number of weeks.
            # Use SUM over all rows divided by number of complete weeks.
            # Simpler: total over the window / number of weeks in the window.
            sum_all = f"SUMIFS({col_range(sum_col)},{date_range},{full_date_hi})"
            n_weeks = max(len(week_mondays), 1)
            t_formula = f"=IFERROR({sum_all}/{n_weeks},\"\")"
        elif grid_kind == "cpc":
            num = f"SUMIFS({col_range(network_cost_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_clicks_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "cpl":
            num = f"SUMIFS({col_range(network_cost_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_conv_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "rpl":
            num = f"SUMIFS({col_range(network_rev_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_conv_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "roas":
            num = f"SUMIFS({col_range(network_rev_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_cost_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "ctr":
            num = f"SUMIFS({col_range(network_clicks_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_imp_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "cvr":
            num = f"SUMIFS({col_range(network_conv_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_clicks_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "margin":
            num = f"SUMIFS({col_range(network_gp_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(network_rev_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        elif grid_kind == "audshare":
            num = f"SUMIFS({col_range(network_cost_col)},{date_range},{full_date_hi})"
            den = f"SUMIFS({col_range(audshare_total_col)},{date_range},{full_date_hi})"
            t_formula = f"=IFERROR({num}/{den},\"\")"
        else:
            t_formula = ""
        t_cell = ws.cell(row=totals_row, column=total_avg_col, value=t_formula)
        t_cell.number_format = number_format
        t_cell.font = totals_font
        t_cell.fill = totals_fill
        t_cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                               left=THIN_BORDER, right=THIN_BORDER)

        # WoW % row beneath the totals row.
        # Compares the most recent COMPLETE week's value to the prior
        # complete week's value, per weekday. Partial / current week is
        # skipped — we only compare complete weeks to each other.
        # Identify the row indices in this grid for the two weeks.
        wow_row = totals_row + 1
        recent_mon = last_complete_sun - timedelta(days=6)
        prior_mon = recent_mon - timedelta(days=7)
        # Find the data rows (header_row + 1 + index in week_mondays) for
        # those two week starts. If either is outside the window, fall
        # back to empty WoW cells.
        recent_idx = next(
            (i for i, m in enumerate(week_mondays) if m == recent_mon), None
        )
        prior_idx = next(
            (i for i, m in enumerate(week_mondays) if m == prior_mon), None
        )
        ws.cell(row=wow_row, column=1, value="WoW %").font = totals_font
        ws.cell(row=wow_row, column=1).fill = totals_fill
        ws.cell(row=wow_row, column=1).border = Border(
            top=THIN_BORDER, bottom=THIN_BORDER,
            left=THIN_BORDER, right=THIN_BORDER,
        )

        if recent_idx is not None and prior_idx is not None:
            recent_data_row = header_row + 1 + recent_idx
            prior_data_row = header_row + 1 + prior_idx
            for wd in range(7):
                col_letter = get_column_letter(2 + wd)
                recent_ref = f"{col_letter}{recent_data_row}"
                prior_ref = f"{col_letter}{prior_data_row}"
                # (recent - prior) / prior — guarded against zero or
                # non-numeric (blank IFERROR outputs from ratio cells).
                formula = (f"=IFERROR(IF(N({prior_ref})=0,\"\","
                           f"(N({recent_ref})-N({prior_ref}))/N({prior_ref})),\"\")")
                cell = ws.cell(row=wow_row, column=2 + wd, value=formula)
                cell.number_format = "0.0%;[Red]-0.0%"
                cell.font = totals_font
                cell.fill = totals_fill
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
            # Total-column WoW% — weekly aggregate this week vs prior week.
            # Reference column 9 (Total) on the same data rows.
            total_letter = get_column_letter(9)
            recent_total_ref = f"{total_letter}{recent_data_row}"
            prior_total_ref = f"{total_letter}{prior_data_row}"
            t_wow_formula = (
                f"=IFERROR(IF(N({prior_total_ref})=0,\"\","
                f"(N({recent_total_ref})-N({prior_total_ref}))/N({prior_total_ref})),\"\")"
            )
            t_cell = ws.cell(row=wow_row, column=9, value=t_wow_formula)
            t_cell.number_format = "0.0%;[Red]-0.0%"
            t_cell.font = totals_font
            t_cell.fill = totals_fill
            t_cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                   left=THIN_BORDER, right=THIN_BORDER)
        else:
            # Not enough complete weeks in the window — leave cells empty.
            for wd in range(7):
                cell = ws.cell(row=wow_row, column=2 + wd, value="")
                cell.fill = totals_fill
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
            # Total column blank
            t_cell = ws.cell(row=wow_row, column=9, value="")
            t_cell.fill = totals_fill
            t_cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                   left=THIN_BORDER, right=THIN_BORDER)

        return wow_row

    # Pull together the grid plan based on metric_group.
    # Each entry: (label, network, grid_kind, sum_col_or_None, format)
    # For ratio grids we pass column letters per network via separate args.
    # The plan emits ordered triplets (total, search, audience) per metric.
    INT_FMT = "#,##0"
    MONEY_FMT = '"$"#,##0.00'
    CONV_FMT = "#,##0.0"
    ROAS_FMT = "0.0000"
    PCT_FMT = "0.00%"

    def triplet(letter_prefix, label_prefix, kind, fmt,
                sum_cols=None,
                cost_cols=None, clk_cols=None, conv_cols=None, rev_cols=None,
                imp_cols=None, gp_cols=None):
        """Emit three grid descriptors for one metric (total, search, audience)."""
        nets = ["total", "search", "audience"]
        out = []
        for i, net in enumerate(nets):
            d = {
                "label": f"{letter_prefix} — {label_prefix} ({net})",
                "network": net,
                "grid_kind": kind,
                "format": fmt,
                "sum_col": sum_cols[i] if sum_cols else None,
                "cost_col": cost_cols[i] if cost_cols else None,
                "clk_col":  clk_cols[i] if clk_cols else None,
                "conv_col": conv_cols[i] if conv_cols else None,
                "rev_col":  rev_cols[i] if rev_cols else None,
                "imp_col":  imp_cols[i] if imp_cols else None,
                "gp_col":   gp_cols[i] if gp_cols else None,
                "audshare_total_col": None,
            }
            out.append(d)
        return out

    def single_grid(letter_prefix, label_prefix, kind, fmt,
                    cost_col=None, total_col=None):
        """Emit one grid descriptor for a non-triplet (network-mix) metric."""
        return [{
            "label": f"{letter_prefix} — {label_prefix}",
            "network": "total",  # styled as non-tinted; it's a mix metric
            "grid_kind": kind,
            "format": fmt,
            "sum_col": None,
            "cost_col": cost_col,
            "clk_col":  None,
            "conv_col": None,
            "rev_col":  None,
            "imp_col":  None,
            "gp_col":   None,
            "audshare_total_col": total_col,
        }]

    plans = {
        "volume": [
            *triplet("A1", "Impressions", "sum", INT_FMT,
                     sum_cols=["C", "D", "E"]),
            *triplet("A2", "Clicks", "sum", INT_FMT,
                     sum_cols=["F", "G", "H"]),
            *triplet("A3", "CTR", "ctr", PCT_FMT,
                     clk_cols=["F", "G", "H"], imp_cols=["C", "D", "E"]),
            *triplet("A4", "Conversions", "sum", CONV_FMT,
                     sum_cols=["L", "M", "N"]),
            *triplet("A5", "Revenue", "sum", MONEY_FMT,
                     sum_cols=["O", "P", "Q"]),
            *triplet("A6", "Cost", "sum", MONEY_FMT,
                     sum_cols=["I", "J", "K"]),
        ],
        "efficiency": [
            *triplet("B1", "CPC", "cpc", MONEY_FMT,
                     cost_cols=["I", "J", "K"], clk_cols=["F", "G", "H"]),
            *triplet("B2", "CPL", "cpl", MONEY_FMT,
                     cost_cols=["I", "J", "K"], conv_cols=["L", "M", "N"]),
            *triplet("B3", "CvR", "cvr", PCT_FMT,
                     conv_cols=["L", "M", "N"], clk_cols=["F", "G", "H"]),
            *triplet("B4", "ROAS", "roas", ROAS_FMT,
                     rev_cols=["O", "P", "Q"], cost_cols=["I", "J", "K"]),
            # Audience share of cost — single grid, not split by network
            *single_grid("B5", "Audience share of cost", "audshare", PCT_FMT,
                         cost_col="K", total_col="I"),
        ],
        "revenue": [
            *triplet("C1", "RPL", "rpl", MONEY_FMT,
                     rev_cols=["O", "P", "Q"], conv_cols=["L", "M", "N"]),
            *triplet("C2", "GP", "sum", MONEY_FMT,
                     sum_cols=["AD", "AE", "AF"]),
            *triplet("C3", "Profit margin", "margin", PCT_FMT,
                     gp_cols=["AD", "AE", "AF"], rev_cols=["O", "P", "Q"]),
        ],
    }

    if metric_group is None:
        # Combined-sheet view: all metrics from all groups.
        plan = plans["volume"] + plans["efficiency"] + plans["revenue"]
    else:
        plan = plans[metric_group]

    # Reserve rows 3-8 for the navigation table (rendered after grids
    # so anchor row numbers are known). Grids start at row 9.
    current_start = 9
    section_rows = []  # (label_text, row_number) for navigation table
    for p in plan:
        last_row = write_grid(
            start_row=current_start,
            metric_label=p["label"],
            network=p["network"],
            grid_kind=p["grid_kind"],
            network_cost_col=p["cost_col"] or "",
            network_clicks_col=p["clk_col"] or "",
            network_conv_col=p["conv_col"] or "",
            network_rev_col=p["rev_col"] or "",
            sum_col=p["sum_col"],
            network_imp_col=p["imp_col"] or "",
            network_gp_col=p["gp_col"] or "",
            audshare_total_col=p["audshare_total_col"] or "",
            number_format=p["format"],
        )
        section_rows.append((p["label"], current_start))
        current_start = last_row + 3

    # Render navigation table in rows 3-7 (reserved region above first
    # grid at row 9). One entry per metric (filter to 'total' network rows
    # plus single-grid metrics that have no network suffix).
    if section_rows:
        nav_entries = []
        for lbl, rn in section_rows:
            if "(search)" in lbl or "(audience)" in lbl:
                continue
            nav_entries.append((lbl, rn))

        nav_title_font = Font(bold=True, color="305496", name="Arial", size=10)
        nav_link_font = Font(color="0563C1", name="Arial", underline="single")

        ws.cell(row=3, column=1, value="Jump to:").font = nav_title_font
        # Layout: 4 link pairs per row across columns A-H.
        per_row = 4
        for i, (lbl, rn) in enumerate(nav_entries):
            nav_row = 4 + (i // per_row)
            col_pair = (i % per_row) * 2 + 1  # cols 1,3,5,7
            display = lbl
            if "—" in display:
                display = display.split("—", 1)[1].strip()
            if "(" in display:
                display = display.split("(", 1)[0].strip()
            # Build hyperlink to local cell. Sheet self-reference uses
            # the sheet name; we use ws.title to avoid hardcoding.
            link_cell = ws.cell(
                row=nav_row, column=col_pair,
                value=f"=HYPERLINK(\"#'{ws.title}'!A{rn}\",\"{display}\")",
            )
            link_cell.font = nav_link_font
            ws.cell(row=nav_row, column=col_pair + 1, value=f"row {rn}").font = note_font

    # Column widths
    ws.column_dimensions["A"].width = 14
    for c in range(2, 9):  # Mon..Sun
        ws.column_dimensions[get_column_letter(c)].width = 12
    ws.column_dimensions["I"].width = 13  # Total — slightly wider
    # Freeze: row 9 (first grid header) and column B (Mon)
    ws.freeze_panes = ws.cell(row=9, column=2)


# ═══════════════════════════════════════════════════════════════════
# INDEX SHEET
# ═══════════════════════════════════════════════════════════════════

def write_index_sheet(ws, code_to_meta: dict, window_start: date,
                      last_complete_sun: date, max_data_date: date,
                      source_note: str):
    """First sheet: short-code -> campaign_name mapping and run context."""
    title_font = Font(bold=True, size=14, color="305496", name="Arial")
    label_font = Font(bold=True, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")

    ws.cell(row=1, column=1,
            value="Microsoft Home Insurance — Performance Data Tables").font = title_font

    # Run context
    ws.cell(row=3, column=1, value="Window start (Mon):").font = label_font
    ws.cell(row=3, column=2, value=str(window_start)).font = body_font
    ws.cell(row=4, column=1, value="Last complete Sunday:").font = label_font
    ws.cell(row=4, column=2, value=str(last_complete_sun)).font = body_font
    ws.cell(row=5, column=1, value="Latest data date:").font = label_font
    ws.cell(row=5, column=2, value=str(max_data_date)).font = body_font
    ws.cell(row=6, column=1, value="End-date source:").font = label_font
    ws.cell(row=6, column=2, value=source_note).font = body_font

    ws.cell(row=8, column=1, value=(
        "This workbook contains data tables only. For per-campaign weekly "
        "trend charts, see campaign_performance_microsoft_<date>.xlsx. "
        "The recommendation engine and rationales live in "
        "efficiency_frontier_microsoft_<date>.xlsx. "
        "Data source: campaign_performance_daily (search) + "
        "campaign_performance_network_daily (audience). Under the "
        "pixel-as-primary convention (May 2026), .conversions is the "
        "pixel-only count and .conversion_value is offline-inclusive "
        "revenue. Reconciles with keyword_performance_daily totals "
        "within ~0.001%. No backfill buffer applied — Rows beyond the "
        "last complete Sunday are highlighted amber as CURRENT week.")
    ).font = note_font

    # Sheet index header
    hdr_row = 10
    for c, h in enumerate(["short_code", "scope", "status",
                            "campaign_name", "current_tcpa"], 1):
        cell = ws.cell(row=hdr_row, column=c, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Sort: Account first, then by campaign_name
    rows_sorted = sorted(
        code_to_meta.items(),
        key=lambda kv: (0 if kv[0] == "Account" else 1, kv[1].get("campaign_name", "")),
    )
    for i, (code, meta) in enumerate(rows_sorted):
        r = hdr_row + 1 + i
        ws.cell(row=r, column=1, value=code).font = body_font
        ws.cell(row=r, column=2, value=("account-level" if code == "Account"
                                         else "single-campaign")).font = body_font
        ws.cell(row=r, column=3, value=meta.get("status", "")).font = body_font
        ws.cell(row=r, column=4, value=meta.get("campaign_name", "")).font = body_font
        tcpa = meta.get("current_tcpa")
        if tcpa is not None and pd.notna(tcpa):
            ws.cell(row=r, column=5, value=float(tcpa)).number_format = '"$"#,##0.00'

    # Column widths
    widths = [14, 16, 10, 60, 13]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ═══════════════════════════════════════════════════════════════════
# WORKBOOK ASSEMBLY
# ═══════════════════════════════════════════════════════════════════

def build_workbook(conn, window_start: date, last_complete_sun: date,
                   max_data_date: date, source_note: str) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)  # drop the default sheet; we'll create them explicitly

    # Pull campaigns (ENABLED + PAUSED, but PAUSED gets the recent-activity filter)
    camp_meta = fetch_campaigns(conn)
    n_enabled = (camp_meta["status"] == "ENABLED").sum()
    n_paused = (camp_meta["status"] == "PAUSED").sum()
    print(f"  {n_enabled} ENABLED + {n_paused} PAUSED candidate campaigns")

    # Pull all-campaign daily metrics for the window
    daily_all = fetch_daily_metrics(conn, window_start, max_data_date)
    if daily_all.empty:
        raise RuntimeError("No daily metrics in the window — nothing to write")

    # Account-level: sum across ALL enabled+paused candidates per date (no
    # activity filter — account view is the full picture).
    account_daily = (
        daily_all.groupby("date", as_index=False)
                 .agg(imp_search=("imp_search", "sum"),
                      imp_audience=("imp_audience", "sum"),
                      clicks_search=("clicks_search", "sum"),
                      clicks_audience=("clicks_audience", "sum"),
                      cost_search=("cost_search", "sum"),
                      cost_audience=("cost_audience", "sum"),
                      conv_search=("conv_search", "sum"),
                      conv_audience=("conv_audience", "sum"),
                      rev_search=("rev_search", "sum"),
                      rev_audience=("rev_audience", "sum"))
    )

    # For the per-campaign filter, compute impressions over the last 14 days
    # of the window (anchored at max_data_date inclusive).
    recent_cutoff = max_data_date - timedelta(days=13)
    daily_all["_date"] = pd.to_datetime(daily_all["date"]).dt.date
    recent_imp = (
        daily_all[daily_all["_date"] >= recent_cutoff]
        .groupby("campaign_id")
        .agg(recent_imp_search=("imp_search", "sum"),
             recent_imp_audience=("imp_audience", "sum"))
    )
    recent_imp["recent_total_imp"] = (
        recent_imp["recent_imp_search"] + recent_imp["recent_imp_audience"]
    )

    code_to_meta = {
        "Account": {"campaign_name": "ALL enabled+paused campaigns (sum)",
                    "status": "—", "current_tcpa": None},
    }

    # ── Account sheets: Daily + 1 stitched DOW (all 40 grids on one sheet)
    ws_d = wb.create_sheet("Daily_Account")
    last_row = write_daily_sheet(
        ws_d, account_daily, window_start, max_data_date,
        max_data_date, last_complete_sun,
        label="Account (all campaigns)", is_account=True,
    )
    print(f"  ✓ Daily_Account ({last_row - 4} rows)")
    daily_first_row = 5
    daily_last_row = last_row

    ws_g = wb.create_sheet("DOW_Account")
    write_dow_grid_sheet(
        ws_g, "Daily_Account", window_start, max_data_date,
        max_data_date, last_complete_sun,
        daily_first_row, daily_last_row,
        label="Account", metric_group=None,  # all 40 grids
    )
    print("  ✓ DOW_Account (all 40 grids, navigation table at rows 3-7)")

    # ── Per-campaign sheets
    written_codes = {"Account"}
    n_included = 0
    n_skipped_paused_idle = 0
    n_skipped_zero_data = 0

    for _, cm in camp_meta.iterrows():
        cid = cm["campaign_id"]
        cname = cm["campaign_name"]
        status = cm["status"]

        camp_daily = daily_all[daily_all["campaign_id"] == cid].drop(
            columns=["campaign_id", "_date"], errors="ignore"
        )

        # Hard zero-data filter: no impressions or clicks across the entire window
        if (camp_daily["imp_search"].sum() == 0
                and camp_daily["imp_audience"].sum() == 0
                and camp_daily["clicks_search"].sum() == 0
                and camp_daily["clicks_audience"].sum() == 0):
            n_skipped_zero_data += 1
            continue

        # PAUSED activity filter: require ≥1 impression in last 14 days
        if status == "PAUSED":
            recent_total = recent_imp.loc[cid, "recent_total_imp"] if cid in recent_imp.index else 0
            if recent_total < 1:
                n_skipped_paused_idle += 1
                continue

        code = short_code(cname)
        base_code = code
        suffix = 2
        while code in written_codes:
            code = f"{base_code}{suffix}"
            suffix += 1
        written_codes.add(code)
        code_to_meta[code] = {"campaign_name": cname, "status": status,
                              "current_tcpa": cm["current_tcpa"]}

        ws_d = wb.create_sheet(f"Daily_{code}")
        last_row = write_daily_sheet(
            ws_d, camp_daily, window_start, max_data_date,
            max_data_date, last_complete_sun,
            label=cname, is_account=False,
        )

        # One consolidated DOW sheet per campaign — all 40 grids, same
        # layout as DOW_Account. Navigation table at row 3 lets you jump
        # to specific sections (impressions, CTR, ROAS, etc.).
        ws_g = wb.create_sheet(f"DOW_{code}")
        write_dow_grid_sheet(
            ws_g, f"Daily_{code}", window_start, max_data_date,
            max_data_date, last_complete_sun,
            5, last_row, label=cname, metric_group=None,
        )

        n_included += 1
        print(f"  ✓ Daily_{code} + DOW_{code}  "
              f"({status}, {cname})")

    # Index sheet — insert at position 0 (first sheet)
    ws_idx = wb.create_sheet("Index", 0)
    write_index_sheet(ws_idx, code_to_meta, window_start,
                       last_complete_sun, max_data_date, source_note)
    print("  ✓ Index")
    print()
    print(f"  Per-campaign sheets included: {n_included}")
    if n_skipped_paused_idle:
        print(f"  Paused campaigns skipped (no impressions in last 14d): "
              f"{n_skipped_paused_idle}")
    if n_skipped_zero_data:
        print(f"  Campaigns skipped (zero data in entire window): "
              f"{n_skipped_zero_data}")

    return wb


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--end-date", default=None,
                   help="If omitted, auto-detects the most recent complete "
                        "Mon-Sun week from MAX(date) in the DB. The current "
                        "partial week is always included regardless. Pass "
                        "explicitly (YYYY-MM-DD) to override.")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help=f"Number of complete weeks to include. "
                        f"Default: {DEFAULT_WEEKS}")
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)

    banner("CAMPAIGN PERFORMANCE DATA TABLES — Microsoft Home")
    print(f"  DB:                  {db_path}")
    print(f"  Output:              {output_dir}")

    conn = sqlite3.connect(db_path)
    window_start, last_complete_sun, max_data_date, source_note = \
        resolve_window(conn, args.end_date, args.weeks)
    print(f"  Window start (Mon):  {window_start}")
    print(f"  Last complete Sun:   {last_complete_sun}")
    print(f"  Latest data date:    {max_data_date}  ({source_note})")
    print(f"  Weeks (complete):    {args.weeks}")
    print()

    print("Building workbook...")
    wb = build_workbook(conn, window_start, last_complete_sun,
                        max_data_date, source_note)

    run_date = max_data_date.isoformat()
    output_path = output_dir / f"campaign_performance_data_microsoft_{run_date}.xlsx"
    wb.save(output_path)
    print()
    print(f"  ✓ Wrote {output_path}")
    conn.close()


if __name__ == "__main__":
    main()
