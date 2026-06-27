"""
03_sync_keyword_performance.py  [v2 — multi-account]
------------------------------
Pulls keyword-level performance metrics for any account from the Google
Ads API and upserts them into keyword_performance_daily.

Replaces account-specific variants. Pass --account to select which account.

Three segmentation passes per date chunk:
    Pass 1a  Traffic metrics (clicks, impressions, cost) -- device='ALL'
    Pass 1b  Conversion metrics by action -- same routing as keyword sync
    Pass 2   Device-segmented traffic

Conversion routing is driven entirely by the conversion_actions table
(seeded by 01_create_schema.py). No hardcoded action names in this script.

Run modes:
    Backfill (first run / forced):  last 12 months, chunked by month
    Incremental (default):          last 3 days (covers late offline uploads)
    Refresh (targeted):             last N days

Usage:
    conda activate paid-search
    python 03_sync_keyword_performance.py --account home
    python 03_sync_keyword_performance.py --account medicare --backfill
    python 03_sync_keyword_performance.py --account home --refresh 20
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

load_dotenv()
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = os.getenv(
    "GOOGLE_ADS_CONFIGURATION_FILE_PATH"
)

DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TODAY            = date.today()
BACKFILL_MONTHS  = 12
INCREMENTAL_DAYS = 3

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
        description="Sync keyword performance for a single Google Ads account."
    )
    parser.add_argument(
        "--account", required=True, choices=list(ACCOUNT_MAP.keys()),
        help="Account to sync.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help=f"Force a full {BACKFILL_MONTHS}-month backfill.",
    )
    parser.add_argument(
        "--refresh", type=int, default=None, metavar="N",
        help="Re-pull the last N days. Use after offline conversion uploads.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, platform_account_id: str) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Account '{platform_account_id}' not found. Run 01_create_schema.py first.")
    return row["id"]


def get_instance_id(cursor, account_id: int,
                    criterion_id: str, ad_group_id: str) -> int | None:
    """
    Resolve keyword_instances.id using the full composite key.
    Using criterion_id alone is ambiguous — the same criterion_id appears
    across multiple ad groups and campaigns (e.g. 102 EXACT instances all
    share criterion_id=19000971). Without ad_group_id, performance rows
    get written to whichever instance sorts first, misattributing BROAD
    performance to EXACT instances.
    """
    row = cursor.execute(
        """
        SELECT id FROM keyword_instances
        WHERE account_id  = ?
          AND ad_group_id = ?
          AND criterion_id = ?
        """,
        (account_id, ad_group_id, criterion_id),
    ).fetchone()
    return row["id"] if row else None


def load_conversion_action_map(cursor, account_id: int) -> dict:
    """
    Loads conversion routing from the conversion_actions table.
    Returns { conversion_name_lowercase: include_mode }.
    All routing decisions live in the DB -- no hardcoded names here.
    """
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode FROM conversion_actions WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    return {row["name"]: row["include_mode"] for row in rows}


def has_existing_performance_data(cursor, account_id: int) -> bool:
    row = cursor.execute(
        """
        SELECT COUNT(*) AS n FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON kpd.instance_id = ki.id
        WHERE ki.account_id = ? LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    return row["n"] > 0


# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def get_monthly_chunks(start_date: date, end_date: date) -> list[tuple[str, str]]:
    chunks = []
    chunk_start = start_date.replace(day=1)
    while chunk_start <= end_date:
        chunk_end = (chunk_start + relativedelta(months=1)) - timedelta(days=1)
        chunk_end = min(chunk_end, end_date)
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


# ---------------------------------------------------------------------------
# Google Ads client
# ---------------------------------------------------------------------------

def get_google_ads_client(login_customer_id: str | None = None) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


# ---------------------------------------------------------------------------
# API queries
# ---------------------------------------------------------------------------

def fetch_traffic_metrics(ga_service, customer_id, start_date, end_date):
    """Pass 1a: clicks, impressions, cost -- no conversion segment."""
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            ad_group.id,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM keyword_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "criterion_id": str(row.ad_group_criterion.criterion_id),
                    "ad_group_id":  str(row.ad_group.id),
                    "campaign_id":  str(row.campaign.id),
                    "report_date":  row.segments.date,
                    "clicks":       row.metrics.clicks,
                    "impressions":  row.metrics.impressions,
                    "cost_micros":  row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1a (traffic)", ex); raise
    return results


def fetch_conversion_metrics(ga_service, customer_id, start_date, end_date):
    """Pass 1b: conversion counts and values segmented by conversion_action."""
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            ad_group.id,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions_by_conversion_date,
            metrics.all_conversions_value_by_conversion_date
        FROM keyword_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "criterion_id":               str(row.ad_group_criterion.criterion_id),
                    "ad_group_id":                str(row.ad_group.id),
                    "campaign_id":                str(row.campaign.id),
                    "report_date":                row.segments.date,
                    "conversion_action_resource": row.segments.conversion_action,
                    "all_conv_count":             row.metrics.all_conversions_by_conversion_date,
                    "all_conv_value":             row.metrics.all_conversions_value_by_conversion_date,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1b (conversions)", ex); raise
    return results


def fetch_device_performance(ga_service, customer_id, start_date, end_date):
    """Pass 2: device-segmented traffic."""
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            ad_group.id,
            segments.date,
            segments.device,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros,
            metrics.all_conversions_by_conversion_date,
            metrics.all_conversions_value_by_conversion_date
        FROM keyword_view
        WHERE
            segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "criterion_id": str(row.ad_group_criterion.criterion_id),
                    "ad_group_id":  str(row.ad_group.id),
                    "report_date":  row.segments.date,
                    "device":       row.segments.device.name,
                    "clicks":       row.metrics.clicks,
                    "impressions":  row.metrics.impressions,
                    "cost_micros":  row.metrics.cost_micros,
                    "all_conv_count": row.metrics.all_conversions_by_conversion_date,
                    "all_conv_value": row.metrics.all_conversions_value_by_conversion_date,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 2 (device)", ex); raise
    return results


def _print_api_error(label, ex):
    print(f"\n  API ERROR in {label}:")
    for error in ex.failure.errors:
        print(f"    [{error.error_code}] {error.message}")


# ---------------------------------------------------------------------------
# Conversion action name resolution
# ---------------------------------------------------------------------------

def extract_conversion_action_name(resource, cache, ga_service, customer_id):
    if resource in cache:
        return cache[resource]
    parts     = resource.split("/")
    action_id = parts[-1] if parts else ""
    name      = f"unknown_{action_id}"
    try:
        q = f"SELECT conversion_action.name FROM conversion_action WHERE conversion_action.id = {action_id}"
        response = ga_service.search_stream(customer_id=customer_id, query=q)
        for batch in response:
            for row in batch.results:
                name = row.conversion_action.name
                break
    except Exception:
        pass
    cache[resource] = name
    return name


# ---------------------------------------------------------------------------
# Aggregation and routing
# ---------------------------------------------------------------------------

def route_and_aggregate_conversions(
    traffic_rows, conversion_rows, conv_action_map, action_name_cache, ga_service, customer_id
):
    aggregated = {}

    for row in traffic_rows:
        key = (row["criterion_id"], row["ad_group_id"], row["campaign_id"], row["report_date"])
        if key not in aggregated:
            aggregated[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        aggregated[key]["clicks"]      += row["clicks"]
        aggregated[key]["impressions"] += row["impressions"]
        aggregated[key]["cost_micros"] += row["cost_micros"]

    for row in conversion_rows:
        key = (row["criterion_id"], row["ad_group_id"], row["campaign_id"], row["report_date"])
        if key not in aggregated:
            aggregated[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }

        action_name = extract_conversion_action_name(
            row["conversion_action_resource"], action_name_cache, ga_service, customer_id
        )
        mode = conv_action_map.get(action_name.lower(), "monitor")

        count = row["all_conv_count"]
        value = row["all_conv_value"]

        if mode == "primary":
            aggregated[key]["primary_conv_count"] += count
            aggregated[key]["primary_conv_value"] += value
            aggregated[key]["all_conv_count"]     += count
            aggregated[key]["all_conv_value"]     += value
        elif mode == "secondary_count":
            aggregated[key]["secondary_conv_count"] += count
            aggregated[key]["all_conv_count"]       += count
        elif mode == "monitor":
            aggregated[key]["all_conv_count"] += count
            aggregated[key]["all_conv_value"] += value
        # excluded: skip entirely

    return aggregated


# ---------------------------------------------------------------------------
# Upsert functions
# ---------------------------------------------------------------------------

def upsert_unsegmented(conn, account_id, aggregated):
    cursor = conn.cursor()
    counts = {"inserted": 0, "skipped": 0}
    for (criterion_id, ad_group_id, campaign_id, report_date), metrics in aggregated.items():
        instance_id = get_instance_id(cursor, account_id, criterion_id, ad_group_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue
        cursor.execute(
            """
            INSERT OR REPLACE INTO keyword_performance_daily
                (instance_id, campaign_id,
                 report_date, device, geo_state, geo_target_constant_id,
                 clicks, impressions, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, ?, 'ALL', 'ALL', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (instance_id, campaign_id, report_date,
             metrics["clicks"], metrics["impressions"], metrics["cost_micros"],
             metrics["primary_conv_count"], metrics["primary_conv_value"],
             metrics["secondary_conv_count"],
             metrics["all_conv_count"], metrics["all_conv_value"]),
        )
        counts["inserted"] += 1
    conn.commit()
    return counts


def upsert_device_performance(conn, account_id, raw_rows):
    cursor = conn.cursor()
    counts = {"inserted": 0, "skipped": 0}

    aggregated = {}
    for row in raw_rows:
        device = row["device"]
        if device in ("UNSPECIFIED", "UNKNOWN", ""):
            continue
        key = (row["criterion_id"], row["ad_group_id"], row["report_date"], device)
        if key not in aggregated:
            aggregated[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        aggregated[key]["clicks"]         += row["clicks"]
        aggregated[key]["impressions"]    += row["impressions"]
        aggregated[key]["cost_micros"]    += row["cost_micros"]
        aggregated[key]["all_conv_count"] += row["all_conv_count"]
        aggregated[key]["all_conv_value"] += row["all_conv_value"]

    for (criterion_id, ad_group_id, report_date, device), metrics in aggregated.items():
        instance_id = get_instance_id(cursor, account_id, criterion_id, ad_group_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue
        cursor.execute(
            """
            INSERT OR REPLACE INTO keyword_performance_daily
                (instance_id, report_date, device, geo_state, geo_target_constant_id,
                 clicks, impressions, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, ?, 'ALL', NULL, ?, ?, ?, 0, 0, 0, ?, ?)
            """,
            (instance_id, report_date, device,
             metrics["clicks"], metrics["impressions"], metrics["cost_micros"],
             metrics["all_conv_count"], metrics["all_conv_value"]),
        )
        counts["inserted"] += 1

    conn.commit()
    return counts


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

    conn   = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_id(cursor, customer_id)

    # Determine run mode
    if args.refresh:
        mode       = f"REFRESH — last {args.refresh} days"
        start_date = TODAY - timedelta(days=args.refresh)
        end_date   = TODAY - timedelta(days=1)
    elif args.backfill or not has_existing_performance_data(cursor, account_id):
        mode       = "BACKFILL"
        start_date = (TODAY - relativedelta(months=BACKFILL_MONTHS)).replace(day=1)
        end_date   = TODAY - timedelta(days=1)
    else:
        mode       = "INCREMENTAL"
        start_date = TODAY - timedelta(days=INCREMENTAL_DAYS)
        end_date   = TODAY - timedelta(days=1)

    date_chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("03_sync_keyword_performance.py")
    print(f"Account  : {account_cfg['display_name']} ({customer_id})")
    print(f"Mode     : {mode}")
    print(f"Range    : {start_date.isoformat()} to {end_date.isoformat()}")
    print(f"Chunks   : {len(date_chunks)} month(s)")
    print(f"Date     : {TODAY.isoformat()}")
    print("=" * 65)

    conv_action_map = load_conversion_action_map(cursor, account_id)
    print(f"\nConversion action map ({len(conv_action_map)} actions):")
    for name, mode_val in sorted(conv_action_map.items()):
        print(f"  [{mode_val:<16}]  {name}")

    print("\nConnecting to Google Ads API...")
    client     = get_google_ads_client(login_customer_id)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.")

    action_name_cache = {}
    totals = {
        "pass1_rows": 0, "pass1_written": 0, "pass1_skipped": 0,
        "pass2_written": 0, "pass2_skipped": 0,
    }

    for i, (chunk_start, chunk_end) in enumerate(date_chunks, 1):
        print(f"\n[{i}/{len(date_chunks)}] {chunk_start} → {chunk_end}")

        print(f"  Pass 1a (traffic)...", end=" ", flush=True)
        traffic_rows = fetch_traffic_metrics(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(traffic_rows):,} rows")

        print(f"  Pass 1b (conversions)...", end=" ", flush=True)
        conversion_rows = fetch_conversion_metrics(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(conversion_rows):,} rows")

        aggregated = route_and_aggregate_conversions(
            traffic_rows, conversion_rows,
            conv_action_map, action_name_cache, ga_service, customer_id
        )
        p1 = upsert_unsegmented(conn, account_id, aggregated)
        totals["pass1_rows"]    += len(traffic_rows) + len(conversion_rows)
        totals["pass1_written"] += p1["inserted"]
        totals["pass1_skipped"] += p1["skipped"]
        print(f"         -> {p1['inserted']:,} rows written, {p1['skipped']:,} skipped")

        print(f"  Pass 2 (device)...", end=" ", flush=True)
        raw_p2 = fetch_device_performance(ga_service, customer_id, chunk_start, chunk_end)
        print(f"{len(raw_p2):,} rows")
        p2 = upsert_device_performance(conn, account_id, raw_p2)
        totals["pass2_written"] += p2["inserted"]
        totals["pass2_skipped"] += p2["skipped"]
        print(f"         -> {p2['inserted']:,} rows written, {p2['skipped']:,} skipped")

    print("\n" + "=" * 65)
    print("SYNC COMPLETE")
    print("=" * 65)
    print(f"  Pass 1 raw API rows   : {totals['pass1_rows']:>10,}")
    print(f"  Pass 1 written        : {totals['pass1_written']:>10,}")
    print(f"  Pass 1 skipped        : {totals['pass1_skipped']:>10,}")
    print(f"  Pass 2 written        : {totals['pass2_written']:>10,}")
    print(f"  Pass 2 skipped        : {totals['pass2_skipped']:>10,}")

    total_rows  = cursor.execute(
        "SELECT COUNT(*) FROM keyword_performance_daily kpd JOIN keyword_instances ki ON kpd.instance_id = ki.id WHERE ki.account_id = ?",
        (account_id,),
    ).fetchone()[0]
    date_range  = cursor.execute(
        "SELECT MIN(report_date), MAX(report_date) FROM keyword_performance_daily kpd JOIN keyword_instances ki ON kpd.instance_id = ki.id WHERE ki.account_id = ? AND kpd.device = 'ALL' AND kpd.geo_state = 'ALL'",
        (account_id,),
    ).fetchone()
    revenue     = cursor.execute(
        "SELECT SUM(primary_conv_count), SUM(primary_conv_value), SUM(cost_micros)/1e6 FROM keyword_performance_daily kpd JOIN keyword_instances ki ON kpd.instance_id = ki.id WHERE ki.account_id = ? AND kpd.device = 'ALL' AND kpd.geo_state = 'ALL'",
        (account_id,),
    ).fetchone()

    print(f"\n  Total performance rows : {total_rows:>10,}")
    print(f"  Date range             : {date_range[0]} to {date_range[1]}")
    print(f"  Primary conversions    : {revenue[0]:>10,.1f}")
    print(f"  Primary revenue        : ${revenue[1]:>10,.2f}")
    print(f"  Total spend            : ${revenue[2]:>10,.2f}")

    conn.close()
    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"\n  Database size: {size_mb:.2f} MB  |  Path: {DB_PATH}")
    print(f"\nDone. Next: python 05_sync_search_queries.py --account {args.account} --backfill")


if __name__ == "__main__":
    main()
