"""
google_geo_model.py — Google Home geo-tiering. Ported from microsoft_geo_model_comparison.py;
the clustering method (A2 quantile, B1 2-D k-means, B4 Ward hierarchical, C2 weighted k-means)
is unchanged. What's configurable is the FEATURE the tiers are built on.

CLUSTERING FEATURE (--cluster-feature):
  gp_per_lead  (DEFAULT) = RPL - CPL  (margin per lead). Matches the current Google tier
                            philosophy, which is a CPC x lead-value PROFILE with margin as the
                            spine (Tier 1 cheap/efficient/positive-margin; Tier 2 premium/high-value/
                            thin-margin; Tier 3 volatile mid-pack; Tier 4 break-even/volume). GP/lead
                            folds cost and value into the margin you bid for. TIER ONE = highest GP/lead.
  roas         = revenue / spend (margin as a ratio).
  rpl          = revenue / conversion (pure value; the original Microsoft basis — ignores CPC).

GOOGLE DATA LAYER (vs the Microsoft core):
  - account_id = 1; source campaign_performance_geo_daily_search (sums device rows; no 'ALL' row;
    no network/location_type). Native conversion basis (same as the workbook RPL_Redistribution):
    RPL/GP ordering sound; absolute $ run ~10-13% conservative.
  - search-only (no audience network); single-state campaigns (--exclude-states) are carved out
    of the clustering pool since they stay standalone.
  - tROAS account: tier_target_tcpa_analytical (= tier RPL / 1.5) is reported for reference only,
    NOT an operational bid. The operational output is tier MEMBERSHIP.

Usage:
    python google_geo_model.py --rpl-blend 0.6 --k 7
    python google_geo_model.py --rpl-blend 0.6 --elbow            # sweep k, write elbow chart
    python google_geo_model.py --rpl-blend 0.6 --k 7 --cluster-feature rpl
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

DEFAULT_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\google-geo-analysis\output"
ACCOUNT_ID = 1
GEO_TABLE = "campaign_performance_geo_daily_search"
CLICK_THRESHOLD = 200
TARGET_ROAS = 1.50
DEFAULT_K = 7
DEFAULT_EXCLUDE = ["California", "Texas", "Florida"]   # standalone single-state campaigns

# Set in main() from --cluster-feature
FEATURE = "gp_per_lead"
FEATURE_LABEL = "GP/lead"

TIER_LABEL_BASE = [
    "TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR", "TIER FIVE",
    "TIER SIX", "TIER SEVEN", "TIER EIGHT", "TIER NINE", "TIER TEN",
    "TIER ELEVEN", "TIER TWELVE",
]
MODEL_NAMES = ["A2_quantile", "B1_kmeans_2d", "B4_hierarchical", "C2_weighted_kmeans"]
CURRENT_TIER_NAMES = ["Tier One", "Tier Two", "Tier Three", "Tier Four",
                      "California", "Texas", "Florida"]


@dataclass
class Windows:
    end_date: date
    w12_start: date
    w8_start: date
    w4_start: date


def build_windows(end_date: date) -> Windows:
    return Windows(end_date=end_date,
                   w12_start=end_date - timedelta(days=83),
                   w8_start=end_date - timedelta(days=55),
                   w4_start=end_date - timedelta(days=27))


def current_label(name: str) -> str:
    u = (name or "").upper()
    for nm in CURRENT_TIER_NAMES:
        if nm.upper() in u:
            return nm
    return name or ""


# ----------------------------------------------------------------------------
# Data pull (Google)
# ----------------------------------------------------------------------------

def resolve_end_date(conn, override):
    if override:
        return date.fromisoformat(override)
    row = conn.execute(f"SELECT MAX(date) FROM {GEO_TABLE} WHERE account_id = ?", (ACCOUNT_ID,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"No rows in {GEO_TABLE} for account_id={ACCOUNT_ID}")
    max_date = date.fromisoformat(row[0])
    yesterday = date.today() - timedelta(days=1)
    return yesterday if max_date == yesterday else max_date


def pull_state_aggregates(conn, start, end):
    sql = f"""
        SELECT geo_state AS state, SUM(clicks) AS clicks,
               SUM(cost_micros) / 1e6 AS spend,
               SUM(conversions) AS conversions, SUM(conversion_value) AS revenue
        FROM {GEO_TABLE}
        WHERE account_id = ? AND device <> 'ALL' AND geo_state IS NOT NULL
          AND date BETWEEN ? AND ?
        GROUP BY geo_state
    """
    return pd.read_sql_query(sql, conn, params=(ACCOUNT_ID, start.isoformat(), end.isoformat()))


def pull_state_to_current_tier(conn, start, end):
    try:
        df = pd.read_sql_query(
            f"""SELECT g.geo_state AS state, c.campaign_name AS campaign,
                       SUM(g.cost_micros)/1e6 AS spend
                FROM {GEO_TABLE} g
                JOIN campaigns c ON c.campaign_id = g.campaign_id AND c.account_id = g.account_id
                WHERE g.account_id = ? AND g.device <> 'ALL' AND g.geo_state IS NOT NULL
                  AND g.date BETWEEN ? AND ?
                GROUP BY g.geo_state, c.campaign_name""",
            conn, params=(ACCOUNT_ID, start.isoformat(), end.isoformat()))
        if df.empty:
            return {}
        top = df.sort_values("spend", ascending=False).groupby("state").head(1)
        return {s: current_label(c) for s, c in zip(top["state"], top["campaign"])}
    except Exception as e:
        print(f"  (current-tier map skipped: {e})", file=sys.stderr)
        return {}


# ============================================================================
# Method (clustering identical to the Microsoft core; value axis = FEATURE)
# ============================================================================

def compute_derived(df):
    df = df.copy()
    df["cpc"] = df["spend"] / df["clicks"].where(df["clicks"] > 0)
    df["cpa"] = df["spend"] / df["conversions"].where(df["conversions"] > 0)
    df["roas"] = df["revenue"] / df["spend"].where(df["spend"] > 0)
    df["rpl"] = df["revenue"] / df["conversions"].where(df["conversions"] > 0)
    df["cpl"] = df["spend"] / df["conversions"].where(df["conversions"] > 0)
    df["gp_per_lead"] = df["rpl"] - df["cpl"]
    df["ideal_tcpa"] = df["rpl"] / TARGET_ROAS
    return df


def apply_rpl_blend(df_12w, df_4w, blend_weight, min_blend_clicks=75):
    """Blend 4w into 12w RPL, then re-derive cpl/gp_per_lead/roas-dependent fields are
    based on the (blended) RPL and the 12w cost structure."""
    out = df_12w.copy()
    out["rpl_12w_original"] = out["rpl"]
    out["blend_applied"] = False
    if blend_weight <= 0.0 or df_4w is None or df_4w.empty:
        return out
    blend_weight = min(max(blend_weight, 0.0), 1.0)
    idx4 = df_4w.set_index("state")

    def _blend(row):
        st = row["state"]; r12 = row["rpl"]
        if st not in idx4.index:
            return r12, False
        r4 = idx4.loc[st]
        if pd.isna(r4["rpl"]) or r4["rpl"] <= 0 or r4["clicks"] < min_blend_clicks:
            return r12, False
        return (1.0 - blend_weight) * r12 + blend_weight * r4["rpl"], True

    res = out.apply(_blend, axis=1)
    out["rpl"] = [b[0] for b in res]
    out["blend_applied"] = [b[1] for b in res]
    # re-derive value-dependent fields from blended RPL (cpl/cpc unchanged — cost-side)
    out["gp_per_lead"] = out["rpl"] - out["cpl"]
    out["ideal_tcpa"] = out["rpl"] / TARGET_ROAS
    return out


def eligible_pool(df, click_threshold=CLICK_THRESHOLD):
    """Eligible = enough clicks, finite RPL/CPC, not UNKNOWN. (GP/lead may be negative —
    a real state we bid on; eligibility is about volume/validity, not profitability.)"""
    return df[
        (df["clicks"] >= click_threshold)
        & df["rpl"].notna() & df["cpc"].notna() & df[FEATURE].notna()
        & (df["rpl"] > 0) & (df["cpc"] > 0)
        & (df["state"].str.upper() != "UNKNOWN")
    ].copy().reset_index(drop=True)


def relabel_by_feature(states, cluster_col, k):
    """Map cluster IDs to TIER labels ordered by mean FEATURE descending.
    TIER ONE = best (highest GP/lead / ROAS / RPL)."""
    means = states.groupby(cluster_col)[FEATURE].mean().sort_values(ascending=False)
    rank_map = {old: TIER_LABEL_BASE[i] for i, old in enumerate(means.index[:k])}
    return states[cluster_col].map(rank_map).astype(object)


def model_a2_quantile(states, k):
    ranks = states[FEATURE].rank(method="first", ascending=False)
    out = states.copy(); out["_c"] = pd.cut(ranks, bins=k, labels=False, include_lowest=True)
    return relabel_by_feature(out, "_c", k)


def model_b1_kmeans_2d(states, k, seed=42):
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    X = StandardScaler().fit_transform(states[[FEATURE, "cpc"]].values)
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(X)
    out = states.copy(); out["_c"] = km.labels_
    return relabel_by_feature(out, "_c", k)


def model_b4_hierarchical(states, k):
    from scipy.cluster.hierarchy import linkage, fcluster
    from sklearn.preprocessing import StandardScaler
    feat = pd.DataFrame({FEATURE: states[FEATURE].values, "cpc": states["cpc"].values,
                         "log_clicks": np.log(states["clicks"].values)})
    X = StandardScaler().fit_transform(feat.values)
    Z = linkage(X, method="ward")
    out = states.copy(); out["_c"] = fcluster(Z, t=k, criterion="maxclust")
    return relabel_by_feature(out, "_c", k), Z


def model_c2_weighted_kmeans(states, k, seed=42):
    """1D weighted k-means on FEATURE, sample_weight=spend (headline). Minimizes
    Σ((feature_i - tier_feature)^2 * spend_i)."""
    from sklearn.cluster import KMeans
    X = states[[FEATURE]].values; w = states["spend"].values
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(X, sample_weight=w)
    out = states.copy(); out["_c"] = km.labels_
    return relabel_by_feature(out, "_c", k)


def apply_all_models(states, k):
    out = states.copy().reset_index(drop=True)
    out["A2_quantile"] = model_a2_quantile(out, k)
    out["B1_kmeans_2d"] = model_b1_kmeans_2d(out, k)
    b4, Z = model_b4_hierarchical(out, k)
    out["B4_hierarchical"] = b4
    out["C2_weighted_kmeans"] = model_c2_weighted_kmeans(out, k)
    return out, {"b4_linkage": Z, "b4_state_order": out["state"].tolist()}


def _wtd(grp, col):
    sp = grp["spend"].sum()
    return (grp[col] * grp["spend"]).sum() / sp if sp > 0 else np.nan


def tier_aggregate_metrics(states, tier_col):
    """Per-tier profile so each tier maps to a margin archetype."""
    rows = []
    for tier, grp in states.groupby(tier_col, observed=True):
        if pd.isna(tier) or tier == "":
            continue
        rows.append({
            "tier": tier, "n_states": len(grp),
            "states": ", ".join(sorted(grp["state"].tolist())),
            "tier_spend": grp["spend"].sum(),
            "wtd_gp_per_lead": _wtd(grp, "gp_per_lead"),
            "wtd_rpl": _wtd(grp, "rpl"),
            "wtd_cpc": _wtd(grp, "cpc"),
            "wtd_cpl": _wtd(grp, "cpl"),
            "wtd_roas": _wtd(grp, "roas"),
            "tier_target_tcpa_analytical": _wtd(grp, "rpl") / TARGET_ROAS,
            "feat_min": grp[FEATURE].min(), "feat_max": grp[FEATURE].max(),
            "feat_range": grp[FEATURE].max() - grp[FEATURE].min(),
            "feat_stdev": grp[FEATURE].std(ddof=0),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {t: i for i, t in enumerate(TIER_LABEL_BASE)}
    df["_o"] = df["tier"].map(order)
    return df.sort_values("_o").drop(columns=["_o"]).reset_index(drop=True)


def model_summary_metrics(states, tier_col):
    ta = tier_aggregate_metrics(states, tier_col)
    if ta.empty:
        return {}
    feat_map = dict(zip(ta["tier"], ta[f"wtd_{FEATURE}"] if f"wtd_{FEATURE}" in ta else ta["wtd_gp_per_lead"]))
    # tier-center of the FEATURE (spend-weighted mean within tier)
    center = states.groupby(tier_col)[FEATURE].apply(lambda g: np.average(g, weights=states.loc[g.index, "spend"]) if states.loc[g.index, "spend"].sum() > 0 else g.mean())
    s = states.copy()
    s["_tc"] = s[tier_col].map(center)
    bid_loss = ((s[FEATURE] - s["_tc"]) ** 2 * s["spend"]).sum()
    vstd = ta.dropna(subset=["feat_stdev"])
    avg_stdev = ((vstd["feat_stdev"] * vstd["n_states"]).sum() / vstd["n_states"].sum()) if not vstd.empty else np.nan
    return {"model": tier_col, "n_tiers_populated": len(ta),
            "avg_within_tier_feature_stdev": avg_stdev,
            "spend_weighted_bid_loss": bid_loss}


def run_models_for_window(conn, start, end, k, click_threshold=CLICK_THRESHOLD, exclude=None):
    agg = pull_state_aggregates(conn, start, end)
    if agg.empty:
        return None
    agg = compute_derived(agg)
    if exclude:
        agg = agg[~agg["state"].isin(exclude)]
    elig = eligible_pool(agg, click_threshold=click_threshold)
    if len(elig) < k:
        return None
    res, _ = apply_all_models(elig, k)
    return res


def build_stability_table(r12, r8, r4):
    rows = []
    states = sorted(set(r12["state"]))
    for r in (r8, r4):
        if r is not None:
            states = sorted(set(states) | set(r["state"]))
    i12 = r12.set_index("state") if r12 is not None else None
    i8 = r8.set_index("state") if r8 is not None else None
    i4 = r4.set_index("state") if r4 is not None else None

    def rank(t):
        if t is None or (isinstance(t, float) and np.isnan(t)):
            return None
        try:
            return TIER_LABEL_BASE.index(t)
        except ValueError:
            return None

    for st in states:
        for m in MODEL_NAMES:
            t12 = i12.loc[st, m] if (i12 is not None and st in i12.index) else None
            t8 = i8.loc[st, m] if (i8 is not None and st in i8.index) else None
            t4 = i4.loc[st, m] if (i4 is not None and st in i4.index) else None
            present = [t for t in (t12, t8, t4) if t is not None and not (isinstance(t, float) and pd.isna(t))]
            nu = len(set(present)) if present else 0
            rk = [rank(t) for t in (t12, t8, t4)]
            valid = [r for r in rk if r is not None]
            if not valid:
                d = "no_data"
            elif nu == 1:
                d = "stable"
            elif len(valid) < 3:
                d = "partial"
            elif rk[2] < rk[0]:
                d = "drifting_up"
            elif rk[2] > rk[0]:
                d = "drifting_down"
            else:
                d = "volatile"
            rows.append({"state": st, "model": m, "tier_12w": t12, "tier_8w": t8,
                         "tier_4w": t4, "stability_score": nu, "direction": d})
    return pd.DataFrame(rows)


def assign_below_threshold(all_states, eligible_result, headline, click_threshold):
    if eligible_result is None or eligible_result.empty:
        return pd.DataFrame()
    below = all_states[
        (all_states["clicks"] < click_threshold)
        & all_states["rpl"].notna() & (all_states["rpl"] > 0)
        & (all_states["state"].str.upper() != "UNKNOWN")
    ].copy()
    if below.empty:
        return pd.DataFrame()
    cent = eligible_result.groupby(headline)[FEATURE].mean()
    below["assigned_tier_nearest"] = below[FEATURE].apply(lambda t: (cent - t).abs().idxmin())
    cols = ["state", "clicks", "conversions", "spend", "rpl", "cpc", "cpl", "gp_per_lead",
            "roas", "assigned_tier_nearest"]
    cols = [c for c in cols if c in below.columns]
    return below[cols].sort_values(FEATURE, ascending=False).reset_index(drop=True)


def elbow_sweep(elig, k_values, out_png: Path | None):
    rows = []
    for k in k_values:
        if len(elig) < k:
            continue
        res, _ = apply_all_models(elig, k)
        m = model_summary_metrics(res, "C2_weighted_kmeans")
        rows.append({"k": k, "spend_weighted_bid_loss": m["spend_weighted_bid_loss"],
                     "avg_within_tier_feature_stdev": m["avg_within_tier_feature_stdev"]})
    df = pd.DataFrame(rows)
    if out_png is not None and not df.empty:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(7, 4.2))
            ax.plot(df["k"], df["spend_weighted_bid_loss"], "o-", color="#305496", lw=2)
            ax.set_xlabel("k (number of tiers)"); ax.set_ylabel(f"spend-weighted bid-loss on {FEATURE_LABEL}")
            ax.set_title(f"Elbow — within-tier {FEATURE_LABEL} dispersion vs k")
            ax.grid(alpha=0.3)
            for _, r in df.iterrows():
                ax.annotate(f"{int(r['k'])}", (r["k"], r["spend_weighted_bid_loss"]),
                            textcoords="offset points", xytext=(0, 8), ha="center", fontsize=8)
            fig.tight_layout(); fig.savefig(out_png, dpi=150); plt.close(fig)
        except Exception as e:
            print(f"  (elbow chart skipped: {e})", file=sys.stderr)
    return df


# ----------------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------------

def _write_df(ws, df, header_row=1):
    from openpyxl.styles import Font, PatternFill
    hf = Font(bold=True, color="FFFFFF"); hfill = PatternFill("solid", fgColor="305496")
    money = {"spend", "tier_spend", "revenue"}
    twod = {"rpl", "cpc", "cpl", "gp_per_lead", "roas", "ideal_tcpa", "wtd_gp_per_lead",
            "wtd_rpl", "wtd_cpc", "wtd_cpl", "wtd_roas", "tier_target_tcpa_analytical",
            "feat_min", "feat_max", "feat_range", "feat_stdev", "avg_within_tier_feature_stdev"}
    cols = list(df.columns)
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=header_row, column=j, value=str(c)); cell.font = hf; cell.fill = hfill
    for i, (_, row) in enumerate(df.iterrows(), header_row + 1):
        for j, c in enumerate(cols, 1):
            v = row[c]
            if isinstance(v, np.floating):
                v = float(v)
            elif isinstance(v, np.integer):
                v = int(v)
            if isinstance(v, float) and pd.isna(v):
                v = None
            cell = ws.cell(row=i, column=j, value=v)
            if c in money:
                cell.number_format = '"$"#,##0'
            elif c == "spend_weighted_bid_loss":
                cell.number_format = "#,##0"
            elif c in twod:
                cell.number_format = "0.00"
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2).coordinate
    return header_row + len(df)


def write_workbook(path, windows, k, rpl_blend, click_threshold, feature, result_12w, headline,
                   tier_metrics, model_compare, stability, below, current_map, ledger_cmp,
                   elbow_df, elbow_png, excluded):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    TITLE = Font(bold=True, size=14, color="305496"); NOTE = Font(italic=True, color="595959", size=9)
    wb = Workbook(); wb.remove(wb.active)

    ws = wb.create_sheet("Proposed Tiers")
    ws["A1"] = f"Google Home geo tiers — {headline} on {FEATURE_LABEL}, k={k}"; ws["A1"].font = TITLE
    ws["A2"] = (f"Window {windows.w12_start}..{windows.end_date} (12w). RPL blend={rpl_blend:.2f}. "
                f"Eligibility >= {click_threshold} clicks. TIER ONE = best {FEATURE_LABEL}. "
                f"Excluded (standalone campaigns): {', '.join(excluded) if excluded else 'none'}. "
                f"Per-tier profile (wtd_cpc / wtd_rpl / wtd_gp_per_lead / wtd_roas) maps each tier "
                f"to a margin archetype. tier_target_tcpa_analytical = wtd RPL / {TARGET_ROAS} (reference, not a bid).")
    ws["A2"].font = NOTE
    _write_df(ws, tier_metrics, header_row=4)

    ws = wb.create_sheet("State Assignments")
    ws["A1"] = "Per-state assignments — all models + current campaign"; ws["A1"].font = TITLE
    ws["A2"] = f"Headline = {headline} on {FEATURE_LABEL}. gp_per_lead = rpl - cpl; roas = revenue/spend."
    ws["A2"].font = NOTE
    assign = result_12w.copy()
    base = ["state"]
    if current_map:
        assign["current_tier"] = assign["state"].map(current_map); base.append("current_tier")
    cols = base + ["clicks", "conversions", "spend", "rpl", "cpc", "cpl", "gp_per_lead", "roas"] + MODEL_NAMES
    cols = [c for c in cols if c in assign.columns]
    _write_df(ws, assign[cols].sort_values(FEATURE, ascending=False).reset_index(drop=True), header_row=4)

    ws = wb.create_sheet("Model Comparison")
    ws["A1"] = f"Model comparison (lower bid-loss / stdev on {FEATURE_LABEL} = tighter tiers)"; ws["A1"].font = TITLE
    _write_df(ws, model_compare, header_row=3)

    if elbow_df is not None and not elbow_df.empty:
        ws = wb.create_sheet("Elbow")
        ws["A1"] = f"Elbow — within-tier {FEATURE_LABEL} dispersion vs k (C2)"; ws["A1"].font = TITLE
        ws["A2"] = "Pick k where bid-loss stops dropping steeply. Lower = tighter tiers."; ws["A2"].font = NOTE
        _write_df(ws, elbow_df, header_row=4)
        if elbow_png and Path(elbow_png).exists():
            from openpyxl.drawing.image import Image as XLImage
            ws.add_image(XLImage(str(elbow_png)), "F4")

    ws = wb.create_sheet("Stability (12w_8w_4w)")
    ws["A1"] = "Tier stability across 12w / 8w / 4w windows"; ws["A1"].font = TITLE
    ws["A2"] = "stability_score = # distinct tiers across windows (1 = rock-stable)."; ws["A2"].font = NOTE
    st = stability[stability["model"] == headline].drop(columns=["model"]) if not stability.empty else stability
    _write_df(ws, st.reset_index(drop=True), header_row=4)

    if below is not None and not below.empty:
        ws = wb.create_sheet("Below Threshold")
        ws["A1"] = f"States below {click_threshold} clicks — nearest-tier on {FEATURE_LABEL}"; ws["A1"].font = TITLE
        _write_df(ws, below, header_row=3)

    if ledger_cmp is not None and not ledger_cmp.empty:
        ws = wb.create_sheet("Ledger Comparison")
        ws["A1"] = "Proposed tier vs committed ledger"; ws["A1"].font = TITLE
        _write_df(ws, ledger_cmp, header_row=3)

    ws = wb.create_sheet("Methodology")
    notes = [
        ("Google Home geo-tiering — methodology", True), ("", False),
        (f"Clustering feature: {feature} ({FEATURE_LABEL}). TIER ONE = best {FEATURE_LABEL}.", False),
        ("Models: A2 quantile, B1 k-means(feature,cpc), B4 Ward(feature,cpc,log_clicks),", False),
        ("C2 weighted k-means on the feature (sample_weight=spend) = headline.", False),
        ("RPL blend mixes 4w into 12w per --rpl-blend, then gp_per_lead is re-derived.", False),
        ("", False),
        ("gp_per_lead = RPL - CPL (margin per lead): encodes the current CPC x lead-value tier", False),
        ("profile (Tier 1 cheap/efficient/positive-margin; Tier 2 premium/high-value/thin-margin;", False),
        ("Tier 3 volatile mid-pack; Tier 4 break-even/volume) in one number.", False),
        ("", False),
        ("Google data: account_id=1; campaign_performance_geo_daily_search (sum device rows; native conv,", False),
        ("~10-13% conservative on $); search-only; single-state campaigns excluded from the pool.", False),
        ("tROAS account: tier_target_tcpa_analytical is reference only; the output is tier membership.", False),
    ]
    for r, (t, h) in enumerate(notes, 1):
        ws.cell(row=r, column=1, value=t).font = TITLE if h else NOTE
    ws.column_dimensions["A"].width = 112

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def load_ledger(path):
    try:
        import yaml
    except ImportError:
        return {}
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    root = data.get("google_home_geo_tiers", data)
    return dict(root.get("commitments", {}))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv=None):
    global FEATURE, FEATURE_LABEL
    p = argparse.ArgumentParser()
    p.add_argument("--end-date", default=None)
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument("--rpl-blend", type=float, default=0.0)
    p.add_argument("--click-threshold", type=int, default=CLICK_THRESHOLD)
    p.add_argument("--cluster-feature", choices=["gp_per_lead", "roas", "rpl"], default="gp_per_lead")
    p.add_argument("--exclude-states", default=",".join(DEFAULT_EXCLUDE),
                   help="Comma-separated states to drop from the pool (standalone campaigns). "
                        "Pass '' to include all.")
    p.add_argument("--elbow", action="store_true", help="Sweep k and write an elbow chart.")
    p.add_argument("--k-range", default="3,4,5,6,7,8,9,10")
    p.add_argument("--headline-model", default="C2_weighted_kmeans")
    p.add_argument("--db-path", default=DEFAULT_DB_PATH)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--ledger", default=None)
    args = p.parse_args(argv)

    FEATURE = args.cluster_feature
    FEATURE_LABEL = {"gp_per_lead": "GP/lead", "roas": "ROAS", "rpl": "RPL"}[FEATURE]
    excluded = [s.strip() for s in args.exclude_states.split(",") if s.strip()]

    db = Path(args.db_path)
    if not db.exists():
        print(f"ERROR: DB not found: {db}", file=sys.stderr); return 1
    conn = sqlite3.connect(str(db))
    end_date = resolve_end_date(conn, args.end_date)
    windows = build_windows(end_date)
    k = args.k
    print("GOOGLE HOME GEO-TIERING")
    print(f"  feature={FEATURE} ({FEATURE_LABEL})  end={end_date}  k={k}  rpl_blend={args.rpl_blend}  "
          f"click_threshold={args.click_threshold}")
    print(f"  excluded (standalone): {excluded or 'none'}")

    raw12 = pull_state_aggregates(conn, windows.w12_start, windows.end_date)
    if raw12.empty:
        print(f"ERROR: no rows in {GEO_TABLE} for 12w.", file=sys.stderr); return 1
    raw4 = None
    if args.rpl_blend > 0.0:
        raw4 = pull_state_aggregates(conn, windows.w4_start, windows.end_date)
        raw4 = compute_derived(raw4) if not raw4.empty else None

    alls = compute_derived(raw12)
    if args.rpl_blend > 0.0:
        alls = apply_rpl_blend(alls, raw4, args.rpl_blend)
    if excluded:
        alls = alls[~alls["state"].isin(excluded)].reset_index(drop=True)

    elig = eligible_pool(alls, click_threshold=args.click_threshold)
    print(f"  eligible states: {len(elig)} of {len(alls)} (after exclusions)")
    if len(elig) < k:
        print(f"ERROR: eligible pool ({len(elig)}) < k ({k}).", file=sys.stderr); return 1

    result12, _ = apply_all_models(elig, k)
    r8 = run_models_for_window(conn, windows.w8_start, windows.end_date, k, 75, excluded)
    r4 = run_models_for_window(conn, windows.w4_start, windows.end_date, k, 75, excluded)
    stability = build_stability_table(result12, r8, r4)

    headline = args.headline_model
    tier_metrics = tier_aggregate_metrics(result12, headline)
    model_compare = pd.DataFrame([model_summary_metrics(result12, m) for m in MODEL_NAMES])
    below = assign_below_threshold(alls, result12, headline, args.click_threshold)
    current_map = pull_state_to_current_tier(conn, windows.w12_start, windows.end_date)

    elbow_df = None; elbow_png = None
    if args.elbow:
        kv = [int(x) for x in args.k_range.split(",")]
        elbow_png = Path(args.output_dir) / f"google_geo_elbow_{FEATURE}_{end_date}.png"
        elbow_df = elbow_sweep(elig, kv, elbow_png)

    ledger_cmp = None
    commit = load_ledger(Path(args.ledger)) if args.ledger else {}
    if commit:
        prop = dict(zip(result12["state"], result12[headline]))
        ledger_cmp = pd.DataFrame([
            {"state": s, "proposed_tier": prop.get(s), "committed_tier": commit.get(s),
             "agreement": "SAME" if prop.get(s) == commit.get(s) else "MOVED"}
            for s in sorted(set(prop) | set(commit))])

    conn.close()
    print(f"\n  Proposed tiers ({FEATURE_LABEL}):")
    for _, r in tier_metrics.iterrows():
        print(f"    {r['tier']:12} n={int(r['n_states']):2}  {FEATURE_LABEL} "
              f"{r['feat_min']:.0f}..{r['feat_max']:.0f}  wtd_cpc ${r['wtd_cpc']:.0f}  "
              f"wtd_rpl ${r['wtd_rpl']:.0f}  wtd_roas {r['wtd_roas']:.2f}")
        print(f"                 {r['states']}")
    if elbow_df is not None and not elbow_df.empty:
        print("\n  Elbow (k -> bid-loss):")
        for _, r in elbow_df.iterrows():
            print(f"    k={int(r['k'])}: {r['spend_weighted_bid_loss']:,.0f}")

    out = Path(args.output_dir) / f"google_geo_model_{FEATURE}_{end_date}.xlsx"
    write_workbook(out, windows, k, args.rpl_blend, args.click_threshold, FEATURE, result12,
                   headline, tier_metrics, model_compare, stability, below, current_map,
                   ledger_cmp, elbow_df, elbow_png, excluded)
    print(f"\n  Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
