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
      Bottom:    4-panel network-split diagnostic — Search vs Audience
                 spend share, ROAS, profit, and impressions+CTR.
                 Header table shows 12-week totals per network
                 (including impressions, clicks, CTR) plus a
                 "tCPA ceiling at ROAS 1.2x" planning number. Built
                 to answer "should we split Audience out of this
                 campaign?"

The elasticity chart builder is reused via import from
elasticity_regression_microsoft.py. That script's own data fetcher
predates the loader fix and does NOT filter device='ALL', so this
script monkey-patches it at startup with a corrected fetcher to
guarantee the elasticity charts here read de-duplicated conversions.
The elasticity script's own outputs (regression coefficients, per-event
sheets) are NOT corrected by this patch — fix that script separately.

DEVICE NOTE (2026): Microsoft now serves a fourth device, CONNECTED_TV,
alongside DESKTOP/MOBILE/TABLET. Every query here filters device='ALL'
(the rollup that already includes CONNECTED_TV), so totals are correct.
Any FUTURE per-device breakdown must use `device != 'ALL'`, never a
hardcoded `device IN ('DESKTOP','MOBILE','TABLET')` list, which would
silently drop CONNECTED_TV.
"""

import argparse
import io
import re
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

def fetch_campaigns(conn) -> pd.DataFrame:
    """ENABLED + PAUSED campaigns with current tCPA and status.

    Paused campaigns are returned here and gated for activity at fetch time
    (see main): paused campaigns that contributed spend/revenue/GP in the
    window are KEPT, matching the data-tables workbook. Sorted ENABLED-first
    then by name for stable ordering.
    """
    sql = """
    SELECT
        campaign_id,
        campaign_name,
        status,
        target_cpa_micros / 1.0e6 AS current_tcpa
    FROM campaigns
    WHERE account_id = ?
      AND status IN ('ENABLED', 'PAUSED')
    ORDER BY status, campaign_name
    """
    df = pd.read_sql(sql, conn, params=(ACCOUNT_ID,))
    return df


# Backward-compatible alias (older callers referenced this name).
fetch_enabled_campaigns = fetch_campaigns


def short_code(campaign_name: str) -> str:
    """Short, collision-free identifier for a campaign — used for the
    Excel tab name and chart short-titles.

    Handles BOTH conventions in the account:
      NEW tier-group: '..._Tier2RPL65Plus_..._USA-STE' -> 'T2'
      OLD state-based: 'HOME INSURANCE-CALIFORNIA-...-USA-STE' -> 'CA'

    Tier branch runs FIRST (load-bearing — Tier 5 is the renamed Indiana
    shell, so a tier name can contain an embedded state token). Codes key
    off the tier NUMBER only; no two tiers share a number today, and the
    caller's dedup loop absorbs any future clash. This replaces the old
    truncate-at-25-chars tab naming, under which all four tier campaigns
    collapsed to the identical prefix 'HOME INSURANCE_STATE-GROU' and the
    3rd/4th tier sheets silently failed to render.
    """
    STATE_ABBR = {
        "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
        "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
        "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
        "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
        "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
        "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
        "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
        "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
        "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
        "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
        "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
        "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
        "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
        "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    }
    name_up = campaign_name.upper()
    m = re.search(r"TIER\s*(\d+)", name_up)
    if m:
        return f"T{m.group(1)}"
    for state, abbr in STATE_ABBR.items():
        if state in name_up:
            return abbr
    cleaned = "".join(c for c in name_up if c.isalnum())[:8]
    return cleaned or "CAMP"


def fetch_rename_boundaries(conn, window_start: date | None = None,
                            window_end: date | None = None) -> dict:
    """Return {campaign_name: [rename_date, ...]} for campaigns that changed
    name, filtered to the [window_start, window_end] range.

    Keyed by CURRENT campaign_name (matching how this script keys its
    per-campaign dicts). A "rename" is the valid_from of any history
    interval after the earliest (the earliest is the campaign's birth name).
    Only the restructured tier shells should appear. Used to draw a
    scope-change marker on the trend charts: pre-boundary weeks reflect the
    prior single-state scope, post-boundary weeks the consolidated tier group.

    SCHEMA ASSUMPTION (verify against live DB): `campaign_name_history` with
    (campaign_id, campaign_name, valid_from, valid_to), joinable to
    `campaigns` on campaign_id. Returns {} if the table is absent/malformed
    so charts still render without markers.
    """
    try:
        id_rows = conn.execute(
            """
            SELECT h.campaign_id, c.campaign_name, h.valid_from
            FROM campaign_name_history h
            JOIN campaigns c ON c.campaign_id = h.campaign_id
            WHERE c.account_id = ? AND c.status = 'ENABLED'
            ORDER BY h.campaign_id, h.valid_from
            """,
            (ACCOUNT_ID,),
        ).fetchall()
    except sqlite3.Error:
        return {}

    by_id: dict = {}
    name_of: dict = {}
    for cid, cname, vf in id_rows:
        name_of[cid] = cname
        if vf:
            by_id.setdefault(cid, []).append(vf)

    boundaries: dict = {}
    for cid, vfs in by_id.items():
        rename_dates = []
        for vf in sorted(vfs)[1:]:  # skip birth interval
            try:
                d = date.fromisoformat(vf)
            except (ValueError, TypeError):
                continue
            if window_start and d < window_start:
                continue
            if window_end and d > window_end:
                continue
            rename_dates.append(d)
        if rename_dates:
            boundaries[name_of[cid]] = rename_dates
    return boundaries


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
        cpd.all_conversion_value  AS conv_value,
        cpd.impressions           AS impressions,
        cpd.clicks                AS clicks
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
        cpnd.all_conversion_value  AS conv_value,
        cpnd.impressions           AS impressions,
        cpnd.clicks                AS clicks
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

    # ── Combined: merge on date, sum search + audience for all-network totals.
    #    Drop impressions/clicks before merging — they're only used in the
    #    per-network split chart and don't need to be in the combined view.
    df_search_for_merge = df_search.drop(
        columns=[c for c in ("impressions", "clicks") if c in df_search.columns]
    )
    df_audience_for_merge = df_audience.drop(
        columns=[c for c in ("impressions", "clicks") if c in df_audience.columns]
    )
    df_search_r = df_search_for_merge.rename(columns={
        "spend_usd": "search_spend", "conv": "search_conv",
        "conv_value": "search_conv_value",
    })
    df_audience_r = df_audience_for_merge.rename(columns={
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

    # df_search and df_audience retain impressions and clicks for the
    # network-split chart's panel 4 (impressions + CTR by network)
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

def fetch_change_events(conn, start_date: date, end_date: date,
                        kinds: tuple | None = None) -> pd.DataFrame:
    """Pull tCPA + budget change events from `campaign_change_events`,
    keyed by campaign_id, for the given date window.

    Replaces the stale name-based CSV. Joining on campaign_id — immutable
    across renames — fixes the marker-dropping bug: a change logged under a
    campaign's pre-rename (state) name still resolves to the now-tier
    campaign, because the id never changed.

    `kinds` selects change categories: subset of {'tcpa','budget'} or None
    for all. Matching is tolerant: tCPA = change_type LIKE %CPA%, budget =
    LIKE %BUDGET% (case-insensitive), so it works whether the table stores
    'TargetCPA'/'DAILY_BUDGET' or variants like 'TARGET_CPA'/'tcpa'. Platform
    match is case-insensitive too.

    SCHEMA ASSUMPTION (verify against live DB): table `campaign_change_events`
    with columns (campaign_id, platform, account_id, change_date, change_type,
    old_value, new_value, raw_event_count); old_value/new_value in dollars;
    raw_event_count > 1 marks a collapsed same-day budget chain. If the query
    fails (missing table/column), it prints ONE clear warning and returns an
    empty frame so charts still render — it does NOT fail silently.
    """
    cols = ["campaign_id", "change_date", "change_type",
            "old_value", "new_value", "raw_event_count"]
    like_for = {"tcpa": "%CPA%", "budget": "%BUDGET%"}
    sql = """
        SELECT campaign_id, change_date, change_type,
               old_value, new_value, raw_event_count
        FROM campaign_change_events
        WHERE LOWER(platform) = 'microsoft'
          AND account_id = ?
          AND change_date BETWEEN ? AND ?
    """
    params: list = [ACCOUNT_ID, start_date.isoformat(), end_date.isoformat()]
    if kinds:
        ors = " OR ".join("UPPER(change_type) LIKE ?" for _ in kinds)
        sql += f"   AND ({ors})\n"
        params += [like_for[k] for k in kinds]
    sql += "        ORDER BY campaign_id, change_date"
    try:
        df = pd.read_sql(sql, conn, params=params)
    except Exception as exc:
        print(f"  ⚠ campaign_change_events query failed — charts will have NO "
              f"change markers. Reason: {exc}")
        return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    # Normalize campaign_id to a consistent integer dtype. Critical: the table
    # may store it as TEXT; without this, the within-window filter
    # (change_events_df["campaign_id"] == cid) compares str vs int and silently
    # matches nothing — which is exactly the bug where the weekly chart lost its
    # markers while the (int-keyed) elasticity chart kept them.
    df["campaign_id"] = pd.to_numeric(df["campaign_id"], errors="coerce").astype("Int64")
    df = df[df["campaign_id"].notna()]
    df["change_date"] = pd.to_datetime(df["change_date"], errors="coerce").dt.date
    df = df[df["change_date"].notna()]
    for c in ("old_value", "new_value"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "raw_event_count" not in df.columns:
        df["raw_event_count"] = 1
    df["raw_event_count"] = (
        pd.to_numeric(df["raw_event_count"], errors="coerce").fillna(1).astype(int)
    )
    return df[cols].reset_index(drop=True)


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
    rename_dates: list | None = None,
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

    # Overlay change-event markers on all panels, labeled with the transition.
    # Events are id-resolved (see fetch_change_events), so pre-rename changes
    # land correctly on a renamed campaign's chart. Color: tCPA red dashed,
    # budget orange dotted. NOTE: for budget changes, read the impact from the
    # day AFTER the marker — the change day is a partial-day budget blend.
    def _fmt_money(v):
        try:
            return f"${float(v):,.0f}"
        except (TypeError, ValueError):
            return str(v)

    for ev in change_events:
        change_dt = pd.Timestamp(ev["change_date"])
        ctype = str(ev.get("change_type", ev.get("attribute", "")))
        is_tcpa = "CPA" in ctype.upper()
        is_budget = "BUDGET" in ctype.upper()
        if is_tcpa:
            color, ls = "red", "--"
        elif is_budget:
            color, ls = "#FF8C00", ":"
        else:
            color, ls = "gray", "--"
        for ax in (ax1, ax_roas, ax2, ax3, ax4):
            ax.axvline(change_dt, color=color, linestyle=ls, linewidth=1.2, alpha=0.6)
        # Transition label on the top panel
        old_v, new_v = ev.get("old_value"), ev.get("new_value")
        if is_tcpa:
            lbl = f"tCPA {_fmt_money(old_v)}→{_fmt_money(new_v)}"
        elif is_budget:
            lbl = f"Budget {_fmt_money(old_v)}→{_fmt_money(new_v)}"
            rc = ev.get("raw_event_count", 1) or 1
            if int(rc) > 1:
                lbl += f" (×{int(rc)})"
        else:
            lbl = ctype
        ax1.annotate(lbl, xy=(change_dt, 0.98),
                     xycoords=("data", "axes fraction"),
                     xytext=(2, 0), textcoords="offset points",
                     fontsize=6.5, color=color, rotation=90,
                     ha="left", va="top", alpha=0.9)

    # Overlay rename / scope-change boundary — visually distinct from the
    # tCPA/budget event lines (thick solid purple). Marks where this
    # campaign_id's population definition changed from a single state to the
    # consolidated tier group, so the step in the series reads as a scope
    # change rather than a performance change.
    for rd in (rename_dates or []):
        rename_dt = pd.Timestamp(rd)
        for ax in (ax1, ax_roas, ax2, ax3, ax4):
            ax.axvline(rename_dt, color="#7030A0", linestyle="-",
                       linewidth=2.2, alpha=0.85, zorder=5)
        # Label on the top panel only, to avoid clutter
        ax1.annotate("scope change\n(renamed)", xy=(rename_dt, 1.0),
                     xycoords=("data", "axes fraction"),
                     xytext=(4, -10), textcoords="offset points",
                     fontsize=7.5, color="#7030A0", fontweight="bold",
                     ha="left", va="top")

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
        short_name = short_code(campaign_name)
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
    week_start, spend, conv, conv_value, impressions, clicks, cpa, rpl,
    roas, profit, ctr per week. Used internally by
    build_network_split_chart_png.

    The daily_df is expected to have impressions and clicks columns
    (populated by fetch_campaign_daily_split). If they're missing —
    e.g., called with synthetic data in tests — they default to 0
    and CTR will be None.
    """
    cols = ["week_start", "spend", "conv", "conv_value", "impressions",
            "clicks", "cpa", "rpl", "roas", "profit", "ctr"]
    if daily_df.empty:
        return pd.DataFrame(columns=cols)
    df = daily_df.copy()
    # Defensive: tolerate input frames that don't carry impressions/clicks
    if "impressions" not in df.columns:
        df["impressions"] = 0
    if "clicks" not in df.columns:
        df["clicks"] = 0
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["week_start"] = (df["report_date"]
                        - pd.to_timedelta(df["report_date"].dt.weekday, unit="D"))
    df["week_start"] = df["week_start"].dt.date
    agg = df.groupby("week_start").agg(
        spend=("spend_usd", "sum"),
        conv=("conv", "sum"),
        conv_value=("conv_value", "sum"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
    ).reset_index()
    agg["cpa"] = agg.apply(
        lambda r: r["spend"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["rpl"] = agg.apply(
        lambda r: r["conv_value"] / r["conv"] if r["conv"] > 0 else None, axis=1)
    agg["roas"] = agg.apply(
        lambda r: r["conv_value"] / r["spend"] if r["spend"] > 0 else None, axis=1)
    agg["profit"] = agg["conv_value"] - agg["spend"]
    agg["ctr"] = agg.apply(
        lambda r: r["clicks"] / r["impressions"] if r["impressions"] > 0 else None,
        axis=1,
    )
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
                    "tcpa_at_roas_1_2": None,
                    "impressions": 0, "clicks": 0, "ctr": None}
        spend = float(df["spend_usd"].sum())
        conv = float(df["conv"].sum())
        rev = float(df["conv_value"].sum())
        impressions = int(df["impressions"].sum()) if "impressions" in df.columns else 0
        clicks = int(df["clicks"].sum()) if "clicks" in df.columns else 0
        cpa = (spend / conv) if conv > 0 else None
        rpl = (rev / conv) if conv > 0 else None
        roas = (rev / spend) if spend > 0 else None
        profit = rev - spend
        tcpa_ceiling = (rpl / 1.2) if rpl is not None else None
        ctr = (clicks / impressions) if impressions > 0 else None
        return {"spend": spend, "conv": conv, "conv_value": rev,
                "cpa": cpa, "rpl": rpl, "roas": roas, "profit": profit,
                "tcpa_at_roas_1_2": tcpa_ceiling,
                "impressions": impressions, "clicks": clicks, "ctr": ctr}
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
            for col in ("spend", "conv", "conv_value", "impressions",
                        "clicks", "profit"):
                empty[col] = 0.0
            for col in ("cpa", "rpl", "roas", "ctr"):
                empty[col] = None
            return empty
        return (df.set_index("week_start")
                  .reindex(all_weeks)
                  .reset_index()
                  .fillna({"spend": 0.0, "conv": 0.0, "conv_value": 0.0,
                           "impressions": 0, "clicks": 0, "profit": 0.0}))
    sw = _reindex(search_weekly)
    aw = _reindex(audience_weekly)

    # Build figure: header area (table) + 4 chart panels
    # Header is now slightly taller because we added impressions/clicks/CTR
    # rows to the summary table.
    fig = plt.figure(figsize=(11, 16.5))
    gs = fig.add_gridspec(
        5, 1, height_ratios=[1.10, 1, 1, 1, 1], hspace=0.32,
    )
    ax_header = fig.add_subplot(gs[0])
    ax_spend = fig.add_subplot(gs[1])
    ax_roas = fig.add_subplot(gs[2], sharex=ax_spend)
    ax_profit = fig.add_subplot(gs[3], sharex=ax_spend)
    ax_imp = fig.add_subplot(gs[4], sharex=ax_spend)

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
    def _fmt_pct(v):
        if v is None: return "—"
        return f"{v*100:.2f}%"

    s = stats["search"]; a = stats["audience"]
    table_rows = [
        ["Metric (12-wk total)",  "Search",                "Audience"],
        ["Spend",                 _fmt_money(s["spend"]),   _fmt_money(a["spend"])],
        ["Impressions",           _fmt_num(s["impressions"]), _fmt_num(a["impressions"])],
        ["Clicks",                _fmt_num(s["clicks"]),    _fmt_num(a["clicks"])],
        ["CTR",                   _fmt_pct(s["ctr"]),       _fmt_pct(a["ctr"])],
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
    # 8% headroom so the Audience % annotations clear the panel top
    max_total_spend = (sw["spend"] + aw["spend"]).max()
    if max_total_spend > 0:
        ax_spend.set_ylim(top=max_total_spend * 1.08)

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
    ax_profit.legend(loc="best", fontsize=8, framealpha=0.9)
    ax_profit.grid(True, alpha=0.3, axis="y")
    ax_profit.set_title("Profit by network (side-by-side bars per week)",
                        fontsize=10, loc="left")

    # ── Panel 4: Impressions (stacked bars) + CTR (overlaid lines) ─
    # Twin-y so impressions go on the left axis (raw scale) and CTR on
    # the right axis (percentage). Impressions are stacked Search-bottom,
    # Audience-top; the bar tops are labeled with Audience % share, same
    # pattern as the spend panel. Two line overlays show Search and
    # Audience CTR on the right axis.
    ax_imp.bar(week_dts, sw["impressions"], width=6,
               color="#4F81BD", alpha=0.85, edgecolor="#1F4E79",
               linewidth=0.5, label="Search impressions")
    ax_imp.bar(week_dts, aw["impressions"], width=6,
               bottom=sw["impressions"], color="#F79646", alpha=0.85,
               edgecolor="#A0522D", linewidth=0.5, label="Audience impressions")
    # Annotate Audience % impression share above each bar
    for i, wk in enumerate(week_dts):
        total_imp = sw["impressions"].iloc[i] + aw["impressions"].iloc[i]
        if total_imp > 0:
            aud_pct = aw["impressions"].iloc[i] / total_imp * 100
            ax_imp.annotate(f"{aud_pct:.0f}%",
                            xy=(wk, total_imp),
                            xytext=(0, 3), textcoords="offset points",
                            ha="center", fontsize=7.5, color="#A0522D")
    ax_imp.set_ylabel("Weekly impressions", fontsize=10)
    ax_imp.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"{v:,.0f}"))
    ax_imp.grid(True, alpha=0.3, axis="y")
    # Give the y-axis ~8% headroom so the Audience % annotations sitting
    # above each stacked bar aren't clipped by the panel boundary.
    max_total_imp = (sw["impressions"] + aw["impressions"]).max()
    if max_total_imp > 0:
        ax_imp.set_ylim(top=max_total_imp * 1.08)
    # CTR overlay on twin axis
    ax_ctr = ax_imp.twinx()
    ax_ctr.plot(week_dts, [c * 100 if c is not None else None for c in sw["ctr"]],
                marker="o", color="#1F4E79", linewidth=2, markersize=6,
                label="Search CTR")
    ax_ctr.plot(week_dts, [c * 100 if c is not None else None for c in aw["ctr"]],
                marker="s", color="#A0522D", linewidth=2, markersize=6,
                label="Audience CTR")
    ax_ctr.set_ylabel("CTR (%)", fontsize=10)
    ax_ctr.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, p: f"{v:.2f}%"))
    # Combine legends from both axes into one. Anchor at lower-right so
    # the legend doesn't collide with the Audience-% annotations at the
    # top of the stacked bars.
    h1, l1 = ax_imp.get_legend_handles_labels()
    h2, l2 = ax_ctr.get_legend_handles_labels()
    ax_imp.legend(h1 + h2, l1 + l2, loc="lower right",
                  fontsize=8, framealpha=0.9)
    ax_imp.set_title("Impressions by network (bars, label = Audience %) + CTR (lines)",
                     fontsize=10, loc="left")
    ax_imp.set_xlabel("Week starting (Mon)", fontsize=10)

    # X-axis formatting on the new bottom panel (impressions)
    ax_imp.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
    ax_imp.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax_imp.xaxis.get_majorticklabels(), rotation=30, ha="right")
    # Hide x-axis labels on the upper panels (sharex)
    plt.setp(ax_spend.xaxis.get_majorticklabels(), visible=False)
    plt.setp(ax_roas.xaxis.get_majorticklabels(), visible=False)
    plt.setp(ax_profit.xaxis.get_majorticklabels(), visible=False)

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
    p.add_argument("--change-history", default=str(DEFAULT_CHANGE_CSV),
                   help="DEPRECATED / unused. Change events now come from the "
                        "campaign_change_events DB table; this flag is kept "
                        "only so existing invocations don't error.")
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
    _change_csv_unused = Path(args.change_history)  # retained for back-compat; not read
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
    print(f"  Change history:      campaign_change_events table (id-keyed)")
    print(f"  Output:              {output_dir}")
    print(f"  Window:              {window_start} (Mon) to {window_end} (Sun)")
    print(f"  Weeks:               {n_weeks}")
    print(f"  End date source:     {end_source}")
    print()

    campaigns_df = fetch_campaigns(conn)
    n_enabled = int((campaigns_df["status"] == "ENABLED").sum())
    n_paused = int((campaigns_df["status"] == "PAUSED").sum())
    print(f"Found {n_enabled} ENABLED + {n_paused} PAUSED candidate campaigns")
    print()

    # Load change events within the window from campaign_change_events,
    # keyed by campaign_id (used to overlay labeled vertical lines on the
    # per-campaign charts). id-keyed so pre-rename changes resolve correctly.
    change_events_df = fetch_change_events(conn, window_start, window_end)
    if change_events_df.empty:
        n_tcpa = n_budget = 0
    else:
        ct = change_events_df["change_type"].astype(str).str.upper()
        n_tcpa = int(ct.str.contains("CPA").sum())
        n_budget = int(ct.str.contains("BUDGET").sum())
    print(f"Loaded {len(change_events_df)} change events within window "
          f"({n_tcpa} tCPA, {n_budget} budget) from campaign_change_events")
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
    # Full-history tCPA changes for the elasticity step function, id-keyed,
    # over the wider elasticity window (back to attribution go-live). Pulled
    # from campaign_change_events so renamed campaigns keep their full history.
    elasticity_events_start = min(date(2026, 1, 13), window_start)
    tcpa_changes_all = fetch_change_events(
        conn, elasticity_events_start, window_end, kinds=("tcpa",)
    )
    print(f"  Loaded {len(tcpa_changes_all)} tCPA change events (id-keyed) "
          f"for elasticity-chart history")
    if change_events_df.empty and tcpa_changes_all.empty:
        print("  ⚠ NO change events returned from campaign_change_events for "
              "account_id=2. Charts will be marker-free. Verify the table is "
              "populated and the column/value assumptions match (see notes).")
    print()

    # Process each campaign: fetch daily (combined + per-network), aggregate weekly.
    # Inclusion gate mirrors the data-tables workbook so both list the same set:
    # include any campaign (ENABLED or PAUSED) that had spend OR revenue in the
    # 12-week window. No recency/status condition — financial contribution only.
    campaign_daily: dict[str, pd.DataFrame] = {}
    campaign_search_daily: dict[str, pd.DataFrame] = {}
    campaign_audience_daily: dict[str, pd.DataFrame] = {}
    campaign_weekly: dict[str, pd.DataFrame] = {}
    campaign_status: dict[str, str] = {}
    n_incl = n_skip_no_contrib = 0

    for _, camp in campaigns_df.iterrows():
        cname = camp["campaign_name"]
        status = camp["status"]
        combined, search_only, audience_only = fetch_campaign_daily_split(
            conn, cname, window_start, window_end
        )

        # Inclusion gate: any spend OR any revenue in the window.
        # combined carries spend_usd and conv_value (= all_conversion_value).
        win_spend = (float(combined["spend_usd"].sum())
                     if (not combined.empty and "spend_usd" in combined.columns) else 0.0)
        win_rev = (float(combined["conv_value"].sum())
                   if (not combined.empty and "conv_value" in combined.columns) else 0.0)
        if win_spend < 1e-6 and win_rev < 1e-6:
            n_skip_no_contrib += 1
            continue

        weekly = aggregate_weekly(combined)
        campaign_daily[cname] = combined
        campaign_search_daily[cname] = search_only
        campaign_audience_daily[cname] = audience_only
        campaign_weekly[cname] = weekly
        campaign_status[cname] = status
        n_incl += 1

    print(f"Campaigns included: {n_incl} "
          f"(skipped, no spend or revenue in window: {n_skip_no_contrib})")

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
        short = short_code(cname)
        print(f"  ${profit:>10,.0f}  {short}")
    print()

    # ── Group full-history tCPA changes by campaign_id (for elasticity charts) ──
    # Keyed by campaign_id (stable across renames), not name. old_value/new_value
    # map to old_tcpa/new_tcpa for the elasticity module's step-function contract.
    elasticity_events_by_campaign: dict[int, list[dict]] = {}
    if not tcpa_changes_all.empty:
        for _, c in tcpa_changes_all.iterrows():
            elasticity_events_by_campaign.setdefault(
                int(c["campaign_id"]), []
            ).append({
                "change_date": c["change_date"],
                "old_tcpa": float(c["old_value"]) if pd.notna(c["old_value"]) else None,
                "new_tcpa": float(c["new_value"]) if pd.notna(c["new_value"]) else None,
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
    enabled_by_name = {row["campaign_name"]: row for _, row in campaigns_df.iterrows()}
    # Scope-change boundaries (restructured tier shells only), keyed by
    # current name, filtered to the window. {} if history table unavailable.
    rename_by_name = fetch_rename_boundaries(conn, window_start, window_end)
    if rename_by_name:
        print(f"  Scope-change boundaries in window: "
              f"{sum(len(v) for v in rename_by_name.values())} across "
              f"{len(rename_by_name)} campaign(s)")
    used_tab_names: set = set()
    sheet_count = 0
    for cname, total_profit in profit_rank:
        camp = enabled_by_name.get(cname)
        if camp is None:
            continue
        current_tcpa = camp["current_tcpa"]
        if pd.isna(current_tcpa):
            current_tcpa = None
        weekly_df = campaign_weekly.get(cname, pd.DataFrame())
        cid = int(camp["campaign_id"])
        # Within-window change events (id-keyed so pre-rename changes resolve)
        if change_events_df.empty:
            camp_events_window = []
        else:
            camp_events_window = change_events_df[
                change_events_df["campaign_id"] == cid
            ].to_dict("records")
        # Full-history tCPA events for the elasticity chart's step function
        camp_events_full = elasticity_events_by_campaign.get(cid, [])
        camp_rename_dates = rename_by_name.get(cname, [])
        is_paused = campaign_status.get(cname) == "PAUSED"
        disp_name = f"{cname}  [PAUSED]" if is_paused else cname

        # Tab name: tier-aware short code (T1/T2/.. or state abbr), with a
        # robust dedup loop. Replaces the old 25-char truncation that
        # collapsed all tier campaigns to one identical, colliding name.
        base_tab = short_code(cname)[:31]
        sheet_name = base_tab
        suffix = 2
        while sheet_name in used_tab_names:
            sheet_name = f"{base_tab}_{suffix}"[:31]
            suffix += 1
        used_tab_names.add(sheet_name)

        try:
            ws_chart = wb.create_sheet(sheet_name)
            # 5-panel weekly chart (left)
            weekly_png = build_4panel_campaign_chart_png(
                weekly_df=weekly_df,
                campaign_name=disp_name,
                current_tcpa=current_tcpa,
                change_events=camp_events_window,
                window_start=window_start,
                window_end=window_end,
                rename_dates=camp_rename_dates,
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
