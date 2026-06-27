"""
10_load_microsoft_geo.py — Microsoft Ads geo performance daily loader.

Reads one or more Microsoft Ads "Geographic Location Report" CSVs and upserts
into geo_performance_daily. Handles per-state per-device-per-network rows from
CSV; synthesizes device='ALL' rollups per (campaign_id, date, network, geo_state).

Usage:
    conda activate paid-search

    # Single file
    python campaign_db_scripts/10_load_microsoft_geo.py \\
        --file "input/microsoft-geo-daily/microsoft-home-geo-20260513.csv"

    # Backfill: load every matching file in INPUT_DIR in date order
    python campaign_db_scripts/10_load_microsoft_geo.py --backfill

    # Load just the most recent file
    python campaign_db_scripts/10_load_microsoft_geo.py --latest

    # Dry run — parse and validate, no writes
    python campaign_db_scripts/10_load_microsoft_geo.py --latest --dry-run

Input folder and filename convention (mirrors loader 09):
    INPUT_DIR = input/microsoft-geo-daily/
    Filename  = microsoft-{vertical}-geo-{YYYYMMDD}.csv
                e.g. microsoft-home-geo-20260513.csv
                YYYYMMDD = last day of the export period.

CSV expectations (Microsoft Ads UI -> Reports -> Geographic Location Report):
    - Report must be filtered to Location type = "Physical location" only.
    - Required columns: Account name, Account number, Campaign name, Campaign ID,
      Base campaign ID, Date, Device type, Network,
      State/province (physical location), Country/region (physical location),
      Location type, Impressions, Clicks, Spend, Conversions, Revenue,
      All conversions, All revenue.
    - CSV structure: 5 metadata rows + 1 blank row + 1 header row + data rows
      + 1 Total row + 1 blank row + 1 copyright row.

Column conventions (matches campaign_performance_daily / _network_daily):
    - campaign_id          <- CSV "Base campaign ID" (NOT "Campaign ID";
                              loader 04 anchors on Base campaign ID so that
                              experiment variants attribute to the canonical
                              campaign, not the variant Campaign ID).
    - conversions          <- CSV "Conversions"   (pixel-only; tCPA denominator)
    - conversion_value     <- CSV "All revenue"   (offline-inclusive)
    - all_conversions      <- CSV "All conversions"
    - all_conversion_value <- CSV "All revenue"   (same source as conversion_value)
    - cost_micros          <- CSV "Spend" * 1_000_000 (rounded, stored as INTEGER)

geo_state format: full English state name as emitted by Microsoft (e.g.
    "California", "Texas", "District of Columbia"). NOT USPS abbreviations.
    Blank state values (Microsoft emits ~0.5-1% of US rows without a resolved
    state — VPN/proxy traffic, carrier-NAT mobile) are coerced to the sentinel
    'UNKNOWN'. Filter with WHERE geo_state != 'UNKNOWN' for pure-state analysis.
    Distinct from the keyword table's geo_state='ALL' (which means "not
    geo-segmented"); 'UNKNOWN' means "geo-segmented but unresolved".

location_type column: 'PHYSICAL' (Microsoft's "Physical location" — where the
    user actually was) or 'INTEREST' (Microsoft's "Location of interest" — the
    geo signal the auction matched on, used when physical wasn't resolvable).
    The two row types are COMPLEMENTARY, not overlapping — each impression
    appears in exactly one. Loading both preserves reconciliation against
    campaign_performance_*_daily totals. Common analytical pattern: filter
    WHERE location_type = 'PHYSICAL' for "where are our users" questions;
    don't filter for total-spend reconciliation or aggregate reporting.

Idempotency: PRIMARY KEY (campaign_id, date, device, network, geo_state,
    location_type) plus INSERT OR REPLACE. No pre-delete needed. Multi-CSV
    loads (e.g. quarterly backfill of several files at once) are supported.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from refresh_helper import (
    ACCOUNT_REGISTRY,
    DEFAULT_ACCOUNT_KEY,
    account_label,
    get_connection,
    resolve_account_id,
)


# --- Input directory and filename convention (mirrors loader 09) ---

INPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\input\microsoft-geo-daily")

# Filename pattern: microsoft-{vertical}-geo-{YYYYMMDD}.csv
FILENAME_RE = re.compile(
    r"microsoft-(\w+)-geo-(\d{8})\.csv$", re.IGNORECASE
)



# --- CSV column name constants (verified against 2026-05-13 sample export) ---

COL_DATE         = "Date"
COL_STATE        = "State/province (physical location)"
COL_COUNTRY      = "Country/region (physical location)"
COUNTRY_US_VALUE = "United States"

EXPECTED_HEADERS = {
    "Account name", "Account number", "Campaign name", "Campaign ID",
    "Base campaign ID", "Date", "Device type", "Network",
    "State/province (physical location)",
    "Country/region (physical location)",
    "Location type",
    "Impressions", "Clicks", "Spend",
    "Conversions", "Revenue", "All conversions", "All revenue",
}

# --- Microsoft -> DB device label mapping ---

DEVICE_MAP = {
    "Computer":   "DESKTOP",
    "Smartphone": "MOBILE",
    "Tablet":     "TABLET",
}

# --- Microsoft -> DB location-type label mapping ---
# Microsoft emits two row types per impression (each impression appears in
# exactly one). Both are loaded; downstream queries filter by location_type
# when needed.

LOCATION_TYPE_MAP = {
    "Physical location":    "PHYSICAL",
    "Location of interest": "INTEREST",
}

# --- Network buckets (used elsewhere for reconciliation, kept here for validation) ---

SEARCH_NETWORKS = {"Microsoft sites and select traffic", "AOL search"}
NON_SEARCH_NETWORKS = {"Audience", "Syndicated search partners"}
ALL_KNOWN_NETWORKS = SEARCH_NETWORKS | NON_SEARCH_NETWORKS


# --- File discovery (mirrors loader 09) ---

def find_all_csvs(input_dir: Path) -> list[Path]:
    """Return all CSV files matching the geo filename pattern, sorted by
    YYYYMMDD ascending (oldest first)."""
    if not input_dir.exists():
        return []
    candidates = []
    for fp in input_dir.glob("microsoft-*-geo-*.csv"):
        m = FILENAME_RE.search(fp.name)
        if m:
            candidates.append((m.group(2), fp))
    candidates.sort(key=lambda x: x[0])
    return [fp for _, fp in candidates]


def find_latest_csv(input_dir: Path) -> Path | None:
    """Return the most recent CSV in input_dir by YYYYMMDD, or None if empty."""
    allf = find_all_csvs(input_dir)
    return allf[-1] if allf else None


def _clean_numeric(s: pd.Series) -> pd.Series:
    """Strip thousands separators before numeric coercion.

    Microsoft CSV exports format integers >= 1000 with comma separators
    (e.g. '22,630'). Calling pd.to_numeric on these strings directly returns
    NaN -- and a subsequent fillna(0) silently zeros high-volume rows. This
    helper is the canonical fix; apply to EVERY numeric column.
    """
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    )


def _validate_account_match(df: pd.DataFrame, expected_platform_account_id: str, csv_path: Path) -> None:
    """Hard-fail if CSV's Account number doesn't match the --account flag."""
    csv_accounts = set(df["Account number"].dropna().unique())
    if csv_accounts != {expected_platform_account_id}:
        raise SystemExit(
            f"FATAL: CSV {csv_path} contains Account number(s) {csv_accounts}, "
            f"but --account flag implies platform_account_id="
            f"{expected_platform_account_id!r}. Refusing to load."
        )


def _validate_headers(df: pd.DataFrame, csv_path: Path) -> None:
    """Hard-fail if any required column is missing."""
    missing = EXPECTED_HEADERS - set(df.columns)
    if missing:
        raise SystemExit(
            f"FATAL: CSV {csv_path} is missing expected columns: {missing}. "
            f"Found columns: {sorted(df.columns)}. Re-export with all required "
            f"columns enabled in the Geographic Location Report template."
        )


def _validate_location_type(df: pd.DataFrame, csv_path: Path) -> None:
    """Hard-fail if any row has an unrecognized Location type.

    We load both 'Physical location' and 'Location of interest' rows (mapped
    to PHYSICAL / INTEREST respectively). An unknown Location type value
    (e.g. Microsoft introducing 'Unknown' or renaming an existing label)
    needs human review before we silently load or drop it.
    """
    unknown = set(df["Location type"].dropna().unique()) - set(LOCATION_TYPE_MAP.keys())
    if unknown:
        raise SystemExit(
            f"FATAL: CSV {csv_path} contains unrecognized Location type(s): "
            f"{unknown}. Known values: {sorted(LOCATION_TYPE_MAP.keys())}. "
            f"Update LOCATION_TYPE_MAP in 10_load_microsoft_geo.py if these "
            f"are expected; otherwise re-export from Microsoft Ads UI."
        )


def _validate_country(df: pd.DataFrame, csv_path: Path) -> pd.DataFrame:
    """Filter out rows with an explicit non-US country value (e.g. 'Mexico').

    Rows with blank/NaN Country are KEPT and will end up under geo_state='UNKNOWN'
    downstream. Those represent Physical location impressions where Microsoft's
    IP geolocation failed to resolve to any country at all — they're real
    impressions on US-targeted campaigns, just unresolvable. Dropping them
    would lose them from the reconciliation against campaign_performance_*_daily
    totals.
    """
    country = df[COL_COUNTRY]
    is_blank = country.isna() | (country.astype(str).str.strip() == "")
    is_us = country == COUNTRY_US_VALUE
    is_known_non_us = ~is_blank & ~is_us

    non_us = df.loc[is_known_non_us]
    if len(non_us) > 0:
        countries = non_us[COL_COUNTRY].dropna().unique().tolist()
        print(
            f"WARNING: {len(non_us)} rows in {csv_path.name} have known non-US "
            f"Country values (countries: {countries}). Dropping.",
            file=sys.stderr,
        )

    blank_count = int(is_blank.sum())
    if blank_count > 0:
        print(
            f"NOTE: {blank_count} rows in {csv_path.name} have blank Country "
            f"(IP geolocation failed; treating as US-impressions-with-unresolved-state). "
            f"Will be loaded with geo_state='UNKNOWN'.",
            file=sys.stderr,
        )

    return df.loc[~is_known_non_us].copy()


def _validate_networks(df: pd.DataFrame, csv_path: Path) -> None:
    """Warn on unknown network labels."""
    csv_networks = set(df["Network"].dropna().unique())
    unknown = csv_networks - ALL_KNOWN_NETWORKS
    if unknown:
        print(
            f"WARNING: {csv_path.name} contains unknown Network value(s) "
            f"{unknown}. They will load, but reconciliation may not cover them. "
            f"Update SEARCH_NETWORKS / NON_SEARCH_NETWORKS if these are expected.",
            file=sys.stderr,
        )


def _validate_devices(df: pd.DataFrame, csv_path: Path) -> None:
    """Hard-fail on unrecognized Device type values."""
    unmapped = set(df["Device type"].dropna().unique()) - set(DEVICE_MAP.keys())
    if unmapped:
        raise SystemExit(
            f"FATAL: CSV {csv_path} contains unrecognized Device type(s): "
            f"{unmapped}. Update DEVICE_MAP in 10_load_microsoft_geo.py."
        )


def _read_and_normalize_csv(csv_path: Path, expected_platform_account_id: str, account_id: int) -> pd.DataFrame:
    """Read one Microsoft geo CSV and normalize to geo_performance_daily column layout."""
    # 5 metadata rows + 1 blank row before the header; Total + blank + copyright at the end.
    df = pd.read_csv(csv_path, dtype=str, skiprows=6, skipfooter=3, engine="python")

    # Belt-and-suspenders: drop any residual row missing a Date (e.g. Total row
    # if Microsoft changes footer structure).
    df = df.dropna(subset=[COL_DATE])

    _validate_headers(df, csv_path)
    _validate_account_match(df, expected_platform_account_id, csv_path)
    _validate_location_type(df, csv_path)
    df = _validate_country(df, csv_path)
    _validate_networks(df, csv_path)
    _validate_devices(df, csv_path)

    # Anchor on Base campaign ID (NOT Campaign ID) to match loader 04's convention.
    # During A/B experiments, Microsoft assigns a new Campaign ID to the variant
    # but preserves Base campaign ID as the canonical anchor. Performance data
    # must attribute to the canonical campaign for cross-loader consistency.
    # Strip Excel-protection brackets ("[531494232]" -> "531494232") to match
    # how campaigns.campaign_id is stored.
    canonical_campaign_id = df["Base campaign ID"].astype(str).str.strip("[]")

    # Date parsing: implicit format with errors='coerce', then explicit ISO
    # string format. Matches loader 09's pattern. Implicit format is more
    # resilient to Microsoft drifting their export date format (M/D/YYYY vs
    # YYYY-MM-DD across locales/template settings); explicit format= would
    # silently fail on drift.
    iso_date = pd.to_datetime(df[COL_DATE], errors="coerce").dt.strftime("%Y-%m-%d")
    invalid_dates = iso_date.isna().sum()
    if invalid_dates > 0:
        print(
            f"  WARNING: {invalid_dates} rows with unparseable dates in "
            f"{csv_path.name}; dropping.",
            file=sys.stderr,
        )

    spend_dollars = _clean_numeric(df["Spend"]).fillna(0)

    # Coerce blank/NA State values to the 'UNKNOWN' sentinel. Microsoft emits
    # ~0.5-1% of US rows with a blank state — IP geolocation resolved to country
    # level but not state (VPN/proxy traffic, carrier-NAT mobile pools). These
    # rows are real impressions and must be preserved for clean reconciliation
    # against the campaign tables; dropping them would cause persistent SUM
    # drift. The sentinel is distinct from the keyword table's geo_state='ALL'
    # (which means "not geo-segmented"); 'UNKNOWN' here means "geo-segmented
    # but unresolved at the state level". Filter with WHERE geo_state !=
    # 'UNKNOWN' when querying for pure-state analysis.
    geo_state_clean = df[COL_STATE].fillna("UNKNOWN").replace(r"^\s*$", "UNKNOWN", regex=True)

    out = pd.DataFrame({
        "account_id":           account_id,
        "campaign_id":          canonical_campaign_id,
        "date":                 iso_date,
        "device":               df["Device type"].map(DEVICE_MAP),
        "network":              df["Network"],
        "geo_state":            geo_state_clean,
        "location_type":        df["Location type"].map(LOCATION_TYPE_MAP),
        # NOTE: cast to plain numpy int64 (lowercase 'i'), NOT pandas Int64
        # (uppercase 'I', the nullable extension type). SQLite's adapter
        # doesn't know how to serialize the Int64 extension type and falls
        # back to storing raw little-endian bytes as BLOB. fillna(0) before
        # the cast means we don't need nullable-int semantics anyway.
        "impressions":          _clean_numeric(df["Impressions"]).fillna(0).astype("int64"),
        "clicks":               _clean_numeric(df["Clicks"]).fillna(0).astype("int64"),
        "cost_micros":          (spend_dollars * 1_000_000).round().astype("int64"),
        "conversions":          _clean_numeric(df["Conversions"]).fillna(0),
        "conversion_value":     _clean_numeric(df["All revenue"]).fillna(0),
        "all_conversions":      _clean_numeric(df["All conversions"]).fillna(0),
        "all_conversion_value": _clean_numeric(df["All revenue"]).fillna(0),
    })

    # Drop rows with unparseable dates after the DataFrame is assembled.
    out = out.dropna(subset=["date"]).copy()

    return out


def _collapse_natural_key_duplicates(per_device_df: pd.DataFrame) -> pd.DataFrame:
    """Sum rows that share the same DB natural key into a single row.

    Microsoft sometimes emits MULTIPLE per-device CSV rows that map to the
    same (account_id, campaign_id, date, device, network, geo_state,
    location_type) tuple after our normalization. Example: a Physical-location
    row with blank State and blank Country (IP geolocation failed entirely),
    plus a separate Physical-location row with blank State but Country=US
    (IP resolved to country but not state). Both normalize to
    geo_state='UNKNOWN', so they collide on the DB natural key.

    Without this collapse, _upsert would call executemany with both rows;
    SQLite's INSERT OR REPLACE would silently keep only the last one and
    drop the earlier one's metrics — invisibly losing impressions/spend.
    Pre-aggregation here makes the collision explicit and lossless.

    This MUST run before _synthesize_device_all because the device='ALL'
    rollup is computed by summing per-device rows, and missing per-device
    rows would produce an ALL that doesn't equal sum(per-device) downstream
    (which is what Check 2 of verify_geo_load.py validates).
    """
    natural_key = ["account_id", "campaign_id", "date", "device", "network",
                   "geo_state", "location_type"]
    metric_cols = ["impressions", "clicks", "cost_micros", "conversions",
                   "conversion_value", "all_conversions", "all_conversion_value"]
    before = len(per_device_df)
    out = per_device_df.groupby(natural_key, as_index=False)[metric_cols].sum()
    after = len(out)
    if after < before:
        print(
            f"  Collapsed {before - after} duplicate-natural-key per-device rows "
            f"(typically blank-state UNKNOWN rows where Microsoft emitted both "
            f"blank-country and US-country variants).",
        )
    return out


def _synthesize_device_all(per_device_df: pd.DataFrame) -> pd.DataFrame:
    """Sum per-device rows into device='ALL' rollups per (campaign_id, date,
    network, geo_state, location_type).

    location_type MUST be in the group key — otherwise PHYSICAL and INTEREST
    rows for the same (campaign, date, network, state) would be summed
    together into a single ALL row, double-counting an impression that
    appears in exactly one of the two location types but rolling it up as if
    it appeared in both.
    """
    group_cols = ["account_id", "campaign_id", "date", "network", "geo_state",
                  "location_type"]
    metric_cols = [
        "impressions", "clicks", "cost_micros", "conversions",
        "conversion_value", "all_conversions", "all_conversion_value",
    ]
    rollup = per_device_df.groupby(group_cols, as_index=False)[metric_cols].sum()
    rollup["device"] = "ALL"
    return rollup


def _attach_snapshot_fields(df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    """Approximate snapshot fields from current campaigns table values.

    Same caveat as the campaign tables: these reflect TODAY's settings, not the
    settings on the row's report date. Backfill when change-history ingestion lands.
    """
    campaign_snapshot = pd.read_sql_query(
        """
        SELECT campaign_id,
               daily_budget_micros         AS budget_micros_that_day,
               bidding_strategy_type       AS bidding_strategy_type_that_day,
               bidding_strategy_id         AS bidding_strategy_id_that_day,
               target_cpa_micros           AS target_cpa_micros_that_day,
               target_roas                 AS target_roas_that_day
        FROM campaigns
        """,
        conn,
    )
    return df.merge(campaign_snapshot, on="campaign_id", how="left")


def _upsert(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """INSERT OR REPLACE on natural key.

    Critical: convert each row's values to plain Python types before executemany.
    SQLite's default adapters don't reliably serialize pandas Int64 (nullable
    extension dtype) or, in some environments, numpy.int64/float64 scalars —
    silently falling back to BLOB storage of raw little-endian bytes. Casting
    to int/float/str at the tuple level guarantees clean INTEGER/REAL/TEXT
    storage regardless of upstream dtype quirks.
    """
    df = df.copy()
    df["last_synced_at"] = datetime.now(timezone.utc).isoformat()

    cols = [
        "account_id", "campaign_id", "date", "device", "network", "geo_state",
        "location_type",
        "impressions", "clicks", "cost_micros",
        "conversions", "conversion_value", "all_conversions", "all_conversion_value",
        "budget_micros_that_day", "bidding_strategy_type_that_day",
        "bidding_strategy_id_that_day", "target_cpa_micros_that_day",
        "target_roas_that_day", "last_synced_at",
    ]
    placeholders = ",".join("?" * len(cols))
    sql = (
        f"INSERT OR REPLACE INTO geo_performance_daily ({','.join(cols)}) "
        f"VALUES ({placeholders})"
    )

    int_cols   = {"account_id", "impressions", "clicks", "cost_micros",
                  "budget_micros_that_day", "target_cpa_micros_that_day"}
    float_cols = {"conversions", "conversion_value", "all_conversions",
                  "all_conversion_value", "target_roas_that_day"}
    str_cols   = {"campaign_id", "date", "device", "network", "geo_state",
                  "location_type",
                  "bidding_strategy_type_that_day", "bidding_strategy_id_that_day",
                  "last_synced_at"}

    def _coerce(col: str, val):
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        if col in int_cols:
            return int(val)
        if col in float_cols:
            return float(val)
        if col in str_cols:
            return str(val)
        return val

    rows = [
        tuple(_coerce(c, v) for c, v in zip(cols, row))
        for row in df[cols].itertuples(index=False, name=None)
    ]
    conn.executemany(sql, rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load Microsoft daily geo performance CSV(s)."
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse, validate, and report — no DB writes.")
    args = parser.parse_args()

    account_label_str = account_label(args.account)
    print(f"Account : {args.account}  ({account_label_str})")
    print(f"Mode    : {'DRY RUN' if args.dry_run else 'LIVE'}")

    # Resolve CSV paths
    if args.backfill or args.latest:
        # Distinguish "folder doesn't exist" from "folder exists but has no
        # matching files" — they have different operational meanings and
        # different fixes. Also create the folder on first run so future
        # drop-and-go workflows don't fail mysteriously. Mirrors loader 09.
        if not INPUT_DIR.exists():
            print(f"WARNING: Input folder does not exist: {INPUT_DIR}", file=sys.stderr)
            print("Creating it. Drop your CSV exports there and re-run.", file=sys.stderr)
            INPUT_DIR.mkdir(parents=True, exist_ok=True)
            print(
                f"ERROR: Expected filename pattern: microsoft-{{vertical}}-geo-YYYYMMDD.csv "
                f"where YYYYMMDD is the last day of the export period.",
                file=sys.stderr,
            )
            return 1

        if args.backfill:
            csv_paths = find_all_csvs(INPUT_DIR)
            if not csv_paths:
                print(
                    f"ERROR: Folder exists but no files match "
                    f"'microsoft-*-geo-YYYYMMDD.csv' in {INPUT_DIR}",
                    file=sys.stderr,
                )
                return 1
            print(f"Backfill: {len(csv_paths)} file(s) to load")
            for f in csv_paths:
                print(f"  {f.name}")
        else:  # args.latest
            latest = find_latest_csv(INPUT_DIR)
            if latest is None:
                print(
                    f"ERROR: Folder exists but no files match "
                    f"'microsoft-*-geo-YYYYMMDD.csv' in {INPUT_DIR}",
                    file=sys.stderr,
                )
                return 1
            csv_paths = [latest]
            print(f"Auto-selected: {latest.name}")
    else:
        csv_paths = [Path(args.file)]

    # Resolve platform_account_id from the registry up-front so the per-CSV
    # validator can use it; account_id (DB integer) requires a live connection
    # because resolve_account_id() does a runtime lookup against the accounts
    # table — never hardcoded.
    platform_account_id = ACCOUNT_REGISTRY[args.account]["platform_account_id"]

    with get_connection() as conn:
        account_id = resolve_account_id(conn, args.account)

        all_per_device = []
        for csv_path in csv_paths:
            print(f"Reading {csv_path}...")
            df = _read_and_normalize_csv(csv_path, platform_account_id, account_id)
            print(f"  {len(df)} per-device rows after validation/filtering.")
            all_per_device.append(df)

        per_device = pd.concat(all_per_device, ignore_index=True)
        per_device = _collapse_natural_key_duplicates(per_device)
        device_all = _synthesize_device_all(per_device)
        print(f"Synthesized {len(device_all)} device='ALL' rollup rows.")

        combined = pd.concat([per_device, device_all], ignore_index=True)
        combined = _attach_snapshot_fields(combined, conn)

        if args.dry_run:
            total = len(combined)
            print(f"DRY RUN — would upsert {total} rows into geo_performance_daily.")
            print("DRY RUN — nothing written.")
        else:
            n = _upsert(combined, conn)
            print(f"Upserted {n} rows into geo_performance_daily.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
