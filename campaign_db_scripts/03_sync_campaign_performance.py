"""
03_sync_campaign_performance.py  [v2 — integer account_id, logging fix]
================================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Syncs campaign performance at three grains for ALL active accounts in .env.

Changes from v1:
  - account_id written as INTEGER (accounts.id) in all three performance tables,
    matching the corrected schema from 01_create_campaign_tables.py v2.
    The integer is resolved once per account via accounts lookup before any
    rows are written — same pattern as 02_sync_campaigns_adgroups.py v2.
  - Logging handler attached directly to root logger before Google Ads client
    imports, preventing the client library from silencing our output.
  - main() uses print() for key progress lines so output is always visible.
  - try/except around each API call per account so one failure doesn't stop
    all remaining accounts from syncing.

────────────────────────────────────────────────────────────────────────────────
GRAINS

  campaign_performance_daily       (FROM campaign)
    - One row per campaign × date × device
    - Plus one derived device='ALL' rollup row per campaign × date
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
    - No IS — not available from geographic_view (API constraint)
    - Totals slightly lower than daily table — geographic_view omits rows with
      no geo association. Matches Google Ads UI "Matched Locations" behavior.

────────────────────────────────────────────────────────────────────────────────
USAGE

  Initial backfill — run each grain separately (recommended for large ranges):
    python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-only
    python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-only
    python 03_sync_campaign_performance.py --hourly-days 90 --hourly-only

  Daily cron — all three grains, yesterday (no args needed):
    python 03_sync_campaign_performance.py

  Single grain overrides:
    python 03_sync_campaign_performance.py --daily-only
    python 03_sync_campaign_performance.py --hourly-only
    python 03_sync_campaign_performance.py --geo-only

  Specific date range:
    python 03_sync_campaign_performance.py --daily-start 2025-01-01 --daily-end 2025-12-31
    python 03_sync_campaign_performance.py --geo-start 2025-01-01 --geo-only

────────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
import os
import argparse
import logging
from pathlib import Path
from datetime import date, timedelta, datetime, timezone
from dotenv import load_dotenv

# ── Logging — attach handler before Google Ads client import ──────────────────
# This prevents the client library from calling basicConfig first and
# silencing all subsequent INFO output.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
log = logging.getLogger(__name__)

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402 — after logging setup

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID  = os.getenv("GOOGLE_ADS_MCC_ID")


# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(conn, platform_account_id: str) -> int | None:
    """Returns accounts.id (integer PK) for a given platform_account_id."""
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery ──────────────────────────────────────────────────────────

def discover_accounts() -> list[dict]:
    accounts = []
    prefix   = "GOOGLE_ADS_CUSTOMER_ID_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix    = key[len(prefix):]
        cid       = value.strip()
        login_key = f"GOOGLE_ADS_LOGIN_CUSTOMER_ID_{suffix}"
        login_id  = os.getenv(login_key, "").strip()
        accounts.append({
            "customer_id":       cid,
            "vertical":          suffix.lower(),
            "login_customer_id": login_id if login_id else MCC_ID,
            "is_standalone":     bool(login_id),
        })
    accounts.sort(key=lambda a: a["vertical"])
    return accounts


# ── Google Ads client ──────────────────────────────────────────────────────────

def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── GAQL Query Templates ───────────────────────────────────────────────────────

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
    """Extract integer criterion ID from 'geoTargetConstants/NNNNN'."""
    if not geo_target_state:
        return None
    try:
        return int(geo_target_state.split("/")[-1])
    except (ValueError, IndexError):
        return None


# ── Geo state name lookup cache ────────────────────────────────────────────────

def build_geo_state_cache(conn) -> dict[int, str]:
    """
    Builds {criterion_id: state_name} for all US State geo constants.
    Called once per sync run — reused across all accounts.
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
    account_id: int,        # integer accounts.id
    customer_id: str,       # Google Ads customer ID string for API call
    start: str,
    end: str,
) -> int:
    """
    Syncs campaign_performance_daily at device grain, then derives an
    ALL-device rollup row per campaign × date.

    IS fields are campaign-level blended figures — identical across all
    device rows for a given campaign-date. The ALL rollup takes the first
    non-NULL IS value; volume metrics are summed across devices.
    """
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
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
            account_id,             # INTEGER — corrected from v1
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
    from collections import defaultdict
    rollup: dict[tuple, list] = defaultdict(list)
    for r in rows:
        key = (r[0], r[1], r[2])   # campaign_id, account_id, date
        rollup[key].append(r)

    all_rows = []
    for (campaign_id, acct_id, date_val), device_rows in rollup.items():
        impressions      = sum(r[4]  for r in device_rows)
        clicks           = sum(r[5]  for r in device_rows)
        cost_micros      = sum(r[6]  for r in device_rows)
        conversions      = sum(r[7]  for r in device_rows)
        conversion_value = sum(r[8]  for r in device_rows)
        view_through     = sum(r[9]  for r in device_rows)
        all_conversions  = sum(r[10] for r in device_rows)
        all_conv_value   = sum(r[11] for r in device_rows)

        def first_is(idx):
            for r in device_rows:
                if r[idx] is not None:
                    return r[idx]
            return None

        all_rows.append((
            campaign_id, acct_id, date_val, 'ALL',
            impressions, clicks, cost_micros,
            conversions, conversion_value, view_through,
            all_conversions, all_conv_value,
            first_is(12), first_is(13), first_is(14), first_is(15),
            device_rows[0][16],   # budget_micros_that_day
            device_rows[0][17],   # bidding_strategy_type_that_day
            device_rows[0][18],   # bidding_strategy_id_that_day
            device_rows[0][19],   # target_cpa_micros_that_day
            device_rows[0][20],   # target_roas_that_day
            now_utc(),
        ))

    insert_sql = """
        INSERT OR REPLACE INTO campaign_performance_daily (
            campaign_id, account_id, date, device,
            impressions, clicks, cost_micros,
            conversions, conversion_value, view_through_conversions,
            all_conversions, all_conversion_value,
            search_impression_share, search_budget_lost_is,
            search_rank_lost_is, search_exact_match_is,
            budget_micros_that_day, bidding_strategy_type_that_day,
            bidding_strategy_id_that_day, target_cpa_micros_that_day,
            target_roas_that_day, last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    cursor = conn.cursor()
    cursor.executemany(insert_sql, rows)        # device-level rows
    cursor.executemany(insert_sql, all_rows)    # ALL rollup rows
    conn.commit()
    return len(rows) + len(all_rows)


# ── Sync: hourly ──────────────────────────────────────────────────────────────

def sync_hourly_for_account(
    client,
    ga_service,
    conn,
    account_id: int,        # integer accounts.id
    customer_id: str,
    start: str,
    end: str,
) -> int:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = HOURLY_QUERY_TEMPLATE.format(start=start, end=end)

    rows = []
    for row in ga_service.search(request=request):
        c = row.campaign
        m = row.metrics
        s = row.segments
        rows.append((
            str(c.id),
            account_id,             # INTEGER — corrected from v1
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
            campaign_id, account_id, date, hour, device,
            impressions, clicks, cost_micros,
            conversions, conversion_value,
            all_conversions, all_conversion_value,
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
    account_id: int,        # integer accounts.id
    customer_id: str,
    start: str,
    end: str,
    geo_cache: dict[int, str],
) -> tuple[int, int]:
    """
    Returns (rows_upserted, unmatched_geo_ids).
    unmatched > 0 means geo_target_constants needs refreshing.
    """
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = GEO_DAILY_QUERY_TEMPLATE.format(start=start, end=end)

    rows      = []
    unmatched = set()

    for row in ga_service.search(request=request):
        c = row.campaign
        m = row.metrics
        s = row.segments

        geo_id = parse_geo_state_id(s.geo_target_state)
        if geo_id is None:
            continue

        geo_state = geo_cache.get(geo_id)
        if geo_state is None:
            unmatched.add(geo_id)

        rows.append((
            str(c.id),
            account_id,             # INTEGER — corrected from v1
            s.date,
            s.device.name,
            geo_id,
            geo_state,
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
            campaign_id, account_id, date, device,
            geo_target_constant_id, geo_state,
            impressions, clicks, cost_micros,
            conversions, conversion_value,
            all_conversions, all_conversion_value,
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
            "  python 03_sync_campaign_performance.py "
            "--daily-start 2025-01-01 --daily-only\n"
            "  python 03_sync_campaign_performance.py "
            "--geo-start 2025-01-01 --geo-only\n"
            "  python 03_sync_campaign_performance.py "
            "--hourly-days 90 --hourly-only\n\n"
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
                        help="Pull last N days for hourly. Default: 1 (yesterday).")
    hourly.add_argument("--hourly-start", metavar="YYYY-MM-DD", default=None,
                        help="Start date for hourly sync.")
    hourly.add_argument("--hourly-end",   metavar="YYYY-MM-DD", default=None,
                        help="End date for hourly sync. Default: yesterday.")

    geo = parser.add_argument_group("Geo daily grain (FROM geographic_view)")
    geo.add_argument("--geo-start", metavar="YYYY-MM-DD", default=None,
                     help="Start date for geo sync. Default: yesterday.")
    geo.add_argument("--geo-end",   metavar="YYYY-MM-DD", default=None,
                     help="End date for geo sync. Default: yesterday.")
    geo.add_argument("--geo-days",  type=int, metavar="N", default=None,
                     help="Pull last N days for geo sync.")

    grain = parser.add_mutually_exclusive_group()
    grain.add_argument("--daily-only",  action="store_true")
    grain.add_argument("--hourly-only", action="store_true")
    grain.add_argument("--geo-only",    action="store_true")

    parser.add_argument("--accounts", metavar="LABELS", default="home,term_life",
                        help="Comma-separated vertical labels to sync "
                             "(default: home,term_life -- scope-by-responsibility "
                             "ruling 2026-09-24; 'all' = every .env account). "
                             "Unknown labels abort.")

    return parser.parse_args()


def resolve_dates(args):
    """Returns (daily_range, hourly_range, geo_range) each as (start, end)."""
    yday = yesterday()

    d_start = args.daily_start or yday
    d_end   = args.daily_end   or yday

    if args.hourly_start:
        h_start = args.hourly_start
        h_end   = args.hourly_end or yday
    elif args.hourly_days:
        h_start = days_ago(args.hourly_days)
        h_end   = yday
    else:
        h_start = yday
        h_end   = yday

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

    run_daily  = not (args.hourly_only or args.geo_only)
    run_hourly = not (args.daily_only  or args.geo_only)
    run_geo    = not (args.daily_only  or args.hourly_only)

    print("=" * 65)
    print("03_sync_campaign_performance.py  [v2]")
    print(f"Started  : {now_utc()}")
    print("=" * 65)

    accounts = discover_accounts()
    if args.accounts.strip().lower() != "all":
        wanted = {x.strip().lower() for x in args.accounts.split(",") if x.strip()}
        known = {a["vertical"] for a in accounts}
        unknown = wanted - known
        if unknown:
            raise SystemExit(f"[ABORT] unknown --accounts label(s): {sorted(unknown)}; "
                             f"known: {sorted(known)}")
        accounts = [a for a in accounts if a["vertical"] in wanted]
    print(f"\nAccounts : {len(accounts)} in scope "
          f"({', '.join(a['vertical'] for a in accounts)}) "
          f"[--accounts {args.accounts}]")
    if run_daily:
        print(f"Daily    : {d_start} → {d_end}")
    if run_hourly:
        print(f"Hourly   : {h_start} → {h_end}")
    if run_geo:
        print(f"Geo      : {g_start} → {g_end}")

    conn = get_db_connection()

    geo_cache = {}
    if run_geo:
        geo_cache = build_geo_state_cache(conn)
        print(f"\nGeo cache: {len(geo_cache)} US state constants loaded.")
        if not geo_cache:
            print("  WARNING: geo_target_constants is empty — geo_state will be NULL.")

    total_daily     = 0
    total_hourly    = 0
    total_geo       = 0
    total_unmatched = 0
    failures        = []

    for account in accounts:
        cid = account["customer_id"]
        print(f"\n── {account['vertical'].upper()} ({cid}) ──────────────────")

        # Resolve integer account_id once per account
        account_id = get_account_internal_id(conn, cid)
        if account_id is None:
            print(f"  ERROR: accounts.id not found for {cid} — skipping.")
            failures.append(cid)
            continue
        print(f"  accounts.id = {account_id}")

        try:
            client     = get_google_ads_client(account["login_customer_id"])
            ga_service = client.get_service("GoogleAdsService")
        except Exception as e:
            print(f"  ERROR: Could not create client: {e}")
            failures.append(cid)
            continue

        if run_daily:
            print(f"  Daily  {d_start} → {d_end} ...", end=" ", flush=True)
            try:
                n = sync_daily_for_account(
                    client, ga_service, conn, account_id, cid, d_start, d_end
                )
                print(f"{n:,} rows")
                total_daily += n
            except Exception as e:
                print(f"\n  ERROR (daily): {e}")
                failures.append(f"{cid}:daily")

        if run_hourly:
            print(f"  Hourly {h_start} → {h_end} ...", end=" ", flush=True)
            try:
                n = sync_hourly_for_account(
                    client, ga_service, conn, account_id, cid, h_start, h_end
                )
                print(f"{n:,} rows")
                total_hourly += n
            except Exception as e:
                print(f"\n  ERROR (hourly): {e}")
                failures.append(f"{cid}:hourly")

        if run_geo:
            print(f"  Geo    {g_start} → {g_end} ...", end=" ", flush=True)
            try:
                n, unmatched = sync_geo_daily_for_account(
                    client, ga_service, conn, account_id, cid,
                    g_start, g_end, geo_cache
                )
                print(f"{n:,} rows", end="")
                if unmatched > 0:
                    print(f"  ({unmatched} unmatched geo IDs — stored with geo_state=NULL)")
                else:
                    print()
                total_geo       += n
                total_unmatched += unmatched
            except Exception as e:
                print(f"\n  ERROR (geo): {e}")
                failures.append(f"{cid}:geo")

    print(f"\n{'='*65}")
    print("SYNC COMPLETE")
    print(f"{'='*65}")
    if run_daily:
        print(f"  Daily rows upserted  : {total_daily:,}")
    if run_hourly:
        print(f"  Hourly rows upserted : {total_hourly:,}")
    if run_geo:
        print(f"  Geo rows upserted    : {total_geo:,}")
        if total_unmatched > 0:
            print(f"  Unmatched geo IDs    : {total_unmatched} (geo_state=NULL)")
    if failures:
        print(f"  Failures ({len(failures)}):")
        for f in failures:
            print(f"    {f}")

    conn.close()
    print(f"\nDone. {now_utc()}")


if __name__ == "__main__":
    main()
