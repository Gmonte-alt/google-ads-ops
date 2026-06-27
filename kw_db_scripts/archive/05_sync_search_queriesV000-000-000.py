"""
05_sync_search_queries.py  [v2 — multi-account, DB-driven conversion routing]
--------------------------
Syncs Google Ads search term (query) performance into paid_search.db.

Replaces account-specific variants. Pass --account to select which account.

Changes from v1:
  - --account argument drives all account-specific config
  - Conversion routing is read from the conversion_actions table at runtime
    (v1 hardcoded CONV_ACTION_MODES for Home Insurance only -- that bug
    would have silently routed all Medicare/Auto/Life conversions to 'monitor')
  - get_last_synced_date() scoped to the specific account being synced
    (v1 queried all Google rows regardless of account)
  - Standalone account support (AUTO_QUOTE)

Two-pass pattern per date chunk:
    Pass 1a  Traffic metrics (ALL device)
    Pass 1b  Conversion metrics by action -- routed via conversion_actions table
    Pass 2   Device-segmented traffic (no conversions -- API limitation)

Usage:
    conda activate paid-search
    python 05_sync_search_queries.py --account home --backfill
    python 05_sync_search_queries.py --account medicare --backfill
    python 05_sync_search_queries.py --account home --incremental
    python 05_sync_search_queries.py --account medicare --refresh 20
"""

import sqlite3
import os
import argparse
from pathlib import Path
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH      = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
YAML_PATH    = Path(r"C:\Projects\google-ads-ops\google-ads.yaml")

load_dotenv()

BACKFILL_MONTHS  = 12
INCREMENTAL_DAYS = 3
TODAY = date.today()

ACCOUNT_MAP = {
    "home":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_HOME",
        "login_customer_id_env": None,
        "display_name":          "Home Insurance",
    },
    "medicare":   {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_MEDICARE",
        "login_customer_id_env": None,
        "display_name":          "Medicare",
    },
    "auto":       {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO",
        "login_customer_id_env": None,
        "display_name":          "Auto Insurance (autoinsurance.org)",
    },
    "term_life":  {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE",
        "login_customer_id_env": None,
        "display_name":          "Term Life",
    },
    "auto_quote": {
        "customer_id_env":       "GOOGLE_ADS_CUSTOMER_ID_AUTO_QUOTE",
        "login_customer_id_env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE",
        "display_name":          "Auto Insurance (auto.quote.com)",
    },
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync Google Ads search query performance for a single account."
    )
    parser.add_argument(
        "--account", required=True, choices=list(ACCOUNT_MAP.keys()),
        help="Account to sync.",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--backfill", action="store_true",
        help=f"Pull last {BACKFILL_MONTHS} months chunked by month.",
    )
    mode_group.add_argument(
        "--incremental", action="store_true",
        help="Pull from last synced date with 3-day overlap.",
    )
    mode_group.add_argument(
        "--refresh", type=int, metavar="N",
        help="Re-pull last N days, overwriting existing data.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    """
    Creates search_queries and search_query_performance if missing.
    Safe to call every run -- tables are also defined in 01_create_schema.py.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_queries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text          TEXT    NOT NULL,
            normalized_text     TEXT    NOT NULL UNIQUE,
            canonical_query_id  INTEGER REFERENCES search_queries(id),
            theme               TEXT,
            theme_confidence    TEXT    DEFAULT 'untagged',
            first_seen_date     TEXT,
            last_seen_date      TEXT,
            first_seen_platform TEXT,
            notes               TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sq_normalized ON search_queries(normalized_text);
        CREATE INDEX IF NOT EXISTS idx_sq_canonical  ON search_queries(canonical_query_id);

        CREATE TABLE IF NOT EXISTS search_query_performance (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id             INTEGER NOT NULL REFERENCES search_queries(id),
            instance_id          INTEGER NOT NULL REFERENCES keyword_instances(id),
            platform             TEXT    NOT NULL,
            report_date          TEXT    NOT NULL,
            device               TEXT    NOT NULL,
            match_type_served    TEXT,
            impressions          INTEGER DEFAULT 0,
            clicks               INTEGER DEFAULT 0,
            cost_micros          REAL    DEFAULT 0,
            primary_conv_count   REAL    DEFAULT 0,
            primary_conv_value   REAL    DEFAULT 0,
            secondary_conv_count REAL    DEFAULT 0,
            all_conv_count       REAL    DEFAULT 0,
            all_conv_value       REAL    DEFAULT 0,
            UNIQUE (query_id, instance_id, report_date, device)
        );
        CREATE INDEX IF NOT EXISTS idx_sqp_query_date    ON search_query_performance(query_id, report_date);
        CREATE INDEX IF NOT EXISTS idx_sqp_platform_date ON search_query_performance(platform, report_date);
        CREATE INDEX IF NOT EXISTS idx_sqp_instance_date ON search_query_performance(instance_id, report_date);
        CREATE INDEX IF NOT EXISTS idx_sqp_date_device   ON search_query_performance(report_date, device);
    """)
    conn.commit()


def load_conversion_action_map(cursor, account_id: int) -> dict:
    """
    Loads conversion routing from the conversion_actions table.
    Returns { conversion_name_lowercase: include_mode }.

    This replaces the hardcoded CONV_ACTION_MODES dict from v1, which only
    covered Home Insurance and would silently misroute all conversions for
    any other account to 'monitor'.
    """
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode FROM conversion_actions WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    if not rows:
        print(
            "  WARNING: No conversion actions found for this account in conversion_actions table.\n"
            "  All conversions will be treated as 'monitor' (all_conv_* only).\n"
            "  Run 01_create_schema.py to seed conversion actions, then re-run this script."
        )
    return {row["name"]: row["include_mode"] for row in rows}


def get_or_create_query(cursor, query_text, report_date, platform) -> int:
    """
    Returns the search_queries.id for the given query text.
    Cross-platform, cross-account identity: same normalized text anywhere
    resolves to the same query_id.
    """
    normalized = query_text.lower().strip()
    row = cursor.execute(
        "SELECT id FROM search_queries WHERE normalized_text = ?", (normalized,)
    ).fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO search_queries (query_text, normalized_text, first_seen_date, last_seen_date, first_seen_platform) VALUES (?, ?, ?, ?, ?)",
            (query_text, normalized, report_date, report_date, platform),
        )
        return cursor.lastrowid
    else:
        cursor.execute(
            "UPDATE search_queries SET last_seen_date = MAX(last_seen_date, ?) WHERE id = ?",
            (report_date, row["id"]),
        )
        return row["id"]


def get_instance_id(cursor, account_id, criterion_id) -> int | None:
    row = cursor.execute(
        "SELECT id FROM keyword_instances WHERE account_id = ? AND criterion_id = ?",
        (account_id, criterion_id),
    ).fetchone()
    return row["id"] if row else None


def get_account_id(cursor, customer_id) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (customer_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Account {customer_id} not found. Run 01_create_schema.py first."
        )
    return row["id"]


# ---------------------------------------------------------------------------
# Google Ads client
# ---------------------------------------------------------------------------

def get_google_ads_client(login_customer_id=None):
    client = GoogleAdsClient.load_from_storage(str(YAML_PATH)) if YAML_PATH.exists() \
             else GoogleAdsClient.load_from_storage()
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def _print_api_error(label, ex):
    print(f"\n  API ERROR in {label}:")
    for error in ex.failure.errors:
        print(f"    [{error.error_code}] {error.message}")


# ---------------------------------------------------------------------------
# API queries
# ---------------------------------------------------------------------------

def fetch_query_traffic(ga_service, customer_id, start_date, end_date):
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            segments.date,
            segments.search_term_match_type,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM search_term_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                resource     = row.segments.keyword.ad_group_criterion
                criterion_id = resource.split("~")[-1] if "~" in resource else ""
                results.append({
                    "query_text":        row.search_term_view.search_term,
                    "criterion_id":      criterion_id,
                    "report_date":       row.segments.date,
                    "match_type_served": row.segments.search_term_match_type.name,
                    "ad_network_type":   row.segments.ad_network_type.name,
                    "clicks":            row.metrics.clicks,
                    "impressions":       row.metrics.impressions,
                    "cost_micros":       row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1a query traffic", ex); raise
    return results


def fetch_query_conversions(ga_service, customer_id, start_date, end_date):
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            segments.date,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions,
            metrics.all_conversions_value
        FROM search_term_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
            AND metrics.all_conversions > 0
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                resource     = row.segments.keyword.ad_group_criterion
                criterion_id = resource.split("~")[-1] if "~" in resource else ""
                results.append({
                    "query_text":        row.search_term_view.search_term,
                    "criterion_id":      criterion_id,
                    "report_date":       row.segments.date,
                    "ad_network_type":   row.segments.ad_network_type.name,
                    "conversion_action": row.segments.conversion_action,
                    "all_conv_count":    row.metrics.all_conversions,
                    "all_conv_value":    row.metrics.all_conversions_value,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1b query conversions", ex); raise
    return results


def fetch_query_device_traffic(ga_service, customer_id, start_date, end_date):
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            segments.date,
            segments.device,
            segments.search_term_match_type,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM search_term_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
            AND segments.device IN ('DESKTOP', 'MOBILE', 'TABLET')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                resource     = row.segments.keyword.ad_group_criterion
                criterion_id = resource.split("~")[-1] if "~" in resource else ""
                results.append({
                    "query_text":        row.search_term_view.search_term,
                    "criterion_id":      criterion_id,
                    "report_date":       row.segments.date,
                    "device":            row.segments.device.name,
                    "match_type_served": row.segments.search_term_match_type.name,
                    "ad_network_type":   row.segments.ad_network_type.name,
                    "clicks":            row.metrics.clicks,
                    "impressions":       row.metrics.impressions,
                    "cost_micros":       row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 2 query device", ex); raise
    return results


# ---------------------------------------------------------------------------
# Conversion action name resolution
# ---------------------------------------------------------------------------

def resolve_conversion_action_name(resource_name, cache, ga_service, customer_id):
    if resource_name in cache:
        return cache[resource_name]
    try:
        q = f"SELECT conversion_action.name FROM conversion_action WHERE conversion_action.resource_name = '{resource_name}'"
        response = ga_service.search_stream(customer_id=customer_id, query=q)
        for batch in response:
            for row in batch.results:
                name = row.conversion_action.name.lower()
                cache[resource_name] = name
                return name
    except Exception:
        pass
    cache[resource_name] = "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# Aggregation and write: Pass 1
# ---------------------------------------------------------------------------

def aggregate_and_write_pass1(
    conn, account_id, traffic_rows, conversion_rows,
    conv_action_map, conv_action_cache, ga_service, customer_id
) -> dict:
    cursor = conn.cursor()
    counts = {"inserted": 0, "skipped": 0, "new_queries": 0}

    # Aggregate traffic across SEARCH + SEARCH_PARTNERS
    traffic = {}
    for row in traffic_rows:
        key = (row["query_text"], row["criterion_id"], row["report_date"], row["match_type_served"])
        if key not in traffic:
            traffic[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        traffic[key]["clicks"]      += row["clicks"]
        traffic[key]["impressions"] += row["impressions"]
        traffic[key]["cost_micros"] += row["cost_micros"]

    # Route conversions
    for row in conversion_rows:
        key_base     = (row["query_text"], row["criterion_id"], row["report_date"])
        matching_keys = [k for k in traffic if k[:3] == key_base]
        if not matching_keys:
            key = key_base + ("UNKNOWN",)
            traffic[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
            matching_keys = [key]

        action_name = resolve_conversion_action_name(
            row["conversion_action"], conv_action_cache, ga_service, customer_id
        )
        mode = conv_action_map.get(action_name, "monitor")

        for k in matching_keys:
            if mode == "primary":
                traffic[k]["primary_conv_count"] += row["all_conv_count"]
                traffic[k]["primary_conv_value"] += row["all_conv_value"]
                traffic[k]["all_conv_count"]     += row["all_conv_count"]
                traffic[k]["all_conv_value"]     += row["all_conv_value"]
            elif mode == "secondary_count":
                traffic[k]["secondary_conv_count"] += row["all_conv_count"]
                traffic[k]["all_conv_count"]       += row["all_conv_count"]
            elif mode == "monitor":
                traffic[k]["all_conv_count"] += row["all_conv_count"]
                traffic[k]["all_conv_value"] += row["all_conv_value"]
            # excluded: not counted

    # Write
    for (query_text, criterion_id, report_date, match_type_served), metrics in traffic.items():
        instance_id = get_instance_id(cursor, account_id, criterion_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue

        prev = cursor.execute(
            "SELECT COUNT(*) FROM search_queries WHERE normalized_text = ?",
            (query_text.lower().strip(),)
        ).fetchone()[0]
        query_id = get_or_create_query(cursor, query_text, report_date, "google")
        if prev == 0:
            counts["new_queries"] += 1

        cursor.execute(
            """
            INSERT INTO search_query_performance
                (query_id, instance_id, platform, report_date, device,
                 match_type_served,
                 impressions, clicks, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, 'google', ?, 'ALL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (query_id, instance_id, report_date, device) DO UPDATE SET
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
            (query_id, instance_id, report_date, match_type_served,
             metrics["impressions"], metrics["clicks"], metrics["cost_micros"],
             metrics["primary_conv_count"], metrics["primary_conv_value"],
             metrics["secondary_conv_count"],
             metrics["all_conv_count"], metrics["all_conv_value"]),
        )
        counts["inserted"] += 1

    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Aggregation and write: Pass 2
# ---------------------------------------------------------------------------

def aggregate_and_write_pass2(conn, account_id, device_rows) -> dict:
    cursor = conn.cursor()
    counts = {"inserted": 0, "skipped": 0}

    aggregated = {}
    for row in device_rows:
        device = row["device"]
        if device in ("UNSPECIFIED", "UNKNOWN", ""):
            continue
        key = (row["query_text"], row["criterion_id"], row["report_date"],
               device, row["match_type_served"])
        if key not in aggregated:
            aggregated[key] = {"clicks": 0, "impressions": 0, "cost_micros": 0.0}
        aggregated[key]["clicks"]      += row["clicks"]
        aggregated[key]["impressions"] += row["impressions"]
        aggregated[key]["cost_micros"] += row["cost_micros"]

    for (query_text, criterion_id, report_date, device, match_type_served), metrics in aggregated.items():
        instance_id = get_instance_id(cursor, account_id, criterion_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue

        query_id = get_or_create_query(cursor, query_text, report_date, "google")
        cursor.execute(
            """
            INSERT INTO search_query_performance
                (query_id, instance_id, platform, report_date, device,
                 match_type_served, impressions, clicks, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, 'google', ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
            ON CONFLICT (query_id, instance_id, report_date, device) DO UPDATE SET
                match_type_served = excluded.match_type_served,
                impressions       = excluded.impressions,
                clicks            = excluded.clicks,
                cost_micros       = excluded.cost_micros
            """,
            (query_id, instance_id, report_date, device, match_type_served,
             metrics["impressions"], metrics["clicks"], metrics["cost_micros"]),
        )
        counts["inserted"] += 1

    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def get_monthly_chunks(start, end):
    chunks = []
    chunk_start = start.replace(day=1)
    while chunk_start <= end:
        chunk_end = (chunk_start + relativedelta(months=1)) - timedelta(days=1)
        chunk_end = min(chunk_end, end)
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


def get_last_synced_date(conn, account_id: int) -> date | None:
    """
    Returns the most recent report_date in search_query_performance for
    this specific account. Scoped by account_id to avoid cross-contamination
    between accounts (v1 queried all Google rows regardless of account).
    """
    row = conn.cursor().execute("""
        SELECT MAX(sqp.report_date) AS last_date
        FROM search_query_performance sqp
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE ki.account_id = ? AND sqp.platform = 'google' AND sqp.device = 'ALL'
    """, (account_id,)).fetchone()
    if row and row["last_date"]:
        return date.fromisoformat(row["last_date"])
    return None


def get_incremental_range(conn, account_id, overlap_days=3):
    end_date  = TODAY - timedelta(days=1)
    last_date = get_last_synced_date(conn, account_id)
    if last_date:
        start_date  = last_date - timedelta(days=overlap_days)
        description = (
            f"INCREMENTAL — last synced {last_date.isoformat()}, "
            f"pulling from {start_date.isoformat()} ({overlap_days}-day overlap)"
        )
    else:
        start_date  = end_date - timedelta(days=INCREMENTAL_DAYS - 1)
        description = f"INCREMENTAL — no existing data, falling back to last {INCREMENTAL_DAYS} days"
    return start_date, end_date, description


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args        = parse_args()
    account_cfg = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(account_cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(f"{account_cfg['customer_id_env']} not set in .env")

    login_customer_id = None
    if account_cfg["login_customer_id_env"]:
        login_customer_id = os.getenv(account_cfg["login_customer_id_env"])

    conn   = get_conn()
    cursor = conn.cursor()

    print("\nEnsuring schema...")
    ensure_schema(conn)

    account_id = get_account_id(cursor, customer_id)
    print(f"  Account id: {account_id} ({customer_id})")

    conv_action_map = load_conversion_action_map(cursor, account_id)
    print(f"\nConversion action map ({len(conv_action_map)} actions):")
    for name, mode_val in sorted(conv_action_map.items()):
        print(f"  [{mode_val:<16}]  {name}")

    # Date range
    if args.backfill:
        end_date   = TODAY - timedelta(days=1)
        start_date = (end_date - relativedelta(months=BACKFILL_MONTHS)).replace(day=1)
        mode_desc  = f"BACKFILL — {BACKFILL_MONTHS} months: {start_date.isoformat()} to {end_date.isoformat()}"
    elif args.refresh:
        end_date   = TODAY - timedelta(days=1)
        start_date = TODAY - timedelta(days=args.refresh)
        mode_desc  = f"REFRESH — last {args.refresh} days: {start_date.isoformat()} to {end_date.isoformat()}"
    else:
        start_date, end_date, mode_desc = get_incremental_range(conn, account_id)

    date_chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("05_sync_search_queries.py")
    print(f"Account  : {account_cfg['display_name']} ({customer_id})")
    print(f"Mode     : {mode_desc}")
    print(f"Chunks   : {len(date_chunks)} month(s)")
    print(f"Date     : {TODAY.isoformat()}")
    print("=" * 65)

    print("\nConnecting to Google Ads API...")
    client     = get_google_ads_client(login_customer_id)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.")

    conv_action_cache = {}
    totals = {
        "p1_traffic": 0, "p1_conv": 0,
        "p1_inserted": 0, "p1_skipped": 0, "new_queries": 0,
        "p2_inserted": 0, "p2_skipped": 0,
    }

    for i, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
        print(f"\n[{i}/{len(date_chunks)}] {chunk_start} → {chunk_end}")

        print(f"  Pass 1a (query traffic)...", end=" ", flush=True)
        traffic_rows = fetch_query_traffic(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(traffic_rows):,} rows")

        print(f"  Pass 1b (query conversions)...", end=" ", flush=True)
        conv_rows = fetch_query_conversions(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(conv_rows):,} rows")

        p1 = aggregate_and_write_pass1(
            conn, account_id, traffic_rows, conv_rows,
            conv_action_map, conv_action_cache, ga_service, customer_id
        )
        totals["p1_traffic"]  += len(traffic_rows)
        totals["p1_conv"]     += len(conv_rows)
        totals["p1_inserted"] += p1["inserted"]
        totals["p1_skipped"]  += p1["skipped"]
        totals["new_queries"] += p1["new_queries"]
        print(f"         -> {p1['inserted']:,} ALL-device rows  "
              f"({p1['skipped']:,} skipped, {p1['new_queries']:,} new queries)")

        print(f"  Pass 2  (device traffic)...", end=" ", flush=True)
        device_rows = fetch_query_device_traffic(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(device_rows):,} rows")

        p2 = aggregate_and_write_pass2(conn, account_id, device_rows)
        totals["p2_inserted"] += p2["inserted"]
        totals["p2_skipped"]  += p2["skipped"]
        print(f"         -> {p2['inserted']:,} device rows  ({p2['skipped']:,} skipped)")

    # Summary
    total_queries   = cursor.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
    total_perf_rows = cursor.execute("SELECT COUNT(*) FROM search_query_performance").fetchone()[0]
    date_range      = cursor.execute(
        "SELECT MIN(sqp.report_date), MAX(sqp.report_date) FROM search_query_performance sqp JOIN keyword_instances ki ON sqp.instance_id = ki.id WHERE ki.account_id = ? AND sqp.device = 'ALL'",
        (account_id,),
    ).fetchone()
    top_revenue     = cursor.execute(
        """
        SELECT sq.query_text, SUM(sqp.primary_conv_value) AS revenue, SUM(sqp.clicks) AS clicks
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        JOIN keyword_instances ki ON sqp.instance_id = ki.id
        WHERE sqp.device = 'ALL' AND ki.account_id = ?
        GROUP BY sqp.query_id ORDER BY revenue DESC LIMIT 10
        """,
        (account_id,),
    ).fetchall()

    print("\n" + "=" * 65)
    print("SYNC COMPLETE")
    print("=" * 65)
    print(f"  Pass 1a traffic rows         : {totals['p1_traffic']:>10,}")
    print(f"  Pass 1b conversion rows      : {totals['p1_conv']:>10,}")
    print(f"  Pass 1 written (ALL device)  : {totals['p1_inserted']:>10,}")
    print(f"  Pass 1 skipped               : {totals['p1_skipped']:>10,}")
    print(f"  Pass 2 written (device)      : {totals['p2_inserted']:>10,}")
    print(f"  Pass 2 skipped               : {totals['p2_skipped']:>10,}")
    print(f"  New query identities         : {totals['new_queries']:>10,}")
    print(f"\n  Total unique queries (all)   : {total_queries:>10,}")
    print(f"  Total performance rows (all) : {total_perf_rows:>10,}")
    print(f"  Account date range           : {date_range[0]} to {date_range[1]}")

    if top_revenue:
        print(f"\n  Top 10 queries by primary revenue ({args.account}):")
        for r in top_revenue:
            print(f"    ${r['revenue']:>10,.2f}  {r['clicks']:>6,} clicks  '{r['query_text']}'")

    conn.close()
    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\n  Database size: {size_mb:.2f} MB  |  Path: {DB_PATH}")
    print("\nDone.")


if __name__ == "__main__":
    main()
