"""
Microsoft Home geo-tiering — 30-DAY recency model (director-requested comparison).

This is a STANDALONE comparison script. It does NOT modify or override the core
12-week model (microsoft_geo_model_comparison.py) or any of its outputs. It imports
the core model's functions so the *method* stays identical — the ONLY differences are:

  1. Lookback window: 30 days (vs 12 weeks / 84 days)
  2. No RPL blend (the 12w/4w blend collapses in a 30-day window — straight 30d RPL)
  3. Eligibility threshold: 75 search clicks OR 10 conversions in the 30-day window
     (vs 200 clicks over 12 weeks)
  4. Stability windows compressed to 30d / 21d / 14d (vs 12w / 8w / 4w)

Everything else — C2 weighted k-means, k=6, network definitions, RPL/1.5 ideal tCPA,
target ROAS — is identical to the core model, pulled directly from the core module.

The output is a separate workbook plus a comparison sheet that puts the 30-day tiers
side-by-side with the committed 12-week tiers from the ledger, so the director's
exercise is directly usable.

Usage:
    python microsoft_geo_model_30day.py --end-date 2026-05-24 --k 6
    python microsoft_geo_model_30day.py --end-date 2026-05-24 --eligibility-mode AND
    python microsoft_geo_model_30day.py --end-date 2026-05-24 \\
        --ledger commitments/microsoft_home_baseline.yaml
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Import the core model — we reuse its functions verbatim so the METHOD is identical.
# This is the key design choice: we don't reimplement clustering, eligibility logic,
# network pulls, or metrics. We only change the window and thresholds.
import microsoft_geo_model_comparison as core

# ----------------------------------------------------------------------------
# 30-day-specific configuration
# ----------------------------------------------------------------------------

LOOKBACK_DAYS = 30
# Stability sub-windows for the 30-day model (compressed analogues of 12w/8w/4w)
STABILITY_DAYS_FULL = 30
STABILITY_DAYS_MID = 21
STABILITY_DAYS_SHORT = 14

# Default eligibility thresholds for the 30-day window
DEFAULT_CLICK_THRESHOLD_30D = 75
DEFAULT_CONV_THRESHOLD_30D = 10


@dataclass
class Windows30:
    end_date: date
    w30_start: date
    w21_start: date
    w14_start: date


def build_windows_30(end_date: date) -> Windows30:
    return Windows30(
        end_date=end_date,
        w30_start=end_date - timedelta(days=LOOKBACK_DAYS - 1),
        w21_start=end_date - timedelta(days=STABILITY_DAYS_MID - 1),
        w14_start=end_date - timedelta(days=STABILITY_DAYS_SHORT - 1),
    )


# ----------------------------------------------------------------------------
# 30-day eligibility — clicks OR conversions (more permissive for short window)
# ----------------------------------------------------------------------------

def eligible_pool_30day(
    df: pd.DataFrame,
    click_threshold: int = DEFAULT_CLICK_THRESHOLD_30D,
    conv_threshold: int = DEFAULT_CONV_THRESHOLD_30D,
    mode: str = "OR",
) -> pd.DataFrame:
    """Eligible if (clicks >= click_threshold) OR/AND (conversions >= conv_threshold),
    with finite positive RPL and CPC, excluding UNKNOWN.

    mode='OR'  -> permissive (default; recommended for the short window)
    mode='AND' -> strict (both thresholds must be met)
    """
    clicks_ok = df["clicks"] >= click_threshold
    conv_ok = df["conversions"] >= conv_threshold
    if mode.upper() == "AND":
        volume_ok = clicks_ok & conv_ok
    else:
        volume_ok = clicks_ok | conv_ok

    return df[
        volume_ok
        & df["rpl"].notna()
        & df["cpc"].notna()
        & (df["rpl"] > 0)
        & (df["cpc"] > 0)
        & (df["state"].str.upper() != "UNKNOWN")
    ].copy().reset_index(drop=True)


def run_models_for_window_30(
    conn: sqlite3.Connection,
    start: date, end: date,
    enabled_campaign_ids: list[str],
    k: int,
    click_threshold: int,
    conv_threshold: int,
    mode: str,
) -> pd.DataFrame | None:
    """Pull a window, build 30-day eligible pool, apply all models.
    Mirrors core.run_models_for_window but uses the OR/AND eligibility.
    """
    agg = core.pull_search_state_aggregates(conn, start, end, enabled_campaign_ids)
    if agg.empty:
        return None
    agg = core.compute_derived(agg)
    elig = eligible_pool_30day(agg, click_threshold, conv_threshold, mode)
    if len(elig) < k:
        return None
    result, _ = core.apply_all_models(elig, k)
    return result


# ----------------------------------------------------------------------------
# Comparison against the committed 12-week ledger
# ----------------------------------------------------------------------------

_TIER_RANK = {
    "TIER ONE": 1, "TIER TWO": 2, "TIER THREE": 3,
    "TIER FOUR": 4, "TIER FIVE": 5, "TIER SIX": 6,
}


def load_ledger_commitments(ledger_path: Path) -> dict[str, str]:
    """Load state -> committed tier from the commitment ledger YAML."""
    try:
        import yaml
    except ImportError:
        print("WARNING: PyYAML not installed; skipping ledger comparison.", file=sys.stderr)
        return {}
    if not ledger_path.exists():
        print(f"WARNING: ledger not found at {ledger_path}; skipping comparison.", file=sys.stderr)
        return {}
    with open(ledger_path) as f:
        data = yaml.safe_load(f) or {}
    root = data.get("microsoft_home_geo_tiers", data)
    return dict(root.get("commitments", {}))


def load_baseline_centroids(centroids_path: Path) -> dict[str, float]:
    """Load frozen 12-week tier centroids (in ideal_tcpa terms) from YAML."""
    try:
        import yaml
    except ImportError:
        return {}
    if not centroids_path.exists():
        print(f"WARNING: centroids not found at {centroids_path}; "
              f"fixed-centroid view will be skipped.", file=sys.stderr)
        return {}
    with open(centroids_path) as f:
        data = yaml.safe_load(f) or {}
    root = data.get("baseline_centroids", data)
    return {t: float(c) for t, c in root.get("centroids", {}).items()}


def assign_to_fixed_centroids(
    result_30d: pd.DataFrame,
    baseline_centroids: dict[str, float],
) -> pd.DataFrame:
    """Assign each state's 30-day ideal_tcpa to the nearest FROZEN 12-week
    centroid, rather than re-clustering. This isolates the pure recency effect:
    a state changes tier only if its own 30-day economics crossed a committed
    boundary — not because other states reshuffled the cluster boundaries.
    """
    if not baseline_centroids:
        return pd.DataFrame()
    centroid_items = sorted(baseline_centroids.items(), key=lambda x: x[1], reverse=True)

    def _nearest(ideal: float) -> str:
        if pd.isna(ideal):
            return None
        return min(centroid_items, key=lambda tc: abs(tc[1] - ideal))[0]

    out = result_30d.copy()
    out["fixed_centroid_tier"] = out["ideal_tcpa"].apply(_nearest)
    return out[["state", "rpl", "ideal_tcpa", "fixed_centroid_tier"]]


def build_comparison(
    result_30d: pd.DataFrame,
    ledger_commitments: dict[str, str],
    fixed_centroid_df: pd.DataFrame | None = None,
    headline_model: str = "C2_weighted_kmeans",
) -> pd.DataFrame:
    """Side-by-side: 30-day tier vs committed 12-week (ledger) tier per state.

    Includes BOTH:
      - tier_30day_reclustered: C2 re-draws boundaries on the 30-day pool
      - tier_30day_fixed_centroid: 30-day RPL scored against frozen 12-week centroids
    The fixed-centroid column isolates the pure recency effect.
    """
    rows = []
    states_30d = dict(zip(result_30d["state"], result_30d[headline_model]))
    fixed_map = {}
    if fixed_centroid_df is not None and not fixed_centroid_df.empty:
        fixed_map = dict(zip(fixed_centroid_df["state"], fixed_centroid_df["fixed_centroid_tier"]))
    all_states = sorted(set(states_30d) | set(ledger_commitments))

    for state in all_states:
        tier_reclust = states_30d.get(state, "(not eligible 30d)")
        tier_fixed = fixed_map.get(state, "(not eligible 30d)")
        tier_12w = ledger_commitments.get(state, "(not committed)")

        # Agreement based on the FIXED-CENTROID view (the cleaner recency signal)
        rank_fixed = _TIER_RANK.get(tier_fixed)
        rank_12w = _TIER_RANK.get(tier_12w)
        if rank_fixed is not None and rank_12w is not None:
            delta = rank_fixed - rank_12w
            if delta == 0:
                agreement = "SAME"
            elif delta > 0:
                agreement = f"30d LOWER by {delta}"
            else:
                agreement = f"30d HIGHER by {abs(delta)}"
        elif tier_fixed.startswith("(not"):
            agreement = "DROPPED in 30d (below threshold)"
        elif tier_12w.startswith("(not"):
            agreement = "NEW in 30d (not in 12w ledger)"
        else:
            agreement = "N/A"

        # Does re-clustering disagree with fixed-centroid? (boundary-shift flag)
        if (not tier_reclust.startswith("(not") and not tier_fixed.startswith("(not")
                and tier_reclust != tier_fixed):
            boundary_note = f"re-cluster says {tier_reclust} (boundary shift)"
        else:
            boundary_note = ""

        econ = result_30d[result_30d["state"] == state]
        rpl = econ["rpl"].iloc[0] if not econ.empty else None
        clicks = econ["clicks"].iloc[0] if not econ.empty else None
        conv = econ["conversions"].iloc[0] if not econ.empty else None

        rows.append({
            "state": state,
            "tier_12week_committed": tier_12w,
            "tier_30day_fixed_centroid": tier_fixed,
            "tier_30day_reclustered": tier_reclust,
            "agreement": agreement,
            "boundary_shift_note": boundary_note,
            "rpl_30day": rpl,
            "clicks_30day": clicks,
            "conversions_30day": conv,
        })

    df = pd.DataFrame(rows)
    df["_disagree"] = (df["agreement"] != "SAME").astype(int)
    df["_rank"] = df["tier_30day_fixed_centroid"].map(_TIER_RANK).fillna(99)
    df = df.sort_values(["_disagree", "_rank", "state"], ascending=[False, True, True])
    df = df.drop(columns=["_disagree", "_rank"]).reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------
# Workbook writer
# ----------------------------------------------------------------------------

def write_30day_workbook(
    output_path: Path,
    windows: Windows30,
    k: int,
    result_30d: pd.DataFrame,
    stability_df: pd.DataFrame,
    comparison_df: pd.DataFrame | None,
    eligibility_label: str,
    summary_metrics: dict,
    n_eligible: int,
    n_12w_committed: int,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    HEADER_FILL = PatternFill("solid", start_color="C55A11", end_color="C55A11")  # orange to distinguish from 12w purple
    HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial")
    TITLE_FONT = Font(bold=True, size=14, name="Arial")
    SECTION_FONT = Font(bold=True, size=11, name="Arial")
    BODY_FONT = Font(name="Arial")
    SAME_FILL = PatternFill("solid", start_color="E8F5E9", end_color="E8F5E9")
    DIFF_FILL = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")
    DROP_FILL = PatternFill("solid", start_color="FFCCCC", end_color="FFCCCC")

    def _write_df(ws, df: pd.DataFrame, header_row: int, agreement_col: str = None):
        for c, col in enumerate(df.columns, start=1):
            cell = ws.cell(row=header_row, column=c, value=str(col))
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center")
        for r_off, rec in enumerate(df.to_dict("records"), start=1):
            for c_idx, col in enumerate(df.columns, start=1):
                v = rec[col]
                if isinstance(v, float) and np.isnan(v):
                    v = None
                elif isinstance(v, (np.integer, np.floating)):
                    v = v.item()
                cell = ws.cell(row=header_row + r_off, column=c_idx, value=v)
                cell.font = BODY_FONT
                col_str = str(col).lower()
                if "rpl" in col_str:
                    cell.number_format = "$#,##0.00"
                elif "clicks" in col_str or "conversion" in col_str:
                    cell.number_format = "#,##0"
                if agreement_col and col == agreement_col:
                    av = rec[col]
                    if av == "SAME":
                        cell.fill = SAME_FILL
                    elif "DROPPED" in str(av) or "NEW" in str(av):
                        cell.fill = DROP_FILL
                    else:
                        cell.fill = DIFF_FILL
        for c_idx, col in enumerate(df.columns, start=1):
            width = max(14, min(42, len(str(col)) + 2))
            ws.column_dimensions[get_column_letter(c_idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)

    # ---- Sheet 1: Overview ----
    ws = wb.create_sheet("Overview")
    ws["A1"] = "Microsoft Home Geo-Tiering — 30-DAY RECENCY MODEL"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (
        f"Director-requested comparison. Same method as the core 12-week model "
        f"(C2 weighted k-means, k={k}), but with a 30-day lookback window and "
        f"recency-adjusted eligibility. This is a COMPARISON artifact — it does not "
        f"replace the 12-week model or the committed tier baseline."
    )
    ws["A2"].font = BODY_FONT

    ws["A4"] = "Configuration"
    ws["A4"].font = SECTION_FONT
    config_lines = [
        ("Lookback window", f"{windows.w30_start} to {windows.end_date} (30 days)"),
        ("Eligibility", eligibility_label),
        ("RPL blend", "None — straight 30-day RPL (the 12w/4w blend collapses in a 30-day window)"),
        ("Headline model", "C2_weighted_kmeans (identical to 12-week model)"),
        ("k (tiers)", str(k)),
        ("Stability windows", f"30d / 21d / 14d (compressed analogue of 12w/8w/4w)"),
        ("Eligible states (30d)", str(n_eligible)),
        ("Committed states (12w ledger)", str(n_12w_committed)),
    ]
    r = 5
    for label, val in config_lines:
        ws.cell(r, 1, label).font = BODY_FONT
        ws.cell(r, 2, val).font = BODY_FONT
        r += 1

    r += 1
    ws.cell(r, 1, "How to read this comparison").font = SECTION_FONT
    r += 1
    notes = [
        "The 'Tier Comparison vs 12-Week Ledger' sheet is the headline output.",
        "  SAME (green) = the 30-day model puts the state in the same tier as the committed 12-week model.",
        "  30d HIGHER / LOWER (yellow) = the recency model assigns a different tier.",
        "  DROPPED (red) = state was eligible in 12w but falls below threshold in the 30-day window.",
        "",
        "Why fewer states qualify in the 30-day model: a 30-day window has roughly one-third",
        "the volume of a 12-week window, so more states fall below the eligibility threshold.",
        "This is the central trade-off the director's exercise surfaces — recency vs statistical volume.",
        "",
        "Interpretation guidance: states that DISAGREE between the two models are the ones where",
        "recency and stability point in different directions. A state that is SAME in both is robust",
        "to the window choice. A state that DROPPED has insufficient recent volume to tier confidently",
        "on 30 days alone — exactly the volume-confidence concern that motivates the 12-week window.",
    ]
    for line in notes:
        ws.cell(r, 1, line).font = BODY_FONT
        r += 1
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 70

    # ---- Sheet 2: Tier Comparison vs 12-Week Ledger ----
    if comparison_df is not None and not comparison_df.empty:
        ws = wb.create_sheet("Tier Comparison vs 12-Week")
        ws["A1"] = "30-Day Model Tiers vs Committed 12-Week Tiers"
        ws["A1"].font = TITLE_FONT
        n_same = int((comparison_df["agreement"] == "SAME").sum())
        n_total = len(comparison_df)
        n_dropped = int(comparison_df["agreement"].str.contains("DROPPED").sum())
        ws["A2"] = (
            f"{n_same} of {n_total} states agree (fixed-centroid basis). "
            f"{n_dropped} states eligible in 12w drop below threshold in 30d. "
            f"Disagreements sorted to top."
        )
        ws["A2"].font = BODY_FONT
        ws["A3"] = (
            "tier_30day_fixed_centroid = 30-day RPL scored against the FROZEN 12-week "
            "centroids (isolates pure recency — a state moves only if its OWN economics "
            "crossed a committed boundary). tier_30day_reclustered = C2 re-draws boundaries "
            "on the 30-day pool (mixes recency with boundary movement). 'agreement' uses the "
            "fixed-centroid column. boundary_shift_note flags where re-clustering alone would "
            "have changed the tier."
        )
        ws["A3"].font = BODY_FONT
        _write_df(ws, comparison_df, header_row=5, agreement_col="agreement")
        ws.freeze_panes = "B6"

    # ---- Sheet 3: 30-Day Tier Assignments ----
    ws = wb.create_sheet("30-Day Tier Assignments")
    ws["A1"] = "Full 30-Day Model Output (all four methods)"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "C2_weighted_kmeans is the headline. A2/B1/B4 shown for method-agreement check."
    ws["A2"].font = BODY_FONT
    assign_cols = ["state", "clicks", "conversions", "spend", "rpl", "cpc", "ideal_tcpa",
                   *core.MODEL_NAMES]
    assign_cols = [c for c in assign_cols if c in result_30d.columns]
    assign_df = result_30d[assign_cols].sort_values("rpl", ascending=False)
    _write_df(ws, assign_df, header_row=4)

    # ---- Sheet 4: Stability (30d/21d/14d) ----
    if stability_df is not None and not stability_df.empty:
        ws = wb.create_sheet("Stability (30d_21d_14d)")
        ws["A1"] = "Tier Stability Across 30d / 21d / 14d Windows"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("Compressed analogue of the 12w/8w/4w stability check. "
                    "Lower stability here than in the 12-week model is expected — "
                    "shorter windows are noisier.")
        ws["A2"].font = BODY_FONT
        _write_df(ws, stability_df, header_row=4)

    # ---- Sheet 5: Methodology ----
    ws = wb.create_sheet("Methodology")
    method_notes = [
        ("30-Day Recency Model — Methodology", True),
        ("", False),
        ("This script imports the core 12-week model's functions verbatim. The clustering", False),
        ("method, network definitions, RPL and ideal-tCPA formulas, and metrics are identical.", False),
        ("Only four things differ from the core model:", False),
        ("", False),
        ("1. Window: 30 days instead of 84 days (12 weeks).", False),
        ("2. RPL blend: none. The 12w/4w blend has no meaning in a 30-day window, so the", False),
        ("   30-day model uses straight 30-day RPL. Note this removes one of the core model's", False),
        ("   noise-smoothing mechanisms, which is part of why the 30-day tiers are less stable.", False),
        ("3. Eligibility: 75 clicks OR 10 conversions (vs 200 clicks over 12 weeks). The OR", False),
        ("   keeps the pool from collapsing in the short window. The conversion floor of 10", False),
        ("   ensures RPL is computed from enough conversions to be minimally reliable.", False),
        ("4. Stability windows: 30d/21d/14d instead of 12w/8w/4w.", False),
        ("", False),
        ("The central trade-off this exercise surfaces:", True),
        ("A 30-day window reflects current conditions faster but has ~1/3 the volume, so RPL", False),
        ("estimates are noisier and fewer states qualify. The 12-week window has more statistical", False),
        ("confidence but reacts more slowly to genuine economic shifts. States that agree across", False),
        ("both models are robust to this choice; states that disagree are where the recency-vs-", False),
        ("confidence tension actually bites.", False),
    ]
    for r_idx, (text, is_hdr) in enumerate(method_notes, start=1):
        c = ws.cell(row=r_idx, column=1, value=text)
        c.font = SECTION_FONT if is_hdr else BODY_FONT
    ws.column_dimensions["A"].width = 95

    wb.save(output_path)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", type=str, default="2026-05-24",
                        help="Anchor end date (YYYY-MM-DD). Default 2026-05-24.")
    parser.add_argument("--k", type=int, default=6)
    parser.add_argument("--db-path", type=str, default=core.__dict__.get("DEFAULT_DB_PATH",
                        r"C:\Projects\google-ads-ops\db\paid_search.db"))
    parser.add_argument("--output-dir", type=str,
                        default=r"C:\Projects\google-ads-ops\analysis\microsoft-geo-analysis\output\30day")
    parser.add_argument("--click-threshold", type=int, default=DEFAULT_CLICK_THRESHOLD_30D)
    parser.add_argument("--conv-threshold", type=int, default=DEFAULT_CONV_THRESHOLD_30D)
    parser.add_argument("--eligibility-mode", type=str, default="OR", choices=["OR", "AND"],
                        help="OR (permissive, default) or AND (strict) for clicks/conversions thresholds.")
    parser.add_argument("--ledger", type=str,
                        default="commitments/microsoft_home_baseline.yaml",
                        help="Path to commitment ledger YAML for side-by-side comparison.")
    parser.add_argument("--baseline-centroids", type=str,
                        default="commitments/baseline_centroids.yaml",
                        help="Path to frozen 12-week centroids YAML. Enables the "
                             "fixed-centroid view (scores 30-day RPL against committed "
                             "boundaries instead of re-clustering).")
    args = parser.parse_args(argv)

    end_date = date.fromisoformat(args.end_date)
    windows = build_windows_30(end_date)

    print(f"30-DAY RECENCY MODEL")
    print(f"  Window:      {windows.w30_start} to {windows.end_date} (30 days)")
    print(f"  k:           {args.k}")
    print(f"  Eligibility: {args.click_threshold} clicks {args.eligibility_mode} "
          f"{args.conv_threshold} conversions")
    print()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(db_path))

    eids = core.pull_enabled_campaign_ids(conn)
    print(f"ENABLED campaigns: {len(eids)}")

    # Pull 30-day window, compute derived, build eligible pool
    agg = core.pull_search_state_aggregates(conn, windows.w30_start, windows.end_date, eids)
    if agg.empty:
        print("ERROR: no data in 30-day window.", file=sys.stderr)
        return 1
    agg = core.compute_derived(agg)
    elig = eligible_pool_30day(agg, args.click_threshold, args.conv_threshold, args.eligibility_mode)
    print(f"30-day eligible states: {len(elig)} "
          f"(of {len(agg)} states with any data)")

    if len(elig) < args.k:
        print(f"ERROR: eligible pool ({len(elig)}) smaller than k ({args.k}). "
              f"Try --eligibility-mode OR or lower thresholds.", file=sys.stderr)
        return 1

    # Apply all models
    result_30d, artifacts = core.apply_all_models(elig, args.k)

    # Stability across compressed windows
    result_21d = run_models_for_window_30(
        conn, windows.w21_start, windows.end_date, eids, args.k,
        args.click_threshold, args.conv_threshold, args.eligibility_mode,
    )
    result_14d = run_models_for_window_30(
        conn, windows.w14_start, windows.end_date, eids, args.k,
        args.click_threshold, args.conv_threshold, args.eligibility_mode,
    )
    stability_df = core.build_stability_table(result_30d, result_21d, result_14d)
    print(f"21d eligible: {len(result_21d) if result_21d is not None else 'N/A'}  "
          f"14d eligible: {len(result_14d) if result_14d is not None else 'N/A'}")

    # Summary metrics for the headline model
    summary = core.model_summary_metrics(result_30d, "C2_weighted_kmeans")

    # Compare to committed 12-week ledger
    ledger_commitments = load_ledger_commitments(Path(args.ledger))

    # Fixed-centroid view: score 30-day ideal_tcpa against frozen 12-week centroids
    baseline_centroids = load_baseline_centroids(Path(args.baseline_centroids))
    fixed_centroid_df = None
    if baseline_centroids:
        fixed_centroid_df = assign_to_fixed_centroids(result_30d, baseline_centroids)

    comparison_df = None
    if ledger_commitments:
        comparison_df = build_comparison(result_30d, ledger_commitments, fixed_centroid_df)
        n_same = int((comparison_df["agreement"] == "SAME").sum())
        n_boundary = int((comparison_df["boundary_shift_note"] != "").sum())
        print(f"\nComparison vs 12-week ledger (fixed-centroid basis): "
              f"{n_same}/{len(comparison_df)} states agree")
        print(f"Boundary-shift-only changes (re-cluster disagrees with fixed-centroid): {n_boundary}")

    eligibility_label = (
        f"{args.click_threshold} search clicks {args.eligibility_mode} "
        f"{args.conv_threshold} conversions (30-day window)"
    )

    output_path = Path(args.output_dir) / f"microsoft_geo_model_30day_{end_date}.xlsx"
    write_30day_workbook(
        output_path, windows, args.k, result_30d, stability_df, comparison_df,
        eligibility_label, summary, len(elig), len(ledger_commitments),
    )
    print(f"\nWrote: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
