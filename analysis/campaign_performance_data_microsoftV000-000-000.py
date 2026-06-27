"""
Campaign Performance Data Tables — Microsoft Home Insurance
═══════════════════════════════════════════════════════════════════════
Produces a data-tables workbook (separate from the chart workbook):

  - Daily_<code>: raw daily impressions / clicks / cost / CPC for the
    last 12 complete Mon-Sun weeks plus all days available in the
    current partial week. No backfill buffer applied (impressions /
    clicks / cost finalize within hours; only conversions need the
    buffer — none in this workbook).
  - Roll7_<code>: trailing-7-day window of the same four metrics,
    computed via Excel formulas that reference the Daily sheet. Each
    row is anchored at one date and sums the prior 7 days inclusive.
    CPC is weighted (sum_cost / sum_clicks), matching how the
    visualization script aggregates ratios.
  - DOW_<code>: weeks-as-rows × weekdays-as-columns matrix, three
    stacked grids per sheet (impressions, clicks, CPC). Scan a single
    weekday column to compare across weeks.

Scope: account-level (sum across all ENABLED + PAUSED campaigns) plus
one set of sheets per enabled-or-paused campaign that has data in the
window. Short codes are used because Excel sheet names cap at 31 chars
and disallow several characters present in campaign names. An Index
sheet at the front maps short code → full campaign name.

Spend source is campaign_performance_daily (search) +
campaign_performance_network_daily (audience), summed per date. CPC
uses all-network clicks from both tables.

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
        "SELECT MAX(date) FROM campaign_performance_daily WHERE account_id = ?",
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
    """Per (campaign_id, date) impressions / clicks / cost, all-network.

    Spend and clicks come from campaign_performance_daily (search) +
    campaign_performance_network_daily (audience), summed.

    Impressions: same — both tables tracked separately, summed.

    Returns columns:
        campaign_id, date, impressions, clicks, cost_usd
    """
    sql_search = """
    SELECT
        cpd.campaign_id AS campaign_id,
        cpd.date AS date,
        SUM(cpd.impressions) AS s_impressions,
        SUM(cpd.clicks) AS s_clicks,
        SUM(cpd.cost_micros) / 1.0e6 AS s_cost
    FROM campaign_performance_daily cpd
    JOIN campaigns c ON c.campaign_id = cpd.campaign_id
    WHERE c.account_id = ?
      AND cpd.date BETWEEN ? AND ?
    GROUP BY cpd.campaign_id, cpd.date
    """
    df_search = pd.read_sql(sql_search, conn, params=(
        ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()
    ))

    sql_audience = """
    SELECT
        cpnd.campaign_id AS campaign_id,
        cpnd.date AS date,
        SUM(cpnd.impressions) AS a_impressions,
        SUM(cpnd.clicks) AS a_clicks,
        SUM(cpnd.cost_micros) / 1.0e6 AS a_cost
    FROM campaign_performance_network_daily cpnd
    JOIN campaigns c ON c.campaign_id = cpnd.campaign_id
    WHERE c.account_id = ?
      AND cpnd.date BETWEEN ? AND ?
    GROUP BY cpnd.campaign_id, cpnd.date
    """
    df_audience = pd.read_sql(sql_audience, conn, params=(
        ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()
    ))

    df = df_search.merge(df_audience, on=["campaign_id", "date"], how="outer").fillna(0.0)
    df["impressions"] = (df["s_impressions"] + df["a_impressions"]).astype(int)
    df["clicks"] = (df["s_clicks"] + df["a_clicks"]).astype(int)
    df["cost_usd"] = df["s_cost"] + df["a_cost"]
    return df[["campaign_id", "date", "impressions", "clicks", "cost_usd"]]


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
    """Write a Daily_<code> sheet.

    daily_df expected columns: date, impressions, clicks, cost_usd
    Rows are filled in for EVERY date in the [start_date, end_date] range
    even where source data is missing (zeros). This makes the rolling-7d
    formulas and DOW grid simple — no date gaps.

    end_date is max_data_date (the most recent day of any data).

    Layout:
      Row 1: title
      Row 2: window note
      Row 3: blank
      Row 4: column headers
      Row 5+: data, one row per date ascending. Cost is currency, CPC
              formula = cost / clicks (or 0 if clicks=0).
    """
    title_font = Font(bold=True, size=12, color="305496", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    current_fill = PatternFill("solid", fgColor=CURRENT_WEEK_FILL)

    ws.cell(row=1, column=1, value=f"Daily metrics: {label}").font = title_font
    ws.cell(row=2, column=1,
            value=(f"Window: {start_date} (Mon) to {max_data_date}. "
                   f"Last complete week ends {last_complete_sun} (Sun). "
                   f"Rows after that date are CURRENT (partial) week — "
                   f"highlighted amber.")).font = note_font

    headers = ["date", "weekday", "impressions", "clicks", "cost_usd", "cpc"]
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # Build a date-indexed map from the daily_df for fast lookup
    date_map = {}
    for _, r in daily_df.iterrows():
        if isinstance(r["date"], str):
            d = date.fromisoformat(r["date"])
        else:
            d = r["date"]
        date_map[d] = (int(r["impressions"]), int(r["clicks"]), float(r["cost_usd"]))

    # Walk every date in window
    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    row_idx = 5
    cur_date = start_date
    while cur_date <= end_date:
        imp, clk, cost = date_map.get(cur_date, (0, 0, 0.0))
        is_current_week = cur_date > last_complete_sun

        ws.cell(row=row_idx, column=1, value=cur_date).number_format = "yyyy-mm-dd"
        ws.cell(row=row_idx, column=2, value=WEEKDAY_NAMES[cur_date.weekday()])
        ws.cell(row=row_idx, column=3, value=imp).number_format = "#,##0"
        ws.cell(row=row_idx, column=4, value=clk).number_format = "#,##0"
        ws.cell(row=row_idx, column=5, value=round(cost, 2)).number_format = '"$"#,##0.00'
        # CPC formula references this row's clicks (col D) and cost (col E)
        cpc_formula = f"=IF(D{row_idx}=0,0,E{row_idx}/D{row_idx})"
        ws.cell(row=row_idx, column=6, value=cpc_formula).number_format = '"$"#,##0.00'

        for col_idx in range(1, 7):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = body_font
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)
            if is_current_week:
                cell.fill = current_fill

        row_idx += 1
        cur_date += timedelta(days=1)

    last_row = row_idx - 1

    # Column widths
    widths = [12, 8, 13, 10, 12, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=5, column=1)

    return last_row  # for use by other sheets referencing this data


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — ROLLING 7-DAY
# ═══════════════════════════════════════════════════════════════════

def write_rolling7_sheet(ws, daily_sheet_name: str,
                         start_date: date, end_date: date,
                         max_data_date: date, last_complete_sun: date,
                         daily_first_row: int, daily_last_row: int,
                         label: str):
    """Write a Roll7_<code> sheet with Excel formulas that reference the
    corresponding Daily_<code> sheet.

    Each row anchors a date (the "as-of" date) and the formulas sum the
    7 days inclusive ending on that date. The first 6 rows of the window
    have <7 days of history available, so the formula sums all available
    days (effectively a left-truncated rolling window). This is a
    documented behavior in the sheet header.

    CPC is weighted: sum(cost) / sum(clicks).
    """
    title_font = Font(bold=True, size=12, color="305496", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(bold=True, color=HEADER_FONT_COLOR, name="Arial")
    body_font = Font(name="Arial")
    note_font = Font(italic=True, color="595959", size=9, name="Arial")
    current_fill = PatternFill("solid", fgColor=CURRENT_WEEK_FILL)

    ws.cell(row=1, column=1, value=f"Rolling 7-day: {label}").font = title_font
    ws.cell(row=2, column=1, value=(
        f"Each row sums the prior 7 days INCLUSIVE of the as-of date, "
        f"referencing '{daily_sheet_name}'. Earliest 6 rows show "
        f"partial windows (only days available within the window are "
        f"summed). CPC is weighted: SUM(cost)/SUM(clicks).")).font = note_font

    headers = ["as_of_date", "weekday",
               "imp_7d", "clicks_7d", "cost_7d", "cpc_7d"]
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    row_idx = 5
    cur_date = start_date
    daily_idx = daily_first_row  # row index on the Daily sheet for cur_date
    while cur_date <= end_date:
        # 7-day window: cur_date back 6 days. Clamp at start_date.
        days_back = min(6, (cur_date - start_date).days)
        first_daily_row = daily_idx - days_back
        last_daily_row = daily_idx
        # Cell references in Daily sheet
        imp_range = f"'{daily_sheet_name}'!C{first_daily_row}:C{last_daily_row}"
        clk_range = f"'{daily_sheet_name}'!D{first_daily_row}:D{last_daily_row}"
        cost_range = f"'{daily_sheet_name}'!E{first_daily_row}:E{last_daily_row}"

        is_current_week = cur_date > last_complete_sun
        is_partial_window = days_back < 6  # first 6 rows are partial

        ws.cell(row=row_idx, column=1, value=cur_date).number_format = "yyyy-mm-dd"
        ws.cell(row=row_idx, column=2, value=WEEKDAY_NAMES[cur_date.weekday()])
        ws.cell(row=row_idx, column=3,
                value=f"=SUM({imp_range})").number_format = "#,##0"
        ws.cell(row=row_idx, column=4,
                value=f"=SUM({clk_range})").number_format = "#,##0"
        ws.cell(row=row_idx, column=5,
                value=f"=SUM({cost_range})").number_format = '"$"#,##0.00'
        # Weighted CPC: cost / clicks of the same rolling window
        cpc_formula = f"=IF(D{row_idx}=0,0,E{row_idx}/D{row_idx})"
        ws.cell(row=row_idx, column=6, value=cpc_formula).number_format = '"$"#,##0.00'

        for col_idx in range(1, 7):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = body_font
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)
            if is_current_week:
                cell.fill = current_fill
            # First 6 rows (partial windows) get a different visual cue
            if is_partial_window and not is_current_week:
                cell.font = Font(italic=True, color="7F7F7F", name="Arial")

        row_idx += 1
        cur_date += timedelta(days=1)
        daily_idx += 1

    widths = [12, 8, 13, 11, 12, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=5, column=1)


# ═══════════════════════════════════════════════════════════════════
# SHEET WRITERS — DAY-OF-WEEK GRID
# ═══════════════════════════════════════════════════════════════════

def write_dow_grid_sheet(ws, daily_sheet_name: str,
                          start_date: date, end_date: date,
                          max_data_date: date, last_complete_sun: date,
                          daily_first_row: int, label: str):
    """Write a DOW_<code> sheet with three stacked grids:
       (A) impressions    (B) clicks    (C) CPC (weighted)

    Each grid: rows = ISO weeks, columns = Mon-Sun. Cells reference
    the Daily sheet directly via cell references. CPC is computed as
    weighted cost / clicks for that single day (so it's just the daily
    CPC, exposed in a grid layout).

    The bottom row of each grid shows column averages / totals so you
    can compare 'avg Monday' to 'avg Friday' at a glance.
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

    ws.cell(row=1, column=1, value=f"Day-of-week grid: {label}").font = title_font
    ws.cell(row=2, column=1, value=(
        f"Three grids stacked vertically. Each row is one ISO week "
        f"(Mon-Sun). Scroll a column to compare a single weekday across "
        f"weeks. Amber rows are the CURRENT (partial) week. Bottom of "
        f"each grid shows column averages; CPC averages are weighted "
        f"by clicks across the column.")).font = note_font

    # Compute the list of week-starting Mondays covered by the window.
    # start_date is Monday of the earliest complete week, end_date is
    # max_data_date which may extend into the partial week.
    week_mondays = []
    cur_mon = start_date
    while cur_mon <= end_date:
        week_mondays.append(cur_mon)
        cur_mon += timedelta(days=7)

    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Map (week_monday, weekday_idx 0-6) -> daily sheet row number.
    # The daily sheet has rows starting at daily_first_row, one row per day.
    daily_row_lookup: dict[tuple[date, int], int] = {}
    cur_d = start_date
    cur_daily_row = daily_first_row
    while cur_d <= end_date:
        wk_mon = cur_d - timedelta(days=cur_d.weekday())
        daily_row_lookup[(wk_mon, cur_d.weekday())] = cur_daily_row
        cur_d += timedelta(days=1)
        cur_daily_row += 1

    def write_grid(start_row: int, metric_label: str,
                   daily_col_letter: str, number_format: str,
                   value_kind: str):
        """value_kind: 'count' (impressions/clicks) or 'cpc' (computed
        from cost col E and clicks col D)."""
        ws.cell(row=start_row, column=1, value=metric_label).font = section_font

        # Header row: week_start | Mon | Tue | ... | Sun
        header_row = start_row + 1
        headers = ["week_start"] + WEEKDAY_NAMES
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=header_row, column=col_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # Data rows: one per week
        for wk_idx, wk_mon in enumerate(week_mondays):
            r = header_row + 1 + wk_idx
            ws.cell(row=r, column=1, value=wk_mon).number_format = "yyyy-mm-dd"
            is_current_week = wk_mon > last_complete_sun - timedelta(days=6)
            for wd in range(7):  # 0..6 Mon..Sun
                target_date = wk_mon + timedelta(days=wd)
                daily_row = daily_row_lookup.get((wk_mon, wd))
                col_idx = 2 + wd
                if daily_row is None:
                    # Future date or out-of-window — leave blank
                    cell = ws.cell(row=r, column=col_idx, value=None)
                else:
                    if value_kind == "count":
                        formula = f"='{daily_sheet_name}'!{daily_col_letter}{daily_row}"
                    else:  # cpc
                        formula = (f"=IF('{daily_sheet_name}'!D{daily_row}=0,"
                                   f"\"\","
                                   f"'{daily_sheet_name}'!E{daily_row}/"
                                   f"'{daily_sheet_name}'!D{daily_row})")
                    cell = ws.cell(row=r, column=col_idx, value=formula)
                cell.number_format = number_format
                cell.font = body_font
                cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                     left=THIN_BORDER, right=THIN_BORDER)
                if is_current_week:
                    cell.fill = current_fill

        # Totals row at the bottom
        totals_row = header_row + 1 + len(week_mondays)
        ws.cell(row=totals_row, column=1, value="avg / weighted").font = totals_font
        ws.cell(row=totals_row, column=1).fill = totals_fill
        for wd in range(7):
            col_letter = get_column_letter(2 + wd)
            first_data_row = header_row + 1
            last_data_row = header_row + len(week_mondays)
            if value_kind == "count":
                # Simple average of the column. Use IF/COUNT to guard against
                # divide-by-zero in case all weekday cells are blank.
                formula = (f"=IF(COUNT({col_letter}{first_data_row}:"
                           f"{col_letter}{last_data_row})=0,\"\","
                           f"AVERAGE({col_letter}{first_data_row}:"
                           f"{col_letter}{last_data_row}))")
            else:
                # Weighted CPC: sum(cost over column) / sum(clicks over column).
                # Need to construct ranges across the daily sheet for the column.
                # Rather than build a single complex range, leverage the rows
                # we wrote on the daily sheet: collect the daily rows for this
                # weekday across all weeks.
                weekday_daily_rows = []
                for wk_mon in week_mondays:
                    dr = daily_row_lookup.get((wk_mon, wd))
                    if dr is not None:
                        weekday_daily_rows.append(dr)
                if not weekday_daily_rows:
                    formula = ""
                else:
                    cost_cells = ",".join(
                        f"'{daily_sheet_name}'!E{dr}" for dr in weekday_daily_rows
                    )
                    clk_cells = ",".join(
                        f"'{daily_sheet_name}'!D{dr}" for dr in weekday_daily_rows
                    )
                    formula = (f"=IF(SUM({clk_cells})=0,\"\","
                               f"SUM({cost_cells})/SUM({clk_cells}))")
            cell = ws.cell(row=totals_row, column=2 + wd, value=formula)
            cell.number_format = number_format
            cell.font = totals_font
            cell.fill = totals_fill
            cell.border = Border(top=THIN_BORDER, bottom=THIN_BORDER,
                                 left=THIN_BORDER, right=THIN_BORDER)

        return totals_row

    # Grid A: impressions (column C on daily sheet)
    last_row = write_grid(start_row=4, metric_label="A — Impressions",
                          daily_col_letter="C", number_format="#,##0",
                          value_kind="count")

    # Grid B: clicks (column D)
    last_row = write_grid(start_row=last_row + 3, metric_label="B — Clicks",
                          daily_col_letter="D", number_format="#,##0",
                          value_kind="count")

    # Grid C: CPC (computed)
    last_row = write_grid(start_row=last_row + 3, metric_label="C — CPC (weighted)",
                          daily_col_letter="",  # unused
                          number_format='"$"#,##0.00',
                          value_kind="cpc")

    # Column widths
    ws.column_dimensions["A"].width = 14
    for c in range(2, 9):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.freeze_panes = ws.cell(row=4, column=2)


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
        "trend charts, see the campaign_performance_microsoft_<date>.xlsx "
        "workbook produced by the visualization script. The recommendation "
        "engine and rationales live in efficiency_frontier_microsoft_<date>.xlsx. "
        "No backfill buffer applied to this workbook — "
        "impressions/clicks/cost finalize within hours. Rows beyond the "
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

    # Pull campaigns
    camp_meta = fetch_campaigns(conn)
    print(f"  {len(camp_meta)} ENABLED+PAUSED campaigns")

    # Pull all-campaign daily metrics for the window
    daily_all = fetch_daily_metrics(conn, window_start, max_data_date)
    if daily_all.empty:
        raise RuntimeError("No daily metrics in the window — nothing to write")

    # Account-level: sum across all enabled+paused campaigns per date
    account_daily = (
        daily_all.groupby("date", as_index=False)
                 .agg(impressions=("impressions", "sum"),
                      clicks=("clicks", "sum"),
                      cost_usd=("cost_usd", "sum"))
    )

    # Index of (code, meta) entries we'll write
    code_to_meta = {
        "Account": {"campaign_name": "ALL enabled+paused campaigns (sum)",
                    "status": "—", "current_tcpa": None},
    }

    # Account sheets (3)
    ws_d = wb.create_sheet("Daily_Account")
    last_row = write_daily_sheet(
        ws_d, account_daily, window_start, max_data_date,
        max_data_date, last_complete_sun,
        label="Account (all campaigns)", is_account=True,
    )
    print(f"  ✓ Daily_Account ({last_row - 4} rows)")
    daily_first_row = 5
    daily_last_row = last_row

    ws_r = wb.create_sheet("Roll7_Account")
    write_rolling7_sheet(
        ws_r, "Daily_Account", window_start, max_data_date,
        max_data_date, last_complete_sun,
        daily_first_row, daily_last_row, label="Account",
    )
    print("  ✓ Roll7_Account")

    ws_g = wb.create_sheet("DOW_Account")
    write_dow_grid_sheet(
        ws_g, "Daily_Account", window_start, max_data_date,
        max_data_date, last_complete_sun, daily_first_row,
        label="Account",
    )
    print("  ✓ DOW_Account")

    # Per-campaign sheets — skip campaigns with no data in the window
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
        # Ensure uniqueness in case two campaigns share a state abbreviation
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
        ws_r = wb.create_sheet(f"Roll7_{code}")
        write_rolling7_sheet(
            ws_r, f"Daily_{code}", window_start, max_data_date,
            max_data_date, last_complete_sun,
            5, last_row, label=cname,
        )
        ws_g = wb.create_sheet(f"DOW_{code}")
        write_dow_grid_sheet(
            ws_g, f"Daily_{code}", window_start, max_data_date,
            max_data_date, last_complete_sun, 5,
            label=cname,
        )
        print(f"  ✓ Daily_{code} / Roll7_{code} / DOW_{code}  ({cname})")

    # Index sheet — insert at position 0 (first sheet)
    ws_idx = wb.create_sheet("Index", 0)
    write_index_sheet(ws_idx, code_to_meta, window_start,
                       last_complete_sun, max_data_date, source_note)
    print("  ✓ Index")

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
