"""
tier_logic_full_pass.py
========================
Account-wide tier similarity + GP impact analysis for Home Insurance
(account_id = 1).

DUAL-PASS METHODOLOGY: Two parallel similarity analyses are run, and
disagreements between them are surfaced as the "challenge the tiering
logic" output.

  Method A — FRAMEWORK SIMILARITY
    Metrics: cpc, value_per_conv, gmpl_proxy
    Question: "Is each state in the right tier per the team's rules?"
    Mismatches indicate drift from the framework definition.

  Method B — PERFORMANCE SIMILARITY  (used for sizing per user instruction)
    Metrics: cpa, roas, ctr, imp_per_day
    Question: "Are similarly-behaving states actually grouped together?"
    Mismatches indicate the framework may be putting structurally-different
    states together — a real challenge to the tiering logic.

For each state in each tier:
  1. Compute z-score profile using pooled stats across all candidate
     states (one pass per method, with that method's metric set).
  2. Compute Euclidean distance from the state's z-vector to each tier's
     centroid (centroid built using non-self members for fair scoring).
  3. Flag as MISMATCH if recommended-tier distance < 0.7× current-tier
     distance (filters closer-by-a-hair noise).
  4. For each mismatch, project GP impact under 5 scenarios from the
     UT/SD workbook (optimistic / neutral / pessimistic / worst / learning).
  5. Apply HOLD guardrail: if neutral GP delta ≤ 0, action = HOLD.

OUTPUT — one workbook (tier_logic_full_pass.xlsx):
  Metadata                     — both methods explained, parameters
  Sizing Summary               — unified GP/Month | Current GP/Day | Expected GP Lift
                                  (sourced from the Performance method)
  Method Disagreement          — states where the two methods give
                                  different recommendations (the most
                                  interesting territory for challenging
                                  the framework)
  State Profiles — Framework   — full per-state results (CPC/V/C/GMPL axes)
  State Profiles — Performance — full per-state results (CPA/ROAS/CTR/Imp/d axes)
  GP Impact Projection         — 5 scenarios per Performance-method mover
  Tier Centroids               — each tier's averages + members
  Dropped Profiles             — dedup transparency (stubs, low-spend rows)
  Diagnostic                   — counts + parameter audit

CONVENTIONS
───────────
  - Same DB_PATH, ACCOUNT_ID, MIN_DAYS_ACTIVE_FOR_CENTROID as
    geo_restructure_home.py.
  - Each tier's centroid is computed using NON-self members for that
    tier (when scoring State X in Tier 1, Tier 1's centroid is built
    from all Tier 1 states except X).
  - States are deduped: if a state appears in multiple tier campaigns
    (mid-restructure stubs), keep the row with the longest days_active
    and drop the rest. Below MIN_SPEND_FOR_ANALYSIS rows are dropped.
  - GP impact scenarios use weekly-rate baseline derived from the
    --days analysis window.

USAGE
─────
  conda activate paid-search
  python analysis/tier_logic_full_pass.py
  python analysis/tier_logic_full_pass.py --end-date 2026-04-30 --days 90
  python analysis/tier_logic_full_pass.py --dry-run
"""

import sqlite3
import argparse
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\google-geo-analysis\output")
ACCOUNT_ID = 1

TIER_KEYWORDS = ["TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR"]

# Min days a state must have been active in a tier campaign to count
# toward centroid calculation. Below this we still report the state but
# exclude it from the centroid math (insufficient history).
MIN_DAYS_ACTIVE_FOR_CENTROID = 30

# Minimum spend in the window for a state to be analyzed at all. Filters
# out near-empty rows (e.g. stubs from mid-restructure where a state
# briefly appeared in a tier and produced almost no performance).
MIN_SPEND_FOR_ANALYSIS = 100.0

# Two parallel similarity methods. Both are run; the workbook surfaces
# where they agree and disagree.
#
#   FRAMEWORK_METRICS — the axes the cheat sheet defines tiers by.
#     This answers: "Is each state in the right tier per the team's rules?"
#     Mismatches = the framework definition being broken (drift from rules).
#
#   PERFORMANCE_METRICS — observed performance characteristics.
#     This answers: "Are similarly-behaving states actually grouped together?"
#     Mismatches = the framework definition might be wrong for that state.
#
# Where they disagree is where the tiering logic is interesting to challenge.
FRAMEWORK_METRICS   = ["cpc", "value_per_conv", "gmpl_proxy"]
PERFORMANCE_METRICS = ["cpa", "roas", "ctr", "imp_per_day"]

# Default for legacy callers (used when no method is specified)
SIMILARITY_METRICS = FRAMEWORK_METRICS

# Mismatch threshold — a state is flagged as a "real" mismatch only if
# the distance to its recommended tier is < this fraction of the distance
# to its current tier. Filters out closer-by-a-hair noise mismatches.
MISMATCH_RATIO_THRESHOLD = 0.70

# tROAS targets per tier — sourced from Google-Home-Tier-Logic-CheatSheet.docx.
# Tier 2 has a softer target (1.10) to allow higher-CPC states to clear;
# all others are 1.19. These drive GP scenario projection.
TIER_TROAS = {
    "TIER ONE":   1.19,
    "TIER TWO":   1.10,
    "TIER THREE": 1.19,
    "TIER FOUR":  1.19,
}

# GP scenario configuration (matches UT/SD workbook scenarios)
SCENARIOS = [
    # (label, volume_delta, roas_achieved_factor)
    # roas_achieved_factor: 1.0 = hits target tROAS exactly;
    # 0.92 (1.10/1.19 - i.e. tier 2 ROAS-like undershoot) for "worst case";
    # 0.80 (~0.95/1.19) for the learning-period dip
    ("Optimistic: volume holds, ROAS hits tROAS",         0.00,  1.00),
    ("Neutral: -15% volume, ROAS hits tROAS",            -0.15,  1.00),
    ("Pessimistic: -30% volume, ROAS hits tROAS",        -0.30,  1.00),
    ("Worst case: -30% volume, ROAS undershoots to ~0.92", -0.30, 0.92),
    ("Learning-period dip: -20% volume, ROAS 0.95",      -0.20,  0.798),  # ~0.95/1.19
]

# Excluded campaign types
EXCLUDED_CAMPAIGN_CLASSES = {"PMAX"}


# ── Helpers ───────────────────────────────────────────────────────
def get_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def tier_campaign_id_map(conn):
    """Resolve the 4 tier campaign IDs from the campaigns table."""
    out = {}
    for tier_kw in TIER_KEYWORDS:
        sql = """
            SELECT campaign_id, campaign_name
              FROM campaigns
             WHERE account_id = ?
               AND UPPER(campaign_name) LIKE ?
               AND status = 'ENABLED'
        """
        rows = conn.execute(sql, (ACCOUNT_ID, f"%{tier_kw}%")).fetchall()
        if not rows:
            print(f"  WARNING: no campaign found matching '{tier_kw}'")
            continue
        # If multiple match, pick the one whose name has just the tier
        # keyword (filter Home Insurance tier campaigns specifically)
        chosen = rows[0]
        for r in rows:
            if "HOME INSURANCE" in r["campaign_name"].upper():
                chosen = r
                break
        out[tier_kw] = {"id": chosen["campaign_id"], "name": chosen["campaign_name"]}
    return out


def fetch_state_profiles(conn, tier_ids, start_date, end_date):
    """
    Per-state profile within each tier campaign over the analysis window.
    Returns list of dicts with derived metrics.
    """
    if not tier_ids:
        return []
    placeholders = ",".join("?" * len(tier_ids))
    sql = f"""
        SELECT
            c.campaign_id,
            c.campaign_name,
            cpg.geo_state,
            COUNT(DISTINCT cpg.date)           AS days_active,
            SUM(cpg.impressions)               AS impressions,
            SUM(cpg.clicks)                    AS clicks,
            SUM(cpg.cost_micros) / 1e6         AS spend,
            SUM(cpg.conversions)               AS conversions,
            SUM(cpg.conversion_value)          AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND cpg.campaign_id IN ({placeholders})
          AND cpg.date BETWEEN ? AND ?
          AND cpg.geo_state IS NOT NULL
        GROUP BY c.campaign_id, c.campaign_name, cpg.geo_state
    """
    params = [ACCOUNT_ID] + tier_ids + [start_date.isoformat(), end_date.isoformat()]
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    for r in rows:
        imp = r["impressions"] or 0
        clk = r["clicks"] or 0
        sp  = r["spend"] or 0.0
        cv  = r["conversions"] or 0.0
        rv  = r["revenue"] or 0.0
        d   = r["days_active"] or 0
        r["ctr"]            = (clk / imp) if imp > 0 else None
        r["cpc"]            = (sp / clk) if clk > 0 else None
        r["cpa"]            = (sp / cv) if cv > 0 else None
        r["roas"]           = (rv / sp) if sp > 0 else None
        r["value_per_conv"] = (rv / cv) if cv > 0 else None
        r["imp_per_day"]    = (imp / d) if d > 0 else None
        r["gmpl_proxy"]     = ((rv - sp) / clk) if clk > 0 else None
        # Identify current tier from campaign name
        cn_upper = r["campaign_name"].upper()
        r["current_tier"] = None
        for tk in TIER_KEYWORDS:
            if tk in cn_upper:
                r["current_tier"] = tk
                break
    return rows


def dedupe_and_filter_profiles(profiles):
    """
    Resolve states that appear in multiple tier campaigns to ONE row each.

    A state can appear in multiple tiers because of mid-restructure stubs
    (e.g. SD briefly in Tier 4 with 11 days, then in Tier 2 with 88 days).
    The right interpretation is: keep the state's PRIMARY tier (longest
    days_active in the window) and drop the stubs.

    Also drops:
      - Profiles below MIN_SPEND_FOR_ANALYSIS (no real performance to score)
      - Profiles with no current_tier identified (defensive)

    Returns (kept_profiles, dropped_log) where dropped_log is a list of
    {state, tier, reason, days_active, spend} dicts for the Diagnostic sheet.
    """
    kept = []
    dropped = []

    # Group by state
    by_state = defaultdict(list)
    for p in profiles:
        if not p.get("current_tier"):
            dropped.append({
                "geo_state": p["geo_state"],
                "tier": p["campaign_name"],
                "reason": "no current_tier match",
                "days_active": p["days_active"],
                "spend": round(p.get("spend") or 0, 2),
            })
            continue
        by_state[p["geo_state"]].append(p)

    for state, rows in by_state.items():
        # Filter rows below the spend floor
        eligible = [r for r in rows if (r.get("spend") or 0) >= MIN_SPEND_FOR_ANALYSIS]
        below_floor = [r for r in rows if (r.get("spend") or 0) < MIN_SPEND_FOR_ANALYSIS]

        for r in below_floor:
            dropped.append({
                "geo_state": state,
                "tier": r["current_tier"],
                "reason": f"spend < ${MIN_SPEND_FOR_ANALYSIS:.0f} in window",
                "days_active": r["days_active"],
                "spend": round(r.get("spend") or 0, 2),
            })

        if not eligible:
            continue   # state has no eligible row → drop entirely

        # Pick primary: longest days_active. Tiebreak on highest spend.
        eligible.sort(key=lambda r: (r["days_active"] or 0, r.get("spend") or 0),
                      reverse=True)
        primary = eligible[0]
        kept.append(primary)

        # Log any non-primary rows as drops
        for r in eligible[1:]:
            dropped.append({
                "geo_state": state,
                "tier": r["current_tier"],
                "reason": f"non-primary tier (kept {primary['current_tier']} "
                          f"with {primary['days_active']}d)",
                "days_active": r["days_active"],
                "spend": round(r.get("spend") or 0, 2),
            })

    return kept, dropped


# ── Z-score similarity (account-wide) ─────────────────────────────
def compute_pool_stats(profiles, metrics, exclude_states=()):
    """
    Compute pooled mean/std per metric across ALL eligible state profiles
    (used for z-score normalization). Excludes states in exclude_states.
    """
    pool = [
        p for p in profiles
        if (p["days_active"] or 0) >= MIN_DAYS_ACTIVE_FOR_CENTROID
        and p["geo_state"] not in exclude_states
        and p["current_tier"] is not None
    ]
    pool_stats = {}
    for m in metrics:
        vals = [p[m] for p in pool if p[m] is not None]
        if vals:
            mu = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
            if sd == 0:
                sd = 1.0
        else:
            mu, sd = 0.0, 1.0
        pool_stats[m] = {"mu": mu, "sd": sd}
    return pool_stats, pool


def zvec(profile, pool_stats, metrics):
    out = []
    for m in metrics:
        v = profile.get(m)
        if v is None:
            out.append(None)
        else:
            s = pool_stats[m]
            out.append((v - s["mu"]) / s["sd"])
    return out


def compute_centroid_z(profiles_in_tier, pool_stats, metrics):
    """Average z-vector across the tier's member states."""
    mem_zs = [zvec(p, pool_stats, metrics) for p in profiles_in_tier]
    if not mem_zs:
        return None
    centroid = []
    for i in range(len(metrics)):
        vals = [z[i] for z in mem_zs if z[i] is not None]
        centroid.append(statistics.mean(vals) if vals else 0.0)
    return centroid


def euclidean(z1, z2):
    d = 0.0
    for a, b in zip(z1, z2):
        av = a if a is not None else 0.0
        bv = b if b is not None else 0.0
        d += (av - bv) ** 2
    return math.sqrt(d)


def score_all_states(profiles, metrics, method_label="similarity"):
    """
    For each state in each tier, compute distance to all 4 tier centroids
    using the supplied metric set. Each tier's centroid for State X is
    computed using all OTHER states in that tier (so X doesn't influence
    the centroid it's being scored against). Returns a list of dicts,
    one per state.

    `metrics` controls which similarity axes are used (FRAMEWORK_METRICS,
    PERFORMANCE_METRICS, or any subset of metric keys present on profile
    rows). `method_label` is just for logging/debugging.
    """
    # Group profiles by current tier
    by_tier = defaultdict(list)
    for p in profiles:
        if p["current_tier"]:
            by_tier[p["current_tier"]].append(p)

    results = []
    for state_profile in profiles:
        if state_profile["current_tier"] is None:
            continue

        # Common scaffolding for every result row — base profile fields and
        # a per-metric z-score map keyed by metric name (e.g. "z_cpc").
        z_keys = {f"z_{m}": None for m in metrics}

        if (state_profile["days_active"] or 0) < MIN_DAYS_ACTIVE_FOR_CENTROID:
            results.append({
                **{k: state_profile[k] for k in
                   ["geo_state", "current_tier", "days_active",
                    "spend", "revenue", "conversions", "clicks", "impressions",
                    "cpa", "roas", "ctr", "cpc", "value_per_conv",
                    "imp_per_day", "gmpl_proxy"]},
                **z_keys,
                "metrics_used":      ",".join(metrics),
                "dist_TIER ONE": None, "dist_TIER TWO": None,
                "dist_TIER THREE": None, "dist_TIER FOUR": None,
                "closest_tier": None,
                "recommended_tier": None,
                "mismatch_ratio": None,
                "is_mismatch": False,
                "exclusion_reason": f"days_active < {MIN_DAYS_ACTIVE_FOR_CENTROID}",
            })
            continue

        # Pool stats: all eligible states EXCEPT this one
        pool_stats, _ = compute_pool_stats(
            profiles, metrics, exclude_states=[state_profile["geo_state"]])
        z_target = zvec(state_profile, pool_stats, metrics)

        # For each tier, build centroid using all OTHER states currently in that tier
        scores = {}
        for tier_kw in TIER_KEYWORDS:
            members = [p for p in by_tier.get(tier_kw, [])
                       if p["geo_state"] != state_profile["geo_state"]
                       and (p["days_active"] or 0) >= MIN_DAYS_ACTIVE_FOR_CENTROID]
            if not members:
                scores[tier_kw] = None
                continue
            cz = compute_centroid_z(members, pool_stats, metrics)
            if cz is None:
                scores[tier_kw] = None
                continue
            scores[tier_kw] = euclidean(z_target, cz)

        valid_scores = {k: v for k, v in scores.items() if v is not None}
        recommended = min(valid_scores, key=valid_scores.get) if valid_scores else None

        # Apply meaningful-difference threshold (filters closer-by-a-hair
        # noise mismatches)
        current_dist     = valid_scores.get(state_profile["current_tier"])
        recommended_dist = valid_scores.get(recommended) if recommended else None
        is_mismatch = False
        mismatch_ratio = None
        if (recommended is not None
                and recommended != state_profile["current_tier"]
                and current_dist is not None
                and recommended_dist is not None
                and current_dist > 0):
            mismatch_ratio = recommended_dist / current_dist
            is_mismatch = mismatch_ratio < MISMATCH_RATIO_THRESHOLD

        operational_recommended = (recommended if is_mismatch
                                   else state_profile["current_tier"])

        # Populate per-metric z-scores
        for i, m in enumerate(metrics):
            z_keys[f"z_{m}"] = z_target[i]

        results.append({
            **{k: state_profile[k] for k in
               ["geo_state", "current_tier", "days_active",
                "spend", "revenue", "conversions", "clicks", "impressions",
                "cpa", "roas", "ctr", "cpc", "value_per_conv",
                "imp_per_day", "gmpl_proxy"]},
            **z_keys,
            "metrics_used":         ",".join(metrics),
            "dist_TIER ONE":        scores.get("TIER ONE"),
            "dist_TIER TWO":        scores.get("TIER TWO"),
            "dist_TIER THREE":      scores.get("TIER THREE"),
            "dist_TIER FOUR":       scores.get("TIER FOUR"),
            "closest_tier":         recommended,
            "recommended_tier":     operational_recommended,
            "mismatch_ratio":       mismatch_ratio,
            "is_mismatch":          is_mismatch,
            "exclusion_reason":     "",
        })
    return results


# ── GP impact projection ──────────────────────────────────────────
def project_gp_scenarios(state_profile, target_tier, window_days):
    """
    Project weekly GP for each scenario, given a state's current profile and
    a target tier. Mirrors the UT/SD workbook logic.

    Returns list of scenario dicts ready for the workbook.
    """
    # Convert window-period totals to weekly rates
    weeks_in_window = max(window_days / 7.0, 1.0)
    w_imp   = (state_profile["impressions"] or 0) / weeks_in_window
    w_clk   = (state_profile["clicks"]      or 0) / weeks_in_window
    w_spend = (state_profile["spend"]       or 0.0) / weeks_in_window
    w_rev   = (state_profile["revenue"]     or 0.0) / weeks_in_window
    w_conv  = (state_profile["conversions"] or 0.0) / weeks_in_window
    cur_gp  = w_rev - w_spend
    cur_roas = state_profile["roas"] or 0.0

    target_troas = TIER_TROAS.get(target_tier, 1.19)

    scenarios = []
    # Baseline row
    scenarios.append({
        "Scenario":          "BASELINE (current avg)",
        "Target Tier":       state_profile["current_tier"],
        "tROAS Target":      TIER_TROAS.get(state_profile["current_tier"], 1.10),
        "Volume Delta%":     0.0,
        "ROAS Achieved":     round(cur_roas, 3),
        "Weekly Impressions": int(round(w_imp)),
        "Weekly Clicks":     round(w_clk, 1),
        "Weekly Spend":      round(w_spend, 2),
        "Weekly Revenue":    round(w_rev, 2),
        "Weekly Conversions": round(w_conv, 1),
        "Weekly GP":         round(cur_gp, 2),
        "GP Delta vs Baseline": 0.0,
        "Notes":             f"Current state in {state_profile['current_tier']}",
    })

    for label, vol_delta, roas_factor in SCENARIOS:
        roas_achieved = target_troas * roas_factor
        new_imp   = w_imp * (1 + vol_delta)
        new_clk   = w_clk * (1 + vol_delta)
        new_spend = w_spend * (1 + vol_delta)
        new_conv  = w_conv * (1 + vol_delta)
        new_rev   = new_spend * roas_achieved
        new_gp    = new_rev - new_spend
        scenarios.append({
            "Scenario":          label,
            "Target Tier":       target_tier,
            "tROAS Target":      target_troas,
            "Volume Delta%":     vol_delta,
            "ROAS Achieved":     round(roas_achieved, 3),
            "Weekly Impressions": int(round(new_imp)),
            "Weekly Clicks":     round(new_clk, 1),
            "Weekly Spend":      round(new_spend, 2),
            "Weekly Revenue":    round(new_rev, 2),
            "Weekly Conversions": round(new_conv, 1),
            "Weekly GP":         round(new_gp, 2),
            "GP Delta vs Baseline": round(new_gp - cur_gp, 2),
            "Notes":             "",
        })
    return scenarios


def evaluate_action(scenarios):
    """
    Apply HOLD guardrail: if neutral-case GP delta is negative, recommend
    'Hold' even if similarity says move. Otherwise 'Move'.

    Returns dict with action, neutral_gp_delta, weekly_gp_delta_range.
    """
    # Find neutral-case row (label starts with "Neutral")
    neutral = next((s for s in scenarios if s["Scenario"].startswith("Neutral")), None)
    optimistic = next((s for s in scenarios if s["Scenario"].startswith("Optimistic")), None)
    pessimistic = next((s for s in scenarios if s["Scenario"].startswith("Pessimistic")), None)

    if not neutral:
        return {"action": "UNKNOWN", "neutral_gp_delta": 0,
                "weekly_gp_delta_low": 0, "weekly_gp_delta_high": 0}

    neutral_delta = neutral["GP Delta vs Baseline"]
    low  = pessimistic["GP Delta vs Baseline"] if pessimistic else neutral_delta
    high = optimistic["GP Delta vs Baseline"]  if optimistic  else neutral_delta

    if neutral_delta > 0:
        action = "MOVE"
    else:
        action = "HOLD (neutral case loses GP)"

    return {
        "action":             action,
        "neutral_gp_delta":   neutral_delta,
        "weekly_gp_delta_low":  low,
        "weekly_gp_delta_high": high,
    }


# ── Sheet builders ────────────────────────────────────────────────
def build_state_profiles_sheet(scored: list, eval_results: dict,
                                metrics: list) -> pd.DataFrame:
    """
    One row per state with profile + tier distances + action.

    `metrics` is the similarity metric set used for THIS scoring pass —
    z-score columns are emitted for those metrics.
    """
    # Pretty-name the z columns for the metrics we used
    zname = {
        "cpc": "z_CPC", "value_per_conv": "z_Value/Conv",
        "gmpl_proxy": "z_GMPL Proxy",
        "cpa": "z_CPA", "roas": "z_ROAS", "ctr": "z_CTR",
        "imp_per_day": "z_Imp/Day",
    }
    rows = []
    for s in scored:
        eval_for_state = eval_results.get(s["geo_state"], {})
        row = {
            "State":            s["geo_state"],
            "Current Tier":     s["current_tier"],
            "Days Active":      s["days_active"],
            "Spend":            round(s["spend"] or 0, 2),
            "Revenue":          round(s["revenue"] or 0, 2),
            "Conversions":      round(s["conversions"] or 0, 1),
            # All raw metrics shown for reference, regardless of which were used
            "CPC":              round(s["cpc"], 2)             if s["cpc"]            is not None else None,
            "Value/Conv":       round(s["value_per_conv"], 2)  if s["value_per_conv"] is not None else None,
            "GMPL Proxy":       round(s["gmpl_proxy"], 2)      if s["gmpl_proxy"]     is not None else None,
            "ROAS":             round(s["roas"], 3)            if s["roas"]           is not None else None,
            "CPA":              round(s["cpa"], 2)             if s["cpa"]            is not None else None,
            "CTR":              round(s["ctr"], 4)             if s["ctr"]            is not None else None,
            "Imp/Day":          round(s["imp_per_day"], 1)     if s["imp_per_day"]    is not None else None,
        }
        # Emit z-scores only for metrics actually used in this pass
        for m in metrics:
            label = zname.get(m, f"z_{m}")
            v = s.get(f"z_{m}")
            row[label] = round(v, 3) if v is not None else None
        row.update({
            "Dist Tier 1":      round(s["dist_TIER ONE"], 3)   if s["dist_TIER ONE"]   is not None else None,
            "Dist Tier 2":      round(s["dist_TIER TWO"], 3)   if s["dist_TIER TWO"]   is not None else None,
            "Dist Tier 3":      round(s["dist_TIER THREE"], 3) if s["dist_TIER THREE"] is not None else None,
            "Dist Tier 4":      round(s["dist_TIER FOUR"], 3)  if s["dist_TIER FOUR"]  is not None else None,
            "Closest Tier (raw)":                  s.get("closest_tier"),
            "Mismatch Ratio":                      round(s["mismatch_ratio"], 3)  if s.get("mismatch_ratio") is not None else None,
            "Recommended Tier (after threshold)":  s["recommended_tier"],
            "Is Mismatch":                         s["is_mismatch"],
            "Action":                              eval_for_state.get("action", ""),
            "Neutral GP Delta/wk":                 round(eval_for_state["neutral_gp_delta"], 2)
                                                    if "neutral_gp_delta" in eval_for_state else None,
            "Exclusion Reason":                    s["exclusion_reason"],
        })
        rows.append(row)
    df = pd.DataFrame(rows)
    if "Is Mismatch" in df.columns:
        df = df.sort_values(["Is Mismatch", "Neutral GP Delta/wk"],
                            ascending=[False, False],
                            na_position="last").reset_index(drop=True)
    return df


def build_gp_projection_sheet(all_scenarios: list) -> pd.DataFrame:
    """Stack of GP impact projections — flat across all move-candidate states."""
    if not all_scenarios:
        return pd.DataFrame([{"Note": "No move candidates found"}])
    df = pd.DataFrame(all_scenarios)
    # Reorder columns to match UT/SD workbook
    col_order = ["State", "Scenario", "Target Tier", "tROAS Target",
                 "Volume Delta%", "ROAS Achieved",
                 "Weekly Impressions", "Weekly Clicks", "Weekly Spend",
                 "Weekly Revenue", "Weekly Conversions", "Weekly GP",
                 "GP Delta vs Baseline", "Notes"]
    df = df[[c for c in col_order if c in df.columns]]
    return df


def build_disagreement_sheet(scored_framework: list, scored_performance: list,
                              eval_framework: dict,
                              eval_performance: dict) -> pd.DataFrame:
    """
    Join the two methods' per-state results and surface where they disagree.

    Disagreement types:
      A) Framework=move,  Performance=hold      → framework rules say drift,
                                                   but the state still behaves
                                                   like its tier-mates
      B) Framework=hold,  Performance=move      → state matches its tier
                                                   definition but performs
                                                   differently from tier-mates
      C) Both=move, different target tiers      → both see drift but disagree
                                                   on destination
      D) Both=move, same target tier            → high-confidence move
                                                   (NOT a disagreement; flagged
                                                   for visibility but excluded
                                                   from the disagreement count)
      E) Both=hold                              → no action; not surfaced here
    """
    fw_by_state = {s["geo_state"]: s for s in scored_framework}
    pf_by_state = {s["geo_state"]: s for s in scored_performance}

    rows = []
    all_states = sorted(set(fw_by_state) | set(pf_by_state))
    for st in all_states:
        fw = fw_by_state.get(st, {})
        pf = pf_by_state.get(st, {})
        fw_action = (eval_framework.get(st, {}).get("action") or
                     ("MOVE" if fw.get("is_mismatch") else "HOLD"))
        pf_action = (eval_performance.get(st, {}).get("action") or
                     ("MOVE" if pf.get("is_mismatch") else "HOLD"))
        fw_target = fw.get("recommended_tier")
        pf_target = pf.get("recommended_tier")

        fw_is_move = fw_action == "MOVE"
        pf_is_move = pf_action == "MOVE"

        # Skip the both-hold case — nothing to surface
        if not fw_is_move and not pf_is_move:
            continue

        # Classify
        if fw_is_move and not pf_is_move:
            kind = "A: framework=move, performance=hold"
        elif not fw_is_move and pf_is_move:
            kind = "B: framework=hold, performance=move"
        elif fw_is_move and pf_is_move:
            if fw_target == pf_target:
                kind = "D: both=move, same target (HIGH-CONFIDENCE)"
            else:
                kind = "C: both=move, different targets"
        else:
            kind = "?"

        rows.append({
            "State":               st,
            "Current Tier":        fw.get("current_tier") or pf.get("current_tier"),
            "Disagreement Type":   kind,
            "Framework Action":    fw_action,
            "Framework Target":    fw_target,
            "Framework GP Δ/wk":
                round(eval_framework.get(st, {}).get("neutral_gp_delta", 0), 2)
                if eval_framework.get(st) else None,
            "Performance Action":  pf_action,
            "Performance Target":  pf_target,
            "Performance GP Δ/wk":
                round(eval_performance.get(st, {}).get("neutral_gp_delta", 0), 2)
                if eval_performance.get(st) else None,
            "Note":                disagreement_interpretation(kind),
        })

    if not rows:
        return pd.DataFrame([{"Note": "No disagreements found — methods agreed on every state."}])

    # Sort: real disagreements (A/B/C) first, high-confidence (D) last
    df = pd.DataFrame(rows)
    df["_sort"] = df["Disagreement Type"].str[0].map(
        {"A": 0, "B": 1, "C": 2, "D": 3}).fillna(9)
    df = df.sort_values(["_sort", "State"]).drop(columns=["_sort"]).reset_index(drop=True)
    return df


def disagreement_interpretation(kind: str) -> str:
    """One-line interpretation per disagreement type."""
    return {
        "A: framework=move, performance=hold":
            "Framework definition has drifted for this state, but it still "
            "behaves like its tier-mates. Framework rules may be too strict.",
        "B: framework=hold, performance=move":
            "State matches its tier definition on framework axes, but "
            "performs differently from tier-mates. The framework may be "
            "putting structurally-different states together.",
        "C: both=move, different targets":
            "Both methods see drift but disagree on destination. Worth "
            "manual review — the methods are emphasizing different signals.",
        "D: both=move, same target (HIGH-CONFIDENCE)":
            "Both methods agree on move and destination. Strongest "
            "candidate for action.",
    }.get(kind, "")


def build_centroids_sheet(profiles: list) -> pd.DataFrame:
    """
    Each tier's centroid: member count + raw averages of FRAMEWORK metrics
    (CPC, Value/Conv, GMPL — the axes the cheat sheet uses to define tiers).
    Secondary metrics (CPA, ROAS, CTR, Imp/Day) shown for context but not
    used in similarity.
    """
    by_tier = defaultdict(list)
    for p in profiles:
        if p["current_tier"] and (p["days_active"] or 0) >= MIN_DAYS_ACTIVE_FOR_CENTROID:
            by_tier[p["current_tier"]].append(p)

    def avg(members, key):
        vals = [m[key] for m in members if m[key] is not None]
        return statistics.mean(vals) if vals else None

    rows = []
    for tier_kw in TIER_KEYWORDS:
        members = by_tier.get(tier_kw, [])
        if not members:
            rows.append({
                "Tier": tier_kw, "tROAS Target": TIER_TROAS.get(tier_kw),
                "Member Count": 0, "Members": "",
                "Avg CPC": None, "Avg Value/Conv": None, "Avg GMPL Proxy": None,
                "Avg ROAS": None, "Avg CPA": None, "Avg Imp/Day": None,
            })
            continue
        avg_cpc = avg(members, "cpc")
        avg_vpc = avg(members, "value_per_conv")
        avg_gmp = avg(members, "gmpl_proxy")
        rows.append({
            "Tier":             tier_kw,
            "tROAS Target":     TIER_TROAS.get(tier_kw),
            "Member Count":     len(members),
            "Members":          ", ".join(sorted(m["geo_state"] for m in members)),
            # FRAMEWORK metrics (used in similarity)
            "Avg CPC":          round(avg_cpc, 2) if avg_cpc is not None else None,
            "Avg Value/Conv":   round(avg_vpc, 2) if avg_vpc is not None else None,
            "Avg GMPL Proxy":   round(avg_gmp, 2) if avg_gmp is not None else None,
            # Secondary (context only)
            "Avg ROAS":         round(avg(members, "roas"), 3) if avg(members, "roas") is not None else None,
            "Avg CPA":          round(avg(members, "cpa"), 2) if avg(members, "cpa") is not None else None,
            "Avg Imp/Day":      round(avg(members, "imp_per_day"), 1) if avg(members, "imp_per_day") is not None else None,
        })
    return pd.DataFrame(rows)


def build_sizing_summary(scored: list, eval_results: dict) -> pd.DataFrame:
    """
    Unified GP/Month | Current GP/Day | Expected GP Lift schema, summing
    across MOVE-recommended states only.
    """
    rows = []

    def _row(initiative, scenario, current_gp_day, lift_day,
             lift_month, total_gp_day, total_gp_month, note=""):
        rows.append({
            "Initiative":                       initiative,
            "Scenario":                         scenario,
            "Current GP / Day ($)":             round(float(current_gp_day), 2),
            "Expected GP Lift / Day ($)":       round(float(lift_day), 2),
            "Expected GP Lift / Month ($)":     round(float(lift_month), 2),
            "Total GP / Day ($)":               round(float(total_gp_day), 2),
            "Total GP / Month ($)":             round(float(total_gp_month), 2),
            "Note":                             note,
        })

    # Aggregate over MOVE-recommended states only
    movers = [s for s in scored if s["is_mismatch"]
              and eval_results.get(s["geo_state"], {}).get("action") == "MOVE"]

    if not movers:
        rows.append({
            "Initiative": "Tier Logic Re-eval (full-pass)",
            "Scenario":   "No move candidates found",
            "Current GP / Day ($)": 0,
            "Expected GP Lift / Day ($)": 0,
            "Expected GP Lift / Month ($)": 0,
            "Total GP / Day ($)": 0,
            "Total GP / Month ($)": 0,
            "Note": "All states are correctly placed in their current tier "
                    "OR neutral-case GP delta is negative (HOLD).",
        })
        return pd.DataFrame(rows)

    # Sum weekly GP and lift across movers; convert to daily and monthly
    total_current_weekly_gp = sum(((m["revenue"] or 0) - (m["spend"] or 0))
                                   * 7 / max(scored_window_days(scored), 1)
                                   for m in movers)
    # Use eval_results for delta values (already weekly)
    total_low_lift_wk     = sum(eval_results[m["geo_state"]]["weekly_gp_delta_low"]
                                for m in movers)
    total_neutral_lift_wk = sum(eval_results[m["geo_state"]]["neutral_gp_delta"]
                                for m in movers)
    total_high_lift_wk    = sum(eval_results[m["geo_state"]]["weekly_gp_delta_high"]
                                for m in movers)

    cur_gp_day  = total_current_weekly_gp / 7
    cur_gp_mo   = total_current_weekly_gp * 30 / 7

    for label, weekly_lift in [
        ("Pessimistic-case  (sum across movers)", total_low_lift_wk),
        ("Neutral-case (HEADLINE; sum across movers)", total_neutral_lift_wk),
        ("Optimistic-case (sum across movers)", total_high_lift_wk),
    ]:
        lift_day = weekly_lift / 7
        lift_mo  = weekly_lift * 30 / 7
        _row(
            initiative="Tier Logic Re-eval (full-pass)",
            scenario=label,
            current_gp_day=cur_gp_day,
            lift_day=lift_day, lift_month=lift_mo,
            total_gp_day=cur_gp_day + lift_day,
            total_gp_month=cur_gp_mo + lift_mo,
            note=f"{len(movers)} move-recommended state(s): "
                 f"{', '.join(m['geo_state'] for m in movers)}",
        )

    return pd.DataFrame(rows)


def scored_window_days(scored: list) -> float:
    """Median days_active across scored states — used to convert window
    totals to weekly rates."""
    days = [s["days_active"] for s in scored
            if s["days_active"] and s["days_active"] > 0]
    return statistics.median(days) if days else 90.0


def build_metadata_sheet(end_date, days, n_states, n_movers,
                         tier_id_map: dict) -> pd.DataFrame:
    rows = [
        ("Run timestamp",          datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Window end date",        str(end_date)),
        ("Window days",            days),
        ("Account ID",             ACCOUNT_ID),
        ("Min days for centroid",  MIN_DAYS_ACTIVE_FOR_CENTROID),
        ("Min spend for analysis", f"${MIN_SPEND_FOR_ANALYSIS:.0f}"),
        ("Mismatch ratio threshold", MISMATCH_RATIO_THRESHOLD),
        ("", ""),
        ("─── Methodology ───", ""),
        ("Approach", "Dual-pass: TWO similarity analyses run in parallel"),
        ("", ""),
        ("Framework method (axes the cheat sheet defines tiers by)",
         ", ".join(FRAMEWORK_METRICS)),
        ("  Question answered",
         "Is each state in the right tier per the team's rules?"),
        ("  Mismatch interpretation",
         "Drift from the framework definition"),
        ("", ""),
        ("Performance method (axes that capture observed behavior)",
         ", ".join(PERFORMANCE_METRICS)),
        ("  Question answered",
         "Are similarly-behaving states actually grouped together?"),
        ("  Mismatch interpretation",
         "The framework may be putting structurally-different states together"),
        ("", ""),
        ("Sizing source",
         "Sizing Summary sheet uses PERFORMANCE method results "
         "(challenges the framework with observed behavior)"),
        ("Disagreement sheet",
         "States where the two methods give different recommendations — "
         "where the tiering logic is most interesting to challenge"),
        ("", ""),
        ("─── Tier campaigns ───", ""),
    ]
    for tier_kw, info in tier_id_map.items():
        rows.append((f"  {tier_kw}", f"{info['name']} (id={info['id']})"))
    rows.extend([
        ("", ""),
        ("States scored",                n_states),
        ("MOVE-recommended (Performance)", n_movers),
        ("HOLD guardrail",
         "Action = HOLD if neutral-case GP delta ≤ 0, even if similarity says move"),
        ("Sizing window",
         f"Weekly rate from {days}-day window → ÷7 for daily, × 30/7 for monthly"),
    ])
    return pd.DataFrame(rows, columns=["Parameter", "Value"])


# ── Workbook formatting ───────────────────────────────────────────
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
    parser.add_argument("--end-date", help="YYYY-MM-DD; defaults to yesterday")
    parser.add_argument("--days", type=int, default=90,
                        help="Analysis window length in days (default: 90)")
    parser.add_argument("--output",
                        default=str(OUTPUT_DIR / "tier_logic_full_pass.xlsx"),
                        help="Output workbook path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary, don't write workbook")
    args = parser.parse_args()

    t0 = time.time()
    end_date = (date.fromisoformat(args.end_date)
                if args.end_date else date.today() - timedelta(days=1))
    start_date = end_date - timedelta(days=args.days - 1)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Tier Logic Full-Pass Analysis ===")
    print(f"Window: {start_date} → {end_date} ({args.days} days)")
    print(f"Account: {ACCOUNT_ID}")

    conn = get_conn()
    tier_id_map = tier_campaign_id_map(conn)
    if not tier_id_map:
        print("ERROR: no tier campaigns found in DB. Aborting.")
        sys.exit(1)
    tier_ids = [v["id"] for v in tier_id_map.values()]
    print(f"  Resolved {len(tier_id_map)}/4 tier campaigns")

    print("\nFetching state profiles...")
    profiles_raw = fetch_state_profiles(conn, tier_ids, start_date, end_date)
    print(f"  Loaded {len(profiles_raw):,} raw (state, tier) profile rows")

    print("\nDeduping + filtering profiles...")
    profiles, dropped = dedupe_and_filter_profiles(profiles_raw)
    print(f"  Kept:    {len(profiles):,} primary state profiles")
    print(f"  Dropped: {len(dropped):,} (stubs, low spend, no current_tier)")

    # Helper closure to do scoring + GP projection + action evaluation for one method
    def run_method(label, metrics):
        print(f"\nScoring tier similarity ({label}; metrics={metrics})...")
        scored = score_all_states(profiles, metrics, method_label=label)
        n_eligible = sum(1 for s in scored if not s["exclusion_reason"])
        n_mismatched = sum(1 for s in scored if s["is_mismatch"])
        print(f"  Eligible for scoring: {n_eligible}/{len(scored)}")
        print(f"  Mismatch candidates:  {n_mismatched}")

        scenario_rows = []
        eval_results = {}
        for s in scored:
            if not s["is_mismatch"] or not s["recommended_tier"]:
                continue
            orig = next((p for p in profiles if p["geo_state"] == s["geo_state"]
                         and p["current_tier"] == s["current_tier"]), None)
            if not orig:
                continue
            scenarios = project_gp_scenarios(orig, s["recommended_tier"], args.days)
            eval_results[s["geo_state"]] = evaluate_action(scenarios)
            for sc in scenarios:
                sc["State"] = s["geo_state"]
                scenario_rows.append(sc)

        n_movers = sum(1 for v in eval_results.values() if v["action"] == "MOVE")
        n_holds  = sum(1 for v in eval_results.values() if v["action"].startswith("HOLD"))
        print(f"  Action=MOVE: {n_movers}")
        print(f"  Action=HOLD: {n_holds}")
        return {
            "label":          label,
            "metrics":        metrics,
            "scored":         scored,
            "scenarios":      scenario_rows,
            "eval_results":   eval_results,
            "n_eligible":     n_eligible,
            "n_mismatched":   n_mismatched,
            "n_movers":       n_movers,
            "n_holds":        n_holds,
        }

    # Run BOTH methods in parallel
    framework   = run_method("framework",   FRAMEWORK_METRICS)
    performance = run_method("performance", PERFORMANCE_METRICS)

    # Sizing summary uses PERFORMANCE results per user instruction
    # ("size what the current GP is and what the expected lift is using the V1 model")
    sizing_method = performance

    if args.dry_run:
        print("\n[dry-run] Skipping workbook write.")
        print("\n--- Framework movers ---")
        for s in framework["scored"]:
            if s["is_mismatch"]:
                ev = framework["eval_results"].get(s["geo_state"], {})
                print(f"  {s['geo_state']:18s} {s['current_tier']:11s} "
                      f"→ {s['recommended_tier']:11s} action={ev.get('action','?'):8s} "
                      f"GP/wk={ev.get('neutral_gp_delta','?')}")
        print("\n--- Performance movers ---")
        for s in performance["scored"]:
            if s["is_mismatch"]:
                ev = performance["eval_results"].get(s["geo_state"], {})
                print(f"  {s['geo_state']:18s} {s['current_tier']:11s} "
                      f"→ {s['recommended_tier']:11s} action={ev.get('action','?'):8s} "
                      f"GP/wk={ev.get('neutral_gp_delta','?')}")
        return

    # Build all sheets
    metadata = build_metadata_sheet(end_date, args.days,
                                    len(profiles),
                                    sizing_method["n_movers"], tier_id_map)

    sizing_sht = build_sizing_summary(sizing_method["scored"],
                                      sizing_method["eval_results"])

    state_profiles_fw_sht = build_state_profiles_sheet(
        framework["scored"], framework["eval_results"], FRAMEWORK_METRICS)
    state_profiles_pf_sht = build_state_profiles_sheet(
        performance["scored"], performance["eval_results"], PERFORMANCE_METRICS)

    disagreement_sht = build_disagreement_sheet(
        framework["scored"], performance["scored"],
        framework["eval_results"], performance["eval_results"])

    # GP impact projection — sized from PERFORMANCE method
    gp_proj_sht  = build_gp_projection_sheet(performance["scenarios"])

    centroids_sht = build_centroids_sheet(profiles)
    dropped_sht   = (pd.DataFrame(dropped) if dropped
                     else pd.DataFrame([{"Note": "No profiles dropped"}]))

    diagnostic = pd.DataFrame([
        {"Metric": "Raw profile rows",        "Value": len(profiles_raw)},
        {"Metric": "Dedup/filter dropped",    "Value": len(dropped)},
        {"Metric": "States kept after dedup", "Value": len(profiles)},
        {"Metric": "Mismatch ratio threshold","Value": MISMATCH_RATIO_THRESHOLD},
        {"Metric": "Min spend for analysis",  "Value": f"${MIN_SPEND_FOR_ANALYSIS:.0f}"},
        {"Metric": "─── Framework method ───", "Value": ""},
        {"Metric": "  Metrics",               "Value": ", ".join(FRAMEWORK_METRICS)},
        {"Metric": "  Mismatched",            "Value": framework["n_mismatched"]},
        {"Metric": "  MOVE recommendations",  "Value": framework["n_movers"]},
        {"Metric": "  HOLD (neutral negative)","Value": framework["n_holds"]},
        {"Metric": "─── Performance method (used for sizing) ───", "Value": ""},
        {"Metric": "  Metrics",               "Value": ", ".join(PERFORMANCE_METRICS)},
        {"Metric": "  Mismatched",            "Value": performance["n_mismatched"]},
        {"Metric": "  MOVE recommendations",  "Value": performance["n_movers"]},
        {"Metric": "  HOLD (neutral negative)","Value": performance["n_holds"]},
        {"Metric": "Wall time (seconds)",     "Value": round(time.time()-t0, 1)},
    ])

    # Sheet order designed for the read flow: summary → disagreements →
    # both methods' details → GP detail → centroids → diagnostics
    sheets = {
        "Metadata":                     metadata,
        "Sizing Summary":               sizing_sht,
        "Method Disagreement":          disagreement_sht,
        "State Profiles — Framework":   state_profiles_fw_sht,
        "State Profiles — Performance": state_profiles_pf_sht,
        "GP Impact Projection":         gp_proj_sht,
        "Tier Centroids":               centroids_sht,
        "Dropped Profiles":             dropped_sht,
        "Diagnostic":                   diagnostic,
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
