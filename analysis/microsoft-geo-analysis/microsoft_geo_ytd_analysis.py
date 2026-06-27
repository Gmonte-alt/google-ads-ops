"""
Microsoft Geo Performance Analysis — Home Insurance (account_id=2)

Assesses each state's fitness within the existing ENABLED-campaign structure.

The campaigns ARE the tiers. Microsoft Home campaigns are multi-state groupings;
campaign names (e.g., "HOME INSURANCE-VIRGINIA-...") are misleading — each
ENABLED campaign typically targets multiple states. Tier assignment isn't
something we derive from the data; it's the campaign each state is already in.

What this analysis answers:
  - Which ENABLED campaign currently drives each state's spend?
  - What's that campaign's current tCPA?
  - What's the state's individual ideal tCPA (cpl_at_150_roas)?
  - Is the state well-fit within its campaign, underbid, or overbid?
  - Are campaigns homogeneous groupings (similar-economics states) or
    heterogeneous (states with widely different RPLs grouped together)?

Window: trailing 12 weeks for primary metrics, trailing 30 days for recency
signal. End date auto-resolves to max(date) if data lags, else yesterday.

Sheets:
  1. State Fitness (Combined)  — primary deliverable, cross-network
  2. Campaign Summary          — campaign-level homogeneity diagnostic
  3. State Fitness (Search)    — same as Sheet 1, search-network only
  4. State Fitness (Audience)  — same as Sheet 1, audience-network only
  5. Methodology               — windows, definitions, caveats

Usage:
    python microsoft_geo_ytd_analysis.py [--dry-run] [--end-date YYYY-MM-DD]
                                          [--db-path PATH]
                                          [--print-networks-only]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DEFAULT_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\analysis\microsoft-geo-analysis\output"

ACCOUNT_ID = 2
LOCATION_TYPE = "PHYSICAL"
DEVICE = "ALL"

CLICK_THRESHOLD = 200
TARGET_ROAS = 1.50
FITNESS_TOLERANCE = 0.20  # ±20% of campaign tCPA = well_fit

# Network mapping confirmed via --print-networks-only on 2026-05-14
SEARCH_NETWORKS: set[str] = {
    "Microsoft sites and select traffic",
    "AOL search",
    "Syndicated search partners",
}
AUDIENCE_NETWORKS: set[str] = {
    "Audience",
}

# Workbook styling — matches Google script convention (purple 7030A0 / white)
HEADER_FILL = PatternFill("solid", start_color="7030A0", end_color="7030A0")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial")
BODY_FONT = Font(name="Arial")
TITLE_FONT = Font(bold=True, size=14, name="Arial")

# ----------------------------------------------------------------------------
# Column definitions per sheet
# ----------------------------------------------------------------------------

STATE_FITNESS_COLUMNS = [
    "state",
    "current_campaign_id",
    "current_campaign_name",
    "current_campaign_tcpa",
    "n_campaigns_active",
    "bid_strategy_mix",
    "clicks_12w",
    "spend_12w",
    "conversions_12w",
    "revenue_12w",
    "cpa_12w",
    "roas_12w",
    "rpl_12w",
    "cpl_at_150_roas",
    "gap_state_vs_campaign_tcpa",
    "fitness_flag",
    "derived_tier_profile",
    "clicks_30d",
    "cpa_30d",
    "roas_30d",
    "below_threshold_reason",
]

CAMPAIGN_SUMMARY_COLUMNS = [
    "campaign_id",
    "campaign_name",
    "current_tcpa",
    "n_states_active",
    "states_list",
    "campaign_spend_12w",
    "campaign_conversions_12w",
    "campaign_revenue_12w",
    "campaign_cpa_12w",
    "campaign_roas_12w",
    "campaign_weighted_rpl",
    "campaign_ideal_tcpa",
    "tcpa_gap_campaign_level",
    "rpl_min",
    "rpl_max",
    "rpl_spread_ratio",
    "tier_mix",
    "heterogeneity_flag",
]

# Number formats
DOLLAR_COLS = {
    "spend_12w", "revenue_12w", "cpa_12w", "rpl_12w", "cpl_at_150_roas",
    "current_campaign_tcpa", "cpa_30d", "gap_state_vs_campaign_tcpa",
    "campaign_spend_12w", "campaign_revenue_12w", "campaign_cpa_12w",
    "campaign_weighted_rpl", "campaign_ideal_tcpa", "tcpa_gap_campaign_level",
    "rpl_min", "rpl_max", "current_tcpa",
}
RATIO_COLS = {"roas_12w", "roas_30d", "campaign_roas_12w", "rpl_spread_ratio"}
INTEGER_COLS = {
    "clicks_12w", "conversions_12w", "clicks_30d", "n_campaigns_active",
    "n_states_active", "campaign_conversions_12w",
}


# ----------------------------------------------------------------------------
# Date window resolution
# ----------------------------------------------------------------------------

@dataclass
class Windows:
    end_date: date
    window_12w_start: date
    window_30d_start: date


def resolve_end_date(conn: sqlite3.Connection, override: str | None) -> date:
    if override:
        return date.fromisoformat(override)
    row = conn.execute(
        """
        SELECT MAX(date) FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ?
        """,
        (ACCOUNT_ID, LOCATION_TYPE),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(
            f"No rows in geo_performance_daily for account_id={ACCOUNT_ID}"
        )
    max_date = date.fromisoformat(row[0])
    yesterday = date.today() - timedelta(days=1)
    return yesterday if max_date == yesterday else max_date


def build_windows(end_date: date) -> Windows:
    return Windows(
        end_date=end_date,
        window_12w_start=end_date - timedelta(days=83),
        window_30d_start=end_date - timedelta(days=29),
    )


# ----------------------------------------------------------------------------
# Data pulls
# ----------------------------------------------------------------------------

def pull_enabled_campaigns(conn: sqlite3.Connection) -> pd.DataFrame:
    """ENABLED campaigns only. tCPA in dollars."""
    df = pd.read_sql_query(
        """
        SELECT
            campaign_id, campaign_name, bidding_strategy_type,
            target_cpa_micros, status
        FROM campaigns
        WHERE account_id = ? AND status = 'ENABLED'
        """,
        conn, params=(ACCOUNT_ID,),
    )
    df["current_tcpa"] = df["target_cpa_micros"].apply(
        lambda x: x / 1e6 if pd.notna(x) and x > 0 else None
    )
    return df


def print_distinct_networks(conn: sqlite3.Connection) -> None:
    df = pd.read_sql_query(
        """
        SELECT network, COUNT(*) AS row_count, SUM(impressions) AS impressions
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ?
        GROUP BY network ORDER BY impressions DESC
        """,
        conn, params=(ACCOUNT_ID, LOCATION_TYPE),
    )
    print("\nDistinct network values:")
    print(df.to_string(index=False))


def pull_state_campaign_spend(
    conn: sqlite3.Connection, start: date, end: date,
    enabled_campaign_ids: list[str],
) -> pd.DataFrame:
    """Per (state, campaign_id) spend for ENABLED campaigns only, in window."""
    if not enabled_campaign_ids:
        return pd.DataFrame(columns=["state", "campaign_id", "spend"])
    placeholders = ",".join("?" for _ in enabled_campaign_ids)
    sql = f"""
        SELECT
            geo_state AS state, campaign_id,
            SUM(cost_micros) / 1e6 AS spend,
            SUM(clicks) AS clicks,
            SUM(conversions) AS conversions,
            SUM(conversion_value) AS revenue
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ? AND device = ?
          AND date BETWEEN ? AND ?
          AND campaign_id IN ({placeholders})
        GROUP BY geo_state, campaign_id
    """
    params = [
        ACCOUNT_ID, LOCATION_TYPE, DEVICE,
        start.isoformat(), end.isoformat(),
        *enabled_campaign_ids,
    ]
    return pd.read_sql_query(sql, conn, params=params)


def pull_state_aggregates(
    conn: sqlite3.Connection, start: date, end: date,
    enabled_campaign_ids: list[str],
    networks: set[str] | None = None,
) -> pd.DataFrame:
    """Per-state aggregates filtered to ENABLED campaigns and optional network subset."""
    if not enabled_campaign_ids:
        return pd.DataFrame(columns=[
            "state", "clicks", "spend", "conversions", "revenue", "impressions"
        ])
    cp = ",".join("?" for _ in enabled_campaign_ids)
    network_clause = ""
    extra_params: list = []
    if networks is not None:
        if not networks:
            return pd.DataFrame(columns=[
                "state", "clicks", "spend", "conversions", "revenue", "impressions"
            ])
        np_ = ",".join("?" for _ in networks)
        network_clause = f" AND network IN ({np_})"
        extra_params = sorted(networks)
    sql = f"""
        SELECT
            geo_state AS state,
            SUM(clicks) AS clicks,
            SUM(cost_micros) / 1e6 AS spend,
            SUM(conversions) AS conversions,
            SUM(conversion_value) AS revenue,
            SUM(impressions) AS impressions
        FROM geo_performance_daily
        WHERE account_id = ? AND location_type = ? AND device = ?
          AND date BETWEEN ? AND ?
          AND campaign_id IN ({cp})
          {network_clause}
        GROUP BY geo_state
    """
    params = [
        ACCOUNT_ID, LOCATION_TYPE, DEVICE,
        start.isoformat(), end.isoformat(),
        *enabled_campaign_ids,
        *extra_params,
    ]
    return pd.read_sql_query(sql, conn, params=params)


# ----------------------------------------------------------------------------
# Metric helpers
# ----------------------------------------------------------------------------

def safe_div(num, den):
    if hasattr(num, "where"):
        den_safe = den.where(den > 0)
        return num / den_safe
    if not den or pd.isna(den):
        return float("nan")
    return num / den


# ----------------------------------------------------------------------------
# State-to-campaign assignment
# ----------------------------------------------------------------------------

def assign_primary_campaign(
    state_campaign_spend: pd.DataFrame, campaigns: pd.DataFrame,
) -> pd.DataFrame:
    """Per state: pick ENABLED campaign with highest spend share; tag n_active and strategy mix."""
    if state_campaign_spend.empty:
        return pd.DataFrame(columns=[
            "state", "current_campaign_id", "current_campaign_name",
            "current_campaign_tcpa", "n_campaigns_active", "bid_strategy_mix",
        ])

    active = state_campaign_spend[state_campaign_spend["spend"] > 0]
    n_active = (
        active.groupby("state")["campaign_id"].nunique()
        .rename("n_campaigns_active").reset_index()
    )
    primary = (
        active.sort_values(["state", "spend"], ascending=[True, False])
        .groupby("state").head(1)[["state", "campaign_id"]]
        .rename(columns={"campaign_id": "current_campaign_id"})
    )
    primary = primary.merge(
        campaigns[["campaign_id", "campaign_name", "current_tcpa", "bidding_strategy_type"]],
        left_on="current_campaign_id", right_on="campaign_id", how="left",
    ).rename(columns={
        "campaign_name": "current_campaign_name",
        "current_tcpa": "current_campaign_tcpa",
    }).drop(columns=["campaign_id"])

    state_strategies = (
        active.merge(
            campaigns[["campaign_id", "bidding_strategy_type"]],
            on="campaign_id", how="left",
        ).groupby("state")["bidding_strategy_type"]
        .apply(lambda s: sorted(set(s.dropna())))
        .reset_index()
    )
    def _mix(s):
        if not s: return ""
        return s[0] if len(s) == 1 else "Mixed"
    state_strategies["bid_strategy_mix"] = state_strategies["bidding_strategy_type"].apply(_mix)
    state_strategies = state_strategies[["state", "bid_strategy_mix"]]

    out = (
        primary.merge(n_active, on="state", how="left")
        .merge(state_strategies, on="state", how="left")
        .drop(columns=["bidding_strategy_type"])
    )
    return out


# ----------------------------------------------------------------------------
# State-level metric assembly + fitness assessment
# ----------------------------------------------------------------------------

def build_state_metrics(
    cross_12w: pd.DataFrame, cross_30d: pd.DataFrame,
    primary_assignment: pd.DataFrame,
) -> pd.DataFrame:
    df = cross_12w.rename(columns={
        "clicks": "clicks_12w", "spend": "spend_12w",
        "conversions": "conversions_12w", "revenue": "revenue_12w",
    }).drop(columns=["impressions"], errors="ignore")

    recent = cross_30d.rename(columns={
        "clicks": "clicks_30d", "spend": "spend_30d",
        "conversions": "conversions_30d", "revenue": "revenue_30d",
    })
    df = df.merge(
        recent[["state", "clicks_30d", "spend_30d", "conversions_30d", "revenue_30d"]],
        on="state", how="left",
    )
    for c in ["clicks_30d", "spend_30d", "conversions_30d", "revenue_30d"]:
        df[c] = df[c].fillna(0)

    df = df.merge(primary_assignment, on="state", how="left")
    none_mask = df["current_campaign_id"].isna()
    df.loc[none_mask, "current_campaign_id"] = "NONE_ACTIVE"
    df.loc[none_mask, "current_campaign_name"] = "NONE_ACTIVE"
    df.loc[none_mask, "n_campaigns_active"] = 0
    df.loc[none_mask, "bid_strategy_mix"] = ""

    df["cpa_12w"] = safe_div(df["spend_12w"], df["conversions_12w"])
    df["roas_12w"] = safe_div(df["revenue_12w"], df["spend_12w"])
    df["rpl_12w"] = safe_div(df["revenue_12w"], df["conversions_12w"])
    df["cpl_at_150_roas"] = df["rpl_12w"] / TARGET_ROAS
    df["cpa_30d"] = safe_div(df["spend_30d"], df["conversions_30d"])
    df["roas_30d"] = safe_div(df["revenue_30d"], df["spend_30d"])
    df["gap_state_vs_campaign_tcpa"] = (
        df["cpl_at_150_roas"] - df["current_campaign_tcpa"]
    )
    return df


def apply_fitness_flag(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fitness_flag"] = ""
    df["below_threshold_reason"] = ""

    is_unknown = df["state"].str.upper() == "UNKNOWN"
    df.loc[is_unknown, "below_threshold_reason"] = "UNKNOWN (geolocation failure)"
    df.loc[is_unknown, "fitness_flag"] = "low_volume"

    below = (df["clicks_12w"] < CLICK_THRESHOLD) & ~is_unknown
    df.loc[below, "below_threshold_reason"] = (
        df.loc[below, "clicks_12w"]
        .apply(lambda c: f"<{CLICK_THRESHOLD} clicks (actual: {int(c)})")
    )
    df.loc[below, "fitness_flag"] = "low_volume"

    elig = (
        (df["fitness_flag"] == "")
        & df["current_campaign_tcpa"].notna()
        & df["cpl_at_150_roas"].notna()
        & (df["current_campaign_tcpa"] > 0)
    )
    ref = df.loc[elig, "current_campaign_tcpa"]
    ideal = df.loc[elig, "cpl_at_150_roas"]
    ratio = ideal / ref
    flag = pd.Series(index=ref.index, dtype=object)
    flag[(ratio >= 1 - FITNESS_TOLERANCE) & (ratio <= 1 + FITNESS_TOLERANCE)] = "well_fit"
    flag[ratio > 1 + FITNESS_TOLERANCE] = "underbid"
    flag[ratio < 1 - FITNESS_TOLERANCE] = "overbid"
    df.loc[elig, "fitness_flag"] = flag

    no_ref = (
        (df["fitness_flag"] == "")
        & ((df["current_campaign_tcpa"].isna()) | (df["current_campaign_tcpa"] == 0))
    )
    df.loc[no_ref, "fitness_flag"] = "no_tcpa_ref"
    return df


# ----------------------------------------------------------------------------
# Derived tier profile (Google-framework diagnostic)
# ----------------------------------------------------------------------------

def assign_derived_tier(df: pd.DataFrame) -> pd.Series:
    """Named-profile classification adapted from Google Home tier framework.

    Diagnostic only — used to assess whether existing ENABLED campaigns are
    economically coherent (member states share a profile) or heterogeneous.
    Thresholds use medians from the eligible (non-low_volume) pool.
    """
    result = pd.Series(index=df.index, dtype=object)
    elig_mask = df["fitness_flag"].isin(["well_fit", "underbid", "overbid"])
    elig = df[elig_mask].copy()
    if elig.empty:
        return result

    elig["profit_per_conv"] = elig["rpl_12w"] - elig["cpa_12w"]
    cpa_med = elig["cpa_12w"].median()
    rpl_med = elig["rpl_12w"].median()
    profit_med = elig["profit_per_conv"].median()

    def _classify(row):
        cpa_low = row["cpa_12w"] <= cpa_med
        rpl_low = row["rpl_12w"] <= rpl_med
        profit_pos = row["profit_per_conv"] > 0
        if (not cpa_low) and (not rpl_low):
            return "TIER TWO — Premium"
        if cpa_low and profit_pos:
            return "TIER ONE — Efficient"
        if abs(row["profit_per_conv"]) <= abs(profit_med):
            return "TIER THREE — Moderate"
        return "TIER FOUR — Break-Even"

    result.loc[elig.index] = elig.apply(_classify, axis=1)
    return result


# ----------------------------------------------------------------------------
# Campaign-level summary
# ----------------------------------------------------------------------------

def build_campaign_summary(
    state_df: pd.DataFrame, campaigns: pd.DataFrame,
) -> pd.DataFrame:
    elig = state_df[
        (state_df["current_campaign_id"] != "NONE_ACTIVE")
        & state_df["fitness_flag"].isin(["well_fit", "underbid", "overbid"])
    ].copy()
    if elig.empty:
        return pd.DataFrame(columns=CAMPAIGN_SUMMARY_COLUMNS)

    rows = []
    for cid, grp in elig.groupby("current_campaign_id"):
        camp = campaigns[campaigns["campaign_id"] == cid]
        if camp.empty:
            continue
        camp_name = camp["campaign_name"].iloc[0]
        camp_tcpa = camp["current_tcpa"].iloc[0]

        total_spend = grp["spend_12w"].sum()
        total_conv = grp["conversions_12w"].sum()
        total_rev = grp["revenue_12w"].sum()

        camp_cpa = total_spend / total_conv if total_conv > 0 else float("nan")
        camp_roas = total_rev / total_spend if total_spend > 0 else float("nan")

        valid_rpl = grp[grp["rpl_12w"].notna() & (grp["spend_12w"] > 0)]
        if not valid_rpl.empty:
            weighted_rpl = (
                (valid_rpl["rpl_12w"] * valid_rpl["spend_12w"]).sum()
                / valid_rpl["spend_12w"].sum()
            )
            rpl_min = valid_rpl["rpl_12w"].min()
            rpl_max = valid_rpl["rpl_12w"].max()
            rpl_spread = rpl_max / rpl_min if rpl_min > 0 else float("nan")
        else:
            weighted_rpl = float("nan")
            rpl_min = rpl_max = rpl_spread = float("nan")

        camp_ideal_tcpa = weighted_rpl / TARGET_ROAS if pd.notna(weighted_rpl) else float("nan")
        tcpa_gap = (
            camp_ideal_tcpa - camp_tcpa
            if pd.notna(camp_ideal_tcpa) and pd.notna(camp_tcpa)
            else float("nan")
        )

        tier_counts = grp["derived_tier_profile"].value_counts()
        tier_mix = ", ".join(f"{t}: {n}" for t, n in tier_counts.items())

        if pd.isna(rpl_spread):
            het_flag = ""
        elif rpl_spread >= 3:
            het_flag = "HETEROGENEOUS (rpl spread >=3x)"
        elif rpl_spread >= 2:
            het_flag = "MIXED (rpl spread 2-3x)"
        else:
            het_flag = "homogeneous"

        rows.append({
            "campaign_id": cid, "campaign_name": camp_name, "current_tcpa": camp_tcpa,
            "n_states_active": len(grp),
            "states_list": ", ".join(sorted(grp["state"].tolist())),
            "campaign_spend_12w": total_spend,
            "campaign_conversions_12w": total_conv,
            "campaign_revenue_12w": total_rev,
            "campaign_cpa_12w": camp_cpa,
            "campaign_roas_12w": camp_roas,
            "campaign_weighted_rpl": weighted_rpl,
            "campaign_ideal_tcpa": camp_ideal_tcpa,
            "tcpa_gap_campaign_level": tcpa_gap,
            "rpl_min": rpl_min, "rpl_max": rpl_max, "rpl_spread_ratio": rpl_spread,
            "tier_mix": tier_mix, "heterogeneity_flag": het_flag,
        })

    return pd.DataFrame(rows, columns=CAMPAIGN_SUMMARY_COLUMNS)


# ----------------------------------------------------------------------------
# Single-network sheet builder
# ----------------------------------------------------------------------------

def build_network_view(
    conn: sqlite3.Connection, windows: Windows,
    enabled_campaign_ids: list[str], primary_assignment: pd.DataFrame,
    networks: set[str],
) -> pd.DataFrame:
    cross_12w = pull_state_aggregates(
        conn, windows.window_12w_start, windows.end_date,
        enabled_campaign_ids, networks,
    )
    cross_30d = pull_state_aggregates(
        conn, windows.window_30d_start, windows.end_date,
        enabled_campaign_ids, networks,
    )
    if cross_12w.empty:
        return pd.DataFrame(columns=STATE_FITNESS_COLUMNS)
    df = build_state_metrics(cross_12w, cross_30d, primary_assignment)
    df = apply_fitness_flag(df)
    df["derived_tier_profile"] = assign_derived_tier(df)
    return df


def order_for_sheet(df: pd.DataFrame) -> pd.DataFrame:
    for col in STATE_FITNESS_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df.sort_values(
        ["current_campaign_id", "gap_state_vs_campaign_tcpa"],
        ascending=[True, False], na_position="last",
    )[STATE_FITNESS_COLUMNS]


# ----------------------------------------------------------------------------
# Workbook writing
# ----------------------------------------------------------------------------

def _apply_number_format(cell, col_name: str) -> None:
    if col_name in DOLLAR_COLS:
        cell.number_format = "$#,##0.00"
    elif col_name in RATIO_COLS:
        cell.number_format = "0.0000"
    elif col_name in INTEGER_COLS:
        cell.number_format = "#,##0"
    elif col_name == "conversions_12w":
        cell.number_format = "#,##0.00"


def _write_table(ws, df: pd.DataFrame, columns: list[str], header_row: int) -> None:
    for col_idx, name in enumerate(columns, start=1):
        c = ws.cell(row=header_row, column=col_idx, value=name)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center")
    for row_offset, rec in enumerate(df.to_dict("records"), start=1):
        for col_idx, name in enumerate(columns, start=1):
            v = rec.get(name)
            if not isinstance(v, str) and pd.isna(v):
                v = None
            c = ws.cell(row=header_row + row_offset, column=col_idx, value=v)
            c.font = BODY_FONT
            _apply_number_format(c, name)
    width_map = {
        "state": 22, "current_campaign_id": 14, "current_campaign_name": 50,
        "bid_strategy_mix": 14, "fitness_flag": 13, "derived_tier_profile": 22,
        "below_threshold_reason": 30, "states_list": 60, "tier_mix": 50,
        "heterogeneity_flag": 30, "campaign_name": 50,
    }
    for col_idx, name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width_map.get(name, 16)


def write_workbook(
    output_path: Path, windows: Windows,
    df_combined: pd.DataFrame, df_search: pd.DataFrame,
    df_audience: pd.DataFrame, df_campaign: pd.DataFrame,
    n_enabled: int,
) -> None:
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("State Fitness (Combined)")
    ws["A1"] = "Microsoft Geo Performance Analysis — State Fitness (Combined)"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (
        f"12-week window: {windows.window_12w_start} to {windows.end_date}  |  "
        f"30-day window: {windows.window_30d_start} to {windows.end_date}"
    )
    ws["A3"] = (
        f"ENABLED campaigns: {n_enabled}  |  "
        f"Click threshold: >={CLICK_THRESHOLD}  |  "
        f"Fitness tolerance: +/-{int(FITNESS_TOLERANCE*100)}% of campaign tCPA"
    )
    _write_table(ws, df_combined, STATE_FITNESS_COLUMNS, header_row=5)
    ws.freeze_panes = "B6"

    ws = wb.create_sheet("Campaign Summary")
    ws["A1"] = "Campaign Summary — Homogeneity Diagnostics"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (
        "RPL spread ratio = max(state_rpl) / min(state_rpl). "
        ">=3x flags HETEROGENEOUS campaigns (restructure candidates)."
    )
    _write_table(ws, df_campaign, CAMPAIGN_SUMMARY_COLUMNS, header_row=4)
    ws.freeze_panes = "B5"

    ws = wb.create_sheet("State Fitness (Search)")
    ws["A1"] = "State Fitness — Search Network Only"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Networks: {', '.join(sorted(SEARCH_NETWORKS))}"
    _write_table(ws, df_search, STATE_FITNESS_COLUMNS, header_row=4)
    ws.freeze_panes = "B5"

    ws = wb.create_sheet("State Fitness (Audience)")
    ws["A1"] = "State Fitness — Audience Network Only"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Networks: {', '.join(sorted(AUDIENCE_NETWORKS))}"
    _write_table(ws, df_audience, STATE_FITNESS_COLUMNS, header_row=4)
    ws.freeze_panes = "B5"

    ws = wb.create_sheet("Methodology")
    notes = [
        ("Microsoft Geo Performance Analysis — Methodology", True),
        ("", False),
        (f"Account: {ACCOUNT_ID}  |  End date: {windows.end_date}", False),
        (f"12w window: {windows.window_12w_start} to {windows.end_date}", False),
        (f"30d window: {windows.window_30d_start} to {windows.end_date}", False),
        ("", False),
        ("Scope", True),
        ("Analysis includes ENABLED Microsoft campaigns only. Paused campaigns are excluded "
         "from state-to-campaign assignment, even if they contributed spend during the window.", False),
        (f"Number of ENABLED campaigns: {n_enabled}", False),
        ("", False),
        ("State-to-campaign assignment", True),
        ("For each state, the 'primary campaign' is the ENABLED campaign with the highest "
         "spend share over the 12-week window. n_campaigns_active counts the distinct ENABLED "
         "campaigns that drove any spend for the state.", False),
        ("", False),
        ("Note on campaign names", True),
        ("Microsoft Home campaign names (e.g., 'HOME INSURANCE-VIRGINIA-...') are misleading - "
         "each ENABLED campaign typically targets multiple states. Do not infer state coverage "
         "from campaign name.", False),
        ("", False),
        ("Metric definitions", True),
        ("cpa = spend / conversions", False),
        ("roas = revenue / spend", False),
        ("rpl = revenue / conversions (revenue per pixel-detected conversion)", False),
        ("cpl_at_150_roas = rpl / 1.50 (per-state ideal tCPA at 150% ROAS target)", False),
        ("gap_state_vs_campaign_tcpa = cpl_at_150_roas - current_campaign_tcpa", False),
        ("  Positive gap: state economics can support higher bids than current campaign tCPA", False),
        ("  Negative gap: state RPL is below what its campaign assumes", False),
        ("", False),
        ("Fitness flag", True),
        ("well_fit   : state ideal tCPA within +/-20% of its campaign's tCPA", False),
        ("underbid   : state ideal tCPA is >20% above its campaign's tCPA", False),
        ("overbid    : state ideal tCPA is >20% below its campaign's tCPA", False),
        ("low_volume : state has <200 clicks in the 12-week window (metrics too noisy)", False),
        ("no_tcpa_ref: primary campaign has no real tCPA (data error to investigate)", False),
        ("", False),
        ("Derived tier profile (diagnostic only)", True),
        ("Named-profile classification adapted from the Google Home tier framework. Used to "
         "assess whether existing ENABLED campaigns are economically coherent. Profile "
         "assignment uses medians of CPA, RPL, and profit_per_conv on the eligible pool:", False),
        ("  TIER ONE - Efficient:   low CPA + positive profit_per_conv", False),
        ("  TIER TWO - Premium:     high CPA + high RPL", False),
        ("  TIER THREE - Moderate:  middling profit, mid-pack on both axes", False),
        ("  TIER FOUR - Break-Even: default bucket, profit near 0 or negative", False),
        ("", False),
        ("Campaign heterogeneity flag", True),
        ("RPL spread ratio = max(state_rpl) / min(state_rpl) within a campaign's member states. "
         ">=3x indicates the campaign contains states with very different economics - "
         "candidate for restructuring/splitting. 2-3x is mixed but tolerable. <2x is homogeneous.", False),
        ("", False),
        ("Critical caveat: tCPA is a point-in-time snapshot", True),
        ("tCPA shown reflects the current campaign setting as of analysis run-date - NOT "
         "necessarily the tCPA active throughout the analysis window. The campaigns table "
         "stores current values only; no historical bid-strategy change log exists. If tCPA "
         "was actively managed during the window, the displayed value may misrepresent "
         "historical bid behavior. This will be resolved when change-history ingestion is added.", False),
        ("", False),
        ("Network split", True),
        (f"Search networks: {', '.join(sorted(SEARCH_NETWORKS))}", False),
        (f"Audience networks: {', '.join(sorted(AUDIENCE_NETWORKS))}", False),
        ("", False),
        ("State exclusions", True),
        ("UNKNOWN: geolocation resolved to country but not state (VPN, carrier NAT). "
         "Always flagged low_volume.", False),
        (f"<{CLICK_THRESHOLD} clicks: metrics too noisy to assess fitness reliably.", False),
        ("NONE_ACTIVE: state had spend in window but no ENABLED campaign drove any of it "
         "(all driving campaigns were paused). Kept visible to flag orphan states.", False),
    ]
    for row_idx, (text, is_hdr) in enumerate(notes, start=1):
        c = ws.cell(row=row_idx, column=1, value=text)
        c.font = Font(bold=True, name="Arial", size=12) if is_hdr else BODY_FONT
    ws.column_dimensions["A"].width = 110

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


# ----------------------------------------------------------------------------
# Terminal reporting
# ----------------------------------------------------------------------------

def print_summary(
    windows: Windows, df_combined: pd.DataFrame,
    df_campaign: pd.DataFrame, n_enabled: int,
) -> None:
    print("=" * 72)
    print("Microsoft Geo Performance Analysis — Summary")
    print("=" * 72)
    print(f"End date:        {windows.end_date}")
    print(f"12-week window:  {windows.window_12w_start} -> {windows.end_date}")
    print(f"30-day window:   {windows.window_30d_start} -> {windows.end_date}")
    print(f"ENABLED campaigns analyzed: {n_enabled}")
    print(f"States in output:           {len(df_combined)}")

    if not df_combined.empty:
        print("\nFitness flag distribution (Combined view):")
        for flag, count in df_combined["fitness_flag"].value_counts().items():
            print(f"  {flag:<14} {count}")

        none_active = df_combined[df_combined["current_campaign_id"] == "NONE_ACTIVE"]
        if not none_active.empty:
            print(f"\nNONE_ACTIVE states ({len(none_active)}) - spend in window but no ENABLED campaign:")
            print(none_active[["state", "spend_12w", "clicks_12w"]].to_string(index=False))

    if not df_campaign.empty:
        print("\nCampaign homogeneity:")
        for flag, count in df_campaign["heterogeneity_flag"].value_counts().items():
            print(f"  {flag:<35} {count}")
        het = df_campaign[df_campaign["heterogeneity_flag"].str.startswith("HETEROGENEOUS", na=False)]
        if not het.empty:
            print("\nHETEROGENEOUS campaigns (RPL spread >=3x - restructure candidates):")
            cols = ["campaign_id", "campaign_name", "n_states_active", "rpl_min", "rpl_max", "rpl_spread_ratio"]
            print(het[cols].to_string(index=False))

    total = df_combined["spend_12w"].sum() if not df_combined.empty else 0
    print(f"\nTotal spend (12w, ENABLED campaigns): ${total:,.2f}")
    print("  -> Cross-check vs campaign_performance_daily for the same window (target: <=1% variance)")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Microsoft geo performance analysis")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--db-path", type=str, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--print-networks-only", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB file not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))

    if args.print_networks_only:
        print_distinct_networks(conn)
        return 0

    end_date = resolve_end_date(conn, args.end_date)
    windows = build_windows(end_date)
    print(f"Resolved end_date: {end_date}")
    print(f"12w window:       {windows.window_12w_start} -> {windows.end_date}")
    print(f"30d window:       {windows.window_30d_start} -> {windows.end_date}\n")

    campaigns = pull_enabled_campaigns(conn)
    if campaigns.empty:
        print("ERROR: No ENABLED campaigns for this account.", file=sys.stderr)
        return 1
    n_enabled = len(campaigns)
    n_with_tcpa = int(campaigns["current_tcpa"].notna().sum())
    print(f"ENABLED campaigns: {n_enabled} (with real tCPA: {n_with_tcpa})")
    if n_with_tcpa < n_enabled:
        missing = campaigns[campaigns["current_tcpa"].isna()]
        print(f"WARNING: {n_enabled - n_with_tcpa} ENABLED campaigns have no tCPA:")
        print(missing[["campaign_id", "campaign_name", "bidding_strategy_type"]].to_string(index=False))

    enabled_ids = campaigns["campaign_id"].tolist()

    sc_spend = pull_state_campaign_spend(
        conn, windows.window_12w_start, windows.end_date, enabled_ids,
    )
    primary = assign_primary_campaign(sc_spend, campaigns)

    cross_12w = pull_state_aggregates(
        conn, windows.window_12w_start, windows.end_date, enabled_ids,
    )
    cross_30d = pull_state_aggregates(
        conn, windows.window_30d_start, windows.end_date, enabled_ids,
    )
    if cross_12w.empty:
        print("ERROR: No 12w cross-network rows for ENABLED campaigns.", file=sys.stderr)
        return 1

    df_combined = build_state_metrics(cross_12w, cross_30d, primary)
    df_combined = apply_fitness_flag(df_combined)
    df_combined["derived_tier_profile"] = assign_derived_tier(df_combined)
    df_combined = order_for_sheet(df_combined)

    df_search = order_for_sheet(build_network_view(
        conn, windows, enabled_ids, primary, SEARCH_NETWORKS,
    ))
    df_audience = order_for_sheet(build_network_view(
        conn, windows, enabled_ids, primary, AUDIENCE_NETWORKS,
    ))

    df_campaign = build_campaign_summary(df_combined, campaigns)

    print_summary(windows, df_combined, df_campaign, n_enabled)

    if args.dry_run:
        print("\n--dry-run set, skipping workbook write.")
        return 0

    output_path = Path(args.output_dir) / f"microsoft_geo_ytd_analysis_{end_date}.xlsx"
    write_workbook(
        output_path, windows, df_combined, df_search, df_audience,
        df_campaign, n_enabled,
    )
    print(f"\nWrote: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
