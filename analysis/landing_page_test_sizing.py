"""
landing_page_test_sizing.py
=============================
Sizes the GP impact of the home-state-in-headline-v2 landing page A/B test
on homeinsurance.quote.com.

INPUT
─────
  - Active_Tests__RPV.csv               (test results from Looker)
  - A_B_Test_Performance_Report.csv     (per-arm conversion details)
  - Campaign_report__6_.csv             (Google Ads UI export, device break-out)

METHODOLOGY
───────────
The test improves landing page conversion rate. For visitors who hit the
test (already paid for via Google Ads), a higher conversion rate produces
more leads at the same media cost. So:

  Incremental revenue = RPV lift × incremental visitors
  Incremental media cost = $0  (already paid; same paid clicks)
  Incremental GP = Incremental revenue × 1.0

EXTRAPOLATION CHOICE
────────────────────
Test pool is ~9.6% of Google Ads paid clicks (consistent across all three
devices: 9.5% mobile, 10.1% desktop, 10.0% tablet). Industry-standard
GA4-vs-Google-Ads gap is 10-30%, not 90% — so the missing 90% of paid
clicks isn't tracking loss; it's that the test only RUNS on ~10% of
paid traffic (likely a subset of pages or a 10% allocation rule).

We size on the TEST-POOL basis (extrapolate the observed test traffic
to a monthly volume), NOT on full Google Ads paid traffic. The variant's
effect can only be measured on traffic the test sees.

  Monthly test-pool visitors = (test_visitors / test_days) × 30

Three rollout scenarios (per user request):
  Scenario 1: Mobile-only (the optimistic / headline case)
  Scenario 2: Mobile + Tablet (positive directional cells)
  Scenario 3: All three devices weighted (honest pooled view; currently
              negative due to large desktop loss)

CONFIDENCE FRAMING
──────────────────
The test ran 9 days and is statistically underpowered:
  Mobile:  P=0.71  (71% chance the +8.3% lift is noise)
  Desktop: P=0.31  (31% chance the −23.7% lift is noise)
  Tablet:  P=0.41  (small sample, ignore)

Per-row confidence is surfaced in the workbook's Sizing Summary so the
tracker reflects the underpowered status of the test.

OUTPUT
──────
  landing_page_test_sizing.xlsx
    Metadata
    Sizing Summary       — three scenarios, with confidence flags
    Test Detail          — per-device test results (RPV, P-value, etc.)
    Coverage Analysis    — GA4 visitors vs Google Ads clicks; coverage %
    Inputs (audit)       — copies of the three input tables

USAGE
─────
  conda activate paid-search
  python analysis/landing_page_test_sizing.py
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DEFAULT_RPV_CSV     = Path(r"C:\Projects\google-ads-ops\analysis\input\test-csv\Active-Tests_ RPV.csv")
DEFAULT_AB_CSV      = Path(r"C:\Projects\google-ads-ops\analysis\input\test-csv\A_B-Test-Performance-Report.csv")
DEFAULT_GADS_CSV    = Path(r"C:\Projects\google-ads-ops\analysis\input\test-csv\Campaign-report_6.csv")
DEFAULT_OUTPUT      = Path(r"C:\Projects\google-ads-ops\analysis\output\landing_page_test_sizing.xlsx")

ANALYSIS_DAYS = 9        # Apr 27 - May 5, 2026
DAYS_IN_MONTH = 30
DOMAIN_FILTER = "homeinsurance.quote.com"

# Map GA4 device labels to Google Ads device labels
GA4_TO_GADS_DEVICE = {
    "mobile":  "Mobile phones",
    "desktop": "Computers",
    "tablet":  "Tablets",
}


# ── Helpers ────────────────────────────────────────────────────────
def load_rpv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, thousands=',')
    df = df[df["Domain"] == DOMAIN_FILTER].copy()
    # Some columns come in as strings with commas; normalize
    for col in ["Control Visitors", "Variant Visitors",
                "Control Revenue", "Variant Revenue"]:
        if df[col].dtype == "object":
            df[col] = df[col].astype(str).str.replace(",", "", regex=False)
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_gads(path: Path) -> pd.DataFrame:
    """
    Read the Google Ads UI export. The file has a 2-line preamble
    (title + date range) before the actual header row.
    """
    df = pd.read_csv(path, skiprows=2, thousands=',')
    df = df.dropna(subset=["Campaign"])
    # Normalize numeric cols
    for col in ["Cost", "Conv. value", "Conversions", "GP",
                "Impr.", "Interactions"]:
        if col in df.columns and df[col].dtype == "object":
            df[col] = (df[col].astype(str)
                                .str.replace("$", "", regex=False)
                                .str.replace(",", "", regex=False)
                                .str.replace(" ", "", regex=False))
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_ab(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, thousands=',')
    df = df[df["Domain"] == DOMAIN_FILTER].copy() if "Domain" in df.columns else df
    return df


def gads_device_rollup(df: pd.DataFrame) -> dict:
    """Return per-device interactions for the Google Ads paid traffic."""
    if "Device" not in df.columns:
        return {}
    agg = df.groupby("Device").agg(
        interactions=("Interactions", "sum"),
        cost=("Cost", "sum"),
    ).to_dict("index")
    return agg


# ── Sizing math ────────────────────────────────────────────────────
def per_device_sizing(rpv_df: pd.DataFrame, gads_devices: dict) -> list:
    """
    For each device row in the test data, compute:
      - test pool monthly extrapolation
      - per-visitor RPV lift
      - monthly GP lift (= revenue lift; no incremental media cost)
      - confidence/P-value
      - coverage % vs Google Ads paid clicks
    """
    out = []
    for _, r in rpv_df.iterrows():
        ga4_device = r["Device Type"]   # 'mobile', 'desktop', 'tablet'
        gads_device = GA4_TO_GADS_DEVICE.get(ga4_device.lower(), "")
        gads_data = gads_devices.get(gads_device, {})
        gads_clicks = gads_data.get("interactions", 0) or 0

        # Test pool: control + variant visitors
        test_pool_visitors = r["Control Visitors"] + r["Variant Visitors"]
        # Monthly extrapolation of test-pool traffic
        monthly_test_pool = (test_pool_visitors / ANALYSIS_DAYS) * DAYS_IN_MONTH

        # RPV lift in dollars (variant - control)
        rpv_lift_dollars = r["Variant Mean RPV"] - r["Control Mean RPV"]
        rpv_lift_pct = r["RPV Lift %"]

        # Monthly revenue lift if we deploy variant to the test-pool population
        # (rolls out variant assignment to 100% of the test-pool, not the 50/50 split)
        monthly_rev_lift = monthly_test_pool * rpv_lift_dollars
        monthly_gp_lift = monthly_rev_lift   # incremental media cost = 0

        # Coverage % (GA4 test pool vs Google Ads clicks for this device)
        gads_monthly_clicks = (gads_clicks / ANALYSIS_DAYS) * DAYS_IN_MONTH
        coverage_pct = test_pool_visitors / gads_clicks if gads_clicks else 0

        out.append({
            "device":               ga4_device,
            "control_visitors":     r["Control Visitors"],
            "variant_visitors":     r["Variant Visitors"],
            "test_pool_visitors":   test_pool_visitors,
            "monthly_test_pool":    monthly_test_pool,
            "control_rpv":          r["Control Mean RPV"],
            "variant_rpv":          r["Variant Mean RPV"],
            "rpv_lift_dollars":     rpv_lift_dollars,
            "rpv_lift_pct":         rpv_lift_pct,
            "monthly_rev_lift":     monthly_rev_lift,
            "monthly_gp_lift":      monthly_gp_lift,
            "p_value":              r["P Value"],
            "confidence":           r["Confidence"],
            "statistical_power":    r["Statistical Power"],
            "winner":               r["Winner"],
            "gads_clicks_window":   gads_clicks,
            "gads_monthly_clicks":  gads_monthly_clicks,
            "coverage_pct":         coverage_pct,
        })
    # Sort: mobile, desktop, tablet (largest test pool first)
    order = {"mobile": 0, "desktop": 1, "tablet": 2}
    out.sort(key=lambda x: order.get(x["device"].lower(), 99))
    return out


# ── Sheet builders ────────────────────────────────────────────────
def build_sizing_summary(per_device: list) -> pd.DataFrame:
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

    by_dev = {d["device"].lower(): d for d in per_device}

    # Per-device rows for visibility
    for d in per_device:
        lift_day = d["monthly_gp_lift"] / DAYS_IN_MONTH
        # "Current" GP/day attributable to test-pool visitors at control RPV
        cur_rev_day = (d["monthly_test_pool"] / DAYS_IN_MONTH) * d["control_rpv"]
        cur_gp_day = cur_rev_day  # see methodology — incremental media cost is what cancels
        # NOTE: we don't actually track CURRENT GP this way because the test pool's
        # control arm has its own media cost embedded. We just show the LIFT.
        # Use 0 for current_gp_day on per-device rows; the rollup rows below show
        # the meaningful aggregate.
        _row(
            initiative="Landing Page Test — per-device read",
            scenario=f"{d['device'].capitalize()} only "
                     f"(RPV lift {d['rpv_lift_pct']*100:+.1f}%, "
                     f"P={d['p_value']:.2f})",
            current_gp_day=0,
            lift_day=lift_day,
            total_gp_day=lift_day,
            total_gp_month=lift_day * DAYS_IN_MONTH,
            note=f"Test pool: {d['test_pool_visitors']:,.0f} visitors over "
                 f"{ANALYSIS_DAYS} days → {d['monthly_test_pool']:,.0f}/month. "
                 f"RPV lift ${d['rpv_lift_dollars']:+.2f}. "
                 f"Confidence: {d['confidence']*100:.0f}% "
                 f"({d['winner']}). "
                 f"Coverage: {d['coverage_pct']*100:.1f}% of Google Ads "
                 f"{d['device']} clicks.",
        )

    # ── Three rollout scenarios
    mobile = by_dev.get("mobile", {})
    desktop = by_dev.get("desktop", {})
    tablet = by_dev.get("tablet", {})

    # SCENARIO 1: Mobile-only (HEADLINE — optimistic)
    if mobile:
        lift_month = mobile.get("monthly_gp_lift", 0)
        _row(
            initiative="Landing Page Test — HEADLINE (Mobile-only rollout)",
            scenario=f"Roll out to mobile only "
                     f"(RPV lift {mobile['rpv_lift_pct']*100:+.1f}%, "
                     f"P={mobile['p_value']:.2f})",
            current_gp_day=0,
            lift_day=lift_month / DAYS_IN_MONTH,
            total_gp_day=lift_month / DAYS_IN_MONTH,
            total_gp_month=lift_month,
            note=f"OPTIMISTIC — only directionally positive cell with "
                 f"meaningful traffic. UNDERPOWERED test "
                 f"(P={mobile['p_value']:.2f}; "
                 f"71% chance the lift is noise). Sized on test-pool "
                 f"basis ({mobile['monthly_test_pool']:,.0f} mobile "
                 f"visitors/month). Test runs on ~10% of paid Google "
                 f"traffic; rollout assumes variant deployed to that "
                 f"same population.",
        )

    # SCENARIO 2: Mobile + Tablet
    if mobile and tablet:
        lift_month = mobile.get("monthly_gp_lift", 0) + tablet.get("monthly_gp_lift", 0)
        _row(
            initiative="Landing Page Test — Mobile + Tablet rollout",
            scenario="Roll out to both directionally positive cells",
            current_gp_day=0,
            lift_day=lift_month / DAYS_IN_MONTH,
            total_gp_day=lift_month / DAYS_IN_MONTH,
            total_gp_month=lift_month,
            note=f"Includes tablet (+135% RPV lift but n=48 visitors, "
                 f"P={tablet['p_value']:.2f} — extremely small sample). "
                 f"Tablet contribution likely overstated.",
        )

    # SCENARIO 3: All three pooled (honest view)
    if mobile and desktop and tablet:
        lift_month = (mobile.get("monthly_gp_lift", 0) +
                      desktop.get("monthly_gp_lift", 0) +
                      tablet.get("monthly_gp_lift", 0))
        _row(
            initiative="Landing Page Test — All-device pooled (honest view)",
            scenario="Roll out to all three devices weighted by traffic",
            current_gp_day=0,
            lift_day=lift_month / DAYS_IN_MONTH,
            total_gp_day=lift_month / DAYS_IN_MONTH,
            total_gp_month=lift_month,
            note=f"Honest pooled view. Currently NEGATIVE because the "
                 f"desktop loss (−23.7% RPV) outweighs the mobile gain "
                 f"(+8.3% RPV). The test in current state is "
                 f"directionally a loser if rolled out to all devices.",
        )

    return pd.DataFrame(rows)


def build_test_detail(per_device: list) -> pd.DataFrame:
    rows = []
    for d in per_device:
        rows.append({
            "Device":              d["device"],
            "Control Visitors":    d["control_visitors"],
            "Variant Visitors":    d["variant_visitors"],
            "Test Pool":           d["test_pool_visitors"],
            "Monthly Test Pool":   round(d["monthly_test_pool"], 0),
            "Control RPV":         round(d["control_rpv"], 2),
            "Variant RPV":         round(d["variant_rpv"], 2),
            "RPV Lift $":          round(d["rpv_lift_dollars"], 2),
            "RPV Lift %":          f"{d['rpv_lift_pct']*100:+.1f}%",
            "P-Value":             round(d["p_value"], 3),
            "Confidence":          f"{d['confidence']*100:.0f}%",
            "Statistical Power":   f"{d['statistical_power']*100:.0f}%",
            "Winner":              d["winner"],
            "Monthly Rev Lift":    round(d["monthly_rev_lift"], 2),
            "Monthly GP Lift":     round(d["monthly_gp_lift"], 2),
        })
    return pd.DataFrame(rows)


def build_coverage_analysis(per_device: list, gads_devices: dict) -> pd.DataFrame:
    rows = []
    for d in per_device:
        gads_device = GA4_TO_GADS_DEVICE.get(d["device"].lower(), "")
        rows.append({
            "Device (GA4)":              d["device"],
            "Device (Google Ads)":       gads_device,
            "GA4 Test Visitors (9d)":    int(d["test_pool_visitors"]),
            "Google Ads Clicks (9d)":    int(d["gads_clicks_window"]),
            "Coverage %":                f"{d['coverage_pct']*100:.1f}%",
            "Monthly GA4 Test Pool":     int(d["monthly_test_pool"]),
            "Monthly Google Ads Clicks": int(d["gads_monthly_clicks"]),
        })
    # Total row
    total_test = sum(d["test_pool_visitors"] for d in per_device)
    total_gads = sum(d["gads_clicks_window"] for d in per_device)
    rows.append({
        "Device (GA4)":              "ALL DEVICES",
        "Device (Google Ads)":       "ALL DEVICES",
        "GA4 Test Visitors (9d)":    int(total_test),
        "Google Ads Clicks (9d)":    int(total_gads),
        "Coverage %":                f"{total_test/total_gads*100:.1f}%" if total_gads else "—",
        "Monthly GA4 Test Pool":     int(total_test / ANALYSIS_DAYS * DAYS_IN_MONTH),
        "Monthly Google Ads Clicks": int(total_gads / ANALYSIS_DAYS * DAYS_IN_MONTH),
    })
    return pd.DataFrame(rows)


def build_metadata(rpv_path: Path, gads_path: Path,
                   per_device: list, total_coverage: float) -> pd.DataFrame:
    rows = [
        ("Run timestamp",       datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Test name",           "home-state-in-headline-v2-20-50-50"),
        ("Domain",              DOMAIN_FILTER),
        ("Window",              f"{ANALYSIS_DAYS} days (Apr 27 - May 5, 2026)"),
        ("Test status",         "In flight; underpowered (no statistical winner)"),
        ("RPV input",           rpv_path.name),
        ("Google Ads input",    gads_path.name),
        ("", ""),
        ("Coverage % (test pool / Google Ads clicks)",
         f"{total_coverage*100:.1f}% — consistent across devices "
         f"(9.5% mobile, 10.1% desktop, 10.0% tablet). "
         f"Industry-standard GA4-vs-Google-Ads gap is 10-30%, not 90%, "
         f"so this isn't tracking loss — the test only RUNS on ~10% "
         f"of paid traffic."),
        ("", ""),
        ("Sizing basis",
         "TEST-POOL extrapolation. Variant effect can only be measured "
         "on traffic the test sees, so we extrapolate test-pool visitors "
         "to a monthly volume and apply the observed RPV lift. We do NOT "
         "extrapolate to full Google Ads paid traffic — that would assume "
         "the variant works on traffic the test never saw."),
        ("GP framing",
         "Revenue lift = GP lift × 1.0. The test improves conversion "
         "rate on already-paid traffic; media cost is held constant in "
         "the test, so each incremental lead's revenue is incremental GP."),
        ("", ""),
        ("Confidence — Mobile",
         f"P={[d for d in per_device if d['device']=='mobile'][0]['p_value']:.2f} "
         f"(71% chance lift is noise); +8.3% RPV"),
        ("Confidence — Desktop",
         f"P={[d for d in per_device if d['device']=='desktop'][0]['p_value']:.2f} "
         f"(31% chance loss is noise); −23.7% RPV"),
        ("Confidence — Tablet",
         f"P={[d for d in per_device if d['device']=='tablet'][0]['p_value']:.2f} "
         f"(41% chance lift is noise); n=48 visitors — too small to read"),
        ("", ""),
        ("Headline scenario",   "Mobile-only rollout (optimistic)"),
        ("Tracker recommendation",
         "Record HEADLINE (mobile-only) with confidence flag = LOW. The "
         "all-device pooled scenario is currently negative; the test is "
         "not yet a clear winner anywhere."),
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
    parser.add_argument("--rpv-csv",  default=str(DEFAULT_RPV_CSV))
    parser.add_argument("--ab-csv",   default=str(DEFAULT_AB_CSV))
    parser.add_argument("--gads-csv", default=str(DEFAULT_GADS_CSV))
    parser.add_argument("--output",   default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    t0 = time.time()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Landing Page A/B Test Sizing ===")
    print(f"Test: home-state-in-headline-v2 (homeinsurance.quote.com)")
    print(f"Window: {ANALYSIS_DAYS} days\n")

    rpv_df  = load_rpv(Path(args.rpv_csv))
    ab_df   = load_ab(Path(args.ab_csv))
    gads_df = load_gads(Path(args.gads_csv))

    print(f"Loaded {len(rpv_df)} test rows for {DOMAIN_FILTER}")
    print(f"Loaded {len(gads_df)} Google Ads rows (device-broken-out)")

    gads_devices = gads_device_rollup(gads_df)
    print(f"\nDevice rollup (Google Ads, {ANALYSIS_DAYS}d):")
    for dev, data in gads_devices.items():
        if data["interactions"] > 0:
            print(f"  {dev:15s}  {int(data['interactions']):>6,} clicks   "
                  f"${data['cost']:>10,.0f} cost")

    per_device = per_device_sizing(rpv_df, gads_devices)

    print(f"\nPer-device sizing (test-pool basis):")
    for d in per_device:
        print(f"  {d['device']:8s}  test pool {d['test_pool_visitors']:>5}  "
              f"RPV lift ${d['rpv_lift_dollars']:>+6.2f}  "
              f"Monthly GP lift ${d['monthly_gp_lift']:>+9,.2f}  "
              f"P={d['p_value']:.2f}  Coverage {d['coverage_pct']*100:>4.1f}%")

    total_coverage = (sum(d["test_pool_visitors"] for d in per_device) /
                       sum(d["gads_clicks_window"] for d in per_device or [1])
                       if per_device else 0)

    print(f"\nHEADLINE (mobile-only): "
          f"${[d for d in per_device if d['device'].lower()=='mobile'][0]['monthly_gp_lift']:+,.2f}/month")

    # Sheets
    sheets = {
        "Metadata":          build_metadata(Path(args.rpv_csv), Path(args.gads_csv),
                                              per_device, total_coverage),
        "Sizing Summary":    build_sizing_summary(per_device),
        "Test Detail":       build_test_detail(per_device),
        "Coverage Analysis": build_coverage_analysis(per_device, gads_devices),
        "RPV Input":         rpv_df,
        "AB Performance":    ab_df,
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
