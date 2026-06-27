"""
Campaign Performance Visualizations — Microsoft Home Insurance
═══════════════════════════════════════════════════════════════════════
Generates a per-campaign weekly performance workbook covering the last
12 weeks of operating equilibrium.

By default the window ends on the Sunday of the most recent complete
week loaded in campaign_performance_daily — auto-detected at runtime,
no buffer applied. Pass --end-date YYYY-MM-DD to override.

Designed to answer the leadership-side question: "Why isn't the model
finding more headroom?" — by showing each campaign's recent trajectory
on conversions, ROAS, CPA, RPL, and profit. Each metric is plotted
weekly so the eye picks up trends without being distracted by daily
noise.

USAGE:
  python analysis/campaign_performance_visualizations_microsoft.py
  python analysis/campaign_performance_visualizations_microsoft.py --end-date 2026-04-26
  python campaign_performance_visualizations_microsoft.py --weeks 16

OUTPUT: campaign_performance_microsoft_<YYYY-MM-DD>.xlsx
  - Overview: large account-wide weekly-profit trend (left) + the
    existing per-campaign small-multiples grid (right)
  - Total Account: 5-panel weekly chart for the whole account (same
    layout as the campaign sheets) + a 3-panel elasticity-style chart
    side-by-side, plus the network-split diagnostic stacked below.
    The CPA panel's reference line is the BLENDED tCPA, computed as
    Σspend ÷ Σconv across all enabled campaigns over the full 12-week
    window.
  - <CAMPAIGN_SHORT> sheets, sorted by 12-week total profit descending,
    each containing three charts:
      Top-left:  5-panel weekly chart (conversions, ROAS, CPA, RPL, profit)
      Top-right: 3-panel daily elasticity chart (tCPA setting, daily CPA
                 + 7d MA, daily conversions + 7d MA) reused from
                 elasticity_regression_microsoft.py
      Bottom:    3-panel network-split diagnostic — Search vs Audience
                 spend share, ROAS, and profit. Header table shows
                 12-week totals per network plus a "tCPA ceiling at
                 ROAS 1.2x" planning number. Built to answer "should
                 we split Audience out of this campaign?"

The elasticity chart builder is reused via import from
elasticity_regression_microsoft.py. That script's own data fetcher
predates the loader fix and does NOT filter device='ALL', so this
script monkey-patches it at startup with a corrected fetcher to
guarantee the elasticity charts here read de-duplicated conversions.
The elasticity script's own outputs (regression coefficients, per-event
sheets) are NOT corrected by this patch — fix that script separately.
"""

import argparse
import io
import sqlite3
from datetime import date, timedelta
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
ACCOUNT_ID = 2  # Microsoft Home Insurance
ACCOUNT_LABEL = "microsoft_home"
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_CHANGE_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Microsoft-bids-budget-changehistory-YTD.csv")
DEFAULT_OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

DEFAULT_WEEKS = 12
# No hard-coded end date — by default the script detects the last
# complete week available in campaign_performance_daily.


def banner(text: str) -> None:
    print("═" * 70)
    print(text)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def fetch_enabled_campaigns(conn) -> pd.DataFrame:
    """Enabled campaigns + their current tCPA. Filters to ENABLED status."""
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


def fetch_latest_complete_week(conn) -> tuple[date, date]:
    """Determine the most recent complete Mon-Sun week available in
    campaign_performance_daily for this account.

    Logic: find MAX(date) for the account, then return the Mon-Sun week
    that is fully covered. If the latest date IS a Sunday, that week is
    complete. Otherwise, step back to the prior Sunday and use the week
    ending there. No 2-day buffer — if a Sunday is loaded, that week
    counts as complete.
    """
    row = conn.execute(
        """
        SELECT MAX(cpd.date)
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON cpd.campaign_id = c.campaign_id
        WHERE c.account_id = ?
        """,
        (ACCOUNT_ID,),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(
            f"No rows found in campaign_performance_daily for account_id={ACCOUNT_ID}"
        )
    latest = date.fromisoformat(row[0])
    # weekday(): Mon=0 ... Sun=6. A complete week ends on Sunday (weekday=6).
    weekday = latest.weekday()
    if weekday == 6:  # Sunday — this week is complete
        week_end = latest
    else:
        # Step back to the prior Sunday
        days_back = weekday + 1  # Mon→1, Tue→2, ..., Sat→6
        week_end = latest - timedelta(days=days_back)
    week_start = week_end - timedelta(days=6)
    return week_start, week_end


def fetch_campaign_daily(conn, campaign_name, start_date, end_date):
    """Pull daily spend + conversions + revenue for one campaign across
    the search and audience networks. Uses the pre-aggregated device='ALL'
    rollup rows in the two campaign performance tables — NOT the keyword
    table.

    Microsoft convention (intentional, not a bug):
      - CPA denominator and conversion volume → `conversions` column.
        This is what the bid strategy optimizes against.
      - Revenue (for RPL, ROAS, profit) → `all_conversion_value` column.
        Microsoft's `conversion_value` excludes offline-attributed revenue,
        but the business reports against the offline-inclusive total.

    Returns the COMBINED (Search + Audience) daily dataframe — same
    columns as before. For the network-split view, call
    fetch_campaign_daily_split() instead.
    """
    combined, _search, _audience = fetch_campaign_daily_split(
        conn, campaign_name, start_date, end_date
    )
    return combined


def fetch_campaign_daily_split(conn, campaign_name, start_date, end_date):
    """Same query pattern as fetch_campaign_daily, but returns THREE
    dataframes: (combined, search_only, audience_only).

    Each dataframe has columns: report_date, spend_usd, conv, conv_value.
    The 'combined' frame is identical to what fetch_campaign_daily()
    returns. The 'search_only' and 'audience_only' frames let the
    network-split diagnostic chart compare the two networks separately.

    Doing this in one function instead of two avoids re-querying the
    same SQL twice.
    """
    # ── Search bucket: campaign_performance_daily, device='ALL' rollup
    sql_search = """
    SELECT
        cpd.date                  AS report_date,
        cpd.cost_micros / 1e6     AS spend_usd,
        cpd.conversions           AS conv,
        cpd.all_conversion_value  AS conv_value
    FROM campaign_performance_daily cpd
    JOIN campaigns c ON cpd.campaign_id = c.campaign_id
    WHERE c.account_id = ?
      AND c.campaign_name = ?
      AND cpd.date BETWEEN ? AND ?
      AND cpd.device = 'ALL'
    """
    df_search = pd.read_sql(sql_search, conn, params=(
        ACCOUNT_ID, campaign_name, start_date.isoformat(), end_date.isoformat()
    ))

    # ── Audience bucket: campaign_performance_network_daily, device='ALL',
    #    network='Audience' (explicitly excludes any Syndicated residue)
    sql_audience = """
    SELECT
        cpnd.date                  AS report_date,
        cpnd.cost_micros / 1e6     AS spend_usd,
        cpnd.conversions           AS conv,
        cpnd.all_conversion_value  AS conv_value
    FROM campaign_performance_network_daily cpnd
    JOIN campaigns c ON cpnd.campaign_id = c.campaign_id
    WHERE c.account_id = ?
      AND c.campaign_name = ?
      AND cpnd.date BETWEEN ? AND ?
      AND cpnd.device = 'ALL'
      AND cpnd.network = 'Audience'
    """
    df_audience = pd.read_sql(sql_audience, conn, params=(
        ACCOUNT_ID, campaign_name, start_date.isoformat(), end_date.isoformat()
    ))

    # Normalize date columns to date type (not datetime) for clean merge
    for df in (df_search, df_audience):
        if not df.empty:
            df["report_date"] = pd.to_datetime(df["report_date"]).dt.date

    # ── Combined: merge on date, sum search + audience for all-network totals
    df_search_r = df_search.rename(columns={
        "spend_usd": "search_spend", "conv": "search_conv",
        "conv_value": "search_conv_value",
    })
    df_audience_r = df_audience.rename(columns={
        "spend_usd": "audience_spend", "conv": "audience_conv",
        "conv_value": "audience_conv_value",
    })
    combined = (df_search_r.merge(df_audience_r, on="report_date", how="outer")
                            .fillna(0))
    combined["spend_usd"] = combined["search_spend"] + combined["audience_spend"]
    combined["conv"] = combined["search_conv"] + combined["audience_conv"]
    combined["conv_value"] = (
        combined["search_conv_value"] + combined["audience_conv_value"]
    )
    combined = combined[["report_date", "spend_usd", "conv", "conv_value"]]

    return combined, df_search, df_audience


# ═══════════════════════════════════════════════════════════════════
# WEEKLY AGGREGATION
# ═══════════════════════════════════════════════════════════════════

def aggregate_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Roll up daily data to ISO weeks (Mon-Sun).

    Returns columns: week_start (Mon date), week_end (Sun date),
    spend, conv, conv_value, cpa, rpl, profit.

    CPA and RPL are weighted ratios (sum spend / sum conv, etc.) — NOT
    averages of daily values. This matches how the strategic script
    computes weekly aggregates.
    """
    if daily_df.empty:
        return pd.DataFrame()

    df = daily_df.copy()
    df["report_date"] = pd.to_datetime(df["report_date"])
    # ISO week: Monday is start of week
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
# CHANGE-HISTORY LOADER (lightweight subset of elasticity script)
# ═══════════════════════════════════════════════════════════════════

def load_change_events(csv_path: Path,
                       start_date: date, end_date: date) -> pd.DataFrame:
    """Load tCPA + budget change events from the YTD change CSV, filtered
    to the visualization window. Used to overlay vertical change-event
    lines on the per-campaign charts.
    """
    if not csv_path.exists():
        return pd.DataFrame(columns=["change_date", "campaign_name",
                                      "attribute", "old_value", "new_value"])
    df = pd.read_csv(csv_path)
    df["change_date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y").dt.date
    df = df.rename(columns={
        "Campaign": "campaign_name",
        "Attribute changed": "attribute",
        "Old value": "old_value",
        "New value": "new_value",
    })
    df = df[(df["change_date"] >= start_date) & (df["change_date"] <= end_date)].copy()
    return df[["change_date", "campaign_name", "attribute",
               "old_value", "new_value"]].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ═══════════════════════════════════════════════════════════════════

def build_4panel_campaign_chart_png(
    weekly_df: pd.DataFrame,
    campaign_name: str,
    current_tcpa: float | None,
    change_events: list[dict],
    window_start: date,
    window_end: date,
) -> bytes:
    """5-panel weekly chart for one campaign:
       Panel 1 (top):    weekly conversions (bar chart)
       Panel 2:          weekly ROAS (line, all_conv_value / spend)
       Panel 3:          weekly CPA + current tCPA reference line
       Panel 4:          weekly RPL
       Panel 5 (bottom): weekly profit (bar chart, color-coded green/red)

    Plus vertical lines at any change events within the window.
    """
    if weekly_df.empty:
        fig, ax = plt.subplots(figsize=(11, 8.5))
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

    fig, (ax1, ax_roas, ax2, ax3, ax4) = plt.subplots(
        5, 1, figsize=(11, 11.5), sharex=True,
        gridspec_kw={"hspace": 0.18},
    )

    # ── Panel 1: weekly conversions ────────────────────────────────
    ax1.bar(df["week_dt"], df["conv"], width=bar_width,
            color="#4F81BD", alpha=0.75, edgecolor="#1F4E79", linewidth=0.5)
    # Annotate each bar with its value
    for _, row in df.iterrows():
        if row["conv"] > 0:
            ax1.annotate(f"{int(round(row['conv']))}",
                         xy=(row["week_dt"], row["conv"]),
                         xytext=(0, 3), textcoords="offset points",
                         ha="center", fontsize=8, color="#1F4E79")
    ax1.set_ylabel("Weekly conversions", fontsize=10)
    ax1.set_title(f"{campaign_name}\n12-week performance: {window_start} to {window_end}",
                  fontsize=11, fontweight="bold", loc="left")
    ax1.grid(True, alpha=0.3, axis="y")

    # ── Panel 2: weekly ROAS ───────────────────────────────────────
    ax_roas.plot(df["week_dt"], df["roas"], marker="D", color="#8064A2",
                 linewidth=2, markersize=7, label="Weekly ROAS")
    # Reference line at ROAS = 1.0 (break-even on revenue vs spend)
    ax_roas.axhline(1.0, color="#404040", linestyle=":", linewidth=1.0,
                    alpha=0.6, label="Break-even (1.0)")
    for _, row in df.iterrows():
        if pd.notna(row["roas"]):
            ax_roas.annotate(f"{row['roas']:.2f}",
                             xy=(row["week_dt"], row["roas"]),
                             xytext=(0, 5), textcoords="offset points",
                             ha="center", fontsize=8, color="#5F4E7A")
    ax_roas.set_ylabel("Weekly ROAS", fontsize=10)
    ax_roas.legend(loc="best", fontsize=8, framealpha=0.9)
    ax_roas.grid(True, alpha=0.3)

    # ── Panel 3: weekly CPA ────────────────────────────────────────
    ax2.plot(df["week_dt"], df["cpa"], marker="o", color="#C0504D",
             linewidth=2, markersize=7, label="Weekly CPA")
    if current_tcpa is not None:
        ax2.axhline(current_tcpa, color="#1F4E79", linestyle="-.", linewidth=1.5,
                    alpha=0.7, label=f"Current tCPA target (${current_tcpa:.0f})")
    # Annotate CPA values
    for _, row in df.iterrows():
        if pd.notna(row["cpa"]):
            ax2.annotate(f"${row['cpa']:.0f}",
                         xy=(row["week_dt"], row["cpa"]),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=8, color="#C0504D")
    ax2.set_ylabel("Weekly CPA ($)", fontsize=10)
    ax2.legend(loc="best", fontsize=8, framealpha=0.9)
    ax2.grid(True, alpha=0.3)

    # ── Panel 4: weekly RPL ────────────────────────────────────────
    ax3.plot(df["week_dt"], df["rpl"], marker="s", color="#9BBB59",
             linewidth=2, markersize=7, label="Weekly RPL")
    for _, row in df.iterrows():
        if pd.notna(row["rpl"]):
            ax3.annotate(f"${row['rpl']:.0f}",
                         xy=(row["week_dt"], row["rpl"]),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=8, color="#5F7F2F")
    ax3.set_ylabel("Weekly RPL ($)", fontsize=10)
    ax3.legend(loc="best", fontsize=8, framealpha=0.9)
    ax3.grid(True, alpha=0.3)

    # ── Panel 5: weekly profit ─────────────────────────────────────
    profit_colors = ["#9BBB59" if p >= 0 else "#C0504D" for p in df["profit"]]
    ax4.bar(df["week_dt"], df["profit"], width=bar_width,
            color=profit_colors, alpha=0.75, edgecolor="#404040", linewidth=0.5)
    ax4.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    for _, row in df.iterrows():
        if pd.notna(row["profit"]):
            offset = 8 if row["profit"] >= 0 else -12
            ax4.annotate(f"${row['profit']:,.0f}",
                         xy=(row["week_dt"], row["profit"]),
                         xytext=(0, offset), textcoords="offset points",
                         ha="center", fontsize=8,
                         color="#5F7F2F" if row["profit"] >= 0 else "#8B0000")
    ax4.set_ylabel("Weekly profit ($)", fontsize=10)
    ax4.set_xlabel("Week starting (Mon)", fontsize=10)
    ax4.grid(True, alpha=0.3, axis="y")

    # Overlay change-event markers on all panels
    for ev in change_events:
        change_dt = pd.Timestamp(ev["change_date"])
        # Choose color by attribute type
        if "TargetCPA" in str(ev.get("attribute", "")):
            color, ls = "red", "--"
        elif "Budget" in str(ev.get("attribute", "")):
            color, ls = "#FF8C00", ":"  # orange dotted for budget changes
        else:
            color, ls = "gray", "--"
        for ax in (ax1, ax_roas, ax2, ax3, ax4):
            ax.axvline(change_dt, color=color, linestyle=ls, linewidth=1.2, alpha=0.6)

    # Format x-axis
    ax4.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))  # Mondays
    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax4.xaxis.get_majorticklabels(), rotation=30, ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_overview_small_multiples_png(
    campaign_weekly: dict[str, pd.DataFrame],
    window_start: date,
    window_end: date,
) -> bytes:
    """One sheet showing all 12 campaigns' weekly profit as small multiples.

    Layout: 4 columns × 3 rows. Each panel is a single campaign's weekly
    profit bar chart. Y-axis is independent per panel (profit magnitudes
    differ by 50× across campaigns; shared y-axis would make small
    campaigns invisible). Each panel labels its total 12-week profit.
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

    # Compute grid dimensions
    n_cols = 4
    n_rows = max(1, (n_campaigns + n_cols - 1) // n_cols)

    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(16, 3.2 * n_rows),
        sharex=True, gridspec_kw={"hspace": 0.55, "wspace": 0.25},
    )
    axes = axes.flatten() if n_rows * n_cols > 1 else [axes]

    # Sort campaigns by total 12-week profit descending so biggest are top-left
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
        short_name = campaign_name.replace("HOME INSURANCE-", "").split("-SEARCH")[0]
        total_profit = profits.sum()
        ax.set_title(f"{short_name}\n${total_profit:,.0f} total",
                     fontsize=9, fontweight="bold", loc="left")
        ax.tick_params(axis="x", labelsize=7)
        ax.tick_params(axis="y", labelsize=7)
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        ax.grid(True, alpha=0.25, axis="y")
        # Format y-axis as currency
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"${v:,.0f}"))

    # Hide unused subplots
    for idx in range(len(sorted_items), len(axes)):
        axes[idx].set_axis_off()

    fig.suptitle(
        f"Weekly Profit by Campaign — {window_start} to {window_end} "
        f"(sorted by total 12-week profit)",
        fontsize=13, fontweight="bold", y=0.995,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# NETWORK-SPLIT DIAGNOSTIC CHART
# ═══════════════════════════════════════════════════════════════════

# Minimum Audience conv per week below which we fade the marker. Below
# this volume, weekly ratios (Audience ROAS especially) are dominated by
# noise from individual conversions and shouldn't drive interpretation.
AUDIENCE_LOW_CONF_THRESHOLD = 5


def _weekly_split_from_daily(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Roll a single-network daily dataframe up to weeks. Returns
    week_start, spend, conv, conv_value, cpa, rpl, roas, profit per
    week — same columns as aggregate_weekly() but without the
    week_end column. Used internally by build_network_split_chart_png.
    """
    if daily_df.empty:
        return pd.DataFrame(columns=["week_start", "spend", "conv", "conv_value",
                                      "cpa", "rpl", "roas", "profit"])
    df = daily_df.copy()
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["week_start"] = (df["report_date"]
                        - pd.to_timedelta(df["report_date"].dt.weekday, unit="D"))
    df["week_start"] = df["week_start"].dt.date
    agg = df.groupby("week_start").agg(
        spend=("spend_usd", "sum"),
        conv=("conv", "sum"),
        conv_value=("conv_value", "sum"),
    ).reset_index()
    agg["cpa"] = agg.apply(
        lambda r: r["spend"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["rpl"] = agg.apply(
        lambda r: r["conv_value"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["roas"] = agg.apply(
        lambda r: r["conv_value"] / r["spend"] if r["spend"] > 0 else None, axis=1)
    agg["profit"] = agg["conv_value"] - agg["spend"]
    return agg


def _split_headline_stats(search_daily: pd.DataFrame,
                          audience_daily: pd.DataFrame) -> dict:
    """Compute the 12-week headline stats per network used in the
    diagnostic chart's title-area summary table.

    For each network: total spend, total conv, total conv_value,
    blended CPA (spend/conv), blended RPL (conv_value/conv),
    blended ROAS (conv_value/spend), total profit, and a
    "tCPA ceiling at ROAS 1.2x" planning number = RPL / 1.2.

    The tCPA-ceiling math: if you want ROAS ≥ 1.2x and your average
    revenue per conversion is $RPL, you can tolerate a maximum CPA of
    RPL / 1.2 before profit margin compresses below 20%. That's a
    *planning ceiling*, not a recommended bid — the bid model will
    average somewhere below the ceiling depending on auction dynamics.
    """
    def _stats(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"spend": 0.0, "conv": 0.0, "conv_value": 0.0,
                    "cpa": None, "rpl": None, "roas": None, "profit": 0.0,
                    "tcpa_at_roas_1_2": None}
        spend = float(df["spend_usd"].sum())
        conv = float(df["conv"].sum())
        rev = float(df["conv_value"].sum())
        cpa = (spend / conv) if conv > 0 else None
        rpl = (rev / conv) if conv > 0 else None
        roas = (rev / spend) if spend > 0 else None
        profit = rev - spend
        tcpa_ceiling = (rpl / 1.2) if rpl is not None else None
        return {"spend": spend, "conv": conv, "conv_value": rev,
                "cpa": cpa, "rpl": rpl, "roas": roas, "profit": profit,
                "tcpa_at_roas_1_2": tcpa_ceiling}
    return {"search": _stats(search_daily), "audience": _stats(audience_daily)}


def build_network_split_chart_png(
    search_daily: pd.DataFrame,
    audience_daily: pd.DataFrame,
    campaign_name: str,
    window_start: date,
    window_end: date,
) -> bytes:
    """3-panel weekly diagnostic chart focused on the Search/Audience
    split decision.

    Panel 1: Stacked spend by network (Search bottom, Audience top).
             Shows total weekly spend AND what % is going to Audience.
    Panel 2: ROAS by network — two lines. Reference line at 1.0.
             Audience markers faded when conv < AUDIENCE_LOW_CONF_THRESHOLD.
    Panel 3: Weekly profit by network — grouped bars.

    The header area has a 12-week summary table (Search vs Audience):
    spend, conv, RPL, CPA, ROAS, profit, plus "tCPA ceiling at ROAS 1.2x".
    """
    search_weekly = _weekly_split_from_daily(search_daily)
    audience_weekly = _weekly_split_from_daily(audience_daily)
    stats = _split_headline_stats(search_daily, audience_daily)

    if search_weekly.empty and audience_weekly.empty:
        fig, ax = plt.subplots(figsize=(11, 9))
        ax.text(0.5, 0.5,
                f"No network-split data available for\n{campaign_name}\n"
                f"between {window_start} and {window_end}",
                ha="center", va="center", fontsize=12, transform=ax.transAxes)
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    # Union of weeks across both networks so x-axis is consistent
    all_weeks = sorted(
        set(search_weekly["week_start"].tolist())
        | set(audience_weekly["week_start"].tolist())
    )
    week_dts = pd.to_datetime(all_weeks)

    # Reindex each network's weekly frame to the union (so missing weeks
    # appear as 0 in the stacked bars rather than gaps).
    def _reindex(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            empty = pd.DataFrame({"week_start": all_weeks})
            for col in ("spend", "conv", "conv_value", "cpa", "rpl", "roas", "profit"):
                empty[col] = 0.0 if col not in ("cpa", "rpl", "roas") else None
            return empty
        return (df.set_index("week_start")
                  .reindex(all_weeks)
                  .reset_index()
                  .fillna({"spend": 0.0, "conv": 0.0, "conv_value": 0.0,
                           "profit": 0.0}))
    sw = _reindex(search_weekly)
    aw = _reindex(audience_weekly)

    # Build figure: header area (table) + 3 chart panels
    fig = plt.figure(figsize=(11, 13.5))
    gs = fig.add_gridspec(
        4, 1, height_ratios=[0.85, 1, 1, 1], hspace=0.32,
    )
    ax_header = fig.add_subplot(gs[0])
    ax_spend = fig.add_subplot(gs[1])
    ax_roas = fig.add_subplot(gs[2], sharex=ax_spend)
    ax_profit = fig.add_subplot(gs[3], sharex=ax_spend)

    # ── Header: title + summary table ─────────────────────────────
    ax_header.set_axis_off()
    title_text = f"{campaign_name}\nNetwork split diagnostic: {window_start} to {window_end}"
    # Place title at the top of the header axis, well above the table
    ax_header.text(0.0, 1.15, title_text, fontsize=11, fontweight="bold",
                   transform=ax_header.transAxes, va="top", ha="left")

    # 12-week summary table: rows = metrics, columns = Search / Audience
    def _fmt_money(v):
        if v is None: return "—"
        return f"${v:,.0f}"
    def _fmt_num(v):
        if v is None: return "—"
        return f"{v:,.0f}"
    def _fmt_x(v):
        if v is None: return "—"
        return f"{v:.2f}x"

    s = stats["search"]; a = stats["audience"]
    table_rows = [
        ["Metric (12-wk total)",  "Search",                "Audience"],
        ["Spend",                 _fmt_money(s["spend"]),   _fmt_money(a["spend"])],
        ["Conversions",           _fmt_num(s["conv"]),      _fmt_num(a["conv"])],
        ["Revenue (all-conv val)",_fmt_money(s["conv_value"]), _fmt_money(a["conv_value"])],
        ["CPA",                   _fmt_money(s["cpa"]),     _fmt_money(a["cpa"])],
        ["RPL",                   _fmt_money(s["rpl"]),     _fmt_money(a["rpl"])],
        ["ROAS",                  _fmt_x(s["roas"]),        _fmt_x(a["roas"])],
        ["Profit",                _fmt_money(s["profit"]),  _fmt_money(a["profit"])],
        ["tCPA ceiling @ROAS 1.2x", _fmt_money(s["tcpa_at_roas_1_2"]),
                                    _fmt_money(a["tcpa_at_roas_1_2"])],
    ]
    table = ax_header.table(
        cellText=table_rows[1:], colLabels=table_rows[0],
        cellLoc="right", colLoc="center", loc="lower left",
        bbox=[0.0, -0.05, 0.65, 0.95],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    # Header row styling
    for j in range(3):
        cell = table[(0, j)]
        cell.set_facecolor("#1F4E79")
        cell.set_text_props(color="white", fontweight="bold")
    # First column left-aligned + bold
    for i in range(1, len(table_rows)):
        cell = table[(i, 0)]
        cell.set_text_props(ha="left", fontweight="bold")
        cell._loc = "left"

    # Headline insight: one-liner about the gap
    if s["roas"] is not None and a["roas"] is not None:
        gap = s["roas"] - a["roas"]
        if abs(gap) < 0.15:
            verdict = ("Search and Audience ROAS within 0.15x — split "
                       "may not materially improve allocation.")
            verdict_color = "#5F5F5F"
        elif gap > 0:
            verdict = (f"Search ROAS exceeds Audience by {gap:.2f}x — "
                       f"hybrid tCPA is over-bidding on Audience relative "
                       f"to its profit potential.")
            verdict_color = "#1F4E79"
        else:
            verdict = (f"Audience ROAS exceeds Search by {abs(gap):.2f}x — "
                       f"unusual; investigate before splitting.")
            verdict_color = "#8B0000"
        ax_header.text(0.68, 0.85, verdict, fontsize=9.5, style="italic",
                       transform=ax_header.transAxes, va="top", ha="left",
                       color=verdict_color, wrap=True)

    # Volume note for Audience
    if a["conv"] < AUDIENCE_LOW_CONF_THRESHOLD * 12:
        ax_header.text(
            0.68, 0.42,
            f"⚠ Audience volume is low ({a['conv']:.0f} conv over 12 weeks); "
            f"weekly Audience ratios are noisy and should be interpreted "
            f"with caution.",
            fontsize=9, style="italic", color="#8B6500",
            transform=ax_header.transAxes, va="top", ha="left", wrap=True,
        )

    # ── Panel 1: Stacked spend by network ─────────────────────────
    bar_width = 6
    ax_spend.bar(week_dts, sw["spend"], width=bar_width,
                 color="#4F81BD", alpha=0.85, edgecolor="#1F4E79",
                 linewidth=0.5, label="Search")
    ax_spend.bar(week_dts, aw["spend"], width=bar_width,
                 bottom=sw["spend"], color="#F79646", alpha=0.85,
                 edgecolor="#A0522D", linewidth=0.5, label="Audience")
    # Annotate Audience % share above each bar
    for i, wk in enumerate(week_dts):
        total = sw["spend"].iloc[i] + aw["spend"].iloc[i]
        if total > 0:
            aud_pct = aw["spend"].iloc[i] / total * 100
            ax_spend.annotate(f"{aud_pct:.0f}%",
                              xy=(wk, total),
                              xytext=(0, 3), textcoords="offset points",
                              ha="center", fontsize=7.5, color="#A0522D")
    ax_spend.set_ylabel("Weekly spend ($)", fontsize=10)
    ax_spend.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"${v:,.0f}"))
    ax_spend.legend(loc="best", fontsize=8, framealpha=0.9)
    ax_spend.grid(True, alpha=0.3, axis="y")
    ax_spend.set_title("Spend share by network (label = Audience %)",
                       fontsize=10, loc="left")

    # ── Panel 2: ROAS by network ──────────────────────────────────
    # Plot Search ROAS solid; Audience ROAS with marker alpha varying
    # by weekly conv volume to fade low-confidence points.
    ax_roas.plot(week_dts, sw["roas"], marker="o", color="#1F4E79",
                 linewidth=2, markersize=6, label="Search ROAS")
    # Audience line: split into high-confidence (solid) and low-conf
    # (dotted with faded markers) segments. Simplest approach: draw
    # the line normally, then overlay markers with per-point alpha.
    ax_roas.plot(week_dts, aw["roas"], color="#A0522D",
                 linewidth=2, alpha=0.7, label="Audience ROAS")
    for i, wk in enumerate(week_dts):
        roas_val = aw["roas"].iloc[i]
        conv_val = aw["conv"].iloc[i]
        if pd.isna(roas_val):
            continue
        if conv_val >= AUDIENCE_LOW_CONF_THRESHOLD:
            ax_roas.plot(wk, roas_val, marker="s", color="#F79646",
                         markersize=7, markeredgecolor="#A0522D")
        else:
            # Faded marker: low-confidence point
            ax_roas.plot(wk, roas_val, marker="s", color="#F79646",
                         markersize=6, alpha=0.3, markeredgecolor="#A0522D")
    ax_roas.axhline(1.0, color="#404040", linestyle=":", linewidth=1.0,
                    alpha=0.7, label="Break-even (1.0x)")
    ax_roas.set_ylabel("Weekly ROAS", fontsize=10)
    ax_roas.legend(loc="best", fontsize=8, framealpha=0.9)
    ax_roas.grid(True, alpha=0.3)
    ax_roas.set_title(
        f"ROAS by network (faded Audience markers = <{AUDIENCE_LOW_CONF_THRESHOLD} conv/wk, low confidence)",
        fontsize=10, loc="left")

    # ── Panel 3: Weekly profit by network (grouped bars) ──────────
    # Offset bars within each week: Search slightly left, Audience right
    offset = pd.Timedelta(days=1.5)
    week_dts_s = week_dts - offset
    week_dts_a = week_dts + offset
    s_colors = ["#9BBB59" if p >= 0 else "#C0504D" for p in sw["profit"]]
    a_colors = ["#F2B176" if p >= 0 else "#C0504D" for p in aw["profit"]]
    ax_profit.bar(week_dts_s, sw["profit"], width=3,
                  color=s_colors, alpha=0.85, edgecolor="#404040",
                  linewidth=0.5, label="Search profit")
    ax_profit.bar(week_dts_a, aw["profit"], width=3,
                  color=a_colors, alpha=0.85, edgecolor="#404040",
                  linewidth=0.5, label="Audience profit")
    ax_profit.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    ax_profit.set_ylabel("Weekly profit ($)", fontsize=10)
    ax_profit.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"${v:,.0f}"))
    ax_profit.set_xlabel("Week starting (Mon)", fontsize=10)
    ax_profit.legend(loc="best", fontsize=8, framealpha=0.9)
    ax_profit.grid(True, alpha=0.3, axis="y")
    ax_profit.set_title("Profit by network (side-by-side bars per week)",
                        fontsize=10, loc="left")

    # X-axis formatting on the bottom panel
    ax_profit.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
    ax_profit.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax_profit.xaxis.get_majorticklabels(), rotation=30, ha="right")
    # Hide x-axis labels on the upper panels (sharex)
    plt.setp(ax_spend.xaxis.get_majorticklabels(), visible=False)
    plt.setp(ax_roas.xaxis.get_majorticklabels(), visible=False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════
# ELASTICITY MODULE REUSE
# ═══════════════════════════════════════════════════════════════════

def _import_elasticity_module():
    """Import elasticity_regression_microsoft as a module, then monkey-
    patch its fetch_campaign_daily with our corrected version.

    Why monkey-patch: the elasticity script's fetcher predates the
    loader fix and sums device rows (no device='ALL' filter), which
    after the loader fix means ~2× the true conversions. Our corrected
    fetcher pulls device='ALL' + network='Audience' from both tables.
    Substituting it on the elasticity module means anything in this
    script that calls elasticity functions (specifically the chart
    builder) gets the corrected daily data.

    Returns the imported module, or None if the script can't be found
    on the import path or sys.path can't be set up. Caller should
    handle None by skipping the elasticity-chart panels.
    """
    import importlib
    import importlib.util

    # Look for the elasticity script in the same directory as this one
    here = Path(__file__).parent
    candidate_paths = [
        here / "elasticity_regression_microsoft.py",
        here / "analysis" / "elasticity_regression_microsoft.py",
        here.parent / "analysis" / "elasticity_regression_microsoft.py",
    ]
    target = next((p for p in candidate_paths if p.exists()), None)
    if target is None:
        # Last resort: regular import (might work if it's on PYTHONPATH)
        try:
            return importlib.import_module("elasticity_regression_microsoft")
        except ImportError:
            return None

    spec = importlib.util.spec_from_file_location(
        "elasticity_regression_microsoft", target
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # ── Monkey-patch: replace mod.fetch_campaign_daily with our corrected
    #    version. Crucially, the elasticity chart's downstream code expects
    #    a DataFrame with columns: report_date, spend_usd, conv, conv_value,
    #    impressions, rank_lost_is_avg. Our fetcher returns only the first
    #    four; we'll add zero-filled stubs for the other two since they
    #    don't affect the 3-panel chart.
    _our_fetcher = fetch_campaign_daily

    def _patched_fetch_campaign_daily(conn, campaign_name, start_date, end_date):
        df = _our_fetcher(conn, campaign_name, start_date, end_date)
        # Add stub columns the elasticity script's other functions might
        # reference. The chart builder uses only conv, cpa (derived from
        # spend_usd / conv), and conv_value, so these stubs are safe.
        if "impressions" not in df.columns:
            df = df.assign(impressions=0)
        if "rank_lost_is_avg" not in df.columns:
            df = df.assign(rank_lost_is_avg=None)
        return df

    mod.fetch_campaign_daily = _patched_fetch_campaign_daily
    return mod


def _load_tcpa_changes_for_elasticity(elasticity_mod, csv_path: Path) -> pd.DataFrame:
    """Use the elasticity module's tCPA-changes loader, or return empty
    if the module isn't available."""
    if elasticity_mod is None or not csv_path.exists():
        return pd.DataFrame(columns=["change_date", "campaign_name",
                                      "old_tcpa", "new_tcpa"])
    return elasticity_mod.load_tcpa_changes(csv_path)


# ═══════════════════════════════════════════════════════════════════
# TOTAL-ACCOUNT AGGREGATION
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
    columns. Used as input to aggregate_weekly() for weekly rollup,
    and to the elasticity chart builder as the "campaign" data
    for the Total Account sheet.

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


def compute_blended_tcpa(campaign_daily_map: dict[str, pd.DataFrame]) -> float | None:
    """Blended tCPA = Σ spend ÷ Σ conversions across all campaigns over
    the full window. This is the realized account-level CPA, weighted
    by where the spend actually went; used as the reference line on
    the Total Account CPA panel.

    Returns None if total conversions is 0.
    """
    total_spend = 0.0
    total_conv = 0.0
    for df in campaign_daily_map.values():
        if df is None or df.empty:
            continue
        total_spend += df["spend_usd"].sum()
        total_conv += df["conv"].sum()
    if total_conv <= 0:
        return None
    return total_spend / total_conv


# ═══════════════════════════════════════════════════════════════════
# TOTAL-ACCOUNT PROFIT CHART (for Overview sheet)
# ═══════════════════════════════════════════════════════════════════

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
# MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--change-history", default=str(DEFAULT_CHANGE_CSV))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--end-date", default=None,
                   help="Last day of analysis window (YYYY-MM-DD). If "
                        "omitted, the script uses the Sunday of the most "
                        "recent complete week loaded in "
                        "campaign_performance_daily.")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help=f"Number of weeks back from --end-date. Default: "
                        f"{DEFAULT_WEEKS}")
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path)
    change_csv = Path(args.change_history)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    n_weeks = args.weeks

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)

    # ── Resolve the analysis window ────────────────────────────────
    # If --end-date was supplied, honor it exactly. Otherwise auto-detect
    # the Sunday of the most recent complete week in the DB (no buffer).
    if args.end_date:
        end_date = date.fromisoformat(args.end_date)
        # Compute window as the N weeks ending in end_date's ISO week
        end_week_monday = end_date - timedelta(days=end_date.weekday())
        window_start = end_week_monday - timedelta(weeks=n_weeks - 1)
        window_end = end_date
        end_source = "user-supplied --end-date"
    else:
        last_week_start, last_week_end = fetch_latest_complete_week(conn)
        window_end = last_week_end                       # Sunday
        window_start = last_week_start - timedelta(weeks=n_weeks - 1)
        end_source = "auto-detected (last complete week in DB)"

    # ── Detect conversion column names per table ───────────────────
    # (No detection needed — Microsoft loader uses fixed column names
    #  `conversions`, `conversion_value`, `all_conversions`,
    #  `all_conversion_value` on both campaign performance tables.
    #  The Microsoft convention used here: CPA denominator is
    #  `conversions`; revenue numerator (RPL, ROAS, profit) is
    #  `all_conversion_value`.)

    banner("CAMPAIGN PERFORMANCE VISUALIZATIONS — Microsoft Home")
    print(f"  DB:                  {db_path}")
    print(f"  Change history:      {change_csv}")
    print(f"  Output:              {output_dir}")
    print(f"  Window:              {window_start} (Mon) to {window_end} (Sun)")
    print(f"  Weeks:               {n_weeks}")
    print(f"  End date source:     {end_source}")
    print()

    enabled = fetch_enabled_campaigns(conn)
    print(f"Found {len(enabled)} enabled campaigns")
    print()

    # Load change events filtered to the window (used to overlay vertical
    # change-event lines on the weekly per-campaign charts)
    change_events_df = load_change_events(change_csv, window_start, window_end)
    print(f"Loaded {len(change_events_df)} change events within window "
          f"({(change_events_df['attribute'] == 'TargetCPA').sum()} tCPA, "
          f"{change_events_df['attribute'].str.contains('Budget', na=False).sum()} budget)")
    print()

    # ── Import the elasticity module (for side-by-side daily charts) ──
    # If the import fails, we proceed without the elasticity panels.
    print("Loading elasticity module (for daily side-by-side charts)...")
    elasticity_mod = _import_elasticity_module()
    if elasticity_mod is None:
        print("  ⚠ elasticity_regression_microsoft.py not found — "
              "elasticity charts will be skipped")
    else:
        print("  ✓ Loaded + monkey-patched fetch_campaign_daily with "
              "device='ALL' fix")
    # Load full tCPA change history (not just within-window) — the
    # elasticity chart uses the full set to draw the tCPA step function
    # and shade past measurement windows.
    tcpa_changes_all = _load_tcpa_changes_for_elasticity(elasticity_mod, change_csv)
    print(f"  Loaded {len(tcpa_changes_all)} tCPA change events for "
          f"elasticity-chart history")
    print()

    # Process each campaign: fetch daily (combined + per-network), aggregate weekly
    campaign_daily: dict[str, pd.DataFrame] = {}
    campaign_search_daily: dict[str, pd.DataFrame] = {}
    campaign_audience_daily: dict[str, pd.DataFrame] = {}
    campaign_weekly: dict[str, pd.DataFrame] = {}
    for _, camp in enabled.iterrows():
        cname = camp["campaign_name"]
        combined, search_only, audience_only = fetch_campaign_daily_split(
            conn, cname, window_start, window_end
        )
        weekly = aggregate_weekly(combined)
        campaign_daily[cname] = combined
        campaign_search_daily[cname] = search_only
        campaign_audience_daily[cname] = audience_only
        campaign_weekly[cname] = weekly

    # ── Total-account rollup ─────────────────────────────────────
    total_daily = aggregate_total_account_daily(campaign_daily)
    total_weekly = aggregate_weekly(total_daily)
    blended_tcpa = compute_blended_tcpa(campaign_daily)
    print(f"Account-level rollup: {len(total_daily)} daily rows, "
          f"{len(total_weekly)} weeks")
    if blended_tcpa is not None:
        print(f"Blended tCPA (Σspend / Σconv across window): ${blended_tcpa:.2f}")
    print()

    # ── Rank campaigns by 12-week total profit (descending) ──────
    # Used both to sort sheets and to know which campaign goes where.
    profit_rank = sorted(
        ((cname, w["profit"].sum() if not w.empty else 0.0)
         for cname, w in campaign_weekly.items()),
        key=lambda kv: -kv[1],
    )
    print("Campaigns ranked by 12-week total profit:")
    for cname, profit in profit_rank:
        short = cname.replace("HOME INSURANCE-", "").split("-SEARCH")[0]
        print(f"  ${profit:>10,.0f}  {short}")
    print()

    # ── Group full-history tCPA changes by campaign (for elasticity charts) ──
    elasticity_events_by_campaign: dict[str, list[dict]] = {}
    if not tcpa_changes_all.empty:
        for _, c in tcpa_changes_all.iterrows():
            elasticity_events_by_campaign.setdefault(
                c["campaign_name"], []
            ).append({
                "change_date": c["change_date"],
                "old_tcpa": c["old_tcpa"],
                "new_tcpa": c["new_tcpa"],
            })

    # The elasticity chart wants a longer history than the 12-week
    # performance window. Match the elasticity script's convention:
    # start at the Microsoft attribution go-live date if reachable,
    # otherwise fall back to the window start.
    elasticity_start = date(2026, 1, 13)
    if elasticity_start > window_start:
        elasticity_start = window_start
    elasticity_end = window_end

    # Helper to build the elasticity chart for one campaign or for the
    # total account. Returns PNG bytes, or None if the elasticity module
    # is unavailable.
    def _build_elasticity_chart(campaign_name: str | None,
                                 current_tcpa: float | None,
                                 events: list[dict]) -> bytes | None:
        if elasticity_mod is None:
            return None
        try:
            if campaign_name is None:
                # Total-account view: synthesize a fake campaign that the
                # elasticity chart can render. We need to override its
                # fetcher to return the account total instead of one
                # campaign's data. Patch in a temporary fetcher.
                original_fetcher = elasticity_mod.fetch_campaign_daily

                def _account_fetcher(conn, cname, sd, ed):
                    # Recompute account daily for whatever window the
                    # chart asked for (likely wider than our default
                    # window_start..window_end).
                    cd_map = {}
                    for c_name in campaign_daily.keys():
                        cd_map[c_name] = fetch_campaign_daily(
                            conn, c_name, sd, ed
                        )
                    df = aggregate_total_account_daily(cd_map)
                    # Add stub columns the elasticity module expects
                    if "impressions" not in df.columns:
                        df = df.assign(impressions=0)
                    if "rank_lost_is_avg" not in df.columns:
                        df = df.assign(rank_lost_is_avg=None)
                    return df

                elasticity_mod.fetch_campaign_daily = _account_fetcher
                try:
                    return elasticity_mod.build_campaign_overview_chart_png(
                        conn=conn,
                        campaign_name="TOTAL ACCOUNT",
                        current_tcpa=current_tcpa,
                        start_date=elasticity_start,
                        end_date=elasticity_end,
                        change_events=events,
                    )
                finally:
                    elasticity_mod.fetch_campaign_daily = original_fetcher
            else:
                return elasticity_mod.build_campaign_overview_chart_png(
                    conn=conn,
                    campaign_name=campaign_name,
                    current_tcpa=current_tcpa,
                    start_date=elasticity_start,
                    end_date=elasticity_end,
                    change_events=events,
                )
        except Exception as exc:
            print(f"    (elasticity chart failed: {exc})")
            return None

    # Build workbook
    print("Building workbook...")
    wb = Workbook()
    wb.remove(wb.active)

    # ── 1. Overview sheet ────────────────────────────────────────
    # Layout: large total-account profit chart on the LEFT (anchored A4),
    # existing small-multiples grid on the RIGHT (anchored at col ~M).
    ws = wb.create_sheet("Overview")
    ws.cell(row=1, column=1,
            value="ACCOUNT-LEVEL OVERVIEW — Microsoft Home Insurance"
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
    # Anchor far enough right that the two images don't visually overlap.
    # The total chart is ~8 inches wide → ~16 default Excel columns.
    ws.add_image(img_grid, "M5")
    print("  ✓ Overview")

    # ── 2. Total Account sheet ───────────────────────────────────
    # 5-panel weekly chart (left) + 3-panel elasticity chart (right).
    # Below them: 3-panel network-split diagnostic chart (full width).
    # CPA panel uses BLENDED tCPA as the reference line.
    ws_total = wb.create_sheet("Total Account")
    ws_total.cell(row=1, column=1, value="TOTAL ACCOUNT").font = Font(bold=True, size=14)
    ws_total.cell(row=2, column=1,
                  value=f"Window: {window_start} to {window_end}. "
                        f"Blended tCPA (Σspend ÷ Σconv): "
                        f"${blended_tcpa:.2f}" if blended_tcpa else "")
    print("  Generating Total Account charts...")
    try:
        # 5-panel: pretend the total is "one campaign" so we can reuse
        # the same chart function. Pass blended_tcpa as the reference.
        weekly_png = build_4panel_campaign_chart_png(
            weekly_df=total_weekly,
            campaign_name="TOTAL ACCOUNT",
            current_tcpa=blended_tcpa,
            change_events=[],  # account-level: don't overlay individual
                               # campaigns' change events on this view
            window_start=window_start,
            window_end=window_end,
        )
        ws_total.add_image(XLImage(io.BytesIO(weekly_png)), "A4")
        # 3-panel elasticity, anchored to the right.
        elasticity_png = _build_elasticity_chart(
            campaign_name=None,
            current_tcpa=blended_tcpa,
            events=[],  # account-level: no single-campaign event lines
        )
        if elasticity_png is not None:
            ws_total.add_image(XLImage(io.BytesIO(elasticity_png)), "Q4")
        # Network-split diagnostic — account-level totals. Stacked below
        # the upper two charts (anchored at row ~78 to clear the 11"
        # weekly + elasticity charts above).
        total_search_daily = pd.concat(
            [df for df in campaign_search_daily.values() if not df.empty],
            ignore_index=True,
        ) if any(not df.empty for df in campaign_search_daily.values()) else pd.DataFrame(
            columns=["report_date", "spend_usd", "conv", "conv_value"]
        )
        total_audience_daily = pd.concat(
            [df for df in campaign_audience_daily.values() if not df.empty],
            ignore_index=True,
        ) if any(not df.empty for df in campaign_audience_daily.values()) else pd.DataFrame(
            columns=["report_date", "spend_usd", "conv", "conv_value"]
        )
        # Roll up to one row per date per network (account-level)
        if not total_search_daily.empty:
            total_search_daily = total_search_daily.groupby(
                "report_date", as_index=False
            ).sum(numeric_only=True)
        if not total_audience_daily.empty:
            total_audience_daily = total_audience_daily.groupby(
                "report_date", as_index=False
            ).sum(numeric_only=True)
        split_png = build_network_split_chart_png(
            search_daily=total_search_daily,
            audience_daily=total_audience_daily,
            campaign_name="TOTAL ACCOUNT",
            window_start=window_start,
            window_end=window_end,
        )
        ws_total.add_image(XLImage(io.BytesIO(split_png)), "A78")
        print("  ✓ Total Account")
    except Exception as exc:
        print(f"  ✗ Total Account: chart failed ({exc})")

    # ── 3. Per-campaign sheets (sorted by 12-week profit desc) ───
    print("  Generating per-campaign side-by-side charts...")
    enabled_by_name = {row["campaign_name"]: row for _, row in enabled.iterrows()}
    sheet_count = 0
    for cname, total_profit in profit_rank:
        camp = enabled_by_name.get(cname)
        if camp is None:
            continue
        current_tcpa = camp["current_tcpa"]
        if pd.isna(current_tcpa):
            current_tcpa = None
        weekly_df = campaign_weekly.get(cname, pd.DataFrame())
        # Within-window change events for the weekly chart's overlay lines
        camp_events_window = change_events_df[
            change_events_df["campaign_name"] == cname
        ].to_dict("records")
        # Full-history tCPA events for the elasticity chart's step function
        camp_events_full = elasticity_events_by_campaign.get(cname, [])

        # Sheet name: short campaign name (Excel limit 31 chars)
        short_name = cname.replace("HOME INSURANCE-", "").split("-SEARCH")[0][:25]
        sheet_name = short_name[:31]
        if sheet_name in [s.title for s in wb.worksheets]:
            sheet_name = f"{short_name[:28]}_2"[:31]

        try:
            ws_chart = wb.create_sheet(sheet_name)
            # 5-panel weekly chart (left)
            weekly_png = build_4panel_campaign_chart_png(
                weekly_df=weekly_df,
                campaign_name=cname,
                current_tcpa=current_tcpa,
                change_events=camp_events_window,
                window_start=window_start,
                window_end=window_end,
            )
            ws_chart.add_image(XLImage(io.BytesIO(weekly_png)), "A1")
            # 3-panel elasticity chart (right). Skip if module not loaded.
            elasticity_png = _build_elasticity_chart(
                campaign_name=cname,
                current_tcpa=current_tcpa,
                events=camp_events_full,
            )
            if elasticity_png is not None:
                ws_chart.add_image(XLImage(io.BytesIO(elasticity_png)), "Q1")
            # Network-split diagnostic — stacked below at row 75. Uses
            # the campaign's split daily data captured in the fetch loop.
            split_png = build_network_split_chart_png(
                search_daily=campaign_search_daily.get(
                    cname, pd.DataFrame(columns=["report_date", "spend_usd", "conv", "conv_value"])
                ),
                audience_daily=campaign_audience_daily.get(
                    cname, pd.DataFrame(columns=["report_date", "spend_usd", "conv", "conv_value"])
                ),
                campaign_name=cname,
                window_start=window_start,
                window_end=window_end,
            )
            ws_chart.add_image(XLImage(io.BytesIO(split_png)), "A75")
            sheet_count += 1
            print(f"  ✓ {sheet_name}  (12-wk profit: ${total_profit:,.0f})")
        except Exception as exc:
            print(f"  ✗ {sheet_name}: chart failed ({exc})")

    conn.close()
    print(f"Generated {sheet_count} per-campaign chart sheets")

    out_path = output_dir / f"campaign_performance_microsoft_{date.today().isoformat()}.xlsx"
    wb.save(out_path)
    print()
    print(f"  ✓ Wrote {out_path}")


if __name__ == "__main__":
    main()
