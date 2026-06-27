"""
diag_geo_constants_coverage.py
================================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Diagnostic script — read-only, makes no changes to the database or API.

Answers the question:
  Is the geo_target_constants table sufficiently populated to support
  campaign_performance_geo_daily geo ID decoding across all five accounts?

Specifically checks:

  [1] geo_target_constants table inventory
      How many rows exist, broken down by target_type.
      We need target_type = 'State' rows for all 50 US states + DC.

  [2] US state coverage
      Are all 51 expected US State-type geo constants present?
      Lists any missing states by name.

  [3] Live geo IDs from the API (one account sample)
      Pulls the actual geo_target_constant IDs that appear in campaign
      performance data for the HOME account over the last 30 days.
      Cross-references them against geo_target_constants to find any IDs
      that would fail to decode at sync time.

  [4] Cross-account spot check (optional)
      If --all-accounts flag is passed, repeats the live API check for
      all accounts, not just HOME. Takes longer but gives full confidence.

Usage:
    # Quick check (HOME account only for live API sample):
    python diag_geo_constants_coverage.py

    # Full check (all 5 accounts):
    python diag_geo_constants_coverage.py --all-accounts

    # Skip the live API check entirely (DB inspection only):
    python diag_geo_constants_coverage.py --db-only
"""

import sqlite3
import os
import argparse
import logging
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
MCC_ID  = os.getenv("GOOGLE_ADS_MCC_ID")

# Sample window for live API geo ID pull
LOOKBACK_DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Expected US states ─────────────────────────────────────────────────────────
# All 50 states + DC. These are the canonical names as Google stores them
# in geo_target_constants.name. Used to verify complete state coverage.
US_STATES_EXPECTED = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
}

# ── GAQL query to pull distinct geo IDs from live campaign performance ─────────
# Geo performance data is NOT available on the campaign resource.
# It lives in geographic_view, which is queried separately.
# segments.geo_target_state returns the integer criterion ID for the US state.
# This joins to geo_target_constants.criterion_id for decoding.
#
# Note: geographic_view only returns rows where a geo segment is non-null,
# so totals here will be slightly lower than campaign-level totals — this is
# expected and matches the Google Ads UI "Matched Locations" report.
GEO_ID_QUERY = """
    SELECT
        segments.geo_target_state,
        campaign.id,
        campaign.status,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros
    FROM geographic_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
      AND metrics.impressions > 0
    ORDER BY metrics.impressions DESC
"""

# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Account discovery ──────────────────────────────────────────────────────────

def discover_accounts() -> list[dict]:
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


def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── Check 1: geo_target_constants table inventory ─────────────────────────────

def check_constants_inventory(conn) -> dict:
    """
    Returns a summary of what's in geo_target_constants broken down by
    target_type. Also checks whether the table exists at all.
    """
    log.info("")
    log.info("=" * 65)
    log.info("[1] geo_target_constants table inventory")
    log.info("=" * 65)

    # Check table exists
    tbl = conn.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_target_constants'
    """).fetchone()

    if not tbl:
        log.error("  geo_target_constants table does not exist.")
        log.error("  Run kw_db_scripts/02_sync_keyword_structure.py first.")
        return {"table_exists": False}

    total = conn.execute("SELECT COUNT(*) FROM geo_target_constants").fetchone()[0]
    log.info(f"  Total rows : {total:,}")

    if total == 0:
        log.error("  Table is empty — geo bootstrap has not been run.")
        log.error("  Run kw_db_scripts/02_sync_keyword_structure.py to populate it.")
        return {"table_exists": True, "total": 0}

    # Breakdown by target_type
    rows = conn.execute("""
        SELECT target_type, COUNT(*) as cnt
        FROM geo_target_constants
        GROUP BY target_type
        ORDER BY cnt DESC
    """).fetchall()

    log.info(f"  {'target_type':<30} {'count':>8}")
    log.info(f"  {'─'*30} {'─'*8}")
    type_counts = {}
    for r in rows:
        log.info(f"  {(r['target_type'] or 'NULL'):<30} {r['cnt']:>8,}")
        type_counts[r["target_type"]] = r["cnt"]

    return {
        "table_exists": True,
        "total": total,
        "by_type": type_counts,
    }


# ── Check 2: US state coverage ────────────────────────────────────────────────

def check_state_coverage(conn) -> dict:
    """
    Verifies all 51 expected US State-type rows are present.
    Returns list of any missing state names.
    """
    log.info("")
    log.info("=" * 65)
    log.info("[2] US State coverage (target_type = 'State')")
    log.info("=" * 65)

    rows = conn.execute("""
        SELECT name, criterion_id, canonical_name
        FROM geo_target_constants
        WHERE target_type = 'State'
          AND country_code = 'US'
        ORDER BY name
    """).fetchall()

    found_names = {r["name"] for r in rows}
    missing     = US_STATES_EXPECTED - found_names
    extra       = found_names - US_STATES_EXPECTED

    log.info(f"  Expected US states : {len(US_STATES_EXPECTED)}")
    log.info(f"  Found in DB        : {len(found_names)}")

    if not missing:
        log.info("  ✓ All expected US states are present.")
    else:
        log.warning(f"  ✗ {len(missing)} state(s) missing from geo_target_constants:")
        for name in sorted(missing):
            log.warning(f"      {name}")

    if extra:
        log.info(f"  Note: {len(extra)} additional State rows found (territories etc.):")
        for name in sorted(extra):
            log.info(f"      {name}")

    # Sample: show first 10 state rows for sanity check
    log.info("")
    log.info("  Sample state rows (first 10):")
    log.info(f"  {'criterion_id':<15} {'name':<25} {'canonical_name'}")
    log.info(f"  {'─'*15} {'─'*25} {'─'*35}")
    for r in rows[:10]:
        log.info(
            f"  {r['criterion_id']:<15} "
            f"{r['name']:<25} "
            f"{r['canonical_name'] or ''}"
        )

    return {
        "found":   len(found_names),
        "missing": sorted(missing),
        "extra":   sorted(extra),
    }


# ── Check 3: Live geo IDs from the API ────────────────────────────────────────

def check_live_geo_ids(conn, account: dict) -> dict:
    """
    Pulls distinct geo_target_constant IDs that appear in actual campaign
    performance data for the given account over the last LOOKBACK_DAYS days.
    Cross-references against geo_target_constants to find decode gaps.
    """
    log.info("")
    log.info("=" * 65)
    log.info(f"[3] Live geo IDs from API — {account['vertical'].upper()} ({account['customer_id']})")
    log.info("=" * 65)

    end   = (date.today() - timedelta(days=1)).isoformat()
    start = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    log.info(f"  Lookback window : {start} → {end}")

    client     = get_google_ads_client(account["login_customer_id"])
    ga_service = client.get_service("GoogleAdsService")

    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = account["customer_id"]
    request.query = GEO_ID_QUERY.format(start=start, end=end)

    # Collect distinct geo state criterion IDs with aggregate impressions.
    # segments.geo_target_state returns a resource name string e.g.
    # "geoTargetConstants/21137" — extract the trailing integer for the DB lookup.
    geo_totals: dict[int, dict] = {}
    for row in ga_service.search(request=request):
        s = row.segments
        m = row.metrics

        raw = s.geo_target_state       # e.g. "geoTargetConstants/21137"
        if not raw:
            continue
        try:
            geo_id = int(raw.split("/")[-1])
        except (ValueError, IndexError):
            continue

        if geo_id not in geo_totals:
            geo_totals[geo_id] = {"impressions": 0, "clicks": 0, "cost_micros": 0}
        geo_totals[geo_id]["impressions"] += m.impressions
        geo_totals[geo_id]["clicks"]      += m.clicks
        geo_totals[geo_id]["cost_micros"] += m.cost_micros

    log.info(f"  Distinct geo IDs in API data : {len(geo_totals):,}")

    if not geo_totals:
        log.warning("  No geo-segmented data returned. Check that campaigns have "
                    "geo targeting enabled and had impressions in this window.")
        return {"geo_ids_found": 0, "unmatched": []}

    # Cross-reference against geo_target_constants
    matched   = []
    unmatched = []

    for geo_id, totals in geo_totals.items():
        db_row = conn.execute("""
            SELECT name, canonical_name, target_type, country_code
            FROM geo_target_constants
            WHERE criterion_id = ?
        """, (geo_id,)).fetchone()

        entry = {
            "geo_id":      geo_id,
            "impressions": totals["impressions"],
            "clicks":      totals["clicks"],
            "cost_micros": totals["cost_micros"],
        }

        if db_row:
            entry.update({
                "name":         db_row["name"],
                "canonical":    db_row["canonical_name"],
                "target_type":  db_row["target_type"],
                "country_code": db_row["country_code"],
            })
            matched.append(entry)
        else:
            entry.update({"name": None, "target_type": None})
            unmatched.append(entry)

    # Sort by impressions descending
    matched.sort(key=lambda x: x["impressions"], reverse=True)
    unmatched.sort(key=lambda x: x["impressions"], reverse=True)

    # Summary
    log.info(f"  Matched in geo_target_constants   : {len(matched):,}")
    log.info(f"  Unmatched (would fail to decode)  : {len(unmatched):,}")

    # Show target_type breakdown of matched IDs
    type_breakdown: dict[str, int] = {}
    for e in matched:
        t = e["target_type"] or "NULL"
        type_breakdown[t] = type_breakdown.get(t, 0) + e["impressions"]
    log.info("")
    log.info("  Impression breakdown by target_type (matched IDs):")
    log.info(f"  {'target_type':<30} {'impressions':>12}")
    log.info(f"  {'─'*30} {'─'*12}")
    for t, imps in sorted(type_breakdown.items(), key=lambda x: -x[1]):
        log.info(f"  {t:<30} {imps:>12,}")

    # Show top 15 matched State rows
    state_rows = [e for e in matched if e.get("target_type") == "State"]
    log.info("")
    log.info(f"  Top state-level geo IDs (up to 15 of {len(state_rows)} found):")
    log.info(f"  {'geo_id':<12} {'state':<25} {'impressions':>12} {'clicks':>8} {'spend':>10}")
    log.info(f"  {'─'*12} {'─'*25} {'─'*12} {'─'*8} {'─'*10}")
    for e in state_rows[:15]:
        spend = e["cost_micros"] / 1e6
        log.info(
            f"  {e['geo_id']:<12} "
            f"{e['name']:<25} "
            f"{e['impressions']:>12,} "
            f"{e['clicks']:>8,} "
            f"${spend:>9,.2f}"
        )

    # Show unmatched IDs — these would produce NULL geo_state at sync time
    if unmatched:
        log.warning("")
        log.warning(f"  Unmatched geo IDs (no row in geo_target_constants):")
        log.warning(f"  {'geo_id':<12} {'impressions':>12} {'clicks':>8}")
        log.warning(f"  {'─'*12} {'─'*12} {'─'*8}")
        for e in unmatched:
            log.warning(f"  {e['geo_id']:<12} {e['impressions']:>12,} {e['clicks']:>8,}")
        log.warning("")
        log.warning("  Action: re-run kw_db_scripts/02_sync_keyword_structure.py")
        log.warning("  to refresh geo_target_constants, or add these IDs manually.")
    else:
        log.info("")
        log.info("  ✓ All geo IDs in API data are present in geo_target_constants.")

    return {
        "geo_ids_found": len(geo_totals),
        "matched":       len(matched),
        "unmatched":     unmatched,
        "state_count":   len(state_rows),
    }


# ── Final verdict ──────────────────────────────────────────────────────────────

def print_verdict(inventory: dict, coverage: dict, api_checks: list[dict]):
    log.info("")
    log.info("=" * 65)
    log.info("VERDICT")
    log.info("=" * 65)

    issues = []

    if not inventory.get("table_exists"):
        issues.append("geo_target_constants table does not exist")
    elif inventory.get("total", 0) == 0:
        issues.append("geo_target_constants table is empty")

    if coverage.get("missing"):
        issues.append(
            f"{len(coverage['missing'])} US state(s) missing from geo_target_constants: "
            + ", ".join(coverage["missing"])
        )

    for check in api_checks:
        if check.get("unmatched"):
            n = len(check["unmatched"])
            issues.append(
                f"{n} live geo ID(s) have no match in geo_target_constants "
                f"and would produce NULL geo_state at sync time"
            )

    if not issues:
        log.info("  ✓ geo_target_constants is complete and ready.")
        log.info("  ✓ All live geo IDs resolve to known constants.")
        log.info("  ✓ Safe to proceed with campaign_performance_geo_daily build.")
    else:
        log.warning(f"  {len(issues)} issue(s) found — resolve before building geo sync:")
        for i, issue in enumerate(issues, 1):
            log.warning(f"  [{i}] {issue}")
        log.warning("")
        log.warning("  Recommended fix: re-run kw_db_scripts/02_sync_keyword_structure.py")
        log.warning("  The geo bootstrap will refresh geo_target_constants from the API.")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Diagnose geo_target_constants coverage for campaign geo sync."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--all-accounts",
        action="store_true",
        help="Run live API geo check against all 5 accounts (slower, most thorough).",
    )
    mode.add_argument(
        "--db-only",
        action="store_true",
        help="Skip live API check. Inspect DB tables only (fastest).",
    )
    return parser.parse_args()


def main():
    args    = parse_args()
    conn    = get_db_connection()
    accounts = discover_accounts()

    log.info(f"Database : {DB_PATH}")
    log.info(f"Accounts : {len(accounts)} discovered from .env")

    # [1] Inventory
    inventory = check_constants_inventory(conn)
    if not inventory.get("table_exists") or inventory.get("total", 0) == 0:
        print_verdict(inventory, {}, [])
        conn.close()
        return

    # [2] State coverage
    coverage = check_state_coverage(conn)

    # [3] Live API check
    api_checks = []
    if not args.db_only:
        if args.all_accounts:
            log.info("")
            log.info(f"Running live API check for all {len(accounts)} accounts...")
            for account in accounts:
                result = check_live_geo_ids(conn, account)
                api_checks.append(result)
        else:
            # Default: HOME account only as representative sample
            home = next(
                (a for a in accounts if a["vertical"] == "home"),
                accounts[0],    # fallback to first account if home not found
            )
            log.info("")
            log.info(f"Running live API check for HOME account only.")
            log.info(f"Use --all-accounts for full coverage check.")
            result = check_live_geo_ids(conn, home)
            api_checks.append(result)
    else:
        log.info("")
        log.info("[3] Live API check skipped (--db-only).")

    # Verdict
    print_verdict(inventory, coverage, api_checks)

    conn.close()
    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
