"""
tier_outliers_breakout_sizing.py
=================================
Sizes the Tier Outlier Breakout initiative for Home Insurance Google
(account_id = 1).

V2 — POST-WINDOW DRIVEN CANDIDATE IDENTIFICATION
─────────────────────────────────────────────────
The Q1 2026 restructure created today's tier campaigns AND broke out
CA/TX/FL into standalone campaigns at the same time. This means:

  - 2025 had NO tier campaigns. CA/TX/FL data for 2025 lives under
    whatever campaigns existed at the time (state-level or blended).
  - 2026 has the 4 tier campaigns + 3 standalone outlier campaigns.

Implications:
  - Candidate identification uses the POST-window (2026) tier-spend share.
    "Which states currently inside Tier X have ≥25% of Tier X's spend?"
  - CA/TX/FL pre/post is COMPARATIVE CONTEXT, not measured-lift-from-
    breakout. The 2025 → 2026 delta blends structural change AND market
    drift; we don't claim it's the breakout's isolated effect.
  - Candidate uplift is sized using an ASSUMED lift % (default 10%, with
    sensitivity at 5/10/15/20%) — conservative since we have no clean
    measured reference.

OUTPUT — one workbook (tier_outliers_breakout_sizing.xlsx):
  Metadata
  Sizing Summary             — unified GP/Month | Current GP/Day | Expected GP Lift
  CA TX FL Pre Post           — comparative context per state, per window
  Seasonality Control         — non-outlier states' GP/day deltas across windows
                                (reference only; not used to adjust the lift)
  New Candidates              — states ≥25% of tier spend in current window,
                                with both 2025 and 2026 metrics + projection
  Data Availability           — what we found in the DB
  Diagnostic                  — counts/stats

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

# State -> current tier (for output labeling).
# Seeded from the Tier Centroids sheet of the most recent dual-pass run
# (tier_logic_full_pass.xlsx, May 2026). States listed in OUTLIER_STATES
# are NOT in this map; they're labeled "Standalone" via the helper below.
# UPDATE if tier membership changes.
STATE_TIER_MAP = {
    # TIER ONE  (9 members)
    "CONNECTICUT":    "TIER ONE",
    "HAWAII":         "TIER ONE",
    "LOUISIANA":      "TIER ONE",
    "NEVADA":         "TIER ONE",
    "NEW JERSEY":     "TIER ONE",
    "NEW YORK":       "TIER ONE",
    "NORTH CAROLINA": "TIER ONE",
    "SOUTH CAROLINA": "TIER ONE",
    "VIRGINIA":       "TIER ONE",
    # TIER TWO  (3 members; SD lives in T2 per dedup)
    "COLORADO":       "TIER TWO",
    "SOUTH DAKOTA":   "TIER TWO",
    "UTAH":           "TIER TWO",
    # TIER THREE  (14 members)
    "ARIZONA":        "TIER THREE",
    "ARKANSAS":       "TIER THREE",
    "DELAWARE":       "TIER THREE",
    "GEORGIA":        "TIER THREE",
    "IDAHO":          "TIER THREE",
    "INDIANA":        "TIER THREE",
    "KANSAS":         "TIER THREE",
    "MINNESOTA":      "TIER THREE",
    "MISSOURI":       "TIER THREE",
    "NEBRASKA":       "TIER THREE",
    "NEW HAMPSHIRE":  "TIER THREE",
    "OHIO":           "TIER THREE",
    "OKLAHOMA":       "TIER THREE",
    "TENNESSEE":      "TIER THREE",
    # TIER FOUR  (16 members)
    "ALABAMA":        "TIER FOUR",
    "ILLINOIS":       "TIER FOUR",
    "IOWA":           "TIER FOUR",
    "KENTUCKY":       "TIER FOUR",
    "MARYLAND":       "TIER FOUR",
    "MASSACHUSETTS":  "TIER FOUR",
    "MICHIGAN":       "TIER FOUR",
    "MISSISSIPPI":    "TIER FOUR",
    "NEW MEXICO":     "TIER FOUR",
    "OREGON":         "TIER FOUR",
    "PENNSYLVANIA":   "TIER FOUR",
    "RHODE ISLAND":   "TIER FOUR",
    "WASHINGTON":     "TIER FOUR",
    "WEST VIRGINIA":  "TIER FOUR",
    "WISCONSIN":      "TIER FOUR",
    "WYOMING":        "TIER FOUR",
}


def resolve_state_tier(state_name: str) -> str:
    """Returns 'Standalone' for outlier states (CA/TX/FL), the tier label
    for tiered states, or 'Unmapped' if the state isn't in either list.
    State name matching is case-insensitive."""
    if not state_name:
        return "Unmapped"
    s = state_name.upper()
    if s in {x.upper() for x in OUTLIER_STATES}:
        return "Standalone"
    if s in {OUTLIER_STATE_DISPLAY[x].upper() for x in OUTLIER_STATES}:
        return "Standalone"
    return STATE_TIER_MAP.get(s, "Unmapped")

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

# Assumed GP/day lift % when a candidate state is broken out from its
# tier into a dedicated campaign. Conservative assumption — we don't
# have a clean measured lift from CA/TX/FL because the pre-period (2025)
# represents a completely different account structure (no tiers existed).
# Sensitivity grid: 5% / 10% / 15% / 20%.
ASSUMED_BREAKOUT_LIFT_PCT = 0.10
BREAKOUT_LIFT_SENSITIVITY = [0.05, 0.10, 0.15, 0.20]

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

    # Post-window: we need the current tier or outlier campaigns to have data.
    # Pre-window: 2025 had per-state campaigns (no tiers), so we don't check
    # specific campaign IDs — the all-Home aggregate will cover whatever
    # campaigns existed at the time.
    post_outlier_cov = max(
        (_coverage_pct(c["id"], window_def["post_start"], window_def["post_end"])
         for c in campaigns["outliers"].values()), default=0.0)
    post_tier_cov = max(
        (_coverage_pct(c["id"], window_def["post_start"], window_def["post_end"])
         for c in campaigns["tiers"].values()), default=0.0)

    if post_outlier_cov < 0.8 and post_tier_cov < 0.8:
        return False, (f"Post-window coverage too low: outlier {post_outlier_cov:.0%}, "
                       f"tier {post_tier_cov:.0%} (need ≥80% on at least one)")
    return True, (f"post outlier {post_outlier_cov:.0%}, post tier {post_tier_cov:.0%} "
                  f"(pre-window uses all-account aggregate)")


# ── Pre/Post measurement ──────────────────────────────────────────
def fetch_geo_aggregate(conn, campaign_ids, start_date, end_date,
                        states_filter=None):
    """
    Aggregate geo_daily rows by geo_state across the given campaigns and
    window. Returns list of dicts with derived metrics.

    If campaign_ids is None or empty, the campaign filter is dropped and
    ALL Home-account geo data for the window is aggregated. Used for the
    pre-window (2025) where today's tier/outlier campaigns didn't exist.
    """
    state_clause = ""

    if campaign_ids:
        placeholders = ",".join("?" * len(campaign_ids))
        campaign_clause = f" AND campaign_id IN ({placeholders}) "
        # Insert campaign_ids after account_id, before the dates.
        params = [ACCOUNT_ID] + list(campaign_ids) + [start_date.isoformat(),
                                                       end_date.isoformat()]
    else:
        campaign_clause = ""
        params = [ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()]

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
           {campaign_clause}
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
    out = []

    for state_kw in OUTLIER_STATES:
        outlier_camp = campaigns["outliers"].get(state_kw)
        if not outlier_camp:
            continue
        state_display = OUTLIER_STATE_DISPLAY[state_kw]

        # PRE-window: all-Home account aggregate filtered to this state.
        # 2025 had no tier campaigns; CA/TX/FL data lived under whatever
        # campaigns existed at the time. Passing campaign_ids=None makes
        # fetch_geo_aggregate drop the campaign filter.
        pre_rows = fetch_geo_aggregate(conn, None,
                                        window_def["pre_start"],
                                        window_def["pre_end"],
                                        states_filter=[state_kw, state_display])
        # POST-window: from this state's dedicated standalone campaign.
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
            "state_tier":        resolve_state_tier(state_display),  # "Standalone"
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

    # Pre-window: all-Home account aggregate (2025 had no tier campaigns;
    # use the whole account as the reference baseline for "non-outlier"
    # state behavior).
    pre_rows  = fetch_geo_aggregate(conn, None,
                                     window_def["pre_start"],
                                     window_def["pre_end"])
    # Post-window: states currently inside tier campaigns (i.e. excludes
    # CA/TX/FL by definition since they're in their own campaigns).
    post_rows = fetch_geo_aggregate(conn, tier_ids,
                                     window_def["post_start"],
                                     window_def["post_end"])

    # Build the state list to compare apples-to-apples: only states that
    # are in TIER CAMPAIGNS in the post-window (= today's tier residents).
    # This excludes outliers AND any 2025-only/marginal states that don't
    # exist in the current account structure. Without this, pre-period
    # n_states blows up (~48) vs post-period (~42), producing a misleading
    # per-state-mean delta dominated by changes in the denominator.
    tier_resident_states_today = {
        (r["geo_state"] or "").upper()
        for r in post_rows
        if r["geo_state"]
    }
    excludes = {s.upper() for s in exclude_states}
    pre_filtered  = [r for r in pre_rows
                     if (r["geo_state"] or "").upper() in tier_resident_states_today
                     and (r["geo_state"] or "").upper() not in excludes]
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


# ── Candidate identification (post-window driven) ────────────────
def identify_candidates(conn, campaigns, window_def, share_threshold):
    """
    Identifies candidate states for breakout. Candidates are states
    currently inside a tier campaign whose POST-window spend share
    of their tier's total exceeds share_threshold.

    For each candidate, also fetches pre-window state-level metrics
    (using the all-Home-account aggregate, since 2025 had no tiers)
    so the output can compare "this state under its 2025 standalone
    campaign" vs "this state under its 2026 tier campaign."
    """
    candidates = []
    excludes = {s.upper() for s in OUTLIER_STATES}
    excludes |= {OUTLIER_STATE_DISPLAY[s].upper() for s in OUTLIER_STATES}

    # Step 1: identify candidates from POST-window (per-tier-campaign)
    tier_post_lookups = {}
    for tier_kw, tier in campaigns["tiers"].items():
        rows = fetch_geo_aggregate(conn, [tier["id"]],
                                    window_def["post_start"],
                                    window_def["post_end"])
        non_outlier_rows = [r for r in rows
                            if (r["geo_state"] or "").upper() not in excludes]
        tier_total_spend = sum((r.get("spend") or 0) for r in rows)
        if tier_total_spend <= 0:
            continue

        for r in non_outlier_rows:
            sp = r.get("spend") or 0
            share = sp / tier_total_spend if tier_total_spend else 0
            if share >= share_threshold:
                state = r["geo_state"]
                tier_post_lookups[(state or "").upper()] = {
                    "geo_state":             state,
                    "current_tier":          tier_kw,
                    "current_tier_campaign": tier["name"],
                    "tier_total_spend":      tier_total_spend,
                    "spend_share":           share,
                    "post_days":             r["days_active"],
                    "post_spend":            sp,
                    "post_revenue":          r["revenue"],
                    "post_gp":               r["gp"],
                    "post_gp_per_day":       r["gp_per_day"],
                    "post_roas":             r["roas"],
                    "post_conversions":      r["conversions"],
                    "post_conv_per_day":     r["conv_per_day"],
                    "smart_bidding_viable":
                        (r["conv_per_day"] or 0) >= SMART_BIDDING_DAILY_CONV_FLOOR,
                }

    # Step 2: fetch pre-window all-Home aggregate (2025 had no tier
    # campaigns; states lived under whatever campaigns existed at the time)
    pre_rows = fetch_geo_aggregate(conn, None,
                                    window_def["pre_start"],
                                    window_def["pre_end"])
    pre_by_state = {(r["geo_state"] or "").upper(): r for r in pre_rows}

    # Step 3: merge — every candidate gets its pre-window measurement
    # alongside its post-window position in the tier campaign
    for state_upper, post_info in tier_post_lookups.items():
        pre = pre_by_state.get(state_upper, {})
        candidates.append({
            **post_info,
            "state_tier":         resolve_state_tier(post_info["geo_state"]),
            "pre_days":           pre.get("days_active"),
            "pre_spend":          pre.get("spend"),
            "pre_revenue":        pre.get("revenue"),
            "pre_gp":             pre.get("gp"),
            "pre_gp_per_day":     pre.get("gp_per_day"),
            "pre_roas":           pre.get("roas"),
            "pre_conversions":    pre.get("conversions"),
            "pre_conv_per_day":   pre.get("conv_per_day"),
            # Backward-compatibility: project_candidate_uplift() reads
            # pre_gp_per_day for the projection base. We want the
            # projection to apply to current GP/day (= post_gp_per_day),
            # so write a base_gp_per_day key we'll use downstream.
            "base_gp_per_day":    post_info["post_gp_per_day"],
        })

    candidates.sort(key=lambda c: c["spend_share"], reverse=True)
    return candidates


def project_candidate_uplift(candidates, lift_pct):
    """
    Project per-candidate uplift by applying lift_pct to each candidate's
    base_gp_per_day. The base is the candidate's POST-window (current,
    inside-tier) GP/day — what they're producing today inside their
    tier campaign. The projection asks: "if we broke this state out into
    its own campaign, how much GP/day would we expect to gain?"
    """
    if lift_pct is None:
        return [{**c, "projected_lift_gp_per_day": None,
                 "projected_lift_gp_per_month": None} for c in candidates]
    out = []
    for c in candidates:
        # Prefer the explicit projection base (post-window GP/day) written
        # by identify_candidates; fall back to pre-period for older callers.
        cur_gp_day = c.get("base_gp_per_day")
        if cur_gp_day is None:
            cur_gp_day = c.get("post_gp_per_day")
        if cur_gp_day is None:
            cur_gp_day = c.get("pre_gp_per_day") or 0
        lift_day = cur_gp_day * lift_pct
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
                "State Tier (label)": o.get("state_tier", "Unmapped"),
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
    """One row per candidate state per window, with BOTH the 2025 pre
    and 2026 post-window metrics + projected breakout lift."""
    rows = []
    for window_key, result in window_results.items():
        if not result or not result.get("candidates"):
            continue
        for c in result["candidates"]:
            rows.append({
                "Window":                 result["window_label"],
                "State":                  c["geo_state"],
                "State Tier (label)":     c.get("state_tier", "Unmapped"),
                "Current Tier Campaign":  c["current_tier"],
                # 2026 (current state inside tier campaign)
                "Post Spend (current)":          round(c["post_spend"], 2),
                "Tier Total Spend (current)":    round(c["tier_total_spend"], 2),
                "Spend Share % (current)":       round(c["spend_share"] * 100, 1),
                "Post Days":                     c["post_days"],
                "Post GP":                       round(c["post_gp"], 2),
                "Post GP/Day":                   round(c["post_gp_per_day"], 2)
                                                  if c["post_gp_per_day"] is not None else None,
                "Post ROAS":                     round(c["post_roas"], 3) if c["post_roas"] else None,
                "Post Conv/Day":                 round(c["post_conv_per_day"], 2)
                                                  if c["post_conv_per_day"] is not None else None,
                "Smart Bidding Viable?":         "Yes" if c["smart_bidding_viable"] else "Marginal",
                # 2025 (under prior structure — for reference)
                "Pre Spend (2025)":              round(c["pre_spend"], 2)
                                                  if c.get("pre_spend") is not None else None,
                "Pre GP/Day (2025)":             round(c["pre_gp_per_day"], 2)
                                                  if c.get("pre_gp_per_day") is not None else None,
                "Pre ROAS (2025)":               round(c["pre_roas"], 3) if c.get("pre_roas") else None,
                "Pre Conv/Day (2025)":           round(c["pre_conv_per_day"], 2)
                                                  if c.get("pre_conv_per_day") is not None else None,
                # Projection
                "Projected Lift GP/Day":         round(c["projected_lift_gp_per_day"], 2)
                                                  if c.get("projected_lift_gp_per_day") is not None else None,
                "Projected Lift GP/Month":       round(c["projected_lift_gp_per_month"], 2)
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

    # ──────────────────────────────────────────────────────────────
    # CA/TX/FL: comparative context, NOT measured-lift-from-breakout
    # ──────────────────────────────────────────────────────────────
    # The 2025 pre-window for CA/TX/FL doesn't represent "CA inside its
    # tier campaign" — tiers didn't exist in 2025. It represents "CA
    # under whatever 2025 campaign structure existed" (likely older
    # state-level or blended campaigns). So pre→post here is a comparison
    # across two different account structures, NOT a clean measurement
    # of the breakout decision in isolation. We surface the comparison
    # as context but DON'T claim it's the measured lift.
    sum_pre_gp_per_day  = 0.0
    sum_post_gp_per_day = 0.0
    n_with_data = 0
    for o in primary["outliers"]:
        if o["pre_gp_per_day"] is not None and o["post_gp_per_day"] is not None:
            sum_pre_gp_per_day  += o["pre_gp_per_day"]
            sum_post_gp_per_day += o["post_gp_per_day"]
            n_with_data += 1

    s_ctl = primary.get("seasonality")
    season_note = ""
    if (primary.get("window_def", {}).get("needs_seasonality_control") and s_ctl
            and s_ctl.get("gp_per_state_per_day_delta") is not None):
        season_adj_per_state = s_ctl["gp_per_state_per_day_delta"]
        season_note = (f" Seasonality reference: tier-resident states' "
                       f"GP/state/day moved by ${season_adj_per_state:.2f} "
                       f"in same window.")

    # Per-state comparative rows (CA/TX/FL)
    for o in primary["outliers"]:
        if o["pre_gp_per_day"] is None or o["post_gp_per_day"] is None:
            continue
        per_state_delta = o["post_gp_per_day"] - o["pre_gp_per_day"]
        _row(
            initiative=f"Tier Outliers — {o['state']} (standalone, comparative context)",
            scenario=f"{primary['window_label']}: 2025 structure → 2026 standalone",
            current_gp_day=o["post_gp_per_day"],   # CURRENT = today
            lift_day=0,                              # NOT a forward-looking lift
            lift_month=0,
            total_gp_day=o["post_gp_per_day"],
            total_gp_month=o["post_gp_per_day"] * 30,
            note=f"2025 GP/d ${o['pre_gp_per_day']:.2f} (pre-tier-restructure "
                 f"all-account structure) vs 2026 standalone GP/d "
                 f"${o['post_gp_per_day']:.2f}, Δ ${per_state_delta:+.2f}/d. "
                 f"Δ reflects BOTH structural change AND market/seasonal drift "
                 f"between periods — not a clean breakout-only measurement."
                 f"{season_note}",
        )

    # Combined CA+TX+FL comparative row
    if n_with_data > 0:
        combined_delta = sum_post_gp_per_day - sum_pre_gp_per_day
        _row(
            initiative=f"Tier Outliers — CA+TX+FL combined (standalone, comparative context)",
            scenario=f"{primary['window_label']}: 2025 → 2026 totals",
            current_gp_day=sum_post_gp_per_day,
            lift_day=0,
            lift_month=0,
            total_gp_day=sum_post_gp_per_day,
            total_gp_month=sum_post_gp_per_day * 30,
            note=f"Sum of CA/TX/FL: 2025 GP/d ${sum_pre_gp_per_day:.2f} → "
                 f"2026 GP/d ${sum_post_gp_per_day:.2f}, Δ ${combined_delta:+.2f}/d. "
                 f"Comparative context only.{season_note}",
        )

    # ──────────────────────────────────────────────────────────────
    # CANDIDATES: forward-looking sizing using ASSUMED breakout lift
    # ──────────────────────────────────────────────────────────────
    # Conservative assumption (default 10%): in the absence of a clean
    # measured lift, project each candidate's potential GP/day gain by
    # applying a fixed % to its current (post-window) GP/day.
    candidates = primary.get("candidates", [])
    if candidates:
        # Headline at ASSUMED_BREAKOUT_LIFT_PCT
        projected = project_candidate_uplift(candidates, ASSUMED_BREAKOUT_LIFT_PCT)
        total_lift_day = sum((c.get("projected_lift_gp_per_day") or 0)
                             for c in projected)
        # "Current" for candidates = their CURRENT (post-window) GP/day
        # inside their tier — what they're producing today.
        total_cur_day = sum((c.get("base_gp_per_day") or 0) for c in projected)
        _row(
            initiative=f"Tier Outliers — New Breakout Candidates",
            scenario=f"HEADLINE: {len(projected)} candidate(s) @ "
                     f"{ASSUMED_BREAKOUT_LIFT_PCT*100:.0f}% assumed lift "
                     f"(conservative)",
            current_gp_day=total_cur_day,
            lift_day=total_lift_day,
            lift_month=total_lift_day * 30,
            total_gp_day=total_cur_day + total_lift_day,
            total_gp_month=(total_cur_day + total_lift_day) * 30,
            note=f"Candidates (≥{primary.get('share_threshold_pct', '?')}% "
                 f"tier-spend share): "
                 f"{', '.join(c['geo_state'] for c in projected)}. "
                 f"Assumed lift is HYPOTHESIS-based (no measured CA/TX/FL "
                 f"reference; pre-period structures differ).",
        )
        # Sensitivity grid
        for sens_pct in BREAKOUT_LIFT_SENSITIVITY:
            if abs(sens_pct - ASSUMED_BREAKOUT_LIFT_PCT) < 1e-9:
                continue   # skip the headline; already shown above
            sens_projected = project_candidate_uplift(candidates, sens_pct)
            sens_lift_day = sum((c.get("projected_lift_gp_per_day") or 0)
                                for c in sens_projected)
            _row(
                initiative=f"Tier Outliers — New Breakout Candidates",
                scenario=f"  └ sensitivity: {sens_pct*100:.0f}% lift",
                current_gp_day=total_cur_day,
                lift_day=sens_lift_day,
                lift_month=sens_lift_day * 30,
                total_gp_day=total_cur_day + sens_lift_day,
                total_gp_month=(total_cur_day + sens_lift_day) * 30,
                note=f"Sensitivity grid around the {ASSUMED_BREAKOUT_LIFT_PCT*100:.0f}% headline",
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
        ("Run timestamp",       datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Account ID",          ACCOUNT_ID),
        ("Breakout date",       str(BREAKOUT_DATE)),
        ("Outlier states",      ", ".join(OUTLIER_STATES)),
        ("Share threshold",     f"{args.share_threshold*100:.0f}%"),
        ("Smart Bidding floor", f"{SMART_BIDDING_DAILY_CONV_FLOOR}/day conversions"),
        ("Assumed breakout lift", f"{ASSUMED_BREAKOUT_LIFT_PCT*100:.0f}% (HYPOTHESIS — "
                                   f"no measured CA/TX/FL reference; pre-period structures differ)"),
        ("Sensitivity grid",    ", ".join(f"{p*100:.0f}%" for p in BREAKOUT_LIFT_SENSITIVITY)),
        ("", ""),
        ("Tier campaigns resolved",    len(campaigns["tiers"])),
        ("Outlier campaigns resolved", len(campaigns["outliers"])),
        ("Windows requested",          args.windows),
        ("Windows available",          available),
        ("", ""),
        ("Methodology — Candidates",
         "Identified from POST-window: states with ≥{:.0f}% spend share "
         "of their tier campaign's total. Pre-window (2025) state metrics "
         "fetched from all-Home-account aggregate (no tier campaigns existed) "
         "and attached for reference only.".format(args.share_threshold*100)),
        ("Methodology — Outliers (CA/TX/FL)",
         "Pre-window: state's GP/day under the 2025 account structure "
         "(all-Home aggregate filtered to the state). Post-window: state's "
         "GP/day under its current standalone campaign. Comparison surfaced "
         "as CONTEXT — not a clean breakout-only measurement, since the "
         "2025 → 2026 transition involved an account-wide restructure."),
        ("Sizing source",
         "CA/TX/FL rows show today's standalone GP/day with comparative "
         "context. Candidate rows project breakout uplift using "
         "ASSUMED_BREAKOUT_LIFT_PCT against each candidate's CURRENT "
         "(post-window) GP/day inside its tier."),
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

        # Project candidate uplift using the ASSUMED breakout lift % at
        # the headline. The Sizing Summary sheet re-projects across the
        # full sensitivity grid (5/10/15/20%) for the workbook output;
        # this run-time projection just feeds the per-state numbers into
        # the New Candidates sheet for the headline scenario.
        candidates_projected = project_candidate_uplift(
            candidates, ASSUMED_BREAKOUT_LIFT_PCT)

        window_results[wk] = {
            "window_label":         wd["label"],
            "window_def":           wd,
            "outliers":             outliers,
            "seasonality":          seasonality,
            "candidates":           candidates_projected,
            "share_threshold_pct":  int(round(args.share_threshold * 100)),
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
