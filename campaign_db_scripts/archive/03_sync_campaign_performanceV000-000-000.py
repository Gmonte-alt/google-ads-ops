"""
03_sync_campaign_performance.py
================================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Syncs campaign performance at three grains for ALL active accounts in .env.

Alignment with existing keyword tracking schema (kw_db_scripts/):
  - load_dotenv() with no path arg
  - GoogleAdsClient.load_from_storage() with no arguments
  - get_db_connection() with PRAGMA foreign_keys=ON and journal_mode=WAL
  - account_id columns store platform_account_id string (no FK declaration)

────────────────────────────────────────────────────────────────────────────────
GRAINS

  campaign_performance_daily       (FROM campaign)
    - One row per campaign × date × device
    - Includes impression share + budget + bid strategy snapshots
    - IS sentinel value -1 stored as NULL via safe_is()

  campaign_performance_hourly      (FROM campaign)
    - One row per campaign × date × hour (0–23 account local time) × device
    - No IS — API error if IS fields requested with segments.hour
    - No bid strategy snapshots — targets stable within a day

  campaign_performance_geo_daily   (FROM geographic_view)
    - One row per campaign × date × device × state
    - Source: geographic_view, NOT campaign (campaign resource does not support
      any segments.geo_target_* fields — API returns UNRECOGNIZED_FIELD error)
    - Segment: segments.geo_target_state returns "geoTargetConstants/NNNNN"
      resource name string; integer is extracted and stored as geo_target_constant_id
    - campaign.status must appear in SELECT because it is used in WHERE
      (geographic_view-specific GAQL requirement)
    - No IS — not available from geographic_view (API constraint)
    - Totals slightly lower than daily table — geographic_view omits rows with
      no geo association. Matches Google Ads UI "Matched Locations" behavior.

────────────────────────────────────────────────────────────────────────────────
USAGE

  Initial backfill — all three grains separately (recommended for large ranges):
    python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only
    python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-only
    python 03_sync_campaign_performance.py --hourly-days 90 --hourly-only

  Daily cron — all three grains, yesterday (no args needed):
    python 03_sync_campaign_performance.py

  Single grain overrides:
    python 03_sync_campaign_performance.py --daily-only
    python 03_sync_campaign_performance.py --hourly-only
    python 03_sync_campaign_performance.py --geo-only

  Specific date range for geo:
    python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-end 2025-06-30

────────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
import os
import argparse
import logging
from pathlib import Path
from datetime import date, timedelta, datetime, timezone
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient


# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID  = os.getenv("GOOGLE_ADS_MCC_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    """Matches the connection helper pattern across all kw_db_scripts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Account discovery ──────────────────────────────────────────────────────────

def discover_accounts() -> list[dict]:
    """
    Loads all active GOOGLE_ADS_CUSTOMER_ID_* entries from the environment.
    Commented-out lines in .env are not loaded by dotenv — automatically excluded.
    """
    accounts = []
    prefix   = "GOOGLE_ADS_CUSTOMER_ID_"

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


# ── Google Ads client ──────────────────────────────────────────────────────────

def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    """Matches GoogleAdsClient.load_from_storage() pattern from kw_db_scripts."""
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── GAQL Query Templates ───────────────────────────────────────────────────────

# Daily: FROM campaign
# IS fields intentionally absent from hourly query — API error if combined
# with segments.hour. IS is only available at daily grain.
DAILY_QUERY_TEMPLATE = """
    SELECT
        campaign.id,
        campaign.status,
        segments.date,
        segments.device,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.conversions_value,
        metrics.view_through_conversions,
        metrics.all_conversions,
        metrics.all_conversions_value,
        metrics.search_impression_share,
        metrics.search_budget_lost_impression_share,
        metrics.search_rank_lost_impression_share,
        metrics.search_exact_match_impression_share,
        campaign_budget.amount_micros,
        campaign.bidding_strategy_type,
        campaign.bidding_strategy,
        campaign.target_cpa.target_cpa_micros,
        campaign.target_roas.target_roas
    FROM campaign
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id, segments.date, segments.device
"""

# Hourly: FROM campaign
# IS fields must NOT be included — causes CANNOT_USE_DURING_SEGMENT error.
HOURLY_QUERY_TEMPLATE = """
    SELECT
        campaign.id,
        segments.date,
        segments.hour,
        segments.device,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.conversions_value,
        metrics.all_conversions,
        metrics.all_conversions_value
    FROM campaign
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id, segments.date, segments.hour, segments.device
"""

# Geo daily: FROM geographic_view
# Key API constraints confirmed via diagnostic (diag_geo_constants_coverage.py):
#   1. Resource must be geographic_view — campaign resource does not support
#      any segments.geo_target_* fields (UNRECOGNIZED_FIELD error)
#   2. segments.geo_target_state is the correct state segment field
#   3. Returns resource name string "geoTargetConstants/NNNNN" — integer
#      extracted via split("/")[-1] in sync function
#   4. campaign.status must appear in SELECT because it is used in WHERE
#      (geographic_view-specific GAQL requirement)
#   5. IS metrics are not available from geographic_view (API constraint)
GEO_DAILY_QUERY_TEMPLATE = """
    SELECT
        campaign.id,
        campaign.status,
        segments.date,
        segments.device,
        segments.geo_target_state,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.conversions_value,
        metrics.all_conversions,
        metrics.all_conversions_value
    FROM geographic_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id, segments.date, segments.device, segments.geo_target_state
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()

def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()

def safe_is(val) -> float | None:
    """
    Convert a Google Ads IS metric to a storable float.
    Google returns -1 as a sentinel for 'below reporting threshold'.
    Stored as NULL so AVG() and other aggregates are not corrupted.
    Valid IS values are 0.0–1.0 (e.g. 0.72 = 72% impression share).
    """
    if val is None:
        return None
    f = float(val)
    return None if f < 0 else f

def resource_id(resource_name: str) -> str | None:
    """Extract trailing numeric ID from a Google Ads resource name string."""
    if not resource_name:
        return None
    parts = resource_name.split("/")
    return parts[-1] if parts and parts[-1].isdigit() else None

def parse_geo_state_id(geo_target_state: str) -> int | None:
    """
    Extract the integer criterion ID from a geo_target_state resource name.
    e.g. "geoTargetConstants/21137" -> 21137
    Returns None if the string is empty or cannot be parsed.
    """
    if not geo_target_state:
        return None
    try:
        return int(geo_target_state.split("/")[-1])
    except (ValueError, IndexError):
        return None


# ── Geo state name lookup cache ────────────────────────────────────────────────

def build_geo_state_cache(conn) -> dict[int, str]:
    """
    Builds a dict mapping geo_target_constants.criterion_id -> name
    for all State-type US geo constants.
    Called once per sync run and reused across all accounts.
    Returns empty dict if geo_target_constants table is missing or empty.
    """
    try:
        rows = conn.execute("""
            SELECT criterion_id, name
            FROM geo_target_constants
            WHERE target_type = 'State'
              AND country_code = 'US'
        """).fetchall()
        return {row["criterion_id"]: row["name"] for row in rows}
    except Exception:
        return {}


# ── Sync: daily ───────────────────────────────────────────────────────────────

def sync_daily_for_account(
    client,
    ga_service,
    conn,
    account: dict,
    start: str,
    end: str,
) -> int:
    """
    Syncs campaign performance at daily × device grain, then derives an
    ALL-device rollup row per campaign × date.

    WHY THE ALL ROLLUP:
      Every other performance table in the schema (keyword_performance_daily,
      search_query_performance) uses device='ALL' as the clean daily aggregate.
      Adding the same sentinel here keeps all analysis queries consistent --
      no special-casing needed when joining campaign data to keyword data.

    IMPRESSION SHARE HANDLING:
      The Google Ads API returns IS metrics on each device row, but IS is a
      campaign-level blended figure -- the same value appears on MOBILE,
      DESKTOP, and TABLET rows for the same campaign-date. IS cannot be summed
      or averaged across devices. The ALL rollup carries the IS value from the
      first device row with a non-NULL IS for that campaign-date. Volume metrics
      (impressions, clicks, cost, conversions) are summed normally.
    """
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = DAILY_QUERY_TEMPLATE.format(start=start, end=end)

    rows = []
    for row in ga_service.search(request=request):
        c  = row.campaign
        m  = row.metrics
        s  = row.segments
        cb = row.campaign_budget

        bst  = c.bidding_strategy_type.name if c.bidding_strategy_type else None
        bsi  = resource_id(c.bidding_strategy)
        tCPA = c.target_cpa.target_cpa_micros if c.target_cpa.target_cpa_micros else None
        tROAS= c.target_roas.target_roas       if c.target_roas.target_roas       else None

        rows.append((
            str(c.id),
            account["customer_id"],
            s.date,
            s.device.name,
            m.impressions,
            m.clicks,
            m.cost_micros,
            m.conversions,
            m.conversions_value,
            m.view_through_conversions,
            m.all_conversions,
            m.all_conversions_value,
            safe_is(m.search_impression_share),
            safe_is(m.search_budget_lost_impression_share),
            safe_is(m.search_rank_lost_impression_share),
            safe_is(m.search_exact_match_impression_share),
            cb.amount_micros or None,
            bst,
            bsi,
            tCPA,
            tROAS,
            now_utc(),
        ))

    # ── Derive ALL-device rollup rows ──────────────────────────────────────
    # Group device rows by (campaign_id, date) and sum volume metrics.
    # IS fields are taken from the first non-NULL value -- they are identical
    # across devices for a given campaign-date (campaign-level blended figure).
    from collections import defaultdict
    rollup: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r[0], r[1], r[2])   # campaign_id, account_id, date
        rollup[key].append(r)

    all_rows = []
    for (campaign_id, account_id, date_val), device_rows in rollup.items():
        # Sum volume metrics (indices 4-11)
        impressions         = sum(r[4]  for r in device_rows)
        clicks              = sum(r[5]  for r in device_rows)
        cost_micros         = sum(r[6]  for r in device_rows)
        conversions         = sum(r[7]  for r in device_rows)
        conversion_value    = sum(r[8]  for r in device_rows)
        view_through        = sum(r[9]  for r in device_rows)
        all_conversions     = sum(r[10] for r in device_rows)
        all_conv_value      = sum(r[11] for r in device_rows)

        # IS: take first non-NULL value (same across all device rows)
        def first_is(idx):
            for r in device_rows:
                if r[idx] is not None:
                    return r[idx]
            return None

        is_val      = first_is(12)
        is_budget   = first_is(13)
        is_rank     = first_is(14)
        is_exact    = first_is(15)

        # Snapshot columns: same as any device row (campaign-level, not per-device)
        budget_micros = device_rows[0][16]
        bst           = device_rows[0][17]
        bsi           = device_rows[0][18]
        tCPA          = device_rows[0][19]
        tROAS         = device_rows[0][20]

        all_rows.append((
            campaign_id, account_id, date_val, 'ALL',
            impressions, clicks, cost_micros,
            conversions, conversion_value, view_through,
            all_conversions, all_conv_value,
            is_val, is_budget, is_rank, is_exact,
            budget_micros, bst, bsi, tCPA, tROAS,
            now_utc(),
        ))

    insert_sql = """
        INSERT OR REPLACE INTO campaign_performance_daily (
            campaign_id,
            account_id,
            date,
            device,
            impressions,
            clicks,
            cost_micros,
            conversions,
            conversion_value,
            view_through_conversions,
            all_conversions,
            all_conversion_value,
            search_impression_share,
            search_budget_lost_is,
            search_rank_lost_is,
            search_exact_match_is,
            budget_micros_that_day,
            bidding_strategy_type_that_day,
            bidding_strategy_id_that_day,
            target_cpa_micros_that_day,
            target_roas_that_day,
            last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    cursor = conn.cursor()
    cursor.executemany(insert_sql, rows)          # device-level rows
    cursor.executemany(insert_sql, all_rows)      # ALL rollup rows
    conn.commit()
    return len(rows) + len(all_rows)


# ── Sync: hourly ──────────────────────────────────────────────────────────────

def sync_hourly_for_account(
    client,
    ga_service,
    conn,
    account: dict,
    start: str,
    end: str,
) -> int:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = HOURLY_QUERY_TEMPLATE.format(start=start, end=end)

    rows = []
    for row in ga_service.search(request=request):
        c = row.campaign
        m = row.metrics
        s = row.segments

        rows.append((
            str(c.id),
            account["customer_id"],
            s.date,
            s.hour,
            s.device.name,
            m.impressions,
            m.clicks,
            m.cost_micros,
            m.conversions,
            m.conversions_value,
            m.all_conversions,
            m.all_conversions_value,
            now_utc(),
        ))

    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO campaign_performance_hourly (
            campaign_id,
            account_id,
            date,
            hour,
            device,
            impressions,
            clicks,
            cost_micros,
            conversions,
            conversion_value,
            all_conversions,
            all_conversion_value,
            last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    return len(rows)


# ── Sync: geo daily ───────────────────────────────────────────────────────────

def sync_geo_daily_for_account(
    client,
    ga_service,
    conn,
    account: dict,
    start: str,
    end: str,
    geo_cache: dict[int, str],
) -> tuple[int, int]:
    """
    Syncs campaign_performance_geo_daily from geographic_view.

    Returns (rows_upserted, unmatched_geo_ids) so the caller can surface
    any criterion IDs that had no match in geo_target_constants.
    unmatched > 0 means geo_target_constants needs refreshing via
    kw_db_scripts/02_sync_keyword_structure.py.
    """
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = GEO_DAILY_QUERY_TEMPLATE.format(start=start, end=end)

    rows     = []
    unmatched = set()

    for row in ga_service.search(request=request):
        c = row.campaign
        m = row.metrics
        s = row.segments

        geo_id = parse_geo_state_id(s.geo_target_state)
        if geo_id is None:
            continue

        geo_state = geo_cache.get(geo_id)   # None if no match — stored as NULL
        if geo_state is None:
            unmatched.add(geo_id)

        rows.append((
            str(c.id),
            account["customer_id"],
            s.date,
            s.device.name,
            geo_id,
            geo_state,          # NULL if not in geo_target_constants
            m.impressions,
            m.clicks,
            m.cost_micros,
            m.conversions,
            m.conversions_value,
            m.all_conversions,
            m.all_conversions_value,
            now_utc(),
        ))

    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO campaign_performance_geo_daily (
            campaign_id,
            account_id,
            date,
            device,
            geo_target_constant_id,
            geo_state,
            impressions,
            clicks,
            cost_micros,
            conversions,
            conversion_value,
            all_conversions,
            all_conversion_value,
            last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    return len(rows), len(unmatched)


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sync campaign performance (daily + hourly + geo) for all accounts.\n\n"
            "Initial backfill — run each grain separately:\n"
            "  python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only\n"
            "  python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-only\n"
            "  python 03_sync_campaign_performance.py --hourly-days 90 --hourly-only\n\n"
            "Daily cron — all three grains, yesterday:\n"
            "  python 03_sync_campaign_performance.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    daily = parser.add_argument_group("Daily grain (FROM campaign)")
    daily.add_argument("--daily-start", metavar="YYYY-MM-DD", default=None,
                       help="Start date for daily sync. Default: yesterday.")
    daily.add_argument("--daily-end",   metavar="YYYY-MM-DD", default=None,
                       help="End date for daily sync. Default: yesterday.")

    hourly = parser.add_argument_group("Hourly grain (FROM campaign)")
    hourly.add_argument("--hourly-days",  type=int, metavar="N", default=None,
                        help="Pull last N days for hourly sync. Default: 1 (yesterday).")
    hourly.add_argument("--hourly-start", metavar="YYYY-MM-DD", default=None,
                        help="Start date for hourly sync (alternative to --hourly-days).")
    hourly.add_argument("--hourly-end",   metavar="YYYY-MM-DD", default=None,
                        help="End date for hourly sync. Default: yesterday.")

    geo = parser.add_argument_group("Geo daily grain (FROM geographic_view)")
    geo.add_argument("--geo-start", metavar="YYYY-MM-DD", default=None,
                     help="Start date for geo daily sync. Default: yesterday.")
    geo.add_argument("--geo-end",   metavar="YYYY-MM-DD", default=None,
                     help="End date for geo daily sync. Default: yesterday.")
    geo.add_argument("--geo-days",  type=int, metavar="N", default=None,
                     help="Pull last N days for geo sync (alternative to --geo-start).")

    grain = parser.add_mutually_exclusive_group()
    grain.add_argument("--daily-only",  action="store_true",
                       help="Sync daily grain only.")
    grain.add_argument("--hourly-only", action="store_true",
                       help="Sync hourly grain only.")
    grain.add_argument("--geo-only",    action="store_true",
                       help="Sync geo daily grain only.")

    return parser.parse_args()


def resolve_dates(args) -> tuple[
    tuple[str, str],
    tuple[str, str],
    tuple[str, str],
]:
    """Returns (daily_range, hourly_range, geo_range) each as (start, end)."""
    yday = yesterday()

    # Daily
    d_start = args.daily_start or yday
    d_end   = args.daily_end   or yday

    # Hourly
    if args.hourly_start:
        h_start = args.hourly_start
        h_end   = args.hourly_end or yday
    elif args.hourly_days:
        h_start = days_ago(args.hourly_days)
        h_end   = yday
    else:
        h_start = yday
        h_end   = yday

    # Geo
    if args.geo_start:
        g_start = args.geo_start
        g_end   = args.geo_end or yday
    elif args.geo_days:
        g_start = days_ago(args.geo_days)
        g_end   = yday
    else:
        g_start = yday
        g_end   = yday

    return (d_start, d_end), (h_start, h_end), (g_start, g_end)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    (d_start, d_end), (h_start, h_end), (g_start, g_end) = resolve_dates(args)

    # Determine which grains are active
    run_daily  = not (args.hourly_only or args.geo_only)
    run_hourly = not (args.daily_only  or args.geo_only)
    run_geo    = not (args.daily_only  or args.hourly_only)

    accounts = discover_accounts()
    log.info(f"Accounts : {len(accounts)} active account(s) from .env")
    if run_daily:
        log.info(f"Daily    : {d_start} → {d_end}")
    if run_hourly:
        log.info(f"Hourly   : {h_start} → {h_end}")
    if run_geo:
        log.info(f"Geo      : {g_start} → {g_end}")
    log.info("")

    conn = get_db_connection()

    # Build geo state name cache once — reused across all accounts
    geo_cache = {}
    if run_geo:
        geo_cache = build_geo_state_cache(conn)
        log.info(f"Geo cache: {len(geo_cache)} US state constants loaded.")
        if not geo_cache:
            log.warning(
                "  geo_target_constants table is empty or missing. "
                "geo_state will be NULL on all geo rows. "
                "Run kw_db_scripts/02_sync_keyword_structure.py to populate it."
            )
        log.info("")

    total_daily   = 0
    total_hourly  = 0
    total_geo     = 0
    total_unmatched = 0

    for account in accounts:
        log.info(f"── {account['vertical'].upper()} ({account['customer_id']}) ────────────")

        client     = get_google_ads_client(account["login_customer_id"])
        ga_service = client.get_service("GoogleAdsService")

        if run_daily:
            log.info(f"  Daily  {d_start} → {d_end} ...")
            n = sync_daily_for_account(client, ga_service, conn, account, d_start, d_end)
            log.info(f"  {n:,} daily rows upserted.")
            total_daily += n

        if run_hourly:
            log.info(f"  Hourly {h_start} → {h_end} ...")
            n = sync_hourly_for_account(client, ga_service, conn, account, h_start, h_end)
            log.info(f"  {n:,} hourly rows upserted.")
            total_hourly += n

        if run_geo:
            log.info(f"  Geo    {g_start} → {g_end} ...")
            n, unmatched = sync_geo_daily_for_account(
                client, ga_service, conn, account, g_start, g_end, geo_cache
            )
            log.info(f"  {n:,} geo rows upserted.")
            if unmatched > 0:
                log.warning(
                    f"  {unmatched} geo criterion ID(s) had no match in "
                    f"geo_target_constants — stored with geo_state=NULL. "
                    f"Run kw_db_scripts/02_sync_keyword_structure.py to fix."
                )
            total_geo       += n
            total_unmatched += unmatched

        log.info("")

    log.info("── Summary ───────────────────────────────────────────────────────")
    if run_daily:
        log.info(f"  Daily rows upserted  : {total_daily:,}")
    if run_hourly:
        log.info(f"  Hourly rows upserted : {total_hourly:,}")
    if run_geo:
        log.info(f"  Geo rows upserted    : {total_geo:,}")
        if total_unmatched > 0:
            log.warning(f"  Unmatched geo IDs    : {total_unmatched} (geo_state=NULL)")

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
