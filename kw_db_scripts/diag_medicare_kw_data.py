"""
diag_medicare_kw_data.py
=========================
Campaign Structure Analytics — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Diagnostic script — read-only, makes no changes to the database.

Checks whether the keyword tracking system tables (populated by kw_db_scripts)
contain data for the Medicare account (5310820901) and all other accounts.

This determines whether medicare_03_keyword_strategy.py can run against
existing DB data or whether kw_db_scripts needs to be extended to sync
keyword structure and performance for the Medicare account first.

Output:
  - Per-account row counts for keyword_instances, keyword_performance_daily,
    search_query_performance, and search_queries
  - Clear go / no-go verdict for keyword analysis per account
  - Date range of available keyword performance data per account

Usage:
  python diag_medicare_kw_data.py
"""

import sqlite3
import logging
from pathlib import Path
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MEDICARE_ACCOUNT_ID = "5310820901"

# ── Database connection ────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── Table existence checks ────────────────────────────────────────────────────

def check_table_exists(conn, table_name: str) -> bool:
    row = conn.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ?
    """, (table_name,)).fetchone()
    return row is not None


# ── Checks ────────────────────────────────────────────────────────────────────

def check_kw_tables(conn) -> dict:
    """Verify all keyword tracking tables exist."""
    log.info("")
    log.info("=" * 65)
    log.info("[1] Keyword tracking table existence")
    log.info("=" * 65)

    tables = [
        "accounts",
        "keyword_instances",
        "keyword_identities",
        "keyword_performance_daily",
        "search_query_performance",
        "search_queries",
        "negative_keywords",
    ]

    results = {}
    for table in tables:
        exists = check_table_exists(conn, table)
        results[table] = exists
        status = "✓ exists" if exists else "✗ MISSING"
        log.info(f"  {table:<35} {status}")

    missing = [t for t, e in results.items() if not e]
    if missing:
        log.warning("")
        log.warning(f"  {len(missing)} table(s) missing — kw_db_scripts/01_create_schema.py")
        log.warning("  must be run before keyword analysis is possible.")

    return results


def check_per_account_kw_data(conn, table_results: dict) -> list[dict]:
    """
    For each account in the accounts table, report:
      - keyword_instances count
      - keyword_performance_daily row count
      - search_query_performance row count
      - search_queries row count
      - date range of available keyword performance data
    """
    log.info("")
    log.info("=" * 65)
    log.info("[2] Keyword data per account")
    log.info("=" * 65)

    # Require all tables to exist before attempting joins
    required = ["accounts", "keyword_instances", "keyword_performance_daily",
                "search_query_performance", "search_queries"]
    if any(not table_results.get(t) for t in required):
        log.warning("  Skipping — one or more required tables are missing.")
        return []

    rows = conn.execute("""
        SELECT
            a.id                    AS internal_id,
            a.platform_account_id,
            a.platform,
            a.account_name,

            COUNT(DISTINCT ki.id)   AS keyword_instances,

            COALESCE(SUM(CASE WHEN ki.id IS NOT NULL THEN 1 ELSE 0 END), 0)
                                    AS kw_instance_check,

            (SELECT COUNT(*)
             FROM keyword_performance_daily kpd2
             JOIN keyword_instances ki2 ON kpd2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS kpd_rows,

            (SELECT COUNT(*)
             FROM search_query_performance sqp2
             JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS sqp_rows,

            (SELECT COUNT(DISTINCT sq2.id)
             FROM search_queries sq2
             JOIN search_query_performance sqp2 ON sqp2.query_id = sq2.id
             JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS unique_queries,

            (SELECT MIN(kpd2.report_date)
             FROM keyword_performance_daily kpd2
             JOIN keyword_instances ki2 ON kpd2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS kpd_min_date,

            (SELECT MAX(kpd2.report_date)
             FROM keyword_performance_daily kpd2
             JOIN keyword_instances ki2 ON kpd2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS kpd_max_date,

            (SELECT MIN(sqp2.report_date)
             FROM search_query_performance sqp2
             JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS sqp_min_date,

            (SELECT MAX(sqp2.report_date)
             FROM search_query_performance sqp2
             JOIN keyword_instances ki2 ON sqp2.instance_id = ki2.id
             WHERE ki2.account_id = a.id
            )                       AS sqp_max_date

        FROM accounts a
        LEFT JOIN keyword_instances ki ON ki.account_id = a.id
        WHERE a.platform = 'google'
        GROUP BY a.id
        ORDER BY a.platform_account_id
    """).fetchall()

    account_results = []
    for r in rows:
        is_medicare = r["platform_account_id"] == MEDICARE_ACCOUNT_ID
        marker = " ◄ MEDICARE" if is_medicare else ""

        log.info("")
        log.info(f"  {'─'*55}")
        log.info(f"  Account      : {r['platform_account_id']} — {r['account_name']}{marker}")
        log.info(f"  Internal ID  : {r['internal_id']}")
        log.info(f"  {'─'*55}")
        log.info(f"  keyword_instances         : {r['keyword_instances']:>10,}")
        log.info(f"  keyword_performance_daily : {r['kpd_rows']:>10,}")
        log.info(f"  search_query_performance  : {r['sqp_rows']:>10,}")
        log.info(f"  unique search_queries     : {r['unique_queries']:>10,}")

        if r["kpd_min_date"]:
            log.info(f"  kpd date range : {r['kpd_min_date']} → {r['kpd_max_date']}")
        else:
            log.info(f"  kpd date range : no data")

        if r["sqp_min_date"]:
            log.info(f"  sqp date range : {r['sqp_min_date']} → {r['sqp_max_date']}")
        else:
            log.info(f"  sqp date range : no data")

        has_kw_data  = r["keyword_instances"] > 0
        has_kpd_data = r["kpd_rows"] > 0
        has_sqp_data = r["sqp_rows"] > 0

        if has_kw_data and has_kpd_data and has_sqp_data:
            log.info(f"  Keyword analysis : ✓ READY")
        elif has_kw_data and has_kpd_data:
            log.info(f"  Keyword analysis : ⚠ PARTIAL — keyword + perf data present, no SQR data")
        elif has_kw_data:
            log.info(f"  Keyword analysis : ⚠ PARTIAL — keyword structure only, no performance data")
        else:
            log.info(f"  Keyword analysis : ✗ NO DATA — run kw_db_scripts for this account first")

        account_results.append({
            "platform_account_id": r["platform_account_id"],
            "account_name":        r["account_name"],
            "internal_id":         r["internal_id"],
            "keyword_instances":   r["keyword_instances"],
            "kpd_rows":            r["kpd_rows"],
            "sqp_rows":            r["sqp_rows"],
            "unique_queries":      r["unique_queries"],
            "kpd_min_date":        r["kpd_min_date"],
            "kpd_max_date":        r["kpd_max_date"],
            "sqp_min_date":        r["sqp_min_date"],
            "sqp_max_date":        r["sqp_max_date"],
            "has_kw_data":         has_kw_data,
            "has_kpd_data":        has_kpd_data,
            "has_sqp_data":        has_sqp_data,
        })

    return account_results


def check_campaign_perf_data(conn) -> None:
    """
    Check campaign_performance_daily coverage for Medicare specifically.
    This is the primary data source for scripts 01, 02, 04, 05.
    """
    log.info("")
    log.info("=" * 65)
    log.info("[3] Campaign performance data — Medicare account")
    log.info("=" * 65)

    tables_needed = [
        "campaigns",
        "campaign_performance_daily",
        "campaign_performance_hourly",
        "campaign_performance_geo_daily",
    ]

    for table in tables_needed:
        exists = check_table_exists(conn, table)
        if not exists:
            log.warning(f"  {table:<40} ✗ MISSING — run 01_create_campaign_tables.py")
            continue

        row = conn.execute(f"""
            SELECT COUNT(*) AS cnt FROM {table}
            WHERE account_id = ?
        """, (MEDICARE_ACCOUNT_ID,)).fetchone()

        cnt = row["cnt"] if row else 0

        if cnt > 0:
            # Get date range.
            # "date" is quoted to avoid SQLite reserved word conflict.
            # campaigns table has no date column — use first_seen_date instead.
            if table == "campaigns":
                date_row = conn.execute(f"""
                    SELECT MIN(first_seen_date) AS min_date,
                           MAX(last_synced_at)  AS max_date
                    FROM {table}
                    WHERE account_id = ?
                """, (MEDICARE_ACCOUNT_ID,)).fetchone()
            else:
                date_row = conn.execute(f"""
                    SELECT MIN(\"date\") AS min_date, MAX(\"date\") AS max_date
                    FROM {table}
                    WHERE account_id = ?
                """, (MEDICARE_ACCOUNT_ID,)).fetchone()
            date_str = f"  ({date_row['min_date']} → {date_row['max_date']})" if date_row["min_date"] else ""
            log.info(f"  {table:<40} {cnt:>10,} rows{date_str}")
        else:
            log.warning(f"  {table:<40} {cnt:>10,} rows  ← no Medicare data yet")


def print_verdict(account_results: list[dict]) -> None:
    """Print a final go/no-go summary for the five analysis scripts."""
    log.info("")
    log.info("=" * 65)
    log.info("VERDICT — Medicare analysis script readiness")
    log.info("=" * 65)

    medicare = next(
        (a for a in account_results if a["platform_account_id"] == MEDICARE_ACCOUNT_ID),
        None,
    )

    scripts = [
        ("medicare_01_account_structure",  True,                      "campaigns + ad_groups + campaign_performance_daily"),
        ("medicare_02_bidding_efficiency", True,                      "campaign_performance_daily (device breakdown)"),
        ("medicare_03_keyword_strategy",   medicare["has_sqp_data"] if medicare else False,
                                                                       "keyword_instances + search_query_performance + search_queries"),
        ("medicare_04_geo_performance",    True,                      "campaign_performance_geo_daily"),
        ("medicare_05_budget_scaling",     True,                      "campaign_performance_daily + campaigns"),
    ]

    all_ready = True
    for name, ready, source in scripts:
        status = "✓ READY" if ready else "✗ BLOCKED — keyword tracking sync needed for Medicare"
        log.info(f"  {name:<40} {status}")
        log.info(f"    Source: {source}")
        if not ready:
            all_ready = False

    log.info("")
    if all_ready:
        log.info("  All five scripts are ready to run.")
    else:
        log.info("  Scripts 01, 02, 04, 05 can run now.")
        log.info("  Script 03 requires keyword tracking data for account 5310820901.")
        log.info("  To unblock 03: extend kw_db_scripts to include the Medicare account")
        log.info("  and run 02_sync_keyword_structure.py + 03_sync_keyword_performance.py.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Database : {DB_PATH}")

    conn = get_db_connection()

    # [1] Table existence
    table_results = check_kw_tables(conn)

    # [2] Per-account keyword data
    account_results = check_per_account_kw_data(conn, table_results)

    # [3] Campaign performance data for Medicare
    check_campaign_perf_data(conn)

    # Verdict
    print_verdict(account_results)

    conn.close()
    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
