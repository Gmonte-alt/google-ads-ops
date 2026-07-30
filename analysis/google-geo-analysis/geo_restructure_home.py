"""
geo_restructure_home.py
========================
Home Insurance geo restructure analysis (account_id = 1).

Tier Two is being converted to Colorado-only. Utah and South Dakota
are being moved out and need to be placed in Tier One, Three, or Four.
This script produces an Excel workbook containing the performance
evidence needed to support that decision.

Scope & methodology (locked in discovery thread):
  - Similarity math (Steps 2-4) uses Tier campaigns ONLY.
  - Step 5 is Tier Two only (Colorado isolation).
  - Step 6 (2026 weekly trend) stitches intermediate 2026 campaigns
    into each state's Tier trend for continuity, and includes
    CA/TX/FL as reference-context trend lines.
  - Step 7 (Diagnostic) catalogs every campaign in the DB and flags
    what was included vs excluded.

Device convention:
  campaign_performance_geo_daily_search has NO device='ALL' rollup.
  Sum across all device rows for daily totals.

  campaign_performance_daily_search (for IS join) IS expected to have a
  device='ALL' rollup; the script verifies before using it and
  falls back to an impression-weighted average if 'ALL' is absent.

Window:
  --days controls analysis window (default 90). Because Tier
  campaigns only launched late Jan / early Feb 2026, many states
  have fewer than --days days of tier-campaign data. The script
  uses whatever days are available per state and reports the
  actual days-active alongside each profile.

Usage:
  python analysis/geo_restructure_home.py
  python analysis/geo_restructure_home.py --end-date 2026-04-20
  python analysis/geo_restructure_home.py --dry-run
"""

import sqlite3
import argparse
import sys
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import statistics
import math

# ─── Config ───────────────────────────────────────────────────────────
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\google-geo-analysis\output")
ACCOUNT_ID = 1

HEADER_FILL = "7030A0"
HEADER_FONT = "FFFFFF"

TIER_CAMPAIGNS = {
    "TIER ONE":   None,
    "TIER TWO":   None,
    "TIER THREE": None,
    "TIER FOUR":  None,
}
CANDIDATE_TIERS = ["TIER ONE", "TIER THREE", "TIER FOUR"]
TARGET_STATES   = ["Utah", "South Dakota"]
COLORADO        = "Colorado"

# Intermediate 2026 campaign names that represent a single state's brief
# stopover before joining a Tier campaign (based on discovery thread).
# Names from diagnostic output: CONNECTICUT, HAWAII, LOUISIANA, NEVADA,
# NEW YORK, NORTH CAROLINA, SOUTH CAROLINA, VIRGINIA, UTAH.
# We detect them by pattern: a campaign whose name is a state name in
# all-caps (and is NOT a -SEARCH-MOBILE-USA-STE legacy or a Tier campaign).

SINGLE_STATE_CAMPAIGNS = {
    "CALIFORNIA", "TEXAS", "FLORIDA"  # active single-state campaigns
}

# Min days required to include a state in tier centroid calc (Step 4)
MIN_DAYS_ACTIVE_FOR_CENTROID = 30


# ─── Excluded campaign types (applied across all steps) ───────────────
# PMAX is excluded everywhere. Campaign rows for PMAX still appear in
# the Diagnostic sheet's "present but excluded" summary.
EXCLUDED_CAMPAIGN_CLASSES = {"PMAX"}


def is_excluded(campaign_name):
    return classify_campaign(campaign_name) in EXCLUDED_CAMPAIGN_CLASSES


def last_complete_iso_week(end_date):
    """
    Return (iso_year, iso_week) of the most recent ISO week fully contained
    on or before end_date. An ISO week runs Monday→Sunday. The week is
    'complete' when its Sunday is <= end_date.
    """
    weekday = end_date.isoweekday()  # 1=Mon, 7=Sun
    # Sunday of end_date's ISO week:
    end_week_sunday = end_date + timedelta(days=(7 - weekday))
    if end_week_sunday <= end_date:
        iy, iw, _ = end_date.isocalendar()
    else:
        # Back off to previous Sunday
        prev_sunday = end_date - timedelta(days=weekday)
        iy, iw, _ = prev_sunday.isocalendar()
    return iy, iw


# ─── DB helpers ───────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def tier_campaign_id_map(conn):
    """Resolve the 4 tier campaign IDs from the campaigns table."""
    out = {}
    for tier_kw in TIER_CAMPAIGNS.keys():
        row = conn.execute(
            "SELECT campaign_id, campaign_name FROM campaigns "
            "WHERE account_id = ? AND campaign_name LIKE ? "
            "AND campaign_name LIKE '%HOME INSURANCE%'",
            (ACCOUNT_ID, f"%{tier_kw}%")
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Could not find campaign matching '{tier_kw}'")
        out[tier_kw] = {"id": row["campaign_id"], "name": row["campaign_name"]}
    return out


def verify_daily_has_all_rollup(conn):
    """
    Check whether campaign_performance_daily_search has device='ALL' rollup rows.
    Returns True if 'ALL' exists, False otherwise.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM campaign_performance_daily_search "
        "WHERE account_id = ? AND device = 'ALL'",
        (ACCOUNT_ID,)
    ).fetchone()
    return (row["n"] or 0) > 0


# ─── Step 1 / inventory helpers (shared with step1 script) ────────────
def fetch_full_inventory(conn):
    """Every (campaign, state) combo with first/last/days/spend."""
    sql = """
        SELECT
            c.campaign_id,
            c.campaign_name,
            cpg.geo_state,
            MIN(cpg.date)              AS first_date,
            MAX(cpg.date)              AS last_date,
            COUNT(DISTINCT cpg.date)   AS days_active,
            SUM(cpg.impressions)       AS impressions,
            SUM(cpg.clicks)            AS clicks,
            SUM(cpg.cost_micros) / 1e6 AS spend,
            SUM(cpg.conversions)       AS conversions,
            SUM(cpg.conversion_value)  AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND cpg.geo_state IS NOT NULL
        GROUP BY c.campaign_id, c.campaign_name, cpg.geo_state
        ORDER BY c.campaign_name, spend DESC
    """
    return [dict(r) for r in conn.execute(sql, (ACCOUNT_ID,)).fetchall()]


def classify_campaign(name):
    """
    Classify a campaign into one of:
      - 'TIER ONE' / 'TIER TWO' / 'TIER THREE' / 'TIER FOUR'
      - 'SINGLE_STATE' (CA/TX/FL -NB-SEARCH-ALL-USA-STE)
      - 'INTERMEDIATE' (state-named -NB-SEARCH-ALL-USA-STE bridge campaigns
        like HAWAII, CONNECTICUT, etc.)
      - 'LEGACY'       (2025 -SEARCH-MOBILE-USA-STE per-state campaigns)
      - 'PMAX'
      - 'TEST'         (-SEARCH-MOBILE-CPA-STE Mar 2025 short-runs)
      - 'OTHER'        (unrecognized)

    Naming convention in this account (account_id = 1):
      HOME INSURANCE-<TAG>-NB-SEARCH-ALL-USA-STE  → 2026-era campaigns
        <TAG> = TIER ONE|TWO|THREE|FOUR | CALIFORNIA|TEXAS|FLORIDA | other state | PMAX
      HOME INSURANCE-<STATE>-SEARCH-MOBILE-USA-STE → 2025 legacy
      HOME INSURANCE-<STATE>-SEARCH-MOBILE-CPA-STE → 2025 tests
    """
    n = name.upper()

    # Tier campaigns (any HOME INSURANCE campaign with TIER X in the name)
    for kw in TIER_CAMPAIGNS:
        if kw in n and "HOME INSURANCE" in n:
            return kw

    # PMAX (checked before generic Home Insurance rules — PMAX in name wins)
    if "PMAX" in n:
        return "PMAX"

    # 2025 legacy / test campaigns (suffix-based)
    if n.endswith("-SEARCH-MOBILE-CPA-STE"):
        return "TEST"
    if n.endswith("-SEARCH-MOBILE-USA-STE"):
        return "LEGACY"

    # 2026-era campaigns with -NB-SEARCH-ALL-USA-STE suffix
    if n.startswith("HOME INSURANCE-") and n.endswith("-NB-SEARCH-ALL-USA-STE"):
        for s in SINGLE_STATE_CAMPAIGNS:
            if f"-{s}-" in n:
                return "SINGLE_STATE"
        # Any other state-named -NB-SEARCH-ALL-USA-STE = bridge campaign
        return "INTERMEDIATE"

    # Fallback (plain state-name campaigns with no suffix — observed in
    # synthetic test data but not in the real account)
    if not n.startswith("HOME INSURANCE-"):
        return "INTERMEDIATE"

    return "OTHER"


# ─── Step 2: per-state tier profiles ──────────────────────────────────
def fetch_tier_state_profiles(conn, tier_ids, start_date, end_date):
    """
    For each state in each tier campaign, compute window metrics.
    Tier campaigns ONLY. Sum across devices.
    Returns list of dicts.
    """
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
        ORDER BY c.campaign_name, spend DESC
    """
    params = [ACCOUNT_ID] + tier_ids + [start_date.isoformat(), end_date.isoformat()]
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Derive rate metrics. Guard against div-by-zero.
    for r in rows:
        imp = r["impressions"] or 0
        clk = r["clicks"] or 0
        spend = r["spend"] or 0.0
        conv = r["conversions"] or 0.0
        rev = r["revenue"] or 0.0
        days = r["days_active"] or 0

        r["ctr"]             = (clk / imp) if imp > 0 else None
        r["cpc"]             = (spend / clk) if clk > 0 else None
        r["cpa"]             = (spend / conv) if conv > 0 else None
        r["roas"]            = (rev / spend) if spend > 0 else None
        r["value_per_conv"]  = (rev / conv) if conv > 0 else None
        r["avg_daily_spend"] = (spend / days) if days > 0 else None
        r["imp_per_day"]     = (imp / days) if days > 0 else None
        # GMPL proxy: (revenue - cost) / clicks — NOT true GMPL (no COGS)
        r["gmpl_proxy"]      = ((rev - spend) / clk) if clk > 0 else None

    return rows


# ─── Step 3: tier centroids ───────────────────────────────────────────
def compute_tier_centroids(profiles, exclude_states=()):
    """
    For each candidate tier, compute mean + stdev of each metric
    across its states, excluding target states.
    Only includes states with days_active >= MIN_DAYS_ACTIVE_FOR_CENTROID.
    """
    metrics = ["cpa", "roas", "ctr", "imp_per_day"]
    by_tier = defaultdict(list)
    excluded = defaultdict(list)

    for p in profiles:
        tier = None
        for kw in CANDIDATE_TIERS:
            if kw in p["campaign_name"].upper():
                tier = kw
                break
        if tier is None:
            continue
        if p["geo_state"] in exclude_states:
            continue
        if (p["days_active"] or 0) < MIN_DAYS_ACTIVE_FOR_CENTROID:
            excluded[tier].append({
                "state": p["geo_state"],
                "days_active": p["days_active"],
                "reason": f"days_active < {MIN_DAYS_ACTIVE_FOR_CENTROID}",
            })
            continue
        by_tier[tier].append(p)

    centroids = {}
    for tier, members in by_tier.items():
        centroids[tier] = {"n_states": len(members), "members": members}
        for m in metrics:
            vals = [x[m] for x in members if x[m] is not None]
            centroids[tier][f"{m}_mean"] = statistics.mean(vals) if vals else None
            centroids[tier][f"{m}_std"]  = (statistics.stdev(vals)
                                            if len(vals) > 1 else 0.0)
    return centroids, dict(excluded)


# ─── Step 4: similarity scoring ───────────────────────────────────────
def score_similarity(target_profiles, centroids, profiles_all):
    """
    Z-score-normalize each metric using pooled mean/std across all
    candidate-tier states (excluding target states themselves).
    Then compute Euclidean distance from each target state's z-vector
    to each tier's centroid z-vector.
    """
    metrics = ["cpa", "roas", "ctr", "imp_per_day"]

    # Build the pool: states in candidate tiers, excluding targets
    pool = []
    for p in profiles_all:
        campn = p["campaign_name"].upper()
        if not any(kw in campn for kw in CANDIDATE_TIERS):
            continue
        if p["geo_state"] in TARGET_STATES:
            continue
        if (p["days_active"] or 0) < MIN_DAYS_ACTIVE_FOR_CENTROID:
            continue
        pool.append(p)

    # Compute pooled mean/std per metric
    pool_stats = {}
    for m in metrics:
        vals = [x[m] for x in pool if x[m] is not None]
        if vals:
            mu = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
            if sd == 0:
                sd = 1.0
        else:
            mu, sd = 0.0, 1.0
        pool_stats[m] = {"mu": mu, "sd": sd}

    def zvec(profile):
        """Convert a profile's metric values to z-scores using pooled stats."""
        out = []
        for m in metrics:
            v = profile.get(m)
            if v is None:
                out.append(None)
            else:
                s = pool_stats[m]
                out.append((v - s["mu"]) / s["sd"])
        return out

    # Compute tier centroid z-vectors (mean of member z-vectors)
    tier_centroid_z = {}
    for tier, data in centroids.items():
        if tier not in CANDIDATE_TIERS:
            continue
        mem_zs = [zvec(m) for m in data["members"]]
        if not mem_zs:
            continue
        centroid_z = []
        for i in range(len(metrics)):
            vals = [z[i] for z in mem_zs if z[i] is not None]
            centroid_z.append(statistics.mean(vals) if vals else 0.0)
        tier_centroid_z[tier] = centroid_z

    # Score targets
    results = []
    for tp in target_profiles:
        target_z = zvec(tp)
        scores = {}
        for tier, cz in tier_centroid_z.items():
            # Euclidean distance, skipping any None components (replace with 0 — neutral)
            d = 0.0
            for t_val, c_val in zip(target_z, cz):
                tv = t_val if t_val is not None else 0.0
                cv = c_val if c_val is not None else 0.0
                d += (tv - cv) ** 2
            scores[tier] = math.sqrt(d)
        best = min(scores, key=scores.get) if scores else None
        results.append({
            "state": tp["geo_state"],
            "days_active": tp["days_active"],
            "cpa": tp["cpa"], "roas": tp["roas"],
            "ctr": tp["ctr"], "imp_per_day": tp["imp_per_day"],
            "z_cpa": target_z[0], "z_roas": target_z[1],
            "z_ctr": target_z[2], "z_imp_per_day": target_z[3],
            "scores": scores,
            "recommended": best,
        })
    return results, pool_stats, tier_centroid_z


# ─── Step 4.5: Trend-adjusted similarity ──────────────────────────────
TREND_WINDOW_WEEKS = 4           # last N complete ISO weeks
MIN_IMP_PER_DAY    = 30          # volume gate (for noise filter)
MIN_CONV_WINDOW    = 20          # minimum convs across the trend window


def build_trend_profiles(step6_weekly, last_iso, tier_assignments):
    """
    From the Step 6 weekly trend data, build per-state profiles over the
    last TREND_WINDOW_WEEKS complete ISO weeks.

    step6_weekly:       list of dicts (output of stitch_weekly_trend)
    last_iso:           (iso_year, iso_week) of the last complete week
    tier_assignments:   dict state -> tier class (TIER ONE/TWO/THREE/FOUR)
                        — only states in tier campaigns are included

    Returns list of dicts with:
      state, tier, weeks_present, impressions, clicks, spend, conversions,
      revenue, cpa, roas, value_per_conv, imp_per_day, ctr, roas_slope,
      volume_ok (bool), conv_ok (bool), signal_ok (bool), low_signal_reason
    """
    # Select the last N ISO week keys
    ly, lw = last_iso
    # Enumerate backward from (ly, lw) through ISO week arithmetic
    target_weeks = set()
    cursor = date.fromisocalendar(ly, lw, 1)  # Monday of that week
    for _ in range(TREND_WINDOW_WEEKS):
        iy, iw, _ = cursor.isocalendar()
        target_weeks.add((iy, iw))
        cursor -= timedelta(days=7)

    # Filter step6 weekly to target weeks and build per-state arrays
    by_state = defaultdict(list)
    for r in step6_weekly:
        if (r["iso_year"], r["iso_week"]) in target_weeks:
            if r["state"] in tier_assignments:
                by_state[r["state"]].append(r)

    out = []
    for state, rows in by_state.items():
        rows.sort(key=lambda x: (x["iso_year"], x["iso_week"]))

        imp  = sum(x["impressions"] for x in rows)
        clk  = sum(x["clicks"] for x in rows)
        spd  = sum(x["spend"] for x in rows)
        conv = sum(x["conversions"] for x in rows)
        rev  = sum(x["revenue"] for x in rows)
        days = sum(x["days_with_data"] for x in rows)

        cpa  = (spd / conv) if conv > 0 else None
        roas = (rev / spd) if spd > 0 else None
        vpc  = (rev / conv) if conv > 0 else None
        ipd  = (imp / days) if days > 0 else None
        ctr  = (clk / imp) if imp > 0 else None

        # ROAS slope — simple linear regression of weekly ROAS vs week index
        # Only includes weeks with spend > 0 (otherwise ROAS is undefined)
        pts = []
        for i, r in enumerate(rows):
            if r["spend"] > 0 and r["revenue"] is not None:
                pts.append((i, r["revenue"] / r["spend"]))
        roas_slope = None
        if len(pts) >= 2:
            # least-squares slope
            n_p = len(pts)
            mean_x = sum(p[0] for p in pts) / n_p
            mean_y = sum(p[1] for p in pts) / n_p
            num = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pts)
            den = sum((p[0] - mean_x) ** 2 for p in pts)
            roas_slope = (num / den) if den > 0 else None

        volume_ok = (ipd is not None and ipd >= MIN_IMP_PER_DAY)
        conv_ok   = (conv >= MIN_CONV_WINDOW)
        signal_ok = volume_ok and conv_ok
        reason = []
        if not volume_ok:
            reason.append(f"imp/day {ipd:.0f}<{MIN_IMP_PER_DAY}" if ipd else "no impressions")
        if not conv_ok:
            reason.append(f"conv {conv:.0f}<{MIN_CONV_WINDOW}")

        out.append({
            "state": state,
            "tier": tier_assignments[state],
            "weeks_present": len(rows),
            "impressions": imp, "clicks": clk,
            "spend": round(spd, 2),
            "conversions": round(conv, 1),
            "revenue": round(rev, 2),
            "cpa": cpa, "roas": roas,
            "value_per_conv": vpc, "imp_per_day": ipd, "ctr": ctr,
            "roas_slope": roas_slope,
            "volume_ok": volume_ok,
            "conv_ok": conv_ok,
            "signal_ok": signal_ok,
            "low_signal_reason": "; ".join(reason) if reason else "",
        })

    return sorted(out, key=lambda x: (x["tier"], -(x["spend"] or 0)))


def score_trend_adjusted_similarity(trend_profiles, target_states):
    """
    Re-run similarity using 4-week trend profiles instead of 90-day.
    Same z-score + Euclidean approach. Targets excluded from centroids.
    States failing the volume gate are excluded from centroid calc, and
    target states failing the gate are flagged but still scored (with
    caveat).

    Returns (results, pool_stats, centroids_z) — same shape as Step 4.
    """
    metrics = ["cpa", "roas", "ctr", "imp_per_day"]

    # Pool: candidate tier states (not TIER TWO, not targets), signal_ok only
    pool = [p for p in trend_profiles
            if p["tier"] in CANDIDATE_TIERS
            and p["state"] not in target_states
            and p["signal_ok"]]

    # Pooled stats
    pool_stats = {}
    for m in metrics:
        vals = [p[m] for p in pool if p[m] is not None]
        if vals:
            mu = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 1.0
            sd = sd if sd > 0 else 1.0
        else:
            mu, sd = 0.0, 1.0
        pool_stats[m] = {"mu": mu, "sd": sd}

    def zvec(profile):
        out = []
        for m in metrics:
            v = profile.get(m)
            if v is None:
                out.append(None)
            else:
                s = pool_stats[m]
                out.append((v - s["mu"]) / s["sd"])
        return out

    # Candidate tier centroids
    tier_centroid_z = {}
    tier_member_counts = {}
    for tier in CANDIDATE_TIERS:
        members = [p for p in pool if p["tier"] == tier]
        tier_member_counts[tier] = len(members)
        if not members:
            continue
        zs = [zvec(p) for p in members]
        centroid = []
        for i in range(len(metrics)):
            vals = [z[i] for z in zs if z[i] is not None]
            centroid.append(statistics.mean(vals) if vals else 0.0)
        tier_centroid_z[tier] = centroid

    # Score targets
    results = []
    for tp in trend_profiles:
        if tp["state"] not in target_states:
            continue
        z = zvec(tp)
        scores = {}
        for tier, cz in tier_centroid_z.items():
            d = 0.0
            for t_val, c_val in zip(z, cz):
                tv = t_val if t_val is not None else 0.0
                cv = c_val if c_val is not None else 0.0
                d += (tv - cv) ** 2
            scores[tier] = math.sqrt(d)
        best = min(scores, key=scores.get) if scores else None
        results.append({
            "state": tp["state"],
            "weeks_present": tp["weeks_present"],
            "conversions": tp["conversions"],
            "imp_per_day": tp["imp_per_day"],
            "cpa": tp["cpa"], "roas": tp["roas"],
            "ctr": tp["ctr"],
            "value_per_conv": tp["value_per_conv"],
            "roas_slope": tp["roas_slope"],
            "signal_ok": tp["signal_ok"],
            "low_signal_reason": tp["low_signal_reason"],
            "scores": scores,
            "recommended": best,
        })
    return results, pool_stats, tier_centroid_z, tier_member_counts


# ─── Step 4.6: GP impact projection for Utah ──────────────────────────
# tROAS targets are PINNED (source: user input 2026-04-22).
# Correct current-reality values:
TROAS_TARGETS = {
    "TIER ONE":   1.19,   # assumed — confirm with team if needed
    "TIER TWO":   1.10,
    "TIER THREE": 1.19,
    "TIER FOUR":  1.19,
}


def project_gp_impact(trend_profile, current_tier, candidate_tiers,
                       elasticity_scenarios):
    """
    Model Utah's (or any target state's) projected weekly gross profit under
    several volume-response scenarios if moved from current_tier to each
    candidate_tier.

    Inputs:
      trend_profile: the 4-week profile dict for the target state
      current_tier:  "TIER TWO" (where Utah lives today)
      candidate_tiers: list of ["TIER ONE","TIER THREE","TIER FOUR"]
      elasticity_scenarios: list of dicts with
          label, volume_change_pct, roas_achieved

    For each (scenario, candidate_tier) combo, projects:
      - projected weekly spend
      - projected weekly revenue
      - projected weekly GP (as revenue - spend)
      - vs. current baseline GP

    Output: list of row dicts suitable for Excel rendering.
    """
    # Baseline: target state's current weekly performance (4-week average)
    # trend_profile has 4-week totals. Divide by 4 for weekly.
    wk_spend = (trend_profile["spend"] or 0) / 4
    wk_rev   = (trend_profile["revenue"] or 0) / 4
    wk_conv  = (trend_profile["conversions"] or 0) / 4
    wk_clk   = (trend_profile["clicks"] or 0) / 4
    wk_imp   = (trend_profile["impressions"] or 0) / 4
    curr_roas = (wk_rev / wk_spend) if wk_spend > 0 else None
    curr_gp   = wk_rev - wk_spend  # GMPL-style: no COGS

    rows = []
    # Baseline row
    rows.append({
        "scenario": "BASELINE (current 4-wk avg)",
        "target_tier": current_tier,
        "tROAS_target": TROAS_TARGETS.get(current_tier),
        "volume_change_pct": 0.0,
        "roas_achieved": curr_roas,
        "weekly_impressions": round(wk_imp, 0),
        "weekly_clicks": round(wk_clk, 1),
        "weekly_spend": round(wk_spend, 2),
        "weekly_revenue": round(wk_rev, 2),
        "weekly_conversions": round(wk_conv, 1),
        "weekly_gp": round(curr_gp, 2),
        "gp_delta_vs_baseline": 0.0,
        "notes": "Current state in Tier Two @ tROAS 1.10",
    })

    for tier in candidate_tiers:
        target_troas = TROAS_TARGETS.get(tier)
        for sc in elasticity_scenarios:
            # Apply volume change to spend (assuming CPC unchanged short-term)
            new_spend = wk_spend * (1 + sc["volume_change_pct"] / 100.0)
            new_roas = sc["roas_achieved"]
            new_rev  = new_spend * new_roas
            new_gp   = new_rev - new_spend
            rows.append({
                "scenario": sc["label"],
                "target_tier": tier,
                "tROAS_target": target_troas,
                "volume_change_pct": sc["volume_change_pct"],
                "roas_achieved": new_roas,
                "weekly_impressions": round(wk_imp * (1 + sc["volume_change_pct"] / 100.0), 0),
                "weekly_clicks": round(wk_clk * (1 + sc["volume_change_pct"] / 100.0), 1),
                "weekly_spend": round(new_spend, 2),
                "weekly_revenue": round(new_rev, 2),
                "weekly_conversions": round(wk_conv * (1 + sc["volume_change_pct"] / 100.0), 1),
                "weekly_gp": round(new_gp, 2),
                "gp_delta_vs_baseline": round(new_gp - curr_gp, 2),
                "notes": sc.get("notes", ""),
            })
    return rows


# ─── Step 5: Colorado isolation ───────────────────────────────────────
def colorado_isolation(conn, tier2_id, start_date, end_date):
    co = conn.execute("""
        SELECT
            COUNT(DISTINCT date) AS days,
            SUM(clicks)          AS clicks,
            SUM(impressions)     AS impressions,
            SUM(cost_micros)/1e6 AS spend,
            SUM(conversions)     AS conversions,
            SUM(conversion_value) AS revenue
        FROM campaign_performance_geo_daily_search
        WHERE account_id = ? AND campaign_id = ?
          AND geo_state = ? AND date BETWEEN ? AND ?
    """, (ACCOUNT_ID, tier2_id, COLORADO,
          start_date.isoformat(), end_date.isoformat())).fetchone()

    total = conn.execute("""
        SELECT
            SUM(clicks)          AS clicks,
            SUM(impressions)     AS impressions,
            SUM(cost_micros)/1e6 AS spend,
            SUM(conversions)     AS conversions,
            SUM(conversion_value) AS revenue
        FROM campaign_performance_geo_daily_search
        WHERE account_id = ? AND campaign_id = ?
          AND date BETWEEN ? AND ?
    """, (ACCOUNT_ID, tier2_id,
          start_date.isoformat(), end_date.isoformat())).fetchone()

    return dict(co), dict(total)


# ─── Step 0: long-range weekly trends (ISO W01 2025 → last complete week) ─
def fetch_pmax_campaign_ids(conn):
    """Return set of campaign_ids classified as PMAX — to exclude."""
    rows = conn.execute(
        "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id = ?",
        (ACCOUNT_ID,)
    ).fetchall()
    return {r["campaign_id"] for r in rows
            if classify_campaign(r["campaign_name"]) == "PMAX"}


def fetch_account_weekly_trend(conn, end_date, excluded_ids):
    """
    Whole-account weekly performance, ISO W01 2025 through the last
    COMPLETE ISO week on or before end_date. PMAX excluded.

    Returns list of dicts keyed (iso_year, iso_week) with aggregate metrics.
    """
    last_iy, last_iw = last_complete_iso_week(end_date)

    # Pull daily rows, then aggregate to ISO week in Python
    start = date(2025, 1, 1)  # anything in Jan 2025 — we'll filter to W01 onward
    placeholders = ("?,") * len(excluded_ids)
    placeholders = placeholders.rstrip(",") if excluded_ids else ""

    sql = """
        SELECT
            cpg.date,
            SUM(cpg.impressions) AS impressions,
            SUM(cpg.clicks)      AS clicks,
            SUM(cpg.cost_micros)/1e6 AS spend,
            SUM(cpg.conversions) AS conversions,
            SUM(cpg.conversion_value) AS revenue
        FROM campaign_performance_geo_daily_search cpg
        WHERE cpg.account_id = ?
          AND cpg.date >= ?
          AND cpg.date <= ?
    """
    params = [ACCOUNT_ID, start.isoformat(), end_date.isoformat()]
    if excluded_ids:
        sql += f" AND cpg.campaign_id NOT IN ({placeholders})"
        params.extend(excluded_ids)
    sql += " GROUP BY cpg.date"

    daily = conn.execute(sql, params).fetchall()

    # Aggregate to ISO week, drop partial (i.e. anything beyond last_complete)
    weekly = defaultdict(lambda: {
        "impressions": 0, "clicks": 0, "spend": 0.0,
        "conversions": 0.0, "revenue": 0.0, "days": set(),
    })
    for r in daily:
        dt = date.fromisoformat(r["date"])
        iy, iw, _ = dt.isocalendar()
        # Skip ISO weeks after the last complete one
        if (iy, iw) > (last_iy, last_iw):
            continue
        k = (iy, iw)
        w = weekly[k]
        w["impressions"] += r["impressions"] or 0
        w["clicks"]      += r["clicks"] or 0
        w["spend"]       += r["spend"] or 0.0
        w["conversions"] += r["conversions"] or 0.0
        w["revenue"]     += r["revenue"] or 0.0
        w["days"].add(r["date"])

    out = []
    for (iy, iw), w in sorted(weekly.items()):
        imp  = w["impressions"]
        clk  = w["clicks"]
        spd  = w["spend"]
        conv = w["conversions"]
        rev  = w["revenue"]
        days = len(w["days"])
        out.append({
            "iso_year": iy,
            "iso_week": iw,
            "week_label": f"{iy}-W{iw:02d}",
            "days_with_data": days,
            "impressions": imp,
            "clicks": clk,
            "spend": round(spd, 2),
            "conversions": round(conv, 1),
            "revenue": round(rev, 2),
            "ctr":  (clk / imp) if imp > 0 else None,
            "cpc":  (spd / clk) if clk > 0 else None,
            "cpa":  (spd / conv) if conv > 0 else None,
            "roas": (rev / spd) if spd > 0 else None,
            "value_per_conv": (rev / conv) if conv > 0 else None,
            "imp_per_day":    (imp / days) if days > 0 else None,
            "avg_daily_spend": (spd / days) if days > 0 else None,
        })
    return out


def fetch_state_weekly_trend(conn, end_date, states, excluded_ids):
    """
    Per-state weekly performance, ISO W01 2025 through last complete ISO
    week on or before end_date. Aggregates across ALL campaigns the state
    appears in (except PMAX / excluded_ids). Used for Utah + South Dakota
    trend context.
    """
    last_iy, last_iw = last_complete_iso_week(end_date)
    start = date(2025, 1, 1)

    state_placeholders = ",".join("?" * len(states))
    ex_placeholders = ",".join("?" * len(excluded_ids)) if excluded_ids else ""

    sql = f"""
        SELECT
            cpg.date, cpg.geo_state,
            SUM(cpg.impressions)      AS impressions,
            SUM(cpg.clicks)           AS clicks,
            SUM(cpg.cost_micros)/1e6  AS spend,
            SUM(cpg.conversions)      AS conversions,
            SUM(cpg.conversion_value) AS revenue
        FROM campaign_performance_geo_daily_search cpg
        WHERE cpg.account_id = ?
          AND cpg.date >= ?
          AND cpg.date <= ?
          AND cpg.geo_state IN ({state_placeholders})
    """
    params = [ACCOUNT_ID, start.isoformat(), end_date.isoformat()] + list(states)
    if excluded_ids:
        sql += f" AND cpg.campaign_id NOT IN ({ex_placeholders})"
        params.extend(excluded_ids)
    sql += " GROUP BY cpg.date, cpg.geo_state"

    daily = conn.execute(sql, params).fetchall()

    weekly = defaultdict(lambda: {
        "impressions": 0, "clicks": 0, "spend": 0.0,
        "conversions": 0.0, "revenue": 0.0, "days": set(),
    })
    for r in daily:
        dt = date.fromisoformat(r["date"])
        iy, iw, _ = dt.isocalendar()
        if (iy, iw) > (last_iy, last_iw):
            continue
        k = (r["geo_state"], iy, iw)
        w = weekly[k]
        w["impressions"] += r["impressions"] or 0
        w["clicks"]      += r["clicks"] or 0
        w["spend"]       += r["spend"] or 0.0
        w["conversions"] += r["conversions"] or 0.0
        w["revenue"]     += r["revenue"] or 0.0
        w["days"].add(r["date"])

    out = []
    for (state, iy, iw), w in sorted(weekly.items()):
        imp  = w["impressions"]
        clk  = w["clicks"]
        spd  = w["spend"]
        conv = w["conversions"]
        rev  = w["revenue"]
        days = len(w["days"])
        out.append({
            "state": state,
            "iso_year": iy, "iso_week": iw,
            "week_label": f"{iy}-W{iw:02d}",
            "days_with_data": days,
            "impressions": imp, "clicks": clk,
            "spend": round(spd, 2),
            "conversions": round(conv, 1),
            "revenue": round(rev, 2),
            "ctr":  (clk / imp) if imp > 0 else None,
            "cpc":  (spd / clk) if clk > 0 else None,
            "cpa":  (spd / conv) if conv > 0 else None,
            "roas": (rev / spd) if spd > 0 else None,
            "value_per_conv": (rev / conv) if conv > 0 else None,
            "imp_per_day":    (imp / days) if days > 0 else None,
        })
    return out


# ─── Step 6: weekly trend (ISO week, 2026 only, starting week 2) ──────
def fetch_weekly_trend(conn, end_date):
    """
    Pull daily rows from 2026-01-05 (ISO W02 start) → end_date for:
      - Tier campaigns (all states)
      - Intermediate 2026 campaigns (state-matched)
      - Single-state campaigns (CA, TX, FL)
    Caller handles ISO week grouping in Python for reliability.
    """
    start = date(2026, 1, 5)  # ISO week 2 Monday
    if end_date < start:
        return []
    sql = """
        SELECT
            c.campaign_id,
            c.campaign_name,
            cpg.geo_state,
            cpg.date,
            SUM(cpg.clicks)          AS clicks,
            SUM(cpg.impressions)     AS impressions,
            SUM(cpg.cost_micros)/1e6 AS spend,
            SUM(cpg.conversions)     AS conversions,
            SUM(cpg.conversion_value) AS revenue
        FROM campaign_performance_geo_daily_search cpg
        JOIN campaigns c ON c.campaign_id = cpg.campaign_id
        WHERE cpg.account_id = ?
          AND cpg.date BETWEEN ? AND ?
          AND cpg.geo_state IS NOT NULL
        GROUP BY c.campaign_id, c.campaign_name, cpg.geo_state, cpg.date
    """
    return [dict(r) for r in conn.execute(
        sql, (ACCOUNT_ID, start.isoformat(), end_date.isoformat())
    ).fetchall()]


def stitch_weekly_trend(daily_rows, end_date):
    """
    Attribute each daily row to a state's trend line. Rules:
      - Tier campaign rows → state's tier trend (one row per state per day)
      - Intermediate 2026 campaign rows (state-name only, not Home Insurance,
        not PMAX/LEGACY/TEST): attribute to that state's trend if the state
        is in a Tier campaign (stitches pre-Tier 2026 history)
      - CA/TX/FL single-state campaign rows → those states' trends
      - LEGACY/PMAX/TEST/OTHER: excluded
    Aggregate to ISO week.
    """
    # Find which states are in tier campaigns (eligible for stitching)
    tier_states = set()
    for r in daily_rows:
        cls = classify_campaign(r["campaign_name"])
        if cls in CANDIDATE_TIERS + ["TIER TWO"]:
            tier_states.add(r["geo_state"])

    # Daily aggregation keyed by state
    by_state_day = defaultdict(lambda: {
        "clicks": 0, "impressions": 0, "spend": 0.0,
        "conversions": 0.0, "revenue": 0.0, "sources": set()
    })

    for r in daily_rows:
        cls = classify_campaign(r["campaign_name"])
        state = r["geo_state"]
        keep = False
        source = None

        if cls in CANDIDATE_TIERS or cls == "TIER TWO":
            keep = True
            source = cls
        elif cls == "INTERMEDIATE" and state in tier_states:
            keep = True
            source = "INTERMEDIATE"
        elif cls == "SINGLE_STATE":
            keep = True
            source = "SINGLE_STATE"

        if not keep:
            continue

        key = (state, r["date"])
        agg = by_state_day[key]
        agg["clicks"]      += r["clicks"] or 0
        agg["impressions"] += r["impressions"] or 0
        agg["spend"]       += r["spend"] or 0.0
        agg["conversions"] += r["conversions"] or 0.0
        agg["revenue"]     += r["revenue"] or 0.0
        agg["sources"].add(source)

    # Aggregate to ISO week
    weekly = defaultdict(lambda: {
        "clicks": 0, "impressions": 0, "spend": 0.0,
        "conversions": 0.0, "revenue": 0.0, "days": set(), "sources": set()
    })
    for (state, date_str), d in by_state_day.items():
        dt = date.fromisoformat(date_str)
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = (state, iso_year, iso_week)
        w = weekly[week_key]
        w["clicks"]      += d["clicks"]
        w["impressions"] += d["impressions"]
        w["spend"]       += d["spend"]
        w["conversions"] += d["conversions"]
        w["revenue"]     += d["revenue"]
        w["days"].add(date_str)
        w["sources"] |= d["sources"]

    # Drop the partial final week (if end_date doesn't fall on a Sunday,
    # the ISO week containing end_date is incomplete — drop it entirely).
    last_iy, last_iw = last_complete_iso_week(end_date)

    # Emit rows
    out = []
    for (state, iy, iw), w in sorted(weekly.items()):
        if (iy, iw) > (last_iy, last_iw):
            continue
        spend = w["spend"]
        conv = w["conversions"]
        cpa = (spend / conv) if conv > 0 else None
        out.append({
            "state": state, "iso_year": iy, "iso_week": iw,
            "week_label": f"{iy}-W{iw:02d}",
            "days_with_data": len(w["days"]),
            "clicks": w["clicks"], "impressions": w["impressions"],
            "spend": round(spend, 2), "conversions": round(conv, 1),
            "revenue": round(w["revenue"], 2), "cpa": cpa,
            "sources": ",".join(sorted(w["sources"])),
        })
    return out


# ─── Excel writing ────────────────────────────────────────────────────
def write_workbook(path, data):
    """Write all sheets. `data` is a dict keyed by step name."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)  # drop default sheet

    def add_sheet(title, headers, rows, col_widths=None, number_formats=None):
        ws = wb.create_sheet(title=title[:31])  # Excel sheet name limit
        ws.append(headers)
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=i)
            c.font = Font(bold=True, color=HEADER_FONT, name="Arial")
            c.fill = PatternFill("solid", start_color=HEADER_FILL,
                                 end_color=HEADER_FILL)
            c.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        # Autowidth (rough)
        for i, h in enumerate(headers, 1):
            col = get_column_letter(i)
            max_len = max(len(str(h)),
                          *(len(str(r[i-1])) if i-1 < len(r) else 0 for r in rows[:200]),
                          8)
            ws.column_dimensions[col].width = min(max_len + 2, 40)
        if number_formats:
            for col_idx, fmt in number_formats.items():
                col_letter = get_column_letter(col_idx)
                for row_idx in range(2, len(rows) + 2):
                    ws[f"{col_letter}{row_idx}"].number_format = fmt
        if col_widths:
            for col_idx, w in col_widths.items():
                ws.column_dimensions[get_column_letter(col_idx)].width = w
        return ws

    # Step 0a: Account-level weekly trend (ISO W01 2025 → last complete week)
    s0_acct = data.get("step0_account", [])
    headers = ["Week", "ISO Year", "ISO Week", "Days w/ Data",
               "Impressions", "Clicks", "Spend", "Conversions", "Revenue",
               "CTR", "CPC", "CPA", "ROAS", "Value/Conv",
               "Impressions/Day", "Avg Daily Spend"]
    rows = []
    for r in s0_acct:
        rows.append([
            r["week_label"], r["iso_year"], r["iso_week"], r["days_with_data"],
            r["impressions"], r["clicks"], r["spend"], r["conversions"],
            r["revenue"],
            round(r["ctr"], 4) if r["ctr"] is not None else None,
            round(r["cpc"], 2) if r["cpc"] is not None else None,
            round(r["cpa"], 2) if r["cpa"] is not None else None,
            round(r["roas"], 2) if r["roas"] is not None else None,
            round(r["value_per_conv"], 2) if r["value_per_conv"] is not None else None,
            round(r["imp_per_day"], 0) if r["imp_per_day"] is not None else None,
            round(r["avg_daily_spend"], 2) if r["avg_daily_spend"] is not None else None,
        ])
    add_sheet("0a. Account Weekly Trend", headers, rows,
              number_formats={5: "#,##0", 6: "#,##0", 7: "$#,##0.00",
                              8: "#,##0.0", 9: "$#,##0.00",
                              10: "0.00%", 11: "$0.00", 12: "$0.00",
                              13: "0.00", 14: "$0.00",
                              15: "#,##0", 16: "$#,##0.00"})

    # Step 0b: Utah + South Dakota weekly trend across all campaigns (ex-PMAX)
    s0_states = data.get("step0_states", [])
    headers = ["State", "Week", "ISO Year", "ISO Week", "Days w/ Data",
               "Impressions", "Clicks", "Spend", "Conversions", "Revenue",
               "CTR", "CPC", "CPA", "ROAS", "Value/Conv", "Impressions/Day"]
    rows = []
    for r in s0_states:
        rows.append([
            r["state"], r["week_label"], r["iso_year"], r["iso_week"],
            r["days_with_data"], r["impressions"], r["clicks"], r["spend"],
            r["conversions"], r["revenue"],
            round(r["ctr"], 4) if r["ctr"] is not None else None,
            round(r["cpc"], 2) if r["cpc"] is not None else None,
            round(r["cpa"], 2) if r["cpa"] is not None else None,
            round(r["roas"], 2) if r["roas"] is not None else None,
            round(r["value_per_conv"], 2) if r["value_per_conv"] is not None else None,
            round(r["imp_per_day"], 0) if r["imp_per_day"] is not None else None,
        ])
    add_sheet("0b. UT & SD Weekly Trend", headers, rows,
              number_formats={6: "#,##0", 7: "#,##0", 8: "$#,##0.00",
                              9: "#,##0.0", 10: "$#,##0.00",
                              11: "0.00%", 12: "$0.00", 13: "$0.00",
                              14: "0.00", 15: "$0.00", 16: "#,##0"})

    # Step 1: Inventory
    s1 = data["step1"]
    headers = ["Campaign", "Campaign Class", "State", "First Date", "Last Date",
               "Days Active", "Impressions", "Clicks", "Spend", "Conversions",
               "Revenue", "Currently Active", "Cross-Tier Note"]
    rows = [
        [r["campaign_name"], r["campaign_class"], r["geo_state"],
         r["first_date"], r["last_date"], r["days_active"],
         r["impressions"] or 0, r["clicks"] or 0,
         round(r["spend"] or 0, 2),
         round(r["conversions"] or 0, 1),
         round(r["revenue"] or 0, 2),
         "YES" if r["currently_active"] else "no",
         r.get("cross_tier_note", "")]
        for r in s1
    ]
    add_sheet("1. Inventory", headers, rows,
              number_formats={7: "#,##0", 8: "#,##0", 9: "$#,##0.00",
                              10: "#,##0.0", 11: "$#,##0.00"})

    # Step 2: Tier profiles
    s2 = data["step2"]
    headers = ["Tier", "State", "Days Active", "Impressions", "Clicks",
               "Spend", "Conversions", "Revenue", "Avg Daily Spend",
               "Impressions/Day", "CTR", "CPC", "CPA", "ROAS",
               "Value/Conv", "GMPL Proxy", "IS Lost (Rank)"]
    rows = []
    for r in s2:
        rows.append([
            r["tier_short"], r["geo_state"], r["days_active"],
            r["impressions"] or 0, r["clicks"] or 0,
            round(r["spend"] or 0, 2),
            round(r["conversions"] or 0, 1),
            round(r["revenue"] or 0, 2),
            round(r["avg_daily_spend"] or 0, 2) if r["avg_daily_spend"] else None,
            round(r["imp_per_day"] or 0, 0) if r["imp_per_day"] else None,
            round(r["ctr"], 4) if r["ctr"] is not None else None,
            round(r["cpc"], 2) if r["cpc"] is not None else None,
            round(r["cpa"], 2) if r["cpa"] is not None else None,
            round(r["roas"], 2) if r["roas"] is not None else None,
            round(r["value_per_conv"], 2) if r["value_per_conv"] is not None else None,
            round(r["gmpl_proxy"], 4) if r["gmpl_proxy"] is not None else None,
            round(r.get("search_rank_lost_is") or 0, 4) if r.get("search_rank_lost_is") is not None else None,
        ])
    add_sheet("2. Tier State Profiles", headers, rows,
              number_formats={4: "#,##0", 5: "#,##0", 6: "$#,##0.00",
                              7: "#,##0.0", 8: "$#,##0.00", 9: "$#,##0.00",
                              10: "#,##0", 11: "0.00%", 12: "$0.00",
                              13: "$0.00", 14: "0.00", 15: "$0.00",
                              16: "$0.0000", 17: "0.00%"})

    # Step 3: Tier centroids
    s3 = data["step3"]
    headers = ["Tier", "N States", "CPA Mean", "CPA Std", "ROAS Mean", "ROAS Std",
               "CTR Mean", "CTR Std", "Imp/Day Mean", "Imp/Day Std",
               "Excluded States (days_active<30)"]
    rows = []
    for tier in CANDIDATE_TIERS:
        c = s3["centroids"].get(tier, {})
        excl = s3["excluded"].get(tier, [])
        rows.append([
            tier, c.get("n_states", 0),
            round(c.get("cpa_mean") or 0, 2) if c.get("cpa_mean") else None,
            round(c.get("cpa_std") or 0, 2) if c.get("cpa_std") is not None else None,
            round(c.get("roas_mean") or 0, 3) if c.get("roas_mean") else None,
            round(c.get("roas_std") or 0, 3) if c.get("roas_std") is not None else None,
            round(c.get("ctr_mean") or 0, 4) if c.get("ctr_mean") else None,
            round(c.get("ctr_std") or 0, 4) if c.get("ctr_std") is not None else None,
            round(c.get("imp_per_day_mean") or 0, 0) if c.get("imp_per_day_mean") else None,
            round(c.get("imp_per_day_std") or 0, 0) if c.get("imp_per_day_std") is not None else None,
            "; ".join(f"{e['state']} ({e['days_active']}d)" for e in excl) or "—",
        ])
    add_sheet("3. Tier Centroids", headers, rows,
              number_formats={3: "$0.00", 4: "$0.00", 5: "0.000", 6: "0.000",
                              7: "0.00%", 8: "0.00%", 9: "#,##0", 10: "#,##0"})

    # Step 4: Similarity
    s4 = data["step4"]
    headers = ["State", "Days Active", "CPA", "ROAS", "CTR", "Imp/Day",
               "Distance → Tier One", "Distance → Tier Three",
               "Distance → Tier Four", "Recommended Tier", "Caveat"]
    rows = []
    for r in s4["results"]:
        caveat = ""
        if r["state"] == "South Dakota":
            caveat = "Small sample — interpret with care"
        rows.append([
            r["state"], r["days_active"],
            round(r["cpa"] or 0, 2) if r["cpa"] else None,
            round(r["roas"] or 0, 2) if r["roas"] else None,
            round(r["ctr"] or 0, 4) if r["ctr"] is not None else None,
            round(r["imp_per_day"] or 0, 0) if r["imp_per_day"] else None,
            round(r["scores"].get("TIER ONE") or 0, 3) if r["scores"].get("TIER ONE") is not None else None,
            round(r["scores"].get("TIER THREE") or 0, 3) if r["scores"].get("TIER THREE") is not None else None,
            round(r["scores"].get("TIER FOUR") or 0, 3) if r["scores"].get("TIER FOUR") is not None else None,
            r["recommended"] or "—",
            caveat,
        ])
    add_sheet("4. Similarity Scoring", headers, rows,
              number_formats={3: "$0.00", 4: "0.00", 5: "0.00%", 6: "#,##0",
                              7: "0.000", 8: "0.000", 9: "0.000"})

    # Step 4.5: Trend-adjusted recommendation
    s45 = data.get("step4_5", {})
    # 4.5a — All tier states' trend profiles (context)
    headers = ["Tier", "State", "Weeks", "Spend", "Conversions",
               "ROAS", "CPA", "Value/Conv", "CTR", "Imp/Day",
               "ROAS Slope", "Signal OK?", "Low-Signal Reason"]
    rows = []
    for p in s45.get("trend_profiles", []):
        rows.append([
            p["tier"], p["state"], p["weeks_present"],
            round(p["spend"], 2),
            round(p["conversions"], 1),
            round(p["roas"], 2) if p["roas"] is not None else None,
            round(p["cpa"], 2) if p["cpa"] is not None else None,
            round(p["value_per_conv"], 2) if p["value_per_conv"] is not None else None,
            round(p["ctr"], 4) if p["ctr"] is not None else None,
            round(p["imp_per_day"], 0) if p["imp_per_day"] is not None else None,
            round(p["roas_slope"], 3) if p["roas_slope"] is not None else None,
            "YES" if p["signal_ok"] else "NO",
            p["low_signal_reason"],
        ])
    add_sheet("4.5a Trend Profiles (4wk)", headers, rows,
              number_formats={4: "$#,##0.00", 5: "#,##0.0",
                              6: "0.00", 7: "$0.00", 8: "$0.00",
                              9: "0.00%", 10: "#,##0",
                              11: "+0.000;-0.000;0.000"})

    # 4.5b — Utah & South Dakota ranked against trend-adjusted centroids
    headers = ["State", "Weeks", "Conv", "Imp/Day", "ROAS", "CPA",
               "Value/Conv", "CTR", "ROAS Slope",
               "Distance → Tier One", "Distance → Tier Three",
               "Distance → Tier Four",
               "Recommended (Trend)", "Signal", "Caveat"]
    rows = []
    for r in s45.get("trend_results", []):
        signal = "OK" if r["signal_ok"] else f"LOW ({r['low_signal_reason']})"
        caveat = ""
        if not r["signal_ok"]:
            caveat = "Below volume gate — recommendation is low-confidence"
        rows.append([
            r["state"], r["weeks_present"],
            round(r["conversions"], 1),
            round(r["imp_per_day"], 0) if r["imp_per_day"] is not None else None,
            round(r["roas"], 2) if r["roas"] is not None else None,
            round(r["cpa"], 2) if r["cpa"] is not None else None,
            round(r["value_per_conv"], 2) if r["value_per_conv"] is not None else None,
            round(r["ctr"], 4) if r["ctr"] is not None else None,
            round(r["roas_slope"], 3) if r["roas_slope"] is not None else None,
            round(r["scores"].get("TIER ONE"), 3) if r["scores"].get("TIER ONE") is not None else None,
            round(r["scores"].get("TIER THREE"), 3) if r["scores"].get("TIER THREE") is not None else None,
            round(r["scores"].get("TIER FOUR"), 3) if r["scores"].get("TIER FOUR") is not None else None,
            r["recommended"] or "—",
            signal, caveat,
        ])
    add_sheet("4.5b Trend-Adjusted Rec", headers, rows,
              number_formats={3: "#,##0.0", 4: "#,##0", 5: "0.00",
                              6: "$0.00", 7: "$0.00", 8: "0.00%",
                              9: "+0.000;-0.000;0.000",
                              10: "0.000", 11: "0.000", 12: "0.000"})

    # Step 4.6: GP impact projection
    s46 = data.get("step4_6", [])
    headers = ["State", "Scenario", "Target Tier", "tROAS Target",
               "Volume Δ%", "ROAS Achieved",
               "Weekly Impressions", "Weekly Clicks", "Weekly Spend",
               "Weekly Revenue", "Weekly Conversions",
               "Weekly GP", "GP Δ vs Baseline", "Notes"]
    rows = []
    for r in s46:
        rows.append([
            r["state"], r["scenario"], r["target_tier"],
            r["tROAS_target"],
            r["volume_change_pct"] / 100.0 if r["volume_change_pct"] is not None else None,
            round(r["roas_achieved"], 3) if r["roas_achieved"] is not None else None,
            r["weekly_impressions"], r["weekly_clicks"],
            r["weekly_spend"], r["weekly_revenue"], r["weekly_conversions"],
            r["weekly_gp"], r["gp_delta_vs_baseline"],
            r["notes"],
        ])
    add_sheet("4.6 GP Impact Projection", headers, rows,
              number_formats={4: "0.00", 5: "+0.0%;-0.0%;0.0%", 6: "0.00",
                              7: "#,##0", 8: "#,##0.0", 9: "$#,##0.00",
                              10: "$#,##0.00", 11: "#,##0.0",
                              12: "$#,##0.00",
                              13: "+$#,##0.00;-$#,##0.00;$0.00"})

    # Step 5: Colorado isolation
    s5 = data["step5"]
    co = s5["colorado"]
    tot = s5["tier2_total"]
    headers = ["Metric", "Colorado", "Tier Two Total", "Colorado %"]
    def pct(a, b):
        return (a / b) if (a is not None and b) else None
    rows = [
        ["Days with data", co["days"], "—", None],
        ["Impressions", co["impressions"] or 0, tot["impressions"] or 0,
         pct(co["impressions"] or 0, tot["impressions"] or 0)],
        ["Clicks", co["clicks"] or 0, tot["clicks"] or 0,
         pct(co["clicks"] or 0, tot["clicks"] or 0)],
        ["Spend", round(co["spend"] or 0, 2), round(tot["spend"] or 0, 2),
         pct(co["spend"] or 0, tot["spend"] or 0)],
        ["Conversions", round(co["conversions"] or 0, 1),
         round(tot["conversions"] or 0, 1),
         pct(co["conversions"] or 0, tot["conversions"] or 0)],
        ["Revenue", round(co["revenue"] or 0, 2), round(tot["revenue"] or 0, 2),
         pct(co["revenue"] or 0, tot["revenue"] or 0)],
    ]
    add_sheet("5. Colorado Isolation", headers, rows,
              number_formats={4: "0.0%"})

    # Step 6: Weekly trend
    s6 = data["step6"]
    headers = ["State", "Week", "ISO Year", "ISO Week", "Days w/ Data",
               "Spend", "Conversions", "CPA", "Revenue", "Clicks", "Sources"]
    rows = []
    for r in s6:
        rows.append([
            r["state"], r["week_label"], r["iso_year"], r["iso_week"],
            r["days_with_data"], r["spend"], r["conversions"],
            round(r["cpa"] or 0, 2) if r["cpa"] is not None else None,
            r["revenue"], r["clicks"], r["sources"],
        ])
    add_sheet("6. Weekly Trend 2026", headers, rows,
              number_formats={6: "$#,##0.00", 7: "#,##0.0", 8: "$0.00",
                              9: "$#,##0.00", 10: "#,##0"})

    # Step 7: Diagnostic
    s7 = data["step7"]
    headers = ["Item", "Value"]
    rows = [[k, v] for k, v in s7.items()]
    add_sheet("7. Diagnostic", headers, rows, col_widths={1: 40, 2: 60})

    wb.save(path)


# ─── Inventory builder (Step 1) ───────────────────────────────────────
def build_step1_inventory(conn, end_date):
    rows = fetch_full_inventory(conn)
    active_cutoff = end_date - timedelta(days=30)

    # Classify + add active flag + handle tier cross-dup
    for r in rows:
        r["campaign_class"] = classify_campaign(r["campaign_name"])
        r["last_date_obj"] = date.fromisoformat(r["last_date"])
        r["currently_active"] = r["last_date_obj"] >= active_cutoff
        r["cross_tier_dropped"] = False
        r["cross_tier_note"] = ""

    # Cross-tier dedup on tier campaigns only
    state_to_tiers = defaultdict(list)
    for r in rows:
        if r["campaign_class"] in TIER_CAMPAIGNS:
            state_to_tiers[r["geo_state"]].append(r)
    for state, tier_rows in state_to_tiers.items():
        if len(tier_rows) <= 1:
            continue
        winner = max(tier_rows, key=lambda x: (x["last_date_obj"], x["spend"] or 0))
        for tr in tier_rows:
            if tr is winner:
                tr["cross_tier_note"] = (
                    f"KEPT (most recent activity {tr['last_date']}; "
                    f"also in {len(tier_rows)-1} other tier(s))"
                )
            else:
                tr["cross_tier_dropped"] = True
                tr["cross_tier_note"] = (
                    f"DROPPED (stale; last={tr['last_date']}, "
                    f"winner {winner['campaign_class']} last={winner['last_date']})"
                )
    return rows


# ─── Main orchestration ───────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Home Insurance geo restructure analysis.")
    p.add_argument("--end-date", default=None,
                   help="End date YYYY-MM-DD (default: yesterday).")
    p.add_argument("--days", type=int, default=90,
                   help="Analysis window days (default: 90).")
    p.add_argument("--dry-run", action="store_true",
                   help="Smoke test + terminal diagnostic only — no Excel output.")
    args = p.parse_args()

    end_date = (date.fromisoformat(args.end_date) if args.end_date
                else date.today() - timedelta(days=1))
    start_date = end_date - timedelta(days=args.days - 1)

    conn = get_conn()

    print("=" * 80)
    print("GEO RESTRUCTURE ANALYSIS — Home Insurance (account_id = 1)")
    print(f"  Database   : {DB_PATH}")
    print(f"  Window     : {start_date} → {end_date}  ({args.days} days)")
    print(f"  Output dir : {OUTPUT_DIR}")
    if args.dry_run:
        print(f"  Mode       : DRY RUN (no Excel output)")
    print("=" * 80)

    # Resolve tier campaign IDs
    tier_map = tier_campaign_id_map(conn)
    print("\nTier campaigns resolved:")
    for kw, info in tier_map.items():
        print(f"  {kw:<11} {info['id']}  {info['name']}")
    tier_ids = [v["id"] for v in tier_map.values()]

    # Verify daily table device convention
    has_all = verify_daily_has_all_rollup(conn)
    print(f"\ncampaign_performance_daily_search has device='ALL' rollup: {has_all}")

    # Resolve PMAX campaign IDs for exclusion
    pmax_ids = fetch_pmax_campaign_ids(conn)
    print(f"PMAX campaigns excluded from analysis: {len(pmax_ids)}")

    # ── Step 0: Long-range weekly trends (ISO W01 2025 → last complete week) ─
    last_iy, last_iw = last_complete_iso_week(end_date)
    print(f"\n── Step 0: Long-range weekly trends ──")
    print(f"  Window: ISO W01 2025 → {last_iy}-W{last_iw:02d} "
          f"(last complete ISO week on/before {end_date})")
    print(f"  PMAX excluded.")

    acct_weekly = fetch_account_weekly_trend(conn, end_date, pmax_ids)
    print(f"  Step 0a (account-level): {len(acct_weekly)} weeks")

    state_weekly = fetch_state_weekly_trend(conn, end_date, TARGET_STATES, pmax_ids)
    print(f"  Step 0b (UT & SD):       {len(state_weekly)} rows")

    # ── Step 1 ────────────────────────────────────────────────────────
    print("\n── Step 1: Full campaign × state inventory ──")
    inventory = build_step1_inventory(conn, end_date)
    print(f"  Rows: {len(inventory):,}")
    print(f"  Distinct campaigns: {len(set(r['campaign_name'] for r in inventory))}")
    print(f"  Distinct states:    {len(set(r['geo_state'] for r in inventory))}")
    class_counts = defaultdict(int)
    for r in inventory:
        class_counts[r["campaign_class"]] += 1
    for cls in sorted(class_counts.keys()):
        print(f"    {cls:<15} {class_counts[cls]:>4}")

    # ── Step 2 ────────────────────────────────────────────────────────
    print("\n── Step 2: Tier state performance profiles ──")
    profiles = fetch_tier_state_profiles(conn, tier_ids, start_date, end_date)

    # Add IS join
    is_by_campaign_window = {}
    if has_all:
        for kw, info in tier_map.items():
            row = conn.execute("""
                SELECT AVG(search_rank_lost_is) AS avg_is
                FROM campaign_performance_daily_search
                WHERE account_id = ? AND campaign_id = ? AND device = 'ALL'
                  AND date BETWEEN ? AND ?
            """, (ACCOUNT_ID, info["id"],
                  start_date.isoformat(), end_date.isoformat())).fetchone()
            is_by_campaign_window[info["id"]] = row["avg_is"]
    else:
        # Impression-weighted fallback
        for kw, info in tier_map.items():
            rows = conn.execute("""
                SELECT date, device, impressions, search_rank_lost_is
                FROM campaign_performance_daily_search
                WHERE account_id = ? AND campaign_id = ?
                  AND date BETWEEN ? AND ?
            """, (ACCOUNT_ID, info["id"],
                  start_date.isoformat(), end_date.isoformat())).fetchall()
            num = sum((r["impressions"] or 0) * (r["search_rank_lost_is"] or 0)
                      for r in rows)
            den = sum(r["impressions"] or 0 for r in rows)
            is_by_campaign_window[info["id"]] = (num / den) if den else None

    # Attach tier_short + IS lookup
    for p_row in profiles:
        for kw in TIER_CAMPAIGNS:
            if kw in p_row["campaign_name"].upper():
                p_row["tier_short"] = kw
                break
        p_row["search_rank_lost_is"] = is_by_campaign_window.get(p_row["campaign_id"])

    print(f"  Profile rows: {len(profiles):,}")

    # ── Step 3 ────────────────────────────────────────────────────────
    print("\n── Step 3: Tier centroids ──")
    centroids, excluded = compute_tier_centroids(profiles,
                                                  exclude_states=TARGET_STATES)
    for tier in CANDIDATE_TIERS:
        c = centroids.get(tier, {})
        excl = excluded.get(tier, [])
        cpa_m = c.get("cpa_mean")
        roas_m = c.get("roas_mean")
        ctr_m = c.get("ctr_mean")
        cpa_s = f"${cpa_m:.2f}" if cpa_m is not None else "n/a"
        roas_s = f"{roas_m:.2f}" if roas_m is not None else "n/a"
        ctr_s = f"{ctr_m*100:.2f}%" if ctr_m is not None else "n/a"
        print(f"  {tier:<11}  n={c.get('n_states', 0):<2}  "
              f"CPA={cpa_s:<8}  ROAS={roas_s:<6}  CTR={ctr_s}")
        if excl:
            print(f"               excluded: " +
                  ", ".join(f"{e['state']}({e['days_active']}d)" for e in excl))

    # ── Step 4 ────────────────────────────────────────────────────────
    print("\n── Step 4: Utah & South Dakota similarity scoring ──")
    target_profiles = [p for p in profiles
                       if p["geo_state"] in TARGET_STATES
                       and "TIER TWO" in p["campaign_name"].upper()]
    sim_results, pool_stats, tier_z = score_similarity(
        target_profiles, centroids, profiles
    )
    for r in sim_results:
        print(f"\n  {r['state']}  (days_active={r['days_active']})")
        print(f"    CPA=${r['cpa']:.2f}  ROAS={r['roas']:.2f}  "
              f"CTR={r['ctr']*100:.2f}%  Imp/day={r['imp_per_day']:.0f}")
        for tier in CANDIDATE_TIERS:
            s = r["scores"].get(tier)
            marker = " ←" if tier == r["recommended"] else ""
            print(f"    distance → {tier:<11}: {s:.3f}{marker}")
        print(f"    RECOMMENDED: {r['recommended']}")

    # ── Step 5 ────────────────────────────────────────────────────────
    print("\n── Step 5: Colorado isolation ──")
    co, tier2_total = colorado_isolation(conn, tier_map["TIER TWO"]["id"],
                                          start_date, end_date)
    co_pct = (co["spend"] / tier2_total["spend"] * 100) if tier2_total["spend"] else 0
    print(f"  Colorado spend    : ${co['spend'] or 0:,.2f}  ({co_pct:.1f}% of Tier Two)")
    print(f"  Tier Two total    : ${tier2_total['spend'] or 0:,.2f}")
    print(f"  Colorado clicks   : {co['clicks'] or 0:,}")
    print(f"  Colorado convs    : {co['conversions'] or 0:,.1f}")
    print(f"  Colorado revenue  : ${co['revenue'] or 0:,.2f}")

    # ── Step 6 ────────────────────────────────────────────────────────
    print("\n── Step 6: Weekly trend (ISO week 2 2026 → end) ──")
    daily_rows = fetch_weekly_trend(conn, end_date)
    weekly = stitch_weekly_trend(daily_rows, end_date)
    states_in_trend = sorted(set(w["state"] for w in weekly))
    print(f"  Weekly rows:      {len(weekly):,}")
    print(f"  States covered:   {len(states_in_trend)}")
    print(f"  Weeks covered:    {len(set((w['iso_year'], w['iso_week']) for w in weekly))}")

    # ── Step 4.5: trend-adjusted similarity ───────────────────────────
    print(f"\n── Step 4.5: Trend-adjusted similarity (last {TREND_WINDOW_WEEKS} complete weeks) ──")
    # Build state→tier map from Step 2 profiles (tier campaigns only)
    tier_assignments = {}
    for p in profiles:
        for kw in TIER_CAMPAIGNS:
            if kw in p["campaign_name"].upper():
                tier_assignments[p["geo_state"]] = kw
                break
    trend_profiles = build_trend_profiles(weekly, (last_iy, last_iw),
                                           tier_assignments)
    trend_results, trend_pool_stats, trend_centroids_z, trend_counts = \
        score_trend_adjusted_similarity(trend_profiles, TARGET_STATES)

    # Report pool composition
    for tier in CANDIDATE_TIERS:
        print(f"  Pool for {tier:<11} (signal-ok only): {trend_counts.get(tier, 0)} states")
    # Summary for UT/SD
    for r in trend_results:
        signal = "OK" if r["signal_ok"] else f"LOW SIGNAL ({r['low_signal_reason']})"
        slope_str = (f"slope={r['roas_slope']:+.3f}/wk"
                     if r["roas_slope"] is not None else "slope=n/a")
        print(f"\n  {r['state']}  [{signal}]  "
              f"weeks={r['weeks_present']}, convs={r['conversions']:.0f}")
        roas_s = f"{r['roas']:.2f}" if r['roas'] is not None else "n/a"
        cpa_s  = f"${r['cpa']:.2f}" if r['cpa'] is not None else "n/a"
        vpc_s  = f"${r['value_per_conv']:.2f}" if r['value_per_conv'] is not None else "n/a"
        ipd_s  = f"{r['imp_per_day']:.0f}" if r['imp_per_day'] is not None else "n/a"
        print(f"    ROAS={roas_s}  CPA={cpa_s}  V/C={vpc_s}  "
              f"Imp/day={ipd_s}  {slope_str}")
        for tier in CANDIDATE_TIERS:
            s = r["scores"].get(tier)
            marker = " ←" if tier == r["recommended"] else ""
            if s is not None:
                print(f"    distance → {tier:<11}: {s:.3f}{marker}")
            else:
                print(f"    distance → {tier:<11}: n/a (tier has no signal-ok members)")
        print(f"    TREND-ADJUSTED RECOMMENDED: {r['recommended']}")

    # ── Step 4.6: GP impact projection for Utah & South Dakota ────────
    print(f"\n── Step 4.6: GP impact projection ──")
    # Scenarios model the possible post-move outcomes.
    # Key insight per user input (2026-04-22): tROAS TIER THREE / TIER FOUR
    # = 1.19 (same for both), while TIER TWO = 1.10. Moving to T3/T4 is a
    # TIGHTENING, not a loosening. Expect short-run volume decrease.
    gp_scenarios = [
        {
            "label": "Optimistic: volume holds, ROAS hits tROAS",
            "volume_change_pct": 0.0,
            "roas_achieved": 1.19,
            "notes": "Best case: Utah's recent ROAS ~1.41 sustains near tROAS "
                     "without volume loss. Assumes Smart Bidding finds "
                     "efficient auctions at the new tROAS.",
        },
        {
            "label": "Neutral: -15% volume, ROAS hits tROAS",
            "volume_change_pct": -15.0,
            "roas_achieved": 1.19,
            "notes": "Moderate volume contraction to meet tighter tROAS. "
                     "Lower-efficiency impressions dropped.",
        },
        {
            "label": "Pessimistic: -30% volume, ROAS hits tROAS",
            "volume_change_pct": -30.0,
            "roas_achieved": 1.19,
            "notes": "Significant volume contraction. tROAS met at the cost "
                     "of sizable audience loss.",
        },
        {
            "label": "Worst case: -30% volume, ROAS 1.10 (undershoots)",
            "volume_change_pct": -30.0,
            "roas_achieved": 1.10,
            "notes": "Utah cannot sustain 1.19 tROAS under Tier 4 strategy; "
                     "reverts to Tier 2-like ROAS but with smaller volume. "
                     "Worst of both worlds.",
        },
        {
            "label": "Learning-period dip: -20% volume, ROAS 0.95",
            "volume_change_pct": -20.0,
            "roas_achieved": 0.95,
            "notes": "Transitional 1-2 week performance dip during "
                     "Smart Bidding learning. Expected to recover but "
                     "should be modeled as a real short-term cost.",
        },
    ]

    gp_proj_rows = []
    for tp in trend_profiles:
        if tp["state"] not in TARGET_STATES:
            continue
        rows = project_gp_impact(
            tp, "TIER TWO", CANDIDATE_TIERS, gp_scenarios
        )
        for r in rows:
            r["state"] = tp["state"]
            gp_proj_rows.append(r)

    # Terminal summary for Utah only (most interesting)
    utah_rows = [r for r in gp_proj_rows if r["state"] == "Utah"]
    if utah_rows:
        baseline = utah_rows[0]
        print(f"\n  Utah baseline (Tier Two, current 4-wk): "
              f"weekly spend ${baseline['weekly_spend']:.2f}, "
              f"rev ${baseline['weekly_revenue']:.2f}, "
              f"GP ${baseline['weekly_gp']:.2f}")
        print(f"  Scenarios (weekly GP, Δ vs baseline):")
        for r in utah_rows[1:]:
            delta = r['gp_delta_vs_baseline']
            sign = "+" if delta >= 0 else ""
            print(f"    {r['target_tier']:<11} │ {r['scenario']:<55} │ "
                  f"GP ${r['weekly_gp']:>7.2f}  ({sign}${delta:.2f})")

    # ── Step 7 (diagnostic) ───────────────────────────────────────────
    total_geo_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM campaign_performance_geo_daily_search "
        "WHERE account_id = ?", (ACCOUNT_ID,)).fetchone()["n"]
    date_range = conn.execute(
        "SELECT MIN(date) AS mn, MAX(date) AS mx FROM campaign_performance_geo_daily_search "
        "WHERE account_id = ?", (ACCOUNT_ID,)).fetchone()
    device_dist = conn.execute(
        "SELECT device, COUNT(*) AS n, SUM(cost_micros)/1e6 AS sp "
        "FROM campaign_performance_geo_daily_search WHERE account_id = ? "
        "GROUP BY device ORDER BY n DESC", (ACCOUNT_ID,)).fetchall()

    # Lookup PMAX stats for diagnostic
    pmax_stats = conn.execute("""
        SELECT COUNT(*) AS n, SUM(cost_micros)/1e6 AS sp,
               MIN(date) AS mn, MAX(date) AS mx
        FROM campaign_performance_geo_daily_search
        WHERE account_id = ? AND campaign_id IN ({})
    """.format(",".join("?" * len(pmax_ids)) if pmax_ids else "NULL"),
        [ACCOUNT_ID] + list(pmax_ids) if pmax_ids else [ACCOUNT_ID]
    ).fetchone() if pmax_ids else None

    diag = {
        "Account ID": ACCOUNT_ID,
        "Analysis window (start)": start_date.isoformat(),
        "Analysis window (end)": end_date.isoformat(),
        "Window days": args.days,
        "Last complete ISO week": f"{last_iy}-W{last_iw:02d}",
        "campaign_performance_geo_daily_search rows": f"{total_geo_rows:,}",
        "geo date range": f"{date_range['mn']} → {date_range['mx']}",
        "device rollup 'ALL' in campaign_performance_daily_search": str(has_all),
        "device distribution (geo_daily, all time)":
            "; ".join(f"{r['device']}: {r['n']:,} rows, ${r['sp']:,.0f}"
                      for r in device_dist),
        "Campaign classes in inventory":
            "; ".join(f"{k}: {v}" for k, v in sorted(class_counts.items())),
        "Tier campaigns resolved":
            "; ".join(f"{k}={v['id']}" for k, v in tier_map.items()),
        "PMAX campaigns excluded (all steps)":
            (f"{len(pmax_ids)} campaign(s); " +
             f"{pmax_stats['n']:,} rows, ${pmax_stats['sp'] or 0:,.2f} spend, "
             f"{pmax_stats['mn']} → {pmax_stats['mx']}"
             if pmax_stats and pmax_stats["n"] else "none present"),
        "States excluded from tier centroids (min_days_active=30)":
            "; ".join(f"{t}: " +
                      ", ".join(f"{e['state']}({e['days_active']}d)" for e in ex)
                      for t, ex in excluded.items() if ex) or "none",
        "GMPL proxy note":
            "Approximated as (conv_value - cost) / clicks. Does NOT include "
            "cost-of-goods. Interpret as revenue margin over media spend.",
        "Colorado spend share of Tier Two":
            f"{co_pct:.1f}%  (Colorado ${co['spend'] or 0:,.2f} / "
            f"Tier Two ${tier2_total['spend'] or 0:,.2f})",
        "Step 0 scope":
            f"Whole-account (0a) and UT/SD per-state (0b) weekly trends, "
            f"ISO W01 2025 → {last_iy}-W{last_iw:02d}. PMAX excluded.",
        "Step 6 scope":
            "2026 ISO W02 → last complete ISO week. Intermediate 2026 "
            "state-named campaigns stitched into each Tier-state's trend; "
            "CA/TX/FL included as reference. Legacy 2025 per-state "
            "campaigns, PMAX, and TEST campaigns EXCLUDED.",
        "Step 4.5 method":
            f"Trend-adjusted similarity using the last "
            f"{TREND_WINDOW_WEEKS} complete ISO weeks of Step 6 data. "
            f"Same z-score Euclidean distance as Step 4. "
            f"Volume gate: imp/day >= {MIN_IMP_PER_DAY} AND "
            f"convs >= {MIN_CONV_WINDOW} over the window. "
            f"Low-signal states are excluded from tier centroids; "
            f"low-signal targets are still scored but flagged.",
        "Step 4.5 vs Step 4":
            "Step 4 uses a 90-day blended window. Step 4.5 uses the recent "
            f"{TREND_WINDOW_WEEKS}-week window. If they disagree, the recent "
            "window is more reflective of current state performance; the "
            "90-day view is more stable but can mask recent shifts.",
        "Step 4.6 method":
            "GP impact projection for Utah and South Dakota under 5 scenarios "
            "(optimistic / neutral / pessimistic / worst-case / learning dip) "
            "applied to each candidate tier. Baseline = current 4-week avg "
            "weekly GP in Tier Two. GP = revenue - spend (no COGS). Volume "
            "change assumes CPC held constant short-term.",
        "tROAS targets (user-provided)":
            "TIER TWO 1.10, TIER ONE 1.19 (assumed), TIER THREE 1.19, "
            "TIER FOUR 1.19. T3/T4 raised from 1.16 to 1.19 recently.",
        "Smart Bidding transition caveat":
            "Utah's 69 days of history inside Tier Two does NOT transfer as "
            "a model prior to Tier Four. Tier Four's Smart Bidding will "
            "re-calibrate Utah from account-aggregate patterns. Expect 1-4 "
            "weeks of bidding exploration / learning-period volatility. "
            "Utah's +0.267 ROAS/wk trend will likely flatten or dip during "
            "this period.",
        "Weekly trend partial-week rule":
            "Any ISO week not fully contained on or before the end date is "
            "dropped from Step 0 and Step 6. If you run mid-week, the current "
            "week's partial data will not appear in weekly outputs.",
        "Wyoming cross-tier resolution":
            "Wyoming appears in 2 tier campaigns; kept the one with most "
            "recent activity. See the Inventory sheet Cross-Tier Note "
            "column for specifics.",
    }

    conn.close()

    if args.dry_run:
        print("\n" + "=" * 80)
        print("DRY RUN — Excel workbook NOT written.")
        print("=" * 80)
        return

    # Write workbook
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"geo_restructure_home_{end_date}.xlsx"

    data = {
        "step0_account": acct_weekly,
        "step0_states": state_weekly,
        "step1": inventory,
        "step2": profiles,
        "step3": {"centroids": centroids, "excluded": excluded},
        "step4": {"results": sim_results, "pool_stats": pool_stats},
        "step4_5": {
            "trend_profiles": trend_profiles,
            "trend_results": trend_results,
        },
        "step4_6": gp_proj_rows,
        "step5": {"colorado": co, "tier2_total": tier2_total},
        "step6": weekly,
        "step7": diag,
    }
    write_workbook(out_path, data)
    print(f"\n✓ Workbook written: {out_path}")


if __name__ == "__main__":
    main()
