"""
efficiency_frontier.py
────────────────────────────────────────────────────────────────────
Weekly tROAS recommendation tool for Google Home Insurance campaigns.

Reads from paid_search.db + two Google Ads change-history CSVs + a
stub partner-pauses CSV. Produces an Excel workbook with recommendations
under three scenarios (base 3% / moderate 7% / aggressive 15%).

Usage:
    python efficiency_frontier.py
    python efficiency_frontier.py --end-date 2026-04-15
    python efficiency_frontier.py --dry-run        # smoke tests + diagnostic, no xlsx
    python efficiency_frontier.py --break-even 1.0 # override break-even floor

Scope (v1):
    - Google Home Insurance (account_id = 1)
    - 7 enabled NB-SEARCH-ALL-USA-STE campaigns
    - 2026 data only
    - Rolling windows end at MAX(date) - 2 days to buffer conversion backfill
────────────────────────────────────────────────────────────────────
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")

# Default CSV locations — override via --targets-csv / --budgets-csv / --pauses-csv
DEFAULT_TARGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report.csv")
DEFAULT_BUDGETS_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\Change_history_report__1_.csv")
DEFAULT_PAUSES_CSV  = Path(r"C:\Projects\google-ads-ops\analysis\input\partner_pauses.csv")

ACCOUNT_ID = 1

ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE
    "23394808297",   # HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE
    "23394808300",   # HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE
    "23428389081",   # HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
    "23496334752",   # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
    "23530860942",   # HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE
    "23586637678",   # HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE
]

# Recommendation thresholds
BREAK_EVEN_HARD_FLOOR = 1.0   # no proposal below this, ever
SCALE_FLOOR           = 1.2   # SCALE proposals never go below margin goal
RAISE_TO_FLOOR_TARGET = 1.2   # RAISE TO FLOOR caps here
GOAL_ROAS             = 1.2   # margin goal — used for aggregate banner and contribution column

# Scenario step sizes — standard (TIGHTEN, RAISE TO FLOOR, SCALE)
STEP_BASE       = 0.03
STEP_MODERATE   = 0.07
STEP_AGGRESSIVE = 0.15

# Scenario step sizes — TIGHTEN LITE (smaller nudges for soft-miss campaigns)
STEP_BASE_LITE       = 0.01
STEP_MODERATE_LITE   = 0.02
STEP_AGGRESSIVE_LITE = 0.03

# Trigger thresholds (multiplicative) — NORMAL mode
SCALE_TRIGGER        = 1.10   # actual ROAS >= target * 1.10 (10% over)
TIGHTEN_TRIGGER      = 0.90   # actual ROAS <  target * 0.90 (10% under)
TIGHTEN_LITE_TRIGGER = 0.95   # 0.95 to 1.00 of target AND target < 1.2 => TIGHTEN LITE

# Trigger thresholds — MARGIN PRESSURE mode
# When enabled via --margin-pressure, regular TIGHTEN absorbs the LITE range
# (fires on any target miss), SCALE becomes harder to trigger, and RAISE TO
# FLOOR fires even on near-miss campaigns.
MP_TIGHTEN_TRIGGER        = 1.00  # actual < target (i.e. any miss) triggers TIGHTEN
MP_SCALE_TRIGGER          = 1.15
MP_RTF_MIN_ACTUAL_PCT     = 0.98  # actual ROAS >= target * 0.98 counts for RAISE TO FLOOR

# Data quality guards
MIN_CONVERSIONS_30D           = 30     # below this => INSUFFICIENT DATA
CONV_FREE_FALL_WOW            = -0.20  # WoW conv drop worse than -20% => HOLD
RECENT_CHANGE_WINDOW_DAYS     = 14     # target/budget change < 14d ago => HOLD
AGGRESSIVE_CAUTION_WINDOW_DAYS = 14    # flags for scenario guidance

# Buffer for offline conversion backfill
BACKFILL_BUFFER_DAYS = 2

# Liberty-mixed regime start (no ad-group status history available)
LIBERTY_START_DATE = date(2026, 2, 11)

# Analysis year cutoff
YEAR_START = date(2026, 1, 1)

# Formatting constants
HEADER_FILL = "7030A0"   # purple
HEADER_FONT = "FFFFFF"   # white
GREEN_FILL  = "E2EFDA"   # SCALE
BLUE_FILL   = "D9E1F2"   # RAISE TO FLOOR
YELLOW_FILL = "FFEB9C"   # HOLD
RED_FILL    = "FFC7CE"   # TIGHTEN
LITE_FILL   = "FFE2E6"   # TIGHTEN LITE (lighter red)
GREY_FILL   = "D9D9D9"   # INSUFFICIENT DATA


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
    p.add_argument("--account", default="home",
                   help="Account vertical (default: home)")
    p.add_argument("--end-date", default=None,
                   help="Analysis end date YYYY-MM-DD (default: MAX(date) - 2 from DB)")
    p.add_argument("--break-even", type=float, default=BREAK_EVEN_HARD_FLOOR,
                   help=f"Hard break-even ROAS floor (default: {BREAK_EVEN_HARD_FLOOR})")
    p.add_argument("--targets-csv", default=str(DEFAULT_TARGETS_CSV),
                   help="Path to target-change CSV export")
    p.add_argument("--budgets-csv", default=str(DEFAULT_BUDGETS_CSV),
                   help="Path to budget-change CSV export")
    p.add_argument("--pauses-csv", default=str(DEFAULT_PAUSES_CSV),
                   help="Path to partner-pause annotation CSV (auto-created if missing)")
    p.add_argument("--db-path", default=str(DB_PATH),
                   help="Path to paid_search.db")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR),
                   help="Directory for Excel output")
    p.add_argument("--dry-run", action="store_true",
                   help="Run smoke tests + diagnostic print, do not write xlsx")
    p.add_argument("--margin-pressure", action="store_true",
                   help="Tighten thresholds globally: TIGHTEN fires on <5%% miss (not 10%%), "
                        "SCALE requires 15%% over target (not 10%%), RAISE TO FLOOR fires even "
                        "on near-miss below-goal campaigns. For margin-pressure weeks where "
                        "raising aggregate ROAS is the priority. Absorbs TIGHTEN LITE.")
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
    If --end-date is provided, uses it directly as stable_end_date.
    """
    row = conn.execute(
        "SELECT MAX(date) AS max_date FROM campaign_performance_daily WHERE account_id = ?",
        (ACCOUNT_ID,),
    ).fetchone()
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


# ═══════════════════════════════════════════════════════════════════
# CHANGE-LOG CSV PARSERS
# ═══════════════════════════════════════════════════════════════════

CAMPAIGN_ID_BY_NAME: dict[str, str] = {}  # populated in smoke tests


def _parse_gads_datetime(s: str) -> datetime:
    """Google Ads CSV exports: 'Mar 30, 2026, 9:31:59 AM'."""
    return datetime.strptime(s.strip(), "%b %d, %Y, %I:%M:%S %p")


def _normalize_target_value(raw: str | None) -> float | None:
    """
    Google Ads exports targets inconsistently: sometimes '116%', sometimes '1.16'.
    Normalize both to decimal ROAS multiplier (116% -> 1.16).
    Returns None if unparseable.
    """
    if raw is None:
        return None
    s = raw.strip().rstrip(",").rstrip('"').strip()
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
    # Heuristic: Google Ads exports targets inconsistently. A value like
    # "1.16" is already decimal; a value like "113" (no % sign) is a
    # percentage missing its symbol. We distinguish by magnitude: any
    # numeric value > 10 is assumed to be a percentage.
    # ASSUMPTION: no campaign targets exceed 1000% (i.e. 10.0 as a decimal).
    # This is safe for insurance verticals; if you ever target >10x ROAS
    # you'll need to revisit this.
    if v > 10:
        return v / 100.0
    return v


def load_target_changes(csv_path: Path) -> pd.DataFrame:
    """
    Parses the target-ROAS change history CSV.
    Returns DataFrame with columns:
        timestamp, campaign_name, campaign_id, ad_group_name,
        old_target, new_target, scope
    Only keeps rows where Target ROAS actually changed.
    Only keeps rows from YEAR_START onwards.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Target-change CSV not found: {csv_path}")

    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    # Skip the two header meta rows ("Change history report", "<date range>")
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

        # Parse "changed from X to Y" OR "changed to Y" (initial set)
        m = re.search(r"changed from\s+(\S+?)\s+to\s+(\S+)", ch)
        if m:
            old = _normalize_target_value(m.group(1))
            new = _normalize_target_value(m.group(2))
        else:
            # "Target ROAS is 120%" style — treat as initial set
            m2 = re.search(r"Target ROAS (?:is|was)\s+(\S+)", ch)
            if m2:
                old, new = None, _normalize_target_value(m2.group(1))
            else:
                old, new = None, None

        scope = "AG" if (r.get("Ad group") or "").strip() else "CAMPAIGN"
        rows.append({
            "timestamp": ts,
            "campaign_name": r["Campaign"],
            "ad_group_name": r.get("Ad group", "") or "",
            "old_target": old,
            "new_target": new,
            "scope": scope,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def load_budget_changes(csv_path: Path) -> pd.DataFrame:
    """
    Parses the budget change history CSV.
    Returns DataFrame with columns:
        timestamp, campaign_name, old_budget, new_budget
    Only 'Budget amount' rows from YEAR_START onwards for tracked campaigns.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Budget-change CSV not found: {csv_path}")

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

        m = re.search(r"from \$([\d,]+\.\d{2}) to \$([\d,]+\.\d{2})", ch)
        if m:
            old = float(m.group(1).replace(",", ""))
            new = float(m.group(2).replace(",", ""))
        else:
            old, new = None, None

        rows.append({
            "timestamp": ts,
            "campaign_name": r.get("Campaign", "") or "",
            "old_budget": old,
            "new_budget": new,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("timestamp").reset_index(drop=True)


def ensure_pauses_csv(csv_path: Path) -> pd.DataFrame:
    """
    Reads partner-pauses CSV. Creates an empty stub with header if missing.
    Columns: campaign_name, pause_date, scope, identifier, notes
    scope ∈ {campaign, ad_group, keyword}
    """
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["campaign_name", "pause_date", "scope", "identifier", "notes"])
        print(f"ℹ  Created stub pause CSV: {csv_path}")
        return pd.DataFrame(columns=["campaign_name", "pause_date", "scope", "identifier", "notes"])

    df = pd.read_csv(csv_path)
    if "pause_date" in df.columns and not df.empty:
        df["pause_date"] = pd.to_datetime(df["pause_date"], errors="coerce").dt.date
    return df


# ═══════════════════════════════════════════════════════════════════
# CURRENT-TARGET DERIVATION
# ═══════════════════════════════════════════════════════════════════

def derive_current_target_per_campaign(
    target_df: pd.DataFrame,
    enabled_campaign_names: set[str],
) -> pd.DataFrame:
    """
    For each enabled campaign, the latest 2026 CAMPAIGN-level target change
    is the current target. Ad-group changes are excluded (Liberty is paused).

    Returns DataFrame indexed by campaign_name with columns:
        current_target, current_target_ts,
        previous_target, previous_target_ts
    """
    if target_df.empty:
        return pd.DataFrame(
            index=pd.Index(sorted(enabled_campaign_names), name="campaign_name"),
            columns=["current_target", "current_target_ts",
                     "previous_target", "previous_target_ts"],
        )

    camp_level = target_df[
        (target_df["scope"] == "CAMPAIGN")
        & (target_df["campaign_name"].isin(enabled_campaign_names))
    ].copy().sort_values("timestamp")

    out = {}
    for name in enabled_campaign_names:
        sub = camp_level[camp_level["campaign_name"] == name]
        if sub.empty:
            out[name] = {
                "current_target": None, "current_target_ts": None,
                "previous_target": None, "previous_target_ts": None,
            }
            continue
        last = sub.iloc[-1]
        prev = sub.iloc[-2] if len(sub) >= 2 else None
        out[name] = {
            "current_target": last["new_target"],
            "current_target_ts": last["timestamp"],
            "previous_target": prev["new_target"] if prev is not None else last["old_target"],
            "previous_target_ts": prev["timestamp"] if prev is not None else None,
        }

    return pd.DataFrame.from_dict(out, orient="index").rename_axis("campaign_name")


# ═══════════════════════════════════════════════════════════════════
# DB QUERIES FOR PERFORMANCE AGGREGATION
# ═══════════════════════════════════════════════════════════════════

def query_campaign_daily(
    conn: sqlite3.Connection,
    campaign_ids: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """
    Returns daily rollup across devices for each campaign in the window.
    Uses campaign_performance_daily (native Google conversions) plus joins to
    keyword_performance_daily for primary_conv_count/value which are the
    offline-routed metrics the team uses.

    Note: campaign_performance_daily has `conversions` (Google native) — but
    the analyst uses primary_conv_count/value from keyword_performance_daily
    as the offline-routed source of truth. We aggregate keyword_performance_daily
    to the campaign × date grain.

    IMPORTANT: campaign_performance_daily uses the same convention as
    keyword_performance_daily — rows exist per device (DESKTOP, MOBILE,
    TABLET) AND a rollup row with device='ALL'. Filter to device='ALL'
    only or spend/conversions will be double-counted.
    """
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
            -- target_roas_that_day is identical across devices for a given
            -- campaign-date, so taking any non-null value is safe.
            MAX(target_roas_that_day)             AS target_roas_that_day,
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

    # Now get primary_conv_count/value via keyword_performance_daily joined to
    # keyword_instances for the campaign bridge.
    #
    # IMPORTANT: keyword_performance_daily stores BOTH per-device / per-geo
    # breakdown rows AND pre-aggregated rollup rows (device='ALL', geo_state='ALL').
    # We must filter to the rollup rows only, or we double- (actually multiple-)
    # count conversions. Matches the codebase convention used elsewhere.
    sql_kpd = f"""
        SELECT
            ki.campaign_id       AS campaign_id,
            kpd.report_date      AS date,
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

    # Outer merge — keep all cpd rows even if kpd has no matching row (zero conv)
    df = df_cpd.merge(df_kpd, on=["campaign_id", "date"], how="left")
    df["primary_conv_count"] = df["primary_conv_count"].fillna(0.0)
    df["primary_conv_value"] = df["primary_conv_value"].fillna(0.0)
    return df


def aggregate_window(df_daily: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """
    Aggregate campaign × date daily DF to per-campaign totals for a window.
    Returns one row per campaign with spend, clicks, impressions, conversions,
    revenue, and computed ROAS / CPA.
    """
    mask = (pd.to_datetime(df_daily["date"]).dt.date >= start) & \
           (pd.to_datetime(df_daily["date"]).dt.date <= end)
    w = df_daily.loc[mask].copy()
    if w.empty:
        return pd.DataFrame(columns=[
            "campaign_id", "impressions", "clicks", "cost_usd",
            "primary_conv_count", "primary_conv_value",
            "actual_roas", "actual_cpa",
        ])

    agg = w.groupby("campaign_id", as_index=False).agg(
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        cost_usd=("cost_usd", "sum"),
        primary_conv_count=("primary_conv_count", "sum"),
        primary_conv_value=("primary_conv_value", "sum"),
    )
    agg["actual_roas"] = agg.apply(
        lambda r: r["primary_conv_value"] / r["cost_usd"] if r["cost_usd"] > 0 else None,
        axis=1,
    )
    agg["actual_cpa"] = agg.apply(
        lambda r: r["cost_usd"] / r["primary_conv_count"]
                  if r["primary_conv_count"] > 0 else None,
        axis=1,
    )
    return agg


def query_campaigns_metadata(conn: sqlite3.Connection) -> pd.DataFrame:
    """Returns current state of the enabled campaigns for smoke tests + display."""
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    sql = f"""
        SELECT campaign_id, campaign_name, status, bidding_strategy_type,
               target_roas, daily_budget_micros
        FROM campaigns
        WHERE campaign_id IN ({placeholders})
          AND account_id = ?
    """
    df = pd.read_sql(sql, conn, params=ENABLED_CAMPAIGN_IDS + [ACCOUNT_ID])
    return df


def query_liberty_ad_groups(conn: sqlite3.Connection) -> pd.DataFrame:
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    sql = f"""
        SELECT ag.ad_group_id, ag.ad_group_name, ag.status, ag.campaign_id,
               c.campaign_name
        FROM ad_groups ag
        JOIN campaigns c ON c.campaign_id = ag.campaign_id
        WHERE ag.account_id = ?
          AND ag.campaign_id IN ({placeholders})
          AND ag.ad_group_name LIKE '%Liberty%'
    """
    return pd.read_sql(sql, conn, params=[ACCOUNT_ID] + ENABLED_CAMPAIGN_IDS)


def query_ad_group_status_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    sql = f"""
        SELECT campaign_id, status, COUNT(*) AS n
        FROM ad_groups
        WHERE account_id = ?
          AND campaign_id IN ({placeholders})
        GROUP BY campaign_id, status
    """
    return pd.read_sql(sql, conn, params=[ACCOUNT_ID] + ENABLED_CAMPAIGN_IDS)


# ═══════════════════════════════════════════════════════════════════
# SMOKE TESTS (halt on failure)
# ═══════════════════════════════════════════════════════════════════

def run_smoke_tests(
    conn: sqlite3.Connection,
    camp_meta: pd.DataFrame,
    current_targets: pd.DataFrame,
    df_daily_30d: pd.DataFrame,
    start_30d: date,
    end_date: date,
) -> None:
    """
    Runs all 10 smoke tests. Raises AssertionError with a descriptive
    message if any fail. Populates CAMPAIGN_ID_BY_NAME as a side effect.
    """
    banner("SMOKE TESTS")

    # 1. Account resolves
    row = conn.execute(
        "SELECT id, account_name, platform FROM accounts WHERE id = ?",
        (ACCOUNT_ID,),
    ).fetchone()
    assert row is not None, f"account_id = {ACCOUNT_ID} not found in accounts table"
    assert row["platform"] == "google", \
        f"Expected platform=google for account_id={ACCOUNT_ID}, got {row['platform']}"
    print(f"  ✓ Account resolves: {row['account_name']} ({row['platform']})")

    # 2. All 7 enabled campaign IDs resolve AND are ENABLED
    assert len(camp_meta) == 7, \
        f"Expected 7 campaigns from ENABLED_CAMPAIGN_IDS, DB returned {len(camp_meta)}. " \
        f"Missing IDs: {set(ENABLED_CAMPAIGN_IDS) - set(camp_meta['campaign_id'])}"
    not_enabled = camp_meta[camp_meta["status"] != "ENABLED"]
    assert not_enabled.empty, \
        f"Campaigns expected ENABLED but are not:\n{not_enabled[['campaign_name', 'status']]}"
    print(f"  ✓ All 7 campaign IDs resolve and are ENABLED")

    # Build name-to-id mapping for downstream use
    for _, r in camp_meta.iterrows():
        CAMPAIGN_ID_BY_NAME[r["campaign_name"]] = r["campaign_id"]

    # 3. Every campaign has a current target derived from the CSV
    missing_targets = current_targets[current_targets["current_target"].isna()]
    assert missing_targets.empty, \
        f"No 2026 campaign-level target change on record for:\n{missing_targets.index.tolist()}"
    print(f"  ✓ Current target derived from CSV for all 7 campaigns")

    # 4. CSV-derived current target agrees with campaigns.target_roas (DB current state)
    discrepancies = []
    for name, row in current_targets.iterrows():
        if name not in CAMPAIGN_ID_BY_NAME:
            continue
        cid = CAMPAIGN_ID_BY_NAME[name]
        db_target = camp_meta.loc[camp_meta["campaign_id"] == cid, "target_roas"].iloc[0]
        csv_target = row["current_target"]
        if db_target is None or csv_target is None:
            continue
        if abs(db_target - csv_target) > 0.005:   # half-cent tolerance
            discrepancies.append(
                f"{name}: CSV={csv_target:.4f}, DB={db_target:.4f}"
            )
    if discrepancies:
        print("  ⚠  CSV-vs-DB target disagreement (continuing — DB is the auction truth):")
        for d in discrepancies:
            print(f"     {d}")
    else:
        print(f"  ✓ CSV-derived targets match DB campaigns.target_roas")

    # 5. Liberty ad groups are all PAUSED (if any exist)
    liberty = query_liberty_ad_groups(conn)
    if not liberty.empty:
        enabled_liberty = liberty[liberty["status"] == "ENABLED"]
        assert enabled_liberty.empty, (
            f"Liberty ad groups expected PAUSED but are ENABLED:\n"
            f"{enabled_liberty[['campaign_name', 'ad_group_name', 'status']]}"
        )
        print(f"  ✓ All {len(liberty)} Liberty ad groups are PAUSED")
    else:
        print(f"  ℹ  No Liberty ad groups found under the 7 campaigns")

    # 6. target_roas_that_day coverage check (30d window)
    if df_daily_30d.empty:
        raise AssertionError(f"No daily performance rows for 30d window {start_30d}..{end_date}")
    coverage = df_daily_30d.groupby("campaign_id").agg(
        days_total=("date", "nunique"),
        days_with_target=("target_roas_that_day", lambda s: s.notna().sum()),
    )
    coverage["gap_days"] = coverage["days_total"] - coverage["days_with_target"]
    gaps = coverage[coverage["gap_days"] > 0]
    if not gaps.empty:
        print("  ⚠  target_roas_that_day has gaps (falling back to CSV for those dates):")
        print(gaps.to_string())
    else:
        print(f"  ✓ target_roas_that_day populated on every day of 30d window")

    # 7. bidding_strategy_type_that_day consistency check
    strat = df_daily_30d.dropna(subset=["bid_strategy_that_day"])
    if not strat.empty:
        bad = strat[~strat["bid_strategy_that_day"].isin(
            ["TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"]
        )]
        if not bad.empty:
            unique_bad = bad.groupby("campaign_id")["bid_strategy_that_day"].unique()
            print("  ⚠  Non-TARGET_ROAS bid strategy detected in window:")
            print(unique_bad.to_string())
        else:
            print(f"  ✓ All days use TARGET_ROAS (or MAXIMIZE_CONVERSION_VALUE) bid strategy")

    # 8. No NULL search_rank_lost_is for the window (from prompt — confirmed 100% populated)
    placeholders = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))
    sql = f"""
        SELECT campaign_id, COUNT(*) AS null_days
        FROM campaign_performance_daily
        WHERE campaign_id IN ({placeholders})
          AND date BETWEEN ? AND ?
          AND account_id = ?
          AND device     = 'ALL'
          AND search_rank_lost_is IS NULL
        GROUP BY campaign_id
    """
    params = ENABLED_CAMPAIGN_IDS + [start_30d.isoformat(), end_date.isoformat(), ACCOUNT_ID]
    null_rank = pd.read_sql(sql, conn, params=params)
    if not null_rank.empty:
        print(f"  ⚠  search_rank_lost_is has NULL rows (informational only):")
        print(null_rank.to_string())
    else:
        print(f"  ✓ search_rank_lost_is fully populated in window")

    # 9. Reasonable spend totals (sanity check: not zero, not absurd)
    spend_total = df_daily_30d["cost_usd"].sum()
    assert spend_total > 0, f"30d spend total is zero — check data pipeline"
    print(f"  ✓ 30d spend total is non-zero: ${spend_total:,.0f}")

    # 10. Every campaign has at least one row in the 30d daily frame
    missing_in_window = set(ENABLED_CAMPAIGN_IDS) - set(df_daily_30d["campaign_id"].unique())
    if missing_in_window:
        print(f"  ⚠  Campaigns with no rows in 30d window: {missing_in_window}")
    else:
        print(f"  ✓ All 7 campaigns have data in 30d window")

    print("\n  All smoke tests passed (warnings above are informational).\n")


# ═══════════════════════════════════════════════════════════════════
# RECOMMENDATION ENGINE
# ═══════════════════════════════════════════════════════════════════

def classify_recommendation(
    actual_roas_14: float | None,
    actual_roas_30: float | None,
    conv_30d: float,
    current_target: float | None,
    wow_conv_trend: float | None,
    recent_target_change: bool,
    recent_budget_change: bool,
    recent_partner_pause: bool,
    margin_pressure: bool = False,
) -> tuple[str, str]:
    """
    Returns (category, rationale).

    Precedence:
      INSUFFICIENT_DATA -> HOLD (guards) -> TIGHTEN -> TIGHTEN LITE ->
      RAISE_TO_FLOOR -> SCALE -> HOLD (fallthrough)

    When margin_pressure=True:
      - TIGHTEN trigger relaxed from 0.90 to 0.95 (absorbs TIGHTEN LITE range)
      - SCALE trigger tightened from 1.10 to 1.15
      - RAISE TO FLOOR accepts actual >= target * 0.98 (was: >= target)
      - TIGHTEN LITE does NOT fire (regular TIGHTEN covers the range)
    """
    # Resolve thresholds based on mode
    tighten_trigger = MP_TIGHTEN_TRIGGER if margin_pressure else TIGHTEN_TRIGGER
    scale_trigger   = MP_SCALE_TRIGGER   if margin_pressure else SCALE_TRIGGER
    rtf_min_pct     = MP_RTF_MIN_ACTUAL_PCT if margin_pressure else 1.0
    mode_tag        = " [margin-pressure mode]" if margin_pressure else ""

    # INSUFFICIENT DATA
    if conv_30d < MIN_CONVERSIONS_30D:
        return ("INSUFFICIENT DATA",
                f"Only {conv_30d:.0f} conversions in 30d window (<{MIN_CONVERSIONS_30D})")

    if current_target is None:
        return ("INSUFFICIENT DATA",
                "No 2026 campaign-level target change on record in CSV")

    # Guard: recent target change (let it settle)
    if recent_target_change:
        return ("HOLD",
                f"Target changed within last {RECENT_CHANGE_WINDOW_DAYS} days — letting it settle")

    # Guard: recent budget change
    if recent_budget_change:
        return ("HOLD",
                f"Budget changed within last {RECENT_CHANGE_WINDOW_DAYS} days — performance is recalibrating")

    # Guard: recent partner pause
    if recent_partner_pause:
        return ("HOLD",
                f"Partner pause recorded within last {RECENT_CHANGE_WINDOW_DAYS} days — inventory shifted")

    # Guard: both windows must have a computable ROAS
    if actual_roas_14 is None or actual_roas_30 is None:
        return ("HOLD", "Missing actual ROAS for 14d or 30d window (no revenue in window)")

    # Guard: conversion free-fall blocks TIGHTEN/TIGHTEN LITE (raising target on collapsing volume
    # causes death spiral) but does not necessarily HOLD across the board
    conv_free_fall = (wow_conv_trend is not None and wow_conv_trend < CONV_FREE_FALL_WOW)

    # MARGIN PRESSURE: evaluate RAISE TO FLOOR first with the relaxed threshold.
    # This catches near-miss below-goal campaigns (e.g. target=1.10, actual=1.08) that
    # would otherwise fall through to TIGHTEN or HOLD. In normal mode, RTF requires
    # actual >= target; in MP mode, RTF accepts actual >= target * 0.98.
    if (margin_pressure
            and current_target < RAISE_TO_FLOOR_TARGET
            and actual_roas_14 >= current_target * MP_RTF_MIN_ACTUAL_PCT
            and actual_roas_30 >= current_target * MP_RTF_MIN_ACTUAL_PCT):
        return ("RAISE TO FLOOR",
                f"Target {current_target:.2f} below 1.2 floor, campaign within {1 - MP_RTF_MIN_ACTUAL_PCT:.0%} "
                f"of target on both windows — pull toward goal [margin-pressure mode]")

    # TIGHTEN: actual < target * tighten_trigger on BOTH windows, not in free-fall
    tighten_condition = (
        actual_roas_14 < current_target * tighten_trigger
        and actual_roas_30 < current_target * tighten_trigger
    )
    if tighten_condition and not conv_free_fall:
        note = (f"14d ROAS {actual_roas_14:.2f} and 30d ROAS {actual_roas_30:.2f} "
                f"both below target × {tighten_trigger:.2f} "
                f"({current_target * tighten_trigger:.2f}){mode_tag}")
        if current_target < RAISE_TO_FLOOR_TARGET:
            note += " | below 1.2 floor — target credibility issue"
        return ("TIGHTEN", note)
    if tighten_condition and conv_free_fall:
        return ("HOLD",
                f"Would TIGHTEN, but conversion volume in free-fall (WoW {wow_conv_trend:+.0%}) — not safe to raise target")

    # TIGHTEN LITE: actual in [target * 0.95, target) on both windows AND target < 1.2
    # Only fires in NORMAL mode. In margin-pressure mode, regular TIGHTEN absorbs this range.
    if (not margin_pressure
            and current_target < RAISE_TO_FLOOR_TARGET
            and actual_roas_14 >= current_target * TIGHTEN_LITE_TRIGGER
            and actual_roas_14 < current_target
            and actual_roas_30 >= current_target * TIGHTEN_LITE_TRIGGER
            and actual_roas_30 < current_target):
        if conv_free_fall:
            return ("HOLD",
                    f"Would TIGHTEN LITE, but conversion volume in free-fall (WoW {wow_conv_trend:+.0%}) — not safe to nudge target")
        return ("TIGHTEN LITE",
                f"14d ROAS {actual_roas_14:.2f} and 30d ROAS {actual_roas_30:.2f} "
                f"both in soft-miss range [target × 0.95, target); target {current_target:.2f} "
                f"below 1.2 floor — small nudge toward goal")

    # RAISE TO FLOOR: current target < 1.2 AND actual >= target * rtf_min_pct on both windows
    if (current_target < RAISE_TO_FLOOR_TARGET
            and actual_roas_14 >= current_target * rtf_min_pct
            and actual_roas_30 >= current_target * rtf_min_pct):
        pct_note = "" if not margin_pressure else f" (accepted on ≥ target × {rtf_min_pct:.2f} under margin pressure)"
        return ("RAISE TO FLOOR",
                f"Target {current_target:.2f} below 1.2 floor, campaign hitting target on both windows{pct_note} — pull toward goal{mode_tag}")

    # SCALE: actual >= target * scale_trigger on both windows AND current target already >= 1.2
    scale_condition = (
        actual_roas_14 >= current_target * scale_trigger
        and actual_roas_30 >= current_target * scale_trigger
        and current_target >= RAISE_TO_FLOOR_TARGET
    )
    if scale_condition:
        note = (f"14d ROAS {actual_roas_14:.2f} and 30d ROAS {actual_roas_30:.2f} "
                f"both ≥ target × {scale_trigger:.2f} "
                f"({current_target * scale_trigger:.2f}), "
                f"and target ≥ 1.2 floor{mode_tag}")
        # If the current target is already at/near the 1.2 floor, all SCALE
        # proposals will clamp to 1.2 — tell the user loosening is blocked
        # by the floor so the "no target change" doesn't look like a bug.
        if current_target * (1 - STEP_BASE) <= SCALE_FLOOR:
            note += " | proposals clamped at SCALE floor 1.2 — loosening blocked by margin-goal policy"
        return ("SCALE", note)

    # HOLD fallthrough
    if (current_target < RAISE_TO_FLOOR_TARGET
            and (actual_roas_14 < current_target or actual_roas_30 < current_target)):
        return ("HOLD",
                f"Target below 1.2 but window performance mixed — not a clean RAISE TO FLOOR signal{mode_tag}")
    return ("HOLD", f"No action threshold triggered — holding current target{mode_tag}")


def propose_targets(
    category: str,
    current_target: float | None,
    break_even_floor: float,
) -> dict[str, float | None]:
    """
    Returns {base, moderate, aggressive} proposed target values.
    Applies direction (up for TIGHTEN/TIGHTEN LITE/RAISE TO FLOOR, down for SCALE) and floors.
    HOLD / INSUFFICIENT DATA => all None.
    TIGHTEN LITE uses 1/2/3% steps; other action categories use 3/7/15%.
    """
    result = {"base": None, "moderate": None, "aggressive": None}

    if category in ("HOLD", "INSUFFICIENT DATA") or current_target is None:
        return result

    # Pick step table
    if category == "TIGHTEN LITE":
        step_table = [("base", STEP_BASE_LITE),
                      ("moderate", STEP_MODERATE_LITE),
                      ("aggressive", STEP_AGGRESSIVE_LITE)]
    else:
        step_table = [("base", STEP_BASE),
                      ("moderate", STEP_MODERATE),
                      ("aggressive", STEP_AGGRESSIVE)]

    for name, step in step_table:
        if category in ("TIGHTEN", "TIGHTEN LITE"):
            proposed = current_target * (1 + step)
            proposed = max(proposed, break_even_floor)
        elif category == "RAISE TO FLOOR":
            proposed = min(current_target * (1 + step), RAISE_TO_FLOOR_TARGET)
            proposed = max(proposed, break_even_floor)
        elif category == "SCALE":
            proposed = current_target * (1 - step)
            proposed = max(proposed, SCALE_FLOOR, break_even_floor)
        else:
            proposed = None
        result[name] = proposed
    return result


def scenario_guidance(
    category: str,
    flags: dict[str, bool],
) -> dict[str, str]:
    """
    Cautionary tagging per scenario based on data quality flags.
    Returns {base, moderate, aggressive} each in {advised, cautioned, not advised, n/a}.
    """
    if category in ("HOLD", "INSUFFICIENT DATA"):
        return {"base": "n/a", "moderate": "n/a", "aggressive": "n/a"}

    # Aggressive needs clean data — any flag disqualifies it
    any_flag = any(flags.values())
    aggressive = "not advised" if any_flag else "advised"

    # Moderate is advised when no significant flags; cautioned with flags
    moderate = "cautioned" if any_flag else "advised"

    # Base is almost always safe
    base = "advised"

    return {"base": base, "moderate": moderate, "aggressive": aggressive}


# ═══════════════════════════════════════════════════════════════════
# CHANGE-LOG ATTRIBUTION
# ═══════════════════════════════════════════════════════════════════

def build_change_attribution(
    target_df: pd.DataFrame,
    budget_df: pd.DataFrame,
    enabled_campaign_names: set[str],
) -> pd.DataFrame:
    """
    For every 2026 target change on the 7 enabled campaigns, compute a
    confoundedness score: was there another target or budget change within
    ±7 days on the same campaign?

    Returns one row per target change with columns:
        timestamp, campaign_name, scope, old_target, new_target,
        direction, confounders_within_7d, clean
    """
    tgt = target_df[target_df["campaign_name"].isin(enabled_campaign_names)].copy()
    bud = budget_df[budget_df["campaign_name"].isin(enabled_campaign_names)].copy()

    rows = []
    for _, tr in tgt.iterrows():
        ts = tr["timestamp"]
        name = tr["campaign_name"]
        window_start = ts - timedelta(days=7)
        window_end   = ts + timedelta(days=7)

        # Other target changes (same campaign, not this row itself)
        same_camp_tgt = tgt[tgt["campaign_name"] == name]
        other_tgt = same_camp_tgt[
            (same_camp_tgt["timestamp"] >= window_start)
            & (same_camp_tgt["timestamp"] <= window_end)
            & (same_camp_tgt["timestamp"] != ts)
        ]
        # Budget changes same campaign
        same_camp_bud = bud[bud["campaign_name"] == name]
        other_bud = same_camp_bud[
            (same_camp_bud["timestamp"] >= window_start)
            & (same_camp_bud["timestamp"] <= window_end)
        ]

        confounders = len(other_tgt) + len(other_bud)
        direction = None
        if tr["old_target"] is not None and tr["new_target"] is not None:
            if tr["new_target"] > tr["old_target"]:
                direction = "tighten"
            elif tr["new_target"] < tr["old_target"]:
                direction = "loosen"
            else:
                direction = "flat"

        rows.append({
            "timestamp": ts,
            "campaign_name": name,
            "scope": tr["scope"],
            "old_target": tr["old_target"],
            "new_target": tr["new_target"],
            "direction": direction,
            "confounders_within_7d": confounders,
            "clean": confounders == 0,
        })

    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def build_aggregate_sanity_check(
    attribution_df: pd.DataFrame,
    df_all_daily: pd.DataFrame,
) -> pd.DataFrame:
    """
    Across the clean target changes, did loosens (target down) lift spend
    over the following 14 days, and did tightens (target up) reduce spend?

    Returns one summary row per direction with pre/post spend and conv volume.
    """
    clean = attribution_df[attribution_df["clean"]].copy()
    clean = clean[clean["scope"] == "CAMPAIGN"]
    clean = clean.dropna(subset=["direction"])
    clean = clean[clean["direction"].isin(["tighten", "loosen"])]

    rows = []
    for _, ev in clean.iterrows():
        name = ev["campaign_name"]
        ts = ev["timestamp"].date()
        cid = CAMPAIGN_ID_BY_NAME.get(name)
        if not cid:
            continue

        pre_start = ts - timedelta(days=14)
        pre_end   = ts - timedelta(days=1)
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

        pre_spend  = sub_pre["cost_usd"].sum()
        post_spend = sub_post["cost_usd"].sum()
        pre_conv   = sub_pre["primary_conv_count"].sum()
        post_conv  = sub_post["primary_conv_count"].sum()

        rows.append({
            "timestamp": ev["timestamp"],
            "campaign_name": name,
            "direction": ev["direction"],
            "old_target": ev["old_target"],
            "new_target": ev["new_target"],
            "pre_spend": pre_spend,
            "post_spend": post_spend,
            "spend_delta_pct": (post_spend - pre_spend) / pre_spend if pre_spend > 0 else None,
            "pre_conv": pre_conv,
            "post_conv": post_conv,
            "conv_delta_pct": (post_conv - pre_conv) / pre_conv if pre_conv > 0 else None,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Also compute direction-level aggregates
    agg_rows = []
    for direction in ["loosen", "tighten"]:
        sub = df[df["direction"] == direction]
        if sub.empty:
            continue
        avg_spend_delta = sub["spend_delta_pct"].dropna().mean()
        avg_conv_delta  = sub["conv_delta_pct"].dropna().mean()
        expected = "+" if direction == "loosen" else "-"
        actual = "+" if (avg_spend_delta or 0) >= 0 else "-"
        agg_rows.append({
            "timestamp": None,
            "campaign_name": f"[AGGREGATE — {direction.upper()}]",
            "direction": direction,
            "old_target": None,
            "new_target": None,
            "pre_spend": None,
            "post_spend": None,
            "spend_delta_pct": avg_spend_delta,
            "pre_conv": None,
            "post_conv": None,
            "conv_delta_pct": avg_conv_delta,
        })

    return pd.concat([df, pd.DataFrame(agg_rows)], ignore_index=True)


# ═══════════════════════════════════════════════════════════════════
# DIAGNOSTIC PRINT (terminal)
# ═══════════════════════════════════════════════════════════════════

def print_diagnostic(
    camp_meta: pd.DataFrame,
    current_targets: pd.DataFrame,
    agg_14d: pd.DataFrame,
    agg_30d: pd.DataFrame,
    windows: dict[int, tuple[date, date]],
    raw_max: date,
    stable_end: date,
    blended_roas_7d: float | None = None,
    blended_roas_14d: float | None = None,
    blended_roas_30d: float | None = None,
    margin_pressure: bool = False,
) -> None:
    banner("DIAGNOSTIC (terminal, pre-workbook)")
    print(f"  Raw MAX(date) in DB: {raw_max}")
    print(f"  Stable end date (used for windows): {stable_end} "
          f"(= raw_max - {BACKFILL_BUFFER_DAYS}d buffer)")
    for d, (s, e) in windows.items():
        print(f"  {d}d window: {s} to {e} ({(e - s).days + 1} days inclusive)")

    # Aggregate banner mirrors what lands in the workbook header block
    print(f"\n  Aggregate blended ROAS (gap to {GOAL_ROAS:.2f} goal):")
    for label, val in [(" 7d", blended_roas_7d),
                       ("14d", blended_roas_14d),
                       ("30d", blended_roas_30d)]:
        if val is not None:
            gap = val - GOAL_ROAS
            print(f"    {label}: {val:.4f}   gap: {gap:+.4f}")
        else:
            print(f"    {label}: n/a")

    mode = "MARGIN PRESSURE (tightened thresholds)" if margin_pressure else "NORMAL"
    print(f"\n  Mode: {mode}")

    print(f"\n  Per-campaign totals (30d window):")
    merge = camp_meta.merge(agg_30d, on="campaign_id", how="left")
    for _, r in merge.iterrows():
        name = r["campaign_name"]
        t_row = current_targets.loc[name] if name in current_targets.index else None
        current_t = t_row["current_target"] if t_row is not None else None
        current_t_str = f"{current_t:.3f}" if current_t else "n/a"
        spend = r["cost_usd"] if pd.notna(r["cost_usd"]) else 0
        conv  = r["primary_conv_count"] if pd.notna(r["primary_conv_count"]) else 0
        rev   = r["primary_conv_value"] if pd.notna(r["primary_conv_value"]) else 0
        roas  = r["actual_roas"]
        roas_str = f"{roas:.3f}" if pd.notna(roas) else "n/a"
        print(f"    {name[:50]:50s}  target={current_t_str}  "
              f"spend=${spend:>9,.0f}  conv={conv:>6.1f}  rev=${rev:>9,.0f}  "
              f"ROAS={roas_str}")


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
            # Convert pandas NaN / NaT / timestamps to friendly forms
            if pd.isna(value):
                value = None
            elif isinstance(value, pd.Timestamp):
                value = value.to_pydatetime()
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = body_font

    for col_idx, col_name in enumerate(df.columns, 1):
        if len(df) > 0:
            max_data_len = df.iloc[:, col_idx - 1].astype(str).str.len().max()
        else:
            max_data_len = 0
        width = min(max(len(str(col_name)), int(max_data_len)) + 2, 50)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    if freeze_row:
        ws.freeze_panes = ws.cell(row=freeze_row + 1, column=1)


def color_recommendation_column(ws, df: pd.DataFrame, col_name: str,
                                header_row: int = 1) -> None:
    """Color the recommendation cells. header_row is the 1-indexed row
    containing the column headers; data begins at header_row + 1."""
    if col_name not in df.columns:
        return
    col_idx = list(df.columns).index(col_name) + 1
    color_map = {
        "SCALE":             GREEN_FILL,
        "RAISE TO FLOOR":    BLUE_FILL,
        "HOLD":              YELLOW_FILL,
        "TIGHTEN":           RED_FILL,
        "TIGHTEN LITE":      LITE_FILL,
        "INSUFFICIENT DATA": GREY_FILL,
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
    Writes the Frontier Summary sheet with an expanded context block at the top:
        Row 1:  Run date                    | DB max date
        Row 2:  14d window                  | <start>  →  <end>
        Row 3:  30d window                  | <start>  →  <end>
        Row 4:  (blank spacer)
        Row 5:  Aggregate  7d:  blended ROAS <v>    gap to 1.20: <g>
        Row 6:  Aggregate 14d:  blended ROAS <v>    gap to 1.20: <g>
        Row 7:  Aggregate 30d:  blended ROAS <v>    gap to 1.20: <g>
        Row 8:  Mode: NORMAL | MARGIN PRESSURE
        Row 9:  (blank spacer)
        Row 10: <column headers>
        Row 11+: data rows

    run_context keys (required):
      run_date, db_max_date,
      window_14d (tuple of dates), window_30d (tuple of dates),
      blended_roas_7d, blended_roas_14d, blended_roas_30d,
      margin_pressure (bool)

    Returns the 1-indexed header row (used by color_recommendation_column).
    """
    label_font = Font(bold=True, name="Arial")
    value_font = Font(name="Arial")
    good_font  = Font(bold=True, color="006100", name="Arial")   # green for gap >= 0
    bad_font   = Font(bold=True, color="9C0006", name="Arial")   # red for gap < 0
    mode_normal_font   = Font(bold=True, name="Arial")
    mode_pressure_font = Font(bold=True, color="9C0006", name="Arial")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL)
    header_font = Font(color=HEADER_FONT, bold=True, name="Arial")
    body_font   = Font(name="Arial")

    # Row 1: Run date + DB max date
    ws.cell(row=1, column=1, value="Run date:").font = label_font
    ws.cell(row=1, column=2, value=run_context["run_date"].isoformat()).font = value_font
    ws.cell(row=1, column=4, value="DB max date:").font = label_font
    ws.cell(row=1, column=5, value=run_context["db_max_date"].isoformat()).font = value_font

    # Row 2: 14d window
    w14_start, w14_end = run_context["window_14d"]
    ws.cell(row=2, column=1, value="14d window:").font = label_font
    ws.cell(
        row=2, column=2,
        value=f"{w14_start.isoformat()}  \u2192  {w14_end.isoformat()}",
    ).font = value_font

    # Row 3: 30d window
    w30_start, w30_end = run_context["window_30d"]
    ws.cell(row=3, column=1, value="30d window:").font = label_font
    ws.cell(
        row=3, column=2,
        value=f"{w30_start.isoformat()}  \u2192  {w30_end.isoformat()}",
    ).font = value_font

    # Row 4: blank spacer

    # Rows 5-7: Aggregate blended ROAS + gap to goal for 7d / 14d / 30d
    for row_offset, (label, blended) in enumerate([
        ("Aggregate  7d:", run_context.get("blended_roas_7d")),
        ("Aggregate 14d:", run_context.get("blended_roas_14d")),
        ("Aggregate 30d:", run_context.get("blended_roas_30d")),
    ]):
        r = 5 + row_offset
        ws.cell(row=r, column=1, value=label).font = label_font
        if blended is not None:
            gap = blended - GOAL_ROAS
            ws.cell(
                row=r, column=2,
                value=f"blended ROAS {blended:.4f}",
            ).font = value_font
            ws.cell(row=r, column=4, value=f"gap to {GOAL_ROAS:.2f}:").font = label_font
            gap_cell = ws.cell(row=r, column=5, value=f"{gap:+.4f}")
            gap_cell.font = good_font if gap >= 0 else bad_font
        else:
            ws.cell(row=r, column=2, value="blended ROAS: n/a").font = value_font

    # Row 8: mode indicator
    ws.cell(row=8, column=1, value="Mode:").font = label_font
    if run_context.get("margin_pressure"):
        mode_cell = ws.cell(row=8, column=2, value="MARGIN PRESSURE (tightened thresholds)")
        mode_cell.font = mode_pressure_font
    else:
        mode_cell = ws.cell(row=8, column=2,
                            value="NORMAL  (run with --margin-pressure for tightened thresholds)")
        mode_cell.font = mode_normal_font

    # Row 9: blank spacer; header on row 10
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

    # Column widths (same logic as write_sheet)
    for col_idx, col_name in enumerate(df.columns, 1):
        if len(df) > 0:
            max_data_len = df.iloc[:, col_idx - 1].astype(str).str.len().max()
        else:
            max_data_len = 0
        width = min(max(len(str(col_name)), int(max_data_len)) + 2, 50)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Freeze below the data header so the context + header stay visible on scroll
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return header_row


def write_workbook(
    output_path: Path,
    frontier_df: pd.DataFrame,
    attribution_df: pd.DataFrame,
    sanity_df: pd.DataFrame,
    diagnostic_df: pd.DataFrame,
    ad_group_inventory_df: pd.DataFrame,
    run_context: dict,
) -> None:
    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Frontier Summary"
    header_row = write_frontier_summary_sheet(ws1, frontier_df, run_context)
    color_recommendation_column(ws1, frontier_df, "recommendation", header_row=header_row)

    ws2 = wb.create_sheet("Change Log Attribution")
    write_sheet(ws2, attribution_df)

    ws3 = wb.create_sheet("Aggregate Sanity Check")
    if sanity_df.empty:
        ws3["A1"] = "No clean target changes found in 2026 for aggregate analysis."
        ws3["A1"].font = Font(italic=True, name="Arial")
    else:
        write_sheet(ws3, sanity_df)

    ws4 = wb.create_sheet("Diagnostic")
    write_sheet(ws4, diagnostic_df)

    ws5 = wb.create_sheet("Ad Group Inventory")
    write_sheet(ws5, ad_group_inventory_df)

    wb.save(output_path)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    db_path     = Path(args.db_path)
    output_dir  = Path(args.output_dir)
    targets_csv = Path(args.targets_csv)
    budgets_csv = Path(args.budgets_csv)
    pauses_csv  = Path(args.pauses_csv)
    break_even  = float(args.break_even)

    banner("EFFICIENCY FRONTIER — v1 (Google Home Insurance)")
    print(f"  DB:            {db_path}")
    print(f"  Targets CSV:   {targets_csv}")
    print(f"  Budgets CSV:   {budgets_csv}")
    print(f"  Pauses CSV:    {pauses_csv}")
    print(f"  Break-even:    {break_even:.2f}")
    print(f"  Dry run:       {args.dry_run}")

    conn = get_db_connection(db_path)

    # ── Load CSVs
    target_df = load_target_changes(targets_csv)
    budget_df = load_budget_changes(budgets_csv)
    pauses_df = ensure_pauses_csv(pauses_csv)
    print(f"\n  Loaded {len(target_df)} 2026 target changes, "
          f"{len(budget_df)} 2026 budget changes, "
          f"{len(pauses_df)} partner pauses")

    # ── Load campaign metadata + derive current targets
    camp_meta = query_campaigns_metadata(conn)
    enabled_names = set(camp_meta["campaign_name"])
    current_targets = derive_current_target_per_campaign(target_df, enabled_names)

    # ── Resolve end date + build windows
    raw_max, stable_end = resolve_end_date(conn, args.end_date)
    windows = build_windows(stable_end)

    # ── Load 30d daily frame (used for smoke tests + 30d agg)
    start_30d, end_30d = windows[30]
    start_14d, end_14d = windows[14]

    # ── Load daily frames.
    # df_daily_30d: the 30d performance window (primary recommendation input)
    # df_daily_full: extends back to YEAR_START so the Aggregate Sanity Check
    # can compute pre/post 14d windows around historical target changes from
    # January onward. Also covers the WoW 7d lookback which otherwise extends
    # one week before start_30d and would silently return NaN.
    df_daily_30d = query_campaign_daily(
        conn, ENABLED_CAMPAIGN_IDS, start_30d, end_30d
    )
    df_daily_full = query_campaign_daily(
        conn, ENABLED_CAMPAIGN_IDS, YEAR_START, end_30d
    )

    # ── Smoke tests (halt on assertion failure)
    run_smoke_tests(conn, camp_meta, current_targets, df_daily_30d, start_30d, stable_end)

    # ── Aggregate windows
    agg_30d = aggregate_window(df_daily_30d, start_30d, end_30d)
    agg_14d = aggregate_window(df_daily_30d, start_14d, end_14d)

    # WoW conv trend: compare current 7d to prior 7d.
    # Use df_daily_full so the prior 7d is never partially outside the loaded
    # frame (it would otherwise extend one week before start_30d).
    cur_start  = stable_end - timedelta(days=6)
    prev_start = cur_start - timedelta(days=7)
    prev_end   = cur_start - timedelta(days=1)
    cur_7d  = aggregate_window(df_daily_full, cur_start, stable_end)
    prev_7d = aggregate_window(df_daily_full, prev_start, prev_end)
    wow_map = {}
    for cid in ENABLED_CAMPAIGN_IDS:
        cur_conv = cur_7d.loc[cur_7d["campaign_id"] == cid, "primary_conv_count"]
        prev_conv = prev_7d.loc[prev_7d["campaign_id"] == cid, "primary_conv_count"]
        cur_v  = float(cur_conv.iloc[0]) if not cur_conv.empty else 0.0
        prev_v = float(prev_conv.iloc[0]) if not prev_conv.empty else 0.0
        wow_map[cid] = ((cur_v - prev_v) / prev_v) if prev_v > 0 else None

    # ── Build recent-change flags
    now_ts = datetime.combine(stable_end, datetime.min.time())
    recent_cutoff = now_ts - timedelta(days=RECENT_CHANGE_WINDOW_DAYS)

    recent_target_map = {}
    recent_budget_map = {}
    for name in enabled_names:
        t_recent = target_df[
            (target_df["campaign_name"] == name)
            & (target_df["scope"] == "CAMPAIGN")
            & (target_df["timestamp"] >= recent_cutoff)
        ]
        b_recent = budget_df[
            (budget_df["campaign_name"] == name)
            & (budget_df["timestamp"] >= recent_cutoff)
        ]
        recent_target_map[name] = not t_recent.empty
        recent_budget_map[name] = not b_recent.empty

    # Recent partner pauses
    recent_pause_map = {name: False for name in enabled_names}
    if not pauses_df.empty:
        recent_pause_cutoff = stable_end - timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
        for _, p in pauses_df.iterrows():
            name = p["campaign_name"]
            pd_ = p.get("pause_date")
            if name in recent_pause_map and pd_ is not None and pd_ >= recent_pause_cutoff:
                recent_pause_map[name] = True

    # Liberty-mixed regime: any window whose start is before or equal to today
    # and overlaps LIBERTY_START_DATE onward (inclusive) is flagged.
    def liberty_mixed(window_start: date, window_end: date) -> bool:
        return window_end >= LIBERTY_START_DATE

    # Precompute aggregate totals across all 7 campaigns (used for banner + contribution column)
    def _blended_roas(agg_df: pd.DataFrame) -> tuple[float, float, float | None]:
        spend = float(agg_df["cost_usd"].sum())
        rev   = float(agg_df["primary_conv_value"].sum())
        roas  = (rev / spend) if spend > 0 else None
        return spend, rev, roas

    agg_7d = aggregate_window(df_daily_full, cur_start, stable_end)
    spend_30d_total, rev_30d_total, blended_roas_30d = _blended_roas(agg_30d)
    spend_14d_total, rev_14d_total, blended_roas_14d = _blended_roas(agg_14d)
    spend_7d_total,  rev_7d_total,  blended_roas_7d  = _blended_roas(agg_7d)

    # ── Build Frontier Summary
    summary_rows = []
    for _, cm in camp_meta.iterrows():
        cid = cm["campaign_id"]
        name = cm["campaign_name"]
        t_row = current_targets.loc[name] if name in current_targets.index else None
        current_t = t_row["current_target"] if t_row is not None else None
        previous_t = t_row["previous_target"] if t_row is not None else None
        current_t_ts = t_row["current_target_ts"] if t_row is not None else None
        days_since_change = (
            (stable_end - current_t_ts.date()).days
            if current_t_ts is not None else None
        )

        # 7d metrics — reuse cur_7d (already computed above for WoW calculation)
        m7 = cur_7d[cur_7d["campaign_id"] == cid]
        spend_7 = float(m7["cost_usd"].iloc[0]) if not m7.empty else 0.0
        conv_7  = float(m7["primary_conv_count"].iloc[0]) if not m7.empty else 0.0
        rev_7   = float(m7["primary_conv_value"].iloc[0]) if not m7.empty else 0.0
        roas_7  = (rev_7 / spend_7) if spend_7 > 0 else None
        cpa_7   = (spend_7 / conv_7) if conv_7 > 0 else None

        # 14d metrics
        m14 = agg_14d[agg_14d["campaign_id"] == cid]
        spend_14 = float(m14["cost_usd"].iloc[0]) if not m14.empty else 0.0
        conv_14  = float(m14["primary_conv_count"].iloc[0]) if not m14.empty else 0.0
        rev_14   = float(m14["primary_conv_value"].iloc[0]) if not m14.empty else 0.0
        roas_14  = (rev_14 / spend_14) if spend_14 > 0 else None
        cpa_14   = (spend_14 / conv_14) if conv_14 > 0 else None

        # 30d metrics
        m30 = agg_30d[agg_30d["campaign_id"] == cid]
        spend_30 = float(m30["cost_usd"].iloc[0]) if not m30.empty else 0.0
        conv_30  = float(m30["primary_conv_count"].iloc[0]) if not m30.empty else 0.0
        rev_30   = float(m30["primary_conv_value"].iloc[0]) if not m30.empty else 0.0
        roas_30  = (rev_30 / spend_30) if spend_30 > 0 else None
        cpa_30   = (spend_30 / conv_30) if conv_30 > 0 else None

        # Momentum: compare 7d ROAS to 14d ROAS. ±10% threshold.
        # Annotation only — does not influence the recommendation category.
        if roas_7 is not None and roas_14 is not None and roas_14 > 0:
            delta = (roas_7 - roas_14) / roas_14
            if delta > 0.10:
                momentum_note = "accelerating"
            elif delta < -0.10:
                momentum_note = "decelerating"
            else:
                momentum_note = "steady"
        else:
            momentum_note = None

        wow = wow_map.get(cid)
        flags = {
            "recent_target_change": recent_target_map.get(name, False),
            "recent_budget_change": recent_budget_map.get(name, False),
            "recent_partner_pause": recent_pause_map.get(name, False),
            "liberty_mixed_30d":    liberty_mixed(start_30d, end_30d),
            "conv_free_fall":       wow is not None and wow < CONV_FREE_FALL_WOW,
        }

        category, rationale = classify_recommendation(
            actual_roas_14=roas_14,
            actual_roas_30=roas_30,
            conv_30d=conv_30,
            current_target=current_t,
            wow_conv_trend=wow,
            recent_target_change=flags["recent_target_change"],
            recent_budget_change=flags["recent_budget_change"],
            recent_partner_pause=flags["recent_partner_pause"],
            margin_pressure=args.margin_pressure,
        )
        proposals = propose_targets(category, current_t, break_even)
        guidance  = scenario_guidance(category, {
            "recent_target_change": flags["recent_target_change"],
            "recent_budget_change": flags["recent_budget_change"],
            "recent_partner_pause": flags["recent_partner_pause"],
            "liberty_mixed_30d":    flags["liberty_mixed_30d"],
            "conv_free_fall":       flags["conv_free_fall"],
        })

        # Contribution to aggregate goal gap: how much this campaign is
        # dragging blended ROAS away from (or pulling it toward) the 1.2 goal.
        # contribution = (campaign_ROAS - GOAL) * (campaign_spend / total_spend)
        # Sum of contributions across all campaigns == blended_ROAS - GOAL (within rounding).
        # Negative = dragging down, positive = pulling up. Snapshot of current state,
        # not a forecast of potential lift.
        if roas_30 is not None and spend_30 > 0 and spend_30d_total > 0:
            contribution_30d = (roas_30 - GOAL_ROAS) * (spend_30 / spend_30d_total)
        else:
            contribution_30d = None

        summary_rows.append({
            "campaign_name": name,
            "current_target": round(current_t, 4) if current_t else None,
            "previous_target": round(previous_t, 4) if previous_t else None,
            "days_since_target_change": days_since_change,
            "spend_7d":  round(spend_7, 2),
            "spend_14d": round(spend_14, 2),
            "spend_30d": round(spend_30, 2),
            "conv_7d":  round(conv_7, 1),
            "conv_14d": round(conv_14, 1),
            "conv_30d": round(conv_30, 1),
            "revenue_7d":  round(rev_7, 2),
            "revenue_14d": round(rev_14, 2),
            "revenue_30d": round(rev_30, 2),
            "roas_7d":  round(roas_7, 4)  if roas_7  else None,
            "roas_14d": round(roas_14, 4) if roas_14 else None,
            "roas_30d": round(roas_30, 4) if roas_30 else None,
            "contribution_to_goal_gap_30d": round(contribution_30d, 4) if contribution_30d is not None else None,
            "momentum_note": momentum_note,
            "cpa_7d":  round(cpa_7, 2)  if cpa_7  else None,
            "cpa_14d": round(cpa_14, 2) if cpa_14 else None,
            "cpa_30d": round(cpa_30, 2) if cpa_30 else None,
            "wow_conv_trend": round(wow, 4) if wow is not None else None,
            "recommendation": category,
            "rationale": rationale,
            "proposed_base":       round(proposals["base"], 4)       if proposals["base"]       else None,
            "proposed_moderate":   round(proposals["moderate"], 4)   if proposals["moderate"]   else None,
            "proposed_aggressive": round(proposals["aggressive"], 4) if proposals["aggressive"] else None,
            "guidance_base":       guidance["base"],
            "guidance_moderate":   guidance["moderate"],
            "guidance_aggressive": guidance["aggressive"],
            "flag_recent_target_change": flags["recent_target_change"],
            "flag_recent_budget_change": flags["recent_budget_change"],
            "flag_recent_partner_pause": flags["recent_partner_pause"],
            "flag_liberty_mixed":        flags["liberty_mixed_30d"],
            "flag_conv_free_fall":       flags["conv_free_fall"],
        })
    frontier_df = pd.DataFrame(summary_rows)

    # ── Change log attribution
    attribution_df = build_change_attribution(target_df, budget_df, enabled_names)

    # ── Aggregate sanity check
    # Use the full-year frame so pre/post windows around historical target
    # changes (which can date from January onward) are fully covered.
    sanity_df = build_aggregate_sanity_check(attribution_df, df_daily_full)

    # ── Diagnostic sheet data
    diag_rows = []
    for window_days, (s, e) in windows.items():
        agg = aggregate_window(df_daily_30d, s, e)
        total = agg.agg({
            "impressions": "sum", "clicks": "sum", "cost_usd": "sum",
            "primary_conv_count": "sum", "primary_conv_value": "sum",
        })
        diag_rows.append({
            "window_days": window_days,
            "window_start": s,
            "window_end": e,
            "impressions": int(total["impressions"]),
            "clicks": int(total["clicks"]),
            "cost_usd": round(float(total["cost_usd"]), 2),
            "primary_conv_count": round(float(total["primary_conv_count"]), 1),
            "primary_conv_value": round(float(total["primary_conv_value"]), 2),
            "blended_roas": (
                round(float(total["primary_conv_value"]) / float(total["cost_usd"]), 4)
                if total["cost_usd"] > 0 else None
            ),
        })
    diagnostic_df = pd.DataFrame(diag_rows)

    # ── Ad group inventory
    ag_counts = query_ad_group_status_counts(conn)
    ag_pivot = ag_counts.pivot(index="campaign_id", columns="status", values="n").fillna(0).astype(int).reset_index()
    ag_pivot = ag_pivot.merge(
        camp_meta[["campaign_id", "campaign_name"]], on="campaign_id", how="left"
    )
    cols_front = ["campaign_name", "campaign_id"]
    other_cols = [c for c in ag_pivot.columns if c not in cols_front]
    ag_pivot = ag_pivot[cols_front + other_cols]

    # ── Terminal diagnostic print
    print_diagnostic(camp_meta, current_targets, agg_14d, agg_30d,
                     windows, raw_max, stable_end,
                     blended_roas_7d=blended_roas_7d,
                     blended_roas_14d=blended_roas_14d,
                     blended_roas_30d=blended_roas_30d,
                     margin_pressure=args.margin_pressure)

    # ── Write workbook (or not, if dry run)
    if args.dry_run:
        banner("DRY RUN COMPLETE — workbook not written")
        print(f"  Would have written {len(frontier_df)} Frontier Summary rows, "
              f"{len(attribution_df)} attribution rows, "
              f"{len(sanity_df)} sanity rows.")
        print("\n  Frontier Summary preview (key columns):")
        cols = ["campaign_name", "current_target", "roas_7d", "roas_14d", "roas_30d",
                "contribution_to_goal_gap_30d", "momentum_note", "recommendation",
                "proposed_base", "proposed_moderate", "proposed_aggressive"]
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(frontier_df[cols].to_string(index=False))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"efficiency_frontier_{args.account}_{stable_end.isoformat()}.xlsx"
        run_context = {
            "run_date":         date.today(),
            "db_max_date":      raw_max,
            "window_14d":       windows[14],
            "window_30d":       windows[30],
            "blended_roas_7d":  blended_roas_7d,
            "blended_roas_14d": blended_roas_14d,
            "blended_roas_30d": blended_roas_30d,
            "margin_pressure":  args.margin_pressure,
        }
        write_workbook(output_path, frontier_df, attribution_df, sanity_df,
                       diagnostic_df, ag_pivot, run_context)
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
