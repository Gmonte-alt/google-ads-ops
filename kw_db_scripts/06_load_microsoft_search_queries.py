"""
06_load_microsoft_search_queries.py
-------------------------------------
Loads Microsoft Advertising search term (query) performance CSV exports
into paid_search.db, populating the same search_queries and
search_query_performance tables used by the Google sync.

Cross-platform identity design:
    The same normalized query text resolves to the same query_id regardless
    of platform. If Google already has "home insurance" as query_id=42, a
    Microsoft row for "home insurance" will attach to query_id=42. Performance
    rows are separate (different instance_ids, platform='microsoft') but share
    the query identity, enabling direct cross-platform revenue comparison.

Keyword ID join:
    The Microsoft search term report includes 'Keyword ID' (with brackets).
    We strip the brackets and join directly to keyword_instances.criterion_id,
    exactly like 04_load_microsoft_csv.py. This is more reliable than
    text-based keyword matching and avoids ambiguity when the same keyword
    text exists in multiple ad groups.

Conversion routing (mirrors 04_load_microsoft_csv.py):
    All conversions - Conversions  -> primary_conv_count  (offline only, deduped)
    All revenue                    -> primary_conv_value   (offline revenue)
    Conversions                    -> secondary_conv_count (pixel lead submit)
    All conversions                -> all_conv_count       (raw total, known double-count)
    All revenue                    -> all_conv_value       (same as primary_conv_value)

Actual CSV columns (confirmed from sample export):
    Account name, Account number, Campaign name, Campaign ID,
    Ad group, Ad group ID, Search term, Keyword ID, Keyword,
    Ad ID, Bid match type, Delivered match type, Date, Device type,
    Network, Impressions, Clicks, Spend, Top impression rate,
    Absolute top impression rate, Conversions, Revenue,
    All conversions, All revenue

    'Delivered match type' is used as match_type_served (auction-level).
    'Bid match type' is informational only -- not stored separately.
    'Top/Absolute top impression rate' are ignored (not in schema).

Input folder:
    C:\\Projects\\google-ads-ops\\kw_db_scripts\\input\\microsoft-sq\\

File naming convention:
    microsoft-home-sq-YYYYMMDD.csv

Modes:
    --backfill          Load ALL files in input folder, oldest first
    --latest            Load only the most recent file
    --file PATH         Load a specific file
    --reset-account     Wipe all Microsoft search_query_performance rows
                        before loading. Does NOT delete search_queries
                        identities (shared with Google).
    --dry-run           Parse and validate without writing to database

Usage:
    conda activate paid-search
    python kw_db_scripts/06_load_microsoft_search_queries.py --backfill
    python kw_db_scripts/06_load_microsoft_search_queries.py --latest
    python kw_db_scripts/06_load_microsoft_search_queries.py --reset-account --backfill
"""

import sqlite3
import re
import csv
import io
import argparse
from pathlib import Path
from datetime import date
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH   = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
INPUT_DIR = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-sq")

MS_ACCOUNT_NUMBER = "F149SN63"
MS_PLATFORM       = "microsoft"
INCLUDE_NETWORKS  = None   # None = all networks (MS sites + AOL + Audience)
TODAY             = date.today()

# ---------------------------------------------------------------------------
# Column map: CSV column name -> internal name
# ---------------------------------------------------------------------------

COLUMN_MAP = {
    "Account name":                  "account_name",
    "Account number":                "account_number",
    "Campaign name":                 "campaign_name",
    "Campaign ID":                   "campaign_id_raw",
    "Ad group":                      "ad_group_name",
    "Ad group ID":                   "ad_group_id_raw",
    "Search term":                   "query_text",
    "Keyword ID":                    "keyword_id_raw",    # bracketed: [12345...]
    "Keyword":                       "matched_keyword",   # for reference only
    "Ad ID":                         "ad_id_raw",
    "Bid match type":                "bid_match_type",    # informational
    "Delivered match type":          "match_type_served", # auction-level match
    "Date":                          "report_date",
    "Device type":                   "device_raw",
    "Network":                       "network",
    "Impressions":                   "impressions",
    "Clicks":                        "clicks",
    "Spend":                         "spend",
    "Conversions":                   "conversions",       # pixel lead submit
    "Revenue":                       "revenue",           # always $0
    "All conversions":               "all_conversions",   # pixel + offline combined
    "All revenue":                   "all_revenue",       # offline revenue only
    # Ignored (not in schema):
    # "Top impression rate"
    # "Absolute top impression rate"
}

OPTIONAL_COLUMNS = {"Revenue", "Ad ID", "Ad group ID", "Campaign ID",
                    "Top impression rate", "Absolute top impression rate"}

DEVICE_MAP = {
    "Computer":   "DESKTOP",
    "Smartphone": "MOBILE",
    "Tablet":     "TABLET",
}

# Delivered match type can include close variant qualifiers
# We normalize to base match type for storage
def normalize_match_type(raw: str) -> str:
    if not raw:
        return "BROAD"
    raw_upper = raw.upper()
    if "EXACT" in raw_upper:
        return "EXACT"
    if "PHRASE" in raw_upper:
        return "PHRASE"
    return "BROAD"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = ?",
        (MS_ACCOUNT_NUMBER, MS_PLATFORM)
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Microsoft account {MS_ACCOUNT_NUMBER} not found in accounts table.\n"
            "Run 04_load_microsoft_csv.py first to seed the account."
        )
    return row["id"]


# ---------------------------------------------------------------------------
# Query identity helpers (mirrors 05_sync_search_queries.py)
# ---------------------------------------------------------------------------

def normalize_query(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower().strip())


def get_or_create_query(cursor, query_text: str, report_date: str) -> int:
    """
    Returns search_queries.id for the query text, creating if needed.
    Deduplication key is normalized_text (lowercase + stripped).
    Same normalized text on Google and Microsoft resolves to the same id.
    """
    normalized = normalize_query(query_text)

    row = cursor.execute(
        "SELECT id FROM search_queries WHERE normalized_text = ?",
        (normalized,)
    ).fetchone()

    if row is None:
        cursor.execute(
            """
            INSERT INTO search_queries
                (query_text, normalized_text,
                 first_seen_date, last_seen_date, first_seen_platform)
            VALUES (?, ?, ?, ?, 'microsoft')
            """,
            (query_text, normalized, report_date, report_date)
        )
        return cursor.lastrowid
    else:
        cursor.execute(
            "UPDATE search_queries SET last_seen_date = MAX(last_seen_date, ?) WHERE id = ?",
            (report_date, row["id"])
        )
        return row["id"]


# ---------------------------------------------------------------------------
# Keyword instance lookup via Keyword ID (criterion_id)
# ---------------------------------------------------------------------------

def strip_brackets(value: str) -> str:
    """Remove square brackets from Microsoft IDs: '[12345]' -> '12345'."""
    return re.sub(r'[\[\]]', '', str(value)).strip()


def build_criterion_id_map(cursor, account_id: int) -> dict:
    """
    Loads all keyword_instances for this account into a dict:
        { criterion_id_str: instance_id }

    Loaded once per file and reused for all row lookups.
    Much faster than a per-row query against 1,000+ instances.
    """
    rows = cursor.execute(
        "SELECT id, criterion_id FROM keyword_instances WHERE account_id = ?",
        (account_id,)
    ).fetchall()
    return {str(r["criterion_id"]): r["id"] for r in rows}


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def find_header_row(lines: list[str]) -> int:
    """
    Finds the line index of the actual column header row.
    Microsoft reports have a 4-6 line preamble (report name, date range,
    timezone, etc.) before the header. We detect the header by looking
    for 'Search term' or 'Account name' in the line.
    """
    for i, line in enumerate(lines[:15]):
        if '"Search term"' in line or '"Account name"' in line:
            return i
        if line.startswith('Account name') or line.startswith('"Account name"'):
            return i
    raise ValueError(
        "Could not find header row in CSV.\n"
        "Ensure this is a Microsoft search term report with 'Search term' column."
    )


def read_csv(filepath: Path) -> pd.DataFrame:
    """
    Reads a Microsoft search term CSV, stripping preamble and footer rows.
    """
    with open(filepath, encoding='utf-8-sig') as f:
        lines = f.readlines()

    header_idx = find_header_row(lines)

    # Strip footer: Total row and copyright
    data_lines = []
    for line in lines[header_idx:]:
        stripped = line.strip().strip('"')
        if stripped.startswith('Total,') or stripped.startswith('"Total"') \
                or stripped.startswith('©') or stripped == '':
            continue
        data_lines.append(line)

    reader = csv.DictReader(io.StringIO(''.join(data_lines)))
    rows = []
    for row in reader:
        acct = row.get('Account name', '').strip().strip('"')
        if not acct or acct.lower() in ('total', 'account name'):
            continue
        if acct.startswith('©'):
            continue
        rows.append(row)

    if not rows:
        raise ValueError(f"No data rows found in {filepath.name}. "
                         "Check that this is a Microsoft search term report.")

    df = pd.DataFrame(rows)
    print(f"  Raw rows: {len(df):,}")
    return df


def clean_and_validate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Renames columns, filters networks, converts types, computes derived fields.
    """
    # 1. Rename known columns
    rename = {k: v for k, v in COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)

    # 2. Check required columns present
    required = [v for k, v in COLUMN_MAP.items() if k not in OPTIONAL_COLUMNS]
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required columns after rename: {missing}\n"
            "Check that the export includes all required columns."
        )

    # 3. Network filter
    if INCLUDE_NETWORKS is not None:
        df = df[df["network"].isin(INCLUDE_NETWORKS)].copy()
    else:
        net_counts = df["network"].value_counts()
        print(f"  Networks (all included):")
        for net, cnt in net_counts.items():
            print(f"    {net:<50} {cnt:>8,} rows")
        df = df.copy()

    # 4. Date -- already YYYY-MM-DD from Microsoft export
    df["report_date"] = pd.to_datetime(
        df["report_date"], format='mixed'
    ).dt.strftime("%Y-%m-%d")

    # 5. Device
    df["device"] = df["device_raw"].map(DEVICE_MAP).fillna("DESKTOP")

    # 6. Match type served -- normalize close variants to base type
    df["match_type_served"] = df["match_type_served"].apply(normalize_match_type)

    # 7. Strip brackets from Keyword ID -> criterion_id
    df["criterion_id"] = df["keyword_id_raw"].apply(strip_brackets)

    # 8. Numeric conversion -- handle comma-formatted numbers
    for col in ["impressions", "clicks"]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(',', ''), errors='coerce'
        ).fillna(0).astype(int)

    for col in ["spend", "conversions", "all_conversions", "all_revenue"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(',', ''), errors='coerce'
            ).fillna(0.0)
        else:
            df[col] = 0.0

    df["revenue"] = 0.0

    # 9. cost_micros
    df["cost_micros"] = (df["spend"] * 1_000_000).round().astype(float)

    # 10. Offline conversion count (deduplication)
    #     All conversions - Conversions = offline-only count, no double-count.
    #     Before 2026-01-13: all_conversions == conversions -> offline = 0 (correct).
    #     After  2026-01-13: all_conversions > conversions  -> offline count correct.
    df["offline_conv_count"] = (
        df["all_conversions"] - df["conversions"]
    ).clip(lower=0)

    # 11. Clean query text
    df["query_text"] = df["query_text"].astype(str).str.strip()
    df = df[df["query_text"].notna() & (df["query_text"] != "")].copy()

    return df


# ---------------------------------------------------------------------------
# Performance upsert
# ---------------------------------------------------------------------------

def upsert_query_performance(
    conn, account_id: int, df: pd.DataFrame, dry_run: bool
) -> dict:
    """
    For each row:
      1. Resolve criterion_id -> instance_id via the pre-built map
      2. Get or create search_queries identity for the query text
      3. Write device-level rows + ALL-device rollup

    Aggregates across networks before writing -- same query on
    Microsoft sites + AOL in same day/device = one row in the DB.
    """
    cursor = conn.cursor()
    counts = {"queries_new": 0, "perf_written": 0, "perf_skipped": 0}

    # Load criterion_id -> instance_id map once
    crit_map = build_criterion_id_map(cursor, account_id)
    print(f"  Keyword instances loaded: {len(crit_map):,}")

    # Aggregate by (query_text, criterion_id, match_type_served, date, device)
    # across networks before writing
    agg_cols = {
        "impressions":        "sum",
        "clicks":             "sum",
        "cost_micros":        "sum",
        "conversions":        "sum",
        "offline_conv_count": "sum",
        "all_conversions":    "sum",
        "all_revenue":        "sum",
    }
    group_device = ["query_text", "criterion_id", "match_type_served",
                    "report_date", "device"]
    group_all    = ["query_text", "criterion_id", "match_type_served",
                    "report_date"]

    perf_device_df = df.groupby(group_device, as_index=False).agg(agg_cols)
    perf_all_df    = df.groupby(group_all,    as_index=False).agg(agg_cols)
    perf_all_df["device"] = "ALL"

    combined = pd.concat([perf_device_df, perf_all_df], ignore_index=True)
    print(f"  Rows to write: {len(combined):,} "
          f"({len(perf_device_df):,} device + {len(perf_all_df):,} ALL-device)")

    BATCH = 500
    updates = []

    for _, row in combined.iterrows():
        crit_id_str  = str(row["criterion_id"])
        instance_id  = crit_map.get(crit_id_str)

        if instance_id is None:
            counts["perf_skipped"] += 1
            continue

        q_text   = str(row["query_text"])
        is_new   = cursor.execute(
            "SELECT COUNT(*) FROM search_queries WHERE normalized_text = ?",
            (normalize_query(q_text),)
        ).fetchone()[0] == 0

        query_id = get_or_create_query(cursor, q_text, row["report_date"])

        if is_new:
            counts["queries_new"] += 1

        updates.append((
            query_id,
            instance_id,
            row["report_date"],
            row["device"],
            row["match_type_served"],
            int(row["impressions"]),
            int(row["clicks"]),
            float(row["cost_micros"]),
            float(row["offline_conv_count"]),  # primary: offline only (deduped)
            float(row["all_revenue"]),          # primary: offline revenue
            float(row["conversions"]),          # secondary: pixel lead
            float(row["all_conversions"]),      # all_conv: raw total
            float(row["all_revenue"]),          # all_conv_value
        ))
        counts["perf_written"] += 1

        if not dry_run and len(updates) >= BATCH:
            _flush(cursor, updates)
            conn.commit()
            updates.clear()

    if not dry_run and updates:
        _flush(cursor, updates)
        conn.commit()

    return counts


def _flush(cursor, updates: list):
    cursor.executemany(
        """
        INSERT INTO search_query_performance
            (query_id, instance_id, platform,
             report_date, device, match_type_served,
             impressions, clicks, cost_micros,
             primary_conv_count, primary_conv_value,
             secondary_conv_count,
             all_conv_count, all_conv_value)
        VALUES
            (?, ?, 'microsoft',
             ?, ?, ?,
             ?, ?, ?,
             ?, ?,
             ?,
             ?, ?)
        ON CONFLICT (query_id, instance_id, report_date, device)
        DO UPDATE SET
            match_type_served    = excluded.match_type_served,
            impressions          = excluded.impressions,
            clicks               = excluded.clicks,
            cost_micros          = excluded.cost_micros,
            primary_conv_count   = excluded.primary_conv_count,
            primary_conv_value   = excluded.primary_conv_value,
            secondary_conv_count = excluded.secondary_conv_count,
            all_conv_count       = excluded.all_conv_count,
            all_conv_value       = excluded.all_conv_value
        """,
        updates
    )


# ---------------------------------------------------------------------------
# Account reset
# ---------------------------------------------------------------------------

def reset_microsoft_search_queries(conn, dry_run: bool):
    cursor = conn.cursor()
    count = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance WHERE platform = 'microsoft'"
    ).fetchone()[0]
    print(f"  Microsoft search_query_performance rows: {count:,}")

    if dry_run:
        print("  DRY RUN -- nothing deleted.")
        return

    cursor.execute(
        "DELETE FROM search_query_performance WHERE platform = 'microsoft'"
    )
    print(f"  Deleted {cursor.rowcount:,} rows")
    print("  search_queries identity rows preserved (shared with Google)")
    conn.commit()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_all_csvs(folder: Path) -> list[Path]:
    return sorted(folder.glob("microsoft-*-sq-*.csv"))


def find_latest_csv(folder: Path) -> Path | None:
    files = find_all_csvs(folder)
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Core load runner
# ---------------------------------------------------------------------------

def _run_load(csv_path: Path, dry_run: bool):
    print(f"\nFile     : {csv_path.name}")
    print(f"Mode     : {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Date     : {TODAY}")

    conn = get_conn()
    cursor = conn.cursor()
    account_id = get_account_id(cursor)
    print(f"Account  : {MS_ACCOUNT_NUMBER} (db id={account_id})")

    print("\nReading CSV...")
    df_raw = read_csv(csv_path)

    print("\nCleaning and validating...")
    df = clean_and_validate(df_raw)

    print(f"\n  Date range          : {df['report_date'].min()} to {df['report_date'].max()}")
    print(f"  Devices             : {sorted(df['device'].unique())}")
    print(f"  Match types served  : {sorted(df['match_type_served'].unique())}")
    print(f"  Unique search terms : {df['query_text'].nunique():,}")
    print(f"\n  Clicks              : {df['clicks'].sum():,.0f}")
    print(f"  Spend               : ${df['spend'].sum():,.2f}")
    print(f"  Conversions (pixel) : {df['conversions'].sum():,.2f}")
    print(f"  All conversions     : {df['all_conversions'].sum():,.2f}")
    print(f"  Offline (deduped)   : {df['offline_conv_count'].sum():,.2f}")
    print(f"  All revenue (offline): ${df['all_revenue'].sum():,.2f}")

    print("\nUpserting search query performance...")
    counts = upsert_query_performance(conn, account_id, df, dry_run=dry_run)

    print(f"\n  New query identities      : {counts['queries_new']:,}")
    print(f"  Performance rows written  : {counts['perf_written']:,}")
    print(f"  Skipped (no kw instance)  : {counts['perf_skipped']:,}")

    # DB summary
    total_sq     = cursor.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
    total_ms     = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance WHERE platform='microsoft'"
    ).fetchone()[0]
    total_g      = cursor.execute(
        "SELECT COUNT(*) FROM search_query_performance WHERE platform='google'"
    ).fetchone()[0]
    overlap      = cursor.execute("""
        SELECT COUNT(DISTINCT id) FROM search_queries
        WHERE id IN (SELECT DISTINCT query_id FROM search_query_performance
                     WHERE platform='google')
          AND id IN (SELECT DISTINCT query_id FROM search_query_performance
                     WHERE platform='microsoft')
    """).fetchone()[0]

    print(f"\n--- Database summary ---")
    print(f"  Total unique search queries  : {total_sq:,}")
    print(f"  Google performance rows      : {total_g:,}")
    print(f"  Microsoft performance rows   : {total_ms:,}")
    print(f"  Queries on BOTH platforms    : {overlap:,}")

    conn.close()
    print("\nDone.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Load Microsoft search term CSV exports into paid_search.db"
    )
    file_group = parser.add_mutually_exclusive_group(required=True)
    file_group.add_argument("--file",     help="Path to a specific CSV")
    file_group.add_argument("--latest",   action="store_true",
                            help="Load the most recent CSV in the input folder")
    file_group.add_argument("--backfill", action="store_true",
                            help="Load ALL CSVs in the input folder, oldest first")

    parser.add_argument("--reset-account", action="store_true",
                        help="Delete all Microsoft search_query_performance rows first")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate without writing to database")

    args = parser.parse_args()

    if args.reset_account:
        print("=" * 65)
        print("RESET: Microsoft search query performance")
        print("=" * 65)
        if not args.dry_run:
            confirm = input(
                "Deletes all Microsoft search_query_performance rows.\n"
                "search_queries identities are preserved.\n"
                "Type YES to confirm: "
            )
            if confirm.strip().upper() != "YES":
                print("Reset cancelled.")
                return
        conn = get_conn()
        reset_microsoft_search_queries(conn, dry_run=args.dry_run)
        conn.close()
        if args.dry_run:
            return
        print()

    if not INPUT_DIR.exists() and not args.file:
        print(f"ERROR: Input folder not found:\n  {INPUT_DIR}")
        print("Create it and place Microsoft search term CSVs there.")
        print("Naming: microsoft-home-sq-YYYYMMDD.csv")
        return

    if args.backfill:
        files = find_all_csvs(INPUT_DIR)
        if not files:
            print(f"No files matching 'microsoft-*-sq-*.csv' in:\n  {INPUT_DIR}")
            return
        print(f"Backfill: {len(files)} file(s) found")
        for f in files:
            print(f"  {f.name}")
        for csv_path in files:
            print("\n" + "=" * 65)
            _run_load(csv_path, args.dry_run)
        print("\nBackfill complete.")

    elif args.latest:
        csv_path = find_latest_csv(INPUT_DIR)
        if csv_path is None:
            print(f"No files found in:\n  {INPUT_DIR}")
            return
        print(f"Latest file: {csv_path.name}")
        _run_load(csv_path, args.dry_run)

    else:
        csv_path = Path(args.file)
        if not csv_path.exists():
            print(f"ERROR: File not found: {csv_path}")
            return
        _run_load(csv_path, args.dry_run)


if __name__ == "__main__":
    main()
