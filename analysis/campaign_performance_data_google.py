"""
Campaign Performance Data Tables — Google Home Insurance
═══════════════════════════════════════════════════════════════════════
Produces a data-tables workbook with the same 13-metric panel the
Microsoft workbook carries, minus the per-network triplets (Google
Home has no Audience Network equivalent — search is the only channel).
The audience-share-of-cost metric is also dropped for the same reason.

METRICS (single-network):
  - Impressions, Clicks, CTR                 (volume + engagement)
  - Cost                                     (spend)
  - Primary Conversions, CvR, Revenue        (outcome, routed)
  - CPC, CPL, RPL, ROAS                      (derived efficiency)
  - GP, Profit Margin                        (profitability)

Window: 12 complete Mon-Sun weeks ending the most recent Sunday on or
before MAX(date), plus all days available in the current partial week.

SHEETS:
  - Index: navigation map + run context.
  - Daily_<code>: 15 columns (date, weekday, 5 stored metrics, 8
    derived metrics). Derived metrics (CPC, CPL, RPL, ROAS, GP, CTR,
    CvR, margin) are Excel formulas referencing the stored columns.
    Current-week rows tinted amber.
  - DOW_<code>: weeks-as-rows × weekdays-as-columns matrix with thirteen
    stacked grids in three thematic blocks. Volume: Impressions, Clicks,
    CTR, Primary Conv, Revenue. Cost & efficiency: Cost, CPC, CPL, CvR.
    Outcome: ROAS, RPL, GP, Margin. Each grid has a "Total" week-sum
    column at the right (weighted for ratio grids), an "avg / weighted"
    totals row, and a "WoW %" row underneath comparing the most recent
    complete week to the prior complete week (per weekday). Grid cells
    use SUMIFS referencing the Daily sheet date column, so they read
    like a human-built workbook. Every grid repeats in three
    side-by-side device sections on the same rows: Total (cols A..I),
    Mobile (V..AD, device='MOBILE'), and Non-Mobile (AQ..AY, computed
    as Total − Mobile so the sections reconcile exactly). Each section
    is followed by a line chart of its weekly "Total" column with
    value-only labels above the line.

SHEET ORDER: Index first, then ALL DOW sheets side by side
(DOW_Account, then per-campaign), then all Daily sheets in the same
relative order. Sheets are created Daily-then-DOW per scope (the DOW
SUMIFS need the Daily row extent) and reordered once at the end of
build_workbook().

Scope: account-level (sum across all ENABLED + PAUSED campaigns) plus
one set of sheets per enabled-or-paused campaign that has data in the
window. Short codes are used because Excel sheet names cap at 31 chars
and disallow several characters present in campaign names.

Volume source is campaign_performance_daily_search with device='ALL' (the
per-account rollup row). The table stores rows per device (MOBILE,
DESKTOP, TABLET) plus an 'ALL' rollup; filtering to 'ALL' is REQUIRED
to avoid double-counting impressions/clicks/cost.

CONVERSIONS — ROUTING NOTE. campaign_performance_daily_search.conversions and
.conversion_value are *unrouted* — they include every conversion action
(zip-submits, page-views, etc.) and inflate revenue 5-10x. The routed
counts live on keyword_performance_daily.primary_conv_count /
primary_conv_value, where the upstream Google Ads sync has already
filtered to only revenue-driving primary actions per conversion_actions.
This script aggregates those routed columns up to (campaign, date) and
joins them onto the campaign-level spend rows. Spend totals reconcile
within 0.00% between the two tables — verified on the home account for
last-30-day totals — so the join is safe. A reconciliation check at the
end of the run prints both spend routes for a final sanity confirmation.

USAGE:
  python campaign_performance_data_google.py
  python campaign_performance_data_google.py --end-date 2026-05-10
  python campaign_performance_data_google.py --weeks 16

OUTPUT: campaign_performance_data_google_<YYYY-MM-DD>.xlsx
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── CONFIG ─────────────────────────────────────────────────────────
ACCOUNT_ID = 1
ACCOUNT_LABEL = "google_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
DEFAULT_WEEKS = 12

HEADER_FILL = "305496"
HEADER_FONT_COLOR = "FFFFFF"
ACCOUNT_FILL = "E7E6E6"      # light grey background for totals rows
CURRENT_WEEK_FILL = "FFF2CC"  # amber tint for partial/current week
TOTAL_COL_TINT = "EAF2F9"     # light blue tint for the Total week-sum column
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
    - max_data_date: MAX(date) in campaign_performance_daily_search (device='ALL');
      the current partial week runs from (last_complete_sun + 1d) through
      max_data_date

    If cli_end_date is provided, treat it as the desired "last_complete_sun"
    (or the most recent Sunday on or before it if it's mid-week), and ignore
    any further data in the DB.

    No backfill buffer is applied — impressions/clicks/cost finalize within
    hours. The workbook surfaces the partial week as a visually distinct
    "current" region.
    """
    cur = conn.execute(
        "SELECT MAX(date) FROM campaign_performance_daily_search "
        "WHERE account_id = ? AND device = 'ALL'",
        (ACCOUNT_ID,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError(
            f"No data in campaign_performance_daily_search for account {ACCOUNT_ID} "
            f"with device='ALL'"
        )
    max_data_date = date.fromisoformat(row[0])

    if cli_end_date:
        anchor = date.fromisoformat(cli_end_date)
        source_note = f"CLI override (--end-date {cli_end_date})"
        days_past_sun = (anchor.weekday() + 1) % 7
        last_complete_sun = anchor - timedelta(days=days_past_sun)
        max_data_date = min(max_data_date, anchor)
    else:
        if max_data_date.weekday() == 6:  # Sunday
            last_complete_sun = max_data_date
        else:
            days_past_sun = (max_data_date.weekday() + 1) % 7
            last_complete_sun = max_data_date - timedelta(days=days_past_sun)
        source_note = f"auto-detect: MAX(date)={max_data_date}"

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
    """Per (campaign_id, date): impressions / clicks / cost + routed conversions.

    Volume metrics (impressions/clicks/cost) come from
    campaign_performance_daily_search with device='ALL' — the per-account rollup
    row. The table stores rows per (campaign, date, device) including a
    device='ALL' rollup; filtering to 'ALL' is REQUIRED, because the
    per-device rows (MOBILE, DESKTOP, TABLET) sum to the same totals.

    Conversion metrics come from a separate aggregation against
    keyword_performance_daily.primary_conv_count and primary_conv_value.
    Those columns are the routed values written by the upstream Google
    Ads sync — they only count revenue-driving primary actions per
    conversion_actions, unlike campaign_performance_daily_search.conversions
    which is unrouted and inflates revenue 5-10x. We aggregate the
    keyword table to (campaign, date) and LEFT JOIN back so campaigns
    with no keywords (or no primary conversions) still appear with zero.

    Returns columns:
        campaign_id, date, impressions, clicks, cost_usd,
        primary_conversions, primary_conv_value,
        m_impressions, m_clicks, m_cost_usd,
        m_primary_conversions, m_primary_conv_value
    (m_* columns are the device='MOBILE' slice of the same metrics.)

    DEVICE HANDLING (per DB architect, 2026-07): both tables store
    complete, additive per-device rows (MOBILE / DESKTOP / TABLET /
    trace CONNECTED_TV) alongside the ALL rollup, and the slices sum
    to ALL within rounding. keyword_performance_daily carries NO
    account_id column — scoping MUST go through a join (campaigns via
    campaign_id, as here, or keyword_instances via instance_id). Each
    table is aggregated ONCE with GROUP BY (campaign, date, device)
    over device IN ('ALL','MOBILE') and pivoted in pandas — no
    per-device query variants. Non-Mobile is never fetched; the
    workbook derives it as Total − Mobile so the three sections
    reconcile exactly by construction.
    """
    cpd_sql = """
    SELECT
        cpd.campaign_id AS campaign_id,
        cpd.date AS date,
        cpd.device AS device,
        SUM(cpd.impressions) AS impressions,
        SUM(cpd.clicks) AS clicks,
        SUM(cpd.cost_micros) / 1.0e6 AS cost_usd
    FROM campaign_performance_daily_search cpd
    JOIN campaigns c ON c.campaign_id = cpd.campaign_id
    WHERE c.account_id = ?
      AND cpd.device IN ('ALL', 'MOBILE')
      AND cpd.date BETWEEN ? AND ?
    GROUP BY cpd.campaign_id, cpd.date, cpd.device
    """
    kpd_sql = """
    SELECT
        kpd.campaign_id AS campaign_id,
        kpd.report_date AS date,
        kpd.device AS device,
        SUM(kpd.primary_conv_count) AS primary_conversions,
        SUM(kpd.primary_conv_value) AS primary_conv_value
    FROM keyword_performance_daily kpd
    JOIN campaigns c ON c.campaign_id = kpd.campaign_id
    WHERE c.account_id = ?
      AND kpd.device IN ('ALL', 'MOBILE')
      AND kpd.report_date BETWEEN ? AND ?
    GROUP BY kpd.campaign_id, kpd.report_date, kpd.device
    """
    params = (ACCOUNT_ID, start_date.isoformat(), end_date.isoformat())
    cpd = pd.read_sql(cpd_sql, conn, params=params)
    kpd = pd.read_sql(kpd_sql, conn, params=params)

    def slice_dev(frame, device, rename_map):
        out = frame[frame["device"] == device].drop(columns=["device"])
        return out.rename(columns=rename_map)

    # Base = ALL-device volume rows (defines the campaign×date universe)
    df = slice_dev(cpd, "ALL", {})
    df = df.merge(
        slice_dev(cpd, "MOBILE", {"impressions": "m_impressions",
                                  "clicks": "m_clicks",
                                  "cost_usd": "m_cost_usd"}),
        on=["campaign_id", "date"], how="left",
    )
    df = df.merge(
        slice_dev(kpd, "ALL", {}),
        on=["campaign_id", "date"], how="left",
    )
    df = df.merge(
        slice_dev(kpd, "MOBILE",
                  {"primary_conversions": "m_primary_conversions",
                   "primary_conv_value": "m_primary_conv_value"}),
        on=["campaign_id", "date"], how="left",
    )

    for col in ("impressions", "clicks", "m_impressions", "m_clicks"):
        df[col] = df[col].fillna(0).astype(int)
    for col in ("cost_usd", "m_cost_usd",
                "primary_conversions", "primary_conv_value",
                "m_primary_conversions", "m_primary_conv_value"):
        df[col] = df[col].fillna(0.0).astype(float)

    # Build-time sanity: if mobile volume exists but mobile routed
    # conversions came back all-zero, say so LOUDLY at fetch time (the
    # reconciliation probe at the end of the run localizes which join
    # route fails). Per the DB architect the device rows exist, so
    # zeros here mean a query/linkage problem, not missing data.
    if df["m_cost_usd"].sum() > 0 and df["m_primary_conversions"].sum() == 0:
        print("  ⚠ WARN: mobile spend present but mobile primary conversions "
              "sum to ZERO in the window — kpd MOBILE rows are not joining "
              "through campaign_id. See the dual-route probe in the "
              "reconciliation output.")

    return df[["campaign_id", "date", "impressions", "clicks", "cost_usd",
               "primary_conversions", "primary_conv_value",
               "m_impressions", "m_clicks", "m_cost_usd",
               "m_primary_conversions", "m_primary_conv_value"]]


# ═══════════════════════════════════════════════════════════════════
# SHORT-CODE GENERATION
# ═══════════════════════════════════════════════════════════════════

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

TIER_ABBR = {
    "TIER ONE": "T1",
    "TIER TWO": "T2",
    "TIER THREE": "T3",
    "TIER FOUR": "T4",
}


def short_code(campaign_name: str) -> str:
    """Extract a short identifier from a Google Home Insurance campaign name.

    Handles three known patterns:
      'HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE'    -> 'CA-NB'
      'HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE'      -> 'T1-NB'
      'HOME INSURANCE-WYOMING-SEARCH-MOBILE-CPA-STE'       -> 'WY-MOB'

    Order of detection matters:
      1. Mobile-CPA variant (state + MOBILE) -> '<ST>-MOB'
      2. Tier + NB                           -> 'T<n>-NB'
      3. State + NB (or just state)          -> '<ST>-NB' or '<ST>'

    Falls back to first 8 cleaned chars if no pattern matches. The caller
    dedupes collisions by appending a numeric suffix.
    """
    name_up = campaign_name.upper()

    if "MOBILE" in name_up:
        for state, abbr in STATE_ABBR.items():
            if state in name_up:
                return f"{abbr}-MOB"

    for tier, tabbr in TIER_ABBR.items():
        if tier in name_up:
            return f"{tabbr}-NB"

    for state, abbr in STATE_ABBR.items():
        if state in name_up:
            return f"{abbr}-NB" if "-NB-" in name_up or name_up.endswith("-NB") else abbr

    cleaned = "".join(c for c in name_up if c.isalnum())[:8]
    return cleaned or "CAMP"


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — DAILY
# ═══════════════════════════════════════════════════════════════════
#
# Daily sheet column map (referenced from DOW SUMIFS — keep in sync):
#
#   A   date
#   B   weekday (Mon/Tue/.../Sun)
#   C   impressions          (stored, ALL devices)
#   D   clicks               (stored)
#   E   cost_usd             (stored)
#   F   primary_conv         (stored, routed)
#   G   revenue              (stored, routed)
#   H   cpc      = E/D
#   I   cpl      = E/F       (cost per primary conversion)
#   J   rpl      = G/F       (revenue per primary conversion)
#   K   roas     = G/E
#   L   gp       = G - E     (gross profit)
#   M   ctr      = D/C       (click-through rate, %)
#   N   cvr      = F/D       (conversion rate, %)
#   O   margin   = L/G       (profit margin, %)
#   ── device split (feeds the Mobile / Non-Mobile DOW sections) ──
#   P   m_impressions        (stored, device='MOBILE')
#   Q   m_clicks             (stored)
#   R   m_cost_usd           (stored)
#   S   m_primary_conv       (stored, routed)
#   T   m_revenue            (stored, routed)
#   U   nm_impressions = C-P (formula — Non-Mobile ≡ Total − Mobile)
#   V   nm_clicks      = D-Q
#   W   nm_cost_usd    = E-R
#   X   nm_primary_conv= F-S
#   Y   nm_revenue     = G-T
#   Z   m_gp           = T-R (formula — mobile gross profit)
#   AA  nm_gp          = Y-W (formula — non-mobile gross profit)
#
# DAILY_COLS is the single source of truth; DOW grids read from it via
# string lookups so a column shuffle here propagates everywhere.
DAILY_COLS = {
    "date": "A", "weekday": "B",
    "impressions": "C", "clicks": "D", "cost_usd": "E",
    "primary_conv": "F", "revenue": "G",
    "cpc": "H", "cpl": "I", "rpl": "J", "roas": "K", "gp": "L",
    "ctr": "M", "cvr": "N", "margin": "O",
    # Mobile (stored)
    "m_impressions": "P", "m_clicks": "Q", "m_cost_usd": "R",
    "m_primary_conv": "S", "m_revenue": "T",
    # Non-Mobile (derived: Total − Mobile)
    "nm_impressions": "U", "nm_clicks": "V", "nm_cost_usd": "W",
    "nm_primary_conv": "X", "nm_revenue": "Y",
    # Device gross profit (derived)
    "m_gp": "Z", "nm_gp": "AA",
}


def write_daily_sheet(ws, daily_df: pd.DataFrame,
                      start_date: date, end_date: date,
                      max_data_date: date, last_complete_sun: date,
                      label: str, is_account: bool):
    """Write a Daily_<code> sheet with 5 stored + 5 derived metrics.

    daily_df expected columns: date, impressions, clicks, cost_usd,
    primary_conversions, primary_conv_value.
    Rows are filled in for EVERY date in the [start_date, end_date] range
    even where source data is missing (zeros). This makes the DOW SUMIFS
    formulas simple — no date gaps.

    Layout: see DAILY_COLS above (12 columns).
    """
    title_font = Font(bold=True, size=12, color="305496", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    current_fill = PatternFill("solid", fgColor=CURRENT_WEEK_FILL)

    ws.cell(row=1, column=1, value=f"Daily metrics: {label}").font = title_font
    ws.cell(row=2, column=1, value=(
        f"Window: {start_date} (Mon) to {max_data_date}. Last complete "
        f"week ends {last_complete_sun} (Sun). Rows after that are "
        f"CURRENT (partial) week — amber. Conversions are routed primary "
        f"only (via keyword_performance_daily). Derived metrics (CPC, "
        f"CPL, RPL, ROAS, GP) are Excel formulas, not stored. Columns "
        f"P-T are the Mobile (device='MOBILE') slice; U-Y are Non-Mobile "
        f"formulas (= Total − Mobile); Z/AA are device gross profit. "
        f"These feed the Mobile / Non-Mobile sections on the DOW "
        f"sheet.")).font = note_font

    headers = [
        "date", "weekday",
        "impressions", "clicks", "cost_usd",
        "primary_conv", "revenue",
        "cpc", "cpl", "rpl", "roas", "gp",
        "ctr", "cvr", "margin",
        "m_impressions", "m_clicks", "m_cost_usd",
        "m_primary_conv", "m_revenue",
        "nm_impressions", "nm_clicks", "nm_cost_usd",
        "nm_primary_conv", "nm_revenue",
        "m_gp", "nm_gp",
    ]
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Lookup map: date -> (imp, clicks, cost, conv, rev,
    #                      m_imp, m_clicks, m_cost, m_conv, m_rev)
    date_map = {}
    for _, r in daily_df.iterrows():
        if isinstance(r["date"], str):
            d = date.fromisoformat(r["date"])
        else:
            d = r["date"]
        date_map[d] = (
            int(r["impressions"]),
            int(r["clicks"]),
            float(r["cost_usd"]),
            float(r["primary_conversions"]),
            float(r["primary_conv_value"]),
            int(r["m_impressions"]),
            int(r["m_clicks"]),
            float(r["m_cost_usd"]),
            float(r["m_primary_conversions"]),
            float(r["m_primary_conv_value"]),
        )

    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    int_fmt = "#,##0"
    money_fmt = '"$"#,##0.00'
    conv_fmt = "#,##0.00"
    roas_fmt = "0.00"
    pct_fmt = "0.00%"

    row_idx = 5
    cur_date = start_date
    while cur_date <= end_date:
        (imp, clk, cost, conv, rev,
         m_imp, m_clk, m_cost, m_conv, m_rev) = date_map.get(
            cur_date, (0, 0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0)
        )
        is_current_week = cur_date > last_complete_sun
        r = row_idx

        # A: date, B: weekday
        ws.cell(row=r, column=1, value=cur_date).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=2, value=WEEKDAY_NAMES[cur_date.weekday()])
        # C: imp, D: clicks, E: cost — stored values
        ws.cell(row=r, column=3, value=imp).number_format = int_fmt
        ws.cell(row=r, column=4, value=clk).number_format = int_fmt
        ws.cell(row=r, column=5, value=round(cost, 2)).number_format = money_fmt
        # F: primary_conv, G: revenue — stored, routed values
        ws.cell(row=r, column=6, value=round(conv, 2)).number_format = conv_fmt
        ws.cell(row=r, column=7, value=round(rev, 2)).number_format = money_fmt
        # H: CPC = cost/clicks
        ws.cell(row=r, column=8, value=f"=IF(D{r}=0,0,E{r}/D{r})").number_format = money_fmt
        # I: CPL = cost/conv
        ws.cell(row=r, column=9, value=f"=IF(F{r}=0,0,E{r}/F{r})").number_format = money_fmt
        # J: RPL = revenue/conv
        ws.cell(row=r, column=10, value=f"=IF(F{r}=0,0,G{r}/F{r})").number_format = money_fmt
        # K: ROAS = revenue/cost
        ws.cell(row=r, column=11, value=f"=IF(E{r}=0,0,G{r}/E{r})").number_format = roas_fmt
        # L: GP = revenue - cost
        ws.cell(row=r, column=12, value=f"=G{r}-E{r}").number_format = money_fmt
        # M: CTR = clicks/impressions
        ws.cell(row=r, column=13, value=f"=IF(C{r}=0,0,D{r}/C{r})").number_format = pct_fmt
        # N: CvR = primary_conv/clicks
        ws.cell(row=r, column=14, value=f"=IF(D{r}=0,0,F{r}/D{r})").number_format = pct_fmt
        # O: Margin = gp/revenue.  References L (gp), so zero-guard on revenue (G).
        ws.cell(row=r, column=15, value=f"=IF(G{r}=0,0,L{r}/G{r})").number_format = pct_fmt

        # P–T: Mobile stored values (device='MOBILE' slice)
        ws.cell(row=r, column=16, value=m_imp).number_format = int_fmt
        ws.cell(row=r, column=17, value=m_clk).number_format = int_fmt
        ws.cell(row=r, column=18, value=round(m_cost, 2)).number_format = money_fmt
        ws.cell(row=r, column=19, value=round(m_conv, 2)).number_format = conv_fmt
        ws.cell(row=r, column=20, value=round(m_rev, 2)).number_format = money_fmt
        # U–Y: Non-Mobile = Total − Mobile (formulas, so the split
        # reconciles exactly by construction)
        ws.cell(row=r, column=21, value=f"=C{r}-P{r}").number_format = int_fmt
        ws.cell(row=r, column=22, value=f"=D{r}-Q{r}").number_format = int_fmt
        ws.cell(row=r, column=23, value=f"=E{r}-R{r}").number_format = money_fmt
        ws.cell(row=r, column=24, value=f"=F{r}-S{r}").number_format = conv_fmt
        ws.cell(row=r, column=25, value=f"=G{r}-T{r}").number_format = money_fmt
        # Z / AA: device gross profit (feeds Mobile / Non-Mobile GP and
        # Margin grids on the DOW sheet)
        ws.cell(row=r, column=26, value=f"=T{r}-R{r}").number_format = money_fmt
        ws.cell(row=r, column=27, value=f"=Y{r}-W{r}").number_format = money_fmt

        # Border + current-week tint
        for col_idx in range(1, 28):
            cell = ws.cell(row=r, column=col_idx)
            cell.font = body_font
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)
            if is_current_week:
                cell.fill = current_fill

        row_idx += 1
        cur_date += timedelta(days=1)

    last_row = row_idx - 1

    # Column widths
    ws.column_dimensions["A"].width = 12   # date
    ws.column_dimensions["B"].width = 8    # weekday
    for c in range(3, 28):
        ws.column_dimensions[get_column_letter(c)].width = 12
    ws.freeze_panes = ws.cell(row=5, column=3)  # freeze date + weekday

    return last_row


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — DAY-OF-WEEK GRID
# ═══════════════════════════════════════════════════════════════════

def write_dow_grid_sheet(ws, daily_sheet_name: str,
                          start_date: date, end_date: date,
                          max_data_date: date, last_complete_sun: date,
                          daily_first_row: int, daily_last_row: int,
                          label: str, full_week_total: bool = False):
    """Write a DOW_<code> sheet with thirteen stacked grids in three
    thematic blocks:

       Volume:           (A) Impressions     (B) Clicks     (C) CTR
                         (D) Primary Conv    (E) Revenue
       Cost/efficiency:  (F) Cost            (G) CPC        (H) CPL
                         (I) CvR
       Outcome:          (J) ROAS            (K) RPL        (L) GP
                         (M) Margin

    Grouping puts raw activity at the top, cost-side metrics in the
    middle, and outcome-side metrics at the bottom, so a top-to-bottom
    scan moves naturally from impressions through spend efficiency to
    profitability.

    Layout per grid:
      week_start | Mon | Tue | Wed | Thu | Fri | Sat | Sun | Total
      ...rows = ISO weeks (Mon-Sun)
      avg/weighted | <weighted column totals>
      WoW %        | <most recent complete week vs prior, per weekday>

    Day cells use SUMIFS to read the Daily sheet by date. The Total
    column (col I on the DOW sheet) is also a SUMIFS — same predicate
    extended to a 7-day range. For ratio grids, the per-day cell is
    SUMIFS(num)/SUMIFS(den) for that one day; the Total column applies
    the same SUMIFS pattern across the week range (i.e., a weighted
    week ratio). The bottom 'avg/weighted' row computes the same kind
    of aggregate across all weeks for a given weekday. The 'WoW %' row
    compares the two most recent COMPLETE weeks per weekday — the
    partial / current week is excluded so the comparison stays
    apples-to-apples; if the window doesn't contain two complete weeks
    the row renders blank.

    GP is a derived metric on Daily (`=revenue - cost`) but it remains
    linear, so SUMIFS over the GP column produces the same answer as
    SUMIFS(revenue) − SUMIFS(cost). Treating it as a 'sum' grid is
    therefore safe; the Margin grid reuses the same numerator.

    Each grid gets a line chart per device section, anchored two
    columns right of that section's Total column, plotting the
    section's weekly Total across all weeks with value-only data
    labels positioned above the line, plus a dashed grey reference
    line at the average of the plotted weeks (fed by a hidden helper
    column right of each Total column).

    MATCHED-DAY TOTALS. The 'Total' column covers Mon..(weekday of the
    latest data date) for every week — full Mon-Sun when the latest
    week is complete — so the current partial week reads like-for-like
    against history in both the grids and the charts.

    DEVICE SECTIONS. Every grid repeats three times side by side on the
    same rows: Total (device='ALL', columns A..I — unchanged layout),
    Mobile (device='MOBILE', columns V..AD), and Non-Mobile (columns
    AQ..AY). Mobile reads the m_* stored columns on the Daily sheet;
    Non-Mobile reads the nm_* formula columns, which are defined as
    Total − Mobile so the three sections reconcile exactly by
    construction (Non-Mobile = Desktop + Tablet). Each section's chart
    occupies the ~11-column gutter to its right (J..U, AE..AP, AZ..).
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
    total_col_fill = PatternFill("solid", fgColor=TOTAL_COL_TINT)

    ws.cell(row=1, column=1, value=f"Day-of-week grid: {label}").font = title_font
    ws.cell(row=2, column=1, value=(
        f"Thirteen grids in three blocks: Volume (Imp, Clicks, CTR, "
        f"Primary Conv, Revenue), Cost & efficiency (Cost, CPC, CPL, "
        f"CvR), Outcome (ROAS, RPL, GP, Margin). Use the 'Jump to:' "
        f"links below to navigate. Each row is one ISO week (Mon-Sun). "
        f"Scan a column to compare a single weekday across weeks. The "
        f"rightmost 'Total' column is MATCHED-DAY: every week sums (or "
        f"weight-averages, for ratios) Mon through the latest data "
        f"weekday, so the current partial week compares like-for-like "
        f"against prior weeks; when the latest week is complete this is "
        f"the full Mon-Sun week. 'avg/weighted' row "
        f"aggregates across all weeks for a single weekday — weighted "
        f"for ratios. 'WoW %' row beneath compares the two most recent "
        f"complete weeks; blank when the window has fewer than two "
        f"complete weeks. Negative WoW shifts render red. Each grid "
        f"repeats in three device sections side by side: Total (col A), "
        f"Mobile (col V), and Non-Mobile (col AQ, computed as Total "
        f"minus Mobile so the three always reconcile). A trend chart of "
        f"each section's weekly Total column follows that section's "
        f"data range; the dashed grey line is the average of the "
        f"plotted weeks.")).font = note_font

    # Build list of week-starting Mondays covered by the window.
    week_mondays = []
    cur_mon = start_date
    while cur_mon <= end_date:
        week_mondays.append(cur_mon)
        cur_mon += timedelta(days=7)
    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # ── Matched-day Total ──────────────────────────────────────────────
    # The 'Total' column sums Mon..(latest data weekday) for EVERY week,
    # not Mon..Sun. When data runs through, say, Wednesday, all weeks
    # total Mon–Wed, so the current partial week is directly comparable
    # to prior weeks (and to the average line on the trend charts). When
    # the latest week is complete the offset is 6 and totals are full
    # weeks, exactly as before.
    # full_week_total=True (--full-week-total on the CLI) overrides this
    # and forces the full Mon-Sun (+6) range regardless of partial data;
    # the current partial week's Total then simply covers whatever days
    # exist so far.
    if full_week_total:
        matched_offset = 6
    else:
        matched_offset = min(6, (max_data_date - week_mondays[-1]).days)
    total_header = ("Total" if matched_offset == 6
                    else f"Total (Mon–{WEEKDAY_NAMES[matched_offset]})")

    # SUMIFS range references on the Daily sheet
    date_range = (f"'{daily_sheet_name}'!$A${daily_first_row}:"
                  f"$A${daily_last_row}")
    weekday_range = (f"'{daily_sheet_name}'!$B${daily_first_row}:"
                     f"$B${daily_last_row}")

    def col_range(col_letter: str) -> str:
        return (f"'{daily_sheet_name}'!${col_letter}${daily_first_row}:"
                f"${col_letter}${daily_last_row}")

    def write_grid(start_row: int, metric_label: str, grid_kind: str,
                   number_format: str, colmap: dict, start_col: int = 1,
                   sum_key: str | None = None,
                   num_key: str | None = None,
                   den_key: str | None = None):
        """Write one grid anchored at (start_row, start_col). Returns the
        last row used (the WoW row).

        colmap maps metric keys ('imp', 'clk', 'cost', 'conv', 'rev',
        'gp') to Daily-sheet column letters for ONE device section, so
        the same spec list drives the Total, Mobile, and Non-Mobile
        grids — only the colmap (and start_col) changes.

        grid_kind:
          'sum'   — single-column SUMIFS using colmap[sum_key]
          'ratio' — SUMIFS(num) / SUMIFS(den), weighted at the SUMIFS
                    aggregation level so the 'Total' column and
                    'avg/weighted' row both stay correct
        """
        sum_col = colmap[sum_key] if sum_key else None
        num_col = colmap[num_key] if num_key else None
        den_col = colmap[den_key] if den_key else None

        wk_letter = get_column_letter(start_col)      # week_start column
        total_col_idx = start_col + 8                 # 'Total' column

        ws.cell(row=start_row, column=start_col,
                value=metric_label).font = section_font

        # Header row: week_start | Mon..Sun | Total
        header_row = start_row + 1
        headers_row = ["week_start"] + WEEKDAY_NAMES + [total_header]
        for offset, h in enumerate(headers_row):
            cell = ws.cell(row=header_row, column=start_col + offset, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Data rows: one per week. start_col+1..+7 are Mon..Sun;
        # start_col+8 is Total.
        for wk_idx, wk_mon in enumerate(week_mondays):
            r = header_row + 1 + wk_idx
            ws.cell(row=r, column=start_col,
                    value=wk_mon).number_format = "yyyy-mm-dd"
            is_current_week = wk_mon > last_complete_sun - timedelta(days=6)

            # Per-day cells (Mon..Sun)
            for wd in range(7):
                col_idx = start_col + 1 + wd
                date_ref = (f"${wk_letter}{r}+{wd}" if wd > 0
                            else f"${wk_letter}{r}")
                if grid_kind == "sum":
                    formula = (f"=SUMIFS({col_range(sum_col)},"
                               f"{date_range},{date_ref})")
                else:  # ratio
                    num = (f"SUMIFS({col_range(num_col)},"
                           f"{date_range},{date_ref})")
                    den = (f"SUMIFS({col_range(den_col)},"
                           f"{date_range},{date_ref})")
                    formula = f"=IFERROR({num}/{den},\"\")"
                cell = ws.cell(row=r, column=col_idx, value=formula)
                cell.number_format = number_format
                cell.font = body_font
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
                if is_current_week:
                    cell.fill = current_fill

            # Total column: same SUMIFS predicate over the MATCHED day
            # range Mon..+matched_offset (see matched-day note above),
            # so every week's Total covers the same weekdays as the
            # latest partial week. For ratios this is the weighted
            # matched-day ratio.
            week_start_ref = f"${wk_letter}{r}"
            week_end_ref = f"${wk_letter}{r}+{matched_offset}"
            if grid_kind == "sum":
                total_formula = (
                    f"=SUMIFS({col_range(sum_col)},"
                    f"{date_range},\">=\"&{week_start_ref},"
                    f"{date_range},\"<=\"&{week_end_ref})"
                )
            else:
                num_total = (
                    f"SUMIFS({col_range(num_col)},"
                    f"{date_range},\">=\"&{week_start_ref},"
                    f"{date_range},\"<=\"&{week_end_ref})"
                )
                den_total = (
                    f"SUMIFS({col_range(den_col)},"
                    f"{date_range},\">=\"&{week_start_ref},"
                    f"{date_range},\"<=\"&{week_end_ref})"
                )
                total_formula = f"=IFERROR({num_total}/{den_total},\"\")"
            cell = ws.cell(row=r, column=total_col_idx, value=total_formula)
            cell.number_format = number_format
            cell.font = body_font
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)
            # Light blue tint on the Total column, unless this week is
            # current (amber takes priority).
            if is_current_week:
                cell.fill = current_fill
            else:
                cell.fill = total_col_fill

        # Bottom 'avg / weighted' row — aggregate across all weeks for
        # a given weekday. AVERAGEIFS for sums, SUMIFS/SUMIFS for ratios.
        totals_row = header_row + 1 + len(week_mondays)
        ws.cell(row=totals_row, column=start_col,
                value="avg / weighted").font = totals_font
        ws.cell(row=totals_row, column=start_col).fill = totals_fill

        for wd in range(7):
            wd_name = WEEKDAY_NAMES[wd]
            if grid_kind == "sum":
                formula = (
                    f"=IFERROR(AVERAGEIFS({col_range(sum_col)},"
                    f"{weekday_range},\"{wd_name}\"),\"\")"
                )
            else:
                num = (f"SUMIFS({col_range(num_col)},"
                       f"{weekday_range},\"{wd_name}\")")
                den = (f"SUMIFS({col_range(den_col)},"
                       f"{weekday_range},\"{wd_name}\")")
                formula = f"=IFERROR({num}/{den},\"\")"
            cell = ws.cell(row=totals_row, column=start_col + 1 + wd,
                           value=formula)
            cell.number_format = number_format
            cell.font = totals_font
            cell.fill = totals_fill
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)

        # Grand total cell (totals row × Total column), consistent with
        # the matched-day Total column above: for sums, the AVERAGE of
        # the per-week matched-day totals; for ratios, weighted across
        # the window restricted to the matched weekdays (WEEKDAY(,2)
        # returns Mon=1..Sun=7).
        tot_letter = get_column_letter(total_col_idx)
        tot_first = header_row + 1
        tot_last = header_row + len(week_mondays)
        tot_cells = f"{tot_letter}{tot_first}:{tot_letter}{tot_last}"
        if grid_kind == "sum":
            grand_formula = f"=IFERROR(AVERAGE({tot_cells}),\"\")"
        else:
            wk_cond = (f"(WEEKDAY({date_range},2)<={matched_offset + 1})")
            num_all = f"SUMPRODUCT({wk_cond}*{col_range(num_col)})"
            den_all = f"SUMPRODUCT({wk_cond}*{col_range(den_col)})"
            grand_formula = f"=IFERROR({num_all}/{den_all},\"\")"
        cell = ws.cell(row=totals_row, column=total_col_idx,
                       value=grand_formula)
        cell.number_format = number_format
        cell.font = totals_font
        cell.fill = totals_fill
        cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                             left=THIN_BORDER, right=THIN_BORDER)

        # ── WoW % row ──────────────────────────────────────────────────
        # One row beneath the totals row. Compares the two most recent
        # COMPLETE weeks per weekday (and per Total). Partial / current
        # week is skipped so the comparison stays apples-to-apples.
        # Recent week's Monday = last_complete_sun - 6 days; prior =
        # recent - 7. If either Monday is outside the window the cells
        # render blank rather than wrong.
        wow_row = totals_row + 1
        recent_mon = last_complete_sun - timedelta(days=6)
        prior_mon = recent_mon - timedelta(days=7)
        recent_idx = next(
            (i for i, m in enumerate(week_mondays) if m == recent_mon), None
        )
        prior_idx = next(
            (i for i, m in enumerate(week_mondays) if m == prior_mon), None
        )

        ws.cell(row=wow_row, column=start_col, value="WoW %").font = totals_font
        ws.cell(row=wow_row, column=start_col).fill = totals_fill
        ws.cell(row=wow_row, column=start_col).border = Border(
            top=THIN_BORDER, bottom=THIN_BORDER,
            left=THIN_BORDER, right=THIN_BORDER,
        )

        # Per-weekday cols + Total col — 8 cells starting one right of
        # the week_start column.
        wow_col_letters = [get_column_letter(start_col + 1 + i)
                           for i in range(8)]
        if recent_idx is not None and prior_idx is not None:
            recent_data_row = header_row + 1 + recent_idx
            prior_data_row = header_row + 1 + prior_idx
            for col_offset, col_letter in enumerate(wow_col_letters):
                col_idx = start_col + 1 + col_offset
                recent_ref = f"{col_letter}{recent_data_row}"
                prior_ref = f"{col_letter}{prior_data_row}"
                # N() coerces blank-strings (returned by IFERROR on ratio
                # cells with zero denominator) to 0 so arithmetic works.
                # Outer IFERROR catches the case where prior is itself blank.
                formula = (f"=IFERROR(IF(N({prior_ref})=0,\"\","
                           f"(N({recent_ref})-N({prior_ref}))/N({prior_ref})),\"\")")
                cell = ws.cell(row=wow_row, column=col_idx, value=formula)
                cell.number_format = "0.0%;[Red]-0.0%"
                cell.font = totals_font
                cell.fill = totals_fill
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
        else:
            # Not enough complete weeks — leave the WoW cells empty but
            # styled so the row is visually present.
            for col_offset in range(len(wow_col_letters)):
                col_idx = start_col + 1 + col_offset
                cell = ws.cell(row=wow_row, column=col_idx, value="")
                cell.fill = totals_fill
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)

        return wow_row

    # Per-device-section column maps into the Daily sheet. Each maps the
    # six base metric keys to the Daily column letters for that device
    # slice. DAILY_COLS is the single source of truth (top of file).
    SEC_TOTAL_COLS = {
        "imp": DAILY_COLS["impressions"], "clk": DAILY_COLS["clicks"],
        "cost": DAILY_COLS["cost_usd"], "conv": DAILY_COLS["primary_conv"],
        "rev": DAILY_COLS["revenue"], "gp": DAILY_COLS["gp"],
    }
    SEC_MOBILE_COLS = {
        "imp": DAILY_COLS["m_impressions"], "clk": DAILY_COLS["m_clicks"],
        "cost": DAILY_COLS["m_cost_usd"], "conv": DAILY_COLS["m_primary_conv"],
        "rev": DAILY_COLS["m_revenue"], "gp": DAILY_COLS["m_gp"],
    }
    SEC_NONMOBILE_COLS = {
        "imp": DAILY_COLS["nm_impressions"], "clk": DAILY_COLS["nm_clicks"],
        "cost": DAILY_COLS["nm_cost_usd"], "conv": DAILY_COLS["nm_primary_conv"],
        "rev": DAILY_COLS["nm_revenue"], "gp": DAILY_COLS["nm_gp"],
    }

    # Three side-by-side device sections per grid row, each followed by
    # an ~11-column chart gutter holding that section's trend chart:
    #   Total      A..I    → chart in J..U   (anchored K)
    #   Mobile     V..AD   → chart in AE..AP (anchored AF)
    #   Non-Mobile AQ..AY  → chart in AZ..~BK (anchored BA)
    sections = [
        ("Total", 1, SEC_TOTAL_COLS),            # A..I
        ("Mobile", 22, SEC_MOBILE_COLS),         # V..AD
        ("Non-Mobile", 43, SEC_NONMOBILE_COLS),  # AQ..AY
    ]

    INT_FMT = "#,##0"
    MONEY_FMT = '"$"#,##0.00'
    CONV_FMT = "#,##0.00"
    ROAS_FMT = "0.00"
    PCT_FMT = "0.00%"

    # Thirteen grids in three thematic blocks. Each spec is a tuple of
    # (section_label, nav_link_text, kind, number_format, kwargs_for_write_grid).
    # kwargs reference metric KEYS ('imp', 'cost', ...) which write_grid
    # resolves against the active device section's column map, so every
    # spec drives all three sections.
    # The nav_link_text is the short label that appears in the "Jump to:"
    # block at the top of the sheet — kept short so it doesn't truncate in
    # the column-A-width-14 layout.
    #
    # Block 1 — Volume:    Imp, Clicks, CTR, Primary Conv, Revenue
    # Block 2 — Cost/eff:  Cost, CPC, CPL, CvR
    # Block 3 — Outcome:   ROAS, RPL, GP, Margin
    grid_specs = [
        ("A — Impressions",         "Impressions",  "sum",   INT_FMT,   {"sum_key": "imp"}),
        ("B — Clicks",              "Clicks",       "sum",   INT_FMT,   {"sum_key": "clk"}),
        ("C — CTR (weighted)",      "CTR",          "ratio", PCT_FMT,   {"num_key": "clk",  "den_key": "imp"}),
        ("D — Primary Conversions", "Primary Conv", "sum",   CONV_FMT,  {"sum_key": "conv"}),
        ("E — Revenue",             "Revenue",      "sum",   MONEY_FMT, {"sum_key": "rev"}),
        ("F — Cost",                "Cost",         "sum",   MONEY_FMT, {"sum_key": "cost"}),
        ("G — CPC (weighted)",      "CPC",          "ratio", MONEY_FMT, {"num_key": "cost", "den_key": "clk"}),
        ("H — CPL (weighted)",      "CPL",          "ratio", MONEY_FMT, {"num_key": "cost", "den_key": "conv"}),
        ("I — CvR (weighted)",      "CvR",          "ratio", PCT_FMT,   {"num_key": "conv", "den_key": "clk"}),
        ("J — ROAS (weighted)",     "ROAS",         "ratio", ROAS_FMT,  {"num_key": "rev",  "den_key": "cost"}),
        ("K — RPL (weighted)",      "RPL",          "ratio", MONEY_FMT, {"num_key": "rev",  "den_key": "conv"}),
        ("L — GP",                  "GP",           "sum",   MONEY_FMT, {"sum_key": "gp"}),
        ("M — Margin (weighted)",   "Margin",       "ratio", PCT_FMT,   {"num_key": "gp",   "den_key": "rev"}),
    ]

    # ── "Jump to:" navigation block ────────────────────────────────────
    # Each grid occupies: 1 label row + 1 header row + N data rows
    # (where N = len(week_mondays)) + 1 totals row + 1 WoW row = N+4
    # rows, with 2 blank spacer rows between consecutive labels. So the
    # row-stride between consecutive label rows is N+6.
    #
    # We pre-compute where each grid label will land, write the nav
    # block at the top, then write the grids starting at FIRST_LABEL_ROW.
    # That row depends on how many nav rows we need (one row per 4 links
    # plus the "Jump to:" header and a blank spacer).
    n_links = len(grid_specs)
    LINKS_PER_NAV_ROW = 4
    n_nav_rows = (n_links + LINKS_PER_NAV_ROW - 1) // LINKS_PER_NAV_ROW
    # Nav layout: row 3 = "Jump to:" header; rows 4..(3+n_nav_rows) = links;
    # one blank spacer row; then first grid label.
    FIRST_LABEL_ROW = 3 + n_nav_rows + 2  # header_row + n_nav_rows + blank
    label_stride = len(week_mondays) + 6

    label_rows = [FIRST_LABEL_ROW + i * label_stride for i in range(n_links)]

    nav_header_font = Font(bold=True, size=11, color="305496", name="Arial")
    link_font = Font(name="Arial", color="0563C1", underline="single")
    annot_font = Font(name="Arial", color="595959", italic=True, size=9)

    ws.cell(row=3, column=1, value="Jump to:").font = nav_header_font

    # Pack links into a grid: 4 (link, annotation) pairs per nav row, so
    # 8 columns used. The link sits in cols 1,3,5,7; the "row N" annotation
    # in cols 2,4,6,8. (Matches the Microsoft layout.)
    for i, (spec, target_row) in enumerate(zip(grid_specs, label_rows)):
        _, nav_text, _, _, _ = spec
        nav_row = 4 + (i // LINKS_PER_NAV_ROW)
        slot_in_row = i % LINKS_PER_NAV_ROW
        link_col = 1 + slot_in_row * 2
        annot_col = link_col + 1

        # HYPERLINK formula: target syntax is "#'SheetName'!A<row>".
        # Quote-escape the sheet name; ws.title is the live sheet name.
        sheet_ref = ws.title.replace("'", "''")  # SQL-style quote-escape
        formula = f'=HYPERLINK("#\'{sheet_ref}\'!A{target_row}","{nav_text}")'
        cell = ws.cell(row=nav_row, column=link_col, value=formula)
        cell.font = link_font

        annot_cell = ws.cell(row=nav_row, column=annot_col, value=f"row {target_row}")
        annot_cell.font = annot_font

    # ── Grids ──────────────────────────────────────────────────────────
    # Write each grid at its pre-computed label row — once per device
    # section, side by side. The Total section keeps the bare metric
    # label (matches the nav links); Mobile / Non-Mobile prefix theirs
    # so a horizontal scroll always tells you which slice you're in.
    last_row = None
    for (label, _, kind, fmt, kwargs), label_row in zip(grid_specs, label_rows):
        for sec_name, sec_start_col, sec_cols in sections:
            sec_label = label if sec_name == "Total" else f"{label} — {sec_name}"
            last_row = write_grid(label_row, sec_label, kind, fmt,
                                  colmap=sec_cols, start_col=sec_start_col,
                                  **kwargs)

    # ── Trend charts ───────────────────────────────────────────────────
    # One line chart per grid PER DEVICE SECTION, anchored two columns
    # right of the section's Total column, level with the grid's section
    # label. Each plots that section's weekly "Total" column across all
    # weeks in the window — complete + current partial — with value-only
    # labels rendered ABOVE the line. Categories are the section's
    # week_start dates. Chart height is derived from the grid stride so
    # consecutive charts never overlap regardless of --weeks.
    n_weeks = len(week_mondays)
    # One grid stride = n_weeks + 6 rows (label + header + weeks + totals
    # + WoW + 2 spacers). Default Excel row height ≈ 0.5 cm; leave one
    # row of breathing room at the bottom of each chart.
    chart_height_cm = max(6.0, (label_stride - 1) * 0.5)
    chart_width_cm = 16.0

    for (label, nav_text, kind, fmt, kwargs), label_row in zip(grid_specs, label_rows):
        grid_header_row = label_row + 1
        first_data_row = grid_header_row + 1
        last_data_row = grid_header_row + n_weeks

        for sec_name, sec_start_col, _sec_cols in sections:
            total_col_idx = sec_start_col + 8
            avg_col_idx = sec_start_col + 9          # hidden helper column
            anchor_col = get_column_letter(sec_start_col + 10)
            tot_letter = get_column_letter(total_col_idx)

            # Helper column: the average of this grid's plotted weekly
            # Totals, repeated for every week row so it draws as a flat
            # reference line across the chart. AVERAGE ignores the ""
            # strings that ratio Totals produce on zero denominators.
            # The column is hidden (see below); charts still read it
            # because visible_cells_only=False.
            avg_ref = (f"=IFERROR(AVERAGE(${tot_letter}${first_data_row}:"
                       f"${tot_letter}${last_data_row}),\"\")")
            ws.cell(row=grid_header_row, column=avg_col_idx, value="Average")
            for rr in range(first_data_row, last_data_row + 1):
                c = ws.cell(row=rr, column=avg_col_idx, value=avg_ref)
                c.number_format = fmt

            chart = LineChart()
            if sec_name == "Total":
                chart.title = f"{nav_text} — weekly Total"
            else:
                chart.title = f"{nav_text} — {sec_name} weekly Total"
            chart.style = 12
            chart.height = chart_height_cm
            chart.width = chart_width_cm
            chart.legend = None
            chart.y_axis.majorGridlines = None
            chart.y_axis.numFmt = fmt
            chart.x_axis.numFmt = "yyyy-mm-dd"
            # openpyxl quirk: without delete=False Excel sometimes hides
            # the axes on generated line charts.
            chart.y_axis.delete = False
            chart.x_axis.delete = False
            # The Average series lives in a hidden helper column — keep
            # plotting it.
            chart.visible_cells_only = False

            # Series 1 — this section's weekly Total. Include the header
            # cell so the series is named after the Total column.
            data_ref = Reference(ws, min_col=total_col_idx,
                                 max_col=total_col_idx,
                                 min_row=grid_header_row,
                                 max_row=last_data_row)
            chart.add_data(data_ref, titles_from_data=True)
            cats_ref = Reference(ws, min_col=sec_start_col,
                                 max_col=sec_start_col,
                                 min_row=first_data_row,
                                 max_row=last_data_row)
            chart.set_categories(cats_ref)

            series = chart.series[0]
            series.smooth = False
            series.dLbls = DataLabelList()
            series.dLbls.showVal = True           # value ONLY —
            series.dLbls.showSerName = False      # no series name,
            series.dLbls.showCatName = False      # no category name,
            series.dLbls.showLegendKey = False    # no legend key,
            series.dLbls.showPercent = False
            series.dLbls.showBubbleSize = False
            series.dLbls.position = "t"          # labels ABOVE the trend line
            series.dLbls.numFmt = fmt

            # Series 2 — flat Average reference line (dashed grey, no
            # markers, no data labels) on the same axis, so daily
            # movement reads against the observed-window average.
            avg_data = Reference(ws, min_col=avg_col_idx,
                                 max_col=avg_col_idx,
                                 min_row=grid_header_row,
                                 max_row=last_data_row)
            chart.add_data(avg_data, titles_from_data=True)
            avg_series = chart.series[1]
            avg_series.smooth = False
            avg_series.graphicalProperties = GraphicalProperties(
                ln=LineProperties(solidFill="808080", w=15875,
                                  prstDash="dash"))

            ws.add_chart(chart, f"{anchor_col}{label_row}")

    # Hide the Average helper columns (one per section, immediately
    # right of each Total column). visible_cells_only=False above keeps
    # the reference line plotting from hidden cells.
    for _, sec_start_col, _ in sections:
        ws.column_dimensions[get_column_letter(sec_start_col + 9)].hidden = True

    # Column widths — repeat the width pattern (week_start=14, days=11,
    # Total=13) for each device section.
    for _, sec_start_col, _ in sections:
        ws.column_dimensions[get_column_letter(sec_start_col)].width = 14
        for c in range(sec_start_col + 1, sec_start_col + 8):
            ws.column_dimensions[get_column_letter(c)].width = 11
        ws.column_dimensions[get_column_letter(sec_start_col + 8)].width = 13
    # Freeze just below the nav block so "Jump to:" links stay visible
    # while a reviewer scrolls through grids.
    ws.freeze_panes = ws.cell(row=FIRST_LABEL_ROW, column=2)


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
            value="Google Home Insurance — Performance Data Tables").font = title_font

    ws.cell(row=3, column=1, value="Window start (Mon):").font = label_font
    ws.cell(row=3, column=2, value=str(window_start)).font = body_font
    ws.cell(row=4, column=1, value="Last complete Sunday:").font = label_font
    ws.cell(row=4, column=2, value=str(last_complete_sun)).font = body_font
    ws.cell(row=5, column=1, value="Latest data date:").font = label_font
    ws.cell(row=5, column=2, value=str(max_data_date)).font = body_font
    ws.cell(row=6, column=1, value="End-date source:").font = label_font
    ws.cell(row=6, column=2, value=source_note).font = body_font

    ws.cell(row=8, column=1, value=(
        "Daily sheets carry 13 metrics (imp, clicks, cost, primary_conv, "
        "revenue + derived cpc, cpl, rpl, roas, gp, ctr, cvr, margin). "
        "DOW sheets show all thirteen of those metrics broken out by weekday × "
        "week, with a Total column (weighted for ratios) and a WoW % row "
        "comparing the two most recent complete weeks. All conversion "
        "figures are routed primary only — sourced from "
        "keyword_performance_daily, not the unrouted columns on "
        "campaign_performance_daily_search.")
    ).font = note_font

    # Sheet index header
    hdr_row = 10
    for c, h in enumerate(["short_code", "scope", "status",
                            "campaign_name", "current_tcpa"], 1):
        cell = ws.cell(row=hdr_row, column=c, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

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

    widths = [14, 16, 10, 60, 13]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ═══════════════════════════════════════════════════════════════════
# WORKBOOK ASSEMBLY
# ═══════════════════════════════════════════════════════════════════

def build_workbook(conn, window_start: date, last_complete_sun: date,
                   max_data_date: date, source_note: str,
                   full_week_total: bool = False) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)

    camp_meta = fetch_campaigns(conn)
    print(f"  {len(camp_meta)} ENABLED+PAUSED campaigns")

    daily_all = fetch_daily_metrics(conn, window_start, max_data_date)
    if daily_all.empty:
        raise RuntimeError("No daily metrics in the window — nothing to write")

    # Account-level: sum per date across all enabled+paused campaigns.
    # We're rolling up rows that already use the routed conv columns
    # (fetch_daily_metrics joined them in), so the account totals also
    # use routed conversions. Don't read account-level conv columns
    # directly from campaign_performance_daily_search — those are unrouted.
    account_daily = (
        daily_all.groupby("date", as_index=False)
                 .agg(impressions=("impressions", "sum"),
                      clicks=("clicks", "sum"),
                      cost_usd=("cost_usd", "sum"),
                      primary_conversions=("primary_conversions", "sum"),
                      primary_conv_value=("primary_conv_value", "sum"),
                      m_impressions=("m_impressions", "sum"),
                      m_clicks=("m_clicks", "sum"),
                      m_cost_usd=("m_cost_usd", "sum"),
                      m_primary_conversions=("m_primary_conversions", "sum"),
                      m_primary_conv_value=("m_primary_conv_value", "sum"))
    )

    code_to_meta = {
        "Account": {"campaign_name": "ALL enabled+paused campaigns (sum)",
                    "status": "—", "current_tcpa": None},
    }

    # Account sheets (2: Daily + DOW)
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
        daily_first_row, daily_last_row, label="Account",
        full_week_total=full_week_total,
    )
    print("  ✓ DOW_Account")

    # Per-campaign sheets (2 each: Daily + DOW)
    written_codes = {"Account"}
    for _, cm in camp_meta.iterrows():
        cid = cm["campaign_id"]
        cname = cm["campaign_name"]
        status = cm["status"]
        camp_daily = daily_all[daily_all["campaign_id"] == cid].drop(columns=["campaign_id"])
        if camp_daily["impressions"].sum() == 0 and camp_daily["clicks"].sum() == 0:
            print(f"  (skip: {cname} — no data in window)")
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
        ws_g = wb.create_sheet(f"DOW_{code}")
        write_dow_grid_sheet(
            ws_g, f"Daily_{code}", window_start, max_data_date,
            max_data_date, last_complete_sun,
            5, last_row, label=cname,
            full_week_total=full_week_total,
        )
        print(f"  ✓ Daily_{code} / DOW_{code}  ({cname})")

    ws_idx = wb.create_sheet("Index", 0)
    write_index_sheet(ws_idx, code_to_meta, window_start,
                       last_complete_sun, max_data_date, source_note)
    print("  ✓ Index")

    # ── Sheet re-order: Index | all DOW | all Daily ────────────────────
    # Sheets are created Daily-then-DOW per scope because the DOW SUMIFS
    # formulas need the Daily sheet's row extent. Reorder once here so
    # every DOW sheet sits side by side immediately after the Index.
    # Relative order within each group is preserved (Account first, then
    # campaigns in creation order). Cross-sheet formula references are by
    # name, so reordering is purely cosmetic and safe.
    dow_sheets = [s for s in wb.worksheets if s.title.startswith("DOW_")]
    daily_sheets = [s for s in wb.worksheets if s.title.startswith("Daily_")]
    wb._sheets = [wb["Index"]] + dow_sheets + daily_sheets
    print("  ✓ Sheet order: Index | DOW×{} | Daily×{}".format(
        len(dow_sheets), len(daily_sheets)))

    return wb


# ═══════════════════════════════════════════════════════════════════
# RECONCILIATION CHECK
# ═══════════════════════════════════════════════════════════════════

def run_reconciliation(conn, max_data_date: date) -> None:
    """Print a last-30-day reconciliation between the two spend routes
    plus the routed conversion totals. See the module docstring for the
    full explanation of why both routes exist.

    The two spend routes should agree within 0.01% on the home account.
    Disagreement of >0.01% means something is off with the
    keyword_performance_daily join (most often a stale or partial sync
    where one table is ahead of the other).
    """
    window_end = max_data_date
    window_start = window_end - timedelta(days=29)

    cur = conn.execute(
        """
        SELECT SUM(cost_micros) / 1.0e6 AS cost_usd
        FROM campaign_performance_daily_search cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ?
          AND cpd.device = 'ALL'
          AND cpd.date BETWEEN ? AND ?
        """,
        (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
    )
    row = cur.fetchone()
    cpd_spend = float(row[0] or 0.0)

    cur = conn.execute(
        """
        SELECT
            SUM(kpd.cost_micros) / 1.0e6 AS cost_usd,
            SUM(kpd.primary_conv_count) AS conv,
            SUM(kpd.primary_conv_value) AS rev
        FROM keyword_performance_daily kpd
        JOIN campaigns c ON c.campaign_id = kpd.campaign_id
        WHERE c.account_id = ?
          AND kpd.device = 'ALL'
          AND kpd.report_date BETWEEN ? AND ?
        """,
        (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
    )
    row = cur.fetchone()
    kpd_spend = float(row[0] or 0.0)
    primary_conv = float(row[1] or 0.0)
    primary_rev = float(row[2] or 0.0)

    if cpd_spend == 0 and kpd_spend == 0:
        pct_diff = 0.0
    elif cpd_spend == 0:
        pct_diff = float("inf")
    else:
        pct_diff = abs(kpd_spend - cpd_spend) / cpd_spend * 100.0

    roas = (primary_rev / cpd_spend) if cpd_spend > 0 else 0.0

    # Mobile slice — sanity check for the DOW device-split sections.
    # The DB architect confirmed (2026-07) that keyword_performance_daily
    # carries complete per-device rows, and that kpd has NO account_id
    # column: scoping must go through a join. We therefore probe mobile
    # routed conversions through BOTH valid routes —
    #   A: kpd.campaign_id → campaigns (the route the workbook fetch uses)
    #   B: kpd.instance_id → keyword_instances (the route the architect
    #      used to verify the data exists)
    # — so if the workbook's mobile columns ever come back zero, this
    # output localizes the failing linkage instead of guessing.
    cur = conn.execute(
        """
        SELECT SUM(cpd.cost_micros) / 1.0e6
        FROM campaign_performance_daily_search cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ?
          AND cpd.device = 'MOBILE'
          AND cpd.date BETWEEN ? AND ?
        """,
        (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
    )
    mob_cpd_spend = float(cur.fetchone()[0] or 0.0)

    def _probe(sql_text):
        try:
            cur = conn.execute(
                sql_text,
                (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
            )
            return float(cur.fetchone()[0] or 0.0), None
        except sqlite3.Error as exc:
            return None, str(exc)

    mob_conv_a, err_a = _probe(
        """
        SELECT SUM(kpd.primary_conv_count)
        FROM keyword_performance_daily kpd
        JOIN campaigns c ON c.campaign_id = kpd.campaign_id
        WHERE c.account_id = ?
          AND kpd.device = 'MOBILE'
          AND kpd.report_date BETWEEN ? AND ?
        """
    )
    mob_conv_b, err_b = _probe(
        """
        SELECT SUM(kpd.primary_conv_count)
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ?
          AND kpd.device = 'MOBILE'
          AND kpd.report_date BETWEEN ? AND ?
        """
    )
    mob_conv = mob_conv_a if mob_conv_a is not None else (mob_conv_b or 0.0)
    mob_share = (mob_cpd_spend / cpd_spend * 100.0) if cpd_spend > 0 else 0.0

    # Device-value probe: DISTINCT device values with row counts, scoped
    # through the instances join per the architect (falls back to the
    # campaigns join if keyword_instances is absent in this DB copy).
    dev_probe = {}
    cur = conn.execute(
        """
        SELECT t.device, COUNT(*)
        FROM campaign_performance_daily_search t
        JOIN campaigns c ON c.campaign_id = t.campaign_id
        WHERE c.account_id = ? AND t.date BETWEEN ? AND ?
        GROUP BY t.device ORDER BY t.device
        """,
        (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
    )
    dev_probe["campaign_performance_daily_search"] = (cur.fetchall(), "campaigns join")
    try:
        cur = conn.execute(
            """
            SELECT t.device, COUNT(*)
            FROM keyword_performance_daily t
            JOIN keyword_instances ki ON ki.id = t.instance_id
            WHERE ki.account_id = ? AND t.report_date BETWEEN ? AND ?
            GROUP BY t.device ORDER BY t.device
            """,
            (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
        )
        dev_probe["keyword_performance_daily"] = (cur.fetchall(), "instances join")
    except sqlite3.Error:
        cur = conn.execute(
            """
            SELECT t.device, COUNT(*)
            FROM keyword_performance_daily t
            JOIN campaigns c ON c.campaign_id = t.campaign_id
            WHERE c.account_id = ? AND t.report_date BETWEEN ? AND ?
            GROUP BY t.device ORDER BY t.device
            """,
            (ACCOUNT_ID, window_start.isoformat(), window_end.isoformat()),
        )
        dev_probe["keyword_performance_daily"] = (
            cur.fetchall(), "campaigns join (keyword_instances unavailable)")

    print()
    print("─" * 70)
    print(f"RECONCILIATION — last 30 days ({window_start} → {window_end})")
    print("─" * 70)
    print(f"  Spend (campaign_performance_daily_search): ${cpd_spend:>14,.2f}")
    print(f"  Spend (keyword_performance_daily):  ${kpd_spend:>14,.2f}")
    print(f"  Δ%:                                  {pct_diff:>14.4f}%   "
          f"{'(OK)' if pct_diff <= 0.01 else '(WARN: >0.01% — investigate)'}")
    print(f"  Primary conversions (routed):        {primary_conv:>14,.2f}")
    print(f"  Primary conv value (routed):        ${primary_rev:>14,.2f}")
    print(f"  ROAS (rev / cpd-spend):              {roas:>14,.2f}")
    print(f"  Mobile spend (cpd, device=MOBILE):  ${mob_cpd_spend:>14,.2f}"
          f"   ({mob_share:.1f}% of total)")
    conv_a = (f"{mob_conv_a:,.2f}" if mob_conv_a is not None
              else f"ERROR: {err_a}")
    conv_b = (f"{mob_conv_b:,.2f}" if mob_conv_b is not None
              else f"ERROR: {err_b}")
    print(f"  Mobile primary conv via campaign_id→campaigns:     {conv_a}")
    print(f"  Mobile primary conv via instance_id→keyword_instances: {conv_b}")
    if (mob_conv_a is not None and mob_conv_b is not None
            and abs(mob_conv_a - mob_conv_b) > 0.01):
        print("    ⚠ The two routes DISAGREE — the campaign_id linkage on "
              "kpd device rows is incomplete. The workbook fetch scopes "
              "via campaign_id, so its Mobile columns understate by the "
              "difference. Investigate before trusting the Mobile section.")
    elif mob_cpd_spend > 0 and mob_conv == 0:
        print("    ⚠ Mobile spend exists but neither route finds mobile "
              "routed conversions in this window — contradicts the "
              "architect-verified table contents. Check the window dates "
              "and device labels below before anything else.")
    print("  Device values present (last 30d):")
    for tbl, (rows, route) in dev_probe.items():
        vals = ", ".join(f"{d or 'NULL'}×{n:,}" for d, n in rows) or "(none)"
        print(f"    {tbl} [{route}]: {vals}")
    print("─" * 70)


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
    p.add_argument("--full-week-total", action="store_true",
                   help="Override matched-day totals: the DOW 'Total' "
                        "column always covers the full Mon-Sun week "
                        "(week_start + 6), even when the latest week is "
                        "partial. Default behavior matches every week's "
                        "Total to the latest data weekday.")
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)

    banner("CAMPAIGN PERFORMANCE DATA TABLES — Google Home")
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
    if args.full_week_total:
        print("  Total column:        FULL WEEK (override; Mon-Sun +6)")
    wb = build_workbook(conn, window_start, last_complete_sun,
                        max_data_date, source_note,
                        full_week_total=args.full_week_total)

    run_date = max_data_date.isoformat()
    output_path = output_dir / f"campaign_performance_data_google_{run_date}.xlsx"
    wb.save(output_path)
    print()
    print(f"  ✓ Wrote {output_path}")

    run_reconciliation(conn, max_data_date)

    conn.close()


if __name__ == "__main__":
    main()
