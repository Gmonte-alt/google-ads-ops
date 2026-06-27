"""
profit_growth_microsoft.py
────────────────────────────────────────────────────────────────────
Strategic profit-maximization model for Microsoft Home Insurance.

Companion to efficiency_frontier_microsoft.py. The frontier script is
the *tactical* layer (week-by-week tCPA recommendations within
existing ranges). This script is the *strategic* layer: an 8-12 week
controlled-growth program designed to push the account up the
efficiency curve while respecting hard margin constraints.

Relationship to the tactical script:
    Tactical answers: "this week, should I move tCPA on this campaign?"
    Strategic answers: "over the next 8-12 weeks, what's the maximum
                        profit envelope and how do I get there safely?"

The strategic layer USES the tactical layer's output as one input
(specifically, the tactical recommendation per campaign — TIGHTEN
campaigns are excluded from cohort 1 of a *raise* program by definition).

Hardcoded into this v1 (intentional, all reviewed with stakeholder):

  • Margin policy — three constraints, ALL enforced:
       C1: per-campaign tCPA ≤ RPL − $8
       C2: per-campaign tCPA ≤ RPL × 0.75
       C3: forecast blended account margin ≥ 20%
    The effective per-campaign ceiling is min(C1, C2, RPL_tactical).

  • Bid-response elasticity = 0.6 (pooled default for v1).
    Reasoning: 7 historical YTD tCPA changes, but 5 of 7 are confounded
    by the 2026-01-13 attribution go-live (conv values pre-go-live were
    not recorded — counts ARE reliable, but for safety v1 doesn't fit
    a per-campaign elasticity from this thin and partly-noisy panel).
    A pooled 0.6 is conservative vs typical search elasticity (0.7-1.0)
    and fits the "controlled" half of "aggressive controlled."
    V2 will fit a real elasticity from clean post-attribution data
    (the 7 historical changes + ~7 from this stair-step program =
    14+ clean data points).

  • Cohort 1 entry gate (replaces IS-headroom gate — most campaigns
    show search_rank_lost_is ≈ 0, so auction-loss is not the right
    headroom signal for this account):
       - 14 consecutive days of 7d-CPA ≤ tCPA × 1.10
       - ≥50 primary conversions in 30d window
       - No tracked change (target/budget/strategy) in last 14d
       - Tactical layer NOT in TIGHTEN
       - Headroom evidence: actual_cpa_30d ≤ tCPA × 0.95
         (cost efficiency, not auction loss)

  • Volatility-gated second-raise eligibility:
       Compute CV (stdev/mean) of 7d-rolling-CPA over the 14d post-raise
       window.
       Low vol (CV < 0.20):  stable within ±10% for 3 consecutive days
                             OR 10 calendar days, whichever first.
       High vol (CV ≥ 0.20): hard 14-day floor + 7-of-10 trailing days
                             at 7d-CPA ≤ tCPA × 1.10.

  • Indiana, Virginia, Colorado: modeled but flagged. Indiana enters
    a special "strategic-excluded — tactical correction first" state.
    Virginia in cohort 2. Colorado deferred to cohort 2/3 with a guard.

  • New Jersey (16 conv/30d) goes on the "warmup track" — budget-only
    expansion until 50 conv/30d is reached, then enters the stair-step.

  • Rollback policy: gradual, not snap-back. 5pp/day decrements over
    the same number of days the raise took to settle. Snap-back causes
    auction whiplash because competitors below have already recalibrated
    their own bids; a hard reversion can over-correct.

  • Measurement: phased rollout (cohort 1 vs cohort 2 as concurrent
    control on weeks 4-7), plus YoY-adjusted same-week comp, plus
    account-trend-adjusted profit delta per cohort. Full DiD/synthetic
    control deferred to v2 (insufficient pre-period: campaigns
    restructured Jan-March 2026, so the cleanest pre-period is ~10
    weeks for TX, ~5 weeks for CA, less for cohort 2 — not enough
    for credible synthetic control until 12+ weeks of intervention
    accumulate).

Usage:
    python analysis/profit_growth_microsoft.py
    python profit_growth_microsoft.py --end-date 2026-04-24
    python profit_growth_microsoft.py --dry-run
    python profit_growth_microsoft.py \
        --frontier-xlsx ./efficiency_frontier_microsoft_home_2026-04-24.xlsx \
        --change-history ./Microsoft-bids-budget-changehistory-YTD.csv

Inputs:
    • paid_search.db (same as tactical script)
    • Microsoft change-history CSV (same as tactical script)
    • Latest Frontier Summary xlsx (output of the tactical script) —
      OPTIONAL but recommended; if absent, the strategic script
      re-derives the tactical signals it needs from the DB directly.

Output:
    profit_growth_microsoft_<account>_<YYYY-MM-DD>.xlsx with sheets:
      • Strategic Summary — headline plan, this week's actions
      • Profit-Optimal Targets — per-campaign optimal tCPA + binding constraint
      • Cohort Plan — Gantt-style 12-week plan per campaign
      • Trigger Log — hard/soft trigger states + rollback queue
      • Measurement — running profit delta vs baseline + YoY
      • Diagnostic — input data quality, model fit, anything weird
────────────────────────────────────────────────────────────────────
"""

import argparse
import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median, stdev

import pandas as pd
import numpy as np
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

CHANGE_HISTORY_CSV = Path(
    r"C:\Projects\google-ads-ops\analysis\input\Microsoft-bids-budget-changehistory-YTD.csv"
)

# Optional: latest tactical-script output. If present, the strategic
# script reads cohort-relevant flags (recommendation, audience_share,
# wow_conv_trend, etc.) from it instead of re-deriving from DB. If
# absent, those signals come from a lighter re-derivation here.
FRONTIER_XLSX_DEFAULT: Path | None = None

ACCOUNT_ID    = 2
ACCOUNT_LABEL = "microsoft_home"

ATTRIBUTION_GO_LIVE = date(2026, 1, 13)
YEAR_START          = date(2026, 1, 1)
BACKFILL_BUFFER_DAYS = 2

# ── Margin policy (REVISED v1.1 — see strategic notes) ─────────────
# All three constraints + the new "current-margin protection" enforced
# simultaneously. Effective per-campaign ceiling = min(C1, C2, RPL_tactical).
#
# CHANGE LOG v1.1:
# - C2 dropped from 25% → 15%. Justification: observed 14d ROAS across
#   the account ranges 118%-783% with median ~175% (≈43% margin). The
#   prior 25% C2 floor was binding hard on 9 of 12 campaigns and
#   limiting profit-optimal tCPA below where actual economics support.
# - Added CURRENT_MARGIN_PROTECTION: any proposed raise must leave the
#   forecast post-raise margin ≥ 20% AND must not cut more than 50%
#   of current observed (14d) margin. This safety net makes the
#   looser C2 floor operationally safe.
# - To revert C2 to 25% on a single run: pass --margin-floor-c2 0.25
MARGIN_C1_DOLLAR_FLOOR = 8.00     # tCPA ≤ RPL − $8
MARGIN_C2_PCT_FLOOR    = 0.15     # tCPA ≤ RPL × (1 − 0.15) — was 0.25
MARGIN_C3_BLENDED_MIN  = 0.20     # forecast blended account margin ≥ 20%

# Current-margin protection (new in v1.1)
# Forecast post-raise margin must satisfy BOTH:
#   (a) absolute floor: post-raise margin ≥ 20%
#   (b) relative protection: post-raise margin ≥ current_margin × 0.50
# (b) prevents a "raise into a thin margin" failure mode for campaigns
# whose RPL is so close to break-even that the static C2 floor doesn't
# bite — a campaign at 22% current margin shouldn't be allowed to raise
# into 11% margin just because the floor says it can.
CURRENT_MARGIN_FLOOR_ABSOLUTE = 0.20
CURRENT_MARGIN_FLOOR_RELATIVE = 0.50   # post-raise must be ≥ 50% of current

# ── Bid-response model (v1 placeholder, see docstring) ─────────────
# Pooled elasticity: a 1% tCPA raise yields 0.6% conv volume increase
# (and 1.0%+0.6%=1.6% spend increase if CPA tracks tCPA proportionally,
# though in practice CPA partially absorbs the raise — see _forecast_).
DEFAULT_ELASTICITY = 0.60

# v1.2: Elasticity sensitivity scenarios
# Three scenarios used by Profit Headroom Summary (3 destination columns)
# and the Elasticity Sensitivity sheet. Each is a BASE elasticity passed
# to forecast_tcpa_move; rank-IS adjustment still applies on top
# (campaigns with high rank loss get effective elasticity scaled up).
# Effective elasticity is capped at 1.5 to prevent runaway forecasts.
ELASTICITY_SCENARIOS = {
    "conservative": 0.60,   # default — model-driven destination
    "mid":          1.00,   # baseline assumption: marginal conv = avg conv
    "aggressive":   1.20,   # marginal conversions are accretive
}
ELASTICITY_EFFECTIVE_CAP = 1.5
# Headline column in Profit Headroom Summary uses this scenario:
# - "aggressive" matches the "fully optimistic" framing
# - reader can compare against other two via the sensitivity sheet
HEADROOM_HEADLINE_SCENARIO = "aggressive"

# ── Cohort 1 entry gate ────────────────────────────────────────────
# Headroom evidence — accept EITHER (any one of):
#   (a) rank-IS path: search_rank_lost_is ≥ 0.20 (auction-loss headroom)
#   (b) cost-efficiency path: actual_cpa_30d ≤ tCPA × 0.95
#   (c) ROAS path (NEW v1.1): actual_roas_14d ≥ 1.50 (≈33% margin —
#       comfortably above 20% blended floor with room to absorb a 15%
#       raise without breaching). This unbinds campaigns whose 30d CPA
#       is at or above target due to old data drag, but whose recent
#       performance shows clear profitability headroom.
COHORT_GATE_DAYS_AT_TARGET    = 14
COHORT_GATE_CPA_RATIO         = 1.10    # 7d-CPA must be ≤ tCPA × 1.10
COHORT_GATE_MIN_CONV_30D      = 50
COHORT_GATE_HEADROOM_CPA_RATIO = 0.95   # cost-efficiency path: cpa_30d ≤ tCPA × 0.95
COHORT_GATE_HEADROOM_RANK_IS   = 0.20   # rank-IS path: search_rank_lost_is ≥ 20%
COHORT_GATE_HEADROOM_ROAS_14D  = 1.50   # ROAS path: actual_roas_14d ≥ 150%

# Cohort 2 entry: looser stat-power floor
COHORT2_MIN_CONV_30D         = 25

# Warmup track: campaigns below cohort 2 floor with budget headroom
WARMUP_MIN_BUDGET_LOST       = 0.15
WARMUP_GRADUATION_CONV_30D   = 50      # graduation threshold to cohort eval

# ── Recent-change recency guard (matches tactical layer) ───────────
RECENT_CHANGE_WINDOW_DAYS = 14

# ── Recency-guard overrides ────────────────────────────────────────
# Per-campaign exemptions from the 14-day recent-change exclusion.
# Use sparingly — these override the default safety guard. Intended
# for campaigns where margin restoration takes priority over the
# normal recalibration window.
# Format: campaign_name → reason_label
RECENCY_GUARD_OVERRIDES: dict[str, str] = {
    "HOME INSURANCE-INDIANA-SEARCH-MOBILE-USA-STE": "MARGIN_RESTORATION_PRIORITY",
}

# ── MARGIN_RESTORATION cohort triggers ─────────────────────────────
# A campaign lands in MARGIN_RESTORATION when actual CPA exceeds the
# margin ceiling — i.e., the campaign is currently spending in a way
# that violates the 25% margin floor. The strategic layer's job for
# these campaigns is to:
#   (a) diagnose WHY CPA is overshooting (network split, attribution lag,
#       conv-volume vs tCPA history, account-wide drift)
#   (b) identify the destination tCPA that satisfies margin policy
#       under both conservative and optimistic CPA-recovery scenarios
#   (c) plan a deferred stair-step that activates only after tactical
#       layer has driven CPA to convergence
# CPA overshoot threshold: cpa_30d > margin_ceiling × 1.0
# (i.e., any breach of margin ceiling triggers it)
MARGIN_RESTORATION_TRIGGER_RATIO = 1.0

# ── Stair-step economics ──────────────────────────────────────────
STEP_INITIAL_PCT    = 0.15    # week 1 raise on cohort 1
STEP_FOLLOWUP_PCT   = 0.15    # week 3-4 second raise (if eligible)
BUDGET_STEP_PCT     = 0.30    # post-tCPA-settle budget bump

# ── Convergence detector (volatility-gated) ───────────────────────
SECOND_RAISE_HARD_FLOOR_DAYS = 14
VOLATILITY_THRESHOLD_CV      = 0.20    # CV ≥ 0.20 → high-vol regime
LOW_VOL_STABILITY_PCT        = 0.10    # CPA within ±10% of trailing
LOW_VOL_STABILITY_DAYS       = 3       # for 3 consecutive days
LOW_VOL_FALLBACK_DAYS        = 10      # OR 10 cal days, whichever first
HIGH_VOL_TRAILING_WINDOW     = 10      # 7-of-10 day check window
HIGH_VOL_DAYS_AT_TARGET      = 7       # 7 of 10 days at CPA ≤ tCPA × 1.10

# ── Trigger thresholds ─────────────────────────────────────────────
# HARD triggers (auto-rollback queued)
TRIGGER_RPL_DRIFT_SIGMA          = 2.0    # |RPL_7d − RPL_12w_mean| > 2σ
TRIGGER_VOLUME_COLLAPSE_WOW      = -0.25  # 7d conv WoW < −25%
TRIGGER_SPEND_DIVERGENCE_PP      = 0.20   # spend WoW > conv WoW + 20pp
TRIGGER_BLENDED_MARGIN_FLOOR     = 0.20   # mirror MARGIN_C3
# SOFT triggers (alert, no auto-rollback)
TRIGGER_CPA_BREACH_RATIO         = 1.30   # 7d CPA > tCPA × 1.30
# (tactical TIGHTEN flip is the other soft trigger — checked separately)

# RPL drift guard floor: campaigns with <8 weeks of clean post-attribution
# data get a wider σ band (use 4-week min stdev instead of 12-week)
RPL_DRIFT_MIN_WEEKS = 8
RPL_DRIFT_HISTORY_WEEKS = 12

# ── Rollback policy ────────────────────────────────────────────────
ROLLBACK_DAILY_DECREMENT_PCT = 0.05   # 5 percentage points per day

# ── Stair-step program horizon ────────────────────────────────────
PROGRAM_WEEKS = 12

# ── Per-campaign manual notes (for known-special-case campaigns) ───
# Mirrors the BREAK_EVEN_CPA pattern in tactical: explicit per-campaign
# overrides take precedence over auto-derived state. Empty = use auto.
COHORT_OVERRIDES: dict[str, str] = {
    # "<campaign_name>": "<cohort_label>",
    # Example: "HOME INSURANCE-INDIANA-SEARCH-MOBILE-USA-STE": "EXCLUDED_TACTICAL_FIRST",
}

# ── Formatting ────────────────────────────────────────────────────
HEADER_FILL  = "203864"   # navy (distinguished from tactical's purple)
HEADER_FONT  = "FFFFFF"
COHORT1_FILL = "C6E0B4"   # deep green — active stair-step
COHORT2_FILL = "E2EFDA"   # light green — week-4 entry
COHORT3_FILL = "FFF2CC"   # pale yellow — week-7 entry
WARMUP_FILL  = "FCE4D6"   # peach — budget-only track
EXCLUDED_FILL = "D9D9D9"  # grey — out of program
MARGIN_RESTORATION_FILL = "F4B084"  # orange — needs descent attention
TRIGGER_FILL = "FFC7CE"   # red — trigger fired
ALERT_FILL   = "FFD966"   # amber — soft trigger / watch
NEUTRAL_FILL = "F2F2F2"   # light grey


# ═══════════════════════════════════════════════════════════════════
# INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════

# Tracked attributes (match tactical script for diffability)
TRACKED_ATTRIBUTES = {
    "TargetCPA":            "target",
    "IncrementalBudgetAmt": "budget",
    "BiddingSchemeId":      "strategy",
    "BidStrategyName":      "strategy",
}


def get_db_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account", default=ACCOUNT_LABEL,
                   help=f"Account label for output naming (default: {ACCOUNT_LABEL})")
    p.add_argument("--end-date", default=None,
                   help="Analysis end date YYYY-MM-DD (default: MAX(date) - 2)")
    p.add_argument("--frontier-xlsx", default=None,
                   help="Path to latest tactical frontier output xlsx. If omitted, "
                        "strategic script re-derives needed signals from DB.")
    p.add_argument("--change-history", default=None,
                   help="Path to MS change-history CSV (overrides default)")
    p.add_argument("--db-path", default=str(DB_PATH))
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    p.add_argument("--dry-run", action="store_true",
                   help="Run smoke tests + print plan summary, do not write xlsx")
    p.add_argument("--what-if-cohort-override", default=None,
                   help="JSON-ish string: 'CAMPAIGN_NAME=COHORT,...' to override "
                        "cohort assignment for sensitivity analysis")
    p.add_argument("--margin-floor-c2", type=float, default=None,
                   help=f"Override the per-campaign C2 percent floor (default "
                        f"{MARGIN_C2_PCT_FLOOR:.0%}). Use 0.25 to revert to v1 "
                        f"behavior, 0.20 for tighter margin protection.")
    p.add_argument("--freeze-as-of", action="store_true",
                   help="Filter change-history to entries on or before --end-date. "
                        "Use this for reproducible analysis against a past state. "
                        "Without this flag, the latest change history is loaded "
                        "regardless of analysis end date.")
    p.add_argument("--ignore-recency-guard", action="store_true",
                   help="Bypass the 14-day recent-change guard for ALL campaigns. "
                        "Use for ideal-state modeling / 'fully optimistic' analyses. "
                        "Does NOT bypass the tactical-TIGHTEN exclusion or any other "
                        "exclusion logic. Logged in the diagnostic sheet.")
    return p.parse_args()


def banner(msg: str) -> None:
    print("\n" + "═" * 70)
    print(msg)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# DATE WINDOW RESOLUTION (matches tactical)
# ═══════════════════════════════════════════════════════════════════

def resolve_end_date(conn: sqlite3.Connection, override: str | None) -> tuple[date, date]:
    row = conn.execute(
        "SELECT MAX(date) AS max_date FROM campaign_performance_daily WHERE account_id = ?",
        (ACCOUNT_ID,),
    ).fetchone()
    if not row or not row["max_date"]:
        raise SystemExit(f"No campaign_performance_daily rows for account_id={ACCOUNT_ID}.")
    raw_max = date.fromisoformat(row["max_date"])
    if override:
        stable = date.fromisoformat(override)
    else:
        stable = raw_max - timedelta(days=BACKFILL_BUFFER_DAYS)
    lag = (date.today() - raw_max).days
    if lag > 2:
        print(f"⚠  WARNING: DB is {lag} days behind today (MAX(date) = {raw_max}).")
    return raw_max, stable


# ═══════════════════════════════════════════════════════════════════
# DATA QUERIES
# ═══════════════════════════════════════════════════════════════════
# We re-implement the queries we need rather than importing from the
# tactical script — keeps the strategic script standalone and avoids
# coupling. Schemas matched against the tactical script's queries.

def query_campaigns_metadata(conn: sqlite3.Connection) -> pd.DataFrame:
    """Returns all Microsoft Home campaigns (any status).

    NOTE: The exact SELECT mirrors the tactical script
    (efficiency_frontier_microsoft.query_campaigns_metadata) verbatim
    so any schema-evolution fixes there propagate cleanly here.
    """
    sql = """
        SELECT campaign_id, campaign_name, status,
               bidding_strategy_type, target_cpa_micros,
               daily_budget_micros, advertising_channel_sub_type
        FROM campaigns
        WHERE account_id = ?
    """
    return pd.read_sql(sql, conn, params=[ACCOUNT_ID])


def query_enabled_campaign_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT campaign_id FROM campaigns "
        "WHERE account_id = ? AND status = 'ENABLED' "
        "ORDER BY campaign_name",
        (ACCOUNT_ID,),
    ).fetchall()
    return [r["campaign_id"] for r in rows]


def query_campaign_daily(
    conn: sqlite3.Connection,
    campaign_ids: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Daily search-network rollup per campaign × date.

    Mirrors tactical script (efficiency_frontier_microsoft.query_campaign_daily)
    verbatim — including the join to keyword_performance_daily for
    primary_conv_count/value (offline-routed metrics). device='ALL' only.
    """
    if not campaign_ids:
        return pd.DataFrame(columns=[
            "campaign_id", "date", "impressions", "clicks", "cost_usd",
            "native_conversions", "native_conv_value",
            "search_rank_lost_is", "search_budget_lost_is",
            "search_impression_share",
            "target_cpa_that_day_usd", "bid_strategy_that_day",
            "budget_that_day_usd",
            "primary_conv_count", "primary_conv_value",
        ])

    placeholders = ",".join("?" * len(campaign_ids))
    sql = f"""
        SELECT
            campaign_id,
            date,
            SUM(impressions)                      AS impressions,
            SUM(clicks)                           AS clicks,
            SUM(cost_micros) / 1e6                AS cost_usd,
            SUM(conversions)                      AS native_conversions,
            SUM(conversion_value)                 AS native_conv_value,
            MAX(search_rank_lost_is)              AS search_rank_lost_is,
            MAX(search_budget_lost_is)            AS search_budget_lost_is,
            MAX(search_impression_share)          AS search_impression_share,
            MAX(target_cpa_micros_that_day) / 1e6 AS target_cpa_that_day_usd,
            MAX(bidding_strategy_type_that_day)   AS bid_strategy_that_day,
            MAX(budget_micros_that_day) / 1e6     AS budget_that_day_usd
        FROM campaign_performance_daily
        WHERE campaign_id IN ({placeholders})
          AND date BETWEEN ? AND ?
          AND account_id = ?
          AND device     = 'ALL'
        GROUP BY campaign_id, date
    """
    params = list(campaign_ids) + [start_date.isoformat(), end_date.isoformat(),
                                   ACCOUNT_ID]
    df_cpd = pd.read_sql(sql, conn, params=params)

    sql_kpd = f"""
        SELECT
            ki.campaign_id              AS campaign_id,
            kpd.report_date             AS date,
            SUM(kpd.primary_conv_count) AS primary_conv_count,
            SUM(kpd.primary_conv_value) AS primary_conv_value
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.campaign_id IN ({placeholders})
          AND kpd.report_date BETWEEN ? AND ?
          AND ki.account_id = ?
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        GROUP BY ki.campaign_id, kpd.report_date
    """
    df_kpd = pd.read_sql(sql_kpd, conn, params=params)

    df = df_cpd.merge(df_kpd, on=["campaign_id", "date"], how="left")
    df["primary_conv_count"] = df["primary_conv_count"].fillna(0.0)
    df["primary_conv_value"] = df["primary_conv_value"].fillna(0.0)

    # Add a campaign_name column for downstream filters that expect it
    # (the tactical script joins this in via aggregate_window; we materialize
    # it here as a convenience). Look up names from the campaigns table.
    if not df.empty:
        names_sql = (f"SELECT campaign_id, campaign_name FROM campaigns "
                     f"WHERE campaign_id IN ({placeholders}) AND account_id = ?")
        names_df = pd.read_sql(names_sql, conn,
                               params=list(campaign_ids) + [ACCOUNT_ID])
        df = df.merge(names_df, on="campaign_id", how="left")

    # Normalize date dtype for downstream comparisons
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def query_audience_network_daily(
    conn: sqlite3.Connection,
    campaign_ids: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Campaign × date audience-network rollup.

    Mirrors tactical script (efficiency_frontier_microsoft.query_audience_network_daily)
    verbatim. campaign_performance_network_daily is audience-only by definition.
    """
    if not campaign_ids:
        return pd.DataFrame(columns=[
            "campaign_id", "date",
            "audience_impressions", "audience_clicks", "audience_cost_usd",
            "audience_native_conv", "audience_native_conv_value",
        ])
    placeholders = ",".join("?" * len(campaign_ids))
    sql = f"""
        SELECT campaign_id,
               date,
               SUM(impressions)           AS audience_impressions,
               SUM(clicks)                AS audience_clicks,
               SUM(cost_micros) / 1e6     AS audience_cost_usd,
               SUM(conversions)           AS audience_native_conv,
               SUM(conversion_value)      AS audience_native_conv_value
        FROM campaign_performance_network_daily
        WHERE campaign_id IN ({placeholders})
          AND date BETWEEN ? AND ?
          AND account_id = ?
          AND device     = 'ALL'
        GROUP BY campaign_id, date
    """
    params = list(campaign_ids) + [start_date.isoformat(), end_date.isoformat(),
                                   ACCOUNT_ID]
    df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


# ═══════════════════════════════════════════════════════════════════
# CHANGE-HISTORY LOADING (matches tactical for diff stability)
# ═══════════════════════════════════════════════════════════════════

def _parse_ms_value(raw: str) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_change_history(csv_path: Path) -> pd.DataFrame:
    """Same loader as tactical — see tactical script for full spec.
       Returns: timestamp, campaign_name, ad_group_name, attribute, change_kind,
                old_value_num, new_value_num, old_value_raw, new_value_raw, scope."""
    if not isinstance(csv_path, Path):
        csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Change-history CSV not found: {csv_path}")
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            attr = (r.get("Attribute changed") or "").strip()
            if attr not in TRACKED_ATTRIBUTES:
                continue
            try:
                ts = datetime.strptime(r["Date"].strip(), "%m/%d/%Y")
            except (ValueError, KeyError):
                continue
            if ts.date() < YEAR_START:
                continue
            old_raw = (r.get("Old value") or "").strip()
            new_raw = (r.get("New value") or "").strip()
            old_num = _parse_ms_value(old_raw)
            new_num = _parse_ms_value(new_raw)
            if (old_num is not None and new_num is not None
                    and abs(old_num - new_num) < 1e-9):
                continue
            rows.append({
                "timestamp":      ts,
                "campaign_name":  (r.get("Campaign") or "").strip(),
                "ad_group_name":  (r.get("Ad group") or "").strip(),
                "attribute":      attr,
                "change_kind":    TRACKED_ATTRIBUTES[attr],
                "old_value_num":  old_num,
                "new_value_num":  new_num,
                "old_value_raw":  old_raw,
                "new_value_raw":  new_raw,
                "scope":          "AG" if (r.get("Ad group") or "").strip() else "CAMPAIGN",
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("timestamp").reset_index(drop=True)


def days_since_last_change(
    change_df: pd.DataFrame,
    campaign_name: str,
    end_date: date,
    kinds: tuple[str, ...] = ("target", "budget", "strategy"),
) -> int | None:
    """Days since last change (any of the given kinds) for this campaign.
       None if no matching change found."""
    if change_df.empty:
        return None
    sub = change_df[
        (change_df["campaign_name"] == campaign_name)
        & (change_df["change_kind"].isin(kinds))
        & (change_df["scope"] == "CAMPAIGN")
    ]
    if sub.empty:
        return None
    last_ts = sub["timestamp"].max()
    return (end_date - last_ts.date()).days


def days_since_last_tcpa_change(
    change_df: pd.DataFrame, campaign_name: str, end_date: date,
) -> int | None:
    return days_since_last_change(change_df, campaign_name, end_date, ("target",))




# ═══════════════════════════════════════════════════════════════════
# AGGREGATION HELPERS
# ═══════════════════════════════════════════════════════════════════

def aggregate_window(
    daily_df: pd.DataFrame,
    audience_daily_df: pd.DataFrame,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Roll up search + audience to campaign-level for [start, end].
       Returns one row per campaign: spend_usd, conv_count, conv_value,
       cpa, rpl, roas, audience_share, plus daily-derived rank/budget IS."""
    if daily_df.empty:
        return pd.DataFrame()
    mask = (daily_df["date"] >= start_date) & (daily_df["date"] <= end_date)
    s = daily_df[mask]
    if s.empty:
        return pd.DataFrame()
    grp = s.groupby(["campaign_id", "campaign_name"], as_index=False).agg(
        search_spend=("cost_usd", "sum"),
        conv_count=("primary_conv_count", "sum"),
        conv_value=("primary_conv_value", "sum"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        # Mean of daily IS values weighted by impressions would be ideal,
        # but the daily values are already share-shaped. Use impression-
        # weighted mean as a reasonable proxy.
        rank_lost_is=("search_rank_lost_is", "mean"),
        budget_lost_is=("search_budget_lost_is", "mean"),
    )
    aud_spend = pd.DataFrame()
    if not audience_daily_df.empty:
        amask = ((audience_daily_df["date"] >= start_date)
                 & (audience_daily_df["date"] <= end_date))
        a = audience_daily_df[amask]
        if not a.empty:
            aud_spend = a.groupby("campaign_id", as_index=False).agg(
                audience_spend=("audience_cost_usd", "sum")
            )
    if not aud_spend.empty:
        grp = grp.merge(aud_spend, on="campaign_id", how="left")
    grp["audience_spend"] = grp.get("audience_spend", 0).fillna(0)
    grp["spend_usd"] = grp["search_spend"] + grp["audience_spend"]
    grp["audience_share"] = grp["audience_spend"] / grp["spend_usd"].where(
        grp["spend_usd"] > 0, 1.0
    )
    grp["cpa"] = grp["spend_usd"] / grp["conv_count"].where(grp["conv_count"] > 0, 1)
    grp["rpl"] = grp["conv_value"] / grp["conv_count"].where(grp["conv_count"] > 0, 1)
    grp["roas"] = grp["conv_value"] / grp["spend_usd"].where(grp["spend_usd"] > 0, 1)
    # null-out where conversions = 0 (these are uninterpretable)
    grp.loc[grp["conv_count"] == 0, ["cpa", "rpl", "roas"]] = None
    grp["window_start"] = start_date
    grp["window_end"] = end_date
    return grp


def compute_daily_cpa_series(
    daily_df: pd.DataFrame,
    audience_daily_df: pd.DataFrame,
    campaign_id: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Per-campaign daily CPA series (search + audience combined).
       Used for the 14d-at-target gate and volatility detection.

       Returns columns: date, spend_usd (combined), conv_count, cpa,
       spend_7d, conv_7d, cpa_7d. The internal 'spend_usd' is the
       combined search+audience spend in USD; this is a synthetic
       column built from cost_usd + audience_cost_usd.
    """
    if daily_df.empty:
        return pd.DataFrame(columns=["date", "spend_usd", "conv_count", "cpa"])
    s = daily_df[
        (daily_df["campaign_id"].astype(str) == str(campaign_id))
        & (daily_df["date"] >= start_date)
        & (daily_df["date"] <= end_date)
    ][["date", "cost_usd", "primary_conv_count"]].copy()
    s = s.rename(columns={"cost_usd": "spend_usd",
                          "primary_conv_count": "conv_count"})
    if not audience_daily_df.empty:
        a = audience_daily_df[
            (audience_daily_df["campaign_id"].astype(str) == str(campaign_id))
            & (audience_daily_df["date"] >= start_date)
            & (audience_daily_df["date"] <= end_date)
        ][["date", "audience_cost_usd"]].copy()
        s = s.merge(a, on="date", how="left")
        s["audience_cost_usd"] = s["audience_cost_usd"].fillna(0)
        s["spend_usd"] = s["spend_usd"] + s["audience_cost_usd"]
        s = s.drop(columns=["audience_cost_usd"])
    s = s.sort_values("date").reset_index(drop=True)
    s["cpa"] = s["spend_usd"] / s["conv_count"].where(s["conv_count"] > 0, None)
    # 7-day rolling CPA (sum of spend / sum of conv across window)
    s["spend_7d"] = s["spend_usd"].rolling(window=7, min_periods=4).sum()
    s["conv_7d"]  = s["conv_count"].rolling(window=7, min_periods=4).sum()
    s["cpa_7d"]   = s["spend_7d"] / s["conv_7d"].where(s["conv_7d"] > 0, None)
    return s


# ═══════════════════════════════════════════════════════════════════
# CONSECUTIVE-DAYS-AT-TARGET CHECK
# ═══════════════════════════════════════════════════════════════════

def consecutive_days_at_target(
    daily_cpa_df: pd.DataFrame,
    current_tcpa: float,
    end_date: date,
    ratio: float = COHORT_GATE_CPA_RATIO,
) -> int:
    """Count consecutive trailing days where 7d-rolling CPA ≤ tCPA × ratio.
       Walks backward from end_date until the first violation.
       Returns 0 if no 7d CPA available at end_date."""
    if daily_cpa_df.empty or current_tcpa is None or current_tcpa <= 0:
        return 0
    s = daily_cpa_df.sort_values("date").reset_index(drop=True)
    # Trim to end_date
    s = s[s["date"] <= end_date]
    if s.empty:
        return 0
    threshold = current_tcpa * ratio
    count = 0
    # Walk back from the end
    for i in range(len(s) - 1, -1, -1):
        cpa_7d = s.iloc[i]["cpa_7d"]
        if pd.isna(cpa_7d):
            break  # can't evaluate, stop counting
        if cpa_7d <= threshold:
            count += 1
        else:
            break
    return count


def cpa_volatility_cv(daily_cpa_df: pd.DataFrame, lookback_days: int = 14) -> float | None:
    """Coefficient of variation (stdev/mean) of 7d-rolling CPA over the
       last `lookback_days` of the series. Returns None if insufficient data."""
    if daily_cpa_df.empty:
        return None
    s = daily_cpa_df.dropna(subset=["cpa_7d"]).tail(lookback_days)
    if len(s) < 4:
        return None
    vals = s["cpa_7d"].tolist()
    mu = mean(vals)
    if mu <= 0:
        return None
    try:
        sd = stdev(vals)
    except Exception:
        return None
    return sd / mu


# ═══════════════════════════════════════════════════════════════════
# RPL DRIFT SURVEILLANCE
# ═══════════════════════════════════════════════════════════════════

def compute_rpl_distribution(
    daily_df: pd.DataFrame,
    audience_daily_df: pd.DataFrame,
    campaign_id: str,
    end_date: date,
    history_weeks: int = RPL_DRIFT_HISTORY_WEEKS,
) -> dict:
    """Per-week RPL for the last `history_weeks` weeks (clipped to post-
       attribution). Returns mean, stdev, weeks_used, current_7d_rpl."""
    history_start = max(
        end_date - timedelta(weeks=history_weeks),
        ATTRIBUTION_GO_LIVE,
    )
    if daily_df.empty:
        return {"mean": None, "stdev": None, "weeks_used": 0,
                "current_7d_rpl": None, "n_below_min": True}

    # Build daily merged spend+conv frame for the campaign
    s = daily_df[daily_df["campaign_id"].astype(str) == str(campaign_id)].copy()
    if not audience_daily_df.empty:
        a = audience_daily_df[
            audience_daily_df["campaign_id"].astype(str) == str(campaign_id)
        ][["date", "audience_cost_usd"]]
        s = s.merge(a, on="date", how="left")
        s["audience_cost_usd"] = s["audience_cost_usd"].fillna(0)
    else:
        s["audience_cost_usd"] = 0
    # cost_usd is search-side spend; audience_cost_usd is audience-side
    s["total_spend"] = s["cost_usd"] + s["audience_cost_usd"]
    s = s[(s["date"] >= history_start) & (s["date"] <= end_date)]
    if s.empty:
        return {"mean": None, "stdev": None, "weeks_used": 0,
                "current_7d_rpl": None, "n_below_min": True}

    # Bucket into ISO weeks
    s["week"] = s["date"].apply(lambda d: d.isocalendar()[:2])
    weekly = s.groupby("week", as_index=False).agg(
        conv_value=("primary_conv_value", "sum"),
        conv_count=("primary_conv_count", "sum"),
    )
    weekly = weekly[weekly["conv_count"] > 0]
    weekly["rpl"] = weekly["conv_value"] / weekly["conv_count"]
    n = len(weekly)
    rpl_mean = weekly["rpl"].mean() if n else None
    rpl_stdev = weekly["rpl"].std(ddof=1) if n >= 2 else None

    # Current 7d RPL
    cur_start = end_date - timedelta(days=6)
    cur = s[s["date"] >= cur_start]
    cur_conv = cur["primary_conv_count"].sum()
    cur_val = cur["primary_conv_value"].sum()
    cur_rpl = (cur_val / cur_conv) if cur_conv > 0 else None

    return {
        "mean": rpl_mean,
        "stdev": rpl_stdev,
        "weeks_used": n,
        "current_7d_rpl": cur_rpl,
        "n_below_min": n < RPL_DRIFT_MIN_WEEKS,
    }


def rpl_drift_signal(rpl_dist: dict) -> tuple[bool, str]:
    """Returns (is_drifting, rationale)."""
    if rpl_dist["mean"] is None or rpl_dist["stdev"] is None:
        return (False, "insufficient RPL history")
    if rpl_dist["current_7d_rpl"] is None:
        return (False, "no current 7d RPL (no recent conversions)")
    deviation = abs(rpl_dist["current_7d_rpl"] - rpl_dist["mean"])
    threshold = TRIGGER_RPL_DRIFT_SIGMA * rpl_dist["stdev"]
    if rpl_dist["n_below_min"]:
        # Widen the band by 50% when history is thin
        threshold *= 1.5
    drifting = deviation > threshold
    rationale = (f"7d RPL ${rpl_dist['current_7d_rpl']:.2f} vs "
                 f"{rpl_dist['weeks_used']}w mean ${rpl_dist['mean']:.2f} "
                 f"(σ ${rpl_dist['stdev']:.2f}, "
                 f"|Δ| ${deviation:.2f} vs {TRIGGER_RPL_DRIFT_SIGMA}σ "
                 f"${threshold:.2f})")
    return (drifting, rationale)




# ═══════════════════════════════════════════════════════════════════
# PROFIT-OPTIMAL TCPA & FORECASTING
# ═══════════════════════════════════════════════════════════════════

def compute_margin_ceiling(
    rpl_30d: float | None,
    c2_floor_override: float | None = None,
) -> tuple[float | None, str]:
    """Returns (effective_ceiling, binding_constraint).
       binding_constraint ∈ {'C1_DOLLAR', 'C2_PERCENT', 'NO_RPL'}.

       v1.1 change: c2_floor_override allows per-run sensitivity analysis
       via --margin-floor-c2 CLI flag. Default uses MARGIN_C2_PCT_FLOOR.
    """
    if rpl_30d is None or rpl_30d <= 0:
        return (None, "NO_RPL")
    c2_floor = c2_floor_override if c2_floor_override is not None else MARGIN_C2_PCT_FLOOR
    c1 = rpl_30d - MARGIN_C1_DOLLAR_FLOOR
    c2 = rpl_30d * (1 - c2_floor)
    if c1 <= c2:
        return (max(c1, 0.01), "C1_DOLLAR")
    else:
        return (max(c2, 0.01), "C2_PERCENT")


def current_margin_from_roas(roas: float | None) -> float | None:
    """Convert ROAS to margin: margin = (revenue − spend) / revenue
       = 1 − (spend / revenue) = 1 − (1 / ROAS).
       Returns None if ROAS missing or ≤ 1 (negative margin / break-even).
    """
    if roas is None or roas <= 1.0:
        return None
    return 1.0 - (1.0 / roas)


def check_current_margin_protection(
    current_roas_14d: float | None,
    forecast_revenue: float,
    forecast_spend: float,
) -> tuple[bool, str]:
    """v1.1 safety net: ensures any proposed raise satisfies BOTH:
       (a) absolute floor: forecast post-raise margin ≥ 20%
       (b) relative floor: forecast post-raise margin ≥ current_margin × 50%

       Returns (passes, rationale).
    """
    if forecast_revenue <= 0:
        return (False, "no forecast revenue")
    forecast_margin = (forecast_revenue - forecast_spend) / forecast_revenue
    # (a) absolute floor
    if forecast_margin < CURRENT_MARGIN_FLOOR_ABSOLUTE:
        return (False,
                f"forecast post-raise margin {forecast_margin:.0%} < "
                f"absolute floor {CURRENT_MARGIN_FLOOR_ABSOLUTE:.0%}")
    # (b) relative floor
    current_margin = current_margin_from_roas(current_roas_14d)
    if current_margin is not None:
        relative_floor = current_margin * CURRENT_MARGIN_FLOOR_RELATIVE
        if forecast_margin < relative_floor:
            return (False,
                    f"forecast post-raise margin {forecast_margin:.0%} < "
                    f"{CURRENT_MARGIN_FLOOR_RELATIVE:.0%} of current "
                    f"({current_margin:.0%}) = {relative_floor:.0%}")
    return (True,
            f"forecast post-raise margin {forecast_margin:.0%} clears both "
            f"absolute ({CURRENT_MARGIN_FLOOR_ABSOLUTE:.0%}) and relative "
            f"floors")


def adjusted_elasticity_for_rank_is(
    rank_lost_is: float | None,
    base_elasticity: float = DEFAULT_ELASTICITY,
) -> float:
    """Adjust pooled elasticity for available rank-IS headroom.

    Logic: when search_rank_lost_is is high, auctions are sitting
    available — raising tCPA captures them roughly proportionally,
    not at the pooled rate. A campaign with 90%+ rank loss has
    dramatically more elastic response to a tCPA raise.

    For base < 1.0 (conservative case): blend toward 1.0 as rank-IS rises.
       Example: base=0.6, rank_lost=50%+ → effective = 1.0
    For base ≥ 1.0 (mid/aggressive case): scale ABOVE base by the same
       contribution mechanism, so high rank-IS preserves the optimism.
       Example: base=1.0, rank_lost=50%+ → effective = 1.2 (capped at 1.5)
                base=1.2, rank_lost=50%+ → effective = 1.44 (capped at 1.5)

    Effective elasticity capped at ELASTICITY_EFFECTIVE_CAP (1.5) to
    prevent unsustainable forecasts from compounding rank-IS + base.
    """
    if rank_lost_is is None:
        return min(base_elasticity, ELASTICITY_EFFECTIVE_CAP)
    contribution = min(max(rank_lost_is, 0.0), 0.50) / 0.50
    if base_elasticity < 1.0:
        # Blend toward 1.0
        effective = base_elasticity + (1.0 - base_elasticity) * contribution
    else:
        # Scale above base by 20% per full rank-IS contribution
        # (matches the 0.6 → 1.0 lift for the conservative case at 50%+ rank loss)
        effective = base_elasticity * (1.0 + 0.20 * contribution)
    return min(effective, ELASTICITY_EFFECTIVE_CAP)


def forecast_tcpa_move(
    current_tcpa: float,
    current_spend_30d: float,
    current_conv_30d: float,
    current_rpl_30d: float,
    target_tcpa: float,
    elasticity: float = DEFAULT_ELASTICITY,
    rank_lost_is: float | None = None,
) -> dict:
    """Forecast spend/conv/profit at a candidate tCPA, holding RPL constant.

    Model (v1, simple, transparent):
      pct_tcpa_change      = (target - current) / current
      pct_conv_change      = elasticity × pct_tcpa_change          # vol response
      forecast_conv        = current_conv × (1 + pct_conv_change)
      forecast_cpa         = target_tcpa × CPA_to_target_ratio     # see below
      forecast_spend       = forecast_conv × forecast_cpa
      forecast_revenue     = forecast_conv × current_rpl
      forecast_profit      = forecast_revenue - forecast_spend

    CPA_to_target_ratio: in steady state we observe actual CPA = target × k
    where k is the campaign-specific ratio. We use the current observed
    ratio (current_cpa / current_tcpa) as the steady-state assumption for
    the new target. This is a simplification — real CPA partially decouples
    from target as auction pressure changes — but it's transparent and
    conservative in either direction.

    Returns dict with: target_tcpa, pct_tcpa_change, forecast_conv,
                       forecast_spend, forecast_revenue, forecast_profit,
                       profit_delta_vs_current, conv_delta_vs_current,
                       and *_low / *_high uncertainty bands (using
                       elasticity ±0.2 as the 1σ band).
    """
    if current_tcpa is None or current_tcpa <= 0 or current_conv_30d <= 0:
        return {"error": "insufficient inputs"}
    pct_tcpa_change = (target_tcpa - current_tcpa) / current_tcpa
    current_cpa = current_spend_30d / current_conv_30d if current_conv_30d > 0 else None
    cpa_ratio = (current_cpa / current_tcpa) if (current_cpa and current_tcpa > 0) else 1.0

    # Adjust elasticity for available rank-IS headroom
    effective_elasticity = adjusted_elasticity_for_rank_is(rank_lost_is, elasticity)

    def _scenario(elast: float) -> dict:
        pct_conv = elast * pct_tcpa_change
        f_conv = current_conv_30d * (1 + pct_conv)
        f_cpa = target_tcpa * cpa_ratio
        f_spend = f_conv * f_cpa
        f_rev = f_conv * current_rpl_30d
        f_profit = f_rev - f_spend
        return {
            "forecast_conv": f_conv,
            "forecast_spend": f_spend,
            "forecast_revenue": f_rev,
            "forecast_profit": f_profit,
        }

    central = _scenario(effective_elasticity)
    low = _scenario(max(effective_elasticity - 0.2, 0.0))
    high = _scenario(min(effective_elasticity + 0.2, 1.2))

    # Current profit baseline (30d realized)
    current_revenue = current_conv_30d * current_rpl_30d
    current_profit = current_revenue - current_spend_30d

    return {
        "target_tcpa": target_tcpa,
        "pct_tcpa_change": pct_tcpa_change,
        "elasticity_used": effective_elasticity,
        "elasticity_base": elasticity,
        "rank_lost_is_input": rank_lost_is,
        "cpa_ratio_assumed": cpa_ratio,
        "forecast_conv": central["forecast_conv"],
        "forecast_spend": central["forecast_spend"],
        "forecast_revenue": central["forecast_revenue"],
        "forecast_profit": central["forecast_profit"],
        "forecast_profit_low":  low["forecast_profit"],
        "forecast_profit_high": high["forecast_profit"],
        "forecast_conv_low":  low["forecast_conv"],
        "forecast_conv_high": high["forecast_conv"],
        "current_profit_30d": current_profit,
        "profit_delta_vs_current": central["forecast_profit"] - current_profit,
        "conv_delta_vs_current": central["forecast_conv"] - current_conv_30d,
    }


def derive_profit_optimal_tcpa(
    current_tcpa: float,
    current_spend_30d: float,
    current_conv_30d: float,
    current_rpl_30d: float,
    margin_ceiling: float,
    elasticity: float = DEFAULT_ELASTICITY,
    rank_lost_is: float | None = None,
    current_roas_14d: float | None = None,
) -> dict:
    """Find the tCPA inside [current, margin_ceiling] that maximizes
       forecasted profit, subject to current-margin protection.

    Profit as a function of tCPA T (holding RPL R constant):
       conv(T)   = C₀ × (1 + ε × ((T − T₀) / T₀))
       spend(T)  = conv(T) × T × k                    [k = current cpa/tcpa]
       profit(T) = conv(T) × (R − T × k)

    For ε < 1, profit is concave in T over the relevant range. We solve
    numerically over a 50-cent grid from current_tcpa to margin_ceiling.

    v1.1: Each candidate tCPA must also pass current-margin protection
    (forecast post-raise margin ≥ 20% absolute AND ≥ 50% of current
    margin). Candidates failing protection are excluded from the scan
    even if they have higher profit; this prevents the model from
    proposing raises that breach margin policy under realistic scenarios.
    """
    if current_tcpa is None or current_tcpa <= 0 or margin_ceiling is None:
        return {"error": "insufficient inputs"}
    if margin_ceiling <= current_tcpa:
        # No upward room — profit-optimal is current
        return {
            "profit_optimal_tcpa": current_tcpa,
            "margin_ceiling_binding": True,
            "scan": [],
            "margin_protection_binding": False,
            "rationale": ("margin ceiling already at or below current tCPA — "
                          "no profitable raise available"),
        }

    # Grid scan
    scan = []
    T = current_tcpa
    best_profit = float("-inf")
    best_T = current_tcpa
    margin_protection_binding = False  # True if protection capped the optimum
    margin_protection_first_fail = None
    while T <= margin_ceiling + 1e-6:
        f = forecast_tcpa_move(
            current_tcpa=current_tcpa,
            current_spend_30d=current_spend_30d,
            current_conv_30d=current_conv_30d,
            current_rpl_30d=current_rpl_30d,
            target_tcpa=T,
            elasticity=elasticity,
            rank_lost_is=rank_lost_is,
        )
        if "forecast_profit" in f:
            # Apply current-margin protection
            protection_ok, protection_rationale = check_current_margin_protection(
                current_roas_14d=current_roas_14d,
                forecast_revenue=f["forecast_revenue"],
                forecast_spend=f["forecast_spend"],
            )
            scan.append({
                "tcpa": T,
                "profit": f["forecast_profit"],
                "margin_protection": protection_ok,
                "protection_note": protection_rationale,
            })
            if not protection_ok:
                # Stop scanning above this point — margin is monotonically
                # eroded as tCPA rises (under the constant-elasticity model)
                if margin_protection_first_fail is None:
                    margin_protection_first_fail = T
                margin_protection_binding = True
                break
            if f["forecast_profit"] > best_profit:
                best_profit = f["forecast_profit"]
                best_T = T
        T = round(T + 0.50, 2)  # 50-cent grid

    return {
        "profit_optimal_tcpa": best_T,
        "margin_ceiling_binding": (abs(best_T - margin_ceiling) < 0.51),
        "margin_protection_binding": margin_protection_binding,
        "margin_protection_capped_at": margin_protection_first_fail,
        "scan": scan,
        "best_profit_30d": best_profit,
    }


def compute_elasticity_sensitivity(
    current_tcpa: float | None,
    current_spend_30d: float,
    current_conv_30d: float,
    current_rpl_30d: float | None,
    margin_ceiling: float | None,
    rank_lost_is: float | None = None,
    current_roas_14d: float | None = None,
) -> dict[str, dict]:
    """Run derive_profit_optimal_tcpa across the three ELASTICITY_SCENARIOS.

    Returns a dict keyed by scenario name ('conservative', 'mid', 'aggressive')
    with each value containing:
      - destination_tcpa: float
      - forecast_profit_30d: float (at destination)
      - forecast_conv_30d: float (at destination)
      - forecast_revenue_30d: float (at destination)
      - forecast_spend_30d: float (at destination)
      - profit_delta_vs_current: float
      - margin_protection_capped_at: float | None
      - effective_elasticity_at_destination: float

    Returns None values for scenarios that can't be computed (missing inputs).
    """
    out: dict[str, dict] = {}
    for scenario_name, base_eps in ELASTICITY_SCENARIOS.items():
        if (current_tcpa is None or current_tcpa <= 0
                or current_rpl_30d is None or current_rpl_30d <= 0
                or current_conv_30d <= 0 or margin_ceiling is None):
            out[scenario_name] = {
                "destination_tcpa": current_tcpa,
                "forecast_profit_30d": None,
                "forecast_conv_30d": None,
                "forecast_revenue_30d": None,
                "forecast_spend_30d": None,
                "profit_delta_vs_current": 0.0,
                "margin_protection_capped_at": None,
                "effective_elasticity_at_destination": base_eps,
                "error": "insufficient inputs",
            }
            continue

        opt = derive_profit_optimal_tcpa(
            current_tcpa=current_tcpa,
            current_spend_30d=current_spend_30d,
            current_conv_30d=current_conv_30d,
            current_rpl_30d=current_rpl_30d,
            margin_ceiling=margin_ceiling,
            elasticity=base_eps,
            rank_lost_is=rank_lost_is,
            current_roas_14d=current_roas_14d,
        )
        dest_tcpa = opt.get("profit_optimal_tcpa", current_tcpa)

        # Forecast at the destination (so the columns in the workbook
        # show the headline numbers at each scenario's optimum)
        if dest_tcpa and dest_tcpa > current_tcpa + 0.01:
            f_at_dest = forecast_tcpa_move(
                current_tcpa=current_tcpa,
                current_spend_30d=current_spend_30d,
                current_conv_30d=current_conv_30d,
                current_rpl_30d=current_rpl_30d,
                target_tcpa=dest_tcpa,
                elasticity=base_eps,
                rank_lost_is=rank_lost_is,
            )
            out[scenario_name] = {
                "destination_tcpa": dest_tcpa,
                "forecast_profit_30d": f_at_dest.get("forecast_profit"),
                "forecast_conv_30d": f_at_dest.get("forecast_conv"),
                "forecast_revenue_30d": f_at_dest.get("forecast_revenue"),
                "forecast_spend_30d": f_at_dest.get("forecast_spend"),
                "profit_delta_vs_current": f_at_dest.get("profit_delta_vs_current"),
                "margin_protection_capped_at": opt.get("margin_protection_capped_at"),
                "effective_elasticity_at_destination": f_at_dest.get("elasticity_used", base_eps),
            }
        else:
            # No raise — destination is current
            current_profit = (current_rpl_30d - current_spend_30d / current_conv_30d) * current_conv_30d
            out[scenario_name] = {
                "destination_tcpa": current_tcpa,
                "forecast_profit_30d": current_profit,
                "forecast_conv_30d": current_conv_30d,
                "forecast_revenue_30d": current_rpl_30d * current_conv_30d,
                "forecast_spend_30d": current_spend_30d,
                "profit_delta_vs_current": 0.0,
                "margin_protection_capped_at": opt.get("margin_protection_capped_at"),
                "effective_elasticity_at_destination": adjusted_elasticity_for_rank_is(
                    rank_lost_is, base_eps),
            }
    return out


# ═══════════════════════════════════════════════════════════════════
# MARGIN_RESTORATION DESTINATIONS + DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════

def derive_margin_restoration_destinations(
    current_tcpa: float,
    current_cpa_30d: float,
    rpl_30d: float,
    margin_ceiling: float,
    account_typical_cpa_to_tcpa_ratio: float | None = None,
    rank_lost_is: float | None = None,
    conv_30d: float = 0,
    spend_30d: float = 0,
) -> dict:
    """For a campaign overshooting margin ceiling, derive two destination
       tCPAs:
         - conservative: tCPA such that the CURRENT cpa-to-tcpa overshoot
           ratio still keeps CPA at or below margin ceiling. Assumes the
           overshoot persists.
         - optimistic:   tCPA assuming CPA-to-tCPA ratio normalizes to
           account-typical (or 1.0 if no account ratio supplied). Then
           the profit-optimal tCPA can be above the ceiling-implied floor.

       The conservative destination is what the descent plan TARGETS.
       The optimistic destination is the upside if the descent reveals
       the overshoot was structural (audience network, broken queries,
       etc.) and gets fixed.

    Args:
        account_typical_cpa_to_tcpa_ratio: blended ratio across non-
            overshooting campaigns. If None, uses 1.0 (assumes CPA = tCPA
            in clean steady state).

    Returns:
        {
          "conservative_tcpa": float,    # descent target
          "conservative_rationale": str,
          "optimistic_tcpa": float,
          "optimistic_rationale": str,
          "current_overshoot_ratio": float,
          "destination_above_current": bool,  # is upside available?
        }
    """
    if current_cpa_30d <= 0 or current_tcpa <= 0:
        return {"error": "insufficient inputs"}

    overshoot_ratio = current_cpa_30d / current_tcpa  # e.g., 50.31/30 = 1.677

    # Conservative: assume the overshoot ratio persists. To get CPA ≤ ceiling,
    # we need tCPA ≤ ceiling / overshoot_ratio.
    conservative = margin_ceiling / overshoot_ratio
    conservative_rationale = (
        f"Assumes current CPA-to-tCPA overshoot ratio ({overshoot_ratio:.2f}x) "
        f"persists. To bring CPA ≤ margin ceiling ${margin_ceiling:.2f}, "
        f"tCPA must descend to ${conservative:.2f} "
        f"(${margin_ceiling:.2f} / {overshoot_ratio:.2f})."
    )

    # Optimistic: assume CPA-to-tCPA ratio normalizes. Then we can
    # solve for profit-optimal subject to margin ceiling.
    typical_ratio = (account_typical_cpa_to_tcpa_ratio
                     if account_typical_cpa_to_tcpa_ratio is not None else 1.0)

    if rpl_30d > 0 and conv_30d > 0:
        # Use the same forecast machinery, but with a hypothetical "fixed"
        # current state where current_cpa = current_tcpa × typical_ratio
        # rather than the broken current_cpa.
        hypothetical_spend_30d = (current_tcpa * typical_ratio) * conv_30d
        opt = derive_profit_optimal_tcpa(
            current_tcpa=current_tcpa,
            current_spend_30d=hypothetical_spend_30d,
            current_conv_30d=conv_30d,
            current_rpl_30d=rpl_30d,
            margin_ceiling=margin_ceiling,
            rank_lost_is=rank_lost_is,
        )
        optimistic = opt.get("profit_optimal_tcpa", current_tcpa)
        optimistic_rationale = (
            f"Assumes CPA-to-tCPA ratio normalizes to {typical_ratio:.2f}x "
            f"(account-typical or assumed 1.0 if no account data). Then "
            f"profit-optimal under rank-IS-adjusted elasticity = "
            f"${optimistic:.2f}, capped by margin ceiling ${margin_ceiling:.2f}."
        )
    else:
        optimistic = current_tcpa
        optimistic_rationale = "insufficient data for optimistic forecast"

    return {
        "conservative_tcpa": round(conservative, 2),
        "conservative_rationale": conservative_rationale,
        "optimistic_tcpa": round(optimistic, 2),
        "optimistic_rationale": optimistic_rationale,
        "current_overshoot_ratio": overshoot_ratio,
        "destination_above_current": optimistic > current_tcpa + 0.50,
    }


def diagnose_overshoot(
    campaign_id: str,
    campaign_name: str,
    current_tcpa: float,
    cpa_30d: float,
    cpa_14d: float | None,
    cpa_7d: float | None,
    audience_share_30d: float | None,
    daily_cpa_df: pd.DataFrame,
    end_date: date,
    change_df: pd.DataFrame,
) -> dict:
    """Five-check diagnostic for why CPA is overshooting target.

    Returns dict with one entry per check:
      - network_split: audience_share + verdict
      - cpa_trend: 30d → 14d → 7d direction + verdict
      - attribution_lag: weekly conv counts last 8 weeks + verdict
      - tcpa_history: did past tCPA changes correlate with CPA shifts?
      - overall: most likely cause + recommended descent strategy
    """
    out = {}

    # Check 1: Network split (audience network as overshoot driver)
    if audience_share_30d is not None:
        if audience_share_30d > 0.40:
            verdict = ("HIGH_AUDIENCE_SHARE — audience network likely "
                       "primary driver of CPA overshoot. Consider lowering "
                       "audience-network bid modifier or capping audience spend.")
        elif audience_share_30d > 0.25:
            verdict = ("MODERATE_AUDIENCE_SHARE — audience network is a "
                       "contributor but not the sole cause.")
        else:
            verdict = ("LOW_AUDIENCE_SHARE — audience network is NOT the "
                       "primary driver; problem is search-side.")
        out["network_split"] = {
            "audience_share_30d": audience_share_30d,
            "verdict": verdict,
        }
    else:
        out["network_split"] = {"audience_share_30d": None,
                                "verdict": "no audience-share data available"}

    # Check 2: CPA trend (30d vs 14d vs 7d)
    trend_pieces = []
    if cpa_30d:
        trend_pieces.append(f"30d=${cpa_30d:.2f}")
    if cpa_14d:
        trend_pieces.append(f"14d=${cpa_14d:.2f}")
    if cpa_7d:
        trend_pieces.append(f"7d=${cpa_7d:.2f}")

    if cpa_30d and cpa_14d and cpa_7d:
        if cpa_7d < cpa_14d < cpa_30d:
            verdict = ("DESCENDING — system is self-correcting. Strategic "
                       "intervention may be unnecessary; let it ride 1-2 more weeks.")
        elif cpa_7d > cpa_14d > cpa_30d:
            verdict = ("ASCENDING — overshoot is worsening. Tactical descent "
                       "is urgent.")
        elif cpa_7d > current_tcpa * 1.20:
            verdict = ("FLAT_HIGH — overshoot is stable but well above target. "
                       "Structural cause (likely network split or query quality).")
        else:
            verdict = ("MIXED — no clear direction. Use 14d as best estimate.")
    else:
        verdict = "insufficient CPA data across windows"

    out["cpa_trend"] = {
        "values": " → ".join(trend_pieces) if trend_pieces else "n/a",
        "verdict": verdict,
    }

    # Check 3: Attribution lag (weekly conv counts last 8 weeks)
    # Note: per stakeholder correction, conv counts pre-attribution-go-live
    # are RELIABLE — only conv values aren't. So this check works on the
    # full 8-week window even crossing the 1/13 attribution boundary.
    attribution_data = []
    if not daily_cpa_df.empty:
        for wk_offset in range(8, 0, -1):
            wk_end = end_date - timedelta(days=(wk_offset - 1) * 7)
            wk_start = wk_end - timedelta(days=6)
            wk_data = daily_cpa_df[
                (daily_cpa_df["date"] >= wk_start)
                & (daily_cpa_df["date"] <= wk_end)
            ]
            wk_conv = wk_data["conv_count"].sum() if not wk_data.empty else 0
            attribution_data.append({"week_end": wk_end, "conv": wk_conv})

    if attribution_data and len(attribution_data) >= 4:
        # Compare last 2 weeks avg vs prior 4-6 weeks avg
        recent = mean([w["conv"] for w in attribution_data[-2:]])
        prior = mean([w["conv"] for w in attribution_data[2:6]])
        if prior > 0 and recent < prior * 0.7:
            verdict = (f"LIKELY_ATTRIBUTION_LAG — recent 2w avg "
                       f"{recent:.0f} conv vs prior 4w avg {prior:.0f}. "
                       f"Some 'overshoot' may be unattributed conversions "
                       f"that will land in 1-3 weeks.")
        else:
            verdict = (f"NO_LAG_DETECTED — recent 2w avg {recent:.0f} conv "
                       f"vs prior 4w avg {prior:.0f}; stable.")
    else:
        verdict = "insufficient weekly conv data"

    out["attribution_lag"] = {
        "weekly_conv": [(w["week_end"].isoformat(), w["conv"])
                        for w in attribution_data],
        "verdict": verdict,
    }

    # Check 4: tCPA history — did past changes correlate with CPA shifts?
    if not change_df.empty:
        camp_changes = change_df[
            (change_df["campaign_name"] == campaign_name)
            & (change_df["change_kind"] == "target")
            & (change_df["scope"] == "CAMPAIGN")
        ].sort_values("timestamp")
        if not camp_changes.empty:
            history = []
            for _, ch in camp_changes.iterrows():
                history.append({
                    "date": ch["timestamp"].date().isoformat(),
                    "from": ch["old_value_num"],
                    "to": ch["new_value_num"],
                })
            verdict = (f"{len(history)} historical tCPA change(s); "
                       f"need daily series around each to assess effect.")
            out["tcpa_history"] = {"changes": history, "verdict": verdict}
        else:
            out["tcpa_history"] = {"changes": [],
                                   "verdict": "no historical tCPA changes"}
    else:
        out["tcpa_history"] = {"changes": [], "verdict": "no change history loaded"}

    # Check 5: Overall — synthesize the above into a recommended strategy
    primary_cause = "UNKNOWN"
    if out["network_split"].get("verdict", "").startswith("HIGH"):
        primary_cause = "AUDIENCE_NETWORK_BLEED"
    elif out["attribution_lag"].get("verdict", "").startswith("LIKELY"):
        primary_cause = "ATTRIBUTION_LAG"
    elif out["cpa_trend"].get("verdict", "").startswith("DESCENDING"):
        primary_cause = "SELF_CORRECTING"
    elif out["cpa_trend"].get("verdict", "").startswith("FLAT_HIGH"):
        primary_cause = "STRUCTURAL_OVERSHOOT"

    rec_strategy = {
        "AUDIENCE_NETWORK_BLEED": (
            "Don't lower tCPA aggressively; the search side may be fine. "
            "First reduce audience-network bid modifier or set a separate "
            "audience-network budget cap. Re-eval CPA after 7d of network rebalance."),
        "ATTRIBUTION_LAG": (
            "Wait 1-2 weeks for delayed conversions to land. Apply a +30% "
            "expected-conv adjustment to the 30d CPA before deciding on descent. "
            "If the inflated CPA persists after 2 more weeks, treat as STRUCTURAL."),
        "SELF_CORRECTING": (
            "Hold current tCPA and let the system self-correct. Re-eval at "
            "end of week 2."),
        "STRUCTURAL_OVERSHOOT": (
            "Tactical descent in 5pp/week increments toward conservative "
            "destination. After CPA stabilizes ≤ tCPA × 1.10, re-enter "
            "cohort assignment as COHORT_2."),
        "UNKNOWN": (
            "Default: tactical descent toward conservative destination. "
            "Diagnostic data insufficient to pick a more targeted strategy."),
    }[primary_cause]

    out["overall"] = {
        "primary_cause": primary_cause,
        "recommended_strategy": rec_strategy,
    }
    return out


def compute_account_typical_cpa_to_tcpa_ratio(rows_df: pd.DataFrame) -> float | None:
    """Volume-weighted average of cpa_30d/current_tcpa across non-
       overshooting campaigns. Used as the 'optimistic destination'
       assumption for MARGIN_RESTORATION scenarios."""
    if rows_df.empty:
        return None
    valid = rows_df[
        (rows_df["cpa_30d"].notna())
        & (rows_df["current_tcpa"].notna())
        & (rows_df["current_tcpa"] > 0)
        & (rows_df["cohort"] != "MARGIN_RESTORATION")  # exclude overshooters
        & (rows_df["spend_30d"] > 0)
    ]
    if valid.empty:
        return None
    ratios = valid["cpa_30d"] / valid["current_tcpa"]
    weights = valid["spend_30d"]
    total_w = weights.sum()
    if total_w <= 0:
        return None
    return float((ratios * weights).sum() / total_w)


# ═══════════════════════════════════════════════════════════════════
# COHORT ASSIGNMENT
# ═══════════════════════════════════════════════════════════════════

# Cohort labels (vocabulary)
#   MARGIN_RESTORATION      — 30d CPA > margin ceiling; tactical descent
#                             then conditional re-eval at week 4
#   COHORT_1                — week 1 stair-step start, gates fully passed
#   COHORT_2                — week 4 start, gates met at lower stat-power floor
#   COHORT_3                — contingent week 7+ entry (dependent on cohort 1 results)
#   WARMUP_BUDGET_ONLY      — sub-stat-power; budget expansion until graduation
#   EXCLUDED_TACTICAL_FIRST — tactical layer needs to correct first (deferred-TIGHTEN)
#   EXCLUDED_RECALIBRATING  — recent change pending settle (Colorado-style)
#   EXCLUDED_LOW_HEADROOM   — insufficient headroom for a profitable raise

def assign_cohort(
    campaign_name: str,
    current_tcpa: float | None,
    cpa_30d: float | None,
    conv_30d: float,
    consec_days_at_target: int,
    days_since_change: int | None,
    days_since_tcpa_change: int | None,
    budget_lost_is: float | None,
    rank_lost_is: float | None,
    tactical_recommendation: str | None,
    margin_ceiling: float | None,
    profit_optimal_tcpa: float | None,
    overrides: dict[str, str] | None = None,
    recency_guard_overrides: dict[str, str] | None = None,
    cpa_14d: float | None = None,
    roas_14d: float | None = None,
    ignore_recency_guard_global: bool = False,
) -> tuple[str, list[str]]:
    """Returns (cohort_label, reasons[]). Reasons explains each gate.

    Decision order (early-return at first match):
      1. Manual cohort override (config-driven)
      2. Hard guards (no tCPA, no margin ceiling)
      3. MARGIN_RESTORATION — BOTH 30d AND 14d CPA exceed margin ceiling
      4. Recency guard (unless overridden per-campaign OR globally via
         ignore_recency_guard_global flag)
      5. Tactical TIGHTEN (deferred descent — different from MARGIN_
         RESTORATION because tactical can drive it without strategic input)
      6. Stat-power floor → WARMUP / EXCLUDED_LOW_HEADROOM
      7. Cohort 1 gate evaluation (v1.2: 2 alternative headroom paths —
         cost-efficiency-30d OR ROAS-14d. Rank-IS removed from cohort
         gating per stakeholder decision; still used in elasticity
         adjustment.)
      8. Cohort 2 fallback
    """
    overrides = overrides or {}
    recency_guard_overrides = recency_guard_overrides or {}
    reasons: list[str] = []

    # 1. Manual cohort override wins
    if campaign_name in overrides:
        return (overrides[campaign_name], [f"manual override: {overrides[campaign_name]}"])

    # 2. Hard guards
    if current_tcpa is None or current_tcpa <= 0:
        reasons.append("no current tCPA on file")
        return ("EXCLUDED_LOW_HEADROOM", reasons)

    # 3. MARGIN_RESTORATION (v1.1: BOTH 30d AND 14d exceed ceiling)
    # If 30d CPA exceeds margin ceiling, that's a *snapshot* breach.
    # But if 14d CPA is below ceiling, the recent equilibrium has already
    # corrected — the 30d signal is a lagging artifact. Only flag as
    # MARGIN_RESTORATION when both windows agree the breach is current.
    is_30d_breach = (margin_ceiling is not None and cpa_30d is not None
                     and cpa_30d > margin_ceiling * MARGIN_RESTORATION_TRIGGER_RATIO)
    is_14d_breach = (margin_ceiling is not None and cpa_14d is not None
                     and cpa_14d > margin_ceiling * MARGIN_RESTORATION_TRIGGER_RATIO)
    if is_30d_breach and is_14d_breach:
        reasons.append(
            f"MARGIN_RESTORATION: 30d CPA ${cpa_30d:.2f} AND 14d CPA "
            f"${cpa_14d:.2f} both exceed margin ceiling ${margin_ceiling:.2f}. "
            f"Strategic layer identifies destination tCPA + descent path; "
            f"tactical layer executes weekly decrements. Re-eval at week 3-4."
        )
        if campaign_name in recency_guard_overrides:
            reasons.append(
                f"recency-guard exempt: {recency_guard_overrides[campaign_name]}"
            )
        return ("MARGIN_RESTORATION", reasons)
    elif is_30d_breach and not is_14d_breach:
        # 30d says breach, 14d says recovered — log this for visibility
        # but don't apply MARGIN_RESTORATION cohort. Continue to normal
        # cohort evaluation.
        reasons.append(
            f"30d-only margin signal: 30d CPA ${cpa_30d:.2f} > ceiling "
            f"${margin_ceiling:.2f}, BUT 14d CPA ${cpa_14d:.2f} below "
            f"ceiling — campaign already self-corrected. Skipping "
            f"MARGIN_RESTORATION; evaluating normal cohort gates."
        )

    # No room above the margin ceiling → can't profitably raise
    # (this is distinct from MARGIN_RESTORATION: here CPA is fine but
    # tCPA is already at or above ceiling — a structural issue, not a
    # behavioral one)
    if margin_ceiling is None or margin_ceiling <= current_tcpa:
        reasons.append(
            f"margin ceiling (${margin_ceiling:.2f}) at or below current tCPA "
            f"(${current_tcpa:.2f}) — no profitable raise available under "
            f"margin policy (RPL−$8 / RPL×0.75)"
            if margin_ceiling else "no margin ceiling available (no RPL data)"
        )
        return ("EXCLUDED_LOW_HEADROOM", reasons)

    # 4. Recency guard (skip if globally disabled OR per-campaign overridden)
    if (days_since_change is not None
            and days_since_change < RECENT_CHANGE_WINDOW_DAYS
            and campaign_name not in recency_guard_overrides
            and not ignore_recency_guard_global):
        reasons.append(
            f"recent change {days_since_change}d ago — recalibrating "
            f"(<{RECENT_CHANGE_WINDOW_DAYS}d guard)"
        )
        return ("EXCLUDED_RECALIBRATING", reasons)

    if (days_since_change is not None
            and days_since_change < RECENT_CHANGE_WINDOW_DAYS
            and campaign_name in recency_guard_overrides):
        reasons.append(
            f"recency-guard override active ({recency_guard_overrides[campaign_name]}): "
            f"bypassing {RECENT_CHANGE_WINDOW_DAYS}d guard "
            f"(actual {days_since_change}d since last change)"
        )
    elif (days_since_change is not None
            and days_since_change < RECENT_CHANGE_WINDOW_DAYS
            and ignore_recency_guard_global):
        reasons.append(
            f"--ignore-recency-guard ACTIVE: bypassing {RECENT_CHANGE_WINDOW_DAYS}d "
            f"guard globally (actual {days_since_change}d since last change). "
            f"OPTIMISTIC-VIEW MODE — not recommended for execution without review."
        )

    # 5. Tactical TIGHTEN — can't be in a raise program
    if tactical_recommendation == "TIGHTEN":
        reasons.append(
            f"tactical layer flags TIGHTEN — actual CPA running materially "
            f"above target. Tactical correction must drive tCPA down first; "
            f"strategic raise is incompatible until tactical re-stabilizes."
        )
        return ("EXCLUDED_TACTICAL_FIRST", reasons)

    # 6. Stat-power floor for warmup-track (sub-cohort-2 floor)
    if conv_30d < COHORT2_MIN_CONV_30D:
        if budget_lost_is is not None and budget_lost_is > WARMUP_MIN_BUDGET_LOST:
            reasons.append(
                f"only {conv_30d:.0f} conv/30d (<{COHORT2_MIN_CONV_30D}); "
                f"budget_lost_is {budget_lost_is:.0%} > {WARMUP_MIN_BUDGET_LOST:.0%} "
                f"→ warmup-track (budget-only expansion until "
                f"{WARMUP_GRADUATION_CONV_30D} conv/30d)"
            )
            return ("WARMUP_BUDGET_ONLY", reasons)
        else:
            reasons.append(
                f"only {conv_30d:.0f} conv/30d and budget loss is "
                f"{(budget_lost_is or 0):.0%} (≤{WARMUP_MIN_BUDGET_LOST:.0%}) — "
                f"no clear lever for volume expansion"
            )
            return ("EXCLUDED_LOW_HEADROOM", reasons)

    # 7. Cohort-1 gate evaluation
    cohort1_pass = True
    cohort1_fail_reasons = []

    if conv_30d < COHORT_GATE_MIN_CONV_30D:
        cohort1_pass = False
        cohort1_fail_reasons.append(
            f"only {conv_30d:.0f} conv/30d (<{COHORT_GATE_MIN_CONV_30D})"
        )
    if consec_days_at_target < COHORT_GATE_DAYS_AT_TARGET:
        cohort1_pass = False
        cohort1_fail_reasons.append(
            f"only {consec_days_at_target} consec days at target "
            f"(<{COHORT_GATE_DAYS_AT_TARGET}); 7d-CPA must be ≤ "
            f"tCPA × {COHORT_GATE_CPA_RATIO}"
        )
    # Headroom evidence (v1.2): TWO alternative paths — rank-IS removed
    #   (a) cost-efficiency-30d path: CPA running below target on 30d
    #   (b) ROAS-14d path: recent margin clearly above floor
    # Rationale: rank-IS dropped from cohort gating per stakeholder
    # decision. IS metrics are noisier in Microsoft and don't directly
    # speak to profitability — ROAS does. Rank-IS is RETAINED inside the
    # elasticity-adjustment model (forecast generation), where it
    # represents auction availability. Excluded only from the cohort
    # entry signal.
    cpa_path_passes = (cpa_30d is not None
                       and cpa_30d <= current_tcpa * COHORT_GATE_HEADROOM_CPA_RATIO)
    roas_path_passes = (roas_14d is not None
                        and roas_14d >= COHORT_GATE_HEADROOM_ROAS_14D)
    if not (cpa_path_passes or roas_path_passes):
        cohort1_pass = False
        cpa_str = f"${cpa_30d:.2f}" if cpa_30d else "n/a"
        roas_str = f"{roas_14d:.0%}" if roas_14d is not None else "n/a"
        cohort1_fail_reasons.append(
            f"no headroom evidence: cost-efficiency path needs "
            f"cpa_30d ≤ tCPA × {COHORT_GATE_HEADROOM_CPA_RATIO} "
            f"(have {cpa_str} vs ${current_tcpa * COHORT_GATE_HEADROOM_CPA_RATIO:.2f}); "
            f"ROAS-14d path needs roas_14d ≥ "
            f"{COHORT_GATE_HEADROOM_ROAS_14D:.0%} (have {roas_str})"
        )

    if cohort1_pass:
        # Prefer ROAS as the named path (most direct profitability evidence)
        if roas_path_passes:
            headroom_path = "ROAS-14d"
        else:
            headroom_path = "cost-efficiency"
        cpa_30d_str = f"${cpa_30d:.2f}" if cpa_30d else "n/a"
        roas_str = f"{roas_14d:.0%}" if roas_14d is not None else "n/a"
        reasons.append(
            f"COHORT 1: {consec_days_at_target} consec days at target, "
            f"{conv_30d:.0f} conv/30d, headroom via {headroom_path} path "
            f"(cpa_30d={cpa_30d_str} vs "
            f"tCPA × 0.95 = ${current_tcpa * COHORT_GATE_HEADROOM_CPA_RATIO:.2f}, "
            f"roas_14d={roas_str}), "
            f"{days_since_change}d since last change, "
            f"tactical={tactical_recommendation}"
        )
        return ("COHORT_1", reasons)

    # 8. Cohort 2 fallback
    reasons.append(
        f"COHORT 2: failed cohort-1 gate ({'; '.join(cohort1_fail_reasons)}). "
        f"Stat-power adequate ({conv_30d:.0f} conv/30d ≥ {COHORT2_MIN_CONV_30D}), "
        f"no exclusions; revisit at week 4 once cohort-1 results are visible "
        f"and any pending stabilization completes."
    )
    return ("COHORT_2", reasons)


def cohort_priority_rank(
    cohort: str,
    profit_delta_at_initial_step: float | None,
    spend_30d: float,
) -> tuple[int, float]:
    """Returns (sort_tier, sort_value) for ranking within cohort.
       Lower tier = higher priority. Bigger winners first."""
    cohort_tier = {
        "MARGIN_RESTORATION": 0,         # highest priority — currently violating margin
        "COHORT_1": 1,
        "COHORT_2": 2,
        "COHORT_3": 3,
        "WARMUP_BUDGET_ONLY": 4,
        "EXCLUDED_RECALIBRATING": 5,
        "EXCLUDED_TACTICAL_FIRST": 6,
        "EXCLUDED_LOW_HEADROOM": 7,
    }.get(cohort, 9)
    # Within tier, rank by absolute forecasted profit delta if available,
    # else by 30d spend (proxy for impact magnitude)
    if profit_delta_at_initial_step is not None:
        return (cohort_tier, -profit_delta_at_initial_step)
    return (cohort_tier, -spend_30d)




# ═══════════════════════════════════════════════════════════════════
# WEEK-BY-WEEK STAIR-STEP PLAN GENERATION
# ═══════════════════════════════════════════════════════════════════

def generate_stairstep_plan(
    campaign_name: str,
    cohort: str,
    current_tcpa: float | None,
    profit_optimal_tcpa: float | None,
    margin_ceiling: float | None,
    program_start: date,
    program_weeks: int = PROGRAM_WEEKS,
    cpa_30d: float | None = None,
    destination_conservative: float | None = None,
    destination_optimistic: float | None = None,
) -> list[dict]:
    """Returns a list of {week, week_start, action, target_tcpa, depends_on}
       describing the planned moves for this campaign over the program.

       Cohort 1: Week 1 raise → settle 7-10d → Budget +30% → settle →
                 Week 3-4 second tCPA raise (volatility-gated) → settle →
                 Budget +30% → ... up to profit-optimal cap.
       Cohort 2: same structure, shifted by 3 weeks.
       Warmup:   Budget-only +30% increments at 14d intervals until
                 graduation to cohort eval.
       Margin restoration: Weeks 1-3 = tactical descent (handled by
                 tactical layer). Week 4 re-evaluation. Weeks 4-12 =
                 conditional stair-step toward destination IF descent
                 succeeded; else extend tactical phase.
       Excluded: empty plan (annotated).
    """
    plan = []

    if cohort == "MARGIN_RESTORATION":
        # The destination is the conservative-scenario tCPA (≤ margin
        # ceiling under the assumption that current CPA-overshoot persists).
        # If destination_conservative is None, fall back to margin_ceiling.
        dest_conservative = (destination_conservative if destination_conservative
                             else margin_ceiling)
        dest_optimistic = destination_optimistic
        # Phase 1 — tactical descent (weeks 1-3)
        # The tactical layer drives tCPA down. Strategic layer just
        # documents the destination + conditions for handoff back.
        plan.append({
            "week": 1, "week_start": program_start,
            "action": "TACTICAL_DESCENT_BEGINS",
            "target_tcpa": current_tcpa,
            "rationale": (f"Hand off to tactical layer for weekly TIGHTEN "
                          f"decrements. Destination (conservative): "
                          f"${dest_conservative:.2f} if margin-overshoot "
                          f"persists. "
                          + (f"Destination (optimistic): "
                             f"${dest_optimistic:.2f} if CPA-to-tCPA ratio "
                             f"normalizes to account-typical."
                             if dest_optimistic else "")),
            "depends_on": "diagnostic_complete",
        })
        plan.append({
            "week": 2, "week_start": program_start + timedelta(weeks=1),
            "action": "TACTICAL_CONTINUES",
            "target_tcpa": None,
            "rationale": "tactical layer in control; no strategic action.",
            "depends_on": None,
        })
        plan.append({
            "week": 3, "week_start": program_start + timedelta(weeks=2),
            "action": "TACTICAL_CONTINUES",
            "target_tcpa": None,
            "rationale": "tactical layer in control; no strategic action.",
            "depends_on": None,
        })
        # Phase 2 — week 4 re-evaluation
        plan.append({
            "week": 4, "week_start": program_start + timedelta(weeks=3),
            "action": "RE_EVALUATE_DESCENT",
            "target_tcpa": None,
            "rationale": (f"Check: did tactical drive 7d CPA to ≤ tCPA × "
                          f"{COHORT_GATE_CPA_RATIO}? If yes → re-enter "
                          f"cohort assignment as COHORT_2 candidate (with "
                          f"corrected destination). If no → extend tactical "
                          f"phase to week 6 and re-eval."),
            "depends_on": "tactical_descent_phase_complete",
        })
        # Phase 3 — conditional stair-step (weeks 5-12)
        # If descent succeeded by week 4, plan a stair-step toward the
        # optimistic destination (which reflects fixed CPA-to-tCPA ratio).
        if dest_optimistic and current_tcpa and dest_optimistic > current_tcpa + 0.50:
            T = current_tcpa
            wk = 5
            step_n = 1
            while T < dest_optimistic - 0.50 and wk <= program_weeks:
                next_T = min(T * (1 + STEP_INITIAL_PCT), dest_optimistic,
                             margin_ceiling or dest_optimistic)
                if next_T <= T + 0.50:
                    break
                plan.append({
                    "week": wk,
                    "week_start": program_start + timedelta(weeks=wk - 1),
                    "action": f"TCPA_RAISE_to_${next_T:.2f}_CONDITIONAL",
                    "target_tcpa": round(next_T, 2),
                    "rationale": (f"step {step_n} of post-descent stair-step "
                                  f"toward optimistic destination "
                                  f"${dest_optimistic:.2f}. CONDITIONAL on "
                                  f"week-4 re-eval finding successful descent."),
                    "depends_on": "descent_confirmed_at_week_4",
                })
                T = next_T
                step_n += 1
                wk += 3
                if step_n > 5:
                    break
        else:
            plan.append({
                "week": program_weeks,
                "week_start": program_start + timedelta(weeks=program_weeks - 1),
                "action": "HOLD_AT_DESTINATION",
                "target_tcpa": dest_conservative,
                "rationale": ("post-descent: hold at conservative destination. "
                              "No upside path because either no optimistic "
                              "destination computed, or current tCPA already "
                              "near it."),
                "depends_on": "descent_confirmed_at_week_4",
            })
        return plan

    if cohort.startswith("EXCLUDED"):
        plan.append({
            "week": 1, "week_start": program_start, "action": "NO_ACTION",
            "target_tcpa": None, "rationale": f"{cohort} — see cohort assignment",
            "depends_on": None,
        })
        return plan

    if cohort == "WARMUP_BUDGET_ONLY":
        # Budget bumps every 14 days until conv 30d ≥ graduation threshold
        for i, week in enumerate([1, 3, 5, 7, 9, 11]):
            wk_start = program_start + timedelta(weeks=week - 1)
            plan.append({
                "week": week,
                "week_start": wk_start,
                "action": f"BUDGET_BUMP_+{int(BUDGET_STEP_PCT*100)}%",
                "target_tcpa": current_tcpa,
                "rationale": (f"warmup track — conditional on still being "
                              f"<{WARMUP_GRADUATION_CONV_30D} conv/30d AND "
                              f"budget_lost_is > {WARMUP_MIN_BUDGET_LOST:.0%}"),
                "depends_on": "prior_step_settled",
            })
        plan.append({
            "week": program_weeks,
            "week_start": program_start + timedelta(weeks=program_weeks - 1),
            "action": "REASSESS_FOR_COHORT",
            "target_tcpa": current_tcpa,
            "rationale": f"if conv_30d ≥ {WARMUP_GRADUATION_CONV_30D}, "
                         f"re-enter cohort assignment as cohort 1/2 candidate",
            "depends_on": None,
        })
        return plan

    # Cohort 1 / 2 / 3 — tCPA stair-step
    week_offset = {"COHORT_1": 0, "COHORT_2": 3, "COHORT_3": 6}.get(cohort, 0)

    if current_tcpa is None or profit_optimal_tcpa is None:
        return plan
    # The stair-step approaches profit-optimal in 15% increments, capped
    # by margin ceiling. Compute the move sequence.
    moves = []
    T = current_tcpa
    step = 1
    while T < profit_optimal_tcpa - 0.50:
        next_T = min(T * (1 + STEP_INITIAL_PCT), profit_optimal_tcpa, margin_ceiling)
        if next_T <= T + 0.50:
            break
        moves.append(round(next_T, 2))
        T = next_T
        step += 1
        if step > 5:  # safety
            break

    # Place moves in the calendar
    week_cursor = 1 + week_offset
    for i, target in enumerate(moves):
        # tCPA raise
        plan.append({
            "week": week_cursor,
            "week_start": program_start + timedelta(weeks=week_cursor - 1),
            "action": f"TCPA_RAISE_to_${target:.2f}",
            "target_tcpa": target,
            "rationale": (f"step {i+1} of stair-step: raise toward profit-optimal "
                          f"${profit_optimal_tcpa:.2f}; "
                          f"hard 14d floor before next raise; "
                          f"volatility-gated settle for next-step eligibility"),
            "depends_on": "prior_step_settled" if i > 0 else "cohort_entry_gate",
        })
        # Budget bump after settle (typically 1-2 weeks after tCPA move,
        # depending on volatility detector)
        plan.append({
            "week": week_cursor + 1,
            "week_start": program_start + timedelta(weeks=week_cursor),
            "action": f"BUDGET_BUMP_+{int(BUDGET_STEP_PCT*100)}%",
            "target_tcpa": target,
            "rationale": (f"remove budget constraint to convert tCPA headroom "
                          f"into volume; conditional on tCPA settled "
                          f"(volatility-gated) AND budget_lost_is > "
                          f"{WARMUP_MIN_BUDGET_LOST:.0%}"),
            "depends_on": "tcpa_step_settled",
        })
        week_cursor += 3  # 2-3 weeks between tCPA moves on same campaign
        if week_cursor > program_weeks:
            break

    return plan


def settled_check(
    daily_cpa_df: pd.DataFrame,
    new_tcpa: float,
    raise_date: date,
    today: date,
) -> tuple[bool, str]:
    """Volatility-gated convergence detector. Was the tCPA raise made on
       raise_date considered 'settled' by `today`?

       - Compute CV over post-raise window
       - Low vol (CV < 0.20): stable within ±10% for 3 consecutive days
                              OR 10 calendar days, whichever first
       - High vol (CV ≥ 0.20): hard 14-day floor + 7-of-10 trailing days
                              at 7d-CPA ≤ tCPA × 1.10
    """
    days_since_raise = (today - raise_date).days
    if days_since_raise < 7:
        return (False, f"{days_since_raise}d since raise (<7 min)")

    # CV computation over post-raise window
    post = daily_cpa_df[
        (daily_cpa_df["date"] >= raise_date)
        & (daily_cpa_df["date"] <= today)
    ].dropna(subset=["cpa_7d"])
    if len(post) < 4:
        return (False, "insufficient post-raise data for CV")
    cv = cpa_volatility_cv(daily_cpa_df, lookback_days=days_since_raise)
    if cv is None:
        return (False, "could not compute volatility")

    if cv < VOLATILITY_THRESHOLD_CV:
        # Low-vol path
        if days_since_raise >= LOW_VOL_FALLBACK_DAYS:
            return (True, f"low-vol (CV={cv:.2f}); {days_since_raise}d ≥ "
                          f"{LOW_VOL_FALLBACK_DAYS}d fallback")
        # Check 3-consecutive-day stability
        last3 = post.tail(3)["cpa_7d"].tolist()
        if len(last3) == 3:
            mn, mx = min(last3), max(last3)
            if mn > 0 and (mx - mn) / mn <= LOW_VOL_STABILITY_PCT:
                return (True,
                        f"low-vol (CV={cv:.2f}); 3-day stability achieved "
                        f"(range {(mx-mn)/mn:.0%})")
        return (False, f"low-vol (CV={cv:.2f}); awaiting stability")
    else:
        # High-vol path
        if days_since_raise < SECOND_RAISE_HARD_FLOOR_DAYS:
            return (False, f"high-vol (CV={cv:.2f}); hard floor "
                           f"{days_since_raise}/{SECOND_RAISE_HARD_FLOOR_DAYS}d")
        # 7-of-10 trailing days at CPA ≤ tCPA × 1.10
        trailing = post.tail(HIGH_VOL_TRAILING_WINDOW)
        threshold = new_tcpa * COHORT_GATE_CPA_RATIO
        days_at_target = (trailing["cpa_7d"] <= threshold).sum()
        if days_at_target >= HIGH_VOL_DAYS_AT_TARGET:
            return (True,
                    f"high-vol (CV={cv:.2f}); {days_at_target}/"
                    f"{HIGH_VOL_TRAILING_WINDOW} trailing days at "
                    f"7d-CPA ≤ tCPA × {COHORT_GATE_CPA_RATIO}")
        return (False,
                f"high-vol (CV={cv:.2f}); only {days_at_target}/"
                f"{HIGH_VOL_TRAILING_WINDOW} trailing days at target "
                f"(need {HIGH_VOL_DAYS_AT_TARGET})")


# ═══════════════════════════════════════════════════════════════════
# TRIGGER EVALUATION
# ═══════════════════════════════════════════════════════════════════

def evaluate_triggers(
    campaign_id: str,
    campaign_name: str,
    current_tcpa: float | None,
    cpa_7d: float | None,
    cpa_14d: float | None,
    spend_7d: float,
    spend_prior_7d: float,
    conv_7d: float,
    conv_prior_7d: float,
    rpl_dist: dict,
    tactical_recommendation: str | None,
) -> dict:
    """Evaluate all hard and soft triggers. Returns dict with one entry
       per trigger: {fired, rationale}."""
    out = {}

    # ── HARD TRIGGER 1: RPL drift > 2σ ────────────────────────────
    drifting, rationale = rpl_drift_signal(rpl_dist)
    out["HARD_RPL_DRIFT"] = {"fired": drifting, "rationale": rationale}

    # ── HARD TRIGGER 2: volume collapse 7d WoW < −25% ─────────────
    if conv_prior_7d > 0:
        wow = (conv_7d - conv_prior_7d) / conv_prior_7d
        fired = wow < TRIGGER_VOLUME_COLLAPSE_WOW
        out["HARD_VOLUME_COLLAPSE"] = {
            "fired": fired,
            "rationale": f"7d conv WoW {wow:+.0%} (vs threshold "
                         f"{TRIGGER_VOLUME_COLLAPSE_WOW:.0%})",
        }
    else:
        out["HARD_VOLUME_COLLAPSE"] = {"fired": False, "rationale": "no prior 7d conv"}

    # ── HARD TRIGGER 3: spend acceleration without conversions ────
    spend_wow = ((spend_7d - spend_prior_7d) / spend_prior_7d) if spend_prior_7d > 0 else 0
    conv_wow = ((conv_7d - conv_prior_7d) / conv_prior_7d) if conv_prior_7d > 0 else 0
    divergence = spend_wow - conv_wow
    fired = divergence > TRIGGER_SPEND_DIVERGENCE_PP
    out["HARD_SPEND_DIVERGENCE"] = {
        "fired": fired,
        "rationale": f"spend WoW {spend_wow:+.0%}, conv WoW {conv_wow:+.0%}; "
                     f"divergence {divergence:+.0%} "
                     f"(threshold {TRIGGER_SPEND_DIVERGENCE_PP:+.0%})",
    }

    # ── SOFT TRIGGER 1: 7d CPA breach ─────────────────────────────
    if current_tcpa and cpa_7d:
        ratio = cpa_7d / current_tcpa
        fired = ratio > TRIGGER_CPA_BREACH_RATIO
        out["SOFT_CPA_BREACH"] = {
            "fired": fired,
            "rationale": f"7d CPA ${cpa_7d:.2f} vs tCPA ${current_tcpa:.2f} "
                         f"(ratio {ratio:.2f} vs threshold "
                         f"{TRIGGER_CPA_BREACH_RATIO:.2f})",
        }
    else:
        out["SOFT_CPA_BREACH"] = {"fired": False, "rationale": "no 7d CPA"}

    # ── SOFT TRIGGER 2: tactical layer flips to TIGHTEN ───────────
    out["SOFT_TACTICAL_TIGHTEN"] = {
        "fired": tactical_recommendation == "TIGHTEN",
        "rationale": f"tactical recommendation = {tactical_recommendation}",
    }

    return out


def aggregate_blended_margin(rows: pd.DataFrame) -> float | None:
    """Account-level forecasted blended margin from per-campaign rows.
       Returns None if forecast_revenue/forecast_spend not populated."""
    if "forecast_revenue" not in rows.columns or "forecast_spend" not in rows.columns:
        return None
    rev = rows["forecast_revenue"].sum()
    spend = rows["forecast_spend"].sum()
    if rev <= 0:
        return None
    return (rev - spend) / rev




# ═══════════════════════════════════════════════════════════════════
# CAMPAIGN HEALTH FLAGS (v1.3 — diagnostic that reads patterns from
# weekly history, surfacing risks the strategic model alone misses)
# ═══════════════════════════════════════════════════════════════════
#
# Each campaign gets six diagnostic readings:
#   1. RPL momentum (12-week slope, classified RISING/FLAT/DECLINING)
#   2. Weekly profit volatility (CV across last 8 weeks)
#   3. Recent CPA breaches (count of weeks in last 8 where weekly CPA
#      exceeded current tCPA)
#   4. Recent profit trend (last 4 weeks vs prior 8 weeks)
#   5. Conv volume trend (same window, conversion-volume axis)
#   6. Suggested action label (advisory; does NOT override cohort)
#
# These are READ-ONLY signals — they don't reassign cohorts. They
# surface the patterns visible in the per-campaign performance charts
# (RPL momentum, recent volatility, CPA breaches) so the strategic
# workbook reflects them too.

HEALTH_RPL_SLOPE_THRESHOLD     = 1.00   # $/week minimum slope to be "rising"/"declining"
HEALTH_RPL_R2_THRESHOLD        = 0.20   # min R² for trend to be classified non-FLAT
HEALTH_VOLATILITY_LOW          = 0.50   # CV < 0.50 = LOW volatility
HEALTH_VOLATILITY_HIGH         = 1.00   # CV ≥ 1.00 = HIGH volatility
HEALTH_TREND_ACCELERATING      = 1.20   # recent/prior ratio ≥ 1.20 = accelerating
HEALTH_TREND_DECELERATING      = 0.80   # recent/prior ratio < 0.80 = decelerating
HEALTH_LOOKBACK_WEEKS_TOTAL    = 12     # weeks for RPL momentum slope
HEALTH_LOOKBACK_WEEKS_RECENT   = 4      # "recent" window
HEALTH_LOOKBACK_WEEKS_PRIOR    = 8      # "prior" window for trend comparison
HEALTH_BREACH_OCCASIONAL_MAX   = 2      # ≤2 breaches = OCCASIONAL; ≥3 = RECURRING


def _aggregate_weekly_for_health(
    daily_df: pd.DataFrame,
    audience_df: pd.DataFrame | None,
    end_date: date,
    weeks: int,
) -> pd.DataFrame:
    """Roll daily campaign data up to ISO weeks (Mon-Sun) for health analysis.
    Combines search-side and audience-side spend if audience data provided.

    Returns columns: week_start, conv, spend, conv_value, cpa, rpl, profit.
    Only weeks fully within [end_date - weeks*7, end_date] are returned.

    NOTE: Both daily_full and aud_full use column name 'date' (not
    'report_date') — matches the strategic script's query_campaign_daily
    and query_audience_network_daily output schemas.
    """
    if daily_df.empty:
        return pd.DataFrame(columns=["week_start", "conv", "spend",
                                      "conv_value", "cpa", "rpl", "profit"])

    df = daily_df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Combine audience spend if present (matches strategic script's profit math)
    if audience_df is not None and not audience_df.empty:
        aud = audience_df.copy()
        aud["date"] = pd.to_datetime(aud["date"]).dt.date
        # Reduce audience to per-day spend
        aud_daily = aud.groupby("date").agg(
            audience_cost_usd=("audience_cost_usd", "sum")
        ).reset_index()
        df = df.merge(aud_daily, on="date", how="left")
        df["audience_cost_usd"] = df["audience_cost_usd"].fillna(0)
        df["spend_total"] = df["cost_usd"] + df["audience_cost_usd"]
    else:
        df["spend_total"] = df["cost_usd"]

    # ISO Monday alignment
    df["report_dt"] = pd.to_datetime(df["date"])
    df["week_start"] = (df["report_dt"]
                        - pd.to_timedelta(df["report_dt"].dt.weekday, unit="D")).dt.date

    agg = df.groupby("week_start").agg(
        spend=("spend_total", "sum"),
        conv=("primary_conv_count", "sum"),
        conv_value=("primary_conv_value", "sum"),
    ).reset_index().sort_values("week_start").reset_index(drop=True)

    # Filter to last `weeks` complete-or-partial weeks ending at end_date
    end_week_monday = end_date - timedelta(days=end_date.weekday())
    window_start = end_week_monday - timedelta(weeks=weeks - 1)
    agg = agg[agg["week_start"] >= window_start].reset_index(drop=True)

    agg["cpa"] = agg.apply(
        lambda r: r["spend"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["rpl"] = agg.apply(
        lambda r: r["conv_value"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["profit"] = agg["conv_value"] - agg["spend"]
    return agg


def compute_rpl_momentum(weekly_df: pd.DataFrame) -> dict:
    """Linear-regression slope of weekly RPL.

    Returns {slope_dollars_per_week, r_squared, classification, n_weeks}.
    classification ∈ {RISING, FLAT, DECLINING, INSUFFICIENT_DATA}.
    """
    valid = weekly_df.dropna(subset=["rpl"]).reset_index(drop=True)
    if len(valid) < 4:
        return {
            "slope_dollars_per_week": None,
            "r_squared": None,
            "classification": "INSUFFICIENT_DATA",
            "n_weeks": len(valid),
        }
    x = np.arange(len(valid), dtype=float)
    y = valid["rpl"].values.astype(float)
    # Simple OLS
    x_mean, y_mean = x.mean(), y.mean()
    denom = np.sum((x - x_mean) ** 2)
    if denom == 0:
        return {"slope_dollars_per_week": 0.0, "r_squared": 0.0,
                "classification": "FLAT", "n_weeks": len(valid)}
    slope = np.sum((x - x_mean) * (y - y_mean)) / denom
    intercept = y_mean - slope * x_mean
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y_mean) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    if r_squared >= HEALTH_RPL_R2_THRESHOLD:
        if slope >= HEALTH_RPL_SLOPE_THRESHOLD:
            classification = "RISING"
        elif slope <= -HEALTH_RPL_SLOPE_THRESHOLD:
            classification = "DECLINING"
        else:
            classification = "FLAT"
    else:
        classification = "FLAT"
    return {
        "slope_dollars_per_week": round(slope, 2),
        "r_squared": round(r_squared, 2),
        "classification": classification,
        "n_weeks": len(valid),
    }


def compute_profit_volatility(weekly_df: pd.DataFrame) -> dict:
    """Coefficient of variation across the last 8 weeks of profit.

    CV = stdev / |mean|. Returns classification ∈ {LOW, MODERATE, HIGH,
    UNDEFINED} (UNDEFINED if mean profit ≈ 0 — CV is meaningless there).
    """
    recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_PRIOR).copy()
    if len(recent) < 4:
        return {"cv": None, "mean_profit": None,
                "classification": "INSUFFICIENT_DATA"}
    profits = recent["profit"].values.astype(float)
    mean_p = profits.mean()
    if abs(mean_p) < 50:
        # Mean too close to zero for CV to be meaningful
        return {"cv": None, "mean_profit": round(mean_p, 2),
                "classification": "UNDEFINED"}
    std_p = profits.std(ddof=1) if len(profits) > 1 else 0.0
    cv = std_p / abs(mean_p)
    if cv < HEALTH_VOLATILITY_LOW:
        classification = "LOW"
    elif cv < HEALTH_VOLATILITY_HIGH:
        classification = "MODERATE"
    else:
        classification = "HIGH"
    return {
        "cv": round(cv, 2),
        "mean_profit": round(mean_p, 2),
        "classification": classification,
    }


def compute_margin_ceiling_breaches(
    weekly_df: pd.DataFrame, margin_ceiling: float | None
) -> dict:
    """Count weeks in the last 8 where weekly CPA exceeded the MARGIN CEILING.

    This is the policy-relevant breach metric. Margin ceiling = max
    sustainable CPA under min(RPL−$8, RPL × (1 − C2_floor)). When weekly
    CPA exceeds this, the campaign is operating outside margin policy —
    that's the genuine concern that should drive a watchlist label.

    Returns {n_breaches, classification, breach_weeks}.
    classification ∈ {NONE, OCCASIONAL, RECURRING, NO_CEILING_REFERENCE}.
    """
    if margin_ceiling is None or margin_ceiling <= 0:
        return {"n_breaches": None, "classification": "NO_CEILING_REFERENCE",
                "breach_weeks": []}
    recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_PRIOR).copy()
    breaches = recent[
        recent["cpa"].notna() & (recent["cpa"] > margin_ceiling)
    ]
    n = len(breaches)
    if n == 0:
        classification = "NONE"
    elif n <= HEALTH_BREACH_OCCASIONAL_MAX:
        classification = "OCCASIONAL"
    else:
        classification = "RECURRING"
    return {
        "n_breaches": n,
        "classification": classification,
        "breach_weeks": breaches["week_start"].astype(str).tolist(),
    }


def compute_tcpa_overshoots(
    weekly_df: pd.DataFrame, current_tcpa: float | None
) -> dict:
    """Count weeks in the last 8 where weekly CPA exceeded the current tCPA.

    DIAGNOSTIC ONLY — does NOT drive the action label. Under
    MaxConversions+tCPA bidding, tCPA is the algorithm's TARGET, not a
    ceiling — so weekly CPA above tCPA is normal MaxConversions noise.
    A high overshoot count tells you the bidder is overshooting often
    (informative for sensing how aggressively the algorithm is bidding
    relative to its target), but it isn't a margin-policy violation
    unless the overshoots also breach the margin ceiling.

    Returns {n_overshoots, classification, overshoot_weeks}.
    classification ∈ {NONE, OCCASIONAL, FREQUENT, NO_TCPA_REFERENCE}.
    """
    if current_tcpa is None or current_tcpa <= 0:
        return {"n_overshoots": None, "classification": "NO_TCPA_REFERENCE",
                "overshoot_weeks": []}
    recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_PRIOR).copy()
    overshoots = recent[
        recent["cpa"].notna() & (recent["cpa"] > current_tcpa)
    ]
    n = len(overshoots)
    if n == 0:
        classification = "NONE"
    elif n <= HEALTH_BREACH_OCCASIONAL_MAX:
        classification = "OCCASIONAL"
    else:
        classification = "FREQUENT"
    return {
        "n_overshoots": n,
        "classification": classification,
        "overshoot_weeks": overshoots["week_start"].astype(str).tolist(),
    }


def compute_profit_trend(weekly_df: pd.DataFrame) -> dict:
    """Mean weekly profit over the last 4 weeks vs prior 8 weeks.

    Returns {recent_mean, prior_mean, ratio, classification}.
    classification ∈ {ACCELERATING, STABLE, DECELERATING,
                      DETERIORATING, INSUFFICIENT_DATA}.
    DETERIORATING dominates: recent <0 AND prior >0 = qualitative break.
    """
    if len(weekly_df) < HEALTH_LOOKBACK_WEEKS_RECENT + HEALTH_LOOKBACK_WEEKS_PRIOR:
        # Not enough data — fall back to comparing whatever's available
        recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_RECENT)
        prior = weekly_df.iloc[:-HEALTH_LOOKBACK_WEEKS_RECENT] if len(weekly_df) > HEALTH_LOOKBACK_WEEKS_RECENT else pd.DataFrame()
    else:
        recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_RECENT)
        prior = weekly_df.iloc[
            -(HEALTH_LOOKBACK_WEEKS_RECENT + HEALTH_LOOKBACK_WEEKS_PRIOR):
            -HEALTH_LOOKBACK_WEEKS_RECENT
        ]
    if recent.empty or prior.empty:
        return {"recent_mean": None, "prior_mean": None, "ratio": None,
                "classification": "INSUFFICIENT_DATA"}
    recent_mean = recent["profit"].mean()
    prior_mean = prior["profit"].mean()

    # Qualitative break check first
    if recent_mean < 0 and prior_mean > 100:
        return {
            "recent_mean": round(recent_mean, 2),
            "prior_mean": round(prior_mean, 2),
            "ratio": None,
            "classification": "DETERIORATING",
        }
    if abs(prior_mean) < 50:
        # Prior near zero — ratio is unstable; report descriptively
        if recent_mean > 100:
            classification = "ACCELERATING"
        elif recent_mean < -50:
            classification = "DECELERATING"
        else:
            classification = "STABLE"
        return {"recent_mean": round(recent_mean, 2),
                "prior_mean": round(prior_mean, 2),
                "ratio": None, "classification": classification}

    ratio = recent_mean / prior_mean
    if ratio >= HEALTH_TREND_ACCELERATING:
        classification = "ACCELERATING"
    elif ratio < HEALTH_TREND_DECELERATING:
        classification = "DECELERATING"
    else:
        classification = "STABLE"
    return {
        "recent_mean": round(recent_mean, 2),
        "prior_mean": round(prior_mean, 2),
        "ratio": round(ratio, 2),
        "classification": classification,
    }


def compute_conv_trend(weekly_df: pd.DataFrame) -> dict:
    """Same logic as compute_profit_trend but on conv volume."""
    if len(weekly_df) < HEALTH_LOOKBACK_WEEKS_RECENT + HEALTH_LOOKBACK_WEEKS_PRIOR:
        recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_RECENT)
        prior = weekly_df.iloc[:-HEALTH_LOOKBACK_WEEKS_RECENT] if len(weekly_df) > HEALTH_LOOKBACK_WEEKS_RECENT else pd.DataFrame()
    else:
        recent = weekly_df.tail(HEALTH_LOOKBACK_WEEKS_RECENT)
        prior = weekly_df.iloc[
            -(HEALTH_LOOKBACK_WEEKS_RECENT + HEALTH_LOOKBACK_WEEKS_PRIOR):
            -HEALTH_LOOKBACK_WEEKS_RECENT
        ]
    if recent.empty or prior.empty:
        return {"recent_mean": None, "prior_mean": None, "ratio": None,
                "classification": "INSUFFICIENT_DATA"}
    recent_mean = recent["conv"].mean()
    prior_mean = prior["conv"].mean()
    if prior_mean < 1:
        # Prior near zero — campaign was warming up; report descriptively
        if recent_mean > 5:
            classification = "ACCELERATING"
        else:
            classification = "STABLE"
        return {"recent_mean": round(recent_mean, 2),
                "prior_mean": round(prior_mean, 2),
                "ratio": None, "classification": classification}
    ratio = recent_mean / prior_mean
    if ratio >= HEALTH_TREND_ACCELERATING:
        classification = "ACCELERATING"
    elif ratio < HEALTH_TREND_DECELERATING:
        classification = "DECELERATING"
    else:
        classification = "STABLE"
    return {
        "recent_mean": round(recent_mean, 2),
        "prior_mean": round(prior_mean, 2),
        "ratio": round(ratio, 2),
        "classification": classification,
    }


def derive_health_action_label(
    rpl_mom: dict,
    profit_vol: dict,
    margin_breach: dict,
    profit_trend: dict,
    conv_trend: dict,
    cohort: str,
    profit_optimal_tcpa: float | None,
    current_tcpa: float | None,
    margin_ceiling: float | None = None,
    tcpa_overshoot: dict | None = None,
) -> tuple[str, str]:
    """Combine the six signals into a single advisory label + rationale.

    The breach signal here is MARGIN-CEILING breach (policy-relevant),
    not tCPA overshoot (which is normal MaxConversions noise). tCPA
    overshoots are reported as a diagnostic in the rationale text but
    do NOT drive the action label by themselves.

    Priority order (first match wins):
      1. MARGIN_RESTORATION_REVISIT — DETERIORATING profit + RECURRING
                                       margin-ceiling breach
      2. WATCHLIST_MARGIN_BREACH — RECURRING margin-ceiling breach (alone)
      3. WATCHLIST_DECLINING_RPL — RPL DECLINING + model proposes raise
      4. INVESTIGATE_VOLUME_SPIKE — conv ACCELERATING + RPL FLAT (CA pattern)
      5. INVESTIGATE_VOLUME_DECLINE — conv DECELERATING + RPL stable/rising
                                      (GA pattern)
      6. RAISE_CANDIDATE_STRONG — model proposes raise, no warnings,
                                   LOW volatility, no margin breaches
      7. RAISE_CANDIDATE_CAUTIOUS — model proposes raise but volatility
                                     moderate or 1-2 margin breaches
      8. STABLE — none of the above

    Returns (label, rationale_string).
    """
    rpl_class = rpl_mom.get("classification", "")
    vol_class = profit_vol.get("classification", "")
    breach_class = margin_breach.get("classification", "")
    profit_trend_class = profit_trend.get("classification", "")
    conv_trend_class = conv_trend.get("classification", "")
    n_breaches = margin_breach.get("n_breaches", 0) or 0
    n_overshoots = (tcpa_overshoot.get("n_overshoots", 0) or 0
                    if tcpa_overshoot else 0)
    overshoot_class = (tcpa_overshoot.get("classification", "")
                       if tcpa_overshoot else "")

    model_proposes_raise = (profit_optimal_tcpa is not None
                            and current_tcpa is not None
                            and profit_optimal_tcpa > current_tcpa + 0.01)

    # Priority 1: deteriorating + breaching
    if profit_trend_class == "DETERIORATING" and breach_class == "RECURRING":
        ceil_str = f"${margin_ceiling:.2f}" if margin_ceiling else "(none)"
        return ("MARGIN_RESTORATION_REVISIT",
                f"profit DETERIORATING (recent 4w mean ${profit_trend.get('recent_mean')}, "
                f"prior 8w mean ${profit_trend.get('prior_mean')}) AND "
                f"{n_breaches} weeks breached margin ceiling {ceil_str} in last 8 — "
                f"campaign may need to return to MARGIN_RESTORATION cohort on next strategic run")

    # Priority 2: recurring margin-ceiling breach alone
    if breach_class == "RECURRING":
        ceil_str = f"${margin_ceiling:.2f}" if margin_ceiling else "(none)"
        return ("WATCHLIST_MARGIN_BREACH",
                f"{n_breaches} weekly CPAs in last 8 weeks exceeded margin "
                f"ceiling {ceil_str} — investigate before any raise; "
                f"model proposing raise = {model_proposes_raise}")

    # Priority 3: declining RPL while model proposes raise
    if rpl_class == "DECLINING" and model_proposes_raise:
        slope = rpl_mom.get("slope_dollars_per_week", 0)
        return ("WATCHLIST_DECLINING_RPL",
                f"RPL DECLINING at {slope}/week (R²={rpl_mom.get('r_squared')}) "
                f"while strategic model proposes tCPA ${current_tcpa:.0f} → "
                f"${profit_optimal_tcpa:.2f}; raising into a declining-RPL trend "
                f"is the ROAS-view's blind spot — defer or downsize")

    # Priority 4: volume spike with flat RPL (CA pattern)
    if conv_trend_class == "ACCELERATING" and rpl_class == "FLAT":
        return ("INVESTIGATE_VOLUME_SPIKE",
                f"conv ACCELERATING (recent 4w {conv_trend.get('recent_mean')}/wk "
                f"vs prior 8w {conv_trend.get('prior_mean')}/wk) but RPL FLAT — "
                f"likely budget/audience/keyword-driven volume change, NOT bid "
                f"response; baseline may not be stable for raise decisions")

    # Priority 5: volume decline with stable/rising RPL (GA pattern)
    if conv_trend_class == "DECELERATING" and rpl_class in ("FLAT", "RISING"):
        return ("INVESTIGATE_VOLUME_DECLINE",
                f"conv DECELERATING (recent {conv_trend.get('recent_mean')}/wk "
                f"vs prior {conv_trend.get('prior_mean')}/wk) but RPL is "
                f"{rpl_class} — campaign may be undergoing a quality shift "
                f"(fewer but better conversions); raise based on stale volume "
                f"may misjudge")

    # Priority 6: strong raise candidate
    if model_proposes_raise and vol_class == "LOW" and breach_class == "NONE":
        overshoot_note = (f", tCPA overshoots {n_overshoots}/8 weeks "
                          f"(normal MaxConv noise — not a margin concern)"
                          if overshoot_class in ("OCCASIONAL", "FREQUENT")
                          else "")
        return ("RAISE_CANDIDATE_STRONG",
                f"model proposes ${current_tcpa:.0f} → ${profit_optimal_tcpa:.2f}, "
                f"LOW volatility, no margin-ceiling breaches, "
                f"RPL {rpl_class.lower()}, profit {profit_trend_class.lower()}"
                f"{overshoot_note}")

    # Priority 7: cautious raise
    if model_proposes_raise:
        return ("RAISE_CANDIDATE_CAUTIOUS",
                f"model proposes raise but volatility {vol_class.lower()}, "
                f"margin breaches {breach_class.lower()} ({n_breaches}); "
                f"consider smaller initial step or longer hold")

    # Priority 8: stable
    return ("STABLE",
            f"no model raise proposed; RPL {rpl_class.lower()}, "
            f"volatility {vol_class.lower()}, "
            f"margin breaches {breach_class.lower()}, "
            f"profit trend {profit_trend_class.lower()}")


def compute_campaign_health_flags(
    daily_full: pd.DataFrame,
    aud_full: pd.DataFrame | None,
    end_date: date,
    rows_df: pd.DataFrame,
) -> pd.DataFrame:
    """Top-level: compute health flags for every campaign in rows_df.

    Returns a DataFrame with one row per campaign, joined to the strategic
    rows (cohort, current_tcpa, profit_optimal_tcpa). All metrics columns
    are explicitly named — no opaque dict columns.
    """
    out_rows = []
    for _, row in rows_df.iterrows():
        cid = str(row["campaign_id"])
        cname = row["campaign_name"]
        current_tcpa = row.get("current_tcpa")
        profit_optimal_tcpa = row.get("profit_optimal_tcpa")
        margin_ceiling = row.get("margin_ceiling")
        cohort = row.get("cohort")

        camp_daily = daily_full[
            daily_full["campaign_id"].astype(str) == cid
        ].copy()
        camp_aud = (aud_full[aud_full["campaign_id"].astype(str) == cid].copy()
                    if (aud_full is not None and not aud_full.empty) else None)

        weekly = _aggregate_weekly_for_health(
            camp_daily, camp_aud, end_date, weeks=HEALTH_LOOKBACK_WEEKS_TOTAL,
        )

        rpl_mom = compute_rpl_momentum(weekly)
        profit_vol = compute_profit_volatility(weekly)
        # v1.3.1: split into two metrics — margin-ceiling breach (policy)
        # and tCPA overshoot (informational, normal MaxConv noise)
        margin_breach = compute_margin_ceiling_breaches(weekly, margin_ceiling)
        tcpa_overshoot = compute_tcpa_overshoots(weekly, current_tcpa)
        profit_trend = compute_profit_trend(weekly)
        conv_trend = compute_conv_trend(weekly)
        action_label, rationale = derive_health_action_label(
            rpl_mom, profit_vol, margin_breach,
            profit_trend, conv_trend,
            cohort, profit_optimal_tcpa, current_tcpa,
            margin_ceiling=margin_ceiling,
            tcpa_overshoot=tcpa_overshoot,
        )

        out_rows.append({
            "campaign_name": cname,
            "cohort": cohort,
            "current_tcpa": current_tcpa,
            "profit_optimal_tcpa": profit_optimal_tcpa,
            "margin_ceiling": margin_ceiling,
            "rpl_slope_per_week": rpl_mom["slope_dollars_per_week"],
            "rpl_r_squared": rpl_mom["r_squared"],
            "rpl_momentum": rpl_mom["classification"],
            "profit_cv": profit_vol["cv"],
            "profit_mean_8w": profit_vol["mean_profit"],
            "profit_volatility": profit_vol["classification"],
            # margin-ceiling breach (policy concern)
            "margin_breaches_8w": margin_breach["n_breaches"],
            "margin_breach_class": margin_breach["classification"],
            # tCPA overshoot (diagnostic only — normal MaxConv volatility)
            "tcpa_overshoots_8w": tcpa_overshoot["n_overshoots"],
            "tcpa_overshoot_class": tcpa_overshoot["classification"],
            "profit_trend_recent_4w": profit_trend["recent_mean"],
            "profit_trend_prior_8w": profit_trend["prior_mean"],
            "profit_trend_ratio": profit_trend["ratio"],
            "profit_trend": profit_trend["classification"],
            "conv_trend_recent_4w": conv_trend["recent_mean"],
            "conv_trend_prior_8w": conv_trend["prior_mean"],
            "conv_trend_ratio": conv_trend["ratio"],
            "conv_trend": conv_trend["classification"],
            "health_action": action_label,
            "health_rationale": rationale,
        })
    return pd.DataFrame(out_rows)


# ═══════════════════════════════════════════════════════════════════
# WORKBOOK WRITERS
# ═══════════════════════════════════════════════════════════════════

def _write_header(ws, headers: list[str], row: int = 1) -> None:
    fill = PatternFill("solid", fgColor=HEADER_FILL)
    font = Font(color=HEADER_FONT, bold=True)
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=h)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _autosize_columns(ws, max_width: int = 50) -> None:
    for col_idx in range(1, ws.max_column + 1):
        letter = get_column_letter(col_idx)
        max_len = 0
        for row_idx in range(1, ws.max_row + 1):
            v = ws.cell(row=row_idx, column=col_idx).value
            if v is not None:
                max_len = max(max_len, min(len(str(v)), max_width))
        ws.column_dimensions[letter].width = max(10, min(max_len + 2, max_width))


def write_strategic_summary(ws, run_date: date, end_date: date,
                            rows: pd.DataFrame, blended_margin: float | None,
                            program_start: date) -> None:
    """Top-of-workbook summary. Banner + cohort counts + this-week actions."""
    ws.cell(row=1, column=1, value="STRATEGIC PROFIT-GROWTH PLAN").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"Run date: {run_date}")
    ws.cell(row=3, column=1, value=f"Analysis end date: {end_date}")
    ws.cell(row=4, column=1, value=f"Program start: {program_start}  ({PROGRAM_WEEKS}-week horizon)")
    ws.cell(row=5, column=1, value=f"Account: {ACCOUNT_LABEL} (id={ACCOUNT_ID})")

    if blended_margin is not None:
        margin_str = f"{blended_margin:.1%}"
        ok = blended_margin >= MARGIN_C3_BLENDED_MIN
        ws.cell(row=6, column=1,
                value=f"Forecasted blended margin (post-program-start moves): "
                      f"{margin_str} {'✓ above floor' if ok else '✗ BELOW FLOOR'}")
        if not ok:
            ws.cell(row=6, column=1).fill = PatternFill("solid", fgColor=TRIGGER_FILL)
    ws.cell(row=7, column=1,
            value=f"Margin policy: tCPA ≤ min(RPL−${MARGIN_C1_DOLLAR_FLOOR:.0f}, "
                  f"RPL×{1-MARGIN_C2_PCT_FLOOR:.0%}); "
                  f"blended ≥ {MARGIN_C3_BLENDED_MIN:.0%}")
    ws.cell(row=8, column=1,
            value=f"Elasticity model (v1): pooled default ε={DEFAULT_ELASTICITY:.2f}; "
                  f"per-campaign fit deferred to v2.")

    # Cohort counts table
    start_row = 11
    ws.cell(row=start_row, column=1, value="COHORT ASSIGNMENTS").font = Font(bold=True)
    cohort_counts = rows["cohort"].value_counts().to_dict() if "cohort" in rows.columns else {}
    cohort_order = ["MARGIN_RESTORATION", "COHORT_1", "COHORT_2", "COHORT_3",
                    "WARMUP_BUDGET_ONLY", "EXCLUDED_RECALIBRATING",
                    "EXCLUDED_TACTICAL_FIRST", "EXCLUDED_LOW_HEADROOM"]
    cohort_fill = {
        "MARGIN_RESTORATION": MARGIN_RESTORATION_FILL,
        "COHORT_1": COHORT1_FILL, "COHORT_2": COHORT2_FILL, "COHORT_3": COHORT3_FILL,
        "WARMUP_BUDGET_ONLY": WARMUP_FILL, "EXCLUDED_RECALIBRATING": EXCLUDED_FILL,
        "EXCLUDED_TACTICAL_FIRST": EXCLUDED_FILL, "EXCLUDED_LOW_HEADROOM": EXCLUDED_FILL,
    }
    r = start_row + 1
    for c in cohort_order:
        n = cohort_counts.get(c, 0)
        if n:
            ws.cell(row=r, column=1, value=c)
            ws.cell(row=r, column=2, value=n)
            ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor=cohort_fill[c])
            r += 1

    # This-week actions table
    r += 2
    ws.cell(row=r, column=1, value="THIS WEEK'S ACTIONS").font = Font(bold=True)
    r += 1
    headers = ["Campaign", "Cohort", "Current tCPA", "Action", "Target tCPA",
               "Profit-optimal tCPA", "Margin ceiling", "Forecast Δprofit/30d"]
    _write_header(ws, headers, row=r)
    r += 1
    actionable = rows[rows["cohort"].isin(["COHORT_1", "COHORT_2", "WARMUP_BUDGET_ONLY"])]
    actionable = actionable.sort_values("priority_rank")
    for _, row in actionable.iterrows():
        ws.cell(row=r, column=1, value=row.get("campaign_name"))
        ws.cell(row=r, column=2, value=row.get("cohort"))
        ws.cell(row=r, column=3, value=row.get("current_tcpa"))
        ws.cell(row=r, column=4, value=row.get("week1_action"))
        ws.cell(row=r, column=5, value=row.get("week1_target_tcpa"))
        ws.cell(row=r, column=6, value=row.get("profit_optimal_tcpa"))
        ws.cell(row=r, column=7, value=row.get("margin_ceiling"))
        ws.cell(row=r, column=8, value=row.get("forecast_profit_delta_30d_initial_step"))
        ws.cell(row=r, column=2).fill = PatternFill(
            "solid", fgColor=cohort_fill.get(row.get("cohort"), NEUTRAL_FILL)
        )
        for col_letter in ("C", "E", "F", "G"):
            cell = ws[f"{col_letter}{r}"]
            if cell.value is not None and isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0.00'
        cell = ws[f"H{r}"]
        if cell.value is not None and isinstance(cell.value, (int, float)):
            cell.number_format = '"$"#,##0'
        r += 1

    _autosize_columns(ws)


def write_profit_optimal_targets(ws, rows: pd.DataFrame) -> None:
    headers = [
        "campaign_name", "current_tcpa", "rpl_30d", "margin_ceiling",
        "binding_constraint", "tactical_rec", "cpa_30d", "consec_days_at_target",
        "conv_30d", "spend_30d", "profit_30d_current",
        "profit_optimal_tcpa", "forecast_profit_30d_at_optimal",
        "profit_delta_at_optimal",
        "step_+15pct_tcpa", "forecast_profit_at_+15pct", "forecast_conv_at_+15pct",
        "step_+15pct_low_band", "step_+15pct_high_band",
        "cohort", "cohort_rationale",
    ]
    _write_header(ws, headers)
    for r_idx, (_, row) in enumerate(rows.iterrows(), start=2):
        for c_idx, h in enumerate(headers, start=1):
            v = row.get(h)
            if pd.isna(v):
                v = None
            ws.cell(row=r_idx, column=c_idx, value=v)
        # Format dollar-ish columns
        for col in ["B", "C", "D", "G", "J", "K", "L", "M", "N", "O", "P", "R", "S"]:
            cell = ws[f"{col}{r_idx}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0.00'
        # forecast conv and forecast profit_30d are bigger numbers
        for col in ["P", "Q"]:
            cell = ws[f"{col}{r_idx}"]
            if col == "Q" and isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0"
    _autosize_columns(ws)
    ws.freeze_panes = "B2"


def write_cohort_plan(ws, plan_rows: list[dict]) -> None:
    """Long-format Gantt-ish: one row per (campaign, week) action."""
    headers = ["campaign_name", "cohort", "week", "week_start", "action",
               "target_tcpa", "depends_on", "rationale"]
    _write_header(ws, headers)
    cohort_fill = {
        "MARGIN_RESTORATION": MARGIN_RESTORATION_FILL,
        "COHORT_1": COHORT1_FILL, "COHORT_2": COHORT2_FILL, "COHORT_3": COHORT3_FILL,
        "WARMUP_BUDGET_ONLY": WARMUP_FILL, "EXCLUDED_RECALIBRATING": EXCLUDED_FILL,
        "EXCLUDED_TACTICAL_FIRST": EXCLUDED_FILL, "EXCLUDED_LOW_HEADROOM": EXCLUDED_FILL,
    }
    for r_idx, row in enumerate(plan_rows, start=2):
        ws.cell(row=r_idx, column=1, value=row.get("campaign_name"))
        ws.cell(row=r_idx, column=2, value=row.get("cohort"))
        ws.cell(row=r_idx, column=3, value=row.get("week"))
        ws.cell(row=r_idx, column=4, value=row.get("week_start"))
        ws.cell(row=r_idx, column=5, value=row.get("action"))
        ws.cell(row=r_idx, column=6, value=row.get("target_tcpa"))
        ws.cell(row=r_idx, column=7, value=row.get("depends_on"))
        ws.cell(row=r_idx, column=8, value=row.get("rationale"))
        ws.cell(row=r_idx, column=2).fill = PatternFill(
            "solid", fgColor=cohort_fill.get(row.get("cohort"), NEUTRAL_FILL)
        )
        if isinstance(ws.cell(row=r_idx, column=6).value, (int, float)):
            ws.cell(row=r_idx, column=6).number_format = '"$"#,##0.00'
    _autosize_columns(ws)
    ws.freeze_panes = "B2"


def write_trigger_log(ws, trigger_rows: list[dict]) -> None:
    headers = ["campaign_name", "trigger", "fired", "severity", "rationale", "rec_action"]
    _write_header(ws, headers)
    for r_idx, row in enumerate(trigger_rows, start=2):
        ws.cell(row=r_idx, column=1, value=row.get("campaign_name"))
        ws.cell(row=r_idx, column=2, value=row.get("trigger"))
        ws.cell(row=r_idx, column=3, value=row.get("fired"))
        ws.cell(row=r_idx, column=4, value=row.get("severity"))
        ws.cell(row=r_idx, column=5, value=row.get("rationale"))
        ws.cell(row=r_idx, column=6, value=row.get("rec_action"))
        if row.get("fired"):
            fill = TRIGGER_FILL if row.get("severity") == "HARD" else ALERT_FILL
            for col in range(1, 7):
                ws.cell(row=r_idx, column=col).fill = PatternFill("solid", fgColor=fill)
    _autosize_columns(ws)
    ws.freeze_panes = "B2"


def write_measurement(ws, rows: pd.DataFrame, end_date: date,
                      blended_margin: float | None) -> None:
    ws.cell(row=1, column=1, value="MEASUREMENT FRAMEWORK").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"As of: {end_date}")
    ws.cell(row=3, column=1, value="")
    ws.cell(row=4, column=1, value="V1 measurement basis:").font = Font(bold=True)
    ws.cell(row=5, column=1,
            value="(b) Phased rollout — cohort 1 (week 1) and cohort 2 (week 4) "
                  "provide concurrent control on weeks 4-7 overlap")
    ws.cell(row=6, column=1,
            value="(c) YoY-adjusted same-week comparison on same campaigns")
    ws.cell(row=7, column=1,
            value="(d) Synthetic control / DiD — DEFERRED to v2 retrospective at week 12. "
                  "Pre-period too short for credible v1 fit (Jan-March restructures).")
    ws.cell(row=9, column=1, value="WEEK 0 BASELINE (this run)").font = Font(bold=True)
    headers = ["campaign_name", "cohort", "spend_30d", "conv_30d",
               "rpl_30d", "cpa_30d", "profit_30d", "rpl_12w_mean",
               "rpl_12w_stdev", "rpl_drift_state"]
    _write_header(ws, headers, row=10)
    r = 11
    for _, row in rows.iterrows():
        ws.cell(row=r, column=1, value=row.get("campaign_name"))
        ws.cell(row=r, column=2, value=row.get("cohort"))
        ws.cell(row=r, column=3, value=row.get("spend_30d"))
        ws.cell(row=r, column=4, value=row.get("conv_30d"))
        ws.cell(row=r, column=5, value=row.get("rpl_30d"))
        ws.cell(row=r, column=6, value=row.get("cpa_30d"))
        ws.cell(row=r, column=7, value=row.get("profit_30d_current"))
        ws.cell(row=r, column=8, value=row.get("rpl_12w_mean"))
        ws.cell(row=r, column=9, value=row.get("rpl_12w_stdev"))
        ws.cell(row=r, column=10, value=row.get("rpl_drift_state"))
        for col in ["C", "E", "F", "G", "H", "I"]:
            cell = ws[f"{col}{r}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0.00'
        r += 1
    if blended_margin is not None:
        r += 2
        ws.cell(row=r, column=1, value=f"Account blended margin (current 30d): "
                                       f"{blended_margin:.1%}").font = Font(bold=True)
        r += 1
        ws.cell(row=r, column=1, value=f"Margin policy floor: {MARGIN_C3_BLENDED_MIN:.0%}")
    _autosize_columns(ws)


def write_profit_headroom_summary(
    ws,
    rows_df: pd.DataFrame,
    blended_margin_current: float | None,
    c2_floor_used: float,
    c2_floor_default: float,
    end_date: date,
) -> None:
    """v1.1 leadership-facing sheet. Shows the destination state (where
    profit is maximized under the relaxed margin policy) for each
    campaign, the gap from current, and the override documentation
    explaining what changed and how to revert.

    Columns are designed to answer "what's the headroom and is the
    model leaving money on the table?"
    """
    ws.cell(row=1, column=1, value="PROFIT HEADROOM SUMMARY").font = Font(
        bold=True, size=14)
    ws.cell(row=2, column=1, value=f"As of: {end_date}")
    r = 4

    # ── Section 1: Account-level headline (3 scenarios) ──────────────
    ws.cell(row=r, column=1, value="ACCOUNT HEADLINE").font = Font(
        bold=True, size=12)
    r += 1
    if blended_margin_current is not None:
        ws.cell(row=r, column=1,
                value=f"Current 30d blended margin: "
                      f"{blended_margin_current:.1%}")
        r += 1

    total_current_profit = rows_df["profit_30d_current"].fillna(0).sum()
    ws.cell(row=r, column=1,
            value=f"Current 30d profit (all enabled): "
                  f"${total_current_profit:,.0f}").font = Font(bold=True)
    r += 1
    ws.cell(row=r, column=1, value="")
    r += 1

    # Three-scenario destinations (sum across enabled campaigns)
    ws.cell(row=r, column=1,
            value="ELASTICITY SENSITIVITY (3 scenarios)").font = Font(bold=True)
    r += 1
    for scenario_name, base_eps in ELASTICITY_SCENARIOS.items():
        col_name = f"forecast_profit_{scenario_name}"
        if col_name in rows_df.columns:
            scenario_total = rows_df[col_name].fillna(
                rows_df["profit_30d_current"].fillna(0)
            ).sum()
            scenario_upside = scenario_total - total_current_profit
            label = scenario_name.upper()
            line = (f"{label} (ε={base_eps:.1f} base): "
                    f"30d profit ${scenario_total:,.0f}  "
                    f"→ upside ${scenario_upside:,.0f} "
                    f"({scenario_upside / max(total_current_profit, 1):+.1%})")
            cell = ws.cell(row=r, column=1, value=line)
            if scenario_name == HEADROOM_HEADLINE_SCENARIO:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor=COHORT1_FILL)
            r += 1
    ws.cell(row=r, column=1, value="")
    r += 1
    ws.cell(row=r, column=1,
            value=f"Headline scenario: {HEADROOM_HEADLINE_SCENARIO.upper()} "
                  f"(ε={ELASTICITY_SCENARIOS[HEADROOM_HEADLINE_SCENARIO]:.1f}). "
                  f"Per-campaign columns below show all 3 destinations + "
                  f"profit numbers at the headline scenario.")
    r += 2

    # ── Section 2: Per-campaign destination state ────────────────────
    ws.cell(row=r, column=1, value="PER-CAMPAIGN HEADROOM").font = Font(
        bold=True, size=12)
    r += 1
    headers = [
        "campaign", "cohort", "current_tcpa",
        "current_roas_14d", "current_margin_14d",
        "destination_conservative", "destination_mid", "destination_aggressive",
        "raise_size_aggressive_pct", "weeks_to_aggressive_dest",
        "current_30d_profit", "aggressive_30d_profit", "monthly_upside_aggressive",
        "binding_constraint", "protection_capped_at",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
        cell.alignment = Alignment(horizontal="left", vertical="center",
                                   wrap_text=True)
    r += 1

    # Sort by aggressive monthly upside descending (biggest opportunities at top)
    sorted_df = rows_df.copy()
    sorted_df["_upside_sort"] = sorted_df.apply(
        lambda x: ((x.get("forecast_profit_aggressive") or
                    x.get("profit_30d_current") or 0)
                   - (x.get("profit_30d_current") or 0)),
        axis=1,
    )
    sorted_df = sorted_df.sort_values("_upside_sort", ascending=False)

    cohort_fill = {
        "MARGIN_RESTORATION": MARGIN_RESTORATION_FILL,
        "COHORT_1": COHORT1_FILL, "COHORT_2": COHORT2_FILL,
        "COHORT_3": COHORT3_FILL, "WARMUP_BUDGET_ONLY": WARMUP_FILL,
        "EXCLUDED_RECALIBRATING": EXCLUDED_FILL,
        "EXCLUDED_TACTICAL_FIRST": EXCLUDED_FILL,
        "EXCLUDED_LOW_HEADROOM": EXCLUDED_FILL,
    }

    for _, row in sorted_df.iterrows():
        cur_tcpa = row.get("current_tcpa")
        dest_conservative = row.get("destination_conservative")
        dest_mid = row.get("destination_mid")
        dest_aggressive = row.get("destination_aggressive")
        cur_profit = row.get("profit_30d_current") or 0
        agg_profit = row.get("forecast_profit_aggressive") or cur_profit

        # Aggressive raise sizing + weeks
        raise_pct_agg = ((dest_aggressive - cur_tcpa) / cur_tcpa
                         if (cur_tcpa and dest_aggressive) else 0)
        if raise_pct_agg > 0:
            steps = max(1, int(raise_pct_agg / 0.15) +
                        (1 if raise_pct_agg % 0.15 > 0.01 else 0))
            weeks_to_dest = steps * 3
        else:
            steps = 0
            weeks_to_dest = 0
        upside_agg = agg_profit - cur_profit

        ws.cell(row=r, column=1, value=row.get("campaign_name"))
        ws.cell(row=r, column=2, value=row.get("cohort"))
        ws.cell(row=r, column=3, value=cur_tcpa)
        roas_14d = row.get("roas_14d")
        ws.cell(row=r, column=4,
                value=f"{roas_14d:.0%}" if pd.notna(roas_14d) else "n/a")
        cur_margin = row.get("current_margin_14d")
        ws.cell(row=r, column=5,
                value=f"{cur_margin:.0%}" if pd.notna(cur_margin) else "n/a")
        ws.cell(row=r, column=6, value=dest_conservative)
        ws.cell(row=r, column=7, value=dest_mid)
        ws.cell(row=r, column=8, value=dest_aggressive)
        ws.cell(row=r, column=9,
                value=f"+{raise_pct_agg:.0%}" if raise_pct_agg > 0 else "—")
        ws.cell(row=r, column=10,
                value=f"~{weeks_to_dest}w" if weeks_to_dest > 0 else "—")
        ws.cell(row=r, column=11, value=cur_profit)
        ws.cell(row=r, column=12, value=agg_profit)
        ws.cell(row=r, column=13, value=upside_agg)
        ws.cell(row=r, column=14, value=row.get("binding_constraint"))
        prot_capped_at = row.get("profit_optimal_protection_capped_at")
        ws.cell(row=r, column=15,
                value=f"${prot_capped_at:.2f}" if pd.notna(prot_capped_at)
                else "—")

        ws.cell(row=r, column=2).fill = PatternFill(
            "solid", fgColor=cohort_fill.get(row.get("cohort"), NEUTRAL_FILL))
        # Format dollar columns: tCPAs (C, F, G, H), profits (K, L, M)
        for col_letter in ("C", "F", "G", "H"):
            cell = ws[f"{col_letter}{r}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0.00'
        for col_letter in ("K", "L", "M"):
            cell = ws[f"{col_letter}{r}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0'
        r += 1

    r += 2

    # ── Section 3: Override documentation ────────────────────────────
    ws.cell(row=r, column=1, value="MARGIN POLICY OVERRIDES & EXECUTION NOTES").font = Font(
        bold=True, size=12)
    r += 1
    override_lines = [
        f"v1.2 changes vs v1.1:",
        "",
        f"A. Rank-IS removed from cohort entry signaling. Cohort-1 entry now "
        f"requires either cost-efficiency-30d (cpa_30d ≤ tCPA × 0.95) or "
        f"ROAS-14d ≥ {COHORT_GATE_HEADROOM_ROAS_14D:.0%}. Rank-IS is RETAINED inside the "
        f"elasticity-adjustment model — it influences forecast magnitudes "
        f"but no longer counts as evidence for cohort entry.",
        "",
        f"B. Three-scenario elasticity sensitivity. Each campaign's "
        f"destination tCPA is computed under three elasticity assumptions:",
        f"   • CONSERVATIVE (ε={ELASTICITY_SCENARIOS['conservative']:.1f}): pooled default; "
        f"matches v1.0/v1.1 behavior",
        f"   • MID (ε={ELASTICITY_SCENARIOS['mid']:.1f}): marginal conv = avg conv (neutral)",
        f"   • AGGRESSIVE (ε={ELASTICITY_SCENARIOS['aggressive']:.1f}): marginal conversions "
        f"are accretive (raising tCPA captures higher-RPL traffic)",
        f"   Headline columns above show all 3 destinations + AGGRESSIVE forecast.",
        f"   See 'Elasticity Sensitivity' sheet for forecast detail per scenario.",
        "",
        f"C. --ignore-recency-guard CLI flag (NEW): bypasses the 14-day "
        f"recent-change guard for ALL campaigns. Use for ideal-state "
        f"modeling. Does not bypass tactical TIGHTEN exclusion or other gates.",
        "",
        f"D. --freeze-as-of CLI flag (NEW): filters change-history to "
        f"entries on or before --end-date. Use for reproducible analysis "
        f"against past states.",
        "",
        f"v1.1 changes (still active):",
        "",
        f"1. C2 floor: 25% → {c2_floor_used:.0%} "
        f"({'CLI override' if abs(c2_floor_used - c2_floor_default) > 1e-6 else 'default'})",
        f"   Justification: account-wide 14d ROAS ranges 118%-783% with "
        f"median ~175% (≈43% margin). Prior 25% C2 floor was binding hard "
        f"on most campaigns.",
        "",
        f"2. Current-margin protection: every proposed raise must satisfy:",
        f"   (a) forecast post-raise margin ≥ {CURRENT_MARGIN_FLOOR_ABSOLUTE:.0%} (absolute)",
        f"   (b) forecast post-raise margin ≥ {CURRENT_MARGIN_FLOOR_RELATIVE:.0%} of "
        f"current observed margin (relative)",
        "",
        f"3. MARGIN_RESTORATION reclassification: requires BOTH 30d AND 14d "
        f"CPA to exceed the margin ceiling.",
        "",
        "EXECUTION NOTES:",
        "",
        f"• Pin analysis to a specific end date (reproducible):",
        f"  python profit_growth_microsoft.py --end-date 2026-04-24 --freeze-as-of",
        "",
        f"• Fully optimistic view (bypass recency guard):",
        f"  python profit_growth_microsoft.py --ignore-recency-guard",
        "",
        f"• Combine for ideal-state modeling at a fixed date:",
        f"  python profit_growth_microsoft.py --end-date 2026-04-24 --freeze-as-of --ignore-recency-guard",
        "",
        f"• Revert C2 to 25% (v1 behavior):",
        f"  python profit_growth_microsoft.py --margin-floor-c2 0.25",
        "",
        f"• Forecasts use rank-IS-aware elasticity (base scenario × scale "
        f"based on rank-IS, capped at {ELASTICITY_EFFECTIVE_CAP:.1f}). "
        f"AGGRESSIVE scenario reflects upside if marginal conversions are "
        f"accretive. CONSERVATIVE matches v1.1 model. Realistic upside "
        f"likely between MID and AGGRESSIVE; the stair-step program is "
        f"designed to discover this empirically.",
    ]
    for line in override_lines:
        cell = ws.cell(row=r, column=1, value=line)
        if line.startswith(("v1.2", "v1.1", "A.", "B.", "C.", "D.",
                            "1.", "2.", "3.", "EXECUTION", "•")):
            cell.font = Font(bold=line.startswith(
                ("v1.2", "v1.1", "EXECUTION")))
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    # Column widths
    ws.column_dimensions["A"].width = 50
    for col in "BCDEFGHIJKLMN":
        ws.column_dimensions[col].width = 15
    ws.column_dimensions["O"].width = 18

    ws.freeze_panes = "B12"


def write_diagnostic(ws, diagnostics: dict) -> None:
    ws.cell(row=1, column=1, value="DIAGNOSTIC").font = Font(bold=True, size=14)
    r = 3
    for k, v in diagnostics.items():
        ws.cell(row=r, column=1, value=k)
        ws.cell(row=r, column=2, value=str(v))
        r += 1
    _autosize_columns(ws)


def write_elasticity_sensitivity(ws, rows_df: pd.DataFrame) -> None:
    """v1.2: Elasticity Sensitivity sheet — one row per (campaign × scenario).

    Shows how each campaign's destination + forecast changes under three
    elasticity assumptions. The reader can scan a campaign's three rows
    side-by-side to see how robust the destination is to the elasticity
    assumption.

    Column structure: campaign | scenario | base_elasticity |
    effective_elasticity_at_dest | destination_tcpa | forecast_conv_30d |
    forecast_profit_30d | profit_delta_vs_current | margin_protection_capped_at
    """
    ws.cell(row=1, column=1, value="ELASTICITY SENSITIVITY").font = Font(
        bold=True, size=14)
    ws.cell(row=2, column=1,
            value="One row per (campaign × elasticity scenario). Compare a "
                  "campaign's three rows to see how its destination responds "
                  "to elasticity assumption. Headline scenario in Profit "
                  f"Headroom Summary = {HEADROOM_HEADLINE_SCENARIO.upper()}.")
    r = 4
    headers = [
        "campaign", "scenario", "base_elasticity",
        "effective_elasticity_at_dest", "destination_tcpa",
        "forecast_conv_30d", "forecast_profit_30d",
        "profit_delta_vs_current", "margin_protection_capped_at",
        "current_30d_profit", "current_tcpa", "cohort",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
        cell.alignment = Alignment(horizontal="left", vertical="center",
                                   wrap_text=True)
    r += 1

    # Sort by aggressive upside descending (biggest opportunities at top),
    # then within campaign by elasticity ascending
    sorted_df = rows_df.copy()
    sorted_df["_upside_sort"] = sorted_df.apply(
        lambda x: ((x.get("forecast_profit_aggressive") or
                    x.get("profit_30d_current") or 0)
                   - (x.get("profit_30d_current") or 0)),
        axis=1,
    )
    sorted_df = sorted_df.sort_values("_upside_sort", ascending=False)

    scenario_fill = {
        "conservative": NEUTRAL_FILL,
        "mid":          COHORT2_FILL,
        "aggressive":   COHORT1_FILL,
    }

    for _, row in sorted_df.iterrows():
        sensitivity = row.get("elasticity_sensitivity", {}) or {}
        cur_tcpa = row.get("current_tcpa")
        cur_profit = row.get("profit_30d_current") or 0
        cohort = row.get("cohort")
        for scenario_name in ["conservative", "mid", "aggressive"]:
            s = sensitivity.get(scenario_name, {})
            base_eps = ELASTICITY_SCENARIOS[scenario_name]
            ws.cell(row=r, column=1, value=row.get("campaign_name"))
            ws.cell(row=r, column=2, value=scenario_name.upper())
            ws.cell(row=r, column=3, value=base_eps)
            eff_eps = s.get("effective_elasticity_at_destination")
            ws.cell(row=r, column=4,
                    value=f"{eff_eps:.2f}" if pd.notna(eff_eps) else "n/a")
            ws.cell(row=r, column=5, value=s.get("destination_tcpa"))
            f_conv = s.get("forecast_conv_30d")
            ws.cell(row=r, column=6,
                    value=round(f_conv) if pd.notna(f_conv) else None)
            ws.cell(row=r, column=7, value=s.get("forecast_profit_30d"))
            ws.cell(row=r, column=8, value=s.get("profit_delta_vs_current"))
            mpcap = s.get("margin_protection_capped_at")
            ws.cell(row=r, column=9,
                    value=f"${mpcap:.2f}" if pd.notna(mpcap) else "—")
            ws.cell(row=r, column=10, value=cur_profit)
            ws.cell(row=r, column=11, value=cur_tcpa)
            ws.cell(row=r, column=12, value=cohort)

            # Highlight the headline (aggressive) row
            ws.cell(row=r, column=2).fill = PatternFill(
                "solid", fgColor=scenario_fill.get(scenario_name, NEUTRAL_FILL))
            # Format currency cells
            for col_letter in ("E", "K"):
                cell = ws[f"{col_letter}{r}"]
                if isinstance(cell.value, (int, float)):
                    cell.number_format = '"$"#,##0.00'
            for col_letter in ("G", "H", "J"):
                cell = ws[f"{col_letter}{r}"]
                if isinstance(cell.value, (int, float)):
                    cell.number_format = '"$"#,##0'
            r += 1
        # Spacer row between campaigns
        r += 1

    # Column widths
    ws.column_dimensions["A"].width = 50
    for col in "BCDEFGHIJKL":
        ws.column_dimensions[col].width = 16
    ws.freeze_panes = "B5"


def write_campaign_health_flags(ws, health_df: pd.DataFrame) -> None:
    """v1.3 Campaign Health Flags sheet — six trend signals per campaign +
    advisory action label. Surfaces patterns visible in the per-campaign
    performance charts (RPL momentum, recent volatility, CPA breaches)
    that the strategic model alone doesn't currently capture.

    Sorted by health_action priority so the most-attention-needed
    campaigns appear first.
    """
    ws.cell(row=1, column=1, value="CAMPAIGN HEALTH FLAGS").font = Font(
        bold=True, size=14)
    ws.cell(row=2, column=1,
            value="Six trend signals per campaign + advisory action label. "
                  "Health action is ADVISORY — does not override cohort "
                  "assignment. Use to factor in campaign-specific dynamics "
                  "that the strategic model doesn't directly observe "
                  "(RPL momentum, recent CPA breaches, profit volatility, "
                  "trend acceleration/deceleration).")
    ws.cell(row=2, column=1).alignment = Alignment(wrap_text=True)
    r = 4

    # Action priority for sorting (most-attention-needed first)
    action_priority = {
        "MARGIN_RESTORATION_REVISIT":      1,
        "WATCHLIST_MARGIN_BREACH":         2,
        "WATCHLIST_DECLINING_RPL":         3,
        "INVESTIGATE_VOLUME_DECLINE":      4,
        "INVESTIGATE_VOLUME_SPIKE":        5,
        "RAISE_CANDIDATE_STRONG":          6,
        "RAISE_CANDIDATE_CAUTIOUS":        7,
        "STABLE":                          8,
    }
    df = health_df.copy()
    df["_priority"] = df["health_action"].map(action_priority).fillna(99)
    df = df.sort_values("_priority").drop(columns="_priority").reset_index(drop=True)

    # Color map for action labels
    action_fill_map = {
        "MARGIN_RESTORATION_REVISIT":      MARGIN_RESTORATION_FILL,
        "WATCHLIST_MARGIN_BREACH":         "FFC7CE",   # red
        "WATCHLIST_DECLINING_RPL":         "FFEB9C",   # yellow
        "INVESTIGATE_VOLUME_DECLINE":      "FFEB9C",   # yellow
        "INVESTIGATE_VOLUME_SPIKE":        "B7DEE8",   # cyan (informational)
        "RAISE_CANDIDATE_STRONG":          "C6EFCE",   # green
        "RAISE_CANDIDATE_CAUTIOUS":        COHORT2_FILL,
        "STABLE":                          NEUTRAL_FILL,
    }

    headers = [
        "campaign", "cohort", "health_action",
        "current_tcpa", "margin_ceiling", "profit_optimal_tcpa",
        "rpl_momentum", "rpl_slope_per_week", "rpl_r_squared",
        "profit_volatility", "profit_cv", "profit_mean_8w",
        "margin_breach_class", "margin_breaches_8w",
        "tcpa_overshoot_class", "tcpa_overshoots_8w",
        "profit_trend", "profit_trend_recent_4w", "profit_trend_prior_8w",
        "conv_trend", "conv_trend_recent_4w", "conv_trend_prior_8w",
        "rationale",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
        cell.alignment = Alignment(horizontal="left", vertical="center",
                                   wrap_text=True)
    r += 1

    for _, row in df.iterrows():
        ws.cell(row=r, column=1, value=row.get("campaign_name"))
        ws.cell(row=r, column=2, value=row.get("cohort"))
        action_cell = ws.cell(row=r, column=3, value=row.get("health_action"))
        action_cell.fill = PatternFill(
            "solid",
            fgColor=action_fill_map.get(row.get("health_action"), NEUTRAL_FILL))
        action_cell.font = Font(bold=True)
        ws.cell(row=r, column=4, value=row.get("current_tcpa"))
        ws.cell(row=r, column=5, value=row.get("margin_ceiling"))
        ws.cell(row=r, column=6, value=row.get("profit_optimal_tcpa"))
        ws.cell(row=r, column=7, value=row.get("rpl_momentum"))
        ws.cell(row=r, column=8, value=row.get("rpl_slope_per_week"))
        ws.cell(row=r, column=9, value=row.get("rpl_r_squared"))
        ws.cell(row=r, column=10, value=row.get("profit_volatility"))
        ws.cell(row=r, column=11, value=row.get("profit_cv"))
        ws.cell(row=r, column=12, value=row.get("profit_mean_8w"))
        ws.cell(row=r, column=13, value=row.get("margin_breach_class"))
        ws.cell(row=r, column=14, value=row.get("margin_breaches_8w"))
        ws.cell(row=r, column=15, value=row.get("tcpa_overshoot_class"))
        ws.cell(row=r, column=16, value=row.get("tcpa_overshoots_8w"))
        ws.cell(row=r, column=17, value=row.get("profit_trend"))
        ws.cell(row=r, column=18, value=row.get("profit_trend_recent_4w"))
        ws.cell(row=r, column=19, value=row.get("profit_trend_prior_8w"))
        ws.cell(row=r, column=20, value=row.get("conv_trend"))
        ws.cell(row=r, column=21, value=row.get("conv_trend_recent_4w"))
        ws.cell(row=r, column=22, value=row.get("conv_trend_prior_8w"))
        ws.cell(row=r, column=23, value=row.get("health_rationale"))

        # Currency formatting: tCPAs (D, E, F), profit means (L, R, S)
        for col_letter in ("D", "E", "F"):
            cell = ws[f"{col_letter}{r}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0.00'
        for col_letter in ("L", "R", "S"):
            cell = ws[f"{col_letter}{r}"]
            if isinstance(cell.value, (int, float)):
                cell.number_format = '"$"#,##0'
        r += 1

    # Column widths
    ws.column_dimensions["A"].width = 50
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 32
    for col in "DEFGHIJKLMNOPQRSTUV":
        ws.column_dimensions[col].width = 14
    ws.column_dimensions["W"].width = 70
    ws.freeze_panes = "B5"


def write_overshoot_diagnostic(ws, overshoot_diagnostics: dict[str, dict],
                               overshoot_destinations: dict[str, dict],
                               rows_df: pd.DataFrame) -> None:
    """Write the per-campaign MARGIN_RESTORATION diagnostic panel."""
    ws.cell(row=1, column=1, value="MARGIN_RESTORATION DIAGNOSTIC").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1,
            value="Campaigns where 30d CPA exceeds margin ceiling. Each panel "
                  "shows: destinations (conservative + optimistic), 5 diagnostic "
                  "checks, recommended descent strategy.")
    r = 4

    if not overshoot_diagnostics:
        ws.cell(row=r, column=1,
                value="No campaigns currently in MARGIN_RESTORATION cohort.")
        _autosize_columns(ws)
        return

    for cname, diag in overshoot_diagnostics.items():
        # Header
        ws.cell(row=r, column=1, value=cname).font = Font(bold=True, size=12)
        ws.cell(row=r, column=1).fill = PatternFill("solid",
                                                     fgColor=MARGIN_RESTORATION_FILL)
        r += 1

        # Pull row data for this campaign
        camp_row = rows_df[rows_df["campaign_name"] == cname]
        if not camp_row.empty:
            cr = camp_row.iloc[0]
            ws.cell(row=r, column=1, value="current_tcpa")
            ws.cell(row=r, column=2, value=f"${cr['current_tcpa']:.2f}")
            r += 1
            ws.cell(row=r, column=1, value="cpa_30d")
            ws.cell(row=r, column=2, value=f"${cr['cpa_30d']:.2f}")
            r += 1
            ws.cell(row=r, column=1, value="rpl_30d")
            ws.cell(row=r, column=2, value=f"${cr['rpl_30d']:.2f}")
            r += 1
            ws.cell(row=r, column=1, value="margin_ceiling")
            ws.cell(row=r, column=2, value=f"${cr['margin_ceiling']:.2f}")
            r += 1
            ws.cell(row=r, column=1, value="overshoot_ratio")
            ws.cell(row=r, column=2,
                    value=f"{cr['cpa_30d']/cr['current_tcpa']:.2f}x")
            r += 1
            ws.cell(row=r, column=1, value="conv_30d")
            ws.cell(row=r, column=2, value=int(cr['conv_30d']))
            r += 1

        # Destinations
        dest = overshoot_destinations.get(cname, {})
        if dest:
            r += 1
            ws.cell(row=r, column=1, value="DESTINATIONS").font = Font(bold=True)
            r += 1
            ws.cell(row=r, column=1, value="conservative_tcpa")
            ws.cell(row=r, column=2,
                    value=f"${dest.get('conservative_tcpa', 0):.2f}")
            r += 1
            ws.cell(row=r, column=1, value="conservative_rationale")
            ws.cell(row=r, column=2, value=dest.get("conservative_rationale", ""))
            ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True,
                                                           vertical="top")
            r += 1
            ws.cell(row=r, column=1, value="optimistic_tcpa")
            ws.cell(row=r, column=2,
                    value=f"${dest.get('optimistic_tcpa', 0):.2f}")
            r += 1
            ws.cell(row=r, column=1, value="optimistic_rationale")
            ws.cell(row=r, column=2, value=dest.get("optimistic_rationale", ""))
            ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True,
                                                           vertical="top")
            r += 1
            ws.cell(row=r, column=1, value="destination_above_current")
            ws.cell(row=r, column=2,
                    value=str(dest.get("destination_above_current", False)))
            r += 1

        # Diagnostic checks
        r += 1
        ws.cell(row=r, column=1, value="DIAGNOSTIC CHECKS").font = Font(bold=True)
        r += 1
        for check_name in ["network_split", "cpa_trend", "attribution_lag",
                           "tcpa_history", "overall"]:
            check = diag.get(check_name, {})
            ws.cell(row=r, column=1, value=check_name).font = Font(bold=True)
            r += 1
            for k, v in check.items():
                ws.cell(row=r, column=1, value=f"  {k}")
                if isinstance(v, list):
                    v = "; ".join(str(x) for x in v[:8])  # truncate long lists
                ws.cell(row=r, column=2, value=str(v))
                ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True,
                                                               vertical="top")
                r += 1
        r += 2  # spacer between campaigns

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 90


# ═══════════════════════════════════════════════════════════════════
# OPTIONAL: TACTICAL FRONTIER XLSX READER
# ═══════════════════════════════════════════════════════════════════

def load_frontier_summary(xlsx_path: Path) -> pd.DataFrame:
    """Read tactical Frontier Summary from xlsx. The sheet has a multi-row
       banner header — same format as the tactical script writes."""
    wb = load_workbook(xlsx_path, data_only=True)
    if "Frontier Summary" not in wb.sheetnames:
        return pd.DataFrame()
    raw = pd.read_excel(xlsx_path, "Frontier Summary", header=None)
    # Find header row (row that contains 'campaign_name')
    header_row = None
    for i in range(min(20, len(raw))):
        for v in raw.iloc[i]:
            if str(v).strip().lower() == "campaign_name":
                header_row = i
                break
        if header_row is not None:
            break
    if header_row is None:
        return pd.DataFrame()
    df = pd.read_excel(xlsx_path, "Frontier Summary", header=header_row)
    df = df[df["campaign_name"].notna()].reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════════════
# SMOKE TESTS
# ═══════════════════════════════════════════════════════════════════

def run_smoke_tests(rows: pd.DataFrame, plan_rows: list[dict],
                    trigger_rows: list[dict]) -> None:
    """Sanity checks before writing the workbook. Mirrors tactical
       smoke-test discipline: anything that shouldn't be possible by
       construction throws AssertionError."""
    banner("SMOKE TESTS")

    # 1. Every cohort assignment is a known label
    valid = {"MARGIN_RESTORATION", "COHORT_1", "COHORT_2", "COHORT_3",
             "WARMUP_BUDGET_ONLY", "EXCLUDED_RECALIBRATING",
             "EXCLUDED_TACTICAL_FIRST", "EXCLUDED_LOW_HEADROOM"}
    bad = set(rows["cohort"]) - valid
    assert not bad, f"Unknown cohort labels: {bad}"
    print(f"✓ All {len(rows)} campaigns assigned valid cohort labels")

    # 2. No proposed tCPA exceeds the margin ceiling (excluding MARGIN_
    # RESTORATION campaigns where the destination is by definition ≤ ceiling
    # but the descent-period plan rows may have target_tcpa = current_tcpa
    # which is fine)
    if "week1_target_tcpa" in rows.columns:
        violations = rows[
            rows["week1_target_tcpa"].notna()
            & rows["margin_ceiling"].notna()
            & (rows["cohort"] != "MARGIN_RESTORATION")
            & (rows["week1_target_tcpa"] > rows["margin_ceiling"] + 0.01)
        ]
        assert violations.empty, (
            f"Margin-ceiling violations:\n{violations[['campaign_name','week1_target_tcpa','margin_ceiling']]}"
        )
    print("✓ No proposed tCPA exceeds margin ceiling")

    # 3. Profit-optimal tCPA is ≥ current_tcpa for COHORT_1/2 entries
    # (MARGIN_RESTORATION is exempt — it's a descent, not a raise)
    raise_cohorts = rows[rows["cohort"].isin(["COHORT_1", "COHORT_2"])]
    for _, row in raise_cohorts.iterrows():
        if pd.notna(row.get("profit_optimal_tcpa")) and pd.notna(row.get("current_tcpa")):
            assert row["profit_optimal_tcpa"] >= row["current_tcpa"] - 0.01, (
                f"Cohort {row['cohort']} campaign {row['campaign_name']} has "
                f"profit_optimal {row['profit_optimal_tcpa']} < current {row['current_tcpa']}"
            )
    print(f"✓ All cohort 1/2 campaigns have profit_optimal_tcpa ≥ current_tcpa")

    # 4. Plan rows reference known campaigns
    plan_camps = {p["campaign_name"] for p in plan_rows if "campaign_name" in p}
    known = set(rows["campaign_name"])
    assert plan_camps.issubset(known), (
        f"Plan references unknown campaigns: {plan_camps - known}"
    )
    print(f"✓ All plan rows reference known campaigns ({len(plan_rows)} rows)")

    # 5. Tactical TIGHTEN campaigns are excluded from cohort 1
    if "tactical_recommendation" in rows.columns:
        tighten_in_c1 = rows[
            (rows["tactical_recommendation"] == "TIGHTEN")
            & (rows["cohort"] == "COHORT_1")
        ]
        assert tighten_in_c1.empty, (
            f"Tactical TIGHTEN campaigns assigned to COHORT_1: "
            f"{tighten_in_c1['campaign_name'].tolist()}"
        )
    print("✓ No tactical TIGHTEN campaigns in COHORT_1")

    # 6. Trigger rows have valid severity
    valid_severity = {"HARD", "SOFT"}
    for tr in trigger_rows:
        assert tr.get("severity") in valid_severity, f"Bad severity: {tr}"
    print(f"✓ All {len(trigger_rows)} trigger rows have valid severity")

    print("✓ All smoke tests passed.")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    change_history_csv = (Path(args.change_history) if args.change_history
                          else CHANGE_HISTORY_CSV)
    frontier_xlsx = Path(args.frontier_xlsx) if args.frontier_xlsx else None

    # v1.1: resolve C2 floor (CLI override or default)
    c2_floor = (args.margin_floor_c2 if args.margin_floor_c2 is not None
                else MARGIN_C2_PCT_FLOOR)
    c2_floor_source = ("CLI override" if args.margin_floor_c2 is not None
                       else "default (v1.1)")

    banner("STRATEGIC PROFIT-GROWTH MODEL — Microsoft Home Insurance")
    print(f"  DB:                {db_path}")
    print(f"  Account ID:        {ACCOUNT_ID} ({ACCOUNT_LABEL})")
    print(f"  Change history:    {change_history_csv}")
    print(f"  Tactical xlsx:     {frontier_xlsx if frontier_xlsx else '(not provided)'}")
    print(f"  Margin policy:     min(RPL−${MARGIN_C1_DOLLAR_FLOOR:.0f}, "
          f"RPL×{1-c2_floor:.0%}); blended ≥ {MARGIN_C3_BLENDED_MIN:.0%}")
    print(f"  C2 floor:          {c2_floor:.0%} ({c2_floor_source})")
    print(f"  Current-margin:    abs floor {CURRENT_MARGIN_FLOOR_ABSOLUTE:.0%}, "
          f"relative {CURRENT_MARGIN_FLOOR_RELATIVE:.0%} of current")
    print(f"  Elasticity (v1):   ε = {DEFAULT_ELASTICITY:.2f} (pooled default)")
    print(f"  Program horizon:   {PROGRAM_WEEKS} weeks")
    print(f"  Freeze-as-of:      {args.freeze_as_of}")
    print(f"  Ignore recency:    {args.ignore_recency_guard}")
    print(f"  Dry run:           {args.dry_run}")
    if args.ignore_recency_guard:
        print(f"  ⚠  --ignore-recency-guard ACTIVE: bypassing 14d recent-change "
              f"guard for ALL campaigns. Output represents 'fully optimistic' view.")

    conn = get_db_connection(db_path)

    # ── Resolve dates
    raw_max, end_date = resolve_end_date(conn, args.end_date)
    program_start = end_date + timedelta(days=7)  # nominal — first Monday after run
    print(f"\n  Stable end date:   {end_date} (raw_max={raw_max})")
    print(f"  Program start:     {program_start} (nominal)")

    # ── Load metadata
    camp_meta = query_campaigns_metadata(conn)
    enabled_ids = query_enabled_campaign_ids(conn)
    print(f"  Enabled campaigns: {len(enabled_ids)}")

    # ── Load daily frames (90d for full coverage of all windows)
    start_90d = end_date - timedelta(days=90)
    daily_30d = query_campaign_daily(conn, enabled_ids,
                                     end_date - timedelta(days=29), end_date)
    daily_full = query_campaign_daily(conn, enabled_ids, start_90d, end_date)
    aud_full = query_audience_network_daily(conn, enabled_ids, start_90d, end_date)

    # ── Change history
    change_df = pd.DataFrame()
    if change_history_csv.exists():
        change_df = load_change_history(change_history_csv)
        n_loaded = len(change_df)
        if args.freeze_as_of and not change_df.empty:
            # Keep only changes on or before end_date — for reproducible
            # analysis against a past state.
            mask = change_df["timestamp"].dt.date <= end_date
            change_df = change_df[mask].reset_index(drop=True)
            n_filtered_out = n_loaded - len(change_df)
            print(f"  Change history:    {len(change_df)} rows "
                  f"(--freeze-as-of {end_date}: filtered out {n_filtered_out})")
        else:
            print(f"  Change history:    {len(change_df)} tracked rows YTD")

    # ── Optional tactical frontier ingest
    frontier_df = pd.DataFrame()
    if frontier_xlsx and frontier_xlsx.exists():
        frontier_df = load_frontier_summary(frontier_xlsx)
        print(f"  Tactical frontier: {len(frontier_df)} campaign rows")

    # ── 30d aggregate per campaign
    agg_30d = aggregate_window(daily_full, aud_full,
                               end_date - timedelta(days=29), end_date)
    # 14d aggregate (for current-state CPA)
    agg_14d = aggregate_window(daily_full, aud_full,
                               end_date - timedelta(days=13), end_date)
    # 7d and prior 7d for trigger evaluation
    agg_7d = aggregate_window(daily_full, aud_full,
                              end_date - timedelta(days=6), end_date)
    agg_prior_7d = aggregate_window(daily_full, aud_full,
                                    end_date - timedelta(days=13),
                                    end_date - timedelta(days=7))

    # ── Build per-campaign master frame
    rows = []
    plan_rows: list[dict] = []
    trigger_rows: list[dict] = []
    diagnostics: dict = {
        "run_date": date.today().isoformat(),
        "end_date": end_date.isoformat(),
        "elasticity_v1": DEFAULT_ELASTICITY,
        "program_horizon_weeks": PROGRAM_WEEKS,
        "campaigns_processed": 0,
        "campaigns_excluded": 0,
        "rpl_drift_min_weeks_threshold": RPL_DRIFT_MIN_WEEKS,
        "c2_floor_used": f"{c2_floor:.0%} ({c2_floor_source})",
        "current_margin_floor_absolute": f"{CURRENT_MARGIN_FLOOR_ABSOLUTE:.0%}",
        "current_margin_floor_relative": f"{CURRENT_MARGIN_FLOOR_RELATIVE:.0%}",
        "cohort_gate_roas_14d": f"{COHORT_GATE_HEADROOM_ROAS_14D:.0%}",
        "freeze_as_of_active": args.freeze_as_of,
        "ignore_recency_guard_active": args.ignore_recency_guard,
        "rank_is_in_cohort_gate": "REMOVED v1.2 (still in elasticity)",
    }

    for _, meta in camp_meta[camp_meta["status"] == "ENABLED"].iterrows():
        cid = str(meta["campaign_id"])
        cname = meta["campaign_name"]

        # Pull aggregates
        a30 = agg_30d[agg_30d["campaign_id"].astype(str) == cid]
        a14 = agg_14d[agg_14d["campaign_id"].astype(str) == cid]
        a7  = agg_7d[agg_7d["campaign_id"].astype(str) == cid]
        a7p = agg_prior_7d[agg_prior_7d["campaign_id"].astype(str) == cid]

        if a30.empty:
            continue
        a30 = a30.iloc[0]

        current_tcpa = (meta["target_cpa_micros"] / 1_000_000.0
                        if pd.notna(meta["target_cpa_micros"])
                        and meta["target_cpa_micros"] > 0 else None)
        rpl_30d = a30["rpl"]
        cpa_30d = a30["cpa"]
        spend_30d = a30["spend_usd"]
        conv_30d = a30["conv_count"]

        # v1.1: 14d signals (ROAS path for cohort gating, CPA for
        # MARGIN_RESTORATION reclassification)
        if not a14.empty:
            a14_first = a14.iloc[0]
            rpl_14d = a14_first.get("rpl")
            cpa_14d = a14_first.get("cpa")
            roas_14d = a14_first.get("roas")
            conv_14d = a14_first.get("conv_count", 0)
            spend_14d = a14_first.get("spend_usd", 0)
        else:
            rpl_14d = cpa_14d = roas_14d = None
            conv_14d = spend_14d = 0
        # Current margin from 14d ROAS
        current_margin_14d = current_margin_from_roas(roas_14d)

        # Margin ceiling
        margin_ceiling, binding_constraint = compute_margin_ceiling(
            rpl_30d, c2_floor_override=c2_floor)

        # Daily CPA series (for consec-days-at-target + volatility)
        daily_cpa = compute_daily_cpa_series(daily_full, aud_full, cid,
                                             start_90d, end_date)
        consec = consecutive_days_at_target(daily_cpa, current_tcpa, end_date)

        # Days since last change (any kind, and target specifically)
        d_change = days_since_last_change(change_df, cname, end_date)
        d_tcpa = days_since_last_tcpa_change(change_df, cname, end_date)

        # Tactical recommendation (from frontier_df if available, else None)
        tactical_rec = None
        if not frontier_df.empty:
            row_t = frontier_df[frontier_df["campaign_name"] == cname]
            if not row_t.empty:
                tactical_rec = row_t.iloc[0].get("recommendation")

        # RPL distribution
        rpl_dist = compute_rpl_distribution(daily_full, aud_full, cid, end_date)
        drifting, drift_rationale = rpl_drift_signal(rpl_dist)

        # Profit-optimal tCPA (with current-margin protection)
        if (current_tcpa is not None and rpl_30d is not None
                and conv_30d > 0 and margin_ceiling is not None):
            opt = derive_profit_optimal_tcpa(
                current_tcpa=current_tcpa,
                current_spend_30d=spend_30d,
                current_conv_30d=conv_30d,
                current_rpl_30d=rpl_30d,
                margin_ceiling=margin_ceiling,
                rank_lost_is=a30.get("rank_lost_is"),
                current_roas_14d=roas_14d,
            )
            profit_optimal_tcpa = opt.get("profit_optimal_tcpa")
            profit_optimal_protection_capped = opt.get("margin_protection_binding", False)
            profit_optimal_protection_capped_at = opt.get("margin_protection_capped_at")
        else:
            profit_optimal_tcpa = None
            profit_optimal_protection_capped = False
            profit_optimal_protection_capped_at = None

        # v1.2: Elasticity sensitivity scenarios (3 scenarios per campaign)
        sensitivity = compute_elasticity_sensitivity(
            current_tcpa=current_tcpa,
            current_spend_30d=spend_30d,
            current_conv_30d=conv_30d,
            current_rpl_30d=rpl_30d,
            margin_ceiling=margin_ceiling,
            rank_lost_is=a30.get("rank_lost_is"),
            current_roas_14d=roas_14d,
        )

        # Forecast at +15% step
        f15 = None
        if (current_tcpa is not None and rpl_30d is not None
                and conv_30d > 0 and margin_ceiling is not None):
            target_15 = min(current_tcpa * (1 + STEP_INITIAL_PCT), margin_ceiling)
            f15 = forecast_tcpa_move(current_tcpa, spend_30d, conv_30d, rpl_30d,
                                     target_15,
                                     rank_lost_is=a30.get("rank_lost_is"))
        # Forecast at profit-optimal
        fopt = None
        if profit_optimal_tcpa and profit_optimal_tcpa > current_tcpa + 0.01:
            fopt = forecast_tcpa_move(current_tcpa, spend_30d, conv_30d, rpl_30d,
                                      profit_optimal_tcpa,
                                      rank_lost_is=a30.get("rank_lost_is"))

        # Cohort assignment
        cohort, cohort_reasons = assign_cohort(
            campaign_name=cname,
            current_tcpa=current_tcpa,
            cpa_30d=cpa_30d,
            conv_30d=conv_30d,
            consec_days_at_target=consec,
            days_since_change=d_change,
            days_since_tcpa_change=d_tcpa,
            budget_lost_is=a30.get("budget_lost_is"),
            rank_lost_is=a30.get("rank_lost_is"),
            tactical_recommendation=tactical_rec,
            margin_ceiling=margin_ceiling,
            profit_optimal_tcpa=profit_optimal_tcpa,
            overrides=COHORT_OVERRIDES,
            recency_guard_overrides=RECENCY_GUARD_OVERRIDES,
            cpa_14d=cpa_14d,
            roas_14d=roas_14d,
            ignore_recency_guard_global=args.ignore_recency_guard,
        )

        # Stair-step plan (deferred for MARGIN_RESTORATION — needs account
        # ratio which requires the full first-pass loop to complete)
        if cohort == "MARGIN_RESTORATION":
            camp_plan = []  # filled in second pass below
        else:
            camp_plan = generate_stairstep_plan(
                campaign_name=cname, cohort=cohort,
                current_tcpa=current_tcpa,
                profit_optimal_tcpa=profit_optimal_tcpa,
                margin_ceiling=margin_ceiling,
                program_start=program_start,
                program_weeks=PROGRAM_WEEKS,
            )
        for entry in camp_plan:
            entry["campaign_name"] = cname
            entry["cohort"] = cohort
            plan_rows.append(entry)

        # Week-1 action extraction (for the strategic summary)
        week1 = next((p for p in camp_plan if p.get("week") == 1
                      or (cohort == "COHORT_2" and p.get("week") == 4)),
                     None)
        week1_action = week1.get("action") if week1 else "NO_ACTION"
        week1_target = week1.get("target_tcpa") if week1 else None

        # Triggers
        spend_7d_val = a7.iloc[0]["spend_usd"] if not a7.empty else 0.0
        spend_p7d_val = a7p.iloc[0]["spend_usd"] if not a7p.empty else 0.0
        conv_7d_val = a7.iloc[0]["conv_count"] if not a7.empty else 0.0
        conv_p7d_val = a7p.iloc[0]["conv_count"] if not a7p.empty else 0.0
        cpa_7d_val = a7.iloc[0]["cpa"] if not a7.empty else None
        cpa_14d_val = a14.iloc[0]["cpa"] if not a14.empty else None

        trig = evaluate_triggers(
            campaign_id=cid, campaign_name=cname, current_tcpa=current_tcpa,
            cpa_7d=cpa_7d_val, cpa_14d=cpa_14d_val,
            spend_7d=spend_7d_val, spend_prior_7d=spend_p7d_val,
            conv_7d=conv_7d_val, conv_prior_7d=conv_p7d_val,
            rpl_dist=rpl_dist, tactical_recommendation=tactical_rec,
        )
        for trigger_name, t in trig.items():
            severity = "HARD" if trigger_name.startswith("HARD") else "SOFT"
            rec_action = ""
            if t["fired"] and severity == "HARD":
                rec_action = ("Queue rollback: 5pp tCPA decrement per day until "
                              "previous tCPA reached. See Strategic Summary.")
            elif t["fired"] and severity == "SOFT":
                rec_action = "Watch only — alert leadership, no auto-pause"
            trigger_rows.append({
                "campaign_name": cname, "trigger": trigger_name,
                "fired": t["fired"], "severity": severity,
                "rationale": t["rationale"], "rec_action": rec_action,
            })

        # Profit baseline
        profit_30d_current = (rpl_30d - cpa_30d) * conv_30d if (
            rpl_30d and cpa_30d and conv_30d
        ) else None

        rows.append({
            "campaign_name": cname,
            "campaign_id": cid,
            "current_tcpa": current_tcpa,
            "rpl_30d": rpl_30d,
            "rpl_14d": rpl_14d,
            "cpa_14d": cpa_14d,
            "roas_14d": roas_14d,
            "current_margin_14d": current_margin_14d,
            "conv_14d": conv_14d,
            "spend_14d": spend_14d,
            "margin_ceiling": margin_ceiling,
            "binding_constraint": binding_constraint,
            "tactical_rec": tactical_rec,
            "tactical_recommendation": tactical_rec,
            "cpa_30d": cpa_30d,
            "consec_days_at_target": consec,
            "conv_30d": conv_30d,
            "spend_30d": spend_30d,
            "profit_30d_current": profit_30d_current,
            "days_since_last_change": d_change,
            "days_since_last_tcpa_change": d_tcpa,
            "budget_lost_is": a30.get("budget_lost_is"),
            "rank_lost_is": a30.get("rank_lost_is"),
            "audience_share": a30.get("audience_share"),
            "profit_optimal_tcpa": profit_optimal_tcpa,
            "profit_optimal_protection_capped": profit_optimal_protection_capped,
            "profit_optimal_protection_capped_at": profit_optimal_protection_capped_at,
            # v1.2: elasticity sensitivity (3 scenarios)
            "elasticity_sensitivity": sensitivity,
            "destination_conservative": sensitivity.get("conservative", {}).get("destination_tcpa"),
            "destination_mid": sensitivity.get("mid", {}).get("destination_tcpa"),
            "destination_aggressive": sensitivity.get("aggressive", {}).get("destination_tcpa"),
            "forecast_profit_aggressive": sensitivity.get("aggressive", {}).get("forecast_profit_30d"),
            "forecast_profit_mid": sensitivity.get("mid", {}).get("forecast_profit_30d"),
            "forecast_profit_conservative": sensitivity.get("conservative", {}).get("forecast_profit_30d"),
            "profit_delta_aggressive": sensitivity.get("aggressive", {}).get("profit_delta_vs_current"),
            "forecast_profit_30d_at_optimal": (fopt.get("forecast_profit")
                                               if fopt else None),
            "profit_delta_at_optimal": (fopt.get("profit_delta_vs_current")
                                        if fopt else None),
            "step_+15pct_tcpa": (f15.get("target_tcpa") if f15 else None),
            "forecast_profit_at_+15pct": (f15.get("forecast_profit") if f15 else None),
            "forecast_conv_at_+15pct": (f15.get("forecast_conv") if f15 else None),
            "step_+15pct_low_band": (f15.get("forecast_profit_low") if f15 else None),
            "step_+15pct_high_band": (f15.get("forecast_profit_high") if f15 else None),
            "forecast_profit_delta_30d_initial_step": (f15.get("profit_delta_vs_current")
                                                      if f15 else None),
            "forecast_revenue": (f15.get("forecast_revenue") if f15 else
                                 (rpl_30d * conv_30d if rpl_30d and conv_30d else 0)),
            "forecast_spend": (f15.get("forecast_spend") if f15 else (spend_30d or 0)),
            "rpl_12w_mean": rpl_dist.get("mean"),
            "rpl_12w_stdev": rpl_dist.get("stdev"),
            "rpl_drift_state": "DRIFT" if drifting else "OK",
            "cohort": cohort,
            "cohort_rationale": "; ".join(cohort_reasons),
            "week1_action": week1_action,
            "week1_target_tcpa": week1_target,
        })

    rows_df = pd.DataFrame(rows)
    diagnostics["campaigns_processed"] = len(rows_df)
    diagnostics["campaigns_excluded"] = (rows_df["cohort"]
                                         .str.startswith("EXCLUDED").sum())

    # ── Second pass: MARGIN_RESTORATION ───────────────────────────
    # For overshooting campaigns, derive (a) the account-typical CPA-to-tCPA
    # ratio from non-overshooters, (b) conservative + optimistic destinations,
    # (c) the deferred stair-step plan, (d) the diagnostic panel.
    overshoot_diagnostics: dict[str, dict] = {}
    overshoot_destinations: dict[str, dict] = {}

    account_ratio = compute_account_typical_cpa_to_tcpa_ratio(rows_df)
    diagnostics["account_typical_cpa_to_tcpa_ratio"] = (
        f"{account_ratio:.3f}" if account_ratio else "n/a"
    )

    margin_rest_campaigns = rows_df[rows_df["cohort"] == "MARGIN_RESTORATION"]
    for _, row in margin_rest_campaigns.iterrows():
        cname = row["campaign_name"]
        cid = row["campaign_id"]
        # Destinations
        dest = derive_margin_restoration_destinations(
            current_tcpa=row["current_tcpa"],
            current_cpa_30d=row["cpa_30d"],
            rpl_30d=row["rpl_30d"],
            margin_ceiling=row["margin_ceiling"],
            account_typical_cpa_to_tcpa_ratio=account_ratio,
            rank_lost_is=row.get("rank_lost_is"),
            conv_30d=row["conv_30d"],
            spend_30d=row["spend_30d"],
        )
        overshoot_destinations[cname] = dest

        # Diagnostic panel
        daily_cpa = compute_daily_cpa_series(daily_full, aud_full, cid,
                                             start_90d, end_date)
        # Add 14d/7d CPA from agg frames
        a14_row = agg_14d[agg_14d["campaign_id"].astype(str) == cid]
        a7_row = agg_7d[agg_7d["campaign_id"].astype(str) == cid]
        cpa_14d_val = a14_row.iloc[0]["cpa"] if not a14_row.empty else None
        cpa_7d_val = a7_row.iloc[0]["cpa"] if not a7_row.empty else None
        diag = diagnose_overshoot(
            campaign_id=cid, campaign_name=cname,
            current_tcpa=row["current_tcpa"], cpa_30d=row["cpa_30d"],
            cpa_14d=cpa_14d_val, cpa_7d=cpa_7d_val,
            audience_share_30d=row.get("audience_share"),
            daily_cpa_df=daily_cpa, end_date=end_date, change_df=change_df,
        )
        overshoot_diagnostics[cname] = diag

        # Generate the deferred stair-step plan with destinations
        camp_plan = generate_stairstep_plan(
            campaign_name=cname, cohort="MARGIN_RESTORATION",
            current_tcpa=row["current_tcpa"],
            profit_optimal_tcpa=row.get("profit_optimal_tcpa"),
            margin_ceiling=row["margin_ceiling"],
            program_start=program_start,
            program_weeks=PROGRAM_WEEKS,
            cpa_30d=row["cpa_30d"],
            destination_conservative=dest.get("conservative_tcpa"),
            destination_optimistic=dest.get("optimistic_tcpa"),
        )
        for entry in camp_plan:
            entry["campaign_name"] = cname
            entry["cohort"] = "MARGIN_RESTORATION"
            plan_rows.append(entry)

    # Add priority ranks
    if not rows_df.empty:
        rows_df["priority_rank"] = rows_df.apply(
            lambda r: cohort_priority_rank(
                r["cohort"], r.get("forecast_profit_delta_30d_initial_step"),
                r["spend_30d"]
            ),
            axis=1,
        )
        rows_df = rows_df.sort_values("priority_rank").reset_index(drop=True)

    # Blended margin forecast
    blended = aggregate_blended_margin(rows_df)
    diagnostics["forecasted_blended_margin"] = (f"{blended:.1%}"
                                                if blended else "n/a")

    # Smoke tests
    run_smoke_tests(rows_df, plan_rows, trigger_rows)

    # Diagnostic print
    banner("PER-CAMPAIGN COHORT ASSIGNMENT")
    cols_show = ["campaign_name", "cohort", "current_tcpa", "cpa_30d",
                 "rpl_30d", "margin_ceiling", "profit_optimal_tcpa",
                 "consec_days_at_target", "tactical_rec"]
    available = [c for c in cols_show if c in rows_df.columns]
    print(rows_df[available].to_string(index=False))

    if args.dry_run:
        banner("DRY RUN — skipping xlsx write")
        return

    # Write xlsx
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"profit_growth_{args.account}_{end_date}.xlsx"
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Profit Headroom Summary")
    write_profit_headroom_summary(
        ws, rows_df, blended, c2_floor_used=c2_floor,
        c2_floor_default=MARGIN_C2_PCT_FLOOR, end_date=end_date,
    )

    ws = wb.create_sheet("Elasticity Sensitivity")
    write_elasticity_sensitivity(ws, rows_df)

    # v1.3: Campaign Health Flags — pattern-based diagnostic that
    # surfaces RPL momentum, recent CPA breaches, profit volatility,
    # and trend acceleration/deceleration that the strategic model
    # alone doesn't directly observe. Advisory only.
    print()
    print("Computing campaign health flags...")
    health_df = compute_campaign_health_flags(
        daily_full=daily_full,
        aud_full=aud_full,
        end_date=end_date,
        rows_df=rows_df,
    )
    # Brief console summary
    action_counts = health_df["health_action"].value_counts()
    for action, n in action_counts.items():
        print(f"  {action}: {n}")
    ws = wb.create_sheet("Campaign Health Flags")
    write_campaign_health_flags(ws, health_df)

    ws = wb.create_sheet("Strategic Summary")
    write_strategic_summary(ws, date.today(), end_date, rows_df, blended,
                            program_start)

    ws = wb.create_sheet("Profit-Optimal Targets")
    write_profit_optimal_targets(ws, rows_df)

    ws = wb.create_sheet("Cohort Plan")
    write_cohort_plan(ws, plan_rows)

    ws = wb.create_sheet("Trigger Log")
    write_trigger_log(ws, trigger_rows)

    ws = wb.create_sheet("Measurement")
    write_measurement(ws, rows_df, end_date, blended)

    if overshoot_diagnostics:
        ws = wb.create_sheet("Margin Restoration")
        write_overshoot_diagnostic(ws, overshoot_diagnostics,
                                   overshoot_destinations, rows_df)

    ws = wb.create_sheet("Diagnostic")
    write_diagnostic(ws, diagnostics)

    wb.save(out_path)
    print(f"\n  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
