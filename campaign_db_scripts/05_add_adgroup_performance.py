"""
05_add_adgroup_performance.py
==============================
Adds the ad_group_performance_daily table to the campaign database
and backfills it from the Google Ads API for all active accounts.

WHY THIS TABLE EXISTS
---------------------
The existing schema aggregates ad group performance from keyword_performance_daily
upward through keyword_instances. That only works when every keyword slot is
correctly attributed to its campaign in keyword_instances -- which breaks after
campaign restructures, keyword moves, or any sync gap.

ad_group_performance_daily syncs directly from the ad_group_view resource,
the same source Google Ads UI uses for its Ad Groups report. This produces
numbers that match the UI exactly, independent of keyword structure.

TABLE GRAIN
-----------
One row per ad_group × date × device.
ALL-device rollup rows are generated automatically (same pattern as
campaign_performance_daily after backfill_campaign_all_device.py).

COLUMNS
-------
  ad_group_id           -- Google Ads ad group criterion ID (string)
  campaign_id           -- parent campaign ID (string)
  account_id            -- platform_account_id string (e.g. '8886502147')
  ad_group_name         -- denormalized for query convenience
  campaign_name         -- denormalized for query convenience
  date                  -- YYYY-MM-DD
  device                -- MOBILE | DESKTOP | TABLET | ALL
  impressions
  clicks
  cost_micros
  conversions           -- all_conversions (Google total, UI sanity check)
  conversion_value      -- all_conversions_value
  primary_conv_count    -- routed offline conversions (same logic as keyword perf)
  primary_conv_value    -- routed offline revenue
  search_impression_share  -- NULL if below threshold
  search_budget_lost_is
  search_rank_lost_is
  search_exact_match_is

BACKFILL
--------
Defaults to 90 days. Override with --start YYYY-MM-DD.
Safe to re-run -- uses INSERT OR REPLACE on the PRIMARY KEY.

RUN
---
  conda activate paid-search

  # Create table + backfill all accounts (90 days):
  python campaign_db_scripts/05_add_adgroup_performance.py

  # Specific date range:
  python campaign_db_scripts/05_add_adgroup_performance.py \\
      --start 2025-01-01 --end 2026-04-08

  # Create table only, no sync:
  python campaign_db_scripts/05_add_adgroup_performance.py --schema-only
"""

import os
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATH          = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_DAYS     = 90
MCC_ID           = os.getenv("GOOGLE_ADS_MCC_ID")   # fallback login_customer_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────────

DDL = [
    """
    CREATE TABLE IF NOT EXISTS ad_group_performance_daily (
        ad_group_id                  TEXT    NOT NULL,
        campaign_id                  TEXT    NOT NULL,
        account_id                   INTEGER NOT NULL REFERENCES accounts(id),
        ad_group_name                TEXT,
        campaign_name                TEXT,

        date                         TEXT    NOT NULL,
        device                       TEXT    NOT NULL,

        impressions                  INTEGER NOT NULL DEFAULT 0,
        clicks                       INTEGER NOT NULL DEFAULT 0,
        cost_micros                  INTEGER NOT NULL DEFAULT 0,

        -- Google total conversions (UI sanity check)
        conversions                  REAL    NOT NULL DEFAULT 0,
        conversion_value             REAL    NOT NULL DEFAULT 0,

        -- Routed offline conversions (same routing as keyword_performance_daily)
        primary_conv_count           REAL    NOT NULL DEFAULT 0,
        primary_conv_value           REAL    NOT NULL DEFAULT 0,

        -- Impression share (NULL = below reporting threshold)
        -- Available at ad group grain from the API
        search_impression_share      REAL,
        search_budget_lost_is        REAL,
        search_rank_lost_is          REAL,
        search_exact_match_is        REAL,

        last_synced_at               TEXT    NOT NULL,

        PRIMARY KEY (ad_group_id, date, device)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agpd_date         ON ad_group_performance_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_agpd_ag_date      ON ad_group_performance_daily(ad_group_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_agpd_camp_date    ON ad_group_performance_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_agpd_account_date ON ad_group_performance_daily(account_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_agpd_device       ON ad_group_performance_daily(device)",
]

# GAQL query -- ad_group_view gives one row per ad_group × date × device
# IS metrics ARE available at ad group grain
AD_GROUP_DAILY_QUERY = """
    SELECT
        ad_group.id,
        ad_group.name,
        campaign.id,
        campaign.name,
        segments.date,
        segments.device,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.all_conversions,
        metrics.all_conversions_value,
        metrics.search_impression_share,
        metrics.search_exact_match_impression_share
    FROM ad_group
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND ad_group.status != 'REMOVED'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id, ad_group.id, segments.date, segments.device
"""

# GAQL for conversion routing -- same pattern as campaign/keyword sync
CONV_QUERY = """
    SELECT
        ad_group.id,
        segments.date,
        segments.conversion_action,
        metrics.all_conversions_by_conversion_date,
        metrics.all_conversions_value_by_conversion_date
    FROM ad_group
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND ad_group.status != 'REMOVED'
      AND campaign.status != 'REMOVED'
    ORDER BY ad_group.id, segments.date
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_is(val) -> float | None:
    if val is None:
        return None
    f = float(val)
    return None if f < 0 else f


def extract_conversion_action_name(resource, cache, ga_service, customer_id):
    """
    Resolves a conversion action resource string to its human-readable name.
    Resource format: "customers/123/conversionActions/456"
    The numeric suffix must be looked up via the API to get the name
    (e.g. "QW Home Offline Conversions") that matches conversion_actions table.
    Results are cached to avoid redundant API calls per sync run.
    Copied from 03_sync_keyword_performance.py.
    """
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


def discover_accounts() -> list[dict]:
    """
    Exact copy of discover_accounts() from 03_sync_campaign_performance.py.
    Uses MCC_ID as the login_customer_id fallback for MCC-managed accounts.
    Without this, MCC-managed accounts get PERMISSION_DENIED because the
    API requires the manager ID in the login-customer-id header.
    """
    accounts = []
    prefix = "GOOGLE_ADS_CUSTOMER_ID_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix      = key[len(prefix):]
        customer_id = value.strip()
        login_key   = f"GOOGLE_ADS_LOGIN_CUSTOMER_ID_{suffix}"
        login_id    = os.getenv(login_key, "").strip()
        accounts.append({
            "customer_id":       customer_id,
            "vertical":          suffix.lower(),
            "login_customer_id": login_id if login_id else MCC_ID,
            "is_standalone":     bool(login_id),
        })
    accounts.sort(key=lambda a: a["vertical"])
    return accounts


def get_google_ads_client(login_customer_id: str) -> object:
    """Exact copy of get_google_ads_client() from 03_sync_campaign_performance.py."""
    from google.ads.googleads.client import GoogleAdsClient
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


def load_conversion_action_map(conn, account_id: str) -> dict:
    """
    Returns {conversion_name_lower: include_mode} from conversion_actions.

    Column names (from 01_create_schema.py):
        conversion_name   -- the action name
        include_mode      -- 'primary' | 'secondary_count' | 'monitor' | 'excluded'

    account_id in conversion_actions is the INTEGER FK (accounts.id),
    not the platform_account_id string.
    """
    cursor = conn.cursor()

    # Get the integer PK for this account
    acct_row = cursor.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (account_id,)
    ).fetchone()
    if not acct_row:
        return {}

    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode "
        "FROM conversion_actions WHERE account_id = ?",
        (acct_row['id'],)
    ).fetchall()
    return {r['name']: r['include_mode'] for r in rows}


def get_monthly_chunks(start: date, end: date) -> list[tuple[str, str]]:
    """Splits a date range into monthly chunks. Matches pattern in 03."""
    from dateutil.relativedelta import relativedelta
    chunks = []
    chunk_start = start.replace(day=1)
    while chunk_start <= end:
        chunk_end = min(
            (chunk_start + relativedelta(months=1)) - timedelta(days=1),
            end
        )
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        chunk_start = chunk_start + relativedelta(months=1)
    return chunks


# ── Schema creation ───────────────────────────────────────────────────────────

def create_schema(conn):
    cursor = conn.cursor()
    created = []
    for stmt in DDL:
        cursor.execute(stmt)
        tokens = stmt.strip().split()
        if len(tokens) >= 6 and tokens[2].upper() == 'TABLE':
            created.append(tokens[5])
    conn.commit()
    log.info(f"  Schema ready: {created if created else 'indexes only'}")


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync_adgroup_daily(client, ga_service, conn, account: dict,
                       start: str, end: str) -> int:
    """
    Syncs ad_group_performance_daily for one account over one date range.
    Three passes (mirrors keyword performance sync pattern):
      Pass 1 : impressions / clicks / cost + IS
      Pass 2 : conversion routing (primary_conv_count / primary_conv_value)
      ALL row : device rollup generated in Python
    """
    customer_id = account["customer_id"]
    now         = now_utc()

    # Resolve integer accounts.id -- required for INSERT (account_id INTEGER FK)
    _cur = conn.cursor()
    _acct = _cur.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (customer_id,)
    ).fetchone()
    if not _acct:
        log.error(f"  No accounts row for {customer_id} -- skipping chunk {start}\u2192{end}")
        return 0
    internal_account_id = _acct["id"]

    conv_map = load_conversion_action_map(conn, customer_id)

    # ── Pass 1: traffic + IS ──────────────────────────────────────────
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = AD_GROUP_DAILY_QUERY.format(start=start, end=end)

    rows_dict: dict[tuple, dict] = {}   # keyed by (ag_id, date, device)

    for row in ga_service.search(request=request):
        ag  = row.ad_group
        c   = row.campaign
        m   = row.metrics
        s   = row.segments

        key = (str(ag.id), s.date, s.device.name)
        rows_dict[key] = {
            "ad_group_id":    str(ag.id),
            "campaign_id":    str(c.id),
            "account_id":     internal_account_id,
            "ad_group_name":  ag.name,
            "campaign_name":  c.name,
            "date":           s.date,
            "device":         s.device.name,
            "impressions":    m.impressions,
            "clicks":         m.clicks,
            "cost_micros":    m.cost_micros,
            "conversions":    m.all_conversions,
            "conversion_value": m.all_conversions_value,
            "primary_conv_count":  0.0,
            "primary_conv_value":  0.0,
            "is":             safe_is(m.search_impression_share),
            "is_budget":      None,   # not available FROM ad_group in Google Ads API
            "is_rank":        None,   # not available FROM ad_group in Google Ads API
            "is_exact":       safe_is(m.search_exact_match_impression_share),
        }

    # ── Pass 2: conversion routing ────────────────────────────────────
    # Mirrors route_and_aggregate_conversions() in 03_sync_keyword_performance.py
    # Uses extract_conversion_action_name() to resolve the API resource string
    # e.g. "customers/123/conversionActions/456" → "QW Home Offline Conversions"
    # then looks that name up in conv_map to get include_mode.
    if conv_map:
        action_name_cache: dict[str, str] = {}

        request2 = client.get_type("SearchGoogleAdsRequest")
        request2.customer_id = customer_id
        request2.query = CONV_QUERY.format(start=start, end=end)

        for row in ga_service.search(request=request2):
            ag_id   = str(row.ad_group.id)
            date_   = row.segments.date
            action_resource = row.segments.conversion_action
            count = row.metrics.all_conversions_by_conversion_date
            value = row.metrics.all_conversions_value_by_conversion_date

            # Resolve resource string → human-readable name → conv_map lookup
            action_name = extract_conversion_action_name(
                action_resource, action_name_cache, ga_service, customer_id
            )
            mode = conv_map.get(action_name.lower(), 'monitor')

            # Route based on include_mode
            for device in ['MOBILE', 'DESKTOP', 'TABLET', 'CONNECTED_TV', 'OTHER']:
                key = (ag_id, date_, device)
                if key not in rows_dict:
                    continue
                if mode == 'primary':
                    rows_dict[key]["primary_conv_count"] += count
                    rows_dict[key]["primary_conv_value"] += value
                # secondary_count, monitor, excluded: don't add to primary

    # ── Build ALL rollup rows ─────────────────────────────────────────
    from collections import defaultdict
    rollup: dict[tuple, list] = defaultdict(list)
    for key, r in rows_dict.items():
        rollup_key = (r["ad_group_id"], r["account_id"], r["date"])
        rollup[rollup_key].append(r)

    all_rows = []
    for (ag_id, acct_id, date_val), device_rows in rollup.items():
        first = device_rows[0]
        all_rows.append({
            "ad_group_id":       ag_id,
            "campaign_id":       first["campaign_id"],
            "account_id":        acct_id,
            "ad_group_name":     first["ad_group_name"],
            "campaign_name":     first["campaign_name"],
            "date":              date_val,
            "device":            "ALL",
            "impressions":       sum(r["impressions"]         for r in device_rows),
            "clicks":            sum(r["clicks"]              for r in device_rows),
            "cost_micros":       sum(r["cost_micros"]         for r in device_rows),
            "conversions":       sum(r["conversions"]         for r in device_rows),
            "conversion_value":  sum(r["conversion_value"]    for r in device_rows),
            "primary_conv_count": sum(r["primary_conv_count"] for r in device_rows),
            "primary_conv_value": sum(r["primary_conv_value"] for r in device_rows),
            # IS: take first non-NULL (same blended value across all devices)
            "is":      next((r["is"]       for r in device_rows if r["is"]       is not None), None),
            "is_budget": next((r["is_budget"] for r in device_rows if r["is_budget"] is not None), None),
            "is_rank":   next((r["is_rank"]   for r in device_rows if r["is_rank"]   is not None), None),
            "is_exact":  next((r["is_exact"]  for r in device_rows if r["is_exact"]  is not None), None),
        })

    # ── Insert ────────────────────────────────────────────────────────
    INSERT_SQL = """
        INSERT OR REPLACE INTO ad_group_performance_daily (
            ad_group_id, campaign_id, account_id,
            ad_group_name, campaign_name,
            date, device,
            impressions, clicks, cost_micros,
            conversions, conversion_value,
            primary_conv_count, primary_conv_value,
            search_impression_share, search_budget_lost_is,
            search_rank_lost_is, search_exact_match_is,
            last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    def row_tuple(r):
        return (
            r["ad_group_id"], r["campaign_id"], r["account_id"],
            r["ad_group_name"], r["campaign_name"],
            r["date"], r["device"],
            r["impressions"], r["clicks"], r["cost_micros"],
            r["conversions"], r["conversion_value"],
            r["primary_conv_count"], r["primary_conv_value"],
            r["is"], r["is_budget"], r["is_rank"], r["is_exact"],
            now,
        )

    cursor = conn.cursor()
    all_data = list(rows_dict.values()) + all_rows
    cursor.executemany(INSERT_SQL, [row_tuple(r) for r in all_data])
    conn.commit()

    return len(all_data)


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Add ad_group_performance_daily table and backfill from API."
    )
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help=f"Backfill start date (default: {DEFAULT_DAYS} days ago)")
    p.add_argument("--end",   default=None, metavar="YYYY-MM-DD",
                   help="Backfill end date (default: yesterday)")
    p.add_argument("--account", default=None, metavar="VERTICAL",
                   help="Only sync this account vertical (e.g. 'home', 'medicare'). "
                        "Matches the suffix of GOOGLE_ADS_CUSTOMER_ID_* env vars. "
                        "Omit to sync all accounts.")
    p.add_argument("--schema-only", action="store_true",
                   help="Create table and indexes only, skip API sync.")
    return p.parse_args()


def main():
    args = parse_args()

    end_date   = (date.fromisoformat(args.end)
                  if args.end else date.today() - timedelta(days=1))
    start_date = (date.fromisoformat(args.start)
                  if args.start else end_date - timedelta(days=DEFAULT_DAYS - 1))

    log.info("=" * 65)
    log.info("05_add_adgroup_performance.py")
    log.info(f"  Database : {DB_PATH}")
    log.info(f"  Range    : {start_date} → {end_date} "
             f"({'schema only' if args.schema_only else str((end_date - start_date).days + 1) + ' days'})")
    log.info("=" * 65)

    conn = get_db_connection()

    log.info("\n[1] Creating schema...")
    create_schema(conn)

    if args.schema_only:
        log.info("  --schema-only: skipping API sync.")
        conn.close()
        return

    log.info("\n[2] Discovering accounts...")
    accounts = discover_accounts()

    if args.account:
        filter_vertical = args.account.lower()
        accounts = [a for a in accounts if a["vertical"] == filter_vertical]
        if not accounts:
            log.error(f"  No account found with vertical='{filter_vertical}'")
            log.error(f"  Available verticals: "
                      f"{[a['vertical'] for a in discover_accounts()]}")
            conn.close()
            return
        log.info(f"  Filtered to account: {filter_vertical}")

    log.info(f"  {len(accounts)} active account(s) from .env")
    for a in accounts:
        mode = "standalone" if a["is_standalone"] else f"MCC {a['login_customer_id']}"
        log.info(f"  {a['vertical']:<20} {a['customer_id']}  ({mode})")

    chunks = get_monthly_chunks(start_date, end_date)
    log.info(f"\n[3] Syncing {len(chunks)} month chunk(s): "
             f"{start_date} → {end_date}")

    total_rows = 0
    for account in accounts:
        log.info(f"\n  ── {account['vertical'].upper()} "
                 f"({account['customer_id']}) ────────────")
        try:
            client     = get_google_ads_client(account["login_customer_id"])
            ga_service = client.get_service("GoogleAdsService")
        except Exception as e:
            log.error(f"  Could not connect: {e}")
            continue

        acct_rows = 0
        for chunk_start, chunk_end in chunks:
            log.info(f"  Chunk {chunk_start} → {chunk_end} ...")
            try:
                n = sync_adgroup_daily(
                    client, ga_service, conn, account,
                    chunk_start, chunk_end
                )
                acct_rows += n
                log.info(f"    {n:,} rows inserted/updated")
            except Exception as e:
                log.error(f"    ERROR: {e}")
                continue

        log.info(f"  {account['vertical']}: {acct_rows:,} total rows")
        total_rows += acct_rows

    # ── Verification ──────────────────────────────────────────────────
    log.info("\n[4] Verification:")
    rows = conn.execute("""
        SELECT
            a.platform_account_id        AS account_id,
            a.account_name,
            COUNT(DISTINCT agpd.date)         AS dates,
            MIN(agpd.date)                    AS min_date,
            MAX(agpd.date)                    AS max_date,
            COUNT(DISTINCT agpd.ad_group_id)  AS ad_groups,
            COUNT(*)                          AS rows
        FROM ad_group_performance_daily agpd
        JOIN accounts a ON agpd.account_id = a.id
        WHERE agpd.device = 'ALL'
        GROUP BY agpd.account_id
        ORDER BY a.platform_account_id
    """).fetchall()

    log.info(f"  {'account_id':<18} {'dates':>6} {'min':>12} {'max':>12} "
             f"{'ad_groups':>10} {'rows':>8}")
    log.info(f"  {'─'*18} {'─'*6} {'─'*12} {'─'*12} {'─'*10} {'─'*8}")
    for r in rows:
        log.info(f"  {str(r['account_id']):<18} {r['account_name']:<20} {r['dates']:>6,} "
                 f"{str(r['min_date']):>12} {str(r['max_date']):>12} "
                 f"{r['ad_groups']:>10,} {r['rows']:>8,}")

    conn.close()
    log.info(f"\nTotal rows synced: {total_rows:,}")
    log.info("Done.")
    log.info("\nNext step: python analysis/analysis_campaign_keyword_roas.py")


if __name__ == "__main__":
    main()
