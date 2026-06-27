"""
00_precheck_google_load.py
==========================
Pre-flight check before running the daily Google data refresh.
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Read-only. Does not modify the database. Run this before:
    refresh_orchestrator_google.py
    sync_all_campaigns_home.py
    03_sync_campaign_performance.py
    03_sync_keyword_performance.py
    05_sync_search_queries.py

WHAT THIS CHECKS

  1. Database file exists at the expected path.

  2. Google Ads API credentials are reachable:
        - .env file is present and loadable
        - GOOGLE_ADS_CONFIGURATION_FILE_PATH points at an existing google-ads.yaml
        - GOOGLE_ADS_MCC_ID is set
        - All five GOOGLE_ADS_CUSTOMER_ID_<vertical> env vars are set
        - GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE is set (standalone account)
     A failed live API token check is a WARN, not a BLOCKER -- the scripts
     will surface the same error with more detail if the token has expired.

  3. All five Google accounts exist in the accounts table at the expected
     platform_account_id values, with business_unit_id wired.

  4. conversion_actions is seeded for every account that has spend.
     Empty conversion_actions for an account means primary_conv_value will
     be $0 across the board even after a successful sync.

  5. Required tables exist:
        accounts, business_units
        campaigns, ad_groups
        campaign_performance_daily, campaign_performance_hourly,
        campaign_performance_geo_daily, ad_group_performance_daily
        keyword_instances, keyword_identities,
        keyword_performance_daily,
        search_queries, search_query_performance

  6. Sync recency per performance table for each account:
        MAX(date) and days-since-yesterday for each table.
        Anything > 5 days behind yesterday is flagged WARN.

  7. Spend reconciliation between campaign_performance_daily and
     keyword_performance_daily for the home account, last 30 days.
     Tolerance: 2% (looser than Microsoft because Google's offline
     conversion uploads settle for ~3 days after each refresh).

WHAT THIS DOES NOT CHECK

  - Live keyword/campaign API queries (the sync scripts do that themselves)
  - Tagging coverage on search_queries / keyword_identities (run weekly,
    not part of the daily SOP)
  - Brand registry freshness (separate cadence)

USAGE
    conda activate paid-search
    python campaign_db_scripts/00_precheck_google_load.py

    # Tighter freshness check:
    python campaign_db_scripts/00_precheck_google_load.py --stale-after 3

    # Skip live API token validation (fastest):
    python campaign_db_scripts/00_precheck_google_load.py --skip-api-check

Exit code 0 on all-green (warnings allowed).
Exit code 1 if any BLOCKER issue is found.
"""

import os
import sys
import sqlite3
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()


DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# Expected accounts. (platform_account_id, vertical_label, business_unit_id)
EXPECTED_ACCOUNTS = [
    ("8886502147", "home",       1),
    ("3863660890", "auto",       2),
    ("1006729211", "auto_quote", 3),
    ("5310820901", "medicare",   4),
    ("1671155176", "term_life",  5),
]

# env var name -> account label (for credential check)
CUSTOMER_ID_ENV_VARS = {
    "GOOGLE_ADS_CUSTOMER_ID_HOME":        "home",
    "GOOGLE_ADS_CUSTOMER_ID_AUTO":        "auto",
    "GOOGLE_ADS_CUSTOMER_ID_AUTO_QUOTE":  "auto_quote",
    "GOOGLE_ADS_CUSTOMER_ID_MEDICARE":    "medicare",
    "GOOGLE_ADS_CUSTOMER_ID_TERM_LIFE":   "term_life",
}

# Performance tables to check freshness on
PERF_TABLES = [
    ("campaign_performance_daily",          "campaign_id"),
    ("campaign_performance_hourly",         "campaign_id"),
    ("campaign_performance_geo_daily",      "campaign_id"),
    ("ad_group_performance_daily",          "ad_group_id"),
    ("keyword_performance_daily",           "instance_id"),
    ("search_query_performance",            "instance_id"),
]


# Formatting helpers ----------------------------------------------------------

GREEN = "[OK]    "
WARN  = "[WARN]  "
FAIL  = "[FAIL]  "
INFO  = "[INFO]  "


def line(ch="-", n=70):
    print(ch * n)


def section(title: str):
    print()
    line()
    print(f"  {title}")
    line()


# Checks ---------------------------------------------------------------------

def check_db_exists() -> bool:
    if not DB_PATH.exists():
        print(f"{FAIL}Database file not found: {DB_PATH}")
        return False
    print(f"{GREEN}Database file found: {DB_PATH}")
    return True


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def check_env_credentials(do_api_check: bool) -> bool:
    """Verifies all .env credentials needed by the Google sync scripts."""
    blocker = False

    # MCC + yaml path
    mcc = os.getenv("GOOGLE_ADS_MCC_ID")
    yaml_path = os.getenv("GOOGLE_ADS_CONFIGURATION_FILE_PATH")

    if not mcc:
        print(f"{FAIL}GOOGLE_ADS_MCC_ID not set in .env")
        blocker = True
    else:
        print(f"{GREEN}GOOGLE_ADS_MCC_ID = {mcc}")

    if not yaml_path:
        print(f"{FAIL}GOOGLE_ADS_CONFIGURATION_FILE_PATH not set in .env")
        blocker = True
    elif not Path(yaml_path).exists():
        print(f"{FAIL}google-ads.yaml not found at {yaml_path}")
        blocker = True
    else:
        print(f"{GREEN}google-ads.yaml found: {yaml_path}")

    # Per-account customer IDs
    for var, label in CUSTOMER_ID_ENV_VARS.items():
        val = os.getenv(var)
        if not val:
            print(f"{FAIL}{var} not set ({label} account)")
            blocker = True
        else:
            print(f"{GREEN}{var} = {val} ({label})")

    # auto_quote login_customer_id (standalone account)
    aq_login = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE")
    if not aq_login:
        print(f"{WARN}GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE not set")
        print(f"        Required only for auto_quote syncs. Other accounts unaffected.")
    else:
        print(f"{GREEN}GOOGLE_ADS_LOGIN_CUSTOMER_ID_AUTO_QUOTE = {aq_login}")

    if blocker:
        return False

    if not do_api_check:
        print(f"{INFO}Skipping live API token check (--skip-api-check)")
        return True

    # Live API check: instantiate the client and run a trivial query against MCC.
    # We do NOT pull real data -- a syntactic error or auth failure surfaces fast.
    try:
        from google.ads.googleads.client import GoogleAdsClient
        from google.ads.googleads.errors import GoogleAdsException
        client = GoogleAdsClient.load_from_storage(yaml_path)
        ga_service = client.get_service("GoogleAdsService")
        # Minimal query against MCC: returns 1 row max
        query = "SELECT customer.id FROM customer LIMIT 1"
        list(ga_service.search(customer_id=mcc, query=query))
        print(f"{GREEN}Live API token validated against MCC {mcc}")
    except ImportError:
        print(f"{WARN}google-ads SDK not importable -- skipping live API check")
    except Exception as e:
        msg = str(e).splitlines()[0][:140]
        print(f"{WARN}Live API check failed: {msg}")
        print(f"        Token may be expired. Refresh with the OAuth flow before syncing.")
        # Treat as warn, not blocker -- the sync script will give a better error.

    return True


def check_accounts(conn) -> bool:
    rows = conn.execute("""
        SELECT id, platform_account_id, account_name, mcc_id, business_unit_id
        FROM accounts
        WHERE platform = 'google'
        ORDER BY id
    """).fetchall()

    found = {r["platform_account_id"]: r for r in rows}
    blocker = False

    for pid, label, expected_bu in EXPECTED_ACCOUNTS:
        if pid not in found:
            print(f"{FAIL}Missing accounts row: platform_account_id={pid} ({label})")
            blocker = True
            continue
        r = found[pid]
        bu_marker = ""
        if r["business_unit_id"] is None:
            bu_marker = " [WARN: business_unit_id NULL]"
            print(f"{WARN}  id={r['id']}  {pid}  {r['account_name']}  bu_id=NULL")
            print(f"        Run 00_seed_business_units.py to wire it.")
        elif r["business_unit_id"] != expected_bu:
            bu_marker = f" [WARN: bu_id={r['business_unit_id']}, expected {expected_bu}]"
            print(f"{WARN}  id={r['id']}  {pid}  {r['account_name']}  "
                  f"bu_id={r['business_unit_id']}{bu_marker}")
        else:
            print(f"{GREEN}  id={r['id']}  {pid}  {r['account_name']}  "
                  f"bu_id={r['business_unit_id']}")

    return not blocker


def check_required_tables(conn) -> bool:
    required = [
        # Core
        "accounts", "business_units",
        # Campaign
        "campaigns", "ad_groups",
        "campaign_performance_daily",
        "campaign_performance_hourly",
        "campaign_performance_geo_daily",
        "ad_group_performance_daily",
        # Keyword
        "keyword_instances", "keyword_identities",
        "keyword_performance_daily",
        # Search query
        "search_queries", "search_query_performance",
        # Routing
        "conversion_actions",
    ]
    blocker = False
    for tbl in required:
        exists = conn.execute("""
            SELECT 1 FROM sqlite_master WHERE type='table' AND name=?
        """, (tbl,)).fetchone() is not None
        if exists:
            print(f"{GREEN}{tbl}")
        else:
            print(f"{FAIL}{tbl} -- does not exist")
            blocker = True
    return not blocker


def check_conversion_actions(conn) -> bool:
    """Each account that has spend should have at least one primary
    conversion_action seeded; otherwise primary_conv_value will be 0
    after every sync."""
    rows = conn.execute("""
        SELECT a.id, a.platform_account_id, a.account_name,
               COUNT(ca.id)                        AS n_actions,
               SUM(CASE WHEN ca.include_mode='primary' THEN 1 ELSE 0 END)
                                                   AS n_primary
        FROM accounts a
        LEFT JOIN conversion_actions ca ON ca.account_id = a.id
        WHERE a.platform = 'google'
        GROUP BY a.id
        ORDER BY a.id
    """).fetchall()

    blocker = False
    for r in rows:
        if r["n_actions"] == 0:
            print(f"{FAIL}{r['account_name']:<25} -- 0 conversion_actions seeded")
            print(f"        primary_conv_value will be $0 after sync.")
            print(f"        Seed via 01_create_schema.py or seed_auto_routing.py.")
            blocker = True
        elif r["n_primary"] == 0:
            print(f"{WARN}{r['account_name']:<25} -- {r['n_actions']:>2} actions, "
                  f"0 marked 'primary'")
            print(f"        Revenue routing may be incorrect.")
        else:
            print(f"{GREEN}{r['account_name']:<25} -- {r['n_actions']:>2} actions, "
                  f"{r['n_primary']} primary")
    return not blocker


def check_freshness(conn, stale_after_days: int):
    """For each account x performance table, report MAX(date) and how
    many days behind yesterday it is."""
    yesterday = date.today() - timedelta(days=1)

    for pid, label, _ in EXPECTED_ACCOUNTS:
        acct = conn.execute("""
            SELECT id FROM accounts
            WHERE platform_account_id = ? AND platform = 'google'
        """, (pid,)).fetchone()
        if not acct:
            continue
        account_id = acct["id"]
        print()
        print(f"  {label} ({pid}):")

        # Account-scoped tables
        for tbl, _ in [
            ("campaign_performance_daily",  None),
            ("campaign_performance_hourly", None),
            ("campaign_performance_geo_daily", None),
            ("ad_group_performance_daily",  None),
        ]:
            row = conn.execute(
                f"SELECT MAX(date) AS d FROM {tbl} WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            max_d = row["d"]
            if max_d is None:
                print(f"    {WARN}{tbl:<35} no rows yet")
                continue
            try:
                lag = (yesterday - date.fromisoformat(max_d)).days
            except Exception:
                lag = -1
            tag = GREEN if 0 <= lag <= stale_after_days else WARN
            print(f"    {tag}{tbl:<35} max={max_d}  lag={lag}d behind yesterday")

        # Keyword + search-query tables join through keyword_instances
        for tbl, date_col in [
            ("keyword_performance_daily", "report_date"),
            ("search_query_performance",  "report_date"),
        ]:
            row = conn.execute(f"""
                SELECT MAX({date_col}) AS d
                FROM {tbl} t
                JOIN keyword_instances ki ON ki.id = t.instance_id
                WHERE ki.account_id = ?
            """, (account_id,)).fetchone()
            max_d = row["d"]
            if max_d is None:
                print(f"    {WARN}{tbl:<35} no rows yet")
                continue
            try:
                lag = (yesterday - date.fromisoformat(max_d)).days
            except Exception:
                lag = -1
            tag = GREEN if 0 <= lag <= stale_after_days else WARN
            print(f"    {tag}{tbl:<35} max={max_d}  lag={lag}d behind yesterday")


def check_spend_reconciliation(conn, tolerance_pct: float = 2.0):
    """Compare campaign_performance_daily total spend (last 30d) to
    keyword_performance_daily total spend, for the home account.
    Expected to match within tolerance_pct."""
    home = conn.execute("""
        SELECT id FROM accounts
        WHERE platform_account_id = '8886502147' AND platform = 'google'
    """).fetchone()
    if not home:
        return
    account_id = home["id"]

    cutoff = (date.today() - timedelta(days=31)).isoformat()

    cp = conn.execute("""
        SELECT SUM(cost_micros)/1e6 AS spend
        FROM campaign_performance_daily
        WHERE account_id = ? AND device = 'ALL' AND date >= ?
    """, (account_id, cutoff)).fetchone()["spend"] or 0.0

    kp = conn.execute("""
        SELECT SUM(kpd.cost_micros)/1e6 AS spend
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id = ? AND kpd.device = 'ALL' AND kpd.report_date >= ?
    """, (account_id, cutoff)).fetchone()["spend"] or 0.0

    print(f"  Home account, last 30d (device=ALL):")
    print(f"    campaign_performance_daily spend : ${cp:>14,.2f}")
    print(f"    keyword_performance_daily spend  : ${kp:>14,.2f}")

    if cp == 0:
        print(f"  {WARN}No campaign-level spend found -- can't reconcile.")
        return

    diff_pct = abs(cp - kp) / cp * 100.0
    if diff_pct <= tolerance_pct:
        print(f"  {GREEN}Divergence: {diff_pct:.2f}% (tolerance {tolerance_pct}%)")
    else:
        print(f"  {WARN}Divergence: {diff_pct:.2f}% (exceeds {tolerance_pct}% tolerance)")
        print(f"        Likely cause: keyword performance behind campaign performance.")
        print(f"        Re-run sync_all_campaigns_home.py to catch up.")


def print_next_steps():
    print()
    line()
    print("  NEXT STEPS (assuming no BLOCKERs above)")
    line()
    print()
    print("  Daily refresh (Mon-Fri, or Mon morning to cover Fri-Sun):")
    print("    python campaign_db_scripts/refresh_orchestrator_google.py --dry-run")
    print("    python campaign_db_scripts/refresh_orchestrator_google.py")
    print()
    print("  Manual single-account refresh (e.g., medicare):")
    print("    python kw_db_scripts/03_sync_keyword_performance.py "
          "--account medicare --refresh 14")
    print()
    print("  Weekly (after daily completes -- Mon morning recommended):")
    print("    python campaign_db_scripts/04_parse_campaign_names.py")
    print("    python kw_db_scripts/08_tag_search_queries.py")
    print("    python kw_db_scripts/09_tag_keyword_identities.py")


# Main -----------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Pre-flight check for Google data refresh.")
    p.add_argument("--stale-after", type=int, default=5,
                   help="Days behind yesterday to flag as stale (default 5).")
    p.add_argument("--skip-api-check", action="store_true",
                   help="Skip the live API token validation (faster).")
    args = p.parse_args()

    print()
    line("=")
    print("  Google data refresh pre-check")
    print(f"  Database  : {DB_PATH}")
    print(f"  Yesterday : {(date.today() - timedelta(days=1)).isoformat()}")
    print(f"  Stale-after: {args.stale_after} days")
    line("=")

    if not check_db_exists():
        return 1

    conn = get_connection()
    blocker = False

    section("1. .env credentials and Google Ads API")
    if not check_env_credentials(do_api_check=not args.skip_api_check):
        blocker = True

    section("2. Google accounts wired to business units")
    if not check_accounts(conn):
        blocker = True

    section("3. Required tables exist")
    if not check_required_tables(conn):
        blocker = True

    section("4. conversion_actions seeded per account")
    if not check_conversion_actions(conn):
        blocker = True

    section("5. Sync recency per account x table")
    check_freshness(conn, args.stale_after)

    section("6. Spend reconciliation (home, last 30d)")
    check_spend_reconciliation(conn)

    conn.close()

    if blocker:
        print()
        line("=")
        print(f"  {FAIL.strip()}: One or more BLOCKER issues found above.")
        print(f"  Resolve them before running the daily refresh.")
        line("=")
        return 1

    print_next_steps()
    print()
    line("=")
    print(f"  {GREEN.strip()}: All checks passed.")
    line("=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
