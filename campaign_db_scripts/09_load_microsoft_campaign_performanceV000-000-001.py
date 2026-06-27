"""
09_load_microsoft_campaign_performance.py
==========================================
Campaign Performance — Microsoft Ads (CSV-based)
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Loads Microsoft daily campaign performance into:

  campaign_performance_daily
      - search-network rows only (Microsoft sites and select traffic,
        Bing and web, AOL search) with IS metrics populated
      - one device='ALL' rollup per campaign x date, summed across the
        three search networks and three device types, mirroring the
        Google sync convention

  campaign_performance_network_daily
      - non-search network rows (Audience, Syndicated search partners)
      - one device='ALL' rollup per campaign x date x network
      - IS rows are audience-specific (audience_impression_share etc.),
        NULL when Microsoft does not report them

INPUT CSV
    Microsoft Reports > Campaign Daily Performance export with
    Date, Device type, and Network segmentation enabled.

    Required columns (exact header strings):
        Account name, Account number, Campaign name, Campaign ID,
        Base campaign ID, Date, Device type, Network,
        Impressions, Clicks, Spend, Conversions, Revenue,
        All conversions, All revenue,
        Impression share, Impression share lost to budget,
        Impression share lost to rank, Exact match impression share,
        Audience network impression share,
        Audience network impression share lost to rank,
        Audience network impression share lost to budget

    The loader FAILS HARD if the Network column is missing. Per SOP
    all Microsoft campaign performance loads require network
    segmentation so the router can split search from audience.

NETWORK ROUTING
    search bucket     -> campaign_performance_daily:
        - "Microsoft sites and select traffic"
        - "Bing and web"
        - "AOL search"

    non-search bucket -> campaign_performance_network_daily:
        - "Audience"
        - "Syndicated search partners"  (warns; should be off per SOP)
        - anything unrecognized         (warns; routed here as default)

IMPRESSION SHARE CONVENTION
    Stored as decimal 0.0-1.0. Blank CSV cells and "--" sentinels
    become NULL. "< 10%" (below reporting threshold) also becomes NULL,
    matching the Google safe_is() convention. NULL is correct because
    AVG() skips NULLs, so cross-platform IS averages compare like
    against like.

CONVERSION AND REVENUE CONVENTION
    Microsoft Ads emits two conversion count columns and two revenue
    columns per row:

        Conversions        — UET pixel-side conversions only.
                             For this account, typically lead form
                             submits (count-only, $0 value).
        Revenue            — pixel-side revenue. ALWAYS $0 on this
                             account because leads do not carry
                             dollar values at pixel submit time.
        All conversions    — pixel + offline conversion uploads
                             combined. This is the full primary
                             conversion count for revenue analysis.
        All revenue        — offline conversion upload revenue.
                             This is where the actual dollars live.

    THIS LOADER MAPS "All *" COLUMNS TO campaign_performance_daily
    PRIMARY FIELDS — NOT the Microsoft-native "Conversions" / "Revenue"
    columns. Rationale:

        conversions       <- All conversions  (not Conversions)
        conversion_value  <- All revenue      (not Revenue)
        all_conversions   <- All conversions  (same source)
        all_conversion_value <- All revenue   (same source)

    The UI's "Revenue = $0" and the DB's loaded conversion_value are
    different numbers by design. If you see $0 revenue somewhere in
    analysis, check whether the query is reading from the UI export
    directly or from this DB — they diverge intentionally.

    This matches how 04_load_microsoft_csv.py routes conversion data at
    the keyword grain (primary_conv_value <- all_revenue). Keeping the
    two loaders aligned means campaign-level and keyword-level revenue
    totals reconcile cleanly.

    Cross-platform note: Google's conversion_value represents primary
    conversion revenue for whatever the primary action is on that
    account. Microsoft's conversion_value here represents offline-upload
    revenue. Both are the "real money" number for their respective
    platforms and are directly comparable.

    The Revenue column from the CSV is NOT stored. It is always $0 on
    this account and its loss is lossless. If a future Microsoft account
    carries pixel-side revenue and pixel/offline need to be tracked
    separately, the schema would need primary_/secondary_ columns like
    keyword_performance_daily has, and this routing would be revisited.

SPEND CONVENTION
    Microsoft reports Spend in dollars. We multiply by 1,000,000 and
    store as cost_micros INTEGER to match Google's convention.

SNAPSHOT COLUMNS
    budget_micros_that_day, bidding_strategy_type_that_day,
    bidding_strategy_id_that_day, target_cpa_micros_that_day,
    target_roas_that_day are approximated from current campaigns
    values and will be updated when change history ingestion is added.
    The loader looks these up ONCE at the start of each run and
    caches them by campaign_id. This bakes the approximation into the
    date of the load rather than letting it drift at query time.

device='ALL' ROLLUP
    For BOTH tables, the ALL row is a computed sum of MOBILE + DESKTOP
    + TABLET for that grouping key (campaign+date for search,
    campaign+date+network for non-search). Volume metrics are summed.
    IS fields on the ALL row take the first non-NULL value from the
    device rows (IS is identical across devices in Microsoft's data).
    This mirrors the Google sync pattern in 03_sync_campaign_performance.py.

IDEMPOTENCY
    INSERT OR REPLACE on primary keys:
        (campaign_id, date, device)          for search
        (campaign_id, date, device, network) for non-search
    Safe to re-run with overlapping date ranges; latest load wins.

Usage:
    conda activate paid-search

    # Single file
    python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\
        --file "input/microsoft-home-camp-perf-20260419.csv"

    # Backfill: load every matching file in INPUT_DIR in date order
    python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\
        --backfill

    # Load just the most recent file
    python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\
        --latest

    # Dry run — parse and validate, no writes
    python campaign_db_scripts/09_load_microsoft_campaign_performance.py \\
        --file ... --dry-run
"""

import sqlite3
import sys
import argparse
import logging
import re
from pathlib import Path
from datetime import date, datetime, timezone
from collections import defaultdict
import pandas as pd


# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TODAY   = date.today().isoformat()

# Microsoft account details are now resolved dynamically from the
# refresh_helper registry. Pass --account home (default) or --account
# auto on the command line. MS_PLATFORM remains a constant since
# every Microsoft account uses platform='microsoft' in the accounts table.
MS_PLATFORM = "microsoft"

sys.path.insert(0, str(Path(__file__).parent))
from refresh_helper import ACCOUNT_REGISTRY, DEFAULT_ACCOUNT_KEY, account_label

# Input directory for backfill / --latest
INPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\input\microsoft-camp-perf")

# Filename pattern: microsoft-{vertical}-camp-perf-{YYYYMMDD}.csv
FILENAME_RE = re.compile(
    r"microsoft-(\w+)-camp-perf-(\d{8})\.csv$", re.IGNORECASE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Network routing ───────────────────────────────────────────────────────────
# Change this set when Microsoft renames networks. Anything not in
# SEARCH_NETWORKS is routed to campaign_performance_network_daily.

SEARCH_NETWORKS = {
    "Microsoft sites and select traffic",
    "Bing and web",
    "AOL search",
}

# Networks we recognize but route to the non-search table. Rows with
# network strings outside both sets trigger a warning but still load
# (into the non-search table) so data isn't silently dropped.
KNOWN_NON_SEARCH_NETWORKS = {
    "Audience",
    "Syndicated search partners",
}


# ── Device normalization (shared with keyword loader) ─────────────────────────

DEVICE_MAP = {
    "Computer":   "DESKTOP",
    "Smartphone": "MOBILE",
    "Tablet":     "TABLET",
}


# ── Required columns — hard-fail on missing Network ───────────────────────────

REQUIRED_COLUMNS = {
    "Account number",
    "Base campaign ID",
    "Date",
    "Device type",
    "Network",          # if this is missing we refuse to load
    "Impressions",
    "Clicks",
    "Spend",
    "Conversions",
    "Revenue",
    "All conversions",
    "All revenue",
}

# IS columns — these may be missing in older exports, loader will NULL them
IS_SEARCH_COLUMNS = {
    "Impression share",
    "Impression share lost to budget",
    "Impression share lost to rank",
    "Exact match impression share",
}
IS_AUDIENCE_COLUMNS = {
    "Audience network impression share",
    "Audience network impression share lost to rank",
    "Audience network impression share lost to budget",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_brackets(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s == "-" or s.lower() == "nan":
        return None
    return s.strip("[]")


def get_account_internal_id(cursor, account_number: str) -> int | None:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (account_number, MS_PLATFORM),
    ).fetchone()
    return row["id"] if row else None


def safe_is(val) -> float | None:
    """
    Normalize a Microsoft IS value to decimal 0.0-1.0 or NULL.

    Microsoft IS values arrive as:
        "72.45%"  -> 0.7245
        "11%"     -> 0.11
        "0.00%"   -> 0.0    (valid zero — campaign had volume but 0 share)
        "< 10%"   -> None   (below threshold; same as Google -1 sentinel)
        "--"      -> None
        ""        -> None
        None/NaN  -> None

    Returns NULL for below-threshold, matching Google's convention in
    03_sync_campaign_performance.py so AVG() is not corrupted by a
    forced numeric stand-in.
    """
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s == "--":
        return None
    # Below-threshold sentinels Microsoft may emit
    if s.startswith("<"):
        return None
    # Strip trailing percent
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        f = float(s)
    except ValueError:
        return None
    # Microsoft reports as percent (e.g. 72.45 meaning 72.45%). Convert
    # to 0.0-1.0 decimal by dividing by 100. Clamp negatives to None —
    # Microsoft shouldn't emit negatives but defensively treat as sentinel.
    if f < 0:
        return None
    return f / 100.0


# ── File discovery ────────────────────────────────────────────────────────────

def find_all_csvs(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    candidates = []
    for fp in input_dir.glob("microsoft-*-camp-perf-*.csv"):
        m = FILENAME_RE.search(fp.name)
        if m:
            candidates.append((m.group(2), fp))
    candidates.sort(key=lambda x: x[0])
    return [fp for _, fp in candidates]


def find_latest_csv(input_dir: Path) -> Path | None:
    allf = find_all_csvs(input_dir)
    return allf[-1] if allf else None


# ── CSV reader ────────────────────────────────────────────────────────────────

def read_performance_csv(filepath: Path) -> pd.DataFrame:
    """
    Reads a Microsoft Campaign Daily Performance CSV export. Handles
    the standard Microsoft metadata header, finds the real column
    header by scanning for 'Account name', and strips the Total
    footer and copyright line.
    """
    if not filepath.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    with open(filepath, encoding="utf-8-sig") as f:
        lines = f.readlines()

    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip('\ufeff').lstrip()
        if stripped.startswith('"Account name"') or stripped.startswith("Account name"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            f"Could not find 'Account name' header row in {filepath.name}."
        )

    header_line = lines[header_idx]
    sep = "\t" if header_line.count("\t") > header_line.count(",") else ","

    df = pd.read_csv(
        filepath,
        skiprows=header_idx,
        sep=sep,
        encoding="utf-8-sig",
        dtype=str,
        thousands=",",
    )

    first_col = df.columns[0]
    df = df[df[first_col].notna()]
    df = df[df[first_col] != "Total"]
    df = df[~df[first_col].astype(str).str.startswith("©")]

    for col in df.select_dtypes(include=["object","string"]).columns:
        df[col] = df[col].str.strip()

    return df


# ── CSV cleaning ──────────────────────────────────────────────────────────────

def clean_performance_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply cleaning, type conversion, and normalization to the raw
    performance DataFrame. Adds derived columns the upsert functions
    need downstream.
    """
    df = df.copy()

    # Required columns check — fail hard on missing Network
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV missing required columns: {sorted(missing)}.\n"
            f"Network segmentation must be enabled on this export.\n"
            f"Found columns: {sorted(df.columns)}"
        )

    # IDs
    df["campaign_id"] = df["Base campaign ID"].apply(strip_brackets)
    df = df[df["campaign_id"].notna()].copy()

    # Date → YYYY-MM-DD string
    df["report_date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    bad_dates = df["report_date"].isna().sum()
    if bad_dates:
        log.warning(f"  {bad_dates:,} rows with unparseable dates — dropping")
        df = df[df["report_date"].notna()].copy()

    # Device
    df["device"] = df["Device type"].map(DEVICE_MAP).fillna("DESKTOP")

    # Network (keep as-is; routing happens in loader)
    df["network"] = df["Network"].astype(str).str.strip()

    # Numeric volume metrics
    for raw, tgt in [
        ("Impressions", "impressions"),
        ("Clicks", "clicks"),
    ]:
        df[tgt] = pd.to_numeric(df[raw], errors="coerce").fillna(0).astype(int)

    # ── Numeric conversion and revenue routing ────────────────────────
    # IMPORTANT: we map "All conversions" / "All revenue" (offline-
    # inclusive) to the DB's primary conversion columns, NOT the
    # UI-native "Conversions" / "Revenue" columns. See the docstring
    # section "CONVERSION AND REVENUE CONVENTION" for full rationale.
    #
    # Short version: on this account "Revenue" is always $0 because
    # leads don't carry pixel-side dollar values. Real revenue lives
    # in "All revenue" (offline upload). Loading from "Revenue" would
    # produce $0 conversion_value rows and break all ROAS analysis.
    # The keyword loader already follows this convention (04_load...);
    # we match it here so campaign-level and keyword-level revenue
    # totals reconcile.
    #
    # The "Conversions" and "Revenue" CSV columns are intentionally
    # not stored. They are near-always $0 on this account and their
    # absence from the DB is lossless. If a future account requires
    # tracking pixel vs offline separately, extend the schema rather
    # than editing this mapping.
    for raw, tgt in [
        ("Spend", "spend_dollars"),
        ("All conversions", "conversions"),        # NOT "Conversions" — see note above
        ("All revenue",     "conversion_value"),   # NOT "Revenue"     — see note above
        ("All conversions", "all_conversions"),    # same source as primary by design
        ("All revenue",     "all_conversion_value"),
    ]:
        df[tgt] = pd.to_numeric(df[raw], errors="coerce").fillna(0.0).astype(float)

    # Spend -> cost_micros
    df["cost_micros"] = (df["spend_dollars"] * 1_000_000).round().astype("int64")

    # View-through conversions — Microsoft doesn't emit these per network
    # in the campaign report; NULL is the honest answer.
    df["view_through_conversions"] = None

    # IS columns — normalize per safe_is semantics
    for col in IS_SEARCH_COLUMNS | IS_AUDIENCE_COLUMNS:
        if col in df.columns:
            df[col + "__pct"] = df[col].apply(safe_is)
        else:
            df[col + "__pct"] = None

    return df


# ── Snapshot column lookup ────────────────────────────────────────────────────

def build_campaign_snapshot_cache(cursor, account_id: int) -> dict:
    """
    Read current campaigns values once at run start. These values are
    written into every *_that_day snapshot column on every performance
    row from this run. Approximation — will be replaced by change
    history ingestion later.
    """
    rows = cursor.execute("""
        SELECT campaign_id,
               daily_budget_micros,
               bidding_strategy_type,
               bidding_strategy_id,
               target_cpa_micros,
               target_roas
        FROM campaigns
        WHERE account_id = ?
    """, (account_id,)).fetchall()

    return {
        r["campaign_id"]: {
            "budget_micros":         r["daily_budget_micros"],
            "bidding_strategy_type": r["bidding_strategy_type"],
            "bidding_strategy_id":   r["bidding_strategy_id"],
            "target_cpa_micros":     r["target_cpa_micros"],
            "target_roas":           r["target_roas"],
        }
        for r in rows
    }


# ── Upsert: search-network rows (campaign_performance_daily) ──────────────────

def load_search_rows(
    conn, account_id: int,
    df_search: pd.DataFrame,
    snapshot_cache: dict,
    dry_run: bool,
) -> tuple[int, int, int, list[str]]:
    """
    Writes search-network rows to campaign_performance_daily, then
    computes and writes device='ALL' rollup rows.

    Returns (device_rows_written, all_rollup_rows_written,
             skipped_missing_campaign, warnings).
    """
    cursor = conn.cursor()
    warnings: list[str] = []
    skipped  = 0

    # Group across networks: one campaign/date/device can have rows from
    # "Microsoft sites and select traffic" AND "Bing and web" AND "AOL search".
    # All three are "search" so they sum into a single campaign_performance_daily
    # row per (campaign, date, device).
    grouped = df_search.groupby(
        ["campaign_id", "report_date", "device"], as_index=False
    ).agg(
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        cost_micros=("cost_micros", "sum"),
        conversions=("conversions", "sum"),
        conversion_value=("conversion_value", "sum"),
        all_conversions=("all_conversions", "sum"),
        all_conversion_value=("all_conversion_value", "sum"),
    )

    # IS columns: take first non-NULL across the networks (IS is campaign-level,
    # emitted on Microsoft sites rows primarily; other search networks blank).
    def first_nonnull_is(series: pd.Series):
        for v in series:
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                return v
        return None

    is_cols = [c + "__pct" for c in IS_SEARCH_COLUMNS]
    is_agg = df_search.groupby(
        ["campaign_id", "report_date", "device"], as_index=False
    )[is_cols].agg(first_nonnull_is)

    grouped = grouped.merge(
        is_agg, on=["campaign_id", "report_date", "device"], how="left"
    )

    device_rows_data: list[tuple] = []
    # Build rows and track campaign presence for FK safety
    known_campaigns: set[str] = set()
    for cid in grouped["campaign_id"].unique():
        row = cursor.execute(
            "SELECT 1 FROM campaigns WHERE campaign_id = ?", (cid,)
        ).fetchone()
        if row:
            known_campaigns.add(cid)

    for _, r in grouped.iterrows():
        cid = r["campaign_id"]
        if cid not in known_campaigns:
            skipped += 1
            continue

        snap = snapshot_cache.get(cid, {})

        # Position indexes called out explicitly — the rollup code below
        # dereferences this tuple by numeric index, so the shape must stay
        # aligned with the INSERT column order at insert_sql below AND with
        # the rollup builder. If you add/remove a field here, update both.
        #   0  campaign_id                       12 search_impression_share
        #   1  account_id                        13 search_budget_lost_is
        #   2  report_date                       14 search_rank_lost_is
        #   3  device                            15 search_exact_match_is
        #   4  impressions                       16 budget_micros_that_day
        #   5  clicks                            17 bidding_strategy_type_that_day
        #   6  cost_micros                       18 bidding_strategy_id_that_day
        #   7  conversions                       19 target_cpa_micros_that_day
        #   8  conversion_value                  20 target_roas_that_day
        #   9  view_through_conversions          21 last_synced_at
        #  10  all_conversions
        #  11  all_conversion_value
        device_rows_data.append((
            cid,                                        # 0  campaign_id
            account_id,                                 # 1  account_id
            r["report_date"],                           # 2  report_date
            r["device"],                                # 3  device
            int(r["impressions"]),                      # 4  impressions
            int(r["clicks"]),                           # 5  clicks
            int(r["cost_micros"]),                      # 6  cost_micros
            float(r["conversions"]),                    # 7  conversions
            float(r["conversion_value"]),               # 8  conversion_value
            None,                                       # 9  view_through_conversions
            float(r["all_conversions"]),                # 10 all_conversions
            float(r["all_conversion_value"]),           # 11 all_conversion_value
            r["Impression share__pct"],                 # 12 search_impression_share
            r["Impression share lost to budget__pct"],  # 13 search_budget_lost_is
            r["Impression share lost to rank__pct"],    # 14 search_rank_lost_is
            r["Exact match impression share__pct"],     # 15 search_exact_match_is
            snap.get("budget_micros"),                  # 16 budget_micros_that_day
            snap.get("bidding_strategy_type"),          # 17 bidding_strategy_type_that_day
            snap.get("bidding_strategy_id"),            # 18 bidding_strategy_id_that_day
            snap.get("target_cpa_micros"),              # 19 target_cpa_micros_that_day
            snap.get("target_roas"),                    # 20 target_roas_that_day
            now_utc(),                                  # 21 last_synced_at
        ))

    # ── Compute device='ALL' rollup ────────────────────────────────────
    # Keyed by (campaign_id, account_id, date) — positions 0, 1, 2 of the
    # device_rows_data tuple above.
    rollup_buckets: dict[tuple, list[tuple]] = defaultdict(list)
    for row in device_rows_data:
        key = (row[0], row[1], row[2])   # campaign_id, account_id, date
        rollup_buckets[key].append(row)

    all_rows_data: list[tuple] = []
    for (cid, acct, date_val), device_rows in rollup_buckets.items():
        impressions      = sum(r[4]  for r in device_rows)
        clicks           = sum(r[5]  for r in device_rows)
        cost_micros      = sum(r[6]  for r in device_rows)
        conversions      = sum(r[7]  for r in device_rows)
        conversion_value = sum(r[8]  for r in device_rows)
        all_conversions  = sum(r[10] for r in device_rows)
        all_conv_value   = sum(r[11] for r in device_rows)

        def first_nonnull(idx):
            for r in device_rows:
                v = r[idx]
                if v is not None:
                    return v
            return None

        snap = snapshot_cache.get(cid, {})

        all_rows_data.append((
            cid, acct, date_val, "ALL",
            impressions, clicks, cost_micros,
            conversions, conversion_value, None,
            all_conversions, all_conv_value,
            first_nonnull(12),   # 12 search_impression_share
            first_nonnull(13),   # 13 search_budget_lost_is
            first_nonnull(14),   # 14 search_rank_lost_is
            first_nonnull(15),   # 15 search_exact_match_is
            snap.get("budget_micros"),
            snap.get("bidding_strategy_type"),
            snap.get("bidding_strategy_id"),
            snap.get("target_cpa_micros"),
            snap.get("target_roas"),
            now_utc(),
        ))

    insert_sql = """
        INSERT OR REPLACE INTO campaign_performance_daily (
            campaign_id, account_id, date, device,
            impressions, clicks, cost_micros,
            conversions, conversion_value, view_through_conversions,
            all_conversions, all_conversion_value,
            search_impression_share, search_budget_lost_is,
            search_rank_lost_is, search_exact_match_is,
            budget_micros_that_day, bidding_strategy_type_that_day,
            bidding_strategy_id_that_day, target_cpa_micros_that_day,
            target_roas_that_day, last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    if not dry_run:
        cursor.executemany(insert_sql, device_rows_data)
        cursor.executemany(insert_sql, all_rows_data)
        conn.commit()

    return len(device_rows_data), len(all_rows_data), skipped, warnings


# ── Upsert: non-search rows (campaign_performance_network_daily) ──────────────

def load_network_rows(
    conn, account_id: int,
    df_nonsearch: pd.DataFrame,
    snapshot_cache: dict,
    dry_run: bool,
) -> tuple[int, int, int]:
    """
    Writes non-search rows to campaign_performance_network_daily, with
    a device='ALL' rollup per (campaign, date, network).

    Returns (device_rows_written, all_rollup_rows_written, skipped).
    """
    cursor = conn.cursor()
    skipped = 0

    if df_nonsearch.empty:
        return 0, 0, 0

    # Group by campaign+date+device+network
    grouped = df_nonsearch.groupby(
        ["campaign_id", "report_date", "device", "network"], as_index=False
    ).agg(
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        cost_micros=("cost_micros", "sum"),
        conversions=("conversions", "sum"),
        conversion_value=("conversion_value", "sum"),
        all_conversions=("all_conversions", "sum"),
        all_conversion_value=("all_conversion_value", "sum"),
    )

    def first_nonnull_is(series: pd.Series):
        for v in series:
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                return v
        return None

    is_cols = [c + "__pct" for c in IS_AUDIENCE_COLUMNS]
    is_agg = df_nonsearch.groupby(
        ["campaign_id", "report_date", "device", "network"], as_index=False
    )[is_cols].agg(first_nonnull_is)

    grouped = grouped.merge(
        is_agg, on=["campaign_id", "report_date", "device", "network"], how="left"
    )

    device_rows_data: list[tuple] = []

    known_campaigns: set[str] = set()
    for cid in grouped["campaign_id"].unique():
        row = cursor.execute(
            "SELECT 1 FROM campaigns WHERE campaign_id = ?", (cid,)
        ).fetchone()
        if row:
            known_campaigns.add(cid)

    for _, r in grouped.iterrows():
        cid = r["campaign_id"]
        if cid not in known_campaigns:
            skipped += 1
            continue
        snap = snapshot_cache.get(cid, {})
        # Position indexes — NOTE this tuple differs from the search tuple:
        # `network` sits at position 4, pushing metrics one slot down.
        # Rollup code below dereferences by numeric index; keep aligned
        # with INSERT column order at insert_sql below.
        #   0  campaign_id                       13 audience_impression_share
        #   1  account_id                        14 audience_rank_lost_is
        #   2  report_date                       15 audience_budget_lost_is
        #   3  device                            16 budget_micros_that_day
        #   4  network                           17 bidding_strategy_type_that_day
        #   5  impressions                       18 bidding_strategy_id_that_day
        #   6  clicks                            19 target_cpa_micros_that_day
        #   7  cost_micros                       20 target_roas_that_day
        #   8  conversions                       21 last_synced_at
        #   9  conversion_value
        #  10  view_through_conversions
        #  11  all_conversions
        #  12  all_conversion_value
        device_rows_data.append((
            cid,                                                         # 0  campaign_id
            account_id,                                                  # 1  account_id
            r["report_date"],                                            # 2  report_date
            r["device"],                                                 # 3  device
            r["network"],                                                # 4  network
            int(r["impressions"]),                                       # 5  impressions
            int(r["clicks"]),                                            # 6  clicks
            int(r["cost_micros"]),                                       # 7  cost_micros
            float(r["conversions"]),                                     # 8  conversions
            float(r["conversion_value"]),                                # 9  conversion_value
            None,                                                        # 10 view_through_conversions
            float(r["all_conversions"]),                                 # 11 all_conversions
            float(r["all_conversion_value"]),                            # 12 all_conversion_value
            r["Audience network impression share__pct"],                 # 13 audience_impression_share
            r["Audience network impression share lost to rank__pct"],    # 14 audience_rank_lost_is
            r["Audience network impression share lost to budget__pct"],  # 15 audience_budget_lost_is
            snap.get("budget_micros"),                                   # 16 budget_micros_that_day
            snap.get("bidding_strategy_type"),                           # 17 bidding_strategy_type_that_day
            snap.get("bidding_strategy_id"),                             # 18 bidding_strategy_id_that_day
            snap.get("target_cpa_micros"),                               # 19 target_cpa_micros_that_day
            snap.get("target_roas"),                                     # 20 target_roas_that_day
            now_utc(),                                                   # 21 last_synced_at
        ))

    # Rollup per (campaign, account, date, network) across devices.
    # Keyed on positions 0, 1, 2, 4 — skips 3 (device) since we sum over it.
    rollup_buckets: dict[tuple, list[tuple]] = defaultdict(list)
    for row in device_rows_data:
        key = (row[0], row[1], row[2], row[4])   # campaign_id, account_id, date, network
        rollup_buckets[key].append(row)

    all_rows_data: list[tuple] = []
    for (cid, acct, date_val, network), device_rows in rollup_buckets.items():
        # Volume metrics sum from positions 5-12 (see tuple comment above).
        impressions      = sum(r[5]  for r in device_rows)   # 5  impressions
        clicks           = sum(r[6]  for r in device_rows)   # 6  clicks
        cost_micros      = sum(r[7]  for r in device_rows)   # 7  cost_micros
        conversions      = sum(r[8]  for r in device_rows)   # 8  conversions
        conversion_value = sum(r[9]  for r in device_rows)   # 9  conversion_value
        all_conversions  = sum(r[11] for r in device_rows)   # 11 all_conversions
        all_conv_value   = sum(r[12] for r in device_rows)   # 12 all_conversion_value

        def first_nonnull(idx):
            for r in device_rows:
                if r[idx] is not None:
                    return r[idx]
            return None

        snap = snapshot_cache.get(cid, {})
        all_rows_data.append((
            cid, acct, date_val, "ALL", network,
            impressions, clicks, cost_micros,
            conversions, conversion_value, None,
            all_conversions, all_conv_value,
            first_nonnull(13),   # 13 audience_impression_share
            first_nonnull(14),   # 14 audience_rank_lost_is
            first_nonnull(15),   # 15 audience_budget_lost_is
            snap.get("budget_micros"),
            snap.get("bidding_strategy_type"),
            snap.get("bidding_strategy_id"),
            snap.get("target_cpa_micros"),
            snap.get("target_roas"),
            now_utc(),
        ))

    insert_sql = """
        INSERT OR REPLACE INTO campaign_performance_network_daily (
            campaign_id, account_id, date, device, network,
            impressions, clicks, cost_micros,
            conversions, conversion_value, view_through_conversions,
            all_conversions, all_conversion_value,
            audience_impression_share,
            audience_rank_lost_is,
            audience_budget_lost_is,
            budget_micros_that_day, bidding_strategy_type_that_day,
            bidding_strategy_id_that_day, target_cpa_micros_that_day,
            target_roas_that_day, last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    if not dry_run:
        cursor.executemany(insert_sql, device_rows_data)
        cursor.executemany(insert_sql, all_rows_data)
        conn.commit()

    return len(device_rows_data), len(all_rows_data), skipped


# ── Diagnostic ────────────────────────────────────────────────────────────────

def print_file_diagnostic(df: pd.DataFrame, file_label: str):
    log.info("")
    log.info(f"── CSV summary: {file_label} ─────────────────────────────────")
    log.info(f"  Rows                : {len(df):,}")
    log.info(f"  Date range          : {df['report_date'].min()} -> {df['report_date'].max()}")
    log.info(f"  Unique campaigns    : {df['campaign_id'].nunique()}")
    log.info(f"  Total spend         : ${df['spend_dollars'].sum():,.2f}")
    # Show both revenue columns side-by-side. conversion_value is
    # populated from All revenue (offline-inclusive) per the mapping
    # convention above. If Total revenue (All) is non-zero, the
    # conversion_value written to the DB will also be non-zero.
    pixel_conv = pd.to_numeric(df.get("Conversions", 0), errors="coerce").fillna(0).sum()
    pixel_rev  = pd.to_numeric(df.get("Revenue",     0), errors="coerce").fillna(0).sum()
    log.info(f"  Total conv (pixel)  : {pixel_conv:,.0f}  <- NOT loaded (Conversions col)")
    log.info(f"  Total rev   (pixel) : ${pixel_rev:,.2f}  <- NOT loaded (Revenue col, always ~$0)")
    log.info(f"  Total conv (All)    : {df['conversions'].sum():,.0f}  <- loaded as conversions")
    log.info(f"  Total rev   (All)   : ${df['conversion_value'].sum():,.2f}  <- loaded as conversion_value")
    log.info(f"  Networks present    :")
    net_counts = df["network"].value_counts()
    for net, n in net_counts.items():
        bucket = "search" if net in SEARCH_NETWORKS else "non-search"
        marker = "" if net in SEARCH_NETWORKS or net in KNOWN_NON_SEARCH_NETWORKS else "  ⚠ UNKNOWN"
        log.info(f"    {net:<45} {n:>7,} rows   [{bucket}]{marker}")


def print_db_diagnostic(conn, account_id: int):
    cursor = conn.cursor()
    log.info("")
    log.info("── DB state after load (Microsoft account_id = %d) ──────────", account_id)

    search = cursor.execute("""
        SELECT COUNT(*) AS rows,
               MIN(date) AS min_date,
               MAX(date) AS max_date,
               SUM(cost_micros)/1e6 AS spend,
               SUM(conversions)     AS conv,
               SUM(conversion_value) AS rev,
               SUM(CASE WHEN search_impression_share IS NOT NULL THEN 1 ELSE 0 END) AS is_populated
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL'
    """, (account_id,)).fetchone()

    log.info("  campaign_performance_daily (search, device='ALL')")
    if search and search["rows"]:
        log.info(f"    rows={search['rows']:,}  dates {search['min_date']}..{search['max_date']}")
        log.info(f"    spend=${search['spend']:,.2f}  "
                 f"conv={search['conv']:,.0f}  "
                 f"rev=${search['rev']:,.2f}  "
                 f"IS_populated={search['is_populated']:,}")
    else:
        log.info("    (no rows)")

    network = cursor.execute("""
        SELECT network,
               COUNT(*) AS rows,
               MIN(date) AS min_date,
               MAX(date) AS max_date,
               SUM(cost_micros)/1e6  AS spend,
               SUM(conversions)      AS conv,
               SUM(conversion_value) AS rev
        FROM campaign_performance_network_daily
        WHERE account_id = ? AND device = 'ALL'
        GROUP BY network
        ORDER BY SUM(cost_micros) DESC
    """, (account_id,)).fetchall()

    log.info("  campaign_performance_network_daily (device='ALL') by network")
    if not network:
        log.info("    (no rows)")
    else:
        for n in network:
            log.info(f"    {n['network']:<40} rows={n['rows']:>6,}  "
                     f"dates {n['min_date']}..{n['max_date']}")
            log.info(f"      spend=${n['spend']:,.2f}  "
                     f"conv={n['conv']:,.0f}  rev=${n['rev']:,.2f}")


# ── Single-file load pipeline ─────────────────────────────────────────────────

def _run_load(conn, account_id: int, account_number: str, account_label_str: str,
              csv_path: Path, dry_run: bool):
    log.info("")
    log.info("=" * 65)
    log.info(f"Loading: {csv_path.name}")
    log.info("=" * 65)

    log.info("Reading CSV...")
    df_raw = read_performance_csv(csv_path)
    log.info(f"  Raw rows after stripping header/footer: {len(df_raw):,}")

    log.info("Cleaning and validating...")
    df = clean_performance_df(df_raw)
    log.info(f"  Cleaned rows: {len(df):,}")

    # -- Account-CSV consistency check --
    # Refuse to load a CSV whose Account number column doesn't match
    # the --account being targeted. Without this, a Home CSV loaded
    # under --account auto would land Home data under the wrong
    # account_id with no clear error.
    if "Account number" in df.columns:
        csv_acct_nums = set(df["Account number"].dropna().astype(str).str.strip().unique())
        csv_acct_nums.discard("")
        if csv_acct_nums and csv_acct_nums != {account_number}:
            raise RuntimeError(
                f"CSV/account mismatch in {csv_path.name}.\n"
                f"  CSV contains Account number(s): {csv_acct_nums}\n"
                f"  Expected (per --account flag) : {{'{account_number}'}} "
                f"({account_label_str})\n"
                f"  Aborting to prevent cross-account data corruption. "
                f"Either re-export the correct CSV, or change the --account flag."
            )

    print_file_diagnostic(df, csv_path.name)

    # ── Route by network ───────────────────────────────────────────────
    unknown_networks = set(df["network"].unique()) - SEARCH_NETWORKS - KNOWN_NON_SEARCH_NETWORKS
    if unknown_networks:
        log.warning(f"  Unknown network(s) encountered: {sorted(unknown_networks)}")
        log.warning(f"  Routed to campaign_performance_network_daily as non-search.")

    df_search    = df[df["network"].isin(SEARCH_NETWORKS)].copy()
    df_nonsearch = df[~df["network"].isin(SEARCH_NETWORKS)].copy()

    log.info(f"  Search bucket     : {len(df_search):,} rows "
             f"(${df_search['spend_dollars'].sum():,.2f} spend)")
    log.info(f"  Non-search bucket : {len(df_nonsearch):,} rows "
             f"(${df_nonsearch['spend_dollars'].sum():,.2f} spend)")

    # ── Snapshot cache ─────────────────────────────────────────────────
    cursor = conn.cursor()
    snapshot_cache = build_campaign_snapshot_cache(cursor, account_id)
    log.warning("  Snapshot columns (budget_micros_that_day, bidding_strategy_*_that_day)")
    log.warning("  are approximated from current campaigns values and will be updated")
    log.warning("  when change history ingestion is added.")

    # ── Load search bucket ─────────────────────────────────────────────
    log.info("")
    log.info("── Loading search rows into campaign_performance_daily ──────")
    s_dev, s_all, s_skip, _ = load_search_rows(
        conn, account_id, df_search, snapshot_cache, dry_run
    )
    log.info(f"  device rows    : {s_dev:,}")
    log.info(f"  ALL rollup     : {s_all:,}")
    if s_skip:
        log.warning(f"  skipped_missing_campaign : {s_skip:,}")

    # ── Load non-search bucket ─────────────────────────────────────────
    log.info("")
    log.info("── Loading non-search rows into campaign_performance_network_daily ──")
    n_dev, n_all, n_skip = load_network_rows(
        conn, account_id, df_nonsearch, snapshot_cache, dry_run
    )
    log.info(f"  device rows    : {n_dev:,}")
    log.info(f"  ALL rollup     : {n_all:,}")
    if n_skip:
        log.warning(f"  skipped_missing_campaign : {n_skip:,}")

    if dry_run:
        log.info("")
        log.info("DRY RUN — nothing written.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Load Microsoft daily campaign performance CSV(s)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Path to a single CSV")
    group.add_argument("--latest", action="store_true",
                       help=f"Load most recent file in {INPUT_DIR}")
    group.add_argument("--backfill", action="store_true",
                       help=f"Load ALL matching files in {INPUT_DIR} in date order")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT_KEY,
                        choices=list(ACCOUNT_REGISTRY.keys()),
                        help=f"Microsoft account key (default: {DEFAULT_ACCOUNT_KEY}). "
                             f"Valid: {', '.join(sorted(ACCOUNT_REGISTRY))}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Resolve which Microsoft account this run targets via the registry
    account_number = ACCOUNT_REGISTRY[args.account]["platform_account_id"]
    account_label_str = account_label(args.account)

    log.info(f"Database : {DB_PATH}")
    log.info(f"Account  : {args.account}  ({account_label_str})")
    log.info(f"Mode     : {'DRY RUN' if args.dry_run else 'LIVE'}")

    # Resolve CSV paths
    if args.backfill or args.latest:
        # Distinguish "folder doesn't exist" from "folder exists but has no
        # matching files" — they have different operational meanings and
        # different fixes. Also create the folder on first run so future
        # drop-and-go workflows don't fail mysteriously.
        if not INPUT_DIR.exists():
            log.warning(f"Input folder does not exist: {INPUT_DIR}")
            log.warning("Creating it. Drop your CSV exports there and re-run.")
            INPUT_DIR.mkdir(parents=True, exist_ok=True)
            log.error(
                f"Expected filename pattern: microsoft-home-camp-perf-YYYYMMDD.csv "
                f"where YYYYMMDD is the last day of the export period."
            )
            return 1

        if args.backfill:
            csv_files = find_all_csvs(INPUT_DIR)
            if not csv_files:
                log.error(
                    f"Folder exists but no files match "
                    f"'microsoft-*-camp-perf-YYYYMMDD.csv' in {INPUT_DIR}"
                )
                return 1
            log.info(f"Backfill: {len(csv_files)} file(s) to load")
            for f in csv_files:
                log.info(f"  {f.name}")
        else:  # args.latest
            latest = find_latest_csv(INPUT_DIR)
            if latest is None:
                log.error(
                    f"Folder exists but no files match "
                    f"'microsoft-*-camp-perf-YYYYMMDD.csv' in {INPUT_DIR}"
                )
                return 1
            csv_files = [latest]
            log.info(f"Auto-selected: {latest.name}")
    else:
        csv_files = [Path(args.file)]

    # Connect & resolve account
    conn = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_internal_id(cursor, account_number)
    if account_id is None:
        log.error(f"No accounts row for platform_account_id={account_number}. "
                  f"Insert the accounts row before running this script.")
        conn.close()
        return 1
    log.info(f"accounts.id = {account_id}  (FK for {account_label_str})")

    # Verify campaign_performance_network_daily exists
    if not cursor.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='campaign_performance_network_daily'
    """).fetchone():
        log.error("Table campaign_performance_network_daily not found. "
                  "Run 01b_extend_campaign_tables.py first.")
        conn.close()
        return 1

    # Load each file
    for csv_path in csv_files:
        _run_load(conn, account_id, account_number, account_label_str, csv_path, args.dry_run)

    print_db_diagnostic(conn, account_id)

    conn.close()
    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main() or 0)
