"""
tier_outliers_breakout_sizing.py
=================================
Sizes the Tier Outlier Breakout initiative for Home Insurance Google
(account_id = 1).

TWO SIZING QUESTIONS ANSWERED
─────────────────────────────
  Q1: Did the existing CA/TX/FL break-out (Jan 2026) work?
       → Pre/post comparison, with seasonality control via tier-resident states

  Q2: Which OTHER states should be broken out next?
       → Identify states with ≥25% of their tier's spend in the pre-breakout
         window. Project per-state uplift using the measured CA/TX/FL avg.

WINDOWS RUN IN PARALLEL (subject to data availability)
──────────────────────────────────────────────────────
  q4_vs_post : Q4 2025 (Oct-Dec) pre vs Q1+Q2 2026 post
                Cleaner statistical comparison; needs seasonality control
                because Q4 has holiday dynamics Q1 doesn't.
  yoy        : Jan-Apr 2025 vs Jan-Apr 2026 (same calendar window, 1yr apart)
                Neutralizes seasonality but bakes in account-level drift
                (budget changes, bid strategy changes, market shifts).

The script PROBES the geo_daily table first and reports which windows have
sufficient data. Missing windows are skipped gracefully with a clear note
in the metadata sheet.

OUTPUT — one workbook (tier_outliers_breakout_sizing.xlsx):
  Metadata
  Sizing Summary             — unified GP/Month | Current GP/Day | Expected GP Lift
  CA TX FL Pre Post           — measured per-state pre/post for each window
  Seasonality Control         — tier-resident states' delta in same windows
                                (used to net out the CA/TX/FL raw delta)
  New Candidates              — states with ≥25% tier-spend share, with
                                projected uplift from CA/TX/FL avg
  Data Availability           — what we found in the DB; what's missing
  Diagnostic                  — counts/stats

CONVENTIONS
───────────
  - DB_PATH, ACCOUNT_ID match other Home Insurance scripts
  - Pre-window: CA/TX/FL geo_daily rows attached to their PRE-breakout
    tier campaigns (resolved by name patterns)
  - Post-window: CA/TX/FL geo_daily rows attached to their CURRENT
    single-state campaigns
  - Outlier candidate threshold: 25% of pre-breakout tier total spend
  - Tier campaigns excluded from candidate set (we're looking for states
    INSIDE tier campaigns that should be broken out)

USAGE
─────
  conda activate paid-search
  python analysis/google-geo-analysis/tier_outliers_breakout_sizing.py
  python analysis/google-geo-analysis/tier_outliers_breakout_sizing.py --windows yoy
  python analysis/google-geo-analysis/tier_outliers_breakout_sizing.py --share-threshold 0.20
"""

import sqlite3
import argparse
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\google-geo-analysis\output")
ACCOUNT_ID = 1

# Single-state outlier campaigns. Pattern matches the campaign name,
# resolved at runtime to actual campaign_id.
OUTLIER_STATES = ["CALIFORNIA", "TEXAS", "FLORIDA"]
OUTLIER_STATE_DISPLAY = {"CALIFORNIA": "California", "TEXAS": "Texas",
                          "FLORIDA": "Florida"}

TIER_KEYWORDS = ["TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR"]

# Outlier breakout was Jan 2026 (per user)
BREAKOUT_DATE = date(2026, 1, 1)

# Window definitions
WINDOWS = {
    "q4_vs_post": {
        "pre_start":  date(2025, 10, 1),
        "pre_end":    date(2025, 12, 31),
        "post_start": date(2026, 1, 1),
        "post_end":   date(2026, 4, 30),  # post window cap
        "label":      "Q4 2025 vs Q1-Q2 2026",
        "needs_seasonality_control": True,
    },
    "yoy": {
        "pre_start":  date(2025, 1, 1),
        "pre_end":    date(2025, 4, 30),
        "post_start": date(2026, 1, 1),
        "post_end":   date(2026, 4, 30),
        "label":      "Jan-Apr 2025 vs Jan-Apr 2026 (YoY)",
        "needs_seasonality_control": False,
    },
}

# Default outlier candidate threshold
DEFAULT_SHARE_THRESHOLD = 0.25

# Minimum daily conversions for Smart Bidding viability of a standalone
# campaign. Used as a guideline flag, not a hard filter.
SMART_BIDDING_DAILY_CONV_FLOOR = 1.0


# ── Helpers ────────────────────────────────────────────────────────
def get_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def resolve_campaigns(conn):
    """
    Returns dict with keys:
      'tiers': {'TIER ONE': {'id', 'name'}, ...}
      'outliers': {'CALIFORNIA': {'id', 'name'}, ...}
    """
    out = {"tiers": {}, "outliers": {}}

    # Tier campaigns (current state)
    for tier_kw in TIER_KEYWORDS:
        sql = """
            SELECT campaign_id, campaign_name, status
              FROM campaigns
             WHERE account_id = ?
               AND UPPER(campaign_name) LIKE ?
        """
        rows = conn.execute(sql, (ACCOUNT_ID, f"%{tier_kw}%")).fetchall()
        # Filter to home insurance
        rows = [r for r in rows if "HOME INSURANCE" in r["campaign_name"].upper()]
        if not rows:
            print(f"  WARNING: no campaign for {tier_kw}")
            continue
        # Prefer ENABLED. If multiple match keep ENABLED if present, else first
        enabled = [r for r in rows if r["status"] == "ENABLED"]
        chosen = (enabled or rows)[0]
        out["tiers"][tier_kw] = {"id": chosen["campaign_id"],
                                  "name": chosen["campaign_name"]}

    # Single-state outlier campaigns. Match by state name AND not containing
    # a tier keyword.
    for st in OUTLIER_STATES:
        sql = """
            SELECT campaign_id, campaign_name, status
              FROM campaigns
             WHERE account_id = ?
               AND UPPER(campaign_name) LIKE ?
               AND UPPER(campaign_name) NOT LIKE '%TIER%'
        """
        rows = conn.execute(sql, (ACCOUNT_ID, f"%{st}%")).fetchall()
        rows = [r for r in rows if "HOME INSURANCE" in r["campaign_name"].upper()]
        if not rows:
            print(f"  WARNING: no single-state campaign for {st}")
            continue
        enabled = [r for r in rows if r["status"] == "ENABLED"]
        chosen = (enabled or rows)[0]
        out["outliers"][st] = {"id": chosen["campaign_id"],
                                "name": chosen["campaign_name"]}
    return out


def probe_data_availability(conn, campaigns):
    """
    Returns date min/max + row count per campaign in the geo_daily table,
    and per-window summaries.
    """
    avail = {}
    all_campaign_ids = [v["id"] for v in campaigns["tiers"].values()] + \
                       [v["id"] for v in campaigns["outliers"].values()]
    if not all_campaign_ids:
        return avail
    placeholders = ",".join("?" * len(all_campaign_ids))
    sql = f"""
        SELECT campaign_id,
               MIN(date) AS first_date,
               MAX(date) AS last_date,
               COUNT(*) AS row_count
          FROM campaign_performance_geo_daily
         WHERE account_id = ?
           AND campaign_id IN ({placeholders})
         GROUP BY campaign_id
    """
    rows = conn.execute(sql, [ACCOUNT_ID] + all_campaign_ids).fetchall()
    for r in rows:
        avail[r["campaign_id"]] = dict(r)
    return avail


def window_is_available(window_def, campaigns, availability):
    """
    Returns (is_available, reason_if_not). A window is available if the
    geo_daily table has data covering at least 80% of both the pre and
    post date ranges for at least one tier campaign and one outlier
    campaign.
    """
    def _coverage_pct(campaign_id, start, end):
        a = availability.get(campaign_id)
        if not a:
            return 0.0
        first = date.fromisoformat(a["first_date"])
        last  = date.fromisoformat(a["last_date"])
        overlap_start = max(first, start)
        overlap_end   = min(last, end)
        if overlap_start > overlap_end:
            return 0.0
        overlap_days = (overlap_end - overlap_start).days + 1
        window_days  = (end - start).days + 1
        return overlap_days / window_days

    # For the pre-window we need the TIER campaigns to have data
    # (CA/TX/FL were inside tiers pre-breakout). For the post-window
    # we need the OUTLIER campaigns to have data.
    pre_tier_cov = max(
        (_coverage_pct(c["id"], window_def["pre_start"], window_def["pre_end"])
         for c in campaigns["tiers"].values()), default=0.0)
    post_outlier_cov = max(
        (_coverage_pct(c["id"], window_def["post_start"], window_def["post_end"])
         for c in campaigns["outliers"].values()), default=0.0)

    if pre_tier_cov < 0.8:
        return False, f"Pre-window tier coverage {pre_tier_cov:.0%} (need ≥80%)"
    if post_outlier_cov < 0.8:
        return False, f"Post-window outlier coverage {post_outlier_cov:.0%} (need ≥80%)"
    return True, f"pre tier {pre_tier_cov:.0%}, post outlier {post_outlier_cov:.0%}"


# ── Pre/Post measurement ──────────────────────────────────────────
def fetch_geo_aggregate(conn, campaign_ids, start_date, end_date,
                        states_filter=None):
    """
    Aggregate geo_daily rows by geo_state across the given campaigns and
    window. Returns list of dicts with derived metrics.
    """
    if not campaign_ids:
        return []
    placeholders = ",".join("?" * len(campaign_ids))
    state_clause = ""
    params = [ACCOUNT_ID] + list(campaign_ids) + [start_date.isoformat(),
                                                    end_date.isoformat()]
    if states_filter:
        state_placeholders = ",".join("?" * len(states_filter))
        state_clause = f" AND UPPER(geo_state) IN ({state_placeholders}) "
        params.extend([s.upper() for s in states_filter])

    sql = f"""
        SELECT geo_state,
               COUNT(DISTINCT date)     AS days_active,
               SUM(impressions)         AS impressions,
               SUM(clicks)              AS clicks,
               SUM(cost_micros)/1e6     AS spend,
               SUM(conversions)         AS conversions,
               SUM(conversion_value)    AS revenue
          FROM campaign_performance_geo_daily
         WHERE account_id = ?
           AND campaign_id IN ({placeholders})
           AND date BETWEEN ? AND ?
           AND geo_state IS NOT NULL
           {state_clause}
         GROUP BY geo_state
    """
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        sp = r["spend"] or 0.0
        rv = r["revenue"] or 0.0
        cv = r["conversions"] or 0.0
        clk = r["clicks"] or 0
        d   = r["days_active"] or 0
        r["gp"]    = rv - sp
        r["roas"]  = (rv / sp) if sp > 0 else None
        r["cpc"]   = (sp / clk) if clk > 0 else None
        r["cpa"]   = (sp / cv) if cv > 0 else None
        r["spend_per_day"]   = (sp / d) if d > 0 else None
        r["gp_per_day"]      = (r["gp"] / d) if d > 0 else None
        r["conv_per_day"]    = (cv / d) if d > 0 else None
        r["revenue_per_day"] = (rv / d) if d > 0 else None
    return rows


def measure_outlier_pre_post(conn, campaigns, window_def):
    """
    For each outlier state (CA/TX/FL), compute pre and post aggregates.
    Pre: aggregated across all tier campaigns (state was in one of them)
    Post: from the dedicated single-state campaign
    """
    tier_ids = [c["id"] for c in campaigns["tiers"].values()]
    out = []

    for state_kw in OUTLIER_STATES:
        outlier_camp = campaigns["outliers"].get(state_kw)
        if not outlier_camp:
            continue
        state_display = OUTLIER_STATE_DISPLAY[state_kw]

        pre_rows = fetch_geo_aggregate(conn, tier_ids,
                                        window_def["pre_start"],
                                        window_def["pre_end"],
                                        states_filter=[state_kw, state_display])
        post_rows = fetch_geo_aggregate(conn, [outlier_camp["id"]],
                                         window_def["post_start"],
                                         window_def["post_end"])

        # Sum across rows (geo_state values may include capitalization variants
        # or UNKNOWN buckets; treat all as belonging to this state)
        def _sum(rows, k):
            return sum((r.get(k) or 0) for r in rows)

        pre_d  = sum((r.get("days_active") or 0) for r in pre_rows)
        post_d = sum((r.get("days_active") or 0) for r in post_rows)

        out.append({
            "state":             state_display,
            "outlier_campaign":  outlier_camp["name"],
            "pre_window":        f"{window_def['pre_start']}→{window_def['pre_end']}",
            "post_window":       f"{window_def['post_start']}→{window_def['post_end']}",
            "pre_days":          pre_d,
            "post_days":         post_d,
            "pre_spend":         _sum(pre_rows, "spend"),
            "post_spend":        _sum(post_rows, "spend"),
            "pre_revenue":       _sum(pre_rows, "revenue"),
            "post_revenue":      _sum(post_rows, "revenue"),
            "pre_gp":            _sum(pre_rows, "gp"),
            "post_gp":           _sum(post_rows, "gp"),
            "pre_conversions":   _sum(pre_rows, "conversions"),
            "post_conversions":  _sum(post_rows, "conversions"),
            "pre_gp_per_day":    (_sum(pre_rows, "gp") / pre_d) if pre_d else None,
            "post_gp_per_day":   (_sum(post_rows, "gp") / post_d) if post_d else None,
            "pre_roas":          (_sum(pre_rows, "revenue") / _sum(pre_rows, "spend"))
                                  if _sum(pre_rows, "spend") else None,
            "post_roas":         (_sum(post_rows, "revenue") / _sum(post_rows, "spend"))
                                  if _sum(post_rows, "spend") else None,
        })

        # Compute deltas
        last = out[-1]
        if last["pre_gp_per_day"] is not None and last["post_gp_per_day"] is not None:
            last["gp_per_day_delta"] = last["post_gp_per_day"] - last["pre_gp_per_day"]
            last["gp_per_day_lift_pct"] = (
                last["gp_per_day_delta"] / abs(last["pre_gp_per_day"])
                if last["pre_gp_per_day"] != 0 else None)
        else:
            last["gp_per_day_delta"] = None
            last["gp_per_day_lift_pct"] = None

    return out


def measure_seasonality_control(conn, campaigns, window_def, exclude_states):
    """
    Same pre/post delta calculation, but for ALL states still inside tier
    campaigns (excluding CA/TX/FL). Used to net out market drift /
    seasonality from the raw outlier-state delta.
    """
    tier_ids = [c["id"] for c in campaigns["tiers"].values()]
    if not tier_ids:
        return None

    pre_rows  = fetch_geo_aggregate(conn, tier_ids,
                                     window_def["pre_start"],
                                     window_def["pre_end"])
    post_rows = fetch_geo_aggregate(conn, tier_ids,
                                     window_def["post_start"],
                                     window_def["post_end"])

    excludes = {s.upper() for s in exclude_states}
    pre_filtered  = [r for r in pre_rows if (r["geo_state"] or "").upper() not in excludes]
    post_filtered = [r for r in post_rows if (r["geo_state"] or "").upper() not in excludes]

    def _agg(rows):
        sp = sum((r.get("spend") or 0) for r in rows)
        rv = sum((r.get("revenue") or 0) for r in rows)
        gp = rv - sp
        d  = sum((r.get("days_active") or 0) for r in rows) / max(len(rows), 1)
        n_states = len(rows)
        return {
            "spend": sp, "revenue": rv, "gp": gp,
            "n_states": n_states, "avg_days": d,
            "gp_per_state_per_day": gp / (d * n_states) if d and n_states else None,
            "roas": (rv / sp) if sp > 0 else None,
        }

    pre  = _agg(pre_filtered)
    post = _agg(post_filtered)

    return {
        "pre":  pre,
        "post": post,
        "gp_per_state_per_day_delta":
            (post["gp_per_state_per_day"] - pre["gp_per_state_per_day"])
            if (pre["gp_per_state_per_day"] is not None
                and post["gp_per_state_per_day"] is not None) else None,
        "gp_per_state_per_day_lift_pct":
            ((post["gp_per_state_per_day"] - pre["gp_per_state_per_day"])
             / abs(pre["gp_per_state_per_day"]))
            if (pre["gp_per_state_per_day"] not in (None, 0)
                and post["gp_per_state_per_day"] is not None) else None,
        "n_states_pre":  pre["n_states"],
        "n_states_post": post["n_states"],
    }


# ── New candidate identification ──────────────────────────────────
def identify_candidates(conn, campaigns, window_def, share_threshold):
    """
    For each tier campaign in the pre-window, find states whose spend
    was ≥ share_threshold of that tier's total. Candidates = those states
    excluding the 3 already-broken-out outliers.
    """
    candidates = []
    excludes = {s.upper() for s in OUTLIER_STATES}
    excludes |= {OUTLIER_STATE_DISPLAY[s].upper() for s in OUTLIER_STATES}

    for tier_kw, tier in campaigns["tiers"].items():
        rows = fetch_geo_aggregate(conn, [tier["id"]],
                                    window_def["pre_start"],
                                    window_def["pre_end"])
        # Filter known outliers out of the candidate pool (they ARE in the
        # tier campaigns in the pre-window — that's where their pre-data lives)
        non_outlier_rows = [r for r in rows
                            if (r["geo_state"] or "").upper() not in excludes]
        tier_total_spend = sum((r.get("spend") or 0) for r in rows)
        if tier_total_spend <= 0:
            continue

        for r in non_outlier_rows:
            sp = r.get("spend") or 0
            share = sp / tier_total_spend if tier_total_spend else 0
            if share >= share_threshold:
                candidates.append({
                    "geo_state":           r["geo_state"],
                    "current_tier":        tier_kw,
                    "current_tier_campaign": tier["name"],
                    "pre_days":            r["days_active"],
                    "pre_spend":           sp,
                    "tier_total_spend":    tier_total_spend,
                    "spend_share":         share,
                    "pre_revenue":         r["revenue"],
                    "pre_gp":              r["gp"],
                    "pre_gp_per_day":      r["gp_per_day"],
                    "pre_roas":            r["roas"],
                    "pre_conversions":     r["conversions"],
                    "pre_conv_per_day":    r["conv_per_day"],
                    "smart_bidding_viable":
                        (r["conv_per_day"] or 0) >= SMART_BIDDING_DAILY_CONV_FLOOR,
                })

    candidates.sort(key=lambda c: c["spend_share"], reverse=True)
    return candidates


def project_candidate_uplift(candidates, measured_lift_pct):
    """
    Project per-candidate uplift using the measured CA/TX/FL lift pct
    applied to each candidate's current GP/day.
    """
    if measured_lift_pct is None:
        return [{**c, "projected_lift_gp_per_day": None,
                 "projected_lift_gp_per_month": None} for c in candidates]
    out = []
    for c in candidates:
        cur_gp_day = c.get("pre_gp_per_day") or 0
        lift_day = cur_gp_day * measured_lift_pct
        out.append({
            **c,
            "projected_lift_gp_per_day": lift_day,
            "projected_lift_gp_per_month": lift_day * 30,
        })
    return out


# ── Sheet builders ────────────────────────────────────────────────
def build_pre_post_sheet(window_results: dict) -> pd.DataFrame:
    """One row per (state, window). Empty if no windows ran."""
    rows = []
    for window_key, result in window_results.items():
        if not result or not result.get("outliers"):
            continue
        for o in result["outliers"]:
            rows.append({
                "Window":             result["window_label"],
                "State":              o["state"],
                "Outlier Campaign":   o["outlier_campaign"],
                "Pre Window":         o["pre_window"],
                "Post Window":        o["post_window"],
                "Pre Days":           o["pre_days"],
                "Post Days":          o["post_days"],
                "Pre Spend":          round(o["pre_spend"], 2),
                "Post Spend":         round(o["post_spend"], 2),
                "Pre Revenue":        round(o["pre_revenue"], 2),
                "Post Revenue":       round(o["post_revenue"], 2),
                "Pre GP":             round(o["pre_gp"], 2),
                "Post GP":            round(o["post_gp"], 2),
                "Pre ROAS":           round(o["pre_roas"], 3) if o["pre_roas"] else None,
                "Post ROAS":          round(o["post_roas"], 3) if o["post_roas"] else None,
                "Pre GP/Day":         round(o["pre_gp_per_day"], 2) if o["pre_gp_per_day"] else None,
                "Post GP/Day":        round(o["post_gp_per_day"], 2) if o["post_gp_per_day"] else None,
                "GP/Day Δ":           round(o["gp_per_day_delta"], 2) if o["gp_per_day_delta"] is not None else None,
                "GP/Day Δ %":         round(o["gp_per_day_lift_pct"] * 100, 1) if o["gp_per_day_lift_pct"] is not None else None,
            })
    if not rows:
        return pd.DataFrame([{"Note": "No window had sufficient data — see Data Availability sheet"}])
    return pd.DataFrame(rows)


def build_seasonality_sheet(window_results: dict) -> pd.DataFrame:
    rows = []
    for window_key, result in window_results.items():
        if not result or not result.get("seasonality"):
            continue
        s = result["seasonality"]
        rows.append({
            "Window":               result["window_label"],
            "Pre State Count":      s["n_states_pre"],
            "Post State Count":     s["n_states_post"],
            "Pre GP/state/day":     round(s["pre"]["gp_per_state_per_day"], 2)
                                    if s["pre"]["gp_per_state_per_day"] is not None else None,
            "Post GP/state/day":    round(s["post"]["gp_per_state_per_day"], 2)
                                    if s["post"]["gp_per_state_per_day"] is not None else None,
            "GP/state/day Δ":       round(s["gp_per_state_per_day_delta"], 2)
                                    if s["gp_per_state_per_day_delta"] is not None else None,
            "GP/state/day Δ %":     round(s["gp_per_state_per_day_lift_pct"] * 100, 1)
                                    if s["gp_per_state_per_day_lift_pct"] is not None else None,
            "Pre ROAS":             round(s["pre"]["roas"], 3) if s["pre"]["roas"] else None,
            "Post ROAS":            round(s["post"]["roas"], 3) if s["post"]["roas"] else None,
            "Note":                 ("Used as seasonality control for outlier-state delta."
                                     if window_key == "q4_vs_post"
                                     else "Reference only; YoY window doesn't need control."),
        })
    if not rows:
        return pd.DataFrame([{"Note": "No seasonality control computed."}])
    return pd.DataFrame(rows)


def build_candidates_sheet(window_results: dict) -> pd.DataFrame:
    """One row per candidate state per window."""
    rows = []
    for window_key, result in window_results.items():
        if not result or not result.get("candidates"):
            continue
        for c in result["candidates"]:
            rows.append({
                "Window":             result["window_label"],
                "State":              c["geo_state"],
                "Current Tier":       c["current_tier"],
                "Pre Spend":          round(c["pre_spend"], 2),
                "Tier Total Spend":   round(c["tier_total_spend"], 2),
                "Spend Share":        round(c["spend_share"] * 100, 1),
                "Pre Days":           c["pre_days"],
                "Pre GP":             round(c["pre_gp"], 2),
                "Pre GP/Day":         round(c["pre_gp_per_day"], 2)
                                      if c["pre_gp_per_day"] is not None else None,
                "Pre ROAS":           round(c["pre_roas"], 3) if c["pre_roas"] else None,
                "Pre Conversions":    round(c["pre_conversions"], 1),
                "Pre Conv/Day":       round(c["pre_conv_per_day"], 2)
                                      if c["pre_conv_per_day"] is not None else None,
                "Smart Bidding Viable?": "Yes" if c["smart_bidding_viable"] else "Marginal",
                "Projected Lift GP/Day":   round(c["projected_lift_gp_per_day"], 2)
                                            if c.get("projected_lift_gp_per_day") is not None else None,
                "Projected Lift GP/Month": round(c["projected_lift_gp_per_month"], 2)
                                            if c.get("projected_lift_gp_per_month") is not None else None,
            })
    if not rows:
        return pd.DataFrame([{"Note": "No candidates identified — see Data Availability sheet"}])
    return pd.DataFrame(rows)


def build_sizing_summary(window_results: dict) -> pd.DataFrame:
    """
    Unified GP/Month | Current GP/Day | Expected GP Lift schema.
    Sources headline from the q4_vs_post window if available, else yoy.
    """
    rows = []

    def _row(initiative, scenario, current_gp_day, lift_day,
             lift_month, total_gp_day, total_gp_month, note=""):
        rows.append({
            "Initiative":                       initiative,
            "Scenario":                         scenario,
            "Current GP / Day ($)":             round(float(current_gp_day or 0), 2),
            "Expected GP Lift / Day ($)":       round(float(lift_day or 0), 2),
            "Expected GP Lift / Month ($)":     round(float(lift_month or 0), 2),
            "Total GP / Day ($)":               round(float((current_gp_day or 0) + (lift_day or 0)), 2),
            "Total GP / Month ($)":             round(float(total_gp_month or 0), 2),
            "Note":                             note,
        })

    # Pick the best window for the headline
    primary_window = None
    for k in ("q4_vs_post", "yoy"):
        if k in window_results and window_results[k] and window_results[k].get("outliers"):
            primary_window = k
            break

    if not primary_window:
        rows.append({
            "Initiative": "Tier Outliers Breakout",
            "Scenario":   "Insufficient data",
            "Current GP / Day ($)": 0,
            "Expected GP Lift / Day ($)": 0,
            "Expected GP Lift / Month ($)": 0,
            "Total GP / Day ($)": 0,
            "Total GP / Month ($)": 0,
            "Note":       "No window had sufficient data; see Data Availability sheet.",
        })
        return pd.DataFrame(rows)

    primary = window_results[primary_window]

    # Aggregate measured uplift across CA/TX/FL combined
    sum_pre_gp_per_day  = 0.0
    sum_post_gp_per_day = 0.0
    n_with_data = 0
    for o in primary["outliers"]:
        if o["pre_gp_per_day"] is not None and o["post_gp_per_day"] is not None:
            sum_pre_gp_per_day  += o["pre_gp_per_day"]
            sum_post_gp_per_day += o["post_gp_per_day"]
            n_with_data += 1

    raw_delta_per_day = (sum_post_gp_per_day - sum_pre_gp_per_day) if n_with_data else 0.0

    # Apply seasonality control if available
    s_ctl = primary.get("seasonality")
    season_adj = 0.0
    season_note = ""
    if (primary.get("window_def", {}).get("needs_seasonality_control") and s_ctl
            and s_ctl.get("gp_per_state_per_day_delta") is not None):
        season_adj_per_state = s_ctl["gp_per_state_per_day_delta"]
        season_adj = season_adj_per_state * n_with_data
        season_note = (f" (seasonality-adjusted: subtracted "
                       f"${season_adj_per_state:.2f}/state/day × "
                       f"{n_with_data} states from raw delta)")

    net_delta_per_day = raw_delta_per_day - season_adj

    # CA/TX/FL measured rows
    for o in primary["outliers"]:
        if o["pre_gp_per_day"] is None or o["post_gp_per_day"] is None:
            continue
        per_state_lift = o["post_gp_per_day"] - o["pre_gp_per_day"]
        if (primary.get("window_def", {}).get("needs_seasonality_control") and s_ctl
                and s_ctl.get("gp_per_state_per_day_delta") is not None):
            per_state_lift -= s_ctl["gp_per_state_per_day_delta"]
        _row(
            initiative=f"Tier Outliers Breakout — {o['state']} (measured)",
            scenario=f"{primary['window_label']}",
            current_gp_day=o["pre_gp_per_day"],
            lift_day=per_state_lift,
            lift_month=per_state_lift * 30,
            total_gp_day=o["post_gp_per_day"],
            total_gp_month=o["post_gp_per_day"] * 30,
            note=f"Pre GP/d ${o['pre_gp_per_day']:.2f} → Post GP/d "
                 f"${o['post_gp_per_day']:.2f}. {season_note}".strip(),
        )

    # Combined row
    avg_pre_gp_per_day = sum_pre_gp_per_day / n_with_data if n_with_data else 0
    _row(
        initiative=f"Tier Outliers Breakout — CA+TX+FL combined (measured)",
        scenario=f"{primary['window_label']}",
        current_gp_day=sum_pre_gp_per_day,
        lift_day=net_delta_per_day,
        lift_month=net_delta_per_day * 30,
        total_gp_day=sum_post_gp_per_day,
        total_gp_month=sum_post_gp_per_day * 30,
        note=f"Sum of CA/TX/FL pre-period GP/day: ${sum_pre_gp_per_day:.2f}; "
             f"post-period GP/day: ${sum_post_gp_per_day:.2f}.{season_note}",
    )

    # Compute measured lift % for projecting to candidates
    measured_lift_pct = (
        (net_delta_per_day / abs(sum_pre_gp_per_day))
        if sum_pre_gp_per_day not in (0, None)
        else None)

    # Candidate projections — need to re-project with measured pct
    candidates = primary.get("candidates", [])
    if candidates and measured_lift_pct is not None:
        # Apply measured pct to each candidate
        projected = project_candidate_uplift(candidates, measured_lift_pct)
        total_lift_day = sum((c.get("projected_lift_gp_per_day") or 0)
                             for c in projected)
        total_cur_day = sum((c.get("pre_gp_per_day") or 0) for c in projected)
        _row(
            initiative=f"Tier Outliers Breakout — New Candidates (projected)",
            scenario=f"Top {len(projected)} candidates @ measured CA/TX/FL lift "
                     f"({measured_lift_pct*100:.1f}%)",
            current_gp_day=total_cur_day,
            lift_day=total_lift_day,
            lift_month=total_lift_day * 30,
            total_gp_day=total_cur_day + total_lift_day,
            total_gp_month=(total_cur_day + total_lift_day) * 30,
            note=f"Candidates: {', '.join(c['geo_state'] for c in projected)}",
        )
        # Sensitivities: 50%, 150% of measured lift
        for label, factor in [("conservative (50% of measured lift)", 0.5),
                               ("aggressive (150% of measured lift)", 1.5)]:
            sens_pct = measured_lift_pct * factor
            sens_projected = project_candidate_uplift(candidates, sens_pct)
            sens_lift_day = sum((c.get("projected_lift_gp_per_day") or 0)
                                for c in sens_projected)
            _row(
                initiative=f"Tier Outliers Breakout — New Candidates ({label})",
                scenario=f"Lift @ {sens_pct*100:.1f}%",
                current_gp_day=total_cur_day,
                lift_day=sens_lift_day,
                lift_month=sens_lift_day * 30,
                total_gp_day=total_cur_day + sens_lift_day,
                total_gp_month=(total_cur_day + sens_lift_day) * 30,
                note=f"Sensitivity around the measured CA/TX/FL lift",
            )

    return pd.DataFrame(rows)


def build_data_availability_sheet(campaigns, availability,
                                  window_status: dict) -> pd.DataFrame:
    rows = []
    rows.append({"Section": "Resolved Campaigns", "Detail": "", "Value": ""})
    for tier_kw, c in campaigns["tiers"].items():
        a = availability.get(c["id"], {})
        rows.append({
            "Section": "  Tier",
            "Detail": f"{tier_kw} — {c['name']}",
            "Value": f"{a.get('first_date','—')} → {a.get('last_date','—')} "
                     f"({a.get('row_count', 0):,} rows)",
        })
    for state_kw, c in campaigns["outliers"].items():
        a = availability.get(c["id"], {})
        rows.append({
            "Section": "  Single-state outlier",
            "Detail": f"{state_kw} — {c['name']}",
            "Value": f"{a.get('first_date','—')} → {a.get('last_date','—')} "
                     f"({a.get('row_count', 0):,} rows)",
        })

    rows.append({"Section": "", "Detail": "", "Value": ""})
    rows.append({"Section": "Window Availability", "Detail": "", "Value": ""})
    for window_key, status in window_status.items():
        is_avail, reason = status
        rows.append({
            "Section": "  Window",
            "Detail": f"{window_key} ({WINDOWS[window_key]['label']})",
            "Value": ("AVAILABLE — " if is_avail else "SKIPPED — ") + reason,
        })
    return pd.DataFrame(rows)


def build_metadata_sheet(args, campaigns, window_status) -> pd.DataFrame:
    available = sum(1 for s in window_status.values() if s[0])
    rows = [
        ("Run timestamp",      datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Account ID",         ACCOUNT_ID),
        ("Breakout date",      str(BREAKOUT_DATE)),
        ("Outlier states",     ", ".join(OUTLIER_STATES)),
        ("Share threshold",    f"{args.share_threshold*100:.0f}%"),
        ("Smart Bidding floor", f"{SMART_BIDDING_DAILY_CONV_FLOOR}/day conversions"),
        ("", ""),
        ("Tier campaigns resolved",   len(campaigns["tiers"])),
        ("Outlier campaigns resolved", len(campaigns["outliers"])),
        ("Windows requested",         args.windows),
        ("Windows available",         available),
        ("", ""),
        ("Methodology",        "Pre-window: states inside tier campaigns. "
                                "Post-window: states inside their dedicated "
                                "single-state campaigns. Seasonality control: "
                                "tier-resident states' delta in same window, "
                                "subtracted from raw outlier delta (q4_vs_post only)."),
        ("Sizing source",      "Headline uses Q4 vs Post if available, else YoY. "
                                "Candidate projections apply measured CA/TX/FL "
                                "lift % to each candidate's pre-period GP/day."),
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
    parser.add_argument("--windows", default="all",
                        choices=["all", "q4_vs_post", "yoy"],
                        help="Which window(s) to run")
    parser.add_argument("--share-threshold", type=float,
                        default=DEFAULT_SHARE_THRESHOLD,
                        help=f"Min share of tier spend for candidacy "
                             f"(default {DEFAULT_SHARE_THRESHOLD})")
    parser.add_argument("--output",
                        default=str(OUTPUT_DIR / "tier_outliers_breakout_sizing.xlsx"),
                        help="Output workbook path")
    args = parser.parse_args()

    t0 = time.time()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Tier Outliers Breakout Sizing ===")
    print(f"Account: {ACCOUNT_ID}")
    print(f"Outlier states: {', '.join(OUTLIER_STATES)}")
    print(f"Share threshold: {args.share_threshold*100:.0f}%")

    conn = get_conn()

    print("\nResolving campaigns...")
    campaigns = resolve_campaigns(conn)
    print(f"  Tier campaigns:    {len(campaigns['tiers'])}/4")
    print(f"  Outlier campaigns: {len(campaigns['outliers'])}/{len(OUTLIER_STATES)}")
    if not campaigns["tiers"] or not campaigns["outliers"]:
        print("ERROR: missing tier or outlier campaigns. Aborting.")
        sys.exit(1)

    print("\nProbing data availability...")
    availability = probe_data_availability(conn, campaigns)

    # Decide which windows to run
    window_keys = list(WINDOWS) if args.windows == "all" else [args.windows]
    window_status = {}
    for wk in window_keys:
        is_avail, reason = window_is_available(WINDOWS[wk], campaigns, availability)
        window_status[wk] = (is_avail, reason)
        print(f"  Window {wk:12s}: {'OK ' if is_avail else 'SKIP'} ({reason})")

    # Run each available window
    window_results = {}
    for wk, (is_avail, _) in window_status.items():
        if not is_avail:
            window_results[wk] = None
            continue
        wd = WINDOWS[wk]
        print(f"\n--- Window: {wk} ({wd['label']}) ---")

        outliers = measure_outlier_pre_post(conn, campaigns, wd)
        for o in outliers:
            print(f"  {o['state']:12s} pre {o['pre_days']}d ${o['pre_spend']:>10,.0f} "
                  f"GP/d ${(o['pre_gp_per_day'] or 0):.2f} → "
                  f"post {o['post_days']}d ${o['post_spend']:>10,.0f} "
                  f"GP/d ${(o['post_gp_per_day'] or 0):.2f}")

        seasonality = (measure_seasonality_control(conn, campaigns, wd, OUTLIER_STATES)
                       if wd["needs_seasonality_control"] else None)
        if seasonality:
            print(f"  Seasonality control: GP/state/day Δ "
                  f"${(seasonality['gp_per_state_per_day_delta'] or 0):.2f}")

        candidates = identify_candidates(conn, campaigns, wd, args.share_threshold)
        print(f"  Candidates (≥{args.share_threshold*100:.0f}% tier spend): {len(candidates)}")
        for c in candidates:
            print(f"    {c['geo_state']:18s} {c['current_tier']:11s} "
                  f"share {c['spend_share']*100:.1f}% conv/d "
                  f"{(c['pre_conv_per_day'] or 0):.2f}")

        # Project candidate uplift using measured CA/TX/FL avg lift pct
        sum_pre  = sum(o["pre_gp_per_day"]  for o in outliers if o["pre_gp_per_day"] is not None)
        sum_post = sum(o["post_gp_per_day"] for o in outliers if o["post_gp_per_day"] is not None)
        # net out seasonality if applicable
        net_delta = sum_post - sum_pre
        if seasonality and seasonality.get("gp_per_state_per_day_delta") is not None:
            n_outliers = sum(1 for o in outliers if o["pre_gp_per_day"] is not None)
            net_delta -= seasonality["gp_per_state_per_day_delta"] * n_outliers
        measured_lift_pct = net_delta / abs(sum_pre) if sum_pre not in (0, None) else None

        candidates_projected = project_candidate_uplift(
            candidates, measured_lift_pct or 0)

        window_results[wk] = {
            "window_label":  wd["label"],
            "window_def":    wd,
            "outliers":      outliers,
            "seasonality":   seasonality,
            "candidates":    candidates_projected,
            "measured_lift_pct": measured_lift_pct,
        }

    print(f"\nWriting {output_path}...")
    sheets = {
        "Metadata":              build_metadata_sheet(args, campaigns, window_status),
        "Sizing Summary":        build_sizing_summary(window_results),
        "CA TX FL Pre Post":     build_pre_post_sheet(window_results),
        "Seasonality Control":   build_seasonality_sheet(window_results),
        "New Candidates":        build_candidates_sheet(window_results),
        "Data Availability":     build_data_availability_sheet(campaigns, availability,
                                                                 window_status),
        "Diagnostic":            pd.DataFrame([
            {"Metric": "Wall time (seconds)",      "Value": round(time.time()-t0, 1)},
            {"Metric": "Tier campaigns resolved",  "Value": len(campaigns["tiers"])},
            {"Metric": "Outlier campaigns resolved","Value": len(campaigns["outliers"])},
            {"Metric": "Windows available",
             "Value": sum(1 for s in window_status.values() if s[0])},
            {"Metric": "Total candidates",
             "Value": sum(len((window_results.get(k) or {}).get("candidates", []))
                          for k in window_results)},
        ]),
    }

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
        format_workbook(writer)

    print(f"Done. Size: {output_path.stat().st_size / 1024:.1f} KB  |  "
          f"Wall time: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
