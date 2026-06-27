"""
04_load_microsoft_csv.py
------------------------
Loads Microsoft Advertising keyword performance CSV exports into the
SQLite database alongside Google Ads data.

What this script populates:
    accounts             -- seeds Microsoft Home Insurance if not present
    conversion_actions   -- seeds Microsoft conversion goals if not present
    keyword_identities   -- shared with Google (cross-platform identity layer)
    keyword_instances    -- one row per keyword slot (microsoft platform)
    keyword_performance_daily -- one row per keyword x date x device

What this script does NOT do:
    - Call any API (Microsoft data is manual CSV export only)
    - Load negative keywords (deferred to later phase)
    - Modify anything in Microsoft Advertising

Safe to run repeatedly. Each run upserts -- no duplicates created.
Re-running with an updated export for the same date range self-corrects
any previously loaded rows via INSERT OR REPLACE.

Usage:
    conda activate paid-search
    python 04_load_microsoft_csv.py --file "path/to/export.csv"

    # Preview without writing to database:
    python 04_load_microsoft_csv.py --file "path/to/export.csv" --dry-run

Prerequisites:
    01_create_schema.py has been run successfully.

CSV format expected:
    Microsoft Advertising keyword report with daily granularity.
    Required columns (exact names from UI):
        Account name, Account number, Campaign name, Campaign ID,
        Base campaign ID, Ad group, Ad group ID, Keyword ID,
        Keyword, Keyword status, Delivered match type, Bid match type,
        Current maximum CPC, Quality score (Keyword), Date,
        Device type, Network, Impressions, Clicks, Spend, Conversions, Revenue

    The file must include Microsoft's standard metadata header
    (lines 1-6) and footer (Total row + copyright line).
    Both are stripped automatically.
"""

import sqlite3
import argparse
import re
import os
from pathlib import Path
from datetime import date
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TODAY      = date.today().isoformat()

# Default input folder for Microsoft keyword exports.
# Expected filename pattern: microsoft-{vertical}-kw-{YYYYMMDD}.csv
# where the date is the LAST day of the export period.
# Examples:
#   microsoft-home-kw-20260331.csv   (full March 2026)
#   microsoft-home-kw-20260228.csv   (full February 2026)
# The --latest flag scans this folder and loads the most recent file
# automatically based on the date in the filename.
INPUT_DIR  = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-kw")

# Filename pattern for Microsoft keyword exports.
# Group 1: vertical (e.g. "home")
# Group 2: date string YYYYMMDD (e.g. "20260331")
MS_KW_FILENAME_RE = re.compile(r"microsoft-(\w+)-kw-(\d{8})\.csv$", re.IGNORECASE)

# Microsoft account details for Home Insurance
MS_ACCOUNT_NAME   = "Home Insurance"
MS_ACCOUNT_NUMBER = "F149SN63"       # platform_account_id for Microsoft
MS_PLATFORM       = "microsoft"

# ---------------------------------------------------------------------------
# Column name mapping
# Microsoft CSV column name -> our internal name
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    "Account name":              "account_name",
    "Account number":            "account_number",
    "Campaign name":             "campaign_name",
    "Campaign ID":               "campaign_id_raw",
    "Base campaign ID":          "base_campaign_id",
    "Ad group":                  "ad_group_name",
    "Ad group ID":               "ad_group_id_raw",
    "Keyword ID":                "keyword_id_raw",
    "Keyword":                   "keyword_text",
    "Keyword status":            "status",
    "Delivered match type":      "delivered_match_type",  # auction signal -- not used for match_type
    "Bid match type":            "match_type",             # structural bid setting -- used for match_type
    "Current maximum CPC":       "cpc",
    "Quality score (Keyword)":   "quality_score",
    "Date":                      "report_date",
    "Device type":               "device_raw",
    "Network":                   "network",
    "Impressions":               "impressions",
    "Clicks":                    "clicks",
    "Spend":                     "spend",
    "Conversions":               "conversions",
    "Revenue":                   "revenue",
}

# Microsoft device names -> our standard device values (matching Google)
DEVICE_MAP = {
    "Computer":   "DESKTOP",
    "Smartphone": "MOBILE",
    "Tablet":     "TABLET",
}

# Match type normalization (Microsoft exports Title Case)
MATCH_TYPE_MAP = {
    "Exact":  "EXACT",
    "Phrase": "PHRASE",
    "Broad":  "BROAD",
}

# Microsoft status normalization
STATUS_MAP = {
    "Active":  "ENABLED",
    "Paused":  "PAUSED",
    "Deleted": "REMOVED",
}

# All Microsoft network types -- no filtering applied.
# Microsoft Advertising splits traffic across:
#   "Microsoft sites and select traffic" -- owned and operated (primary volume)
#   "Audience"                           -- audience network (display-style)
#   "Bing and web"                       -- Bing + syndicated partners
#   "AOL search"                         -- AOL extended network
#   "Syndicated search partners"         -- third-party search partners
# All are included. If you want to exclude Audience network in future
# analysis, filter in the query layer rather than discarding rows at load.
INCLUDE_NETWORKS = None   # None = include all networks

# ---------------------------------------------------------------------------
# Microsoft Home Insurance conversion actions
# Sourced from screenshot: Tools -> Conversion tracking
# These are seeded into conversion_actions with the Microsoft account_id.
#
# Note on conversion routing for Microsoft:
# The keyword CSV export does not break out conversions by action name --
# Conversions and Revenue are unfiltered totals across all goals.
# We map the total directly to primary_conv_count / primary_conv_value.
# The conversion_actions rows below document what those totals represent.
# ---------------------------------------------------------------------------
MS_CONVERSION_ACTIONS = [
    {
        "conversion_name": "Home Bing Offline Conversions",
        "include_mode":    "primary",
        "notes": (
            "Primary revenue metric. Offline conversion upload. "
            "CSV Conversions and Revenue columns reflect this as the "
            "dominant signal. 90-day conversion window."
        ),
    },
    {
        "conversion_name": "Home Insurance Lead - Quote Wizard",
        "include_mode":    "secondary_count",
        "notes": (
            "Online lead form. Tag active and recording. "
            "Count only -- no dollar value confirmed. "
            "30-day conversion window."
        ),
    },
    {
        "conversion_name": "Home - EverQuote Conversion (External)",
        "include_mode":    "excluded",
        "notes": (
            "Tag inactive as of audit date. Exclude until reactivated. "
            "30-day conversion window. Unique count."
        ),
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def strip_brackets(value: str) -> str:
    """
    Removes Microsoft's bracket wrapping from ID fields.
    "[85007318860892]" -> "85007318860892"
    Returns the original string unchanged if no brackets present.
    """
    if isinstance(value, str):
        return value.strip("[]")
    return str(value)


def make_ms_criterion_id(keyword_id: str) -> str:
    """
    Returns the Microsoft Keyword ID as the criterion_id directly.

    Microsoft Keyword IDs are globally unique per keyword slot -- a given
    ID never appears across multiple ad groups, campaigns, or match types
    in the same account. Confirmed by analysis of the exported data:
    zero IDs with multiple bid match types, zero IDs across multiple
    ad groups or campaigns.

    This is structurally equivalent to Google's criterion_id, which is
    also unique per keyword slot within an ad group.

    Format: bare integer string, e.g. "85282197298231"
    """
    return keyword_id


def normalize_keyword_text(text: str) -> str:
    """Lowercase, strip, collapse internal whitespace."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def make_identity_key(normalized_text: str, match_type: str) -> str:
    return f"{normalized_text}::{match_type.upper()}"


def get_or_create_identity(cursor, normalized_text: str, match_type: str) -> int:
    """Returns existing identity id or creates a new one. Shared with Google."""
    key = make_identity_key(normalized_text, match_type)
    row = cursor.execute(
        "SELECT id FROM keyword_identities WHERE identity_key = ?", (key,)
    ).fetchone()
    if row:
        return row["id"]
    cursor.execute(
        """
        INSERT INTO keyword_identities (normalized_text, match_type, identity_key)
        VALUES (?, ?, ?)
        """,
        (normalized_text, match_type.upper(), key),
    )
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def read_microsoft_csv(filepath: str) -> pd.DataFrame:
    """
    Reads a Microsoft Advertising keyword CSV export into a DataFrame.

    Handles three Microsoft-specific formatting issues:
    1. Metadata header block (lines 1-6): report name, date range, timezone.
       We find the real header by scanning for the line starting with
       "Account name" rather than assuming a fixed skip count.
    2. Footer rows: strips the "Total" summary row and copyright notice.
    3. BOM character: the file starts with a UTF-8 BOM (\\ufeff) which
       pandas handles automatically with encoding="utf-8-sig".
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"CSV file not found: {filepath}")

    # Find the header row index by scanning for "Account name"
    header_row = None
    with open(filepath, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            if line.startswith('"Account name"') or line.startswith("Account name"):
                header_row = i
                break

    if header_row is None:
        raise ValueError(
            "Could not find header row in CSV. "
            "Expected a row starting with 'Account name'. "
            "Check that the export includes the standard Microsoft columns."
        )

    df = pd.read_csv(
        filepath,
        skiprows=header_row,
        encoding="utf-8-sig",
        dtype=str,           # read everything as string first; convert below
        thousands=",",
    )

    # Strip the Total row and copyright footer
    # Total row has "Total" in the first column
    df = df[df.iloc[:, 0] != "Total"]
    # Drop any rows where Account name is NaN (blank lines, copyright)
    df = df[df.iloc[:, 0].notna()]
    df = df[~df.iloc[:, 0].str.startswith("©")]

    # Rename columns using our mapping
    # Only rename columns that exist -- future exports may add columns
    rename = {k: v for k, v in COLUMN_MAP.items() if k in df.columns}
    # Delivered match type is optional -- kept for future match efficiency analysis
    # All other mapped columns are required
    optional_columns = {"Delivered match type"}
    missing = [k for k in COLUMN_MAP if k not in df.columns and k not in optional_columns]
    if missing:
        raise ValueError(
            f"CSV is missing expected columns: {missing}\n"
            f"Found columns: {list(df.columns)}"
        )
    df = df.rename(columns=rename)

    # Strip whitespace from all string columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip()

    return df


def clean_and_validate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies all data cleaning and type conversions to the raw DataFrame.

    Steps:
    1. Filter to search network rows only
    2. Strip brackets from ID fields
    3. Build criterion_id from bare keyword_id (globally unique per slot)
    4. Normalize match type, device, status to our standard values
    5. Convert numeric columns to correct types
    6. Convert Spend (dollars) to cost_micros (multiply by 1,000,000)
    """
    # 1. Network filter
    original_rows = len(df)
    if INCLUDE_NETWORKS is not None:
        df = df[df["network"].isin(INCLUDE_NETWORKS)].copy()
        filtered = original_rows - len(df)
        if filtered > 0:
            print(f"  Filtered out {filtered:,} rows by network filter")
    else:
        # Include all networks -- show breakdown for visibility
        network_counts = df["network"].value_counts()
        print(f"  Network breakdown (all included):")
        for network, count in network_counts.items():
            print(f"    {network:<45} {count:>6,} rows")
        df = df.copy()

    # 2. Strip brackets from ID fields
    df["keyword_id"]    = df["keyword_id_raw"].apply(strip_brackets)
    df["campaign_id"]   = df["base_campaign_id"].apply(strip_brackets)
    df["ad_group_id"]   = df["ad_group_id_raw"].apply(strip_brackets)

    # 3. Build criterion_id: bare keyword_id (confirmed unique per slot in this account)
    #    Done after match type normalization below
    df["match_type"] = df["match_type"].map(MATCH_TYPE_MAP).fillna(df["match_type"])
    df["criterion_id"] = df["keyword_id"].apply(make_ms_criterion_id)

    # 4. Normalize device and status
    df["device"] = df["device_raw"].map(DEVICE_MAP).fillna("DESKTOP")
    df["status"] = df["status"].map(STATUS_MAP).fillna("ENABLED")

    # 5. Convert numeric columns
    for col in ["impressions", "clicks", "quality_score"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    for col in ["spend", "conversions", "revenue", "cpc"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)

    # 6. Convert Spend (dollars) -> cost_micros to match Google schema
    #    Google stores cost_micros; we do the same for Microsoft so all
    #    cost queries work identically across platforms.
    df["cost_micros"] = (df["spend"] * 1_000_000).round().astype(float)

    # Validate date format
    df["report_date"] = pd.to_datetime(
        df["report_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    invalid_dates = df["report_date"].isna().sum()
    if invalid_dates > 0:
        print(f"  WARNING: {invalid_dates:,} rows have unparseable dates -- dropping")
        df = df[df["report_date"].notna()]

    # Handle quality_score: 0 means not available -- treat as NULL
    df["quality_score"] = df["quality_score"].apply(lambda x: x if x > 0 else None)

    # Handle CPC: 0 means not available
    df["cpc"] = df["cpc"].apply(lambda x: x if x > 0 else None)

    return df


# ---------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------

def ensure_account(cursor, conn) -> int:
    """
    Ensures the Microsoft Home Insurance account row exists.
    Returns the account_id.
    """
    cursor.execute(
        """
        INSERT OR IGNORE INTO accounts
            (account_name, platform_account_id, platform, mcc_id)
        VALUES (?, ?, ?, NULL)
        """,
        (MS_ACCOUNT_NAME, MS_ACCOUNT_NUMBER, MS_PLATFORM),
    )
    conn.commit()
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (MS_ACCOUNT_NUMBER, MS_PLATFORM),
    ).fetchone()
    return row["id"]


def ensure_conversion_actions(cursor, conn, account_id: int):
    """Seeds Microsoft conversion actions if not already present."""
    for ca in MS_CONVERSION_ACTIONS:
        cursor.execute(
            """
            INSERT OR IGNORE INTO conversion_actions
                (account_id, conversion_name, include_mode, notes)
            VALUES (?, ?, ?, ?)
            """,
            (account_id, ca["conversion_name"], ca["include_mode"], ca["notes"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Upsert functions
# ---------------------------------------------------------------------------

def upsert_instances_and_performance(
    conn, account_id: int, df: pd.DataFrame, dry_run: bool
) -> dict:
    """
    Processes the cleaned DataFrame and upserts both keyword_instances
    and keyword_performance_daily.

    Structure upsert (keyword_instances):
        - Groups by criterion_id (bare keyword_id) to get one
          row per unique keyword slot
        - Takes the most recent quality_score and cpc from the date range
        - Sets first_seen_date on new instances, updates last_seen_date always

    Performance upsert (keyword_performance_daily):
        - Each row in the DataFrame is one (keyword x date x device) combination
        - Aggregates across network types (Bing, AOL, Syndicated) into one
          combined row per (criterion x date x device)
        - Uses INSERT OR REPLACE so re-running for the same date range
          self-corrects previously loaded rows

    Microsoft conversion routing:
        Since the CSV does not break out conversions by goal, we map:
            Conversions -> primary_conv_count
            Revenue     -> primary_conv_value
        secondary_conv_count and all_conv_* are set to match primary
        (they are the same unfiltered total).
        This is documented in the conversion_actions notes.
    """
    cursor = conn.cursor()
    counts = {
        "instances_inserted": 0,
        "instances_updated":  0,
        "perf_written":       0,
    }

    # ------------------------------------------------------------------
    # Step 1: Build keyword_instances from unique criterion slots
    # ------------------------------------------------------------------
    # Get one structural row per criterion_id -- use the row with the
    # latest date to get the most current status, bid, and QS.
    structure_df = (
        df.sort_values("report_date", ascending=False)
        .groupby("criterion_id", as_index=False)
        .first()
    )

    for _, row in structure_df.iterrows():
        normalized = normalize_keyword_text(row["keyword_text"])
        identity_id = get_or_create_identity(cursor, normalized, row["match_type"])

        existing = cursor.execute(
            """
            SELECT id, first_seen_date FROM keyword_instances
            WHERE account_id = ? AND criterion_id = ?
            """,
            (account_id, row["criterion_id"]),
        ).fetchone()

        if existing is None:
            if not dry_run:
                cursor.execute(
                    """
                    INSERT INTO keyword_instances
                        (account_id, identity_id, criterion_id,
                         keyword_text, match_type,
                         campaign_name, ad_group_name,
                         status, cpc_bid_micros, quality_score,
                         first_seen_date, last_seen_date, platform)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'microsoft')
                    """,
                    (
                        account_id,
                        identity_id,
                        row["criterion_id"],
                        row["keyword_text"],
                        row["match_type"],
                        row["campaign_name"],
                        row["ad_group_name"],
                        row["status"],
                        row["cpc"],
                        row["quality_score"],
                        TODAY,
                        TODAY,
                    ),
                )
            counts["instances_inserted"] += 1
        else:
            if not dry_run:
                cursor.execute(
                    """
                    UPDATE keyword_instances
                    SET status         = ?,
                        cpc_bid_micros = ?,
                        quality_score  = ?,
                        campaign_name  = ?,
                        ad_group_name  = ?,
                        last_seen_date = ?
                    WHERE account_id = ? AND criterion_id = ?
                    """,
                    (
                        row["status"],
                        row["cpc"],
                        row["quality_score"],
                        row["campaign_name"],
                        row["ad_group_name"],
                        TODAY,
                        account_id,
                        row["criterion_id"],
                    ),
                )
            counts["instances_updated"] += 1

    if not dry_run:
        conn.commit()

    # ------------------------------------------------------------------
    # Step 2: Aggregate performance and write to keyword_performance_daily
    # ------------------------------------------------------------------
    # Two aggregations are written for each keyword/date:
    #
    #   device-level rows  (DESKTOP, MOBILE, TABLET)
    #     -- for device breakdown analysis, mirrors Google's Pass 2
    #     -- sums across network types (Audience + MS sites + AOL)
    #
    #   device='ALL' row   (one per keyword/date)
    #     -- the clean daily total used by all cross-platform analysis
    #     -- mirrors Google's Pass 1 (unsegmented daily rollup)
    #     -- without this, the analysis query (device='ALL') returns 0
    #        for all Microsoft keywords
    #
    # ------------------------------------------------------------------

    # Device-level aggregation: sum across network types per device
    perf_df = (
        df.groupby(["criterion_id", "report_date", "device"], as_index=False)
        .agg(
            impressions=("impressions", "sum"),
            clicks=("clicks", "sum"),
            cost_micros=("cost_micros", "sum"),
            conversions=("conversions", "sum"),
            revenue=("revenue", "sum"),
        )
    )

    # ALL-device rollup: sum across all devices AND network types
    perf_all_df = (
        df.groupby(["criterion_id", "report_date"], as_index=False)
        .agg(
            impressions=("impressions", "sum"),
            clicks=("clicks", "sum"),
            cost_micros=("cost_micros", "sum"),
            conversions=("conversions", "sum"),
            revenue=("revenue", "sum"),
        )
    )
    perf_all_df["device"] = "ALL"

    def _write_perf_rows(rows_df: "pd.DataFrame"):
        """Write a batch of aggregated performance rows to the database."""
        for _, row in rows_df.iterrows():
            inst_row = cursor.execute(
                """
                SELECT id FROM keyword_instances
                WHERE account_id = ? AND criterion_id = ?
                """,
                (account_id, row["criterion_id"]),
            ).fetchone()

            if inst_row is None:
                continue

            instance_id = inst_row["id"]

            if not dry_run:
                try:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO keyword_performance_daily
                            (instance_id, report_date, device, geo_state,
                             geo_target_constant_id,
                             clicks, impressions, cost_micros,
                             primary_conv_count, primary_conv_value,
                             secondary_conv_count,
                             all_conv_count, all_conv_value)
                        VALUES
                            (?, ?, ?, 'ALL', NULL,
                             ?, ?, ?,
                             ?, ?,
                             0,
                             ?, ?)
                        """,
                        (
                            instance_id,
                            row["report_date"],
                            row["device"],
                            int(row["clicks"]),
                            int(row["impressions"]),
                            float(row["cost_micros"]),
                            float(row["conversions"]),
                            float(row["revenue"]),
                            float(row["conversions"]),
                            float(row["revenue"]),
                        ),
                    )
                except Exception as e:
                    print(f"  ERROR writing performance row: {e}")
                    print(f"  Row: criterion={row['criterion_id']} "
                          f"date={row['report_date']} device={row['device']}")
            counts["perf_written"] += 1

    # Write device-level rows first, then ALL rollup
    _write_perf_rows(perf_df)
    _write_perf_rows(perf_all_df)

    if not dry_run:
        conn.commit()

    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Single-file load pipeline
# ---------------------------------------------------------------------------

def _run_load(csv_path: Path, dry_run: bool):
    """
    Executes the full load pipeline for a single CSV file.
    Called by both --latest/--file (once) and --backfill (once per file).
    """
    print(f"File    : {csv_path}")
    print(f"Mode    : {'DRY RUN (no database writes)' if dry_run else 'LIVE'}")
    print(f"Date    : {TODAY}")

    # -- Parse CSV --
    print("\nReading CSV...")
    df_raw = read_microsoft_csv(csv_path)
    print(f"  Raw rows after stripping header/footer: {len(df_raw):,}")

    print("\nCleaning and validating...")
    df = clean_and_validate(df_raw)
    print(f"  Rows after network filter and cleaning : {len(df):,}")

    # -- Preview unique dimensions --
    print(f"\n  Date range  : {df['report_date'].min()} to {df['report_date'].max()}")
    print(f"  Devices     : {sorted(df['device'].unique().tolist())}")
    print(f"  Match types : {sorted(df['match_type'].unique().tolist())}")
    print(f"  Keywords    : {df['criterion_id'].nunique():,} unique criterion slots")
    print(f"  Campaigns   : {df['campaign_name'].nunique():,}")
    print(f"  Ad groups   : {df['ad_group_name'].nunique():,}")

    total_clicks = df["clicks"].sum()
    total_imps   = df["impressions"].sum()
    total_spend  = df["spend"].sum()
    total_convs  = df["conversions"].sum()
    total_rev    = df["revenue"].sum()
    print(f"\n  Impressions : {total_imps:,.0f}")
    print(f"  Clicks      : {total_clicks:,.0f}")
    print(f"  Spend       : ${total_spend:,.2f}")
    print(f"  Conversions : {total_convs:,.2f}")
    print(f"  Revenue     : ${total_rev:,.2f}")

    if dry_run:
        print("\nDRY RUN complete. No data written to database.")
        return

    # -- Database operations --
    conn = get_db_connection()
    cursor = conn.cursor()

    print("\n--- Seeding account and conversion actions ---")
    account_id = ensure_account(cursor, conn)
    print(f"  Microsoft Home Insurance account_id: {account_id}")
    ensure_conversion_actions(cursor, conn, account_id)
    print(f"  Seeded {len(MS_CONVERSION_ACTIONS)} conversion actions")

    print("\n--- Upserting keyword instances and performance ---")
    counts = upsert_instances_and_performance(conn, account_id, df, dry_run=False)
    print(f"  Instances inserted : {counts['instances_inserted']:,}")
    print(f"  Instances updated  : {counts['instances_updated']:,}")
    print(f"  Performance rows   : {counts['perf_written']:,}")

    # -- Summary --
    print("\n--- Database summary after load ---")
    ms_instances = cursor.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?",
        (account_id,)
    ).fetchone()[0]
    ms_perf = cursor.execute(
        """
        SELECT COUNT(*) FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        WHERE ki.account_id = ?
        """,
        (account_id,)
    ).fetchone()[0]
    identity_total = cursor.execute(
        "SELECT COUNT(*) FROM keyword_identities"
    ).fetchone()[0]

    print(f"  Microsoft keyword instances             : {ms_instances:>8,}")
    print(f"  Microsoft performance rows              : {ms_perf:>8,}")
    print(f"  Total keyword_identities (all platforms): {identity_total:>8,}")

    # Cross-platform identity overlap check
    google_account = cursor.execute(
        "SELECT id FROM accounts "
        "WHERE platform_account_id = '8886502147' AND platform = 'google'"
    ).fetchone()

    if google_account:
        overlap = cursor.execute(
            """
            SELECT COUNT(DISTINCT ki_ms.identity_id) AS overlap_count
            FROM keyword_instances ki_ms
            JOIN keyword_instances ki_g
                ON ki_ms.identity_id = ki_g.identity_id
            WHERE ki_ms.account_id = ?
              AND ki_g.account_id  = ?
            """,
            (account_id, google_account["id"]),
        ).fetchone()["overlap_count"]

        ms_only = cursor.execute(
            "SELECT COUNT(DISTINCT identity_id) FROM keyword_instances "
            "WHERE account_id = ?",
            (account_id,)
        ).fetchone()[0]

        print(f"\n  Cross-platform keyword overlap:")
        print(f"  Keywords on both Google and Microsoft   : {overlap:>8,}")
        print(f"  Microsoft unique identities             : {ms_only:>8,}")
        if ms_only > 0:
            pct = overlap / ms_only * 100
            print(f"  Overlap rate                            : {pct:>7.1f}%")

    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\n  Database size: {size_mb:.2f} MB")
    conn.close()
    print("\nDone.")


# ---------------------------------------------------------------------------
# Account reset
# ---------------------------------------------------------------------------

def reset_microsoft_account(conn, dry_run: bool):
    """
    Wipes all Microsoft Home Insurance data from the database so a clean
    backfill can be run with corrected CSV exports.

    Deletes in dependency order (children before parents):
        1. keyword_performance_daily rows for Microsoft instances
        2. keyword_instances for the Microsoft account
        3. keyword_identities that are now orphaned (no remaining instances)
        4. conversion_actions for the Microsoft account
        5. The accounts row itself is preserved -- account_id stays stable

    Does NOT delete the accounts row. This keeps account_id=2 consistent
    so any external references remain valid.

    Safe to run before --backfill. The backfill will re-seed everything.
    """
    cursor = conn.cursor()

    # Get Microsoft account_id
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (MS_ACCOUNT_NUMBER, MS_PLATFORM)
    ).fetchone()

    if row is None:
        print("  Microsoft account not found in database -- nothing to reset.")
        return

    account_id = row["id"]

    # Count what will be deleted for the confirmation message
    perf_count = cursor.execute("""
        SELECT COUNT(*) FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        WHERE ki.account_id = ?
    """, (account_id,)).fetchone()[0]

    instance_count = cursor.execute(
        "SELECT COUNT(*) FROM keyword_instances WHERE account_id = ?",
        (account_id,)
    ).fetchone()[0]

    # Orphaned identities: identity_ids used only by this account
    orphan_count = cursor.execute("""
        SELECT COUNT(*) FROM keyword_identities kd
        WHERE kd.id IN (
            SELECT identity_id FROM keyword_instances WHERE account_id = ?
        )
        AND kd.id NOT IN (
            SELECT identity_id FROM keyword_instances WHERE account_id != ?
        )
    """, (account_id, account_id)).fetchone()[0]

    conv_count = cursor.execute(
        "SELECT COUNT(*) FROM conversion_actions WHERE account_id = ?",
        (account_id,)
    ).fetchone()[0]

    print(f"  Account id       : {account_id} ({MS_ACCOUNT_NUMBER})")
    print(f"  Performance rows : {perf_count:,}")
    print(f"  Instances        : {instance_count:,}")
    print(f"  Orphaned identities: {orphan_count:,}")
    print(f"  Conversion actions : {conv_count:,}")

    if dry_run:
        print("  DRY RUN -- nothing deleted.")
        return

    # Delete performance rows
    cursor.execute("""
        DELETE FROM keyword_performance_daily
        WHERE instance_id IN (
            SELECT id FROM keyword_instances WHERE account_id = ?
        )
    """, (account_id,))
    print(f"  Deleted {cursor.rowcount:,} performance rows")

    # Capture orphaned identity_ids before deleting instances
    orphan_ids = [r[0] for r in cursor.execute("""
        SELECT kd.id FROM keyword_identities kd
        WHERE kd.id IN (
            SELECT identity_id FROM keyword_instances WHERE account_id = ?
        )
        AND kd.id NOT IN (
            SELECT identity_id FROM keyword_instances WHERE account_id != ?
        )
    """, (account_id, account_id)).fetchall()]

    # Delete instances
    cursor.execute(
        "DELETE FROM keyword_instances WHERE account_id = ?",
        (account_id,)
    )
    print(f"  Deleted {cursor.rowcount:,} keyword instances")

    # Delete orphaned identities
    if orphan_ids:
        placeholders = ",".join("?" * len(orphan_ids))
        cursor.execute(
            f"DELETE FROM keyword_identities WHERE id IN ({placeholders})",
            orphan_ids
        )
        print(f"  Deleted {cursor.rowcount:,} orphaned keyword identities")

    # Delete conversion actions
    cursor.execute(
        "DELETE FROM conversion_actions WHERE account_id = ?",
        (account_id,)
    )
    print(f"  Deleted {cursor.rowcount:,} conversion actions")

    conn.commit()
    print("  Reset complete. Account row preserved (account_id stable).")


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_all_csvs(input_dir: Path) -> list[Path]:
    """
    Scans input_dir for all files matching the Microsoft keyword export
    naming convention and returns them sorted chronologically (oldest first).

    Pattern: microsoft-{vertical}-kw-{YYYYMMDD}.csv
    Oldest first so backfill loads history before recent data -- consistent
    with how first_seen_date gets set on keyword_instances.
    """
    if not input_dir.exists():
        return []

    candidates = []
    for filepath in input_dir.glob("microsoft-*-kw-*.csv"):
        match = MS_KW_FILENAME_RE.search(filepath.name)
        if match:
            date_str = match.group(2)
            candidates.append((date_str, filepath))

    candidates.sort(key=lambda x: x[0])   # ascending -- oldest first
    return [fp for _, fp in candidates]


def find_latest_csv(input_dir: Path) -> Path | None:
    """
    Returns the single most recent CSV in input_dir based on the date
    in the filename, or None if no matching files are found.
    """
    all_files = find_all_csvs(input_dir)
    return all_files[-1] if all_files else None


def main():
    parser = argparse.ArgumentParser(
        description="Load Microsoft Advertising keyword CSV into paid_search.db"
    )
    file_group = parser.add_mutually_exclusive_group(required=True)
    file_group.add_argument(
        "--file",
        help=(
            "Path to a specific Microsoft keyword CSV export. "
            "Example: --file microsoft-home-kw-20260331.csv"
        ),
    )
    file_group.add_argument(
        "--latest",
        action="store_true",
        help=(
            f"Find and load the single most recent CSV in {INPUT_DIR} "
            "based on the date in the filename. Use for weekly incremental loads."
        ),
    )
    file_group.add_argument(
        "--backfill",
        action="store_true",
        help=(
            f"Load ALL CSVs in {INPUT_DIR} in chronological order (oldest first). "
            "Use when loading historical exports for the first time. "
            "Safe to re-run -- INSERT OR REPLACE means no duplicates."
        ),
    )
    parser.add_argument(
        "--reset-account",
        action="store_true",
        help=(
            "Wipe all Microsoft Home Insurance data from the database before "
            "loading. Use when re-importing corrected CSV exports. "
            "Must be combined with --backfill or --latest. "
            "Safe with --dry-run to preview what would be deleted."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate the CSV without writing to the database"
    )
    args = parser.parse_args()

    # Run account reset if requested -- always before any file loading
    if args.reset_account:
        print("=" * 65)
        print("RESET MICROSOFT ACCOUNT")
        print("=" * 65)
        if args.dry_run:
            print("Mode: DRY RUN -- showing what would be deleted\n")
        else:
            print("Mode: LIVE -- deleting existing Microsoft data\n")
            confirm = input(
                "This will permanently delete all Microsoft Home Insurance data "
                "from the database.\nType YES to confirm: "
            )
            if confirm.strip().upper() != "YES":
                print("Reset cancelled.")
                return
        conn = get_db_connection()
        reset_microsoft_account(conn, dry_run=args.dry_run)
        conn.close()
        if args.dry_run:
            print("\nDry-run complete. Re-run without --dry-run to execute.")
            return
        print()

    # Resolve file list -- single file, latest only, or all files for backfill
    if args.backfill:
        csv_files = find_all_csvs(INPUT_DIR)
        if not csv_files:
            print(f"ERROR: No files matching 'microsoft-*-kw-YYYYMMDD.csv' found in:")
            print(f"  {INPUT_DIR}")
            print("Check that the folder exists and exports follow the naming convention.")
            return
        print(f"Backfill mode: found {len(csv_files)} file(s) to load (oldest first):")
        for f in csv_files:
            print(f"  {f.name}")
        print()
        # Run the full load pipeline for each file in order
        for csv_path in csv_files:
            print("=" * 65)
            print(f"Loading: {csv_path.name}")
            print("=" * 65)
            _run_load(csv_path, args.dry_run)
        print("\nBackfill complete.")
        return
    elif args.latest:
        csv_path = find_latest_csv(INPUT_DIR)
        if csv_path is None:
            print(f"ERROR: No files matching 'microsoft-*-kw-YYYYMMDD.csv' found in:")
            print(f"  {INPUT_DIR}")
            print("Check that the folder exists and exports follow the naming convention.")
            return
        print(f"Auto-selected latest file: {csv_path.name}")
    else:
        csv_path = Path(args.file)

    _run_load(csv_path, args.dry_run)


if __name__ == "__main__":
    main()
