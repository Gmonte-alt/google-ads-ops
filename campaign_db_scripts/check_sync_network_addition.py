"""
check_sync_network_addition.py
==============================
Drop-in section for campaign_db_scripts/check_sync.py

The existing check_sync.py reports row counts, date ranges, and spend
totals for campaigns, ad_groups, campaign_performance_daily (filtered
to device='ALL'), campaign_performance_hourly, campaign_performance_geo_daily,
keyword_performance_daily, and search_query_performance.

This file adds a parallel section for campaign_performance_network_daily
(device='ALL') — the Microsoft-specific table that stores Audience
Network spend and any other non-search network rows.

INTEGRATION
    Option A — paste the print_campaign_performance_network_daily()
    function body into check_sync.py alongside the existing section
    for campaign_performance_daily, and call it from the same place.

    Option B — run this file standalone as a spot check after running
    09_load_microsoft_campaign_performance.py. Useful before formally
    merging the addition into check_sync.py.

Style deliberately mirrors the existing check_sync.py sections:
    - Account / Acct ID columns, underscored column separator row
    - Per-account aggregation joined via accounts.id (INTEGER FK)
    - device='ALL' filter to avoid summing device breakdowns
    - Dollar spend computed from cost_micros / 1e6
    - One extra column — Network — because this table is segmented
      by network where the search table is not
"""

import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def print_campaign_performance_network_daily(conn):
    """
    Per-account, per-network summary of campaign_performance_network_daily
    (device='ALL' rows only). Mirrors the formatting of the
    campaign_performance_daily section in check_sync.py.

    Expected output shape:

        Account              Network              Min date     Max date        Rows        Spend         Impr   Clicks
        -------------------- -------------------- ------------ ------------ ------- ------------ ------------ --------
        Home Insurance       Audience             2026-01-01   2026-04-19     3,360 $     57,216    2,103,441    5,992
    """
    print("── campaign_performance_network_daily  (device='ALL') ──────────")
    rows = conn.execute("""
        SELECT
            a.account_name,
            a.platform_account_id                             AS acct_id,
            cpnd.network,
            MIN(cpnd.date)                                    AS min_date,
            MAX(cpnd.date)                                    AS max_date,
            COUNT(*)                                          AS rows,
            SUM(cpnd.cost_micros) / 1e6                       AS spend,
            SUM(cpnd.impressions)                             AS impressions,
            SUM(cpnd.clicks)                                  AS clicks,
            SUM(cpnd.conversions)                             AS conversions
        FROM campaign_performance_network_daily cpnd
        JOIN accounts a ON cpnd.account_id = a.id
        WHERE cpnd.device = 'ALL'
        GROUP BY a.id, cpnd.network
        ORDER BY a.platform_account_id, cpnd.network
    """).fetchall()

    if not rows:
        print("  (no data yet — run 09_load_microsoft_campaign_performance.py)")
        print()
        return

    print(f"  {'Account':<20} {'Network':<20} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>7} {'Spend':>12} {'Impr':>12} {'Clicks':>8}")
    print(f"  {'-'*20} {'-'*20} {'-'*12} {'-'*12} "
          f"{'-'*7} {'-'*12} {'-'*12} {'-'*8}")

    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{(r['network'] or '')[:19]:<20} "
            f"{r['min_date'] or '—':<12} "
            f"{r['max_date'] or '—':<12} "
            f"{r['rows']:>7,} "
            f"${r['spend']:>11,.2f} "
            f"{r['impressions']:>12,} "
            f"{r['clicks']:>8,}"
        )
    print()


def print_cross_table_spend_check(conn):
    """
    Reconciliation: for each Microsoft account, show search spend
    (from campaign_performance_daily) + non-search spend (from
    campaign_performance_network_daily) and the combined total. This
    is the invariant that must match the unfiltered CSV spend total
    after any 09_load_microsoft_campaign_performance.py run.

    Only Microsoft rows are included — Google spend is search-only
    by definition (Google Display lives in different campaigns that
    never land in paid_search.db).
    """
    print("── Microsoft spend reconciliation  (device='ALL', all dates) ────")
    rows = conn.execute("""
        SELECT
            a.account_name,
            a.platform_account_id AS acct_id,
            COALESCE(search.spend, 0)     AS search_spend,
            COALESCE(network.spend, 0)    AS network_spend,
            COALESCE(search.spend, 0) + COALESCE(network.spend, 0) AS total_spend
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, SUM(cost_micros)/1e6 AS spend
            FROM campaign_performance_daily
            WHERE device = 'ALL'
            GROUP BY account_id
        ) search  ON search.account_id  = a.id
        LEFT JOIN (
            SELECT account_id, SUM(cost_micros)/1e6 AS spend
            FROM campaign_performance_network_daily
            WHERE device = 'ALL'
            GROUP BY account_id
        ) network ON network.account_id = a.id
        WHERE a.platform = 'microsoft'
        ORDER BY a.platform_account_id
    """).fetchall()

    if not rows:
        print("  (no Microsoft accounts found)")
        print()
        return

    print(f"  {'Account':<20} {'Acct ID':<12} "
          f"{'Search':>14} {'Audience/Other':>16} {'Total':>14}")
    print(f"  {'-'*20} {'-'*12} {'-'*14} {'-'*16} {'-'*14}")
    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{r['acct_id']:<12} "
            f"${r['search_spend']:>13,.2f} "
            f"${r['network_spend']:>15,.2f} "
            f"${r['total_spend']:>13,.2f}"
        )
    print()
    print("  This total must match the unfiltered Spend total of the")
    print("  campaign daily performance CSV(s) that were loaded via 09.")
    print("  If they diverge, check for unrecognized network strings in 09's log.")
    print()


def main():
    print()
    print("=" * 65)
    print("check_sync_network_addition.py")
    print(f"Database: {DB_PATH}")
    print("=" * 65)
    print()

    conn = get_db_connection()

    # Sanity: the extension table must exist
    exists = conn.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='campaign_performance_network_daily'
    """).fetchone()
    if not exists:
        print("  Table campaign_performance_network_daily does not exist.")
        print("  Run 01b_extend_campaign_tables.py first.")
        conn.close()
        return

    print_campaign_performance_network_daily(conn)
    print_cross_table_spend_check(conn)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
