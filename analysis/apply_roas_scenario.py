"""
Apply the ROAS-driven destination tCPAs to the existing profit-growth Excel
model and report the calculated forecast.

This is a one-off scenario applier — it does NOT modify build_profit_growth_model.py.
The model builder remains a clean reflection of the strategic script's
profit_optimal_tcpa output. ROAS-driven destinations are manual overrides
applied on top.

Usage:
    python apply_roas_scenario.py <model.xlsx> [--output <out.xlsx>]

What it does:
    1. Opens the model file
    2. Sets destination tCPAs in Per-Campaign Plan column C to the
       ROAS-driven values below
    3. Sets per-campaign cycle counts in Stair-step Schedule column D
       to reflect the slow/normal pacing strategy
    4. Sets Model Controls: Scenario=Mid, RPL mode=Continue current trend
    5. Saves the result and prints the calculated forecast

Override the destinations or pacing here by editing ROAS_DESTINATIONS below.
"""

import argparse
import subprocess
from pathlib import Path
import sys

from openpyxl import load_workbook


# ═══════════════════════════════════════════════════════════════════
# ROAS-DRIVEN OVERRIDES
# ═══════════════════════════════════════════════════════════════════
#
# Each entry: (campaign_key, destination_tcpa, cycles_to_destination, notes)
# campaign_key matches the trailing campaign name (case-insensitive substring)
#
# Pacing:
#   2-3 cycles = "normal pace"  (both networks ROAS ≥ 1.2)
#   3-4 cycles = "slower pace"  (Audience ROAS at/below 1.2 OR sparse Audience needing dwell)

ROAS_DESTINATIONS = [
    # (key,           dest,   cycles, notes)
    ("TEXAS",         32.0,   4,      "Search ROAS 1.58, Audience 1.21 borderline — pace slow"),
    ("ILLINOIS",      34.0,   2,      "Both networks >1.2, Search 1.78 strong"),
    ("INDIANA",       30.0,   3,      "Search ROAS 1.23 borderline — hold or tiny step"),
    ("COLORADO",      35.0,   2,      "Hold at $35 — blended CPA already at target"),
    ("CALIFORNIA",    16.0,   2,      "Search ROAS 1.85 supports raise despite blended CPA above target"),
    ("GEORGIA",       29.0,   3,      "Search 1.52, Audience sparse"),
    ("VIRGINIA",      28.0,   4,      "Audience ROAS 1.09 <1.2 — pace slow, flag for split"),
    ("MASSACHUSETTS", 30.0,   3,      "Search ROAS 1.82 strong"),
    ("NORTH CAROLINA",22.0,   2,      "Both networks strong (Search 2.04, Audience 3.01)"),
    ("NEW JERSEY",    25.0,   4,      "Search ROAS 3.13 strongest in account; sparse volume needs dwell"),
    ("FLORIDA",       14.0,   4,      "Search 1.60, Audience dominates 53-82% of spend at 1.39 — pace slow"),
    ("KENTUCKY",      25.0,   1,      "Hold — excluded"),
]

# Model Controls scenarios to set (logical_name, value)
# RPL mode is "Flat" by default — "Continue current trend" linearly extrapolates
# the 12-week RPL slope over the full 12-week forecast horizon, which produces
# implausible numbers ($479K annualized in testing) because RPL momentum doesn't
# continue linearly. Use Flat for defensible central estimate; flip manually in
# Excel if you want to see the upside-case (but apply a shorter horizon first).
MODEL_CONTROLS_SETTINGS = {
    "scenario":    "Mid",                        # B7 — elasticity scenario
    "rpl_mode":    "Flat",                       # B17 — RPL mode
    # cpa_scaling left at default 0.5
}


# ═══════════════════════════════════════════════════════════════════
# IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════

def find_campaign_row(ws_baseline, key: str) -> int | None:
    """Find the row in the Baseline sheet whose campaign_name contains `key`."""
    # Baseline data starts at row 9
    for r in range(9, 30):
        name = ws_baseline.cell(row=r, column=1).value
        if name and key.upper() in str(name).upper():
            return r
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_xlsx", help="Path to profit_growth_model_*.xlsx")
    parser.add_argument("--output", default=None,
                        help="Output path (default: same file with '_roas' suffix)")
    parser.add_argument("--no-recalc", action="store_true",
                        help="Skip the libreoffice recalc step (just save the file with new values)")
    args = parser.parse_args()

    model_path = Path(args.model_xlsx)
    if not model_path.exists():
        print(f"ERROR: model not found: {model_path}")
        sys.exit(1)

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = model_path.with_name(model_path.stem + "_roas.xlsx")

    print(f"Reading {model_path.name}...")
    wb = load_workbook(model_path)

    # ── Apply Model Controls settings ────────────────────────────────
    ws_mc = wb["Model Controls"]
    # B7 = scenario, B17 = RPL mode — these are positional based on the
    # builder's layout. If the layout changes, these need updating.
    ws_mc["B7"] = MODEL_CONTROLS_SETTINGS["scenario"]
    ws_mc["B17"] = MODEL_CONTROLS_SETTINGS["rpl_mode"]
    print(f"  Model Controls: scenario={MODEL_CONTROLS_SETTINGS['scenario']}, "
          f"RPL mode={MODEL_CONTROLS_SETTINGS['rpl_mode']}")

    # ── Apply destination tCPAs to Per-Campaign Plan ─────────────────
    ws_plan = wb["Per-Campaign Plan"]
    ws_baseline = wb["Baseline"]
    ws_stair = wb["Stair-step Schedule"]

    # Per-Campaign Plan: data rows are sorted alphabetically starting row 5
    # Baseline: data rows start at row 9, also alphabetical
    # The Plan row index = baseline row index - 4 (5 vs 9, sorted same)
    print()
    print(f"  {'Campaign':<20s} {'cur':>5s} {'dest':>6s} {'cycles':>7s}  notes")
    print(f"  {'-'*20} {'-'*5} {'-'*6} {'-'*7}  {'-'*60}")

    applied_count = 0
    for key, dest, cycles, notes in ROAS_DESTINATIONS:
        baseline_row = find_campaign_row(ws_baseline, key)
        if baseline_row is None:
            print(f"  ⚠ {key:<20s} NOT FOUND in Baseline sheet")
            continue

        plan_row = baseline_row - 4  # baseline row 9 → plan row 5
        stair_row = baseline_row - 4  # same offset for stair-step

        # Set destination in Plan sheet column C
        ws_plan.cell(row=plan_row, column=3, value=dest)
        # Set cycles in Stair-step column D
        ws_stair.cell(row=stair_row, column=4, value=cycles)

        current = ws_baseline.cell(row=baseline_row, column=3).value
        print(f"  {key:<20s} ${current:>4.0f} ${dest:>5.0f} {cycles:>7d}  {notes[:55]}")
        applied_count += 1

    print()
    print(f"  Applied {applied_count} destination overrides")
    print(f"  Saving to {out_path}...")
    wb.save(out_path)

    # ── Recalculate via LibreOffice (so we can read the forecasted totals) ──
    if not args.no_recalc:
        # Check if libreoffice is available — on Windows with Excel only, skip
        import shutil
        if shutil.which("libreoffice") is None and shutil.which("soffice") is None:
            print()
            print("  ℹ LibreOffice not found — skipping sensitivity recalc.")
            print(f"  ℹ Open {out_path.name} in Excel to see calculated values.")
            print(f"  ℹ The file has destinations + cycles applied;")
            print(f"  ℹ change scenario (B7) or RPL mode (B17) in Model Controls to flex.")
            return

        print(f"  Running sensitivity across (Mid/Aggressive) × (Flat/Continue trend)...")
        recalc_dir = out_path.parent / "_recalc"
        recalc_dir.mkdir(exist_ok=True)

        # Pick libreoffice or soffice (Windows-installed LibreOffice uses 'soffice')
        loffice_cmd = shutil.which("libreoffice") or shutil.which("soffice")

        sensitivities = [
            ("Mid",        "Flat",                       "Conservative central estimate"),
            ("Aggressive", "Flat",                       "Aggressive flat-RPL"),
            ("Mid",        "Continue current trend",     "Mid elasticity + RPL momentum"),
            ("Aggressive", "Continue current trend",     "Aggressive + RPL momentum — UPPER BOUND only"),
        ]

        print()
        print("═" * 78)
        print("SENSITIVITY ACROSS SCENARIOS — monthly incremental profit")
        print("═" * 78)
        print(f"  {'Elasticity':<12s} {'RPL mode':<28s} {'Incr/mo':>12s} {'Annualized':>14s}  {'Notes':<40s}")
        print(f"  {'-'*12} {'-'*28} {'-'*12} {'-'*14}  {'-'*40}")

        for scenario, rpl_mode, notes in sensitivities:
            # Load fresh copy and set scenario
            wb_s = load_workbook(out_path)
            wb_s["Model Controls"]["B7"] = scenario
            wb_s["Model Controls"]["B17"] = rpl_mode
            scenario_path = recalc_dir / f"scenario_{scenario}_{rpl_mode.replace(' ', '_')}.xlsx"
            wb_s.save(scenario_path)

            result = subprocess.run(
                [loffice_cmd, "--headless", "--calc", "--convert-to", "xlsx",
                 str(scenario_path), "--outdir", str(recalc_dir / "out")],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"  ⚠ recalc failed for {scenario}/{rpl_mode}")
                continue

            evaluated = recalc_dir / "out" / scenario_path.name
            wb_eval = load_workbook(evaluated, data_only=True)
            ws_mc = wb_eval["Model Controls"]
            # Find the Profit row's "change" column (D)
            incr = None
            for r in range(34, 45):
                label = ws_mc.cell(row=r, column=1).value
                if label == "Profit":
                    incr = ws_mc.cell(row=r, column=4).value
                    break
            if incr is None:
                print(f"  ⚠ couldn't read incremental profit for {scenario}/{rpl_mode}")
                continue
            annualized = incr * 12
            print(f"  {scenario:<12s} {rpl_mode:<28s} ${incr:>10,.0f}  ${annualized:>12,.0f}  {notes:<40s}")

        print()
        print("Per-campaign breakdown (Mid elasticity + Flat RPL — central estimate):")
        # Use Mid/Flat case for per-campaign reporting
        central_path = recalc_dir / "out" / "scenario_Mid_Flat.xlsx"
        if central_path.exists():
            wb_eval = load_workbook(central_path, data_only=True)
            ws_plan = wb_eval["Per-Campaign Plan"]
            rows = []
            for r in range(5, 17):
                name = ws_plan.cell(row=r, column=1).value
                if not name: continue
                cur = ws_plan.cell(row=r, column=2).value
                dest = ws_plan.cell(row=r, column=3).value
                incr = ws_plan.cell(row=r, column=13).value
                status = ws_plan.cell(row=r, column=16).value
                rows.append((name, cur, dest, incr, status))
            rows.sort(key=lambda x: -(x[3] or 0))
            print(f"  {'Campaign':<32s} {'cur':>5s} {'dest':>5s} {'incr':>8s}  status")
            for name, cur, dest, incr, status in rows:
                short = name.replace("HOME INSURANCE-", "").split("-SEARCH")[0][:30]
                print(f"  {short:<32s} ${cur:>4.0f} ${dest:>4.0f} ${incr:>7,.0f}  {status}")

        print()
        print("Saved scenario files in:", recalc_dir / "out")


if __name__ == "__main__":
    main()
