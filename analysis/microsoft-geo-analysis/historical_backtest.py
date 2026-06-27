"""
Historical backtest for Microsoft Home geo-tiering analysis.

Runs the model_comparison pipeline for multiple end-date anchors, then
aggregates the results into a comparison workbook showing how tier
assignments and key portfolio metrics drift across snapshots.

Use this BEFORE committing to a baseline snapshot, to calibrate how much
drift is normal week-over-week and to verify the chosen model configuration
is stable across historical snapshots.

Typical use:
    python historical_backtest.py \\
        --anchors "2026-05-24,2026-05-10,2026-04-26,2026-04-12,2026-04-07" \\
        --k 6 --rpl-blend 0.6 \\
        --output-dir output\\backtest
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DEFAULT_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\microsoft-geo-analysis\output\backtest"
DEFAULT_SCRIPT_PATH = (
    r"C:\Projects\google-ads-ops\analysis\microsoft-geo-analysis"
    r"\microsoft_geo_model_comparison.py"
)

# The 2026-01-13 cutoff is when offline conversion uploads changed methodology.
# Any 12w window starting before this date mixes pre- and post-cutoff revenue,
# which breaks comparability.
OFFLINE_UPLOADS_CUTOFF = date(2026, 1, 13)


# ----------------------------------------------------------------------------
# Anchor validation
# ----------------------------------------------------------------------------

@dataclass
class AnchorStatus:
    anchor_date: date
    w12_start: date
    is_clean: bool
    warning: Optional[str]


def validate_anchor(anchor: date) -> AnchorStatus:
    """Check whether an anchor's 12w window stays in clean (post-cutoff) data."""
    w12_start = anchor - timedelta(days=83)
    if w12_start < OFFLINE_UPLOADS_CUTOFF:
        days_pre_cutoff = (OFFLINE_UPLOADS_CUTOFF - w12_start).days
        return AnchorStatus(
            anchor_date=anchor,
            w12_start=w12_start,
            is_clean=False,
            warning=(
                f"12w_start {w12_start} is {days_pre_cutoff} days before "
                f"the offline-uploads cutoff ({OFFLINE_UPLOADS_CUTOFF}). "
                f"Revenue methodology may be inconsistent."
            ),
        )
    return AnchorStatus(anchor_date=anchor, w12_start=w12_start, is_clean=True, warning=None)


def parse_anchors(anchors_str: str) -> list[date]:
    """Parse a comma-separated string of YYYY-MM-DD dates into date objects."""
    return [
        date.fromisoformat(a.strip())
        for a in anchors_str.split(",") if a.strip()
    ]


# ----------------------------------------------------------------------------
# Run the model_comparison script for each anchor
# ----------------------------------------------------------------------------

def run_pipeline_for_anchor(
    anchor: date,
    script_path: str,
    db_path: str,
    output_dir: Path,
    k: int,
    rpl_blend: float,
    extra_args: list[str],
) -> Path | None:
    """Invoke the existing model_comparison script for a single anchor.
    Returns the output workbook path, or None if the run failed.
    """
    anchor_dir = output_dir / f"anchor_{anchor.isoformat()}"
    anchor_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        script_path,
        "--db-path", db_path,
        "--end-date", anchor.isoformat(),
        "--k", str(k),
        "--rpl-blend", str(rpl_blend),
        "--output-dir", str(anchor_dir),
        # Backtest doesn't need k-sensitivity or snapshots for individual runs;
        # they'd be redundant noise. Keep them light.
        "--k-sensitivity", "2,4,6,8,12",
        *extra_args,
    ]
    print(f"  Running pipeline for anchor {anchor}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: pipeline failed for anchor {anchor}", file=sys.stderr)
        print(f"  stderr: {result.stderr[-500:]}", file=sys.stderr)
        return None

    # Find the produced workbook
    candidates = list(anchor_dir.glob("microsoft_geo_model_comparison_*.xlsx"))
    if not candidates:
        print(f"  ERROR: no output workbook found in {anchor_dir}", file=sys.stderr)
        return None
    return candidates[0]


# ----------------------------------------------------------------------------
# Extract per-anchor data from the produced workbooks
# ----------------------------------------------------------------------------

@dataclass
class AnchorResult:
    anchor_date: date
    workbook_path: Path
    master_tier_review: pd.DataFrame
    model_summary: pd.DataFrame
    gp_rollup: dict
    is_clean: bool
    warning: Optional[str]


def extract_anchor_result(
    anchor_status: AnchorStatus, workbook_path: Path,
) -> AnchorResult | None:
    """Pull the key tables/metrics out of one anchor's workbook."""
    if not workbook_path or not workbook_path.exists():
        return None

    try:
        mtr = pd.read_excel(workbook_path, sheet_name="Master Tier Review", header=13)
    except Exception as e:
        print(f"  WARNING: Couldn't read Master Tier Review for {anchor_status.anchor_date}: {e}")
        return None

    try:
        ms = pd.read_excel(workbook_path, sheet_name="Model Comparison Summary", header=3)
    except Exception as e:
        print(f"  WARNING: Couldn't read Model Comparison Summary for {anchor_status.anchor_date}: {e}")
        ms = pd.DataFrame()

    # Pull the GP rollup numbers from the top of the Master Tier Review sheet
    # (cells B5-B10). Need openpyxl for direct cell access.
    import openpyxl
    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    ws = wb["Master Tier Review"]
    gp_rollup = {
        "theoretical_max": ws["B5"].value,
        "conservative_monthly": ws["B6"].value,
        "realistic_monthly": ws["B7"].value,
        "optimistic_monthly": ws["B8"].value,
        "weekly_equivalent": ws["B9"].value,
        "daily_equivalent": ws["B10"].value,
    }

    return AnchorResult(
        anchor_date=anchor_status.anchor_date,
        workbook_path=workbook_path,
        master_tier_review=mtr,
        model_summary=ms,
        gp_rollup=gp_rollup,
        is_clean=anchor_status.is_clean,
        warning=anchor_status.warning,
    )


# ----------------------------------------------------------------------------
# Build the cross-anchor comparison tables
# ----------------------------------------------------------------------------

def build_tier_across_anchors(results: list[AnchorResult]) -> pd.DataFrame:
    """For each state, show its tier under each anchor side by side."""
    # Get the union of all states across all anchors
    all_states = set()
    for r in results:
        all_states.update(r.master_tier_review["state"].tolist())
    all_states = sorted(all_states)

    rows = []
    for state in all_states:
        row = {"state": state}
        for r in results:
            ar = r.master_tier_review
            match = ar[ar["state"] == state]
            anchor_label = f"anchor_{r.anchor_date}"
            if match.empty:
                row[anchor_label] = "(not eligible)"
            else:
                m = match.iloc[0]
                tier = m.get("tier")
                # Mark below-threshold visibly
                if m.get("below_threshold"):
                    row[anchor_label] = f"{tier} (low-conf)"
                else:
                    row[anchor_label] = str(tier) if pd.notna(tier) else "(no tier)"
        rows.append(row)
    df = pd.DataFrame(rows)

    # Add a stability classification per state
    anchor_cols = [c for c in df.columns if c.startswith("anchor_")]

    def _classify(row):
        tiers = [row[c] for c in anchor_cols]
        # Treat any value containing "low-conf" or "not eligible" as missing
        valid = [t for t in tiers if "(not eligible)" not in t]
        if not valid:
            return "NO_DATA"
        if len(set(valid)) == 1:
            return "STABLE"
        # Determine direction by mapping tier names to rank
        tier_rank_map = {
            "TIER ONE": 1, "TIER TWO": 2, "TIER THREE": 3,
            "TIER FOUR": 4, "TIER FIVE": 5, "TIER SIX": 6,
            "TIER SEVEN": 7, "TIER EIGHT": 8, "TIER NINE": 9,
            "TIER TEN": 10, "TIER ELEVEN": 11, "TIER TWELVE": 12,
        }
        ranks = []
        for t in valid:
            # Strip "(low-conf)" suffix if present
            t_clean = t.replace(" (low-conf)", "")
            ranks.append(tier_rank_map.get(t_clean, 99))
        # Anchors are ordered newest first in the column list; reverse for chronological
        ranks_chrono = list(reversed(ranks))
        if all(ranks_chrono[i] <= ranks_chrono[i + 1] for i in range(len(ranks_chrono) - 1)):
            return "DRIFTING_DOWN"  # tier rank increasing = moving to lower tier
        if all(ranks_chrono[i] >= ranks_chrono[i + 1] for i in range(len(ranks_chrono) - 1)):
            return "DRIFTING_UP"
        # Some movement up and some down — volatile
        return "VOLATILE"

    df["stability"] = df.apply(_classify, axis=1)
    df["n_distinct_tiers"] = df[anchor_cols].nunique(axis=1)

    # Sort: VOLATILE first, then DRIFTING, then STABLE
    sort_order = {"VOLATILE": 0, "DRIFTING_UP": 1, "DRIFTING_DOWN": 1, "STABLE": 2, "NO_DATA": 3}
    df["_sort"] = df["stability"].map(sort_order)
    df = df.sort_values(["_sort", "state"]).drop(columns=["_sort"]).reset_index(drop=True)
    return df


def build_rpl_across_anchors(results: list[AnchorResult]) -> pd.DataFrame:
    """For each state, show its blended RPL under each anchor side by side."""
    all_states = set()
    for r in results:
        all_states.update(r.master_tier_review["state"].tolist())
    all_states = sorted(all_states)

    rows = []
    for state in all_states:
        row = {"state": state}
        rpls = []
        for r in results:
            ar = r.master_tier_review
            match = ar[ar["state"] == state]
            anchor_label = f"rpl_{r.anchor_date}"
            if match.empty:
                row[anchor_label] = None
            else:
                rpl = match.iloc[0].get("rpl")
                row[anchor_label] = float(rpl) if pd.notna(rpl) else None
                if pd.notna(rpl):
                    rpls.append(float(rpl))
        # Compute pct change from oldest valid to newest valid
        rpl_cols = [c for c in row.keys() if c.startswith("rpl_")]
        # rpl_cols are in newest-first order; oldest is last
        first_valid = next((row[c] for c in reversed(rpl_cols) if row[c] is not None), None)
        last_valid = next((row[c] for c in rpl_cols if row[c] is not None), None)
        if first_valid and first_valid > 0 and last_valid is not None:
            row["pct_change_oldest_to_newest"] = (last_valid - first_valid) / first_valid
        else:
            row["pct_change_oldest_to_newest"] = None
        if rpls:
            row["min_rpl"] = min(rpls)
            row["max_rpl"] = max(rpls)
            row["rpl_range_ratio"] = max(rpls) / min(rpls) if min(rpls) > 0 else None
        else:
            row["min_rpl"] = None
            row["max_rpl"] = None
            row["rpl_range_ratio"] = None
        rows.append(row)
    df = pd.DataFrame(rows)
    # Sort by absolute pct change descending — biggest movers on top
    df["_abs"] = df["pct_change_oldest_to_newest"].abs()
    df = df.sort_values("_abs", ascending=False, na_position="last").drop(columns=["_abs"])
    return df.reset_index(drop=True)


def build_tier_change_frequency(results: list[AnchorResult]) -> pd.DataFrame:
    """For each state, count how many anchor-to-anchor transitions involve a
    tier change. 0 = perfectly stable across all anchors. Max = changed at
    every transition.
    """
    all_states = set()
    for r in results:
        all_states.update(r.master_tier_review["state"].tolist())
    all_states = sorted(all_states)

    # Order results chronologically (oldest first)
    chrono = sorted(results, key=lambda r: r.anchor_date)

    rows = []
    for state in all_states:
        tiers = []
        for r in chrono:
            ar = r.master_tier_review
            match = ar[ar["state"] == state]
            if match.empty:
                tiers.append(None)
            else:
                t = match.iloc[0].get("tier")
                tiers.append(str(t) if pd.notna(t) else None)

        # Count transitions where the tier label changed (ignoring None gaps)
        n_transitions = 0
        n_changes = 0
        for i in range(len(tiers) - 1):
            if tiers[i] is not None and tiers[i + 1] is not None:
                n_transitions += 1
                if tiers[i] != tiers[i + 1]:
                    n_changes += 1
        change_rate = n_changes / n_transitions if n_transitions > 0 else None
        rows.append({
            "state": state,
            "n_transitions_observed": n_transitions,
            "n_tier_changes": n_changes,
            "change_rate": change_rate,
            "trajectory": " → ".join(t.replace("TIER ", "T") if t else "(none)" for t in tiers),
        })
    df = pd.DataFrame(rows)
    df = df.sort_values(["n_tier_changes", "state"], ascending=[False, True]).reset_index(drop=True)
    return df


def build_portfolio_metrics_across_anchors(results: list[AnchorResult]) -> pd.DataFrame:
    """Portfolio-level GP and model metrics at each anchor date."""
    rows = []
    for r in sorted(results, key=lambda x: x.anchor_date):
        row = {
            "anchor_date": r.anchor_date.isoformat(),
            "is_clean": r.is_clean,
            "warning": r.warning,
            "theoretical_max_monthly_gp": r.gp_rollup.get("theoretical_max"),
            "realistic_monthly_gp": r.gp_rollup.get("realistic_monthly"),
            "weekly_equivalent": r.gp_rollup.get("weekly_equivalent"),
        }
        # Pull C2 metrics from model summary
        if not r.model_summary.empty:
            c2 = r.model_summary[r.model_summary["model"] == "C2_weighted_kmeans"]
            if not c2.empty:
                c2r = c2.iloc[0]
                row["c2_bid_loss"] = c2r.get("spend_weighted_bid_loss")
                row["c2_stable_spend_rate"] = c2r.get("stable_spend_rate")
                row["c2_gp_capture_rate"] = c2r.get("gp_upside_capture_rate")
        rows.append(row)
    return pd.DataFrame(rows)


def build_trajectory_watch(
    rpl_df: pd.DataFrame, tier_df: pd.DataFrame, results: list,
    target_roas: float = 1.50,
) -> pd.DataFrame:
    """For each above-threshold state, fit a linear regression of RPL over the
    anchor timeline and project forward.

    Output columns per state:
      state, stability (from tier_df), n_observations, current_rpl, current_tier,
      rpl_slope_per_week, r_squared, projected_rpl_2w, projected_rpl_4w,
      projected_tier_4w, weeks_to_next_tier_boundary, classification

    Classification logic:
      DRIFTING_CONFIRMED — sustained directional trend, R² > 0.5
      DRIFTING_UNSTABLE  — directional but R² ≤ 0.5 (movement real but noisy)
      OSCILLATING        — VOLATILE in tier_df, no clear slope
      STABLE             — no meaningful trend
    """
    # Match only columns of the form rpl_YYYY-MM-DD; exclude rpl_range_ratio, min_rpl, etc.
    import re
    date_pattern = re.compile(r"^rpl_\d{4}-\d{2}-\d{2}$")
    rpl_cols = sorted([c for c in rpl_df.columns if date_pattern.match(c)])
    rpl_cols_chrono = list(reversed(rpl_cols))  # oldest first
    anchor_dates_chrono = [
        date.fromisoformat(c.replace("rpl_", "")) for c in rpl_cols_chrono
    ]

    # Build tier-centroid map from the most recent anchor (the current view)
    if not results:
        return pd.DataFrame()
    latest = max(results, key=lambda r: r.anchor_date)
    mtr = latest.master_tier_review
    above = mtr[mtr["below_threshold"] == False]
    tier_centroids = (
        above.groupby("tier")["rpl"].mean().sort_values(ascending=False).to_dict()
    )
    # Boundaries: for each tier, lower RPL bound = next tier's centroid
    tier_order = list(tier_centroids.keys())
    tier_lower_bound = {}
    for i, t in enumerate(tier_order):
        if i + 1 < len(tier_order):
            tier_lower_bound[t] = (tier_centroids[t] + tier_centroids[tier_order[i + 1]]) / 2
        else:
            tier_lower_bound[t] = 0
    tier_upper_bound = {}
    for i, t in enumerate(tier_order):
        if i == 0:
            tier_upper_bound[t] = float("inf")
        else:
            tier_upper_bound[t] = (tier_centroids[t] + tier_centroids[tier_order[i - 1]]) / 2

    # Stability lookup
    stab_lookup = dict(zip(tier_df["state"], tier_df["stability"]))

    rows = []
    for _, row in rpl_df.iterrows():
        state = row["state"]
        rpls = [row[c] for c in rpl_cols_chrono]

        valid_pairs = [
            (anchor_dates_chrono[i], rpls[i])
            for i in range(len(rpls))
            if rpls[i] is not None and rpls[i] > 0
        ]
        if len(valid_pairs) < 3:
            continue  # Need at least 3 points for meaningful regression

        # Convert dates to weeks-since-oldest for regression
        oldest_date = valid_pairs[0][0]
        weeks = np.array([(d - oldest_date).days / 7.0 for d, _ in valid_pairs])
        values = np.array([v for _, v in valid_pairs])

        # Linear regression: rpl = slope * weeks + intercept
        n = len(values)
        x_mean = weeks.mean()
        y_mean = values.mean()
        ss_xy = np.sum((weeks - x_mean) * (values - y_mean))
        ss_xx = np.sum((weeks - x_mean) ** 2)
        slope = ss_xy / ss_xx if ss_xx > 0 else 0.0
        intercept = y_mean - slope * x_mean
        # R²
        y_pred = slope * weeks + intercept
        ss_res = np.sum((values - y_pred) ** 2)
        ss_tot = np.sum((values - y_mean) ** 2)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        current_rpl = values[-1]
        current_week = weeks[-1]
        # Project forward
        projected_rpl_2w = slope * (current_week + 2) + intercept
        projected_rpl_4w = slope * (current_week + 4) + intercept

        # Tier projection — find which tier the projected RPL lands in
        def _tier_for_rpl(r):
            for t in tier_order:
                if tier_lower_bound[t] <= r < tier_upper_bound[t]:
                    return t
            return tier_order[-1] if r < tier_centroids[tier_order[-1]] else tier_order[0]

        current_tier = _tier_for_rpl(current_rpl)
        projected_tier_4w = _tier_for_rpl(projected_rpl_4w)

        # Weeks to next tier boundary at current slope
        weeks_to_boundary = None
        if abs(slope) > 0.01:  # avoid division by tiny slope
            if slope > 0:
                # Crossing upper bound (moving to higher tier)
                ub = tier_upper_bound.get(current_tier, float("inf"))
                if ub != float("inf"):
                    weeks_to_boundary = (ub - current_rpl) / slope
            else:
                # Crossing lower bound (moving to lower tier)
                lb = tier_lower_bound.get(current_tier, 0)
                if lb > 0:
                    weeks_to_boundary = (current_rpl - lb) / abs(slope)
            if weeks_to_boundary is not None and weeks_to_boundary < 0:
                weeks_to_boundary = None  # already crossed

        # Classification
        stability = stab_lookup.get(state, "")
        abs_slope_pct = abs(slope) / y_mean if y_mean > 0 else 0
        if stability == "STABLE":
            classification = "STABLE"
        elif stability == "VOLATILE":
            classification = "OSCILLATING"
        elif r_squared >= 0.5 and abs_slope_pct >= 0.02:  # >2%/week sustained
            classification = "DRIFTING_CONFIRMED"
        elif abs_slope_pct >= 0.02:
            classification = "DRIFTING_UNSTABLE"
        else:
            classification = "MINIMAL_MOVEMENT"

        rows.append({
            "state": state,
            "stability": stability,
            "n_observations": int(n),
            "current_rpl": float(current_rpl),
            "current_tier": current_tier,
            "rpl_slope_per_week": float(slope),
            "rpl_slope_pct_per_week": float(slope / y_mean) if y_mean > 0 else 0,
            "r_squared": float(r_squared),
            "projected_rpl_2w": float(projected_rpl_2w),
            "projected_rpl_4w": float(projected_rpl_4w),
            "projected_tier_4w": projected_tier_4w,
            "tier_change_projected": current_tier != projected_tier_4w,
            "weeks_to_next_tier_boundary": (
                round(float(weeks_to_boundary), 1) if weeks_to_boundary else None
            ),
            "classification": classification,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Sort: DRIFTING_CONFIRMED first (most actionable), then by abs slope
    sort_order = {
        "DRIFTING_CONFIRMED": 0, "DRIFTING_UNSTABLE": 1, "OSCILLATING": 2,
        "MINIMAL_MOVEMENT": 3, "STABLE": 4,
    }
    df["_sort"] = df["classification"].map(sort_order)
    df["_abs_slope"] = df["rpl_slope_per_week"].abs()
    df = df.sort_values(["_sort", "_abs_slope"], ascending=[True, False])
    df = df.drop(columns=["_sort", "_abs_slope"]).reset_index(drop=True)
    return df


def build_drift_thresholds(rpl_df: pd.DataFrame, tier_df: pd.DataFrame) -> dict:
    """Calibrate empirical drift thresholds from the historical RPL data.

    Returns a dict with two distributions:
      - 'confident': percentile distribution of anchor-to-anchor abs RPL
        %change for states that were above-threshold in EVERY anchor.
        These are the operationally meaningful thresholds.
      - 'all': same distribution including low-confidence states.
        Useful as a sanity check but not recommended for setting thresholds.
    Also returns a per-state max-change table.

    rpl_df: output of build_rpl_across_anchors (one row per state)
    tier_df: output of build_tier_across_anchors (used to detect low-conf flags)
    """
    import re
    date_pattern = re.compile(r"^rpl_\d{4}-\d{2}-\d{2}$")
    rpl_cols = sorted([c for c in rpl_df.columns if date_pattern.match(c)])
    rpl_cols_chrono = list(reversed(rpl_cols))  # oldest first

    # Determine which states were above-threshold in every anchor.
    # A state is "consistently confident" if no anchor column contains "(low-conf)"
    # or "(not eligible)" in the tier dataframe.
    anchor_tier_cols = [c for c in tier_df.columns if c.startswith("anchor_")]
    confident_states = set()
    for _, r in tier_df.iterrows():
        if all(
            ("(low-conf)" not in str(r[c])) and ("(not eligible)" not in str(r[c]))
            for c in anchor_tier_cols
        ):
            confident_states.add(r["state"])

    confident_changes = []
    all_changes = []
    per_state = []

    for _, row in rpl_df.iterrows():
        state = row["state"]
        rpls_chrono = [row[c] for c in rpl_cols_chrono]
        valid = [(i, v) for i, v in enumerate(rpls_chrono) if v is not None and v > 0]
        if len(valid) < 2:
            continue

        state_changes = []
        for i in range(len(valid) - 1):
            _, prev = valid[i]
            _, curr = valid[i + 1]
            pct = (curr - prev) / prev
            state_changes.append(abs(pct))
            all_changes.append(abs(pct))
            if state in confident_states:
                confident_changes.append(abs(pct))

        if state_changes:
            per_state.append({
                "state": state,
                "is_confident_throughout": state in confident_states,
                "max_abs_pct_change": max(state_changes),
                "mean_abs_pct_change": np.mean(state_changes),
                "n_transitions": len(state_changes),
            })

    def _percentiles(arr: list, label: str) -> dict | None:
        if not arr:
            return None
        a = np.array(arr)
        return {
            "metric": label,
            "n_observations": len(a),
            "min": a.min(),
            "p25": np.percentile(a, 25),
            "p50_median": np.percentile(a, 50),
            "p75": np.percentile(a, 75),
            "p90": np.percentile(a, 90),
            "p95": np.percentile(a, 95),
            "p99": np.percentile(a, 99),
            "max": a.max(),
            "mean": a.mean(),
            "stdev": a.std(),
        }

    summary_rows = []
    confident_row = _percentiles(
        confident_changes,
        f"Confident-states only (above-threshold in all anchors) — {len(confident_states)} states",
    )
    if confident_row:
        summary_rows.append(confident_row)
    all_row = _percentiles(
        all_changes,
        "ALL states including low-confidence (do not use for thresholds — included as sanity check)",
    )
    if all_row:
        summary_rows.append(all_row)

    summary_df = pd.DataFrame(summary_rows) if summary_rows else pd.DataFrame()
    per_state_df = (
        pd.DataFrame(per_state)
        .sort_values(["is_confident_throughout", "max_abs_pct_change"], ascending=[False, False])
        if per_state else pd.DataFrame()
    )

    return {
        "summary_df": summary_df,
        "per_state_df": per_state_df,
        "confident_states": sorted(confident_states),
    }


# ----------------------------------------------------------------------------
# Workbook writer
# ----------------------------------------------------------------------------

def write_backtest_workbook(
    output_path: Path,
    anchors: list[AnchorStatus],
    results: list[AnchorResult],
    k: int,
    rpl_blend: float,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    HEADER_FILL = PatternFill("solid", start_color="7030A0", end_color="7030A0")
    HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial")
    TITLE_FONT = Font(bold=True, size=14, name="Arial")
    SECTION_FONT = Font(bold=True, size=11, name="Arial")
    BODY_FONT = Font(name="Arial")
    WARN_FILL = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")

    def _write_df(ws, df: pd.DataFrame, header_row: int):
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
                if "rpl" in col_str and col != "rpl_range_ratio":
                    cell.number_format = "$#,##0.00"
                elif "gp" in col_str or "bid_loss" in col_str or col_str.endswith("_monthly"):
                    cell.number_format = "$#,##0"
                elif "pct" in col_str or "rate" in col_str:
                    cell.number_format = "0.0%"
                elif "ratio" in col_str:
                    cell.number_format = "0.00"
        for c_idx, col in enumerate(df.columns, start=1):
            width = max(14, min(40, len(str(col)) + 2))
            ws.column_dimensions[get_column_letter(c_idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)

    # ---- Sheet 1: Overview ----
    ws = wb.create_sheet("Overview")
    ws["A1"] = "Historical Backtest — Microsoft Home Geo-Tiering Stability"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"Configuration: k={k}, rpl_blend={rpl_blend}, "
                f"headline model = C2_weighted_kmeans  |  "
                f"Anchors: {len(anchors)} dates")
    ws["A2"].font = BODY_FONT

    ws["A4"] = "Anchor dates and validation status"
    ws["A4"].font = SECTION_FONT
    overview_rows = []
    for a in anchors:
        result = next((r for r in results if r.anchor_date == a.anchor_date), None)
        overview_rows.append({
            "anchor_date": a.anchor_date.isoformat(),
            "12w_start": a.w12_start.isoformat(),
            "is_clean": "YES" if a.is_clean else "NO",
            "warning": a.warning or "",
            "pipeline_status": "OK" if result else "FAILED",
            "workbook_path": str(result.workbook_path) if result else "",
        })
    overview_df = pd.DataFrame(overview_rows)
    _write_df(ws, overview_df, header_row=6)

    # Highlight non-clean rows
    for row_idx, a in enumerate(anchors, start=7):
        if not a.is_clean:
            for col in range(1, len(overview_df.columns) + 1):
                ws.cell(row=row_idx, column=col).fill = WARN_FILL

    # Narrative
    ws[f"A{8 + len(overview_df)}"] = "How to read this backtest"
    ws[f"A{8 + len(overview_df)}"].font = SECTION_FONT
    narrative = [
        "",
        "Each anchor date represents running the full geo-tiering analysis as if",
        "that date were 'today'. The 12w/8w/4w windows are computed relative to",
        "each anchor independently. By comparing tier assignments across anchors,",
        "we can see how much natural drift exists in the model's recommendations.",
        "",
        "Key sheets:",
        " * State Tier Across Anchors — every state's tier under each anchor.",
        "   STABLE states held tier across all anchors. VOLATILE states moved up",
        "   and down. DRIFTING states moved consistently in one direction.",
        " * RPL Trend Across Anchors — blended RPL trajectory per state. Big",
        "   movers (top of sheet) are candidates for partner-team verification.",
        " * Tier Change Frequency — for each state, the count of tier transitions",
        "   across observed anchor pairs. Useful for spotting boundary states.",
        " * Portfolio Metrics Across Anchors — top-line GP and bid-loss numbers at",
        "   each anchor.",
        " * Drift Threshold Calibration — empirical percentile distribution of",
        "   anchor-to-anchor RPL movement, for setting evidence-based drift",
        "   thresholds rather than guesses.",
        "",
        "Limitation: anchors whose 12w window crosses the 2026-01-13 offline-uploads",
        "cutoff will mix pre- and post-cutoff revenue. These rows are highlighted",
        "yellow above and should be interpreted with caution.",
    ]
    for i, line in enumerate(narrative, start=9 + len(overview_df)):
        ws.cell(row=i, column=1, value=line).font = BODY_FONT
    ws.column_dimensions["A"].width = 90

    # ---- Sheet 2: State Tier Across Anchors ----
    if results:
        ws = wb.create_sheet("State Tier Across Anchors")
        ws["A1"] = "Tier Assignment by State Across Historical Anchors"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("STABLE = same tier in every anchor.  "
                    "DRIFTING_UP/DOWN = consistent direction across anchors.  "
                    "VOLATILE = tier moved both up and down.")
        ws["A2"].font = BODY_FONT
        tier_df = build_tier_across_anchors(results)
        _write_df(ws, tier_df, header_row=4)
        ws.freeze_panes = "B5"

    # ---- Sheet 3: RPL Trend Across Anchors ----
    if results:
        ws = wb.create_sheet("RPL Trend Across Anchors")
        ws["A1"] = "Blended RPL by State Across Historical Anchors"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("Sorted by absolute % change from oldest to newest anchor. "
                    "Top rows are the biggest movers — candidates for partner-team verification.")
        ws["A2"].font = BODY_FONT
        rpl_df = build_rpl_across_anchors(results)
        _write_df(ws, rpl_df, header_row=4)
        ws.freeze_panes = "B5"

    # ---- Sheet 4: Tier Change Frequency ----
    if results:
        ws = wb.create_sheet("Tier Change Frequency")
        ws["A1"] = "How Often Each State's Tier Has Changed Across Anchors"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("Sorted by n_tier_changes descending. States at the top moved "
                    "tier most often. trajectory column shows the actual sequence.")
        ws["A2"].font = BODY_FONT
        freq_df = build_tier_change_frequency(results)
        _write_df(ws, freq_df, header_row=4)

    # ---- Sheet 5: Trajectory Watch (new) ----
    if results:
        traj_df = build_trajectory_watch(
            build_rpl_across_anchors(results),
            build_tier_across_anchors(results),
            results,
        )
        if not traj_df.empty:
            ws = wb.create_sheet("Trajectory Watch")
            ws["A1"] = "RPL Trajectory Watch — Drifting States"
            ws["A1"].font = TITLE_FONT
            ws["A2"] = (
                "For each state with 3+ valid anchors, fits a linear regression "
                "of RPL across time and projects 2-4 weeks forward. "
                "DRIFTING_CONFIRMED states have sustained directional movement (R² ≥ 0.5, "
                "≥2%/week slope) — act on these proactively. OSCILLATING states are tier "
                "boundary noisy — hold commitment regardless of model output."
            )
            ws["A2"].font = BODY_FONT
            _write_df(ws, traj_df, header_row=4)

    # ---- Sheet 6: Portfolio Metrics Across Anchors ----
    if results:
        ws = wb.create_sheet("Portfolio Metrics by Anchor")
        ws["A1"] = "Top-Line Portfolio Metrics at Each Anchor"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("How the headline GP estimates and C2 model metrics have varied "
                    "across the backtest window.")
        ws["A2"].font = BODY_FONT
        pm_df = build_portfolio_metrics_across_anchors(results)
        _write_df(ws, pm_df, header_row=4)

    # ---- Sheet 6: Drift Threshold Calibration ----
    if results:
        rpl_df_for_calib = build_rpl_across_anchors(results)
        tier_df_for_calib = build_tier_across_anchors(results)
        drift_out = build_drift_thresholds(rpl_df_for_calib, tier_df_for_calib)
        if drift_out and not drift_out["summary_df"].empty:
            ws = wb.create_sheet("Drift Threshold Calibration")
            ws["A1"] = "Empirical Drift Threshold Calibration"
            ws["A1"].font = TITLE_FONT
            ws["A2"] = ("Percentile distribution of anchor-to-anchor abs RPL %change. "
                        "Two distributions shown: confident-states-only (use these for "
                        "threshold setting) and all-states (sanity check only). "
                        "Confident = above click threshold in EVERY anchor.")
            ws["A2"].font = BODY_FONT

            ws["A4"] = "Aggregate distributions"
            ws["A4"].font = SECTION_FONT
            _write_df(ws, drift_out["summary_df"], header_row=5)

            n_summary_rows = len(drift_out["summary_df"]) + 1
            confident_count = len(drift_out["confident_states"])
            ws[f"A{6 + n_summary_rows + 1}"] = (
                f"Recommended thresholds from confident-states distribution "
                f"(n={confident_count} states):"
            )
            ws[f"A{6 + n_summary_rows + 1}"].font = SECTION_FONT
            # Pull p90/p95 from confident row (first row of summary)
            conf_row = drift_out["summary_df"].iloc[0]
            ws[f"A{7 + n_summary_rows + 1}"] = (
                f"  Watch threshold (≥p90, individual state move): {conf_row['p90']:.1%} RPL change"
            )
            ws[f"A{8 + n_summary_rows + 1}"] = (
                f"  Act threshold (≥p95, individual state move):   {conf_row['p95']:.1%} RPL change"
            )

            ws[f"A{10 + n_summary_rows + 1}"] = "Per-state max abs %change across the backtest window"
            ws[f"A{10 + n_summary_rows + 1}"].font = SECTION_FONT
            _write_df(ws, drift_out["per_state_df"], header_row=11 + n_summary_rows + 1)

    wb.save(output_path)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--anchors", type=str,
        default="2026-05-24,2026-05-17,2026-05-10,2026-05-03,2026-04-26,2026-04-19,2026-04-12,2026-04-07",
        help="Comma-separated list of YYYY-MM-DD anchor dates. "
             "Default: 8 weekly-spaced anchors over the last ~7 weeks "
             "(more anchors = better trajectory resolution for drifting states).",
    )
    parser.add_argument("--k", type=int, default=6)
    parser.add_argument("--rpl-blend", type=float, default=0.6)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--script-path", type=str, default=DEFAULT_SCRIPT_PATH,
                        help="Path to microsoft_geo_model_comparison.py")
    parser.add_argument("--skip-pipeline", action="store_true",
                        help="Skip running the pipeline; just aggregate existing anchor workbooks")
    parser.add_argument("--allow-pre-cutoff", action="store_true",
                        help="Run anchors even if their 12w window crosses the offline-uploads cutoff")
    args, extra_args = parser.parse_known_args(argv)

    print(f"Configuration:")
    print(f"  k = {args.k}")
    print(f"  rpl_blend = {args.rpl_blend}")
    print(f"  output_dir = {args.output_dir}")
    print(f"  script_path = {args.script_path}")
    print()

    anchors_list = parse_anchors(args.anchors)
    if not anchors_list:
        print("ERROR: no anchors provided", file=sys.stderr)
        return 1

    anchor_statuses = [validate_anchor(a) for a in anchors_list]

    print(f"Anchor validation:")
    runnable = []
    for s in anchor_statuses:
        flag = "[CLEAN]" if s.is_clean else "[PRE-CUTOFF]"
        print(f"  {flag} {s.anchor_date}  (12w_start={s.w12_start})")
        if s.warning:
            print(f"           {s.warning}")
        if s.is_clean or args.allow_pre_cutoff:
            runnable.append(s)
    print()

    if not runnable:
        print("ERROR: no clean anchors to run. Use --allow-pre-cutoff to override.")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run the pipeline for each anchor (or skip and read existing outputs)
    results = []
    for status in runnable:
        if args.skip_pipeline:
            # Try to find an existing workbook
            anchor_dir = output_dir / f"anchor_{status.anchor_date.isoformat()}"
            candidates = list(anchor_dir.glob("microsoft_geo_model_comparison_*.xlsx"))
            wb_path = candidates[0] if candidates else None
            if wb_path is None:
                print(f"  SKIP: no existing workbook for {status.anchor_date}")
                continue
        else:
            wb_path = run_pipeline_for_anchor(
                status.anchor_date, args.script_path, args.db_path,
                output_dir, args.k, args.rpl_blend, extra_args,
            )
        if wb_path is None:
            continue
        ar = extract_anchor_result(status, wb_path)
        if ar:
            results.append(ar)

    if not results:
        print("ERROR: no anchor pipelines produced usable output.", file=sys.stderr)
        return 1

    latest_anchor = max(r.anchor_date for r in results)
    out_path = output_dir / f"backtest_comparison_{latest_anchor.isoformat()}.xlsx"
    print(f"\nBuilding backtest comparison workbook: {out_path}")
    write_backtest_workbook(out_path, anchor_statuses, results, args.k, args.rpl_blend)
    print(f"Done. Ran {len(results)} of {len(runnable)} anchors successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
