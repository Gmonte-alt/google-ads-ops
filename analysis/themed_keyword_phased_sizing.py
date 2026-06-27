"""
themed_keyword_phased_sizing.py
================================
Sizes the GP impact of two phased theme initiatives for Home Insurance
Google paid search:

  PHASE 1: DKI + Themed Keywords in Existing Ad Groups
           Add Price/Compare-Best canonicals into their natural-fit
           existing ad groups. Use DKI in RSA copy for keyword-level
           personalization. No AG restructure.

  PHASE 2: Dedicated Themed Ad Groups (contingent on Phase 1 results)
           Restructure themed keywords into dedicated themed AGs with
           bespoke copy. Captures the structural / Smart-Bidding-signal
           benefits that DKI alone can't deliver. Sizing represents
           INCREMENTAL lift on top of Phase 1.

INPUT
─────
  HomeInsuranceAnalysisV3-final.xlsx (Search Query Analysis 30d sheet)

METHODOLOGY
───────────
Themable population is identified from the existing Query Group + Intent
Action taxonomy in the input workbook:

  Price theme       — Query Group "Cost / pricing informational" OR
                      query text contains: cheap, affordable, cost,
                      price, cheapest, lowest
  Compare/Best theme — Query Group "Compare / best" OR "Carrier reviews"
                       OR Intent Action in (compare, review)

Lift mechanism (same as keyword expansion, partially captured per phase):

  Phase 1:  -3% CPC efficiency + +7% CTR uplift
              (DKI captures ~70% of personalization mechanism; structural
              AG benefits are deferred to Phase 2)

  Phase 1+2: -5% CPC efficiency + +10% CTR uplift
              (Full theming mechanism: AG-level themed copy + Smart
              Bidding signal concentration)

  Phase 2 incremental = (Phase 1+2 lift) - (Phase 1 lift)

Math (per phase, applied to themable spend/revenue):
  new_revenue = old_revenue × (1 + ctr_lift)
  new_spend   = old_spend × (1 + ctr_lift) × (1 - cpc_efficiency)
  new_GP      = new_revenue - new_spend
  lift        = new_GP - old_GP

OUTPUT
──────
  themed_keyword_phased_sizing.xlsx
    Metadata
    Sizing Summary             — Phase 1, Phase 2 incremental, combined
    Themable Cohort Detail     — per-theme economics
    Sensitivity Grid           — both phases under multiple lift assumptions
    Themable Queries (audit)   — every query classified into a theme

USAGE
─────
  conda activate paid-search
  python analysis/themed_keyword_phased_sizing.py
  python analysis/themed_keyword_phased_sizing.py --input <path> --output <path>
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DEFAULT_INPUT  = Path(r"C:\Projects\google-ads-ops\analysis\input\HomeInsuranceAnalysisV3-final.xlsx")
DEFAULT_OUTPUT = Path(r"C:\Projects\google-ads-ops\analysis\output\themed_keyword_phased_sizing.xlsx")
DEFAULT_SHEET  = "Search Query Analysis (30d)"

ANALYSIS_DAYS = 30

# Phase 1: DKI in existing AGs — partial mechanism
PHASE1_CPC_EFFICIENCY = 0.03   # -3% CPC
PHASE1_CTR_LIFT       = 0.07   # +7% CTR

# Full (Phase 1 + Phase 2): dedicated themed AGs — full mechanism
FULL_CPC_EFFICIENCY = 0.05     # -5% CPC
FULL_CTR_LIFT       = 0.10     # +10% CTR

# Sensitivity grids
PHASE1_SENSITIVITY = [
    (0.02, 0.05, "Conservative"),
    (0.03, 0.07, "Headline"),
    (0.05, 0.10, "Aggressive (= Phase 1+2 combined)"),
]

PHASE2_INCREMENTAL_SENSITIVITY = [
    # (additional_cpc_efficiency, additional_ctr_lift, label)
    (0.02, 0.03, "Conservative incremental"),
    (0.02, 0.03, "Headline incremental"),  # default same as conservative for now
    (0.04, 0.06, "Aggressive incremental"),
]

# Theme classification keywords for the Search Query column
PRICE_KEYWORDS = ["cheap", "affordable", "cost", "price", "cheapest",
                   "lowest", "budget", "deal", "discount", "save"]


# ── Helpers ────────────────────────────────────────────────────────
def classify_theme(row) -> str:
    """Bucket a search query row into Price, Compare/Best, or Other."""
    qg = str(row.get("Query Group", "")) if pd.notna(row.get("Query Group")) else ""
    ia = str(row.get("Intent Action", "")) if pd.notna(row.get("Intent Action")) else ""
    sq = str(row.get("Search Query", "")).lower() if pd.notna(row.get("Search Query")) else ""

    # Price theme
    if "10. Cost / pricing" in qg:
        return "Price"
    if any(w in sq for w in PRICE_KEYWORDS):
        return "Price"

    # Compare / Best theme
    if "13. Compare" in qg or "12. Carrier reviews" in qg:
        return "Compare/Best"
    if ia in ("compare", "review"):
        return "Compare/Best"

    # Quote-intent: explicitly EXCLUDE from theme initiative
    if "Quote intent" in qg:
        return "Quote-intent (excluded)"

    return "Other"


def apply_uplift(spend: float, revenue: float,
                  cpc_efficiency: float, ctr_lift: float) -> dict:
    """Two-lever uplift model — same mechanism as keyword_expansion_sizing."""
    new_revenue = revenue * (1 + ctr_lift)
    new_spend   = spend   * (1 + ctr_lift) * (1 - cpc_efficiency)
    new_gp      = new_revenue - new_spend
    cur_gp      = revenue - spend
    return {
        "new_spend":   new_spend,
        "new_revenue": new_revenue,
        "new_gp":      new_gp,
        "lift_gp":     new_gp - cur_gp,
        "new_roas":    new_revenue / new_spend if new_spend > 0 else 0,
    }


# ── Sizing computation ────────────────────────────────────────────
def compute_themable_economics(df: pd.DataFrame) -> dict:
    """Aggregate themable-only spend/revenue/GP."""
    df = df.copy()
    df["theme"] = df.apply(classify_theme, axis=1)
    themable = df[df["theme"].isin(["Price", "Compare/Best"])].copy()

    by_theme = themable.groupby("theme").agg(
        n_queries=("Search Query", "count"),
        impressions=("Impressions", "sum"),
        clicks=("Clicks", "sum"),
        spend=("Spend ($)", "sum"),
        conversions=("Conversions", "sum"),
        revenue=("Revenue ($)", "sum"),
    ).reset_index()
    by_theme["gp"] = by_theme["revenue"] - by_theme["spend"]
    by_theme["roas"] = by_theme["revenue"] / by_theme["spend"]
    by_theme["ctr"] = by_theme["clicks"] / by_theme["impressions"]

    total_spend   = themable["Spend ($)"].sum()
    total_revenue = themable["Revenue ($)"].sum()
    total_gp      = total_revenue - total_spend

    return {
        "themable":     themable,
        "by_theme":     by_theme,
        "total_spend":  total_spend,
        "total_revenue": total_revenue,
        "total_gp":     total_gp,
        "n_queries":    len(themable),
    }


# ── Sheet builders ────────────────────────────────────────────────
def build_sizing_summary(econ: dict) -> pd.DataFrame:
    """Unified GP/Month | Current GP/Day | Expected GP Lift schema."""
    rows = []

    def _row(initiative, scenario, current_gp_day, lift_day,
             total_gp_day, total_gp_month, note=""):
        rows.append({
            "Initiative":                       initiative,
            "Scenario":                         scenario,
            "Current GP / Day ($)":             round(float(current_gp_day or 0), 2),
            "Expected GP Lift / Day ($)":       round(float(lift_day or 0), 2),
            "Expected GP Lift / Month ($)":     round(float((lift_day or 0) * ANALYSIS_DAYS), 2),
            "Total GP / Day ($)":               round(float(total_gp_day or 0), 2),
            "Total GP / Month ($)":             round(float(total_gp_month or 0), 2),
            "Note":                             note,
        })

    spend = econ["total_spend"]
    revenue = econ["total_revenue"]
    gp = econ["total_gp"]
    cur_gp_day = gp / ANALYSIS_DAYS

    # ── Phase 1 — HEADLINE
    p1 = apply_uplift(spend, revenue, PHASE1_CPC_EFFICIENCY, PHASE1_CTR_LIFT)
    p1_lift_day = p1["lift_gp"] / ANALYSIS_DAYS
    p1_total_gp_day = cur_gp_day + p1_lift_day
    _row(
        initiative="Phase 1: DKI + Themed Keywords in Existing Ad Groups",
        scenario=f"HEADLINE: -{PHASE1_CPC_EFFICIENCY*100:.0f}% CPC, "
                 f"+{PHASE1_CTR_LIFT*100:.0f}% CTR (DKI in existing AGs)",
        current_gp_day=cur_gp_day,
        lift_day=p1_lift_day,
        total_gp_day=p1_total_gp_day,
        total_gp_month=p1_total_gp_day * ANALYSIS_DAYS,
        note=f"Themable cohort: {econ['n_queries']:,} queries, "
             f"${spend:,.0f} spend, ${revenue:,.0f} revenue over 30d. "
             f"DKI captures keyword-level personalization without AG "
             f"restructure. Expected to deliver ~70% of full theming "
             f"mechanism. New ROAS: {p1['new_roas']:.2f}.",
    )

    # Phase 1 sensitivity
    for cpc_e, ctr_l, label in PHASE1_SENSITIVITY:
        if abs(cpc_e - PHASE1_CPC_EFFICIENCY) < 1e-9 and abs(ctr_l - PHASE1_CTR_LIFT) < 1e-9:
            continue   # skip headline; already shown
        u = apply_uplift(spend, revenue, cpc_e, ctr_l)
        sens_lift_day = u["lift_gp"] / ANALYSIS_DAYS
        _row(
            initiative="Phase 1: DKI + Themed Keywords in Existing Ad Groups",
            scenario=f"  └ sensitivity: {label} "
                     f"(-{cpc_e*100:.0f}% CPC, +{ctr_l*100:.0f}% CTR)",
            current_gp_day=cur_gp_day,
            lift_day=sens_lift_day,
            total_gp_day=cur_gp_day + sens_lift_day,
            total_gp_month=(cur_gp_day + sens_lift_day) * ANALYSIS_DAYS,
            note=f"Sensitivity around the headline. New ROAS: {u['new_roas']:.2f}.",
        )

    # ── Phase 2 — INCREMENTAL on top of Phase 1
    full = apply_uplift(spend, revenue, FULL_CPC_EFFICIENCY, FULL_CTR_LIFT)
    p2_incremental_lift = full["lift_gp"] - p1["lift_gp"]
    p2_lift_day = p2_incremental_lift / ANALYSIS_DAYS

    # The "current" baseline for Phase 2 is Phase 1's NEW state, not the original
    p2_current_gp_day = (p1["new_gp"]) / ANALYSIS_DAYS
    p2_total_gp_day = p2_current_gp_day + p2_lift_day

    _row(
        initiative="Phase 2: Dedicated Themed Ad Groups (INCREMENTAL on Phase 1)",
        scenario=f"HEADLINE incremental: -{(FULL_CPC_EFFICIENCY-PHASE1_CPC_EFFICIENCY)*100:.0f}% "
                 f"additional CPC, +{(FULL_CTR_LIFT-PHASE1_CTR_LIFT)*100:.0f}% additional CTR",
        current_gp_day=p2_current_gp_day,
        lift_day=p2_lift_day,
        total_gp_day=p2_total_gp_day,
        total_gp_month=p2_total_gp_day * ANALYSIS_DAYS,
        note=f"CONTINGENT on Phase 1 results. Phase 2 captures the "
             f"AG-restructure benefits (themed copy + Smart Bidding "
             f"signal concentration) that DKI alone can't deliver. "
             f"INCREMENTAL lift over Phase 1, not stacked. "
             f"Should NOT execute Phase 2 if Phase 1 fails to show "
             f"theme-specific volume concentration justifying AG "
             f"restructure.",
    )

    # ── Combined: Phase 1 + Phase 2 (full mechanism)
    full_lift_day = full["lift_gp"] / ANALYSIS_DAYS
    full_total_gp_day = cur_gp_day + full_lift_day
    _row(
        initiative="Combined: Phase 1 + Phase 2 (reference; full mechanism)",
        scenario=f"-{FULL_CPC_EFFICIENCY*100:.0f}% CPC, "
                 f"+{FULL_CTR_LIFT*100:.0f}% CTR (full theming)",
        current_gp_day=cur_gp_day,
        lift_day=full_lift_day,
        total_gp_day=full_total_gp_day,
        total_gp_month=full_total_gp_day * ANALYSIS_DAYS,
        note=f"Reference only — equals Phase 1 + Phase 2 combined. "
             f"Use Phase 1 + Phase 2 incremental rows above for the "
             f"actual tracker entries.",
    )

    return pd.DataFrame(rows)


def build_themable_detail(econ: dict) -> pd.DataFrame:
    """Per-theme economics."""
    rows = []
    for _, t in econ["by_theme"].iterrows():
        rows.append({
            "Theme":                t["theme"],
            "Queries":              int(t["n_queries"]),
            "Impressions (30d)":    int(t["impressions"]),
            "Clicks (30d)":         int(t["clicks"]),
            "Spend (30d)":          round(t["spend"], 2),
            "Revenue (30d)":        round(t["revenue"], 2),
            "GP (30d)":             round(t["gp"], 2),
            "GP/Day":               round(t["gp"] / ANALYSIS_DAYS, 2),
            "ROAS":                 round(t["roas"], 3),
            "CTR":                  f"{t['ctr']*100:.2f}%",
        })
    # Add total row
    rows.append({
        "Theme":                "TOTAL THEMABLE",
        "Queries":              int(econ["n_queries"]),
        "Impressions (30d)":    int(econ["by_theme"]["impressions"].sum()),
        "Clicks (30d)":         int(econ["by_theme"]["clicks"].sum()),
        "Spend (30d)":          round(econ["total_spend"], 2),
        "Revenue (30d)":        round(econ["total_revenue"], 2),
        "GP (30d)":             round(econ["total_gp"], 2),
        "GP/Day":               round(econ["total_gp"] / ANALYSIS_DAYS, 2),
        "ROAS":                 round(econ["total_revenue"]/econ["total_spend"], 3)
                                 if econ["total_spend"] else 0,
        "CTR":                  "—",
    })
    return pd.DataFrame(rows)


def build_sensitivity_grid(econ: dict) -> pd.DataFrame:
    """Full sensitivity grid for both phases."""
    rows = []
    spend = econ["total_spend"]
    revenue = econ["total_revenue"]

    # Phase 1 standalone scenarios
    for cpc_e, ctr_l, label in PHASE1_SENSITIVITY:
        u = apply_uplift(spend, revenue, cpc_e, ctr_l)
        rows.append({
            "Phase":                "Phase 1 only",
            "Scenario":             label,
            "CPC Efficiency":       f"-{cpc_e*100:.0f}%",
            "CTR Lift":             f"+{ctr_l*100:.0f}%",
            "Lift GP / 30d":        round(u["lift_gp"], 2),
            "Lift GP / Day":        round(u["lift_gp"] / ANALYSIS_DAYS, 2),
            "Lift GP / Month":      round(u["lift_gp"], 2),  # same as 30d here
            "New ROAS":             round(u["new_roas"], 3),
        })

    # Phase 1 + Phase 2 combined scenarios (showing the full math)
    p1 = apply_uplift(spend, revenue, PHASE1_CPC_EFFICIENCY, PHASE1_CTR_LIFT)
    full = apply_uplift(spend, revenue, FULL_CPC_EFFICIENCY, FULL_CTR_LIFT)
    p2_incremental_lift = full["lift_gp"] - p1["lift_gp"]
    rows.append({
        "Phase":                "Phase 2 INCREMENTAL",
        "Scenario":             "Headline (full - Phase 1)",
        "CPC Efficiency":       f"-{(FULL_CPC_EFFICIENCY-PHASE1_CPC_EFFICIENCY)*100:.0f}% additional",
        "CTR Lift":             f"+{(FULL_CTR_LIFT-PHASE1_CTR_LIFT)*100:.0f}% additional",
        "Lift GP / 30d":        round(p2_incremental_lift, 2),
        "Lift GP / Day":        round(p2_incremental_lift / ANALYSIS_DAYS, 2),
        "Lift GP / Month":      round(p2_incremental_lift, 2),
        "New ROAS":             round(full["new_roas"], 3),
    })
    rows.append({
        "Phase":                "Phase 1 + 2 COMBINED",
        "Scenario":             "Reference (full mechanism)",
        "CPC Efficiency":       f"-{FULL_CPC_EFFICIENCY*100:.0f}%",
        "CTR Lift":             f"+{FULL_CTR_LIFT*100:.0f}%",
        "Lift GP / 30d":        round(full["lift_gp"], 2),
        "Lift GP / Day":        round(full["lift_gp"] / ANALYSIS_DAYS, 2),
        "Lift GP / Month":      round(full["lift_gp"], 2),
        "New ROAS":             round(full["new_roas"], 3),
    })

    return pd.DataFrame(rows)


def build_themable_audit(econ: dict) -> pd.DataFrame:
    """Per-query audit — sample of the top themable queries by spend."""
    df = econ["themable"].copy()
    df = df.sort_values("Spend ($)", ascending=False).head(200)
    cols = ["theme", "Search Query", "Query Group", "Intent Action",
            "Ad Group", "Keyword", "Match Type", "Targeting Status",
            "Impressions", "Clicks", "Spend ($)", "Revenue ($)", "Query ROAS"]
    return df[[c for c in cols if c in df.columns]]


def build_metadata(input_path: Path, econ: dict) -> pd.DataFrame:
    rows = [
        ("Run timestamp",       datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Input workbook",      input_path.name),
        ("Source sheet",        DEFAULT_SHEET),
        ("Analysis window",     f"{ANALYSIS_DAYS} days"),
        ("", ""),
        ("Themable queries",    f"{econ['n_queries']:,}"),
        ("Themable spend (30d)", f"${econ['total_spend']:,.2f}"),
        ("Themable revenue (30d)", f"${econ['total_revenue']:,.2f}"),
        ("Themable GP (30d)",   f"${econ['total_gp']:,.2f}"),
        ("", ""),
        ("Phase 1 hypothesis",
         f"-{PHASE1_CPC_EFFICIENCY*100:.0f}% CPC efficiency + "
         f"+{PHASE1_CTR_LIFT*100:.0f}% CTR lift "
         f"(DKI in existing AGs; ~70% of full theming mechanism)"),
        ("Phase 2 hypothesis (full mechanism)",
         f"-{FULL_CPC_EFFICIENCY*100:.0f}% CPC efficiency + "
         f"+{FULL_CTR_LIFT*100:.0f}% CTR lift "
         f"(dedicated themed AGs; full theming mechanism)"),
        ("Phase 2 INCREMENTAL",
         f"-{(FULL_CPC_EFFICIENCY-PHASE1_CPC_EFFICIENCY)*100:.0f}% additional CPC + "
         f"+{(FULL_CTR_LIFT-PHASE1_CTR_LIFT)*100:.0f}% additional CTR "
         f"(captures AG-restructure benefits beyond DKI)"),
        ("", ""),
        ("Theme classification",
         "Price = Query Group 'Cost / pricing informational' OR query "
         "text contains cheap/affordable/cost/price/cheapest/lowest/"
         "budget/deal/discount/save. Compare/Best = Query Group "
         "'Compare / best' OR 'Carrier reviews' OR Intent Action in "
         "(compare, review)."),
        ("Excluded from sizing",
         "Quote-intent queries (already commercial-intent; theming "
         "doesn't add) and Other (no theme modifier)."),
        ("", ""),
        ("Phase 2 contingency",
         "Phase 2 should NOT be executed unless Phase 1 data shows "
         "theme-specific volume concentration justifying dedicated AGs. "
         "Phase 1 is the measurement vehicle."),
    ]
    return pd.DataFrame(rows, columns=["Parameter", "Value"])


def format_workbook(writer):
    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        ws.freeze_panes = "A2"
        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None),
                         default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 60)


# ── Main ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--sheet",  default=DEFAULT_SHEET)
    args = parser.parse_args()

    t0 = time.time()
    input_path  = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Themed Keyword Phased Sizing ===")
    print(f"Input:  {input_path.name}")
    print(f"Sheet:  {args.sheet}")

    # Load + classify
    print("\nLoading search query data...")
    df = pd.read_excel(input_path, sheet_name=args.sheet)
    print(f"  Loaded {len(df):,} search query rows")

    print("\nClassifying themes + computing economics...")
    econ = compute_themable_economics(df)
    print(f"  Themable queries: {econ['n_queries']:,}")
    print(f"  Themable spend (30d):   ${econ['total_spend']:,.2f}")
    print(f"  Themable revenue (30d): ${econ['total_revenue']:,.2f}")
    print(f"  Themable GP (30d):      ${econ['total_gp']:,.2f}")
    print(f"\n  By theme:")
    for _, t in econ["by_theme"].iterrows():
        print(f"    {t['theme']:15s}  {int(t['n_queries']):>5} queries  "
              f"${t['spend']:>10,.0f} spend  GP ${t['gp']:>8,.0f}  ROAS {t['roas']:.2f}")

    # Headline
    spend, revenue = econ["total_spend"], econ["total_revenue"]
    p1 = apply_uplift(spend, revenue, PHASE1_CPC_EFFICIENCY, PHASE1_CTR_LIFT)
    full = apply_uplift(spend, revenue, FULL_CPC_EFFICIENCY, FULL_CTR_LIFT)
    p2_inc = full["lift_gp"] - p1["lift_gp"]

    print(f"\n  Phase 1 (DKI in existing AGs):       ${p1['lift_gp']:+,.2f}/month")
    print(f"  Phase 2 (incremental over Phase 1):  ${p2_inc:+,.2f}/month")
    print(f"  Combined (full mechanism):           ${full['lift_gp']:+,.2f}/month")

    # Build sheets
    sheets = {
        "Metadata":              build_metadata(input_path, econ),
        "Sizing Summary":        build_sizing_summary(econ),
        "Themable Cohort Detail": build_themable_detail(econ),
        "Sensitivity Grid":      build_sensitivity_grid(econ),
        "Themable Queries":      build_themable_audit(econ),
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
