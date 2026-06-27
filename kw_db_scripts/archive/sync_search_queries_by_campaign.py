"""
sync_search_queries_by_campaign.py  [v2 — corrected composite key]
=====================================
Syncs search_query_performance for a specific campaign using
  FROM search_term_view WHERE campaign.id = ?

Changes from v1:
  - criterion_id and ad_group_id are now both extracted from the
    segments.keyword.ad_group_criterion resource name string:
    format: customers/{cid}/adGroups/{ag_id}/criteria/{criterion_id}
    This gives us both values without adding an extra GAQL field.
  - get_instance_id() updated to use the corrected composite key
    (account_id, ad_group_id, criterion_id).
  - Aggregation key now includes ad_group_id to correctly differentiate
    keyword deployments across campaigns that share ad group names.

RUN
───
    python kw_db_scripts/sync_search_queries_by_campaign.py \\
        --account home --campaign-id 23496334752 --refresh 90

    # Full backfill:
    python kw_db_scripts/sync_search_queries_by_campaign.py \\
        --account home --campaign-id 23496334752 --start 2025-01-01
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

DB_PATH   = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
YAML_PATH = Path(r"C:\Projects\google-ads-ops\google-ads.yaml")
TODAY     = date.today()

ACCOUNT_MAP = {
    "home":       {"customer_id_env": "GOOGLE_ADS_CUSTOMER_ID_HOME",
                   "login_customer_id_env": None,
                   "display_name": "Home Insurance"},
    "medicare":   {"customer_id_env": "GOOGLE_ADS_CUSTOMER_ID_MEDICARE",
                   "login_customer_id_env": None,
                   "display_name": "Medicare"},
    "auto":       {"customer_id_env": "GOOGLE_ADS_CUSTOMER_ID_AUTO",
                   "login_customer_id_env": None,
                   "display_name": "Auto Insurance (autoinsurance.org)"},
    "term_life":  {"customer_id_env": "GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE",
                   "login_customer_id_env": None,
                   "display_name": "Term Life"},
    "auto_quote": {"customer_id_env": "GOOGLE_ADS_CUSTOMER_ID_AUTO_QUOTE",
                   "login_customer_id_env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE",
                   "display_name": "Auto Insurance (auto.quote.com)"},
}


# ── DB helpers ────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, customer_id) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts "
        "WHERE platform_account_id = ? AND platform = 'google'",
        (customer_id,)
    ).fetchone()
    if not row:
        raise RuntimeError(f"Account {customer_id} not found.")
    return row["id"]


def get_instance_id(cursor, account_id: int,
                    ad_group_id: str, criterion_id: str) -> int | None:
    """
    Resolves keyword_instances.id using the corrected composite key
    (account_id, ad_group_id, criterion_id).
    """
    row = cursor.execute(
        """
        SELECT id FROM keyword_instances
        WHERE account_id  = ?
          AND ad_group_id = ?
          AND criterion_id = ?
        """,
        (account_id, ad_group_id, criterion_id)
    ).fetchone()
    return row["id"] if row else None


def parse_ad_group_criterion_resource(resource: str) -> tuple[str, str]:
    """
    Extracts (ad_group_id, criterion_id) from the resource name string.

    Actual Google Ads API format for segments.keyword.ad_group_criterion:
      customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}
      → 4 slash-separated parts
      → parts[3] = "{ad_group_id}~{criterion_id}"

    Both IDs are encoded together in the tilde segment.
    parts[3].split("~") = [ad_group_id, criterion_id]

    Returns ("", "") if resource is empty or unparseable.
    """
    if not resource:
        return "", ""
    if "~" not in resource:
        return "", ""
    # Extract the tilde segment — always the last slash-separated part
    tilde_segment = resource.split("/")[-1]
    tilde_parts   = tilde_segment.split("~")
    if len(tilde_parts) < 2:
        return "", tilde_segment
    ad_group_id  = tilde_parts[0]
    criterion_id = tilde_parts[-1]
    return ad_group_id, criterion_id


def get_or_create_query(cursor, query_text, report_date, platform) -> int:
    normalized = query_text.lower().strip()
    row = cursor.execute(
        "SELECT id FROM search_queries WHERE normalized_text = ?",
        (normalized,)
    ).fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO search_queries "
            "(query_text, normalized_text, first_seen_date, "
            " last_seen_date, first_seen_platform) "
            "VALUES (?, ?, ?, ?, ?)",
            (query_text, normalized, report_date, report_date, platform)
        )
        return cursor.lastrowid
    cursor.execute(
        "UPDATE search_queries SET last_seen_date = MAX(last_seen_date, ?) "
        "WHERE id = ?",
        (report_date, row["id"])
    )
    return row["id"]


def load_conversion_action_map(cursor, account_id: int) -> dict:
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode "
        "FROM conversion_actions WHERE account_id = ?",
        (account_id,)
    ).fetchall()
    return {r["name"]: r["include_mode"] for r in rows}


def get_monthly_chunks(start: date, end: date) -> list[tuple[str, str]]:
    chunks, chunk_start = [], start.replace(day=1)
    while chunk_start <= end:
        chunk_end = min(
            (chunk_start + relativedelta(months=1)) - timedelta(days=1), end
        )
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


# ── API helpers ───────────────────────────────────────────────────
def get_google_ads_client(login_customer_id=None):
    client = (GoogleAdsClient.load_from_storage(str(YAML_PATH))
              if YAML_PATH.exists()
              else GoogleAdsClient.load_from_storage())
    if login_customer_id:
        client.login_customer_id = login_customer_id
    return client


def _print_api_error(label, ex):
    print(f"\n  API ERROR in {label}:")
    for error in ex.failure.errors:
        print(f"    [{error.error_code}] {error.message}")


def resolve_conversion_action_name(resource, cache, ga_service, customer_id):
    if resource in cache:
        return cache[resource]
    try:
        q = (f"SELECT conversion_action.name FROM conversion_action "
             f"WHERE conversion_action.resource_name = '{resource}'")
        response = ga_service.search_stream(customer_id=customer_id, query=q)
        for batch in response:
            for row in batch.results:
                name = row.conversion_action.name.lower()
                cache[resource] = name
                return name
    except Exception:
        pass
    cache[resource] = "unknown"
    return "unknown"


# ── GAQL (campaign-scoped) ────────────────────────────────────────
def fetch_query_traffic(ga_service, customer_id, campaign_id,
                        start, end) -> list[dict]:
    """
    Pass 1a: clicks/impressions/cost for search queries in this campaign.
    ad_group_id is extracted from segments.keyword.ad_group_criterion
    resource name — no additional GAQL field needed.
    """
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            segments.search_term_match_type,
            segments.ad_network_type,
            metrics.clicks,
            metrics.impressions,
            metrics.cost_micros
        FROM search_term_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                resource = row.segments.keyword.ad_group_criterion
                ad_group_id, criterion_id = parse_ad_group_criterion_resource(
                    resource
                )
                results.append({
                    "query_text":       row.search_term_view.search_term,
                    "criterion_id":     criterion_id,
                    "ad_group_id":      ad_group_id,
                    "campaign_id":      str(row.campaign.id),
                    "report_date":      row.segments.date,
                    "match_type_served":row.segments.search_term_match_type.name,
                    "clicks":           row.metrics.clicks,
                    "impressions":      row.metrics.impressions,
                    "cost_micros":      row.metrics.cost_micros,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1a (query traffic)", ex)
        raise
    return results


def fetch_query_conversions(ga_service, customer_id, campaign_id,
                            start, end) -> list[dict]:
    """Pass 1b: conversion data for search queries in this campaign."""
    query = f"""
        SELECT
            search_term_view.search_term,
            segments.keyword.ad_group_criterion,
            campaign.id,
            segments.date,
            segments.ad_network_type,
            segments.conversion_action,
            metrics.all_conversions,
            metrics.all_conversions_value
        FROM search_term_view
        WHERE
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
            AND campaign.advertising_channel_type = 'SEARCH'
            AND segments.ad_network_type IN ('SEARCH', 'SEARCH_PARTNERS')
            AND metrics.all_conversions > 0
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id,
                                            query=query)
        for batch in response:
            for row in batch.results:
                resource = row.segments.keyword.ad_group_criterion
                ad_group_id, criterion_id = parse_ad_group_criterion_resource(
                    resource
                )
                results.append({
                    "query_text":       row.search_term_view.search_term,
                    "criterion_id":     criterion_id,
                    "ad_group_id":      ad_group_id,
                    "campaign_id":      str(row.campaign.id),
                    "report_date":      row.segments.date,
                    "conversion_action":row.segments.conversion_action,
                    "all_conv_count":   row.metrics.all_conversions,
                    "all_conv_value":   row.metrics.all_conversions_value,
                })
    except GoogleAdsException as ex:
        _print_api_error("Pass 1b (query conversions)", ex)
        raise
    return results


# ── Aggregation + write ───────────────────────────────────────────
def aggregate_and_write(conn, account_id, traffic_rows, conversion_rows,
                        conv_map, conv_cache, ga_service, customer_id) -> dict:
    """
    Aggregates traffic and conversions then writes to search_query_performance.
    Aggregation key now includes ad_group_id for unambiguous instance lookup.
    """
    cursor = conn.cursor()
    counts = {"written": 0, "skipped": 0, "new_queries": 0}

    # Aggregate traffic — key includes ad_group_id
    traffic = {}
    for row in traffic_rows:
        key = (row["query_text"], row["criterion_id"], row["ad_group_id"],
               row["campaign_id"], row["report_date"],
               row["match_type_served"])
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

    # Route conversions — match on (query_text, criterion_id, ad_group_id,
    # campaign_id, report_date), then fan out to matching match_type keys
    for row in conversion_rows:
        key_base = (row["query_text"], row["criterion_id"], row["ad_group_id"],
                    row["campaign_id"], row["report_date"])
        matching = [k for k in traffic if k[:5] == key_base]

        if not matching:
            key = key_base + ("UNKNOWN",)
            traffic[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
            matching = [key]

        action_name = resolve_conversion_action_name(
            row["conversion_action"], conv_cache, ga_service, customer_id
        )
        mode = conv_map.get(action_name, "monitor")

        for k in matching:
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

    # Write to sqp
    for (query_text, criterion_id, ad_group_id, campaign_id,
         report_date, match_type_served), m in traffic.items():

        instance_id = get_instance_id(
            cursor, account_id, ad_group_id, criterion_id
        )
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

        cursor.execute("""
            INSERT INTO search_query_performance
                (query_id, instance_id, campaign_id, platform,
                 report_date, device, match_type_served,
                 impressions, clicks, cost_micros,
                 primary_conv_count, primary_conv_value,
                 secondary_conv_count, all_conv_count, all_conv_value)
            VALUES (?, ?, ?, 'google', ?, 'ALL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (query_id, instance_id, report_date, device)
            DO UPDATE SET
                campaign_id          = excluded.campaign_id,
                match_type_served    = excluded.match_type_served,
                impressions          = excluded.impressions,
                clicks               = excluded.clicks,
                cost_micros          = excluded.cost_micros,
                primary_conv_count   = excluded.primary_conv_count,
                primary_conv_value   = excluded.primary_conv_value,
                secondary_conv_count = excluded.secondary_conv_count,
                all_conv_count       = excluded.all_conv_count,
                all_conv_value       = excluded.all_conv_value
        """, (
            query_id, instance_id, campaign_id,
            report_date, match_type_served,
            m["impressions"], m["clicks"], m["cost_micros"],
            m["primary_conv_count"], m["primary_conv_value"],
            m["secondary_conv_count"],
            m["all_conv_count"], m["all_conv_value"],
        ))
        counts["written"] += 1

    conn.commit()
    return counts


# ── Args + main ───────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Sync search query performance for a specific campaign."
    )
    p.add_argument("--account", required=True,
                   choices=list(ACCOUNT_MAP.keys()))
    p.add_argument("--campaign-id", required=True, metavar="ID")
    p.add_argument("--refresh", type=int, default=90, metavar="N",
                   help="Number of days to pull (default: 90).")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--end",   default=None, metavar="YYYY-MM-DD")
    return p.parse_args()


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
                  if args.start else
                  end_date - timedelta(days=args.refresh - 1))

    chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("sync_search_queries_by_campaign.py  [v2]")
    print(f"  Account    : {cfg['display_name']} ({customer_id})")
    print(f"  Campaign ID: {args.campaign_id}")
    print(f"  Range      : {start_date} → {end_date} "
          f"({(end_date-start_date).days+1} days)")
    print(f"  Chunks     : {len(chunks)}")
    print("=" * 65)

    conn   = get_conn()
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

    conv_cache = {}
    totals = {"written": 0, "skipped": 0,
              "new_queries": 0, "traffic_rows": 0, "conv_rows": 0}

    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        print(f"[{i}/{len(chunks)}] {chunk_start} → {chunk_end}")

        print(f"  Pass 1a (query traffic)...", end=" ", flush=True)
        traffic = fetch_query_traffic(ga_service, customer_id,
                                      args.campaign_id, chunk_start, chunk_end)
        print(f"{len(traffic):,} rows")

        print(f"  Pass 1b (conversions)...",  end=" ", flush=True)
        convs = fetch_query_conversions(ga_service, customer_id,
                                        args.campaign_id, chunk_start, chunk_end)
        print(f"{len(convs):,} rows")

        counts = aggregate_and_write(
            conn, account_id, traffic, convs,
            conv_map, conv_cache, ga_service, customer_id
        )
        print(f"       → {counts['written']:,} written, "
              f"{counts['skipped']:,} skipped, "
              f"{counts['new_queries']:,} new queries")

        totals["traffic_rows"]  += len(traffic)
        totals["conv_rows"]     += len(convs)
        totals["written"]       += counts["written"]
        totals["skipped"]       += counts["skipped"]
        totals["new_queries"]   += counts["new_queries"]

    print("\n" + "=" * 65)
    print("SYNC COMPLETE")
    print("=" * 65)
    print(f"  Traffic rows          : {totals['traffic_rows']:,}")
    print(f"  Conversion rows       : {totals['conv_rows']:,}")
    print(f"  sqp rows written      : {totals['written']:,}")
    print(f"  sqp rows skipped      : {totals['skipped']:,}")
    print(f"  New search queries    : {totals['new_queries']:,}")

    v = conn.execute("""
        SELECT COUNT(DISTINCT query_id) AS queries,
               COUNT(DISTINCT instance_id) AS kw_slots,
               COUNT(*) AS rows,
               SUM(cost_micros)/1e6 AS spend,
               SUM(primary_conv_value) AS revenue,
               MIN(report_date) AS min_date,
               MAX(report_date) AS max_date
        FROM search_query_performance
        WHERE campaign_id = ? AND device = 'ALL'
    """, (args.campaign_id,)).fetchone()

    print(f"\n  sqp verification (campaign {args.campaign_id}, device=ALL):")
    print(f"    Queries    : {v['queries']:,}")
    print(f"    KW slots   : {v['kw_slots']:,}")
    print(f"    Total rows : {v['rows']:,}")
    print(f"    Date range : {v['min_date']} → {v['max_date']}")
    spend   = v['spend']   or 0.0
    revenue = v['revenue'] or 0.0
    print(f"    Spend      : ${spend:,.2f}")
    print(f"    Revenue    : ${revenue:,.2f}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
