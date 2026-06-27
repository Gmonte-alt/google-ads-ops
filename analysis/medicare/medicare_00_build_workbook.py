"""
medicare_00_build_workbook.py
==============================
Medicare Analysis Orchestrator
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\analysis\\

Runs all five Medicare analysis modules and assembles their sheets into a
single combined Excel workbook. Also writes five individual workbooks
(one per module) alongside the combined file.

Output files (in analysis/output/):
  medicare_analysis_{period}.xlsx                ← combined workbook (all sheets)
  medicare_01_account_structure_{period}.xlsx
  medicare_02_bidding_efficiency_{period}.xlsx
  medicare_03_keyword_strategy_{period}.xlsx
  medicare_04_geo_performance_{period}.xlsx
  medicare_05_budget_scaling_{period}.xlsx

Usage:
  # Run all modules, combined + individual workbooks
  python medicare_00_build_workbook.py --period last_90
  python medicare_00_build_workbook.py --period aep2025
  python medicare_00_build_workbook.py --period oep2026
  python medicare_00_build_workbook.py --period custom --start 2025-10-01 --end 2025-12-31

  # Skip specific modules (e.g. if keyword data not ready)
  python medicare_00_build_workbook.py --period last_90 --skip 03

  # Combined workbook only, no individual files
  python medicare_00_build_workbook.py --period last_90 --combined-only

Named periods:
  aep2025   2025-10-15 → 2025-12-07   Annual Enrollment Period 2025
  oep2026   2026-01-01 → 2026-03-31   Open Enrollment Period 2026
  off_peak  2025-04-01 → 2025-10-14   Between OEP end and AEP start
  ytd       2026-01-01 → yesterday     Year-to-date 2026
  last_90   rolling 90 days            Default operational window
  last_30   rolling 30 days            Signal density / learning flags
  custom    requires --start + --end
"""

import argparse
import logging
import time
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

import sys
sys.path.insert(0, str(Path(__file__).parent))

from medicare_utils import (
    resolve_period, PERIOD_LABELS, VALID_PERIODS,
    write_title_row, write_dataframe_to_sheet, autofit_columns,
    freeze_header_rows, XL
)

import medicare_01_account_structure  as m01
import medicare_02_bidding_efficiency as m02
import medicare_03_keyword_strategy   as m03
import medicare_04_geo_performance    as m04
import medicare_05_budget_scaling     as m05

OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\analysis\output")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Module registry ────────────────────────────────────────────────────────────

MODULES = {
    "01": {
        "label":  "Account Structure & Hygiene",
        "module": m01,
    },
    "02": {
        "label":  "Bidding Strategy & Efficiency",
        "module": m02,
    },
    "03": {
        "label":  "Keyword Strategy & Query Mining",
        "module": m03,
    },
    "04": {
        "label":  "Geo & Demographic Performance",
        "module": m04,
    },
    "05": {
        "label":  "Budget & Scaling Opportunities",
        "module": m05,
    },
}


# ── Cover sheet ────────────────────────────────────────────────────────────────

def add_cover_sheet(wb: Workbook, period: str, start: str, end: str,
                    module_results: dict) -> None:
    """Add an index/cover sheet as the first sheet in the combined workbook."""
    ws = wb.create_sheet(title="📋 Index", index=0)

    # Title
    ws.merge_cells("A1:F1")
    cell = ws["A1"]
    cell.value     = "Medicare Google Ads — Campaign Analytics"
    cell.font      = Font(name="Arial", size=16, bold=True, color=XL.TEXT_WHITE)
    cell.fill      = PatternFill("solid", start_color=XL.HEADER_DARK,
                                  fgColor=XL.HEADER_DARK)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    # Period subtitle
    ws.merge_cells("A2:F2")
    cell = ws["A2"]
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    cell.value     = f"Analysis Period: {label}  ({start} → {end})"
    cell.font      = Font(name="Arial", size=11, italic=True, color=XL.TEXT_WHITE)
    cell.fill      = PatternFill("solid", start_color=XL.HEADER_MID,
                                  fgColor=XL.HEADER_MID)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20

    ws.row_dimensions[3].height = 10  # spacer

    # Section headers
    headers = ["Module", "Section", "Sheets", "Rows", "Status", "Notes"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=i, value=h)
        c.font      = Font(name="Arial", size=11, bold=True, color=XL.TEXT_WHITE)
        c.fill      = PatternFill("solid", start_color=XL.HEADER_MID,
                                   fgColor=XL.HEADER_MID)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border    = XL.thin_border()
    ws.row_dimensions[4].height = 18

    # Module rows
    row = 5
    for module_key in sorted(MODULES.keys()):
        info   = MODULES[module_key]
        result = module_results.get(module_key, {})
        sheets = result.get("sheets", {})
        status = result.get("status", "skipped")
        error  = result.get("error", "")

        total_rows   = sum(len(df) for df in sheets.values())
        sheet_count  = len(sheets)
        fill_color   = (XL.ROW_ALT if int(module_key) % 2 == 0 else XL.ROW_WHITE)
        status_color = {
            "ok":      XL.FLAG_GREEN,
            "error":   XL.FLAG_RED,
            "skipped": XL.FLAG_AMBER,
        }.get(status, XL.ROW_WHITE)

        values = [
            f"Module {module_key}",
            info["label"],
            sheet_count,
            f"{total_rows:,}",
            status.upper(),
            error[:80] if error else "",
        ]
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=row, column=i, value=v)
            c.font      = Font(name="Arial", size=10)
            c.fill      = PatternFill("solid", start_color=fill_color,
                                       fgColor=fill_color)
            c.alignment = Alignment(horizontal="left", vertical="center",
                                    wrap_text=False)
            c.border    = XL.thin_border()
            if i == 5:  # Status column
                c.fill      = PatternFill("solid", start_color=status_color,
                                           fgColor=status_color)
                c.font      = Font(name="Arial", size=10, bold=True,
                                   color=XL.TEXT_WHITE)
                c.alignment = Alignment(horizontal="center")

        ws.row_dimensions[row].height = 16
        row += 1

    # Column widths
    widths = {"A": 14, "B": 38, "C": 8, "D": 10, "E": 12, "F": 55}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    ws.sheet_view.zoomScale = 100


# ── Combined workbook writer ───────────────────────────────────────────────────

def write_combined_workbook(
    module_results: dict,
    period: str,
    start: str,
    end: str,
    output_path: Path,
) -> None:
    """
    Merge all module sheets into a single workbook.
    Sheets are ordered by module number.
    """
    label = PERIOD_LABELS.get(period, f"{start} → {end}")
    wb    = Workbook()
    wb.remove(wb.active)

    # Add cover/index first
    add_cover_sheet(wb, period, start, end, module_results)

    # Add all module sheets in order
    for module_key in sorted(MODULES.keys()):
        result = module_results.get(module_key, {})
        sheets = result.get("sheets", {})

        for sheet_name, df in sheets.items():
            ws = wb.create_sheet(title=sheet_name)

            if df is None or df.empty:
                ws["A1"] = f"No data for period: {label}"
                continue

            n_cols   = len(df.columns)
            data_row = write_title_row(
                ws,
                title        = sheet_name.replace("_", " ").title(),
                subtitle     = f"Module {module_key} — {MODULES[module_key]['label']}",
                period_label = label,
                n_cols       = n_cols,
            )

            write_dataframe_to_sheet(ws, df, data_row)
            autofit_columns(ws)
            freeze_header_rows(ws, freeze_row=data_row + 1)
            ws.sheet_view.zoomScale = 90

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    log.info(f"Combined workbook saved: {output_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Medicare Analysis Orchestrator — builds combined Excel workbook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[0].strip(),
    )
    parser.add_argument(
        "--period", choices=VALID_PERIODS, default="last_90",
        help="Named analysis period (default: last_90)"
    )
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date (required for --period custom)")
    parser.add_argument("--end",   metavar="YYYY-MM-DD",
                        help="End date (required for --period custom)")
    parser.add_argument(
        "--skip", metavar="MODULE", nargs="+", default=[],
        help="Module numbers to skip e.g. --skip 03 05"
    )
    parser.add_argument(
        "--combined-only", action="store_true",
        help="Write combined workbook only; skip individual module files"
    )
    parser.add_argument(
        "--modules", metavar="MODULE", nargs="+", default=None,
        help="Run only specific modules e.g. --modules 01 04"
    )
    return parser.parse_args()


def main():
    args       = parse_args()
    start, end = resolve_period(args.period, args.start, args.end)
    label      = PERIOD_LABELS.get(args.period, f"{start} → {end}")

    log.info(f"Medicare Campaign Analytics")
    log.info(f"Period  : {label} ({start} → {end})")
    log.info(f"Output  : {OUTPUT_DIR}")
    log.info("")

    active_modules = args.modules or list(MODULES.keys())
    skip_modules   = set(args.skip)

    module_results = {}
    total_start    = time.time()

    for module_key in sorted(active_modules):
        if module_key in skip_modules:
            log.info(f"  [{module_key}] {MODULES[module_key]['label']} — SKIPPED")
            module_results[module_key] = {"status": "skipped", "sheets": {}}
            continue

        if module_key not in MODULES:
            log.warning(f"  [{module_key}] Unknown module — skipping")
            continue

        info = MODULES[module_key]
        log.info(f"  [{module_key}] {info['label']} ...")
        t0 = time.time()

        try:
            mod    = info["module"]
            sheets = mod.build_sheets(args.period, start, end)
            module_results[module_key] = {"status": "ok", "sheets": sheets}

            for sheet_name, df in sheets.items():
                log.info(f"    {sheet_name:<42} {len(df):>6,} rows")

            # Write individual module workbook
            if not args.combined_only:
                ind_path = OUTPUT_DIR / f"medicare_{module_key}_{args.period}.xlsx"
                mod.write_workbook(sheets, args.period, start, end, ind_path)

            elapsed = time.time() - t0
            log.info(f"    Done ({elapsed:.1f}s)")

        except Exception as e:
            log.error(f"    ERROR in module {module_key}: {e}")
            module_results[module_key] = {
                "status": "error",
                "sheets": {},
                "error":  str(e),
            }

        log.info("")

    # Write combined workbook
    combined_path = OUTPUT_DIR / f"medicare_analysis_{args.period}.xlsx"
    log.info(f"Building combined workbook ...")
    write_combined_workbook(module_results, args.period, start, end, combined_path)

    total_elapsed = time.time() - total_start
    log.info("")
    log.info(f"Complete in {total_elapsed:.1f}s")
    log.info(f"Combined : {combined_path}")
    if not args.combined_only:
        log.info(f"Individual files in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
