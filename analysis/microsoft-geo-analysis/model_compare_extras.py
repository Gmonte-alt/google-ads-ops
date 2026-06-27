"""Additional analytics layered on top of model_compare.

This module is imported by model_compare.py via `from model_compare_extras import *`
at the end. Keeping it separate so we can iterate on the new features without
churning the core file.
"""

from __future__ import annotations

import pickle
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================================
# Below-threshold state assignment
# ============================================================================

def assign_below_threshold_states(
    all_states_with_metrics: pd.DataFrame,
    eligible_pool_with_tiers: pd.DataFrame,
    model_col: str = "A2_rpl_quantile",
    df_4w: pd.DataFrame | None = None,
    blend_weight: float = 0.0,
    target_roas: float = 1.50,
) -> pd.DataFrame:
    """For each below-threshold state, assign it to the nearest tier by RPL.

    Below-threshold = not in eligible pool (i.e., <200 clicks, UNKNOWN, or
    missing RPL/CPC). States with zero conversions go to UNCLASSIFIED.

    RPL basis for provisional states (gateless blend):
      Provisional states are low-volume by definition, so the eligible-pool
      blend gate (min_blend_clicks=75) skips all of them and leaves them on
      raw 12w RPL — inconsistent with committed states, which are tiered on
      blended RPL. To make the basis consistent, this function recomputes each
      provisional state's RPL with the SAME blend weight but NO click gate:

        - if the state has >0 conversions in the 4w window:
              rpl = (1 - blend_weight)*rpl_12w + blend_weight*rpl_4w
        - if the state has 0 conversions in the 4w window (no measurable 4w RPL):
              rpl = rpl_12w  (full 12-week fallback)

      States with 0 conversions over the full 12w remain UNCLASSIFIED as before.
      When blend_weight <= 0 or df_4w is None, behavior is unchanged (raw 12w).
      Two diagnostic columns are added: rpl_12w_raw and provisional_blend.

    Returns one row per below-threshold state:
      state, clicks, spend, conversions, revenue, rpl, rpl_12w_raw,
      provisional_blend, cpc, ideal_tcpa, confidence, assigned_tier,
      tier_centroid_rpl, rpl_distance, watchlist_flag, watchlist_reason
    """
    # 4w lookup for the gateless provisional blend
    blend_weight = min(max(blend_weight or 0.0, 0.0), 1.0)
    use_blend = blend_weight > 0.0 and df_4w is not None and not df_4w.empty
    df_4w_indexed = df_4w.set_index("state") if use_blend else None
    # Compute tier centroids (mean RPL) from the eligible pool
    tier_centroids = (
        eligible_pool_with_tiers.groupby(model_col)["rpl"].mean()
        .sort_values(ascending=False)
        .to_dict()
    )

    # Below-threshold = in the full state list but not in the eligible pool
    eligible_states = set(eligible_pool_with_tiers["state"].tolist())
    below = all_states_with_metrics[
        ~all_states_with_metrics["state"].isin(eligible_states)
    ].copy().reset_index(drop=True)

    if below.empty:
        return pd.DataFrame()

    rows = []
    for _, r in below.iterrows():
        state = r["state"]
        clicks = r.get("clicks", 0) or 0
        conv = r.get("conversions", 0) or 0
        rpl_12w_raw = r.get("rpl", np.nan)
        cpc = r.get("cpc", np.nan)

        # --- Gateless provisional RPL blend ---------------------------------
        # The eligible-pool blend already ran with a 75-click gate, so for
        # provisional states `rpl` is still the raw 12w value. Recompute here
        # with the same weight but no click gate, deciding blend vs. fallback
        # on whether the state had any conversions in the 4w window.
        rpl = rpl_12w_raw
        provisional_blend = "12w_only"
        if use_blend and not pd.isna(rpl_12w_raw) and state in df_4w_indexed.index:
            r4 = df_4w_indexed.loc[state]
            conv_4w = r4.get("conversions", 0) or 0
            rpl_4w = r4.get("rpl", np.nan)
            if conv_4w > 0 and not pd.isna(rpl_4w) and rpl_4w > 0:
                rpl = (1.0 - blend_weight) * rpl_12w_raw + blend_weight * rpl_4w
                provisional_blend = f"{int((1-blend_weight)*100)}/{int(blend_weight*100)} 12w/4w"
            # else: no 4w conversions -> keep full 12w RPL (fallback)

        # ideal_tcpa derived from the (possibly blended) provisional RPL
        ideal_tcpa = rpl / target_roas if not pd.isna(rpl) and rpl > 0 else np.nan

        # Confidence flag
        if state.upper() == "UNKNOWN":
            confidence = "UNCLASSIFIED"
            reason_prefix = "UNKNOWN state (geolocation)"
        elif conv == 0 or pd.isna(rpl) or pd.isna(cpc):
            confidence = "UNCLASSIFIED"
            reason_prefix = "no conversions / no RPL"
        elif clicks < 50:
            confidence = "LOW"
            reason_prefix = f"{int(clicks)} clicks"
        elif clicks < 200:
            confidence = "MEDIUM"
            reason_prefix = f"{int(clicks)} clicks"
        else:
            # Shouldn't happen — this state would be in eligible pool
            confidence = "MEDIUM"
            reason_prefix = "edge case"

        # Tier assignment by nearest RPL centroid (only if RPL is measurable)
        if confidence == "UNCLASSIFIED" or pd.isna(rpl):
            assigned_tier = "UNCLASSIFIED"
            tier_centroid = np.nan
            rpl_distance = np.nan
            watchlist_flag = False
            watchlist_reason = ""
        else:
            best_tier = min(
                tier_centroids.keys(),
                key=lambda t: abs(tier_centroids[t] - rpl),
            )
            assigned_tier = best_tier
            tier_centroid = tier_centroids[best_tier]
            rpl_distance = abs(rpl - tier_centroid)

            # Watchlist: state's measured RPL is dramatically different from any
            # tier centroid, suggesting it doesn't fit well anywhere
            # (e.g., a state with RPL $90 but low confidence ends up assigned
            # to TIER ONE at $80 centroid — fine — but if it's at $30 and
            # the nearest tier centroid is $50, the assignment is uncertain)
            relative_gap = rpl_distance / tier_centroid if tier_centroid > 0 else 0
            if relative_gap > 0.30:
                watchlist_flag = True
                watchlist_reason = (
                    f"RPL {rpl:.0f} is {relative_gap*100:.0f}% from {assigned_tier} "
                    f"centroid {tier_centroid:.0f}"
                )
            elif confidence == "LOW" and rpl >= max(tier_centroids.values()) * 0.8:
                # Low-confidence state with potentially-premium RPL — escalate if volume picks up
                watchlist_flag = True
                watchlist_reason = (
                    f"low-confidence but RPL {rpl:.0f} suggests premium tier; "
                    f"escalate if clicks grow"
                )
            else:
                watchlist_flag = False
                watchlist_reason = ""

        rows.append({
            "state": state,
            "clicks": int(clicks) if pd.notna(clicks) else 0,
            "spend": r.get("spend", 0.0) or 0.0,
            "conversions": float(conv) if pd.notna(conv) else 0.0,
            "revenue": r.get("revenue", 0.0) or 0.0,
            "rpl": rpl,
            "rpl_12w_raw": rpl_12w_raw,
            "provisional_blend": provisional_blend,
            "cpc": cpc,
            "ideal_tcpa": ideal_tcpa,
            "confidence": confidence,
            "assigned_tier": assigned_tier,
            "tier_centroid_rpl": tier_centroid,
            "rpl_distance": rpl_distance,
            "watchlist_flag": watchlist_flag,
            "watchlist_reason": (
                f"{reason_prefix}: {watchlist_reason}" if watchlist_reason
                else reason_prefix
            ),
        })

    out = pd.DataFrame(rows)
    # Sort: UNCLASSIFIED last, then by clicks descending within other groups
    out["_sort_key"] = out["confidence"].map(
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNCLASSIFIED": 3}
    )
    out = out.sort_values(["_sort_key", "clicks"], ascending=[True, False])
    return out.drop(columns=["_sort_key"]).reset_index(drop=True)


# ============================================================================
# GP upside metric
# ============================================================================

def gp_upside_metrics(
    eligible_pool_with_tiers: pd.DataFrame,
    state_to_campaign: dict[str, str],
    state_to_campaign_tcpa: dict[str, float],
    model_col: str,
) -> dict:
    """Compute GP-upside metrics for one model.

    Per state:
      upside_vs_current_campaign = max(0, ideal_tcpa − current_campaign_tcpa) × conversions
        ("GP currently left on the table due to under-bidding")
      upside_vs_tier             = max(0, ideal_tcpa − tier_tcpa) × conversions
        ("GP still available within-tier after model regrouping")

    Returns aggregate values:
      gp_upside_vs_current_campaign  (Σ across all eligible states)
      gp_upside_vs_tier              (Σ across all eligible states)
      gp_upside_capture_rate         = 1 - (vs_tier / vs_current)
        ("% of upside captured by adopting the model's tiers")
    """
    df = eligible_pool_with_tiers.copy()
    df["current_campaign"] = df["state"].map(state_to_campaign)
    df["current_campaign_tcpa"] = df["state"].map(state_to_campaign_tcpa)

    # Tier-level target tCPA = tier weighted RPL / 1.5
    tier_target_map = {}
    for tier, grp in df.groupby(model_col):
        if grp["spend"].sum() > 0:
            w_rpl = (grp["rpl"] * grp["spend"]).sum() / grp["spend"].sum()
        else:
            w_rpl = grp["rpl"].mean()
        tier_target_map[tier] = w_rpl / 1.50
    df["tier_tcpa"] = df[model_col].map(tier_target_map)

    df["upside_vs_current"] = (
        (df["ideal_tcpa"] - df["current_campaign_tcpa"]).clip(lower=0)
        * df["conversions"]
    )
    df["upside_vs_tier"] = (
        (df["ideal_tcpa"] - df["tier_tcpa"]).clip(lower=0)
        * df["conversions"]
    )

    total_vs_current = df["upside_vs_current"].sum()
    total_vs_tier = df["upside_vs_tier"].sum()
    capture_rate = (
        1.0 - (total_vs_tier / total_vs_current)
        if total_vs_current > 0 else np.nan
    )

    return {
        "gp_upside_vs_current_campaign": total_vs_current,
        "gp_upside_vs_tier": total_vs_tier,
        "gp_upside_capture_rate": capture_rate,
    }


# ============================================================================
# Reallocation recommendations (per state)
# ============================================================================

def state_reallocation_recommendations(
    eligible_pool_with_tiers: pd.DataFrame,
    state_to_campaign: dict[str, str],
    model_col: str = "A2_rpl_quantile",
) -> pd.Series:
    """For each state, return MOVE_TO_<tier> or STAY.

    Logic:
      - STAY if the state's tier matches the campaign's plurality tier
        (the most common tier across the campaign's members).
      - If state's tier is the plurality but tied with another tier, STAY.
      - Otherwise MOVE_TO_<state's tier>.

    Rationale: in heterogeneous campaigns (multiple tiers represented), the
    plurality represents the campaign's "center of mass." States in that
    plurality are operationally aligned with the campaign; states outside it
    are the move candidates.
    """
    df = eligible_pool_with_tiers.copy()
    df["current_campaign"] = df["state"].map(state_to_campaign)
    df = df[df["current_campaign"].notna()]

    # For each campaign, find the set of plurality tiers (handles ties)
    plurality_tiers = {}
    for camp, grp in df.groupby("current_campaign"):
        counts = grp[model_col].value_counts()
        if len(counts) == 0:
            plurality_tiers[camp] = set()
            continue
        top = counts.iloc[0]
        plurality_tiers[camp] = set(counts[counts == top].index.tolist())

    def _rec(row):
        camp = row["current_campaign"]
        state_tier = row[model_col]
        plur = plurality_tiers.get(camp, set())
        if state_tier in plur:
            return "STAY"
        return f"MOVE_TO_{state_tier}"

    rec = df.apply(_rec, axis=1)
    out = pd.Series(index=eligible_pool_with_tiers.index, dtype=object)
    out.loc[df.index] = rec
    return out


# ============================================================================
# RPL drift watchlist
# ============================================================================

def build_drift_watchlist(
    result_12w: pd.DataFrame,
    result_4w: pd.DataFrame | None,
    result_8w: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compare 12w RPL against most-recent-available short window.

    For each 12w-eligible state, computes:
      rpl_12w, rpl_recent (4w if available, else 8w),
      recent_window_used, rpl_diff, rpl_pct_change,
      direction (up/down/stable), magnitude_flag (HIGH/MEDIUM/LOW)
    """
    if result_12w is None or result_12w.empty:
        return pd.DataFrame()

    rows = []
    for _, r in result_12w.iterrows():
        state = r["state"]
        rpl_12w = r["rpl"]
        rpl_recent = np.nan
        recent_window = "none"

        # Prefer 4w; fall back to 8w if 4w doesn't have the state
        if result_4w is not None:
            m = result_4w[result_4w["state"] == state]
            if not m.empty:
                rpl_recent = m.iloc[0]["rpl"]
                recent_window = "4w"
        if pd.isna(rpl_recent) and result_8w is not None:
            m = result_8w[result_8w["state"] == state]
            if not m.empty:
                rpl_recent = m.iloc[0]["rpl"]
                recent_window = "8w"

        if pd.isna(rpl_recent):
            rpl_diff = np.nan
            rpl_pct = np.nan
            direction = "no_recent_data"
            magnitude = "N/A"
        else:
            rpl_diff = rpl_recent - rpl_12w
            rpl_pct = (rpl_diff / rpl_12w) if rpl_12w > 0 else np.nan
            if abs(rpl_pct) < 0.10:
                direction = "stable"
                magnitude = "LOW"
            elif rpl_pct > 0:
                direction = "up"
                if rpl_pct >= 0.30:
                    magnitude = "HIGH"
                elif rpl_pct >= 0.20:
                    magnitude = "MEDIUM"
                else:
                    magnitude = "LOW"
            else:
                direction = "down"
                if rpl_pct <= -0.30:
                    magnitude = "HIGH"
                elif rpl_pct <= -0.20:
                    magnitude = "MEDIUM"
                else:
                    magnitude = "LOW"

        rows.append({
            "state": state,
            "spend_12w": r["spend"],
            "rpl_12w": rpl_12w,
            "rpl_recent": rpl_recent,
            "recent_window_used": recent_window,
            "rpl_diff": rpl_diff,
            "rpl_pct_change": rpl_pct,
            "direction": direction,
            "magnitude": magnitude,
        })

    out = pd.DataFrame(rows)
    # Sort by absolute % change descending so largest movers are on top
    out["_abs"] = out["rpl_pct_change"].abs()
    out = out.sort_values("_abs", ascending=False, na_position="last")
    return out.drop(columns=["_abs"]).reset_index(drop=True)


# ============================================================================
# Dendrogram cuts → proposed campaign structures
# ============================================================================

def proposed_structures_from_dendrogram(
    linkage_matrix,
    state_order: list[str],
    eligible_pool_with_tiers: pd.DataFrame,
    state_to_campaign: dict[str, str],
    cut_distances: list[float] = (2.0, 3.0, 3.85, 5.0),
    target_roas: float = 1.50,
) -> pd.DataFrame:
    """For each Ward distance cut, propose groupings and per-group metrics.

    Returns long-format dataframe:
      cut_distance, n_groups, group_id, member_states, n_states,
      total_spend, weighted_rpl, target_tcpa, action_classification
    """
    from scipy.cluster.hierarchy import fcluster

    rpl_map = dict(zip(eligible_pool_with_tiers["state"], eligible_pool_with_tiers["rpl"]))
    spend_map = dict(zip(eligible_pool_with_tiers["state"], eligible_pool_with_tiers["spend"]))
    conv_map = dict(zip(eligible_pool_with_tiers["state"], eligible_pool_with_tiers["conversions"]))

    rows = []
    for cut in cut_distances:
        labels = fcluster(linkage_matrix, t=cut, criterion="distance")
        n_groups = len(set(labels))

        # Build groupings
        group_map = {}
        for state, label in zip(state_order, labels):
            group_map.setdefault(label, []).append(state)

        for raw_label, members in group_map.items():
            members_sorted = sorted(members)
            total_spend = sum(spend_map.get(s, 0) for s in members)
            total_conv = sum(conv_map.get(s, 0) for s in members)
            if total_spend > 0:
                weighted_rpl = sum(rpl_map.get(s, 0) * spend_map.get(s, 0) for s in members) / total_spend
            else:
                weighted_rpl = np.mean([rpl_map.get(s, np.nan) for s in members])
            target_tcpa = weighted_rpl / target_roas if pd.notna(weighted_rpl) else np.nan

            # Action classification — how disruptive is implementing this grouping?
            # Look at the current campaigns these states are spread across.
            current_camps = set()
            for s in members:
                c = state_to_campaign.get(s)
                if c:
                    current_camps.add(c)
            n_current_camps_touched = len(current_camps)

            if n_current_camps_touched == 0:
                action_class = "all_below_threshold"
            elif n_current_camps_touched == 1 and len(members) == 1:
                action_class = "in_place_tcpa_only"
            elif n_current_camps_touched == 1:
                action_class = "in_place_no_move"
            elif n_current_camps_touched <= 3:
                action_class = "re_tier_partial"
            else:
                action_class = "campaign_split_required"

            rows.append({
                "cut_distance": cut,
                "n_groups_total_at_cut": n_groups,
                "group_id": int(raw_label),
                "n_states": len(members),
                "member_states": ", ".join(members_sorted),
                "total_spend": total_spend,
                "total_conversions": total_conv,
                "weighted_rpl": weighted_rpl,
                "target_tcpa_150_roas": target_tcpa,
                "n_current_campaigns_touched": n_current_camps_touched,
                "action_classification": action_class,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["cut_distance", "weighted_rpl"], ascending=[True, False])
    return df.reset_index(drop=True)


# ============================================================================
# k-sensitivity analysis
# ============================================================================

def k_sensitivity_table(
    eligible_pool: pd.DataFrame,
    k_range: list[int],
    state_to_campaign: dict[str, str],
    state_to_campaign_tcpa: dict[str, float],
    apply_all_models_func,
    model_summary_func,
    compare_to_campaigns_func,
) -> pd.DataFrame:
    """For each k, run all models and emit one row per (model, k) combo.

    Reuses caller-provided functions to avoid circular imports.
    """
    rows = []
    for k in k_range:
        if k > len(eligible_pool):
            continue
        result, _ = apply_all_models_func(eligible_pool, k)
        for model_col in ["A2_rpl_quantile", "B1_kmeans_rpl_cpc",
                          "B4_hierarchical", "C2_weighted_kmeans"]:
            summary = model_summary_func(result, model_col)
            compare = compare_to_campaigns_func(result, state_to_campaign, model_col)
            upside = gp_upside_metrics(
                result, state_to_campaign, state_to_campaign_tcpa, model_col,
            )
            # Tier-size warnings
            tier_sizes = result[model_col].value_counts()
            largest_tier_size = int(tier_sizes.max()) if len(tier_sizes) > 0 else 0
            smallest_tier_size = int(tier_sizes.min()) if len(tier_sizes) > 0 else 0
            rows.append({
                "k": k,
                "model": model_col,
                "n_tiers_populated": summary.get("n_tiers_populated"),
                "largest_tier_size": largest_tier_size,
                "smallest_tier_size": smallest_tier_size,
                "avg_within_tier_rpl_spread": summary.get("avg_within_tier_rpl_spread"),
                "spend_weighted_bid_loss": summary.get("spend_weighted_bid_loss"),
                "gp_upside_vs_tier": upside.get("gp_upside_vs_tier"),
                "rand_index": compare.get("rand_index"),
                "adjusted_rand_index": compare.get("adjusted_rand_index"),
            })
    return pd.DataFrame(rows)


# ============================================================================
# Snapshot save / load / compare
# ============================================================================

def save_snapshot(
    path: Path, snapshot: dict,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_snapshot(path: Path) -> dict:
    with open(path, "rb") as f:
        return pickle.load(f)


def compare_snapshots(prior: dict, current: dict, model_col: str = "A2_rpl_quantile") -> pd.DataFrame:
    """Compare a prior snapshot's tier assignments to current.

    Returns dataframe per state:
      state, prior_tier, current_tier, change
    """
    p_df = prior.get("result_12w")
    c_df = current.get("result_12w")
    if p_df is None or c_df is None:
        return pd.DataFrame()

    p_map = dict(zip(p_df["state"], p_df[model_col]))
    c_map = dict(zip(c_df["state"], c_df[model_col]))

    all_states = sorted(set(p_map.keys()) | set(c_map.keys()))
    rows = []
    for s in all_states:
        pt = p_map.get(s, "NOT_IN_PRIOR")
        ct = c_map.get(s, "NOT_IN_CURRENT")
        if pt == ct:
            change = "unchanged"
        elif pt == "NOT_IN_PRIOR":
            change = "new_in_current"
        elif ct == "NOT_IN_CURRENT":
            change = "dropped_from_current"
        else:
            change = "tier_changed"
        rows.append({
            "state": s,
            "prior_tier": pt,
            "current_tier": ct,
            "change": change,
        })
    out = pd.DataFrame(rows)
    return out


# ============================================================================
# Method consistency score (state-level cross-model agreement)
# ============================================================================

def method_consistency_score(states_with_tiers: pd.DataFrame) -> pd.DataFrame:
    """For each state, count how many of the 4 models assign the same tier
    as the headline method (C2). Returns a series of integers 1..4 plus a
    score column = count/4.

    Higher = more agreement. A state with 4/4 method consistency is one all
    methods agree belongs in that tier.
    """
    methods = ["A2_rpl_quantile", "B1_kmeans_rpl_cpc",
               "B4_hierarchical", "C2_weighted_kmeans"]
    df = states_with_tiers.copy()
    headline = "C2_weighted_kmeans"
    df["n_methods_agree_with_headline"] = df.apply(
        lambda r: sum(1 for m in methods if r[m] == r[headline]), axis=1,
    )
    df["method_consistency_score"] = df["n_methods_agree_with_headline"] / 4.0
    return df[["state", "n_methods_agree_with_headline", "method_consistency_score"]]


# ============================================================================
# Window consistency score (cross-window stability per state, per method)
# ============================================================================

def window_consistency_per_state(
    stability_df: pd.DataFrame,
    model: str = "C2_weighted_kmeans",
) -> pd.DataFrame:
    """For each state under a model, compute window_consistency_score:
       count of windows where state has the same tier as 12w / count of
       windows where state was eligible.

    Range 1/1 = 1.0 (perfectly stable) to 1/3 = 0.33 (only matched 12w once,
    if the model's 8w and 4w both gave different tiers).
    """
    s = stability_df[stability_df["model"] == model].copy()
    if s.empty:
        return pd.DataFrame()

    def _score(row):
        tiers = [row["tier_12w"], row["tier_8w"], row["tier_4w"]]
        valid = [t for t in tiers if pd.notna(t) and t]
        if not valid:
            return (0, 0, 0.0)
        target = row["tier_12w"]
        if pd.isna(target) or not target:
            return (len(valid), 0, 0.0)
        matches = sum(1 for t in valid if t == target)
        return (len(valid), matches, matches / len(valid) if valid else 0.0)

    results = s.apply(_score, axis=1)
    s["n_windows_eligible"] = [r[0] for r in results]
    s["n_windows_matching_12w"] = [r[1] for r in results]
    s["window_consistency_score"] = [r[2] for r in results]
    return s[["state", "n_windows_eligible", "n_windows_matching_12w", "window_consistency_score"]]


# ============================================================================
# Per-state diagnostic columns
# ============================================================================

def rank_and_tier_distance(states_with_tiers: pd.DataFrame, tier_col: str) -> pd.DataFrame:
    """For each state, compute rank in eligible pool and distance to tier
    boundaries.
    """
    df = states_with_tiers.copy()
    df = df.sort_values("rpl", ascending=False).reset_index(drop=True)
    df["rank_in_eligible_pool"] = range(1, len(df) + 1)

    tier_mins = df.groupby(tier_col)["rpl"].min().to_dict()
    df["rpl_to_tier_min"] = df.apply(
        lambda r: r["rpl"] - tier_mins.get(r[tier_col], r["rpl"]), axis=1,
    )
    return df[["state", "rank_in_eligible_pool", "rpl_to_tier_min"]]


# ============================================================================
# Tier boundary justification
# ============================================================================

def build_tier_boundary_justification(
    states_with_tiers: pd.DataFrame, tier_col: str,
) -> pd.DataFrame:
    """For each adjacent tier pair, identify the boundary states and the
    RPL gap separating them. Compare to the largest alternative gap nearby
    to show whether the chosen boundary is at a natural break point.
    """
    df = states_with_tiers.sort_values("rpl", ascending=False).reset_index(drop=True)
    tier_order = []
    seen = set()
    for t in df[tier_col]:
        if t not in seen:
            tier_order.append(t)
            seen.add(t)

    rows = []
    for i in range(len(tier_order) - 1):
        upper = tier_order[i]
        lower = tier_order[i + 1]
        upper_min_row = df[df[tier_col] == upper].iloc[-1]
        lower_max_row = df[df[tier_col] == lower].iloc[0]
        gap = upper_min_row["rpl"] - lower_max_row["rpl"]

        # Compare to adjacent within-tier gaps to assess whether this is
        # the largest natural break in the region.
        upper_states = df[df[tier_col] == upper].reset_index(drop=True)
        lower_states = df[df[tier_col] == lower].reset_index(drop=True)
        nearby_gaps = []
        if len(upper_states) >= 2:
            nearby_gaps.append(
                upper_states.iloc[-2]["rpl"] - upper_states.iloc[-1]["rpl"]
            )
        if len(lower_states) >= 2:
            nearby_gaps.append(
                lower_states.iloc[0]["rpl"] - lower_states.iloc[1]["rpl"]
            )
        max_nearby = max(nearby_gaps) if nearby_gaps else 0.0
        is_largest_local = gap >= max_nearby

        rows.append({
            "boundary": f"{upper} → {lower}",
            "upper_tier_floor_state": upper_min_row["state"],
            "upper_tier_floor_rpl": upper_min_row["rpl"],
            "lower_tier_ceiling_state": lower_max_row["state"],
            "lower_tier_ceiling_rpl": lower_max_row["rpl"],
            "rpl_gap": gap,
            "largest_nearby_within_tier_gap": max_nearby,
            "is_largest_local_break": "YES" if is_largest_local else "NO",
        })
    return pd.DataFrame(rows)


# ============================================================================
# k justification — metrics across k=2..8 and GP capture chart
# ============================================================================

def build_k_justification_table(k_sensitivity_df: pd.DataFrame, headline_model: str = "C2_weighted_kmeans") -> pd.DataFrame:
    """Pivot the k_sensitivity raw data into a clean per-k summary for the
    headline model. Adds marginal columns.
    """
    if k_sensitivity_df is None or k_sensitivity_df.empty:
        return pd.DataFrame()
    sub = k_sensitivity_df[k_sensitivity_df["model"] == headline_model].copy()
    sub = sub.sort_values("k").reset_index(drop=True)

    # Marginal bid-loss reduction per added tier
    sub["bid_loss_prev"] = sub["spend_weighted_bid_loss"].shift(1)
    sub["bid_loss_reduction"] = sub["bid_loss_prev"] - sub["spend_weighted_bid_loss"]
    sub["bid_loss_reduction_pct_of_prev"] = sub["bid_loss_reduction"] / sub["bid_loss_prev"]

    # GP capture rate proxy = 1 - bid_loss / max(bid_loss across k)
    bl_max = sub["spend_weighted_bid_loss"].max()
    sub["gp_capture_rate"] = 1.0 - (sub["spend_weighted_bid_loss"] / bl_max) if bl_max > 0 else 0.0

    # Average tier size
    sub["avg_tier_size"] = sub.apply(
        lambda r: r.get("largest_tier_size", 0) / 2 if r.get("n_tiers_populated") else 0,
        axis=1,
    )

    out_cols = ["k", "n_tiers_populated", "largest_tier_size", "smallest_tier_size",
                "avg_within_tier_rpl_spread", "spend_weighted_bid_loss",
                "bid_loss_reduction", "bid_loss_reduction_pct_of_prev",
                "gp_capture_rate"]
    return sub[[c for c in out_cols if c in sub.columns]]


def render_k_justification_png(k_df: pd.DataFrame, headline_k: int = 6,
                                current_k: int = 12) -> bytes:
    """Render the elbow chart: GP capture rate by k, with markers at both
    the recommended k and the current structure k.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import io

    if k_df is None or k_df.empty:
        return b""

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(k_df["k"], k_df["gp_capture_rate"] * 100, "o-",
            color="#7030A0", linewidth=2, markersize=8)

    # Highlight recommended k
    rec_row = k_df[k_df["k"] == headline_k]
    if not rec_row.empty:
        rec_y = rec_row["gp_capture_rate"].iloc[0] * 100
        ax.plot(headline_k, rec_y, "o", markersize=18,
                markerfacecolor="none", markeredgecolor="#00A651",
                markeredgewidth=3, label=f"Recommended (k={headline_k})")
        ax.annotate(f"Recommended\nk={headline_k} ({rec_y:.0f}%)",
                    xy=(headline_k, rec_y),
                    xytext=(headline_k + 0.6, rec_y - 12),
                    fontsize=11, fontweight="bold", color="#00A651",
                    arrowprops=dict(arrowstyle="-", color="#00A651", lw=1))

    # Highlight current structure k
    cur_row = k_df[k_df["k"] == current_k]
    if not cur_row.empty:
        cur_y = cur_row["gp_capture_rate"].iloc[0] * 100
        ax.plot(current_k, cur_y, "s", markersize=18,
                markerfacecolor="none", markeredgecolor="#C00000",
                markeredgewidth=3, label=f"Current (k={current_k})")
        ax.annotate(f"Current\nstructure\nk={current_k} ({cur_y:.0f}%)",
                    xy=(current_k, cur_y),
                    xytext=(current_k - 2.5, cur_y - 18),
                    fontsize=11, fontweight="bold", color="#C00000",
                    arrowprops=dict(arrowstyle="-", color="#C00000", lw=1))

    # Annotation explaining the marginal gain
    if not rec_row.empty and not cur_row.empty:
        marginal = (cur_y - rec_y)
        annotation_x = (headline_k + current_k) / 2
        ax.annotate(
            f"Going from k={headline_k} to k={current_k} only adds {marginal:.0f}%\n"
            f"of GP capture — at the cost of {current_k - headline_k} extra campaigns to manage.",
            xy=(annotation_x, 50),
            ha="center", fontsize=10, style="italic",
            color="#444444",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#FFFAE6",
                      edgecolor="#999999"),
        )

    ax.set_xlabel("Number of tiers (k)", fontsize=12)
    ax.set_ylabel("GP capture rate (% of max possible)", fontsize=12)
    ax.set_title("Elbow Plot — GP Capture Rate by Tier Count\n"
                 "(Why we recommend fewer tiers than the current structure)",
                 fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0, 110)
    ax.legend(loc="lower right", fontsize=10)
    ax.set_xticks(k_df["k"].tolist())

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ============================================================================
# Network economics + pattern labeling
# ============================================================================

def build_network_economics(
    conn,
    start_date,
    end_date,
    enabled_campaign_ids: list,
    state_list: list,
    pull_state_aggregates_func,
    search_networks: set,
    audience_networks: set,
    target_roas: float = 1.50,
) -> pd.DataFrame:
    """Per-state search and audience economics (RPL, CPL, ideal tCPA).

    Returns one row per state in state_list with both network breakdowns.
    """
    search_df = pull_state_aggregates_func(
        conn, start_date, end_date, enabled_campaign_ids, search_networks,
    )
    audience_df = pull_state_aggregates_func(
        conn, start_date, end_date, enabled_campaign_ids, audience_networks,
    )

    def _enrich(df: pd.DataFrame, suffix: str) -> pd.DataFrame:
        df = df.copy()
        df[f"clicks_{suffix}"] = df["clicks"]
        df[f"spend_{suffix}"] = df["spend"]
        df[f"conversions_{suffix}"] = df["conversions"]
        df[f"revenue_{suffix}"] = df["revenue"]
        df[f"rpl_{suffix}"] = df["revenue"] / df["conversions"].where(df["conversions"] > 0)
        df[f"cpl_{suffix}"] = df["spend"] / df["conversions"].where(df["conversions"] > 0)
        df[f"cpc_{suffix}"] = df["spend"] / df["clicks"].where(df["clicks"] > 0)
        df[f"ideal_tcpa_{suffix}"] = df[f"rpl_{suffix}"] / target_roas
        keep = ["state", f"clicks_{suffix}", f"spend_{suffix}",
                f"conversions_{suffix}", f"revenue_{suffix}",
                f"rpl_{suffix}", f"cpl_{suffix}", f"cpc_{suffix}",
                f"ideal_tcpa_{suffix}"]
        return df[keep]

    s = _enrich(search_df, "search") if not search_df.empty else pd.DataFrame(columns=["state"])
    a = _enrich(audience_df, "audience") if not audience_df.empty else pd.DataFrame(columns=["state"])

    base = pd.DataFrame({"state": state_list})
    out = base.merge(s, on="state", how="left").merge(a, on="state", how="left")
    return out


def label_pattern(row, pct_threshold: float = 0.15, click_threshold: int = 200) -> str:
    """Pattern A: search RPL > audience RPL by >threshold (risk: audience may
    over-bid). Pattern B: audience RPL > search RPL by >threshold (lower risk
    — audience economics also support higher bid). N/A: within threshold or
    low-volume on either network.
    """
    s_rpl = row.get("rpl_search")
    a_rpl = row.get("rpl_audience")
    s_clicks = row.get("clicks_search") or 0
    a_clicks = row.get("clicks_audience") or 0

    if pd.isna(s_rpl) or pd.isna(a_rpl) or s_rpl <= 0 or a_rpl <= 0:
        return "N/A"
    if s_clicks < click_threshold or a_clicks < click_threshold:
        return "N/A"
    if s_rpl >= a_rpl * (1 + pct_threshold):
        return "A"
    if a_rpl >= s_rpl * (1 + pct_threshold):
        return "B"
    return "N/A"


def label_fitness(state_rpl: float, state_clicks: int, campaign_tcpa: float,
                  target_roas: float = 1.50, tolerance: float = 0.20,
                  click_threshold: int = 200) -> str:
    """Returns well_fit / underbid / overbid / low_volume."""
    if pd.isna(state_rpl) or pd.isna(state_clicks) or state_clicks < click_threshold:
        return "low_volume"
    if pd.isna(campaign_tcpa) or campaign_tcpa <= 0:
        return "no_tcpa_ref"
    ideal = state_rpl / target_roas
    ratio = ideal / campaign_tcpa
    if 1 - tolerance <= ratio <= 1 + tolerance:
        return "well_fit"
    if ratio > 1 + tolerance:
        return "underbid"
    return "overbid"


# ============================================================================
# Master Tier Review sheet builder
# ============================================================================

def build_master_tier_review(
    eligible_with_tiers: pd.DataFrame,
    below_threshold_df: pd.DataFrame,
    network_economics: pd.DataFrame,
    state_to_campaign: dict,
    state_to_campaign_tcpa: dict,
    headline_model: str = "C2_weighted_kmeans",
    target_roas: float = 1.50,
    pattern_threshold: float = 0.15,
    click_threshold: int = 200,
    capture_low: float = 0.40,
    capture_high: float = 0.70,
) -> pd.DataFrame:
    """The master view: every state (above + below threshold) grouped under
    its C2 tier with network economics, Pattern label, current vs proposed
    tCPA, GP-upside estimate, and decision rationale.
    """
    # Tier centroids from the eligible pool — used to assign below-threshold
    centroids = (
        eligible_with_tiers.groupby(headline_model)["rpl"].mean()
        .sort_values(ascending=False)
    )
    tier_target_tcpa = {
        t: ((eligible_with_tiers[eligible_with_tiers[headline_model] == t]["rpl"] *
             eligible_with_tiers[eligible_with_tiers[headline_model] == t]["spend"]).sum()
            / eligible_with_tiers[eligible_with_tiers[headline_model] == t]["spend"].sum()) / target_roas
        for t in centroids.index
    }

    rows = []

    # Above-threshold states
    for _, r in eligible_with_tiers.iterrows():
        rows.append(_master_row(
            r, "above", network_economics, state_to_campaign,
            state_to_campaign_tcpa, headline_model, tier_target_tcpa,
            target_roas, pattern_threshold, click_threshold,
            capture_low, capture_high,
        ))

    # Below-threshold states — assign to nearest centroid by RPL
    if below_threshold_df is not None and not below_threshold_df.empty:
        for _, r in below_threshold_df.iterrows():
            if pd.isna(r.get("rpl")) or r.get("confidence") == "UNCLASSIFIED":
                assigned = "UNCLASSIFIED"
            else:
                assigned = min(centroids.index, key=lambda t: abs(centroids[t] - r["rpl"]))
            r2 = r.copy()
            r2[headline_model] = assigned
            rows.append(_master_row(
                r2, "below", network_economics, state_to_campaign,
                state_to_campaign_tcpa, headline_model, tier_target_tcpa,
                target_roas, pattern_threshold, click_threshold,
                capture_low, capture_high,
            ))

    df = pd.DataFrame(rows)
    # Sort: by tier (TIER ONE first), then by RPL descending within tier
    tier_rank = {t: i for i, t in enumerate(centroids.index)}
    tier_rank["UNCLASSIFIED"] = 999
    df["_sort"] = df["tier"].map(tier_rank).fillna(998)
    df = df.sort_values(["_sort", "rpl"], ascending=[True, False]).drop(columns=["_sort"])
    return df.reset_index(drop=True)


def _master_row(r, source: str, network_economics: pd.DataFrame,
                state_to_campaign: dict, state_to_campaign_tcpa: dict,
                headline_model: str, tier_target_tcpa: dict, target_roas: float,
                pattern_threshold: float, click_threshold: int,
                capture_low: float, capture_high: float) -> dict:
    """Helper: build one row for the Master Tier Review."""
    state = r["state"]
    tier = r.get(headline_model, "UNCLASSIFIED")
    rpl_12w = r.get("rpl", float("nan"))
    clicks = r.get("clicks", 0)
    spend = r.get("spend", 0.0)
    conversions = r.get("conversions", 0.0)

    # Network economics (may be missing for some states)
    ne = network_economics[network_economics["state"] == state]
    if not ne.empty:
        ne_row = ne.iloc[0].to_dict()
    else:
        ne_row = {}

    rpl_search = ne_row.get("rpl_search", float("nan"))
    rpl_audience = ne_row.get("rpl_audience", float("nan"))
    cpl_search = ne_row.get("cpl_search", float("nan"))
    cpl_audience = ne_row.get("cpl_audience", float("nan"))
    clicks_search = ne_row.get("clicks_search", 0) or 0
    clicks_audience = ne_row.get("clicks_audience", 0) or 0
    ideal_tcpa_search = ne_row.get("ideal_tcpa_search", float("nan"))
    ideal_tcpa_audience = ne_row.get("ideal_tcpa_audience", float("nan"))

    current_campaign = state_to_campaign.get(state, "NONE_ACTIVE")
    current_tcpa = state_to_campaign_tcpa.get(state)

    pattern_row = {"rpl_search": rpl_search, "rpl_audience": rpl_audience,
                   "clicks_search": clicks_search, "clicks_audience": clicks_audience}
    pattern = label_pattern(pattern_row, pattern_threshold, click_threshold)

    search_flag = label_fitness(rpl_search, clicks_search, current_tcpa,
                                 target_roas, click_threshold=click_threshold)
    audience_flag = label_fitness(rpl_audience, clicks_audience, current_tcpa,
                                   target_roas, click_threshold=click_threshold)

    new_tcpa = tier_target_tcpa.get(tier) if tier != "UNCLASSIFIED" else None
    if new_tcpa is None or pd.isna(new_tcpa) or current_tcpa is None or pd.isna(current_tcpa):
        change_direction = "no_change"
    elif new_tcpa > current_tcpa * 1.05:
        change_direction = "Raising targets"
    elif new_tcpa < current_tcpa * 0.95:
        change_direction = "Lowering targets"
    else:
        change_direction = "Holding (within 5%)"
    # Blended-network ideal tCPA (audience-justification view)
    blended_rpl_full = float("nan")
    blended_ideal_tcpa = float("nan")
    spend_search = ne_row.get("spend_search", 0) or 0
    spend_audience = ne_row.get("spend_audience", 0) or 0
    total_net_spend = spend_search + spend_audience
    if total_net_spend > 0 and pd.notna(rpl_search) and pd.notna(rpl_audience):
        blended_rpl_full = (rpl_search * spend_search + rpl_audience * spend_audience) / total_net_spend
        blended_ideal_tcpa = blended_rpl_full / target_roas

    # GP upside estimate (monthly; will be aggregated to weekly later)
    # gp_upside_max = max(0, ideal_tcpa - current_tcpa) * monthly_conversions
    # Note: conversions here are 12w totals; scale to monthly
    monthly_conversions = conversions / (12.0 / 4.33) if conversions else 0
    if (pd.notna(rpl_12w) and current_tcpa is not None
            and pd.notna(current_tcpa) and rpl_12w > 0):
        ideal_tcpa_state = rpl_12w / target_roas
        gp_upside_max = max(0, ideal_tcpa_state - current_tcpa) * monthly_conversions
    else:
        gp_upside_max = 0.0
    gp_upside_conservative = gp_upside_max * capture_low
    gp_upside_realistic = gp_upside_max * (capture_low + capture_high) / 2.0
    gp_upside_optimistic = gp_upside_max * capture_high

    # Per-state ideal tCPA (the state's own economics, ignoring tier averaging)
    # and gap analysis: when ideal differs meaningfully from tier target,
    # the state is at a tier edge and should be bid at its own ideal_tcpa
    # rather than the tier average to avoid systematic over- or under-bidding.
    BOUNDARY_GAP_THRESHOLD = 0.10
    ideal_tcpa_state_val = float("nan")
    gap_to_tier_target_pct = float("nan")
    recommended_tcpa = new_tcpa  # default: use tier target
    use_individual_override = False
    if pd.notna(rpl_12w) and rpl_12w > 0:
        ideal_tcpa_state_val = rpl_12w / target_roas
        if new_tcpa is not None and pd.notna(new_tcpa) and new_tcpa > 0:
            gap_to_tier_target_pct = abs(ideal_tcpa_state_val - new_tcpa) / new_tcpa
            if gap_to_tier_target_pct >= BOUNDARY_GAP_THRESHOLD and source == "above":
                recommended_tcpa = ideal_tcpa_state_val
                use_individual_override = True

    # Effectively-holding override: when the dollar impact is trivial (<$25/mo
    # of realistic GP upside) and there's no overbid to correct, label as
    # "effectively_holding" so stakeholders don't conflate trivial tier-target
    # adjustments with meaningful bid changes.
    EFFECTIVELY_HOLDING_THRESHOLD = 25.0
    if (source == "above"
            and change_direction in ("Raising targets", "Lowering targets")
            and gp_upside_realistic < EFFECTIVELY_HOLDING_THRESHOLD):
        # Only override raising-direction states; overbid corrections (Lowering)
        # don't currently surface in gp_upside_max so we leave them alone.
        if change_direction == "Raising targets":
            change_direction = "effectively_holding"

    threshold_states = "YES" if (source == "above") else "NO"
    below_threshold = (source == "below")

    # Decision rationale
    rationale = _decision_rationale(
        state, tier, pattern, change_direction, source,
        rpl_search, rpl_audience, current_tcpa, new_tcpa,
        r.get("confidence", "HIGH"),
    )
    if use_individual_override:
        rationale += (
            f" BOUNDARY OVERRIDE: state's individual ideal tCPA "
            f"(${ideal_tcpa_state_val:.2f}) is {gap_to_tier_target_pct:.0%} away "
            f"from tier target (${new_tcpa:.2f}). Recommend bidding individual "
            f"ideal_tcpa to avoid {'over' if ideal_tcpa_state_val < new_tcpa else 'under'}-bidding."
        )

    return {
        "tier": tier,
        "pattern": pattern,
        "state": state,
        "below_threshold": below_threshold,
        "confidence": r.get("confidence", "HIGH"),
        "search_flag": search_flag,
        "audience_flag": audience_flag,
        "rpl": rpl_12w,
        "rpl_search": rpl_search,
        "rpl_audience": rpl_audience,
        "cpl_search": cpl_search,
        "cpl_audience": cpl_audience,
        "ideal_tcpa_search": ideal_tcpa_search,
        "ideal_tcpa_audience": ideal_tcpa_audience,
        "blended_rpl_full_network": blended_rpl_full,
        "blended_ideal_tcpa": blended_ideal_tcpa,
        "tier_target_tcpa": new_tcpa,
        "ideal_tcpa_individual": ideal_tcpa_state_val,
        "gap_to_tier_target_pct": gap_to_tier_target_pct,
        "recommended_tcpa": recommended_tcpa,
        "use_individual_override": use_individual_override,
        "current_tcpa": current_tcpa,
        "change_direction": change_direction,
        "current_campaign": current_campaign,
        "gp_upside_max_monthly": gp_upside_max,
        "gp_upside_conservative_monthly": gp_upside_conservative,
        "gp_upside_realistic_monthly": gp_upside_realistic,
        "gp_upside_optimistic_monthly": gp_upside_optimistic,
        "decision_rationale": rationale,
        "threshold_states": threshold_states,
    }


def _decision_rationale(state, tier, pattern, change_direction, source,
                         rpl_search, rpl_audience, current_tcpa, new_tcpa,
                         confidence) -> str:
    """Plain-English one-sentence rationale per state."""
    if source == "below":
        if confidence == "UNCLASSIFIED":
            return f"Insufficient data ({confidence}); excluded from tier-driven recommendations."
        return (f"Below-threshold ({confidence} confidence) — assigned to {tier} by RPL proximity. "
                "Monitor for volume growth before committing to bid changes.")

    if change_direction == "no_change":
        return f"Currently in {tier}; no campaign tCPA on file."

    if change_direction == "effectively_holding":
        return (f"Trivial change: target moves from ${current_tcpa:.2f} to ${new_tcpa:.2f} but "
                f"state's individual economics already align with current bid (GP upside <$25/mo). "
                f"Tier reassignment for grouping consistency; no material bid action needed.")

    direction_words = {
        "Raising targets": "raise",
        "Lowering targets": "lower",
        "Holding (within 5%)": "hold",
    }
    verb = direction_words.get(change_direction, "adjust")

    base = (f"{change_direction.split()[0]} tCPA from ${current_tcpa:.2f} to ${new_tcpa:.2f} "
            f"(per {tier} target).")

    if pattern == "A":
        return (f"{base} Pattern A: search RPL (${rpl_search:.0f}) exceeds audience RPL "
                f"(${rpl_audience:.0f}); monitor audience spend after change.")
    if pattern == "B":
        return (f"{base} Pattern B: audience RPL (${rpl_audience:.0f}) exceeds search "
                f"(${rpl_search:.0f}); cross-network economics support the change.")
    return f"{base} Network RPLs within threshold; standard tier alignment."


# ============================================================================
# State Economics vs Cluster comparison (fixed-centroid diagnostic)
# ============================================================================

def load_or_create_baseline_centroids(
    baseline_path: Path,
    master_tier_review_df: pd.DataFrame | None = None,
    baseline_date: str | None = None,
) -> dict:
    """Load fixed baseline centroids from YAML. If the file doesn't exist and
    a Master Tier Review is provided, generate the baseline from it and save.

    Returns a dict of {tier_name: centroid_tcpa}.
    """
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML required. Run: pip install pyyaml")

    baseline_path = Path(baseline_path)

    if baseline_path.exists():
        with open(baseline_path) as f:
            data = yaml.safe_load(f) or {}
        root = data.get("baseline_centroids", data)
        return {t: float(c) for t, c in root.get("centroids", {}).items()}

    if master_tier_review_df is None:
        return {}

    # Generate from current workbook — use spend-weighted tier targets as centroids
    above = master_tier_review_df[master_tier_review_df["below_threshold"] == False]
    centroids = (
        above.groupby("tier")["tier_target_tcpa"].first().to_dict()
    )
    # Filter out any NaN
    centroids = {t: float(c) for t, c in centroids.items() if pd.notna(c)}

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    with open(baseline_path, "w") as f:
        yaml.dump({
            "baseline_centroids": {
                "baseline_date": baseline_date or "unknown",
                "note": (
                    "Tier centroids (spend-weighted ideal_tcpa per tier) frozen "
                    "at baseline date. Used as fixed reference points in the "
                    "'State Economics vs Cluster' diagnostic sheet. To re-anchor, "
                    "delete this file and rerun."
                ),
                "centroids": centroids,
            }
        }, f, default_flow_style=False, sort_keys=False)
    return centroids


def build_state_economics_vs_cluster(
    master_tier_review_df: pd.DataFrame,
    baseline_centroids: dict,
) -> pd.DataFrame:
    """For each above-threshold state, compare its current C2 cluster
    assignment to what a fixed-centroid assignment (using baseline centroids)
    would say. States where the two disagree are flagged as candidates
    where the current 'VOLATILE' label may be a cluster artifact rather
    than real economic movement.
    """
    if not baseline_centroids:
        return pd.DataFrame()

    above = master_tier_review_df[master_tier_review_df["below_threshold"] == False].copy()
    if above.empty:
        return pd.DataFrame()

    # Build the centroid list once
    centroid_items = sorted(baseline_centroids.items(), key=lambda x: x[1], reverse=True)

    def _nearest_fixed_tier(ideal_tcpa: float) -> str:
        if pd.isna(ideal_tcpa):
            return None
        return min(centroid_items, key=lambda tc: abs(tc[1] - ideal_tcpa))[0]

    rows = []
    for _, r in above.iterrows():
        state = r["state"]
        ideal = r.get("ideal_tcpa_individual", float("nan"))
        if pd.isna(ideal):
            # Fallback to RPL/1.5 if not present
            rpl = r.get("rpl", float("nan"))
            ideal = (rpl / 1.5) if pd.notna(rpl) else float("nan")
        cluster_tier = r["tier"]
        fixed_tier = _nearest_fixed_tier(ideal)
        agree = (cluster_tier == fixed_tier) if fixed_tier else False

        # Distance from ideal to its assigned fixed centroid and to the nearest alternative
        if fixed_tier and pd.notna(ideal):
            distance_to_fixed = abs(ideal - baseline_centroids[fixed_tier])
            other_distances = [
                abs(ideal - c) for t, c in baseline_centroids.items() if t != fixed_tier
            ]
            distance_to_2nd_nearest = min(other_distances) if other_distances else None
            # How close is the state to flipping? Lower = more boundary-noisy
            boundary_proximity = (
                distance_to_fixed / distance_to_2nd_nearest
                if distance_to_2nd_nearest and distance_to_2nd_nearest > 0 else None
            )
        else:
            distance_to_fixed = None
            boundary_proximity = None

        if not agree:
            interpretation = (
                f"Cluster says {cluster_tier}; fixed centroids say {fixed_tier}. "
                "Disagreement suggests cluster boundary movement is influencing assignment."
            )
        elif boundary_proximity and boundary_proximity > 0.75:
            interpretation = (
                f"Cluster and fixed-centroid agree on {cluster_tier}, but state sits "
                f"close to the next-tier boundary. Watch for borderline reassignment."
            )
        else:
            interpretation = f"Cluster and fixed-centroid agree on {cluster_tier}. State fits its tier cleanly."

        rows.append({
            "state": state,
            "rpl": r.get("rpl"),
            "ideal_tcpa": ideal,
            "cluster_tier": cluster_tier,
            "fixed_centroid_tier": fixed_tier,
            "agree": agree,
            "boundary_proximity_ratio": boundary_proximity,
            "interpretation": interpretation,
        })

    df = pd.DataFrame(rows)
    # Sort: disagreements first (most actionable), then by boundary proximity
    df["_sort_agree"] = df["agree"].astype(int)
    df = df.sort_values(
        ["_sort_agree", "boundary_proximity_ratio"],
        ascending=[True, False],
        na_position="last",
    ).drop(columns=["_sort_agree"]).reset_index(drop=True)
    return df
