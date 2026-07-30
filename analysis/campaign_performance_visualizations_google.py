"""
Campaign Performance Visualizations — Google Home Insurance
═══════════════════════════════════════════════════════════════════════
Generates a per-campaign weekly performance workbook covering the last
12 weeks of operating equilibrium. Designed to answer the leadership-
side question: "Why isn't the model finding more headroom?" — by
showing each campaign's recent trajectory on conversions, ROAS, CPA,
RPL, and profit. Each metric is plotted weekly so the eye picks up
trends without being distracted by daily noise.

PANEL LAYOUT (per campaign sheet, top to bottom):
  Panel 1: weekly conversions (bar chart)
  Panel 2: weekly ROAS + current tROAS reference line
  Panel 3: weekly CPA
  Panel 4: weekly RPL
  Panel 5: weekly profit (color-coded green/red bars)

Plus vertical lines at any change events within the window:
  - Red dashed: Target ROAS changes
  - Orange dotted: Budget changes

USAGE:
  python campaign_performance_visualizations_google.py
  python campaign_performance_visualizations_google.py --end-date 2026-04-24
  python campaign_performance_visualizations_google.py --weeks 16
  python campaign_performance_visualizations_google.py --elasticity-start 2026-03-01

END-DATE BEHAVIOR:
  By default the weekly window ends at the LAST COMPLETE Mon-Sun week
  available in the database — partial weeks aren't shown. Pass
  --end-date to override (still snapped back to that week's Sunday).
  The elasticity chart is independent and always runs to the latest
  available date in the DB, regardless of the weekly cutoff.

OUTPUT: campaign_performance_google_<YYYY-MM-DD>.xlsx
  - Overview: large total-account weekly-profit chart (left) + the
    per-campaign small-multiples grid (right), sorted by 12-week profit
  - Total Account: 5-panel weekly chart (left) + 3-panel elasticity
    chart (right), both at the account level
  - <CAMPAIGN_SHORT> sheets: 5-panel weekly chart (left) + 3-panel
    elasticity chart (right), one sheet per campaign
"""

import argparse
import io
import sqlite3
import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.drawing.image import Image as XLImage

# ── CONFIG ─────────────────────────────────────────────────────────
ACCOUNT_ID = 1  # Google Home Insurance
ACCOUNT_LABEL = "google_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_TARGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report.csv")
DEFAULT_BUDGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report__1_.csv")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE
    "23394808297",   # HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE
    "23394808300",   # HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE
    "23428389081",   # HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
    "23496334752",   # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
    "23530860942",   # HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE
    "23586637678",   # HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE
]

DEFAULT_WEEKS = 12

# Backfill buffer: number of days back from "today" to use as the default
# analysis end date. Google Ads offline-routed primary conversions can lag
# the click date by 1-2 days; running the chart against truly-yesterday
# data sometimes shows a phantom dip in the most recent week purely from
# unsettled attribution. The default of 1 day matches "yesterday"; bump
# to 2 if you regularly see suspicious recent-week dips. Override entirely
# with --end-date for reproducible historical runs.
DEFAULT_BACKFILL_BUFFER_DAYS = 1


def default_end_date() -> date:
    """Compute the default analysis end date dynamically. Equals
    today − DEFAULT_BACKFILL_BUFFER_DAYS, so a routine run on Monday
    May 4 with buffer=1 yields end_date = May 3 (Sunday) and the
    chart's most recent week is the just-completed Mon–Sun week.
    """
    return date.today() - timedelta(days=DEFAULT_BACKFILL_BUFFER_DAYS)


YEAR_START = date(2026, 1, 1)

# ── ELASTICITY CHART CONSTANTS ─────────────────────────────────────
# These define the pre/post measurement windows shaded around each
# tROAS change event on the elasticity chart. They must match the
# values in elasticity_regression_google.py — that script is the
# source of truth on this analysis design. If you change these here,
# update the elasticity script too, or the visual story will differ
# from the regression's actual measurement windows.
ELASTICITY_PRE_DAYS = 14    # days before change measured as "pre"
ELASTICITY_POST_DAYS = 14   # days after change measured as "post"
ELASTICITY_LAG_DAYS = 3     # days skipped after change (algorithm settling)


def banner(text: str) -> None:
    print("═" * 70)
    print(text)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def fetch_enabled_campaigns(conn) -> pd.DataFrame:
    """Enabled campaigns + their current tROAS. Filters to the 7
    NB-SEARCH-ALL-USA-STE campaigns from efficiency_frontier scope."""
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


def fetch_latest_data_date(conn) -> date | None:
    """Return MAX(date) from campaign_performance_daily_search for this account.

    This is the most recent date we have spend data for, regardless of
    whether the week containing it is complete. Used to anchor the
    elasticity charts (which want as much history as is available, not
    the conservative complete-week cutoff used for weekly aggregations).

    Returns None if no rows exist for the account.
    """
    row = conn.execute(
        """
        SELECT MAX(cpd.date)
        FROM campaign_performance_daily_search cpd
        JOIN campaigns c ON cpd.campaign_id = c.campaign_id
        WHERE c.account_id = ?
        """,
        (ACCOUNT_ID,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return date.fromisoformat(row[0])


def fetch_latest_complete_week(conn) -> tuple[date, date]:
    """Determine the most recent complete Mon-Sun week available in
    campaign_performance_daily_search for this account.

    Logic: find MAX(date) for the account, then return the Mon-Sun week
    that is fully covered. If the latest date IS a Sunday, that week is
    complete. Otherwise, step back to the prior Sunday and use the week
    ending there. No backfill buffer — if a Sunday is loaded, that week
    counts as complete.

    Returns (week_start_monday, week_end_sunday). Raises RuntimeError if
    no data exists for the account.
    """
    latest = fetch_latest_data_date(conn)
    if latest is None:
        raise RuntimeError(
            f"No rows found in campaign_performance_daily_search for account_id={ACCOUNT_ID}"
        )
    # weekday(): Mon=0 ... Sun=6. A complete week ends on Sunday (weekday=6).
    weekday = latest.weekday()
    if weekday == 6:
        week_end = latest
    else:
        days_back = weekday + 1  # Mon→1, Tue→2, ..., Sat→6
        week_end = latest - timedelta(days=days_back)
    week_start = week_end - timedelta(days=6)
    return week_start, week_end


def fetch_campaign_daily(conn, campaign_name: str,
                         start_date: date, end_date: date) -> pd.DataFrame:
    """Daily campaign metrics. Same dual-source pattern as the elasticity
    script and efficiency_frontier:
      • Spend from campaign_performance_daily_search (device='ALL' to skip the
        per-device breakdown rows, which would otherwise double-count).
      • Conversions from keyword_performance_daily aggregated to the
        campaign × date grain (device='ALL', geo_state='ALL'), summing
        primary_conv_count and primary_conv_value — the offline-routed
        source of truth for Google Home.

    Returns columns: report_date, spend_usd, conv, conv_value.
    """
    sql_cpd = """
    SELECT
        cpd.date AS report_date,
        SUM(cpd.cost_micros) / 1e6 AS spend_usd
    FROM campaign_performance_daily_search cpd
    JOIN campaigns c ON cpd.campaign_id = c.campaign_id
    WHERE c.account_id = ?
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
# WEEKLY AGGREGATION
# ═══════════════════════════════════════════════════════════════════

def aggregate_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Roll up daily data to ISO weeks (Mon-Sun).

    Returns columns: week_start (Mon date), week_end (Sun date),
    spend, conv, conv_value, cpa, rpl, roas, profit.

    CPA, RPL, and ROAS are weighted ratios (sum-over-sum, NOT averages
    of daily values) — same convention as the Microsoft script and
    efficiency_frontier.
    """
    if daily_df.empty:
        return pd.DataFrame()

    df = daily_df.copy()
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["week_start"] = df["report_date"] - pd.to_timedelta(
        df["report_date"].dt.weekday, unit="D"
    )
    df["week_start"] = df["week_start"].dt.date

    agg = df.groupby("week_start").agg(
        spend=("spend_usd", "sum"),
        conv=("conv", "sum"),
        conv_value=("conv_value", "sum"),
    ).reset_index()

    agg["week_end"] = agg["week_start"].apply(lambda d: d + timedelta(days=6))
    agg["cpa"] = agg.apply(lambda r: r["spend"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["rpl"] = agg.apply(lambda r: r["conv_value"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["roas"] = agg.apply(lambda r: r["conv_value"] / r["spend"] if r["spend"] > 0 else None, axis=1)
    agg["profit"] = agg["conv_value"] - agg["spend"]

    return agg


# ═══════════════════════════════════════════════════════════════════
# CHANGE-HISTORY LOADER (Google CSV format)
# ═══════════════════════════════════════════════════════════════════

def _parse_gads_datetime(s: str) -> datetime:
    """Google Ads CSV exports: 'Mar 30, 2026, 9:31:59 AM'."""
    return datetime.strptime(s.strip(), "%b %d, %Y, %I:%M:%S %p")


def _normalize_target_value(raw) -> float | None:
    """116% → 1.16, 1.16 → 1.16, 113 → 1.13, '' → None."""
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


def load_change_events(targets_csv: Path, budgets_csv: Path,
                       enabled_campaign_names: set[str],
                       start_date: date, end_date: date) -> pd.DataFrame:
    """Load tROAS + budget change events from the Google Ads change
    history CSVs, filtered to the visualization window AND tracked
    campaigns. Used to overlay vertical change-event lines on the
    per-campaign charts.

    Returns DataFrame with columns:
      change_date, campaign_name, attribute (TargetROAS|Budget),
      old_value, new_value
    """
    rows = []

    # ── Target ROAS changes (CAMPAIGN-level only) ──
    if targets_csv.exists():
        with open(targets_csv, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        reader = csv.DictReader(lines[2:] if len(lines) > 2 else lines)
        for r in reader:
            ch = r.get("Changes", "") or ""
            if "Target ROAS" not in ch:
                continue
            try:
                ts = _parse_gads_datetime(r["Date & time"])
            except (ValueError, KeyError):
                continue
            cd = ts.date()
            if cd < start_date or cd > end_date:
                continue
            scope = "AG" if (r.get("Ad group") or "").strip() else "CAMPAIGN"
            if scope != "CAMPAIGN":
                continue
            cname = r.get("Campaign", "") or ""
            if cname not in enabled_campaign_names:
                continue
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
            rows.append({
                "change_date": cd,
                "campaign_name": cname,
                "attribute": "TargetROAS",
                "old_value": old,
                "new_value": new,
            })
    else:
        print(f"  ℹ  Targets CSV not found at {targets_csv} — no target overlays")

    # ── Budget changes ──
    if budgets_csv.exists():
        with open(budgets_csv, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        reader = csv.DictReader(lines[2:] if len(lines) > 2 else lines)
        for r in reader:
            ch = r.get("Changes", "") or ""
            if "Budget amount" not in ch:
                continue
            try:
                ts = _parse_gads_datetime(r["Date & time"])
            except (ValueError, KeyError):
                continue
            cd = ts.date()
            if cd < start_date or cd > end_date:
                continue
            cname = r.get("Campaign", "") or ""
            if cname not in enabled_campaign_names:
                continue
            m = re.search(r"from \$([\d,]+\.\d{2}) to \$([\d,]+\.\d{2})", ch)
            if m:
                old = float(m.group(1).replace(",", ""))
                new = float(m.group(2).replace(",", ""))
            else:
                old, new = None, None
            rows.append({
                "change_date": cd,
                "campaign_name": cname,
                "attribute": "Budget",
                "old_value": old,
                "new_value": new,
            })
    else:
        print(f"  ℹ  Budgets CSV not found at {budgets_csv} — no budget overlays")

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ═══════════════════════════════════════════════════════════════════

def build_5panel_campaign_chart_png(
    weekly_df: pd.DataFrame,
    campaign_name: str,
    current_target: float | None,
    change_events: list[dict],
    window_start: date,
    window_end: date,
) -> bytes:
    """5-panel weekly chart for one campaign:
       Panel 1 (top):    weekly conversions (bar chart)
       Panel 2:          weekly ROAS + current tROAS reference line
       Panel 3:          weekly CPA
       Panel 4:          weekly RPL
       Panel 5 (bottom): weekly profit (bar chart, color-coded green/red)

    Plus vertical lines at any change events within the window
    (red dashed for Target ROAS, orange dotted for Budget).
    """
    if weekly_df.empty:
        fig, ax = plt.subplots(figsize=(11, 11))
        ax.text(0.5, 0.5, f"No data available for\n{campaign_name}\n"
                          f"between {window_start} and {window_end}",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    df = weekly_df.copy()
    df["week_dt"] = pd.to_datetime(df["week_start"])
    bar_width = 6  # days; leaves a 1-day gap between weekly bars

    fig, (ax1, ax2, ax3, ax4, ax5) = plt.subplots(
        5, 1, figsize=(11, 12), sharex=True,
        gridspec_kw={"hspace": 0.18},
    )

    # ── Panel 1: weekly conversions ────────────────────────────────
    ax1.bar(df["week_dt"], df["conv"], width=bar_width,
            color="#4F81BD", alpha=0.75, edgecolor="#1F4E79", linewidth=0.5)
    for _, row in df.iterrows():
        if row["conv"] > 0:
            ax1.annotate(f"{int(round(row['conv']))}",
                         xy=(row["week_dt"], row["conv"]),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", fontsize=8, color="#1F4E79")
    ax1.set_ylabel("Weekly conversions", fontsize=10)
    ax1.set_title(f"{campaign_name}\n"
                  f"12-week performance: {window_start} to {window_end}",
                  fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3, axis="y")

    # ── Panel 2: weekly ROAS + tROAS reference ─────────────────────
    ax2.plot(df["week_dt"], df["roas"], marker="o", color="#7030A0",
             linewidth=2, markersize=7, label="Weekly ROAS")
    if current_target is not None:
        ax2.axhline(current_target, color="#1F4E79", linestyle="-.", linewidth=1.5,
                    alpha=0.7, label=f"Current tROAS target ({current_target:.2f})")
    for _, row in df.iterrows():
        if pd.notna(row["roas"]):
            ax2.annotate(f"{row['roas']:.2f}",
                         xy=(row["week_dt"], row["roas"]),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=8, color="#7030A0")
    ax2.set_ylabel("Weekly ROAS", fontsize=10)
    ax2.legend(loc="best", fontsize=8, framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    # ── Panel 3: weekly CPA ────────────────────────────────────────
    ax3.plot(df["week_dt"], df["cpa"], marker="o", color="#C0504D",
             linewidth=2, markersize=7, label="Weekly CPA")
    for _, row in df.iterrows():
        if pd.notna(row["cpa"]):
            ax3.annotate(f"${row['cpa']:.0f}",
                         xy=(row["week_dt"], row["cpa"]),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=8, color="#C0504D")
    ax3.set_ylabel("Weekly CPA ($)", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)

    # ── Panel 4: weekly RPL ────────────────────────────────────────
    ax4.plot(df["week_dt"], df["rpl"], marker="s", color="#9BBB59",
             linewidth=2, markersize=7, label="Weekly RPL")
    for _, row in df.iterrows():
        if pd.notna(row["rpl"]):
            ax4.annotate(f"${row['rpl']:.0f}",
                         xy=(row["week_dt"], row["rpl"]),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=8, color="#5F7F2F")
    ax4.set_ylabel("Weekly RPL ($)", fontsize=10)
    ax4.legend(loc="best", fontsize=8, framealpha=0.9)
    ax4.grid(True, alpha=0.3)

    # ── Panel 5: weekly profit ─────────────────────────────────────
    profit_colors = ["#9BBB59" if p >= 0 else "#C0504D" for p in df["profit"]]
    ax5.bar(df["week_dt"], df["profit"], width=bar_width,
            color=profit_colors, alpha=0.75, edgecolor="#404040", linewidth=0.5)
    ax5.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    for _, row in df.iterrows():
        if pd.notna(row["profit"]):
            offset = 8 if row["profit"] >= 0 else -12
            ax5.annotate(f"${row['profit']:,.0f}",
                         xy=(row["week_dt"], row["profit"]),
                         xytext=(0, offset), textcoords="offset points",
                         ha="center", fontsize=8,
                         color="#5F7F2F" if row["profit"] >= 0 else "#8B0000")
    ax5.set_ylabel("Weekly profit ($)", fontsize=10)
    ax5.set_xlabel("Week starting (Mon)", fontsize=10)
    ax5.grid(True, alpha=0.3, axis="y")

    # Overlay change-event markers on all panels
    for ev in change_events:
        change_dt = pd.Timestamp(ev["change_date"])
        attr = str(ev.get("attribute", ""))
        if "TargetROAS" in attr:
            color, ls = "red", "--"
        elif "Budget" in attr:
            color, ls = "#FF8C00", ":"  # orange dotted for budget
        else:
            color, ls = "gray", "--"
        for ax in (ax1, ax2, ax3, ax4, ax5):
            ax.axvline(change_dt, color=color, linestyle=ls, linewidth=1.2, alpha=0.6)

    ax5.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))  # Mondays
    ax5.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax5.xaxis.get_majorticklabels(), rotation=30, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_overview_small_multiples_png(
    campaign_weekly: dict[str, pd.DataFrame],
    window_start: date,
    window_end: date,
) -> bytes:
    """One sheet showing all enabled campaigns' weekly profit as small
    multiples. Layout: up to 4 columns, rows as needed. Each panel is a
    single campaign's weekly profit bar chart with independent y-axis
    (profit magnitudes differ enough that shared scale would make small
    campaigns invisible). Each panel labels its total window profit.
    """
    n_campaigns = len(campaign_weekly)
    if n_campaigns == 0:
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.text(0.5, 0.5, "No campaign data available",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    n_cols = min(4, n_campaigns)
    n_rows = max(1, (n_campaigns + n_cols - 1) // n_cols)

    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(16, 3.2 * n_rows),
        sharex=True, gridspec_kw={"hspace": 0.55, "wspace": 0.25},
    )
    if n_rows * n_cols == 1:
        axes = [axes]
    else:
        axes = axes.flatten() if hasattr(axes, "flatten") else list(axes)

    # Sort campaigns by total window profit descending so biggest are top-left
    sorted_items = sorted(
        campaign_weekly.items(),
        key=lambda kv: -(kv[1]["profit"].sum() if not kv[1].empty else 0),
    )

    for idx, (campaign_name, weekly_df) in enumerate(sorted_items):
        ax = axes[idx]
        if weekly_df.empty:
            ax.text(0.5, 0.5, "(no data)", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="gray")
            ax.set_axis_off()
            continue

        weeks = pd.to_datetime(weekly_df["week_start"])
        profits = weekly_df["profit"].values
        colors = ["#9BBB59" if p >= 0 else "#C0504D" for p in profits]
        ax.bar(weeks, profits, width=6, color=colors, alpha=0.8,
               edgecolor="#404040", linewidth=0.4)
        ax.axhline(0, color="black", linewidth=0.5, alpha=0.5)

        # Title: short campaign name + total profit
        short_name = (campaign_name
                      .replace("HOME INSURANCE-", "")
                      .split("-NB-SEARCH")[0])
        total_profit = profits.sum()
        ax.set_title(f"{short_name}\n${total_profit:,.0f} total",
                     fontsize=9, fontweight="bold", loc="left")
        ax.tick_params(axis="x", labelsize=7)
        ax.tick_params(axis="y", labelsize=7)
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        ax.grid(True, alpha=0.25, axis="y")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"${v:,.0f}"))

    # Hide unused subplots
    for idx in range(len(sorted_items), len(axes)):
        axes[idx].set_axis_off()

    fig.suptitle(
        f"Weekly Profit by Campaign — {window_start} to {window_end} "
        f"(sorted by total window profit)",
        fontsize=13, fontweight="bold", y=0.995,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# TOTAL-ACCOUNT AGGREGATION & CHART
# ═══════════════════════════════════════════════════════════════════

def aggregate_total_account_daily(
    campaign_daily_map: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Sum daily spend, conversions, and revenue across all campaigns
    to produce account-level daily totals.

    Input: dict of campaign_name → daily DataFrame (the per-campaign
    output of fetch_campaign_daily — columns: report_date, spend_usd,
    conv, conv_value).

    Output: single DataFrame with one row per date and the same
    columns. Used as input to aggregate_weekly() for the Total Account
    sheet and the Overview profit chart.

    Notes on aggregation correctness:
    - spend, conv, conv_value are all additive across campaigns at the
      daily grain (no cross-campaign attribution to worry about).
    - CPA, ROAS, RPL must be computed AFTER summing — never average
      per-campaign rates. aggregate_weekly() handles this correctly.
    """
    if not campaign_daily_map:
        return pd.DataFrame(columns=["report_date", "spend_usd", "conv", "conv_value"])

    frames = []
    for cname, df in campaign_daily_map.items():
        if df is None or df.empty:
            continue
        frames.append(df[["report_date", "spend_usd", "conv", "conv_value"]])

    if not frames:
        return pd.DataFrame(columns=["report_date", "spend_usd", "conv", "conv_value"])

    combined = pd.concat(frames, ignore_index=True)
    totals = combined.groupby("report_date", as_index=False).sum(numeric_only=True)
    return totals


def compute_blended_roas(campaign_daily_map: dict[str, pd.DataFrame]) -> float | None:
    """Blended ROAS = Σ conv_value ÷ Σ spend across all campaigns over
    the full window. This is the realized account-level ROAS, weighted
    by where spend actually went; used as the reference line on the
    Total Account ROAS panel.

    This is analogous to the Microsoft compute_blended_tcpa (which is
    Σspend ÷ Σconv). On tROAS strategy the natural blended reference
    is the realized ROAS rather than CPA — and it puts the reference
    on the same panel that has the per-campaign tROAS reference,
    keeping the visual story consistent across sheets.

    Returns None if total spend is 0.
    """
    total_spend = 0.0
    total_revenue = 0.0
    for df in campaign_daily_map.values():
        if df is None or df.empty:
            continue
        total_spend += df["spend_usd"].sum()
        total_revenue += df["conv_value"].sum()
    if total_spend <= 0:
        return None
    return total_revenue / total_spend


def build_total_account_profit_chart_png(
    total_weekly_df: pd.DataFrame,
    window_start: date,
    window_end: date,
) -> bytes:
    """Single large chart of total-account weekly profit. Goes on the
    Overview sheet to the LEFT of the existing small-multiples grid.

    Bars colored green for profit, red for loss. Each bar annotated
    with its dollar value. A summary stat (total profit over the
    window) is shown in the title.
    """
    if total_weekly_df.empty:
        fig, ax = plt.subplots(figsize=(8, 9))
        ax.text(0.5, 0.5, "No account-level data available",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    df = total_weekly_df.copy()
    df["week_dt"] = pd.to_datetime(df["week_start"])
    fig, ax = plt.subplots(figsize=(8, 9))
    bar_colors = ["#9BBB59" if p >= 0 else "#C0504D" for p in df["profit"]]
    ax.bar(df["week_dt"], df["profit"], width=6,
           color=bar_colors, alpha=0.8, edgecolor="#404040", linewidth=0.5)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    for _, row in df.iterrows():
        if pd.notna(row["profit"]):
            offset = 8 if row["profit"] >= 0 else -14
            ax.annotate(f"${row['profit']:,.0f}",
                        xy=(row["week_dt"], row["profit"]),
                        xytext=(0, offset), textcoords="offset points",
                        ha="center", fontsize=9,
                        color="#5F7F2F" if row["profit"] >= 0 else "#8B0000",
                        fontweight="bold")

    total_profit = df["profit"].sum()
    n_weeks = len(df)
    ax.set_title(
        f"TOTAL ACCOUNT — Weekly Profit\n"
        f"{window_start} to {window_end}   |   "
        f"${total_profit:,.0f} total over {n_weeks} weeks",
        fontsize=12, fontweight="bold", loc="left",
    )
    ax.set_ylabel("Weekly profit ($)", fontsize=11)
    ax.set_xlabel("Week starting (Mon)", fontsize=10)
    ax.grid(True, alpha=0.3, axis="y")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"${v:,.0f}"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# ELASTICITY CHART (per-campaign full-history view)
# ═══════════════════════════════════════════════════════════════════
# Adapted from elasticity_regression_google.build_campaign_overview_chart_png.
# That script remains the source of truth on the elasticity analysis design
# (measurement windows, lag, weighting). This is a presentation-layer port:
# takes a pre-fetched daily DataFrame instead of a DB conn, so it slots into
# the visualization workbook without monkey-patching across modules.
# If the elasticity chart design changes there, update here too.
# ═══════════════════════════════════════════════════════════════════

def build_campaign_elasticity_chart_png(
    daily_df: pd.DataFrame,
    campaign_name: str,
    current_target: float | None,
    start_date: date,
    end_date: date,
    change_events: list[dict],
    measurement_pre: int = ELASTICITY_PRE_DAYS,
    measurement_post: int = ELASTICITY_POST_DAYS,
    lag_days: int = ELASTICITY_LAG_DAYS,
) -> bytes:
    """Build a 3-panel time-series chart for a single campaign over the
    elasticity-analysis window:
      Panel 1: tROAS step function (current target if no changes; otherwise
               steps at each change with old→new values)
      Panel 2: Daily ROAS + 7-day rolling avg + current-target reference,
               vertical lines + pre/post shading at each change event
      Panel 3: Daily conversions + 7-day rolling avg, same overlays

    daily_df: pre-fetched daily DataFrame with columns
              [report_date, spend_usd, conv, conv_value]
    change_events: list of dicts with {change_date, old_target, new_target}
    """
    df = daily_df.copy() if not daily_df.empty else pd.DataFrame()

    if df.empty:
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.text(0.5, 0.5, f"No data available for\n{campaign_name}\n"
                          f"between {start_date} and {end_date}",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    # Compute daily ROAS + 7d rolling averages
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
        all_targets = ([e["old_target"] for e in sorted_events]
                       + [e["new_target"] for e in sorted_events])
        y_min, y_max = min(all_targets), max(all_targets)
        y_pad = max(0.05, (y_max - y_min) * 0.3)
        ax1.set_ylim(y_min - y_pad, y_max + y_pad)

    ax1.set_ylabel("tROAS setting", fontsize=10)
    title = (f"{campaign_name}\n"
             f"{len(sorted_events)} tROAS change(s) in window "
             f"{start_date} to {end_date}")
    ax1.set_title(title, fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: Daily ROAS + 7-day MA + current-target reference ──
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
        post_end_e   = pd.Timestamp(ev["change_date"]
                                    + timedelta(days=lag_days + measurement_post - 1))
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

    # ── Panel 3: Daily conversions + 7-day MA ──────────────────────
    ax3.scatter(df["report_dt"], df["conv"], color="gray", alpha=0.4, s=15,
                label="Daily conv")
    ax3.plot(df["report_dt"], df["conv_7d_ma"], color="#4F81BD", linewidth=2,
             label="7-day rolling avg")
    for ev in sorted_events:
        change_dt = pd.Timestamp(ev["change_date"])
        pre_start_e = pd.Timestamp(ev["change_date"] - timedelta(days=measurement_pre))
        pre_end_e   = pd.Timestamp(ev["change_date"] - timedelta(days=1))
        post_start_e = pd.Timestamp(ev["change_date"] + timedelta(days=lag_days))
        post_end_e   = pd.Timestamp(ev["change_date"]
                                    + timedelta(days=lag_days + measurement_post - 1))
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
# MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--targets-csv", default=str(DEFAULT_TARGETS_CSV),
                   help="Google Ads target-change CSV (for tROAS overlays)")
    p.add_argument("--budgets-csv", default=str(DEFAULT_BUDGETS_CSV),
                   help="Google Ads budget-change CSV (for budget overlays)")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--end-date", default=None,
                   help="Last day of weekly analysis window (ISO YYYY-MM-DD). "
                        "Default: auto-derived as the last complete Mon-Sun "
                        "week available in the DB. Pass explicitly to override "
                        "(useful for reproducible historical runs). Does NOT "
                        "control the elasticity chart's end date — that always "
                        "runs to the latest available date in the DB.")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help=f"Number of weeks back from --end-date. Default: "
                        f"{DEFAULT_WEEKS}")
    p.add_argument("--elasticity-start", default=YEAR_START.isoformat(),
                   help=f"Start date for the per-campaign elasticity charts "
                        f"(ISO YYYY-MM-DD). Default: {YEAR_START.isoformat()} "
                        f"(year-to-date). The end of the elasticity window is "
                        f"always the latest available date in the DB.")
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path)
    targets_csv = Path(args.targets_csv)
    budgets_csv = Path(args.budgets_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # End-date for the weekly window:
    #   - If --end-date given: use it verbatim (legacy behavior, for
    #     reproducible historical runs).
    #   - Otherwise: defer resolution until after we've opened the DB,
    #     and use the last-complete-Mon-Sun week available there.
    n_weeks = args.weeks
    explicit_end_date = (date.fromisoformat(args.end_date)
                         if args.end_date else None)
    elasticity_start = date.fromisoformat(args.elasticity_start)

    banner("CAMPAIGN PERFORMANCE VISUALIZATIONS — Google Home")
    print(f"  DB:                  {db_path}")
    print(f"  Targets CSV:         {targets_csv}")
    print(f"  Budgets CSV:         {budgets_csv}")
    print(f"  Output:              {output_dir}")
    if explicit_end_date:
        print(f"  End date (forced):   {explicit_end_date}")
    else:
        print(f"  End date:            (auto: last complete Mon-Sun week in DB)")
    print(f"  Weeks:               {n_weeks}")
    print(f"  Elasticity start:    {elasticity_start}")
    print()

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    enabled = fetch_enabled_campaigns(conn)
    if len(enabled) != len(ENABLED_CAMPAIGN_IDS):
        missing = set(ENABLED_CAMPAIGN_IDS) - set(enabled["campaign_id"].astype(str))
        print(f"⚠  Expected {len(ENABLED_CAMPAIGN_IDS)} enabled campaigns, DB returned {len(enabled)}. "
              f"Missing: {missing}")
    enabled_names = set(enabled["campaign_name"].tolist())
    print(f"Found {len(enabled)} enabled campaigns")

    # Resolve the weekly window end-date now that we have the DB connection.
    # The weekly charts must end on a complete Sunday — otherwise the most
    # recent bar shows a partial week's data with artificially low values.
    if explicit_end_date is not None:
        # User-forced end-date: snap to its containing week's Sunday
        # (or back to prior Sunday if mid-week).
        weekday = explicit_end_date.weekday()
        if weekday == 6:
            window_end = explicit_end_date
        else:
            window_end = explicit_end_date - timedelta(days=weekday + 1)
    else:
        _, window_end = fetch_latest_complete_week(conn)
    end_week_monday = window_end - timedelta(days=window_end.weekday())
    window_start = end_week_monday - timedelta(weeks=n_weeks - 1)

    # Resolve the elasticity window — runs to the latest available date in
    # the DB, not bounded by the complete-week constraint. This gives the
    # most up-to-date view of the elasticity charts even mid-week.
    elasticity_end = fetch_latest_data_date(conn)
    if elasticity_end is None:
        elasticity_end = window_end  # fall back if no data

    print(f"  Weekly window:       {window_start} (Mon) to {window_end} (Sun, complete)")
    print(f"  Elasticity window:   {elasticity_start} to {elasticity_end}")
    print()

    # Load change events. Two scopes:
    #   - For the weekly chart overlays: filter to the 12-week visualization window
    #   - For the elasticity chart overlays: filter to the elasticity window
    change_events_df = load_change_events(targets_csv, budgets_csv,
                                          enabled_names, window_start, window_end)
    if change_events_df.empty:
        n_target = n_budget = 0
    else:
        n_target = (change_events_df["attribute"] == "TargetROAS").sum()
        n_budget = (change_events_df["attribute"] == "Budget").sum()
    print(f"Loaded {len(change_events_df)} change events within weekly window "
          f"({n_target} tROAS, {n_budget} budget)")
    elasticity_events_df = load_change_events(targets_csv, budgets_csv,
                                              enabled_names, elasticity_start, elasticity_end)
    n_el_target = (elasticity_events_df["attribute"] == "TargetROAS").sum() \
        if not elasticity_events_df.empty else 0
    print(f"Loaded {n_el_target} tROAS change events within elasticity window")
    print()

    # Process each campaign: fetch daily for BOTH windows.
    #   - campaign_daily (weekly window): feeds the 5-panel weekly chart and
    #     the account totals.
    #   - campaign_daily_elasticity (elasticity window): feeds the elasticity
    #     chart. Wider window (typically YTD) so the chart shows long-history
    #     context around each tROAS change.
    campaign_daily: dict[str, pd.DataFrame] = {}
    campaign_weekly: dict[str, pd.DataFrame] = {}
    campaign_daily_elasticity: dict[str, pd.DataFrame] = {}
    for _, camp in enabled.iterrows():
        cname = camp["campaign_name"]
        daily = fetch_campaign_daily(conn, cname, window_start, window_end)
        campaign_daily[cname] = daily
        campaign_weekly[cname] = aggregate_weekly(daily)
        # Elasticity window fetch — wider, separate query
        campaign_daily_elasticity[cname] = fetch_campaign_daily(
            conn, cname, elasticity_start, elasticity_end,
        )

    # Account-level rollup for the weekly window (feeds Overview profit chart
    # and Total Account sheet's 5-panel weekly chart).
    total_daily = aggregate_total_account_daily(campaign_daily)
    total_weekly = aggregate_weekly(total_daily)
    blended_roas = compute_blended_roas(campaign_daily)
    if blended_roas is not None:
        print(f"Account-level blended ROAS (Σ revenue ÷ Σ spend, weekly window): "
              f"{blended_roas:.3f}")

    # Account-level rollup for the elasticity window (feeds Total Account's
    # elasticity chart). Separate from the weekly aggregation — different
    # date window means different blended values.
    total_daily_elasticity = aggregate_total_account_daily(campaign_daily_elasticity)
    blended_roas_elasticity = compute_blended_roas(campaign_daily_elasticity)
    print()

    # Reshape change events per campaign into the dict format the elasticity
    # chart expects: {change_date, old_target, new_target}. We pull from
    # elasticity_events_df (the wider window), not the 12-week one, because
    # the elasticity chart spans more history. Filter to TargetROAS only —
    # the elasticity chart doesn't visualize budget changes.
    elasticity_changes_by_campaign: dict[str, list[dict]] = {}
    if not elasticity_events_df.empty:
        troas_only = elasticity_events_df[
            elasticity_events_df["attribute"] == "TargetROAS"
        ]
        for cname in enabled_names:
            rows = troas_only[troas_only["campaign_name"] == cname]
            elasticity_changes_by_campaign[cname] = [
                {"change_date": r["change_date"],
                 "old_target": r["old_value"],
                 "new_target": r["new_value"]}
                for _, r in rows.iterrows()
                if r["old_value"] is not None and r["new_value"] is not None
            ]

    # Build workbook
    print("Building workbook...")
    wb = Workbook()
    wb.remove(wb.active)

    # ── 1. Overview sheet ────────────────────────────────────────────
    # Layout: large total-account profit chart on the LEFT (anchored A4),
    # per-campaign small-multiples grid on the RIGHT (anchored at col ~M).
    # The total chart is ~8 inches → ~16 default Excel columns, so column M
    # gives the grid a clean start without visual overlap.
    ws = wb.create_sheet("Overview")
    ws.cell(row=1, column=1,
            value="ACCOUNT-LEVEL OVERVIEW — Google Home Insurance"
            ).font = Font(bold=True, size=14)
    ws.cell(row=2, column=1,
            value=f"Window: {window_start} to {window_end} "
                  f"({n_weeks} weeks).")
    ws.cell(row=3, column=1,
            value="Left: total-account weekly profit. Right: per-campaign "
                  "profit small-multiples, sorted by 12-week total profit.")

    print("  Generating total-account profit chart...")
    total_profit_png = build_total_account_profit_chart_png(
        total_weekly, window_start, window_end,
    )
    img_total = XLImage(io.BytesIO(total_profit_png))
    ws.add_image(img_total, "A5")

    print("  Generating small-multiples overview...")
    overview_png = build_overview_small_multiples_png(
        campaign_weekly, window_start, window_end,
    )
    img_grid = XLImage(io.BytesIO(overview_png))
    ws.add_image(img_grid, "M5")
    print("  ✓ Overview")

    # ── 2. Total Account sheet ───────────────────────────────────────
    # Reuses build_5panel_campaign_chart_png by passing the totals as if
    # they were "one campaign" — blended ROAS goes in as the reference
    # line. No change-event overlays (account-level view; the per-campaign
    # sheets are where individual change events belong).
    # Layout: 5-panel weekly chart at A4 (left), 3-panel elasticity chart
    # at Q4 (right). Same side-by-side layout as the per-campaign sheets.
    ws_total = wb.create_sheet("Total Account")
    ws_total.cell(row=1, column=1, value="TOTAL ACCOUNT").font = Font(bold=True, size=14)
    subtitle = f"Weekly window: {window_start} to {window_end}."
    if blended_roas is not None:
        subtitle += (f" Blended ROAS (weekly): {blended_roas:.3f} — "
                     f"shown as reference on ROAS panel.")
    ws_total.cell(row=2, column=1, value=subtitle)
    ws_total.cell(row=3, column=1,
                  value=f"Elasticity window: {elasticity_start} to {elasticity_end}.")
    print("  Generating Total Account 5-panel chart...")
    try:
        total_png = build_5panel_campaign_chart_png(
            weekly_df=total_weekly,
            campaign_name="TOTAL ACCOUNT",
            current_target=blended_roas,
            change_events=[],
            window_start=window_start,
            window_end=window_end,
        )
        ws_total.add_image(XLImage(io.BytesIO(total_png)), "A4")
        print("  ✓ Total Account weekly")
    except Exception as exc:
        print(f"  ✗ Total Account weekly: chart failed ({exc})")

    print("  Generating Total Account elasticity chart...")
    try:
        total_el_png = build_campaign_elasticity_chart_png(
            daily_df=total_daily_elasticity,
            campaign_name="TOTAL ACCOUNT",
            current_target=blended_roas_elasticity,
            start_date=elasticity_start,
            end_date=elasticity_end,
            change_events=[],  # account-level: no single-campaign event lines
        )
        ws_total.add_image(XLImage(io.BytesIO(total_el_png)), "Q4")
        print("  ✓ Total Account elasticity")
    except Exception as exc:
        print(f"  ✗ Total Account elasticity: chart failed ({exc})")

    # ── 3. Per-campaign sheets ───────────────────────────────────────
    # Each sheet has the 5-panel weekly chart at A1 (left) and the 3-panel
    # elasticity chart at Q1 (right). Same layout as the Microsoft script.
    sheet_count = 0
    print("  Generating per-campaign charts (5-panel weekly + 3-panel elasticity)...")
    used_sheet_names = {"Overview", "Total Account"}
    for _, camp in enabled.iterrows():
        cname = camp["campaign_name"]
        current_target = camp["current_target"]
        if pd.isna(current_target):
            current_target = None
        weekly_df = campaign_weekly.get(cname, pd.DataFrame())
        elasticity_daily_df = campaign_daily_elasticity.get(cname, pd.DataFrame())
        elasticity_events = elasticity_changes_by_campaign.get(cname, [])

        # Filter change events to this campaign (for the weekly chart's overlays)
        if change_events_df.empty:
            camp_events = []
        else:
            camp_events = change_events_df[
                change_events_df["campaign_name"] == cname
            ].to_dict("records")

        # Sheet name: short campaign name (Excel limit 31 chars)
        short_name = (cname.replace("HOME INSURANCE-", "")
                      .split("-NB-SEARCH")[0][:25])
        sheet_name = short_name[:31]
        # Avoid duplicate sheet names defensively
        if sheet_name in used_sheet_names:
            sheet_name = f"{short_name[:28]}_2"[:31]
        used_sheet_names.add(sheet_name)

        ws_chart = wb.create_sheet(sheet_name)
        weekly_ok = elasticity_ok = False

        # 5-panel weekly chart at A1 (left)
        try:
            png_bytes = build_5panel_campaign_chart_png(
                weekly_df=weekly_df,
                campaign_name=cname,
                current_target=current_target,
                change_events=camp_events,
                window_start=window_start,
                window_end=window_end,
            )
            ws_chart.add_image(XLImage(io.BytesIO(png_bytes)), "A1")
            weekly_ok = True
        except Exception as exc:
            print(f"  ✗ {sheet_name}: weekly chart failed ({exc})")

        # 3-panel elasticity chart at Q1 (right)
        try:
            el_png_bytes = build_campaign_elasticity_chart_png(
                daily_df=elasticity_daily_df,
                campaign_name=cname,
                current_target=current_target,
                start_date=elasticity_start,
                end_date=elasticity_end,
                change_events=elasticity_events,
            )
            ws_chart.add_image(XLImage(io.BytesIO(el_png_bytes)), "Q1")
            elasticity_ok = True
        except Exception as exc:
            print(f"  ✗ {sheet_name}: elasticity chart failed ({exc})")

        if weekly_ok and elasticity_ok:
            sheet_count += 1
            print(f"  ✓ {sheet_name}")
        elif weekly_ok or elasticity_ok:
            sheet_count += 1
            print(f"  ⚠ {sheet_name} (partial — see warnings above)")

    conn.close()
    print(f"Generated {sheet_count} per-campaign chart sheets")

    out_path = output_dir / f"campaign_performance_google_{date.today().isoformat()}.xlsx"
    wb.save(out_path)
    print()
    print(f"  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
