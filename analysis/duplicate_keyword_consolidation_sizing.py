"""
duplicate_keyword_consolidation_sizing.py
==========================================
Sizes the lift from pausing the under-performing variant of duplicate
keywords (same keyword text in same campaign/ad group, both broad and
exact match types active).

INPUT
─────
  Google_duplicate_keyword_consolidation__Choose_between_exact_or_broad_match_type_.xlsx
  (sheet: "Match Type Conflicts (30d)")
  481 keyword pairs analyzed over 30 days (Mar 16 - Apr 14, 2026).

METHODOLOGY
───────────
For each duplicate-keyword pair, the input workbook classifies an
action via the Recommendation column. We bucket those into:

  PAUSE EXACT (43 pairs):  BROAD is productive; EXACT is wasting spend
                           Action: pause EXACT, keep BROAD
                           GP recovery = -(EXACT GP loss)

  PAUSE BROAD (41 pairs):  EXACT is productive; BROAD is wasting spend
                           Action: pause BROAD, keep EXACT
                           GP recovery = -(BROAD GP loss)

  PAUSE BROAD (idle, 22):  EXACT profitable; BROAD has no spend
                           Action: housekeeping pause; no GP impact

  KEEP BOTH (20):          Both profitable, low priority
                           Excluded from sizing

  BOTH BELOW BREAKEVEN (119): Both unprofitable; needs deeper review
                           Surfaced as SEPARATE row in workbook for
                           visibility — NOT in consolidation headline

  NO SPEND (236):          Both idle; nothing to size

HEADLINE = Pause EXACT + Pause BROAD pairs at 80% RECOVERY FACTOR.

The 80% factor accounts for:
  - Cannibalization: some BROAD-matched queries won't be captured by
    the kept EXACT keyword once BROAD is paused (the EXACT only matches
    its literal text + close variants)
  - Attribution: some EXACT-credited revenue may actually be downstream
    of BROAD-matched search queries; pausing BROAD could lose those
  - Behavior: query volume may shift to other keywords with unknown
    profitability

Industry-typical for match-type consolidation; conservative enough to be
defensible but honest about the recoverable signal.

OUTPUT — one workbook (duplicate_keyword_consolidation_sizing.xlsx):
  Metadata
  Sizing Summary             — unified GP/Month | Current GP/Day | Expected GP Lift
  Pause EXACT Detail          — per-pair detail for the 43 Pause EXACT recommendations
  Pause BROAD Detail          — per-pair detail for the 41 Pause BROAD recommendations
  Both Below Breakeven Detail — separate-row cohort (119 pairs, not in headline)
  Cohort Summary              — counts + dollars per recommendation bucket

USAGE
─────
  conda activate paid-search
  python analysis/duplicate_keyword_consolidation_sizing.py
  python analysis/duplicate_keyword_consolidation_sizing.py --recovery-factor 0.80
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DEFAULT_INPUT  = Path(r"C:\Projects\google-ads-ops\analysis\input\Google duplicate keyword consolidation-Choose between exact or broad match type.xlsx")
DEFAULT_OUTPUT = Path(r"C:\Projects\google-ads-ops\analysis\output\duplicate_keyword_consolidation_sizing.xlsx")

ANALYSIS_DAYS = 30   # Mar 16 - Apr 14, 2026 (per workbook)
DAYS_IN_MONTH = 30

# Recovery factor: % of theoretical GP recovery that materializes
# 1.00 = full recovery (assumes no cannibalization, no attribution loss)
# 0.80 = industry-typical conservative for match-type consolidation
# 0.50 = aggressive haircut for high-attribution-risk scenarios
DEFAULT_RECOVERY_FACTOR = 0.80


# ── Helpers ────────────────────────────────────────────────────────
def classify_recommendation(reco: str) -> str:
    """Bucket the freeform Recommendation column into action types."""
    if not isinstance(reco, str):
        return "Unknown"
    r = reco.strip()
    if r.startswith("Pause EXACT"):
        return "Pause EXACT"
    if r.startswith("Pause BROAD"):
        return "Pause BROAD"
    if r.startswith("EXACT profitable; BROAD has no meaningful spend"):
        return "Pause BROAD (idle)"
    if r.startswith("Keep both"):
        return "Keep both"
    if r.startswith("Both below breakeven"):
        return "Both below breakeven"
    if r.startswith("No spend in window"):
        return "No spend"
    return "Other"


def compute_gp_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add UI-derived GP columns to each row."""
    df = df.copy()
    df["BROAD_gp"] = df["BROAD — UI Revenue ($)"].fillna(0) - df["BROAD — UI Spend ($)"].fillna(0)
    df["EXACT_gp"] = df["EXACT — UI Revenue ($)"].fillna(0) - df["EXACT — UI Spend ($)"].fillna(0)
    df["combined_gp"] = df["BROAD_gp"] + df["EXACT_gp"]
    return df


def cohort_aggregate(df: pd.DataFrame, side_to_pause: str) -> dict:
    """
    Sum the GP impact of pausing one side of each pair.
    side_to_pause: 'BROAD' or 'EXACT'
    """
    paused_gp_col = f"{side_to_pause}_gp"
    kept_side = "EXACT" if side_to_pause == "BROAD" else "BROAD"
    kept_gp_col = f"{kept_side}_gp"

    return {
        "n_pairs":         len(df),
        "paused_side":     side_to_pause,
        "kept_side":       kept_side,
        "paused_spend":    df[f"{side_to_pause} — UI Spend ($)"].fillna(0).sum(),
        "paused_revenue":  df[f"{side_to_pause} — UI Revenue ($)"].fillna(0).sum(),
        "paused_gp":       df[paused_gp_col].sum(),
        "kept_spend":      df[f"{kept_side} — UI Spend ($)"].fillna(0).sum(),
        "kept_revenue":    df[f"{kept_side} — UI Revenue ($)"].fillna(0).sum(),
        "kept_gp":         df[kept_gp_col].sum(),
        # GP recovery (theoretical) = the negative GP being printed by the paused side
        "theoretical_recovery_gp_30d": -df[paused_gp_col].sum(),
    }


# ── Sheet builders ────────────────────────────────────────────────
def build_sizing_summary(pause_exact: dict, pause_broad: dict,
                          both_below: dict, recovery_factor: float) -> pd.DataFrame:
    """Unified GP/Month | Current GP/Day | Expected GP Lift schema."""
    rows = []

    def _row(initiative, scenario, current_gp_day, lift_day,
             total_gp_day, total_gp_month, note=""):
        rows.append({
            "Initiative":                   initiative,
            "Scenario":                     scenario,
            "Current GP / Day ($)":         round(float(current_gp_day or 0), 2),
            "Expected GP Lift / Day ($)":   round(float(lift_day or 0), 2),
            "Expected GP Lift / Month ($)": round(float((lift_day or 0) * DAYS_IN_MONTH), 2),
            "Total GP / Day ($)":           round(float(total_gp_day or 0), 2),
            "Total GP / Month ($)":         round(float(total_gp_month or 0), 2),
            "Note":                         note,
        })

    # ── Pause EXACT
    pe_recovery_30d  = pause_exact["theoretical_recovery_gp_30d"] * recovery_factor
    pe_lift_day      = pe_recovery_30d / ANALYSIS_DAYS
    pe_current_gp_day = pause_exact["paused_gp"] / ANALYSIS_DAYS  # negative — what's being burned today
    pe_total_gp_day  = pe_current_gp_day + pe_lift_day
    _row(
        initiative="Duplicate Keyword Consolidation — Pause EXACT cohort",
        scenario=f"{pause_exact['n_pairs']} pairs; pause EXACT, keep BROAD; "
                 f"recovery factor {recovery_factor*100:.0f}%",
        current_gp_day=pe_current_gp_day,
        lift_day=pe_lift_day,
        total_gp_day=pe_total_gp_day,
        total_gp_month=pe_total_gp_day * DAYS_IN_MONTH,
        note=f"30-day theoretical recovery: ${pause_exact['theoretical_recovery_gp_30d']:+,.0f}; "
             f"after {(1-recovery_factor)*100:.0f}% haircut for cannibalization/attribution: "
             f"${pe_recovery_30d:+,.0f}/30d.",
    )

    # ── Pause BROAD
    pb_recovery_30d  = pause_broad["theoretical_recovery_gp_30d"] * recovery_factor
    pb_lift_day      = pb_recovery_30d / ANALYSIS_DAYS
    pb_current_gp_day = pause_broad["paused_gp"] / ANALYSIS_DAYS
    pb_total_gp_day  = pb_current_gp_day + pb_lift_day
    _row(
        initiative="Duplicate Keyword Consolidation — Pause BROAD cohort",
        scenario=f"{pause_broad['n_pairs']} pairs; pause BROAD, keep EXACT; "
                 f"recovery factor {recovery_factor*100:.0f}%",
        current_gp_day=pb_current_gp_day,
        lift_day=pb_lift_day,
        total_gp_day=pb_total_gp_day,
        total_gp_month=pb_total_gp_day * DAYS_IN_MONTH,
        note=f"30-day theoretical recovery: ${pause_broad['theoretical_recovery_gp_30d']:+,.0f}; "
             f"after {(1-recovery_factor)*100:.0f}% haircut: ${pb_recovery_30d:+,.0f}/30d. "
             f"This is the larger of the two cohorts — most of the dollars live here.",
    )

    # ── Combined headline
    combined_current = pe_current_gp_day + pb_current_gp_day
    combined_lift    = pe_lift_day + pb_lift_day
    combined_total   = combined_current + combined_lift
    _row(
        initiative="Duplicate Keyword Consolidation — HEADLINE (both cohorts combined)",
        scenario=f"{pause_exact['n_pairs'] + pause_broad['n_pairs']} pairs total; "
                 f"recovery factor {recovery_factor*100:.0f}%",
        current_gp_day=combined_current,
        lift_day=combined_lift,
        total_gp_day=combined_total,
        total_gp_month=combined_total * DAYS_IN_MONTH,
        note=f"Combined GP lift from pausing the wasteful variant in each "
             f"flagged pair. 'Current GP/Day' is what's being lost TODAY by "
             f"the soon-to-be-paused variants.",
    )

    # ── Sensitivity rows
    for label, factor in [("Aggressive (100% recovery)", 1.00),
                           ("Conservative (50% recovery)", 0.50)]:
        sens_recovery = (pause_exact["theoretical_recovery_gp_30d"] +
                         pause_broad["theoretical_recovery_gp_30d"]) * factor
        sens_lift_day = sens_recovery / ANALYSIS_DAYS
        _row(
            initiative="Duplicate Keyword Consolidation — HEADLINE (both cohorts combined)",
            scenario=f"  └ sensitivity: {label}",
            current_gp_day=combined_current,
            lift_day=sens_lift_day,
            total_gp_day=combined_current + sens_lift_day,
            total_gp_month=(combined_current + sens_lift_day) * DAYS_IN_MONTH,
            note=f"Sensitivity around the {recovery_factor*100:.0f}% headline",
        )

    # ── Both Below Breakeven (separate row, not in headline)
    bb_recovery_30d  = both_below["theoretical_recovery_gp_30d"] * recovery_factor
    bb_lift_day      = bb_recovery_30d / ANALYSIS_DAYS
    bb_current_gp_day = both_below["paused_gp"] / ANALYSIS_DAYS  # combined GP across both sides
    bb_total_gp_day  = bb_current_gp_day + bb_lift_day
    _row(
        initiative="Both Below Breakeven cohort (SEPARATE — not in consolidation headline)",
        scenario=f"{both_below['n_pairs']} pairs; pause BOTH variants; "
                 f"recovery factor {recovery_factor*100:.0f}%",
        current_gp_day=bb_current_gp_day,
        lift_day=bb_lift_day,
        total_gp_day=bb_total_gp_day,
        total_gp_month=bb_total_gp_day * DAYS_IN_MONTH,
        note=f"Different intervention than match-type consolidation — these "
             f"pairs need 'pause both or restructure'. 30-day theoretical "
             f"recovery: ${both_below['theoretical_recovery_gp_30d']:+,.0f}; "
             f"at {recovery_factor*100:.0f}% recovery: ${bb_recovery_30d:+,.0f}/30d. "
             f"Surfaced for visibility; not part of the consolidation initiative scope.",
    )

    return pd.DataFrame(rows)


def build_pause_detail_sheet(df: pd.DataFrame, side_to_pause: str,
                              recovery_factor: float) -> pd.DataFrame:
    """Per-pair detail for one cohort."""
    paused_gp_col = f"{side_to_pause}_gp"
    kept_side = "EXACT" if side_to_pause == "BROAD" else "BROAD"
    kept_gp_col = f"{kept_side}_gp"

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "Campaign":               r["Campaign"],
            "Keyword":                r["Keyword"],
            f"{side_to_pause} (paused) Spend":   round(r[f"{side_to_pause} — UI Spend ($)"] or 0, 2),
            f"{side_to_pause} (paused) Revenue": round(r[f"{side_to_pause} — UI Revenue ($)"] or 0, 2),
            f"{side_to_pause} (paused) GP":      round(r[paused_gp_col], 2),
            f"{side_to_pause} (paused) ROAS":    round(r[f"{side_to_pause} — UI ROAS"] or 0, 3),
            f"{kept_side} (kept) Spend":      round(r[f"{kept_side} — UI Spend ($)"] or 0, 2),
            f"{kept_side} (kept) Revenue":    round(r[f"{kept_side} — UI Revenue ($)"] or 0, 2),
            f"{kept_side} (kept) GP":         round(r[kept_gp_col], 2),
            f"{kept_side} (kept) ROAS":       round(r[f"{kept_side} — UI ROAS"] or 0, 3),
            "Theoretical Recovery (30d)":     round(-r[paused_gp_col], 2),
            f"Recovery @ {recovery_factor*100:.0f}% factor": round(-r[paused_gp_col] * recovery_factor, 2),
            "Recommendation":         (r["Recommendation"][:200] if isinstance(r["Recommendation"], str) else ""),
        })
    df_out = pd.DataFrame(rows)
    if not df_out.empty:
        df_out = df_out.sort_values("Theoretical Recovery (30d)", ascending=False).reset_index(drop=True)
    return df_out


def build_both_below_detail(df: pd.DataFrame, recovery_factor: float) -> pd.DataFrame:
    """Per-pair detail for the Both Below Breakeven cohort."""
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "Campaign":           r["Campaign"],
            "Keyword":            r["Keyword"],
            "BROAD Spend":        round(r["BROAD — UI Spend ($)"] or 0, 2),
            "BROAD Revenue":      round(r["BROAD — UI Revenue ($)"] or 0, 2),
            "BROAD GP":           round(r["BROAD_gp"], 2),
            "BROAD ROAS":         round(r["BROAD — UI ROAS"] or 0, 3),
            "EXACT Spend":        round(r["EXACT — UI Spend ($)"] or 0, 2),
            "EXACT Revenue":      round(r["EXACT — UI Revenue ($)"] or 0, 2),
            "EXACT GP":           round(r["EXACT_gp"], 2),
            "EXACT ROAS":         round(r["EXACT — UI ROAS"] or 0, 3),
            "Combined GP loss (30d)":  round(r["combined_gp"], 2),
            f"Recovery @ {recovery_factor*100:.0f}% factor": round(-r["combined_gp"] * recovery_factor, 2),
        })
    df_out = pd.DataFrame(rows)
    if not df_out.empty:
        df_out = df_out.sort_values("Combined GP loss (30d)", ascending=True).reset_index(drop=True)
    return df_out


def build_cohort_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Counts + dollars per action bucket."""
    rows = []
    cohorts = df.groupby("action").agg(
        n_pairs=("Keyword", "count"),
        broad_spend=("BROAD — UI Spend ($)", "sum"),
        broad_revenue=("BROAD — UI Revenue ($)", "sum"),
        exact_spend=("EXACT — UI Spend ($)", "sum"),
        exact_revenue=("EXACT — UI Revenue ($)", "sum"),
        broad_gp=("BROAD_gp", "sum"),
        exact_gp=("EXACT_gp", "sum"),
        combined_gp=("combined_gp", "sum"),
    ).reset_index()
    for _, r in cohorts.iterrows():
        rows.append({
            "Action Bucket":          r["action"],
            "Pairs":                  int(r["n_pairs"]),
            "BROAD Spend (30d)":      round(r["broad_spend"], 2),
            "BROAD Revenue (30d)":    round(r["broad_revenue"], 2),
            "BROAD GP (30d)":         round(r["broad_gp"], 2),
            "EXACT Spend (30d)":      round(r["exact_spend"], 2),
            "EXACT Revenue (30d)":    round(r["exact_revenue"], 2),
            "EXACT GP (30d)":         round(r["exact_gp"], 2),
            "Combined GP (30d)":      round(r["combined_gp"], 2),
        })
    return pd.DataFrame(rows)


def build_metadata(input_path: Path, recovery_factor: float,
                   pause_exact: dict, pause_broad: dict,
                   both_below: dict) -> pd.DataFrame:
    rows = [
        ("Run timestamp",       datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Input workbook",      input_path.name),
        ("Analysis window",     f"{ANALYSIS_DAYS} days (Mar 16 - Apr 14, 2026)"),
        ("Recovery factor",     f"{recovery_factor*100:.0f}% "
                                 f"(20% haircut for cannibalization + attribution risk)"),
        ("", ""),
        ("Pause EXACT pairs",   f"{pause_exact['n_pairs']} (BROAD productive; EXACT wasteful)"),
        ("Pause BROAD pairs",   f"{pause_broad['n_pairs']} (EXACT productive; BROAD wasteful)"),
        ("Both Below Breakeven pairs",
         f"{both_below['n_pairs']} (surfaced separately; not in headline)"),
        ("", ""),
        ("Methodology",
         "For each duplicate pair, the input workbook's Recommendation column "
         "classifies the action. We bucket those, sum the GP loss being "
         "printed by the soon-to-be-paused variant in each pair, and apply "
         "the recovery factor. Recovery = -(paused variant's negative GP)."),
        ("Recovery factor rationale",
         "80% conservative for match-type consolidation. The 20% haircut "
         "covers: (a) cannibalization — some BROAD-matched queries won't "
         "match the kept EXACT keyword once BROAD is paused; (b) attribution "
         "— some EXACT-credited revenue may actually be downstream of "
         "BROAD-matched search queries, lost on pause; (c) query-volume "
         "shift to other keywords with unknown profitability."),
        ("Source of truth",     "UI columns (Recommendation column says the API "
                                 "was misattributing, so UI is correct)."),
    ]
    return pd.DataFrame(rows, columns=["Parameter", "Value"])


def format_workbook(writer):
    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        ws.freeze_panes = "A2"
        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None),
                         default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)


# ── Main ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--recovery-factor", type=float, default=DEFAULT_RECOVERY_FACTOR,
                        help=f"% of theoretical recovery that materializes "
                             f"(default {DEFAULT_RECOVERY_FACTOR})")
    args = parser.parse_args()

    if not (0 < args.recovery_factor <= 1.0):
        print(f"ERROR: --recovery-factor must be in (0, 1], got {args.recovery_factor}")
        sys.exit(1)

    t0 = time.time()
    input_path  = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Duplicate Keyword Consolidation Sizing ===")
    print(f"Input:  {input_path.name}")
    print(f"Output: {output_path}")
    print(f"Recovery factor: {args.recovery_factor*100:.0f}%")

    # Load input
    df = pd.read_excel(input_path, sheet_name="Match Type Conflicts (30d)")
    print(f"\nLoaded {len(df):,} keyword pairs")

    # Classify and compute GP
    df["action"] = df["Recommendation"].apply(classify_recommendation)
    df = compute_gp_columns(df)

    print(f"\nAction distribution:")
    for action, count in df["action"].value_counts().items():
        print(f"  {action:30s} {count:>5}")

    # Aggregate cohorts
    pe_df = df[df["action"] == "Pause EXACT"].copy()
    pb_df = df[df["action"] == "Pause BROAD"].copy()
    bb_df = df[df["action"] == "Both below breakeven"].copy()

    pause_exact = cohort_aggregate(pe_df, "EXACT")
    pause_broad = cohort_aggregate(pb_df, "BROAD")
    # For Both Below Breakeven we pause both, so paused_gp = combined_gp
    both_below = {
        "n_pairs":         len(bb_df),
        "paused_side":     "BOTH",
        "kept_side":       "NONE",
        "paused_gp":       bb_df["combined_gp"].sum(),
        "theoretical_recovery_gp_30d": -bb_df["combined_gp"].sum(),
    }

    print(f"\nPause EXACT  cohort: {pause_exact['n_pairs']:>3} pairs, "
          f"theoretical recovery ${pause_exact['theoretical_recovery_gp_30d']:+,.0f}/30d")
    print(f"Pause BROAD  cohort: {pause_broad['n_pairs']:>3} pairs, "
          f"theoretical recovery ${pause_broad['theoretical_recovery_gp_30d']:+,.0f}/30d")
    print(f"Both BB      cohort: {both_below['n_pairs']:>3} pairs, "
          f"theoretical recovery ${both_below['theoretical_recovery_gp_30d']:+,.0f}/30d")

    headline_lift = (pause_exact["theoretical_recovery_gp_30d"] +
                     pause_broad["theoretical_recovery_gp_30d"]) * args.recovery_factor
    print(f"\nHEADLINE (Pause EXACT + Pause BROAD @ {args.recovery_factor*100:.0f}%): "
          f"${headline_lift:+,.0f}/month")

    # Build sheets
    sheets = {
        "Metadata":              build_metadata(input_path, args.recovery_factor,
                                                  pause_exact, pause_broad, both_below),
        "Sizing Summary":        build_sizing_summary(pause_exact, pause_broad,
                                                       both_below, args.recovery_factor),
        "Pause EXACT Detail":    build_pause_detail_sheet(pe_df, "EXACT", args.recovery_factor),
        "Pause BROAD Detail":    build_pause_detail_sheet(pb_df, "BROAD", args.recovery_factor),
        "Both Below Breakeven":  build_both_below_detail(bb_df, args.recovery_factor),
        "Cohort Summary":        build_cohort_summary(df),
    }

    print(f"\nWriting {output_path}...")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {output_path.stat().st_size / 1024:.1f} KB  |  "
          f"Wall time: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
