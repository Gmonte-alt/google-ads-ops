"""
sync_keyword_perf_by_campaign.py
==================================
Syncs keyword_performance_daily for a specific campaign using
  FROM keyword_view WHERE campaign.id = ?

WHY THIS EXISTS
───────────────
The standard 03_sync_keyword_performance.py queries keyword_view without
a campaign filter. The API returns each keyword attributed to its owning
campaign (the campaign that holds the criterion_id). When ad groups are
shared across campaigns (e.g. TIER TWO shares ad groups with Florida CPA),
the keywords' performance is attributed to the owning campaign, not the
campaign whose ad group is running them.

This script adds WHERE campaign.id = ? to keyword_view. This tells the API
to return keyword performance scoped to a specific campaign's ad groups,
regardless of which campaign technically owns each criterion_id. The result
matches what the Google Ads UI shows when you filter the keyword report to
a specific campaign.

Writes to the same keyword_performance_daily table with campaign_id stamped
from the GAQL response (not from keyword_instances). The analysis script
then filters AND campaign_id = ? to get correctly scoped keyword data.

USAGE
─────
    conda activate paid-search

    # Last 90 days for TIER TWO:
    python kw_db_scripts/sync_keyword_perf_by_campaign.py \\
        --account home \\
        --campaign-id 23496334752 \\
        --refresh 90

    # Custom date range:
    python kw_db_scripts/sync_keyword_perf_by_campaign.py \\
        --account home \\
        --campaign-id 23496334752 \\
        --start 2026-01-01 --end 2026-04-08
"""

import os
import sqlite3
import argparse
from pathlib import Path
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
TODAY   = date.today()

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


# ── DB helpers ────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, platform_account_id: str) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts "
        "WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Account '{platform_account_id}' not found.")
    return row["id"]


def get_instance_id(cursor, account_id: int,
                    criterion_id: str) -> int | None:
    row = cursor.execute(
        "SELECT id FROM keyword_instances "
        "WHERE account_id = ? AND criterion_id = ?",
        (account_id, criterion_id),
    ).fetchone()
    return row["id"] if row else None


def load_conversion_action_map(cursor, account_id: int) -> dict:
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode "
        "FROM conversion_actions WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    return {r["name"]: r["include_mode"] for r in rows}


def get_monthly_chunks(start: date, end: date) -> list[tuple[str, str]]:
    chunks, chunk_start = [], start.replace(day=1)
    while chunk_start <= end:
        chunk_end = min(
            (chunk_start + relativedelta(months=1)) - timedelta(days=1),
            end,
        )
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


# ── API helpers ───────────────────────────────────────────────────
def get_google_ads_client(login_customer_id=None):
    client = GoogleAdsClient.load_from_storage()
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def _print_api_error(label, ex):
    print(f"\n  API ERROR in {label}:")
    for error in ex.failure.errors:
        print(f"    [{error.error_code}] {error.message}")


def extract_conversion_action_name(resource, cache, ga_service, customer_id):
    if resource in cache:
        return cache[resource]
    parts     = resource.split("/")
    action_id = parts[-1] if parts else ""
    name      = f"unknown_{action_id}"
    try:
        q = (f"SELECT conversion_action.name FROM conversion_action "
             f"WHERE conversion_action.id = {action_id}")
        response = ga_service.search_stream(customer_id=customer_id, query=q)
        for batch in response:
            for row in batch.results:
                name = row.conversion_action.name
                break
    except Exception:
        pass
    cache[resource] = name
    return name


# ── GAQL queries (campaign-scoped) ────────────────────────────────
def fetch_traffic(ga_service, customer_id: str, campaign_id: str,
                  start: str, end: str) -> list[dict]:
    """Pass 1a: clicks, impressions, cost scoped to one campaign."""
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM keyword_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "criterion_id": str(row.ad_group_criterion.criterion_id),
                    "campaign_id":  str(row.campaign.id),
                    "report_date":  row.segments.date,
                    "clicks":       row.metrics.clicks,
                    "impressions":  row.metrics.impressions,
                    "cost_micros":  row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1a (traffic)", ex)
        raise
    return results


def fetch_conversions(ga_service, customer_id: str, campaign_id: str,
                      start: str, end: str) -> list[dict]:
    """Pass 1b: conversions by action scoped to one campaign."""
    query = f"""
        SELECT
            ad_group_criterion.criterion_id,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions_by_conversion_date,
            metrics.all_conversions_value_by_conversion_date
        FROM keyword_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND ad_group_criterion.type = 'KEYWORD'
            AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "criterion_id":               str(row.ad_group_criterion.criterion_id),
                    "campaign_id":                str(row.campaign.id),
                    "report_date":                row.segments.date,
                    "conversion_action_resource": row.segments.conversion_action,
                    "all_conv_count":             row.metrics.all_conversions_by_conversion_date,
                    "all_conv_value":             row.metrics.all_conversions_value_by_conversion_date,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1b (conversions)", ex)
        raise
    return results


# ── Aggregation ───────────────────────────────────────────────────
def route_and_aggregate(traffic_rows, conversion_rows, conv_map,
                        action_name_cache, ga_service, customer_id) -> dict:
    aggregated = {}

    for row in traffic_rows:
        key = (row["criterion_id"], row["campaign_id"], row["report_date"])
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
        key = (row["criterion_id"], row["campaign_id"], row["report_date"])
        if key not in aggregated:
            aggregated[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        action_name = extract_conversion_action_name(
            row["conversion_action_resource"], action_name_cache,
            ga_service, customer_id,
        )
        mode  = conv_map.get(action_name.lower(), "monitor")
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
        # excluded: skip

    return aggregated


# ── Upsert ────────────────────────────────────────────────────────
def upsert_kpd(conn, account_id: int, aggregated: dict) -> dict:
    cursor = conn.cursor()
    counts = {"written": 0, "skipped": 0}

    for (criterion_id, campaign_id, report_date), m in aggregated.items():
        instance_id = get_instance_id(cursor, account_id, criterion_id)
        if instance_id is None:
            counts["skipped"] += 1
            continue
        cursor.execute("""
            INSERT OR REPLACE INTO keyword_performance_daily
                (instance_id, campaign_id,
                 report_date, device, geo_state, geo_target_constant_id,
                 clicks, impressions, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, ?, 'ALL', 'ALL', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            instance_id, campaign_id, report_date,
            m["clicks"], m["impressions"], m["cost_micros"],
            m["primary_conv_count"], m["primary_conv_value"],
            m["secondary_conv_count"],
            m["all_conv_count"], m["all_conv_value"],
        ))
        counts["written"] += 1

    conn.commit()
    return counts


# ── Args ──────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Sync keyword performance for a specific campaign."
    )
    p.add_argument("--account", required=True,
                   choices=list(ACCOUNT_MAP.keys()),
                   help="Account to sync (e.g. 'home').")
    p.add_argument("--campaign-id", required=True, metavar="ID",
                   help="Google Ads campaign ID (numeric, e.g. 23496334752).")
    p.add_argument("--refresh", type=int, default=90, metavar="N",
                   help="Number of days to pull (default: 90).")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help="Override start date (ignores --refresh).")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="Override end date (default: yesterday).")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────
def main():
    args = parse_args()
    cfg  = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(f"{cfg['customer_id_env']} not set in .env")

    login_cid = (os.getenv(cfg["login_customer_id_env"])
                 if cfg["login_customer_id_env"] else None)

    end_date   = (date.fromisoformat(args.end)
                  if args.end else TODAY - timedelta(days=1))
    start_date = (date.fromisoformat(args.start)
                  if args.start else end_date - timedelta(days=args.refresh - 1))

    chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("sync_keyword_perf_by_campaign.py")
    print(f"  Account    : {cfg['display_name']} ({customer_id})")
    print(f"  Campaign ID: {args.campaign_id}")
    print(f"  Range      : {start_date} → {end_date} "
          f"({(end_date - start_date).days + 1} days)")
    print(f"  Chunks     : {len(chunks)}")
    print("=" * 65)

    conn   = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_id(cursor, customer_id)

    conv_map = load_conversion_action_map(cursor, account_id)
    print(f"\nConversion action map ({len(conv_map)} actions):")
    for name, mode in sorted(conv_map.items()):
        print(f"  [{mode:<16}]  {name}")

    print("\nConnecting to Google Ads API...")
    client     = get_google_ads_client(login_cid)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.\n")

    action_name_cache = {}
    totals = {"written": 0, "skipped": 0,
              "traffic_rows": 0, "conv_rows": 0}

    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        print(f"[{i}/{len(chunks)}] {chunk_start} → {chunk_end}")

        print(f"  Pass 1a (traffic)...", end=" ", flush=True)
        traffic = fetch_traffic(ga_service, customer_id, args.campaign_id,
                                chunk_start, chunk_end)
        print(f"{len(traffic):,} rows")

        print(f"  Pass 1b (conversions)...", end=" ", flush=True)
        convs = fetch_conversions(ga_service, customer_id, args.campaign_id,
                                  chunk_start, chunk_end)
        print(f"{len(convs):,} rows")

        aggregated = route_and_aggregate(
            traffic, convs, conv_map,
            action_name_cache, ga_service, customer_id,
        )
        counts = upsert_kpd(conn, account_id, aggregated)
        print(f"       → {counts['written']:,} written, "
              f"{counts['skipped']:,} skipped")

        totals["traffic_rows"] += len(traffic)
        totals["conv_rows"]    += len(convs)
        totals["written"]      += counts["written"]
        totals["skipped"]      += counts["skipped"]

    # ── Verify ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("SYNC COMPLETE")
    print("=" * 65)
    print(f"  Traffic rows from API  : {totals['traffic_rows']:,}")
    print(f"  Conversion rows        : {totals['conv_rows']:,}")
    print(f"  kpd rows written       : {totals['written']:,}")
    print(f"  kpd rows skipped       : {totals['skipped']:,}")
    print(f"  (skipped = criterion_id not in keyword_instances)")

    verify = conn.execute("""
        SELECT
            COUNT(DISTINCT instance_id) AS kw_slots,
            COUNT(*)                    AS rows,
            SUM(cost_micros)/1e6        AS spend,
            SUM(primary_conv_value)     AS revenue,
            MIN(report_date)            AS min_date,
            MAX(report_date)            AS max_date
        FROM keyword_performance_daily
        WHERE campaign_id = ?
          AND device      = 'ALL'
    """, (args.campaign_id,)).fetchone()

    print(f"\n  kpd verification (campaign {args.campaign_id}, device=ALL):")
    print(f"    Keyword slots : {verify['kw_slots']:,}")
    print(f"    Total rows    : {verify['rows']:,}")
    print(f"    Date range    : {verify['min_date']} → {verify['max_date']}")
    print(f"    Total spend   : ${verify['spend']:,.2f}")
    print(f"    Primary rev   : ${verify['revenue']:,.2f}")

    conn.close()
    print("\nDone.")
    print("\nNext step:")
    print("  python analysis/analysis_tier_two_keywords.py")


if __name__ == "__main__":
    main()
