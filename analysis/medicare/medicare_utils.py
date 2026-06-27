"""
medicare_utils.py
==================
Shared utilities for Medicare analysis scripts.
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Provides:
  - PERIODS: named date range definitions
  - get_db_connection()
  - MEDICARE_ACCOUNT_ID constant
  - Excel formatting helpers (colors, styles)
  - Common pandas helpers
"""

import sqlite3
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, numbers
)
from openpyxl.utils import get_column_letter

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

DB_PATH            = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MEDICARE_ACCOUNT_ID = "5310820901"

# ── Named period definitions ───────────────────────────────────────────────────
# All dates are inclusive on both ends.
# Rolling periods (last_90, last_30) are computed at runtime.

def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()

def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()

PERIOD_DEFINITIONS = {
    "aep2025":  ("2025-10-15", "2025-12-07"),   # Annual Enrollment Period 2025
    "oep2026":  ("2026-01-01", "2026-03-31"),   # Open Enrollment Period 2026
    "off_peak": ("2025-04-01", "2025-10-14"),   # Between OEP end and AEP start
    "ytd":      ("2026-01-01", None),            # Year-to-date 2026 (end = yesterday)
    "last_90":  (None, None),                    # Rolling 90 days (computed at runtime)
    "last_30":  (None, None),                    # Rolling 30 days (computed at runtime)
}

PERIOD_LABELS = {
    "aep2025":  "AEP 2025 (Oct 15 – Dec 7, 2025)",
    "oep2026":  "OEP 2026 (Jan 1 – Mar 31, 2026)",
    "off_peak": "Off-Peak (Apr 1 – Oct 14, 2025)",
    "ytd":      "Year-to-Date 2026",
    "last_90":  "Last 90 Days (rolling)",
    "last_30":  "Last 30 Days (rolling)",
    "custom":   "Custom Date Range",
}

VALID_PERIODS = list(PERIOD_DEFINITIONS.keys()) + ["custom"]


def resolve_period(period: str, start: str = None, end: str = None) -> tuple[str, str]:
    """
    Resolve a period key to (start_date, end_date) strings (YYYY-MM-DD).
    For 'custom', requires both start and end args.
    """
    yday = _yesterday()

    if period == "custom":
        if not start or not end:
            raise ValueError("--period custom requires --start and --end")
        return start, end

    if period == "last_90":
        return _days_ago(90), yday

    if period == "last_30":
        return _days_ago(30), yday

    if period not in PERIOD_DEFINITIONS:
        raise ValueError(f"Unknown period '{period}'. Valid: {VALID_PERIODS}")

    p_start, p_end = PERIOD_DEFINITIONS[period]
    return p_start, (p_end or yday)


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection() -> sqlite3.Connection:
    """Standard connection matching all kw_db_scripts and campaign_db_scripts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Excel color palette ────────────────────────────────────────────────────────

class XL:
    """Centralised Excel style constants for consistent workbook appearance."""

    # Header backgrounds
    HEADER_DARK   = "1F3864"   # Dark navy  — section headers
    HEADER_MID    = "2E75B6"   # Mid blue   — sub-headers
    HEADER_LIGHT  = "D6E4F0"   # Light blue — column headers

    # Flag colors
    FLAG_RED      = "FF0000"   # Bad / over threshold
    FLAG_AMBER    = "FFC000"   # Warning / near threshold
    FLAG_GREEN    = "70AD47"   # Good / on target

    # Row alternation
    ROW_ALT       = "EBF3FB"   # Alternating row tint
    ROW_WHITE     = "FFFFFF"

    # Text
    TEXT_WHITE    = "FFFFFF"
    TEXT_DARK     = "1F1F1F"
    TEXT_BLUE     = "2E75B6"   # Hyperlinks / cross-refs

    # Font
    FONT_NAME     = "Arial"
    FONT_SIZE_HDR = 11
    FONT_SIZE_DATA= 10

    @staticmethod
    def header_font(bold: bool = True) -> Font:
        return Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_HDR,
                    bold=bold, color=XL.TEXT_WHITE)

    @staticmethod
    def data_font(bold: bool = False) -> Font:
        return Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA, bold=bold)

    @staticmethod
    def fill(hex_color: str) -> PatternFill:
        return PatternFill("solid", start_color=hex_color, fgColor=hex_color)

    @staticmethod
    def center() -> Alignment:
        return Alignment(horizontal="center", vertical="center", wrap_text=False)

    @staticmethod
    def left() -> Alignment:
        return Alignment(horizontal="left", vertical="center", wrap_text=False)

    @staticmethod
    def right() -> Alignment:
        return Alignment(horizontal="right", vertical="center")

    @staticmethod
    def thin_border() -> Border:
        s = Side(style="thin", color="BFBFBF")
        return Border(left=s, right=s, top=s, bottom=s)

    # Number formats
    FMT_DOLLAR    = '"$"#,##0'
    FMT_DOLLAR_2  = '"$"#,##0.00'
    FMT_PCT_1     = '0.0%'
    FMT_PCT_0     = '0%'
    FMT_INT       = '#,##0'
    FMT_FLOAT_2   = '#,##0.00'


# ── Sheet writing helpers ─────────────────────────────────────────────────────

def write_title_row(ws, title: str, subtitle: str, period_label: str,
                    n_cols: int, row: int = 1) -> int:
    """Write a two-row title block and return the next available row number."""
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=n_cols)
    cell = ws.cell(row=row, column=1, value=title)
    cell.font  = Font(name=XL.FONT_NAME, size=14, bold=True, color=XL.TEXT_WHITE)
    cell.fill  = XL.fill(XL.HEADER_DARK)
    cell.alignment = XL.center()

    row += 1
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=n_cols)
    cell = ws.cell(row=row, column=1,
                   value=f"{subtitle}  |  Period: {period_label}")
    cell.font  = Font(name=XL.FONT_NAME, size=10, italic=True, color=XL.TEXT_WHITE)
    cell.fill  = XL.fill(XL.HEADER_MID)
    cell.alignment = XL.left()

    return row + 2   # blank gap row


def write_column_headers(ws, headers: list[str], row: int,
                         col_start: int = 1) -> None:
    """Write a styled header row."""
    for i, h in enumerate(headers, start=col_start):
        cell = ws.cell(row=row, column=i, value=h)
        cell.font      = Font(name=XL.FONT_NAME, size=XL.FONT_SIZE_HDR,
                              bold=True, color=XL.TEXT_WHITE)
        cell.fill      = XL.fill(XL.HEADER_LIGHT[0:2] +  # reuse mid blue for col headers
                                  "75B6")
        cell.fill      = XL.fill(XL.HEADER_MID)
        cell.alignment = XL.center()
        cell.border    = XL.thin_border()


def write_dataframe_to_sheet(ws, df, start_row: int,
                              col_widths: dict = None,
                              number_formats: dict = None,
                              flag_col: str = None,
                              flag_rules: list = None) -> int:
    """
    Write a pandas DataFrame to a worksheet starting at start_row.
    Writes column headers, then data rows with alternating fill.

    col_widths:     {col_name: width} optional column width overrides
    number_formats: {col_name: format_str} optional per-column number formats
    flag_col:       column name to apply conditional fill based on flag_rules
    flag_rules:     list of (threshold, comparison, hex_color) tuples
                    e.g. [('>', 100, XL.FLAG_RED), ('<', 50, XL.FLAG_GREEN)]

    Returns next available row after data.
    """
    import pandas as pd

    headers = list(df.columns)
    write_column_headers(ws, headers, start_row)

    col_idx = {h: i + 1 for i, h in enumerate(headers)}
    flag_col_idx = col_idx.get(flag_col) if flag_col else None

    for r_offset, (_, row_data) in enumerate(df.iterrows()):
        r = start_row + 1 + r_offset
        fill = XL.fill(XL.ROW_ALT if r_offset % 2 == 0 else XL.ROW_WHITE)

        for col_name, value in row_data.items():
            c = col_idx[col_name]
            cell = ws.cell(row=r, column=c, value=value)
            cell.font      = XL.data_font()
            cell.fill      = fill
            cell.border    = XL.thin_border()
            cell.alignment = XL.right() if isinstance(value, (int, float)) else XL.left()

            if number_formats and col_name in number_formats:
                cell.number_format = number_formats[col_name]

        # Apply flag fill to flag column if rules provided
        if flag_col_idx and flag_rules:
            flag_val = row_data.get(flag_col)
            for op, threshold, color in flag_rules:
                hit = False
                if flag_val is not None:
                    try:
                        if op == ">"  and float(flag_val) > threshold: hit = True
                        elif op == ">=" and float(flag_val) >= threshold: hit = True
                        elif op == "<"  and float(flag_val) < threshold: hit = True
                        elif op == "<=" and float(flag_val) <= threshold: hit = True
                        elif op == "==" and float(flag_val) == threshold: hit = True
                    except (TypeError, ValueError):
                        pass
                if hit:
                    ws.cell(row=r, column=flag_col_idx).fill = XL.fill(color)
                    ws.cell(row=r, column=flag_col_idx).font = Font(
                        name=XL.FONT_NAME, size=XL.FONT_SIZE_DATA,
                        bold=True, color=XL.TEXT_WHITE)
                    break

    # Apply column widths
    for col_name, width in (col_widths or {}).items():
        if col_name in col_idx:
            ws.column_dimensions[
                get_column_letter(col_idx[col_name])
            ].width = width

    ws.row_dimensions[start_row].height = 20

    return start_row + 1 + len(df) + 1   # next available row


def autofit_columns(ws, min_width: int = 8, max_width: int = 45) -> None:
    """Auto-size columns based on content length."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(min_width,
                                                      min(max_len + 2, max_width))


def freeze_header_rows(ws, freeze_row: int = 4) -> None:
    """Freeze rows above freeze_row (title + header rows)."""
    ws.freeze_panes = ws.cell(row=freeze_row, column=1)


# ── Common safe math helpers ───────────────────────────────────────────────────

def safe_div(numerator, denominator, default=None):
    """Null-safe division. Returns default if denominator is 0 or None."""
    try:
        if denominator == 0 or denominator is None:
            return default
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default


def micros_to_dollars(micros) -> float:
    """Convert Google Ads micros to dollars."""
    if micros is None:
        return 0.0
    return float(micros) / 1_000_000
