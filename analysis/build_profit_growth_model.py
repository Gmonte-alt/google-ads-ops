"""
Build the Microsoft Home Insurance profit-growth Excel model.

Reads `model_baseline_microsoft_home_<date>.csv` (produced by
profit_growth_microsoft.py) and emits an interactive xlsx with:

  Sheet 1: Baseline           — read-only data import
  Sheet 2: Model Controls     — scenario toggles, global parameters,
                                  summary outputs
  Sheet 3: Per-Campaign Plan  — destination tCPA per campaign +
                                  forecast under chosen scenario
  Sheet 4: Stair-step Schedule — pacing rollout, 6 cycles max,
                                  shows tCPA + expected outcomes per cycle

The model is formula-driven end-to-end — change a value on Model
Controls or Per-Campaign Plan and everything recomputes.

Usage:
    python build_profit_growth_model.py <baseline_csv> [output_xlsx]
"""

import argparse
import csv
from datetime import date
from pathlib import Path
import sys

from openpyxl import Workbook
from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side,
                              NamedStyle)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import (CellIsRule, FormulaRule,
                                       ColorScaleRule)
from openpyxl.workbook.defined_name import DefinedName


# ═══════════════════════════════════════════════════════════════════
# STYLING
# ═══════════════════════════════════════════════════════════════════

HEADER_FILL_DARK = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT_WHITE = Font(color="FFFFFF", bold=True, size=11)
SECTION_FILL = PatternFill("solid", fgColor="DDEBF7")
SECTION_FONT = Font(color="1F4E79", bold=True, size=12)

READONLY_FILL = PatternFill("solid", fgColor="F2F2F2")
EDITABLE_FILL = PatternFill("solid", fgColor="FFF2CC")
COMPUTED_FILL = PatternFill("solid", fgColor="FFFFFF")
NOTE_FILL = PatternFill("solid", fgColor="EDEDED")

POSITIVE_FILL = PatternFill("solid", fgColor="C6EFCE")
NEGATIVE_FILL = PatternFill("solid", fgColor="FFC7CE")
WARNING_FILL = PatternFill("solid", fgColor="FFEB9C")

THIN_BORDER = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)

MAX_CYCLES = 6


# ═══════════════════════════════════════════════════════════════════
# LOAD BASELINE CSV
# ═══════════════════════════════════════════════════════════════════

def load_baseline_csv(path: Path) -> tuple[list[dict], dict]:
    """Load the baseline CSV. Returns (rows, metadata).

    The CSV has comment lines (starting with #) at the top with metadata,
    then a header row, then per-campaign data rows.
    """
    metadata = {}
    data_lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#"):
                # Parse metadata
                if ":" in stripped:
                    key, _, val = stripped.lstrip("#").strip().partition(":")
                    metadata[key.strip()] = val.strip()
            else:
                data_lines.append(line)

    # Parse the CSV body
    rows = list(csv.DictReader(data_lines))
    # Convert numeric columns
    numeric_cols = [
        "current_tcpa", "profit_optimal_tcpa", "margin_ceiling",
        "current_30d_spend", "current_30d_conv", "current_30d_revenue",
        "current_30d_cpa", "current_30d_rpl", "current_30d_profit",
        "current_30d_roas",
        "rpl_slope_per_week", "rpl_r_squared", "rpl_8w_recent_mean",
        "weekly_cpa_latest", "weekly_conv_latest", "weekly_rpl_latest",
        "weekly_profit_latest",
    ]
    for row in rows:
        for col in numeric_cols:
            if col in row and row[col] not in ("", "None", None):
                try:
                    row[col] = float(row[col])
                except (TypeError, ValueError):
                    row[col] = None
            else:
                row[col] = None

    return rows, metadata


# ═══════════════════════════════════════════════════════════════════
# SHEET 1: BASELINE
# ═══════════════════════════════════════════════════════════════════

def build_baseline_sheet(ws, rows: list[dict], metadata: dict) -> None:
    """Read-only data import sheet."""
    ws.cell(row=1, column=1, value="BASELINE DATA — READ-ONLY").font = Font(
        bold=True, size=14, color="1F4E79")
    ws.cell(row=2, column=1,
            value="Data imported from profit_growth_microsoft.py output. "
                  "Do not edit values here — they drive all formulas. "
                  "To refresh, re-run the Python script and paste the new "
                  "CSV's data rows into this sheet (rows 8+).").alignment = (
        Alignment(wrap_text=True, vertical="top"))

    # Metadata block
    r = 4
    for key in ["Generated", "Data window end", "Campaigns", "Source"]:
        if key in metadata:
            ws.cell(row=r, column=1, value=key + ":").font = Font(bold=True)
            ws.cell(row=r, column=2, value=metadata[key])
            r += 1

    # Headers
    header_row = 8
    headers = [
        "campaign_name", "cohort", "current_tcpa", "margin_ceiling",
        "current_30d_spend", "current_30d_conv", "current_30d_revenue",
        "current_30d_cpa", "current_30d_rpl", "current_30d_profit",
        "current_30d_roas",
        "rpl_slope_per_week", "rpl_r_squared", "rpl_momentum_class",
        "rpl_8w_recent_mean",
        "weekly_cpa_latest", "weekly_conv_latest", "weekly_rpl_latest",
        "weekly_profit_latest", "weekly_latest_week_start",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell.fill = HEADER_FILL_DARK
        cell.font = HEADER_FONT_WHITE
        cell.alignment = Alignment(horizontal="left", vertical="center",
                                   wrap_text=True)
        cell.border = THIN_BORDER

    # Data rows
    for row_idx, row_data in enumerate(rows, start=header_row + 1):
        for col_idx, h in enumerate(headers, start=1):
            value = row_data.get(h)
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.fill = READONLY_FILL
            cell.border = THIN_BORDER
            # Number formats
            if h in ("current_tcpa", "margin_ceiling", "current_30d_cpa",
                     "current_30d_rpl", "rpl_slope_per_week",
                     "rpl_8w_recent_mean", "weekly_cpa_latest",
                     "weekly_rpl_latest"):
                cell.number_format = '"$"#,##0.00'
            elif h in ("current_30d_spend", "current_30d_revenue",
                       "current_30d_profit", "weekly_profit_latest"):
                cell.number_format = '"$"#,##0'
            elif h == "current_30d_roas":
                cell.number_format = "0.0%"
            elif h in ("current_30d_conv", "weekly_conv_latest"):
                cell.number_format = "0"
            elif h == "rpl_r_squared":
                cell.number_format = "0.00"

    # Column widths
    ws.column_dimensions["A"].width = 50
    for col_letter in "BCDEFGHIJKLMNOPQRST":
        ws.column_dimensions[col_letter].width = 16
    ws.freeze_panes = "B9"


# ═══════════════════════════════════════════════════════════════════
# SHEET 2: MODEL CONTROLS
# ═══════════════════════════════════════════════════════════════════

def build_model_controls_sheet(ws, n_campaigns: int) -> dict[str, str]:
    """Scenario toggles and global parameters.

    Returns a dict mapping logical-name → cell-reference (e.g.,
    "global_elasticity" → "$B$10") so other sheets can build formulas
    against named cells without hard-coding.
    """
    refs = {}

    ws.cell(row=1, column=1, value="MODEL CONTROLS").font = Font(
        bold=True, size=14, color="1F4E79")
    ws.cell(row=2, column=1,
            value="Adjust scenarios, elasticity, and RPL assumptions here. "
                  "All values cascade to the Per-Campaign Plan and "
                  "Stair-step Schedule sheets.").alignment = Alignment(
        wrap_text=True, vertical="top")

    # ── ELASTICITY SCENARIO ───────────────────────────────────────
    r = 4
    ws.cell(row=r, column=1, value="ELASTICITY ASSUMPTION").font = SECTION_FONT
    ws.cell(row=r, column=1).fill = SECTION_FILL
    r += 1
    ws.cell(row=r, column=1, value="Description:")
    ws.cell(row=r, column=2,
            value="A 1% raise in tCPA produces N% more conversions").font = (
        Font(italic=True, color="595959"))
    r += 2

    ws.cell(row=r, column=1, value="Scenario selector:").font = Font(bold=True)
    scenario_cell = ws.cell(row=r, column=2, value="Aggressive")
    scenario_cell.fill = EDITABLE_FILL
    scenario_cell.border = THIN_BORDER
    scenario_cell.alignment = Alignment(horizontal="center")
    refs["scenario"] = f"$B${r}"
    # Dropdown for scenario
    dv = DataValidation(type="list",
                         formula1='"Conservative,Mid,Aggressive,Custom"',
                         allow_blank=False)
    dv.add(f"B{r}")
    ws.add_data_validation(dv)
    r += 1

    # Three scenario values (look-up table) and Custom override
    ws.cell(row=r, column=1, value="    Conservative ε:")
    ws.cell(row=r, column=2, value=0.6).number_format = "0.00"
    ws.cell(row=r, column=2).fill = READONLY_FILL
    refs["conservative_eps"] = f"$B${r}"
    r += 1
    ws.cell(row=r, column=1, value="    Mid ε:")
    ws.cell(row=r, column=2, value=1.0).number_format = "0.00"
    ws.cell(row=r, column=2).fill = READONLY_FILL
    refs["mid_eps"] = f"$B${r}"
    r += 1
    ws.cell(row=r, column=1, value="    Aggressive ε:")
    ws.cell(row=r, column=2, value=1.2).number_format = "0.00"
    ws.cell(row=r, column=2).fill = READONLY_FILL
    refs["aggressive_eps"] = f"$B${r}"
    r += 1
    ws.cell(row=r, column=1, value="    Custom ε (used if scenario=Custom):")
    custom_cell = ws.cell(row=r, column=2, value=1.0)
    custom_cell.fill = EDITABLE_FILL
    custom_cell.border = THIN_BORDER
    custom_cell.number_format = "0.00"
    refs["custom_eps"] = f"$B${r}"
    r += 1

    # Active elasticity (derived from scenario selector)
    ws.cell(row=r, column=1,
            value="→ Active elasticity:").font = Font(bold=True)
    active_cell = ws.cell(row=r, column=2,
                          value=(f'=IF({refs["scenario"]}="Conservative",'
                                 f'{refs["conservative_eps"]},'
                                 f'IF({refs["scenario"]}="Mid",'
                                 f'{refs["mid_eps"]},'
                                 f'IF({refs["scenario"]}="Aggressive",'
                                 f'{refs["aggressive_eps"]},'
                                 f'{refs["custom_eps"]})))'))
    active_cell.font = Font(bold=True, color="1F4E79")
    active_cell.fill = COMPUTED_FILL
    active_cell.border = THIN_BORDER
    active_cell.number_format = "0.00"
    refs["active_eps"] = f"$B${r}"
    r += 2

    # ── RPL DRIFT ────────────────────────────────────────────────
    ws.cell(row=r, column=1, value="RPL ASSUMPTION").font = SECTION_FONT
    ws.cell(row=r, column=1).fill = SECTION_FILL
    r += 1
    ws.cell(row=r, column=1, value="Description:")
    ws.cell(row=r, column=2,
            value="How RPL is expected to behave over the forecast horizon").font = (
        Font(italic=True, color="595959"))
    r += 2

    ws.cell(row=r, column=1, value="RPL mode:").font = Font(bold=True)
    rpl_mode_cell = ws.cell(row=r, column=2, value="Flat")
    rpl_mode_cell.fill = EDITABLE_FILL
    rpl_mode_cell.border = THIN_BORDER
    rpl_mode_cell.alignment = Alignment(horizontal="center")
    refs["rpl_mode"] = f"$B${r}"
    dv2 = DataValidation(type="list",
                         formula1='"Flat,Continue current trend,Manual %"',
                         allow_blank=False)
    dv2.add(f"B{r}")
    ws.add_data_validation(dv2)
    r += 1

    ws.cell(row=r, column=1,
            value="    Forecast horizon (weeks, used if 'Continue trend'):")
    horizon_cell = ws.cell(row=r, column=2, value=12)
    horizon_cell.fill = EDITABLE_FILL
    horizon_cell.border = THIN_BORDER
    horizon_cell.number_format = "0"
    refs["rpl_horizon_weeks"] = f"$B${r}"
    r += 1

    ws.cell(row=r, column=1,
            value="    Manual RPL drift % (used if 'Manual %'):")
    manual_cell = ws.cell(row=r, column=2, value=0.0)
    manual_cell.fill = EDITABLE_FILL
    manual_cell.border = THIN_BORDER
    manual_cell.number_format = "0.0%"
    refs["rpl_manual_pct"] = f"$B${r}"
    r += 2

    # ── CPA SCALING ASSUMPTION ────────────────────────────────────
    # When tCPA rises by ΔtCPA, actual realized CPA rises by some fraction
    # of that. 0 = CPA stays put (rare), 1 = CPA rises dollar-for-dollar
    # with tCPA. Reality is usually 0.3-0.7 — the algorithm uses some of
    # the extra headroom to bid more aggressively but not all of it.
    ws.cell(row=r, column=1, value="CPA SCALING").font = SECTION_FONT
    ws.cell(row=r, column=1).fill = SECTION_FILL
    r += 1
    ws.cell(row=r, column=1, value="Description:")
    ws.cell(row=r, column=2,
            value=("Fraction of tCPA-raise dollars that show up in CPA. "
                   "0 = CPA stays put. 1 = CPA rises dollar-for-dollar "
                   "with tCPA. Reality is usually 0.3-0.7.")).font = (
        Font(italic=True, color="595959"))
    ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True,
                                                     vertical="top")
    ws.row_dimensions[r].height = 42
    r += 2

    ws.cell(row=r, column=1,
            value="CPA scaling factor:").font = Font(bold=True)
    cpa_scale_cell = ws.cell(row=r, column=2, value=0.5)
    cpa_scale_cell.fill = EDITABLE_FILL
    cpa_scale_cell.border = THIN_BORDER
    cpa_scale_cell.number_format = "0.00"
    refs["cpa_scaling"] = f"$B${r}"
    r += 2

    # ── MARGIN POLICY ─────────────────────────────────────────────
    ws.cell(row=r, column=1, value="MARGIN POLICY").font = SECTION_FONT
    ws.cell(row=r, column=1).fill = SECTION_FILL
    r += 1
    ws.cell(row=r, column=1, value="Account margin floor:").font = Font(bold=True)
    floor_cell = ws.cell(row=r, column=2, value=0.20)
    floor_cell.fill = EDITABLE_FILL
    floor_cell.border = THIN_BORDER
    floor_cell.number_format = "0%"
    refs["margin_floor"] = f"$B${r}"
    r += 1

    # ── STOP CONDITION (advisory, no formula) ─────────────────────
    ws.cell(row=r, column=1, value="Pull-back trigger:").font = Font(bold=True)
    ws.cell(row=r, column=2,
            value="If account-blended margin drops below 25% in any 7-day "
                  "window, pull back the raises.").alignment = Alignment(
        wrap_text=True)
    ws.cell(row=r, column=2).fill = NOTE_FILL
    ws.row_dimensions[r].height = 30
    r += 2

    # ── ACCOUNT SUMMARY (formula-driven, references Per-Campaign Plan) ──
    ws.cell(row=r, column=1,
            value="ACCOUNT-LEVEL FORECAST").font = SECTION_FONT
    ws.cell(row=r, column=1).fill = SECTION_FILL
    r += 1
    ws.cell(row=r, column=1, value="Sums across all campaigns at the "
                                   "chosen scenario").font = Font(italic=True,
                                                                   color="595959")
    r += 2

    # Headers for the comparison table
    ws.cell(row=r, column=1, value="Metric").font = Font(bold=True)
    ws.cell(row=r, column=2, value="Current (30d)").font = Font(bold=True)
    ws.cell(row=r, column=3, value="Destination (30d)").font = Font(bold=True)
    ws.cell(row=r, column=4, value="Change").font = Font(bold=True)
    for c in range(1, 5):
        ws.cell(row=r, column=c).fill = HEADER_FILL_DARK
        ws.cell(row=r, column=c).font = HEADER_FONT_WHITE
        ws.cell(row=r, column=c).border = THIN_BORDER
    r += 1

    # Per-campaign-plan range (rows 5 .. 5 + n_campaigns - 1 on that sheet)
    plan_range_start = 5  # row where first campaign data starts on Plan sheet
    plan_range_end = plan_range_start + n_campaigns - 1

    # SPEND
    ws.cell(row=r, column=1, value="Spend")
    ws.cell(row=r, column=2,
            value=f"=SUM(Baseline!E9:E{8 + n_campaigns})")
    ws.cell(row=r, column=3,
            value=f"=SUM('Per-Campaign Plan'!K{plan_range_start}:K{plan_range_end})")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = '"$"#,##0'
    r += 1

    # CONVERSIONS
    ws.cell(row=r, column=1, value="Conversions")
    ws.cell(row=r, column=2,
            value=f"=SUM(Baseline!F9:F{8 + n_campaigns})")
    ws.cell(row=r, column=3,
            value=f"=SUM('Per-Campaign Plan'!I{plan_range_start}:I{plan_range_end})")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = "0"
    r += 1

    # REVENUE
    ws.cell(row=r, column=1, value="Revenue")
    ws.cell(row=r, column=2,
            value=f"=SUM(Baseline!G9:G{8 + n_campaigns})")
    ws.cell(row=r, column=3,
            value=f"=SUM('Per-Campaign Plan'!J{plan_range_start}:J{plan_range_end})")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = '"$"#,##0'
    r += 1

    # PROFIT
    ws.cell(row=r, column=1, value="Profit").font = Font(bold=True)
    ws.cell(row=r, column=2,
            value=f"=SUM(Baseline!J9:J{8 + n_campaigns})")
    ws.cell(row=r, column=3,
            value=f"=SUM('Per-Campaign Plan'!L{plan_range_start}:L{plan_range_end})")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}").font = Font(bold=True)
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = '"$"#,##0'
        ws.cell(row=r, column=c).font = Font(bold=True)
    refs["total_current_profit"] = f"$B${r}"
    refs["total_dest_profit"] = f"$C${r}"
    refs["total_profit_change"] = f"$D${r}"
    r += 1

    # AVG CPA (blended = spend / conv)
    # Spend was 4 rows above, conv was 3 rows above
    cpa_row = r
    spend_row_ref = r - 4
    conv_row_ref = r - 3
    ws.cell(row=r, column=1, value="Avg CPA (blended)")
    ws.cell(row=r, column=2,
            value=f"=IFERROR(B{spend_row_ref}/B{conv_row_ref},0)")
    ws.cell(row=r, column=3,
            value=f"=IFERROR(C{spend_row_ref}/C{conv_row_ref},0)")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = '"$"#,##0.00'
    r += 1

    # AVG ROAS (blended = revenue / spend)
    # At this point: spend is 5 rows above, revenue is 3 rows above
    spend_row_ref2 = r - 5
    rev_row_ref = r - 3
    ws.cell(row=r, column=1, value="Avg ROAS (blended)")
    ws.cell(row=r, column=2,
            value=f"=IFERROR(B{rev_row_ref}/B{spend_row_ref2},0)")
    ws.cell(row=r, column=3,
            value=f"=IFERROR(C{rev_row_ref}/C{spend_row_ref2},0)")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = "0.0%"
    r += 1

    # AVG MARGIN (blended = profit / revenue)
    # At this point: revenue is 4 rows above, profit is 3 rows above
    margin_row = r
    rev_row_ref2 = r - 4
    profit_row_ref = r - 3
    ws.cell(row=r, column=1, value="Avg margin (blended)")
    ws.cell(row=r, column=2,
            value=f"=IFERROR(B{profit_row_ref}/B{rev_row_ref2},0)")
    ws.cell(row=r, column=3,
            value=f"=IFERROR(C{profit_row_ref}/C{rev_row_ref2},0)")
    ws.cell(row=r, column=4,
            value=f"=C{r}-B{r}")
    for c in (2, 3, 4):
        ws.cell(row=r, column=c).number_format = "0.0%"
    refs["blended_margin_destination"] = f"$C${margin_row}"
    r += 1

    # Annualized profit lift callout
    r += 1
    ws.cell(row=r, column=1,
            value="ANNUALIZED PROFIT LIFT (×12 monthly):").font = Font(
        bold=True, size=12)
    ws.cell(row=r, column=2,
            value=f"={refs['total_profit_change']}*12")
    ws.cell(row=r, column=2).number_format = '"$"#,##0'
    ws.cell(row=r, column=2).font = Font(bold=True, size=12, color="1F4E79")
    ws.cell(row=r, column=2).fill = COMPUTED_FILL
    ws.cell(row=r, column=2).border = THIN_BORDER

    # Conditional formatting: warn if blended margin < floor
    ws.conditional_formatting.add(
        f"C{margin_row}",
        FormulaRule(formula=[f"C{margin_row}<{refs['margin_floor']}"],
                     fill=NEGATIVE_FILL))

    # Column widths
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 16

    return refs


# ═══════════════════════════════════════════════════════════════════
# SHEET 3: PER-CAMPAIGN PLAN
# ═══════════════════════════════════════════════════════════════════

def build_per_campaign_plan_sheet(ws, rows: list[dict],
                                    refs: dict[str, str]) -> None:
    """Per-campaign destination + forecast.

    Layout (columns):
      A: campaign_name
      B: current_tcpa (from Baseline)
      C: destination_tcpa (USER INPUT — yellow)
      D: per_campaign_elasticity_override (USER INPUT, blank = use global)
      E: pct_tcpa_change (computed)
      F: effective_elasticity (computed)
      G: forecast_cpa
      H: pct_conv_lift
      I: forecast_conv
      J: forecast_revenue
      K: forecast_spend
      L: forecast_profit
      M: incremental_profit (vs current)
      N: forecast_roas
      O: forecast_margin
      P: status_flag (warning text)

    Data starts at row 5. Row 1-3 are header/instructions.
    """
    ws.cell(row=1, column=1, value="PER-CAMPAIGN PLAN").font = Font(
        bold=True, size=14, color="1F4E79")
    ws.cell(row=2, column=1,
            value="Set the destination tCPA per campaign (column C, yellow). "
                  "Optionally override elasticity per campaign (column D, "
                  "yellow). All other columns compute automatically.").alignment = (
        Alignment(wrap_text=True, vertical="top"))

    headers = [
        "campaign", "current\ntCPA", "destination\ntCPA",
        "per-campaign\nε override", "Δ% tCPA",
        "effective ε", "forecast CPA", "Δ% conv",
        "forecast conv", "forecast revenue",
        "forecast spend", "forecast profit", "incremental profit",
        "forecast ROAS", "forecast margin", "status",
    ]
    header_row = 4
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell.fill = HEADER_FILL_DARK
        cell.font = HEADER_FONT_WHITE
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[header_row].height = 38

    # Sort rows by campaign_name to match Baseline sheet order
    sorted_rows = sorted(rows, key=lambda r: r["campaign_name"])

    # Data starts at row 5
    data_start_row = 5
    baseline_data_start = 9  # Row where Baseline campaign data starts

    for idx, row_data in enumerate(sorted_rows):
        r = data_start_row + idx
        baseline_row = baseline_data_start + idx
        current_tcpa = row_data.get("current_tcpa") or 0

        # A: campaign name (from Baseline reference)
        ws.cell(row=r, column=1, value=f"=Baseline!A{baseline_row}")
        ws.cell(row=r, column=1).font = Font(bold=True)

        # B: current tCPA (from Baseline)
        ws.cell(row=r, column=2, value=f"=Baseline!C{baseline_row}")
        ws.cell(row=r, column=2).number_format = '"$"#,##0.00'

        # C: destination tCPA — USER INPUT
        # Pre-fill with profit_optimal_tcpa from the strategic model output
        # so the workbook lands in a meaningful state on first open.
        # User can override any cell by typing a new value.
        # Fallback: current_tcpa if profit_optimal_tcpa is missing.
        default_dest = (row_data.get("profit_optimal_tcpa")
                        if row_data.get("profit_optimal_tcpa") is not None
                        else current_tcpa)
        cell_c = ws.cell(row=r, column=3, value=default_dest)
        cell_c.fill = EDITABLE_FILL
        cell_c.border = THIN_BORDER
        cell_c.number_format = '"$"#,##0.00'

        # D: per-campaign elasticity override (blank = use global)
        cell_d = ws.cell(row=r, column=4, value=None)
        cell_d.fill = EDITABLE_FILL
        cell_d.border = THIN_BORDER
        cell_d.number_format = "0.00"

        # E: pct tCPA change
        ws.cell(row=r, column=5,
                value=f"=IFERROR((C{r}-B{r})/B{r},0)")
        ws.cell(row=r, column=5).number_format = "0.0%"

        # F: effective elasticity (per-campaign override OR global active)
        ws.cell(row=r, column=6,
                value=(f"=IF(ISNUMBER(D{r}),D{r},"
                       f"'Model Controls'!{refs['active_eps']})"))
        ws.cell(row=r, column=6).number_format = "0.00"

        # G: forecast CPA
        # When destination tCPA > current tCPA: forecast CPA scales up with
        # CPA scaling factor, capped at margin ceiling.
        # When destination tCPA <= current tCPA (no raise or tighten): forecast
        # CPA holds at current CPA (no model-artifact "savings").
        ws.cell(row=r, column=7,
                value=(f"=IF(C{r}>B{r},"
                       f"MIN("
                       f"Baseline!H{baseline_row}+(C{r}-B{r})*'Model Controls'!{refs['cpa_scaling']},"
                       f"Baseline!D{baseline_row}"
                       f"),"
                       f"Baseline!H{baseline_row}"
                       f")"))
        ws.cell(row=r, column=7).number_format = '"$"#,##0.00'

        # H: pct conv lift = pct_tcpa_change × effective_elasticity
        ws.cell(row=r, column=8, value=f"=E{r}*F{r}")
        ws.cell(row=r, column=8).number_format = "0.0%"

        # I: forecast conv = current_conv × (1 + pct_conv_lift)
        ws.cell(row=r, column=9,
                value=f"=Baseline!F{baseline_row}*(1+H{r})")
        ws.cell(row=r, column=9).number_format = "0"

        # J: forecast revenue = forecast_conv × forecast_rpl
        # forecast_rpl depends on RPL mode (Flat / Continue trend / Manual %)
        rpl_formula = (
            f"=IF('Model Controls'!{refs['rpl_mode']}=\"Flat\","
            f"Baseline!I{baseline_row},"
            f"IF('Model Controls'!{refs['rpl_mode']}=\"Continue current trend\","
            f"Baseline!I{baseline_row}+Baseline!L{baseline_row}*'Model Controls'!{refs['rpl_horizon_weeks']},"
            f"Baseline!I{baseline_row}*(1+'Model Controls'!{refs['rpl_manual_pct']})))"
        )
        # We're computing forecast revenue inline using the RPL formula
        ws.cell(row=r, column=10,
                value=f"=I{r}*({rpl_formula[1:]})")  # drop leading '='
        ws.cell(row=r, column=10).number_format = '"$"#,##0'

        # K: forecast spend = forecast_conv × forecast_cpa
        ws.cell(row=r, column=11, value=f"=I{r}*G{r}")
        ws.cell(row=r, column=11).number_format = '"$"#,##0'

        # L: forecast profit = revenue - spend
        ws.cell(row=r, column=12, value=f"=J{r}-K{r}")
        ws.cell(row=r, column=12).number_format = '"$"#,##0'

        # M: incremental profit = forecast profit - current profit
        ws.cell(row=r, column=13,
                value=f"=L{r}-Baseline!J{baseline_row}")
        ws.cell(row=r, column=13).number_format = '"$"#,##0'
        ws.cell(row=r, column=13).font = Font(bold=True)

        # N: forecast ROAS = revenue / spend
        ws.cell(row=r, column=14, value=f"=IFERROR(J{r}/K{r},0)")
        ws.cell(row=r, column=14).number_format = "0.0%"

        # O: forecast margin = profit / revenue
        ws.cell(row=r, column=15, value=f"=IFERROR(L{r}/J{r},0)")
        ws.cell(row=r, column=15).number_format = "0.0%"

        # P: status flag (warns on policy breaches)
        ws.cell(row=r, column=16,
                value=(f'=IF(G{r}>Baseline!D{baseline_row},"⚠ CPA > margin ceiling",'
                       f'IF(O{r}<\'Model Controls\'!{refs["margin_floor"]},"⚠ below margin floor",'
                       f'IF(E{r}>0.5,"⚠ large move (>50%)",'
                       f'IF(E{r}<=0,"hold","OK"))))'))
        ws.cell(row=r, column=16).font = Font(italic=True, size=10)

    # Conditional formatting for incremental profit (M column)
    end_row = data_start_row + len(sorted_rows) - 1
    ws.conditional_formatting.add(
        f"M{data_start_row}:M{end_row}",
        CellIsRule(operator="greaterThan", formula=["0"],
                   fill=POSITIVE_FILL))
    ws.conditional_formatting.add(
        f"M{data_start_row}:M{end_row}",
        CellIsRule(operator="lessThan", formula=["0"],
                   fill=NEGATIVE_FILL))

    # Conditional formatting for forecast margin (O column) - warn below floor
    ws.conditional_formatting.add(
        f"O{data_start_row}:O{end_row}",
        FormulaRule(formula=[f"O{data_start_row}<'Model Controls'!{refs['margin_floor']}"],
                     fill=NEGATIVE_FILL))

    # Conditional formatting for forecast CPA vs margin ceiling
    ws.conditional_formatting.add(
        f"G{data_start_row}:G{end_row}",
        FormulaRule(formula=[f"G{data_start_row}>Baseline!D{baseline_data_start}"],
                     fill=WARNING_FILL))

    # Totals row
    total_row = end_row + 2
    ws.cell(row=total_row, column=1, value="TOTAL").font = Font(bold=True, size=12)
    ws.cell(row=total_row, column=1).fill = SECTION_FILL
    for col_idx, col_letter in zip(
            [9, 10, 11, 12, 13],
            ["I", "J", "K", "L", "M"]):
        cell = ws.cell(row=total_row, column=col_idx,
                       value=f"=SUM({col_letter}{data_start_row}:{col_letter}{end_row})")
        cell.font = Font(bold=True)
        cell.fill = SECTION_FILL
        cell.border = THIN_BORDER
        if col_idx == 9:
            cell.number_format = "0"
        else:
            cell.number_format = '"$"#,##0'

    # Column widths
    ws.column_dimensions["A"].width = 50
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 13
    ws.column_dimensions["E"].width = 10
    ws.column_dimensions["F"].width = 12
    ws.column_dimensions["G"].width = 13
    ws.column_dimensions["H"].width = 10
    ws.column_dimensions["I"].width = 13
    ws.column_dimensions["J"].width = 16
    ws.column_dimensions["K"].width = 15
    ws.column_dimensions["L"].width = 15
    ws.column_dimensions["M"].width = 18
    ws.column_dimensions["N"].width = 14
    ws.column_dimensions["O"].width = 14
    ws.column_dimensions["P"].width = 28
    ws.freeze_panes = "B5"


# ═══════════════════════════════════════════════════════════════════
# SHEET 4: STAIR-STEP SCHEDULE
# ═══════════════════════════════════════════════════════════════════

def build_stairstep_sheet(ws, rows: list[dict],
                           refs: dict[str, str]) -> None:
    """Per-campaign stair-step rollout.

    Columns:
      A: campaign
      B: current_tcpa (from Plan)
      C: destination_tcpa (from Plan)
      D: cycles_to_destination (USER INPUT, default 4)
      E: step_size_pct (computed from D: equal % steps)
      F-K: cycle 1-6 tCPA values
      L-Q: cycle 1-6 forecasted profit values (at that cycle's tCPA)
    """
    ws.cell(row=1, column=1, value="STAIR-STEP SCHEDULE").font = Font(
        bold=True, size=14, color="1F4E79")
    ws.cell(row=2, column=1,
            value="For each campaign, set the number of cycles to reach "
                  "destination (column D, yellow). Default is 4 cycles. "
                  "Steps are equal-% per cycle. Cycle tCPAs are capped at "
                  "destination. Forecast profit at each cycle uses the "
                  "active elasticity from Model Controls.").alignment = (
        Alignment(wrap_text=True, vertical="top"))

    # Headers
    cycle_headers = [f"Cycle {i+1}\ntCPA" for i in range(MAX_CYCLES)]
    profit_headers = [f"Cycle {i+1}\nforecast profit" for i in range(MAX_CYCLES)]
    headers = ["campaign", "current\ntCPA", "destination\ntCPA",
               "cycles\nto dest.", "step\nsize %"] \
              + cycle_headers + profit_headers
    header_row = 4
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell.fill = HEADER_FILL_DARK
        cell.font = HEADER_FONT_WHITE
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[header_row].height = 38

    sorted_rows = sorted(rows, key=lambda r: r["campaign_name"])
    data_start_row = 5
    plan_data_start = 5  # Per-Campaign Plan starts at row 5

    for idx, row_data in enumerate(sorted_rows):
        r = data_start_row + idx
        plan_row = plan_data_start + idx
        baseline_row = 9 + idx

        # A: campaign name
        ws.cell(row=r, column=1, value=f"=Baseline!A{baseline_row}")
        ws.cell(row=r, column=1).font = Font(bold=True)

        # B: current tCPA
        ws.cell(row=r, column=2, value=f"=Baseline!C{baseline_row}")
        ws.cell(row=r, column=2).number_format = '"$"#,##0.00'

        # C: destination tCPA (from Plan sheet)
        ws.cell(row=r, column=3, value=f"='Per-Campaign Plan'!C{plan_row}")
        ws.cell(row=r, column=3).number_format = '"$"#,##0.00'

        # D: cycles to destination — USER INPUT, default 4
        cell_d = ws.cell(row=r, column=4, value=4)
        cell_d.fill = EDITABLE_FILL
        cell_d.border = THIN_BORDER
        cell_d.number_format = "0"

        # E: step size % = (destination - current) / current / cycles
        # Equal-% steps, applied compounding
        # Using formula: (destination/current)^(1/cycles) - 1
        ws.cell(row=r, column=5,
                value=(f"=IFERROR(IF(D{r}>0,"
                       f"(C{r}/B{r})^(1/D{r})-1,0),0)"))
        ws.cell(row=r, column=5).number_format = "0.0%"

        # F-K: cycle 1-6 tCPAs (capped at destination)
        for cycle_num in range(1, MAX_CYCLES + 1):
            col_idx = 5 + cycle_num  # F=6, G=7, ..., K=11
            col_letter = get_column_letter(col_idx)
            # cycle_N_tcpa = MIN(current × (1 + step_size_pct)^N, destination)
            #   if N <= cycles, otherwise = destination (capped/held)
            ws.cell(row=r, column=col_idx,
                    value=(f"=IF({cycle_num}<=D{r},"
                           f"MIN(B{r}*(1+E{r})^{cycle_num},C{r}),"
                           f"C{r})"))
            ws.cell(row=r, column=col_idx).number_format = '"$"#,##0.00'

        # L-Q: cycle 1-6 forecast profit at that cycle's tCPA
        # Forecast profit = forecast_conv × (rpl - forecast_cpa)
        # using cycle's tCPA as the destination tCPA
        for cycle_num in range(1, MAX_CYCLES + 1):
            tcpa_col_letter = get_column_letter(5 + cycle_num)
            profit_col_idx = 11 + cycle_num  # L=12, M=13, ..., Q=17
            # pct change in tCPA at this cycle relative to current
            # conv lift = pct_tcpa_change × elasticity
            # forecast_conv = current_conv × (1 + conv_lift)
            # forecast_cpa = tcpa_cycle × (current_cpa / current_tcpa)
            # forecast_rpl follows the same RPL mode logic
            # NOTE: simplified RPL handling here uses Plan sheet's effective ε
            # via 'Per-Campaign Plan' column F (effective elasticity)
            formula = (
                f"=IFERROR("
                # forecast conv at this cycle
                f"(Baseline!F{baseline_row}*"
                f"(1+(({tcpa_col_letter}{r}-B{r})/B{r})*'Per-Campaign Plan'!F{plan_row}))"
                f"*"
                # (rpl - cpa) per conv
                f"("
                # rpl (using RPL mode)
                f"IF('Model Controls'!{refs['rpl_mode']}=\"Flat\","
                f"Baseline!I{baseline_row},"
                f"IF('Model Controls'!{refs['rpl_mode']}=\"Continue current trend\","
                f"Baseline!I{baseline_row}+Baseline!L{baseline_row}*'Model Controls'!{refs['rpl_horizon_weeks']},"
                f"Baseline!I{baseline_row}*(1+'Model Controls'!{refs['rpl_manual_pct']})))"
                # - forecast_cpa at this cycle (CPA scaling only if raising)
                f"-IF({tcpa_col_letter}{r}>B{r},"
                f"MIN(Baseline!H{baseline_row}+({tcpa_col_letter}{r}-B{r})*'Model Controls'!{refs['cpa_scaling']},"
                f"Baseline!D{baseline_row}),"
                f"Baseline!H{baseline_row})"
                f")"
                f",0)"
            )
            ws.cell(row=r, column=profit_col_idx, value=formula)
            ws.cell(row=r, column=profit_col_idx).number_format = '"$"#,##0'

    # Totals row
    end_row = data_start_row + len(sorted_rows) - 1
    total_row = end_row + 2
    ws.cell(row=total_row, column=1, value="TOTAL profit by cycle").font = Font(
        bold=True, size=12)
    ws.cell(row=total_row, column=1).fill = SECTION_FILL
    # Sum the profit columns
    for cycle_num in range(1, MAX_CYCLES + 1):
        col_idx = 11 + cycle_num
        col_letter = get_column_letter(col_idx)
        cell = ws.cell(row=total_row, column=col_idx,
                       value=f"=SUM({col_letter}{data_start_row}:{col_letter}{end_row})")
        cell.font = Font(bold=True)
        cell.fill = SECTION_FILL
        cell.border = THIN_BORDER
        cell.number_format = '"$"#,##0'

    # Current total for comparison
    cur_total_row = total_row + 1
    ws.cell(row=cur_total_row, column=1,
            value="Current monthly profit (baseline reference)").font = Font(italic=True)
    ws.cell(row=cur_total_row, column=12,
            value=f"=SUM(Baseline!J9:J{8 + len(sorted_rows)})")
    ws.cell(row=cur_total_row, column=12).number_format = '"$"#,##0'
    ws.cell(row=cur_total_row, column=12).font = Font(italic=True)

    # Column widths
    ws.column_dimensions["A"].width = 50
    for col_letter in "BCDE":
        ws.column_dimensions[col_letter].width = 12
    for cycle_num in range(1, MAX_CYCLES + 1):
        ws.column_dimensions[get_column_letter(5 + cycle_num)].width = 12
        ws.column_dimensions[get_column_letter(11 + cycle_num)].width = 16
    ws.freeze_panes = "B5"


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_csv", help="Path to model_baseline CSV")
    parser.add_argument("--output", default=None,
                        help="Output xlsx path. Default: alongside the CSV.")
    args = parser.parse_args()

    csv_path = Path(args.baseline_csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        sys.exit(1)

    rows, metadata = load_baseline_csv(csv_path)
    print(f"Loaded {len(rows)} campaigns from {csv_path.name}")
    print(f"Metadata: {metadata}")

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = csv_path.with_suffix(".xlsx")
        out_path = out_path.with_name(
            out_path.name.replace("model_baseline", "profit_growth_model"))

    # Build workbook
    wb = Workbook()
    wb.remove(wb.active)

    # Sheet 1: Baseline
    ws1 = wb.create_sheet("Baseline")
    build_baseline_sheet(ws1, rows, metadata)

    # Sheet 2: Model Controls
    ws2 = wb.create_sheet("Model Controls")
    refs = build_model_controls_sheet(ws2, n_campaigns=len(rows))

    # Sheet 3: Per-Campaign Plan
    ws3 = wb.create_sheet("Per-Campaign Plan")
    build_per_campaign_plan_sheet(ws3, rows, refs)

    # Sheet 4: Stair-step Schedule
    ws4 = wb.create_sheet("Stair-step Schedule")
    build_stairstep_sheet(ws4, rows, refs)

    # Set Model Controls as the active/landing sheet
    wb.active = wb.sheetnames.index("Model Controls")

    wb.save(out_path)
    print(f"\n  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
