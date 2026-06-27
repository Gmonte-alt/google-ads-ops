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
    df = pd.read_sql_query(
        """SELECT campaign_id FROM campaigns
           WHERE account_id = ? AND status = 'ENABLED'""",
        conn, params=(ACCOUNT_ID,),
    )
    return df["campaign_id"].tolist()


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


def eligible_pool(df: pd.DataFrame) -> pd.DataFrame:
    """Eligible = >=200 search clicks, has finite RPL and CPC, not UNKNOWN."""
    return df[
        (df["clicks"] >= CLICK_THRESHOLD)
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
) -> pd.DataFrame | None:
    """Pull a window, build eligible pool, apply all models.
    Returns the tier-assigned eligible pool, or None if no data.
    """
    agg = pull_search_state_aggregates(conn, start, end, enabled_campaign_ids)
    if agg.empty:
        return None
    agg = compute_derived(agg)
    elig = eligible_pool(agg)
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

    # ---------- Sheet 1: Model Comparison Summary ----------
    ws = wb.create_sheet("Model Comparison Summary")
    ws["A1"] = f"Model Comparison — Microsoft Home Search-Only (k={k})"
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
        "rand_index", "adjusted_rand_index",
        "n_splits", "n_merges", "n_compared",
        "stable_spend_rate", "best_k", "silhouette_score",
    ]]
    _write_dataframe(ws, summary_df, header_row=4)

    # ---------- Sheet 2: State Tier Assignments ----------
    ws = wb.create_sheet("State Tier Assignments")
    ws["A1"] = "State-Level Tier Assignment by Model (12w)"
    ws["A1"].font = TITLE_FONT
    assignment_df = result_12w[[
        "state", "clicks", "spend", "rpl", "cpc", "ideal_tcpa",
        *MODEL_NAMES,
    ]].copy()
    # Add current campaign for reference
    assignment_df["current_campaign_id"] = assignment_df["state"].map(state_to_campaign)
    assignment_df = assignment_df.sort_values("rpl", ascending=False)
    _write_dataframe(ws, assignment_df, header_row=3)

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

    # ---------- Sheet 7: Methodology ----------
    ws = wb.create_sheet("Methodology")
    notes = [
        ("Microsoft Geo Model Comparison — Methodology", True),
        ("", False),
        (f"Window: {windows.w12_start} to {windows.end_date} (12 weeks primary)", False),
        (f"Stability windows: 12w / 8w / 4w", False),
        (f"Tier count k = {k}", False),
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
    args = parser.parse_args(argv)

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

    # Build state->campaign map from 12w SEARCH spend
    state_to_camp = pull_state_to_campaign_map(conn, windows.w12_start, windows.end_date, eids)

    # Run all models on the 12w window (primary)
    result_12w = run_models_for_window(conn, windows.w12_start, windows.end_date, eids, k)
    if result_12w is None or len(result_12w) < k:
        print(f"ERROR: 12w eligible pool too small for k={k}.", file=sys.stderr)
        return 1
    _, artifacts_12w = apply_all_models(result_12w, k)
    print(f"12w eligible states: {len(result_12w)}")

    # Stability windows
    result_8w = run_models_for_window(conn, windows.w8_start, windows.end_date, eids, k)
    result_4w = run_models_for_window(conn, windows.w4_start, windows.end_date, eids, k)
    print(f"8w eligible states:  {len(result_8w) if result_8w is not None else 'N/A'}")
    print(f"4w eligible states:  {len(result_4w) if result_4w is not None else 'N/A'}\n")

    stability_df = build_stability_table(result_12w, result_8w, result_4w)

    print_summary(windows, k, result_12w, stability_df, state_to_camp)

    if args.dry_run:
        print("--dry-run set, skipping workbook write.")
        return 0

    output_path = Path(args.output_dir) / f"microsoft_geo_model_comparison_{end_date}.xlsx"
    write_workbook(output_path, windows, k, result_12w, stability_df,
                   state_to_camp, artifacts_12w)
    print(f"Wrote: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
