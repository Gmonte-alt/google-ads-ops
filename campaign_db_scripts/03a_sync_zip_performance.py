"""
03a_sync_zip_performance.py  [v1 — Phase 2 zip-grain performance sync]
===========================
Term Life Zip-Targeting Subsystem — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Performance sync (sub-bullet of the 03 performance syncs). Daily routine
placement: AFTER the structure block (02 + 02a) — structure-sync scope
always covers performance-sync scope. Table contract and doc lines live in
01b_create_zip_performance_table.py.

WHAT IT DOES, per account and date range:
  1. Pulls geographic_view + segments.geo_target_postal_code at
     campaign x date x device x location_type grain (campaign.status in
     SELECT — GAQL requires WHERE-filtered attributes in SELECT).
  2. Resolves postal geo ids to zips LOCAL-FIRST:
       campaign_zip_targeting (authoritative for targeted zips)
       -> geo_target_constants (postal rows)
       -> API fallback in chunks for the remainder (served zips are a
          SUPERSET of targeted — CR45 serves via country-minus-negatives).
     Unresolvable ids, or names failing ^\\d{5}$, are STORED with zip=NULL
     and counted loudly — spend is never dropped for a naming problem.
     Rows missing the postal segment entirely are skipped and counted
     (probe showed 0; the coverage check below would expose any loss).
  3. DELETE-THEN-RELOAD per month chunk, per account, each chunk in its own
     transaction (rollback restores that chunk; backfill is resumable).
     Ranges longer than ~5 weeks are split into calendar-month chunks
     automatically. Batch insert falls back to row-by-row skip-and-count on
     failure — never all-or-nothing.
  4. Per-run QA: spend coverage vs campaign_performance_daily
     (device='ALL') for the same account and range — expect ~91%
     (matched-locations behavior); a large move in that number is the
     drift alarm.

ACCEPTANCE (after backfill, week 2026-09-27 -> 2026-10-03 must reproduce
the probe): rows = 18,138 · cost = $49,204.70 · coverage ≈ 90.9% ·
location_type 100% LOCATION_OF_PRESENCE.

RUN:
  conda activate paid-search

  # dry run on the probe week first (pull + resolve + QA, writes NOTHING):
  python campaign_db_scripts\\03a_sync_zip_performance.py --account term_life --dry-run --start 2026-09-27 --end 2026-10-03

  # full backfill (floor matches campaign-grain provenance):
  python campaign_db_scripts\\03a_sync_zip_performance.py --account term_life --start 2024-10-01 > zip_perf_backfill.txt 2>&1

  # daily routine line (yesterday; AFTER 02 + 02a in the routine file):
  python campaign_db_scripts\\03a_sync_zip_performance.py --account term_life > daily_3a_zip_perf.txt 2>&1
"""

import re
import sqlite3
import os
import argparse
import logging
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from dotenv import load_dotenv

# ── Logging — attach handler before Google Ads client import ──────────────────
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

DB_PATH   = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID    = os.getenv("GOOGLE_ADS_MCC_ID")
ZIP_RE    = re.compile(r"^\d{5}$")
GEO_CHUNK = 1000
CHUNK_THRESHOLD_DAYS = 35   # ranges longer than this split into month chunks


# ── Database ──────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(conn, platform_account_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery (same pattern as 02/03) ─────────────────────────────────

def discover_accounts(account_filter: str | None = None) -> list[dict]:
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
    if account_filter:
        wanted = account_filter.lower()
        accounts = [a for a in accounts if a["vertical"] == wanted]
    return accounts


def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def resource_tail(resource_name: str) -> str | None:
    if not resource_name:
        return None
    parts = str(resource_name).split("/")
    return parts[-1] if parts else None


def micros_to_dollars(m) -> float:
    return (m or 0) / 1e6


def month_chunks(start: str, end: str) -> list[tuple[str, str]]:
    """
    Splits [start, end] into calendar-month chunks when the range exceeds
    CHUNK_THRESHOLD_DAYS; otherwise returns it whole.
    """
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if s > e:
        raise SystemExit(f"[ABORT] start {start} is after end {end}")
    if (e - s).days <= CHUNK_THRESHOLD_DAYS:
        return [(start, end)]
    chunks = []
    cur = s
    while cur <= e:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, e)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + timedelta(days=1)
    return chunks


# ── Zip resolution (local-first, API fallback) ────────────────────────────────

def build_local_zip_cache(conn) -> dict[int, str]:
    """
    {geo_target_constant_id: zip} from (1) campaign_zip_targeting —
    authoritative for targeted zips — and (2) geo_target_constants postal
    rows. Served zips exceed both; the remainder goes to the API.
    """
    cache: dict[int, str] = {}
    try:
        for r in conn.execute(
            "SELECT criterion_id, name, target_type FROM geo_target_constants"):
            if "postal" in (r["target_type"] or "").lower():
                cache[r["criterion_id"]] = r["name"]
    except Exception as e:
        print(f"  WARNING: could not read geo_target_constants: {e}")
    try:
        for r in conn.execute("""
            SELECT DISTINCT geo_target_constant_id AS gid, zip
            FROM campaign_zip_targeting
            WHERE zip IS NOT NULL AND geo_target_constant_id IS NOT NULL"""):
            cache[r["gid"]] = r["zip"]
    except Exception as e:
        print(f"  WARNING: could not read campaign_zip_targeting: {e}")
    return cache


def api_resolve_zips(client, ga_service, customer_id: str,
                     geo_ids: list[int]) -> dict[int, str]:
    """API fallback: resolve postal geo ids to names, chunked."""
    out: dict[int, str] = {}
    for i in range(0, len(geo_ids), GEO_CHUNK):
        batch = geo_ids[i:i + GEO_CHUNK]
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = f"""
            SELECT geo_target_constant.id, geo_target_constant.name
            FROM geo_target_constant
            WHERE geo_target_constant.id IN ({", ".join(str(x) for x in batch)})
        """
        for row in ga_service.search(request=request):
            out[int(row.geo_target_constant.id)] = row.geo_target_constant.name
    return out


# ── Pull + build (one chunk) ──────────────────────────────────────────────────

ZIP_PERF_QUERY = """
    SELECT
        campaign.id,
        campaign.status,
        segments.date,
        segments.device,
        geographic_view.location_type,
        segments.geo_target_postal_code,
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
"""

INSERT_SQL = """
    INSERT OR REPLACE INTO campaign_performance_zip_daily (
        account_id, campaign_id, date, device, location_type,
        geo_target_constant_id, zip,
        impressions, clicks, cost_micros,
        conversions, conversion_value,
        all_conversions, all_conversion_value,
        last_synced_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def pull_chunk(client, ga_service, customer_id: str,
               start: str, end: str) -> tuple[list[dict], int]:
    """Returns (raw rows, no_postal_skipped)."""
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = ZIP_PERF_QUERY.format(start=start, end=end)
    raw, no_postal = [], 0
    for row in ga_service.search(request=request):
        gid = resource_tail(row.segments.geo_target_postal_code)
        if gid is None or not gid.isdigit():
            no_postal += 1
            continue
        m = row.metrics
        raw.append({
            "campaign_id":   str(row.campaign.id),
            "date":          row.segments.date,
            "device":        row.segments.device.name,
            "location_type": row.geographic_view.location_type.name,
            "geo_id":        int(gid),
            "impressions":   m.impressions,
            "clicks":        m.clicks,
            "cost_micros":   m.cost_micros,
            "conversions":   m.conversions,
            "conv_value":    m.conversions_value,
            "all_conv":      m.all_conversions,
            "all_conv_val":  m.all_conversions_value,
        })
    return raw, no_postal


def build_chunk_rows(raw: list[dict], zip_cache: dict[int, str],
                     account_id: int, cnt: dict) -> list[tuple]:
    ts = now_utc()
    rows = []
    for r in raw:
        name = zip_cache.get(r["geo_id"])
        if name is None:
            zip_val = None
            cnt["zip_null_unresolved"] += 1
        elif not ZIP_RE.match(name):
            zip_val = None
            cnt["zip_null_invalid_name"] += 1
        else:
            zip_val = name
        rows.append((
            account_id, r["campaign_id"], r["date"], r["device"],
            r["location_type"], r["geo_id"], zip_val,
            r["impressions"], r["clicks"], r["cost_micros"],
            r["conversions"], r["conv_value"],
            r["all_conv"], r["all_conv_val"], ts,
        ))
        cnt["cost_micros"] += r["cost_micros"]
        cnt["loc_" + r["location_type"]] += 1
    return rows


def load_chunk(conn, account_id: int, start: str, end: str,
               rows: list[tuple]) -> tuple[int, int]:
    """Delete-then-reload for one chunk, in one transaction."""
    failed = 0
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.execute("""
            DELETE FROM campaign_performance_zip_daily
            WHERE account_id = ? AND date BETWEEN ? AND ?
        """, (account_id, start, end))
        try:
            cur.executemany(INSERT_SQL, rows)
            loaded = len(rows)
        except Exception as batch_err:
            print(f"    WARNING: batch insert failed ({batch_err}) — "
                  f"row-by-row skip-and-count fallback.")
            loaded = 0
            for r in rows:
                try:
                    cur.execute(INSERT_SQL, r)
                    loaded += 1
                except Exception as row_err:
                    failed += 1
                    if failed <= 20:
                        print(f"      ROW FAILED: campaign {r[1]} {r[2]} "
                              f"geo {r[5]}: {row_err}")
        conn.commit()
        return loaded, failed
    except Exception:
        conn.rollback()
        raise


# ── QA: spend coverage vs campaign truth ──────────────────────────────────────

def coverage_check(conn, account_id: int, start: str, end: str,
                   zip_cost: float):
    try:
        row = conn.execute("""
            SELECT SUM(cost_micros) AS cost
            FROM campaign_performance_daily
            WHERE account_id = ? AND device = 'ALL'
              AND date BETWEEN ? AND ?
        """, (account_id, start, end)).fetchone()
        cpd_cost = micros_to_dollars(row["cost"])
        pct = (zip_cost / cpd_cost * 100) if cpd_cost else 0.0
        print(f"\n  Spend coverage vs campaign_performance_daily:")
        print(f"    cpd cost ({start} → {end}) : ${cpd_cost:,.2f}")
        print(f"    zip-grain cost             : ${zip_cost:,.2f}  "
              f"({pct:.1f}% coverage; ~91% expected)")
        if cpd_cost and (pct < 80 or pct > 100.5):
            print(f"    !!! COVERAGE OUT OF BAND — investigate before "
                  f"trusting this range.")
    except Exception as e:
        print(f"\n  ERROR (coverage check): {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync zip-grain campaign performance (geographic_view + "
                    "postal segment) into campaign_performance_zip_daily.")
    parser.add_argument("--account", default="term_life", metavar="VERTICAL",
                        help="Vertical label to sync (default: term_life).")
    parser.add_argument("--start", metavar="YYYY-MM-DD", default=None,
                        help="Range start. Default: yesterday.")
    parser.add_argument("--end", metavar="YYYY-MM-DD", default=None,
                        help="Range end. Default: yesterday.")
    parser.add_argument("--days", type=int, metavar="N", default=None,
                        help="Pull last N days (alternative to --start).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pull + resolve + QA; write NOTHING.")
    args = parser.parse_args()

    if args.start:
        start = args.start
        end   = args.end or yesterday()
    elif args.days:
        start = (date.today() - timedelta(days=args.days)).isoformat()
        end   = yesterday()
    else:
        start = end = yesterday()

    print("=" * 65)
    print("03a_sync_zip_performance.py  [v1 — Phase 2]")
    print(f"Database : {DB_PATH}")
    print(f"Started  : {now_utc()}")
    print(f"Account  : {args.account}")
    print(f"Range    : {start} → {end}")
    if args.dry_run:
        print("Mode     : DRY RUN — no writes")
    print("=" * 65)

    accounts = discover_accounts(account_filter=args.account)
    if not accounts:
        raise SystemExit(f"[ABORT] no GOOGLE_ADS_CUSTOMER_ID_"
                         f"{args.account.upper()} in .env")
    account = accounts[0]
    cid = account["customer_id"]
    print(f"\nCustomer : {cid}  login={account['login_customer_id']}")

    conn = get_db_connection()
    account_id = get_account_internal_id(conn, cid)
    if account_id is None:
        raise SystemExit(f"[ABORT] accounts.id not found for {cid}.")
    print(f"accounts.id = {account_id}")

    client     = get_google_ads_client(account["login_customer_id"])
    ga_service = client.get_service("GoogleAdsService")

    zip_cache = build_local_zip_cache(conn)
    print(f"Local zip cache: {len(zip_cache):,} postal ids "
          f"(campaign_zip_targeting + geo_target_constants)")

    chunks = month_chunks(start, end)
    if len(chunks) > 1:
        print(f"Range split into {len(chunks)} month chunks.")

    cnt = defaultdict(int)
    total_rows, total_loaded, total_failed, total_no_postal = 0, 0, 0, 0

    for (cs, ce) in chunks:
        print(f"\n── {cs} → {ce} ──────────────────", flush=True)
        try:
            raw, no_postal = pull_chunk(client, ga_service, cid, cs, ce)
            total_no_postal += no_postal
            print(f"  pulled: {len(raw):,} rows"
                  + (f"  (skipped {no_postal} with no postal segment)"
                     if no_postal else ""))

            # API fallback for geo ids the local cache doesn't know
            misses = sorted({r["geo_id"] for r in raw} - set(zip_cache.keys()))
            if misses:
                resolved = api_resolve_zips(client, ga_service, cid, misses)
                zip_cache.update(resolved)
                print(f"  API fallback: {len(resolved):,} of "
                      f"{len(misses):,} unknown ids resolved")

            rows = build_chunk_rows(raw, zip_cache, account_id, cnt)
            total_rows += len(rows)

            if args.dry_run:
                print(f"  [DRY RUN] would load {len(rows):,} rows.")
            else:
                loaded, failed = load_chunk(conn, account_id, cs, ce, rows)
                total_loaded += loaded
                total_failed += failed
                print(f"  loaded: {loaded:,} rows"
                      + (f"  (FAILED: {failed:,})" if failed else ""))
        except Exception as e:
            print(f"  ERROR (chunk {cs} → {ce}): {e}")
            print(f"  Chunk rolled back — re-run with "
                  f"--start {cs} --end {end} to resume.")
            raise

    # ── Summary + QA ───────────────────────────────────────────────────────
    zip_cost = micros_to_dollars(cnt["cost_micros"])
    print(f"\n{'=' * 65}\nSYNC COMPLETE\n{'=' * 65}")
    print(f"  Rows pulled/built          : {total_rows:,}")
    if not args.dry_run:
        print(f"  Rows loaded                : {total_loaded:,}"
              + (f"  (FAILED: {total_failed:,})" if total_failed else ""))
    print(f"  zip=NULL (unresolved id)   : {cnt['zip_null_unresolved']:,}")
    print(f"  zip=NULL (bad 5-digit name): {cnt['zip_null_invalid_name']:,}")
    print(f"  Skipped (no postal segment): {total_no_postal:,}")
    loc_counts = {k[4:]: v for k, v in cnt.items() if k.startswith('loc_')}
    print(f"  location_type split        : {loc_counts}")
    print(f"  Zip-grain cost             : ${zip_cost:,.2f}")
    coverage_check(conn, account_id, start, end, zip_cost)

    conn.close()
    print(f"\nDone. {now_utc()}")


if __name__ == "__main__":
    main()
