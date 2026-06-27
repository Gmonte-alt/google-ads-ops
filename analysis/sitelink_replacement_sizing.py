"""
sitelink_replacement_sizing.py
==============================
Sizes the lift from replacing the 3 weakest sitelinks called out in
the Sitelink_Performance_Analysis-Google-HomeInsurance.xlsx workbook
(analysis period: Mar 5 - Apr 15, 2026; 42 days).

TWO LIFT METHODOLOGIES SHOWN AS ALTERNATIVE VIEWS (NOT ADDITIVE)
────────────────────────────────────────────────────────────────
  Method A — Narrow: direct lift on the 3 replaced sitelinks only.
              Replacement sitelinks are assumed to perform at the
              MONITOR average (CTR 5.27%, Conv Rate 19.66%) — a
              conservative aspiration vs. CHAMPION average. The lift
              is almost entirely Conv-Rate-driven (bottom 3 CTRs are
              already close to MONITOR average).

  Method B — Broad: Quality-Score-driven CPC efficiency lift across
              ALL sitelink-attributed parent-ad spend. Mechanism:
              better sitelinks → higher QS on the parent ad → lower
              CPCs at the same auction position. Industry rule of
              thumb (Google): 10-20% lift when adding sitelinks; we
              use 5% as a conservative incremental for replacing 3
              of 16 existing sitelinks. Revenue holds; spend drops.

Methods A and B answer different questions:
  - A: "How much do these specific sitelinks drag us down?"
  - B: "What's the QS lever worth on our sitelink-rendering traffic?"

They are not summed because the QS effect partially captures whatever
the bottom 3 sitelinks contribute to (or detract from) parent-ad QS.
The reader picks which lens to size from.

INPUT
─────
  /mnt/user-data/uploads/Sitelink_Performance_Analysis-Google-HomeInsurance.xlsx
  (or specify --input)

OUTPUT
──────
  sitelink_replacement_sizing.xlsx
    Metadata
    Sizing Summary               — unified GP/Month | Current GP/Day | Expected GP Lift
    Method A Narrow Detail       — bottom 3 today vs replacement projection
    Method B Broad Detail        — full sitelink cohort under sensitivity grid
    Inputs                       — sitelink performance details (audit copy)

USAGE
─────
  conda activate paid-search
  python analysis/sitelink_replacement_sizing.py
  python analysis/sitelink_replacement_sizing.py --input <path> --output <path>
  python analysis/sitelink_replacement_sizing.py --account-roas 1.55
"""

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DEFAULT_INPUT  = Path(r"C:\Projects\google-ads-ops\analysis\input\Sitelink_Performance_Analysis-Google-HomeInsurance.xlsx")
DEFAULT_OUTPUT = Path(r"C:\Projects\google-ads-ops\analysis\output\sitelink_replacement_sizing.xlsx")

ANALYSIS_DAYS = 42  # Mar 5 - Apr 15, 2026 (per workbook)
DAYS_IN_MONTH = 30

# The 3 weak sitelinks called out in the Executive Summary
WEAK_SITELINKS = [
    "Calculate Your Savings",
    "Highly Rated Providers",
    "Shop Online & Save Big",
]

# Account-level ROAS proxy (per user instruction — use prior workbook value)
DEFAULT_ACCOUNT_ROAS = 1.60

# Haircut applied to Method A's lift to hedge against ROAS-proxy
# uncertainty (the workbook is the Google Ads UI export with no
# revenue data; we derive revenue from cost × account_roas, which
# may overstate sitelink-attributed value-per-conversion). 0.0 = no
# haircut (full Method A lift); 0.5 = take 50% of the lift.
DEFAULT_HAIRCUT_PCT = 0.50

# Method A — "replacement performs at MONITOR average"
# (We're NOT assuming new sitelinks become CHAMPIONs; that would be too
# optimistic for a conservative estimate. MONITOR average is the existing
# pool's middle, a defensible aspiration for a fresh sitelink.)

# Method B — efficiency lift assumptions
# Google's "10-20% lift" claim applies to ADDING sitelinks (vs none).
# Replacing 3 of 16 existing sitelinks is incremental; conservative anchor
# is well below the headline. Sensitivity grid covers a defensible range.
METHOD_B_HEADLINE_LIFT_PCT = 0.05   # 5% CPC efficiency lift (conservative)
METHOD_B_SENSITIVITY = [0.03, 0.05, 0.08, 0.10]


# ── Sizing math ────────────────────────────────────────────────────
def compute_method_a(sitelink_df: pd.DataFrame, account_roas: float,
                      haircut_pct: float = 0.0) -> dict:
    """
    Replacement targets: bottom 3 sitelinks → MONITOR-average CTR & conv-rate.

    Holds impressions constant for the 3 replaced sitelinks (Google still
    shows them at the same rate, just with new copy). Lift comes from:
      - small CTR delta (bottom 3 ≈ 4.8%, MONITOR avg ≈ 5.27%)
      - meaningful Conv-Rate delta (bottom 3 14.3% → MONITOR avg 19.66%)

    Cost is held flat (clicks barely change). Revenue scales with
    conversions × value-per-conv. GP lift = added revenue.
    """
    # Identify sitelinks
    weak_mask = sitelink_df["Sitelink Text"].isin(WEAK_SITELINKS)
    monitor_mask = sitelink_df["Recommendation"].str.contains("MONITOR", na=False)

    weak    = sitelink_df[weak_mask].copy()
    monitor = sitelink_df[monitor_mask].copy()

    # Pool MONITOR averages — use the MEDIAN of the MONITOR sitelinks (NOT
    # the impression-weighted average). The weighted avg is dominated by
    # one or two high-volume MONITORs (e.g. "Rates For Seniors" with 261K
    # impressions and 31.75% CR), which is not representative of what a
    # new replacement sitelink should be expected to achieve. Median is
    # the conservative defensible anchor for "typical MONITOR performance."
    mon_avg_ctr  = float(monitor["CTR"].median())
    mon_avg_cr   = float(monitor["Conv Rate"].median())

    # Today's bottom-3 totals (over the 42-day window)
    cur_imp   = weak["Total Impressions"].sum()
    cur_click = weak["Total Clicks"].sum()
    cur_conv  = weak["Total Conversions"].sum()
    cur_cost  = weak["Total Cost"].sum()
    cur_cpc   = (cur_cost / cur_click) if cur_click else 0
    cur_cr    = (cur_conv / cur_click) if cur_click else 0
    cur_ctr   = (cur_click / cur_imp) if cur_imp else 0

    # Value per conversion implied by account ROAS:
    # account_roas = revenue / spend → revenue = spend × ROAS
    # value_per_conv = revenue / conversions = spend × ROAS / conversions
    cur_revenue = cur_cost * account_roas
    cur_gp      = cur_revenue - cur_cost
    value_per_conv = (cur_revenue / cur_conv) if cur_conv else 0

    # Projected: same impressions, MONITOR-average CTR & CR
    new_click  = cur_imp * mon_avg_ctr
    new_conv   = new_click * mon_avg_cr
    new_cost   = new_click * cur_cpc        # holding CPC constant
    new_revenue = new_conv * value_per_conv  # holding value-per-conv constant
    new_gp      = new_revenue - new_cost

    # Per-day and per-month
    cur_gp_day = cur_gp / ANALYSIS_DAYS
    new_gp_day = new_gp / ANALYSIS_DAYS
    raw_lift_gp_day = new_gp_day - cur_gp_day

    # Apply haircut (hedge against ROAS-proxy uncertainty)
    haircut_factor = 1.0 - haircut_pct
    lift_gp_day = raw_lift_gp_day * haircut_factor

    return {
        "weak_sitelinks":    weak,
        "monitor_avg_ctr":   mon_avg_ctr,
        "monitor_avg_cr":    mon_avg_cr,
        "current": {
            "impressions": cur_imp,
            "clicks":      cur_click,
            "conversions": cur_conv,
            "cost":        cur_cost,
            "revenue":     cur_revenue,
            "gp":          cur_gp,
            "ctr":         cur_ctr,
            "cr":          cur_cr,
            "cpc":         cur_cpc,
            "value_per_conv": value_per_conv,
            "gp_per_day":  cur_gp_day,
        },
        "projected": {
            "impressions": cur_imp,    # held constant
            "clicks":      new_click,
            "conversions": new_conv,
            "cost":        new_cost,
            "revenue":     new_revenue,
            "gp":          new_gp,
            "gp_per_day":  new_gp_day,
        },
        "lift_gp_day":      lift_gp_day,
        "lift_gp_month":    lift_gp_day * DAYS_IN_MONTH,
        "raw_lift_gp_day":  raw_lift_gp_day,
        "raw_lift_gp_month": raw_lift_gp_day * DAYS_IN_MONTH,
        "haircut_pct":      haircut_pct,
    }


def compute_method_b(sitelink_df: pd.DataFrame, account_roas: float,
                     lift_pct: float) -> dict:
    """
    Quality-Score-driven efficiency lift on the FULL sitelink-attributed
    cohort. Mechanism: replacing weak sitelinks improves the parent ad's
    QS, which lowers CPCs at the same auction position.

    Math (revenue holds, spend drops):
      new_spend  = current_spend × (1 - lift_pct)
      new_revenue = current_revenue
      lift_GP    = current_spend × lift_pct
    """
    total_cost = sitelink_df["Total Cost"].sum()
    total_conv = sitelink_df["Total Conversions"].sum()
    total_revenue = total_cost * account_roas
    total_gp = total_revenue - total_cost

    new_cost = total_cost * (1 - lift_pct)
    new_revenue = total_revenue
    new_gp = new_revenue - new_cost
    lift_gp = new_gp - total_gp

    cur_gp_day = total_gp / ANALYSIS_DAYS
    new_gp_day = new_gp / ANALYSIS_DAYS
    lift_gp_day = lift_gp / ANALYSIS_DAYS

    return {
        "lift_pct":          lift_pct,
        "current_cost":      total_cost,
        "current_revenue":   total_revenue,
        "current_gp":        total_gp,
        "new_cost":          new_cost,
        "new_revenue":       new_revenue,
        "new_gp":            new_gp,
        "lift_gp":           lift_gp,
        "cur_gp_day":        cur_gp_day,
        "new_gp_day":        new_gp_day,
        "lift_gp_day":       lift_gp_day,
        "lift_gp_month":     lift_gp_day * DAYS_IN_MONTH,
        "cur_gp_month":      cur_gp_day * DAYS_IN_MONTH,
    }


# ── Sheet builders ────────────────────────────────────────────────
def build_sizing_summary(method_a: dict, method_b_runs: list,
                          account_roas: float) -> pd.DataFrame:
    """Unified GP/Month | Current GP/Day | Expected GP Lift schema."""
    rows = []

    def _row(initiative, scenario, current_gp_day, lift_day,
             total_gp_day, total_gp_month, note=""):
        rows.append({
            "Initiative":                       initiative,
            "Scenario":                         scenario,
            "Current GP / Day ($)":             round(float(current_gp_day or 0), 2),
            "Expected GP Lift / Day ($)":       round(float(lift_day or 0), 2),
            "Expected GP Lift / Month ($)":     round(float((lift_day or 0) * DAYS_IN_MONTH), 2),
            "Total GP / Day ($)":               round(float(total_gp_day or 0), 2),
            "Total GP / Month ($)":             round(float(total_gp_month or 0), 2),
            "Note":                             note,
        })

    # ── Method A — narrow lift on the 3 replaced sitelinks
    cur_gp_day_a  = method_a["current"]["gp_per_day"]
    lift_day_a    = method_a["lift_gp_day"]
    total_gp_day_a = cur_gp_day_a + lift_day_a
    haircut_pct = method_a.get("haircut_pct", 0.0)
    haircut_label = (f", with {haircut_pct*100:.0f}% haircut applied"
                     if haircut_pct > 0 else "")
    _row(
        initiative="Sitelink Replacement — Method A (Narrow: bottom 3 only)",
        scenario=f"HEADLINE: replacements at MONITOR median "
                 f"(CTR {method_a['monitor_avg_ctr']*100:.2f}%, "
                 f"Conv Rate {method_a['monitor_avg_cr']*100:.2f}%)"
                 f"{haircut_label}",
        current_gp_day=cur_gp_day_a,
        lift_day=lift_day_a,
        total_gp_day=total_gp_day_a,
        total_gp_month=total_gp_day_a * DAYS_IN_MONTH,
        note=f"Lift on the 3 replaced sitelinks ONLY. Holds impressions and "
             f"CPC constant; lift driven primarily by Conv-Rate gain "
             f"(bottom 3 CR {method_a['current']['cr']*100:.2f}% → "
             f"MONITOR median {method_a['monitor_avg_cr']*100:.2f}%). "
             f"Bottom 3 are 2.1% of total sitelink spend. "
             f"Raw lift before haircut: ${method_a['raw_lift_gp_month']:+,.0f}/mo; "
             f"haircut hedges ROAS-proxy uncertainty (UI export has no "
             f"revenue, only conversion counts).",
    )

    # ── Method B — broad QS-efficiency lift on full sitelink cohort
    # Find the headline run
    headline_run = next(r for r in method_b_runs
                         if abs(r["lift_pct"] - METHOD_B_HEADLINE_LIFT_PCT) < 1e-9)
    cur_gp_day_b = headline_run["cur_gp_day"]

    # Headline row
    _row(
        initiative="Sitelink Replacement — Method B (Broad: QS efficiency lift)",
        scenario=f"HEADLINE: {METHOD_B_HEADLINE_LIFT_PCT*100:.0f}% CPC efficiency "
                 f"lift on full sitelink-attributed cohort",
        current_gp_day=cur_gp_day_b,
        lift_day=headline_run["lift_gp_day"],
        total_gp_day=headline_run["new_gp_day"],
        total_gp_month=headline_run["new_gp_day"] * DAYS_IN_MONTH,
        note=f"Mechanism: improved sitelinks lift parent-ad Quality Score → "
             f"lower CPC at same auction position. Revenue holds; spend "
             f"drops by lift_pct. ROAS proxy: {account_roas:.2f} (account-wide). "
             f"Industry reference: Google cites 10-20% for ADDING sitelinks; "
             f"5% is a conservative incremental for replacing 3 of 16 existing.",
    )

    # Sensitivity rows for Method B
    for run in method_b_runs:
        if abs(run["lift_pct"] - METHOD_B_HEADLINE_LIFT_PCT) < 1e-9:
            continue
        _row(
            initiative="Sitelink Replacement — Method B (Broad: QS efficiency lift)",
            scenario=f"  └ sensitivity: {run['lift_pct']*100:.0f}% lift",
            current_gp_day=run["cur_gp_day"],
            lift_day=run["lift_gp_day"],
            total_gp_day=run["new_gp_day"],
            total_gp_month=run["new_gp_day"] * DAYS_IN_MONTH,
            note=f"Sensitivity around the {METHOD_B_HEADLINE_LIFT_PCT*100:.0f}% headline",
        )

    # Reading guidance row
    _row(
        initiative="Sitelink Replacement — Reading Guidance",
        scenario="Methods A and B are alternative views, NOT additive",
        current_gp_day=0,
        lift_day=0,
        total_gp_day=0,
        total_gp_month=0,
        note=f"Method A measures the direct effect of replacing the 3 specific "
             f"sitelinks; Method B measures the QS/CPC efficiency lever on the "
             f"full sitelink cohort. Method B's lift partially CAPTURES whatever "
             f"the bottom 3 contribute (or fail to contribute) to QS, so summing "
             f"would double-count. Pick the lens that matches your stakeholder "
             f"question: A for 'these specific sitelinks are dragging us down', "
             f"B for 'fixing sitelinks improves the parent ad'.",
    )

    return pd.DataFrame(rows)


def build_method_a_detail(method_a: dict) -> pd.DataFrame:
    """Bottom 3 today vs replacement projection."""
    rows = []
    cur = method_a["current"]
    proj = method_a["projected"]

    def _add(label, key_cur, key_proj=None, fmt="${:,.2f}", divisor=1):
        c = cur.get(key_cur)
        p = proj.get(key_proj or key_cur) if key_proj or key_cur in proj else None
        rows.append({
            "Metric (over 42-day window)":  label,
            "Current (bottom 3)":           fmt.format(c / divisor) if c is not None else None,
            "Projected (after replacement)": fmt.format(p / divisor) if p is not None else None,
            "Δ":                            fmt.format((p - c) / divisor)
                                              if (p is not None and c is not None) else None,
        })

    rows.append({
        "Metric (over 42-day window)": "Impressions",
        "Current (bottom 3)":          f"{cur['impressions']:,}",
        "Projected (after replacement)": f"{proj['impressions']:,}",
        "Δ":                           f"{proj['impressions'] - cur['impressions']:,} (held constant)",
    })
    rows.append({
        "Metric (over 42-day window)": "Clicks",
        "Current (bottom 3)":          f"{cur['clicks']:,}",
        "Projected (after replacement)": f"{int(round(proj['clicks'])):,}",
        "Δ":                           f"{int(round(proj['clicks']-cur['clicks'])):+,}",
    })
    rows.append({
        "Metric (over 42-day window)": "CTR",
        "Current (bottom 3)":          f"{cur['ctr']*100:.2f}%",
        "Projected (after replacement)": f"{method_a['monitor_avg_ctr']*100:.2f}%",
        "Δ":                           f"{(method_a['monitor_avg_ctr']-cur['ctr'])*100:+.2f}pp",
    })
    rows.append({
        "Metric (over 42-day window)": "Conversions",
        "Current (bottom 3)":          f"{cur['conversions']:.1f}",
        "Projected (after replacement)": f"{proj['conversions']:.1f}",
        "Δ":                           f"{proj['conversions']-cur['conversions']:+.1f}",
    })
    rows.append({
        "Metric (over 42-day window)": "Conversion Rate",
        "Current (bottom 3)":          f"{cur['cr']*100:.2f}%",
        "Projected (after replacement)": f"{method_a['monitor_avg_cr']*100:.2f}%",
        "Δ":                           f"{(method_a['monitor_avg_cr']-cur['cr'])*100:+.2f}pp",
    })
    rows.append({
        "Metric (over 42-day window)": "Cost",
        "Current (bottom 3)":          f"${cur['cost']:,.2f}",
        "Projected (after replacement)": f"${proj['cost']:,.2f}",
        "Δ":                           f"${proj['cost']-cur['cost']:+,.2f}",
    })
    rows.append({
        "Metric (over 42-day window)": "Revenue (at ROAS proxy)",
        "Current (bottom 3)":          f"${cur['revenue']:,.2f}",
        "Projected (after replacement)": f"${proj['revenue']:,.2f}",
        "Δ":                           f"${proj['revenue']-cur['revenue']:+,.2f}",
    })
    rows.append({
        "Metric (over 42-day window)": "GP",
        "Current (bottom 3)":          f"${cur['gp']:,.2f}",
        "Projected (after replacement)": f"${proj['gp']:,.2f}",
        "Δ":                           f"${proj['gp']-cur['gp']:+,.2f}",
    })
    rows.append({
        "Metric (over 42-day window)": "GP/Day",
        "Current (bottom 3)":          f"${cur['gp_per_day']:,.2f}",
        "Projected (after replacement)": f"${proj['gp_per_day']:,.2f}",
        "Δ":                           f"${proj['gp_per_day']-cur['gp_per_day']:+,.2f}",
    })
    rows.append({
        "Metric (over 42-day window)": "Implied Value/Conversion",
        "Current (bottom 3)":          f"${cur['value_per_conv']:,.2f}",
        "Projected (after replacement)": f"${cur['value_per_conv']:,.2f}",
        "Δ":                           "(held constant — assumes same lead quality)",
    })
    rows.append({
        "Metric (over 42-day window)": "Raw Lift GP/Month (before haircut)",
        "Current (bottom 3)":          "—",
        "Projected (after replacement)": "—",
        "Δ":                           f"${method_a['raw_lift_gp_month']:+,.2f}",
    })
    rows.append({
        "Metric (over 42-day window)": f"Haircut Applied ({method_a['haircut_pct']*100:.0f}%)",
        "Current (bottom 3)":          "—",
        "Projected (after replacement)": "—",
        "Δ":                           f"${method_a['raw_lift_gp_month'] * method_a['haircut_pct']:-,.2f}"
                                       if method_a['haircut_pct'] > 0 else "(none)",
    })
    rows.append({
        "Metric (over 42-day window)": "Net Lift GP/Month (after haircut)",
        "Current (bottom 3)":          "—",
        "Projected (after replacement)": "—",
        "Δ":                           f"${method_a['lift_gp_month']:+,.2f}",
    })
    return pd.DataFrame(rows)


def build_method_b_detail(method_b_runs: list, account_roas: float) -> pd.DataFrame:
    """Full sitelink cohort under sensitivity grid."""
    rows = []
    for run in method_b_runs:
        rows.append({
            "Lift Assumption":      f"{run['lift_pct']*100:.0f}%",
            "Current Spend (42d)":  f"${run['current_cost']:,.2f}",
            "Current Revenue (42d)": f"${run['current_revenue']:,.2f}",
            "Current GP (42d)":     f"${run['current_gp']:,.2f}",
            "Projected Spend (42d)": f"${run['new_cost']:,.2f}",
            "Projected Revenue (42d)": f"${run['new_revenue']:,.2f}",
            "Projected GP (42d)":   f"${run['new_gp']:,.2f}",
            "Lift GP (42d)":        f"${run['lift_gp']:+,.2f}",
            "Lift GP/Day":          f"${run['lift_gp_day']:+,.2f}",
            "Lift GP/Month":        f"${run['lift_gp_month']:+,.2f}",
        })
    return pd.DataFrame(rows)


def build_metadata(input_path: Path, account_roas: float,
                   sitelink_df: pd.DataFrame,
                   haircut_pct: float = 0.0) -> pd.DataFrame:
    rows = [
        ("Run timestamp",          datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Input workbook",         input_path.name),
        ("Analysis window",        "Mar 5 - Apr 15, 2026 (42 days)"),
        ("Replacement scope",      f"Bottom 3 sitelinks: {', '.join(WEAK_SITELINKS)}"),
        ("Total active sitelinks", len(sitelink_df)),
        ("Total sitelink spend (42d)", f"${sitelink_df['Total Cost'].sum():,.2f}"),
        ("Total sitelink conversions (42d)", f"{sitelink_df['Total Conversions'].sum():,.1f}"),
        ("", ""),
        ("Account ROAS proxy",     f"{account_roas:.2f} (per user instruction; "
                                    f"used to derive revenue from cost)"),
        ("Method A headline",      f"Replacements perform at MONITOR median "
                                    f"(CTR/CR — conservative anchor)"),
        ("Method A haircut",        f"{int(round(haircut_pct*100))}% applied to raw lift "
                                    f"(hedges ROAS-proxy uncertainty)"),
        ("Method B headline",      f"{METHOD_B_HEADLINE_LIFT_PCT*100:.0f}% CPC "
                                    f"efficiency lift on sitelink-attributed cohort"),
        ("Method B sensitivity",   ", ".join(f"{p*100:.0f}%" for p in METHOD_B_SENSITIVITY)),
        ("", ""),
        ("Methodology — Method A",
         "Holds impressions and CPC constant for bottom 3. Replacement copy "
         "is assumed to perform at the MEDIAN of MONITOR sitelinks (not the "
         "impression-weighted average — that's dominated by a few high-volume "
         "outliers and overstates what a fresh sitelink should achieve). "
         "Lift = revenue from improved CR (and small CTR delta), "
         "minus added cost from extra clicks. A haircut is then applied to "
         "the raw lift to hedge ROAS-proxy uncertainty (the UI export has "
         "no revenue, only conversion counts; we derive revenue using "
         "account_roas which may overstate sitelink-attributed value)."),
        ("Methodology — Method B",
         "Quality-Score-driven CPC efficiency lift on full sitelink cohort. "
         "Mechanism: improved sitelink rotation → higher QS → lower CPCs at "
         "same auction position. Spend drops by lift_pct; revenue holds. "
         "Reference: Google cites 10-20% for ADDING sitelinks. We use 5% "
         "headline as conservative for REPLACING 3 of 16 existing sitelinks."),
        ("Reading guidance",
         "Methods A and B are ALTERNATIVE VIEWS of the same intervention, "
         "NOT additive. Method B's lift partially captures whatever the "
         "bottom 3 contribute to (or detract from) parent-ad QS. "
         "Method B's number dominates Method A's by ~120x — the QS/CPC "
         "efficiency story is the real value lever."),
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
    parser.add_argument("--account-roas", type=float, default=DEFAULT_ACCOUNT_ROAS,
                        help=f"Account-wide ROAS proxy (default {DEFAULT_ACCOUNT_ROAS})")
    parser.add_argument("--haircut-pct", type=float, default=DEFAULT_HAIRCUT_PCT,
                        help=f"Method A lift haircut to hedge ROAS-proxy "
                             f"uncertainty. 0.0 = full lift, 0.5 = halve the "
                             f"lift (default {DEFAULT_HAIRCUT_PCT})")
    args = parser.parse_args()

    t0 = time.time()
    input_path  = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Sitelink Replacement Sizing ===")
    print(f"Input:  {input_path.name}")
    print(f"Output: {output_path}")
    print(f"Account ROAS proxy: {args.account_roas:.2f}")

    # Load sitelink performance details
    sitelink_df = pd.read_excel(input_path, sheet_name="Sitelink Performance Details")
    print(f"\nLoaded {len(sitelink_df)} sitelinks")
    print(f"Total spend (42d): ${sitelink_df['Total Cost'].sum():,.2f}")
    print(f"Total conversions (42d): {sitelink_df['Total Conversions'].sum():,.1f}")

    # Validate weak sitelinks are present
    missing = [w for w in WEAK_SITELINKS if w not in sitelink_df["Sitelink Text"].values]
    if missing:
        print(f"\nWARNING: weak sitelinks not found in input: {missing}")

    # Method A
    print(f"\nComputing Method A (narrow)...")
    method_a = compute_method_a(sitelink_df, args.account_roas, args.haircut_pct)
    print(f"  Bottom 3 today GP/day: ${method_a['current']['gp_per_day']:,.2f}")
    print(f"  Projected GP/day:      ${method_a['projected']['gp_per_day']:,.2f}")
    print(f"  Raw lift GP/month:     ${method_a['raw_lift_gp_month']:+,.2f}")
    print(f"  Haircut applied:       {args.haircut_pct*100:.0f}%")
    print(f"  Net lift GP/day:       ${method_a['lift_gp_day']:+,.2f}")
    print(f"  Net lift GP/month:     ${method_a['lift_gp_month']:+,.2f}")

    # Method B (sensitivity grid)
    print(f"\nComputing Method B (broad QS efficiency)...")
    method_b_runs = [compute_method_b(sitelink_df, args.account_roas, p)
                     for p in METHOD_B_SENSITIVITY]
    for run in method_b_runs:
        print(f"  Lift {run['lift_pct']*100:5.1f}%: GP/day {run['lift_gp_day']:+,.2f}, "
              f"GP/month {run['lift_gp_month']:+,.2f}")

    # Build sheets
    sheets = {
        "Metadata":              build_metadata(input_path, args.account_roas,
                                                  sitelink_df, args.haircut_pct),
        "Sizing Summary":        build_sizing_summary(method_a, method_b_runs, args.account_roas),
        "Method A Narrow Detail": build_method_a_detail(method_a),
        "Method B Broad Detail":  build_method_b_detail(method_b_runs, args.account_roas),
        "Inputs":                 sitelink_df,
    }

    print(f"\nWriting {output_path}...")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {output_path.stat().st_size / 1024:.1f} KB  |  "
          f"Wall time: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
