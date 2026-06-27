"""
Microsoft Geo Model Comparison — Home Insurance, Search Network Only

Compares four state-tiering models side-by-side with a stability layer that
re-runs each model on 12w / 8w / 4w windows.

Models:
  A2 — Quantile bins on RPL
  B1 — K-means on (RPL, CPC), z-scored
  B4 — Hierarchical Ward on (RPL, CPC, log_clicks), z-scored
  C2 — Weighted k-means on ideal_tcpa, weights = spend

Tier labels are normalized so TIER ONE = highest mean RPL across every model.

Comparison vs current campaign structure uses Rand Index, Adjusted Rand Index,
and pairwise split/merge counts.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DEFAULT_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\microsoft-geo-analysis\output"

ACCOUNT_ID = 2
LOCATION_TYPE = "PHYSICAL"
DEVICE = "ALL"
CLICK_THRESHOLD = 200
TARGET_ROAS = 1.50
DEFAULT_K = 5

SEARCH_NETWORKS: set[str] = {
    "Microsoft sites and select traffic",
    "AOL search",
    "Syndicated search partners",
}

AUDIENCE_NETWORKS: set[str] = {
    "Audience",
}

TIER_LABEL_BASE = [
    "TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR", "TIER FIVE",
    "TIER SIX", "TIER SEVEN", "TIER EIGHT", "TIER NINE", "TIER TEN",
    "TIER ELEVEN", "TIER TWELVE",
]

MODEL_NAMES = [
    "A2_rpl_quantile",
    "B1_kmeans_rpl_cpc",
    "B4_hierarchical",
    "C2_weighted_kmeans",
]


# ----------------------------------------------------------------------------
# Date windows
# ----------------------------------------------------------------------------

@dataclass
class Windows:
    end_date: date
    w12_start: date
    w8_start: date
    w4_start: date


def resolve_end_date(conn: sqlite3.Connection, override: str | None) -> date:
    if override:
        return date.fromisoformat(override)
    row = conn.execute(
        """SELECT MAX(date) FROM geo_performance_daily
           WHERE account_id = ? AND location_type = ?""",
        (ACCOUNT_ID, LOCATION_TYPE),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError("No rows in geo_performance_daily")
    max_date = date.fromisoformat(row[0])
    yesterday = date.today() - timedelta(days=1)
    return yesterday if max_date == yesterday else max_date


def build_windows(end_date: date) -> Windows:
    return Windows(
        end_date=end_date,
        w12_start=end_date - timedelta(days=83),
        w8_start=end_date - timedelta(days=55),
        w4_start=end_date - timedelta(days=27),
    )


# ----------------------------------------------------------------------------
# Data pull
# ----------------------------------------------------------------------------

def pull_enabled_campaign_ids(conn: sqlite3.Connection) -> list[str]:
    """Just the IDs of ENABLED campaigns for this account."""
    df = pd.read_sql_query(
        """SELECT campaign_id FROM campaigns
           WHERE account_id = ? AND status = 'ENABLED'""",
        conn, params=(ACCOUNT_ID,),
    )
    return df["campaign_id"].tolist()


def pull_enabled_campaigns_with_tcpa(conn: sqlite3.Connection) -> pd.DataFrame:
    """ENABLED campaigns with tCPA in dollars. Used for GP upside vs current tCPA."""
    df = pd.read_sql_query(
        """SELECT campaign_id, campaign_name, bidding_strategy_type,
                  target_cpa_micros
           FROM campaigns
           WHERE account_id = ? AND status = 'ENABLED'""",
        conn, params=(ACCOUNT_ID,),
    )
    df["current_tcpa"] = df["target_cpa_micros"].apply(
        lambda x: x / 1e6 if pd.notna(x) and x > 0 else None
    )
    return df


def pull_search_state_aggregates(
    conn: sqlite3.Connection, start: date, end: date,
    enabled_campaign_ids: list[str],
) -> pd.DataFrame:
    """Per-state SEARCH-network-only aggregates for ENABLED campaigns."""
    if not enabled_campaign_ids:
        return pd.DataFrame()
    cp = ",".join("?" for _ in enabled_campaign_ids)
    np_ = ",".join("?" for _ in SEARCH_NETWORKS)
    sql = f"""
        SELECT
            geo_state AS state,
            SUM(clicks) AS clicks,
            SUM(impressions) AS impressions,
            SUM(cost_micros) / 1e6 AS spend,
            SUM(conversions) AS conversions,
            SUM(conversion_value) AS revenue
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ? AND device = ?
          AND date BETWEEN ? AND ?
          AND campaign_id IN ({cp})
          AND network IN ({np_})
        GROUP BY geo_state
    """
    params = [
        ACCOUNT_ID, LOCATION_TYPE, DEVICE,
        start.isoformat(), end.isoformat(),
        *enabled_campaign_ids,
        *sorted(SEARCH_NETWORKS),
    ]
    return pd.read_sql_query(sql, conn, params=params)


def pull_state_aggregates_by_network(
    conn: sqlite3.Connection, start: date, end: date,
    enabled_campaign_ids: list[str], networks: set[str],
) -> pd.DataFrame:
    """Per-state aggregates for ENABLED campaigns on the given network subset."""
    if not enabled_campaign_ids or not networks:
        return pd.DataFrame()
    cp = ",".join("?" for _ in enabled_campaign_ids)
    np_ = ",".join("?" for _ in networks)
    sql = f"""
        SELECT
            geo_state AS state,
            SUM(clicks) AS clicks,
            SUM(impressions) AS impressions,
            SUM(cost_micros) / 1e6 AS spend,
            SUM(conversions) AS conversions,
            SUM(conversion_value) AS revenue
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ? AND device = ?
          AND date BETWEEN ? AND ?
          AND campaign_id IN ({cp})
          AND network IN ({np_})
        GROUP BY geo_state
    """
    params = [
        ACCOUNT_ID, LOCATION_TYPE, DEVICE,
        start.isoformat(), end.isoformat(),
        *enabled_campaign_ids,
        *sorted(networks),
    ]
    return pd.read_sql_query(sql, conn, params=params)


def pull_state_to_campaign_map(
    conn: sqlite3.Connection, start: date, end: date,
    enabled_campaign_ids: list[str],
) -> dict[str, str]:
    """Per-state primary ENABLED campaign by SEARCH-network spend share."""
    if not enabled_campaign_ids:
        return {}
    cp = ",".join("?" for _ in enabled_campaign_ids)
    np_ = ",".join("?" for _ in SEARCH_NETWORKS)
    sql = f"""
        SELECT geo_state AS state, campaign_id,
               SUM(cost_micros) / 1e6 AS spend
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ? AND device = ?
          AND date BETWEEN ? AND ?
          AND campaign_id IN ({cp})
          AND network IN ({np_})
        GROUP BY geo_state, campaign_id
    """
    params = [
        ACCOUNT_ID, LOCATION_TYPE, DEVICE,
        start.isoformat(), end.isoformat(),
        *enabled_campaign_ids,
        *sorted(SEARCH_NETWORKS),
    ]
    df = pd.read_sql_query(sql, conn, params=params)
    df = df[df["spend"] > 0]
    primary = (
        df.sort_values(["state", "spend"], ascending=[True, False])
        .groupby("state").head(1)
    )
    return dict(zip(primary["state"], primary["campaign_id"]))


def compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["cpc"] = df["spend"] / df["clicks"].where(df["clicks"] > 0)
    df["cpa"] = df["spend"] / df["conversions"].where(df["conversions"] > 0)
    df["roas"] = df["revenue"] / df["spend"].where(df["spend"] > 0)
    df["rpl"] = df["revenue"] / df["conversions"].where(df["conversions"] > 0)
    df["ideal_tcpa"] = df["rpl"] / TARGET_ROAS
    return df


def apply_rpl_blend(
    df_12w: pd.DataFrame,
    df_4w: pd.DataFrame | None,
    blend_weight: float,
    min_blend_clicks: int = 75,
) -> pd.DataFrame:
    """Replace 12w RPL with a weighted blend of 12w and 4w RPL.

    blend_weight = 0.0 → pure 12w (default, no change)
    blend_weight = 0.3 → 70% × rpl_12w + 30% × rpl_4w
    blend_weight = 1.0 → pure 4w

    For states with insufficient 4w volume (< min_blend_clicks), the 12w value
    is retained unchanged — these states couldn't have a reliable 4w blend.
    Adds two diagnostic columns:
      rpl_12w_original — the unblended 12w RPL (always preserved)
      blend_applied    — True if the state's RPL was blended; False if 12w-only
    """
    out = df_12w.copy()
    out["rpl_12w_original"] = out["rpl"]
    out["blend_applied"] = False

    if blend_weight <= 0.0 or df_4w is None or df_4w.empty:
        return out

    blend_weight = min(max(blend_weight, 0.0), 1.0)
    # Map state -> rpl from the 4w aggregate
    df_4w_indexed = df_4w.set_index("state")

    def _blend(row):
        state = row["state"]
        rpl_12w = row["rpl"]
        if state not in df_4w_indexed.index:
            return rpl_12w, False
        r4 = df_4w_indexed.loc[state]
        c4 = r4["clicks"]
        rpl_4w = r4["rpl"]
        if pd.isna(rpl_4w) or rpl_4w <= 0 or c4 < min_blend_clicks:
            return rpl_12w, False
        blended = (1.0 - blend_weight) * rpl_12w + blend_weight * rpl_4w
        return blended, True

    blended_results = out.apply(_blend, axis=1)
    out["rpl"] = [b[0] for b in blended_results]
    out["blend_applied"] = [b[1] for b in blended_results]
    # Re-derive ideal_tcpa from the blended RPL
    out["ideal_tcpa"] = out["rpl"] / TARGET_ROAS
    return out


def eligible_pool(df: pd.DataFrame, click_threshold: int = CLICK_THRESHOLD) -> pd.DataFrame:
    """Eligible = >=click_threshold search clicks, has finite RPL and CPC, not UNKNOWN."""
    return df[
        (df["clicks"] >= click_threshold)
        & df["rpl"].notna()
        & df["cpc"].notna()
        & (df["rpl"] > 0)
        & (df["cpc"] > 0)
        & (df["state"].str.upper() != "UNKNOWN")
    ].copy().reset_index(drop=True)


# ----------------------------------------------------------------------------
# Tier-label normalization
# ----------------------------------------------------------------------------

def relabel_by_rpl(states: pd.DataFrame, cluster_col: str, k: int) -> pd.Series:
    """Map raw cluster IDs to TIER labels ordered by mean RPL descending.
    TIER ONE = highest mean RPL cluster.
    """
    means = states.groupby(cluster_col)["rpl"].mean().sort_values(ascending=False)
    rank_map = {old: TIER_LABEL_BASE[i] for i, old in enumerate(means.index[:k])}
    return states[cluster_col].map(rank_map).astype(object)


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------

def model_a2_rpl_quantile(states: pd.DataFrame, k: int) -> pd.Series:
    """Equal-count bins on RPL. Top bin = TIER ONE."""
    ranks = states["rpl"].rank(method="first", ascending=False)
    bins = pd.cut(ranks, bins=k, labels=False, include_lowest=True)
    out = states.copy()
    out["_cluster"] = bins
    return relabel_by_rpl(out, "_cluster", k)


def model_b1_kmeans_rpl_cpc(states: pd.DataFrame, k: int, seed: int = 42) -> pd.Series:
    """K-means on z-scored (RPL, CPC)."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    features = states[["rpl", "cpc"]].values
    X = StandardScaler().fit_transform(features)
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(X)
    out = states.copy()
    out["_cluster"] = km.labels_
    return relabel_by_rpl(out, "_cluster", k)


def model_b4_hierarchical(states: pd.DataFrame, k: int):
    """Hierarchical Ward clustering on z-scored (RPL, CPC, log_clicks).
    Returns: (labels series, linkage matrix Z for dendrogram export).
    """
    from scipy.cluster.hierarchy import linkage, fcluster
    from sklearn.preprocessing import StandardScaler
    feat = pd.DataFrame({
        "rpl": states["rpl"].values,
        "cpc": states["cpc"].values,
        "log_clicks": np.log(states["clicks"].values),
    })
    X = StandardScaler().fit_transform(feat.values)
    Z = linkage(X, method="ward")
    labels = fcluster(Z, t=k, criterion="maxclust")
    out = states.copy()
    out["_cluster"] = labels
    return relabel_by_rpl(out, "_cluster", k), Z


def model_c2_weighted_kmeans(states: pd.DataFrame, k: int, seed: int = 42) -> pd.Series:
    """1D k-means on ideal_tcpa with sample_weight = spend.
    Mathematically minimizes Σ((tcpa_i − tier_tcpa)² × spend_i).
    """
    from sklearn.cluster import KMeans
    X = states[["ideal_tcpa"]].values
    w = states["spend"].values
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(X, sample_weight=w)
    out = states.copy()
    out["_cluster"] = km.labels_
    return relabel_by_rpl(out, "_cluster", k)


def apply_all_models(states: pd.DataFrame, k: int) -> tuple[pd.DataFrame, dict]:
    """Apply all four models. Returns (states_df_with_tier_cols, artifacts).
    artifacts contains b4_linkage and b4_state_order for dendrogram export.
    """
    out = states.copy().reset_index(drop=True)
    out["A2_rpl_quantile"] = model_a2_rpl_quantile(out, k)
    out["B1_kmeans_rpl_cpc"] = model_b1_kmeans_rpl_cpc(out, k)
    b4_labels, Z = model_b4_hierarchical(out, k)
    out["B4_hierarchical"] = b4_labels
    out["C2_weighted_kmeans"] = model_c2_weighted_kmeans(out, k)
    return out, {"b4_linkage": Z, "b4_state_order": out["state"].tolist()}


# ----------------------------------------------------------------------------
# Within-tier homogeneity metrics (per model)
# ----------------------------------------------------------------------------

def tier_aggregate_metrics(states: pd.DataFrame, tier_col: str) -> pd.DataFrame:
    """For one model, compute per-tier aggregates."""
    rows = []
    for tier, grp in states.groupby(tier_col, observed=True):
        if pd.isna(tier) or tier == "":
            continue
        weighted_rpl = (
            (grp["rpl"] * grp["spend"]).sum() / grp["spend"].sum()
            if grp["spend"].sum() > 0 else np.nan
        )
        rpl_min = grp["rpl"].min()
        rpl_max = grp["rpl"].max()
        rpl_spread = rpl_max / rpl_min if rpl_min > 0 else np.nan
        tier_tcpa = weighted_rpl / TARGET_ROAS if pd.notna(weighted_rpl) else np.nan
        tcpa_stdev = grp["ideal_tcpa"].std(ddof=0)
        rows.append({
            "tier": tier,
            "n_states": len(grp),
            "states": ", ".join(sorted(grp["state"].tolist())),
            "tier_spend": grp["spend"].sum(),
            "tier_weighted_rpl": weighted_rpl,
            "tier_target_tcpa": tier_tcpa,
            "rpl_min": rpl_min,
            "rpl_max": rpl_max,
            "rpl_spread_ratio": rpl_spread,
            "ideal_tcpa_stdev": tcpa_stdev,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Sort by mean RPL descending so TIER ONE is first
    tier_order = {t: i for i, t in enumerate(TIER_LABEL_BASE)}
    df["_order"] = df["tier"].map(tier_order)
    df = df.sort_values("_order").drop(columns=["_order"])
    return df


def model_summary_metrics(states: pd.DataFrame, tier_col: str) -> dict:
    """One row of comparison metrics for a single model."""
    tier_agg = tier_aggregate_metrics(states, tier_col)
    if tier_agg.empty:
        return {}
    # Spend-weighted bid loss = sum over states of (state_ideal_tcpa - tier_tcpa)^2 * spend
    tier_tcpa_map = dict(zip(tier_agg["tier"], tier_agg["tier_target_tcpa"]))
    s = states.copy()
    s["_tier_tcpa"] = s[tier_col].map(tier_tcpa_map)
    s["_loss"] = (s["ideal_tcpa"] - s["_tier_tcpa"]) ** 2 * s["spend"]
    bid_loss = s["_loss"].sum()
    # Avg RPL spread ratio across tiers (size-weighted)
    valid = tier_agg.dropna(subset=["rpl_spread_ratio"])
    if not valid.empty:
        avg_spread = (
            (valid["rpl_spread_ratio"] * valid["n_states"]).sum()
            / valid["n_states"].sum()
        )
    else:
        avg_spread = np.nan
    # Mean within-tier ideal_tcpa stdev (size-weighted)
    valid_std = tier_agg.dropna(subset=["ideal_tcpa_stdev"])
    if not valid_std.empty:
        avg_stdev = (
            (valid_std["ideal_tcpa_stdev"] * valid_std["n_states"]).sum()
            / valid_std["n_states"].sum()
        )
    else:
        avg_stdev = np.nan
    return {
        "model": tier_col,
        "n_tiers_populated": len(tier_agg),
        "avg_within_tier_rpl_spread": avg_spread,
        "avg_within_tier_tcpa_stdev": avg_stdev,
        "spend_weighted_bid_loss": bid_loss,
    }


# ----------------------------------------------------------------------------
# Comparison vs current campaign structure
# ----------------------------------------------------------------------------

def _pairwise_agreement_counts(labels_a: pd.Series, labels_b: pd.Series) -> dict:
    """Pairwise comparison of two labelings on the same items.
    Returns: a=same in A & same in B, b=same in A & diff in B,
             c=diff in A & same in B, d=diff in A & diff in B.
    Rand Index = (a + d) / total pairs.
    """
    n = len(labels_a)
    if n < 2:
        return {"a": 0, "b": 0, "c": 0, "d": 0, "total": 0}
    a = b = c = d = 0
    la = labels_a.values
    lb = labels_b.values
    for i in range(n):
        for j in range(i + 1, n):
            sa = la[i] == la[j]
            sb = lb[i] == lb[j]
            if sa and sb:    a += 1
            elif sa and not sb: b += 1
            elif (not sa) and sb: c += 1
            else: d += 1
    return {"a": a, "b": b, "c": c, "d": d, "total": a + b + c + d}


def compare_to_campaigns(
    states_with_tiers: pd.DataFrame,
    state_to_campaign: dict[str, str],
    model_col: str,
) -> dict:
    """Compare a model's tier assignment to the current campaign grouping.

    Returns dict with:
      - rand_index
      - adjusted_rand_index
      - n_splits  (pairs same-campaign, diff-tier — model would split them)
      - n_merges  (pairs diff-campaign, same-tier — model would merge them)
      - n_compared (# states with both a campaign and a tier)
    """
    from sklearn.metrics import adjusted_rand_score
    s = states_with_tiers.copy()
    s["_campaign"] = s["state"].map(state_to_campaign)
    s = s[s["_campaign"].notna() & s[model_col].notna()].reset_index(drop=True)
    if len(s) < 2:
        return {
            "rand_index": np.nan, "adjusted_rand_index": np.nan,
            "n_splits": 0, "n_merges": 0, "n_compared": len(s),
        }
    counts = _pairwise_agreement_counts(s["_campaign"], s[model_col])
    rand = (counts["a"] + counts["d"]) / counts["total"] if counts["total"] > 0 else np.nan
    ari = adjusted_rand_score(s["_campaign"].astype(str), s[model_col].astype(str))
    return {
        "rand_index": rand,
        "adjusted_rand_index": ari,
        "n_splits": counts["b"],     # same campaign, diff tier
        "n_merges": counts["c"],     # diff campaign, same tier
        "n_compared": len(s),
    }


def enumerate_splits_and_merges(
    states_with_tiers: pd.DataFrame,
    state_to_campaign: dict[str, str],
    model_col: str,
) -> tuple[list[tuple], list[tuple]]:
    """Return (splits, merges) as lists of (state_a, state_b, campaign_or_tier) tuples."""
    s = states_with_tiers.copy()
    s["_campaign"] = s["state"].map(state_to_campaign)
    s = s[s["_campaign"].notna() & s[model_col].notna()].reset_index(drop=True)
    splits, merges = [], []
    n = len(s)
    for i in range(n):
        for j in range(i + 1, n):
            same_camp = s.loc[i, "_campaign"] == s.loc[j, "_campaign"]
            same_tier = s.loc[i, model_col] == s.loc[j, model_col]
            if same_camp and not same_tier:
                splits.append((s.loc[i, "state"], s.loc[j, "state"], s.loc[i, "_campaign"]))
            elif (not same_camp) and same_tier:
                merges.append((s.loc[i, "state"], s.loc[j, "state"], s.loc[i, model_col]))
    return splits, merges


# ----------------------------------------------------------------------------
# Stability layer (D1)
# ----------------------------------------------------------------------------

def run_models_for_window(
    conn: sqlite3.Connection,
    start: date, end: date,
    enabled_campaign_ids: list[str],
    k: int,
    click_threshold: int = CLICK_THRESHOLD,
) -> pd.DataFrame | None:
    """Pull a window, build eligible pool, apply all models.
    Returns the tier-assigned eligible pool, or None if no data.
    """
    agg = pull_search_state_aggregates(conn, start, end, enabled_campaign_ids)
    if agg.empty:
        return None
    agg = compute_derived(agg)
    elig = eligible_pool(agg, click_threshold=click_threshold)
    if len(elig) < k:
        # Not enough states to form k clusters
        return None
    result, _ = apply_all_models(elig, k)
    return result


def build_stability_table(
    result_12w: pd.DataFrame,
    result_8w: pd.DataFrame | None,
    result_4w: pd.DataFrame | None,
) -> pd.DataFrame:
    """For each model, produce a long-format table:
       state, model, tier_12w, tier_8w, tier_4w, stability_score, direction
    Joins on state across the three window snapshots.
    """
    rows = []
    states = sorted(set(result_12w["state"].tolist()))
    if result_8w is not None:
        states = sorted(set(states) | set(result_8w["state"].tolist()))
    if result_4w is not None:
        states = sorted(set(states) | set(result_4w["state"].tolist()))

    # Index helpers
    idx_12w = result_12w.set_index("state") if result_12w is not None else None
    idx_8w = result_8w.set_index("state") if result_8w is not None else None
    idx_4w = result_4w.set_index("state") if result_4w is not None else None

    def tier_rank(tier: str | None) -> int | None:
        if tier is None or (isinstance(tier, float) and np.isnan(tier)):
            return None
        try:
            return TIER_LABEL_BASE.index(tier)
        except ValueError:
            return None

    for state in states:
        for model in MODEL_NAMES:
            t12 = idx_12w.loc[state, model] if (idx_12w is not None and state in idx_12w.index) else None
            t8 = idx_8w.loc[state, model] if (idx_8w is not None and state in idx_8w.index) else None
            t4 = idx_4w.loc[state, model] if (idx_4w is not None and state in idx_4w.index) else None

            tiers_present = [t for t in (t12, t8, t4) if t is not None and not (isinstance(t, float) and pd.isna(t))]
            n_unique = len(set(tiers_present)) if tiers_present else 0

            # Direction: stable / drifting_up / drifting_down / volatile / partial
            ranks = [tier_rank(t) for t in (t12, t8, t4)]
            ranks_valid = [r for r in ranks if r is not None]
            if not ranks_valid:
                direction = "no_data"
            elif n_unique == 1:
                direction = "stable"
            elif len(ranks_valid) < 3:
                direction = "partial"  # state in some windows but not others
            else:
                # ranks[0] = 12w (oldest), ranks[2] = 4w (newest)
                # Lower rank index = higher tier (TIER ONE = 0)
                if ranks[2] < ranks[0]:
                    direction = "drifting_up"   # better recently
                elif ranks[2] > ranks[0]:
                    direction = "drifting_down"
                else:
                    direction = "volatile"      # ping-pong (e.g., TWO/FOUR/TWO)

            rows.append({
                "state": state,
                "model": model,
                "tier_12w": t12,
                "tier_8w": t8,
                "tier_4w": t4,
                "stability_score": n_unique,
                "direction": direction,
            })
    return pd.DataFrame(rows)


def stability_rate(stability_df: pd.DataFrame, result_12w: pd.DataFrame, model: str) -> float:
    """% of total 12w spend assigned to 'stable' states (one tier across all windows)."""
    s = stability_df[stability_df["model"] == model]
    stable = set(s[s["direction"] == "stable"]["state"])
    spend_map = dict(zip(result_12w["state"], result_12w["spend"]))
    total = sum(spend_map.values())
    stable_spend = sum(spend_map.get(st, 0.0) for st in stable)
    return stable_spend / total if total > 0 else 0.0


def best_k_silhouette(states: pd.DataFrame, model: str, k_range=range(2, 9)) -> tuple[int, float]:
    """Best k by silhouette for B1 or B4. Returns (k, score)."""
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.cluster import KMeans
    from scipy.cluster.hierarchy import linkage, fcluster

    if model == "B1_kmeans_rpl_cpc":
        X = StandardScaler().fit_transform(states[["rpl", "cpc"]].values)
        best, best_s = None, -1.0
        for k in k_range:
            if k >= len(states): break
            km = KMeans(n_clusters=k, random_state=42, n_init=10).fit(X)
            try:
                s = silhouette_score(X, km.labels_)
                if s > best_s:
                    best, best_s = k, s
            except Exception:
                continue
        return (best or 0, best_s)
    elif model == "B4_hierarchical":
        feat = pd.DataFrame({
            "rpl": states["rpl"].values,
            "cpc": states["cpc"].values,
            "log_clicks": np.log(states["clicks"].values),
        })
        X = StandardScaler().fit_transform(feat.values)
        Z = linkage(X, method="ward")
        best, best_s = None, -1.0
        for k in k_range:
            if k >= len(states): break
            labels = fcluster(Z, t=k, criterion="maxclust")
            if len(set(labels)) < 2: continue
            try:
                s = silhouette_score(X, labels)
                if s > best_s:
                    best, best_s = k, s
            except Exception:
                continue
        return (best or 0, best_s)
    return (0, np.nan)


# ----------------------------------------------------------------------------
# Dendrogram export
# ----------------------------------------------------------------------------

def render_dendrogram_png(linkage_matrix, state_labels: list[str]) -> bytes:
    """Render B4 dendrogram as PNG bytes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.cluster.hierarchy import dendrogram

    fig, ax = plt.subplots(figsize=(14, 7))
    dendrogram(
        linkage_matrix, labels=state_labels, leaf_rotation=90,
        leaf_font_size=10, ax=ax, color_threshold=0,
    )
    ax.set_title("Hierarchical Ward Clustering — B4 (RPL, CPC, log_clicks)",
                 fontsize=12, fontweight="bold")
    ax.set_ylabel("Ward distance")
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ----------------------------------------------------------------------------
# Workbook writer
# ----------------------------------------------------------------------------

def _style_header_row(ws, row: int, columns: list[str]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    HEADER_FILL = PatternFill("solid", start_color="7030A0", end_color="7030A0")
    HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial")
    for i, name in enumerate(columns, start=1):
        c = ws.cell(row=row, column=i, value=name)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")


def _write_dataframe(ws, df: pd.DataFrame, header_row: int) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    BODY_FONT = Font(name="Arial")
    columns = list(df.columns)
    _style_header_row(ws, header_row, columns)
    for r_off, rec in enumerate(df.to_dict("records"), start=1):
        for c_idx, col in enumerate(columns, start=1):
            v = rec[col]
            if isinstance(v, float) and np.isnan(v):
                v = None
            elif isinstance(v, (np.integer, np.floating)):
                v = v.item()
            c = ws.cell(row=header_row + r_off, column=c_idx, value=v)
            c.font = BODY_FONT
            if col in {"spend", "rpl", "cpc", "cpa", "ideal_tcpa", "tier_spend",
                        "tier_weighted_rpl", "tier_target_tcpa", "rpl_min", "rpl_max"}:
                c.number_format = "$#,##0.00"
            elif col in {"roas", "rpl_spread_ratio", "rand_index",
                          "adjusted_rand_index", "stable_spend_rate",
                          "silhouette_score"}:
                c.number_format = "0.000"
            elif col in {"spend_weighted_bid_loss"}:
                c.number_format = "$#,##0"
            elif col in {"avg_within_tier_tcpa_stdev"}:
                c.number_format = "$#,##0.00"
            elif col in {"avg_within_tier_rpl_spread"}:
                c.number_format = "0.000"
            elif col in {"clicks", "impressions", "n_states", "n_tiers_populated",
                          "n_compared", "n_splits", "n_merges", "stability_score",
                          "best_k"}:
                c.number_format = "#,##0"
    # Column widths
    width_map = {
        "state": 22, "states": 60, "tier": 13, "model": 24,
        "campaign_id": 14, "direction": 14, "tier_12w": 13,
        "tier_8w": 13, "tier_4w": 13, "state_a": 22, "state_b": 22,
        "campaign": 14,
    }
    for c_idx, col in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(c_idx)].width = width_map.get(col, 16)


def write_workbook(
    output_path: Path,
    windows: Windows,
    k: int,
    result_12w: pd.DataFrame,
    stability_df: pd.DataFrame,
    state_to_campaign: dict[str, str],
    artifacts_12w: dict,
    # New optional inputs for v2 sheets
    state_to_campaign_tcpa: dict[str, float] | None = None,
    below_threshold_df: pd.DataFrame | None = None,
    drift_watchlist_df: pd.DataFrame | None = None,
    proposed_structures_df: pd.DataFrame | None = None,
    k_sensitivity_df: pd.DataFrame | None = None,
    snapshot_comparison_df: pd.DataFrame | None = None,
    rpl_blend: float = 0.0,
    # v3 additions
    headline_model: str = "C2_weighted_kmeans",
    master_tier_review_df: pd.DataFrame | None = None,
    method_consistency_df: pd.DataFrame | None = None,
    window_consistency_df: pd.DataFrame | None = None,
    diagnostic_df: pd.DataFrame | None = None,
    tier_boundary_df: pd.DataFrame | None = None,
    k_justification_df: pd.DataFrame | None = None,
    capture_low: float = 0.40,
    capture_high: float = 0.70,
    current_k: int = 12,
) -> None:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Font

    TITLE_FONT = Font(bold=True, size=14, name="Arial")
    SECTION_FONT = Font(bold=True, size=11, name="Arial")
    BODY_FONT = Font(name="Arial")

    wb = Workbook()
    wb.remove(wb.active)

    # Ensure output dir exists early — needed before we write the dendrogram image
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- Sheet 1: Master Tier Review (NEW — headline view) ----------
    if master_tier_review_df is not None and not master_tier_review_df.empty:
        ws = wb.create_sheet("Master Tier Review")

        # Portfolio-level GP rollup at the top
        # Filter to above-threshold for the headline rollup
        above = master_tier_review_df[master_tier_review_df["below_threshold"] == False]
        total_max_monthly = above["gp_upside_max_monthly"].sum()
        total_conservative = above["gp_upside_conservative_monthly"].sum()
        total_realistic = above["gp_upside_realistic_monthly"].sum()
        total_optimistic = above["gp_upside_optimistic_monthly"].sum()

        ws["A1"] = f"Master Tier Review — Headline Model: {headline_model}"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = (f"Window: {windows.w12_start} to {windows.end_date}  |  "
                    f"Total states reviewed: {len(master_tier_review_df)}  |  "
                    f"Above-threshold: {len(above)}  |  "
                    f"Below-threshold: {len(master_tier_review_df) - len(above)}")
        ws["A2"].font = BODY_FONT

        ws["A4"] = "PORTFOLIO GP UPSIDE FROM TIER RESTRUCTURE (monthly estimate)"
        ws["A4"].font = SECTION_FONT
        ws["A5"] = "Theoretical max (if every state instantly hit its ideal tCPA):"
        ws["B5"] = total_max_monthly
        ws["B5"].number_format = "$#,##0"
        ws["A6"] = f"Conservative estimate ({int(capture_low*100)}% capture):"
        ws["B6"] = total_conservative
        ws["B6"].number_format = "$#,##0"
        ws["A7"] = f"Realistic estimate ({int((capture_low+capture_high)/2*100)}% capture, midpoint):"
        ws["B7"] = total_realistic
        ws["B7"].number_format = "$#,##0"
        ws["A8"] = f"Optimistic estimate ({int(capture_high*100)}% capture):"
        ws["B8"] = total_optimistic
        ws["B8"].number_format = "$#,##0"
        ws["A9"] = "Weekly equivalent (realistic / 4.33):"
        ws["B9"] = total_realistic / 4.33
        ws["B9"].number_format = "$#,##0"
        ws["A10"] = "Daily equivalent (realistic / 30):"
        ws["B10"] = total_realistic / 30.0
        ws["B10"].number_format = "$#,##0"

        ws["A12"] = ("Note: capture rates account for volume response uncertainty, tCPA learning "
                     "periods, and audience-network response under campaign-level bid changes.")
        ws["A12"].font = BODY_FONT

        # Display columns for the master table — ordered for stakeholder readability
        display_cols = [
            "tier", "pattern", "state", "below_threshold", "confidence",
            "search_flag", "audience_flag",
            "rpl", "rpl_search", "rpl_audience",
            "cpl_search", "cpl_audience",
            "ideal_tcpa_search", "ideal_tcpa_audience",
            "blended_rpl_full_network", "blended_ideal_tcpa",
            "tier_target_tcpa", "current_tcpa", "change_direction",
            "current_campaign",
            "gp_upside_max_monthly", "gp_upside_conservative_monthly",
            "gp_upside_realistic_monthly", "gp_upside_optimistic_monthly",
            "decision_rationale",
            "threshold_states",
        ]
        display_cols = [c for c in display_cols if c in master_tier_review_df.columns]
        display_df = master_tier_review_df[display_cols].copy()
        _write_dataframe(ws, display_df, header_row=14)
        ws.freeze_panes = "D15"

    # ---------- Sheet 2: Model Comparison Summary ----------
    blend_label = (
        f"RPL: pure 12w" if rpl_blend <= 0.0
        else f"RPL: {int((1-rpl_blend)*100)}% 12w + {int(rpl_blend*100)}% 4w blend"
    )
    ws = wb.create_sheet("Model Comparison Summary")
    ws["A1"] = f"Model Comparison — Microsoft Home Search-Only (k={k}, {blend_label}, headline={headline_model})"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"12-week window: {windows.w12_start} to {windows.end_date}  |  "
                f"Eligible states: {len(result_12w)}  |  "
                f"Click threshold: ≥{CLICK_THRESHOLD}")
    ws["A2"].font = BODY_FONT

    summary_rows = []
    for m in MODEL_NAMES:
        s = model_summary_metrics(result_12w, m)
        c = compare_to_campaigns(result_12w, state_to_campaign, m)
        s.update(c)
        s["stable_spend_rate"] = stability_rate(stability_df, result_12w, m)
        # GP upside (new): requires state_to_campaign_tcpa map
        if state_to_campaign_tcpa is not None:
            from model_compare_extras import gp_upside_metrics
            u = gp_upside_metrics(result_12w, state_to_campaign, state_to_campaign_tcpa, m)
            s.update(u)
        else:
            s["gp_upside_vs_current_campaign"] = None
            s["gp_upside_vs_tier"] = None
            s["gp_upside_capture_rate"] = None
        if m in ("B1_kmeans_rpl_cpc", "B4_hierarchical"):
            bk, bs = best_k_silhouette(result_12w, m)
            s["best_k"] = bk
            s["silhouette_score"] = bs
        else:
            s["best_k"] = None
            s["silhouette_score"] = None
        summary_rows.append(s)
    summary_df = pd.DataFrame(summary_rows)[[
        "model", "n_tiers_populated",
        "avg_within_tier_rpl_spread",
        "avg_within_tier_tcpa_stdev",
        "spend_weighted_bid_loss",
        "gp_upside_vs_current_campaign", "gp_upside_vs_tier", "gp_upside_capture_rate",
        "rand_index", "adjusted_rand_index",
        "n_splits", "n_merges", "n_compared",
        "stable_spend_rate", "best_k", "silhouette_score",
    ]]
    _write_dataframe(ws, summary_df, header_row=4)

    # ---------- Sheet 3: State Tier Assignments ----------
    ws = wb.create_sheet("State Tier Assignments")
    ws["A1"] = f"State-Level Tier Assignment by Model (12w, headline={headline_model})"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"reallocation_recommendation based on {headline_model}. "
                "method_consistency_score: how many of 4 methods agree with headline (1.0 = all agree). "
                "window_consistency_score: how many windows match headline 12w (1.0 = stable).")
    ws["A2"].font = BODY_FONT
    assignment_df = result_12w[[
        "state", "clicks", "spend", "rpl", "cpc", "ideal_tcpa",
        *MODEL_NAMES,
    ]].copy()
    # Add current campaign for reference
    assignment_df["current_campaign_id"] = assignment_df["state"].map(state_to_campaign)
    # Reallocation recommendation column based on headline model
    from model_compare_extras import state_reallocation_recommendations
    recs = state_reallocation_recommendations(result_12w, state_to_campaign, headline_model)
    assignment_df["reallocation_recommendation"] = result_12w["state"].map(
        dict(zip(result_12w["state"], recs))
    )
    # Merge method consistency scores
    if method_consistency_df is not None and not method_consistency_df.empty:
        assignment_df = assignment_df.merge(method_consistency_df, on="state", how="left")
    # Merge window consistency scores
    if window_consistency_df is not None and not window_consistency_df.empty:
        assignment_df = assignment_df.merge(window_consistency_df, on="state", how="left")
    # Merge diagnostic columns (rank, distance to tier min)
    if diagnostic_df is not None and not diagnostic_df.empty:
        assignment_df = assignment_df.merge(diagnostic_df, on="state", how="left")
    assignment_df = assignment_df.sort_values("rpl", ascending=False)
    _write_dataframe(ws, assignment_df, header_row=4)

    # ---------- Sheet 3: Stability ----------
    ws = wb.create_sheet("Stability (12w_8w_4w)")
    ws["A1"] = "Stability Layer — Tier Assignments Across Windows"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = ("stability_score: 1=stable across all 3 windows, 3=different tier each window. "
                "direction: drifting_up = better in recent windows; drifting_down = worse; "
                "volatile = ping-pong; stable = unchanged.")
    ws["A2"].font = BODY_FONT
    _write_dataframe(ws, stability_df.sort_values(["model", "state"]), header_row=4)

    # ---------- Sheet 4: Tier Breakdowns ----------
    ws = wb.create_sheet("Tier Breakdowns")
    ws["A1"] = "Per-Tier Membership and Aggregates (12w)"
    ws["A1"].font = TITLE_FONT
    current_row = 3
    for m in MODEL_NAMES:
        c = ws.cell(row=current_row, column=1, value=f"Model: {m}")
        c.font = SECTION_FONT
        current_row += 1
        tier_agg = tier_aggregate_metrics(result_12w, m)
        _write_dataframe(ws, tier_agg, header_row=current_row)
        current_row += len(tier_agg) + 3

    # ---------- Sheet 5: Dendrogram ----------
    ws = wb.create_sheet("Dendrogram (B4)")
    ws["A1"] = "Hierarchical Clustering Tree — B4"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = ("Ward linkage on z-scored (RPL, CPC, log_clicks). "
                "Lower in the tree = more similar. Branches that join near the top "
                "are dissimilar even when their members share a tier label.")
    ws["A2"].font = BODY_FONT
    try:
        png_bytes = render_dendrogram_png(
            artifacts_12w["b4_linkage"], artifacts_12w["b4_state_order"]
        )
        img_path = output_path.parent / f".dendrogram_{windows.end_date}.png"
        img_path.write_bytes(png_bytes)
        img = XLImage(str(img_path))
        ws.add_image(img, "A5")
    except Exception as e:
        ws["A5"] = f"Dendrogram failed to render: {e}"

    # ---------- Sheet 6: vs Current Campaigns ----------
    ws = wb.create_sheet("vs Current Campaigns")
    ws["A1"] = "Each Model's Disagreements With Current Campaign Structure"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = ("SPLITS: same-campaign state-pairs the model would split apart. "
                "MERGES: different-campaign state-pairs the model would merge.")
    ws["A2"].font = BODY_FONT
    current_row = 4
    for m in MODEL_NAMES:
        splits, merges = enumerate_splits_and_merges(result_12w, state_to_campaign, m)
        c = ws.cell(row=current_row, column=1, value=f"Model: {m}  |  splits={len(splits)}  merges={len(merges)}")
        c.font = SECTION_FONT
        current_row += 1
        if splits:
            split_df = pd.DataFrame(splits, columns=["state_a", "state_b", "campaign"])
            ws.cell(row=current_row, column=1, value="SPLITS (same campaign, different tier):").font = SECTION_FONT
            current_row += 1
            _write_dataframe(ws, split_df, header_row=current_row)
            current_row += len(split_df) + 2
        if merges:
            merge_df = pd.DataFrame(merges, columns=["state_a", "state_b", "tier"])
            ws.cell(row=current_row, column=1, value="MERGES (different campaign, same tier):").font = SECTION_FONT
            current_row += 1
            _write_dataframe(ws, merge_df, header_row=current_row)
            current_row += len(merge_df) + 2
        current_row += 1

    # ---------- Sheet 7: Proposed Campaign Structure (from dendrogram) ----------
    if proposed_structures_df is not None and not proposed_structures_df.empty:
        ws = wb.create_sheet("Proposed Structure (B4 cuts)")
        ws["A1"] = "Proposed Campaign Groupings from Dendrogram Cuts"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("For each Ward distance cut, the dendrogram splits eligible states into groups. "
                    "Each group becomes a candidate campaign with the listed weighted RPL and "
                    "target_tcpa_150_roas. action_classification indicates implementation friction.")
        ws["A2"].font = BODY_FONT
        current_row = 4
        for cut in sorted(proposed_structures_df["cut_distance"].unique()):
            block = proposed_structures_df[proposed_structures_df["cut_distance"] == cut].copy()
            n_groups = int(block["n_groups_total_at_cut"].iloc[0])
            c = ws.cell(row=current_row, column=1,
                        value=f"Cut distance {cut} → {n_groups} groupings")
            c.font = SECTION_FONT
            current_row += 1
            display = block[[
                "group_id", "n_states", "member_states",
                "total_spend", "total_conversions", "weighted_rpl",
                "target_tcpa_150_roas", "n_current_campaigns_touched",
                "action_classification",
            ]]
            _write_dataframe(ws, display, header_row=current_row)
            current_row += len(display) + 3

    # ---------- Sheet 8: RPL Drift Watchlist ----------
    if drift_watchlist_df is not None and not drift_watchlist_df.empty:
        ws = wb.create_sheet("RPL Drift Watchlist")
        ws["A1"] = "Recent vs 12-Week RPL Movement"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("For each 12w-eligible state: rpl_pct_change = (rpl_recent - rpl_12w) / rpl_12w. "
                    "Sorted by absolute change descending. Magnitude: HIGH ≥30%, MEDIUM 20-30%, LOW <20%. "
                    "recent_window_used = '4w' if state had data in 4w pool; '8w' if only in 8w; else 'none'.")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, drift_watchlist_df, header_row=4)

    # ---------- Sheet 9: Below-Threshold State Assignment ----------
    if below_threshold_df is not None and not below_threshold_df.empty:
        ws = wb.create_sheet("Below-Threshold States")
        ws["A1"] = "Low-Volume States — Tier Assignment by Nearest A2 Centroid"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("States below 200-click threshold. Assigned to the A2 tier whose centroid RPL "
                    "is closest to the state's measured RPL. Confidence: HIGH ≥200 clicks (would be "
                    "in eligible pool), MEDIUM 50-199, LOW <50, UNCLASSIFIED = no conversions or UNKNOWN. "
                    "watchlist_flag = TRUE when assignment is uncertain (e.g., LOW-confidence state "
                    "with premium-RPL signal, or RPL >30% from assigned tier centroid).")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, below_threshold_df, header_row=5)

    # ---------- Sheet 10: k Sensitivity ----------
    if k_sensitivity_df is not None and not k_sensitivity_df.empty:
        ws = wb.create_sheet("k Sensitivity")
        ws["A1"] = "Sensitivity to Tier Count (k) Across All Models"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("Each model run at multiple k values. Look for the k where avg_within_tier_rpl_spread "
                    "and spend_weighted_bid_loss stop dropping meaningfully — that's the elbow. "
                    "k=2 typically maximizes silhouette but is operationally unusable.")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, k_sensitivity_df, header_row=4)

    # ---------- Sheet 11: k Justification (per-k summary + chart) ----------
    if k_justification_df is not None and not k_justification_df.empty:
        ws = wb.create_sheet("k Justification")
        ws["A1"] = f"Why k={k}? — Per-tier-count summary for {headline_model}"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("The data statistically prefers k=2 (silhouette). We chose k>2 for operational "
                    "granularity. The elbow shows where marginal tier value drops below operational cost.")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, k_justification_df, header_row=4)

        # Embed the elbow chart
        try:
            import model_compare_extras as mx
            png = mx.render_k_justification_png(k_justification_df, headline_k=k,
                                                 current_k=current_k)
            if png:
                img_path = output_path.parent / f".k_justification_{windows.end_date}.png"
                img_path.write_bytes(png)
                from openpyxl.drawing.image import Image as XLImage
                img = XLImage(str(img_path))
                ws.add_image(img, f"A{4 + len(k_justification_df) + 3}")
        except Exception as e:
            ws[f"A{4 + len(k_justification_df) + 3}"] = f"Elbow chart failed: {e}"

    # ---------- Sheet 12: Tier Boundary Justification ----------
    if tier_boundary_df is not None and not tier_boundary_df.empty:
        ws = wb.create_sheet("Tier Boundary Justification")
        ws["A1"] = f"Tier Boundary Justification ({headline_model})"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("For each tier boundary, shows the two boundary states and the RPL gap. "
                    "Compares to the largest within-tier gap nearby to confirm the boundary "
                    "lands at a natural break point in the RPL distribution.")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, tier_boundary_df, header_row=4)

    # ---------- Sheet 13: Snapshot Comparison (if loaded) ----------
    if snapshot_comparison_df is not None and not snapshot_comparison_df.empty:
        ws = wb.create_sheet("vs Prior Snapshot")
        ws["A1"] = f"Tier Changes vs Prior Snapshot ({headline_model})"
        ws["A1"].font = TITLE_FONT
        ws["A2"] = ("State-by-state comparison against a previously saved snapshot.")
        ws["A2"].font = BODY_FONT
        _write_dataframe(ws, snapshot_comparison_df, header_row=4)

    # ---------- Sheet 14: Methodology ----------
    ws = wb.create_sheet("Methodology")
    notes = [
        ("Microsoft Geo Model Comparison — Methodology", True),
        ("", False),
        (f"Window: {windows.w12_start} to {windows.end_date} (12 weeks primary)", False),
        (f"Stability windows: 12w / 8w / 4w", False),
        (f"Tier count k = {k}", False),
        (f"RPL blend setting: {blend_label} (per --rpl-blend CLI flag)", False),
        (f"Network filter: search only ({', '.join(sorted(SEARCH_NETWORKS))})", False),
        (f"Campaign filter: ENABLED only", False),
        (f"State eligibility: >= {CLICK_THRESHOLD} search clicks; non-UNKNOWN; finite RPL/CPC", False),
        ("", False),
        ("Models", True),
        ("A2 — RPL Quantile Bins: Equal-count bins on RPL. Top bin = TIER ONE.", False),
        ("B1 — K-Means on (RPL, CPC) z-scored: 2D k-means with random_state=42.", False),
        ("B4 — Hierarchical Ward on (RPL, CPC, log_clicks) z-scored: dendrogram cut at k.", False),
        ("C2 — Weighted K-Means on ideal_tcpa, sample_weight=spend: minimizes spend-weighted bid loss.", False),
        ("", False),
        ("Tier label normalization", True),
        ("Within each model, raw cluster IDs are reassigned so TIER ONE has the highest mean RPL, ", False),
        ("TIER TWO the next highest, and so on. This makes tier labels comparable across models.", False),
        ("", False),
        ("Comparison metrics", True),
        ("avg_within_tier_rpl_spread: average of max(rpl)/min(rpl) across tiers (weighted by tier size).", False),
        ("   Lower = tighter bid economics inside each tier.", False),
        ("avg_within_tier_tcpa_stdev: stdev of state ideal_tcpa within tier (size-weighted).", False),
        ("   Direct readout of how badly a single tier-level tCPA misfits its members.", False),
        ("spend_weighted_bid_loss: sum over states of (ideal_tcpa - tier_tcpa)^2 * spend.", False),
        ("   Operational dollars-at-stake. Lower = better.", False),
        ("rand_index: pairwise agreement % between model tiers and current campaign groupings.", False),
        ("adjusted_rand_index: Rand Index corrected for chance. 0 = no signal, 1 = perfect agreement.", False),
        ("n_splits: state-pairs in the same campaign that the model places in different tiers.", False),
        ("n_merges: state-pairs in different campaigns that the model places in the same tier.", False),
        ("stable_spend_rate: % of total 12w spend in states with one tier across 12w/8w/4w.", False),
        ("best_k / silhouette_score (B1, B4 only): k that maximizes silhouette across k=2..8.", False),
        ("gp_upside_vs_current_campaign: sum over eligible states of max(0, ideal_tcpa - ", False),
        ("   current_campaign_tcpa) * conversions. Headline 'GP left on the table' number — ", False),
        ("   independent of model choice; depends only on current campaign tCPAs.", False),
        ("gp_upside_vs_tier: sum over eligible states of max(0, ideal_tcpa - tier_tcpa) * conversions. ", False),
        ("   'GP still available within-tier after adopting model's recommendations.'", False),
        ("gp_upside_capture_rate: 1 - (vs_tier / vs_current). % of upside the model captures.", False),
        ("", False),
        ("Additional sheets", True),
        ("Proposed Structure (B4 cuts): For each Ward distance cut on the B4 dendrogram, the ", False),
        ("   eligible states split into groups. Each group is a candidate campaign with its own ", False),
        ("   weighted RPL and target_tcpa_150_roas. action_classification flags implementation ", False),
        ("   friction (in_place_tcpa_only, in_place_no_move, re_tier_partial, campaign_split_required).", False),
        ("RPL Drift Watchlist: rpl_pct_change between 12w and the most recent eligible window ", False),
        ("   (4w preferred, falls back to 8w). Sorted by absolute magnitude descending. ", False),
        ("   Use to surface states where recency vs history disagrees significantly.", False),
        ("Below-Threshold States: states with <200 search clicks in 12w. Assigned to the nearest ", False),
        ("   A2 tier by RPL centroid distance. confidence = HIGH (would be in eligible pool), ", False),
        ("   MEDIUM (50-199 clicks), LOW (<50 clicks), or UNCLASSIFIED (zero conversions / UNKNOWN). ", False),
        ("   watchlist_flag = TRUE for high-RPL LOW-confidence states (escalate if volume grows) ", False),
        ("   or states whose measured RPL is >30% from their assigned tier's centroid.", False),
        ("k Sensitivity: each model run at k = 2, 3, 4, 5, 6, 7. Compare metrics across k to ", False),
        ("   identify the elbow where adding tiers stops materially improving the trade-offs.", False),
        ("vs Prior Snapshot: present only when --compare-snapshot is provided. Per-state tier ", False),
        ("   changes vs a previously saved snapshot. Useful for longitudinal tracking.", False),
        ("", False),
        ("Snapshot save/load", True),
        ("--save-snapshot PATH: pickles the full analysis state for later reproducibility.", False),
        ("--load-snapshot PATH: bypasses DB query; regenerates the workbook from saved state.", False),
        ("--compare-snapshot PATH: adds a 'vs Prior Snapshot' sheet comparing today vs that path.", False),
        ("Saved snapshots include result_12w/8w/4w, stability_df, state_to_campaign, tCPAs, ", False),
        ("   artifacts, and the windows. They do NOT include the raw DB.", False),
        ("", False),
        ("Stability direction labels", True),
        ("stable: same tier in all 3 windows", False),
        ("drifting_up: tier improved from 12w to 4w (now in a higher tier — better)", False),
        ("drifting_down: tier worsened from 12w to 4w (now in a lower tier)", False),
        ("volatile: tier changed but ended where it started (ping-pong)", False),
        ("partial: state missing from at least one window's eligible pool", False),
        ("", False),
        ("Choosing a model", True),
        ("There's no universal winner. Trade-offs to weigh:", False),
        ("  - C2 will always have the lowest spend_weighted_bid_loss by construction.", False),
        ("  - A2 is the simplest to defend and most reproducible.", False),
        ("  - B1 adds CPC sensitivity but its bid_loss may suffer when low-CPC states have low RPL.", False),
        ("  - B4 surfaces structure via the dendrogram but fragments more than the others.", False),
        ("  - Models with high stable_spend_rate are more defensible against the 30-day push-back.", False),
    ]
    for r_idx, (text, is_hdr) in enumerate(notes, start=1):
        c = ws.cell(row=r_idx, column=1, value=text)
        c.font = SECTION_FONT if is_hdr else BODY_FONT
    ws.column_dimensions["A"].width = 110

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


# ----------------------------------------------------------------------------
# Terminal reporting

def print_summary(
    windows: Windows, k: int,
    result_12w: pd.DataFrame,
    stability_df: pd.DataFrame,
    state_to_campaign: dict[str, str],
) -> None:
    print("=" * 78)
    print(f"Model Comparison — Microsoft Home Search-Only (k={k})")
    print("=" * 78)
    print(f"12w window:      {windows.w12_start} -> {windows.end_date}")
    print(f"8w window:       {windows.w8_start} -> {windows.end_date}")
    print(f"4w window:       {windows.w4_start} -> {windows.end_date}")
    print(f"Eligible states: {len(result_12w)}")
    print()

    print(f"{'Model':<24} {'AvgSpread':>10} {'tCPAStdev':>10} {'BidLoss':>14} {'Rand':>6} {'ARI':>6} {'Splits':>7} {'Merges':>7} {'Stable%':>8}")
    print("-" * 100)
    for m in MODEL_NAMES:
        s = model_summary_metrics(result_12w, m)
        c = compare_to_campaigns(result_12w, state_to_campaign, m)
        sr = stability_rate(stability_df, result_12w, m)
        print(f"{m:<24} "
              f"{s['avg_within_tier_rpl_spread']:>10.3f} "
              f"${s['avg_within_tier_tcpa_stdev']:>9.2f} "
              f"${s['spend_weighted_bid_loss']:>13,.0f} "
              f"{c['rand_index']:>6.3f} "
              f"{c['adjusted_rand_index']:>6.3f} "
              f"{c['n_splits']:>7} "
              f"{c['n_merges']:>7} "
              f"{sr:>7.1%}")
    print()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--k", type=int, default=DEFAULT_K,
                        help=f"Tier count for all models (default {DEFAULT_K})")
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--save-snapshot", type=str, default=None,
                        help="Save the analysis state to this pickle path")
    parser.add_argument("--load-snapshot", type=str, default=None,
                        help="Load a previously saved snapshot instead of querying DB")
    parser.add_argument("--compare-snapshot", type=str, default=None,
                        help="Compare current analysis to a prior snapshot at this path")
    parser.add_argument("--k-sensitivity", type=str, default="2,3,4,5,6,7,8,9,10,11,12",
                        help="Comma-separated k values for k-sensitivity sheet. "
                             "Default range extends to k=12 (current Microsoft Home structure).")
    parser.add_argument("--rpl-blend", type=float, default=0.0,
                        help="Weight for blending 4w RPL into 12w RPL. "
                             "0.0 = pure 12w (default); 0.3 = 70%% 12w + 30%% 4w; "
                             "1.0 = pure 4w. States with <75 4w clicks skip the blend.")
    parser.add_argument("--click-threshold-12w", type=int, default=CLICK_THRESHOLD,
                        help=f"Click threshold for 12w eligible pool (default {CLICK_THRESHOLD}).")
    parser.add_argument("--click-threshold-8w", type=int, default=75,
                        help="Click threshold for 8w stability window (default 75).")
    parser.add_argument("--click-threshold-4w", type=int, default=75,
                        help="Click threshold for 4w stability window (default 75).")
    parser.add_argument("--headline-model", type=str, default="C2_weighted_kmeans",
                        help="Primary model for tier recommendations "
                             "(default C2_weighted_kmeans). Master Tier Review and "
                             "all recommendation columns use this model.")
    parser.add_argument("--capture-low", type=float, default=0.40,
                        help="Conservative GP capture rate for upside estimates (default 0.40).")
    parser.add_argument("--capture-high", type=float, default=0.70,
                        help="Optimistic GP capture rate for upside estimates (default 0.70).")
    parser.add_argument("--current-k", type=int, default=12,
                        help="Current account structure tier count, for elbow chart "
                             "annotation (default 12 — Microsoft Home's current campaigns).")
    parser.add_argument("--dendrogram-cuts", type=str, default="2.0,3.0,3.85,5.0",
                        help="Comma-separated Ward distances for Proposed Structure sheet")
    args = parser.parse_args(argv)

    # Lazy import to avoid module-load failure if extras is missing
    import model_compare_extras as mx

    snapshot = None  # populated if --load-snapshot

    if args.load_snapshot:
        snap_path = Path(args.load_snapshot)
        if not snap_path.exists():
            print(f"ERROR: snapshot not found: {snap_path}", file=sys.stderr)
            return 1
        print(f"Loading snapshot from {snap_path}")
        snapshot = mx.load_snapshot(snap_path)
        windows = snapshot["windows"]
        k = snapshot["k"]
        result_12w = snapshot["result_12w"]
        result_8w = snapshot.get("result_8w")
        result_4w = snapshot.get("result_4w")
        state_to_camp = snapshot["state_to_camp"]
        state_to_camp_tcpa = snapshot.get("state_to_camp_tcpa", {})
        all_states_12w = snapshot.get("all_states_12w")
        artifacts_12w = snapshot["artifacts_12w"]
        stability_df = snapshot.get("stability_df")
        # Snapshot v3+ also contains network economics, headline model, thresholds
        snapshot_network_economics = snapshot.get("network_economics_df")
        snapshot_below_threshold = snapshot.get("below_threshold_df")
        end_date = windows.end_date
        print(f"Restored snapshot: end_date={end_date}, k={k}, "
              f"eligible_states={len(result_12w)}")
    else:
        db_path = Path(args.db_path)
        if not db_path.exists():
            print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))

        end_date = resolve_end_date(conn, args.end_date)
        windows = build_windows(end_date)
        k = args.k

        print(f"End date:  {windows.end_date}")
        print(f"12w:       {windows.w12_start} -> {windows.end_date}")
        print(f"8w:        {windows.w8_start} -> {windows.end_date}")
        print(f"4w:        {windows.w4_start} -> {windows.end_date}")
        print(f"k:         {k}\n")

        eids = pull_enabled_campaign_ids(conn)
        if not eids:
            print("ERROR: No ENABLED campaigns.", file=sys.stderr)
            return 1
        print(f"ENABLED campaigns: {len(eids)}")

        # Pull tCPA per campaign — needed for GP upside metric
        camps_df = pull_enabled_campaigns_with_tcpa(conn)
        camp_tcpa_map = dict(zip(camps_df["campaign_id"], camps_df["current_tcpa"]))

        state_to_camp = pull_state_to_campaign_map(conn, windows.w12_start, windows.end_date, eids)
        state_to_camp_tcpa = {
            s: camp_tcpa_map.get(c) for s, c in state_to_camp.items()
        }

        # Pull all 12w state aggregates (raw — for below-threshold sheet)
        raw_12w = pull_search_state_aggregates(conn, windows.w12_start, windows.end_date, eids)
        if raw_12w.empty:
            print("ERROR: No 12w search-network rows.", file=sys.stderr)
            return 1

        # Pull 4w raw aggregates IF blending is requested (also reused below for stability)
        raw_4w = None
        if args.rpl_blend > 0.0:
            raw_4w = pull_search_state_aggregates(conn, windows.w4_start, windows.end_date, eids)
            raw_4w = compute_derived(raw_4w) if not raw_4w.empty else None

        # Compute derived metrics, then apply RPL blend (no-op if blend=0)
        all_states_12w = compute_derived(raw_12w)
        if args.rpl_blend > 0.0:
            all_states_12w = apply_rpl_blend(all_states_12w, raw_4w, args.rpl_blend)
            n_blended = int(all_states_12w["blend_applied"].sum())
            print(f"RPL blend active: weight={args.rpl_blend:.2f}; "
                  f"{n_blended}/{len(all_states_12w)} states had 4w data and were blended")
        else:
            print(f"RPL blend: disabled (using pure 12w RPL)")

        # Build eligible pool from the (possibly blended) 12w aggregates
        elig_12w = eligible_pool(all_states_12w, click_threshold=args.click_threshold_12w)
        if len(elig_12w) < k:
            print(f"ERROR: 12w eligible pool too small for k={k}.", file=sys.stderr)
            return 1

        # Apply all four models on the eligible pool
        result_12w, artifacts_12w = apply_all_models(elig_12w, k)
        print(f"12w eligible states: {len(result_12w)} (threshold={args.click_threshold_12w})")

        # Stability windows — use the lower thresholds for these (diagnostic)
        result_8w = run_models_for_window(
            conn, windows.w8_start, windows.end_date, eids, k,
            click_threshold=args.click_threshold_8w,
        )
        result_4w = run_models_for_window(
            conn, windows.w4_start, windows.end_date, eids, k,
            click_threshold=args.click_threshold_4w,
        )
        print(f"8w eligible states:  {len(result_8w) if result_8w is not None else 'N/A'} (threshold={args.click_threshold_8w})")
        print(f"4w eligible states:  {len(result_4w) if result_4w is not None else 'N/A'} (threshold={args.click_threshold_4w})\n")

        stability_df = build_stability_table(result_12w, result_8w, result_4w)

    # ------ Build new dataframes (works for both fresh and loaded paths) ------

    headline_model = args.headline_model if snapshot is None else snapshot.get("headline_model", "C2_weighted_kmeans")

    # Below-threshold assignment (now driven by headline model, not A2)
    below_threshold_df = None
    if all_states_12w is not None and not all_states_12w.empty:
        below_threshold_df = mx.assign_below_threshold_states(
            all_states_12w, result_12w, headline_model,
        )
        print(f"Below-threshold states: {len(below_threshold_df) if below_threshold_df is not None else 0}")

    # Drift watchlist
    drift_watchlist_df = mx.build_drift_watchlist(result_12w, result_4w, result_8w)

    # Proposed structures from dendrogram cuts
    cut_distances = [float(x.strip()) for x in args.dendrogram_cuts.split(",")]
    proposed_structures_df = mx.proposed_structures_from_dendrogram(
        artifacts_12w["b4_linkage"], artifacts_12w["b4_state_order"],
        result_12w, state_to_camp,
        cut_distances=cut_distances,
    )

    # k-sensitivity (only on fresh data — skip on snapshot load)
    k_sensitivity_df = None
    k_justification_df = None
    if snapshot is None:
        k_values = [int(x.strip()) for x in args.k_sensitivity.split(",")]
        eligible_only = result_12w.drop(columns=MODEL_NAMES).copy() if all(
            m in result_12w.columns for m in MODEL_NAMES
        ) else result_12w.copy()
        k_sensitivity_df = mx.k_sensitivity_table(
            eligible_only, k_values, state_to_camp, state_to_camp_tcpa,
            apply_all_models, model_summary_metrics, compare_to_campaigns,
        )
        print(f"k-sensitivity rows: {len(k_sensitivity_df)} ({len(k_values)} k values × {len(MODEL_NAMES)} models)")
        # Build the k-justification table (pivot to headline model only)
        k_justification_df = mx.build_k_justification_table(k_sensitivity_df, headline_model)

    # Method consistency score (per state, agreement across the 4 models)
    method_consistency_df = mx.method_consistency_score(result_12w)

    # Window consistency score (per state, agreement of 8w/4w with 12w under headline)
    window_consistency_df = mx.window_consistency_per_state(stability_df, headline_model)

    # Per-state diagnostic columns (rank, distance to tier min)
    diagnostic_df = mx.rank_and_tier_distance(result_12w, headline_model)

    # Tier boundary justification
    tier_boundary_df = mx.build_tier_boundary_justification(result_12w, headline_model)

    # Network economics — pull search and audience separately (snapshot-aware)
    network_economics_df = None
    if snapshot is None:
        # Need to re-pull because fresh data path.
        eids_for_ne = pull_enabled_campaign_ids(conn)
        below_states = []
        if below_threshold_df is not None and not below_threshold_df.empty and "state" in below_threshold_df.columns:
            below_states = list(below_threshold_df["state"])
        all_states_in_pool = list(result_12w["state"]) + below_states
        network_economics_df = mx.build_network_economics(
            conn, windows.w12_start, windows.end_date, eids_for_ne,
            all_states_in_pool, pull_state_aggregates_by_network,
            SEARCH_NETWORKS, AUDIENCE_NETWORKS,
        )
    else:
        # Reuse what's in the snapshot (v3+ stores it)
        network_economics_df = snapshot.get("network_economics_df")

    # Master Tier Review (the sheet 1 deliverable)
    master_tier_review_df = None
    if network_economics_df is not None:
        master_tier_review_df = mx.build_master_tier_review(
            result_12w, below_threshold_df, network_economics_df,
            state_to_camp, state_to_camp_tcpa,
            headline_model=headline_model,
            capture_low=args.capture_low,
            capture_high=args.capture_high,
        )
        print(f"Master Tier Review rows: {len(master_tier_review_df)}")

    # Snapshot comparison
    snapshot_comparison_df = None
    if args.compare_snapshot:
        prior_path = Path(args.compare_snapshot)
        if prior_path.exists():
            prior = mx.load_snapshot(prior_path)
            current = {"result_12w": result_12w}
            snapshot_comparison_df = mx.compare_snapshots(prior, current, headline_model)
            print(f"Snapshot comparison: {len(snapshot_comparison_df)} states compared against {prior_path}")
        else:
            print(f"WARNING: --compare-snapshot path not found: {prior_path}")

    # Print summary
    print_summary(windows, k, result_12w, stability_df, state_to_camp)

    # Save snapshot if requested (BEFORE workbook write so it doesn't depend on success)
    if args.save_snapshot:
        snap_out_path = Path(args.save_snapshot)
        snap_dict = {
            "version": 3,
            "saved_at": str(date.today()),
            "windows": windows,
            "k": k,
            "rpl_blend": args.rpl_blend,
            "headline_model": headline_model,
            "click_threshold_12w": args.click_threshold_12w,
            "click_threshold_8w": args.click_threshold_8w,
            "click_threshold_4w": args.click_threshold_4w,
            "result_12w": result_12w,
            "result_8w": result_8w,
            "result_4w": result_4w,
            "state_to_camp": state_to_camp,
            "state_to_camp_tcpa": state_to_camp_tcpa,
            "all_states_12w": all_states_12w,
            "artifacts_12w": artifacts_12w,
            "stability_df": stability_df,
            "network_economics_df": network_economics_df,
            "below_threshold_df": below_threshold_df,
        }
        mx.save_snapshot(snap_out_path, snap_dict)
        print(f"Saved snapshot: {snap_out_path}")

    if args.dry_run:
        print("--dry-run set, skipping workbook write.")
        return 0

    output_path = Path(args.output_dir) / f"microsoft_geo_model_comparison_{end_date}.xlsx"
    write_workbook(
        output_path, windows, k, result_12w, stability_df,
        state_to_camp, artifacts_12w,
        state_to_campaign_tcpa=state_to_camp_tcpa,
        below_threshold_df=below_threshold_df,
        drift_watchlist_df=drift_watchlist_df,
        proposed_structures_df=proposed_structures_df,
        k_sensitivity_df=k_sensitivity_df,
        snapshot_comparison_df=snapshot_comparison_df,
        rpl_blend=args.rpl_blend if snapshot is None else snapshot.get("rpl_blend", 0.0),
        headline_model=headline_model,
        master_tier_review_df=master_tier_review_df,
        method_consistency_df=method_consistency_df,
        window_consistency_df=window_consistency_df,
        diagnostic_df=diagnostic_df,
        tier_boundary_df=tier_boundary_df,
        k_justification_df=k_justification_df,
        capture_low=args.capture_low,
        capture_high=args.capture_high,
        current_k=args.current_k,
    )
    print(f"Wrote: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
