"""
03_sync_keyword_performance.py  [v3 — per-campaign loop, all accounts]
=======================================================================
Syncs keyword_performance_daily for one account by looping over every
non-REMOVED SEARCH campaign and querying keyword_view with
WHERE campaign.id = ? for each one.

Replaces and archives:
  - 03_sync_keyword_performance.py  [v1/v2 — account-wide, wrong composite key]
  - sync_keyword_perf_by_campaign.py — logic absorbed here
  - sync_home_active_campaigns.py   — orchestration absorbed here

WHY PER-CAMPAIGN
────────────────
Querying keyword_view without a campaign filter returns keywords attributed
to their owning campaign, not the campaign whose ad group is running them.
For accounts with shared ad groups (e.g. Home TIER TWO shares ad groups
with 100+ state campaigns), account-wide queries produce non-deterministic
campaign attribution. Per-campaign GAQL with WHERE campaign.id = ? matches
what the Google Ads UI shows when you filter the keyword report to a
specific campaign.

COMPOSITE KEY
─────────────
instance_id is resolved via the corrected three-column composite key:
  (account_id, ad_group_id, criterion_id)
ad_group.id is fetched in the GAQL SELECT so it is available at write time.
The old two-column lookup (account_id, criterion_id) was ambiguous when
the same criterion_id appeared in multiple ad groups across campaigns.

USAGE
─────
    conda activate paid-search

    # Full backfill from Jan 2025 for Home:
    python kw_db_scripts/03_sync_keyword_performance.py --account home --start 2025-01-01

    # Last 90 days (default):
    python kw_db_scripts/03_sync_keyword_performance.py --account home

    # Specific date range:
    python kw_db_scripts/03_sync_keyword_performance.py \\
        --account home --start 2026-01-01 --end 2026-04-14

    # All accounts in sequence (run once per account):
    python kw_db_scripts/03_sync_keyword_performance.py --account medicare
    python kw_db_scripts/03_sync_keyword_performance.py --account auto
    python kw_db_scripts/03_sync_keyword_performance.py --account term_life
    python kw_db_scripts/03_sync_keyword_performance.py --account auto_quote
"""

import os
import sqlite3
import argparse
import time
from pathlib import Path
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH      = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
YAML_PATH    = Path(r"C:\Projects\google-ads-ops\google-ads.yaml")
DEFAULT_DAYS = 90

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


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Sync keyword_performance_daily per campaign for one account."
    )
    p.add_argument("--account", required=True, choices=list(ACCOUNT_MAP.keys()),
                   help="Account to sync.")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help=f"Start date (default: {DEFAULT_DAYS} days ago).")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="End date (default: yesterday).")
    p.add_argument("--campaign-id", default=None, metavar="ID",
                   help="Sync a single campaign ID only (optional). "
                        "Omit to loop over all non-REMOVED SEARCH campaigns.")
    return p.parse_args()


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_id(cursor, platform_account_id: str) -> int:
    row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    if not row:
        raise ValueError(
            f"Account '{platform_account_id}' not found. Run 01_create_schema.py first."
        )
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
        WHERE account_id   = ?
          AND ad_group_id  = ?
          AND criterion_id = ?
        """,
        (account_id, ad_group_id, criterion_id),
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
            (chunk_start + relativedelta(months=1)) - timedelta(days=1), end
        )
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start += relativedelta(months=1)
    return chunks


# ── Google Ads client ─────────────────────────────────────────────────────────

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


# ── Campaign discovery ────────────────────────────────────────────────────────

def discover_campaigns(ga_service, customer_id: str) -> list[dict]:
    """Returns all non-REMOVED SEARCH campaigns — ENABLED and PAUSED."""
    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status
        FROM campaign
        WHERE campaign.advertising_channel_type = 'SEARCH'
          AND campaign.status != 'REMOVED'
        ORDER BY campaign.name
    """
    results = []
    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                results.append({
                    "campaign_id":   str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "status":        row.campaign.status.name,
                })
    except GoogleAdsException as ex:
        _print_api_error("discover_campaigns", ex)
        raise
    return results


# ── Conversion action name resolution ─────────────────────────────────────────

def resolve_conversion_action_name(resource, cache, ga_service, customer_id):
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


# ── GAQL queries (campaign-scoped) ────────────────────────────────────────────

def fetch_traffic(ga_service, customer_id: str, campaign_id: str,
                  start: str, end: str) -> list[dict]:
    """Pass 1a: clicks, impressions, cost — includes ad_group.id for composite key."""
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
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
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
        _print_api_error("Pass 1a (traffic)", ex)
        raise
    return results


def fetch_conversions(ga_service, customer_id: str, campaign_id: str,
                      start: str, end: str) -> list[dict]:
    """Pass 1b: conversions by action — includes ad_group.id for composite key."""
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
            campaign.id = {campaign_id}
            AND segments.date BETWEEN '{start}' AND '{end}'
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
        _print_api_error("Pass 1b (conversions)", ex)
        raise
    return results


# ── Aggregation ───────────────────────────────────────────────────────────────

def route_and_aggregate(traffic_rows, conversion_rows, conv_map,
                        action_name_cache, ga_service, customer_id) -> dict:
    """
    Merges traffic and conversion rows.
    Key: (criterion_id, ad_group_id, campaign_id, report_date)
    """
    aggregated = {}

    for row in traffic_rows:
        key = (row["criterion_id"], row["ad_group_id"],
               row["campaign_id"],  row["report_date"])
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
        key = (row["criterion_id"], row["ad_group_id"],
               row["campaign_id"],  row["report_date"])
        if key not in aggregated:
            aggregated[key] = {
                "clicks": 0, "impressions": 0, "cost_micros": 0.0,
                "primary_conv_count": 0.0, "primary_conv_value": 0.0,
                "secondary_conv_count": 0.0,
                "all_conv_count": 0.0, "all_conv_value": 0.0,
            }
        action_name = resolve_conversion_action_name(
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


# ── Upsert ────────────────────────────────────────────────────────────────────

def upsert_kpd(conn, cursor, account_id: int, aggregated: dict) -> dict:
    counts = {"written": 0, "skipped": 0}

    for (criterion_id, ad_group_id, campaign_id, report_date), m in aggregated.items():
        instance_id = get_instance_id(cursor, account_id, ad_group_id, criterion_id)
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


# ── Per-campaign sync ─────────────────────────────────────────────────────────

def sync_campaign(ga_service, conn, cursor, account_id: int,
                  customer_id: str, campaign_id: str,
                  chunks: list, conv_map: dict,
                  action_name_cache: dict) -> dict:
    """Syncs kpd for one campaign over all date chunks."""
    totals = {"written": 0, "skipped": 0, "traffic_rows": 0, "conv_rows": 0}

    for chunk_start, chunk_end in chunks:
        traffic = fetch_traffic(ga_service, customer_id, campaign_id,
                                chunk_start, chunk_end)
        convs   = fetch_conversions(ga_service, customer_id, campaign_id,
                                    chunk_start, chunk_end)
        aggregated = route_and_aggregate(
            traffic, convs, conv_map, action_name_cache, ga_service, customer_id
        )
        counts = upsert_kpd(conn, cursor, account_id, aggregated)

        totals["traffic_rows"] += len(traffic)
        totals["conv_rows"]    += len(convs)
        totals["written"]      += counts["written"]
        totals["skipped"]      += counts["skipped"]

        print(f"    {chunk_start} → {chunk_end}  "
              f"traffic={len(traffic):,}  conv={len(convs):,}  "
              f"written={counts['written']:,}  skipped={counts['skipped']:,}")

    return totals


# ── Post-sync diagnostic ──────────────────────────────────────────────────────

def print_diagnostic(conn, account_id: int):
    rows = conn.execute("""
        SELECT
            kpd.campaign_id,
            c.campaign_name,
            COUNT(DISTINCT kpd.instance_id)  AS kw_slots,
            COUNT(*)                          AS rows,
            MIN(kpd.report_date)              AS min_date,
            MAX(kpd.report_date)              AS max_date,
            SUM(kpd.cost_micros) / 1e6        AS spend,
            SUM(kpd.primary_conv_value)       AS revenue
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        LEFT JOIN campaigns c     ON c.campaign_id = kpd.campaign_id
        WHERE ki.account_id = ?
          AND kpd.device    = 'ALL'
        GROUP BY kpd.campaign_id
        ORDER BY spend DESC
    """, (account_id,)).fetchall()

    print(f"\n{'─'*90}")
    print(f"  DIAGNOSTIC — keyword_performance_daily (device='ALL')")
    print(f"{'─'*90}")
    if not rows:
        print("  (no data)")
        return
    print(f"  {'Campaign':<50} {'Slots':>6} {'Rows':>7} "
          f"{'Spend':>12} {'Revenue':>11} {'Date range'}")
    print(f"  {'─'*50} {'─'*6} {'─'*7} {'─'*12} {'─'*11} {'─'*24}")
    for r in rows:
        name = (r["campaign_name"] or r["campaign_id"] or "")[:49]
        print(f"  {name:<50} "
              f"{r['kw_slots']:>6,} "
              f"{r['rows']:>7,} "
              f"${r['spend']:>11,.2f} "
              f"${r['revenue']:>10,.2f} "
              f"{r['min_date']} → {r['max_date']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args    = parse_args()
    cfg     = ACCOUNT_MAP[args.account]

    customer_id = os.getenv(cfg["customer_id_env"])
    if not customer_id:
        raise ValueError(f"{cfg['customer_id_env']} not set in .env")

    login_cid = (os.getenv(cfg["login_customer_id_env"])
                 if cfg["login_customer_id_env"] else None)

    end_date   = (date.fromisoformat(args.end)
                  if args.end else date.today() - timedelta(days=1))
    start_date = (date.fromisoformat(args.start)
                  if args.start else end_date - timedelta(days=DEFAULT_DAYS - 1))

    chunks = get_monthly_chunks(start_date, end_date)

    print("=" * 65)
    print("03_sync_keyword_performance.py  [v3]")
    print(f"  Account  : {cfg['display_name']} ({customer_id})")
    print(f"  Range    : {start_date} → {end_date} "
          f"({(end_date - start_date).days + 1} days, {len(chunks)} chunk(s))")
    print("=" * 65)

    conn   = get_db_connection()
    cursor = conn.cursor()
    account_id = get_account_id(cursor, customer_id)
    print(f"\n  accounts.id = {account_id}")

    conv_map = load_conversion_action_map(cursor, account_id)
    print(f"\n  Conversion action map ({len(conv_map)} actions):")
    for name, mode in sorted(conv_map.items()):
        print(f"    [{mode:<16}]  {name}")

    print("\n  Connecting to Google Ads API...")
    client     = get_google_ads_client(login_cid)
    ga_service = client.get_service("GoogleAdsService")
    print("  Connected.")

    # Campaign list — single campaign or full discovery
    if args.campaign_id:
        campaigns = [{"campaign_id": args.campaign_id,
                      "campaign_name": f"(specified: {args.campaign_id})",
                      "status": "UNKNOWN"}]
        print(f"\n  Mode: single campaign ({args.campaign_id})")
    else:
        print("\n  Discovering campaigns...")
        campaigns = discover_campaigns(ga_service, customer_id)
        enabled = sum(1 for c in campaigns if c["status"] == "ENABLED")
        paused  = sum(1 for c in campaigns if c["status"] == "PAUSED")
        print(f"  Found {len(campaigns):,} campaigns "
              f"({enabled} ENABLED, {paused} PAUSED)")

    action_name_cache = {}
    grand_totals = {"written": 0, "skipped": 0,
                    "traffic_rows": 0, "conv_rows": 0}
    results = []
    t_start = time.time()

    for i, camp in enumerate(campaigns, 1):
        cid   = camp["campaign_id"]
        cname = camp["campaign_name"]
        status = camp["status"]
        print(f"\n  [{i}/{len(campaigns)}] [{status}] {cname} ({cid})")

        t0 = time.time()
        try:
            totals = sync_campaign(
                ga_service, conn, cursor, account_id,
                customer_id, cid, chunks, conv_map, action_name_cache,
            )
        except GoogleAdsException as ex:
            _print_api_error(f"campaign {cid}", ex)
            results.append({"name": cname, "id": cid, "ok": False, "elapsed": 0})
            continue

        elapsed = time.time() - t0
        for k in grand_totals:
            grand_totals[k] += totals[k]
        results.append({"name": cname, "id": cid, "ok": True,
                         "written": totals["written"],
                         "skipped": totals["skipped"],
                         "elapsed": elapsed})
        print(f"    ✓ written={totals['written']:,}  "
              f"skipped={totals['skipped']:,}  "
              f"({elapsed:.0f}s)")

    total_elapsed = time.time() - t_start

    # Summary table
    print(f"\n{'='*65}")
    print("SYNC COMPLETE")
    print(f"{'='*65}")
    print(f"  Campaigns processed : {len(campaigns):,}")
    print(f"  Traffic rows (API)  : {grand_totals['traffic_rows']:,}")
    print(f"  Conversion rows     : {grand_totals['conv_rows']:,}")
    print(f"  kpd rows written    : {grand_totals['written']:,}")
    print(f"  kpd rows skipped    : {grand_totals['skipped']:,}")
    print(f"  (skipped = no matching row in keyword_instances)")
    print(f"  Total time          : {total_elapsed/60:.1f} min")

    failures = [r for r in results if not r["ok"]]
    if failures:
        print(f"\n  ⚠ {len(failures)} campaign(s) failed:")
        for f in failures:
            print(f"    {f['name']} ({f['id']})")

    print_diagnostic(conn, account_id)

    conn.close()
    print(f"\nNext step:")
    print(f"  python kw_db_scripts/05_sync_search_queries.py "
          f"--account {args.account} --start {start_date.isoformat()}")


if __name__ == "__main__":
    main()
