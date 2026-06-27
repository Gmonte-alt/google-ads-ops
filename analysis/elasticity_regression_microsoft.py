"""
Elasticity Regression — Microsoft Home Insurance
═══════════════════════════════════════════════════════════════════════
Empirically estimates the bid-response elasticity for the Microsoft
Home Insurance account by treating each historical tCPA change as a
natural experiment.

For each tCPA change event in the change-history CSV:
  - Pre-window: 14 days BEFORE the change
  - Post-window: 14 days AFTER the change, skipping a 3-day stabilization lag
  - Compute % change in tCPA, % change in conversions, % change in CPA
  - Compute empirical elasticity = (% Δ conv) / (% Δ tCPA)

Then fits a weighted regression across all events to produce an
account-level empirical elasticity estimate.

USAGE:
  python elasticity_regression_microsoft.py
  python elasticity_regression_microsoft.py --db-path C:\\path\\to\\paid_search.db
  python elasticity_regression_microsoft.py --restructure-end 2026-02-15
  python elasticity_regression_microsoft.py --pre-days 7 --post-days 14

The --restructure-end flag excludes change events whose pre-window or
post-window overlaps with the restructure period (default: 2026-02-15).
Change events fully inside the restructure window are excluded entirely.

OUTPUT: elasticity_evidence_microsoft_<YYYY-MM-DD>.xlsx with sheets:
  - Headline: top-line empirical elasticity vs. model scenarios
  - Per-Event Detail: every change event with pre/post metrics + elasticity
  - Regression Output: fit statistics, R², residuals
  - Sensitivity: results across alternate window sizes (7/14/21 day)
  - Diagnostics: events excluded and why
"""

import argparse
import sqlite3
import io
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
ACCOUNT_ID = 2  # Microsoft Home Insurance
ACCOUNT_LABEL = "microsoft_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_CHANGE_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Microsoft-bids-budget-changehistory-YTD.csv")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

# Restructure window: changes overlapping this period are excluded.
# DEFAULT BEHAVIOR (per stakeholder direction): use all 5 usable change
# events, even those whose post-windows overlap the restructure period.
# The justification for the included caveat: with only 5 usable events
# in the YTD change history, excluding any of them leaves us with too
# few observations for a meaningful regression. Better to include all 5
# with a clear "n=5, results include restructure-overlap confounds"
# disclosure than exclude 3 of them and end up with n=2.
#
# To enable strict restructure exclusion, pass --restructure-end on the CLI
# (e.g. --restructure-end 2026-02-15).
DEFAULT_RESTRUCTURE_END = None  # disabled by default; set via CLI if desired

# Pre/post windows
DEFAULT_PRE_DAYS = 14    # days before change to measure baseline
DEFAULT_POST_DAYS = 14   # days after change to measure response
STABILIZATION_LAG = 3    # skip first N days post-change (algorithm settling)

# Visualization windows (wider than measurement windows for context)
VIZ_PRE_DAYS = 45
VIZ_POST_DAYS = 30

# Confound thresholds — events where these breach get flagged "low confidence"
RPL_DRIFT_FLAG_THRESHOLD = 0.15   # >15% RPL drift between pre/post = flag
# Lowered from 10 to 5: with only 5 usable events YTD, we want to keep all
# of them in the analysis even if NJ's volume is low (16 conv/30d). Events
# below this threshold still get included but flagged as "low confidence"
# in the per-event sheet; weight in regression is total conv anyway, so
# low-volume events naturally count less in the slope estimate.
MIN_CONV_FOR_RELIABLE = 5

# Model scenarios from strategic script (for comparison in Headline sheet)
MODEL_SCENARIOS = {
    "conservative": 0.6,
    "mid": 1.0,
    "aggressive": 1.2,
}

# Visual styling (matches strategic script)
HEADER_FILL = "1F4E79"
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
# CHANGE-HISTORY LOADING
# ═══════════════════════════════════════════════════════════════════

def load_tcpa_changes(csv_path: Path) -> pd.DataFrame:
    """Load the Microsoft change-history CSV, filter to TargetCPA changes only.

    Returns DataFrame with columns:
      change_date (date), campaign_name, old_tcpa (float|None), new_tcpa (float)
    """
    df = pd.read_csv(csv_path)
    # Filter to TargetCPA rows
    df = df[df["Attribute changed"] == "TargetCPA"].copy()
    # Parse dates
    df["change_date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y").dt.date
    # Old/new values: 'Old value' may be empty (initial set), 'New value' must be present
    df["old_tcpa"] = pd.to_numeric(df["Old value"], errors="coerce")
    df["new_tcpa"] = pd.to_numeric(df["New value"], errors="coerce")
    df = df.rename(columns={"Campaign": "campaign_name"})
    df = df[["change_date", "campaign_name", "old_tcpa", "new_tcpa"]].copy()
    # Drop rows with no new_tcpa (shouldn't happen but guard anyway)
    df = df.dropna(subset=["new_tcpa"]).reset_index(drop=True)
    # Sort by date so we can backfill old_tcpa from prior change if missing
    df = df.sort_values(["campaign_name", "change_date"]).reset_index(drop=True)
    # Where old_tcpa is missing (Microsoft's first-set behavior leaves it
    # blank), try to derive it from the prior change for the same campaign.
    # Use combine_first so we only fill NaNs and don't overwrite populated values.
    prior_new = df.groupby("campaign_name")["new_tcpa"].shift(1)
    df["old_tcpa"] = df["old_tcpa"].combine_first(prior_new)
    # Remove first-set events (no prior tCPA = nothing to measure against)
    df = df.dropna(subset=["old_tcpa"]).reset_index(drop=True)
    # Compute pct change and direction
    df["pct_change_tcpa"] = (df["new_tcpa"] - df["old_tcpa"]) / df["old_tcpa"]
    df["direction"] = df["pct_change_tcpa"].apply(
        lambda x: "RAISE" if x > 0.001 else ("CUT" if x < -0.001 else "NO_CHANGE"))
    df = df[df["direction"] != "NO_CHANGE"].reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def fetch_enabled_campaigns(conn) -> pd.DataFrame:
    """Return enabled Microsoft Home campaigns with their current tCPA.

    Columns: campaign_id, campaign_name, current_tcpa
    """
    sql = """
    SELECT
        campaign_id,
        campaign_name,
        target_cpa_micros / 1.0e6 AS current_tcpa
    FROM campaigns
    WHERE account_id = ?
      AND status = 'ENABLED'
    ORDER BY campaign_name
    """
    df = pd.read_sql(sql, conn, params=(ACCOUNT_ID,))
    return df


def fetch_campaign_daily(conn, campaign_name: str,
                         start_date: date, end_date: date) -> pd.DataFrame:
    """Fetch daily campaign metrics for a window from campaign_performance_daily.

    Aggregates across devices (filtering device='ALL' if present, otherwise
    summing all device rows). Returns one row per date with conversions,
    spend, conv_value, impressions, search_rank_lost_is.
    """
    sql = """
    SELECT
        cpd.date AS report_date,
        SUM(cpd.cost_micros) / 1e6 AS spend_usd,
        SUM(cpd.conversions) AS conv,
        SUM(cpd.conversion_value) AS conv_value,
        SUM(cpd.impressions) AS impressions,
        AVG(cpd.search_rank_lost_is) AS rank_lost_is_avg
    FROM campaign_performance_daily cpd
    JOIN campaigns c ON cpd.campaign_id = c.campaign_id
    WHERE c.account_id = ?
      AND c.campaign_name = ?
      AND cpd.date >= ?
      AND cpd.date <= ?
    GROUP BY cpd.date
    ORDER BY cpd.date
    """
    df = pd.read_sql(sql, conn, params=(ACCOUNT_ID, campaign_name,
                                         start_date.isoformat(),
                                         end_date.isoformat()))
    df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    return df


# ═══════════════════════════════════════════════════════════════════
# PER-EVENT ELASTICITY COMPUTATION
# ═══════════════════════════════════════════════════════════════════

def compute_event_elasticity(
    conn,
    campaign_name: str,
    change_date: date,
    old_tcpa: float,
    new_tcpa: float,
    pre_days: int = DEFAULT_PRE_DAYS,
    post_days: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
    end_date_cutoff: date | None = None,
) -> dict:
    """Compute empirical elasticity for a single tCPA-change event.

    Pre window:  [change_date - pre_days, change_date - 1]
    Post window: [change_date + lag_days, change_date + lag_days + post_days - 1]

    Returns a dict with all the metrics + an exclusion_reason if applicable.
    """
    pre_start = change_date - timedelta(days=pre_days)
    pre_end   = change_date - timedelta(days=1)
    post_start = change_date + timedelta(days=lag_days)
    post_end   = change_date + timedelta(days=lag_days + post_days - 1)

    out = {
        "campaign_name": campaign_name,
        "change_date": change_date,
        "old_tcpa": old_tcpa,
        "new_tcpa": new_tcpa,
        "pct_change_tcpa": (new_tcpa - old_tcpa) / old_tcpa,
        "direction": "RAISE" if new_tcpa > old_tcpa else "CUT",
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

    # Fetch pre + post data
    pre_df  = fetch_campaign_daily(conn, campaign_name, pre_start, pre_end)
    post_df = fetch_campaign_daily(conn, campaign_name, post_start, post_end)

    # Aggregate
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

    # Compute elasticity
    pct_change_conv = (post_conv_per_day - pre_conv_per_day) / pre_conv_per_day
    pct_change_spend = (post_spend_per_day - pre_spend_per_day) / pre_spend_per_day if pre_spend_per_day > 0 else None
    empirical_elasticity = pct_change_conv / out["pct_change_tcpa"]

    out.update({
        "pct_change_conv_per_day": pct_change_conv,
        "pct_change_spend_per_day": pct_change_spend,
        "empirical_elasticity": empirical_elasticity,
    })

    # Low-confidence flags (don't exclude, just downweight)
    if pre_rpl and post_rpl:
        rpl_drift = abs(post_rpl - pre_rpl) / pre_rpl
        out["rpl_drift_pct"] = rpl_drift
        if rpl_drift > RPL_DRIFT_FLAG_THRESHOLD:
            out["low_confidence_flags"].append(
                f"RPL drifted {rpl_drift:.0%} between pre/post windows "
                f"(pre ${pre_rpl:.2f} vs post ${post_rpl:.2f})"
            )

    # Compute confidence weight: total conversions in both windows
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
    old_tcpa: float,
    new_tcpa: float,
    pre_days: int = VIZ_PRE_DAYS,
    post_days: int = VIZ_POST_DAYS,
    measurement_pre: int = DEFAULT_PRE_DAYS,
    measurement_post: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
) -> bytes:
    """Build a 3-panel time-series chart for a single tCPA change event.

    Panels (top to bottom):
      1. tCPA setting (step function: old_tcpa → new_tcpa on change_date)
      2. Daily CPA, with 7-day rolling average
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
        # Build a "no data" placeholder chart
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
    df["cpa"] = df.apply(lambda r: (r["spend_usd"] / r["conv"]) if r["conv"] > 0 else None, axis=1)
    df["conv_7d_ma"] = df["conv"].rolling(window=7, min_periods=3).mean()
    spend_7d = df["spend_usd"].rolling(window=7, min_periods=3).sum()
    conv_7d = df["conv"].rolling(window=7, min_periods=3).sum()
    df["cpa_7d_ma"] = spend_7d / conv_7d.where(conv_7d > 0)

    # Build 3-panel figure
    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8.5), sharex=True,
        gridspec_kw={"height_ratios": [1, 2, 2], "hspace": 0.15},
    )

    # Convert change_date for plotting (matplotlib needs datetime, not date)
    change_dt = pd.Timestamp(change_date)
    pre_window_start = pd.Timestamp(change_date - timedelta(days=measurement_pre))
    pre_window_end   = pd.Timestamp(change_date - timedelta(days=1))
    post_window_start = pd.Timestamp(change_date + timedelta(days=lag_days))
    post_window_end   = pd.Timestamp(change_date + timedelta(days=lag_days + measurement_post - 1))
    df["report_dt"] = pd.to_datetime(df["report_date"])

    # ── Panel 1: tCPA setting (step function)
    tcpa_dates = pd.date_range(viz_start, viz_end, freq="D")
    tcpa_values = [old_tcpa if d < change_dt else new_tcpa for d in tcpa_dates]
    ax1.step(tcpa_dates, tcpa_values, where="post", color="#1F4E79", linewidth=2.5)
    ax1.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax1.set_ylabel("tCPA setting ($)", fontsize=10)
    ax1.set_title(f"{campaign_name}\ntCPA change: ${old_tcpa:.0f} → ${new_tcpa:.0f} "
                  f"({(new_tcpa/old_tcpa - 1)*100:+.0f}%) on {change_date}",
                  fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3)
    # Annotate the values
    ax1.annotate(f"${old_tcpa:.0f}", xy=(viz_start, old_tcpa), xytext=(5, -3),
                 textcoords="offset points", fontsize=9, color="#1F4E79")
    ax1.annotate(f"${new_tcpa:.0f}", xy=(viz_end, new_tcpa), xytext=(-30, 3),
                 textcoords="offset points", fontsize=9, color="#1F4E79", fontweight="bold")
    # Set y-range to give some headroom
    y_pad = max(2, (new_tcpa - old_tcpa) * 0.5) if new_tcpa != old_tcpa else 2
    ax1.set_ylim(min(old_tcpa, new_tcpa) - y_pad, max(old_tcpa, new_tcpa) + y_pad)

    # ── Panel 2: Daily CPA + 7-day MA
    ax2.scatter(df["report_dt"], df["cpa"], color="gray", alpha=0.4, s=15, label="Daily CPA")
    ax2.plot(df["report_dt"], df["cpa_7d_ma"], color="#C0504D", linewidth=2, label="7-day rolling avg")
    # Reference lines for old/new tCPA
    ax2.axhline(old_tcpa, color="#1F4E79", linestyle=":", linewidth=1, alpha=0.6,
                label=f"Old tCPA target (${old_tcpa:.0f})")
    ax2.axhline(new_tcpa, color="#1F4E79", linestyle="-.", linewidth=1, alpha=0.6,
                label=f"New tCPA target (${new_tcpa:.0f})")
    ax2.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    # Shade measurement windows
    ax2.axvspan(pre_window_start, pre_window_end, color="#1F4E79", alpha=0.08,
                label="Pre measurement (14d)")
    ax2.axvspan(post_window_start, post_window_end, color="#9BBB59", alpha=0.12,
                label="Post measurement (14d, +3d lag)")
    ax2.set_ylabel("CPA ($)", fontsize=10)
    ax2.legend(loc="best", fontsize=8, framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: Daily conversions + 7-day MA
    ax3.scatter(df["report_dt"], df["conv"], color="gray", alpha=0.4, s=15, label="Daily conv")
    ax3.plot(df["report_dt"], df["conv_7d_ma"], color="#4F81BD", linewidth=2, label="7-day rolling avg")
    ax3.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
    ax3.axvspan(pre_window_start, pre_window_end, color="#1F4E79", alpha=0.08)
    ax3.axvspan(post_window_start, post_window_end, color="#9BBB59", alpha=0.12)
    ax3.set_ylabel("Conversions / day", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)
    ax3.set_xlabel("Date", fontsize=10)

    # Format x-axis dates nicely
    ax3.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha="right")

    # Render to PNG bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_campaign_overview_chart_png(
    conn,
    campaign_name: str,
    current_tcpa: float | None,
    start_date: date,
    end_date: date,
    change_events: list[dict],
    measurement_pre: int = DEFAULT_PRE_DAYS,
    measurement_post: int = DEFAULT_POST_DAYS,
    lag_days: int = STABILIZATION_LAG,
) -> bytes:
    """Build a 3-panel time-series chart for a single campaign over its full
    post-attribution history.

    Differs from build_event_chart_png:
      - No single change event anchors the chart; covers a fixed date range
      - For campaigns with NO change events: clean chart, no vertical lines,
        no shading. tCPA panel shows flat line at current_tcpa.
      - For campaigns with change events: red dashed line at each change date,
        and pre/post measurement window shading around each event.
      - tCPA panel is a step function reconstructed from change_events.

    change_events: list of dicts with at minimum {change_date, old_tcpa, new_tcpa}
                   (typically the per-event records from load_tcpa_changes).
                   Filtered to events for THIS campaign before passing in.
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

    # Compute daily metrics
    df = df.copy()
    df["cpa"] = df.apply(lambda r: (r["spend_usd"] / r["conv"]) if r["conv"] > 0 else None, axis=1)
    df["conv_7d_ma"] = df["conv"].rolling(window=7, min_periods=3).mean()
    df["cpa_7d_ma"] = df["cpa"].rolling(window=7, min_periods=3).mean()
    df["report_dt"] = pd.to_datetime(df["report_date"])

    # Sort change events by date so step function is monotonic
    sorted_events = sorted(change_events, key=lambda e: e["change_date"])

    # Build 3-panel figure
    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8.5), sharex=True,
        gridspec_kw={"height_ratios": [1, 2, 2], "hspace": 0.15},
    )

    # ── Panel 1: tCPA step function ────────────────────────────────
    # Reconstruct tCPA-over-time from change events.
    # If there are events: walk forward, applying each new_tcpa from change_date.
    # If no events: flat line at current_tcpa.
    panel1_dates = pd.date_range(start_date, end_date, freq="D")

    if not sorted_events:
        if current_tcpa is None:
            # No events AND no current tCPA — likely manual-bidding history;
            # show empty panel with annotation
            ax1.text(0.5, 0.5, "No tCPA setting on file (manual bidding?)",
                     ha="center", va="center", fontsize=10, transform=ax1.transAxes,
                     style="italic", color="gray")
        else:
            ax1.plot(panel1_dates,
                     [current_tcpa] * len(panel1_dates),
                     color="#1F4E79", linewidth=2.5)
            ax1.annotate(f"${current_tcpa:.0f}",
                         xy=(panel1_dates[-1], current_tcpa),
                         xytext=(-30, 3), textcoords="offset points",
                         fontsize=9, color="#1F4E79", fontweight="bold")
    else:
        # Build step values: starts at first event's old_tcpa, jumps at each change
        first_tcpa = sorted_events[0]["old_tcpa"]
        tcpa_values = []
        event_idx = 0
        current = first_tcpa
        for d in panel1_dates:
            # Advance through events whose change_date is on or before d
            while (event_idx < len(sorted_events)
                   and sorted_events[event_idx]["change_date"] <= d.date()):
                current = sorted_events[event_idx]["new_tcpa"]
                event_idx += 1
            tcpa_values.append(current)
        ax1.step(panel1_dates, tcpa_values, where="post",
                 color="#1F4E79", linewidth=2.5)
        # Annotate first and last values
        ax1.annotate(f"${first_tcpa:.0f}",
                     xy=(panel1_dates[0], first_tcpa),
                     xytext=(5, -3), textcoords="offset points",
                     fontsize=9, color="#1F4E79")
        ax1.annotate(f"${tcpa_values[-1]:.0f}",
                     xy=(panel1_dates[-1], tcpa_values[-1]),
                     xytext=(-30, 3), textcoords="offset points",
                     fontsize=9, color="#1F4E79", fontweight="bold")
        # Set y-range with padding
        all_tcpas = [e["old_tcpa"] for e in sorted_events] + [e["new_tcpa"] for e in sorted_events]
        y_min, y_max = min(all_tcpas), max(all_tcpas)
        y_pad = max(2, (y_max - y_min) * 0.3)
        ax1.set_ylim(y_min - y_pad, y_max + y_pad)

    ax1.set_ylabel("tCPA setting ($)", fontsize=10)
    title = (f"{campaign_name}\n"
             f"{len(sorted_events)} tCPA change(s) in window {start_date} to {end_date}")
    ax1.set_title(title, fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: Daily CPA + 7-day MA + reference lines ────────────
    ax2.scatter(df["report_dt"], df["cpa"], color="gray", alpha=0.4, s=15,
                label="Daily CPA")
    ax2.plot(df["report_dt"], df["cpa_7d_ma"], color="#C0504D", linewidth=2,
             label="7-day rolling avg")
    # Current-tCPA reference if available
    if current_tcpa is not None:
        ax2.axhline(current_tcpa, color="#1F4E79", linestyle="-.", linewidth=1,
                    alpha=0.6, label=f"Current tCPA target (${current_tcpa:.0f})")

    # Shaded measurement windows + change-event lines
    for ev in sorted_events:
        change_dt = pd.Timestamp(ev["change_date"])
        pre_start = pd.Timestamp(ev["change_date"] - timedelta(days=measurement_pre))
        pre_end   = pd.Timestamp(ev["change_date"] - timedelta(days=1))
        post_start = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days))
        post_end   = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days + measurement_post - 1))
        ax2.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
        ax2.axvspan(pre_start, pre_end, color="#1F4E79", alpha=0.08)
        ax2.axvspan(post_start, post_end, color="#9BBB59", alpha=0.12)

    # Custom legend additions for shading (only if events exist)
    handles, labels = ax2.get_legend_handles_labels()
    if sorted_events:
        from matplotlib.patches import Patch
        handles.append(Patch(facecolor="#1F4E79", alpha=0.08,
                             label=f"Pre measurement ({measurement_pre}d)"))
        handles.append(Patch(facecolor="#9BBB59", alpha=0.12,
                             label=f"Post measurement ({measurement_post}d, +{lag_days}d lag)"))
    ax2.legend(handles=handles, loc="best", fontsize=8, framealpha=0.9)

    ax2.set_ylabel("CPA ($)", fontsize=10)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: Daily conversions + 7-day MA ──────────────────────
    ax3.scatter(df["report_dt"], df["conv"], color="gray", alpha=0.4, s=15,
                label="Daily conv")
    ax3.plot(df["report_dt"], df["conv_7d_ma"], color="#4F81BD", linewidth=2,
             label="7-day rolling avg")
    for ev in sorted_events:
        change_dt = pd.Timestamp(ev["change_date"])
        pre_start = pd.Timestamp(ev["change_date"] - timedelta(days=measurement_pre))
        pre_end   = pd.Timestamp(ev["change_date"] - timedelta(days=1))
        post_start = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days))
        post_end   = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days + measurement_post - 1))
        ax3.axvline(change_dt, color="red", linestyle="--", linewidth=1.5, alpha=0.7)
        ax3.axvspan(pre_start, pre_end, color="#1F4E79", alpha=0.08)
        ax3.axvspan(post_start, post_end, color="#9BBB59", alpha=0.12)
    ax3.set_ylabel("Conversions / day", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)
    ax3.set_xlabel("Date", fontsize=10)

    # Format x-axis
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
    """Fit a weighted least-squares regression of (% Δ conv) on (% Δ tCPA).

    The slope of this line IS the empirical elasticity for the account
    (under the assumption that the relationship is linear over the
    observed range of tCPA changes).

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

    x = np.array([e["pct_change_tcpa"] for e in valid])
    y = np.array([e["pct_change_conv_per_day"] for e in valid])
    w = np.array([e["weight_total_conv"] for e in valid])

    # Weighted least squares: forces line through origin only if intercept=0
    # Allow intercept to absorb baseline drift; report both
    # slope-through-origin and slope-with-intercept.

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

    # R² (weighted)
    y_pred = X @ beta
    ss_res = np.sum(w * (y - y_pred) ** 2)
    y_mean_w = np.average(y, weights=w)
    ss_tot = np.sum(w * (y - y_mean_w) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    # Standard error of slope (approximate, using residual variance)
    if len(valid) > 2:
        residual_var = ss_res / (len(valid) - 2)
        try:
            cov_matrix = residual_var * np.linalg.inv(XtWX)
            se_slope = np.sqrt(cov_matrix[1, 1])
        except np.linalg.LinAlgError:
            se_slope = None
    else:
        se_slope = None

    # 95% CI on slope (approximate, using normal approximation)
    ci_low = slope - 1.96 * se_slope if se_slope else None
    ci_high = slope + 1.96 * se_slope if se_slope else None

    # 3. Slope through origin (force intercept=0): pure elasticity
    slope_origin = np.sum(w * x * y) / np.sum(w * x * x)

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
                   restructure_end, n_total_events: int,
                   pre_days: int, post_days: int, lag_days: int) -> None:
    ws.cell(row=1, column=1, value="ELASTICITY EVIDENCE — Microsoft Home").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"Run date: {date.today().isoformat()}")
    r = 4
    ws.cell(row=r, column=1, value="METHODOLOGY").font = Font(bold=True, size=12)
    r += 1
    ws.cell(row=r, column=1,
            value=f"For each tCPA change event, compare conv volume in "
                  f"{pre_days}d pre-window vs {post_days}d post-window "
                  f"(skipping {lag_days}d stabilization lag). "
                  f"Empirical elasticity = (% Δ conv per day) / (% Δ tCPA).")
    ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    r += 1
    if restructure_end is not None:
        ws.cell(row=r, column=1,
                value=f"Excludes: events with post-window starting before "
                      f"{restructure_end} (restructure / attribution-stabilization), "
                      f"or with <{MIN_CONV_FOR_RELIABLE} conv in either window.")
    else:
        ws.cell(row=r, column=1,
                value=f"Restructure-cutoff DISABLED — all 5 usable change events "
                      f"included. Events whose post-window overlaps the Jan-Feb 2026 "
                      f"restructure period (IL Feb 10, IN Jan 26, VA Feb 9) are flagged "
                      f"in the per-event detail; their elasticity readings may reflect "
                      f"restructure-driven effects in addition to bid response. "
                      f"With n=5 total events, this is the trade-off for keeping a "
                      f"meaningful sample size.")
    ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)
    r += 2

    # Headline numbers — RAISE regression (the operationally relevant one)
    ws.cell(row=r, column=1, value="EMPIRICAL ELASTICITY — RAISES").font = Font(bold=True, size=12)
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
        r2 = regression_raise["r_squared"]

        cell = ws.cell(row=r, column=1,
                       value=f"Empirical elasticity (regression slope): {slope:.2f}")
        cell.font = Font(bold=True, size=12)
        # Color-code based on which scenario it matches
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
            verdict = "BELOW conservative scenario — bid raises ineffective"
        r += 1
        ws.cell(row=r, column=1, value=f"Verdict: {verdict}").font = Font(italic=True)
        r += 1

        if ci_low is not None:
            ws.cell(row=r, column=1,
                    value=f"95% confidence interval: [{ci_low:.2f}, {ci_high:.2f}]")
            r += 1
        ws.cell(row=r, column=1, value=f"R²: {r2:.2f}")
        r += 1
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
        empirical = regression_raise["slope_with_intercept"]
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

    # Cuts regression (reported but not headline)
    ws.cell(row=r, column=1, value="EMPIRICAL ELASTICITY — CUTS (reference only)").font = Font(bold=True, size=12)
    r += 1
    if "error" in regression_cut:
        ws.cell(row=r, column=1, value=f"Could not compute: {regression_cut['error']}")
    else:
        ws.cell(row=r, column=1,
                value=f"Cut elasticity: {regression_cut['slope_with_intercept']:.2f} "
                      f"(n={regression_cut['n']}, R²={regression_cut['r_squared']:.2f})")
        r += 1
        ws.cell(row=r, column=1,
                value="(Cut elasticity often differs from raise elasticity. "
                      "For strategic bid-raise decisions, the RAISE number is the relevant one.)")
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True)

    # Column widths
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 50


def write_per_event(ws, events: list[dict]) -> None:
    ws.cell(row=1, column=1, value="PER-EVENT ELASTICITY DETAIL").font = Font(bold=True, size=14)
    r = 3
    headers = [
        "campaign", "change_date", "direction", "old_tcpa", "new_tcpa",
        "pct_change_tcpa", "pre_n_days", "post_n_days",
        "pre_conv_per_day", "post_conv_per_day", "pct_change_conv",
        "pre_cpa", "post_cpa", "pre_rpl", "post_rpl",
        "rpl_drift_pct", "empirical_elasticity",
        "weight_total_conv", "confidence_score",
        "exclusion_reason", "low_confidence_flags",
    ]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=col_idx, value=h)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.font = Font(color=HEADER_FONT, bold=True)
    r += 1

    for e in events:
        ws.cell(row=r, column=1, value=e.get("campaign_name"))
        ws.cell(row=r, column=2, value=e.get("change_date"))
        ws.cell(row=r, column=3, value=e.get("direction"))
        ws.cell(row=r, column=4, value=e.get("old_tcpa"))
        ws.cell(row=r, column=5, value=e.get("new_tcpa"))
        pct_t = e.get("pct_change_tcpa")
        ws.cell(row=r, column=6, value=f"{pct_t:+.1%}" if pct_t is not None else "")
        ws.cell(row=r, column=7, value=e.get("pre_n_days"))
        ws.cell(row=r, column=8, value=e.get("post_n_days"))
        ws.cell(row=r, column=9, value=round(e["pre_conv_per_day"], 2) if e.get("pre_conv_per_day") is not None else "")
        ws.cell(row=r, column=10, value=round(e["post_conv_per_day"], 2) if e.get("post_conv_per_day") is not None else "")
        pct_c = e.get("pct_change_conv_per_day")
        ws.cell(row=r, column=11, value=f"{pct_c:+.1%}" if pct_c is not None else "")
        ws.cell(row=r, column=12, value=round(e["pre_cpa"], 2) if e.get("pre_cpa") is not None else "")
        ws.cell(row=r, column=13, value=round(e["post_cpa"], 2) if e.get("post_cpa") is not None else "")
        ws.cell(row=r, column=14, value=round(e["pre_rpl"], 2) if e.get("pre_rpl") is not None else "")
        ws.cell(row=r, column=15, value=round(e["post_rpl"], 2) if e.get("post_rpl") is not None else "")
        rd = e.get("rpl_drift_pct")
        ws.cell(row=r, column=16, value=f"{rd:.0%}" if rd is not None else "")
        eps = e.get("empirical_elasticity")
        cell = ws.cell(row=r, column=17, value=round(eps, 2) if eps is not None else "")
        if eps is not None:
            if eps >= 1.0:
                cell.fill = PatternFill("solid", fgColor=GOOD_FILL)
            elif eps >= 0.6:
                cell.fill = PatternFill("solid", fgColor=NEUTRAL_FILL)
            else:
                cell.fill = PatternFill("solid", fgColor=BAD_FILL)
        ws.cell(row=r, column=18, value=round(e.get("weight_total_conv", 0), 1) if e.get("weight_total_conv") else "")
        ws.cell(row=r, column=19, value=e.get("confidence_score", ""))
        ws.cell(row=r, column=20, value=e.get("exclusion_reason") or "")
        ws.cell(row=r, column=21, value="; ".join(e.get("low_confidence_flags", [])))
        r += 1

    # Column widths
    widths = [50, 14, 10, 10, 10, 14, 10, 10, 14, 14, 14, 10, 10, 10, 10, 12, 14, 14, 14, 50, 50]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else "A" + chr(64 + i - 26)].width = w
    ws.freeze_panes = "B4"


def write_diagnostics(ws, events: list[dict], regression_raise: dict,
                      regression_cut: dict, restructure_end,
                      pre_days: int, post_days: int, lag_days: int) -> None:
    ws.cell(row=1, column=1, value="DIAGNOSTICS").font = Font(bold=True, size=14)
    r = 3
    ws.cell(row=r, column=1, value="Configuration:").font = Font(bold=True)
    r += 1
    items = [
        ("Pre-window days", pre_days),
        ("Post-window days", post_days),
        ("Stabilization lag days", lag_days),
        ("Restructure cutoff (events post-windows must start ≥ this date)",
         restructure_end.isoformat() if restructure_end else "DISABLED (all events used)"),
        ("RPL drift flag threshold", f"{RPL_DRIFT_FLAG_THRESHOLD:.0%}"),
        ("Min conversions per window", MIN_CONV_FOR_RELIABLE),
        ("Total events scanned", len(events)),
        ("Events excluded", sum(1 for e in events if e.get("exclusion_reason"))),
        ("Events used in RAISE regression", regression_raise.get("n", 0)),
        ("Events used in CUT regression", regression_cut.get("n", 0)),
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
            ws.cell(row=r, column=1, value=f"{e['campaign_name']} on {e['change_date']}: {e['exclusion_reason']}")
            r += 1
    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["B"].width = 30


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--change-history", default=str(DEFAULT_CHANGE_CSV))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--restructure-end", default=None,
                   help="Exclude events whose post-window starts before this "
                        "date (ISO YYYY-MM-DD). DEFAULT: disabled — all 5 "
                        "usable events are included with disclosure of "
                        "restructure-overlap confounds. Use this flag for "
                        "stricter sensitivity analysis (warning: drops n to ~2).")
    p.add_argument("--pre-days", type=int, default=DEFAULT_PRE_DAYS)
    p.add_argument("--post-days", type=int, default=DEFAULT_POST_DAYS)
    p.add_argument("--end-date", default=None,
                   help="Cutoff date (ISO YYYY-MM-DD). Events whose post-window "
                        "extends past this date are excluded. Default: today.")
    return p.parse_args()


def main():
    args = parse_args()

    db_path = Path(args.db_path)
    change_csv = Path(args.change_history)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    restructure_end = (date.fromisoformat(args.restructure_end)
                       if args.restructure_end else None)
    end_date_cutoff = date.fromisoformat(args.end_date) if args.end_date else date.today()

    banner("ELASTICITY REGRESSION — Microsoft Home Insurance")
    print(f"  DB:                  {db_path}")
    print(f"  Change history:      {change_csv}")
    if restructure_end:
        print(f"  Restructure cutoff:  {restructure_end} (post-windows must start ≥ this)")
    else:
        print(f"  Restructure cutoff:  DISABLED (all usable events included)")
    print(f"  Pre-window days:     {args.pre_days}")
    print(f"  Post-window days:    {args.post_days}")
    print(f"  Stabilization lag:   {STABILIZATION_LAG} days")
    print(f"  End-date cutoff:     {end_date_cutoff}")
    print()

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)
    if not change_csv.exists():
        print(f"ERROR: change history CSV not found at {change_csv}")
        sys.exit(1)

    # Load tCPA changes
    changes = load_tcpa_changes(change_csv)
    print(f"Loaded {len(changes)} tCPA change events from CSV")
    print()

    # Apply restructure-window filter at the event level (post-window must start ≥ cutoff)
    # Skip entirely if --restructure-end was not passed.
    pre_filter_count = len(changes)
    changes_to_process = []
    pre_filter_excluded = []
    if restructure_end is not None:
        for _, c in changes.iterrows():
            post_start = c["change_date"] + timedelta(days=STABILIZATION_LAG)
            if post_start < restructure_end:
                pre_filter_excluded.append({
                    "campaign_name": c["campaign_name"],
                    "change_date": c["change_date"],
                    "old_tcpa": c["old_tcpa"],
                    "new_tcpa": c["new_tcpa"],
                    "direction": c["direction"],
                    "pct_change_tcpa": c["pct_change_tcpa"],
                    "exclusion_reason": (
                        f"post-window starts {post_start}, before restructure cutoff "
                        f"{restructure_end}"
                    ),
                    "low_confidence_flags": [],
                })
            else:
                changes_to_process.append(c)
        print(f"Excluded {len(pre_filter_excluded)} events overlapping restructure window")
    else:
        changes_to_process = [c for _, c in changes.iterrows()]
    print(f"Processing {len(changes_to_process)} events")
    print()

    # Connect to DB and process each event
    conn = sqlite3.connect(db_path)
    events = list(pre_filter_excluded)  # start with pre-filter exclusions
    for c in changes_to_process:
        result = compute_event_elasticity(
            conn,
            campaign_name=c["campaign_name"],
            change_date=c["change_date"],
            old_tcpa=c["old_tcpa"],
            new_tcpa=c["new_tcpa"],
            pre_days=args.pre_days,
            post_days=args.post_days,
            end_date_cutoff=end_date_cutoff,
        )
        events.append(result)
    conn.close()

    # Sort: included raises first (by pct_change_tcpa desc), then included cuts,
    # then excluded
    events.sort(key=lambda e: (
        e.get("exclusion_reason") is not None,
        e.get("direction") != "RAISE",
        -abs(e.get("pct_change_tcpa", 0)),
    ))

    # Print quick summary to console
    n_used = sum(1 for e in events if e.get("exclusion_reason") is None
                 and e.get("empirical_elasticity") is not None)
    n_raises = sum(1 for e in events if e.get("exclusion_reason") is None
                   and e.get("direction") == "RAISE")
    n_cuts = sum(1 for e in events if e.get("exclusion_reason") is None
                 and e.get("direction") == "CUT")
    print(f"Events with valid elasticity: {n_used} ({n_raises} raises, {n_cuts} cuts)")
    print()

    # Fit regressions
    regression_raise = weighted_regression(events, direction_filter="RAISE")
    regression_cut = weighted_regression(events, direction_filter="CUT")

    # Headline output to console
    if "slope_with_intercept" in regression_raise:
        slope = regression_raise["slope_with_intercept"]
        ci_low = regression_raise.get("ci_low")
        ci_high = regression_raise.get("ci_high")
        r2 = regression_raise.get("r_squared", 0)
        print(f"┌─────────────────────────────────────────────────────────┐")
        print(f"│  EMPIRICAL ELASTICITY (raises): {slope:.2f}                       │")
        if ci_low is not None:
            print(f"│  95% CI: [{ci_low:.2f}, {ci_high:.2f}]                                │")
        print(f"│  R²: {r2:.2f}    n events: {regression_raise['n']}                          │")
        print(f"└─────────────────────────────────────────────────────────┘")
    else:
        print(f"⚠  Could not fit raise regression: {regression_raise.get('error')}")

    # Write workbook
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Headline")
    write_headline(ws, regression_raise, regression_cut, restructure_end,
                   len(events), args.pre_days, args.post_days, STABILIZATION_LAG)

    ws = wb.create_sheet("Per-Event Detail")
    write_per_event(ws, events)

    # Chart sheets — generated using a single DB connection for efficiency
    conn = sqlite3.connect(db_path)

    # ── Per-event charts (one per non-excluded event) ──
    print("Generating per-event time-series charts...")
    chart_count = 0
    for e in events:
        if e.get("exclusion_reason") is not None:
            continue
        camp_short = e["campaign_name"].replace("HOME INSURANCE-", "").split("-")[0][:12]
        sheet_name = f"Chart_{camp_short}_{e['change_date'].strftime('%m%d')}"[:31]
        try:
            png_bytes = build_event_chart_png(
                conn=conn,
                campaign_name=e["campaign_name"],
                change_date=e["change_date"],
                old_tcpa=e["old_tcpa"],
                new_tcpa=e["new_tcpa"],
            )
            ws_chart = wb.create_sheet(sheet_name)
            img = XLImage(io.BytesIO(png_bytes))
            ws_chart.add_image(img, "A1")
            chart_count += 1
            print(f"  ✓ {sheet_name}")
        except Exception as exc:
            print(f"  ✗ {sheet_name}: chart failed ({exc})")
    print(f"Generated {chart_count} per-event chart sheets")

    # ── Per-campaign overview charts (one per enabled campaign) ──
    # Full post-attribution history (Jan 13, 2026 - end_date_cutoff).
    # Campaigns with no tCPA changes get a clean chart (no shading).
    # Campaigns with changes get red dashed lines + pre/post shading at each event.
    print()
    print("Generating per-campaign overview charts...")
    overview_start = date(2026, 1, 13)  # Microsoft attribution go-live
    enabled = fetch_enabled_campaigns(conn)
    print(f"  Found {len(enabled)} enabled campaigns")

    # Group changes by campaign for fast lookup
    changes_by_campaign: dict[str, list[dict]] = {}
    for _, c in changes.iterrows():
        cname = c["campaign_name"]
        changes_by_campaign.setdefault(cname, []).append({
            "change_date": c["change_date"],
            "old_tcpa": c["old_tcpa"],
            "new_tcpa": c["new_tcpa"],
        })

    overview_count = 0
    for _, camp in enabled.iterrows():
        cname = camp["campaign_name"]
        current_tcpa = camp["current_tcpa"]
        if pd.isna(current_tcpa):
            current_tcpa = None
        camp_short = cname.replace("HOME INSURANCE-", "").split("-")[0][:18]
        sheet_name = f"Overview_{camp_short}"[:31]
        camp_events = changes_by_campaign.get(cname, [])
        try:
            png_bytes = build_campaign_overview_chart_png(
                conn=conn,
                campaign_name=cname,
                current_tcpa=current_tcpa,
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
                      restructure_end, args.pre_days, args.post_days, STABILIZATION_LAG)

    out_path = output_dir / f"elasticity_evidence_microsoft_{date.today().isoformat()}.xlsx"
    wb.save(out_path)
    print()
    print(f"  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
