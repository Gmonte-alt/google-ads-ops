"""
diag_campaign_tables.py
========================
Diagnostic report for the campaign and ad group database tables.

Covers:
  - Table existence and row counts
  - Date ranges (min / max) per table and per account
  - Platform breakdown (google / microsoft)
  - Account inventory with business unit wiring
  - Campaign status distribution
  - Ad group status and bid override flags
  - campaign_performance_daily coverage by account
  - Impression share data availability
  - Hourly and geo table coverage
  - Cross-check: campaigns in keyword_instances but not in campaigns table
  - What to run next if tables are empty or stale

Run from the project root:
    conda activate paid-search
    python campaign_db_scripts/diag_campaign_tables.py

No arguments needed -- reads from the database only (read-only).
"""

import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

SEP  = "─" * 68
SEP2 = "═" * 68


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_exists(cursor, table_name: str) -> bool:
    r = cursor.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return r[0] > 0


def safe_fetch(cursor, sql: str, params: tuple = ()) -> list:
    try:
        return cursor.execute(sql, params).fetchall()
    except Exception as e:
        return [{"_error": str(e)}]


def fmt_int(v) -> str:
    try:    return f"{int(v):,}"
    except: return str(v or "—")


def fmt_pct(v) -> str:
    try:    return f"{float(v)*100:.1f}%"
    except: return "—"


def days_ago(date_str) -> str:
    try:
        d   = date.fromisoformat(str(date_str))
        gap = (date.today() - d).days
        if gap == 0: return "today"
        if gap == 1: return "yesterday"
        return f"{gap}d ago"
    except:
        return "—"


def print_section(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ─────────────────────────────────────────────────────────────────────────────

def check_table_inventory(cursor):
    print_section("1. TABLE INVENTORY")

    campaign_tables = [
        "campaigns",
        "ad_groups",
        "campaign_performance_daily",
        "campaign_performance_hourly",
        "campaign_performance_geo_daily",
        "business_units",
    ]
    kw_tables = [
        "accounts",
        "keyword_identities",
        "keyword_instances",
        "keyword_performance_daily",
        "search_queries",
        "search_query_performance",
    ]

    print(f"\n  {'Table':<38} {'Exists':<8} {'Rows':>10}")
    print(f"  {'─'*38} {'─'*8} {'─'*10}")

    for tbl in campaign_tables + ["—"] + kw_tables:
        if tbl == "—":
            print()
            continue
        exists = table_exists(cursor, tbl)
        if exists:
            try:
                n = cursor.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except:
                n = "ERR"
        else:
            n = "—"
        marker = "✓" if exists and (isinstance(n, int) and n > 0) else \
                 "○" if exists else "✗"
        print(f"  {marker} {tbl:<36} {'yes' if exists else 'NO':<8} {fmt_int(n) if exists else '—':>10}")


# ─────────────────────────────────────────────────────────────────────────────

def check_accounts(cursor):
    print_section("2. ACCOUNT INVENTORY (accounts × business_units)")

    if not table_exists(cursor, "accounts"):
        print("  accounts table does not exist.")
        return

    has_bu = table_exists(cursor, "business_units")
    if has_bu:
        rows = safe_fetch(cursor, """
            SELECT
                a.platform_account_id,
                a.platform,
                a.account_name,
                a.mcc_id,
                bu.vertical,
                bu.domain,
                a.business_unit_id
            FROM accounts a
            LEFT JOIN business_units bu ON a.business_unit_id = bu.id
            ORDER BY bu.vertical, a.platform, a.platform_account_id
        """)
    else:
        rows = safe_fetch(cursor, """
            SELECT platform_account_id, platform, account_name, mcc_id,
                   NULL AS vertical, NULL AS domain, NULL AS business_unit_id
            FROM accounts
            ORDER BY platform, platform_account_id
        """)

    print(f"\n  {'platform_account_id':<18} {'platform':<12} {'vertical':<12} "
          f"{'domain':<24} {'account_name':<30} {'MCC':<14} {'BU wired'}")
    print(f"  {'─'*18} {'─'*12} {'─'*12} {'─'*24} {'─'*30} {'─'*14} {'─'*8}")

    for r in rows:
        bu_ok = "✓" if r["business_unit_id"] else "⚠ NULL"
        print(f"  {str(r['platform_account_id']):<18} "
              f"{str(r['platform']):<12} "
              f"{str(r['vertical'] or '—'):<12} "
              f"{str(r['domain'] or '—'):<24} "
              f"{str(r['account_name'] or '—')[:29]:<30} "
              f"{str(r['mcc_id'] or '—'):<14} "
              f"{bu_ok}")

    if not has_bu:
        print("\n  NOTE: business_units table not found. "
              "Run campaign_db_scripts/00_seed_business_units.py")


# ─────────────────────────────────────────────────────────────────────────────

def check_campaigns(cursor):
    print_section("3. CAMPAIGNS TABLE")

    if not table_exists(cursor, "campaigns"):
        print("  campaigns table does not exist.")
        print("  → Run: python campaign_db_scripts/01_create_campaign_tables.py")
        return

    total = cursor.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    if total == 0:
        print("  campaigns table exists but is EMPTY.")
        print("  → Run: python campaign_db_scripts/02_sync_campaigns_adgroups.py")
        return

    print(f"\n  Total campaigns: {fmt_int(total)}")

    # Per account × platform breakdown
    rows = safe_fetch(cursor, """
        SELECT
            c.account_id                                    AS platform_account_id,
            a.platform,
            bu.vertical,
            bu.domain,
            COUNT(*)                                        AS campaign_count,
            SUM(CASE WHEN c.status='ENABLED' THEN 1 ELSE 0 END)  AS enabled,
            SUM(CASE WHEN c.status='PAUSED'  THEN 1 ELSE 0 END)  AS paused,
            SUM(CASE WHEN c.status='REMOVED' THEN 1 ELSE 0 END)  AS removed,
            MIN(c.first_seen_date)                          AS min_first_seen,
            MAX(c.last_synced_at)                           AS max_synced
        FROM campaigns c
        LEFT JOIN accounts a ON a.platform_account_id = c.account_id
        LEFT JOIN business_units bu ON a.business_unit_id = bu.id
        GROUP BY c.account_id
        ORDER BY bu.vertical, a.platform
    """)

    print(f"\n  {'platform_account_id':<18} {'platform':<10} {'vertical':<12} "
          f"{'domain':<22} {'total':>6} {'enabled':>7} {'paused':>7} "
          f"{'removed':>8} {'first_seen':<12} {'last_sync':<12} {'freshness'}")
    print(f"  {'─'*18} {'─'*10} {'─'*12} {'─'*22} {'─'*6} {'─'*7} {'─'*7} "
          f"{'─'*8} {'─'*12} {'─'*12} {'─'*12}")

    for r in rows:
        print(f"  {str(r['platform_account_id'] or '—'):<18} "
              f"{str(r['platform'] or '—'):<10} "
              f"{str(r['vertical'] or '—'):<12} "
              f"{str(r['domain'] or '—'):<22} "
              f"{fmt_int(r['campaign_count']):>6} "
              f"{fmt_int(r['enabled']):>7} "
              f"{fmt_int(r['paused']):>7} "
              f"{fmt_int(r['removed']):>8} "
              f"{str(r['min_first_seen'] or '—'):<12} "
              f"{str(r['max_synced'] or '—')[:10]:<12} "
              f"{days_ago(str(r['max_synced'] or '')[:10])}")

    # Naming convention flags
    flags = cursor.execute(
        "SELECT COUNT(*) FROM campaigns WHERE naming_convention_flag = 1"
    ).fetchone()[0]
    parsed = cursor.execute(
        "SELECT COUNT(*) FROM campaigns WHERE parsed_product_type IS NOT NULL"
    ).fetchone()[0]
    print(f"\n  Naming convention flags  : {fmt_int(flags)} campaigns flagged")
    print(f"  Parsed dimensions filled : {fmt_int(parsed)} campaigns "
          f"(run 04_parse_campaign_names.py if 0)")


# ─────────────────────────────────────────────────────────────────────────────

def check_ad_groups(cursor):
    print_section("4. AD GROUPS TABLE")

    if not table_exists(cursor, "ad_groups"):
        print("  ad_groups table does not exist.")
        return

    total = cursor.execute("SELECT COUNT(*) FROM ad_groups").fetchone()[0]
    if total == 0:
        print("  ad_groups table exists but is EMPTY.")
        print("  → Run: python campaign_db_scripts/02_sync_campaigns_adgroups.py")
        return

    print(f"\n  Total ad groups: {fmt_int(total)}")

    rows = safe_fetch(cursor, """
        SELECT
            ag.account_id                                         AS platform_account_id,
            a.platform,
            bu.vertical,
            COUNT(*)                                              AS ag_count,
            SUM(CASE WHEN ag.status='ENABLED' THEN 1 ELSE 0 END) AS enabled,
            SUM(CASE WHEN ag.status='PAUSED'  THEN 1 ELSE 0 END) AS paused,
            SUM(CASE WHEN ag.status='REMOVED' THEN 1 ELSE 0 END) AS removed,
            SUM(ag.bid_override_flag)                             AS bid_overrides,
            MIN(ag.first_seen_date)                               AS min_first_seen,
            MAX(ag.last_synced_at)                                AS max_synced
        FROM ad_groups ag
        LEFT JOIN accounts a ON a.platform_account_id = ag.account_id
        LEFT JOIN business_units bu ON a.business_unit_id = bu.id
        GROUP BY ag.account_id
        ORDER BY bu.vertical, a.platform
    """)

    print(f"\n  {'platform_account_id':<18} {'platform':<10} {'vertical':<12} "
          f"{'total':>6} {'enabled':>7} {'paused':>7} {'removed':>8} "
          f"{'bid_ovr':>8} {'first_seen':<12} {'last_sync':<12} {'freshness'}")
    print(f"  {'─'*18} {'─'*10} {'─'*12} {'─'*6} {'─'*7} {'─'*7} "
          f"{'─'*8} {'─'*8} {'─'*12} {'─'*12} {'─'*12}")

    for r in rows:
        print(f"  {str(r['platform_account_id'] or '—'):<18} "
              f"{str(r['platform'] or '—'):<10} "
              f"{str(r['vertical'] or '—'):<12} "
              f"{fmt_int(r['ag_count']):>6} "
              f"{fmt_int(r['enabled']):>7} "
              f"{fmt_int(r['paused']):>7} "
              f"{fmt_int(r['removed']):>8} "
              f"{fmt_int(r['bid_overrides']):>8} "
              f"{str(r['min_first_seen'] or '—'):<12} "
              f"{str(r['max_synced'] or '—')[:10]:<12} "
              f"{days_ago(str(r['max_synced'] or '')[:10])}")

    if any(r['bid_overrides'] for r in rows if isinstance(r['bid_overrides'], int)
           and r['bid_overrides'] > 0):
        print(f"\n  ⚠ Bid overrides detected. Ad groups with non-null target_cpa or "
              f"target_roas may drift from campaign strategy.")


# ─────────────────────────────────────────────────────────────────────────────

def check_campaign_performance(cursor):
    print_section("5. CAMPAIGN_PERFORMANCE_DAILY — DATE COVERAGE")

    if not table_exists(cursor, "campaign_performance_daily"):
        print("  campaign_performance_daily does not exist.")
        return

    total = cursor.execute(
        "SELECT COUNT(*) FROM campaign_performance_daily"
    ).fetchone()[0]
    if total == 0:
        print("  campaign_performance_daily is EMPTY.")
        print("  → Run: python campaign_db_scripts/03_sync_campaign_performance.py "
              "--daily-start YYYY-MM-DD")
        return

    rows = safe_fetch(cursor, """
        SELECT
            cpd.account_id                      AS platform_account_id,
            a.platform,
            bu.vertical,
            bu.domain,
            COUNT(DISTINCT cpd.date)            AS date_count,
            MIN(cpd.date)                       AS min_date,
            MAX(cpd.date)                       AS max_date,
            COUNT(*)                            AS row_count,
            SUM(cpd.impressions)                AS total_impressions,
            SUM(cpd.clicks)                     AS total_clicks,
            SUM(cpd.cost_micros) / 1e6          AS total_spend,
            SUM(cpd.conversion_value)           AS total_revenue,
            -- Impression share availability
            SUM(CASE WHEN cpd.search_impression_share IS NOT NULL
                     THEN 1 ELSE 0 END)         AS rows_with_is,
            AVG(cpd.search_impression_share)    AS avg_is
        FROM campaign_performance_daily cpd
        LEFT JOIN accounts a ON a.platform_account_id = cpd.account_id
        LEFT JOIN business_units bu ON a.business_unit_id = bu.id
        WHERE cpd.device = 'ALL'
        GROUP BY cpd.account_id
        ORDER BY bu.vertical, a.platform
    """)

    today = date.today()

    print(f"\n  {'platform_account_id':<18} {'platform':<10} {'vertical':<12} "
          f"{'min_date':<12} {'max_date':<12} {'freshness':<12} "
          f"{'date_cnt':>8} {'rows':>10} {'revenue':>12} {'avg_IS':>8}")
    print(f"  {'─'*18} {'─'*10} {'─'*12} {'─'*12} {'─'*12} {'─'*12} "
          f"{'─'*8} {'─'*10} {'─'*12} {'─'*8}")

    for r in rows:
        try:
            gap  = (today - date.fromisoformat(str(r['max_date']))).days
            flag = "⚠ STALE" if gap > 3 else ""
        except:
            gap, flag = None, ""

        print(f"  {str(r['platform_account_id'] or '—'):<18} "
              f"{str(r['platform'] or '—'):<10} "
              f"{str(r['vertical'] or '—'):<12} "
              f"{str(r['min_date']):<12} "
              f"{str(r['max_date']):<12} "
              f"{days_ago(str(r['max_date'])):<12} "
              f"{fmt_int(r['date_count']):>8} "
              f"{fmt_int(r['row_count']):>10} "
              f"${float(r['total_revenue'] or 0):>11,.2f} "
              f"{fmt_pct(r['avg_is']):>8}  {flag}")

    # IS availability detail
    total_rows = cursor.execute(
        "SELECT COUNT(*) FROM campaign_performance_daily WHERE device='ALL'"
    ).fetchone()[0]
    rows_with_is = cursor.execute(
        "SELECT COUNT(*) FROM campaign_performance_daily "
        "WHERE device='ALL' AND search_impression_share IS NOT NULL"
    ).fetchone()[0]
    pct_is = rows_with_is / total_rows * 100 if total_rows > 0 else 0
    print(f"\n  Impression share populated: {fmt_int(rows_with_is)} / "
          f"{fmt_int(total_rows)} rows ({pct_is:.1f}%)")


# ─────────────────────────────────────────────────────────────────────────────

def check_hourly_geo(cursor):
    print_section("6. HOURLY & GEO PERFORMANCE TABLES")

    for tbl, label in [
        ("campaign_performance_hourly",     "Hourly"),
        ("campaign_performance_geo_daily",  "Geo daily"),
    ]:
        if not table_exists(cursor, tbl):
            print(f"  {label}: table does not exist")
            continue

        rows = safe_fetch(cursor, f"""
            SELECT
                t.account_id                 AS platform_account_id,
                a.platform,
                bu.vertical,
                COUNT(DISTINCT date)         AS date_count,
                MIN(date)                    AS min_date,
                MAX(date)                    AS max_date,
                COUNT(*)                     AS row_count
            FROM {tbl} t
            LEFT JOIN accounts a ON a.platform_account_id = t.account_id
            LEFT JOIN business_units bu ON a.business_unit_id = bu.id
            GROUP BY t.account_id
        """)

        if not rows or (len(rows) == 1 and "_error" in dict(rows[0])):
            print(f"  {label}: query error or no data")
            continue

        total_rows = sum(r['row_count'] for r in rows)
        if total_rows == 0:
            print(f"  {label}: empty (0 rows)")
            continue

        print(f"\n  {label}:")
        for r in rows:
            print(f"    {str(r['platform_account_id'] or '—'):<18} "
                  f"{str(r['platform'] or '—'):<10} "
                  f"{str(r['vertical'] or '—'):<12} "
                  f"{str(r['min_date']):<12} → {str(r['max_date']):<12} "
                  f"({days_ago(str(r['max_date']))})  "
                  f"{fmt_int(r['row_count'])} rows")


# ─────────────────────────────────────────────────────────────────────────────

def check_keyword_campaign_crosswalk(cursor):
    print_section("7. KEYWORD → CAMPAIGN CROSSWALK")
    print("  Campaigns active in keyword_instances vs campaigns table.")
    print("  Gaps = campaigns that need 02_sync_campaigns_adgroups.py to run.\n")

    if not table_exists(cursor, "keyword_instances"):
        print("  keyword_instances not found.")
        return

    # Campaigns known in keyword_instances (always populated by kw sync)
    ki_camps = safe_fetch(cursor, """
        SELECT DISTINCT
            ki.campaign_id,
            ki.campaign_name,
            a.platform,
            a.platform_account_id,
            COUNT(DISTINCT ki.id)       AS keyword_count
        FROM keyword_instances ki
        JOIN accounts a ON ki.account_id = a.id
        GROUP BY ki.campaign_id, a.platform_account_id
        ORDER BY a.platform_account_id, ki.campaign_name
    """)

    has_camp_tbl = table_exists(cursor, "campaigns")
    camp_ids_in_db = set()
    if has_camp_tbl:
        rows = cursor.execute("SELECT campaign_id FROM campaigns").fetchall()
        camp_ids_in_db = {r['campaign_id'] for r in rows}

    print(f"  {'campaign_id':<25} {'platform_account_id':<18} {'platform':<10} "
          f"{'in campaigns?':<15} {'kw_count':>8}  {'campaign_name'}")
    print(f"  {'─'*25} {'─'*18} {'─'*10} {'─'*15} {'─'*8}  {'─'*40}")

    missing_count = 0
    for r in ki_camps:
        in_tbl = r['campaign_id'] in camp_ids_in_db if has_camp_tbl else False
        marker = "✓" if in_tbl else "⚠ missing"
        if not in_tbl:
            missing_count += 1
        print(f"  {str(r['campaign_id'] or '—'):<25} "
              f"{str(r['platform_account_id'] or '—'):<18} "
              f"{str(r['platform'] or '—'):<10} "
              f"{marker:<15} "
              f"{fmt_int(r['keyword_count']):>8}  "
              f"{str(r['campaign_name'] or '—')[:50]}")

    if missing_count > 0:
        print(f"\n  ⚠ {missing_count} campaign(s) in keyword data but not in campaigns table.")
        print("  → Run: python campaign_db_scripts/02_sync_campaigns_adgroups.py")
    else:
        print(f"\n  ✓ All keyword campaigns are present in campaigns table.")


# ─────────────────────────────────────────────────────────────────────────────

def check_keyword_performance_dates(cursor):
    print_section("8. KEYWORD & SEARCH QUERY PERFORMANCE — DATE COVERAGE")

    for tbl, date_col, label in [
        ("keyword_performance_daily",   "report_date", "KW performance daily"),
        ("search_query_performance",    "report_date", "Search query performance"),
    ]:
        if not table_exists(cursor, tbl):
            print(f"  {label}: table not found")
            continue

        rows = safe_fetch(cursor, f"""
            SELECT
                a.platform,
                a.platform_account_id,
                bu.vertical,
                MIN(t.{date_col})                   AS min_date,
                MAX(t.{date_col})                   AS max_date,
                COUNT(DISTINCT t.{date_col})        AS date_count,
                COUNT(*)                            AS row_count,
                SUM(t.primary_conv_value)           AS total_revenue,
                SUM(t.cost_micros) / 1e6            AS total_spend
            FROM {tbl} t
            JOIN keyword_instances ki ON t.instance_id = ki.id
            JOIN accounts a ON ki.account_id = a.id
            LEFT JOIN business_units bu ON a.business_unit_id = bu.id
            WHERE t.device = 'ALL'
            GROUP BY ki.account_id
            ORDER BY bu.vertical, a.platform
        """)

        today = date.today()
        print(f"\n  {label}:")
        print(f"  {'platform_account_id':<18} {'platform':<10} {'vertical':<12} "
              f"{'min_date':<12} {'max_date':<12} {'freshness':<12} "
              f"{'dates':>6} {'rows':>10} {'revenue':>12}")
        print(f"  {'─'*18} {'─'*10} {'─'*12} {'─'*12} {'─'*12} {'─'*12} "
              f"{'─'*6} {'─'*10} {'─'*12}")

        for r in rows:
            try:
                gap  = (today - date.fromisoformat(str(r['max_date']))).days
                flag = " ⚠ STALE" if gap > 3 else ""
            except:
                flag = ""
            print(f"  {str(r['platform_account_id'] or '—'):<18} "
                  f"{str(r['platform'] or '—'):<10} "
                  f"{str(r['vertical'] or '—'):<12} "
                  f"{str(r['min_date']):<12} "
                  f"{str(r['max_date']):<12} "
                  f"{days_ago(str(r['max_date'])):<12} "
                  f"{fmt_int(r['date_count']):>6} "
                  f"{fmt_int(r['row_count']):>10} "
                  f"${float(r['total_revenue'] or 0):>11,.2f}{flag}")


# ─────────────────────────────────────────────────────────────────────────────

def print_next_steps(cursor):
    print_section("9. RECOMMENDED NEXT STEPS")

    steps = []

    # Check what's missing
    if not table_exists(cursor, "business_units") or \
       cursor.execute("SELECT COUNT(*) FROM business_units").fetchone()[0] == 0:
        steps.append("1. python campaign_db_scripts/00_seed_business_units.py")

    if not table_exists(cursor, "campaigns") or \
       cursor.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0:
        steps.append("2. python campaign_db_scripts/01_create_campaign_tables.py")
        steps.append("3. python campaign_db_scripts/02_sync_campaigns_adgroups.py")

    if not table_exists(cursor, "campaign_performance_daily") or \
       cursor.execute(
           "SELECT COUNT(*) FROM campaign_performance_daily"
       ).fetchone()[0] == 0:
        steps.append(
            "4. python campaign_db_scripts/03_sync_campaign_performance.py "
            "--daily-start 2025-01-01 --hourly-days 90"
        )

    if table_exists(cursor, "campaigns") and \
       cursor.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] > 0 and \
       cursor.execute(
           "SELECT COUNT(*) FROM campaigns WHERE parsed_product_type IS NOT NULL"
       ).fetchone()[0] == 0:
        steps.append("5. python campaign_db_scripts/04_parse_campaign_names.py")

    # Check keyword data staleness
    if table_exists(cursor, "keyword_performance_daily"):
        max_kw = cursor.execute(
            "SELECT MAX(report_date) FROM keyword_performance_daily"
        ).fetchone()[0]
        if max_kw:
            gap = (date.today() - date.fromisoformat(max_kw)).days
            if gap > 3:
                steps.append(
                    f"6. python kw_db_scripts/03_sync_keyword_performance.py "
                    f"--refresh {gap + 2}  (keyword data is {gap}d stale)"
                )
                steps.append(
                    f"7. python kw_db_scripts/05_sync_search_queries.py "
                    f"--refresh {gap + 2}"
                )

    if not steps:
        print("\n  ✓ All tables are present and appear up to date.")
        print("  Ready to run: python analysis/analysis_campaign_keyword_roas.py")
    else:
        print("\n  Run in this order:")
        for s in steps:
            print(f"    {s}")


# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{SEP2}")
    print("  Campaign & Ad Group Database Diagnostic")
    print(f"  Database : {DB_PATH}")
    print(f"  Run date : {date.today()}")
    print(SEP2)

    if not DB_PATH.exists():
        print(f"\n  ERROR: Database not found at {DB_PATH}")
        print("  Run kw_db_scripts/01_create_schema.py first.")
        return

    conn   = get_conn()
    cursor = conn.cursor()

    check_table_inventory(cursor)
    check_accounts(cursor)
    check_campaigns(cursor)
    check_ad_groups(cursor)
    check_campaign_performance(cursor)
    check_hourly_geo(cursor)
    check_keyword_campaign_crosswalk(cursor)
    check_keyword_performance_dates(cursor)
    print_next_steps(cursor)

    conn.close()
    print(f"\n{SEP2}\n  Done.\n{SEP2}\n")


if __name__ == "__main__":
    main()
