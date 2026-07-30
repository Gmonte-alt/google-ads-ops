"""
Elasticity Regression — Google Home Insurance
═══════════════════════════════════════════════════════════════════════
Empirically estimates the bid-response elasticity for the Google Home
Insurance account by treating each historical tROAS change as a natural
experiment. Companion to the Microsoft elasticity script — same
methodology, ported to the Google tROAS strategy.

For each tROAS change event in the change-history CSV:
  - Pre-window: 14 days BEFORE the change
  - Post-window: 14 days AFTER the change, skipping a 3-day stabilization lag
  - Compute % loosening of bid (-(% Δ tROAS), see Sign Convention below)
  - Compute % change in conversions (per day, normalized for window length)
  - Compute empirical elasticity = (% Δ conv) / (% loosening)

Then fits a weighted regression across all events to produce an
account-level empirical elasticity estimate.

──────────────────────────────────────────────────────────────────────
SIGN CONVENTION (important — differs from raw mathematical reading)
──────────────────────────────────────────────────────────────────────
On Microsoft tCPA, raising the target loosens the bid (algorithm pays
more per conversion → volume up). Empirical elasticity is naturally
positive.

On Google tROAS, the lever is INVERTED: raising tROAS tightens the bid
(demand more revenue per dollar → volume down). Lowering tROAS loosens.

To keep elasticity comparable to the Microsoft strategic profit-growth
model — and to the 0.6/1.0/1.2 scenario benchmarks — we define:

    pct_loosening      = -(new_target - old_target) / old_target
    empirical_elasticity = (% Δ conv per day) / pct_loosening

That is: a tROAS CUT (loosening) of 10% with ε=0.6 forecasts a 6% conv
lift — same operational shape as the Microsoft model. A well-behaved
campaign produces a POSITIVE elasticity. The "RAISE" / "CUT" labels in
this script refer to bid LOOSENING vs. TIGHTENING (i.e., the operational
direction), not the raw direction of the tROAS number.

──────────────────────────────────────────────────────────────────────
USAGE:
  python elasticity_regression_google.py
  python elasticity_regression_google.py --db-path C:\\path\\to\\paid_search.db
  python elasticity_regression_google.py --pre-days 7 --post-days 14
  python elasticity_regression_google.py --exclude-pre 2026-02-15

The --exclude-pre flag excludes change events whose post-window starts
before that date. Off by default; use it if you have an attribution
go-live or restructure period to gate out.

OUTPUT: elasticity_evidence_google_<YYYY-MM-DD>.xlsx with sheets:
  - Headline: top-line empirical elasticity vs. model scenarios
  - Per-Event Detail: every change event with pre/post metrics + elasticity
  - Chart_<campaign>_<MMDD>: 3-panel time-series per non-excluded event
  - Overview_<campaign>: full-history per-campaign chart per enabled campaign
  - Diagnostics: events excluded and why
"""

import argparse
import sqlite3
import io
import re
import csv
from datetime import date, timedelta, datetime
from pathlib import Path
import sys

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # non-interactive backend; no display required
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.drawing.image import Image as XLImage

# ── CONFIG ─────────────────────────────────────────────────────────
ACCOUNT_ID = 1  # Google Home Insurance
ACCOUNT_LABEL = "google_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
# Google Ads change-history CSV (same export the efficiency_frontier
# script uses for target ROAS changes — the "Change_history_report.csv"
# default in that script). We use the targets CSV for the change events
# and also load the budgets CSV so we can flag confounding budget changes.
DEFAULT_TARGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report.csv")
DEFAULT_BUDGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report__1_.csv")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

# 7 enabled NB-SEARCH-ALL-USA-STE campaigns — the same set efficiency_frontier operates on.
# Listed by campaign_id so we don't drift if names get re-tagged.
ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE
    "23394808297",   # HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE
    "23394808300",   # HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE
    "23428389081",   # HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
    "23496334752",   # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
    "23530860942",   # HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE
    "23586637678",   # HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE
]

# Pre-/exclude cutoff (off by default).
# Set via --exclude-pre on the CLI; events whose post-window starts before
# this date get excluded with a clear reason. Useful if you have an
# attribution go-live or restructure period to gate out.
DEFAULT_EXCLUDE_PRE = None

# Pre/post windows
DEFAULT_PRE_DAYS = 14    # days before change to measure baseline
DEFAULT_POST_DAYS = 14   # days after change to measure response
STABILIZATION_LAG = 3    # skip first N days post-change (algorithm settling)

# Backfill buffer for the default --end-date. Google Ads offline-routed primary
# conversions can lag the click date by 1-2 days; running with end_date = today
# would partially evaluate any event whose 14-day post-window includes today.
# Default of 1 (yesterday) matches the visualization script.
DEFAULT_BACKFILL_BUFFER_DAYS = 1

# Visualization windows (wider than measurement windows for context)
VIZ_PRE_DAYS = 45
VIZ_POST_DAYS = 30

# Confound thresholds
RPL_DRIFT_FLAG_THRESHOLD = 0.15   # >15% RPL drift between pre/post = flag
MIN_CONV_FOR_RELIABLE = 5         # below this in either window = exclude

# Confounder window — borrowed from efficiency_frontier's build_change_attribution.
# An event is "confounded" if there's another target OR budget change on the
# SAME campaign within ±CONFOUNDER_WINDOW_DAYS. Confounded events still get
# included in the regression but are flagged "low confidence" in the output;
# their weight is total conversions, so noisy events naturally count less.
CONFOUNDER_WINDOW_DAYS = 7

# 2026 cutoff — only year-to-date events
YEAR_START = date(2026, 1, 1)

# Model scenarios from strategic profit-growth script (for comparison in Headline sheet).
# These are the elasticity assumptions that feed forecast_tcpa_move:
# pct_conv = elasticity × pct_loosening. Same shape, both platforms.
MODEL_SCENARIOS = {
    "conservative": 0.6,
    "mid": 1.0,
    "aggressive": 1.2,
}

# Visual styling (matches Microsoft script + efficiency_frontier purple)
HEADER_FILL = "7030A0"   # purple — distinguishes Google output from Microsoft navy
HEADER_FONT = "FFFFFF"
GOOD_FILL   = "C6EFCE"
NEUTRAL_FILL = "F2F2F2"
WARN_FILL   = "FFEB9C"
BAD_FILL    = "FFC7CE"


def banner(text: str) -> None:
    print("═" * 70)
    print(text)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# CHANGE-HISTORY LOADING (Google CSV format)
# ═══════════════════════════════════════════════════════════════════

def _parse_gads_datetime(s: str) -> datetime:
    """Google Ads CSV exports: 'Mar 30, 2026, 9:31:59 AM'."""
    return datetime.strptime(s.strip(), "%b %d, %Y, %I:%M:%S %p")


def _normalize_target_value(raw) -> float | None:
    """Google Ads exports targets inconsistently: sometimes '116%', sometimes
    '1.16'. Normalize both to decimal ROAS multiplier (116% → 1.16).
    Mirrors the helper in efficiency_frontierV2 — the ASSUMPTION is that
    no campaign targets exceed 1000% (i.e. 10.0 as a decimal), so any
    bare numeric value > 10 is treated as a percentage missing its symbol.
    Returns None if unparseable.
    """
    if raw is None:
        return None
    s = str(raw).strip().rstrip(",").rstrip('"').strip()
    if not s:
        return None
    if s.endswith("%"):
        try:
            return float(s.rstrip("%")) / 100.0
        except ValueError:
            return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v > 10:
        return v / 100.0
    return v


def load_target_changes(csv_path: Path,
                        enabled_campaign_names: set[str]) -> pd.DataFrame:
    """Load the Google Ads change-history CSV, filter to Target ROAS
    CAMPAIGN-level changes on tracked campaigns. Mirrors the parser in
    efficiency_frontierV2.load_target_changes but returns the columns
    this script needs.

    Returns DataFrame with columns:
      change_date (date), timestamp (datetime), campaign_name,
      old_target (float|None), new_target (float)
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Target-change CSV not found: {csv_path}")

    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    # Skip the two header meta rows ("Change history report", date range)
    reader = csv.DictReader(lines[2:] if len(lines) > 2 else lines)

    rows = []
    for r in reader:
        ch = r.get("Changes", "") or ""
        if "Target ROAS" not in ch:
            continue
        try:
            ts = _parse_gads_datetime(r["Date & time"])
        except (ValueError, KeyError):
            continue
        if ts.date() < YEAR_START:
            continue
        # Campaign-level only — Liberty AG-level changes are excluded
        # (Liberty is paused on the 7 enabled campaigns).
        scope = "AG" if (r.get("Ad group") or "").strip() else "CAMPAIGN"
        if scope != "CAMPAIGN":
            continue
        cname = r.get("Campaign", "") or ""
        if cname not in enabled_campaign_names:
            continue

        # Parse "changed from X to Y" OR "changed to Y" (initial set)
        m = re.search(r"changed from\s+(\S+?)\s+to\s+(\S+)", ch)
        if m:
            old = _normalize_target_value(m.group(1))
            new = _normalize_target_value(m.group(2))
        else:
            m2 = re.search(r"Target ROAS (?:is|was)\s+(\S+)", ch)
            if m2:
                old, new = None, _normalize_target_value(m2.group(1))
            else:
                old, new = None, None

        if new is None:
            continue
        rows.append({
            "change_date": ts.date(),
            "timestamp": ts,
            "campaign_name": cname,
            "old_target": old,
            "new_target": new,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Sort and backfill missing old_target from the prior change for the same campaign
    df = df.sort_values(["campaign_name", "timestamp"]).reset_index(drop=True)
    prior_new = df.groupby("campaign_name")["new_target"].shift(1)
    df["old_target"] = df["old_target"].combine_first(prior_new)
    # Drop rows that have no old_target even after backfill (true initial set,
    # nothing to measure against)
    df = df.dropna(subset=["old_target"]).reset_index(drop=True)

    # Compute pct change in target and the OPERATIONAL direction.
    # On tROAS: target UP = bid TIGHTENING (operational "CUT" of bid permissiveness).
    #          target DOWN = bid LOOSENING (operational "RAISE" of permissiveness).
    df["pct_change_target"] = (df["new_target"] - df["old_target"]) / df["old_target"]
    df["pct_loosening"] = -df["pct_change_target"]
    df["direction"] = df["pct_loosening"].apply(
        lambda x: "RAISE" if x > 0.001 else ("CUT" if x < -0.001 else "NO_CHANGE"))
    df = df[df["direction"] != "NO_CHANGE"].reset_index(drop=True)
    return df


def load_budget_changes(csv_path: Path,
                        enabled_campaign_names: set[str]) -> pd.DataFrame:
    """Load the Google Ads budget-change CSV — used only to flag confounders
    on the same campaign within ±CONFOUNDER_WINDOW_DAYS. If file missing,
    returns empty DF (confounder check just no-ops on budgets).
    Mirrors efficiency_frontierV2.load_budget_changes.
    """
    if not csv_path.exists():
        print(f"  ℹ  Budget CSV not found at {csv_path} — skipping budget confounder check")
        return pd.DataFrame(columns=["change_date", "timestamp", "campaign_name"])

    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    reader = csv.DictReader(lines[2:] if len(lines) > 2 else lines)

    rows = []
    for r in reader:
        ch = r.get("Changes", "") or ""
        if "Budget amount" not in ch:
            continue
        try:
            ts = _parse_gads_datetime(r["Date & time"])
        except (ValueError, KeyError):
            continue
        if ts.date() < YEAR_START:
            continue
        cname = r.get("Campaign", "") or ""
        if cname not in enabled_campaign_names:
            continue
        rows.append({
            "change_date": ts.date(),
            "timestamp": ts,
            "campaign_name": cname,
        })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def fetch_enabled_campaigns(conn) -> pd.DataFrame:
    """Return enabled Google Home campaigns with their current tROAS.
    Filters to the 7 ENABLED_CAMPAIGN_IDS from efficiency_frontier scope.

    Columns: campaign_id, campaign_name, current_target
    """
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    sql = f"""
    SELECT
        campaign_id,
        campaign_name,
        target_roas AS current_target
    FROM campaigns
    WHERE account_id = ?
      AND campaign_id IN ({placeholders})
      AND status = 'ENABLED'
    ORDER BY campaign_name
    """
    df = pd.read_sql(sql, conn, params=[ACCOUNT_ID] + ENABLED_CAMPAIGN_IDS)
    return df


def fetch_campaign_daily(conn, campaign_name: str,
                         start_date: date, end_date: date) -> pd.DataFrame:
    """Fetch daily campaign metrics for a window.

    Returns one row per date with:
      report_date, spend_usd, conv (primary_conv_count from kpd),
      conv_value (primary_conv_value from kpd), impressions,
      rank_lost_is_avg.

    IMPORTANT — diverges from Microsoft script:
      • Spend & impressions come from campaign_performance_daily_search filtered
        to device='ALL' (per the efficiency_frontier convention — there's
        a per-device row AND a rollup row, summing all of them double-counts).
      • Conversions come from keyword_performance_daily aggregated to the
        campaign × date grain, filtered to device='ALL' AND geo_state='ALL'.
        That's the offline-routed primary_conv_count/value, which is the
        source of truth for Google Home (matches efficiency_frontier).
    """
    sql_cpd = """
    SELECT
        cpd.date AS report_date,
        SUM(cpd.cost_micros) / 1e6        AS spend_usd,
        SUM(cpd.impressions)              AS impressions,
        AVG(cpd.search_rank_lost_is)      AS rank_lost_is_avg
    FROM campaign_performance_daily_search cpd
    JOIN campaigns c ON cpd.campaign_id = c.campaign_id
    WHERE c.account_id   = ?
      AND c.campaign_name = ?
      AND cpd.date >= ?
      AND cpd.date <= ?
      AND cpd.device = 'ALL'
    GROUP BY cpd.date
    ORDER BY cpd.date
    """
    df_cpd = pd.read_sql(sql_cpd, conn, params=(ACCOUNT_ID, campaign_name,
                                                 start_date.isoformat(),
                                                 end_date.isoformat()))

    sql_kpd = """
    SELECT
        kpd.report_date AS report_date,
        SUM(kpd.primary_conv_count) AS conv,
        SUM(kpd.primary_conv_value) AS conv_value
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON ki.id = kpd.instance_id
    JOIN campaigns c ON ki.campaign_id = c.campaign_id
    WHERE c.account_id    = ?
      AND c.campaign_name = ?
      AND kpd.report_date >= ?
      AND kpd.report_date <= ?
      AND kpd.device      = 'ALL'
      AND kpd.geo_state   = 'ALL'
    GROUP BY kpd.report_date
    """
    df_kpd = pd.read_sql(sql_kpd, conn, params=(ACCOUNT_ID, campaign_name,
                                                 start_date.isoformat(),
                                                 end_date.isoformat()))

    df = df_cpd.merge(df_kpd, on="report_date", how="left")
    df["conv"] = df["conv"].fillna(0.0)
    df["conv_value"] = df["conv_value"].fillna(0.0)
    if not df.empty:
        df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    return df


# ═══════════════════════════════════════════════════════════════════
# CONFOUNDER CHECK
# ═══════════════════════════════════════════════════════════════════

def find_confounders(target_changes: pd.DataFrame,
                     budget_changes: pd.DataFrame,
                     campaign_name: str,
                     event_ts: datetime,
                     window_days: int = CONFOUNDER_WINDOW_DAYS) -> dict:
    """For a single change event, count other target changes and budget
    changes on the SAME campaign within ±window_days. Mirrors the logic
    in efficiency_frontierV2.build_change_attribution.

    Returns dict with keys: other_target_changes, budget_changes,
    total_confounders, clean (bool).
    """
    win_start = event_ts - timedelta(days=window_days)
    win_end   = event_ts + timedelta(days=window_days)

    same_camp_tgt = target_changes[target_changes["campaign_name"] == campaign_name]
    other_tgt = same_camp_tgt[
        (same_camp_tgt["timestamp"] >= win_start)
        & (same_camp_tgt["timestamp"] <= win_end)
        & (same_camp_tgt["timestamp"] != event_ts)
    ]
    same_camp_bud = budget_changes[budget_changes["campaign_name"] == campaign_name] \
        if not budget_changes.empty else budget_changes
    if not same_camp_bud.empty:
        bud_in_win = same_camp_bud[
            (same_camp_bud["timestamp"] >= win_start)
            & (same_camp_bud["timestamp"] <= win_end)
        ]
        n_bud = len(bud_in_win)
    else:
        n_bud = 0

    n_tgt = len(other_tgt)
    return {
        "other_target_changes": n_tgt,
        "budget_changes": n_bud,
        "total_confounders": n_tgt + n_bud,
        "clean": (n_tgt + n_bud) == 0,
    }


# ═══════════════════════════════════════════════════════════════════
# PER-EVENT ELASTICITY COMPUTATION
# ═══════════════════════════════════════════════════════════════════

def compute_event_elasticity(
    conn,
    campaign_name: str,
    change_date: date,
    event_ts: datetime,
    old_target: float,
    new_target: float,
    target_changes: pd.DataFrame,
    budget_changes: pd.DataFrame,
    pre_days: int = DEFAULT_PRE_DAYS,
    post_days: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
    end_date_cutoff: date | None = None,
) -> dict:
    """Compute empirical elasticity for a single tROAS-change event.

    Pre window:  [change_date - pre_days, change_date - 1]
    Post window: [change_date + lag_days, change_date + lag_days + post_days - 1]

    See SIGN CONVENTION at top of file. Output uses pct_loosening as the
    "x" in the regression so positive elasticity means well-behaved.

    Returns a dict with all the metrics + an exclusion_reason if applicable.
    """
    pre_start = change_date - timedelta(days=pre_days)
    pre_end   = change_date - timedelta(days=1)
    post_start = change_date + timedelta(days=lag_days)
    post_end   = change_date + timedelta(days=lag_days + post_days - 1)

    pct_change_target = (new_target - old_target) / old_target
    pct_loosening = -pct_change_target
    direction = "RAISE" if pct_loosening > 0 else "CUT"

    out = {
        "campaign_name": campaign_name,
        "change_date": change_date,
        "old_target": old_target,
        "new_target": new_target,
        "pct_change_target": pct_change_target,    # raw signed (negative on loosening)
        "pct_loosening": pct_loosening,            # operational (positive on loosening)
        "direction": direction,                    # RAISE = loosen, CUT = tighten
        "pre_window_start": pre_start,
        "pre_window_end": pre_end,
        "post_window_start": post_start,
        "post_window_end": post_end,
        "exclusion_reason": None,
        "low_confidence_flags": [],
    }

    # Cutoff check — if post-window extends past end_date_cutoff, skip
    if end_date_cutoff is not None and post_end > end_date_cutoff:
        out["exclusion_reason"] = (
            f"post-window ends {post_end}, beyond cutoff {end_date_cutoff} "
            f"(insufficient post-change data available)"
        )
        return out

    # Confounder check
    conf = find_confounders(target_changes, budget_changes,
                            campaign_name, event_ts)
    out["confounders_other_target"] = conf["other_target_changes"]
    out["confounders_budget"] = conf["budget_changes"]
    out["clean"] = conf["clean"]
    if not conf["clean"]:
        out["low_confidence_flags"].append(
            f"{conf['other_target_changes']} other target change(s) and "
            f"{conf['budget_changes']} budget change(s) within "
            f"±{CONFOUNDER_WINDOW_DAYS}d on same campaign"
        )

    # Fetch pre + post data
    pre_df  = fetch_campaign_daily(conn, campaign_name, pre_start, pre_end)
    post_df = fetch_campaign_daily(conn, campaign_name, post_start, post_end)

    def agg(df):
        if df.empty:
            return {"spend": 0, "conv": 0, "conv_value": 0, "n_days": 0,
                    "rank_lost_is": None}
        return {
            "spend": df["spend_usd"].sum(),
            "conv": df["conv"].sum(),
            "conv_value": df["conv_value"].sum(),
            "n_days": len(df),
            "rank_lost_is": df["rank_lost_is_avg"].mean(),
        }

    pre  = agg(pre_df)
    post = agg(post_df)

    # Daily-rate normalization (in case pre/post windows are unequal lengths)
    pre_conv_per_day  = pre["conv"]  / pre["n_days"]  if pre["n_days"]  else 0
    post_conv_per_day = post["conv"] / post["n_days"] if post["n_days"] else 0
    pre_spend_per_day  = pre["spend"]  / pre["n_days"]  if pre["n_days"]  else 0
    post_spend_per_day = post["spend"] / post["n_days"] if post["n_days"] else 0

    pre_cpa  = (pre["spend"]  / pre["conv"])  if pre["conv"]  > 0 else None
    post_cpa = (post["spend"] / post["conv"]) if post["conv"] > 0 else None
    pre_rpl  = (pre["conv_value"]  / pre["conv"])  if pre["conv"]  > 0 else None
    post_rpl = (post["conv_value"] / post["conv"]) if post["conv"] > 0 else None
    pre_roas  = (pre["conv_value"]  / pre["spend"])  if pre["spend"]  > 0 else None
    post_roas = (post["conv_value"] / post["spend"]) if post["spend"] > 0 else None

    out.update({
        "pre_n_days": pre["n_days"],
        "post_n_days": post["n_days"],
        "pre_conv_per_day": pre_conv_per_day,
        "post_conv_per_day": post_conv_per_day,
        "pre_spend_per_day": pre_spend_per_day,
        "post_spend_per_day": post_spend_per_day,
        "pre_cpa": pre_cpa,
        "post_cpa": post_cpa,
        "pre_rpl": pre_rpl,
        "post_rpl": post_rpl,
        "pre_roas": pre_roas,
        "post_roas": post_roas,
        "pre_rank_lost_is": pre["rank_lost_is"],
        "post_rank_lost_is": post["rank_lost_is"],
    })

    # Exclusions: insufficient data
    if pre["n_days"] < pre_days * 0.7:
        out["exclusion_reason"] = (
            f"insufficient pre-window data: only {pre['n_days']}/{pre_days} days"
        )
        return out
    if post["n_days"] < post_days * 0.7:
        out["exclusion_reason"] = (
            f"insufficient post-window data: only {post['n_days']}/{post_days} days"
        )
        return out
    if pre["conv"] < MIN_CONV_FOR_RELIABLE:
        out["exclusion_reason"] = (
            f"insufficient pre-window conversions: {pre['conv']:.1f} < "
            f"{MIN_CONV_FOR_RELIABLE} threshold"
        )
        return out
    if post["conv"] < MIN_CONV_FOR_RELIABLE:
        out["exclusion_reason"] = (
            f"insufficient post-window conversions: {post['conv']:.1f} < "
            f"{MIN_CONV_FOR_RELIABLE} threshold"
        )
        return out

    # Compute elasticity. Note: pct_loosening can be negative (a tighten),
    # in which case empirical_elasticity uses negative-by-negative giving
    # positive when conv falls — operationally consistent.
    pct_change_conv = (post_conv_per_day - pre_conv_per_day) / pre_conv_per_day
    pct_change_spend = (post_spend_per_day - pre_spend_per_day) / pre_spend_per_day if pre_spend_per_day > 0 else None
    if abs(pct_loosening) > 1e-6:
        empirical_elasticity = pct_change_conv / pct_loosening
    else:
        empirical_elasticity = None

    out.update({
        "pct_change_conv_per_day": pct_change_conv,
        "pct_change_spend_per_day": pct_change_spend,
        "empirical_elasticity": empirical_elasticity,
    })

    # Low-confidence flags (don't exclude, just mark)
    if pre_rpl and post_rpl:
        rpl_drift = abs(post_rpl - pre_rpl) / pre_rpl
        out["rpl_drift_pct"] = rpl_drift
        if rpl_drift > RPL_DRIFT_FLAG_THRESHOLD:
            out["low_confidence_flags"].append(
                f"RPL drifted {rpl_drift:.0%} between pre/post windows "
                f"(pre ${pre_rpl:.2f} vs post ${post_rpl:.2f})"
            )

    # Confidence weight: total conversions in both windows
    out["weight_total_conv"] = pre["conv"] + post["conv"]
    out["confidence_score"] = (
        "high" if not out["low_confidence_flags"] and out["weight_total_conv"] >= 50
        else ("medium" if out["weight_total_conv"] >= 20 else "low")
    )

    return out


# ═══════════════════════════════════════════════════════════════════
# VISUALIZATION
# ═══════════════════════════════════════════════════════════════════

def build_event_chart_png(
    conn,
    campaign_name: str,
    change_date: date,
    old_target: float,
    new_target: float,
    pre_days: int = VIZ_PRE_DAYS,
    post_days: int = VIZ_POST_DAYS,
    measurement_pre: int = DEFAULT_PRE_DAYS,
    measurement_post: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
) -> bytes:
    """Build a 3-panel time-series chart for a single tROAS change event.

    Panels (top to bottom):
      1. tROAS setting (step function: old_target → new_target on change_date)
      2. Daily ROAS, with 7-day rolling average (replaces Microsoft's CPA panel)
      3. Daily conversions, with 7-day rolling average

    Each panel:
      - Vertical line at change_date
      - Shaded measurement windows (pre and post, w/ stabilization lag gap)

    Returns: PNG bytes (for embedding in Excel via openpyxl).
    """
    viz_start = change_date - timedelta(days=pre_days)
    viz_end   = change_date + timedelta(days=post_days)
    df = fetch_campaign_daily(conn, campaign_name, viz_start, viz_end)

    if df.empty:
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.text(0.5, 0.5, f"No data available for\n{campaign_name}\nbetween {viz_start} and {viz_end}",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    # Compute daily metrics
    df = df.copy()
    df["roas"] = df.apply(
        lambda r: (r["conv_value"] / r["spend_usd"]) if r["spend_usd"] > 0 else None,
        axis=1)
    df["conv_7d_ma"] = df["conv"].rolling(window=7, min_periods=3).mean()
    spend_7d = df["spend_usd"].rolling(window=7, min_periods=3).sum()
    rev_7d = df["conv_value"].rolling(window=7, min_periods=3).sum()
    df["roas_7d_ma"] = rev_7d / spend_7d.where(spend_7d > 0)

    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8.5), sharex=True,
        gridspec_kw={"height_ratios": [1, 2, 2], "hspace": 0.15},
    )

    change_dt = pd.Timestamp(change_date)
    pre_window_start = pd.Timestamp(change_date - timedelta(days=measurement_pre))
    pre_window_end   = pd.Timestamp(change_date - timedelta(days=1))
    post_window_start = pd.Timestamp(change_date + timedelta(days=lag_days))
    post_window_end   = pd.Timestamp(change_date + timedelta(days=lag_days + measurement_post - 1))
    df["report_dt"] = pd.to_datetime(df["report_date"])

    # ── Panel 1: tROAS setting (step function)
    tcpa_dates = pd.date_range(viz_start, viz_end, freq="D")
    target_values = [old_target if d < change_dt else new_target for d in tcpa_dates]
    ax1.step(tcpa_dates, target_values, where="post", color="#1F4E79", linewidth=2.5)
    ax1.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax1.set_ylabel("tROAS setting", fontsize=10)
    direction_label = "LOOSEN" if new_target < old_target else "TIGHTEN"
    pct_change = (new_target / old_target - 1) * 100
    ax1.set_title(f"{campaign_name}\ntROAS change: {old_target:.2f} → {new_target:.2f} "
                  f"({pct_change:+.0f}%, {direction_label}) on {change_date}",
                  fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3)
    ax1.annotate(f"{old_target:.2f}", xy=(viz_start, old_target), xytext=(5, -3),
                 textcoords="offset points", fontsize=9, color="#1F4E79")
    ax1.annotate(f"{new_target:.2f}", xy=(viz_end, new_target), xytext=(-30, 3),
                 textcoords="offset points", fontsize=9, color="#1F4E79", fontweight="bold")
    y_pad = max(0.05, abs(new_target - old_target) * 0.5) if new_target != old_target else 0.05
    ax1.set_ylim(min(old_target, new_target) - y_pad, max(old_target, new_target) + y_pad)

    # ── Panel 2: Daily ROAS + 7-day MA + reference lines
    ax2.scatter(df["report_dt"], df["roas"], color="gray", alpha=0.4, s=15, label="Daily ROAS")
    ax2.plot(df["report_dt"], df["roas_7d_ma"], color="#C0504D", linewidth=2,
             label="7-day rolling avg")
    ax2.axhline(old_target, color="#1F4E79", linestyle=":", linewidth=1, alpha=0.6,
                label=f"Old tROAS target ({old_target:.2f})")
    ax2.axhline(new_target, color="#1F4E79", linestyle="-.", linewidth=1, alpha=0.6,
                label=f"New tROAS target ({new_target:.2f})")
    ax2.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax2.axvspan(pre_window_start, pre_window_end, color="#1F4E79", alpha=0.08,
                label=f"Pre measurement ({measurement_pre}d)")
    ax2.axvspan(post_window_start, post_window_end, color="#9BBB59", alpha=0.12,
                label=f"Post measurement ({measurement_post}d, +{lag_days}d lag)")
    ax2.set_ylabel("ROAS", fontsize=10)
    ax2.legend(loc="best", fontsize=8, framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: Daily conversions + 7-day MA
    ax3.scatter(df["report_dt"], df["conv"], color="gray", alpha=0.4, s=15, label="Daily conv")
    ax3.plot(df["report_dt"], df["conv_7d_ma"], color="#4F81BD", linewidth=2,
             label="7-day rolling avg")
    ax3.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax3.axvspan(pre_window_start, pre_window_end, color="#1F4E79", alpha=0.08)
    ax3.axvspan(post_window_start, post_window_end, color="#9BBB59", alpha=0.12)
    ax3.set_ylabel("Conversions / day", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)
    ax3.set_xlabel("Date", fontsize=10)

    ax3.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_campaign_overview_chart_png(
    conn,
    campaign_name: str,
    current_target: float | None,
    start_date: date,
    end_date: date,
    change_events: list[dict],
    measurement_pre: int = DEFAULT_PRE_DAYS,
    measurement_post: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
) -> bytes:
    """Build a 3-panel time-series chart for a single campaign over its
    full history window.

    For campaigns with no change events: clean chart at flat current_target.
    For campaigns with changes: red dashed lines + pre/post shading.

    change_events: list of dicts with at minimum {change_date, old_target, new_target}
    """
    df = fetch_campaign_daily(conn, campaign_name, start_date, end_date)

    if df.empty:
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.text(0.5, 0.5, f"No data available for\n{campaign_name}\n"
                          f"between {start_date} and {end_date}",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    df = df.copy()
    df["roas"] = df.apply(
        lambda r: (r["conv_value"] / r["spend_usd"]) if r["spend_usd"] > 0 else None,
        axis=1)
    df["conv_7d_ma"] = df["conv"].rolling(window=7, min_periods=3).mean()
    df["roas_7d_ma"] = df["roas"].rolling(window=7, min_periods=3).mean()
    df["report_dt"] = pd.to_datetime(df["report_date"])

    sorted_events = sorted(change_events, key=lambda e: e["change_date"])

    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8.5), sharex=True,
        gridspec_kw={"height_ratios": [1, 2, 2], "hspace": 0.15},
    )

    # ── Panel 1: tROAS step function ───────────────────────────────
    panel1_dates = pd.date_range(start_date, end_date, freq="D")
    if not sorted_events:
        if current_target is None:
            ax1.text(0.5, 0.5, "No tROAS setting on file",
                     ha="center", va="center", fontsize=10, transform=ax1.transAxes,
                     style="italic", color="gray")
        else:
            ax1.plot(panel1_dates,
                     [current_target] * len(panel1_dates),
                     color="#1F4E79", linewidth=2.5)
            ax1.annotate(f"{current_target:.2f}",
                         xy=(panel1_dates[-1], current_target),
                         xytext=(-30, 3), textcoords="offset points",
                         fontsize=9, color="#1F4E79", fontweight="bold")
    else:
        first_target = sorted_events[0]["old_target"]
        target_values = []
        event_idx = 0
        current = first_target
        for d in panel1_dates:
            while (event_idx < len(sorted_events)
                   and sorted_events[event_idx]["change_date"] <= d.date()):
                current = sorted_events[event_idx]["new_target"]
                event_idx += 1
            target_values.append(current)
        ax1.step(panel1_dates, target_values, where="post",
                 color="#1F4E79", linewidth=2.5)
        ax1.annotate(f"{first_target:.2f}",
                     xy=(panel1_dates[0], first_target),
                     xytext=(5, -3), textcoords="offset points",
                     fontsize=9, color="#1F4E79")
        ax1.annotate(f"{target_values[-1]:.2f}",
                     xy=(panel1_dates[-1], target_values[-1]),
                     xytext=(-30, 3), textcoords="offset points",
                     fontsize=9, color="#1F4E79", fontweight="bold")
        all_targets = [e["old_target"] for e in sorted_events] + [e["new_target"] for e in sorted_events]
        y_min, y_max = min(all_targets), max(all_targets)
        y_pad = max(0.05, (y_max - y_min) * 0.3)
        ax1.set_ylim(y_min - y_pad, y_max + y_pad)

    ax1.set_ylabel("tROAS setting", fontsize=10)
    title = (f"{campaign_name}\n"
             f"{len(sorted_events)} tROAS change(s) in window {start_date} to {end_date}")
    ax1.set_title(title, fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: Daily ROAS + 7-day MA + current-target reference
    ax2.scatter(df["report_dt"], df["roas"], color="gray", alpha=0.4, s=15,
                label="Daily ROAS")
    ax2.plot(df["report_dt"], df["roas_7d_ma"], color="#C0504D", linewidth=2,
             label="7-day rolling avg")
    if current_target is not None:
        ax2.axhline(current_target, color="#1F4E79", linestyle="-.", linewidth=1,
                    alpha=0.6, label=f"Current tROAS target ({current_target:.2f})")

    for ev in sorted_events:
        change_dt = pd.Timestamp(ev["change_date"])
        pre_start_e = pd.Timestamp(ev["change_date"] - timedelta(days=measurement_pre))
        pre_end_e   = pd.Timestamp(ev["change_date"] - timedelta(days=1))
        post_start_e = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days))
        post_end_e   = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days + measurement_post - 1))
        ax2.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
        ax2.axvspan(pre_start_e, pre_end_e, color="#1F4E79", alpha=0.08)
        ax2.axvspan(post_start_e, post_end_e, color="#9BBB59", alpha=0.12)

    handles, labels = ax2.get_legend_handles_labels()
    if sorted_events:
        from matplotlib.patches import Patch
        handles.append(Patch(facecolor="#1F4E79", alpha=0.08,
                             label=f"Pre measurement ({measurement_pre}d)"))
        handles.append(Patch(facecolor="#9BBB59", alpha=0.12,
                             label=f"Post measurement ({measurement_post}d, +{lag_days}d lag)"))
    ax2.legend(handles=handles, loc="best", fontsize=8, framealpha=0.9)
    ax2.set_ylabel("ROAS", fontsize=10)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: Daily conversions + 7-day MA
    ax3.scatter(df["report_dt"], df["conv"], color="gray", alpha=0.4, s=15,
                label="Daily conv")
    ax3.plot(df["report_dt"], df["conv_7d_ma"], color="#4F81BD", linewidth=2,
             label="7-day rolling avg")
    for ev in sorted_events:
        change_dt = pd.Timestamp(ev["change_date"])
        pre_start_e = pd.Timestamp(ev["change_date"] - timedelta(days=measurement_pre))
        pre_end_e   = pd.Timestamp(ev["change_date"] - timedelta(days=1))
        post_start_e = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days))
        post_end_e   = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days + measurement_post - 1))
        ax3.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
        ax3.axvspan(pre_start_e, pre_end_e, color="#1F4E79", alpha=0.08)
        ax3.axvspan(post_start_e, post_end_e, color="#9BBB59", alpha=0.12)
    ax3.set_ylabel("Conversions / day", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)
    ax3.set_xlabel("Date", fontsize=10)

    ax3.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# REGRESSION FIT
# ═══════════════════════════════════════════════════════════════════

def weighted_regression(events: list[dict], direction_filter: str = "RAISE") -> dict:
    """Fit a weighted least-squares regression of (% Δ conv per day) on
    (% loosening). Slope = empirical elasticity for the account.

    direction_filter:
      "RAISE" — bid LOOSENINGS (tROAS cuts). Operationally: did loosening
                lift volume as expected?
      "CUT"   — bid TIGHTENINGS (tROAS raises). Reference only; usually
                differs from raise elasticity due to ratchet effects.

    Returns a dict with: slope (elasticity), intercept, n, r_squared,
    se_slope, ci_low, ci_high, weighted_mean_elasticity.
    """
    valid = [e for e in events
             if e.get("exclusion_reason") is None
             and e.get("direction") == direction_filter
             and e.get("empirical_elasticity") is not None]
    if len(valid) < 2:
        return {
            "n": len(valid),
            "error": f"insufficient events for regression "
                     f"(need ≥2, got {len(valid)})",
        }

    x = np.array([e["pct_loosening"] for e in valid])
    y = np.array([e["pct_change_conv_per_day"] for e in valid])
    w = np.array([e["weight_total_conv"] for e in valid])

    # 1. Weighted mean of per-event elasticities (simple)
    eps = np.array([e["empirical_elasticity"] for e in valid])
    weighted_mean_eps = np.average(eps, weights=w)

    # 2. Weighted regression with intercept
    W = np.diag(w)
    X = np.column_stack([np.ones(len(x)), x])
    XtWX = X.T @ W @ X
    XtWy = X.T @ W @ y
    try:
        beta = np.linalg.solve(XtWX, XtWy)
        intercept, slope = beta[0], beta[1]
    except np.linalg.LinAlgError:
        return {"n": len(valid), "error": "regression failed (singular matrix)"}

    y_pred = X @ beta
    ss_res = np.sum(w * (y - y_pred) ** 2)
    y_mean_w = np.average(y, weights=w)
    ss_tot = np.sum(w * (y - y_mean_w) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    if len(valid) > 2:
        residual_var = ss_res / (len(valid) - 2)
        try:
            cov_matrix = residual_var * np.linalg.inv(XtWX)
            se_slope = np.sqrt(cov_matrix[1, 1])
        except np.linalg.LinAlgError:
            se_slope = None
    else:
        se_slope = None

    ci_low = slope - 1.96 * se_slope if se_slope else None
    ci_high = slope + 1.96 * se_slope if se_slope else None

    # 3. Slope through origin (force intercept=0): pure elasticity
    denom = np.sum(w * x * x)
    slope_origin = np.sum(w * x * y) / denom if denom > 0 else None

    return {
        "n": len(valid),
        "n_excluded": len(events) - len(valid),
        "slope_with_intercept": slope,
        "intercept": intercept,
        "slope_through_origin": slope_origin,
        "weighted_mean_elasticity": weighted_mean_eps,
        "r_squared": r_squared,
        "se_slope": se_slope,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "events_used": valid,
    }


# ═══════════════════════════════════════════════════════════════════
# WORKBOOK WRITERS
# ═══════════════════════════════════════════════════════════════════

def write_headline(ws, regression_raise: dict, regression_cut: dict,
                   exclude_pre, n_total_events: int,
                   pre_days: int, post_days: int, lag_days: int) -> None:
    ws.cell(row=1, column=1, value="ELASTICITY EVIDENCE — Google Home").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"Run date: {date.today().isoformat()}")
    r = 4
    ws.cell(row=r, column=1, value="METHODOLOGY").font = Font(bold=True, size=12)
    r += 1
    ws.cell(row=r, column=1,
            value=f"For each tROAS change event, compare conv volume in "
                  f"{pre_days}d pre-window vs {post_days}d post-window "
                  f"(skipping {lag_days}d stabilization lag). "
                  f"Empirical elasticity = (% Δ conv per day) / (% loosening). "
                  f"% loosening = -(% Δ tROAS), so a tROAS CUT loosens the "
                  f"bid (positive % loosening) and a well-behaved campaign "
                  f"produces positive elasticity — directly comparable to the "
                  f"Microsoft strategic model's 0.6/1.0/1.2 scenarios.")
    ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    r += 1
    if exclude_pre is not None:
        ws.cell(row=r, column=1,
                value=f"Excludes: events with post-window starting before "
                      f"{exclude_pre}, or with <{MIN_CONV_FOR_RELIABLE} conv "
                      f"in either window.")
    else:
        ws.cell(row=r, column=1,
                value=f"All YTD change events with sufficient pre/post data "
                      f"are included. Confounded events (other target/budget "
                      f"changes within ±{CONFOUNDER_WINDOW_DAYS}d) are flagged "
                      f"in Per-Event Detail but kept in the regression with "
                      f"low-confidence weight.")
    ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    r += 2

    # Headline numbers — RAISE regression (loosenings; the operationally relevant one)
    ws.cell(row=r, column=1, value="EMPIRICAL ELASTICITY — LOOSENINGS (tROAS cuts)").font = Font(bold=True, size=12)
    r += 1
    if "error" in regression_raise:
        ws.cell(row=r, column=1, value=f"Could not compute: {regression_raise['error']}").font = Font(color="9C0006")
        r += 2
    else:
        ws.cell(row=r, column=1, value=f"Events used: {regression_raise['n']} (of {n_total_events} total events)")
        r += 1
        slope = regression_raise["slope_with_intercept"]
        ci_low = regression_raise["ci_low"]
        ci_high = regression_raise["ci_high"]
        r2 = regression_raise.get("r_squared", 0)

        cell = ws.cell(row=r, column=1,
                       value=f"Empirical elasticity (regression slope): {slope:.2f}")
        cell.font = Font(bold=True, size=12)
        if slope >= 1.1:
            cell.fill = PatternFill("solid", fgColor=GOOD_FILL)
            verdict = "supports AGGRESSIVE scenario"
        elif slope >= 0.85:
            cell.fill = PatternFill("solid", fgColor=NEUTRAL_FILL)
            verdict = "supports MID scenario"
        elif slope >= 0.45:
            cell.fill = PatternFill("solid", fgColor=WARN_FILL)
            verdict = "supports CONSERVATIVE scenario"
        else:
            cell.fill = PatternFill("solid", fgColor=BAD_FILL)
            verdict = "BELOW conservative scenario — bid loosenings ineffective"
        r += 1
        ws.cell(row=r, column=1, value=f"Verdict: {verdict}").font = Font(italic=True)
        r += 1

        if ci_low is not None:
            ws.cell(row=r, column=1,
                    value=f"95% confidence interval: [{ci_low:.2f}, {ci_high:.2f}]")
            r += 1
        ws.cell(row=r, column=1, value=f"R²: {r2:.2f}")
        r += 1
        if regression_raise.get("slope_through_origin") is not None:
            ws.cell(row=r, column=1,
                    value=f"Slope through origin (no intercept): "
                          f"{regression_raise['slope_through_origin']:.2f}")
            r += 1
        ws.cell(row=r, column=1,
                value=f"Weighted mean of per-event elasticities: "
                      f"{regression_raise['weighted_mean_elasticity']:.2f}")
        r += 2

    # Comparison vs. model scenarios
    ws.cell(row=r, column=1, value="COMPARISON VS MODEL SCENARIOS").font = Font(bold=True, size=12)
    r += 1
    headers = ["scenario", "model_assumption", "empirical_evidence_supports"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
    r += 1
    if "slope_with_intercept" in regression_raise:
        ci_low = regression_raise.get("ci_low")
        ci_high = regression_raise.get("ci_high")
        for scen, val in MODEL_SCENARIOS.items():
            ws.cell(row=r, column=1, value=scen.upper())
            ws.cell(row=r, column=2, value=f"ε = {val:.1f}")
            if ci_low is not None and ci_high is not None:
                if ci_low <= val <= ci_high:
                    verdict = "YES — within 95% CI"
                    fill = GOOD_FILL
                elif val < ci_low:
                    verdict = f"NO — empirical evidence higher (CI floor {ci_low:.2f})"
                    fill = WARN_FILL
                else:
                    verdict = f"NO — empirical evidence lower (CI ceiling {ci_high:.2f})"
                    fill = BAD_FILL
            else:
                verdict = "insufficient data for CI comparison"
                fill = NEUTRAL_FILL
            cell = ws.cell(row=r, column=3, value=verdict)
            cell.fill = PatternFill("solid", fgColor=fill)
            r += 1
    r += 2

    # Cuts (tightenings) — reference only
    ws.cell(row=r, column=1, value="EMPIRICAL ELASTICITY — TIGHTENINGS (reference only)").font = Font(bold=True, size=12)
    r += 1
    if "error" in regression_cut:
        ws.cell(row=r, column=1, value=f"Could not compute: {regression_cut['error']}")
    else:
        ws.cell(row=r, column=1,
                value=f"Tighten elasticity: {regression_cut['slope_with_intercept']:.2f} "
                      f"(n={regression_cut['n']}, R²={regression_cut['r_squared']:.2f})")
        r += 1
        ws.cell(row=r, column=1,
                value="(Tighten elasticity often differs from loosen elasticity. "
                      "For strategic loosen-the-bid decisions, the LOOSENINGS number is the relevant one.)")
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)

    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 50


def write_per_event(ws, events: list[dict]) -> None:
    ws.cell(row=1, column=1, value="PER-EVENT ELASTICITY DETAIL").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1,
            value="Direction RAISE = bid LOOSENING (tROAS cut). "
                  "Direction CUT = bid TIGHTENING (tROAS raise). "
                  "pct_loosening = -(% Δ tROAS).")
    ws.cell(row=2, column=1).alignment = Alignment(wrap_text=True)
    r = 4
    headers = [
        "campaign", "change_date", "direction", "old_target", "new_target",
        "pct_change_target", "pct_loosening",
        "pre_n_days", "post_n_days",
        "pre_conv_per_day", "post_conv_per_day", "pct_change_conv",
        "pre_roas", "post_roas", "pre_cpa", "post_cpa", "pre_rpl", "post_rpl",
        "rpl_drift_pct", "empirical_elasticity",
        "weight_total_conv", "confidence_score",
        "confounders_other_target", "confounders_budget", "clean",
        "exclusion_reason", "low_confidence_flags",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
    r += 1

    def fmt_pct(v): return f"{v:+.1%}" if v is not None else ""
    def fmt_n(v, d=2):
        return round(v, d) if v is not None else ""

    for e in events:
        ws.cell(row=r, column=1, value=e.get("campaign_name"))
        ws.cell(row=r, column=2, value=e.get("change_date"))
        ws.cell(row=r, column=3, value=e.get("direction"))
        ws.cell(row=r, column=4, value=fmt_n(e.get("old_target"), 4))
        ws.cell(row=r, column=5, value=fmt_n(e.get("new_target"), 4))
        ws.cell(row=r, column=6, value=fmt_pct(e.get("pct_change_target")))
        ws.cell(row=r, column=7, value=fmt_pct(e.get("pct_loosening")))
        ws.cell(row=r, column=8, value=e.get("pre_n_days"))
        ws.cell(row=r, column=9, value=e.get("post_n_days"))
        ws.cell(row=r, column=10, value=fmt_n(e.get("pre_conv_per_day")))
        ws.cell(row=r, column=11, value=fmt_n(e.get("post_conv_per_day")))
        ws.cell(row=r, column=12, value=fmt_pct(e.get("pct_change_conv_per_day")))
        ws.cell(row=r, column=13, value=fmt_n(e.get("pre_roas"), 3))
        ws.cell(row=r, column=14, value=fmt_n(e.get("post_roas"), 3))
        ws.cell(row=r, column=15, value=fmt_n(e.get("pre_cpa")))
        ws.cell(row=r, column=16, value=fmt_n(e.get("post_cpa")))
        ws.cell(row=r, column=17, value=fmt_n(e.get("pre_rpl")))
        ws.cell(row=r, column=18, value=fmt_n(e.get("post_rpl")))
        rd = e.get("rpl_drift_pct")
        ws.cell(row=r, column=19, value=f"{rd:.0%}" if rd is not None else "")
        eps = e.get("empirical_elasticity")
        cell = ws.cell(row=r, column=20, value=round(eps, 2) if eps is not None else "")
        if eps is not None:
            if eps >= 1.0:
                cell.fill = PatternFill("solid", fgColor=GOOD_FILL)
            elif eps >= 0.6:
                cell.fill = PatternFill("solid", fgColor=NEUTRAL_FILL)
            else:
                cell.fill = PatternFill("solid", fgColor=BAD_FILL)
        ws.cell(row=r, column=21, value=round(e.get("weight_total_conv", 0), 1) if e.get("weight_total_conv") else "")
        ws.cell(row=r, column=22, value=e.get("confidence_score", ""))
        ws.cell(row=r, column=23, value=e.get("confounders_other_target", ""))
        ws.cell(row=r, column=24, value=e.get("confounders_budget", ""))
        ws.cell(row=r, column=25, value="yes" if e.get("clean") else ("no" if e.get("clean") is False else ""))
        ws.cell(row=r, column=26, value=e.get("exclusion_reason") or "")
        ws.cell(row=r, column=27, value="; ".join(e.get("low_confidence_flags", [])))
        r += 1

    widths = [55, 14, 10, 10, 10, 14, 14, 10, 10, 14, 14, 14, 10, 10, 10, 10, 10, 10, 12, 14, 14, 14, 14, 14, 8, 50, 50]
    for i, w in enumerate(widths, start=1):
        col_letter = chr(64 + i) if i <= 26 else "A" + chr(64 + i - 26)
        ws.column_dimensions[col_letter].width = w
    ws.freeze_panes = "B5"


def write_diagnostics(ws, events: list[dict], regression_raise: dict,
                      regression_cut: dict, exclude_pre,
                      pre_days: int, post_days: int, lag_days: int) -> None:
    ws.cell(row=1, column=1, value="DIAGNOSTICS").font = Font(bold=True, size=14)
    r = 3
    ws.cell(row=r, column=1, value="Configuration:").font = Font(bold=True)
    r += 1
    items = [
        ("Account", f"id={ACCOUNT_ID} ({ACCOUNT_LABEL})"),
        ("Campaigns analyzed", f"{len(ENABLED_CAMPAIGN_IDS)} enabled NB-SEARCH-ALL-USA-STE"),
        ("Pre-window days", pre_days),
        ("Post-window days", post_days),
        ("Stabilization lag days", lag_days),
        ("Exclude-pre cutoff", exclude_pre.isoformat() if exclude_pre else "DISABLED (all events used)"),
        ("Confounder window (±days, same campaign)", CONFOUNDER_WINDOW_DAYS),
        ("RPL drift flag threshold", f"{RPL_DRIFT_FLAG_THRESHOLD:.0%}"),
        ("Min conversions per window", MIN_CONV_FOR_RELIABLE),
        ("Total events scanned", len(events)),
        ("Events excluded", sum(1 for e in events if e.get("exclusion_reason"))),
        ("Events used in LOOSENINGS regression", regression_raise.get("n", 0)),
        ("Events used in TIGHTENINGS regression", regression_cut.get("n", 0)),
        ("Events flagged confounded (kept, weighted)",
         sum(1 for e in events if e.get("clean") is False)),
    ]
    for k, v in items:
        ws.cell(row=r, column=1, value=k)
        ws.cell(row=r, column=2, value=str(v))
        r += 1

    r += 2
    ws.cell(row=r, column=1, value="Exclusion reasons:").font = Font(bold=True)
    r += 1
    excluded = [e for e in events if e.get("exclusion_reason")]
    if not excluded:
        ws.cell(row=r, column=1, value="(no events excluded)")
    else:
        for e in excluded:
            ws.cell(row=r, column=1,
                    value=f"{e['campaign_name']} on {e['change_date']}: "
                          f"{e['exclusion_reason']}")
            r += 1

    r += 2
    ws.cell(row=r, column=1, value="Confounded but kept (low confidence):").font = Font(bold=True)
    r += 1
    confounded = [e for e in events if e.get("clean") is False
                  and e.get("exclusion_reason") is None]
    if not confounded:
        ws.cell(row=r, column=1, value="(no confounded events kept)")
    else:
        for e in confounded:
            flags = "; ".join(e.get("low_confidence_flags", []))
            ws.cell(row=r, column=1,
                    value=f"{e['campaign_name']} on {e['change_date']}: {flags}")
            r += 1

    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["B"].width = 30


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--targets-csv", default=str(DEFAULT_TARGETS_CSV),
                   help="Google Ads target-change CSV export")
    p.add_argument("--budgets-csv", default=str(DEFAULT_BUDGETS_CSV),
                   help="Google Ads budget-change CSV (used for confounder check)")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--exclude-pre", default=None,
                   help="Exclude events whose post-window starts before this "
                        "date (ISO YYYY-MM-DD). DEFAULT: disabled.")
    p.add_argument("--pre-days", type=int, default=DEFAULT_PRE_DAYS)
    p.add_argument("--post-days", type=int, default=DEFAULT_POST_DAYS)
    p.add_argument("--end-date", default=None,
                   help="Cutoff date (ISO YYYY-MM-DD). Events whose post-window "
                        f"extends past this date are excluded. Default: today − "
                        f"{DEFAULT_BACKFILL_BUFFER_DAYS}d (yesterday) to allow "
                        "for offline-conversion attribution lag.")
    p.add_argument("--overview-start", default="2026-01-01",
                   help="Start date for the per-campaign overview charts. "
                        "Default: 2026-01-01.")
    return p.parse_args()


def main():
    args = parse_args()

    db_path = Path(args.db_path)
    targets_csv = Path(args.targets_csv)
    budgets_csv = Path(args.budgets_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exclude_pre = (date.fromisoformat(args.exclude_pre)
                   if args.exclude_pre else None)
    end_date_cutoff = (date.fromisoformat(args.end_date) if args.end_date
                       else date.today() - timedelta(days=DEFAULT_BACKFILL_BUFFER_DAYS))
    overview_start = date.fromisoformat(args.overview_start)

    banner("ELASTICITY REGRESSION — Google Home Insurance")
    print(f"  DB:                  {db_path}")
    print(f"  Targets CSV:         {targets_csv}")
    print(f"  Budgets CSV:         {budgets_csv}")
    if exclude_pre:
        print(f"  Exclude pre cutoff:  {exclude_pre} (post-windows must start ≥ this)")
    else:
        print(f"  Exclude pre cutoff:  DISABLED (all events with sufficient data)")
    print(f"  Pre-window days:     {args.pre_days}")
    print(f"  Post-window days:    {args.post_days}")
    print(f"  Stabilization lag:   {STABILIZATION_LAG} days")
    print(f"  End-date cutoff:     {end_date_cutoff}")
    print(f"  Overview chart start:{overview_start}")
    print()

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)
    if not targets_csv.exists():
        print(f"ERROR: targets CSV not found at {targets_csv}")
        sys.exit(1)

    # Resolve enabled campaign names from DB (since CSV uses names, DB uses IDs)
    conn = sqlite3.connect(db_path)
    enabled = fetch_enabled_campaigns(conn)
    if len(enabled) != len(ENABLED_CAMPAIGN_IDS):
        missing = set(ENABLED_CAMPAIGN_IDS) - set(enabled["campaign_id"].astype(str))
        print(f"⚠  Expected {len(ENABLED_CAMPAIGN_IDS)} campaigns, DB returned {len(enabled)}. "
              f"Missing: {missing}")
    enabled_names = set(enabled["campaign_name"].tolist())
    print(f"Resolved {len(enabled_names)} enabled campaigns:")
    for n in sorted(enabled_names):
        print(f"  • {n}")
    print()
    conn.close()

    # Load change events
    target_changes = load_target_changes(targets_csv, enabled_names)
    print(f"Loaded {len(target_changes)} CAMPAIGN-level Target ROAS change events from CSV")
    budget_changes = load_budget_changes(budgets_csv, enabled_names)
    print(f"Loaded {len(budget_changes)} budget change events for confounder check")
    print()

    # Apply exclude-pre filter at the event level if set
    pre_filter_excluded = []
    changes_to_process = []
    if exclude_pre is not None:
        for _, c in target_changes.iterrows():
            post_start = c["change_date"] + timedelta(days=STABILIZATION_LAG)
            if post_start < exclude_pre:
                pre_filter_excluded.append({
                    "campaign_name": c["campaign_name"],
                    "change_date": c["change_date"],
                    "old_target": c["old_target"],
                    "new_target": c["new_target"],
                    "direction": c["direction"],
                    "pct_change_target": c["pct_change_target"],
                    "pct_loosening": c["pct_loosening"],
                    "exclusion_reason": (
                        f"post-window starts {post_start}, before exclude-pre "
                        f"cutoff {exclude_pre}"
                    ),
                    "low_confidence_flags": [],
                })
            else:
                changes_to_process.append(c)
        print(f"Excluded {len(pre_filter_excluded)} events overlapping exclude-pre window")
    else:
        changes_to_process = [c for _, c in target_changes.iterrows()]
    print(f"Processing {len(changes_to_process)} events")
    print()

    # Connect to DB and process each event
    conn = sqlite3.connect(db_path)
    events = list(pre_filter_excluded)
    for c in changes_to_process:
        result = compute_event_elasticity(
            conn,
            campaign_name=c["campaign_name"],
            change_date=c["change_date"],
            event_ts=c["timestamp"],
            old_target=c["old_target"],
            new_target=c["new_target"],
            target_changes=target_changes,
            budget_changes=budget_changes,
            pre_days=args.pre_days,
            post_days=args.post_days,
            end_date_cutoff=end_date_cutoff,
        )
        events.append(result)
    conn.close()

    # Sort: included loosenings first (by pct_loosening desc), then included tightenings,
    # then excluded
    events.sort(key=lambda e: (
        e.get("exclusion_reason") is not None,
        e.get("direction") != "RAISE",
        -abs(e.get("pct_loosening", 0)),
    ))

    # Console summary
    n_used = sum(1 for e in events if e.get("exclusion_reason") is None
                 and e.get("empirical_elasticity") is not None)
    n_loosen = sum(1 for e in events if e.get("exclusion_reason") is None
                   and e.get("direction") == "RAISE")
    n_tighten = sum(1 for e in events if e.get("exclusion_reason") is None
                    and e.get("direction") == "CUT")
    print(f"Events with valid elasticity: {n_used} "
          f"({n_loosen} loosenings, {n_tighten} tightenings)")
    print()

    # Fit regressions
    regression_raise = weighted_regression(events, direction_filter="RAISE")
    regression_cut = weighted_regression(events, direction_filter="CUT")

    if "slope_with_intercept" in regression_raise:
        slope = regression_raise["slope_with_intercept"]
        ci_low = regression_raise.get("ci_low")
        ci_high = regression_raise.get("ci_high")
        r2 = regression_raise.get("r_squared", 0)
        print(f"┌─────────────────────────────────────────────────────────┐")
        print(f"│  EMPIRICAL ELASTICITY (loosenings): {slope:.2f}                   │")
        if ci_low is not None:
            print(f"│  95% CI: [{ci_low:.2f}, {ci_high:.2f}]                                │")
        print(f"│  R²: {r2:.2f}    n events: {regression_raise['n']}                          │")
        print(f"└─────────────────────────────────────────────────────────┘")
    else:
        print(f"⚠  Could not fit loosenings regression: {regression_raise.get('error')}")

    # Write workbook
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Headline")
    write_headline(ws, regression_raise, regression_cut, exclude_pre,
                   len(events), args.pre_days, args.post_days, STABILIZATION_LAG)

    ws = wb.create_sheet("Per-Event Detail")
    write_per_event(ws, events)

    # Charts
    conn = sqlite3.connect(db_path)
    print("Generating per-event time-series charts...")
    chart_count = 0
    for e in events:
        if e.get("exclusion_reason") is not None:
            continue
        # Short campaign name for sheet (Excel 31-char limit)
        camp_short = (e["campaign_name"]
                      .replace("HOME INSURANCE-", "")
                      .split("-NB-SEARCH")[0][:12])
        sheet_name = f"Chart_{camp_short}_{e['change_date'].strftime('%m%d')}"[:31]
        try:
            png_bytes = build_event_chart_png(
                conn=conn,
                campaign_name=e["campaign_name"],
                change_date=e["change_date"],
                old_target=e["old_target"],
                new_target=e["new_target"],
            )
            ws_chart = wb.create_sheet(sheet_name)
            img = XLImage(io.BytesIO(png_bytes))
            ws_chart.add_image(img, "A1")
            chart_count += 1
            print(f"  ✓ {sheet_name}")
        except Exception as exc:
            print(f"  ✗ {sheet_name}: chart failed ({exc})")
    print(f"Generated {chart_count} per-event chart sheets")

    # Per-campaign overview charts
    print()
    print("Generating per-campaign overview charts...")
    enabled_df = fetch_enabled_campaigns(conn)
    print(f"  Found {len(enabled_df)} enabled campaigns")

    changes_by_campaign: dict[str, list[dict]] = {}
    for _, c in target_changes.iterrows():
        cname = c["campaign_name"]
        changes_by_campaign.setdefault(cname, []).append({
            "change_date": c["change_date"],
            "old_target": c["old_target"],
            "new_target": c["new_target"],
        })

    overview_count = 0
    for _, camp in enabled_df.iterrows():
        cname = camp["campaign_name"]
        current_target = camp["current_target"]
        if pd.isna(current_target):
            current_target = None
        camp_short = (cname.replace("HOME INSURANCE-", "")
                      .split("-NB-SEARCH")[0][:18])
        sheet_name = f"Overview_{camp_short}"[:31]
        camp_events = changes_by_campaign.get(cname, [])
        try:
            png_bytes = build_campaign_overview_chart_png(
                conn=conn,
                campaign_name=cname,
                current_target=current_target,
                start_date=overview_start,
                end_date=end_date_cutoff,
                change_events=camp_events,
            )
            ws_chart = wb.create_sheet(sheet_name)
            img = XLImage(io.BytesIO(png_bytes))
            ws_chart.add_image(img, "A1")
            overview_count += 1
            n_events_str = f"{len(camp_events)} event(s)" if camp_events else "no events"
            print(f"  ✓ {sheet_name} ({n_events_str})")
        except Exception as exc:
            print(f"  ✗ {sheet_name}: chart failed ({exc})")
    print(f"Generated {overview_count} per-campaign overview chart sheets")

    conn.close()

    ws = wb.create_sheet("Diagnostics")
    write_diagnostics(ws, events, regression_raise, regression_cut,
                      exclude_pre, args.pre_days, args.post_days, STABILIZATION_LAG)

    out_path = output_dir / f"elasticity_evidence_google_{date.today().isoformat()}.xlsx"
    wb.save(out_path)
    print()
    print(f"  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
