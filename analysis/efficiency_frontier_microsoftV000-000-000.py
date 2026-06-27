"""
efficiency_frontier_microsoft.py
────────────────────────────────────────────────────────────────────
Weekly tCPA recommendation tool for Microsoft Home Insurance campaigns.

Microsoft equivalent of efficiency_frontier.py (Google tROAS). The
structure mirrors the Google script intentionally — same helpers, same
smoke-test discipline, same Frontier Summary layout — so the two tools
stay in sync and diffable.

Key differences from the Google tROAS tool:

  • tCPA direction is INVERTED vs tROAS.
      SCALE   → RAISE tCPA  (willing to pay more per conversion,
                              capture headroom)
      TIGHTEN → LOWER tCPA  (protect efficiency)

  • Offline attribution went live 2026-01-13. primary_conv_value is
    near-zero for any date before that. Windows crossing this boundary
    are flagged as MIXED REGIME and scenario guidance is downgraded.

  • Change history: reads a Microsoft Ads change-history CSV export
    (Reports → Change history, download as CSV). Wired up at
    CHANGE_HISTORY_CSV below or via --change-history. Enables:
       - Real recent-change guards (14d window for target/budget/strategy)
       - Change Log Attribution sheet (±7d confounder analysis)
       - Aggregate Sanity Check sheet (14d pre/post deltas)
    The snapshot-disagreement check remains as a backup in case the CSV
    is stale or a change happened via API/import that didn't hit the UI log.

  • Break-even CPA: computed dynamically per campaign from the 30d RPL
    window (SUM(primary_conv_value) / SUM(primary_conv_count)). Manual
    per-campaign overrides in BREAK_EVEN_CPA dict take precedence;
    --break-even CLI flag is a global override that wins over both.

  • Microsoft has a Hybrid campaign type (advertising_channel_sub_type
    = 'Search & content') where spend splits between search and
    audience. tCPA is set once and applies to both. search_rank_lost_is
    only describes headroom on the search side, so the Frontier Summary
    includes a 30d audience-spend share column to flag campaigns where
    search IS is only a partial headroom picture.

Usage:
    python efficiency_frontier_microsoft.py
    python analysis\efficiency_frontier_microsoft.py --end-date 2026-04-17
    python efficiency_frontier_microsoft.py --dry-run
    python efficiency_frontier_microsoft.py --break-even 40
    python efficiency_frontier_microsoft.py --min-conversions 20

Scope (v1):
    • Microsoft Home Insurance (account_id = 2, platform = microsoft)
    • All ENABLED campaigns (12 as of April 2026)
    • 2025-01-01 onward in DB; 30d rolling recommendation window
    • Rolling windows end at MAX(date) - 2 days to buffer conversion backfill
────────────────────────────────────────────────────────────────────
"""

import argparse
import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

# Microsoft Advertising change-history export — tCPA, budget, and bid-
# strategy changes for YTD. Format produced by Microsoft Ads UI:
#    Reports → Change history → Download as CSV
# Columns: Date, Changed by, Tool, Campaign, Ad group, Entity type,
#          Entity name, Attribute changed, How changed, Old value, New value
CHANGE_HISTORY_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Microsoft-bids-budget-changehistory-YTD.csv")

ACCOUNT_ID    = 2                 # Microsoft Home Insurance (INTEGER, not "F149SN63")
ACCOUNT_LABEL = "microsoft_home"

# Offline attribution pipeline went live. primary_conv_value is near-zero
# before this date.
ATTRIBUTION_GO_LIVE = date(2026, 1, 13)

# Minimum post-attribution days a window needs to generate a
# recommendation when it straddles ATTRIBUTION_GO_LIVE.
MIN_POST_ATTRIBUTION_DAYS = 14

# Change-history recency guard: any tCPA, budget, or bid-strategy change
# within this many days of the stable end-date forces HOLD.
RECENT_CHANGE_WINDOW_DAYS = 14

# Analysis cutoff — used for any full-year frame queries.
YEAR_START = date(2026, 1, 1)

# ── Break-even tCPA ceiling (USD) ──────────────────────────────────
# Break-even CPA is the revenue-per-lead — the ceiling beyond which the
# campaign is unprofitable. Default behavior: compute break-even
# dynamically per campaign from the 30d RPL window
# (SUM(primary_conv_value) / SUM(primary_conv_count)). This tracks the
# business reality you're already monitoring — no hardcoded LTV table
# needed.
#
# BREAK_EVEN_CPA manual overrides take precedence over the dynamic value
# (use when you have a better number than 30d RPL — e.g. LTV models,
# margin policy, or campaigns whose RPL is too noisy). --break-even on
# the CLI is a global override that wins over both.
BREAK_EVEN_CPA: dict[str, float] = {
    # "<campaign_id>": 35.00,
}
BREAK_EVEN_DEFAULT = 35.00   # fallback if both dynamic RPL and override are unavailable

# ── Scenario step sizes (as fraction of current tCPA) ──────────────
STEP_BASE       = 0.10
STEP_MODERATE   = 0.15
STEP_AGGRESSIVE = 0.20

# Hard ceiling on any single-run proposed change. If the step logic ever
# tries to exceed this (shouldn't with the steps above, but a safeguard),
# the proposal is capped and a warning is attached.
MAX_CHANGE_PCT = 0.20

# ── Trigger thresholds ─────────────────────────────────────────────
# Note direction: for tCPA, *lower* actual CPA is better efficiency,
# *higher* actual CPA is worse. SCALE triggers on actual_cpa ≤ target
# (with slack); TIGHTEN triggers on actual_cpa > target (with slack).
SCALE_CPA_RATIO              = 0.90   # actual ≤ target × 0.90 ⇒ SCALE candidate
AGGRESSIVE_SCALE_CPA_RATIO   = 0.85   # actual ≤ target × 0.85 ⇒ AGGRESSIVE SCALE candidate
TIGHTEN_CPA_RATIO            = 1.10   # actual > target × 1.10 on BOTH windows ⇒ TIGHTEN

RANK_LOST_SCALE              = 0.20   # search_rank_lost_is threshold for SCALE
RANK_LOST_AGGRESSIVE_SCALE   = 0.50   # threshold for AGGRESSIVE SCALE
BUDGET_LOST_THRESHOLD        = 0.20   # search_budget_lost_is ≥ this ⇒ BUDGET CONSTRAINED
IS_FLOOR                     = 0.10   # reporting floor — informational

# Snapshot-disagreement threshold: if campaigns.target_cpa_micros differs
# from the 30d-window median of target_cpa_micros_that_day by more than
# this, downgrade the recommendation to HOLD with a note.
SNAPSHOT_DISAGREEMENT_PCT = 0.10

# WoW conversion free-fall guard (mirrors Google). If current 7d conv
# count is more than 20% below the prior 7d, block TIGHTEN — lowering
# tCPA on collapsing volume deepens the spiral.
CONV_FREE_FALL_WOW = -0.20

# Buffer for offline conversion backfill
BACKFILL_BUFFER_DAYS = 2

# ── Formatting constants ───────────────────────────────────────────
HEADER_FILL  = "7030A0"   # purple
HEADER_FONT  = "FFFFFF"   # white
GREEN_FILL   = "E2EFDA"   # SCALE
DEEP_GREEN   = "C6E0B4"   # AGGRESSIVE SCALE
YELLOW_FILL  = "FFEB9C"   # HOLD
RED_FILL     = "FFC7CE"   # TIGHTEN
ORANGE_FILL  = "FCD5B4"   # BUDGET CONSTRAINED
GREY_FILL    = "D9D9D9"   # INSUFFICIENT DATA
BLUE_FILL    = "D9E1F2"   # MIXED REGIME


# ═══════════════════════════════════════════════════════════════════
# INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════

def get_db_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account", default=ACCOUNT_LABEL,
                   help=f"Account label for output file naming (default: {ACCOUNT_LABEL})")
    p.add_argument("--end-date", default=None,
                   help="Analysis end date YYYY-MM-DD (default: MAX(date) - 2 from DB)")
    p.add_argument("--break-even", type=float, default=None,
                   help="Global override for break-even tCPA ceiling (USD). Wins over "
                        "both the dynamic-from-RPL value and BREAK_EVEN_CPA manual dict.")
    p.add_argument("--min-conversions", type=int, default=15,
                   help="Minimum primary conversions in 30d window to generate "
                        "a recommendation (default: 15)")
    p.add_argument("--change-history", default=None,
                   help=f"Path to Microsoft Ads change-history CSV (overrides "
                        f"CHANGE_HISTORY_CSV default). Set to empty string '' to skip.")
    p.add_argument("--db-path", default=str(DB_PATH),
                   help="Path to paid_search.db")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR),
                   help="Directory for Excel output")
    p.add_argument("--dry-run", action="store_true",
                   help="Run smoke tests + diagnostic print, do not write xlsx")
    return p.parse_args()


def banner(msg: str) -> None:
    print("\n" + "═" * 70)
    print(msg)
    print("═" * 70)


# ═══════════════════════════════════════════════════════════════════
# DATE WINDOW RESOLUTION
# ═══════════════════════════════════════════════════════════════════

def resolve_end_date(conn: sqlite3.Connection, override: str | None) -> tuple[date, date]:
    """
    Returns (raw_max_date, stable_end_date).
    stable_end_date = raw_max - BACKFILL_BUFFER_DAYS.
    If --end-date is provided, uses it directly as stable_end_date and
    raw_max is read anyway for the diagnostic banner.
    """
    row = conn.execute(
        "SELECT MAX(date) AS max_date FROM campaign_performance_daily WHERE account_id = ?",
        (ACCOUNT_ID,),
    ).fetchone()
    if not row or not row["max_date"]:
        raise SystemExit(
            f"No campaign_performance_daily rows for account_id={ACCOUNT_ID}."
        )
    raw_max = date.fromisoformat(row["max_date"])

    if override:
        stable = date.fromisoformat(override)
    else:
        stable = raw_max - timedelta(days=BACKFILL_BUFFER_DAYS)

    lag = (date.today() - raw_max).days
    if lag > 2:
        print(f"⚠  WARNING: DB is {lag} days behind today (MAX(date) = {raw_max}). "
              f"Consider running the sync before trusting output.")
    return raw_max, stable


def build_windows(end_date: date) -> dict[int, tuple[date, date]]:
    """7d, 14d, and 30d windows, inclusive on both ends, ending at end_date."""
    return {
        7:  (end_date - timedelta(days=6),  end_date),
        14: (end_date - timedelta(days=13), end_date),
        30: (end_date - timedelta(days=29), end_date),
    }


def crosses_attribution_boundary(
    window_start: date, window_end: date
) -> tuple[bool, int]:
    """
    Returns (crosses, post_attribution_days).
    crosses = True if window straddles ATTRIBUTION_GO_LIVE (start < go-live ≤ end).
    post_attribution_days = number of days in window on or after ATTRIBUTION_GO_LIVE.
    """
    if window_end < ATTRIBUTION_GO_LIVE:
        return False, 0
    if window_start >= ATTRIBUTION_GO_LIVE:
        return False, (window_end - window_start).days + 1
    crosses = True
    post_days = (window_end - ATTRIBUTION_GO_LIVE).days + 1
    return crosses, post_days


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES
# ═══════════════════════════════════════════════════════════════════

def query_campaigns_metadata(conn: sqlite3.Connection) -> pd.DataFrame:
    """Returns all Microsoft Home campaigns (any status)."""
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
    start: date,
    end: date,
) -> pd.DataFrame:
    """
    Daily search-network rollup per campaign × date.
    Joins to keyword_performance_daily for primary_conv_count/value
    (offline-routed metrics). device='ALL' only.
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
    params = campaign_ids + [start.isoformat(), end.isoformat(), ACCOUNT_ID]
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
    return df


def query_audience_network_daily(
    conn: sqlite3.Connection,
    campaign_ids: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """
    Campaign × date audience-network rollup. Returns spend, clicks, imps,
    and native conversions/value from campaign_performance_network_daily.
    Used for the audience-share flag on Frontier Summary and for the
    Network Split sheet.

    Revenue here comes from the same offline partner-upload pipeline that
    feeds keyword_performance_daily on the search side — both networks
    are apples-to-apples on the economic signal.
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
    params = campaign_ids + [start.isoformat(), end.isoformat(), ACCOUNT_ID]
    return pd.read_sql(sql, conn, params=params)


def query_ad_group_status_counts(
    conn: sqlite3.Connection, campaign_ids: list[str]
) -> pd.DataFrame:
    if not campaign_ids:
        return pd.DataFrame(columns=["campaign_id", "status", "n"])
    placeholders = ",".join("?" * len(campaign_ids))
    sql = f"""
        SELECT campaign_id, status, COUNT(*) AS n
        FROM ad_groups
        WHERE account_id = ?
          AND campaign_id IN ({placeholders})
        GROUP BY campaign_id, status
    """
    return pd.read_sql(sql, conn, params=[ACCOUNT_ID] + campaign_ids)


# ═══════════════════════════════════════════════════════════════════
# WINDOW AGGREGATION
# ═══════════════════════════════════════════════════════════════════

def aggregate_window(
    df_daily: pd.DataFrame,
    start: date,
    end: date,
    audience_daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Aggregate campaign × date daily DF to per-campaign window totals.

    By default (audience_daily=None) produces search-only spend aggregates —
    used by the Network Split sheet's search row and for legacy debugging.

    When audience_daily is provided, the function adds audience spend to
    cost_usd before computing actual_cpa and actual_roas, so those numbers
    reflect **all-network** spend divided by **all-network** conversion
    counts / revenue (which is how keyword_performance_daily is already
    populated in this DB). This is what the recommendation engine needs:
    Microsoft's tCPA applies across all networks from a single target, so
    the actual-vs-target comparison must be on all-network totals.

    audience_daily columns expected (from query_audience_network_daily):
        campaign_id, date, audience_cost_usd
    Other audience columns ignored.
    """
    mask = (pd.to_datetime(df_daily["date"]).dt.date >= start) & \
           (pd.to_datetime(df_daily["date"]).dt.date <= end)
    w = df_daily.loc[mask].copy()
    if w.empty:
        return pd.DataFrame(columns=[
            "campaign_id", "impressions", "clicks", "cost_usd",
            "search_cost_usd", "audience_cost_usd",
            "primary_conv_count", "primary_conv_value",
            "actual_cpa", "actual_roas",
            "mean_rank_lost_is", "mean_budget_lost_is",
            "median_target_cpa_that_day_usd",
        ])

    agg = w.groupby("campaign_id", as_index=False).agg(
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        search_cost_usd=("cost_usd", "sum"),
        primary_conv_count=("primary_conv_count", "sum"),
        primary_conv_value=("primary_conv_value", "sum"),
        mean_rank_lost_is=("search_rank_lost_is", "mean"),
        mean_budget_lost_is=("search_budget_lost_is", "mean"),
        median_target_cpa_that_day_usd=("target_cpa_that_day_usd", "median"),
    )

    # Audience spend — window-filtered, per campaign
    if audience_daily is not None and not audience_daily.empty:
        amask = (pd.to_datetime(audience_daily["date"]).dt.date >= start) & \
                (pd.to_datetime(audience_daily["date"]).dt.date <= end)
        aw = audience_daily.loc[amask]
        if not aw.empty:
            audience_agg = (
                aw.groupby("campaign_id", as_index=False)
                  .agg(audience_cost_usd=("audience_cost_usd", "sum"))
            )
            agg = agg.merge(audience_agg, on="campaign_id", how="left")
        else:
            agg["audience_cost_usd"] = 0.0
    else:
        agg["audience_cost_usd"] = 0.0
    agg["audience_cost_usd"] = agg["audience_cost_usd"].fillna(0.0)

    # cost_usd = all-network total when audience is supplied; search-only otherwise
    agg["cost_usd"] = agg["search_cost_usd"] + agg["audience_cost_usd"]

    agg["actual_cpa"] = agg.apply(
        lambda r: r["cost_usd"] / r["primary_conv_count"]
                  if r["primary_conv_count"] > 0 else None,
        axis=1,
    )
    agg["actual_roas"] = agg.apply(
        lambda r: r["primary_conv_value"] / r["cost_usd"] if r["cost_usd"] > 0 else None,
        axis=1,
    )
    return agg


# ═══════════════════════════════════════════════════════════════════
# CHANGE-HISTORY CSV LOADER
# ═══════════════════════════════════════════════════════════════════

# Microsoft Ads change-history attribute names that we care about.
# The CSV has many more attribute types (status flips, ad-copy edits,
# geo adjustments, etc.) but only these four affect tCPA recommendations.
TRACKED_ATTRIBUTES = {
    "TargetCPA":            "target",       # tCPA value change
    "IncrementalBudgetAmt": "budget",       # daily budget change
    "BiddingSchemeId":      "strategy",     # bid strategy type switch
    "BidStrategyName":      "strategy",     # portfolio strategy name switch
}


def _parse_ms_value(raw: str) -> float | None:
    """
    Microsoft exports numeric old/new values as raw numbers, e.g. '25',
    '87.5', '200'. Empty strings are legitimate (TargetCPA initial-set
    rows have blank Old value). Returns float or None.
    """
    if raw is None:
        return None
    s = str(raw).strip().lstrip("$").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_change_history(csv_path: Path) -> pd.DataFrame:
    """
    Parses the Microsoft Ads change-history CSV.

    Microsoft format (verified 2026-04):
      Date: M/D/YYYY (no time component)
      Columns: Date, Changed by, Tool, Campaign, Ad group, Entity type,
               Entity name, Attribute changed, How changed,
               Old value, New value

    Returns DataFrame (one row per tracked change) with columns:
      timestamp         (pd.Timestamp at midnight — CSV has no time)
      campaign_name
      ad_group_name     (blank for campaign-level, which is all of them)
      attribute         (raw Microsoft name — e.g. 'TargetCPA')
      change_kind       (normalized: 'target' | 'budget' | 'strategy')
      old_value_num     (float where parseable, else None)
      new_value_num     (float where parseable, else None)
      old_value_raw     (string, for strategy changes like 'MaxConversions')
      new_value_raw     (string)
      scope             ('AG' if Ad group populated else 'CAMPAIGN')

    Filters out attributes not in TRACKED_ATTRIBUTES (status flips,
    ad-copy edits, etc.). Filters out rows before YEAR_START. Rows where
    Old and New numeric values both parse and are equal (a no-op edit)
    are dropped — we only keep actual changes.
    """
    if not isinstance(csv_path, Path):
        csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Change-history CSV not found: {csv_path}")

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows: list[dict] = []
        for r in reader:
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

            # No-op filter: both numeric and equal — skip.
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


def derive_current_tcpa_from_csv(
    change_df: pd.DataFrame,
    enabled_campaign_names: set[str],
) -> pd.DataFrame:
    """
    For each enabled campaign, the latest 2026 CAMPAIGN-level TargetCPA
    change is the CSV-derived current tCPA. Returns a frame indexed by
    campaign_name with: current_target, current_target_ts, previous_target.

    Campaigns with no TargetCPA row in the CSV get all-None — downstream
    code falls back to campaigns.target_cpa_micros from the DB.
    """
    out: dict[str, dict] = {
        name: {"current_target": None, "current_target_ts": None,
               "previous_target": None}
        for name in enabled_campaign_names
    }
    if change_df.empty:
        return pd.DataFrame.from_dict(out, orient="index").rename_axis("campaign_name")

    tcpa_rows = change_df[
        (change_df["change_kind"] == "target")
        & (change_df["scope"] == "CAMPAIGN")
        & (change_df["campaign_name"].isin(enabled_campaign_names))
    ].copy().sort_values("timestamp")

    for name, sub in tcpa_rows.groupby("campaign_name"):
        last = sub.iloc[-1]
        prev = sub.iloc[-2] if len(sub) >= 2 else None
        out[name] = {
            "current_target":    last["new_value_num"],
            "current_target_ts": last["timestamp"],
            "previous_target":   (prev["new_value_num"] if prev is not None
                                  else last["old_value_num"]),
        }

    return pd.DataFrame.from_dict(out, orient="index").rename_axis("campaign_name")


def build_change_attribution(
    change_df: pd.DataFrame,
    enabled_campaign_names: set[str],
) -> pd.DataFrame:
    """
    For every tCPA change on an enabled campaign, count how many other
    tracked changes (tCPA, budget, or strategy) happened on the same
    campaign within ±7 days. clean = 0 confounders.

    Returns: timestamp, campaign_name, scope, old_target, new_target,
             direction (tighten | loosen | flat | n/a),
             confounders_within_7d, clean
    """
    if change_df.empty:
        return pd.DataFrame(columns=[
            "timestamp", "campaign_name", "scope",
            "old_target", "new_target", "direction",
            "confounders_within_7d", "clean",
        ])

    in_scope = change_df[change_df["campaign_name"].isin(enabled_campaign_names)]
    tcpa = in_scope[in_scope["change_kind"] == "target"].copy()
    all_tracked = in_scope.copy()

    rows = []
    for _, tr in tcpa.iterrows():
        ts = tr["timestamp"]
        name = tr["campaign_name"]
        window_start = ts - timedelta(days=7)
        window_end   = ts + timedelta(days=7)

        same_camp = all_tracked[all_tracked["campaign_name"] == name]
        # Other changes (exclude the event itself, match on timestamp + attribute)
        others = same_camp[
            (same_camp["timestamp"] >= window_start)
            & (same_camp["timestamp"] <= window_end)
            & ~(
                (same_camp["timestamp"] == ts)
                & (same_camp["attribute"] == tr["attribute"])
            )
        ]
        confounders = len(others)

        # Direction — for tCPA, RAISING target is "loosen" (more willingness
        # to pay), LOWERING is "tighten" (protect efficiency). Same semantics
        # as the Google ROAS script so the Aggregate Sanity Check reads the
        # same way across tools.
        old, new = tr["old_value_num"], tr["new_value_num"]
        old_missing = old is None or (isinstance(old, float) and pd.isna(old))
        new_missing = new is None or (isinstance(new, float) and pd.isna(new))
        if old_missing and new_missing:
            direction = "n/a"
        elif old_missing:
            direction = "initial_set"
        elif new_missing:
            direction = "cleared"
        elif new > old:
            direction = "loosen"
        elif new < old:
            direction = "tighten"
        else:
            direction = "flat"

        rows.append({
            "timestamp":             ts,
            "campaign_name":         name,
            "scope":                 tr["scope"],
            "old_target":            old,
            "new_target":            new,
            "direction":             direction,
            "confounders_within_7d": confounders,
            "clean":                 confounders == 0,
        })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def build_aggregate_sanity_check(
    attribution_df: pd.DataFrame,
    df_all_daily: pd.DataFrame,
    campaign_name_to_id: dict[str, str],
) -> pd.DataFrame:
    """
    Across clean tCPA changes, did loosens (target raised) lift spend
    and conversions in the following 14 days, and did tightens (target
    lowered) reduce them? Returns one row per clean event + aggregate
    rows by direction.

    14d pre vs 14d post spend and primary_conv comparison. Matches the
    Google script's interpretation so the two tools are diffable.
    """
    if attribution_df.empty or df_all_daily.empty:
        return pd.DataFrame()

    clean = attribution_df[
        (attribution_df["clean"])
        & (attribution_df["scope"] == "CAMPAIGN")
        & (attribution_df["direction"].isin(["loosen", "tighten"]))
    ].copy()

    if clean.empty:
        return pd.DataFrame()

    rows = []
    for _, ev in clean.iterrows():
        name = ev["campaign_name"]
        cid  = campaign_name_to_id.get(name)
        if not cid:
            continue
        ts = ev["timestamp"].date()
        pre_start  = ts - timedelta(days=14)
        pre_end    = ts - timedelta(days=1)
        post_start = ts + timedelta(days=1)
        post_end   = ts + timedelta(days=14)

        dates = pd.to_datetime(df_all_daily["date"]).dt.date
        sub_pre = df_all_daily[
            (df_all_daily["campaign_id"] == cid)
            & (dates >= pre_start) & (dates <= pre_end)
        ]
        sub_post = df_all_daily[
            (df_all_daily["campaign_id"] == cid)
            & (dates >= post_start) & (dates <= post_end)
        ]
        if sub_pre.empty or sub_post.empty:
            continue

        pre_spend  = float(sub_pre["cost_usd"].sum())
        post_spend = float(sub_post["cost_usd"].sum())
        pre_conv   = float(sub_pre["primary_conv_count"].sum())
        post_conv  = float(sub_post["primary_conv_count"].sum())

        rows.append({
            "timestamp":       ev["timestamp"],
            "campaign_name":   name,
            "direction":       ev["direction"],
            "old_target":      ev["old_target"],
            "new_target":      ev["new_target"],
            "pre_spend":       round(pre_spend, 2),
            "post_spend":      round(post_spend, 2),
            "spend_delta_pct": round((post_spend - pre_spend) / pre_spend, 4) if pre_spend > 0 else None,
            "pre_conv":        round(pre_conv, 1),
            "post_conv":       round(post_conv, 1),
            "conv_delta_pct":  round((post_conv - pre_conv) / pre_conv, 4) if pre_conv > 0 else None,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    agg_rows = []
    for direction in ["loosen", "tighten"]:
        sub = df[df["direction"] == direction]
        if sub.empty:
            continue
        agg_rows.append({
            "timestamp":       None,
            "campaign_name":   f"[AGGREGATE — {direction.upper()} ({len(sub)} events)]",
            "direction":       direction,
            "old_target":      None,
            "new_target":      None,
            "pre_spend":       None,
            "post_spend":      None,
            "spend_delta_pct": round(sub["spend_delta_pct"].dropna().mean(), 4) if not sub["spend_delta_pct"].dropna().empty else None,
            "pre_conv":        None,
            "post_conv":       None,
            "conv_delta_pct":  round(sub["conv_delta_pct"].dropna().mean(), 4) if not sub["conv_delta_pct"].dropna().empty else None,
        })

    return pd.concat([df, pd.DataFrame(agg_rows)], ignore_index=True)


# ═══════════════════════════════════════════════════════════════════
# NETWORK SPLIT DIAGNOSTIC (search vs audience per campaign)
# ═══════════════════════════════════════════════════════════════════

def build_network_split(
    camp_meta: pd.DataFrame,
    enabled_campaign_ids: list[str],
    df_daily_30d: pd.DataFrame,
    audience_rollup: pd.DataFrame,
    start_30d: date,
    end_30d: date,
) -> pd.DataFrame:
    """
    Per-campaign split of 30d performance, three rows per campaign:
        network='total'    — search + audience combined (spend, conv, rev all summed)
        network='search'   — from campaign_performance_daily + keyword_performance_daily
                             join (offline Home Insurance Lead - Quote Wizard goal)
        network='audience' — from campaign_performance_network_daily
                             (native conversions + revenue from the same offline
                             upload pipeline as search)

    Both networks draw revenue from the same offline partner-upload pipeline,
    so summing them in the total row produces a consistent blended view —
    no attribution asymmetry to caveat.

    Columns: spend_usd, conv_count, conv_value, cpl, rpl, roas, conv_source.
    The conv_source column records which table the row's metrics came from,
    for debugging.
    """
    search_agg = aggregate_window(df_daily_30d, start_30d, end_30d)
    search_map = {r["campaign_id"]: r for _, r in search_agg.iterrows()}

    audience_map_full: dict[str, dict] = {}
    for _, r in audience_rollup.iterrows():
        audience_map_full[r["campaign_id"]] = {
            "spend":      float(r["audience_cost_usd"]            or 0.0),
            "conv_count": float(r["audience_native_conv"]         or 0.0),
            "conv_value": float(r["audience_native_conv_value"]   or 0.0),
        }

    name_by_id = dict(zip(camp_meta["campaign_id"], camp_meta["campaign_name"]))

    def _metrics(spend: float, conv: float, rev: float) -> tuple[float | None, float | None, float | None]:
        cpl  = (spend / conv) if conv > 0 else None
        rpl  = (rev   / conv) if conv > 0 else None
        roas = (rev   / spend) if spend > 0 else None
        return cpl, rpl, roas

    rows = []
    for cid in enabled_campaign_ids:
        name = name_by_id.get(cid, cid)

        # Search side (keyword_performance_daily join)
        s = search_map.get(cid)
        s_spend = float(s["cost_usd"])            if s is not None else 0.0
        s_conv  = float(s["primary_conv_count"])  if s is not None else 0.0
        s_rev   = float(s["primary_conv_value"])  if s is not None else 0.0
        s_cpl, s_rpl, s_roas = _metrics(s_spend, s_conv, s_rev)

        # Audience side (campaign_performance_network_daily)
        a = audience_map_full.get(cid, {"spend": 0.0, "conv_count": 0.0, "conv_value": 0.0})
        a_spend = a["spend"]
        a_conv  = a["conv_count"]
        a_rev   = a["conv_value"]
        a_cpl, a_rpl, a_roas = _metrics(a_spend, a_conv, a_rev)

        # Total: straight sum — both sides attribute from the same offline pipeline
        t_spend = s_spend + a_spend
        t_conv  = s_conv  + a_conv
        t_rev   = s_rev   + a_rev
        t_cpl, t_rpl, t_roas = _metrics(t_spend, t_conv, t_rev)

        for network, spend, conv, rev, cpl, rpl, roas, source in [
            ("total",    t_spend, t_conv, t_rev, t_cpl, t_rpl, t_roas, "search+audience"),
            ("search",   s_spend, s_conv, s_rev, s_cpl, s_rpl, s_roas, "keyword_performance_daily"),
            ("audience", a_spend, a_conv, a_rev, a_cpl, a_rpl, a_roas, "campaign_performance_network_daily"),
        ]:
            rows.append({
                "campaign_name":  name,
                "campaign_id":    cid,
                "network":        network,
                "spend_usd":      round(spend, 2),
                "conv_count":     round(conv, 1),
                "conv_value":     round(rev, 2),
                "cpl":            round(cpl, 2)  if cpl  is not None else None,
                "rpl":            round(rpl, 2)  if rpl  is not None else None,
                "roas":           round(roas, 4) if roas is not None else None,
                "conv_source":    source,
            })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# DYNAMIC BREAK-EVEN CPA (FROM RPL)
# ═══════════════════════════════════════════════════════════════════

def compute_break_even_cpa(
    df_daily: pd.DataFrame,
    start: date,
    end: date,
) -> dict[str, float]:
    """
    Compute break-even tCPA per campaign from the window's RPL:
        break_even = SUM(primary_conv_value) / SUM(primary_conv_count)
    Revenue per lead — the maximum we can pay per conversion before
    operating at a loss. Campaigns with zero conversions in the window
    are omitted (caller falls back to manual override or BREAK_EVEN_DEFAULT).

    Takes the already-loaded df_daily rather than re-querying the DB —
    keeps the data source consistent and avoids an extra round trip.
    """
    if df_daily.empty:
        return {}

    mask = (pd.to_datetime(df_daily["date"]).dt.date >= start) & \
           (pd.to_datetime(df_daily["date"]).dt.date <= end)
    w = df_daily.loc[mask]
    if w.empty:
        return {}

    agg = w.groupby("campaign_id", as_index=False).agg(
        conv=("primary_conv_count", "sum"),
        rev=("primary_conv_value", "sum"),
    )
    result: dict[str, float] = {}
    for _, r in agg.iterrows():
        conv = float(r["conv"])
        rev  = float(r["rev"])
        if conv > 0:
            result[r["campaign_id"]] = round(rev / conv, 2)
    return result


# ═══════════════════════════════════════════════════════════════════
# SMOKE TESTS (halt on failure)
# ═══════════════════════════════════════════════════════════════════

def run_smoke_tests(
    conn: sqlite3.Connection,
    camp_meta: pd.DataFrame,
    enabled_campaign_ids: list[str],
    df_daily_30d: pd.DataFrame,
    start_30d: date,
    end_date: date,
) -> None:
    banner("SMOKE TESTS")

    # 1. Account resolves
    row = conn.execute(
        "SELECT id, account_name, platform FROM accounts WHERE id = ?",
        (ACCOUNT_ID,),
    ).fetchone()
    assert row is not None, f"account_id = {ACCOUNT_ID} not found in accounts table"
    assert row["platform"] == "microsoft", \
        f"Expected platform=microsoft for account_id={ACCOUNT_ID}, got {row['platform']}"
    print(f"  ✓ Account resolves: {row['account_name']} ({row['platform']})")

    # 2. Active campaigns with bidding_strategy_type populated
    enabled = camp_meta[camp_meta["status"] == "ENABLED"]
    assert not enabled.empty, "No ENABLED campaigns for Microsoft Home"
    missing_bs = enabled[enabled["bidding_strategy_type"].isna()]
    assert missing_bs.empty, (
        f"ENABLED campaigns missing bidding_strategy_type:\n"
        f"{missing_bs[['campaign_id', 'campaign_name']]}"
    )
    print(f"  ✓ {len(enabled)} ENABLED campaigns, all with bidding_strategy_type populated")

    # 3. tCPA populated (informational — not every bid strategy uses tCPA)
    with_tcpa = enabled[enabled["target_cpa_micros"].notna()]
    no_tcpa   = enabled[enabled["target_cpa_micros"].isna()]
    print(f"  ℹ  {len(with_tcpa)}/{len(enabled)} ENABLED campaigns have target_cpa_micros")
    if not no_tcpa.empty:
        strategies = no_tcpa["bidding_strategy_type"].value_counts().to_dict()
        print(f"     Without tCPA, by strategy: {strategies}")
        print(f"     These will be excluded from recommendations (INSUFFICIENT DATA).")

    # 4. IS coverage check (≥ 80% non-null search_rank_lost_is in 30d)
    if df_daily_30d.empty:
        raise AssertionError(f"No daily rows for 30d window {start_30d}..{end_date}")
    coverage = df_daily_30d.groupby("campaign_id").agg(
        days_total=("date", "nunique"),
        days_with_rank_is=("search_rank_lost_is", lambda s: s.notna().sum()),
    )
    coverage["pct"] = coverage["days_with_rank_is"] / coverage["days_total"]
    low_cov = coverage[coverage["pct"] < 0.80]
    if not low_cov.empty:
        print("  ⚠  search_rank_lost_is coverage < 80% for some campaigns "
              "(informational; headroom logic degrades gracefully on NaN):")
        print(low_cov.to_string())
    else:
        print(f"  ✓ search_rank_lost_is ≥ 80% coverage across all ENABLED campaigns in 30d")

    # 5. Spend non-zero
    spend_total = df_daily_30d["cost_usd"].sum()
    assert spend_total > 0, "30d spend total is zero — check data pipeline"
    print(f"  ✓ 30d spend total is non-zero: ${spend_total:,.0f}")

    # 6. Attribution boundary check
    crosses, post_days = crosses_attribution_boundary(start_30d, end_date)
    print(f"  ℹ  30d window: {start_30d} → {end_date}")
    if crosses:
        status = (f"Will generate MIXED REGIME flags."
                  if post_days >= MIN_POST_ATTRIBUTION_DAYS
                  else "INSUFFICIENT post-attribution data — recommendations will be withheld.")
        print(f"     ⚠  Window crosses attribution go-live ({ATTRIBUTION_GO_LIVE}). "
              f"Post-attribution days in window: {post_days}. {status}")
    elif end_date < ATTRIBUTION_GO_LIVE:
        print(f"     ⚠  Window is entirely PRE-attribution. primary_conv_value "
              f"will be near-zero; recommendations will be withheld.")
    else:
        print(f"     ✓ Window is entirely POST-attribution. Clean regime.")

    # 7. Snapshot-agreement check (warn, don't halt)
    print("  ℹ  Snapshot agreement (campaigns.target_cpa_micros vs 30d median snapshot):")
    disagreements = []
    for _, cm in enabled.iterrows():
        cid = cm["campaign_id"]
        current_t = (cm["target_cpa_micros"] / 1e6) if pd.notna(cm["target_cpa_micros"]) else None
        camp_rows = df_daily_30d[df_daily_30d["campaign_id"] == cid]
        snap_vals = camp_rows["target_cpa_that_day_usd"].dropna().tolist()
        if current_t is None or not snap_vals:
            continue
        snap_median = median(snap_vals)
        if snap_median == 0:
            continue
        delta_pct = abs(current_t - snap_median) / snap_median
        if delta_pct > SNAPSHOT_DISAGREEMENT_PCT:
            disagreements.append(
                f"{cm['campaign_name'][:50]:50s}  current=${current_t:>6.2f}  "
                f"30d median=${snap_median:>6.2f}  Δ {delta_pct:+.0%}"
            )
    if disagreements:
        print(f"     ⚠  {len(disagreements)} campaigns with >{SNAPSHOT_DISAGREEMENT_PCT:.0%} "
              f"disagreement (recent-change guard will downgrade to HOLD):")
        for d in disagreements:
            print(f"        {d}")
    else:
        print(f"     ✓ No campaigns with snapshot disagreement > {SNAPSHOT_DISAGREEMENT_PCT:.0%}")

    print("\n  All critical smoke tests passed (warnings/info above do not halt).\n")


# ═══════════════════════════════════════════════════════════════════
# RECOMMENDATION ENGINE
# ═══════════════════════════════════════════════════════════════════

def classify_recommendation(
    actual_cpa_14: float | None,
    actual_cpa_30: float | None,
    conv_30d: float,
    current_tcpa: float | None,
    rank_lost_is: float | None,
    budget_lost_is: float | None,
    wow_conv_trend: float | None,
    mixed_regime: bool,
    post_attribution_days: int,
    snapshot_disagreement: bool,
    recent_target_change: bool,
    recent_budget_change: bool,
    recent_strategy_change: bool,
    min_conversions: int,
) -> tuple[str, str]:
    """
    Returns (category, rationale).

    Category vocabulary:
      SCALE               → raise tCPA
      AGGRESSIVE SCALE    → raise tCPA (more)
      TIGHTEN             → lower tCPA
      HOLD                → no change
      BUDGET CONSTRAINED  → budget is the binding constraint
      INSUFFICIENT DATA   → cannot recommend
      MIXED REGIME        → window straddles attribution boundary; flagged

    Precedence:
      INSUFFICIENT DATA
      BUDGET CONSTRAINED
      HOLD (recent target/budget/strategy change from CSV — 14d guard)
      HOLD (snapshot disagreement — backup for CSV gaps)
      HOLD (conv free-fall blocking TIGHTEN)
      AGGRESSIVE SCALE / SCALE
      TIGHTEN
      HOLD (fallthrough)

    When mixed_regime is True and post-days ≥ MIN_POST_ATTRIBUTION_DAYS,
    the recommendation is LABELED 'MIXED REGIME' (overriding the action
    category). The underlying reasoning appears in the rationale.
    """
    if current_tcpa is None:
        return ("INSUFFICIENT DATA",
                "Campaign has no target_cpa_micros in campaigns table "
                "(likely non-tCPA bid strategy)")

    if conv_30d < min_conversions:
        return ("INSUFFICIENT DATA",
                f"Only {conv_30d:.0f} primary conversions in 30d window (<{min_conversions})")

    if mixed_regime and post_attribution_days < MIN_POST_ATTRIBUTION_DAYS:
        return ("INSUFFICIENT DATA",
                f"Window crosses attribution go-live ({ATTRIBUTION_GO_LIVE}); "
                f"only {post_attribution_days} days post-attribution "
                f"(<{MIN_POST_ATTRIBUTION_DAYS})")

    if budget_lost_is is not None and budget_lost_is > BUDGET_LOST_THRESHOLD:
        return ("BUDGET CONSTRAINED",
                f"search_budget_lost_is = {budget_lost_is:.0%} (>{BUDGET_LOST_THRESHOLD:.0%}) — "
                f"raise budget before changing tCPA")

    # Recent-change guards from change-history CSV (primary signal)
    if recent_target_change:
        return ("HOLD",
                f"tCPA changed within last {RECENT_CHANGE_WINDOW_DAYS} days — letting it settle")
    if recent_budget_change:
        return ("HOLD",
                f"Budget changed within last {RECENT_CHANGE_WINDOW_DAYS} days — "
                f"performance is recalibrating")
    if recent_strategy_change:
        return ("HOLD",
                f"Bid strategy changed within last {RECENT_CHANGE_WINDOW_DAYS} days — "
                f"auction behavior is recalibrating")

    # Backup guard: snapshot disagreement (catches cases where CSV is stale
    # or the change happened via API/import that didn't hit the UI log)
    if snapshot_disagreement:
        return ("HOLD",
                f"campaigns.target_cpa_micros disagrees with 30d median "
                f"target_cpa_micros_that_day by >{SNAPSHOT_DISAGREEMENT_PCT:.0%} — "
                f"target appears to have changed recently, let it settle")

    if actual_cpa_14 is None or actual_cpa_30 is None:
        return ("HOLD",
                "Missing actual CPA for 14d or 30d window (no conversions in window)")

    mixed_suffix = " [MIXED REGIME — lower confidence]" if mixed_regime else ""
    conv_free_fall = (wow_conv_trend is not None and wow_conv_trend < CONV_FREE_FALL_WOW)

    # AGGRESSIVE SCALE: deep efficiency headroom + substantial search rank loss
    if (actual_cpa_14 <= current_tcpa * AGGRESSIVE_SCALE_CPA_RATIO
            and actual_cpa_30 <= current_tcpa * AGGRESSIVE_SCALE_CPA_RATIO
            and rank_lost_is is not None
            and rank_lost_is > RANK_LOST_AGGRESSIVE_SCALE):
        label = "MIXED REGIME" if mixed_regime else "AGGRESSIVE SCALE"
        return (label,
                f"Actual CPA (14d=${actual_cpa_14:.2f}, 30d=${actual_cpa_30:.2f}) "
                f"≤ target × {AGGRESSIVE_SCALE_CPA_RATIO:.2f} "
                f"(${current_tcpa * AGGRESSIVE_SCALE_CPA_RATIO:.2f}); "
                f"rank-lost-IS {rank_lost_is:.0%} > {RANK_LOST_AGGRESSIVE_SCALE:.0%} — "
                f"deep headroom{mixed_suffix}")

    # SCALE: standard efficiency headroom + meaningful rank loss
    if (actual_cpa_14 <= current_tcpa * SCALE_CPA_RATIO
            and rank_lost_is is not None
            and rank_lost_is > RANK_LOST_SCALE):
        label = "MIXED REGIME" if mixed_regime else "SCALE"
        return (label,
                f"Actual CPA (14d=${actual_cpa_14:.2f}) ≤ target × {SCALE_CPA_RATIO:.2f} "
                f"(${current_tcpa * SCALE_CPA_RATIO:.2f}); "
                f"rank-lost-IS {rank_lost_is:.0%} > {RANK_LOST_SCALE:.0%} — "
                f"room to capture{mixed_suffix}")

    # TIGHTEN: actual > target × 1.10 on BOTH windows (and not in free-fall)
    tighten_condition = (
        actual_cpa_14 > current_tcpa * TIGHTEN_CPA_RATIO
        and actual_cpa_30 > current_tcpa * TIGHTEN_CPA_RATIO
    )
    if tighten_condition and not conv_free_fall:
        label = "MIXED REGIME" if mixed_regime else "TIGHTEN"
        return (label,
                f"Actual CPA (14d=${actual_cpa_14:.2f}, 30d=${actual_cpa_30:.2f}) "
                f"both > target × {TIGHTEN_CPA_RATIO:.2f} "
                f"(${current_tcpa * TIGHTEN_CPA_RATIO:.2f}) — "
                f"protect efficiency{mixed_suffix}")
    if tighten_condition and conv_free_fall:
        return ("HOLD",
                f"Would TIGHTEN, but conversion volume in free-fall "
                f"(WoW {wow_conv_trend:+.0%}) — lowering tCPA deepens the spiral")

    # HOLD fallthrough
    fallthrough_label = "MIXED REGIME" if mixed_regime else "HOLD"
    return (fallthrough_label,
            f"No action threshold triggered — holding current tCPA{mixed_suffix}")


def propose_targets(
    category: str,
    current_tcpa: float | None,
    break_even_ceiling: float,
) -> tuple[dict[str, float | None], list[str]]:
    """
    Returns ({base, moderate, aggressive}, [warnings]).

    Direction:
      SCALE / AGGRESSIVE SCALE → raise tCPA (capped at break-even ceiling)
      TIGHTEN                  → lower tCPA (lower bound $0.01 safety)
      HOLD / INSUFFICIENT / BUDGET / MIXED → all None

    MIXED REGIME deliberately proposes nothing. The MIXED label exists
    to say "the numbers *suggest* an action, but the window spans a
    regime change, so wait and re-evaluate rather than act." The
    rationale still describes the underlying signal.
    """
    result = {"base": None, "moderate": None, "aggressive": None}
    warnings: list[str] = []

    if category in ("HOLD", "INSUFFICIENT DATA", "BUDGET CONSTRAINED",
                    "MIXED REGIME") or current_tcpa is None:
        return result, warnings

    step_table = [
        ("base",       STEP_BASE),
        ("moderate",   STEP_MODERATE),
        ("aggressive", STEP_AGGRESSIVE),
    ]

    for name, step in step_table:
        effective_step = min(step, MAX_CHANGE_PCT)
        if effective_step != step:
            warnings.append(
                f"{name} step capped at {MAX_CHANGE_PCT:.0%} (was {step:.0%})"
            )

        if category in ("SCALE", "AGGRESSIVE SCALE"):
            proposed = current_tcpa * (1 + effective_step)
            if proposed > break_even_ceiling:
                proposed = break_even_ceiling
                warnings.append(
                    f"{name} capped at break-even ceiling ${break_even_ceiling:.2f}"
                )
        elif category == "TIGHTEN":
            proposed = current_tcpa * (1 - effective_step)
            proposed = max(proposed, 0.01)
        else:
            proposed = None

        result[name] = proposed

    return result, list(dict.fromkeys(warnings))


def scenario_guidance(
    category: str,
    flags: dict[str, bool],
) -> dict[str, str]:
    if category in ("HOLD", "INSUFFICIENT DATA", "BUDGET CONSTRAINED", "MIXED REGIME"):
        return {"base": "n/a", "moderate": "n/a", "aggressive": "n/a"}
    any_flag = any(flags.values())
    return {
        "base":       "advised" if not any_flag else "cautioned",
        "moderate":   "advised" if not any_flag else "cautioned",
        "aggressive": "advised" if not any_flag else "not advised",
    }


# ═══════════════════════════════════════════════════════════════════
# DIAGNOSTIC PRINT (terminal)
# ═══════════════════════════════════════════════════════════════════

def print_diagnostic(
    camp_meta: pd.DataFrame,
    agg_14d: pd.DataFrame,
    agg_30d: pd.DataFrame,
    windows: dict[int, tuple[date, date]],
    raw_max: date,
    stable_end: date,
    blended_cpa_7d: float | None,
    blended_cpa_14d: float | None,
    blended_cpa_30d: float | None,
) -> None:
    banner("DIAGNOSTIC (terminal, pre-workbook)")
    print(f"  Raw MAX(date) in DB: {raw_max}")
    print(f"  Stable end date (used for windows): {stable_end} "
          f"(= raw_max - {BACKFILL_BUFFER_DAYS}d buffer)")
    for d, (s, e) in windows.items():
        crosses, post_days = crosses_attribution_boundary(s, e)
        suffix = ""
        if crosses:
            suffix = f"  [crosses attribution; {post_days}d post]"
        elif e < ATTRIBUTION_GO_LIVE:
            suffix = "  [entirely pre-attribution]"
        print(f"  {d}d window: {s} to {e} ({(e - s).days + 1} days inclusive){suffix}")

    print(f"\n  Aggregate blended CPA (primary_conv_count-based):")
    for label, val in [(" 7d", blended_cpa_7d),
                       ("14d", blended_cpa_14d),
                       ("30d", blended_cpa_30d)]:
        print(f"    {label}: ${val:.2f}" if val is not None else f"    {label}: n/a")

    print(f"\n  Per-campaign totals (30d window, ENABLED campaigns, all-network):")
    enabled = camp_meta[camp_meta["status"] == "ENABLED"]
    merge = enabled.merge(agg_30d, on="campaign_id", how="left")
    for _, r in merge.iterrows():
        name = r["campaign_name"]
        current_t = (r["target_cpa_micros"] / 1e6) if pd.notna(r["target_cpa_micros"]) else None
        current_t_str = f"${current_t:>5.2f}" if current_t is not None else "    n/a"
        spend = r["cost_usd"] if pd.notna(r["cost_usd"]) else 0
        conv  = r["primary_conv_count"] if pd.notna(r["primary_conv_count"]) else 0
        rev   = r["primary_conv_value"] if pd.notna(r["primary_conv_value"]) else 0
        cpa   = r["actual_cpa"]
        cpa_str = f"${cpa:>5.2f}" if pd.notna(cpa) else "    n/a"
        sub_type = r.get("advertising_channel_sub_type") or ""
        hybrid_tag = "  [HYBRID]" if "content" in str(sub_type).lower() else ""
        print(f"    {name[:46]:46s}  tCPA={current_t_str}  "
              f"spend=${spend:>9,.0f}  conv={conv:>6.1f}  rev=${rev:>9,.0f}  "
              f"CPA={cpa_str}{hybrid_tag}")


# ═══════════════════════════════════════════════════════════════════
# EXCEL OUTPUT
# ═══════════════════════════════════════════════════════════════════

def write_sheet(ws, df: pd.DataFrame, freeze_row: int = 1) -> None:
    """Standard sheet writer: purple header, autowidth, freeze top row."""
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(color=HEADER_FONT, bold=True, name="Arial")
    body_font   = Font(name="Arial")

    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, value in enumerate(row, 1):
            if pd.isna(value):
                value = None
            elif isinstance(value, pd.Timestamp):
                value = value.to_pydatetime()
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = body_font

    for col_idx, col_name in enumerate(df.columns, 1):
        if len(df) > 0:
            max_data_len = df.iloc[:, col_idx - 1].astype(str).str.len().max()
            if pd.isna(max_data_len):
                max_data_len = 0
        else:
            max_data_len = 0
        width = min(max(len(str(col_name)), int(max_data_len)) + 2, 50)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if freeze_row:
        ws.freeze_panes = ws.cell(row=freeze_row + 1, column=1)


def color_recommendation_column(ws, df: pd.DataFrame, col_name: str,
                                header_row: int = 1) -> None:
    if col_name not in df.columns:
        return
    col_idx = list(df.columns).index(col_name) + 1
    color_map = {
        "SCALE":              GREEN_FILL,
        "AGGRESSIVE SCALE":   DEEP_GREEN,
        "HOLD":               YELLOW_FILL,
        "TIGHTEN":            RED_FILL,
        "BUDGET CONSTRAINED": ORANGE_FILL,
        "INSUFFICIENT DATA":  GREY_FILL,
        "MIXED REGIME":       BLUE_FILL,
    }
    first_data_row = header_row + 1
    for i, value in enumerate(df[col_name]):
        fill_color = color_map.get(value)
        if fill_color:
            ws.cell(row=first_data_row + i, column=col_idx).fill = \
                PatternFill("solid", fgColor=fill_color)


def write_frontier_summary_sheet(
    ws,
    df: pd.DataFrame,
    run_context: dict,
) -> int:
    """
    Frontier Summary sheet with context block:
    Row 1: Run date | DB max date
    Row 2: 14d window
    Row 3: 30d window
    Row 4: blank
    Row 5-7: Aggregate blended CPA for 7d / 14d / 30d
    Row 8: Attribution regime
    Row 9: blank
    Row 10: column headers
    Row 11+: data rows
    """
    label_font = Font(bold=True, name="Arial")
    value_font = Font(name="Arial")
    warn_font  = Font(bold=True, color="9C5700", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(color=HEADER_FONT, bold=True, name="Arial")
    body_font   = Font(name="Arial")

    ws.cell(row=1, column=1, value="Run date:").font = label_font
    ws.cell(row=1, column=2, value=run_context["run_date"].isoformat()).font = value_font
    ws.cell(row=1, column=4, value="DB max date:").font = label_font
    ws.cell(row=1, column=5, value=run_context["db_max_date"].isoformat()).font = value_font

    w14_start, w14_end = run_context["window_14d"]
    ws.cell(row=2, column=1, value="14d window:").font = label_font
    ws.cell(row=2, column=2,
            value=f"{w14_start.isoformat()}  →  {w14_end.isoformat()}").font = value_font

    w30_start, w30_end = run_context["window_30d"]
    ws.cell(row=3, column=1, value="30d window:").font = label_font
    ws.cell(row=3, column=2,
            value=f"{w30_start.isoformat()}  →  {w30_end.isoformat()}").font = value_font

    for row_offset, (label, blended) in enumerate([
        ("Aggregate  7d CPA:", run_context.get("blended_cpa_7d")),
        ("Aggregate 14d CPA:", run_context.get("blended_cpa_14d")),
        ("Aggregate 30d CPA:", run_context.get("blended_cpa_30d")),
    ]):
        r = 5 + row_offset
        ws.cell(row=r, column=1, value=label).font = label_font
        if blended is not None:
            ws.cell(row=r, column=2, value=f"${blended:.2f}").font = value_font
        else:
            ws.cell(row=r, column=2, value="n/a").font = value_font

    ws.cell(row=8, column=1, value="Attribution:").font = label_font
    crosses, post_days = crosses_attribution_boundary(w30_start, w30_end)
    if crosses:
        ws.cell(row=8, column=2,
                value=f"30d window crosses {ATTRIBUTION_GO_LIVE} ({post_days}d post) — "
                      f"mixed-regime flags will appear").font = warn_font
    elif w30_end < ATTRIBUTION_GO_LIVE:
        ws.cell(row=8, column=2,
                value=f"30d window is entirely PRE-attribution — primary_conv_value "
                      f"will be near-zero; recommendations suppressed").font = warn_font
    else:
        ws.cell(row=8, column=2,
                value=f"30d window is entirely POST-attribution ({ATTRIBUTION_GO_LIVE}) — "
                      f"clean regime").font = value_font

    header_row = 10
    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row_offset, row in enumerate(df.itertuples(index=False)):
        for col_idx, value in enumerate(row, 1):
            if pd.isna(value):
                value = None
            elif isinstance(value, pd.Timestamp):
                value = value.to_pydatetime()
            cell = ws.cell(row=header_row + 1 + row_offset, column=col_idx, value=value)
            cell.font = body_font

    for col_idx, col_name in enumerate(df.columns, 1):
        if len(df) > 0:
            max_data_len = df.iloc[:, col_idx - 1].astype(str).str.len().max()
            if pd.isna(max_data_len):
                max_data_len = 0
        else:
            max_data_len = 0
        width = min(max(len(str(col_name)), int(max_data_len)) + 2, 50)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    return header_row


def write_workbook(
    output_path: Path,
    frontier_df: pd.DataFrame,
    diagnostic_df: pd.DataFrame,
    ad_group_inventory_df: pd.DataFrame,
    run_context: dict,
    attribution_df: pd.DataFrame | None = None,
    sanity_df: pd.DataFrame | None = None,
    network_split_df: pd.DataFrame | None = None,
) -> None:
    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Frontier Summary"
    header_row = write_frontier_summary_sheet(ws1, frontier_df, run_context)
    color_recommendation_column(ws1, frontier_df, "recommendation",
                                header_row=header_row)

    # Change-history sheets (optional — omitted if no CSV was loaded)
    if attribution_df is not None:
        ws_attr = wb.create_sheet("Change Log Attribution")
        if attribution_df.empty:
            ws_attr["A1"] = ("No tCPA changes in change-history CSV for enabled campaigns "
                             "in the analysis window.")
            ws_attr["A1"].font = Font(italic=True, name="Arial")
        else:
            write_sheet(ws_attr, attribution_df)

    if sanity_df is not None:
        ws_sanity = wb.create_sheet("Aggregate Sanity Check")
        if sanity_df.empty:
            ws_sanity["A1"] = ("No clean tCPA changes (with no confounders within ±7d) "
                               "found for aggregate pre/post analysis.")
            ws_sanity["A1"].font = Font(italic=True, name="Arial")
        else:
            write_sheet(ws_sanity, sanity_df)

    # Network Split sheet (30d per-campaign × total/search/audience)
    if network_split_df is not None and not network_split_df.empty:
        ws_net = wb.create_sheet("Network Split")
        # Write attribution-source note in row 1, then data header on row 3
        note = Font(italic=True, color="595959", name="Arial")
        ws_net.cell(row=1, column=1,
                    value="NETWORK SPLIT: 30d view per campaign. Search row = "
                          "keyword_performance_daily (offline 'Home Insurance Lead - Quote Wizard'). "
                          "Audience row = campaign_performance_network_daily (same offline pipeline). "
                          "Total row sums both — CPL/RPL/ROAS are blended across networks.").font = note
        # Blank row 2, data header on row 3
        header_fill = PatternFill("solid", fgColor=HEADER_FILL)
        header_font = Font(color=HEADER_FONT, bold=True, name="Arial")
        body_font   = Font(name="Arial")
        for col_idx, col_name in enumerate(network_split_df.columns, 1):
            cell = ws_net.cell(row=3, column=col_idx, value=col_name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        # Color the network column: total=grey, search=light blue, audience=amber
        net_col_idx = list(network_split_df.columns).index("network") + 1
        net_colors = {
            "total":    "D9D9D9",
            "search":   "DDEBF7",
            "audience": "FFF2CC",
        }
        for row_offset, row in enumerate(network_split_df.itertuples(index=False)):
            for col_idx, value in enumerate(row, 1):
                if pd.isna(value):
                    value = None
                cell = ws_net.cell(row=4 + row_offset, column=col_idx, value=value)
                cell.font = body_font
            net_value = network_split_df.iloc[row_offset]["network"]
            if net_value in net_colors:
                ws_net.cell(row=4 + row_offset, column=net_col_idx).fill = \
                    PatternFill("solid", fgColor=net_colors[net_value])
        # Column widths
        for col_idx, col_name in enumerate(network_split_df.columns, 1):
            if len(network_split_df) > 0:
                max_data_len = network_split_df.iloc[:, col_idx - 1].astype(str).str.len().max()
                if pd.isna(max_data_len):
                    max_data_len = 0
            else:
                max_data_len = 0
            width = min(max(len(str(col_name)), int(max_data_len)) + 2, 50)
            ws_net.column_dimensions[get_column_letter(col_idx)].width = width
        ws_net.freeze_panes = ws_net.cell(row=4, column=1)

    ws2 = wb.create_sheet("Diagnostic")
    write_sheet(ws2, diagnostic_df)

    ws3 = wb.create_sheet("Ad Group Inventory")
    write_sheet(ws3, ad_group_inventory_df)

    wb.save(output_path)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    db_path    = Path(args.db_path)
    output_dir = Path(args.output_dir)
    break_even_override = args.break_even
    change_history_csv  = Path(args.change_history) if args.change_history else CHANGE_HISTORY_CSV

    banner("EFFICIENCY FRONTIER — Microsoft Home Insurance (tCPA)")
    print(f"  DB:              {db_path}")
    print(f"  Account ID:      {ACCOUNT_ID} ({ACCOUNT_LABEL})")
    if break_even_override is not None:
        be_str = f"${break_even_override:.2f} (CLI override)"
    else:
        be_str = (f"dynamic from 30d RPL "
                  f"(manual overrides: {len(BREAK_EVEN_CPA) or 'none'}, "
                  f"fallback: ${BREAK_EVEN_DEFAULT:.2f})")
    print(f"  Break-even:      {be_str}")
    print(f"  Min conversions: {args.min_conversions}")
    print(f"  Dry run:         {args.dry_run}")
    if change_history_csv is None:
        print(f"  Change history:  NOT SET (attribution + sanity sheets omitted)")
    else:
        print(f"  Change history:  {change_history_csv}")

    conn = get_db_connection(db_path)

    # ── Load campaign metadata
    camp_meta = query_campaigns_metadata(conn)
    enabled_campaign_ids = query_enabled_campaign_ids(conn)

    # ── Resolve end date + build windows
    raw_max, stable_end = resolve_end_date(conn, args.end_date)
    windows = build_windows(stable_end)
    start_30d, end_30d = windows[30]
    start_14d, end_14d = windows[14]
    start_7d,  end_7d  = windows[7]

    # ── Load daily frames
    df_daily_30d = query_campaign_daily(
        conn, enabled_campaign_ids, start_30d, end_30d
    )
    # 38d pull so prior-7d window (for WoW) is fully covered
    df_daily_wow = query_campaign_daily(
        conn, enabled_campaign_ids,
        start_30d - timedelta(days=7), end_30d
    )
    # Full-year pull for the Aggregate Sanity Check's pre/post windows
    # around historical target changes.
    df_daily_full = query_campaign_daily(
        conn, enabled_campaign_ids, YEAR_START, end_30d
    )

    # ── Change history (optional)
    change_df = pd.DataFrame()
    if change_history_csv is not None:
        try:
            change_df = load_change_history(change_history_csv)
            print(f"\n  Loaded {len(change_df)} YTD change-history rows: "
                  f"{(change_df['change_kind'] == 'target').sum()} tCPA, "
                  f"{(change_df['change_kind'] == 'budget').sum()} budget, "
                  f"{(change_df['change_kind'] == 'strategy').sum()} strategy")
            # Filter to enabled campaigns — unknown names are informational
            known = set(camp_meta[camp_meta["status"] == "ENABLED"]["campaign_name"])
            unknown_in_csv = set(change_df["campaign_name"]) - set(camp_meta["campaign_name"])
            if unknown_in_csv:
                print(f"  ⚠  {len(unknown_in_csv)} CSV campaign names not found in DB "
                      f"(may be paused/renamed; changes will be ignored)")
                if len(unknown_in_csv) <= 5:
                    for n in sorted(unknown_in_csv):
                        print(f"     - {n}")
        except FileNotFoundError as e:
            print(f"\n  ⚠  Change history CSV not found: {e}")
            print(f"     Continuing without attribution sheets; falling back to "
                  f"snapshot-disagreement guard only.")
            change_df = pd.DataFrame()

    # ── Smoke tests
    run_smoke_tests(conn, camp_meta, enabled_campaign_ids,
                    df_daily_30d, start_30d, stable_end)

    # ── Audience-network daily frame (wider than 30d so WoW prior-7d and
    # full-year sanity check windows are fully covered). Microsoft's tCPA
    # applies to search + audience under a single target, so the recommendation
    # engine needs all-network spend (search + audience) divided by all-network
    # conversions. keyword_performance_daily already aggregates conversions
    # across all networks; this query provides the audience-spend side.
    audience_daily_df = query_audience_network_daily(
        conn, enabled_campaign_ids,
        start_30d - timedelta(days=7),   # covers WoW prior-7d lookback
        end_30d,
    )
    # Wider pull for the aggregate sanity check's pre/post windows around
    # historical tCPA changes (which may date from January onward).
    audience_daily_full = query_audience_network_daily(
        conn, enabled_campaign_ids, YEAR_START, end_30d,
    )

    # Campaign-level 30d rollup — used by audience-share flag and Network Split
    if not audience_daily_df.empty:
        audience_mask_30d = (
            (pd.to_datetime(audience_daily_df["date"]).dt.date >= start_30d) &
            (pd.to_datetime(audience_daily_df["date"]).dt.date <= end_30d)
        )
        audience_30d = audience_daily_df.loc[audience_mask_30d]
        audience_rollup = (
            audience_30d.groupby("campaign_id", as_index=False)
                        .agg(audience_cost_usd=("audience_cost_usd", "sum"),
                             audience_impressions=("audience_impressions", "sum"),
                             audience_clicks=("audience_clicks", "sum"),
                             audience_native_conv=("audience_native_conv", "sum"),
                             audience_native_conv_value=("audience_native_conv_value", "sum"))
        )
        audience_map = dict(zip(
            audience_rollup["campaign_id"], audience_rollup["audience_cost_usd"]
        ))
    else:
        audience_rollup = pd.DataFrame(columns=[
            "campaign_id", "audience_cost_usd", "audience_impressions",
            "audience_clicks", "audience_native_conv", "audience_native_conv_value",
        ])
        audience_map = {}

    # ── Aggregate windows (ALL-NETWORK: search_spend + audience_spend in cost_usd)
    agg_30d = aggregate_window(df_daily_30d, start_30d, end_30d,
                               audience_daily=audience_daily_df)
    agg_14d = aggregate_window(df_daily_30d, start_14d, end_14d,
                               audience_daily=audience_daily_df)
    agg_7d  = aggregate_window(df_daily_wow, start_7d,  end_7d,
                               audience_daily=audience_daily_df)

    # Prior-7d for WoW
    prev_start = start_7d - timedelta(days=7)
    prev_end   = start_7d - timedelta(days=1)
    agg_prev_7d = aggregate_window(df_daily_wow, prev_start, prev_end,
                                   audience_daily=audience_daily_df)

    # ── Dynamic break-even from 30d RPL. Break-even CPA = revenue per lead,
    # which is already all-network because keyword_performance_daily aggregates
    # across networks. No spend term in this calc, so no audience fix needed.
    dynamic_break_even = compute_break_even_cpa(df_daily_30d, start_30d, end_30d)

    # ── Build recent-change maps (14d window from stable_end)
    name_to_id = dict(zip(camp_meta["campaign_name"], camp_meta["campaign_id"]))
    recent_target_by_id: dict[str, bool]   = {cid: False for cid in enabled_campaign_ids}
    recent_budget_by_id: dict[str, bool]   = {cid: False for cid in enabled_campaign_ids}
    recent_strategy_by_id: dict[str, bool] = {cid: False for cid in enabled_campaign_ids}
    if not change_df.empty:
        cutoff_ts = datetime.combine(
            stable_end - timedelta(days=RECENT_CHANGE_WINDOW_DAYS - 1),
            datetime.min.time(),
        )
        recent = change_df[change_df["timestamp"] >= cutoff_ts]
        for _, r in recent.iterrows():
            cid = name_to_id.get(r["campaign_name"])
            if cid not in recent_target_by_id:
                continue
            if r["change_kind"] == "target":
                recent_target_by_id[cid] = True
            elif r["change_kind"] == "budget":
                recent_budget_by_id[cid] = True
            elif r["change_kind"] == "strategy":
                recent_strategy_by_id[cid] = True

    # ── Blended aggregate CPAs
    def _blended_cpa(agg_df: pd.DataFrame) -> float | None:
        spend = float(agg_df["cost_usd"].sum())
        conv  = float(agg_df["primary_conv_count"].sum())
        return (spend / conv) if conv > 0 else None

    blended_cpa_30d = _blended_cpa(agg_30d)
    blended_cpa_14d = _blended_cpa(agg_14d)
    blended_cpa_7d  = _blended_cpa(agg_7d)

    # ── Mixed-regime detection per window
    mixed_30d, post_days_30d = crosses_attribution_boundary(start_30d, end_30d)

    # ── Build Frontier Summary
    summary_rows = []
    enabled = camp_meta[camp_meta["status"] == "ENABLED"]

    def _pick(df: pd.DataFrame, col: str, default=0.0):
        return float(df[col].iloc[0]) if not df.empty and pd.notna(df[col].iloc[0]) else default

    for _, cm in enabled.iterrows():
        cid  = cm["campaign_id"]
        name = cm["campaign_name"]
        current_tcpa = (cm["target_cpa_micros"] / 1e6) if pd.notna(cm["target_cpa_micros"]) else None
        sub_type = (cm["advertising_channel_sub_type"] or "")
        is_hybrid = "content" in sub_type.lower()

        m14 = agg_14d[agg_14d["campaign_id"] == cid]
        m30 = agg_30d[agg_30d["campaign_id"] == cid]
        m7  = agg_7d[agg_7d["campaign_id"]  == cid]
        mp7 = agg_prev_7d[agg_prev_7d["campaign_id"] == cid]

        spend_14 = _pick(m14, "cost_usd")
        spend_30 = _pick(m30, "cost_usd")
        spend_7  = _pick(m7,  "cost_usd")
        conv_14  = _pick(m14, "primary_conv_count")
        conv_30  = _pick(m30, "primary_conv_count")
        conv_7   = _pick(m7,  "primary_conv_count")
        conv_prev7 = _pick(mp7, "primary_conv_count")
        rev_14   = _pick(m14, "primary_conv_value")
        rev_30   = _pick(m30, "primary_conv_value")
        rev_7    = _pick(m7,  "primary_conv_value")

        cpa_14  = (spend_14 / conv_14) if conv_14 > 0 else None
        cpa_30  = (spend_30 / conv_30) if conv_30 > 0 else None
        cpa_7   = (spend_7  / conv_7)  if conv_7  > 0 else None
        roas_14 = (rev_14 / spend_14) if spend_14 > 0 else None
        roas_30 = (rev_30 / spend_30) if spend_30 > 0 else None
        roas_7  = (rev_7  / spend_7)  if spend_7  > 0 else None

        rank_lost = None
        if not m30.empty and pd.notna(m30["mean_rank_lost_is"].iloc[0]):
            rank_lost = float(m30["mean_rank_lost_is"].iloc[0])
        budget_lost = None
        if not m30.empty and pd.notna(m30["mean_budget_lost_is"].iloc[0]):
            budget_lost = float(m30["mean_budget_lost_is"].iloc[0])

        wow = ((conv_7 - conv_prev7) / conv_prev7) if conv_prev7 > 0 else None

        snapshot_disagreement = False
        if (current_tcpa is not None and not m30.empty
                and pd.notna(m30["median_target_cpa_that_day_usd"].iloc[0])):
            snap_med = float(m30["median_target_cpa_that_day_usd"].iloc[0])
            if snap_med > 0:
                snapshot_disagreement = (
                    abs(current_tcpa - snap_med) / snap_med > SNAPSHOT_DISAGREEMENT_PCT
                )

        audience_spend = float(audience_map.get(cid, 0.0))
        total_30d_spend = spend_30 + audience_spend
        audience_share_30d = (audience_spend / total_30d_spend) if total_30d_spend > 0 else 0.0

        # Break-even precedence: CLI override > manual dict > dynamic RPL > fallback
        if break_even_override is not None:
            break_even_ceiling = break_even_override
            break_even_source  = "cli_override"
        elif cid in BREAK_EVEN_CPA:
            break_even_ceiling = BREAK_EVEN_CPA[cid]
            break_even_source  = "manual_dict"
        elif cid in dynamic_break_even:
            break_even_ceiling = dynamic_break_even[cid]
            break_even_source  = "dynamic_rpl_30d"
        else:
            break_even_ceiling = BREAK_EVEN_DEFAULT
            break_even_source  = "fallback_default"

        # Recent-change flags from CSV (14d window)
        rec_target   = recent_target_by_id.get(cid, False)
        rec_budget   = recent_budget_by_id.get(cid, False)
        rec_strategy = recent_strategy_by_id.get(cid, False)

        category, rationale = classify_recommendation(
            actual_cpa_14=cpa_14,
            actual_cpa_30=cpa_30,
            conv_30d=conv_30,
            current_tcpa=current_tcpa,
            rank_lost_is=rank_lost,
            budget_lost_is=budget_lost,
            wow_conv_trend=wow,
            mixed_regime=mixed_30d,
            post_attribution_days=post_days_30d,
            snapshot_disagreement=snapshot_disagreement,
            recent_target_change=rec_target,
            recent_budget_change=rec_budget,
            recent_strategy_change=rec_strategy,
            min_conversions=args.min_conversions,
        )
        proposals, prop_warnings = propose_targets(
            category, current_tcpa, break_even_ceiling
        )

        flags = {
            "mixed_regime":           mixed_30d,
            "snapshot_disagreement":  snapshot_disagreement,
            "recent_target_change":   rec_target,
            "recent_budget_change":   rec_budget,
            "recent_strategy_change": rec_strategy,
            "conv_free_fall":         wow is not None and wow < CONV_FREE_FALL_WOW,
            "high_audience_share":    audience_share_30d > 0.25,
        }
        guidance = scenario_guidance(category, flags)

        summary_rows.append({
            "campaign_name":           name,
            "campaign_id":             cid,
            "is_hybrid":               is_hybrid,
            "current_tcpa":            round(current_tcpa, 2) if current_tcpa else None,
            "break_even_ceiling":      round(break_even_ceiling, 2),
            "break_even_source":       break_even_source,
            "actual_cpa_7d":           round(cpa_7, 2)  if cpa_7  else None,
            "actual_cpa_14d":          round(cpa_14, 2) if cpa_14 else None,
            "actual_cpa_30d":          round(cpa_30, 2) if cpa_30 else None,
            "actual_roas_7d":          round(roas_7, 4)  if roas_7  else None,
            "actual_roas_14d":         round(roas_14, 4) if roas_14 else None,
            "actual_roas_30d":         round(roas_30, 4) if roas_30 else None,
            "spend_7d":                round(spend_7, 2),
            "spend_14d":               round(spend_14, 2),
            "spend_30d":               round(spend_30, 2),
            "conv_7d":                 round(conv_7, 1),
            "conv_14d":                round(conv_14, 1),
            "conv_30d":                round(conv_30, 1),
            "search_rank_lost_is":     round(rank_lost, 4) if rank_lost is not None else None,
            "search_budget_lost_is":   round(budget_lost, 4) if budget_lost is not None else None,
            "audience_spend_30d":      round(audience_spend, 2),
            "audience_share_30d":      round(audience_share_30d, 4),
            "wow_conv_trend_7d":       round(wow, 4) if wow is not None else None,
            "recommendation":          category,
            "rationale":               rationale,
            "proposed_base":           round(proposals["base"], 2)       if proposals["base"]       else None,
            "proposed_moderate":       round(proposals["moderate"], 2)   if proposals["moderate"]   else None,
            "proposed_aggressive":     round(proposals["aggressive"], 2) if proposals["aggressive"] else None,
            "guidance_base":           guidance["base"],
            "guidance_moderate":       guidance["moderate"],
            "guidance_aggressive":     guidance["aggressive"],
            "flag_mixed_regime":         flags["mixed_regime"],
            "flag_snapshot_disagreement": flags["snapshot_disagreement"],
            "flag_recent_target_change":   flags["recent_target_change"],
            "flag_recent_budget_change":   flags["recent_budget_change"],
            "flag_recent_strategy_change": flags["recent_strategy_change"],
            "flag_conv_free_fall":       flags["conv_free_fall"],
            "flag_high_audience_share":  flags["high_audience_share"],
            "proposal_warnings":       "; ".join(prop_warnings) if prop_warnings else None,
        })
    frontier_df = pd.DataFrame(summary_rows)

    # ── Diagnostic sheet (all-network totals)
    diag_rows = []
    for window_days, (s, e) in windows.items():
        src = df_daily_wow if window_days == 7 else df_daily_30d
        agg = aggregate_window(src, s, e, audience_daily=audience_daily_df)
        total = agg.agg({
            "impressions": "sum", "clicks": "sum",
            "search_cost_usd": "sum", "audience_cost_usd": "sum",
            "cost_usd": "sum",
            "primary_conv_count": "sum", "primary_conv_value": "sum",
        })
        crosses, post_days = crosses_attribution_boundary(s, e)
        diag_rows.append({
            "window_days":          window_days,
            "window_start":         s,
            "window_end":           e,
            "impressions":          int(total["impressions"]),
            "clicks":               int(total["clicks"]),
            "search_cost_usd":      round(float(total["search_cost_usd"]), 2),
            "audience_cost_usd":    round(float(total["audience_cost_usd"]), 2),
            "cost_usd":             round(float(total["cost_usd"]), 2),
            "primary_conv_count":   round(float(total["primary_conv_count"]), 1),
            "primary_conv_value":   round(float(total["primary_conv_value"]), 2),
            "blended_cpa":          (
                round(float(total["cost_usd"]) / float(total["primary_conv_count"]), 2)
                if total["primary_conv_count"] > 0 else None
            ),
            "blended_roas":         (
                round(float(total["primary_conv_value"]) / float(total["cost_usd"]), 4)
                if total["cost_usd"] > 0 else None
            ),
            "crosses_attribution":  crosses,
            "post_attribution_days": post_days,
        })
    diagnostic_df = pd.DataFrame(diag_rows)

    # ── Ad group inventory
    ag_counts = query_ad_group_status_counts(conn, enabled_campaign_ids)
    if not ag_counts.empty:
        ag_pivot = (
            ag_counts.pivot(index="campaign_id", columns="status", values="n")
                     .fillna(0).astype(int).reset_index()
        )
        ag_pivot = ag_pivot.merge(
            camp_meta[["campaign_id", "campaign_name"]],
            on="campaign_id", how="left"
        )
        cols_front = ["campaign_name", "campaign_id"]
        other_cols = [c for c in ag_pivot.columns if c not in cols_front]
        ag_pivot = ag_pivot[cols_front + other_cols]
        if "ENABLED" in ag_pivot.columns:
            ag_pivot["zero_enabled_flag"] = ag_pivot["ENABLED"] == 0
        else:
            ag_pivot["zero_enabled_flag"] = True
    else:
        ag_pivot = pd.DataFrame(columns=[
            "campaign_name", "campaign_id", "zero_enabled_flag"
        ])

    # ── Change log attribution + aggregate sanity check (optional)
    if not change_df.empty:
        enabled_names = set(camp_meta[camp_meta["status"] == "ENABLED"]["campaign_name"])
        attribution_df = build_change_attribution(change_df, enabled_names)

        # Augment the full-year daily frame with audience spend so pre/post
        # windows reflect all-network spend (matching how the tCPA actually
        # governs both networks under a single target).
        df_daily_full_all_network = df_daily_full.copy()
        if not audience_daily_full.empty:
            audience_slim = audience_daily_full[[
                "campaign_id", "date", "audience_cost_usd"
            ]]
            df_daily_full_all_network = df_daily_full_all_network.merge(
                audience_slim, on=["campaign_id", "date"], how="left"
            )
            df_daily_full_all_network["audience_cost_usd"] = \
                df_daily_full_all_network["audience_cost_usd"].fillna(0.0)
            # Overwrite cost_usd with all-network total
            df_daily_full_all_network["cost_usd"] = (
                df_daily_full_all_network["cost_usd"]
                + df_daily_full_all_network["audience_cost_usd"]
            )

        sanity_df = build_aggregate_sanity_check(
            attribution_df, df_daily_full_all_network, name_to_id
        )
    else:
        attribution_df = None
        sanity_df      = None

    # ── Network split diagnostic (30d, per-campaign × total/search/audience)
    network_split_df = build_network_split(
        camp_meta, enabled_campaign_ids, df_daily_30d,
        audience_rollup, start_30d, end_30d,
    )

    # ── Terminal diagnostic
    print_diagnostic(
        camp_meta, agg_14d, agg_30d, windows, raw_max, stable_end,
        blended_cpa_7d=blended_cpa_7d,
        blended_cpa_14d=blended_cpa_14d,
        blended_cpa_30d=blended_cpa_30d,
    )

    # Print dynamic break-even summary so you can sanity-check against
    # your RPL monitoring
    if dynamic_break_even:
        id_to_name = {v: k for k, v in name_to_id.items()}
        print(f"\n  Dynamic break-even CPAs (30d RPL, enabled campaigns only):")
        for cid in enabled_campaign_ids:
            cname = id_to_name.get(cid, cid)
            be = dynamic_break_even.get(cid)
            if be is None:
                print(f"    {cname[:50]:50s}  n/a (zero conversions in 30d)")
            else:
                print(f"    {cname[:50]:50s}  ${be:>6.2f}")

    # ── Write workbook (or skip on dry-run)
    if args.dry_run:
        banner("DRY RUN COMPLETE — workbook not written")
        extra_sheets = []
        if attribution_df is not None:
            extra_sheets.append(f"{len(attribution_df)} attribution")
        if sanity_df is not None:
            extra_sheets.append(f"{len(sanity_df)} sanity")
        if network_split_df is not None and not network_split_df.empty:
            extra_sheets.append(f"{len(network_split_df)} network-split")
        extra_note = f" plus {', '.join(extra_sheets)} rows" if extra_sheets else ""
        print(f"  Would have written {len(frontier_df)} Frontier Summary rows, "
              f"{len(diagnostic_df)} diagnostic rows, "
              f"{len(ag_pivot)} ad group inventory rows{extra_note}.")
        if not frontier_df.empty:
            print("\n  Frontier Summary preview (key columns):")
            cols = ["campaign_name", "current_tcpa", "break_even_ceiling",
                    "actual_cpa_7d", "actual_cpa_14d", "actual_cpa_30d",
                    "search_rank_lost_is", "search_budget_lost_is",
                    "recommendation", "proposed_base", "proposed_moderate",
                    "proposed_aggressive"]
            cols = [c for c in cols if c in frontier_df.columns]
            with pd.option_context("display.max_columns", None, "display.width", 240):
                print(frontier_df[cols].to_string(index=False))
        if attribution_df is not None and not attribution_df.empty:
            print("\n  Change Log Attribution preview:")
            with pd.option_context("display.max_columns", None, "display.width", 200):
                print(attribution_df.to_string(index=False))
        if network_split_df is not None and not network_split_df.empty:
            print("\n  Network Split preview (30d, per-campaign × total/search/audience):")
            cols = ["campaign_name", "network", "spend_usd", "conv_count",
                    "conv_value", "cpl", "rpl", "roas"]
            cols = [c for c in cols if c in network_split_df.columns]
            with pd.option_context("display.max_columns", None, "display.width", 240):
                print(network_split_df[cols].to_string(index=False))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"efficiency_frontier_{args.account}_{stable_end.isoformat()}.xlsx"
        run_context = {
            "run_date":        date.today(),
            "db_max_date":     raw_max,
            "window_14d":      windows[14],
            "window_30d":      windows[30],
            "blended_cpa_7d":  blended_cpa_7d,
            "blended_cpa_14d": blended_cpa_14d,
            "blended_cpa_30d": blended_cpa_30d,
        }
        write_workbook(output_path, frontier_df, diagnostic_df, ag_pivot,
                       run_context,
                       attribution_df=attribution_df, sanity_df=sanity_df,
                       network_split_df=network_split_df)
        banner("DONE")
        print(f"  Workbook written to: {output_path}")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n❌ SMOKE TEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        raise
